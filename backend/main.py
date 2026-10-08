import os
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from exporter import (
    DEFAULT_EXPORT_DIR,
    DEFAULT_GESTIUNE,
    DEFAULT_TVA_PERCENT,
    DEFAULT_UM,
    Department,
    ExportAllocation,
    ExportInvoicePayload,
    VehicleTarget,
    export_to_winmentor,
    extract_winmentor_doc_number,
    format_bon_obs,
    retrigger_archived_export,
)
from extractor import (
    extract_quote_data,
    extract_vehicle_data,
    validate_quote,
    validate_vehicle,
)
from nomenclature import (
    DEFAULT_DB_PATH,
    ExportLogEntry,
    check_duplicate_invoice,
    init_db,
    list_export_history,
    resolve_parts,
    search_codes,
)
from seed_nomenclature import seed_from_winmentor_excel
from talon_scanner import TalonCropConfig, build_local_scan_preview

DB_PATH: str = DEFAULT_DB_PATH
EXPORT_DIR: str = DEFAULT_EXPORT_DIR

app = FastAPI(title="FacturiGUI WinMentor Bridge", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- API Schemas -------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str
    db_path: str
    export_dir: str
    departments: list[str]


class SuggestionOut(BaseModel):
    code: str
    name: str
    reason: str
    score: float


class EnrichedPartOut(BaseModel):
    quote_code: str
    quote_name: str
    quantity: float
    unit_price: float
    total_price: float
    status: str
    resolved_code: str
    canonical_name: str
    matched_via: str
    suggestions: list[SuggestionOut]


class ExportLogOut(BaseModel):
    doc_key: str
    supplier_name: str
    document_code: str
    winmentor_doc_nr: str
    quote_date: str
    total_fara_tva: float
    archive_dir: str
    exported_at: str


class QuoteUploadResponse(BaseModel):
    supplier_name: str
    document_code: str
    winmentor_doc_nr: str
    quote_date: str
    duplicate_warning: ExportLogOut | None
    validation_issues: list[str]
    parts: list[EnrichedPartOut]


class TalonPreviewResponse(BaseModel):
    warped_data_url: str
    scanned_crop_data_url: str
    x1: float
    y1: float
    x2: float
    y2: float
    rotate_deg: int
    redact_owner_c: bool
    detected_quad: bool


class VehicleUploadResponse(BaseModel):
    numar_inmatriculare: str
    serie_sasiu: str
    marca_model: str
    obs_preview: str
    validation_issues: list[str]


class SeedExcelResponse(BaseModel):
    articles_seeded: int
    db_path: str


class VehicleTargetIn(BaseModel):
    numar_inmatriculare: str
    serie_sasiu: str = ""
    marca_model: str = ""
    departament: Department = Department.TINICHIGERIE


class AllocationIn(BaseModel):
    part_code: str
    part_name: str
    quantity: float
    unit_price: float
    um: str = DEFAULT_UM
    vehicle: VehicleTargetIn | None = None


class ExportRequestIn(BaseModel):
    supplier_name: str
    document_code: str
    quote_date: str
    supplier_cif: str | None = None
    tva_percent: int = Field(default=DEFAULT_TVA_PERCENT)
    gestiune: str = Field(default=DEFAULT_GESTIUNE)
    allow_duplicate: bool = False
    allocations: list[AllocationIn]


class ExportResponseOut(BaseModel):
    winmentor_doc_nr: str
    archive_dir: str
    bundle_json_path: str
    fact_int_path: str
    bon_cons_path: str | None
    invoice_lines_written: int
    consumption_vouchers_written: int
    nomenclature_updated: int


class RetriggerRequestIn(BaseModel):
    archive_dir: str


class RetriggerResponseOut(BaseModel):
    bundle_json_path: str
    fact_int_path: str
    bon_cons_path: str | None


# --- Helpers -----------------------------------------------------------------------------

def _save_upload_to_temp(upload: UploadFile, default_ext: str = ".pdf") -> str:
    suffix = Path(upload.filename or f"document{default_ext}").suffix or default_ext
    fd, temp_path = tempfile.mkstemp(suffix=suffix, prefix="facturigui_")
    os.close(fd)
    with open(temp_path, "wb") as buffer:
        shutil.copyfileobj(upload.file, buffer)
    return temp_path


def _to_log_out(entry: ExportLogEntry) -> ExportLogOut:
    return ExportLogOut(
        doc_key=entry.doc_key,
        supplier_name=entry.supplier_name,
        document_code=entry.document_code,
        winmentor_doc_nr=entry.winmentor_doc_nr,
        quote_date=entry.quote_date,
        total_fara_tva=entry.total_fara_tva,
        archive_dir=entry.archive_dir,
        exported_at=entry.exported_at,
    )


# --- Endpoints ---------------------------------------------------------------------------

@app.get("/api/health", response_model=HealthResponse)
def health_check() -> HealthResponse:
    init_db(DB_PATH)
    Path(EXPORT_DIR, "archive").mkdir(parents=True, exist_ok=True)
    return HealthResponse(
        status="ok",
        db_path=DB_PATH,
        export_dir=EXPORT_DIR,
        departments=[d.value for d in Department],
    )


@app.post("/api/upload-quote", response_model=QuoteUploadResponse)
def upload_quote(file: UploadFile = File(...)) -> QuoteUploadResponse:
    temp_path = _save_upload_to_temp(file, default_ext=".pdf")
    try:
        quote = extract_quote_data(temp_path)
        issues = validate_quote(quote)
        resolutions = resolve_parts(
            [(p.part_code, p.part_name) for p in quote.parts],
            db_path=DB_PATH,
        )
        dup_entry = check_duplicate_invoice(
            quote.supplier_name,
            quote.document_code,
            db_path=DB_PATH,
        )

        enriched: list[EnrichedPartOut] = []
        for item, res in zip(quote.parts, resolutions):
            enriched.append(
                EnrichedPartOut(
                    quote_code=item.part_code,
                    quote_name=item.part_name,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    total_price=item.total_price,
                    status=res.status.value,
                    resolved_code=res.code,
                    canonical_name=res.name,
                    matched_via=res.matched_via,
                    suggestions=[
                        SuggestionOut(
                            code=s.code,
                            name=s.name,
                            reason=s.reason,
                            score=s.score,
                        )
                        for s in res.suggestions
                    ],
                )
            )

        return QuoteUploadResponse(
            supplier_name=quote.supplier_name,
            document_code=quote.document_code,
            winmentor_doc_nr=extract_winmentor_doc_number(quote.document_code),
            quote_date=quote.quote_date,
            duplicate_warning=_to_log_out(dup_entry) if dup_entry else None,
            validation_issues=issues,
            parts=enriched,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@app.post("/api/preprocess-vehicle-preview", response_model=TalonPreviewResponse)
def preprocess_vehicle_preview(
    file: UploadFile = File(...),
    x1: float | None = Form(default=None),
    y1: float | None = Form(default=None),
    x2: float | None = Form(default=None),
    y2: float | None = Form(default=None),
    rotate_deg: int = Form(default=0),
    redact_owner_c: bool = Form(default=False),
) -> TalonPreviewResponse:
    """
    100% LOCAL endpoint (zero Google calls):
    Perspective-flattens a distant Talon photo, crops only the Top-Left A-to-K area,
    applies the scanned-document filter, and returns Base64 images for operator confirmation.
    """
    temp_path = _save_upload_to_temp(file, default_ext=".jpg")
    try:
        custom_crop = (
            TalonCropConfig(
                x1=x1,
                y1=y1,
                x2=x2,
                y2=y2,
                rotate_deg=rotate_deg,
                redact_owner_c=redact_owner_c,
            )
            if (x1 is not None and y1 is not None and x2 is not None and y2 is not None)
            else None
        )
        prev = build_local_scan_preview(temp_path, custom_crop=custom_crop)
        return TalonPreviewResponse(
            warped_data_url=prev.warped_data_url,
            scanned_crop_data_url=prev.scanned_crop_data_url,
            x1=prev.suggested_crop.x1,
            y1=prev.suggested_crop.y1,
            x2=prev.suggested_crop.x2,
            y2=prev.suggested_crop.y2,
            rotate_deg=prev.suggested_crop.rotate_deg,
            redact_owner_c=prev.suggested_crop.redact_owner_c,
            detected_quad=prev.detected_quad,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@app.post("/api/upload-vehicle", response_model=VehicleUploadResponse)
def upload_vehicle(
    file: UploadFile = File(...),
    x1: float | None = Form(default=None),
    y1: float | None = Form(default=None),
    x2: float | None = Form(default=None),
    y2: float | None = Form(default=None),
    rotate_deg: int = Form(default=0),
    redact_owner_c: bool = Form(default=False),
) -> VehicleUploadResponse:
    """
    Crops and scan-enhances ONLY the Top-Left A-to-K region locally, then uploads
    only that cropped PNG patch to Gemini OCR.
    """
    temp_path = _save_upload_to_temp(file, default_ext=".pdf")
    try:
        custom_crop = (
            TalonCropConfig(
                x1=x1,
                y1=y1,
                x2=x2,
                y2=y2,
                rotate_deg=rotate_deg,
                redact_owner_c=redact_owner_c,
            )
            if (x1 is not None and y1 is not None and x2 is not None and y2 is not None)
            else None
        )
        doc = extract_vehicle_data(temp_path, crop_config=custom_crop)
        issues = validate_vehicle(doc)
        preview_target = VehicleTarget(
            numar_inmatriculare=doc.numar_inmatriculare,
            serie_sasiu=doc.serie_sasiu,
            marca_model=doc.marca_model,
        )
        return VehicleUploadResponse(
            numar_inmatriculare=doc.numar_inmatriculare,
            serie_sasiu=doc.serie_sasiu,
            marca_model=doc.marca_model,
            obs_preview=format_bon_obs(preview_target),
            validation_issues=issues,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@app.get("/api/nomenclature/search", response_model=list[SuggestionOut])
def search_nomenclature(
    q: str = Query(..., min_length=2, description="Code or part name query"),
    limit: int = Query(default=10, ge=1, le=50),
) -> list[SuggestionOut]:
    hits = search_codes(q, limit=limit, db_path=DB_PATH)
    return [
        SuggestionOut(code=h.code, name=h.name, reason=h.reason, score=h.score)
        for h in hits
    ]


@app.post("/api/nomenclature/seed-excel", response_model=SeedExcelResponse)
def seed_nomenclature_excel(file: UploadFile = File(...)) -> SeedExcelResponse:
    temp_path = _save_upload_to_temp(file, default_ext=".xlsx")
    try:
        count = seed_from_winmentor_excel(temp_path, db_path=DB_PATH)
        return SeedExcelResponse(articles_seeded=count, db_path=DB_PATH)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@app.post("/api/export", response_model=ExportResponseOut)
def export_invoice(req: ExportRequestIn) -> ExportResponseOut:
    try:
        allocations = [
            ExportAllocation(
                part_code=a.part_code,
                part_name=a.part_name,
                quantity=a.quantity,
                unit_price=a.unit_price,
                um=a.um,
                vehicle=(
                    VehicleTarget(
                        numar_inmatriculare=a.vehicle.numar_inmatriculare,
                        serie_sasiu=a.vehicle.serie_sasiu,
                        marca_model=a.vehicle.marca_model,
                        departament=a.vehicle.departament,
                    )
                    if a.vehicle is not None
                    else None
                ),
            )
            for a in req.allocations
        ]
        payload = ExportInvoicePayload(
            supplier_name=req.supplier_name,
            document_code=req.document_code,
            quote_date=req.quote_date,
            supplier_cif=req.supplier_cif,
            tva_percent=req.tva_percent,
            gestiune=req.gestiune,
            allow_duplicate=req.allow_duplicate,
            allocations=allocations,
        )
        result = export_to_winmentor(
            payload,
            export_dir=EXPORT_DIR,
            db_path=DB_PATH,
        )
        return ExportResponseOut(
            winmentor_doc_nr=result.winmentor_doc_nr,
            archive_dir=result.archive_dir,
            bundle_json_path=result.bundle_json_path,
            fact_int_path=result.fact_int_path,
            bon_cons_path=result.bon_cons_path,
            invoice_lines_written=result.invoice_lines_written,
            consumption_vouchers_written=result.consumption_vouchers_written,
            nomenclature_updated=result.nomenclature_updated,
        )
    except ValueError as e:
        msg = str(e)
        status_code = 409 if "Duplicate invoice blocked" in msg else 400
        raise HTTPException(status_code=status_code, detail=msg) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/exports/history", response_model=list[ExportLogOut])
def get_export_history(limit: int = Query(default=50, ge=1, le=200)) -> list[ExportLogOut]:
    entries = list_export_history(limit=limit, db_path=DB_PATH)
    return [_to_log_out(e) for e in entries]


@app.post("/api/exports/retrigger", response_model=RetriggerResponseOut)
def retrigger_export(req: RetriggerRequestIn) -> RetriggerResponseOut:
    try:
        paths = retrigger_archived_export(req.archive_dir, export_dir=EXPORT_DIR)
        return RetriggerResponseOut(
            bundle_json_path=str(paths["bundle_json_path"]),
            fact_int_path=str(paths["fact_int_path"]),
            bon_cons_path=paths["bon_cons_path"],
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e