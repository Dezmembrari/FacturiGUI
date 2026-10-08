<script setup lang="ts">
import { ref, watch } from 'vue'
import {
  type PartRecord,
  type ModelStat,
  type ExportLogEntry,
  fetchStockList,
  upsertStockPart,
  deleteStockPart,
  fetchModelStats,
  fetchExportHistory,
  retriggerExport,
} from '../api'

const props = defineProps<{
  activeModal: 'stock' | 'models' | 'history' | null
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'toast', msg: string): void
  (e: 'error', msg: string): void
}>()

// Stock Browser State
const stockSearch = ref('')
const stockOnlyPositive = ref(false)
const stockTotalCount = ref(0)
const stockTotalSoldRon = ref(0)
const stockItems = ref<PartRecord[]>([])
const newStockCode = ref('')
const newStockName = ref('')

// Model Stats State
const modelStats = ref<ModelStat[]>([])

// Export History State
const exportHistory = ref<ExportLogEntry[]>([])

watch(
  () => props.activeModal,
  async (modal) => {
    if (modal === 'stock') await loadStockData()
    else if (modal === 'models') await loadModelsData()
    else if (modal === 'history') await loadHistoryData()
  }
)

async function loadStockData() {
  try {
    const res = await fetchStockList(stockSearch.value, stockOnlyPositive.value, 300, 0)
    stockTotalCount.value = res.total_count
    stockTotalSoldRon.value = res.total_sold_ron
    stockItems.value = res.items
  } catch (err) {
    emit('error', err instanceof Error ? err.message : String(err))
  }
}

async function handleSaveStockPart() {
  if (!newStockCode.value.trim() || !newStockName.value.trim()) return
  try {
    await upsertStockPart(newStockCode.value.trim(), newStockName.value.trim())
    newStockCode.value = ''
    newStockName.value = ''
    await loadStockData()
    emit('toast', 'Saved article to nomenclature.db')
  } catch (err) {
    emit('error', err instanceof Error ? err.message : String(err))
  }
}

async function handleDeleteStockPart(code: string) {
  try {
    await deleteStockPart(code)
    await loadStockData()
    emit('toast', `Deleted article ${code}`)
  } catch (err) {
    emit('error', err instanceof Error ? err.message : String(err))
  }
}

async function loadModelsData() {
  try {
    modelStats.value = await fetchModelStats()
  } catch (err) {
    emit('error', err instanceof Error ? err.message : String(err))
  }
}

async function loadHistoryData() {
  try {
    exportHistory.value = await fetchExportHistory()
  } catch (err) {
    emit('error', err instanceof Error ? err.message : String(err))
  }
}

async function handleRetrigger(entry: ExportLogEntry) {
  try {
    await retriggerExport(entry.archive_dir)
    emit(
      'toast',
      `Restored #${entry.winmentor_doc_nr} (${entry.supplier_name}) to active export folder!`
    )
    emit('close')
  } catch (err) {
    emit('error', err instanceof Error ? err.message : String(err))
  }
}
</script>

<template>
  <!-- 1. Stock & Nomenclature Browser Modal -->
  <div v-if="activeModal === 'stock'" class="modal-backdrop" @click.self="emit('close')">
    <div class="modal-card modal-wide">
      <div class="panel-header">
        <div>
          <h3>
            📦 WinMentor Stock & Nomenclature ({{ stockTotalCount }} articles • Total Stock Value:
            {{ stockTotalSoldRon.toFixed(2) }} RON)
          </h3>
          <span class="subtext">
            Synced from WinMentor stock balance (`stoc sept 2026.xlsx`)
          </span>
        </div>
        <button class="btn-icon" @click="emit('close')">✕</button>
      </div>

      <div class="stock-toolbar">
        <input
          v-model="stockSearch"
          placeholder="Search by Nr. (e.g. 165), Code (e.g. MS430, 46673FE), or Article Name..."
          @input="loadStockData"
        />
        <label class="checkbox-label">
          <input
            v-model="stockOnlyPositive"
            type="checkbox"
            @change="loadStockData"
          />
          In Stock Only (Stock &gt; 0)
        </label>
      </div>

      <div class="stock-add-bar">
        <input v-model="newStockCode" placeholder="New External Code" class="mono" />
        <input v-model="newStockName" placeholder="WinMentor Article Name" />
        <button class="btn btn-primary btn-sm" @click="handleSaveStockPart">
          + Save to Nomenclature
        </button>
      </div>

      <div class="table-container stock-table-scroll">
        <table class="parts-table">
          <thead>
            <tr>
              <th>Nr.</th>
              <th>External Code</th>
              <th>Article Name (WinMentor)</th>
              <th class="text-right">Physical Stock</th>
              <th class="text-right">Stock Value (Sold RON)</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in stockItems" :key="item.part_code">
              <td class="mono">{{ item.nr_crt || '—' }}</td>
              <td class="mono bold">{{ item.part_code }}</td>
              <td>{{ item.part_name }}</td>
              <td class="mono text-right" :class="{ bold: item.stoc > 0 }">
                {{ item.stoc }}
              </td>
              <td class="mono text-right">{{ item.sold.toFixed(2) }} RON</td>
              <td>
                <button
                  class="btn-icon"
                  title="Delete from nomenclature"
                  @click="handleDeleteStockPart(item.part_code)"
                >
                  🗑️
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- 2. Gemini Model Telemetry Modal -->
  <div v-else-if="activeModal === 'models'" class="modal-backdrop" @click.self="emit('close')">
    <div class="modal-card">
      <div class="panel-header">
        <div>
          <h3>🤖 Gemini Model Telemetry & Dynamic Priority</h3>
          <span class="subtext">
            Models returning HTTP 429 (quota limit) automatically enter a 120s cooldown while requests route to the next healthy model.
          </span>
        </div>
        <button class="btn-icon" @click="emit('close')">✕</button>
      </div>

      <table class="parts-table">
        <thead>
          <tr>
            <th>Priority</th>
            <th>Gemini Model</th>
            <th>Status</th>
            <th>Successes</th>
            <th>429 (Rate Limit)</th>
            <th>503 (Busy)</th>
            <th>Avg Latency</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(m, idx) in modelStats" :key="m.model_name">
            <td class="mono bold">#{{ idx + 1 }}</td>
            <td class="mono bold">{{ m.model_name }}</td>
            <td>
              <span v-if="m.in_cooldown" class="badge badge-conflict">
                ⏳ Cooldown ({{ m.cooldown_remaining_s }}s)
              </span>
              <span v-else class="badge badge-match">✅ Healthy</span>
            </td>
            <td class="mono bold">{{ m.success_count }}</td>
            <td class="mono">{{ m.rate_limit_429_count }}</td>
            <td class="mono">{{ m.unavailable_503_count }}</td>
            <td class="mono">
              {{ m.avg_latency_ms > 0 ? `${(m.avg_latency_ms / 1000).toFixed(1)}s` : '—' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- 3. Export Archive History Modal -->
  <div v-else-if="activeModal === 'history'" class="modal-backdrop" @click.self="emit('close')">
    <div class="modal-card">
      <div class="panel-header">
        <h3>Export Archive (`backend/exports/archive/YYYY-MM/`)</h3>
        <button class="btn-icon" @click="emit('close')">✕</button>
      </div>
      <table class="parts-table">
        <thead>
          <tr>
            <th>Exported At</th>
            <th>Supplier</th>
            <th>WinMentor #</th>
            <th>Invoice Date</th>
            <th>Total Ex-VAT</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="exportHistory.length === 0">
            <td colspan="6" class="empty-cell">
              No exports recorded yet. Export your first invoice to populate `backend/exports/archive/`.
            </td>
          </tr>
          <tr v-for="item in exportHistory" :key="item.doc_key">
            <td class="mono">{{ item.exported_at.replace('T', ' ').slice(0, 19) }}</td>
            <td>{{ item.supplier_name }}</td>
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
</template>