import os
from talon_scanner import TalonCropConfig, prepare_talon_for_ocr
import re
import time
from datetime import datetime
from typing import TypeVar

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field


class PartItem(BaseModel):
    part_code: str = Field(
        description=(
            "The primary manufacturer part code or SKU (Cod Articol / Cod Piesă) as stored in WinMentor. "
            "Follow the supplier-specific rules in the prompt: strip Materom ' - ' suffixes, extract inline "
            "codes before the hyphen on Autonet/AutoTotal, extract the manufacturer/tint code from the "
            "description on Sprint Car (never use Cod articol client or 13-digit EAN barcodes), and never "
            "use 8-digit customs tariff codes (Cod tarifar vamal) or NC8/CPV codes."
        )
    )
    part_name: str = Field(
        description=(
            "The clean description or name of the part (Denumire), excluding leading inline part codes, "
            "13-digit EAN barcodes, and trailing NC8/CPV customs tags."
        )
    )
    quantity: float = Field(description="The quantity ordered (Cantitate).")
    unit_price: float = Field(description="The price per unit excluding VAT (Preț unitar fără TVA).")
    total_price: float = Field(description="The total price for this line item excluding VAT (Valoare fără TVA).")


class SupplierQuote(BaseModel):
    supplier_name: str = Field(
        description="The name of the vendor/supplier (e.g., MATEROM, AUTONET IMPORT, AD AUTO TOTAL, SPRINT CAR DISTRIBUTION)."
    )
    document_code: str = Field(
        description="The invoice or quote number (Serie și număr, e.g. 'FB2AX10394098', 'IN26 000001380')."
    )
    quote_date: str = Field(description="The date of the document in YYYY-MM-DD format.")
    parts: list[PartItem] = Field(description="The list of all auto parts and billable items on the quote/invoice.")


class VehicleDocument(BaseModel):
    numar_inmatriculare: str = Field(description="The license plate number (Point A on the Talon).")
    serie_sasiu: str = Field(description="The 17-character Vehicle Identification Number or VIN (Point E on the Talon).")
    marca_model: str = Field(
        default="",
        description="The vehicle make and commercial model (Points D.1 and D.3 on the Talon, e.g. 'OPEL CORSA', 'PEUGEOT 208').",
    )


T = TypeVar("T", bound=BaseModel)

load_dotenv()

# Client-level timeout (ms): ceiling for file uploads and anything without its own timeout.
client = genai.Client(http_options=types.HttpOptions(timeout=120_000))

# Ordered by what responded reliably in testing. Re-test periodically: availability of
# the newest models changes. Avoid 'gemini-flash-latest': it follows the newest model.
MODEL_CHAIN = [
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
]

# Per-attempt timeout (ms) for generation, so a dead model doesn't block for minutes.
ATTEMPT_TIMEOUT_MS = 45_000

# Errors that mean "try the next model".
FALLBACK_TAGS = ("503", "504", "DEADLINE", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "NOT_FOUND")
# Errors worth one quick retry on the same model before falling back (brief overloads).
RETRY_SAME_MODEL_TAGS = ("503", "UNAVAILABLE")


def _normalize_code(code: str) -> str:
    """Canonical form of a part code: trimmed, inner whitespace collapsed, uppercase."""
    return re.sub(r"\s+", " ", code).strip().upper()


def _is_eco_tax_line(item: PartItem) -> bool:
    """Detects environmental / lubricant fund tax lines ('Taxa fond mediu lubrifianti')."""
    code_up = item.part_code.upper()
    name_low = item.part_name.lower()
    return (
        "TAXA_MEDIU" in code_up
        or "TAXA MEDIU" in code_up
        or ("taxa" in name_low and "mediu" in name_low)
    )


def _rollup_eco_taxes(parts: list[PartItem]) -> list[PartItem]:
    """
    Rolls 'Taxa fond mediu lubrifianti' into the preceding lubricant/oil line item
    so the tax is included in the product's value rather than exported as a separate row.
    """
    merged: list[PartItem] = []
    for item in parts:
        if _is_eco_tax_line(item) and merged:
            target = merged[-1]
            new_total = round(target.total_price + item.total_price, 2)
            qty = target.quantity if target.quantity != 0 else 1.0
            new_unit = round(new_total / qty, 4)
            merged[-1] = PartItem(
                part_code=target.part_code,
                part_name=target.part_name,
                quantity=target.quantity,
                unit_price=new_unit,
                total_price=new_total,
            )
        else:
            merged.append(item)
    return merged


def _generate_structured(gemini_file: types.File, prompt: str, schema: type[T]) -> T:
    """Runs the request down MODEL_CHAIN and returns a parsed instance of `schema`."""
    last_error: Exception | None = None

    for model_name in MODEL_CHAIN:
        for attempt in range(2):
            try:
                response = client.models.generate_content(  # pyright: ignore[reportUnknownMemberType]
                    model=model_name,
                    contents=[gemini_file, prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=schema,
                        temperature=0.0,
                        http_options=types.HttpOptions(timeout=ATTEMPT_TIMEOUT_MS),
                    ),
                )
            except Exception as e:
                last_error = e
                msg = str(e)
                if attempt == 0 and any(tag in msg for tag in RETRY_SAME_MODEL_TAGS):
                    print(f"[Retry] {model_name} unavailable. Retrying once...")
                    time.sleep(2)
                    continue
                if any(tag in msg for tag in FALLBACK_TAGS):
                    print(f"[Fallback] {model_name} failed ({msg[:80]}). Trying next model...")
                    break
                # Anything else (e.g. invalid API key, bad request) is a real error.
                raise

            parsed = response.parsed
            if isinstance(parsed, schema):
                return parsed

            # Output was empty, blocked, or not valid against the schema.
            print(f"[Fallback] {model_name} returned unparseable output. Trying next model...")
            break

    raise RuntimeError("All models in the fallback chain failed.") from last_error


def _delete_quietly(gemini_file: types.File) -> None:
    """Remove the uploaded file from Google's storage; never let cleanup raise."""
    if not gemini_file.name:
        return
    try:
        client.files.delete(name=gemini_file.name)
    except Exception:
        pass


def extract_quote_data(file_path: str) -> SupplierQuote:
    """Uploads a PDF or image of a quote/invoice and returns structured Part data."""
    prompt = """
    You are an expert data extractor for Romanian auto repair shops.
    Extract the supplier details and every line item from this quote/invoice.
    If the page is rotated or sideways, read it in its proper orientation.
    Ignore subtotals ('TOTAL NIR', 'Total:'), shipping fees, or legal footers.

    NUMBER FORMATTING:
    Romanian documents mix comma and dot separators (e.g. '1.234,56' or '1,072.29') and
    occasionally have OCR colons ('59:97'). Return all quantities, unit prices (up to 3-4 decimals,
    e.g. 306.369), and line totals (excluding VAT) as plain numbers with a dot decimal (1234.56).

    SUPPLIER-SPECIFIC PART CODE & DESCRIPTION RULES:
    1. MATEROM: If the code column contains multiple segments separated by ' - '
       (e.g. 'GDB2063 - 11-477-298 - TRW - OEM'), extract ONLY the first manufacturer SKU ('GDB2063').
    2. AUTONET IMPORT: There is NO separate part code column (NEVER use the 8-digit 'Cod tarifar vamal'
       column like 70091000 or 87088099). The part code is printed at the start of 'Denumirea produselor'
       before the hyphen:
       - 'FD4287313P PRA -Retrovizor exterior' -> part_code: 'FD4287313P PRA', part_name: 'Retrovizor exterior'
       - '21904 FE -Brat suspensie...' -> part_code: '21904FE', part_name: 'Brat suspensie...'
       - 'CCE-ESS 08-Banda mastic...' -> part_code: 'CCE-ESS 08', part_name: 'Banda mastic...'
       - 'LIQ LL III 5W30 5L -LIQUI MOLY...' -> part_code: 'LIQ LL III 5W30 5L', part_name: 'LIQUI MOLY...'
       - For 'Taxa fond mediu lubrifianti...', use part_code: 'TAXA_MEDIU'.
    3. AD AUTO TOTAL: The part code is the first token at the start of 'Denumirea produselor'
       (e.g. '70-31414-10', '6PK1698CT', '15-41286-01', 'F89-40003.25'). Strip 'NC8...' and
       'CPV = ...' codes from both part_code and part_name.
    4. SPRINT CAR DISTRIBUTION: NEVER use 'Cod articol client' (like 7564H.3500, 0100H.1000, 3403K.0000)
       and NEVER use the 13-digit EAN 'Cod de bare' (like 4025331466970 or *8026376246318).
       Extract the manufacturer or tint code embedded inside 'Denumirea produselor':
       - Paint mixing tints (e.g. '32035180M MB518 FINE SILVER/3.5LT') -> part_code: 'MB518'
       - '37180074G LAC HS PERMAFAST 8007/1LT' -> part_code: '37180074G', part_name: 'LAC HS PERMAFAST 8007/1LT'
       - '37130080G CATALIZ. PERMAFAST 3008 HS/1LT' -> part_code: '37130080G', part_name: 'CATALIZ. PERMAFAST 3008 HS/1LT'
       - '9010050 BUR.ABRAZ.BEJ MICROFINE 2 FETE' -> part_code: '9010050', part_name: 'BUR.ABRAZ.BEJ MICROFINE 2 FETE'
       - If no alphanumeric manufacturer code exists in the description (after ignoring 13-digit barcodes),
         use the primary product type keyword: 'CATALIZ' (for C15/C67), 'FOND' (for F91), 'LAC' (for H79), or 'CHIT'.
    5. AUTOMOTIVE REFINISH DISTRIBUTION (ARD): Use the dedicated 'COD(ID)' column (e.g. 'ARD4MX300M', 'IND39233').
    6. NO PART CODE COLUMN (e.g. MARCOS SERV AUTOMOBILE): Use the primary part noun in uppercase
       (e.g. 'USA' for 'USA STANGA FATA DACIA DOKKER', 'HAYON', 'BARA FATA').
    """
    gemini_file = client.files.upload(file=file_path)
    try:
        quote = _generate_structured(gemini_file, prompt, SupplierQuote)
    finally:
        _delete_quietly(gemini_file)

    # Post-processing normalization
    quote.supplier_name = quote.supplier_name.strip()
    quote.document_code = quote.document_code.strip()
    quote.quote_date = quote.quote_date.strip()

    for item in quote.parts:
        item.part_code = _normalize_code(item.part_code)
        item.part_name = re.sub(r"\s+", " ", item.part_name).strip()

    quote.parts = _rollup_eco_taxes(quote.parts)
    return quote


# def extract_vehicle_data(file_path: str) -> VehicleDocument:
#     """Uploads a Talon or CIV photo and extracts the license plate, VIN, and Make/Model."""
#     prompt = """
#     Extract the vehicle registration data from this Romanian vehicle document (Talon / CIV).
#     Copy the license plate (Point A), the 17-character VIN (Point E), and the Make + Model
#     (Points D.1 and D.3, e.g. 'OPEL CORSA' or 'PEUGEOT 208') as printed.
#     A VIN has 17 characters and never contains the letters I, O or Q.
#     Do not guess characters you cannot read.
#     """
#     gemini_file = client.files.upload(file=file_path)
#     try:
#         doc = _generate_structured(gemini_file, prompt, VehicleDocument)
#     finally:
#         _delete_quietly(gemini_file)

#     doc.serie_sasiu = doc.serie_sasiu.upper().replace(" ", "")
#     doc.numar_inmatriculare = doc.numar_inmatriculare.upper().strip()
#     doc.marca_model = re.sub(r"\s+", " ", doc.marca_model).strip().upper()
#     return doc

def extract_vehicle_data(
    file_path: str, crop_config: TalonCropConfig | None = None
) -> VehicleDocument:
    """
    1. Locally perspective-flattens the Talon photo, crops ONLY the Top-Left A-to-K region,
       and converts it into a high-contrast monochrome scan via talon_scanner.py.
    2. Uploads ONLY that cropped A-to-K scan image to Gemini and deletes it immediately after.
    """
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

# --- Validation: call these after extraction and surface the warnings to the user ---

def validate_vehicle(doc: VehicleDocument) -> list[str]:
    """Returns a list of problems; an empty list means the checks passed."""
    issues: list[str] = []
    vin = doc.serie_sasiu
    if len(vin) != 17:
        issues.append(f"VIN has {len(vin)} characters, expected 17: {vin!r}")
    if not vin.isalnum():
        issues.append(f"VIN contains non-alphanumeric characters: {vin!r}")
    if any(c in "IOQ" for c in vin):
        issues.append(f"VIN contains I, O or Q, which are not valid in a VIN: {vin!r}")
    if not doc.numar_inmatriculare:
        issues.append("License plate is empty")
    return issues


def validate_quote(quote: SupplierQuote) -> list[str]:
    """Returns a list of problems; an empty list means the checks passed."""
    issues: list[str] = []

    try:
        datetime.strptime(quote.quote_date, "%Y-%m-%d")
    except ValueError:
        issues.append(f"Date is not a valid YYYY-MM-DD value: {quote.quote_date!r}")

    if not quote.document_code.strip():
        issues.append("Document code is empty")
    if not quote.parts:
        issues.append("No line items were extracted")

    for i, p in enumerate(quote.parts, start=1):
        # Unit prices are often rounded to 2-4 decimals while the line total is exact,
        # so allow a tolerance that scales with abs(quantity) (handles negative storno rows too).
        tolerance = 0.01 * max(abs(p.quantity), 1.0) + 0.01
        expected = p.quantity * p.unit_price
        if abs(expected - p.total_price) > tolerance:
            issues.append(
                f"Line {i} ({p.part_code}): {p.quantity} x {p.unit_price} = {expected:.2f}, "
                f"but total is {p.total_price:.2f}"
            )
        if not p.part_code.strip():
            issues.append(f"Line {i}: empty part code")

    return issues