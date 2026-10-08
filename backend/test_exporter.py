import os
import shutil
from pathlib import Path
from exporter import (
    ExportAllocation,
    ExportInvoicePayload,
    VehicleTarget,
    export_to_winmentor,
)
from nomenclature import PartStatus, init_db, resolve_part

TEST_EXPORT_DIR = str(Path(__file__).with_name("test_exports_temp"))
TEST_DB_PATH = str(Path(__file__).with_name("test_exporter_temp.db"))


def run_exporter_tests() -> None:
    if os.path.exists(TEST_EXPORT_DIR):
        shutil.rmtree(TEST_EXPORT_DIR)
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)

    try:
        init_db(TEST_DB_PATH)

        car_peugeot = VehicleTarget(
            numar_inmatriculare="B-119-RPI",
            serie_sasiu="VF7SXHMRVMT673522",
        )
        car_audi = VehicleTarget(
            numar_inmatriculare="IF-18-DJF",
            serie_sasiu="WAUZZZ8V5GA050511",
        )

        # Scenario: 2 brake parts go to B-119-RPI, 1 filter goes to IF-18-DJF,
        # and 5L of oil is split: 4L to IF-18-DJF and 1L stays in Stock (vehicle=None)
        payload = ExportInvoicePayload(
            supplier_name="MATEROM SRL",
            document_code="MTRF 915268324",
            quote_date="2026-10-07",
            allocations=[
                ExportAllocation(
                    part_code="GDB2063",
                    part_name="Set plăcuțe frână spate",
                    quantity=1.0,
                    unit_price=109.74,
                    vehicle=car_peugeot,
                ),
                ExportAllocation(
                    part_code="GIC404",
                    part_name="Senzor avertizare uzură plăcuțe frână",
                    quantity=1.0,
                    unit_price=38.06,
                    vehicle=car_peugeot,
                ),
                ExportAllocation(
                    part_code="HU7020Z",
                    part_name="Filtru ulei",
                    quantity=1.0,
                    unit_price=26.84,
                    vehicle=car_audi,
                ),
                ExportAllocation(
                    part_code="MOTYLG75W851L",
                    part_name="Ulei Motul 75W85 1L",
                    quantity=4.0,
                    unit_price=65.66,
                    vehicle=car_audi,
                ),
                ExportAllocation(
                    part_code="MOTYLG75W851L",
                    part_name="Ulei Motul 75W85 1L",
                    quantity=1.0,
                    unit_price=65.66,
                    vehicle=None,  # Goes to Magazie (Stock)
                ),
            ],
        )

        print("1. Running export_to_winmentor()...")
        result = export_to_winmentor(payload, export_dir=TEST_EXPORT_DIR, db_path=TEST_DB_PATH)

        # Split oil (4 + 1 = 5) should re-aggregate into 1 line on FACT_INT.TXT -> 4 total invoice lines
        assert result.invoice_lines_written == 4
        # 2 vehicles received parts -> 2 consumption vouchers in BON_CONS.TXT
        assert result.consumption_vouchers_written == 2
        assert result.nomenclature_updated == 4
        print(
            f"   [PASS] Wrote {result.invoice_lines_written} invoice lines and "
            f"{result.consumption_vouchers_written} consumption vouchers."
        )

        print("\n2. Verifying CRLF line endings and generated file contents...")
        fact_bytes = Path(result.fact_int_path).read_bytes()
        assert b"\r\n" in fact_bytes, "Expected Windows CRLF line endings in FACT_INT.TXT"
        assert result.bon_cons_path is not None
        bon_bytes = Path(result.bon_cons_path).read_bytes()
        assert b"\r\n" in bon_bytes, "Expected Windows CRLF line endings in BON_CONS.TXT"

        print("\n--- Generated FACT_INT.TXT ---")
        print(fact_bytes.decode("utf-8"))

        print("--- Generated BON_CONS.TXT ---")
        print(bon_bytes.decode("utf-8"))

        print("3. Verifying nomenclature.db was automatically updated...")
        res = resolve_part("GDB2063", "Set placute frana spate", db_path=TEST_DB_PATH)
        assert res.status == PartStatus.MATCH
        print(f"   [PASS] GDB2063 is now saved in nomenclature.db as '{res.name}'.")

        print("\n=== ALL MODULE 3 EXPORTER TESTS PASSED ===")

    finally:
        if os.path.exists(TEST_EXPORT_DIR):
            shutil.rmtree(TEST_EXPORT_DIR)
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)


if __name__ == "__main__":
    run_exporter_tests()