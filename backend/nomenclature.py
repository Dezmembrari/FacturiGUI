import os
import re
import sqlite3
import time
import unicodedata
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from enum import Enum
from pathlib import Path
from typing import cast

DEFAULT_DB_PATH = str(
    Path(os.environ.get("NOMENCLATURE_DB", Path(__file__).with_name("nomenclature.db")))
)
NAME_SIMILARITY_THRESHOLD = 0.5
MIN_EXTENSION_LEN = 5
FUZZY_CODE_THRESHOLD = 0.85
MAX_SUGGESTIONS = 6
_CONFUSABLE = str.maketrans("OISBZ", "01582")

DEFAULT_MODEL_POOL: list[str] = [
    "gemini-2.5-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-3.6-flash",
]

_BRAND_SUFFIX_RE = re.compile(
    r"^([A-Z0-9.\-/]{4,})\s+(FE|FEB|LMI|LM|TRW|BOS|ATE|KYB|SAC|LUK|VAL|INA|SKF|FAG|DAY|ELR|NRF|TYC|PRA|PL|TO|HP|TX|EL|MD|QC|BCO|SIKA|OE|OEM)$",
    re.IGNORECASE,
)
_ATTACHED_BRAND_RE = re.compile(
    r"^(\d{4,8})(FE|FEB|LMI|LM|TRW|BOS|ATE|KYB|SAC|LUK|VAL|INA|SKF|FAG|DAY|ELR|NRF|TYC|PRA|PL|TO|HP|TX|EL|MD|QC|BCO|SIKA|OE|OEM)$",
    re.IGNORECASE,
)

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS parts (
        part_code TEXT NOT NULL PRIMARY KEY,
        part_name TEXT NOT NULL,
        nr_crt INTEGER NOT NULL DEFAULT 0,
        stoc REAL NOT NULL DEFAULT 0.0,
        sold REAL NOT NULL DEFAULT 0.0,
        last_used TEXT
    );
    CREATE TABLE IF NOT EXISTS exports_log (
        doc_key TEXT NOT NULL PRIMARY KEY,
        supplier_name TEXT NOT NULL,
        document_code TEXT NOT NULL,
        winmentor_doc_nr TEXT NOT NULL,
        quote_date TEXT NOT NULL,
        total_fara_tva REAL NOT NULL,
        archive_dir TEXT NOT NULL,
        exported_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS model_stats (
        model_name TEXT NOT NULL PRIMARY KEY,
        success_count INTEGER NOT NULL DEFAULT 0,
        rate_limit_429_count INTEGER NOT NULL DEFAULT 0,
        unavailable_503_count INTEGER NOT NULL DEFAULT 0,
        other_error_count INTEGER NOT NULL DEFAULT 0,
        avg_latency_ms REAL NOT NULL DEFAULT 0.0,
        cooldown_until REAL NOT NULL DEFAULT 0.0,
        last_used TEXT
    );
"""


class PartStatus(Enum):
    NEW = "NEW"
    MATCH = "MATCH"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class CodeSuggestion:
    code: str
    name: str
    reason: str
    score: float
    stoc: float = 0.0


@dataclass(frozen=True)
class PartResolution:
    status: PartStatus
    code: str
    name: str
    matched_via: str
    stoc: float = 0.0
    sold: float = 0.0
    suggestions: list[CodeSuggestion] = field(default_factory=list[CodeSuggestion])


@dataclass(frozen=True)
class PartRecord:
    nr_crt: int
    part_code: str
    part_name: str
    stoc: float
    sold: float
    last_used: str | None


@dataclass(frozen=True)
class ExportLogEntry:
    doc_key: str
    supplier_name: str
    document_code: str
    winmentor_doc_nr: str
    quote_date: str
    total_fara_tva: float
    archive_dir: str
    exported_at: str


@dataclass(frozen=True)
class ModelStatEntry:
    model_name: str
    success_count: int
    rate_limit_429_count: int
    unavailable_503_count: int
    other_error_count: int
    avg_latency_ms: float
    in_cooldown: bool
    cooldown_remaining_s: int
    last_used: str | None


# --- Code & Name Normalization -----------------------------------------------------------

def strip_supplier_suffix(code: str) -> str:
    primary = re.split(r"\s+[-–—/]\s+", code.strip(), maxsplit=1)[0]
    return primary.strip()


def normalize_code(code: str) -> str:
    return re.sub(r"\s+", " ", code).strip().upper()


def compact_code(code: str) -> str:
    decomposed = unicodedata.normalize("NFKD", code).upper()
    return re.sub(r"[^A-Z0-9]", "", decomposed)


def strip_distributor_brand_suffix(code: str) -> str:
    norm = normalize_code(strip_supplier_suffix(code))
    m_space = _BRAND_SUFFIX_RE.match(norm)
    if m_space:
        return compact_code(m_space.group(1))
    comp = compact_code(norm)
    m_att = _ATTACHED_BRAND_RE.match(comp)
    if m_att:
        return m_att.group(1)
    return comp


def is_valid_part_name(name: str) -> bool:
    cleaned = name.strip().upper()
    if not cleaned or cleaned in {"0", "0.0", "0,00", "NONE", "NULL", "NAN", "-"}:
        return False
    return any(c.isalpha() for c in cleaned)


def normalize_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip().casefold()


_SYNONYMS: dict[str, str] = {
    "stg": "stanga",
    "st": "stanga",
    "dr": "dreapta",
    "drp": "dreapta",
    "ant": "fata",
    "post": "spate",
    "sup": "superior",
    "inf": "inferior",
    "int": "interior",
    "ext": "exterior",
}

_STOP_TOKENS: set[str] = {
    "de", "la", "cu", "pe", "in", "pt", "pentru", "buc", "set",
    "1l", "1lt", "5l", "4l", "200ml", "400ml", "100ml", "500ml",
}

_OPPOSITES: list[tuple[str, str]] = [
    ("fata", "spate"),
    ("stanga", "dreapta"),
    ("interior", "exterior"),
    ("superior", "inferior"),
    ("sus", "jos"),
]


def _word_list(name: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", normalize_name(name))


def _tokens(name: str) -> set[str]:
    return {
        _SYNONYMS.get(w, w)
        for w in _word_list(name)
        if len(w) >= 2 and w not in _STOP_TOKENS
    }


def _has_opposite_conflict(a: set[str], b: set[str]) -> bool:
    for x, y in _OPPOSITES:
        if (x in a and y in b and y not in a and x not in b) or (
            y in a and x in b and x not in a and y not in b
        ):
            return True
    return False


def names_match(
    quote_name: str, canonical_name: str, threshold: float = NAME_SIMILARITY_THRESHOLD
) -> bool:
    if not is_valid_part_name(canonical_name):
        return False
    a, b = _tokens(quote_name), _tokens(canonical_name)
    if not a or not b:
        return a == b
    if _has_opposite_conflict(a, b):
        return False
    if a <= b or b <= a:
        return True
    return len(a & b) / len(a | b) >= threshold


@contextmanager
def _connect(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        with conn:
            conn.executescript(_SCHEMA)
            columns = {cast(str, row[1]) for row in conn.execute("PRAGMA table_info(parts)")}
            if "last_used" not in columns:
                conn.execute("ALTER TABLE parts ADD COLUMN last_used TEXT")
            if "nr_crt" not in columns:
                conn.execute("ALTER TABLE parts ADD COLUMN nr_crt INTEGER NOT NULL DEFAULT 0")
            if "stoc" not in columns:
                conn.execute("ALTER TABLE parts ADD COLUMN stoc REAL NOT NULL DEFAULT 0.0")
            if "sold" not in columns:
                conn.execute("ALTER TABLE parts ADD COLUMN sold REAL NOT NULL DEFAULT 0.0")
            conn.execute("DELETE FROM parts WHERE TRIM(part_name) IN ('0', '0.0', '')")
            yield conn
    finally:
        conn.close()


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    with _connect(db_path):
        pass


def _load_rows(conn: sqlite3.Connection) -> list[tuple[str, str, float, float, str, str]]:
    rows = cast(
        list[tuple[str, str, float, float]],
        conn.execute("SELECT part_code, part_name, stoc, sold FROM parts").fetchall(),
    )
    return [
        (
            c,
            n,
            float(st or 0.0),
            float(so or 0.0),
            compact_code(c),
            strip_distributor_brand_suffix(c),
        )
        for c, n, st, so in rows
    ]


def _extract_description_code_candidates(quote_code: str, quote_name: str) -> list[CodeSuggestion]:
    """
    For Sprint Car / bodyshop lines like 'CATALIZ ACRIL NORMAL HS C15/200ML' or
    'LAC H79 SPRINT AIR WIDE/1LT', extracts candidate codes from the description itself
    (e.g., primary category noun 'CATALIZ' / 'LAC' and model token 'C15' / 'H79')
    so the operator can switch between them in 1 click.
    """
    norm_c = normalize_code(quote_code)
    raw_words = [
        w.upper()
        for w in re.findall(r"[A-Z0-9]+", unicodedata.normalize("NFKD", quote_name).upper())
    ]
    if not raw_words:
        return []

    candidates: list[CodeSuggestion] = []
    first_noun = raw_words[0]
    if len(first_noun) >= 3 and first_noun.isalpha() and first_noun != norm_c:
        candidates.append(
            CodeSuggestion(
                code=first_noun,
                name=quote_name.strip().upper(),
                reason="category_noun_code",
                score=0.88,
                stoc=0.0,
            )
        )

    # Look for short alphanumeric model codes inside the description (e.g. C15, H79, F91, CL500)
    ignore_units = {"1L", "1LT", "2L", "4L", "5L", "20L", "100ML", "200ML", "400ML", "500ML", "1KG", "2KG", "2K", "1K"}
    for w in raw_words[1:]:
        if (
            w != norm_c
            and w not in ignore_units
            and 2 <= len(w) <= 8
            and any(ch.isdigit() for ch in w)
            and any(ch.isalpha() for ch in w)
        ):
            candidates.append(
                CodeSuggestion(
                    code=w,
                    name=quote_name.strip().upper(),
                    reason="embedded_model_code",
                    score=0.86,
                    stoc=0.0,
                )
            )
    return candidates


def _suggest(
    rows: list[tuple[str, str, float, float, str, str]],
    code: str,
    quote_name: str,
    limit: int,
) -> list[CodeSuggestion]:
    """
    Suggests stored WinMentor articles based on:
    1. Code similarity (separators, brand suffix, prefix/extension, fuzzy OCR chars).
    2. Stored Article Name similarity (exact/half word overlap, e.g. 'CATALIZ ACRIL NORMAL HS C15/200ML' -> 'CATALIZ').
    3. Embedded description candidates for Sprint/paint quotes (e.g. 'LAC' vs 'H79').
    """
    norm = normalize_code(code)
    key = compact_code(code)
    root_in = strip_distributor_brand_suffix(code)
    in_words = norm.split()
    in_first = compact_code(in_words[0]) if in_words else ""
    q_tokens = _tokens(quote_name)
    q_word_list = _word_list(quote_name)
    q_first_word = q_word_list[0] if q_word_list else ""

    found: list[CodeSuggestion] = []
    seen_codes: set[str] = set()

    for stored, name, stoc, _sold, skey, sroot in rows:
        if stored == norm or not skey:
            continue
        st_words = stored.split()
        reason, score = "", 0.0
        shorter, longer = sorted((len(key), len(skey)))

        # 1. Code-based matching
        if key and skey == key:
            reason, score = "same_code_different_separators", 1.0
        elif root_in and len(root_in) >= 4 and root_in == sroot:
            reason, score = "same_root_sku_brand_suffix", 0.95
        elif key and len(st_words) > 1 and compact_code(st_words[0]) == key:
            reason, score = "stored_code_has_extra_text", 0.9
        elif key and len(in_words) > 1 and len(skey) >= 4 and in_first == skey:
            reason, score = "incoming_code_has_extra_text", 0.9
        elif key and shorter >= MIN_EXTENSION_LEN and (skey.startswith(key) or key.startswith(skey)):
            reason, score = "one_code_extends_the_other", 0.65 + 0.2 * shorter / max(longer, 1)
        elif key and shorter >= MIN_EXTENSION_LEN and longer - shorter <= 2:
            sm = SequenceMatcher(None, key.translate(_CONFUSABLE), skey.translate(_CONFUSABLE))
            if (
                sm.real_quick_ratio() >= FUZZY_CODE_THRESHOLD
                and sm.quick_ratio() >= FUZZY_CODE_THRESHOLD
                and sm.ratio() >= FUZZY_CODE_THRESHOLD
            ):
                reason, score = "similar_code_characters", 0.8 * sm.ratio()

        # 2. Name-based matching (Full or Half-Match on stored WinMentor article names)
        if not reason and q_tokens and is_valid_part_name(name):
            n_tokens = _tokens(name)
            n_word_list = _word_list(name)
            n_first_word = n_word_list[0] if n_word_list else ""
            if n_tokens and not _has_opposite_conflict(q_tokens, n_tokens):
                inter = q_tokens & n_tokens
                if inter:
                    overlap_min = len(inter) / min(len(q_tokens), len(n_tokens))
                    jaccard = len(inter) / len(q_tokens | n_tokens)
                    if n_tokens <= q_tokens and len(n_tokens) >= 2:
                        reason, score = "stored_name_match", round(0.85 + 0.1 * jaccard, 3)
                    elif overlap_min >= 0.5 and len(inter) >= 2:
                        reason, score = "half_name_match", round(0.65 + 0.25 * jaccard, 3)
                    elif (
                        q_first_word
                        and q_first_word == n_first_word
                        and q_first_word in {"lac", "cataliz", "chit", "fond", "diluant", "intaritor", "burete"}
                    ):
                        reason, score = "same_product_type", round(0.52 + 0.2 * jaccard, 3)

        if reason:
            found.append(CodeSuggestion(stored, name, reason, round(score, 3), stoc))
            seen_codes.add(stored)

    # 3. Also include description-derived code chips (e.g. 'CATALIZ' or 'H79') if not already in found
    for cand in _extract_description_code_candidates(code, quote_name):
        if cand.code not in seen_codes:
            found.append(cand)
            seen_codes.add(cand.code)

    found.sort(key=lambda s: (-s.score, s.code))
    return found[:limit]


def resolve_parts(
    items: list[tuple[str, str]],
    db_path: str = DEFAULT_DB_PATH,
    auto_match_separators: bool = True,
    max_suggestions: int = MAX_SUGGESTIONS,
) -> list[PartResolution]:
    with _connect(db_path) as conn:
        rows = _load_rows(conn)

    by_code: dict[str, tuple[str, float, float]] = {
        c: (n, st, so) for c, n, st, so, _, _ in rows
    }
    by_key: dict[str, list[tuple[str, str, float, float]]] = {}
    by_root: dict[str, list[tuple[str, str, float, float]]] = {}
    by_exact_name: dict[str, list[tuple[str, str, float, float]]] = {}

    for c, n, st, so, k, r in rows:
        if k:
            by_key.setdefault(k, []).append((c, n, st, so))
        if r and len(r) >= 4:
            by_root.setdefault(r, []).append((c, n, st, so))
        if is_valid_part_name(n):
            by_exact_name.setdefault(normalize_name(n), []).append((c, n, st, so))

    def judge(
        code: str, canonical: str, quote_name: str, via: str, stoc: float, sold: float
    ) -> PartResolution:
        status = PartStatus.MATCH if names_match(quote_name, canonical) else PartStatus.CONFLICT
        return PartResolution(status, code, canonical, via, stoc=stoc, sold=sold)

    results: list[PartResolution] = []
    for code, quote_name in items:
        cleaned = strip_supplier_suffix(code)
        norm = normalize_code(cleaned)
        key = compact_code(cleaned)
        root = strip_distributor_brand_suffix(cleaned)
        norm_qname = normalize_name(quote_name)

        # 1. Exact code match
        if norm in by_code:
            n, st, so = by_code[norm]
            results.append(judge(norm, n, quote_name, "exact", st, so))
            continue

        # 2. Separator-insensitive code match
        same_key = by_key.get(key, []) if key else []
        if auto_match_separators and len(same_key) == 1:
            c0, n0, st0, so0 = same_key[0]
            results.append(judge(c0, n0, quote_name, "separators", st0, so0))
            continue

        # 3. Distributor brand-suffix code match (e.g. '46673 FE' <-> '46673FE')
        same_root = by_root.get(root, []) if (root and len(root) >= 4) else []
        if auto_match_separators and len(same_root) == 1:
            c0, n0, st0, so0 = same_root[0]
            results.append(judge(c0, n0, quote_name, "brand_suffix", st0, so0))
            continue

        # 4. Exact stored article name match (e.g. 'CATALIZ ACRIL NORMAL HS C15/200ML' -> 'CATALIZ')
        same_name = by_exact_name.get(norm_qname, []) if len(norm_qname) >= 6 else []
        if len(same_name) == 1:
            c0, n0, st0, so0 = same_name[0]
            results.append(
                PartResolution(
                    PartStatus.MATCH,
                    c0,
                    n0,
                    "exact_name",
                    stoc=st0,
                    sold=so0,
                    suggestions=_suggest(rows, code, quote_name, max_suggestions),
                )
            )
            continue

        results.append(
            PartResolution(
                PartStatus.NEW,
                norm or key,
                quote_name,
                "none",
                stoc=0.0,
                sold=0.0,
                suggestions=_suggest(rows, code, quote_name, max_suggestions),
            )
        )
    return results


def resolve_part(
    part_code: str,
    quote_name: str,
    db_path: str = DEFAULT_DB_PATH,
    auto_match_separators: bool = True,
) -> PartResolution:
    return resolve_parts([(part_code, quote_name)], db_path, auto_match_separators)[0]


def check_part(
    part_code: str, quote_name: str, db_path: str = DEFAULT_DB_PATH
) -> tuple[PartStatus, str]:
    r = resolve_part(part_code, quote_name, db_path)
    return (r.status, r.name)


def find_similar_codes(
    part_code: str,
    limit: int = MAX_SUGGESTIONS,
    db_path: str = DEFAULT_DB_PATH,
    quote_name: str = "",
) -> list[CodeSuggestion]:
    with _connect(db_path) as conn:
        rows = _load_rows(conn)
    return _suggest(rows, part_code, quote_name, limit)


def search_codes(
    query: str, limit: int = 12, db_path: str = DEFAULT_DB_PATH
) -> list[CodeSuggestion]:
    """
    Searches nomenclature by code or whole-word/word-prefix name tokens.
    Prevents false substring matches like 'LAC' matching 'PLACUTE FRANA'.
    """
    qkey = compact_code(query)
    qroot = strip_distributor_brand_suffix(query)
    qwords = _word_list(query)

    with _connect(db_path) as conn:
        rows = _load_rows(conn)

    found: list[CodeSuggestion] = []
    for code, name, stoc, _sold, key, sroot in rows:
        reason, score = "", 0.0
        nwords = _word_list(name)

        if qkey and key == qkey:
            reason, score = "exact_code", 1.0
        elif qroot and len(qroot) >= 4 and sroot == qroot:
            reason, score = "brand_suffix_match", 0.96
        elif qkey and len(qkey) >= 2 and key.startswith(qkey):
            reason, score = "code_prefix", 0.92
        elif qwords and nwords and all(any(nw == qw for nw in nwords) for qw in qwords):
            # Every query word is an exact whole word in the article name
            first_bonus = 0.05 if nwords[0] == qwords[0] else 0.0
            reason, score = "exact_word_in_name", round(0.85 + first_bonus, 3)
        elif qwords and nwords and all(
            any(nw.startswith(qw) for nw in nwords) for qw in qwords if len(qw) >= 2
        ):
            # Query words match the start of words in the article name (never middle substrings like pLACute)
            reason, score = "word_prefix_in_name", 0.72
        elif len(qkey) >= 3 and qkey in key:
            reason, score = "code_contains", 0.65

        if reason:
            found.append(CodeSuggestion(code, name, reason, score, stoc))

    found.sort(key=lambda s: (-s.score, -s.stoc, len(s.code), s.code))
    return found[:limit]


# --- Stock & Nomenclature Browser CRUD ---------------------------------------------------

def list_all_parts(
    search: str = "",
    only_in_stock: bool = False,
    limit: int = 100,
    offset: int = 0,
    db_path: str = DEFAULT_DB_PATH,
) -> tuple[int, float, list[PartRecord]]:
    q = search.strip()
    with _connect(db_path) as conn:
        rows = cast(
            list[tuple[int, str, str, float, float, str | None]],
            conn.execute(
                """
                SELECT nr_crt, part_code, part_name, stoc, sold, last_used
                FROM parts
                ORDER BY CASE WHEN nr_crt > 0 THEN nr_crt ELSE 999999 END ASC, part_code ASC
                """
            ).fetchall(),
        )

    filtered: list[PartRecord] = []
    q_up = q.upper()
    q_comp = compact_code(q)
    q_words = _word_list(q) if q else []

    for nr, c, n, st, so, lu in rows:
        nr_i = int(nr or 0)
        st_f = float(st or 0.0)
        so_f = float(so or 0.0)
        if only_in_stock and st_f <= 0.0:
            continue
        if q:
            c_up = c.upper()
            n_words = _word_list(n)
            c_comp = compact_code(c)
            word_match = bool(q_words) and all(
                any(nw.startswith(qw) for nw in n_words) for qw in q_words
            )
            match = (
                q_up in c_up
                or q_up == str(nr_i)
                or (len(q_comp) >= 2 and q_comp in c_comp)
                or word_match
            )
            if not match:
                continue
        filtered.append(PartRecord(nr_i, c, n, round(st_f, 4), round(so_f, 2), lu))

    total_count = len(filtered)
    total_sold = round(sum(r.sold for r in filtered), 2)
    return total_count, total_sold, filtered[offset : offset + limit]


def delete_part(part_code: str, db_path: str = DEFAULT_DB_PATH) -> bool:
    norm = normalize_code(part_code)
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM parts WHERE part_code = ?", (norm,))
        return cur.rowcount > 0


def upsert_parts(
    items: list[tuple[str, str]] | list[tuple[str, str, int, float, float]],
    db_path: str = DEFAULT_DB_PATH,
    update_stock_values: bool = False,
) -> None:
    prepared: list[tuple[str, str, int, float, float]] = []
    bad: list[int] = []
    for i, item in enumerate(items):
        code = item[0]
        name = item[1]
        nr_crt = int(item[2]) if len(item) > 2 else 0
        stoc = float(item[3]) if len(item) > 3 else 0.0
        sold = float(item[4]) if len(item) > 4 else 0.0

        c = normalize_code(strip_supplier_suffix(code))
        n = name.strip()
        if not is_valid_part_name(n) and is_valid_part_name(c):
            n = c
        if c and is_valid_part_name(n):
            prepared.append((c, n, nr_crt, stoc, sold))
        else:
            bad.append(i)
    if bad:
        raise ValueError(f"empty or invalid part_code / part_name at positions {bad}")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        if update_stock_values:
            conn.executemany(
                """
                INSERT INTO parts (part_code, part_name, nr_crt, stoc, sold, last_used)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(part_code) DO UPDATE SET
                    part_name = excluded.part_name,
                    nr_crt = CASE WHEN excluded.nr_crt > 0 THEN excluded.nr_crt ELSE parts.nr_crt END,
                    stoc = excluded.stoc,
                    sold = excluded.sold,
                    last_used = excluded.last_used
                """,
                [(c, n, nr, st, so, now) for c, n, nr, st, so in prepared],
            )
        else:
            conn.executemany(
                """
                INSERT INTO parts (part_code, part_name, last_used)
                VALUES (?, ?, ?)
                ON CONFLICT(part_code) DO UPDATE SET
                    part_name = excluded.part_name,
                    last_used = excluded.last_used
                """,
                [(c, n, now) for c, n, _nr, _st, _so in prepared],
            )


def update_or_add_part(part_code: str, part_name: str, db_path: str = DEFAULT_DB_PATH) -> None:
    upsert_parts([(part_code, part_name)], db_path=db_path, update_stock_values=False)


# --- Duplicate Invoice Protection & Leading-Zero Trailing Digits -------------------------

def canonical_supplier_token(supplier_name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", supplier_name).upper()
    compact = re.sub(r"[^A-Z0-9]", "", decomposed)
    if compact.startswith("SC") and len(compact) > 4:
        compact = compact[2:]
    for suffix in ("SRL", "SNC", "SCS", "SA"):
        if compact.endswith(suffix) and len(compact) > len(suffix) + 2:
            compact = compact[: -len(suffix)]
            break
    return compact[:12]


def extract_trailing_doc_digits(doc_code: str) -> str:
    """
    Extracts the trailing invoice digits (up to 6 digits) while allowing leading zeros
    (e.g. '078561' stays '078561', '01234' stays '01234').
    Only strips excess zero-padding when a raw number is >6 digits starting with '000'.
    """
    cleaned = unicodedata.normalize("NFKD", doc_code).upper().strip()
    if re.fullmatch(r"\d{1,7}", cleaned):
        return cleaned
    groups: list[str] = re.findall(r"\d+", cleaned)
    if not groups:
        return compact_code(doc_code)
    last = groups[-1]
    if len(last) > 6 and last.startswith("00"):
        return last.lstrip("0") or "0"
    if len(last) > 6:
        return last[-6:]
    return last


def _make_doc_key(supplier_name: str, document_code: str) -> str:
    sup = canonical_supplier_token(supplier_name)
    doc = extract_trailing_doc_digits(document_code).lstrip("0") or "0"
    return f"{sup}:{doc}"


def check_duplicate_invoice(
    supplier_name: str, document_code: str, db_path: str = DEFAULT_DB_PATH
) -> ExportLogEntry | None:
    key = _make_doc_key(supplier_name, document_code)
    trailing_nr = extract_trailing_doc_digits(document_code)
    with _connect(db_path) as conn:
        rows = cast(
            list[tuple[str, str, str, str, str, float, str, str]],
            conn.execute(
                """
                SELECT doc_key, supplier_name, document_code, winmentor_doc_nr,
                       quote_date, total_fara_tva, archive_dir, exported_at
                FROM exports_log
                WHERE doc_key = ? OR winmentor_doc_nr = ?
                ORDER BY exported_at DESC
                """,
                (key, trailing_nr),
            ).fetchall(),
        )
    target_sup = canonical_supplier_token(supplier_name)
    for r in rows:
        entry = ExportLogEntry(*r)
        if canonical_supplier_token(entry.supplier_name) == target_sup:
            return entry
    return None


def record_export(
    supplier_name: str,
    document_code: str,
    winmentor_doc_nr: str,
    quote_date: str,
    total_fara_tva: float,
    archive_dir: str,
    db_path: str = DEFAULT_DB_PATH,
) -> ExportLogEntry:
    key = _make_doc_key(supplier_name, document_code)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry = ExportLogEntry(
        doc_key=key,
        supplier_name=supplier_name.strip(),
        document_code=document_code.strip(),
        winmentor_doc_nr=winmentor_doc_nr,
        quote_date=quote_date,
        total_fara_tva=round(total_fara_tva, 2),
        archive_dir=archive_dir,
        exported_at=now,
    )
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO exports_log (
                doc_key, supplier_name, document_code, winmentor_doc_nr,
                quote_date, total_fara_tva, archive_dir, exported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_key) DO UPDATE SET
                supplier_name = excluded.supplier_name,
                document_code = excluded.document_code,
                winmentor_doc_nr = excluded.winmentor_doc_nr,
                quote_date = excluded.quote_date,
                total_fara_tva = excluded.total_fara_tva,
                archive_dir = excluded.archive_dir,
                exported_at = excluded.exported_at
            """,
            (
                entry.doc_key,
                entry.supplier_name,
                entry.document_code,
                entry.winmentor_doc_nr,
                entry.quote_date,
                entry.total_fara_tva,
                entry.archive_dir,
                entry.exported_at,
            ),
        )
    return entry


def list_export_history(limit: int = 50, db_path: str = DEFAULT_DB_PATH) -> list[ExportLogEntry]:
    with _connect(db_path) as conn:
        rows = cast(
            list[tuple[str, str, str, str, str, float, str, str]],
            conn.execute(
                """
                SELECT doc_key, supplier_name, document_code, winmentor_doc_nr,
                       quote_date, total_fara_tva, archive_dir, exported_at
                FROM exports_log
                ORDER BY exported_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall(),
        )
    return [ExportLogEntry(*r) for r in rows]


# --- Gemini Model Telemetry & Dynamic Priority -------------------------------------------

def get_prioritized_models(
    pool: list[str] | None = None, db_path: str = DEFAULT_DB_PATH
) -> list[str]:
    models = list(pool) if pool is not None else list(DEFAULT_MODEL_POOL)
    now_ts = time.time()
    with _connect(db_path) as conn:
        rows = cast(
            list[tuple[str, int, int, int, int, float, float]],
            conn.execute(
                """
                SELECT model_name, success_count, rate_limit_429_count,
                       unavailable_503_count, other_error_count, avg_latency_ms, cooldown_until
                FROM model_stats
                """
            ).fetchall(),
        )
    stats_map = {r[0]: r for r in rows}

    def sort_key(m: str) -> tuple[int, float, float, int]:
        default_idx = models.index(m) if m in models else 99
        if m not in stats_map:
            return (0, 0.5, 5000.0, default_idx)
        _name, succ, r429, u503, oerr, lat, cd_until = stats_map[m]
        in_cd = 1 if cd_until > now_ts else 0
        total = succ + r429 + u503 + oerr
        fail_ratio = (r429 + u503 + oerr) / max(total, 1)
        return (in_cd, round(fail_ratio, 3), lat if lat > 0 else 5000.0, default_idx)

    return sorted(models, key=sort_key)


def record_model_attempt(
    model_name: str,
    outcome: str,
    latency_ms: float = 0.0,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    now_ts = time.time()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cooldown_sec = 120.0 if outcome == "429" else (25.0 if outcome == "503" else 0.0)
    cd_until = now_ts + cooldown_sec if cooldown_sec > 0 else 0.0

    with _connect(db_path) as conn:
        row = cast(
            tuple[int, int, int, int, float, float] | None,
            conn.execute(
                """
                SELECT success_count, rate_limit_429_count, unavailable_503_count,
                       other_error_count, avg_latency_ms, cooldown_until
                FROM model_stats WHERE model_name = ?
                """,
                (model_name,),
            ).fetchone(),
        )
        if row is None:
            succ = 1 if outcome == "success" else 0
            r429 = 1 if outcome == "429" else 0
            u503 = 1 if outcome == "503" else 0
            oerr = 1 if outcome == "error" else 0
            avg_lat = round(latency_ms, 1) if outcome == "success" else 0.0
            conn.execute(
                """
                INSERT INTO model_stats (
                    model_name, success_count, rate_limit_429_count, unavailable_503_count,
                    other_error_count, avg_latency_ms, cooldown_until, last_used
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (model_name, succ, r429, u503, oerr, avg_lat, cd_until, now_iso),
            )
        else:
            succ, r429, u503, oerr, prev_lat, prev_cd = row
            if outcome == "success":
                new_lat = (
                    round((prev_lat * succ + latency_ms) / (succ + 1), 1)
                    if succ > 0
                    else round(latency_ms, 1)
                )
                succ += 1
                new_cd = 0.0
            else:
                new_lat = prev_lat
                new_cd = max(prev_cd, cd_until)
                if outcome == "429":
                    r429 += 1
                elif outcome == "503":
                    u503 += 1
                else:
                    oerr += 1
            conn.execute(
                """
                UPDATE model_stats
                SET success_count = ?, rate_limit_429_count = ?, unavailable_503_count = ?,
                    other_error_count = ?, avg_latency_ms = ?, cooldown_until = ?, last_used = ?
                WHERE model_name = ?
                """,
                (succ, r429, u503, oerr, new_lat, new_cd, now_iso, model_name),
            )


def list_model_stats(db_path: str = DEFAULT_DB_PATH) -> list[ModelStatEntry]:
    now_ts = time.time()
    ordered = get_prioritized_models(db_path=db_path)
    with _connect(db_path) as conn:
        rows = cast(
            list[tuple[str, int, int, int, int, float, float, str | None]],
            conn.execute(
                """
                SELECT model_name, success_count, rate_limit_429_count,
                       unavailable_503_count, other_error_count, avg_latency_ms,
                       cooldown_until, last_used
                FROM model_stats
                """
            ).fetchall(),
        )
    by_name = {r[0]: r for r in rows}
    out: list[ModelStatEntry] = []
    for m in ordered:
        if m in by_name:
            _, s, r429, u503, oerr, lat, cd_until, lu = by_name[m]
            rem = max(0, int(round(cd_until - now_ts)))
            out.append(ModelStatEntry(m, s, r429, u503, oerr, round(lat, 1), rem > 0, rem, lu))
        else:
            out.append(ModelStatEntry(m, 0, 0, 0, 0, 0.0, False, 0, None))
    return out


# --- Standalone Stock Deduction for Bon de Consum ----------------------------------------

def deduct_stock_quantities(
    items: list[tuple[str, float]], db_path: str = DEFAULT_DB_PATH
) -> None:
    """
    Deducts consumed quantities from `parts.stoc` when a standalone Bon de Consum is generated.
    Also proportionally scales `parts.sold` (total remaining stock valuation).
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        for code, qty in items:
            norm_code = normalize_code(code)
            row = cast(
                tuple[float, float] | None,
                conn.execute(
                    "SELECT stoc, sold FROM parts WHERE part_code = ?", (norm_code,)
                ).fetchone(),
            )
            if row is not None:
                cur_stoc, cur_sold = row
                new_stoc = max(0.0, round(cur_stoc - qty, 4))
                new_sold = (
                    round((cur_sold / cur_stoc) * new_stoc, 2)
                    if cur_stoc > 0
                    else 0.0
                )
                conn.execute(
                    """
                    UPDATE parts
                    SET stoc = ?, sold = ?, last_used = ?
                    WHERE part_code = ?
                    """,
                    (new_stoc, new_sold, now, norm_code),
                )