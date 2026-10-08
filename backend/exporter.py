import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from nomenclature import DEFAULT_DB_PATH, normalize_code, upsert_parts

# Default export directory (override via WINMENTOR_EXPORT_DIR in .env for your SMB share)
DEFAULT_EXPORT_DIR = str(
    Path(os.environ.get("WINMENTOR_EXPORT_DIR", Path(__file__).with_name("exports")))
)

# Default accounting constants (21% VAT matches your October 2026 invoices and SEPT.xlsx)
DEFAULT_TVA_PERCENT = 21
DEFAULT_GESTIUNE = "MAGAZIE"
DEFAULT_UM = "BUC"

# Known supplier CIFs from your invoices (optional helper for WinMentor partner matching)
KNOWN_SUPPLIER_CIFS: dict[str, str] = {
    "MATEROM": "RO12456540",
    "AUTONET": "RO8539532",
    "AUTO TOTAL": "RO6844726",
    "AUTOTOTAL": "RO6844726",
    "SPRINT CAR": "RO13632904",
    "AUTOMOTIVE REFINISH": "RO31158435",
    "MARCOS SERV": "RO33841085",
}


@dataclass(frozen=True)
class VehicleTarget:
    """Identifies the car / work order receiving a consumed part."""
    numar_inmatriculare: str
    serie_sasiu: str = ""


@dataclass(frozen=True)
class ExportAllocation:
    """
    A resolved line item (or split portion of a line item) ready for export.
    If `vehicle` is None, the quantity stays in general shop inventory (Magazie)
    and is NOT written to BON_CONS.TXT.
    """
    part_code: str
    part_name: str
    quantity: float
    unit_price: float
    um: str = DEFAULT_UM
    vehicle: VehicleTarget | None = None


@dataclass(frozen=True)
class ExportInvoicePayload:
    """Complete operator-approved invoice and vehicle allocations."""
    supplier_name: str
    document_code: str
    quote_date: str  # YYYY-MM-DD
    allocations: list[ExportAllocation] = field(default_factory=list[ExportAllocation])
    supplier_cif: str | None = None
    tva_percent: int = DEFAULT_TVA_PERCENT
    gestiune: str = DEFAULT_GESTIUNE


@dataclass(frozen=True)
class ExportResult:
    fact_int_path: str
    bon_cons_path: str | None
    invoice_lines_written: int
    consumption_vouchers_written: int
    nomenclature_updated: int


# --- Formatting Helpers ------------------------------------------------------------------

def _ascii_clean(text: str) -> str:
    """Strips diacritics and semicolons so legacy WinMentor parsers never corrupt text."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = stripped.replace(";", ",").replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def _to_winmentor_date(iso_date: str) -> str:
    """Converts 'YYYY-MM-DD' to WinMentor's 'DD.MM.YYYY' format."""
    dt = datetime.strptime(iso_date.strip(), "%Y-%m-%d")
    return dt.strftime("%d.%m.%Y")


def _split_doc_code(doc_code: str) -> tuple[str, str]:
    """Splits 'MTRF 915268324' or 'ARD-F219034' into (Serie, Numar)."""
    cleaned = _ascii_clean(doc_code).upper()
    parts = re.split(r"[\s\-]+", cleaned, maxsplit=1)
    if len(parts) == 2 and parts[0].isalpha():
        return parts[0], parts[1]
    # Handle attached prefix like 'FB2AX10394098' -> ('FB2AX', '10394098')
    m = re.match(r"^([A-Z0-9]*?[A-Z])(\d{5,})$", cleaned)
    if m:
        return m.group(1), m.group(2)
    return "", cleaned


def _resolve_supplier_cif(supplier_name: str, explicit_cif: str | None) -> str:
    if explicit_cif and explicit_cif.strip():
        return _ascii_clean(explicit_cif).upper()
    upper_sup = _ascii_clean(supplier_name).upper()
    for key, cif in KNOWN_SUPPLIER_CIFS.items():
        if key in upper_sup:
            return cif
    return ""


# --- Generators --------------------------------------------------------------------------

@dataclass
class _AggregatedInvoiceLine:
    part_code: str
    part_name: str
    um: str
    quantity: float
    unit_price: float


def _aggregate_invoice_lines(allocations: list[ExportAllocation]) -> list[_AggregatedInvoiceLine]:
    """
    Re-combines split allocations of the same part_code + unit_price so FACT_INT.TXT
    reflects the original invoice lines, while keeping genuinely separate lines intact.
    """
    grouped: dict[tuple[str, float], _AggregatedInvoiceLine] = {}
    for a in allocations:
        code = normalize_code(a.part_code)
        name = _ascii_clean(a.part_name).upper()
        um = _ascii_clean(a.um).upper() or DEFAULT_UM
        key = (code, round(a.unit_price, 4))
        if key in grouped:
            grouped[key].quantity = round(grouped[key].quantity + a.quantity, 4)
        else:
            grouped[key] = _AggregatedInvoiceLine(
                part_code=code,
                part_name=name,
                um=um,
                quantity=round(a.quantity, 4),
                unit_price=round(a.unit_price, 4),
            )
    return list(grouped.values())


def build_fact_int_content(payload: ExportInvoicePayload) -> tuple[str, int]:
    """Builds the INI-style FACT_INT.TXT content with CRLF line endings."""
    wm_date = _to_winmentor_date(payload.quote_date)
    dt = datetime.strptime(payload.quote_date.strip(), "%Y-%m-%d")
    serie, numar = _split_doc_code(payload.document_code)
    cif = _resolve_supplier_cif(payload.supplier_name, payload.supplier_cif)
    supplier = _ascii_clean(payload.supplier_name).upper()
    gestiune = _ascii_clean(payload.gestiune).upper() or DEFAULT_GESTIUNE

    inv_lines = _aggregate_invoice_lines(payload.allocations)
    total_fara_tva = sum(round(line.quantity * line.unit_price, 2) for line in inv_lines)
    total_tva = round(total_fara_tva * (payload.tva_percent / 100.0), 2)

    lines: list[str] = [
        "[InfoPachet]",
        f"AnLucru={dt.year}",
        f"LunaLucru={dt.month:02d}",
        "TipDocument=FACTURA INTRARE",
        "TotalFacturi=1",
        "",
        "[Factura_1]",
        f"SerieDoc={serie}",
        f"NumarDoc={numar}",
        f"Data={wm_date}",
        f"DenumireFurnizor={supplier}",
        f"CodFurnizor={cif}",
        "Moneda=RON",
        f"CotaTVA={payload.tva_percent}",
        f"TotalFaraTVA={total_fara_tva:.2f}",
        f"TotalTVA={total_tva:.2f}",
        "",
        "[Items_1]",
    ]

    for idx, item in enumerate(inv_lines, start=1):
        valoare = round(item.quantity * item.unit_price, 2)
        # Format: CodExtern;DenumireArticol;UM;Cantitate;PretUnitar;ValoareFaraTVA;CotaTVA;Gestiune
        lines.append(
            f"Item_{idx}={item.part_code};{item.part_name};{item.um};"
            f"{item.quantity:g};{item.unit_price:.4f};{valoare:.2f};"
            f"{payload.tva_percent};{gestiune}"
        )

    return "\r\n".join(lines) + "\r\n", len(inv_lines)


def build_bon_cons_content(payload: ExportInvoicePayload) -> tuple[str | None, int]:
    """
    Groups vehicle-assigned allocations by car and builds BON_CONS.TXT.
    Returns (None, 0) if all items were sent to general shop stock (Magazie).
    """
    by_car: dict[tuple[str, str], list[ExportAllocation]] = {}
    for a in payload.allocations:
        if a.vehicle is not None and a.vehicle.numar_inmatriculare.strip():
            plate = _ascii_clean(a.vehicle.numar_inmatriculare).upper()
            vin = _ascii_clean(a.vehicle.serie_sasiu).upper().replace(" ", "")
            by_car.setdefault((plate, vin), []).append(a)

    if not by_car:
        return None, 0

    wm_date = _to_winmentor_date(payload.quote_date)
    dt = datetime.strptime(payload.quote_date.strip(), "%Y-%m-%d")
    _, doc_num = _split_doc_code(payload.document_code)
    gestiune = _ascii_clean(payload.gestiune).upper() or DEFAULT_GESTIUNE

    lines: list[str] = [
        "[InfoPachet]",
        f"AnLucru={dt.year}",
        f"LunaLucru={dt.month:02d}",
        "TipDocument=BON CONSUM",
        f"TotalBonuri={len(by_car)}",
    ]

    for bon_idx, ((plate, vin), car_items) in enumerate(by_car.items(), start=1):
        lines.extend(
            [
                "",
                f"[Bon_{bon_idx}]",
                f"NumarBon={doc_num}-{bon_idx}",
                f"Data={wm_date}",
                f"Gestiune={gestiune}",
                f"Comanda={plate}",
                f"SerieSasiu={vin}",
                f"Observatii=Factura { _ascii_clean(payload.document_code).upper() }",
                "",
                f"[Items_{bon_idx}]",
            ]
        )
        for item_idx, item in enumerate(car_items, start=1):
            code = normalize_code(item.part_code)
            name = _ascii_clean(item.part_name).upper()
            um = _ascii_clean(item.um).upper() or DEFAULT_UM
            # Format: CodExtern;DenumireArticol;UM;Cantitate;PretUnitar;Comanda
            lines.append(
                f"Item_{item_idx}={code};{name};{um};"
                f"{item.quantity:g};{item.unit_price:.4f};{plate}"
            )

    return "\r\n".join(lines) + "\r\n", len(by_car)


# --- Main Export Entrypoint --------------------------------------------------------------

def export_to_winmentor(
    payload: ExportInvoicePayload,
    export_dir: str = DEFAULT_EXPORT_DIR,
    db_path: str = DEFAULT_DB_PATH,
) -> ExportResult:
    """
    1. Validates payload and atomically updates nomenclature.db with approved part names.
    2. Writes FACT_INT.TXT and (if any parts are assigned to cars) BON_CONS.TXT with CRLF endings.
    """
    if not payload.allocations:
        raise ValueError("Cannot export an invoice with zero part allocations.")

    # 1. Deduplicate and upsert approved (part_code, part_name) pairs into nomenclature.db
    unique_parts: dict[str, str] = {}
    for a in payload.allocations:
        code = normalize_code(a.part_code)
        name = a.part_name.strip()
        if not code or not name:
            raise ValueError(f"Invalid empty part_code or part_name in allocation: {a!r}")
        unique_parts[code] = name

    upsert_parts(list(unique_parts.items()), db_path=db_path)

    # 2. Ensure target export directory exists
    target_dir = Path(export_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    # 3. Build and write FACT_INT.TXT (binary write preserves exact CRLF on both Windows & Linux)
    fact_content, inv_count = build_fact_int_content(payload)
    fact_path = target_dir / "FACT_INT.TXT"
    fact_path.write_bytes(fact_content.encode("utf-8"))

    # 4. Build and write BON_CONS.TXT if any items were allocated to vehicles
    bon_content, bon_count = build_bon_cons_content(payload)
    bon_path_str: str | None = None
    if bon_content is not None:
        bon_path = target_dir / "BON_CONS.TXT"
        bon_path.write_bytes(bon_content.encode("utf-8"))
        bon_path_str = str(bon_path)

    return ExportResult(
        fact_int_path=str(fact_path),
        bon_cons_path=bon_path_str,
        invoice_lines_written=inv_count,
        consumption_vouchers_written=bon_count,
        nomenclature_updated=len(unique_parts),
    )