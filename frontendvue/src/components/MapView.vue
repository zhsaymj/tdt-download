<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { createMapController } from '../composables/useMap'
import { mapController } from '../composables/mapController'
import { useDrawStore } from '../stores/draw'
import { useBasemapStore } from '../stores/basemap'
import { api } from '../api'

const drawStore = useDrawStore()
const basemapStore = useBasemapStore()
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
  basemapStore.apply()
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

/* OL 缩放控件默认在左上,会和量测面板打架。统一挪到右下、量测面板正上方,
   并随右侧面板宽度避让——OL 生成的 DOM 不带 scoped 属性,只能用 :deep 命中。
   量测面板向上展开,高度不定,故由它把实时高度写进 --measure-h(见 MeasurePanel.vue)。 */
.map-canvas :deep(.ol-zoom) {
  top: auto; left: auto;
  bottom: calc(18px + var(--measure-h, 0px));
  right: calc(10px + var(--pad-right, 0px));
  transition: right .22s ease, bottom .22s ease;
}
.map-info {
  position: absolute; left: 0; bottom: 0; z-index: 10;
  left: 50%;
  transform: translateX(-50%);
  display: flex; gap: 16px; padding: 5px 14px;
  background: rgba(2, 132, 199, 0.82); color: #f0f9ff;
  font-size: 12px; font-family: Consolas, "Courier New", monospace;
  border-top-right-radius: 8px;
}
.map-info span { white-space: nowrap; }
</style>
