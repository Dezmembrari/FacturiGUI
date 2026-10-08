<script setup lang="ts">
import { ref, watch } from 'vue'

const props = defineProps<{
  title: string
  pages: string[]
}>()

const emit = defineEmits<{
  (e: 'close'): void
}>()

const pageIdx = ref(0)
const zoom = ref(1)
const rotate = ref(0)
const panX = ref(0)
const panY = ref(0)
const isPanning = ref(false)
let panStartX = 0
let panStartY = 0

watch(
  () => props.pages,
  () => {
    pageIdx.value = 0
    resetTransform()
  }
)

function resetTransform() {
  zoom.value = 1
  rotate.value = 0
  panX.value = 0
  panY.value = 0
}

function onWheel(e: WheelEvent) {
  e.preventDefault()
  const delta = e.deltaY < 0 ? 0.18 : -0.18
  zoom.value = Math.min(4.5, Math.max(0.5, Number((zoom.value + delta).toFixed(2))))
}

function startPan(e: MouseEvent) {
  isPanning.value = true
  panStartX = e.clientX - panX.value
  panStartY = e.clientY - panY.value
}

function movePan(e: MouseEvent) {
  if (!isPanning.value) return
  panX.value = e.clientX - panStartX
  panY.value = e.clientY - panStartY
}

function endPan() {
  isPanning.value = false
}
</script>

<template>
  <aside class="panel viewer-panel">
    <div class="panel-header">
      <div>
        <h3>🔍 {{ title }}</h3>
        <span class="subtext">Mouse wheel to Zoom • Click & Drag to Pan</span>
      </div>
      <button class="btn-icon" @click="emit('close')">✕</button>
    </div>

    <div class="viewer-toolbar">
      <button
        class="btn btn-secondary btn-xs"
        @click="zoom = Math.max(0.5, Number((zoom - 0.25).toFixed(2)))"
      >
        − Zoom
      </button>
      <span class="mono bold">{{ Math.round(zoom * 100) }}%</span>
      <button
        class="btn btn-secondary btn-xs"
        @click="zoom = Math.min(4.5, Number((zoom + 0.25).toFixed(2)))"
      >
        + Zoom
      </button>
      <button class="btn btn-secondary btn-xs" @click="rotate = (rotate + 90) % 360">
        ↻ 90°
      </button>
      <button class="btn btn-secondary btn-xs" @click="resetTransform">Reset</button>

      <div v-if="pages.length > 1" class="page-Nav">
        <button
          class="btn btn-secondary btn-xs"
          :disabled="pageIdx === 0"
          @click="pageIdx--"
        >
          ◀
        </button>
        <span class="mono">{{ pageIdx + 1 }}/{{ pages.length }}</span>
        <button
          class="btn btn-secondary btn-xs"
          :disabled="pageIdx >= pages.length - 1"
          @click="pageIdx++"
        >
          ▶
        </button>
      </div>
    </div>

    <div
      class="viewer-viewport"
      @wheel="onWheel"
      @mousedown="startPan"
      @mousemove="movePan"
      @mouseup="endPan"
      @mouseleave="endPan"
    >
      <img
        v-if="pages[pageIdx]"
        :src="pages[pageIdx]"
        draggable="false"
        class="viewer-img"
        :style="{
          transform: `translate(${panX}px, ${panY}px) scale(${zoom}) rotate(${rotate}deg)`,
        }"
      />
    </div>
  </aside>
</template>