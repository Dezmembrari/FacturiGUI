import os
from pathlib import Path
from nomenclature import PartStatus, resolve_part, resolve_parts, search_codes
from seed_nomenclature import seed_from_winmentor_excel

TEST_DB = str(Path(__file__).with_name("test_sept_temp.db"))
SEPT_EXCEL = str(Path(__file__).with_name("SEPT.xlsx"))


def run_real_data_tests() -> None:
    if not os.path.exists(SEPT_EXCEL):
        print(f"Place '{SEPT_EXCEL}' in the backend folder first.")
        return
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

    try:
        print("=== 1. Seeding DB from real SEPT.xlsx ===")
        count = seed_from_winmentor_excel(SEPT_EXCEL, TEST_DB)
        assert count == 134, f"Expected 134 unique codes, got {count}"

        print("\n=== 2. Testing Sprint Car Invoice IN26 000001380 against SEPT.xlsx ===")
        sprint_items = [
            ("37180074G", "LAC HS PERMAFAST 8007/1LT"),
            ("37130080G", "CATALIZ. PERMAFAST 3008 HS/1LT"),
            ("9010050", "BUR.ABRAZ.BEJ MICROFINE 2 FETE"),
            ("9010040", "BUR.ABRAZ.VERDE ULTRAFINE 2 FETE"),
            ("CATALIZ", "CATALIZ ACRIL NORMAL HS C15/200ML"),
        ]
        res = resolve_parts(sprint_items, TEST_DB)

        # 37180074G -> Exact MATCH with 'LAC HS PERMAFAST 8007 / 1 L'
        assert res[0].status == PartStatus.MATCH and res[0].code == "37180074G"
        print(f"  [PASS] 37180074G -> {res[0].status.value} ('{res[0].name}')")

        # 37130080G -> Exact MATCH via 'cataliz' == 'intaritor' synonym!
        assert res[1].status == PartStatus.MATCH and res[1].code == "37130080G"
        print(f"  [PASS] 37130080G -> {res[1].status.value} ('{res[1].name}')")

        # 9010050 -> Exact MATCH with 'BUR ABRAZ BEJ MICROFINE 2 FETE'
        assert res[2].status == PartStatus.MATCH and res[2].code == "9010050"
        print(f"  [PASS] 9010050   -> {res[2].status.value} ('{res[2].name}')")

        # 9010040 -> NEW, but suggests 9010050 via similar_characters!
        assert res[3].status == PartStatus.NEW and res[3].suggestions[0].code == "9010050"
        print(f"  [PASS] 9010040   -> NEW (Suggested lookalike: '{res[3].suggestions[0].code}')")

        # CATALIZ -> Exact MATCH with 'CATALIZ ACRIL NORMAL HS C15/200ML'
        assert res[4].status == PartStatus.MATCH and res[4].code == "CATALIZ"
        print(f"  [PASS] CATALIZ   -> {res[4].status.value} ('{res[4].name}')")

        print("\n=== 3. Testing Autonet Finixa Codes (FTDSX011146176) ===")
        finixa = resolve_part("CCE-MAS12", "Masca praf Finixa cu supapa P2", TEST_DB)
        assert finixa.status == PartStatus.MATCH and finixa.code == "CCE-MAS 12"
        print(f"  [PASS] 'CCE-MAS12' auto-healed separators to '{finixa.code}'")

        print("\n=== 4. Testing Autocomplete on 134 Real Articles ===")
        hits = search_codes("permafast", db_path=TEST_DB)
        assert len(hits) == 2
        print(f"  [PASS] Search 'permafast' found: {[h.code for h in hits]}")

        print("\n=== ALL REAL-DATA NOMENCLATURE TESTS PASSED ===")
    finally:
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)


if __name__ == "__main__":
    run_real_data_tests()