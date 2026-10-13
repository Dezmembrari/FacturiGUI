<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import {
  type Department,
  type VehicleTarget,
  type AllocationPayload,
  uploadQuoteFile,
  preprocessVehiclePreview,
  uploadVehicleFile,
  seedExcelFile,
  submitExport,
  extractWinmentorDocNr,
  canonicalSupplierToken,
  triggerLocalAutoFill,
} from './api'

import VehiclesPanel, { type PendingTalonJob } from './components/VehiclesPanel.vue'
import TalonScannerModal, { type TalonCropQueueItem } from './components/TalonScannerModal.vue'
import InvoiceStagingPanel, {
  type QuoteQueueItem,
  type StagedRow,
} from './components/InvoiceStagingPanel.vue'
import BonConsumPanel from './components/BonConsumPanel.vue'
import DocumentViewerPanel from './components/DocumentViewerPanel.vue'
import ModalsManager from './components/ModalsManager.vue'

// --- Active Top Navigation Tab ---
const activeTab = ref<'invoices' | 'bon_consum'>('invoices')

// --- Global UI Banners & Modals ---
const busyMessage = ref<string | null>(null)
const errorMessage = ref<string | null>(null)
const toastMessage = ref<string | null>(null)
const activeModal = ref<'stock' | 'models' | 'history' | null>(null)
const autoFillWinmentor = ref<boolean>(true)

// --- Export Modal State ---
const showDuplicateModal = ref(false)
const pendingExportAdvance = ref(false)
const duplicateModalSupplier = ref('')
const duplicateModalDocNr = ref('')

// --- Active Vehicles & Bulk Talon Cropping/Extraction Queue ---
const vehicles = ref<VehicleTarget[]>([])
const talonCropQueue = ref<TalonCropQueueItem[]>([])
const activeTalonCropIdx = ref(0)
const pendingTalonJobs = ref<PendingTalonJob[]>([])

// --- Bulk Quote Queue ---
const quoteQueue = ref<QuoteQueueItem[]>([])
const activeQueueId = ref<string | null>(null)
const isProcessingQueue = ref(false)

// --- Document Split-Viewer ---
const showDocViewer = ref(false)
const viewerTitle = ref('')
const viewerPages = ref<string[]>([])

const fallbackQuote = ref<QuoteQueueItem>({
  id: 'scratchpad',
  fileName: 'Manual Entry',
  file: null,
  state: 'ready',
  errorMsg: null,
  supplierName: '',
  supplierCif: null, // <-- ADDED
  documentCode: '',
  quoteDate: '',
  tvaPercent: 21,
  gestiune: 'MAGAZIE',
  validationIssues: [],
  duplicateWarning: null,
  allowDuplicateOverride: false,
  rows: [],
  previewPages: [],
  exportResult: null,
})

const currentQuote = computed<QuoteQueueItem>(() => {
  if (!activeQueueId.value) return fallbackQuote.value
  return quoteQueue.value.find((q) => q.id === activeQueueId.value) ?? fallbackQuote.value
})

watch(
  () => currentQuote.value.id,
  () => {
    if (showDocViewer.value && currentQuote.value.previewPages.length > 0) {
      openQuotePreview(currentQuote.value)
    }
  }
)

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

const duplicateQueueTab = computed<QuoteQueueItem | null>(() => {
  const cur = currentQuote.value
  if (!cur.supplierName || !cur.documentCode) return null
  const curSup = canonicalSupplierToken(cur.supplierName)
  const curNr = extractWinmentorDocNr(cur.documentCode)
  if (!curSup) return null

  return (
    quoteQueue.value.find(
      (other) =>
        other.id !== cur.id &&
        other.supplierName &&
        other.documentCode &&
        canonicalSupplierToken(other.supplierName) === curSup &&
        extractWinmentorDocNr(other.documentCode) === curNr
    ) ?? null
  )
})

function showToast(msg: string) {
  toastMessage.value = msg
  setTimeout(() => {
    if (toastMessage.value === msg) toastMessage.value = null
  }, 3400)
}

function copyText(text: string, label: string) {
  navigator.clipboard.writeText(text)
  showToast(`Copied ${label}: ${text}`)
}

// --- Bulk Talon Cropping & Background Extraction ---

async function handleTalonFilesSelected(files: File[]) {
  if (files.length === 0) return
  busyMessage.value = `Preparing ${files.length} Talon scan(s) locally...`
  try {
    for (const file of files) {
      const prev = await preprocessVehiclePreview(file)
      const id = `talon_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`
      talonCropQueue.value.push({
        id,
        fileName: file.name,
        scannedFullUrl: prev.scanned_full_data_url,
        x1: prev.x1,
        y1: prev.y1,
        x2: prev.x2,
        y2: prev.y2,
        rotate_deg: 0,
      })
    }
    activeTalonCropIdx.value = 0
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    busyMessage.value = null
  }
}

function removeTalonCropItem(idx: number) {
  talonCropQueue.value.splice(idx, 1)
  if (activeTalonCropIdx.value >= talonCropQueue.value.length) {
    activeTalonCropIdx.value = Math.max(0, talonCropQueue.value.length - 1)
  }
}

function handleConfirmTalonCrop(payload: {
  item: TalonCropQueueItem
  blob: Blob
  dataUrl: string
}) {
  const idx = talonCropQueue.value.findIndex((t) => t.id === payload.item.id)
  if (idx !== -1) removeTalonCropItem(idx)

  const job: PendingTalonJob = {
    id: payload.item.id,
    fileName: payload.item.fileName,
    status: 'extracting',
    errorMsg: null,
    blob: payload.blob,
  }
  pendingTalonJobs.value.push(job)

  void runTalonOcrJob(job, payload.dataUrl, payload.item.scannedFullUrl)
}

async function runTalonOcrJob(job: PendingTalonJob, cropUrl: string, fullUrl: string) {
  if (!job.blob) return
  job.status = 'extracting'
  job.errorMsg = null
  try {
    const doc = await uploadVehicleFile(job.blob)
    const newCar: VehicleTarget = {
      id: `car_${Date.now()}_${Math.random().toString(36).slice(2, 5)}`,
      numar_inmatriculare: doc.numar_inmatriculare,
      serie_sasiu: doc.serie_sasiu,
      marca_model: doc.marca_model,
      talonScanUrl: cropUrl,
      talonFullUrl: fullUrl,
    }
    vehicles.value.push(newCar)
    pendingTalonJobs.value = pendingTalonJobs.value.filter((j) => j.id !== job.id)
    showToast(`Added vehicle ${newCar.numar_inmatriculare}`)
  } catch (err) {
    job.status = 'error'
    job.errorMsg = err instanceof Error ? err.message : String(err)
  }
}

function handleRetryTalonJob(job: PendingTalonJob) {
  void runTalonOcrJob(job, '', '')
}

function handleRemoveTalonJob(jobId: string) {
  pendingTalonJobs.value = pendingTalonJobs.value.filter((j) => j.id !== jobId)
}

function handleAddManualVehicle(payload: { plate: string; model: string; vin: string }) {
  vehicles.value.push({
    id: `car_${Date.now()}`,
    numar_inmatriculare: payload.plate,
    serie_sasiu: payload.vin,
    marca_model: payload.model,
  })
  showToast(`Added vehicle ${payload.plate}`)
}

function handleRemoveVehicle(id: string) {
  vehicles.value = vehicles.value.filter((v) => v.id !== id)
  const targetKey = `car:${id}`
  for (const q of quoteQueue.value) {
    for (const r of q.rows) {
      if (r.destinationKey === targetKey) r.destinationKey = ''
    }
  }
}

// --- Bulk Quote Queue & Background Worker ---

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
      supplierCif: null, // <-- ADDED
      documentCode: '',
      quoteDate: '',
      tvaPercent: 21,
      gestiune: 'MAGAZIE',
      validationIssues: [],
      duplicateWarning: null,
      allowDuplicateOverride: false,
      rows: [],
      previewPages: [],
      exportResult: null,
    })
  }

  if (!activeQueueId.value || currentQuote.value.state === 'exported') {
    activeQueueId.value = firstNewId
  }

  showToast(`Added ${files.length} invoice(s) to the extraction queue`)
  void processQuoteQueue()
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
        nextItem.supplierCif = data.supplier_cif || null // <-- ADDED CIF ASSIGNMENT
        nextItem.documentCode = data.winmentor_doc_nr || data.document_code
        nextItem.quoteDate = data.quote_date
        nextItem.validationIssues = data.validation_issues
        nextItem.duplicateWarning = data.duplicate_warning
        nextItem.allowDuplicateOverride = false
        nextItem.previewPages = data.preview_pages || []

        const defaultDest =
          vehicles.value.length === 1 ? `car:${vehicles.value[0].id}` : ''
        nextItem.rows = data.parts.map((p, idx) => ({
          rowId: `${nextItem.id}_row_${idx}`,
          quote_code: p.quote_code,
          quote_name: p.quote_name,
          part_code: p.resolved_code,
          part_name: p.canonical_name,
          canonical_name: p.canonical_name,
          status: p.status,
          matched_via: p.matched_via as StagedRow['matched_via'],
          stoc: p.stoc,
          sold: p.sold,
          suggestions: p.suggestions,
          um: 'Buc',
          quantity: p.quantity,
          unit_price: p.unit_price,
          destinationKey: defaultDest,
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

// --- Document Preview Helpers ---

function openQuotePreview(q: QuoteQueueItem) {
  if (!q.previewPages || q.previewPages.length === 0) return
  viewerTitle.value = `${q.supplierName || q.fileName} (#${extractWinmentorDocNr(q.documentCode)})`
  viewerPages.value = q.previewPages
  showDocViewer.value = true
}

function openCarTalonPreview(car: VehicleTarget) {
  const pages = [car.talonScanUrl, car.talonFullUrl].filter((u): u is string => Boolean(u))
  if (pages.length === 0) return
  viewerTitle.value = `Talon: ${car.numar_inmatriculare} (${car.marca_model || 'Vehicle'})`
  viewerPages.value = pages
  showDocViewer.value = true
}

// --- Excel Seeding & Final Export ---

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

function buildRowAllocationPayload(row: StagedRow): AllocationPayload {
  const is_new = row.status === 'NEW'; 
  const key = row.destinationKey;

  if (key.startsWith('dept:')) {
    return {
      part_code: row.part_code,
      part_name: row.part_name,
      quantity: row.quantity,
      unit_price: row.unit_price,
      um: row.um,
      is_new: is_new,
      vehicle: null,
      departament: key.slice(5) as Department,
    }
  }
  if (key.startsWith('car:')) {
    const car = vehicles.value.find((v) => v.id === key.slice(4))
    if (car) {
      return {
        part_code: row.part_code,
        part_name: row.part_name,
        quantity: row.quantity,
        unit_price: row.unit_price,
        um: row.um,
        is_new: is_new,
        vehicle: {
          numar_inmatriculare: car.numar_inmatriculare,
          serie_sasiu: car.serie_sasiu,
          marca_model: car.marca_model,
        },
        departament: null,
      }
    }
  }
  return {
    part_code: row.part_code,
    part_name: row.part_name,
    quantity: row.quantity,
    unit_price: row.unit_price,
    um: row.um,
    is_new: is_new,
    vehicle: null,
    departament: null,
  }
}

async function handleExport(advanceToNext: boolean) {
  const q = currentQuote.value
  errorMessage.value = null
  busyMessage.value = 'Writing WinMentor archive bundle & updating nomenclature.db...'
  try {
    const allocations = q.rows.map((r) => buildRowAllocationPayload(r))
    const res = await submitExport({
      supplier_name: q.supplierName,
      supplier_cif: q.supplierCif, // <-- ADDED CIF TO PAYLOAD
      document_code: q.documentCode,
      quote_date: q.quoteDate,
      tva_percent: q.tvaPercent,
      gestiune: q.gestiune,
      allow_duplicate: q.allowDuplicateOverride,
      allocations,
    })

    // Reset override flag on success
    q.allowDuplicateOverride = false
    showDuplicateModal.value = false

    q.exportResult = res
    q.state = 'exported'
    q.duplicateWarning = null
    showToast(`Exported WinMentor Factura #${res.winmentor_doc_nr}!`)

    if (autoFillWinmentor.value) {
      showToast('Pinging WinMentor Auto-Filler...')
      try {
        await triggerLocalAutoFill(res.export_bundle)
        showToast('WinMentor Auto-Fill Triggered!')
      } catch (err) {
        errorMessage.value = 'Auto-Filler agent not running on this PC. Start winmentor_agent.py.'
      }
    }

    if (advanceToNext && nextReadyQuote.value) {
      activeQueueId.value = nextReadyQuote.value.id
    }
  } catch (err: any) {
    const msg = err instanceof Error ? err.message : String(err)
    // Check if the backend screams "Duplicate" or 409
    if (msg.includes('409') || msg.toLowerCase().includes('duplicate')) {
      duplicateModalSupplier.value = q.supplierName
      duplicateModalDocNr.value = extractWinmentorDocNr(q.documentCode)
      pendingExportAdvance.value = advanceToNext
      showDuplicateModal.value = true
    } else {
      errorMessage.value = msg
    }
  } finally {
    busyMessage.value = null
  }
}

function confirmForceExport() {
  if (currentQuote.value) {
    currentQuote.value.allowDuplicateOverride = true
    handleExport(pendingExportAdvance.value)
  }
}

function cancelForceExport() {
  showDuplicateModal.value = false
  if (currentQuote.value) {
    currentQuote.value.allowDuplicateOverride = false
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
        <button class="btn btn-primary btn-sm" @click="activeModal = 'stock'">
          📦 Stock & Nomenclature
        </button>
        <label class="btn btn-secondary btn-sm">
          🔄 Sync Excel (.xlsx)
          <input type="file" accept=".xlsx,.xls" hidden @change="onSeedExcelSelected" />
        </label>
        <button class="btn btn-secondary btn-sm" @click="activeModal = 'models'">
          🤖 Gemini Models
        </button>
        <button class="btn btn-secondary btn-sm" @click="activeModal = 'history'">
          🗂️ Export Archive
        </button>
      </div>
    </header>

    <!-- Workspace Mode Tabs -->
    <nav class="mode-tabs">
      <button
        class="mode-tab-btn"
        :class="{ 'mode-tab-active': activeTab === 'invoices' }"
        @click="activeTab = 'invoices'"
      >
        📥 1. Recepție Facturi (Facturi Intrare)
      </button>
      <button
        class="mode-tab-btn"
        :class="{ 'mode-tab-active': activeTab === 'bon_consum' }"
        @click="activeTab = 'bon_consum'"
      >
        📤 2. Eliberare Bon de Consum (Direct din Stoc)
      </button>
    </nav>

    <div v-if="busyMessage" class="banner banner-info">{{ busyMessage }}</div>
    <div v-if="errorMessage" class="banner banner-error">
      <span>{{ errorMessage }}</span>
      <button class="btn-close" @click="errorMessage = null">×</button>
    </div>
    <div v-if="toastMessage" class="toast">{{ toastMessage }}</div>

    <div v-if="activeTab === 'invoices'" class="autofill-toggle-bar">
      <label class="checkbox-label" style="color: #1d4ed8;">
        <input v-model="autoFillWinmentor" type="checkbox" />
        🤖 Auto-Type generated exports into WinMentor locally
      </label>
    </div>

    <!-- Modular Workspace -->
    <main class="workspace" :class="{ 'workspace-with-viewer': showDocViewer && activeTab === 'invoices' }">
      <!-- Vehicles panel is always visible on the left for both modes -->
      <VehiclesPanel
        :vehicles="vehicles"
        :pending-jobs="pendingTalonJobs"
        @files-selected="handleTalonFilesSelected"
        @add-manual="handleAddManualVehicle"
        @remove-vehicle="handleRemoveVehicle"
        @preview-talon="openCarTalonPreview"
        @copy-text="copyText"
        @retry-job="handleRetryTalonJob"
        @remove-job="handleRemoveTalonJob"
      />

      <!-- Tab 1: Invoice Staging Panel -->
      <InvoiceStagingPanel
        v-if="activeTab === 'invoices'"
        :quote-queue="quoteQueue"
        :active-queue-id="activeQueueId"
        :current-quote="currentQuote"
        :next-ready-quote="nextReadyQuote"
        :duplicate-queue-tab="duplicateQueueTab"
        :vehicles="vehicles"
        @select-queue="(id) => (activeQueueId = id)"
        @remove-queue="removeQueueItem"
        @retry-queue="retryQueueItem"
        @clear-exported="clearExportedFromQueue"
        @enqueue-files="enqueueQuoteFiles"
        @open-preview="openQuotePreview"
        @export="handleExport"
        @copy-text="copyText"
      />

      <!-- Tab 2: Dedicated Bon de Consum Panel -->
      <BonConsumPanel
        v-else
        :vehicles="vehicles"
        :quote-queue="quoteQueue"
        @toast="showToast"
        @error="(msg) => (errorMessage = msg)"
      />

      <!-- Document Split Viewer (Invoice Mode) -->
      <DocumentViewerPanel
        v-if="showDocViewer && activeTab === 'invoices'"
        :title="viewerTitle"
        :pages="viewerPages"
        @close="showDocViewer = false"
      />
    </main>

    <!-- Fast Bulk Talon Cropping Modal -->
    <TalonScannerModal
      :queue="talonCropQueue"
      :active-index="activeTalonCropIdx"
      @select-index="(idx) => (activeTalonCropIdx = idx)"
      @confirm-crop="handleConfirmTalonCrop"
      @remove-item="removeTalonCropItem"
      @close="talonCropQueue = []"
    />

    <!-- Top-Bar Utility Modals (Stock Browser, Model Stats, Export History) -->
    <ModalsManager
      :active-modal="activeModal"
      @close="activeModal = null"
      @toast="showToast"
      @error="(msg) => (errorMessage = msg)"
    />

    <!-- Stylized Duplicate Warning Modal (Removed Tailwind, matched App styles) -->
    <div v-if="showDuplicateModal" class="modal-overlay">
      <div class="modal-content duplicate-modal">
        <div class="modal-header">
          <h3 style="color: #dc2626; display: flex; align-items: center; gap: 0.5rem; margin: 0;">
            <span style="font-size: 1.5rem;">⚠️</span> Duplicate Detected
          </h3>
        </div>
        <div class="modal-body" style="margin-top: 1rem; color: #374151;">
          <p>
            The invoice from <strong>{{ duplicateModalSupplier }}</strong> 
            (Nr. <code>#{{ duplicateModalDocNr }}</code>) has already been exported to WinMentor. 
          </p>
          <p class="subtext" style="margin-top: 0.75rem;">
            Exporting it again will create duplicate entries in your database and WinMentor. Are you sure you want to proceed?
          </p>
        </div>
        <div class="modal-footer">
          <button
            class="btn btn-secondary"
            @click="cancelForceExport"
          >
            Cancel
          </button>
          <button
            class="btn btn-primary"
            style="background-color: #dc2626; border-color: #b91c1c; color: white;"
            @click="confirmForceExport"
          >
            Force Export
          </button>
        </div>
      </div>
    </div>

  </div>
</template>

<style scoped>
.autofill-toggle-bar {
  background: #e0f2fe;
  padding: 0.5rem 1.5rem;
  border-bottom: 1px solid #bae6fd;
  display: flex;
  justify-content: flex-end;
}

/* Modal Styling to match standard app aesthetics */
.modal-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
}

.modal-content {
  background: white;
  padding: 1.5rem;
  border-radius: 8px;
  box-shadow: 0 10px 25px rgba(0, 0, 0, 0.2);
  max-width: 450px;
  width: 100%;
}

.duplicate-modal {
  border-top: 4px solid #dc2626;
}

.modal-footer {
  margin-top: 1.5rem;
  display: flex;
  justify-content: flex-end;
  gap: 0.75rem;
}
</style>