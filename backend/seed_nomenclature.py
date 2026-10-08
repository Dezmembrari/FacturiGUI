import sys
from pathlib import Path
from typing import Any, cast
import openpyxl
from nomenclature import DEFAULT_DB_PATH, init_db, normalize_code, upsert_parts


def seed_from_winmentor_excel(excel_path: str, db_path: str = DEFAULT_DB_PATH) -> int:
    """Parses a WinMentor NIR Excel export (like SEPT.xlsx) and seeds nomenclature.db."""
    init_db(db_path)
    wb = openpyxl.load_workbook(excel_path, data_only=True)
    sheet = wb.active
    if sheet is None:
        raise RuntimeError("Excel workbook has no active sheet.")

    deduped: dict[str, str] = {}
    skipped_blank = 0

    # Row 1-2 are headers; data rows start at row 3
    for row in sheet.iter_rows(min_row=3, values_only=True):
        cells = cast(tuple[Any, ...], row)
        if len(cells) < 4:
            continue
        nr_crt, cod_ext, _cod_int, articol = cells[0], cells[1], cells[2], cells[3]

        # Skip NIR header rows and TOTAL rows where Articol is empty
        if articol is None or nr_crt is None:
            continue
        if isinstance(nr_crt, str) and (nr_crt.startswith("NIR") or "TOTAL" in nr_crt):
            continue

        name_str = str(articol).strip()
        if not name_str or name_str.lower() == "articol":
            continue

        if cod_ext is None or not str(cod_ext).strip():
            skipped_blank += 1
            continue

        code_str = normalize_code(str(cod_ext))
        if code_str:
            deduped[code_str] = name_str

    items = list(deduped.items())
    if items:
        upsert_parts(items, db_path)

    print(
        f"Seeded {len(items)} unique WinMentor articles from '{excel_path}' "
        f"(skipped {skipped_blank} uncoded rows)."
    )
    return len(items)


if __name__ == "__main__":
    target_excel = sys.argv[1] if len(sys.argv) > 1 else "SEPT.xlsx"
    if not Path(target_excel).exists():
        print(f"Error: '{target_excel}' not found in {Path.cwd()}")
        sys.exit(1)
    seed_from_winmentor_excel(target_excel)