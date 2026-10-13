import os
import re
import time
from datetime import datetime
from typing import TypeVar

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from nomenclature import DEFAULT_DB_PATH, get_prioritized_models, record_model_attempt
from talon_scanner import TalonCropConfig, prepare_talon_for_ocr

load_dotenv()

client = genai.Client()

MODEL_CHAIN = [
    "gemini-2.5-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-3.6-flash",
]

ATTEMPT_TIMEOUT_MS = 45_000

FALLBACK_TAGS = (
    "503",
    "504",
    "DEADLINE",
    "UNAVAILABLE",
    "429",
    "RESOURCE_EXHAUSTED",
    "NOT_FOUND",
)
RETRY_SAME_MODEL_TAGS = ("503", "UNAVAILABLE")

T = TypeVar("T", bound=BaseModel)


# --- Data Schemas ------------------------------------------------------------------------

class PartItem(BaseModel):
    part_code: str = Field(
        description=(
            "Primary manufacturer part code or paint code for WinMentor's 'Cod extern' column. "
            "RULES BY SUPPLIER: "
            "1) Materom ('GDB2063 - 11-477-298 - TRW - OEM'): take ONLY the first segment before ' - ' ('GDB2063'). "
            "2) Autonet ('FD4287313P PRA -Carenaj roata' or '21904FE -Brat suspensie'): take the code before '-' ('FD4287313P PRA', '21904FE'). Ignore the 8-digit customs code ('Cod tarifar vamal'). "
            "3) AD Auto Total ('70-31414-10 SET GARNITURI... NC8: ...'): take ONLY the leading code token ('70-31414-10', '6PK1698CT'). Ignore NC8 and CPV codes. "
            "4) Sprint Car Distribution: NEVER use 'Cod articol client' (like '7564H.3500' or '0100H.1000') and NEVER use 13-digit EAN barcodes. Extract the manufacturer code from 'Descriere articol' (like '37180074G', '37130080G', '9010050', or paint code 'MB518'). If none exists, use the first uppercase product noun ('CATALIZ', 'FOND', 'CHIT', 'LAC'). "
            "5) Invoices without a code column (e.g. Marcos Serv 'USA STANGA FATA DACIA DOKKER'): use the main part noun in uppercase ('USA', 'BARA', 'HAYON')."
        )
    )
    part_name: str = Field(
        description="Clean Romanian description of the part (without NC8/CPV/customs codes or barcodes)."
    )
    quantity: float = Field(description="Quantity ordered or invoiced.")
    unit_price: float = Field(description="Unit price WITHOUT VAT (Pret unitar fara TVA).")
    total_price: float = Field(description="Line total WITHOUT VAT (Valoare fara TVA).")


class SupplierQuote(BaseModel):
    supplier_name: str = Field(description="Name of the auto parts supplier.")
    supplier_cif: str | None = Field(
        default=None,
        description="The CUI / Cod Fiscal / CIF of the supplier (e.g., 'RO1234567', '1234567')."
    )
    document_code: str = Field(
        description="The unique invoice or offer number (Numar factura / Numar oferta)."
    )
    quote_date: str = Field(
        description="Date of the quote or invoice in YYYY-MM-DD format."
    )
    parts: list[PartItem] = Field(description="List of auto parts on the document.")
    total_fara_tva: float | None = Field(
        default=None,
        description="Document subtotal WITHOUT VAT if printed on the document, else null.",
    )


class VehicleDocument(BaseModel):
    numar_inmatriculare: str = Field(
        description="Vehicle registration plate (Field A on the Romanian Talon, e.g. B-123-ABC or CT-61-MLG)."
    )
    serie_sasiu: str = Field(
        description="17-character VIN / Chassis number (Field E on the Romanian Talon)."
    )
    marca_model: str = Field(
        default="",
        description=(
            "Vehicle Make and Model (Fields D.1 and D.3 on the Romanian Talon, "
            "e.g. 'OPEL CORSA', 'PEUGEOT 208', 'DACIA DOKKER')."
        ),
    )


# --- Internal Helpers & Model Telemetry Router -------------------------------------------

def _delete_quietly(gemini_file: types.File) -> None:
    if not gemini_file.name:
        return
    try:
        client.files.delete(name=gemini_file.name)
    except Exception:
        pass


def _generate_structured(gemini_file: types.File, prompt: str, schema: type[T]) -> T:
    """
    Routes OCR requests across the dynamically prioritized model pool and records
    every attempt (success, 429 rate-limit, 503 busy, latency) into `nomenclature.db`.
    """
    last_error: Exception | None = None
    ordered_models = get_prioritized_models(MODEL_CHAIN, db_path=DEFAULT_DB_PATH)

    for model_name in ordered_models:
        for attempt in range(2):
            t0 = time.perf_counter()
            try:
                response = client.models.generate_content(  # pyright: ignore[reportUnknownMemberType]
                    model=model_name,
                    contents=[gemini_file, prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=schema,
                        temperature=0.0,
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(
                            disable=True
                        ),
                        http_options=types.HttpOptions(timeout=ATTEMPT_TIMEOUT_MS),
                    ),
                )
            except Exception as e:
                last_error = e
                msg = str(e)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    record_model_attempt(model_name, "429", elapsed_ms, db_path=DEFAULT_DB_PATH)
                    print(
                        f"[Fallback 429] {model_name} rate-limited (120s cooldown). Trying next model..."
                    )
                    break
                if any(tag in msg for tag in RETRY_SAME_MODEL_TAGS):
                    record_model_attempt(model_name, "503", elapsed_ms, db_path=DEFAULT_DB_PATH)
                    if attempt == 0:
                        print(f"[Retry 503] {model_name} busy. Retrying once...")
                        time.sleep(1.5)
                        continue
                    print(f"[Fallback 503] {model_name} unavailable. Trying next model...")
                    break
                record_model_attempt(model_name, "error", elapsed_ms, db_path=DEFAULT_DB_PATH)
                if any(tag in msg for tag in FALLBACK_TAGS):
                    print(f"[Fallback] {model_name} failed ({msg[:80]}). Trying next model...")
                    break
                raise

            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            parsed = response.parsed
            if isinstance(parsed, schema):
                record_model_attempt(model_name, "success", elapsed_ms, db_path=DEFAULT_DB_PATH)
                return parsed

            record_model_attempt(model_name, "error", elapsed_ms, db_path=DEFAULT_DB_PATH)
            print(f"[Fallback] {model_name} returned unparseable output. Trying next model...")
            break

    raise RuntimeError("All models in the fallback chain failed.") from last_error


# --- Eco-Tax Rollup (Rule 3.3: 'Taxa fond mediu lubrifianti') ----------------------------

_ECO_TAX_PATTERNS = (
    "taxa fond mediu",
    "fond mediu lubrifianti",
    "taxa eco",
    "ecotaxa",
)


def _is_eco_tax_line(item: PartItem) -> bool:
    combined = f"{item.part_code} {item.part_name}".lower()
    return any(pat in combined for pat in _ECO_TAX_PATTERNS)


def _is_oil_or_lubricant(item: PartItem) -> bool:
    combined = f"{item.part_code} {item.part_name}".lower()
    return any(
        kw in combined
        for kw in ("ulei", "5w30", "5w40", "0w30", "0w20", "10w40", "lubrifiant", "antigel")
    )


def _rollup_eco_taxes(parts: list[PartItem]) -> list[PartItem]:
    tax_lines = [p for p in parts if _is_eco_tax_line(p)]
    if not tax_lines:
        return parts

    normal_lines = [p for p in parts if not _is_eco_tax_line(p)]
    if not normal_lines:
        return parts

    tax_total = round(sum(t.total_price for t in tax_lines), 2)
    target_idx = next(
        (i for i, p in enumerate(normal_lines) if _is_oil_or_lubricant(p)),
        0,
    )
    target = normal_lines[target_idx]
    new_total = round(target.total_price + tax_total, 2)
    new_unit = (
        round(new_total / target.quantity, 4) if target.quantity != 0 else new_total
    )
    normal_lines[target_idx] = PartItem(
        part_code=target.part_code,
        part_name=target.part_name,
        quantity=target.quantity,
        unit_price=new_unit,
        total_price=new_total,
    )
    return normal_lines


# --- Extraction Functions ----------------------------------------------------------------

def extract_quote_data(file_path: str) -> SupplierQuote:
    prompt = """
    Extract all auto parts from this Romanian supplier quote or invoice.
    Pay close attention to the supplier-specific part code rules:
    - Materom: extract ONLY the first code before ' - ' (e.g., 'GDB2063').
    - Autonet: extract the inline SKU + brand code at the start of the description before '-' (e.g., 'FD4287313P PRA' or '21904FE'), ignoring the 8-digit customs code ('Cod tarifar vamal').
    - AD Auto Total: extract ONLY the first token in the description (e.g., '70-31414-10', '6PK1698CT'), ignoring trailing NC8 and CPV codes.
    - Sprint Car Distribution: NEVER use 'Cod articol client' (like '7564H.3500') or the 13-digit EAN barcode. Extract the code embedded inside the description (like '37180074G', '37130080G', '9010050', or paint code 'MB518'), or the main uppercase noun ('CATALIZ', 'FOND', 'CHIT', 'LAC') if no manufacturer code is present.
    - Always extract unit prices and line totals WITHOUT VAT (fara TVA).
    - Convert the document date to YYYY-MM-DD.
    - Extract the supplier's CUI/CIF (Cod de inregistrare fiscala) if present.
    """
    gemini_file = client.files.upload(file=file_path)
    try:
        quote = _generate_structured(gemini_file, prompt, SupplierQuote)
        quote.parts = _rollup_eco_taxes(quote.parts)
        return quote
    finally:
        _delete_quietly(gemini_file)


def extract_vehicle_data(
    file_path: str, crop_config: TalonCropConfig | None = None
) -> VehicleDocument:
    prompt = """
    Extract the vehicle registration data from this cropped Top-Left (A to K) section of a
    Romanian vehicle registration document (Talon / Certificat de inmatriculare).
    Copy the license plate (Field A), the 17-character VIN (Field E), and the Make + Model
    (Fields D.1 and D.3, e.g. 'OPEL CORSA' or 'PEUGEOT 208') exactly as printed.
    A VIN has 17 characters and never contains the letters I, O or Q.
    Do not guess characters you cannot read.
    """
    cropped_scan_path = prepare_talon_for_ocr(file_path, custom_crop=crop_config)
    gemini_file = client.files.upload(file=cropped_scan_path)
    try:
        doc = _generate_structured(gemini_file, prompt, VehicleDocument)
    finally:
        _delete_quietly(gemini_file)
        if os.path.exists(cropped_scan_path):
            os.remove(cropped_scan_path)

    doc.serie_sasiu = doc.serie_sasiu.upper().replace(" ", "")
    doc.numar_inmatriculare = doc.numar_inmatriculare.upper().strip()
    doc.marca_model = re.sub(r"\s+", " ", doc.marca_model).strip().upper()
    return doc


# --- Validation Functions ----------------------------------------------------------------

_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_RO_PLATE_RE = re.compile(r"^(B|[A-Z]{2})[\s\-]?\d{2,3}[\s\-]?[A-Z]{3}$")


def validate_quote(quote: SupplierQuote, tolerance: float = 0.05) -> list[str]:
    issues: list[str] = []
    if not quote.parts:
        issues.append("No parts were extracted from the quote.")

    try:
        datetime.strptime(quote.quote_date, "%Y-%m-%d")
    except ValueError:
        issues.append(f"quote_date '{quote.quote_date}' is not a valid YYYY-MM-DD date.")

    lines_sum = 0.0
    for i, p in enumerate(quote.parts, start=1):
        if not p.part_code.strip():
            issues.append(f"Line {i} ('{p.part_name}'): missing part_code.")
        expected = round(abs(p.quantity) * p.unit_price, 2)
        if abs(expected - abs(p.total_price)) > tolerance:
            issues.append(
                f"Line {i} ({p.part_code}): {p.quantity} * {p.unit_price} = {expected:.2f}, "
                f"but total_price is {p.total_price:.2f}."
            )
        lines_sum += p.total_price

    if (
        quote.total_fara_tva is not None
        and abs(lines_sum - quote.total_fara_tva) > tolerance
    ):
        issues.append(
            f"Sum of lines ({lines_sum:.2f}) does not match document total_fara_tva ({quote.total_fara_tva:.2f})."
        )
    return issues


def validate_vehicle(doc: VehicleDocument) -> list[str]:
    issues: list[str] = []
    vin = doc.serie_sasiu
    if len(vin) != 17:
        issues.append(f"VIN '{vin}' has {len(vin)} characters (expected 17).")
    elif not _VIN_RE.match(vin):
        bad = sorted({c for c in vin if not re.match(r"[A-HJ-NPR-Z0-9]", c)})
        issues.append(f"VIN '{vin}' contains invalid characters: {bad} (I, O, Q are never valid).")

    if not _RO_PLATE_RE.match(doc.numar_inmatriculare):
        issues.append(
            f"Plate '{doc.numar_inmatriculare}' does not match standard Romanian format."
        )
    if not doc.marca_model:
        issues.append("Vehicle Make/Model (Fields D.1/D.3) was not detected on the Talon.")
    return issues