<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { createMapController } from '../composables/useMap'
import { mapController } from '../composables/mapController'
import { useDrawStore } from '../stores/draw'
import { api } from '../api'

const drawStore = useDrawStore()
const mapEl = ref(null)
const info = ref({ zoom: '—', scale: '—', lon: null, lat: null })

let controller = null

onMounted(async () => {
  controller = createMapController(mapEl.value, {
    onRangeChange: ({ bbox, geometry, shape }) => {
      if (!bbox) { drawStore.clear(); return }
      drawStore.setRange({ bbox, geometry, shape })
    },
    onEditingChange: (v) => drawStore.setEditing(v),
    onInfo: ({ zoom, scale, lon, lat }) => {
      info.value = { zoom, scale, lon, lat }
    },
  })
  mapController.value = controller

  try {
    const cfg = await api.getConfig()
    controller.setupBasemap(cfg.basemap_token)
  } catch (_) {
    controller.setupBasemap(null)
  }
})

onBeforeUnmount(() => {
  mapController.value = null
})
</script>

<template>
  <div class="map-wrap">
    <div ref="mapEl" class="map-canvas"></div>
    <div class="map-info">
      <span>层级:{{ info.zoom }}</span>
      <span>比例尺:1:{{ typeof info.scale === 'number' ? info.scale.toLocaleString() : info.scale }}</span>
      <span v-if="info.lon != null">
        经纬度:{{ info.lon.toFixed(5) }}, {{ info.lat.toFixed(5) }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.map-wrap { position: relative; width: 100%; height: 100%; }
.map-canvas { width: 100%; height: 100%; }
.map-info {
  position: absolute; left: 0; bottom: 0; z-index: 10;
  display: flex; gap: 16px; padding: 5px 14px;
  background: rgba(2, 132, 199, 0.82); color: #f0f9ff;
  font-size: 12px; font-family: Consolas, "Courier New", monospace;
  border-top-right-radius: 8px;
}
.map-info span { white-space: nowrap; }
</style>
