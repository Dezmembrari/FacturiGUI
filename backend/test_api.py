import os
import shutil
from pathlib import Path
from typing import Any, Protocol, cast

from fastapi.testclient import TestClient

import main

TEST_DB = str(Path(__file__).with_name("test_api_temp.db"))
TEST_EXPORT_DIR = str(Path(__file__).with_name("test_api_exports_temp"))
SEPT_EXCEL = str(Path(__file__).with_name("SEPT.xlsx"))
OFERTA_PDF = str(Path(__file__).with_name("Oferta.pdf"))
TALON_PDF = str(Path(__file__).with_name("Talon.pdf"))


class _ResponseProto(Protocol):
    status_code: int
    text: str

    def json(self) -> Any: ...


class _ClientProto(Protocol):
    def get(self, url: str, *, params: dict[str, Any] | None = None) -> _ResponseProto: ...

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> _ResponseProto: ...


main.DB_PATH = TEST_DB
main.EXPORT_DIR = TEST_EXPORT_DIR

client: _ClientProto = cast(_ClientProto, TestClient(main.app))


def run_api_tests() -> None:
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.exists(TEST_EXPORT_DIR):
        shutil.rmtree(TEST_EXPORT_DIR)

    try:
        print("1. Testing GET /api/health...")
        r_health = client.get("/api/health")
        assert r_health.status_code == 200
        health_data = cast(dict[str, Any], r_health.json())
        assert health_data["departments"] == ["TINICHIGERIE", "VOPSITORIE", "MECANICA"]
        print(f"   [PASS] Health OK. Departments: {health_data['departments']}")

        print("\n2. Testing POST /api/nomenclature/seed-excel with SEPT.xlsx...")
        with open(SEPT_EXCEL, "rb") as f:
            r_seed = client.post(
                "/api/nomenclature/seed-excel",
                files={
                    "file": (
                        "SEPT.xlsx",
                        f,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
            )
        assert r_seed.status_code == 200, r_seed.text
        seed_data = cast(dict[str, Any], r_seed.json())
        assert seed_data["articles_seeded"] == 134
        print("   [PASS] Seeded 134 articles via API upload.")

        print("\n3. Testing GET /api/nomenclature/search?q=permafast...")
        r_search = client.get("/api/nomenclature/search", params={"q": "permafast"})
        assert r_search.status_code == 200
        hits = cast(list[dict[str, Any]], r_search.json())
        assert len(hits) == 2
        print(f"   [PASS] Search returned: {[str(h['code']) for h in hits]}")

        print("\n4. Testing POST /api/export (Vehicle + Stock split, TINICHIGERIE, 4.Obs)...")
        export_payload: dict[str, Any] = {
            "supplier_name": "SPRINT CAR DISTRIBUTION",
            "document_code": "IN26 000001380",
            "quote_date": "2026-10-07",
            "allocations": [
                {
                    "part_code": "37180074G",
                    "part_name": "LAC HS PERMAFAST 8007 / 1 L",
                    "quantity": 4.0,
                    "unit_price": 115.60,
                    "um": "Buc",
                    "vehicle": {
                        "numar_inmatriculare": "CT-61-MLG",
                        "serie_sasiu": "W0L00000000000000",
                        "marca_model": "OPEL CORSA",
                        "departament": "TINICHIGERIE",
                    },
                },
                {
                    "part_code": "37180074G",
                    "part_name": "LAC HS PERMAFAST 8007 / 1 L",
                    "quantity": 2.0,
                    "unit_price": 115.60,
                    "um": "Buc",
                    "vehicle": None,  # Stays in MAGAZIE
                },
            ],
        }
        r_export = client.post("/api/export", json=export_payload)
        assert r_export.status_code == 200, r_export.text
        exp_data = cast(dict[str, Any], r_export.json())
        assert exp_data["winmentor_doc_nr"] == "1380"
        assert exp_data["invoice_lines_written"] == 1
        assert exp_data["consumption_vouchers_written"] == 1
        print(
            f"   [PASS] Exported WinMentor Doc #{exp_data['winmentor_doc_nr']} "
            f"to {exp_data['archive_dir']}"
        )

        print("\n5. Testing HTTP 409 Conflict on duplicate invoice export...")
        r_dup = client.post("/api/export", json=export_payload)
        assert r_dup.status_code == 409
        dup_data = cast(dict[str, Any], r_dup.json())
        print(f"   [PASS] Duplicate blocked with HTTP 409: {dup_data['detail']}")

        print("\n6. Testing GET /api/exports/history & POST /api/exports/retrigger...")
        r_hist = client.get("/api/exports/history")
        assert r_hist.status_code == 200
        history = cast(list[dict[str, Any]], r_hist.json())
        assert len(history) == 1
        r_retrig = client.post(
            "/api/exports/retrigger",
            json={"archive_dir": str(history[0]["archive_dir"])},
        )
        assert r_retrig.status_code == 200
        print("   [PASS] Export history listed and archive re-triggered with one click.")

        if os.path.exists(OFERTA_PDF):
            print("\n7. Testing POST /api/upload-quote (Live Gemini OCR + Nomenclature)...")
            with open(OFERTA_PDF, "rb") as f:
                r_quote = client.post(
                    "/api/upload-quote",
                    files={"file": ("Oferta.pdf", f, "application/pdf")},
                )
            assert r_quote.status_code == 200, r_quote.text
            q_data = cast(dict[str, Any], r_quote.json())
            q_parts = cast(list[dict[str, Any]], q_data["parts"])
            print(
                f"   [PASS] Extracted {q_data['supplier_name']} #{q_data['winmentor_doc_nr']} "
                f"with {len(q_parts)} enriched line items."
            )

        if os.path.exists(TALON_PDF):
            print("\n8. Testing POST /api/upload-vehicle (Live Gemini OCR + 4.Obs preview)...")
            with open(TALON_PDF, "rb") as f:
                r_veh = client.post(
                    "/api/upload-vehicle",
                    files={"file": ("Talon.pdf", f, "application/pdf")},
                )
            assert r_veh.status_code == 200, r_veh.text
            v_data = cast(dict[str, Any], r_veh.json())
            print(
                f"   [PASS] Extracted Plate={v_data['numar_inmatriculare']} | "
                f"Model='{v_data['marca_model']}' | 4.Obs='{v_data['obs_preview']}'"
            )

        print("\n=== ALL MODULE 4 FASTAPI TESTS (INCLUDING LIVE OCR) PASSED ===")
    finally:
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        if os.path.exists(TEST_EXPORT_DIR):
            shutil.rmtree(TEST_EXPORT_DIR)


if __name__ == "__main__":
    run_api_tests()