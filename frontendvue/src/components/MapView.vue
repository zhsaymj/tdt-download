<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { createMapController } from '../composables/useMap'
import { mapController } from '../composables/mapController'
import { useDrawStore } from '../stores/draw'
import { useBasemapStore } from '../stores/basemap'
import { useMapInfoStore } from '../stores/mapInfo'
import { api } from '../api'

const drawStore = useDrawStore()
const basemapStore = useBasemapStore()
const mapInfo = useMapInfoStore()
const mapEl = ref(null)

let controller = null

onMounted(async () => {
  controller = createMapController(mapEl.value, {
    onRangeChange: ({ bbox, geometry, shape }) => {
      if (!bbox) { drawStore.clear(); return }
      drawStore.setRange({ bbox, geometry, shape })
    },
    onEditingChange: (v) => drawStore.setEditing(v),
    // 实时视口信息(层级/比例尺/鼠标经纬度)写进 mapInfo store,由底部状态条
    // 统一显示 —— 不再在地图上单独浮一条 .map-info。
    onInfo: ({ zoom, scale, lon, lat }) => {
      mapInfo.set({ zoom, scale, lon, lat })
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
</style>
