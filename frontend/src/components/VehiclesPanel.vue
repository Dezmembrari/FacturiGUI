<script setup lang="ts">
import { ref } from 'vue'
import type { VehicleTarget } from '../api'

export interface PendingTalonJob {
  id: string
  fileName: string
  status: 'cropping' | 'extracting' | 'error'
  errorMsg: string | null
  blob: Blob | null
}

defineProps<{
  vehicles: VehicleTarget[]
  pendingJobs: PendingTalonJob[]
}>()

const emit = defineEmits<{
  (e: 'files-selected', files: File[]): void
  (e: 'add-manual', payload: { plate: string; model: string; vin: string }): void
  (e: 'remove-vehicle', id: string): void
  (e: 'preview-talon', car: VehicleTarget): void
  (e: 'copy-text', text: string, label: string): void
  (e: 'retry-job', job: PendingTalonJob): void
  (e: 'remove-job', jobId: string): void
}>()

const newCarPlate = ref('')
const newCarModel = ref('')
const newCarVin = ref('')

function onFilesChange(e: Event) {
  const input = e.target as HTMLInputElement
  if (input.files && input.files.length > 0) {
    emit('files-selected', Array.from(input.files))
    input.value = ''
  }
}

function submitManualCar() {
  if (!newCarPlate.value.trim()) return
  emit('add-manual', {
    plate: newCarPlate.value.trim().toUpperCase(),
    model: newCarModel.value.trim().toUpperCase(),
    vin: newCarVin.value.trim().toUpperCase(),
  })
  newCarPlate.value = ''
  newCarModel.value = ''
  newCarVin.value = ''
}

function formatBonObs(car: VehicleTarget): string {
  const plateClean = car.numar_inmatriculare.toLowerCase().replace(/[^a-z0-9]/g, '')
  const modelClean = car.marca_model.trim().toLowerCase().replace(/\s+/g, ' ')
  if (!plateClean) return modelClean
  return modelClean ? `${modelClean} ${plateClean}` : plateClean
}

// Live per-field validation for Point 2 (highlights the exact broken input box)
const RO_PLATE_RE = /^(B|[A-Z]{2})[\s-]?\d{2,3}[\s-]?[A-Z]{3}$/
const VALID_VIN_RE = /^[A-HJ-NPR-Z0-9]{17}$/

function getPlateError(plate: string): string | null {
  const p = plate.trim().toUpperCase()
  if (!p) return 'Missing Plate'
  if (!RO_PLATE_RE.test(p)) return 'Check Plate format'
  return null
}

function getModelError(model: string): string | null {
  if (!model.trim()) return 'Missing Make/Model (D.1–D.3)'
  return null
}

function getVinError(vin: string): string | null {
  const v = vin.trim().toUpperCase()
  if (!v) return null // Optional on manual entry
  if (v.length !== 17) return `VIN has ${v.length}/17 chars`
  if (!VALID_VIN_RE.test(v)) return 'VIN has invalid chars (I, O, Q)'
  return null
}

function hasAnyVehicleWarning(car: VehicleTarget): boolean {
  return Boolean(
    getPlateError(car.numar_inmatriculare) ||
      getModelError(car.marca_model) ||
      getVinError(car.serie_sasiu)
  )
}
</script>

<template>
  <aside class="panel vehicles-panel">
    <div class="panel-header">
      <h2>1. Active Vehicles</h2>
      <label class="btn btn-primary btn-sm">
        + Scan Talons (Bulk)
        <input type="file" accept=".pdf,image/*" multiple hidden @change="onFilesChange" />
      </label>
    </div>

    <!-- Background Talon Extraction Queue Cards -->
    <div v-if="pendingJobs.length > 0" class="talon-jobs-list">
      <div
        v-for="job in pendingJobs"
        :key="job.id"
        class="talon-job-card"
        :class="{ 'job-error': job.status === 'error' }"
      >
        <div class="job-info">
          <span v-if="job.status === 'cropping'">✂️ Cropping: {{ job.fileName }}</span>
          <span v-else-if="job.status === 'extracting'">🔄 Gemini OCR: {{ job.fileName }}...</span>
          <span v-else>⚠️ Failed: {{ job.fileName }} ({{ job.errorMsg }})</span>
        </div>
        <div class="job-actions">
          <button
            v-if="job.status === 'error' && job.blob"
            class="btn btn-primary btn-xs"
            @click="emit('retry-job', job)"
          >
            Retry
          </button>
          <button class="btn-icon" @click="emit('remove-job', job.id)">✕</button>
        </div>
      </div>
    </div>

    <!-- Manual Car Form -->
    <div class="manual-car-box">
      <input v-model="newCarPlate" placeholder="Plate (e.g. CT-61-MLG)" />
      <input v-model="newCarModel" placeholder="Make/Model (e.g. OPEL CORSA)" />
      <input
        v-model="newCarVin"
        placeholder="VIN (17 chars, optional)"
        class="full-span mono"
      />
      <button class="btn btn-secondary btn-sm full-span" @click="submitManualCar">
        + Add Vehicle Manually
      </button>
    </div>

    <!-- Destinations & Vehicle Cards List -->
    <div class="vehicle-list">
      <div class="vehicle-card stock-card">
        <div>
          <strong>Built-in Destinations (Always Available)</strong>
          <div class="subtext">
            • <strong>📦 MAGAZIE</strong> (General Stock)<br />
            • <strong>🏭 TINICHIGERIE / VOPSITORIE / MECANICA</strong> (General shop consumables)
          </div>
        </div>
      </div>

      <!-- Editable Vehicle Cards with Field-Specific Warning Highlights (Point 2) -->
      <div
        v-for="car in vehicles"
        :key="car.id"
        class="vehicle-card"
        :class="{ 'vehicle-card-warning': hasAnyVehicleWarning(car) }"
      >
        <div class="vehicle-card-top">
          <div class="validated-field-wrap">
            <input
              v-model="car.numar_inmatriculare"
              class="plate-input mono bold"
              :class="{ 'input-error-highlight': getPlateError(car.numar_inmatriculare) }"
              title="Edit license plate"
              placeholder="PLATE"
            />
            <span v-if="getPlateError(car.numar_inmatriculare)" class="field-error-tag">
              ⚠️ {{ getPlateError(car.numar_inmatriculare) }}
            </span>
          </div>

          <div class="validated-field-wrap flex-1">
            <input
              v-model="car.marca_model"
              :class="{ 'input-warn-highlight': getModelError(car.marca_model) }"
              placeholder="Make & Model (e.g. OPEL CORSA)"
            />
            <span v-if="getModelError(car.marca_model)" class="field-warn-tag">
              ⚠️ {{ getModelError(car.marca_model) }}
            </span>
          </div>

          <button
            v-if="car.talonScanUrl"
            class="btn-icon"
            title="Inspect Talon Scan with Zoom"
            @click="emit('preview-talon', car)"
          >
            🔍
          </button>
          <button
            class="btn-icon"
            title="Remove vehicle"
            @click="emit('remove-vehicle', car.id)"
          >
            ✕
          </button>
        </div>

        <div class="car-inputs">
          <div class="validated-field-wrap">
            <input
              v-model="car.serie_sasiu"
              placeholder="VIN (17 characters)"
              class="mono"
              :class="{ 'input-error-highlight': getVinError(car.serie_sasiu) }"
            />
            <span v-if="getVinError(car.serie_sasiu)" class="field-error-tag">
              ⚠️ {{ getVinError(car.serie_sasiu) }}
            </span>
          </div>
        </div>

        <div
          class="obs-preview"
          @click="emit('copy-text', formatBonObs(car), '4.Obs')"
        >
          <span>
            WinMentor <code>4.Obs</code>: <strong>{{ formatBonObs(car) }}</strong>
          </span>
          <span class="copy-hint">Copy</span>
        </div>
      </div>
    </div>
  </aside>
</template>