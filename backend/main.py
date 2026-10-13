import os
import shutil
import tempfile
from pathlib import Path
from typing import Any
from fastapi.staticfiles import StaticFiles

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
    StandaloneBonItem,
    StandaloneBonPayload,
    export_standalone_bon_consum,
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
    delete_part,
    init_db,
    list_all_parts,
    list_export_history,
    list_model_stats,
    resolve_parts,
    search_codes,
    update_or_add_part,
)
from seed_nomenclature import seed_from_winmentor_excel
from talon_scanner import (
    TalonCropConfig,
    build_local_scan_preview,
    render_document_pages,
)

DB_PATH: str = DEFAULT_DB_PATH
EXPORT_DIR: str = DEFAULT_EXPORT_DIR

app = FastAPI(title="FacturiGUI WinMentor Bridge", version="1.3.0")

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
    stoc: float = 0.0


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
    stoc: float
    sold: float
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
    supplier_cif: str | None = None  # Added CIF for the frontend
    document_code: str
    winmentor_doc_nr: str
    quote_date: str
    duplicate_warning: ExportLogOut | None
    validation_issues: list[str]
    parts: list[EnrichedPartOut]
    preview_pages: list[str]


class TalonPreviewResponse(BaseModel):
    warped_data_url: str
    scanned_full_data_url: str
    scanned_crop_data_url: str
    x1: float
    y1: float
    x2: float
    y2: float
    rotate_deg: int
    redact_owner_c: bool
    c_y1: float
    c_y2: float
    detected_quad: bool
    detected_c_section: bool


class VehicleUploadResponse(BaseModel):
    numar_inmatriculare: str
    serie_sasiu: str
    marca_model: str
    obs_preview: str
    validation_issues: list[str]


class SeedExcelResponse(BaseModel):
    articles_seeded: int
    db_path: str


class PartRecordOut(BaseModel):
    nr_crt: int
    part_code: str
    part_name: str
    stoc: float
    sold: float
    last_used: str | None


class StockListResponse(BaseModel):
    total_count: int
    total_sold_ron: float
    items: list[PartRecordOut]


class UpsertPartIn(BaseModel):
    part_code: str
    part_name: str


class ModelStatOut(BaseModel):
    model_name: str
    success_count: int
    rate_limit_429_count: int
    unavailable_503_count: int
    other_error_count: int
    avg_latency_ms: float
    in_cooldown: bool
    cooldown_remaining_s: int
    last_used: str | None


class VehicleTargetIn(BaseModel):
    numar_inmatriculare: str
    serie_sasiu: str = ""
    marca_model: str = ""
    departament: str = ""


class AllocationIn(BaseModel):
    part_code: str
    part_name: str
    quantity: float
    unit_price: float
    um: str = DEFAULT_UM
    is_new: bool = False  # Added flag for the agent
    vehicle: VehicleTargetIn | None = None
    departament: Department | None = None


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
    export_bundle: dict[str, Any]


class RetriggerRequestIn(BaseModel):
    archive_dir: str


class RetriggerResponseOut(BaseModel):
    bundle_json_path: str
    fact_int_path: str
    bon_cons_path: str | None


class StandaloneBonItemIn(BaseModel):
    part_code: str
    part_name: str
    um: str = DEFAULT_UM
    quantity: float = Field(gt=0)
    unit_price: float = Field(ge=0)
    is_new: bool = False  # Added flag for the agent


class StandaloneBonRequestIn(BaseModel):
    date: str
    doc_number: str
    gestiune: str = DEFAULT_GESTIUNE
    departament: str = ""
    vehicle: VehicleTargetIn | None = None
    items: list[StandaloneBonItemIn]
    deduct_from_stock: bool = True


class StandaloneBonResponseOut(BaseModel):
    doc_number: str
    archive_dir: str
    bon_cons_path: str
    lines_written: int
    total_valoare: float
    export_bundle: dict[str, Any]


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
        preview_pages = render_document_pages(temp_path, max_pages=6)
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
                    stoc=res.stoc,
                    sold=res.sold,
                    suggestions=[
                        SuggestionOut(
                            code=s.code,
                            name=s.name,
                            reason=s.reason,
                            score=s.score,
                            stoc=s.stoc,
                        )
                        for s in res.suggestions
                    ],
                )
            )

        return QuoteUploadResponse(
            supplier_name=quote.supplier_name,
            supplier_cif=getattr(quote, "supplier_cif", None),  # Inject the extracted CIF
            document_code=quote.document_code,
            winmentor_doc_nr=extract_winmentor_doc_number(quote.document_code),
            quote_date=quote.quote_date,
            duplicate_warning=_to_log_out(dup_entry) if dup_entry else None,
            validation_issues=issues,
            parts=enriched,
            preview_pages=preview_pages,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@app.post("/api/preprocess-vehicle-preview", response_model=TalonPreviewResponse)
def preprocess_vehicle_preview(
    file: UploadFile = File(...),
) -> TalonPreviewResponse:
    temp_path = _save_upload_to_temp(file, default_ext=".jpg")
    try:
        prev = build_local_scan_preview(temp_path, custom_crop=None)
        return TalonPreviewResponse(
            warped_data_url=prev.warped_data_url,
            scanned_full_data_url=prev.scanned_full_data_url,
            scanned_crop_data_url=prev.scanned_crop_data_url,
            x1=prev.suggested_crop.x1,
            y1=prev.suggested_crop.y1,
            x2=prev.suggested_crop.x2,
            y2=prev.suggested_crop.y2,
            rotate_deg=prev.suggested_crop.rotate_deg,
            redact_owner_c=prev.suggested_crop.redact_owner_c,
            c_y1=prev.suggested_crop.c_y1,
            c_y2=prev.suggested_crop.c_y2,
            detected_quad=prev.detected_quad,
            detected_c_section=prev.detected_c_section,
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
    c_y1: float = Form(default=0.22),
    c_y2: float = Form(default=0.36),
) -> VehicleUploadResponse:
    temp_path = _save_upload_to_temp(file, default_ext=".png")
    try:
        custom_crop = (
            TalonCropConfig(
                x1=x1,
                y1=y1,
                x2=x2,
                y2=y2,
                rotate_deg=rotate_deg,
                redact_owner_c=redact_owner_c,
                c_y1=c_y1,
                c_y2=c_y2,
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
    q: str = Query(..., min_length=1, description="Code or part name query"),
    limit: int = Query(default=12, ge=1, le=50),
) -> list[SuggestionOut]:
    hits = search_codes(q, limit=limit, db_path=DB_PATH)
    return [
        SuggestionOut(
            code=h.code, name=h.name, reason=h.reason, score=h.score, stoc=h.stoc
        )
        for h in hits
    ]


@app.get("/api/nomenclature/list", response_model=StockListResponse)
def get_nomenclature_stock_list(
    search: str = Query(default=""),
    only_in_stock: bool = Query(default=False),
    limit: int = Query(default=150, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> StockListResponse:
    total, total_sold, records = list_all_parts(
        search=search,
        only_in_stock=only_in_stock,
        limit=limit,
        offset=offset,
        db_path=DB_PATH,
    )
    return StockListResponse(
        total_count=total,
        total_sold_ron=total_sold,
        items=[
            PartRecordOut(
                nr_crt=r.nr_crt,
                part_code=r.part_code,
                part_name=r.part_name,
                stoc=r.stoc,
                sold=r.sold,
                last_used=r.last_used,
            )
            for r in records
        ],
    )


@app.post("/api/nomenclature/upsert")
def upsert_nomenclature_part(req: UpsertPartIn) -> dict[str, str]:
    try:
        update_or_add_part(req.part_code, req.part_name, db_path=DB_PATH)
        return {"status": "ok"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.delete("/api/nomenclature/{part_code}")
def remove_nomenclature_part(part_code: str) -> dict[str, bool]:
    deleted = delete_part(part_code, db_path=DB_PATH)
    return {"deleted": deleted}


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


@app.get("/api/models/stats", response_model=list[ModelStatOut])
def get_gemini_model_stats() -> list[ModelStatOut]:
    stats = list_model_stats(db_path=DB_PATH)
    return [
        ModelStatOut(
            model_name=s.model_name,
            success_count=s.success_count,
            rate_limit_429_count=s.rate_limit_429_count,
            unavailable_503_count=s.unavailable_503_count,
            other_error_count=s.other_error_count,
            avg_latency_ms=s.avg_latency_ms,
            in_cooldown=s.in_cooldown,
            cooldown_remaining_s=s.cooldown_remaining_s,
            last_used=s.last_used,
        )
        for s in stats
    ]


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
                        departament="",
                    )
                    if a.vehicle is not None
                    else None
                ),
                departament=a.departament,
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
        
        # INJECT the `is_new` flags dynamically into the agent bundle data 
        # so we don't have to break the ExportAllocation schemas
        export_bundle = result.bundle_data
        new_codes = {a.part_code for a in req.allocations if a.is_new}
        
        if export_bundle.get("factura_intrare"):
            # INJECT CIF FOR THE AGENT
            export_bundle["factura_intrare"]["furnizor_cif"] = req.supplier_cif 
            
            for item in export_bundle["factura_intrare"].get("items", []):
                if item.get("part_code") in new_codes:
                    item["is_new"] = True
                    
        for bon in export_bundle.get("bonuri_consum", []):
            for item in bon.get("items", []):
                if item.get("part_code") in new_codes:
                    item["is_new"] = True

        return ExportResponseOut(
            winmentor_doc_nr=result.winmentor_doc_nr,
            archive_dir=result.archive_dir,
            bundle_json_path=result.bundle_json_path,
            fact_int_path=result.fact_int_path,
            bon_cons_path=result.bon_cons_path,
            invoice_lines_written=result.invoice_lines_written,
            consumption_vouchers_written=result.consumption_vouchers_written,
            nomenclature_updated=result.nomenclature_updated,
            export_bundle=export_bundle,
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


@app.post("/api/export-bon-consum", response_model=StandaloneBonResponseOut)
def export_bon_consum_standalone(req: StandaloneBonRequestIn) -> StandaloneBonResponseOut:
    try:
        items = [
            StandaloneBonItem(
                part_code=i.part_code,
                part_name=i.part_name,
                um=i.um,
                quantity=i.quantity,
                unit_price=i.unit_price,
            )
            for i in req.items
        ]
        target_veh = (
            VehicleTarget(
                numar_inmatriculare=req.vehicle.numar_inmatriculare,
                serie_sasiu=req.vehicle.serie_sasiu,
                marca_model=req.vehicle.marca_model,
                departament=req.vehicle.departament,
            )
            if req.vehicle is not None
            else None
        )
        payload = StandaloneBonPayload(
            date=req.date,
            doc_number=req.doc_number,
            gestiune=req.gestiune,
            departament=req.departament,
            vehicle=target_veh,
            items=items,
            deduct_from_stock=req.deduct_from_stock,
        )
        res = export_standalone_bon_consum(payload, export_dir=EXPORT_DIR, db_path=DB_PATH)
        
        # INJECT the `is_new` flags dynamically into the agent bundle data 
        export_bundle = res.bundle_data
        new_codes = {i.part_code for i in req.items if i.is_new}
        
        for bon in export_bundle.get("bonuri_consum", []):
            for item in bon.get("items", []):
                if item.get("part_code") in new_codes:
                    item["is_new"] = True
                    
        return StandaloneBonResponseOut(
            doc_number=res.doc_number,
            archive_dir=res.archive_dir,
            bon_cons_path=res.bon_cons_path,
            lines_written=res.lines_written,
            total_valoare=res.total_valoare,
            export_bundle=export_bundle,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

DIST_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"

if DIST_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(DIST_DIR / "assets")), name="assets")

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        # Serve favicon or direct static files if requested
        target = DIST_DIR / full_path
        if full_path and target.is_file():
            return FileResponse(target)
        # Fall back to index.html for Vue SPA routing
        return FileResponse(DIST_DIR / "index.html")