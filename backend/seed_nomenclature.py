import re
import unicodedata
from pathlib import Path
from typing import Any, cast

import openpyxl
from nomenclature import (
    DEFAULT_DB_PATH,
    init_db,
    is_valid_part_name,
    normalize_code,
    strip_supplier_suffix,
    upsert_parts,
)

DEFAULT_EXCEL_PATH = str(Path(__file__).with_name("SEPT.xlsx"))


def _clean_header(val: object) -> str:
    if val is None:
        return ""
    s = unicodedata.normalize("NFKD", str(val))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _to_float(val: object) -> float:
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(" ", "")
    if not s:
        return 0.0
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _to_int(val: object) -> int:
    try:
        return int(round(_to_float(val)))
    except Exception:
        return 0


def seed_from_winmentor_excel(
    excel_path: str = DEFAULT_EXCEL_PATH,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    """
    Citește atât Balanța de Stocuri ('stoc sept 2026.xlsx': Col A=Nr., Col B=Articol, Col C=Cod,
    Col F=Stoc, Col I=Sold), cât și Registrul NIR ('SEPT.xlsx'), începând chiar de la primul rând
    de date (ex. Nr. 165). Păstrează și articolele cu Stoc=0 și Sold=0.
    """
    path = Path(excel_path)
    if not path.exists():
        raise FileNotFoundError(f"Excel file not found: {excel_path}")

    init_db(db_path)

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet: Any = wb.active
        if sheet is None:
            raise ValueError(f"Workbook {excel_path} has no active worksheet.")

        nr_col = 0
        code_col = 1
        name_col = 3
        stoc_col: int | None = None
        sold_col: int | None = None
        data_start_row = 3
        has_stock_cols = False

        header_rows = list(sheet.iter_rows(min_row=1, max_row=6, values_only=True))
        for r_idx, raw_row in enumerate(header_rows, start=1):
            row_cells = list(cast(tuple[object, ...], raw_row))
            cleaned = [_clean_header(c) for c in row_cells]
            if "articol" in cleaned and "cod" in cleaned:
                if "nr" in cleaned:
                    nr_col = cleaned.index("nr")
                elif "nrcrt" in cleaned:
                    nr_col = cleaned.index("nrcrt")
                name_col = cleaned.index("articol")
                code_col = cleaned.index("cod")
                if "stoc" in cleaned:
                    stoc_col = cleaned.index("stoc")
                    has_stock_cols = True
                if "sold" in cleaned:
                    sold_col = cleaned.index("sold")
                    has_stock_cols = True

                # Verifică dacă rândul următor este un sub-antet ('extern'/'intern' în SEPT.xlsx)
                # sau este deja primul rând de date (precum '165 | 30104300M MS430... | MS430' în stoc sept 2026.xlsx)
                if r_idx < len(header_rows):
                    next_cells = list(cast(tuple[object, ...], header_rows[r_idx]))
                    next_clean = [_clean_header(c) for c in next_cells]
                    if "extern" in next_clean or "intern" in next_clean:
                        data_start_row = r_idx + 2
                    else:
                        data_start_row = r_idx + 1
                else:
                    data_start_row = r_idx + 1
                break

        # Mapare: part_code -> (part_name, nr_crt, stoc_total, sold_total)
        aggregated: dict[str, tuple[str, int, float, float]] = {}
        fallback_uncoded = 0

        for raw_row in sheet.iter_rows(min_row=data_start_row, values_only=True):
            cells = list(cast(tuple[object, ...], raw_row))
            if len(cells) <= max(code_col, name_col):
                continue

            first_cell = str(cells[0] if cells[0] is not None else "").strip()
            if first_cell.startswith(("NIR", "Total", "TOTAL")):
                continue

            nr_val = _to_int(cells[nr_col]) if nr_col < len(cells) else 0
            raw_name = cells[name_col]
            raw_code = cells[code_col]

            if raw_name is None:
                continue
            name_str = re.sub(r"\s+", " ", str(raw_name)).strip().rstrip(";")
            if not is_valid_part_name(name_str):
                continue

            code_str = ""
            if raw_code is not None:
                code_str = normalize_code(strip_supplier_suffix(str(raw_code)))

            if not code_str or code_str in {"0", "0.0", "NAN", "NONE"}:
                up_name = name_str.upper()
                if up_name.startswith("FOND "):
                    code_str = "FOND"
                elif up_name.startswith("CATALIZ"):
                    code_str = "CATALIZ"
                else:
                    code_str = normalize_code(up_name[:16])
                fallback_uncoded += 1

            stoc_val = (
                _to_float(cells[stoc_col])
                if (stoc_col is not None and stoc_col < len(cells))
                else 0.0
            )
            sold_val = (
                _to_float(cells[sold_col])
                if (sold_col is not None and sold_col < len(cells))
                else 0.0
            )

            if code_str in aggregated:
                prev_name, prev_nr, prev_stoc, prev_sold = aggregated[code_str]
                best_name = prev_name if len(prev_name) >= len(name_str) else name_str
                best_nr = prev_nr if prev_nr > 0 else nr_val
                aggregated[code_str] = (
                    best_name,
                    best_nr,
                    round(prev_stoc + stoc_val, 4),
                    round(prev_sold + sold_val, 2),
                )
            else:
                aggregated[code_str] = (
                    name_str,
                    nr_val,
                    round(stoc_val, 4),
                    round(sold_val, 2),
                )
    finally:
        wb.close()

    if aggregated:
        payload = [
            (c, n, nr, st, so) for c, (n, nr, st, so) in aggregated.items()
        ]
        upsert_parts(payload, db_path=db_path, update_stock_values=has_stock_cols)

    print(
        f"Seeded {len(aggregated)} unique WinMentor articles from '{path.name}' "
        f"(stock_cols={has_stock_cols}, uncoded_fallbacks={fallback_uncoded})."
    )
    return len(aggregated)


if __name__ == "__main__":
    seed_from_winmentor_excel()