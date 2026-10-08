export type Department = 'TINICHIGERIE' | 'VOPSITORIE' | 'MECANICA'

export interface Suggestion {
  code: string
  name: string
  reason: string
  score: number
  stoc: number
}

export interface EnrichedPart {
  quote_code: string
  quote_name: string
  quantity: number
  unit_price: number
  total_price: number
  status: 'NEW' | 'MATCH' | 'CONFLICT'
  resolved_code: string
  canonical_name: string
  matched_via: 'exact' | 'separators' | 'brand_suffix' | 'none'
  stoc: number
  sold: number
  suggestions: Suggestion[]
}

export interface ExportLogEntry {
  doc_key: string
  supplier_name: string
  document_code: string
  winmentor_doc_nr: string
  quote_date: string
  total_fara_tva: number
  archive_dir: string
  exported_at: string
}

export interface QuoteUploadResponse {
  supplier_name: string
  document_code: string
  winmentor_doc_nr: string
  quote_date: string
  duplicate_warning: ExportLogEntry | null
  validation_issues: string[]
  parts: EnrichedPart[]
  preview_pages: string[]
}

/**
 * Normalized [0.0 .. 1.0] coordinates for the local Talon scanner:
 * - (x1, y1, x2, y2): Top-Left A-to-K bounding box.
 * - (c_y1, c_y2): Relative vertical band inside the A-to-K crop where Section C
 *   (C.2/C.3 Owner Name & Address, between B and D.1) is located.
 */
export interface TalonCropParams {
  x1: number
  y1: number
  x2: number
  y2: number
  rotate_deg: number
  redact_owner_c: boolean
  c_y1: number
  c_y2: number
}

export interface TalonPreviewResponse extends TalonCropParams {
  warped_data_url: string
  scanned_full_data_url: string
  scanned_crop_data_url: string
  detected_quad: boolean
  detected_c_section: boolean
}

export interface VehicleUploadResponse {
  numar_inmatriculare: string
  serie_sasiu: string
  marca_model: string
  obs_preview: string
  validation_issues: string[]
}

/**
 * Pure vehicle entity for Bon de Consum allocations.
 * Vehicles are independent of workshop departments.
 */
export interface VehicleTarget {
  id: string
  numar_inmatriculare: string
  serie_sasiu: string
  marca_model: string
  talonScanUrl?: string
  talonFullUrl?: string
}

export interface PartRecord {
  nr_crt: number
  part_code: string
  part_name: string
  stoc: number
  sold: number
  last_used: string | null
}

export interface StockListResponse {
  total_count: number
  total_sold_ron: number
  items: PartRecord[]
}

export interface ModelStat {
  model_name: string
  success_count: number
  rate_limit_429_count: number
  unavailable_503_count: number
  other_error_count: number
  avg_latency_ms: number
  in_cooldown: boolean
  cooldown_remaining_s: number
  last_used: string | null
}

/**
 * Mutually-exclusive 3-way destination payload per invoice line item:
 * 1. Specific Vehicle:    `vehicle` is populated, `departament` is null.
 * 2. Specific Department: `departament` is populated, `vehicle` is null (general shop consumable).
 * 3. MAGAZIE (Stock):     both `vehicle` and `departament` are null.
 */
export interface AllocationPayload {
  part_code: string
  part_name: string
  quantity: number
  unit_price: number
  um: string
  vehicle: {
    numar_inmatriculare: string
    serie_sasiu: string
    marca_model: string
  } | null
  departament: Department | null
}

export interface ExportRequestPayload {
  supplier_name: string
  document_code: string
  quote_date: string
  supplier_cif?: string | null
  tva_percent: number
  gestiune: string
  allow_duplicate: boolean
  allocations: AllocationPayload[]
}

export interface ExportResponse {
  winmentor_doc_nr: string
  archive_dir: string
  bundle_json_path: string
  fact_int_path: string
  bon_cons_path: string | null
  invoice_lines_written: number
  consumption_vouchers_written: number
  nomenclature_updated: number
}

/**
 * Parses JSON responses and formats both string errors and FastAPI 422 validation
 * error arrays into human-readable messages (preventing `[object Object]`).
 */
async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const data = (await res.json()) as {
        detail?: string | Array<{ loc?: Array<string | number>; msg?: string }>
      }
      if (typeof data.detail === 'string') {
        detail = data.detail
      } else if (Array.isArray(data.detail)) {
        detail = data.detail
          .map((item) => `${(item.loc ?? []).join('.')}: ${item.msg ?? 'invalid'}`)
          .join(' | ')
      }
    } catch {
      detail = await res.text()
    }
    throw new Error(detail)
  }
  return (await res.json()) as T
}

export async function uploadQuoteFile(file: File): Promise<QuoteUploadResponse> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch('/api/upload-quote', { method: 'POST', body: form })
  return handleResponse<QuoteUploadResponse>(res)
}

/**
 * Sends a Talon file to the local OpenCV endpoint (zero Google calls) to auto-upright,
 * deskew, and detect the initial Top-Left A-to-K and Section C.2/C.3 bounds.
 */
export async function preprocessVehiclePreview(file: File): Promise<TalonPreviewResponse> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch('/api/preprocess-vehicle-preview', { method: 'POST', body: form })
  return handleResponse<TalonPreviewResponse>(res)
}

/**
 * Uploads the final cropped, rotated, and C-redacted PNG Blob generated by the
 * browser `<canvas>` to Gemini OCR.
 */
export async function uploadVehicleFile(croppedPngBlob: Blob): Promise<VehicleUploadResponse> {
  const form = new FormData()
  form.append('file', croppedPngBlob, 'talon_cropped_scan.png')
  const res = await fetch('/api/upload-vehicle', { method: 'POST', body: form })
  return handleResponse<VehicleUploadResponse>(res)
}

export async function seedExcelFile(
  file: File
): Promise<{ articles_seeded: number; db_path: string }> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch('/api/nomenclature/seed-excel', { method: 'POST', body: form })
  return handleResponse<{ articles_seeded: number; db_path: string }>(res)
}

export async function searchNomenclature(query: string): Promise<Suggestion[]> {
  const res = await fetch(`/api/nomenclature/search?q=${encodeURIComponent(query)}&limit=10`)
  return handleResponse<Suggestion[]>(res)
}

export async function fetchStockList(
  search = '',
  onlyInStock = false,
  limit = 200,
  offset = 0
): Promise<StockListResponse> {
  const params = new URLSearchParams({
    search,
    only_in_stock: String(onlyInStock),
    limit: String(limit),
    offset: String(offset),
  })
  const res = await fetch(`/api/nomenclature/list?${params.toString()}`)
  return handleResponse<StockListResponse>(res)
}

export async function upsertStockPart(partCode: string, partName: string): Promise<void> {
  const res = await fetch('/api/nomenclature/upsert', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ part_code: partCode, part_name: partName }),
  })
  await handleResponse<{ status: string }>(res)
}

export async function deleteStockPart(partCode: string): Promise<void> {
  const res = await fetch(`/api/nomenclature/${encodeURIComponent(partCode)}`, {
    method: 'DELETE',
  })
  await handleResponse<{ deleted: boolean }>(res)
}

export async function fetchModelStats(): Promise<ModelStat[]> {
  const res = await fetch('/api/models/stats')
  return handleResponse<ModelStat[]>(res)
}

export async function submitExport(payload: ExportRequestPayload): Promise<ExportResponse> {
  const res = await fetch('/api/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return handleResponse<ExportResponse>(res)
}

export async function fetchExportHistory(): Promise<ExportLogEntry[]> {
  const res = await fetch('/api/exports/history?limit=30')
  return handleResponse<ExportLogEntry[]>(res)
}

export async function retriggerExport(archiveDir: string): Promise<{
  bundle_json_path: string
  fact_int_path: string
  bon_cons_path: string | null
}> {
  const res = await fetch('/api/exports/retrigger', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ archive_dir: archiveDir }),
  })
  return handleResponse<{
    bundle_json_path: string
    fact_int_path: string
    bon_cons_path: string | null
  }>(res)
}