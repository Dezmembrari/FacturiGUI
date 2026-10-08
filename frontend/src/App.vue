<script setup lang="ts">
import { ref, computed } from 'vue'
import {
  type Department,
  type Suggestion,
  type VehicleTarget,
  type ExportLogEntry,
  type ExportResponse,
  type TalonCropParams,
  uploadQuoteFile,
  preprocessVehiclePreview,
  uploadVehicleFile,
  seedExcelFile,
  searchNomenclature,
  submitExport,
  fetchExportHistory,
  retriggerExport,
} from './api'

interface StagedRow {
  rowId: string
  quote_code: string
  quote_name: string
  part_code: string
  part_name: string
  canonical_name: string
  status: 'NEW' | 'MATCH' | 'CONFLICT'
  matched_via: 'exact' | 'separators' | 'none'
  suggestions: Suggestion[]
  um: 'Buc' | 'L'
  quantity: number
  unit_price: number
  targetVehicleId: string
}

interface QuoteQueueItem {
  id: string
  fileName: string
  file: File | null
  state: 'queued' | 'extracting' | 'ready' | 'exported' | 'error'
  errorMsg: string | null
  supplierName: string
  documentCode: string
  quoteDate: string
  tvaPercent: number
  gestiune: string
  validationIssues: string[]
  duplicateWarning: ExportLogEntry | null
  allowDuplicateOverride: boolean
  rows: StagedRow[]
  exportResult: ExportResponse | null
}

const departments: Department[] = ['TINICHIGERIE', 'VOPSITORIE', 'MECANICA']

const busyMessage = ref<string | null>(null)
const errorMessage = ref<string | null>(null)
const toastMessage = ref<string | null>(null)
const isDraggingOver = ref(false)

// Quote Queue
const quoteQueue = ref<QuoteQueueItem[]>([])
const activeQueueId = ref<string | null>(null)
const isProcessingQueue = ref(false)

// Vehicles
const vehicles = ref<VehicleTarget[]>([])
const bulkVehicleId = ref<string>('')
const newCarPlate = ref('')
const newCarModel = ref('')
const newCarVin = ref('')
const newCarDept = ref<Department>('TINICHIGERIE')
const vehicleIssues = ref<string[]>([])

// Local Talon Privacy Scanner Modal State
const showTalonModal = ref(false)
const pendingTalonFile = ref<File | null>(null)
const talonWarpedUrl = ref('')
const talonScannedCropUrl = ref('')
const talonDetectedQuad = ref(false)
const talonCrop = ref<TalonCropParams>({
  x1: 0.02,
  y1: 0.02,
  x2: 0.56,
  y2: 0.62,
  rotate_deg: 0,
  redact_owner_c: false,
})

// Catalog Search Popover
const activeSearchRowId = ref<string | null>(null)
const searchQuery = ref('')
const searchResults = ref<Suggestion[]>([])

// Archive History Modal
const showHistoryModal = ref(false)
const exportHistory = ref<ExportLogEntry[]>([])

const fallbackQuote = ref<QuoteQueueItem>({
  id: 'scratchpad',
  fileName: 'Manual Entry',
  file: null,
  state: 'ready',
  errorMsg: null,
  supplierName: '',
  documentCode: '',
  quoteDate: '',
  tvaPercent: 21,
  gestiune: 'MAGAZIE',
  validationIssues: [],
  duplicateWarning: null,
  allowDuplicateOverride: false,
  rows: [],
  exportResult: null,
})

const currentQuote = computed<QuoteQueueItem>(() => {
  if (!activeQueueId.value) return fallbackQuote.value
  return quoteQueue.value.find((q) => q.id === activeQueueId.value) ?? fallbackQuote.value
})

const nextReadyQuote = computed<QuoteQueueItem | null>(() => {
  const list = quoteQueue.value
  const currentIndex = list.findIndex((q) => q.id === activeQueueId.value)
  for (let i = currentIndex + 1; i < list.length; i++) {
    if (list[i].state === 'ready' || list[i].state === 'queued' || list[i].state === 'extracting') {
      return list[i]
    }
  }
  return list.find((q) => q.id !== activeQueueId.value && q.state === 'ready') ?? null
})

const queueStats = computed(() => {
  const total = quoteQueue.value.length
  const exported = quoteQueue.value.filter((q) => q.state === 'exported').length
  const ready = quoteQueue.value.filter((q) => q.state === 'ready').length
  const pending = quoteQueue.value.filter((q) => q.state === 'queued' || q.state === 'extracting').length
  return { total, exported, ready, pending }
})

function showToast(msg: string) {
  toastMessage.value = msg
  setTimeout(() => {
    if (toastMessage.value === msg) toastMessage.value = null
  }, 3200)
}

function copyText(text: string, label: string) {
  navigator.clipboard.writeText(text)
  showToast(`Copied ${label}: ${text}`)
}

function computeWinmentorDocNr(docCode: string): string {
  const matches = docCode.match(/\d+/g)
  if (!matches || matches.length === 0) return docCode.trim()
  let last = matches[matches.length - 1]
  if (last.length > 6) last = last.slice(-6)
  return last.replace(/^0+/, '') || '0'
}

const winmentorDocNrPreview = computed(() => computeWinmentorDocNr(currentQuote.value.documentCode))

function formatBonObs(car: VehicleTarget): string {
  const plateClean = car.numar_inmatriculare.toLowerCase().replace(/[^a-z0-9]/g, '')
  const modelClean = car.marca_model.trim().toLowerCase().replace(/\s+/g, ' ')
  return modelClean ? `${modelClean} ${plateClean}` : plateClean
}

const invoiceTotalExVat = computed(() =>
  currentQuote.value.rows.reduce(
    (acc, r) => acc + Math.round(r.quantity * r.unit_price * 100) / 100,
    0
  )
)

const invoiceTotalVat = computed(() =>
  Math.round(invoiceTotalExVat.value * (currentQuote.value.tvaPercent / 100) * 100) / 100
)

const unresolvedConflictsCount = computed(
  () => currentQuote.value.rows.filter((r) => r.status === 'CONFLICT').length
)

// --- Bulk Quote Queue ---

function enqueueQuoteFiles(fileList: FileList | File[]) {
  const files = Array.from(fileList)
  if (files.length === 0) return

  let firstNewId: string | null = null
  for (const file of files) {
    const id = `q_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`
    if (!firstNewId) firstNewId = id
    quoteQueue.value.push({
      id,
      fileName: file.name,
      file,
      state: 'queued',
      errorMsg: null,
      supplierName: '',
      documentCode: '',
      quoteDate: '',
      tvaPercent: 21,
      gestiune: 'MAGAZIE',
      validationIssues: [],
      duplicateWarning: null,
      allowDuplicateOverride: false,
      rows: [],
      exportResult: null,
    })
  }

  if (!activeQueueId.value || currentQuote.value.state === 'exported') {
    activeQueueId.value = firstNewId
  }

  showToast(`Added ${files.length} file(s) to the extraction queue`)
  void processQuoteQueue()
}

function onQuoteFilesSelected(e: Event) {
  const input = e.target as HTMLInputElement
  if (input.files && input.files.length > 0) {
    enqueueQuoteFiles(input.files)
    input.value = ''
  }
}

function onDropFiles(e: DragEvent) {
  isDraggingOver.value = false
  if (e.dataTransfer?.files && e.dataTransfer.files.length > 0) {
    enqueueQuoteFiles(e.dataTransfer.files)
  }
}

async function processQuoteQueue() {
  if (isProcessingQueue.value) return
  isProcessingQueue.value = true

  try {
    while (true) {
      const nextItem = quoteQueue.value.find((q) => q.state === 'queued')
      if (!nextItem || !nextItem.file) break

      nextItem.state = 'extracting'
      nextItem.errorMsg = null

      try {
        const data = await uploadQuoteFile(nextItem.file)
        nextItem.supplierName = data.supplier_name
        nextItem.documentCode = data.document_code
        nextItem.quoteDate = data.quote_date
        nextItem.validationIssues = data.validation_issues
        nextItem.duplicateWarning = data.duplicate_warning
        nextItem.allowDuplicateOverride = false

        const defaultCarId = vehicles.value.length === 1 ? vehicles.value[0].id : ''
        nextItem.rows = data.parts.map((p, idx) => ({
          rowId: `${nextItem.id}_row_${idx}`,
          quote_code: p.quote_code,
          quote_name: p.quote_name,
          part_code: p.resolved_code,
          part_name: p.canonical_name,
          canonical_name: p.canonical_name,
          status: p.status,
          matched_via: p.matched_via,
          suggestions: p.suggestions,
          um: 'Buc',
          quantity: p.quantity,
          unit_price: p.unit_price,
          targetVehicleId: defaultCarId,
        }))
        nextItem.state = 'ready'
      } catch (err) {
        nextItem.state = 'error'
        nextItem.errorMsg = err instanceof Error ? err.message : String(err)
      }
    }
  } finally {
    isProcessingQueue.value = false
  }
}

function retryQueueItem(item: QuoteQueueItem) {
  if (!item.file) return
  item.state = 'queued'
  item.errorMsg = null
  void processQuoteQueue()
}

function removeQueueItem(id: string) {
  const idx = quoteQueue.value.findIndex((q) => q.id === id)
  if (idx === -1) return
  quoteQueue.value.splice(idx, 1)
  if (activeQueueId.value === id) {
    activeQueueId.value = quoteQueue.value[idx]?.id ?? quoteQueue.value[idx - 1]?.id ?? null
  }
}

function clearExportedFromQueue() {
  quoteQueue.value = quoteQueue.value.filter((q) => q.state !== 'exported')
  if (!quoteQueue.value.some((q) => q.id === activeQueueId.value)) {
    activeQueueId.value = quoteQueue.value[0]?.id ?? null
  }
}

// --- Local Talon Privacy Scanner & A-to-K Cropper ---

async function onVehicleFileSelected(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  input.value = ''

  pendingTalonFile.value = file
  errorMessage.value = null
  vehicleIssues.value = []
  busyMessage.value = `Running local OpenCV document scanner & A–K crop on ${file.name}...`
  try {
    const prev = await preprocessVehiclePreview(file)
    talonWarpedUrl.value = prev.warped_data_url
    talonScannedCropUrl.value = prev.scanned_crop_data_url
    talonDetectedQuad.value = prev.detected_quad
    talonCrop.value = {
      x1: prev.x1,
      y1: prev.y1,
      x2: prev.x2,
      y2: prev.y2,
      rotate_deg: prev.rotate_deg,
      redact_owner_c: prev.redact_owner_c,
    }
    showTalonModal.value = true
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    busyMessage.value = null
  }
}

async function refreshLocalTalonPreview() {
  if (!pendingTalonFile.value) return
  try {
    const prev = await preprocessVehiclePreview(pendingTalonFile.value, talonCrop.value)
    talonWarpedUrl.value = prev.warped_data_url
    talonScannedCropUrl.value = prev.scanned_crop_data_url
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : String(err)
  }
}

function rotateTalon90() {
  talonCrop.value.rotate_deg = (talonCrop.value.rotate_deg + 90) % 360
  void refreshLocalTalonPreview()
}

async function confirmAndSendTalonToGemini() {
  if (!pendingTalonFile.value) return
  const file = pendingTalonFile.value
  showTalonModal.value = false
  busyMessage.value = 'Uploading ONLY the cropped A–K scan patch to Gemini OCR...'
  try {
    const doc = await uploadVehicleFile(file, talonCrop.value)
    vehicleIssues.value = doc.validation_issues
    const newCar: VehicleTarget = {
      id: `car_${Date.now()}`,
      numar_inmatriculare: doc.numar_inmatriculare,
      serie_sasiu: doc.serie_sasiu,
      marca_model: doc.marca_model,
      departament: newCarDept.value,
    }
    vehicles.value.push(newCar)
    showToast(`Added vehicle ${newCar.numar_inmatriculare} (${formatBonObs(newCar)})`)
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    busyMessage.value = null
    pendingTalonFile.value = null
  }
}

function addManualVehicle() {
  if (!newCarPlate.value.trim()) return
  const newCar: VehicleTarget = {
    id: `car_${Date.now()}`,
    numar_inmatriculare: newCarPlate.value.trim().toUpperCase(),
    serie_sasiu: newCarVin.value.trim().toUpperCase(),
    marca_model: newCarModel.value.trim().toUpperCase(),
    departament: newCarDept.value,
  }
  vehicles.value.push(newCar)
  newCarPlate.value = ''
  newCarModel.value = ''
  newCarVin.value = ''
  showToast(`Added vehicle ${newCar.numar_inmatriculare}`)
}

function removeVehicle(id: string) {
  vehicles.value = vehicles.value.filter((v) => v.id !== id)
  for (const q of quoteQueue.value) {
    for (const r of q.rows) {
      if (r.targetVehicleId === id) r.targetVehicleId = ''
    }
  }
  for (const r of fallbackQuote.value.rows) {
    if (r.targetVehicleId === id) r.targetVehicleId = ''
  }
}

function assignAllRowsTo(vehicleId: string) {
  for (const r of currentQuote.value.rows) {
    r.targetVehicleId = vehicleId
  }
  showToast(vehicleId ? 'Assigned all rows to selected car' : 'Assigned all rows to MAGAZIE')
}

// --- Row Actions ---

function applySuggestion(row: StagedRow, sug: Suggestion) {
  row.part_code = sug.code
  row.part_name = sug.name
  row.canonical_name = sug.name
  row.status = 'MATCH'
  row.matched_via = 'exact'
  activeSearchRowId.value = null
  showToast(`Mapped to WinMentor article [${sug.code}]`)
}

function resolveConflict(row: StagedRow, choice: 'canonical' | 'quote') {
  row.part_name = choice === 'canonical' ? row.canonical_name : row.quote_name.toUpperCase()
  row.status = 'MATCH'
}

function splitRow(index: number) {
  const orig = currentQuote.value.rows[index]
  const splitQty = orig.quantity > 1 ? 1 : Number((orig.quantity / 2).toFixed(2))
  orig.quantity = Number((orig.quantity - splitQty).toFixed(4))

  const clone: StagedRow = {
    ...orig,
    rowId: `row_${Date.now()}_split`,
    suggestions: [...orig.suggestions],
    quantity: splitQty,
    targetVehicleId: '',
  }
  currentQuote.value.rows.splice(index + 1, 0, clone)
}

function removeRow(index: number) {
  currentQuote.value.rows.splice(index, 1)
}

function addBlankRow() {
  currentQuote.value.rows.push({
    rowId: `row_${Date.now()}`,
    quote_code: '',
    quote_name: '',
    part_code: '',
    part_name: '',
    canonical_name: '',
    status: 'NEW',
    matched_via: 'none',
    suggestions: [],
    um: 'Buc',
    quantity: 1,
    unit_price: 0,
    targetVehicleId: '',
  })
}

// --- Nomenclature Live Search ---

async function openRowSearch(row: StagedRow) {
  if (activeSearchRowId.value === row.rowId) {
    activeSearchRowId.value = null
    return
  }
  activeSearchRowId.value = row.rowId
  searchQuery.value = row.part_code || row.part_name.slice(0, 12)
  await runLiveSearch()
}

async function runLiveSearch() {
  if (searchQuery.value.trim().length < 2) {
    searchResults.value = []
    return
  }
  try {
    searchResults.value = await searchNomenclature(searchQuery.value.trim())
  } catch {
    searchResults.value = []
  }
}

// --- Monthly Excel Sync & Export History ---

async function onSeedExcelSelected(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  input.value = ''

  busyMessage.value = `Syncing nomenclature.db from ${file.name}...`
  errorMessage.value = null
  try {
    const res = await seedExcelFile(file)
    showToast(`Synced ${res.articles_seeded} WinMentor articles from ${file.name}`)
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    busyMessage.value = null
  }
}

async function openHistoryModal() {
  showHistoryModal.value = true
  try {
    exportHistory.value = await fetchExportHistory()
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : String(err)
  }
}

async function handleRetrigger(entry: ExportLogEntry) {
  busyMessage.value = `Re-triggering WinMentor #${entry.winmentor_doc_nr}...`
  try {
    await retriggerExport(entry.archive_dir)
    showToast(`Restored #${entry.winmentor_doc_nr} (${entry.supplier_name}) to active export folder!`)
    showHistoryModal.value = false
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    busyMessage.value = null
  }
}

// --- Final Export Submission ---

async function handleExport(advanceToNext = false) {
  const q = currentQuote.value
  errorMessage.value = null
  busyMessage.value = 'Writing WinMentor archive bundle & updating nomenclature.db...'
  try {
    const allocations = q.rows.map((r) => {
      const car = vehicles.value.find((v) => v.id === r.targetVehicleId) || null
      return {
        part_code: r.part_code,
        part_name: r.part_name,
        quantity: r.quantity,
        unit_price: r.unit_price,
        um: r.um,
        vehicle: car
          ? {
              numar_inmatriculare: car.numar_inmatriculare,
              serie_sasiu: car.serie_sasiu,
              marca_model: car.marca_model,
              departament: car.departament,
            }
          : null,
      }
    })

    const res = await submitExport({
      supplier_name: q.supplierName,
      document_code: q.documentCode,
      quote_date: q.quoteDate,
      tva_percent: q.tvaPercent,
      gestiune: q.gestiune,
      allow_duplicate: q.allowDuplicateOverride,
      allocations,
    })

    q.exportResult = res
    q.state = 'exported'
    q.duplicateWarning = null
    showToast(`Exported WinMentor Factura #${res.winmentor_doc_nr}!`)

    if (advanceToNext && nextReadyQuote.value) {
      activeQueueId.value = nextReadyQuote.value.id
    }
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    busyMessage.value = null
  }
}
</script>

<template>
  <div class="app-shell">
    <!-- Top Navigation Bar -->
    <header class="topbar">
      <div class="brand">
        <span class="brand-badge">TOP SPEED SERVICE</span>
        <h1>FacturiGUI <span class="muted">→ WinMentor 25.082/1 BDE</span></h1>
      </div>
      <div class="topbar-actions">
        <label class="btn btn-secondary">
          Sync Monthly Excel (.xlsx)
          <input type="file" accept=".xlsx,.xls" hidden @change="onSeedExcelSelected" />
        </label>
        <button class="btn btn-secondary" @click="openHistoryModal">
          Archive / Re-Trigger History
        </button>
      </div>
    </header>

    <!-- Status Banners -->
    <div v-if="busyMessage" class="banner banner-info">{{ busyMessage }}</div>
    <div v-if="errorMessage" class="banner banner-error">
      <span>{{ errorMessage }}</span>
      <button class="btn-close" @click="errorMessage = null">×</button>
    </div>
    <div v-if="toastMessage" class="toast">{{ toastMessage }}</div>

    <!-- Main 2-Column Workspace -->
    <main class="workspace">
      <!-- Left Column: Active Vehicles (Bon Consum Targets) -->
      <aside class="panel vehicles-panel">
        <div class="panel-header">
          <h2>1. Active Vehicles (Bon Consum)</h2>
          <label class="btn btn-primary btn-sm">
            + Scan Talon
            <input type="file" accept=".pdf,image/*" hidden @change="onVehicleFileSelected" />
          </label>
        </div>

        <div v-if="vehicleIssues.length" class="alert-box">
          <div v-for="(iss, i) in vehicleIssues" :key="i">⚠️ {{ iss }}</div>
        </div>

        <!-- Manual Car Form -->
        <div class="manual-car-box">
          <input v-model="newCarPlate" placeholder="Plate (e.g. CT-61-MLG)" />
          <select v-model="newCarDept">
            <option v-for="d in departments" :key="d" :value="d">{{ d }}</option>
          </select>
          <input
            v-model="newCarModel"
            placeholder="Make & Model (e.g. OPEL CORSA)"
            class="full-span"
          />
          <button class="btn btn-secondary btn-sm full-span" @click="addManualVehicle">
            + Add Car Manually
          </button>
        </div>

        <!-- Vehicle Cards List -->
        <div class="vehicle-list">
          <div class="vehicle-card stock-card">
            <div>
              <strong>MAGAZIE (General Stock)</strong>
              <div class="subtext">Entered on Factura Intrare (NIR) only — no Bon Consum</div>
            </div>
          </div>

          <div v-for="car in vehicles" :key="car.id" class="vehicle-card">
            <div class="vehicle-card-top">
              <span class="plate-tag">{{ car.numar_inmatriculare }}</span>
              <select v-model="car.departament" class="dept-select">
                <option v-for="d in departments" :key="d" :value="d">{{ d }}</option>
              </select>
              <button class="btn-icon" title="Remove car" @click="removeVehicle(car.id)">✕</button>
            </div>
            <div class="car-inputs">
              <input v-model="car.marca_model" placeholder="Make & Model (e.g. OPEL CORSA)" />
              <input v-model="car.serie_sasiu" placeholder="VIN (optional)" class="mono" />
            </div>
            <div class="obs-preview" @click="copyText(formatBonObs(car), '4.Obs')">
              <span>WinMentor <code>4.Obs</code>: <strong>{{ formatBonObs(car) }}</strong></span>
              <span class="copy-hint">Copy</span>
            </div>
          </div>
        </div>
      </aside>

      <!-- Right Column: Bulk Quote Queue + Invoice Staging -->
      <section
        class="panel invoice-panel"
        :class="{ 'dropzone-active': isDraggingOver }"
        @dragover.prevent="isDraggingOver = true"
        @dragleave.prevent="isDraggingOver = false"
        @drop.prevent="onDropFiles"
      >
        <div class="panel-header">
          <div>
            <h2>2. Supplier Quote / Invoice Staging</h2>
            <span class="subtext">
              Select multiple PDFs/photos or drag & drop a batch of invoices onto this panel
            </span>
          </div>
          <div class="header-buttons">
            <button
              v-if="queueStats.exported > 0"
              class="btn btn-secondary btn-sm"
              @click="clearExportedFromQueue"
            >
              Clear Exported ({{ queueStats.exported }})
            </button>
            <label class="btn btn-primary">
              + Upload Supplier PDFs / Photos (Bulk)
              <input
                type="file"
                accept=".pdf,image/*"
                multiple
                hidden
                @change="onQuoteFilesSelected"
              />
            </label>
          </div>
        </div>

        <!-- Bulk Quote Queue Bar -->
        <div v-if="quoteQueue.length > 0" class="queue-bar">
          <div class="queue-bar-header">
            <span class="queue-title">
              Invoice Queue ({{ queueStats.exported }}/{{ queueStats.total }} exported<span v-if="queueStats.pending > 0">, {{ queueStats.pending }} extracting in background...</span>)
            </span>
          </div>
          <div class="queue-pills">
            <div
              v-for="(q, idx) in quoteQueue"
              :key="q.id"
              class="queue-pill"
              :class="[
                `pill-${q.state}`,
                { 'pill-active': q.id === activeQueueId },
              ]"
              @click="activeQueueId = q.id"
            >
              <span class="pill-index">{{ idx + 1 }}.</span>
              <span v-if="q.state === 'queued'" title="Waiting in queue">⏳</span>
              <span v-else-if="q.state === 'extracting'" title="Extracting via Gemini OCR">🔄</span>
              <span v-else-if="q.state === 'ready'" title="Ready to review">📋</span>
              <span v-else-if="q.state === 'exported'" title="Exported">✅</span>
              <span v-else-if="q.state === 'error'" title="Extraction failed">⚠️</span>

              <span class="pill-label">
                <template v-if="q.supplierName">
                  {{ q.supplierName.slice(0, 14) }} #{{ computeWinmentorDocNr(q.documentCode) }}
                </template>
                <template v-else>
                  {{ q.fileName }}
                </template>
              </span>

              <button
                class="pill-close"
                title="Remove from queue"
                @click.stop="removeQueueItem(q.id)"
              >
                ×
              </button>
            </div>
          </div>
        </div>

        <!-- Queue Item Extracting or Error State -->
        <div v-if="currentQuote.state === 'extracting' || currentQuote.state === 'queued'" class="queue-loading-box">
          <strong>🔄 Extracting {{ currentQuote.fileName }} via Gemini OCR...</strong>
          <p class="subtext">
            You can switch to any other ready invoice in the queue bar above while this finishes.
          </p>
        </div>

        <div v-else-if="currentQuote.state === 'error'" class="alert-box duplicate-box">
          <div>
            <strong>Failed to extract {{ currentQuote.fileName }}:</strong>
            {{ currentQuote.errorMsg }}
          </div>
          <button class="btn btn-primary btn-sm" @click="retryQueueItem(currentQuote)">
            Retry Extraction
          </button>
        </div>

        <!-- Ready / Exported Invoice Workspace -->
        <template v-else>
          <div v-if="currentQuote.duplicateWarning" class="alert-box duplicate-box">
            <div>
              <strong>Duplicate Invoice Detected:</strong>
              {{ currentQuote.duplicateWarning.supplier_name }}
              <code>{{ currentQuote.duplicateWarning.document_code }}</code>
              (WinMentor #{{ currentQuote.duplicateWarning.winmentor_doc_nr }}) was already exported on
              {{ currentQuote.duplicateWarning.exported_at }}.
            </div>
            <label class="checkbox-label">
              <input v-model="currentQuote.allowDuplicateOverride" type="checkbox" />
              Allow re-exporting this invoice anyway
            </label>
          </div>

          <div v-if="currentQuote.validationIssues.length" class="alert-box">
            <strong>Validation Warnings:</strong>
            <div v-for="(iss, i) in currentQuote.validationIssues" :key="i">• {{ iss }}</div>
          </div>

          <div class="invoice-meta-grid">
            <div class="field">
              <label>Furnizor (Supplier)</label>
              <input v-model="currentQuote.supplierName" placeholder="e.g. AUTONET IMPORT SRL" />
            </div>
            <div class="field">
              <label>Raw Document Code</label>
              <input v-model="currentQuote.documentCode" placeholder="e.g. FB2AX10394623" />
            </div>
            <div class="field">
              <label>WinMentor <code>Numar</code> (Rule 2.2)</label>
              <div class="copy-input-group">
                <input :value="winmentorDocNrPreview" readonly class="mono bold" />
                <button
                  class="btn btn-secondary btn-sm"
                  @click="copyText(winmentorDocNrPreview, 'Numar Factura')"
                >
                  Copy
                </button>
              </div>
            </div>
            <div class="field">
              <label>Invoice Date (YYYY-MM-DD)</label>
              <input v-model="currentQuote.quoteDate" type="date" />
            </div>
            <div class="field">
              <label>Gestiune</label>
              <input v-model="currentQuote.gestiune" />
            </div>
            <div class="field">
              <label>Bulk Assign All Rows</label>
              <div class="copy-input-group">
                <select v-model="bulkVehicleId">
                  <option value="">MAGAZIE (Stoc)</option>
                  <option v-for="c in vehicles" :key="c.id" :value="c.id">
                    {{ c.numar_inmatriculare }} ({{ c.departament }})
                  </option>
                </select>
                <button class="btn btn-secondary btn-sm" @click="assignAllRowsTo(bulkVehicleId)">
                  Apply
                </button>
              </div>
            </div>
          </div>

          <!-- Parts Table -->
          <div class="table-container">
            <table class="parts-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Status</th>
                  <th>WinMentor <code>Cod extern</code></th>
                  <th>WinMentor Article Name (<code>Denumire</code>)</th>
                  <th>UM</th>
                  <th>Qty</th>
                  <th>Unit Price</th>
                  <th>Line Total</th>
                  <th>Destination (Car / Stock)</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                <tr v-if="currentQuote.rows.length === 0">
                  <td colspan="10" class="empty-cell">
                    Upload one or more supplier PDFs/photos above or click "+ Add Line" below.
                  </td>
                </tr>
                <tr
                  v-for="(row, idx) in currentQuote.rows"
                  :key="row.rowId"
                  :class="`status-row-${row.status.toLowerCase()}`"
                >
                  <td class="mono">{{ idx + 1 }}</td>

                  <td>
                    <span :class="`badge badge-${row.status.toLowerCase()}`">
                      {{ row.status }}
                      <small v-if="row.matched_via === 'separators'">(sep)</small>
                    </span>
                  </td>

                  <td class="code-cell">
                    <div class="copy-input-group">
                      <input v-model="row.part_code" class="mono input-code" />
                      <button
                        class="btn-icon"
                        title="Copy Code"
                        @click="copyText(row.part_code, 'Cod extern')"
                      >
                        📋
                      </button>
                      <button
                        class="btn-icon"
                        title="Search WinMentor Catalog"
                        @click="openRowSearch(row)"
                      >
                        🔍
                      </button>
                    </div>

                    <div
                      v-if="row.status === 'NEW' && row.suggestions.length"
                      class="suggestions-list"
                    >
                      <small>Suggestions:</small>
                      <button
                        v-for="s in row.suggestions"
                        :key="s.code"
                        class="chip"
                        :title="`${s.name} (${s.reason})`"
                        @click="applySuggestion(row, s)"
                      >
                        Use {{ s.code }}
                      </button>
                    </div>

                    <div v-if="activeSearchRowId === row.rowId" class="search-popover">
                      <div class="search-bar">
                        <input
                          v-model="searchQuery"
                          placeholder="Search code or name..."
                          @input="runLiveSearch"
                        />
                        <button class="btn-icon" @click="activeSearchRowId = null">✕</button>
                      </div>
                      <div
                        v-for="hit in searchResults"
                        :key="hit.code"
                        class="search-hit"
                        @click="applySuggestion(row, hit)"
                      >
                        <strong class="mono">{{ hit.code }}</strong> — {{ hit.name }}
                      </div>
                      <div v-if="searchResults.length === 0" class="subtext">
                        No matching articles
                      </div>
                    </div>
                  </td>

                  <td>
                    <input v-model="row.part_name" class="input-name" />
                    <div v-if="row.status === 'CONFLICT'" class="conflict-box">
                      <span>
                        WinMentor has: <strong>{{ row.canonical_name }}</strong> | Quote says:
                        <strong>{{ row.quote_name }}</strong>
                      </span>
                      <div class="conflict-actions">
                        <button class="chip" @click="resolveConflict(row, 'canonical')">
                          Keep WinMentor
                        </button>
                        <button class="chip" @click="resolveConflict(row, 'quote')">
                          Use Quote Name
                        </button>
                      </div>
                    </div>
                  </td>

                  <td>
                    <select v-model="row.um" class="input-um">
                      <option value="Buc">Buc</option>
                      <option value="L">L</option>
                    </select>
                  </td>

                  <td>
                    <input
                      v-model.number="row.quantity"
                      type="number"
                      step="0.01"
                      class="input-num mono"
                    />
                  </td>

                  <td>
                    <input
                      v-model.number="row.unit_price"
                      type="number"
                      step="0.0001"
                      class="input-num mono"
                    />
                  </td>

                  <td class="mono bold text-right">
                    {{ (row.quantity * row.unit_price).toFixed(2) }}
                  </td>

                  <td>
                    <select v-model="row.targetVehicleId" class="input-target">
                      <option value="">📦 MAGAZIE (Stoc)</option>
                      <option v-for="car in vehicles" :key="car.id" :value="car.id">
                        🚗 {{ car.numar_inmatriculare }} ({{ car.departament }})
                      </option>
                    </select>
                  </td>

                  <td class="actions-cell">
                    <button
                      class="btn btn-secondary btn-xs"
                      title="Split quantity across cars/stock"
                      @click="splitRow(idx)"
                    >
                      Split
                    </button>
                    <button class="btn-icon" title="Delete row" @click="removeRow(idx)">🗑️</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="invoice-footer">
            <div class="footer-left">
              <button class="btn btn-secondary" @click="addBlankRow">+ Add Line</button>
              <div class="totals-pills">
                <span>Total Fără TVA: <strong>{{ invoiceTotalExVat.toFixed(2) }} RON</strong></span>
                <span>TVA ({{ currentQuote.tvaPercent }}%): <strong>{{ invoiceTotalVat.toFixed(2) }} RON</strong></span>
                <span>Total de Plată: <strong>{{ (invoiceTotalExVat + invoiceTotalVat).toFixed(2) }} RON</strong></span>
              </div>
            </div>

            <div class="footer-actions">
              <button
                class="btn btn-success"
                :disabled="
                  currentQuote.rows.length === 0 ||
                  (currentQuote.duplicateWarning !== null && !currentQuote.allowDuplicateOverride)
                "
                @click="handleExport(false)"
              >
                Export Bundle & Save
                <span v-if="unresolvedConflictsCount > 0">
                  ({{ unresolvedConflictsCount }} conflicts)
                </span>
              </button>

              <button
                v-if="nextReadyQuote"
                class="btn btn-primary"
                :disabled="
                  currentQuote.rows.length === 0 ||
                  (currentQuote.duplicateWarning !== null && !currentQuote.allowDuplicateOverride)
                "
                @click="handleExport(true)"
              >
                Export & Next in Queue →
              </button>
            </div>
          </div>

          <div v-if="currentQuote.exportResult" class="export-summary-card">
            <h3>
              ✅ Export Complete — WinMentor Factura #{{ currentQuote.exportResult.winmentor_doc_nr }}
            </h3>
            <p class="subtext">
              Archived to: <code>{{ currentQuote.exportResult.archive_dir }}</code>
            </p>
            <div class="summary-stats">
              <span>
                Invoice Lines:
                <strong>{{ currentQuote.exportResult.invoice_lines_written }}</strong>
              </span>
              <span>
                Bonuri Consum Generated:
                <strong>{{ currentQuote.exportResult.consumption_vouchers_written }}</strong>
              </span>
              <span>
                Nomenclature Updated:
                <strong>{{ currentQuote.exportResult.nomenclature_updated }} articles</strong>
              </span>
            </div>
          </div>
        </template>
      </section>
    </main>

    <!-- Local Talon Privacy Scanner & Top-Left A-to-K Crop Modal -->
    <div v-if="showTalonModal" class="modal-backdrop" @click.self="showTalonModal = false">
      <div class="modal-card modal-wide">
        <div class="panel-header">
          <div>
            <h3>🔒 Local Talon Privacy Scanner & Top-Left (A–K) Cropper</h3>
            <span class="subtext">
              Processed 100% locally via OpenCV. Only the cropped right-hand scan (Fields A to K) will be uploaded to Gemini.
            </span>
          </div>
          <button class="btn-icon" @click="showTalonModal = false">✕</button>
        </div>

        <div class="talon-scanner-grid">
          <!-- Left: Perspective-Flattened Document + Crop Box Overlay -->
          <div class="scanner-col">
            <div class="scanner-col-header">
              <strong>1. Flattened Document & A–K Crop Zone</strong>
              <span v-if="talonDetectedQuad" class="badge badge-match">4-Corner Perspective Warped</span>
              <span v-else class="badge badge-new">Full Frame</span>
              <button class="btn btn-secondary btn-xs" @click="rotateTalon90">↻ Rotate 90°</button>
            </div>

            <div class="crop-stage">
              <img :src="talonWarpedUrl" alt="Flattened Talon" class="stage-img" />
              <div
                class="crop-overlay-box"
                :style="{
                  left: `${talonCrop.x1 * 100}%`,
                  top: `${talonCrop.y1 * 100}%`,
                  width: `${(talonCrop.x2 - talonCrop.x1) * 100}%`,
                  height: `${(talonCrop.y2 - talonCrop.y1) * 100}%`,
                }"
              >
                <span class="crop-tag">Top-Left A–K Zone</span>
              </div>
            </div>

            <!-- Crop Bounds Sliders -->
            <div class="crop-sliders">
              <label>
                Left (X1: {{ Math.round(talonCrop.x1 * 100) }}%)
                <input
                  v-model.number="talonCrop.x1"
                  type="range"
                  min="0"
                  max="0.8"
                  step="0.01"
                  @change="refreshLocalTalonPreview"
                />
              </label>
              <label>
                Right (X2: {{ Math.round(talonCrop.x2 * 100) }}%)
                <input
                  v-model.number="talonCrop.x2"
                  type="range"
                  min="0.2"
                  max="1"
                  step="0.01"
                  @change="refreshLocalTalonPreview"
                />
              </label>
              <label>
                Top (Y1: {{ Math.round(talonCrop.y1 * 100) }}%)
                <input
                  v-model.number="talonCrop.y1"
                  type="range"
                  min="0"
                  max="0.7"
                  step="0.01"
                  @change="refreshLocalTalonPreview"
                />
              </label>
              <label>
                Bottom Cutoff at K (Y2: {{ Math.round(talonCrop.y2 * 100) }}%)
                <input
                  v-model.number="talonCrop.y2"
                  type="range"
                  min="0.25"
                  max="1"
                  step="0.01"
                  @change="refreshLocalTalonPreview"
                />
              </label>
            </div>

            <label class="checkbox-label">
              <input
                v-model="talonCrop.redact_owner_c"
                type="checkbox"
                @change="refreshLocalTalonPreview"
              />
              Also black out Section C (Owner Name & Address band between B and D.1)
            </label>
          </div>

          <!-- Right: Exact Monochrome Scan Image Sent to Google -->
          <div class="scanner-col">
            <div class="scanner-col-header">
              <strong>2. Exact Scanned Patch Sent to Gemini OCR</strong>
              <span class="badge badge-match">300 DPI Monochrome Scan Filter</span>
            </div>
            <div class="crop-stage scan-output-stage">
              <img :src="talonScannedCropUrl" alt="Scanned A-K Patch" class="stage-img" />
            </div>
            <p class="subtext">
              Verify that <strong>A</strong> (Plate), <strong>D.1–D.3</strong> (Make/Model), and
              <strong>E</strong> (17-char VIN) are visible above, and everything below <strong>K</strong> is cut off.
            </p>
            <div class="scanner-actions">
              <button class="btn btn-secondary" @click="showTalonModal = false">Cancel</button>
              <button class="btn btn-success" @click="confirmAndSendTalonToGemini">
                Confirm & Send Only Cropped Scan to Gemini →
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Archive History Modal -->
    <div v-if="showHistoryModal" class="modal-backdrop" @click.self="showHistoryModal = false">
      <div class="modal-card">
        <div class="panel-header">
          <h3>Export Archive (`backend/exports/archive/YYYY-MM/`)</h3>
          <button class="btn-icon" @click="showHistoryModal = false">✕</button>
        </div>
        <table class="parts-table">
          <thead>
            <tr>
              <th>Exported At</th>
              <th>Supplier</th>
              <th>Raw Doc</th>
              <th>WinMentor #</th>
              <th>Invoice Date</th>
              <th>Total Ex-VAT</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="exportHistory.length === 0">
              <td colspan="7" class="empty-cell">
                No exports recorded yet. Export your first invoice to populate `backend/exports/archive/`.
              </td>
            </tr>
            <tr v-for="item in exportHistory" :key="item.doc_key">
              <td class="mono">{{ item.exported_at.replace('T', ' ').slice(0, 19) }}</td>
              <td>{{ item.supplier_name }}</td>
              <td class="mono">{{ item.document_code }}</td>
              <td class="mono bold">{{ item.winmentor_doc_nr }}</td>
              <td class="mono">{{ item.quote_date }}</td>
              <td class="mono">{{ item.total_fara_tva.toFixed(2) }} RON</td>
              <td>
                <button class="btn btn-primary btn-xs" @click="handleRetrigger(item)">
                  1-Click Re-Trigger
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>