export type Department = 'TINICHIGERIE' | 'VOPSITORIE' | 'MECANICA'

export interface Suggestion {
  code: string
  name: string
  reason: string
  score: number
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
  matched_via: 'exact' | 'separators' | 'none'
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
}

export interface TalonCropParams {
  x1: number
  y1: number
  x2: number
  y2: number
  rotate_deg: number
  redact_owner_c: boolean
}

export interface TalonPreviewResponse extends TalonCropParams {
  warped_data_url: string
  scanned_crop_data_url: string
  detected_quad: boolean
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
  departament: Department
}

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
    departament: Department
  } | null
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

async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const data = (await res.json()) as { detail?: string }
      if (data.detail) detail = data.detail
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

export async function preprocessVehiclePreview(
  file: File,
  crop?: TalonCropParams
): Promise<TalonPreviewResponse> {
  const form = new FormData()
  form.append('file', file)
  if (crop) {
    form.append('x1', String(crop.x1))
    form.append('y1', String(crop.y1))
    form.append('x2', String(crop.x2))
    form.append('y2', String(crop.y2))
    form.append('rotate_deg', String(crop.rotate_deg))
    form.append('redact_owner_c', String(crop.redact_owner_c))
  }
  const res = await fetch('/api/preprocess-vehicle-preview', { method: 'POST', body: form })
  return handleResponse<TalonPreviewResponse>(res)
}

export async function uploadVehicleFile(
  file: File,
  crop?: TalonCropParams
): Promise<VehicleUploadResponse> {
  const form = new FormData()
  form.append('file', file)
  if (crop) {
    form.append('x1', String(crop.x1))
    form.append('y1', String(crop.y1))
    form.append('x2', String(crop.x2))
    form.append('y2', String(crop.y2))
    form.append('rotate_deg', String(crop.rotate_deg))
    form.append('redact_owner_c', String(crop.redact_owner_c))
  }
  const res = await fetch('/api/upload-vehicle', { method: 'POST', body: form })
  return handleResponse<VehicleUploadResponse>(res)
}

export async function seedExcelFile(file: File): Promise<{ articles_seeded: number; db_path: string }> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch('/api/nomenclature/seed-excel', { method: 'POST', body: form })
  return handleResponse<{ articles_seeded: number; db_path: string }>(res)
}

export async function searchNomenclature(query: string): Promise<Suggestion[]> {
  const res = await fetch(`/api/nomenclature/search?q=${encodeURIComponent(query)}&limit=8`)
  return handleResponse<Suggestion[]>(res)
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
  return handleResponse<{ bundle_json_path: string; fact_int_path: string; bon_cons_path: string | null }>(res)
}