<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  type VehicleTarget,
  type Department,
  type PartRecord,
  fetchStockList,
  submitStandaloneBonConsum,
} from '../api'
import type { QuoteQueueItem } from './InvoiceStagingPanel.vue'

const props = defineProps<{
  vehicles: VehicleTarget[]
  quoteQueue: QuoteQueueItem[]
}>()

const emit = defineEmits<{
  (e: 'toast', msg: string): void
  (e: 'error', msg: string): void
}>()

const workshopDepartments: Department[] = ['TINICHIGERIE', 'VOPSITORIE', 'MECANICA']

// --- Voucher Target & Header Fields ---
const targetType = ref<'vehicle' | 'dept'>('vehicle')
const selectedVehicleId = ref<string>('')
const selectedDept = ref<Department>('VOPSITORIE')
const bonDate = ref<string>(new Date().toISOString().slice(0, 10))
const bonDocNr = ref<string>('1')
const bonGestiune = ref<string>('MAGAZIE')
const deductFromStock = ref<boolean>(true)
const isExporting = ref<boolean>(false)

// --- Cart Items on this Bon de Consum ---
export interface BonCartItem {
  id: string
  part_code: string
  part_name: string
  um: string
  stoc_disponibil: number
  quantity: number
  unit_price: number
}

const cart = ref<BonCartItem[]>([])

// --- Warehouse Stock Browser State ---
const stockSearch = ref<string>('')
const onlyInStock = ref<boolean>(true)
const stockItems = ref<PartRecord[]>([])
const isLoadingStock = ref<boolean>(false)

const totalBonValoare = computed(() =>
  cart.value.reduce((acc, item) => acc + Math.round(item.quantity * item.unit_price * 100) / 100, 0)
)

const selectedVehicle = computed(() =>
  props.vehicles.find((v) => v.id === selectedVehicleId.value) ?? null
)

const hasOverdrawError = computed(() =>
  cart.value.some((item) => item.quantity > item.stoc_disponibil && item.stoc_disponibil > 0)
)

// Computed key to match staged invoice destinations (e.g., 'car:car_123' or 'dept:VOPSITORIE')
const currentTargetKey = computed(() => {
  if (targetType.value === 'vehicle' && selectedVehicleId.value) {
    return `car:${selectedVehicleId.value}`
  } else if (targetType.value === 'dept' && selectedDept.value) {
    return `dept:${selectedDept.value}`
  }
  return ''
})

// Finds all parts in the Invoice Staging queue that are assigned to the currently selected vehicle
const stagedFromQuotes = computed(() => {
  const items: { code: string; name: string; qty: number; invoice: string; state: string }[] = []
  if (!currentTargetKey.value) return items

  for (const q of props.quoteQueue) {
    for (const r of q.rows) {
      if (r.destinationKey === currentTargetKey.value) {
        items.push({
          code: r.part_code || '---',
          name: r.part_name,
          qty: r.quantity,
          invoice: q.supplierName || q.fileName || 'Factură',
          state: q.state,
        })
      }
    }
  }
  return items
})

onMounted(async () => {
  if (props.vehicles.length > 0) {
    selectedVehicleId.value = props.vehicles[0].id
  }
  await loadStock()
})

async function loadStock() {
  isLoadingStock.value = true
  try {
    const res = await fetchStockList(stockSearch.value, onlyInStock.value, 250, 0)
    stockItems.value = res.items
  } catch (err) {
    emit('error', err instanceof Error ? err.message : String(err))
  } finally {
    isLoadingStock.value = false
  }
}

function addToCart(part: PartRecord) {
  const existing = cart.value.find((c) => c.part_code === part.part_code)
  if (existing) {
    existing.quantity = Number((existing.quantity + 1).toFixed(3))
    return
  }

  // Calculate unit price from remaining sold value
  const estimatedUnitPrice =
    part.stoc > 0 ? Number((part.sold / part.stoc).toFixed(4)) : 0

  cart.value.push({
    id: `item_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
    part_code: part.part_code,
    part_name: part.part_name,
    um: 'Buc',
    stoc_disponibil: part.stoc,
    quantity: 1,
    unit_price: estimatedUnitPrice,
  })
}

function removeFromCart(index: number) {
  cart.value.splice(index, 1)
}

function clearCart() {
  cart.value = []
}

async function handleGenerateBon() {
  if (cart.value.length === 0) return
  isExporting.value = true
  try {
    const payload = {
      date: bonDate.value,
      doc_number: bonDocNr.value.trim() || '1',
      gestiune: bonGestiune.value.trim() || 'MAGAZIE',
      departament: targetType.value === 'dept' ? selectedDept.value : '',
      vehicle:
        targetType.value === 'vehicle' && selectedVehicle.value
          ? {
              numar_inmatriculare: selectedVehicle.value.numar_inmatriculare,
              serie_sasiu: selectedVehicle.value.serie_sasiu,
              marca_model: selectedVehicle.value.marca_model,
              departament: '',
            }
          : null,
      items: cart.value.map((i) => ({
        part_code: i.part_code,
        part_name: i.part_name,
        um: i.um,
        quantity: i.quantity,
        unit_price: i.unit_price,
      })),
      deduct_from_stock: deductFromStock.value,
    }

    const res = await submitStandaloneBonConsum(payload)
    emit(
      'toast',
      `Exported Bon de Consum #${res.doc_number} (${res.lines_written} items, ${res.total_valoare.toFixed(2)} RON)!`
    )
    clearCart()
    // Refresh warehouse stock list to reflect the deducted items
    await loadStock()
  } catch (err) {
    emit('error', err instanceof Error ? err.message : String(err))
  } finally {
    isExporting.value = false
  }
}
</script>

<template>
  <section class="panel bon-consum-panel">
    <!-- Header Controls -->
    <div class="panel-header">
      <div>
        <h2>📤 Eliberare Bon de Consum (Direct din Stoc)</h2>
        <span class="subtext">
          Selectează destinația (vehicul sau departament) și adaugă piesele/consumabilele din magazia existentă.
        </span>
      </div>
    </div>

    <!-- Target Selector & Voucher Details Bar (Fixed Grid Layout) -->
    <div class="bon-header-grid">
      <div class="field">
        <label>Tip Destinație</label>
        <div class="target-type-toggle">
          <button
            class="btn btn-xs"
            :class="targetType === 'vehicle' ? 'btn-primary' : 'btn-secondary'"
            @click="targetType = 'vehicle'"
          >
            🚗 Mașină
          </button>
          <button
            class="btn btn-xs"
            :class="targetType === 'dept' ? 'btn-primary' : 'btn-secondary'"
            @click="targetType = 'dept'"
          >
            🏭 Dep. Atelier
          </button>
        </div>
      </div>

      <!-- Pick Vehicle Target -->
      <div v-if="targetType === 'vehicle'" class="field flex-1">
        <label>Selectează Mașina</label>
        <select v-model="selectedVehicleId">
          <option v-if="vehicles.length === 0" value="">
            (Nicio mașină activă — adaugă una în stânga)
          </option>
          <option v-for="car in vehicles" :key="car.id" :value="car.id">
            🚗 {{ car.numar_inmatriculare }} — {{ car.marca_model || 'Vehicul' }}
          </option>
        </select>
      </div>

      <!-- Pick Department Target -->
      <div v-else class="field flex-1">
        <label>Departament Consumator</label>
        <select v-model="selectedDept">
          <option v-for="d in workshopDepartments" :key="d" :value="d">
            🏭 {{ d }}
          </option>
        </select>
      </div>

      <div class="field">
        <label>Număr Bon (Serie TOP)</label>
        <input v-model="bonDocNr" class="mono bold" placeholder="ex: 1" />
      </div>

      <div class="field">
        <label>Data Bonului</label>
        <input v-model="bonDate" type="date" />
      </div>

      <div class="field">
        <label>Gestiune Sursă</label>
        <input v-model="bonGestiune" />
      </div>
    </div>

    <!-- 2-Column Split: Cart on Left, Stock Browser on Right -->
    <div class="bon-workspace-split">
      <!-- Left Column: The Consumption Voucher Cart -->
      <div class="bon-cart-col">
        <!-- Read-Only Preview of Parts Assigned Directly From Invoices (Express Flow) -->
        <div v-if="stagedFromQuotes.length > 0" class="staged-quotes-box">
          <div class="staged-quotes-title">
            <strong>📦 Piese alocate direct din Facturi (Auto-Bon)</strong>
            <span class="subtext">
              Aceste piese vor genera automat un Bon de Consum la exportul facturii lor de intrare. Nu le adăuga manual mai jos.
            </span>
          </div>
          <table class="parts-table parts-table-split" style="background: #fff; margin-top: 0.5rem; border-radius: 4px;">
            <colgroup>
              <col style="width: 20%" />
              <col style="width: 44%" />
              <col style="width: 9%" />
              <col style="width: 27%" />
            </colgroup>
            <thead>
              <tr>
                <th>Cod</th>
                <th>Denumire</th>
                <th>Cant.</th>
                <th>Sursă (Factură)</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(item, idx) in stagedFromQuotes" :key="idx">
                <td class="mono bold">{{ item.code }}</td>
                <td>{{ item.name }}</td>
                <td class="mono">{{ item.qty }}</td>
                <td>
                  <span class="badge" :class="item.state === 'exported' ? 'badge-match' : 'badge-new'">
                    {{ item.invoice }} ({{ item.state === 'exported' ? 'Exportat' : 'Așteaptă' }})
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="col-title-bar" style="margin-top: 0.5rem;">
          <strong>1. Articole Consumate din Magazie ({{ cart.length }})</strong>
          <button
            v-if="cart.length > 0"
            class="btn btn-secondary btn-xs"
            @click="clearCart"
          >
            Golește Coșul
          </button>
        </div>

        <div v-if="hasOverdrawError" class="alert-box" style="margin-bottom: 0.5rem">
          ⚠️ Unele cantități depășesc stocul fizic disponibil în magazie!
        </div>

        <div class="table-container cart-table-wrap">
          <table class="parts-table parts-table-split">
            <colgroup>
              <col style="width: 4%" />
              <col style="width: 16%" />
              <col style="width: 28%" />
              <col style="width: 10%" />
              <col style="width: 13%" />
              <col style="width: 14%" />
              <col style="width: 10%" />
              <col style="width: 5%" />
            </colgroup>
            <thead>
              <tr>
                <th>#</th>
                <th>Cod</th>
                <th>Denumire Articol</th>
                <th>UM</th>
                <th>Cant.</th>
                <th>Preț Unitar</th>
                <th class="text-right">Valoare</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="cart.length === 0">
                <td colspan="8" class="empty-cell">
                  Coșul este gol. Alege articole din lista din dreapta pentru a le trece pe bon.
                </td>
              </tr>
              <tr v-for="(item, idx) in cart" :key="item.id">
                <td class="mono">{{ idx + 1 }}</td>
                <td class="mono bold">{{ item.part_code }}</td>
                <td>
                  <div class="cart-item-name">{{ item.part_name }}</div>
                  <small class="subtext">Stoc disponibil: {{ item.stoc_disponibil }}</small>
                </td>
                <td>
                  <select v-model="item.um" class="input-um">
                    <option value="Buc">Buc</option>
                    <option value="L">L</option>
                  </select>
                </td>
                <td>
                  <input
                    v-model.number="item.quantity"
                    type="number"
                    step="0.01"
                    min="0.001"
                    class="input-num mono"
                    :class="{ 'input-error-highlight': item.quantity > item.stoc_disponibil && item.stoc_disponibil > 0 }"
                  />
                </td>
                <td>
                  <input
                    v-model.number="item.unit_price"
                    type="number"
                    step="0.0001"
                    class="input-num mono"
                  />
                </td>
                <td class="mono bold text-right">
                  {{ (item.quantity * item.unit_price).toFixed(2) }}
                </td>
                <td>
                  <button class="btn-icon" @click="removeFromCart(idx)">🗑️</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="bon-cart-footer">
          <div class="totals-pills">
            <span>
              Total Valoare Bon: <strong>{{ totalBonValoare.toFixed(2) }} RON</strong>
            </span>
          </div>

          <label class="checkbox-label">
            <input v-model="deductFromStock" type="checkbox" />
            Scade automat cantitățile din stocul magaziei
          </label>

          <button
            class="btn btn-success"
            :disabled="cart.length === 0 || isExporting"
            @click="handleGenerateBon"
          >
            <template v-if="isExporting">Se generează BON_CONS.TXT...</template>
            <template v-else>
              ✅ Emite & Exportă Bon de Consum (BON_CONS.TXT)
            </template>
          </button>
        </div>
      </div>

      <!-- Right Column: Live Stock Browser & Picker -->
      <div class="bon-stock-col">
        <div class="col-title-bar">
          <strong>2. Magazie Stoc (Din Sistem)</strong>
        </div>

        <div class="stock-search-bar">
          <input
            v-model="stockSearch"
            placeholder="Caută piese (ex: LAC, VOPSEA)..."
            @input="loadStock"
          />
          <label class="checkbox-label">
            <input v-model="onlyInStock" type="checkbox" @change="loadStock" />
            Stoc &gt; 0
          </label>
        </div>

        <div class="table-container stock-picker-scroll">
          <table class="parts-table parts-table-split">
            <colgroup>
              <col style="width: 17%" />
              <col style="width: 41%" />
              <col style="width: 12%" />
              <col style="width: 14%" />
              <col style="width: 16%" />
            </colgroup>
            <thead>
              <tr>
                <th>Cod</th>
                <th>Denumire Articol</th>
                <th class="text-right">Stoc</th>
                <th class="text-right">Preț Mediu</th>
                <th>Acțiune</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="isLoadingStock">
                <td colspan="5" class="empty-cell">Se încarcă articolele din stoc...</td>
              </tr>
              <tr v-else-if="stockItems.length === 0">
                <td colspan="5" class="empty-cell">Niciun articol găsit</td>
              </tr>
              <tr v-for="part in stockItems" :key="part.part_code">
                <td class="mono bold">{{ part.part_code }}</td>
                <td>{{ part.part_name }}</td>
                <td class="mono text-right" :class="{ bold: part.stoc > 0 }">
                  {{ part.stoc }}
                </td>
                <td class="mono text-right">
                  {{ part.stoc > 0 ? (part.sold / part.stoc).toFixed(2) : '—' }} RON
                </td>
                <td>
                  <button
                    class="btn btn-primary btn-xs w-full"
                    :disabled="part.stoc <= 0"
                    @click="addToCart(part)"
                  >
                    + Adaugă
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </section>
</template>