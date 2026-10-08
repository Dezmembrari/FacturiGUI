<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'

export interface TalonCropQueueItem {
  id: string
  fileName: string
  scannedFullUrl: string
  x1: number
  y1: number
  x2: number
  y2: number
  rotate_deg: number
}

const props = defineProps<{
  queue: TalonCropQueueItem[]
  activeIndex: number
}>()

const emit = defineEmits<{
  (e: 'select-index', idx: number): void
  (e: 'confirm-crop', payload: { item: TalonCropQueueItem; blob: Blob; dataUrl: string }): void
  (e: 'remove-item', idx: number): void
  (e: 'close'): void
}>()

const leftCanvasRef = ref<HTMLCanvasElement | null>(null)
const rightCanvasRef = ref<HTMLCanvasElement | null>(null)
const stageWrapRef = ref<HTMLDivElement | null>(null)

let loadedImg: HTMLImageElement | null = null
const isDraggingBox = ref(false)
let dragStartX = 0
let dragStartY = 0

function currentItem(): TalonCropQueueItem | undefined {
  return props.queue[props.activeIndex]
}

function loadHtmlImage(dataUrl: string): Promise<HTMLImageElement | null> {
  return new Promise((resolve) => {
    if (!dataUrl) {
      resolve(null)
      return
    }
    const img = new Image()
    img.onload = () => resolve(img)
    img.onerror = () => resolve(null)
    img.src = dataUrl
  })
}

function drawRotatedToCanvas(
  sourceImg: HTMLImageElement,
  deg: number,
  targetCanvas: HTMLCanvasElement
) {
  const rot = ((deg % 360) + 360) % 360
  const sw = sourceImg.naturalWidth
  const sh = sourceImg.naturalHeight
  const dw = rot === 90 || rot === 270 ? sh : sw
  const dh = rot === 90 || rot === 270 ? sw : sh

  targetCanvas.width = dw
  targetCanvas.height = dh
  const ctx = targetCanvas.getContext('2d')
  if (!ctx) return

  ctx.save()
  ctx.translate(dw / 2, dh / 2)
  ctx.rotate((rot * Math.PI) / 180)
  ctx.drawImage(sourceImg, -sw / 2, -sh / 2)
  ctx.restore()
}

function renderCanvases() {
  const item = currentItem()
  const leftCanvas = leftCanvasRef.value
  const rightCanvas = rightCanvasRef.value
  if (!item || !leftCanvas || !rightCanvas || !loadedImg || loadedImg.naturalWidth === 0) return

  drawRotatedToCanvas(loadedImg, item.rotate_deg, leftCanvas)

  const iw = leftCanvas.width
  const ih = leftCanvas.height
  const x1 = Math.max(0, Math.min(iw - 4, Math.round(Math.min(item.x1, item.x2) * iw)))
  const x2 = Math.max(x1 + 4, Math.min(iw, Math.round(Math.max(item.x1, item.x2) * iw)))
  const y1 = Math.max(0, Math.min(ih - 4, Math.round(Math.min(item.y1, item.y2) * ih)))
  const y2 = Math.max(y1 + 4, Math.min(ih, Math.round(Math.max(item.y1, item.y2) * ih)))

  const cw = x2 - x1
  const ch = y2 - y1
  rightCanvas.width = cw
  rightCanvas.height = ch

  const ctx = rightCanvas.getContext('2d')
  if (!ctx) return
  ctx.drawImage(leftCanvas, x1, y1, cw, ch, 0, 0, cw, ch)
}

watch(
  () => [props.activeIndex, props.queue.length],
  async () => {
    const item = currentItem()
    if (!item) return
    loadedImg = await loadHtmlImage(item.scannedFullUrl)
    await nextTick()
    renderCanvases()
  },
  { immediate: true }
)

function rotate90() {
  const item = currentItem()
  if (!item) return
  item.rotate_deg = (item.rotate_deg + 90) % 360
  renderCanvases()
}

// Allow clicking and dragging directly on the Left Stage image to draw the A–K crop box
function onStageMouseDown(e: MouseEvent) {
  const item = currentItem()
  const wrap = stageWrapRef.value
  if (!item || !wrap) return
  const rect = wrap.getBoundingClientRect()
  if (rect.width <= 0 || rect.height <= 0) return

  isDraggingBox.value = true
  dragStartX = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
  dragStartY = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height))
}

function onStageMouseMove(e: MouseEvent) {
  if (!isDraggingBox.value) return
  const item = currentItem()
  const wrap = stageWrapRef.value
  if (!item || !wrap) return
  const rect = wrap.getBoundingClientRect()
  if (rect.width <= 0 || rect.height <= 0) return

  const curX = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
  const curY = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height))

  if (Math.abs(curX - dragStartX) > 0.03 && Math.abs(curY - dragStartY) > 0.03) {
    item.x1 = Number(Math.min(dragStartX, curX).toFixed(2))
    item.x2 = Number(Math.max(dragStartX, curX).toFixed(2))
    item.y1 = Number(Math.min(dragStartY, curY).toFixed(2))
    item.y2 = Number(Math.max(dragStartY, curY).toFixed(2))
    renderCanvases()
  }
}

function onStageMouseUp() {
  isDraggingBox.value = false
}

async function handleConfirm() {
  const item = currentItem()
  const rightCanvas = rightCanvasRef.value
  if (!item || !rightCanvas) return

  const dataUrl = rightCanvas.toDataURL('image/png')
  const blob = await new Promise<Blob>((resolve, reject) => {
    rightCanvas.toBlob((b) => {
      if (b) resolve(b)
      else reject(new Error('Failed to encode cropped canvas to PNG'))
    }, 'image/png')
  })

  emit('confirm-crop', { item, blob, dataUrl })
}
</script>

<template>
  <div v-if="queue.length > 0 && currentItem()" class="modal-backdrop">
    <div class="modal-card modal-wide">
      <div class="panel-header">
        <div>
          <h3>
            🔒 Talon Scanner & Top-Left (A–K) Cropper ({{ activeIndex + 1 }} of {{ queue.length }})
          </h3>
          <span class="subtext">
            300 DPI Monochrome Scan Filter is active. Click & drag on the left image (or use sliders) to frame Fields A to K.
          </span>
        </div>
        <button class="btn-icon" @click="emit('close')">✕</button>
      </div>

      <!-- Bulk Talon Cropping Queue Pills -->
      <div v-if="queue.length > 1" class="queue-bar" style="margin-top: 0.5rem">
        <div class="queue-pills">
          <div
            v-for="(t, idx) in queue"
            :key="t.id"
            class="queue-pill"
            :class="{ 'pill-active': idx === activeIndex }"
            @click="emit('select-index', idx)"
          >
            <span>{{ idx + 1 }}. {{ t.fileName }}</span>
            <button class="pill-close" @click.stop="emit('remove-item', idx)">×</button>
          </div>
        </div>
      </div>

      <div class="talon-scanner-grid">
        <!-- Left: Full Scanned Document + Interactive Draggable A-K Box -->
        <div class="scanner-col">
          <div class="scanner-col-header">
            <strong>1. Frame Top-Left A–K Region (Click & Drag on Image)</strong>
            <button class="btn btn-secondary btn-xs" @click="rotate90">↻ Rotate 90°</button>
          </div>

          <div class="crop-stage">
            <div
              ref="stageWrapRef"
              class="stage-canvas-wrap"
              @mousedown="onStageMouseDown"
              @mousemove="onStageMouseMove"
              @mouseup="onStageMouseUp"
              @mouseleave="onStageMouseUp"
            >
              <canvas ref="leftCanvasRef" class="stage-img" />
              <div
                class="crop-overlay-box"
                :style="{
                  left: `${currentItem()!.x1 * 100}%`,
                  top: `${currentItem()!.y1 * 100}%`,
                  width: `${(currentItem()!.x2 - currentItem()!.x1) * 100}%`,
                  height: `${(currentItem()!.y2 - currentItem()!.y1) * 100}%`,
                }"
              >
                <span class="crop-tag">Top-Left A–K Zone</span>
              </div>
            </div>
          </div>

          <div class="crop-sliders">
            <label>
              Left (X1: {{ Math.round(currentItem()!.x1 * 100) }}%)
              <input
                v-model.number="currentItem()!.x1"
                type="range"
                min="0"
                max="0.85"
                step="0.01"
                @input="renderCanvases"
              />
            </label>
            <label>
              Right (X2: {{ Math.round(currentItem()!.x2 * 100) }}%)
              <input
                v-model.number="currentItem()!.x2"
                type="range"
                min="0.15"
                max="1"
                step="0.01"
                @input="renderCanvases"
              />
            </label>
            <label>
              Top (Y1: {{ Math.round(currentItem()!.y1 * 100) }}%)
              <input
                v-model.number="currentItem()!.y1"
                type="range"
                min="0"
                max="0.75"
                step="0.01"
                @input="renderCanvases"
              />
            </label>
            <label>
              Bottom Cutoff at K (Y2: {{ Math.round(currentItem()!.y2 * 100) }}%)
              <input
                v-model.number="currentItem()!.y2"
                type="range"
                min="0.2"
                max="1"
                step="0.01"
                @input="renderCanvases"
              />
            </label>
          </div>
        </div>

        <!-- Right: Exact Cropped Monochrome Scan Sent to Gemini -->
        <div class="scanner-col">
          <div class="scanner-col-header">
            <strong>2. Exact Scanned Patch Sent to Gemini OCR</strong>
            <span class="badge badge-match">300 DPI Monochrome Scan</span>
          </div>

          <div class="crop-stage scan-output-stage">
            <canvas ref="rightCanvasRef" class="stage-img" />
          </div>

          <p class="subtext">
            Confirm that <strong>A</strong> (Plate), <strong>D.1–D.3</strong> (Make/Model), and
            <strong>E</strong> (17-char VIN) are visible above. Clicking confirm sends this crop to Gemini in the background and advances immediately to the next Talon.
          </p>

          <div class="scanner-actions">
            <button class="btn btn-secondary" @click="emit('close')">Close</button>
            <button class="btn btn-success" @click="handleConfirm">
              <template v-if="queue.length > 1">
                Confirm Crop & Next Talon ({{ queue.length - 1 }} left) →
              </template>
              <template v-else>
                Confirm & Extract in Background →
              </template>
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>