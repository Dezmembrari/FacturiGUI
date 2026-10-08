import os
from extractor import (
    extract_quote_data,
    extract_vehicle_data,
    validate_quote,
    validate_vehicle,
)
from nomenclature import PartStatus, resolve_parts


def test_quote_extraction(file_path: str) -> bool:
    """Runs extraction, mathematical validation, and WinMentor nomenclature resolution on a quote."""
    if not os.path.exists(file_path):
        print(f"[SKIP] Quote file not found: {file_path}")
        return False

    print(f"\n{'=' * 65}")
    print(f"TESTING QUOTE EXTRACTION & NOMENCLATURE: {file_path}")
    print(f"{'=' * 65}")
    print("Uploading to Gemini and processing schema fallback chain...")

    try:
        quote = extract_quote_data(file_path)

        print("\n--- Extracted Quote Metadata ---")
        print(f"Supplier Name: {quote.supplier_name}")
        print(f"Document Code: {quote.document_code}")
        print(f"Quote Date:    {quote.quote_date}")
        print(f"Line Items:    {len(quote.parts)}")

        # Resolve extracted parts against nomenclature.db (seeded from SEPT.xlsx)
        resolutions = resolve_parts([(p.part_code, p.part_name) for p in quote.parts])

        print("\n--- Parsed Line Items & Nomenclature Resolution ---")
        for idx, (item, res) in enumerate(zip(quote.parts, resolutions), start=1):
            print(
                f"{idx:02d}. [{item.part_code}] {item.part_name[:35]:<35} | "
                f"Qty: {item.quantity:>6.2f} | "
                f"Unit: {item.unit_price:>8.2f} | "
                f"Total: {item.total_price:>8.2f}"
            )
            if res.status == PartStatus.MATCH:
                print(f"    -> [MATCH via {res.matched_via}] WinMentor: [{res.code}] '{res.name}'")
            elif res.status == PartStatus.CONFLICT:
                print(
                    f"    -> [CONFLICT] Quote says '{item.part_name}' "
                    f"vs WinMentor [{res.code}] '{res.name}'"
                )
            else:
                sug_str = (
                    f" (Suggestions: {', '.join(f'{s.code} [{s.reason}]' for s in res.suggestions[:2])})"
                    if res.suggestions
                    else ""
                )
                print(f"    -> [NEW] Will store as [{res.code}]{sug_str}")

        print("\n--- Validation Check ---")
        issues = validate_quote(quote)
        if not issues:
            print("[PASS] All line items match mathematically and date/codes are valid.")
            return True

        print(f"[FAIL] Found {len(issues)} validation issue(s):")
        for issue in issues:
            print(f"  - {issue}")
        return False

    except Exception as e:
        print(f"[ERROR] Extraction failed: {e}")
        return False


def test_vehicle_extraction(file_path: str) -> bool:
    """Runs extraction and structural validation on a vehicle registration document (Talon/CIV)."""
    if not os.path.exists(file_path):
        print(f"[SKIP] Vehicle document not found: {file_path}")
        return False

    print(f"\n{'=' * 65}")
    print(f"TESTING VEHICLE EXTRACTION: {file_path}")
    print(f"{'=' * 65}")
    print("Uploading to Gemini and processing schema fallback chain...")

    try:
        doc = extract_vehicle_data(file_path)

        print("\n--- Extracted Vehicle Identifiers ---")
        print(f"License Plate: {doc.numar_inmatriculare}")
        print(f"Chassis / VIN: {doc.serie_sasiu}")

        print("\n--- Validation Check ---")
        issues = validate_vehicle(doc)
        if not issues:
            print("[PASS] VIN has exactly 17 characters, no invalid chars (I, O, Q), plate is populated.")
            return True

        print(f"[FAIL] Found {len(issues)} validation issue(s):")
        for issue in issues:
            print(f"  - {issue}")
        return False

    except Exception as e:
        print(f"[ERROR] Vehicle extraction failed: {e}")
        return False


def main() -> None:
    quote_file_path = "Oferta.pdf"
    vehicle_file_path = "Talon.pdf"

    quote_passed = test_quote_extraction(quote_file_path)
    vehicle_passed = test_vehicle_extraction(vehicle_file_path)

    print(f"\n{'=' * 65}")
    print("TEST SUITE SUMMARY")
    print(f"{'=' * 65}")
    print(f"Quote Extraction:   {'PASSED' if quote_passed else 'FAILED / SKIPPED'}")
    print(f"Vehicle Extraction: {'PASSED' if vehicle_passed else 'FAILED / SKIPPED'}")


if __name__ == "__main__":
    main()