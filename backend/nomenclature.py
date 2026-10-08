import os
import re
import sqlite3
import unicodedata
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from enum import Enum
from pathlib import Path
from typing import cast

# Anchored to this file (or an env override) instead of the current working directory,
# so the backend uses the same database no matter where it is launched from.
DEFAULT_DB_PATH = str(Path(os.environ.get("NOMENCLATURE_DB", Path(__file__).with_name("nomenclature.db"))))

# Names: two names count as "the same description" when one's words are all contained in the
# other's, or when at least this fraction of their combined words are shared.
NAME_SIMILARITY_THRESHOLD = 0.5

# Codes (all comparisons use the separator-free form, see compact_code):
MIN_EXTENSION_LEN = 5        # shortest code allowed to be a prefix of another without a space boundary
FUZZY_CODE_THRESHOLD = 0.85  # minimum character similarity for an "OCR-lookalike" suggestion
MAX_SUGGESTIONS = 5

# Characters OCR commonly confuses; used only to make fuzzy code comparison forgiving.
_CONFUSABLE = str.maketrans("OISBZ", "01582")

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS parts (
        part_code TEXT NOT NULL PRIMARY KEY,
        part_name TEXT NOT NULL,
        last_used TEXT
    )
"""


class PartStatus(Enum):
    NEW = "NEW"
    MATCH = "MATCH"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class CodeSuggestion:
    """An existing code that might be what the incoming code really refers to."""
    code: str     # the code exactly as stored (i.e. as it exists in WinMentor)
    name: str     # its canonical name
    reason: str   # why it was suggested (see _suggest)
    score: float  # heuristic ranking value, NOT a probability


@dataclass(frozen=True)
class PartResolution:
    status: PartStatus
    code: str          # code to export: the stored code if found, else the compact form of the incoming one
    name: str          # canonical name if found, else the quote's name
    matched_via: str   # "exact", "separators" (matched ignoring spaces/dashes) or "none"
    suggestions: list[CodeSuggestion] = field(default_factory=list[CodeSuggestion])  # only filled for NEW


# --- Normalization -----------------------------------------------------------------------

def strip_supplier_suffix(code: str) -> str:
    """Strips multi-column supplier suffixes separated by ' - ' (e.g. Materom's
    'GDB2063 - 11-477-298 - TRW - OEM' -> 'GDB2063') while keeping real hyphens ('GDB-2063')."""
    primary = re.split(r"\s+[-–—/]\s+", code.strip(), maxsplit=1)[0]
    return primary.strip()


def normalize_code(code: str) -> str:
    """Canonical form of a part code: trimmed, inner whitespace collapsed, uppercase.
    Use this one function everywhere (including the extraction module) so codes never drift."""
    return re.sub(r"\s+", " ", code).strip().upper()


def compact_code(code: str) -> str:
    """Separator-free form: 'A-5', 'A 5' and 'a5' all become 'A5'.
    Used to compare codes loosely, and as the form in which NEW codes are stored."""
    decomposed = unicodedata.normalize("NFKD", code).upper()
    return re.sub(r"[^A-Z0-9]", "", decomposed)


def normalize_name(name: str) -> str:
    """Comparison form of a name: ignores case, diacritics (incl. ș/ş cedilla variants) and spacing."""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip().casefold()


# Abbreviations mapped to full words before names are compared.
# Evidence from the September WinMentor export: STG, DR, INF, EXT are used as real abbreviations.
# RISK: SUP, INT, ANT and ST are never abbreviations in that export; the same letters begin
# SUPORT/SUPAPE, INTINZATOR/INTARITOR and ANTIRULIU, so a supplier writing "SUP." for "suport"
# would be read as "superior". The cost is an extra CONFLICT to review. Delete a line to disable it.
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
    "cataliz": "intaritor",
    "catalizator": "intaritor",
}

# Words that flip the meaning of a part. If one name has one side and the other name has the
# opposite side, the names conflict no matter how many other words they share.
# Names containing BOTH sides (e.g. "STG +DR") are not treated as contradicting either side.
# Starting list: extend it as you see real cases (SUS appears once in the September export).
_OPPOSITES: list[tuple[str, str]] = [
    ("fata", "spate"),
    ("stanga", "dreapta"),
    ("interior", "exterior"),
    ("superior", "inferior"),
    ("sus", "jos"),
]


def _tokens(name: str) -> set[str]:
    words: list[str] = re.findall(r"[a-z0-9]+", normalize_name(name))
    return {_SYNONYMS[w] if w in _SYNONYMS else w for w in words}


def names_match(quote_name: str, canonical_name: str, threshold: float = NAME_SIMILARITY_THRESHOLD) -> bool:
    """True if the two names describe the same thing closely enough to not need review."""
    a, b = _tokens(quote_name), _tokens(canonical_name)
    if not a or not b:
        return a == b
    for x, y in _OPPOSITES:
        if (x in a and y in b and y not in a and x not in b) or (y in a and x in b and x not in a and y not in b):
            return False
    if a <= b or b <= a:  # e.g. short WinMentor label vs. longer supplier description
        return True
    return len(a & b) / len(a | b) >= threshold


# --- Database ----------------------------------------------------------------------------

@contextmanager
def _connect(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    """Opens a connection, commits on success, rolls back on error, and always closes it.

    Note: `with sqlite3.connect(...)` alone only handles the transaction; it never closes
    the connection, which leaks handles and can keep the file locked on Windows.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        with conn:  # commit / rollback
            conn.execute(_SCHEMA)  # idempotent, so a missing init_db() can't break lookups
            columns = {cast(str, row[1]) for row in conn.execute("PRAGMA table_info(parts)")}
            if "last_used" not in columns:  # databases created before last_used existed
                conn.execute("ALTER TABLE parts ADD COLUMN last_used TEXT")
            yield conn
    finally:
        conn.close()


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """Initializes the SQLite database with the parts table."""
    with _connect(db_path):
        pass


def _load_rows(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """All parts as (code, name, compact_code). A full scan is fine for a few thousand
    to tens of thousands of codes; if the table grows far beyond that, store the compact
    key in an indexed column instead."""
    rows = cast(list[tuple[str, str]], conn.execute("SELECT part_code, part_name FROM parts").fetchall())
    return [(c, n, compact_code(c)) for c, n in rows]


# --- Code matching -----------------------------------------------------------------------

def _suggest(rows: list[tuple[str, str, str]], code: str, limit: int) -> list[CodeSuggestion]:
    """Existing codes that may be the same part as `code`, best first.

    Reasons, strongest first:
      same_code_different_separators  A-5 / A 5 / A5
      stored_code_has_extra_text      incoming 3713008G, stored "3713008G CATALIZ."
      incoming_code_has_extra_text    incoming "GDB400 TRW", stored GDB400
      one_code_extends_the_other      one compact code is a prefix of the other (both >= 5 chars)
      similar_characters              near-identical after OCR-confusable mapping (O/0, I/1, S/5, B/8, Z/2)
    """
    norm = normalize_code(code)
    key = compact_code(code)
    if not key:
        return []
    in_words = norm.split()
    in_first = compact_code(in_words[0]) if in_words else ""

    found: list[CodeSuggestion] = []
    for stored, name, skey in rows:
        if stored == norm or not skey:
            continue
        st_words = stored.split()
        reason, score = "", 0.0
        shorter, longer = sorted((len(key), len(skey)))

        if skey == key:
            reason, score = "same_code_different_separators", 1.0
        elif len(st_words) > 1 and compact_code(st_words[0]) == key:
            reason, score = "stored_code_has_extra_text", 0.9
        elif len(in_words) > 1 and len(skey) >= 4 and in_first == skey:
            reason, score = "incoming_code_has_extra_text", 0.9
        elif shorter >= MIN_EXTENSION_LEN and (skey.startswith(key) or key.startswith(skey)):
            reason, score = "one_code_extends_the_other", 0.6 + 0.2 * shorter / longer
        elif shorter >= MIN_EXTENSION_LEN and longer - shorter <= 2:
            sm = SequenceMatcher(None, key.translate(_CONFUSABLE), skey.translate(_CONFUSABLE))
            if (
                sm.real_quick_ratio() >= FUZZY_CODE_THRESHOLD
                and sm.quick_ratio() >= FUZZY_CODE_THRESHOLD
                and sm.ratio() >= FUZZY_CODE_THRESHOLD
            ):
                reason, score = "similar_characters", 0.8 * sm.ratio()

        if reason:
            found.append(CodeSuggestion(stored, name, reason, round(score, 3)))

    found.sort(key=lambda s: (-s.score, s.code))
    return found[:limit]


def resolve_parts(
    items: list[tuple[str, str]],
    db_path: str = DEFAULT_DB_PATH,
    auto_match_separators: bool = True,
    max_suggestions: int = MAX_SUGGESTIONS,
) -> list[PartResolution]:
    """
    Resolves (part_code, quote_name) pairs against the cache in one database pass.

    1. Exact code (after normalize_code): MATCH or CONFLICT depending on the name.
    2. Same code ignoring spaces/dashes/case, and exactly one stored candidate: treated as
       that stored code (matched_via="separators"). Set auto_match_separators=False to be
       asked instead. If several stored codes collapse to the same compact form, none is
       auto-picked; they are all returned as suggestions.
    3. Otherwise NEW. `code` is the compact form of the primary SKU to store, and `suggestions`
       lists existing codes the operator should confirm or reject before adding it as new.
    """
    with _connect(db_path) as conn:
        rows = _load_rows(conn)
    by_code = {c: n for c, n, _ in rows}
    by_key: dict[str, list[tuple[str, str]]] = {}
    for c, n, k in rows:
        by_key.setdefault(k, []).append((c, n))

    def judge(code: str, canonical: str, quote_name: str, via: str) -> PartResolution:
        status = PartStatus.MATCH if names_match(quote_name, canonical) else PartStatus.CONFLICT
        return PartResolution(status, code, canonical, via)

    results: list[PartResolution] = []
    for code, quote_name in items:
        cleaned = strip_supplier_suffix(code)
        norm = normalize_code(cleaned)
        key = compact_code(cleaned)
        if norm in by_code:
            results.append(judge(norm, by_code[norm], quote_name, "exact"))
            continue
        same = by_key.get(key, []) if key else []
        if auto_match_separators and len(same) == 1:
            results.append(judge(same[0][0], same[0][1], quote_name, "separators"))
            continue
        results.append(
            PartResolution(PartStatus.NEW, key or norm, quote_name, "none", _suggest(rows, code, max_suggestions))
        )
    return results


def resolve_part(
    part_code: str, quote_name: str, db_path: str = DEFAULT_DB_PATH, auto_match_separators: bool = True
) -> PartResolution:
    """Single-item version of resolve_parts."""
    return resolve_parts([(part_code, quote_name)], db_path, auto_match_separators)[0]


def check_part(part_code: str, quote_name: str, db_path: str = DEFAULT_DB_PATH) -> tuple[PartStatus, str]:
    """
    Backward-compatible wrapper: returns (status, canonical_name) only.
    Use resolve_part / resolve_parts when you need the resolved code and the suggestions.
    """
    r = resolve_part(part_code, quote_name, db_path)
    return (r.status, r.name)


def find_similar_codes(part_code: str, limit: int = MAX_SUGGESTIONS, db_path: str = DEFAULT_DB_PATH) -> list[CodeSuggestion]:
    """Existing codes that may be the same part as `part_code` (excluding an exact match)."""
    with _connect(db_path) as conn:
        rows = _load_rows(conn)
    return _suggest(rows, part_code, limit)


def search_codes(query: str, limit: int = 10, db_path: str = DEFAULT_DB_PATH) -> list[CodeSuggestion]:
    """Autocomplete: existing parts whose code starts with / contains the query
    (ignoring spaces and dashes), or whose name contains it."""
    qkey = compact_code(query)
    qname = normalize_name(query)
    with _connect(db_path) as conn:
        rows = _load_rows(conn)

    found: list[CodeSuggestion] = []
    for code, name, key in rows:
        if qkey and key.startswith(qkey):
            reason, score = "code_prefix", 0.9
        elif len(qkey) >= 2 and qkey in key:
            reason, score = "code_contains", 0.6
        elif len(qname) >= 3 and qname in normalize_name(name):
            reason, score = "name_contains", 0.4
        else:
            continue
        found.append(CodeSuggestion(code, name, reason, score))

    found.sort(key=lambda s: (-s.score, len(s.code), s.code))
    return found[:limit]


# --- Writes ------------------------------------------------------------------------------

def upsert_parts(items: list[tuple[str, str]], db_path: str = DEFAULT_DB_PATH) -> None:
    """
    Export step: atomically adds new parts, updates approved names, and stamps last_used.
    Either every item is written or none is. Pass PartResolution.code and the approved name
    (the canonical name for MATCH lines, so only last_used changes). Empty codes/names are
    rejected before anything is written.
    """
    prepared: list[tuple[str, str]] = []
    bad: list[int] = []
    for i, (code, name) in enumerate(items):
        c, n = normalize_code(strip_supplier_suffix(code)), name.strip()
        if c and n:
            prepared.append((c, n))
        else:
            bad.append(i)
    if bad:
        raise ValueError(f"empty part_code or part_name at positions {bad}")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO parts (part_code, part_name, last_used)
            VALUES (?, ?, ?)
            ON CONFLICT(part_code) DO UPDATE SET
                part_name = excluded.part_name,
                last_used = excluded.last_used
            """,
            [(c, n, now) for c, n in prepared],
        )


def update_or_add_part(part_code: str, part_name: str, db_path: str = DEFAULT_DB_PATH) -> None:
    """Adds a new part or updates an existing one after manual conflict resolution."""
    upsert_parts([(part_code, part_name)], db_path)


def normalize_existing_codes(db_path: str = DEFAULT_DB_PATH) -> int:
    """
    One-time migration for a database filled before codes were normalized.
    Re-keys every row to its normalized code (existing spaces/dashes are kept; only
    whitespace and case are normalized). If two old codes collapse to the same
    normalized code, one is kept (arbitrarily) and the rest dropped. Returns the number
    of rows dropped, so you can review them.
    """
    with _connect(db_path) as conn:
        rows = cast(
            list[tuple[str, str, str | None]],
            conn.execute("SELECT part_code, part_name, last_used FROM parts").fetchall(),
        )
        conn.execute("DELETE FROM parts")
        conn.executemany(
            "INSERT OR IGNORE INTO parts (part_code, part_name, last_used) VALUES (?, ?, ?)",
            [(normalize_code(c), n, u) for c, n, u in rows if normalize_code(c)],
        )
        after = cast(tuple[int], conn.execute("SELECT COUNT(*) FROM parts").fetchone())[0]
    return len(rows) - after


if __name__ == "__main__":
    init_db()
    print(f"Database initialized successfully at {DEFAULT_DB_PATH}")