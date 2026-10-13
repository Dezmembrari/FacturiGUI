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
  supplier_cif: string | null  // Added for CIF extraction
  document_code: string
  winmentor_doc_nr: string
  quote_date: string
  duplicate_warning: ExportLogEntry | null
  validation_issues: string[]
  parts: EnrichedPart[]
  preview_pages: string[]
}

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

export interface AllocationPayload {
  part_code: string
  part_name: string
  quantity: number
  unit_price: number
  um: string
  is_new: boolean
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
  supplier_cif?: string | null  // Added to match backend
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
  export_bundle: Record<string, any>
}

export interface StandaloneBonItemPayload {
  part_code: string
  part_name: string
  um: string
  quantity: number
  unit_price: number
  is_new: boolean
}

export interface StandaloneBonRequestPayload {
  date: string
  doc_number: string
  gestiune: string
  departament: string
  vehicle: {
    numar_inmatriculare: string
    serie_sasiu: string
    marca_model: string
    departament: string
  } | null
  items: StandaloneBonItemPayload[]
  deduct_from_stock: boolean
}

export interface StandaloneBonResponse {
  doc_number: string
  archive_dir: string
  bon_cons_path: string
  lines_written: number
  total_valoare: number
  export_bundle: Record<string, any>
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

export function extractWinmentorDocNr(docCode: string): string {
  const cleaned = docCode.trim().toUpperCase()
  if (/^\d{1,7}$/.test(cleaned)) return cleaned
  const matches = cleaned.match(/\d+/g)
  if (!matches || matches.length === 0) return cleaned
  const last = matches[matches.length - 1]
  if (last.length > 6 && last.startsWith('00')) return last.replace(/^0+/, '') || '0'
  if (last.length > 6) return last.slice(-6)
  return last
}

export function canonicalSupplierToken(sup: string): string {
  let compact = sup.normalize('NFKD').toUpperCase().replace(/[^A-Z0-9]/g, '')
  if (compact.startsWith('SC') && compact.length > 4) compact = compact.slice(2)
  for (const sfx of ['SRL', 'SNC', 'SCS', 'SA']) {
    if (compact.endsWith(sfx) && compact.length > sfx.length + 2) {
      compact = compact.slice(0, -sfx.length)
      break
    }
  }
  return compact.slice(0, 12)
}

export async function uploadQuoteFile(file: File): Promise<QuoteUploadResponse> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch('/api/upload-quote', { method: 'POST', body: form })
  return handleResponse<QuoteUploadResponse>(res)
}

export async function preprocessVehiclePreview(file: File): Promise<TalonPreviewResponse> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch('/api/preprocess-vehicle-preview', { method: 'POST', body: form })
  return handleResponse<TalonPreviewResponse>(res)
}

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

export async function submitStandaloneBonConsum(
  payload: StandaloneBonRequestPayload
): Promise<StandaloneBonResponse> {
  const res = await fetch('/api/export-bon-consum', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return handleResponse<StandaloneBonResponse>(res)
}

/**
 * Pings the local Windows PC (Auto-Filler agent) with the exported JSON bundle
 * so it immediately takes over the mouse/keyboard and types the data into WinMentor.
 */
export async function triggerLocalAutoFill(payload: Record<string, any>): Promise<void> {
  const res = await fetch('http://localhost:8001/autofill', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    throw new Error(`WinMentor Auto-Filler rejected request: HTTP ${res.status}`)
  }
}