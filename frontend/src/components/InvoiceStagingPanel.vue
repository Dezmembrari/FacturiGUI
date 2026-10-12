<script setup lang="ts">
import { ref, computed } from 'vue'
import {
  type Department,
  type Suggestion,
  type VehicleTarget,
  type ExportLogEntry,
  type ExportResponse,
  searchNomenclature,
  canonicalSupplierToken,
  extractWinmentorDocNr,
} from '../api'

export interface StagedRow {
  rowId: string
  quote_code: string
  quote_name: string
  part_code: string
  part_name: string
  canonical_name: string
  status: 'NEW' | 'MATCH' | 'CONFLICT'
  matched_via: 'exact' | 'separators' | 'brand_suffix' | 'exact_name' | 'none'
  stoc: number
  sold: number
  suggestions: Suggestion[]
  um: 'Buc' | 'L'
  quantity: number
  unit_price: number
  destinationKey: string
}

export interface QuoteQueueItem {
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
  previewPages: string[]
  exportResult: ExportResponse | null
}

const props = defineProps<{
  quoteQueue: QuoteQueueItem[]
  activeQueueId: string | null
  currentQuote: QuoteQueueItem
  nextReadyQuote: QuoteQueueItem | null
  duplicateQueueTab: QuoteQueueItem | null
  vehicles: VehicleTarget[]
}>()

const emit = defineEmits<{
  (e: 'select-queue', id: string): void
  (e: 'remove-queue', id: string): void
  (e: 'retry-queue', item: QuoteQueueItem): void
  (e: 'clear-exported'): void
  (e: 'enqueue-files', files: FileList | File[]): void
  (e: 'open-preview', item: QuoteQueueItem): void
  (e: 'export', advanceToNext: boolean): void
  (e: 'copy-text', text: string, label: string): void
}>()

const workshopDepartments: Department[] = ['TINICHIGERIE', 'VOPSITORIE', 'MECANICA']
const isDraggingOver = ref(false)
const bulkDestinationKey = ref('')

// Floating Catalog Search Popover State
const activeSearchRowId = ref<string | null>(null)
const searchQuery = ref('')
const searchResults = ref<Suggestion[]>([])

const queueStats = computed(() => {
  const total = props.quoteQueue.length
  const exported = props.quoteQueue.filter((q) => q.state === 'exported').length
  const pending = props.quoteQueue.filter(
    (q) => q.state === 'queued' || q.state === 'extracting'
  ).length
  return { total, exported, pending }
})

// Tracks all tab IDs that appear 2+ times in the active queue (Yellow highlight)
const queueDuplicateIds = computed<Set<string>>(() => {
  const counts = new Map<string, string[]>()
  for (const q of props.quoteQueue) {
    if (!q.supplierName || !q.documentCode) continue
    const sup = canonicalSupplierToken(q.supplierName)
    const nr = extractWinmentorDocNr(q.documentCode)
    if (!sup || !nr) continue
    const key = `${sup}:${nr}`
    const list = counts.get(key) ?? []
    list.push(q.id)
    counts.set(key, list)
  }
  const dupIds = new Set<string>()
  for (const ids of counts.values()) {
    if (ids.length > 1) {
      for (const id of ids) dupIds.add(id)
    }
  }
  return dupIds
})

function isDbDuplicatePill(q: QuoteQueueItem): boolean {
  return q.state === 'ready' && q.duplicateWarning !== null && !q.allowDuplicateOverride
}

function isQueueDuplicatePill(q: QuoteQueueItem): boolean {
  return q.state === 'ready' && queueDuplicateIds.value.has(q.id)
}

const isSprintSupplier = computed(() =>
  props.currentQuote.supplierName.toUpperCase().includes('SPRINT')
)

const invoiceTotalExVat = computed(() =>
  props.currentQuote.rows.reduce(
    (acc, r) => acc + Math.round(r.quantity * r.unit_price * 100) / 100,
    0
  )
)

const invoiceTotalVat = computed(() =>
  Math.round(invoiceTotalExVat.value * (props.currentQuote.tvaPercent / 100) * 100) / 100
)

const unresolvedConflictsCount = computed(
  () => props.currentQuote.rows.filter((r) => r.status === 'CONFLICT').length
)

function onQuoteFilesSelected(e: Event) {
  const input = e.target as HTMLInputElement
  if (input.files && input.files.length > 0) {
    emit('enqueue-files', input.files)
    input.value = ''
  }
}

function onDropFiles(e: DragEvent) {
  isDraggingOver.value = false
  if (e.dataTransfer?.files && e.dataTransfer.files.length > 0) {
    emit('enqueue-files', e.dataTransfer.files)
  }
}

function assignAllRowsTo(destKey: string) {
  for (const r of props.currentQuote.rows) {
    r.destinationKey = destKey
  }
}

function applySuggestion(row: StagedRow, sug: Suggestion) {
  row.part_code = sug.code
  if (sug.reason !== 'category_noun_code' && sug.reason !== 'embedded_model_code') {
    row.part_name = sug.name
    row.canonical_name = sug.name
    row.stoc = sug.stoc
    row.status = 'MATCH'
    row.matched_via = 'exact'
  }
  activeSearchRowId.value = null
}

function resolveConflict(row: StagedRow, choice: 'canonical' | 'quote') {
  row.part_name = choice === 'canonical' ? row.canonical_name : row.quote_name.toUpperCase()
  row.status = 'MATCH'
}

function splitRow(index: number) {
  const orig = props.currentQuote.rows[index]
  const splitQty = orig.quantity > 1 ? 1 : Number((orig.quantity / 2).toFixed(2))
  orig.quantity = Number((orig.quantity - splitQty).toFixed(4))

  const clone: StagedRow = {
    ...orig,
    rowId: `row_${Date.now()}_split`,
    suggestions: [...orig.suggestions],
    quantity: splitQty,
    destinationKey: '',
  }
  props.currentQuote.rows.splice(index + 1, 0, clone)
}

function removeRow(index: number) {
  props.currentQuote.rows.splice(index, 1)
}

function addBlankRow() {
  props.currentQuote.rows.push({
    rowId: `row_${Date.now()}`,
    quote_code: '',
    quote_name: '',
    part_code: '',
    part_name: '',
    canonical_name: '',
    status: 'NEW',
    matched_via: 'none',
    stoc: 0,
    sold: 0,
    suggestions: [],
    um: 'Buc',
    quantity: 1,
    unit_price: 0,
    destinationKey: '',
  })
}

async function openRowSearch(row: StagedRow) {
  if (activeSearchRowId.value === row.rowId) {
    activeSearchRowId.value = null
    return
  }
  activeSearchRowId.value = row.rowId
  searchQuery.value = row.part_code || row.part_name.split(' ')[0] || ''
  await runLiveSearch()
}

async function runLiveSearch() {
  if (searchQuery.value.trim().length < 1) {
    searchResults.value = []
    return
  }
  try {
    searchResults.value = await searchNomenclature(searchQuery.value.trim())
  } catch {
    searchResults.value = []
  }
}
</script>

<template>
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
          v-if="currentQuote.previewPages && currentQuote.previewPages.length > 0"
          class="btn btn-secondary btn-sm"
          @click="emit('open-preview', currentQuote)"
        >
          🔍 Preview Invoice (Zoom)
        </button>
        <button
          v-if="queueStats.exported > 0"
          class="btn btn-secondary btn-sm"
          @click="emit('clear-exported')"
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
          Invoice Queue ({{ queueStats.exported }}/{{ queueStats.total }} exported<span
            v-if="queueStats.pending > 0"
            >, {{ queueStats.pending }} extracting in background...</span
          >)
        </span>
        <div class="queue-legend">
          <span class="legend-dot legend-red"></span> Already Exported in DB
          <span class="legend-dot legend-yellow"></span> Duplicate in Queue
        </div>
      </div>
      <div class="queue-pills">
        <div
          v-for="(q, idx) in quoteQueue"
          :key="q.id"
          class="queue-pill"
          :class="[
            `pill-${q.state}`,
            {
              'pill-db-duplicate': isDbDuplicatePill(q),
              'pill-queue-duplicate': !isDbDuplicatePill(q) && isQueueDuplicatePill(q),
              'pill-active': q.id === activeQueueId,
            },
          ]"
          :title="
            isDbDuplicatePill(q)
              ? 'Already exported in DB memory'
              : isQueueDuplicatePill(q)
                ? 'Duplicate tab in active queue'
                : q.fileName
          "
          @click="emit('select-queue', q.id)"
        >
          <span class="pill-index">{{ idx + 1 }}.</span>
          <span v-if="q.state === 'queued'" title="Waiting in queue">⏳</span>
          <span v-else-if="q.state === 'extracting'" title="Extracting via Gemini OCR">🔄</span>
          <span v-else-if="isDbDuplicatePill(q)" title="Already exported in DB">🛑</span>
          <span v-else-if="isQueueDuplicatePill(q)" title="Duplicate in queue">⚠️</span>
          <span v-else-if="q.state === 'ready'" title="Ready to review">📋</span>
          <span v-else-if="q.state === 'exported'" title="Exported">✅</span>
          <span v-else-if="q.state === 'error'" title="Extraction failed">⚠️</span>

          <span class="pill-label">
            <template v-if="q.supplierName">
              {{ q.supplierName.slice(0, 14) }} #{{ extractWinmentorDocNr(q.documentCode) }}
            </template>
            <template v-else>
              {{ q.fileName }}
            </template>
          </span>

          <button
            class="pill-close"
            title="Remove from queue"
            @click.stop="emit('remove-queue', q.id)"
          >
            ×
          </button>
        </div>
      </div>
    </div>

    <!-- Queue Item Extracting or Error State -->
    <div
      v-if="currentQuote.state === 'extracting' || currentQuote.state === 'queued'"
      class="queue-loading-box"
    >
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
      <button class="btn btn-primary btn-sm" @click="emit('retry-queue', currentQuote)">
        Retry Extraction
      </button>
    </div>

    <!-- Ready / Exported Invoice Workspace -->
    <template v-else>
      <!-- Duplicate in Active Queue Warning -->
      <div v-if="duplicateQueueTab" class="alert-box queue-dup-box">
        <div>
          <strong>⚠️ Duplicate Invoice in Active Queue:</strong>
          This invoice ({{ currentQuote.supplierName }}
          <code>#{{ extractWinmentorDocNr(currentQuote.documentCode) }}</code>) matches queued tab
          <strong>{{ duplicateQueueTab.fileName }}</strong> ({{
            duplicateQueueTab.supplierName
          }}).
        </div>
        <button
          class="btn btn-secondary btn-sm"
          @click="emit('remove-queue', currentQuote.id)"
        >
          Remove Duplicate Tab
        </button>
      </div>

      <div v-if="currentQuote.validationIssues.length" class="alert-box">
        <strong>Validation Warnings:</strong>
        <div v-for="(iss, i) in currentQuote.validationIssues" :key="i">• {{ iss }}</div>
      </div>

      <!-- Clean 5-Field Invoice Header -->
      <div class="invoice-meta-grid-5">
        <div class="field">
          <label>Furnizor (Supplier)</label>
          <input v-model="currentQuote.supplierName" placeholder="e.g. AUTONET IMPORT SRL" />
        </div>
        <div class="field">
          <label>WinMentor <code>Numar</code> (Last 6 Digits)</label>
          <div class="copy-input-group">
            <input
              v-model="currentQuote.documentCode"
              class="mono bold"
              placeholder="e.g. 394623 or 001348"
            />
            <button
              class="btn btn-secondary btn-xs"
              @click="emit('copy-text', extractWinmentorDocNr(currentQuote.documentCode), 'Numar Factura')"
            >
              Copy
            </button>
          </div>
        </div>
        <div class="field">
          <label>Invoice Date</label>
          <input v-model="currentQuote.quoteDate" type="date" />
        </div>
        <div class="field">
          <label>Gestiune</label>
          <input v-model="currentQuote.gestiune" />
        </div>
        <div class="field">
          <label>Bulk Assign All Rows</label>
          <div class="copy-input-group">
            <select v-model="bulkDestinationKey">
              <option value="">📦 MAGAZIE (Stock)</option>
              <optgroup label="🏭 Department (Consumable)">
                <option v-for="d in workshopDepartments" :key="d" :value="`dept:${d}`">
                  🏭 {{ d }}
                </option>
              </optgroup>
              <optgroup v-if="vehicles.length" label="🚗 Specific Vehicle">
                <option v-for="c in vehicles" :key="c.id" :value="`car:${c.id}`">
                  🚗 {{ c.numar_inmatriculare }}
                  {{ c.marca_model ? `(${c.marca_model})` : '' }}
                </option>
              </optgroup>
            </select>
            <button
              class="btn btn-secondary btn-xs"
              @click="assignAllRowsTo(bulkDestinationKey)"
            >
              Apply
            </button>
          </div>
        </div>
      </div>

      <!-- Parts Table (Fits 100% Width + Auto-Growing Article Name) -->
      <div class="table-container">
        <table class="parts-table parts-table-fit">
          <colgroup>
            <col style="width: 3%" />   <!-- # -->
            <col style="width: 8%" />   <!-- Status / Stock -->
            <col style="width: 16%" />  <!-- Cod Extern -->
            <col style="width: 27%" />  <!-- Article Name (Guaranteed 27% width) -->
            <col style="width: 6%" />   <!-- UM -->
            <col style="width: 7%" />   <!-- Qty -->
            <col style="width: 8%" />   <!-- Unit Price -->
            <col style="width: 7%" />   <!-- Total -->
            <col style="width: 11%" />  <!-- Destination -->
            <col style="width: 7%" />   <!-- Actions -->
          </colgroup>
          <thead>
            <tr>
              <th>#</th>
              <th>Status</th>
              <th><code>Cod Extern</code></th>
              <th>Article Name (<code>Denumire</code>)</th>
              <th>UM</th>
              <th>Qty</th>
              <th>Unit Price</th>
              <th class="text-right">Total</th>
              <th>Destination</th>
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
                  <small v-else-if="row.matched_via === 'brand_suffix'">(brand)</small>
                  <small v-else-if="row.matched_via === 'exact_name'">(name)</small>
                </span>
                <div v-if="row.status !== 'NEW'" class="stock-mini-tag">
                  Stock: <strong>{{ row.stoc }}</strong>
                </div>
              </td>

              <!-- Part Code + Floating Overlay Search + Suggestions -->
              <td class="code-cell">
                <div class="copy-input-group">
                  <input
                    v-model="row.part_code"
                    class="mono input-code"
                    :class="{
                      'input-warn-highlight':
                        (isSprintSupplier && row.status === 'NEW') || !row.part_code.trim(),
                    }"
                  />
                  <button
                    class="btn-icon"
                    title="Copy Code"
                    @click="emit('copy-text', row.part_code, 'Cod extern')"
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
                  v-if="isSprintSupplier && row.status === 'NEW'"
                  class="field-warn-tag"
                >
                  ⚠️ Sprint NEW code:
                </div>

                <div v-if="row.suggestions.length > 0" class="suggestions-list">
                  <button
                    v-for="s in row.suggestions"
                    :key="s.code"
                    class="chip"
                    :title="`${s.name} (${s.reason}, Stock: ${s.stoc})`"
                    @click="applySuggestion(row, s)"
                  >
                    Use <strong>{{ s.code }}</strong>
                    <small v-if="s.reason.includes('name') || s.reason === 'same_product_type'">
                      ({{ s.name.slice(0, 14) }})
                    </small>
                  </button>
                </div>

                <!-- Floating Overlay Search Dropdown -->
                <div v-if="activeSearchRowId === row.rowId" class="search-floating-popover">
                  <div class="search-bar">
                    <input
                      v-model="searchQuery"
                      placeholder="Search code or whole word..."
                      autofocus
                      @input="runLiveSearch"
                    />
                    <button class="btn-icon" @click="activeSearchRowId = null">✕</button>
                  </div>
                  <div class="search-results-scroll">
                    <div
                      v-for="hit in searchResults"
                      :key="hit.code"
                      class="search-hit"
                      @click="applySuggestion(row, hit)"
                    >
                      <div class="search-hit-main">
                        <strong class="mono">{{ hit.code }}</strong> — {{ hit.name }}
                      </div>
                      <span class="stock-pill">Stock: {{ hit.stoc }}</span>
                    </div>
                    <div v-if="searchResults.length === 0" class="subtext" style="padding: 0.4rem">
                      No matching articles found
                    </div>
                  </div>
                </div>
              </td>

              <!-- Auto-Growing Article Name (Locked Width, No Manual Resize) -->
              <td class="name-cell">
                <div class="autogrow-name" :data-replicated-value="row.part_name">
                  <textarea
                    v-model="row.part_name"
                    rows="1"
                    class="input-name-locked"
                  />
                </div>
                <div v-if="row.status === 'CONFLICT'" class="conflict-box">
                  <span>
                    WinMentor: <strong>{{ row.canonical_name }}</strong> | Quote:
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
                <select v-model="row.destinationKey" class="input-target">
                  <option value="">📦 MAGAZIE</option>
                  <optgroup label="🏭 Department (Consumable)">
                    <option v-for="d in workshopDepartments" :key="d" :value="`dept:${d}`">
                      🏭 {{ d }}
                    </option>
                  </optgroup>
                  <optgroup v-if="vehicles.length" label="🚗 Specific Vehicle">
                    <option v-for="car in vehicles" :key="car.id" :value="`car:${car.id}`">
                      🚗 {{ car.numar_inmatriculare }}
                      {{ car.marca_model ? `(${car.marca_model})` : '' }}
                    </option>
                  </optgroup>
                </select>
              </td>

              <td>
                <div class="actions-cell">
                  <button
                    class="btn btn-secondary btn-xs"
                    title="Split quantity across cars/departments/stock"
                    @click="splitRow(idx)"
                  >
                    Split
                  </button>
                  <button class="btn-icon" title="Delete row" @click="removeRow(idx)">🗑️</button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- Table Footer & Totals -->
      <div class="invoice-footer">
        <div class="footer-left">
          <button class="btn btn-secondary" @click="addBlankRow">+ Add Line</button>
          <div class="totals-pills">
            <span>
              Total Ex-VAT: <strong>{{ invoiceTotalExVat.toFixed(2) }} RON</strong>
            </span>
            <span>
              VAT ({{ currentQuote.tvaPercent }}%):
              <strong>{{ invoiceTotalVat.toFixed(2) }} RON</strong>
            </span>
            <span>
              Total Gross:
              <strong>{{ (invoiceTotalExVat + invoiceTotalVat).toFixed(2) }} RON</strong>
            </span>
          </div>
        </div>

        <div class="footer-actions">
          <button
            class="btn btn-success"
            :disabled="currentQuote.rows.length === 0"
            @click="emit('export', false)"
          >
            Export Bundle & Save
            <span v-if="unresolvedConflictsCount > 0">
              ({{ unresolvedConflictsCount }} conflicts)
            </span>
          </button>

          <button
            v-if="nextReadyQuote"
            class="btn btn-primary"
            :disabled="currentQuote.rows.length === 0"
            @click="emit('export', true)"
          >
            Export & Next in Queue →
          </button>
        </div>
      </div>

      <!-- Post-Export Summary Card -->
      <div v-if="currentQuote.exportResult" class="export-summary-card">
        <h3>
          ✅ Export Complete — WinMentor Factura #{{
            currentQuote.exportResult.winmentor_doc_nr
          }}
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
</template>