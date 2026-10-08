import os
import shutil
from pathlib import Path
from exporter import (
    Department,
    ExportAllocation,
    ExportInvoicePayload,
    VehicleTarget,
    export_to_winmentor,
    extract_winmentor_doc_number,
    format_bon_obs,
    retrigger_archived_export,
)
from nomenclature import init_db

TEST_EXPORT_DIR = str(Path(__file__).with_name("test_exports_temp"))
TEST_DB_PATH = str(Path(__file__).with_name("test_exporter_temp.db"))


def run_exporter_tests() -> None:
    if os.path.exists(TEST_EXPORT_DIR):
        shutil.rmtree(TEST_EXPORT_DIR)
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)

    try:
        init_db(TEST_DB_PATH)

        print("1. Testing WinMentor trailing invoice number extraction (Rule 2.2)...")
        assert extract_winmentor_doc_number("IN26 000001380") == "1380"
        assert extract_winmentor_doc_number("FB2AX10394098") == "394098"
        assert extract_winmentor_doc_number("22608977520") == "977520"
        assert extract_winmentor_doc_number("MSA 3241") == "3241"
        print("   [PASS] All supplier invoice codes stripped to trailing digits.")

        print("\n2. Testing Bon Consum 4.Obs vehicle formatting ('opel corsa ct61mlg')...")
        car_opel = VehicleTarget(
            numar_inmatriculare="CT-61-MLG",
            serie_sasiu="W0L00000000000000",
            marca_model="OPEL CORSA",
            departament=Department.TINICHIGERIE,
        )
        assert format_bon_obs(car_opel) == "opel corsa ct61mlg"
        print(f"   [PASS] Formatted 4.Obs: '{format_bon_obs(car_opel)}' ({car_opel.departament.value})")

        print("\n3. Running export_to_winmentor() with archiving...")
        payload = ExportInvoicePayload(
            supplier_name="AUTONET IMPORT SRL",
            document_code="FB2AX10394623",
            quote_date="2026-10-02",
            allocations=[
                ExportAllocation(
                    part_code="HU7020Z",
                    part_name="Filtru ulei",
                    quantity=1.0,
                    unit_price=26.84,
                    vehicle=car_opel,
                ),
                ExportAllocation(
                    part_code="LIQ LL III 5W30 5L",
                    part_name="LIQUI MOLY Longlife III 5W-30 5L",
                    quantity=1.0,
                    unit_price=333.83,  # 332.37 + 1.46 eco-tax rolled in
                    vehicle=None,       # Stays in MAGAZIE
                ),
            ],
        )
        res = export_to_winmentor(payload, export_dir=TEST_EXPORT_DIR, db_path=TEST_DB_PATH)
        assert res.winmentor_doc_nr == "394623"
        assert Path(res.archive_dir).exists()
        print(f"   [PASS] Archived to: {res.archive_dir}")

        print("\n4. Testing duplicate invoice protection...")
        try:
            export_to_winmentor(payload, export_dir=TEST_EXPORT_DIR, db_path=TEST_DB_PATH)
            raise AssertionError("Should have blocked duplicate invoice!")
        except ValueError as e:
            print(f"   [PASS] Duplicate blocked: {e}")

        print("\n5. Testing 1-click archive re-trigger...")
        retriggered = retrigger_archived_export(res.archive_dir, export_dir=TEST_EXPORT_DIR)
        assert Path(str(retriggered["bundle_json_path"])).exists()
        print("   [PASS] Re-triggered archive bundle successfully.")

        print("\n=== ALL UPGRADED MODULE 3 TESTS PASSED ===")
    finally:
        if os.path.exists(TEST_EXPORT_DIR):
            shutil.rmtree(TEST_EXPORT_DIR)
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)


if __name__ == "__main__":
    run_exporter_tests()