import json
import os
import re
import shutil
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, TypedDict

from nomenclature import (
    DEFAULT_DB_PATH,
    check_duplicate_invoice,
    deduct_stock_quantities,
    normalize_code,
    record_export,
    upsert_parts,
)

# Default directory where WinMentor export bundles and flat files are written
DEFAULT_EXPORT_DIR = str(
    Path(os.environ.get("WINMENTOR_EXPORT_DIR", Path(__file__).with_name("exports")))
)

# Standard accounting defaults for Top Speed Service S.R.L.
DEFAULT_TVA_PERCENT = 21
DEFAULT_GESTIUNE = "MAGAZIE"
DEFAULT_UM = "Buc"


class Department(str, Enum):
    """Valid workshop departments for general consumable allocations."""
    TINICHIGERIE = "TINICHIGERIE"
    VOPSITORIE = "VOPSITORIE"
    MECANICA = "MECANICA"


# Known supplier CIFs for automatic header population
KNOWN_SUPPLIER_CIFS: dict[str, str] = {
    "MATEROM": "RO12456540",
    "AUTONET": "RO8539532",
    "AUTO TOTAL": "RO6844726",
    "AUTOTOTAL": "RO6844726",
    "SPRINT CAR": "RO13632904",
    "AUTOMOTIVE REFINISH": "RO31158435",
    "MARCOS SERV": "RO33841085",
}


# --- Strictly Typed Dictionaries for JSON Bundles ---

class BonItemDict(TypedDict):
    part_code: str
    part_name: str
    um: str
    quantity: float
    unit_price: float
    valoare: float


class BonConsumDict(TypedDict):
    bon_index: int
    serie_carnet: str
    data: str
    gestiune: str
    cine_consuma: str
    observatii: str
    total_valoare: float
    items: list[BonItemDict]


@dataclass(frozen=True)
class VehicleTarget:
    """
    Represents a specific vehicle for a Consumption Voucher (Bon de Consum).
    Note: `departament` is kept optional with a blank default only so legacy tests
    (test_exporter.py) continue to pass without tying cars to departments in the UI.
    """
    numar_inmatriculare: str
    serie_sasiu: str = ""
    marca_model: str = ""
    departament: Department | str = ""


@dataclass(frozen=True)
class ExportAllocation:
    """
    A single part allocation from a staged supplier quote.
    Mutually exclusive 3-way destination:
      1. `vehicle` is set     -> Consumed on that specific car (Bon consum with 4.Obs = '<make/model> <plate>').
      2. `departament` is set -> General shop consumable for that department (Bon consum with CineConsuma = dept).
      3. Both are None        -> Stays in MAGAZIE general stock (Factura fiscala only, no Bon consum).
    """
    part_code: str
    part_name: str
    quantity: float
    unit_price: float
    um: str = DEFAULT_UM
    vehicle: VehicleTarget | None = None
    departament: Department | None = None


@dataclass(frozen=True)
class ExportInvoicePayload:
    supplier_name: str
    document_code: str
    quote_date: str  # ISO format: YYYY-MM-DD
    allocations: list[ExportAllocation] = field(default_factory=list[ExportAllocation])
    supplier_cif: str | None = None
    tva_percent: int = DEFAULT_TVA_PERCENT
    gestiune: str = DEFAULT_GESTIUNE
    allow_duplicate: bool = False


@dataclass(frozen=True)
class ExportResult:
    winmentor_doc_nr: str
    archive_dir: str
    bundle_json_path: str
    fact_int_path: str
    bon_cons_path: str | None
    invoice_lines_written: int
    consumption_vouchers_written: int
    nomenclature_updated: int
    bundle_data: dict[str, Any]


# --- Formatting & WinMentor Convention Helpers -------------------------------------------

def _ascii_clean(text: str) -> str:
    """Strips Romanian diacritics and semicolons/newlines for clean WinMentor text output."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = stripped.replace(";", ",").replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def _to_winmentor_date(iso_date: str) -> str:
    """Converts YYYY-MM-DD into WinMentor's DD.MM.YYYY format."""
    dt = datetime.strptime(iso_date.strip(), "%Y-%m-%d")
    return dt.strftime("%d.%m.%Y")


def extract_winmentor_doc_number(doc_code: str) -> str:
    """
    Rule 2.2 + Point 7: Extracts the trailing numeric invoice number for WinMentor's `Numar` field.
    - Strips series prefixes (e.g., 'FB2AX10394623' -> '394623').
    - Preserves leading zeros on 1-to-6 digit invoice numbers (e.g., '078561' -> '078561', '01234' -> '01234').
    - Only strips excess zero padding when a raw number is >6 digits starting with '00' (e.g., Sprint '000001380' -> '1380').
    """
    cleaned = _ascii_clean(doc_code).upper().strip()
    if re.fullmatch(r"\d{1,7}", cleaned):
        return cleaned
    digits_groups: list[str] = re.findall(r"\d+", cleaned)
    if not digits_groups:
        return cleaned
    last_digits = digits_groups[-1]
    if len(last_digits) > 6 and last_digits.startswith("00"):
        return last_digits.lstrip("0") or "0"
    if len(last_digits) > 6:
        return last_digits[-6:]
    return last_digits


def format_bon_obs(vehicle: VehicleTarget) -> str:
    """
    Formats the vehicle identifier for the `4.Obs` field on WinMentor's `Bon consum`:
    '<make/model> <plate_without_hyphens>' in lowercase (e.g., 'opel corsa ct61mlg').
    """
    plate_compact = re.sub(r"[^a-z0-9]", "", _ascii_clean(vehicle.numar_inmatriculare).lower())
    model_clean = _ascii_clean(vehicle.marca_model).lower()
    if not plate_compact:
        return model_clean
    if model_clean:
        return f"{model_clean} {plate_compact}".strip()
    return plate_compact


def _resolve_supplier_cif(supplier_name: str, explicit_cif: str | None) -> str:
    if explicit_cif and explicit_cif.strip():
        return _ascii_clean(explicit_cif).upper()
    upper_sup = _ascii_clean(supplier_name).upper()
    for key, cif in KNOWN_SUPPLIER_CIFS.items():
        if key in upper_sup:
            return cif
    return ""


# --- Invoice Line Aggregation (Recombines Split Rows on Factura Intrare) -----------------

@dataclass
class _AggregatedInvoiceLine:
    part_code: str
    part_name: str
    um: str
    quantity: float
    unit_price: float
    valoare_fara_tva: float
    valoare_tva: float


def _aggregate_invoice_lines(
    allocations: list[ExportAllocation], tva_percent: int
) -> list[_AggregatedInvoiceLine]:
    """
    Recombines split allocations of the same part on the supplier invoice (NIR) so
    the supplier invoice line matches the physical PDF invoice, even if the part's
    quantity was split across a car, a department, and MAGAZIE stock.
    """
    grouped: dict[tuple[str, float], _AggregatedInvoiceLine] = {}
    for a in allocations:
        code = normalize_code(a.part_code)
        name = _ascii_clean(a.part_name).upper()
        um = _ascii_clean(a.um) or DEFAULT_UM
        key = (code, round(a.unit_price, 4))
        if key in grouped:
            new_qty = round(grouped[key].quantity + a.quantity, 4)
            new_val = round(new_qty * grouped[key].unit_price, 2)
            new_tva = round(new_val * (tva_percent / 100.0), 2)
            grouped[key].quantity = new_qty
            grouped[key].valoare_fara_tva = new_val
            grouped[key].valoare_tva = new_tva
        else:
            qty = round(a.quantity, 4)
            up = round(a.unit_price, 4)
            val = round(qty * up, 2)
            tva = round(val * (tva_percent / 100.0), 2)
            grouped[key] = _AggregatedInvoiceLine(
                part_code=code,
                part_name=name,
                um=um,
                quantity=qty,
                unit_price=up,
                valoare_fara_tva=val,
                valoare_tva=tva,
            )
    return list(grouped.values())


# --- Content Builders (FACT_INT.TXT & BON_CONS.TXT) --------------------------------------

def build_fact_int_content(
    payload: ExportInvoicePayload,
) -> tuple[str, list[_AggregatedInvoiceLine]]:
    wm_date = _to_winmentor_date(payload.quote_date)
    dt = datetime.strptime(payload.quote_date.strip(), "%Y-%m-%d")
    numar = extract_winmentor_doc_number(payload.document_code)
    cif = _resolve_supplier_cif(payload.supplier_name, payload.supplier_cif)
    supplier = _ascii_clean(payload.supplier_name).upper()
    gestiune = _ascii_clean(payload.gestiune).upper() or DEFAULT_GESTIUNE

    inv_lines = _aggregate_invoice_lines(payload.allocations, payload.tva_percent)
    total_fara_tva = round(sum(line.valoare_fara_tva for line in inv_lines), 2)
    total_tva = round(sum(line.valoare_tva for line in inv_lines), 2)

    lines: list[str] = [
        "[InfoPachet]",
        f"AnLucru={dt.year}",
        f"LunaLucru={dt.month:02d}",
        "TipDocument=FACTURA INTRARE",
        "TotalFacturi=1",
        "",
        "[Factura_1]",
        f"NumarDoc={numar}",
        f"Data={wm_date}",
        f"DenumireFurnizor={supplier}",
        f"CodFurnizor={cif}",
        "Moneda=lei",
        f"CotaTVA={payload.tva_percent}",
        f"TotalFaraTVA={total_fara_tva:.2f}",
        f"TotalTVA={total_tva:.2f}",
        "",
        "[Items_1]",
    ]

    for idx, item in enumerate(inv_lines, start=1):
        lines.append(
            f"Item_{idx}={item.part_code};{item.part_name};{item.um};"
            f"{item.quantity:g};{item.unit_price:.4f};{item.valoare_fara_tva:.2f};"
            f"{payload.tva_percent};{gestiune}"
        )

    return "\r\n".join(lines) + "\r\n", inv_lines


def build_bon_cons_content(
    payload: ExportInvoicePayload,
) -> tuple[str | None, list[BonConsumDict]]:
    """
    Groups non-stock allocations into Consumption Vouchers (`Bon consum`):
    - If `a.vehicle` is set: grouped by that specific car (`Observatii` = 'opel corsa ct61mlg').
    - Else if `a.departament` is set: grouped by that department (`CineConsuma` = 'TINICHIGERIE', `Observatii` = '').
    - Else: item stays in `MAGAZIE` (ignored here).
    """
    by_target: dict[tuple[str, str], list[ExportAllocation]] = {}
    for a in payload.allocations:
        if a.vehicle is not None and a.vehicle.numar_inmatriculare.strip():
            raw_dept = (
                a.vehicle.departament.value
                if isinstance(a.vehicle.departament, Department)
                else str(a.vehicle.departament)
            )
            dept = _ascii_clean(raw_dept).upper()
            obs = format_bon_obs(a.vehicle)
            by_target.setdefault((dept, obs), []).append(a)
        elif a.departament is not None:
            dept = _ascii_clean(a.departament.value).upper()
            by_target.setdefault((dept, ""), []).append(a)

    if not by_target:
        return None, []

    wm_date = _to_winmentor_date(payload.quote_date)
    dt = datetime.strptime(payload.quote_date.strip(), "%Y-%m-%d")
    doc_num = extract_winmentor_doc_number(payload.document_code)
    gestiune = _ascii_clean(payload.gestiune).upper() or DEFAULT_GESTIUNE

    lines: list[str] = [
        "[InfoPachet]",
        f"AnLucru={dt.year}",
        f"LunaLucru={dt.month:02d}",
        "TipDocument=BON DE CONSUM",
        f"TotalBonuri={len(by_target)}",
    ]

    vouchers_meta: list[BonConsumDict] = []

    for bon_idx, ((dept, obs), items_for_target) in enumerate(by_target.items(), start=1):
        lines.extend(
            [
                "",
                f"[Bon_{bon_idx}]",
                "SerieCarnet=TOP",
                f"NumarBon={doc_num}-{bon_idx}",
                f"Data={wm_date}",
                f"Gestiune={gestiune}",
                f"CineConsuma={dept}",
                f"Observatii={obs}",
                "",
                f"[Items_{bon_idx}]",
            ]
        )
        voucher_items: list[BonItemDict] = []
        for item_idx, item in enumerate(items_for_target, start=1):
            code = normalize_code(item.part_code)
            name = _ascii_clean(item.part_name).upper()
            um = _ascii_clean(item.um) or DEFAULT_UM
            val = round(item.quantity * item.unit_price, 2)
            lines.append(
                f"Item_{item_idx}={code};{name};{um};"
                f"{item.quantity:g};{item.unit_price:.4f};{obs}"
            )
            voucher_items.append(
                BonItemDict(
                    part_code=code,
                    part_name=name,
                    um=um,
                    quantity=round(item.quantity, 4),
                    unit_price=round(item.unit_price, 4),
                    valoare=val,
                )
            )
        vouchers_meta.append(
            BonConsumDict(
                bon_index=bon_idx,
                serie_carnet="TOP",
                data=wm_date,
                gestiune=gestiune,
                cine_consuma=dept,
                observatii=obs,
                total_valoare=round(
                    sum(round(i.quantity * i.unit_price, 2) for i in items_for_target), 2
                ),
                items=voucher_items,
            )
        )

    return "\r\n".join(lines) + "\r\n", vouchers_meta


# --- Main Exporter & Archiver (Invoice Staging Mode) -------------------------------------

def export_to_winmentor(
    payload: ExportInvoicePayload,
    export_dir: str = DEFAULT_EXPORT_DIR,
    db_path: str = DEFAULT_DB_PATH,
) -> ExportResult:
    """
    1. Blocks duplicate supplier invoices (unless `allow_duplicate=True`).
    2. Atomically upserts all reconciled part codes and names into `nomenclature.db`.
    3. Writes `export_bundle.json`, `FACT_INT.TXT`, and `BON_CONS.TXT` into a permanent
       monthly archive subfolder (`exports/archive/YYYY-MM/<timestamp>_<supplier>_<doc_nr>/`)
       as well as the active `exports/` root directory.
    """
    if not payload.allocations:
        raise ValueError("Cannot export an invoice with zero part allocations.")

    if not payload.allow_duplicate:
        existing = check_duplicate_invoice(
            payload.supplier_name, payload.document_code, db_path=db_path
        )
        if existing is not None:
            raise ValueError(
                f"Duplicate invoice blocked: {payload.supplier_name} '{payload.document_code}' "
                f"(WinMentor #{existing.winmentor_doc_nr}) was already exported on {existing.exported_at}."
            )

    unique_parts: dict[str, str] = {}
    for a in payload.allocations:
        code = normalize_code(a.part_code)
        name = a.part_name.strip()
        if not code or not name:
            raise ValueError(f"Invalid empty part_code or part_name in allocation: {a!r}")
        unique_parts[code] = name

    upsert_parts(list(unique_parts.items()), db_path=db_path, update_stock_values=False)

    dt = datetime.strptime(payload.quote_date.strip(), "%Y-%m-%d")
    month_folder = dt.strftime("%Y-%m")
    wm_doc_nr = extract_winmentor_doc_number(payload.document_code)
    sup_slug = re.sub(
        r"[^A-Z0-9]+", "_", _ascii_clean(payload.supplier_name).upper()
    ).strip("_")[:16]
    timestamp_slug = datetime.now().strftime("%Y%m%d_%H%M%S")

    root_dir = Path(export_dir)
    archive_dir = (
        root_dir / "archive" / month_folder / f"{timestamp_slug}_{sup_slug}_{wm_doc_nr}"
    )
    archive_dir.mkdir(parents=True, exist_ok=True)

    fact_content, inv_lines = build_fact_int_content(payload)
    bon_content, vouchers_meta = build_bon_cons_content(payload)

    total_fara_tva = round(sum(line.valoare_fara_tva for line in inv_lines), 2)
    total_tva = round(sum(line.valoare_tva for line in inv_lines), 2)

    bundle_data: dict[str, Any] = {
        "factura_intrare": {
            "furnizor": _ascii_clean(payload.supplier_name).upper(),
            "cod_furnizor": _resolve_supplier_cif(
                payload.supplier_name, payload.supplier_cif
            ),
            "document_code_raw": payload.document_code,
            "numar_doc_winmentor": wm_doc_nr,
            "data_winmentor": _to_winmentor_date(payload.quote_date),
            "gestiune": _ascii_clean(payload.gestiune).upper() or DEFAULT_GESTIUNE,
            "cota_tva": payload.tva_percent,
            "total_fara_tva": total_fara_tva,
            "total_tva": total_tva,
            "total_de_plata": round(total_fara_tva + total_tva, 2),
            "items": [asdict(line) for line in inv_lines],
        },
        "bonuri_consum": vouchers_meta,
    }
    bundle_bytes = json.dumps(bundle_data, indent=2, ensure_ascii=False).encode("utf-8")

    arch_bundle = archive_dir / "export_bundle.json"
    arch_fact = archive_dir / "FACT_INT.TXT"
    arch_bundle.write_bytes(bundle_bytes)
    arch_fact.write_bytes(fact_content.encode("utf-8"))

    root_bundle = root_dir / "latest_export.json"
    root_fact = root_dir / "FACT_INT.TXT"
    root_bundle.write_bytes(bundle_bytes)
    root_fact.write_bytes(fact_content.encode("utf-8"))

    root_bon_str: str | None = None
    if bon_content is not None:
        arch_bon = archive_dir / "BON_CONS.TXT"
        root_bon = root_dir / "BON_CONS.TXT"
        arch_bon.write_bytes(bon_content.encode("utf-8"))
        root_bon.write_bytes(bon_content.encode("utf-8"))
        root_bon_str = str(root_bon)
    else:
        stale_bon = root_dir / "BON_CONS.TXT"
        if stale_bon.exists():
            stale_bon.unlink()

    record_export(
        supplier_name=payload.supplier_name,
        document_code=payload.document_code,
        winmentor_doc_nr=wm_doc_nr,
        quote_date=payload.quote_date,
        total_fara_tva=total_fara_tva,
        archive_dir=str(archive_dir),
        db_path=db_path,
    )

    return ExportResult(
        winmentor_doc_nr=wm_doc_nr,
        archive_dir=str(archive_dir),
        bundle_json_path=str(root_bundle),
        fact_int_path=str(root_fact),
        bon_cons_path=root_bon_str,
        invoice_lines_written=len(inv_lines),
        consumption_vouchers_written=len(vouchers_meta),
        nomenclature_updated=len(unique_parts),
        bundle_data=bundle_data,
    )


def retrigger_archived_export(
    archive_dir: str, export_dir: str = DEFAULT_EXPORT_DIR
) -> dict[str, str | None]:
    """Copies an archived export bundle back into the root `exports/` directory."""
    src = Path(archive_dir)
    if not src.exists():
        raise FileNotFoundError(f"Archive folder not found: {archive_dir}")

    dst = Path(export_dir)
    dst.mkdir(parents=True, exist_ok=True)

    shutil.copy2(src / "export_bundle.json", dst / "latest_export.json")
    shutil.copy2(src / "FACT_INT.TXT", dst / "FACT_INT.TXT")

    bon_src = src / "BON_CONS.TXT"
    bon_dst = dst / "BON_CONS.TXT"
    if bon_src.exists():
        shutil.copy2(bon_src, bon_dst)
        bon_path: str | None = str(bon_dst)
    else:
        if bon_dst.exists():
            bon_dst.unlink()
        bon_path = None

    return {
        "bundle_json_path": str(dst / "latest_export.json"),
        "fact_int_path": str(dst / "FACT_INT.TXT"),
        "bon_cons_path": bon_path,
    }


# --- Standalone Bon Consum Direct From Stock Export --------------------------------------

@dataclass(frozen=True)
class StandaloneBonItem:
    part_code: str
    part_name: str
    um: str = DEFAULT_UM
    quantity: float = 1.0
    unit_price: float = 0.0


@dataclass(frozen=True)
class StandaloneBonPayload:
    date: str  # YYYY-MM-DD
    doc_number: str  # e.g. "1" or "001"
    gestiune: str = DEFAULT_GESTIUNE
    departament: Department | str = ""
    vehicle: VehicleTarget | None = None
    items: list[StandaloneBonItem] = field(default_factory=list[StandaloneBonItem])
    deduct_from_stock: bool = True


@dataclass(frozen=True)
class StandaloneBonResult:
    doc_number: str
    archive_dir: str
    bon_cons_path: str
    lines_written: int
    total_valoare: float
    bundle_data: dict[str, Any]


def export_standalone_bon_consum(
    payload: StandaloneBonPayload,
    export_dir: str = DEFAULT_EXPORT_DIR,
    db_path: str = DEFAULT_DB_PATH,
) -> StandaloneBonResult:
    """
    Generates a standalone BON_CONS.TXT directly from warehouse stock (without Factura Intrare).
    """
    if not payload.items:
        raise ValueError("Cannot generate a consumption voucher with zero items.")

    wm_date = _to_winmentor_date(payload.date)
    dt = datetime.strptime(payload.date.strip(), "%Y-%m-%d")
    month_folder = dt.strftime("%Y-%m")
    gestiune = _ascii_clean(payload.gestiune).upper() or DEFAULT_GESTIUNE

    if payload.vehicle is not None and payload.vehicle.numar_inmatriculare.strip():
        obs = format_bon_obs(payload.vehicle)
        dept = (
            _ascii_clean(
                payload.vehicle.departament.value
                if isinstance(payload.vehicle.departament, Department)
                else str(payload.vehicle.departament)
            ).upper()
            if payload.vehicle.departament
            else _ascii_clean(str(payload.departament)).upper()
        )
    else:
        obs = ""
        dept = _ascii_clean(str(payload.departament)).upper()

    doc_num = _ascii_clean(payload.doc_number) or "1"
    total_val = round(sum(i.quantity * i.unit_price for i in payload.items), 2)

    lines: list[str] = [
        "[InfoPachet]",
        f"AnLucru={dt.year}",
        f"LunaLucru={dt.month:02d}",
        "TipDocument=BON DE CONSUM",
        "TotalBonuri=1",
        "",
        "[Bon_1]",
        "SerieCarnet=TOP",
        f"NumarBon={doc_num}",
        f"Data={wm_date}",
        f"Gestiune={gestiune}",
        f"CineConsuma={dept}",
        f"Observatii={obs}",
        "",
        "[Items_1]",
    ]

    voucher_items: list[BonItemDict] = []
    for idx, item in enumerate(payload.items, start=1):
        code = normalize_code(item.part_code)
        name = _ascii_clean(item.part_name).upper()
        um = _ascii_clean(item.um) or DEFAULT_UM
        val = round(item.quantity * item.unit_price, 2)
        lines.append(
            f"Item_{idx}={code};{name};{um};"
            f"{item.quantity:g};{item.unit_price:.4f};{obs}"
        )
        voucher_items.append(
            BonItemDict(
                part_code=code,
                part_name=name,
                um=um,
                quantity=round(item.quantity, 4),
                unit_price=round(item.unit_price, 4),
                valoare=val,
            )
        )

    bon_content = "\r\n".join(lines) + "\r\n"

    bon_dict = BonConsumDict(
        bon_index=1,
        serie_carnet="TOP",
        data=wm_date,
        gestiune=gestiune,
        cine_consuma=dept,
        observatii=obs,
        total_valoare=total_val,
        items=voucher_items,
    )

    bundle_data: dict[str, Any] = {
        "bonuri_consum": [bon_dict]
    }
    bundle_bytes = json.dumps(bundle_data, indent=2, ensure_ascii=False).encode("utf-8")

    target_slug = (
        re.sub(r"[^A-Z0-9]+", "_", obs.upper())
        if obs
        else (dept or "GENERAL")
    )[:16]
    timestamp_slug = datetime.now().strftime("%Y%m%d_%H%M%S")
    root_dir = Path(export_dir)
    archive_dir = (
        root_dir / "archive" / month_folder / f"{timestamp_slug}_BON_{target_slug}_{doc_num}"
    )
    archive_dir.mkdir(parents=True, exist_ok=True)

    arch_bundle = archive_dir / "export_bundle.json"
    arch_bon = archive_dir / "BON_CONS.TXT"
    root_bundle = root_dir / "latest_export.json"
    root_bon = root_dir / "BON_CONS.TXT"

    arch_bundle.write_bytes(bundle_bytes)
    arch_bon.write_bytes(bon_content.encode("utf-8"))
    root_bundle.write_bytes(bundle_bytes)
    root_bon.write_bytes(bon_content.encode("utf-8"))

    if payload.deduct_from_stock:
        deduct_stock_quantities(
            [(i.part_code, i.quantity) for i in payload.items], db_path=db_path
        )

    return StandaloneBonResult(
        doc_number=doc_num,
        archive_dir=str(archive_dir),
        bon_cons_path=str(root_bon),
        lines_written=len(payload.items),
        total_valoare=total_val,
        bundle_data=bundle_data,
    )