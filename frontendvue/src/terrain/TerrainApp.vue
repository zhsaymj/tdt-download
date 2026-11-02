<script setup>
/**
 * DEM → Cesium 地形瓦片工具的独立页面骨架。
 *
 * 地图只用于画框选取在线 DEM 下载范围(本地 DEM 文件无需画框),故不用
 * MapView.vue(它带底图切换/注记等主下载工具才需要的逻辑),改用
 * createMapController 精简接入:只建矩形绘制 + 底图,省去与主应用共享的
 * mapController 单例(本页只有一个地图实例,没有跨组件共享的必要)。
 */
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { createMapController } from '../composables/useMap'
import { useDrawStore } from '../stores/draw'
import { api } from '../api'
import TerrainTaskForm from './TerrainTaskForm.vue'
import TerrainTaskList from './TerrainTaskList.vue'

const drawStore = useDrawStore()
const mapEl = ref(null)
const formVisible = ref(false)
const listVisible = ref(true)
let controller = null

onMounted(async () => {
  controller = createMapController(mapEl.value, {
    onRangeChange: ({ bbox, geometry, shape }) => {
      if (!bbox) { drawStore.clear(); return }
      drawStore.setRange({ bbox, geometry, shape })
    },
  })
  try {
    const cfg = await api.getConfig()
    controller.setupBasemap(cfg.basemap_token)
  } catch (_) {
    controller.setupBasemap(null)
  }
})

onBeforeUnmount(() => {
  controller = null
})

function drawRect() { controller?.startDrawRect() }
function clearRange() { controller?.clearDraw() }

function openForm() { formVisible.value = true }
function onCreated() { listVisible.value = true }
</script>

<template>
  <div class="terrain-app">
    <header class="topbar">
      <div class="brand">
        <span class="logo">⛰️</span>
        <span class="title">DEM 地形切片工具</span>
      </div>
      <nav class="menus">
        <t-button theme="primary" @click="openForm">+ 新建地形任务</t-button>
        <a class="nav-link" href="/download.html">主下载工具</a>
      </nav>
    </header>

    <main class="main">
      <div class="map-wrap">
        <div ref="mapEl" class="map-canvas"></div>

        <div class="draw-tools">
          <t-button size="small" variant="outline" @click="drawRect">画矩形选区</t-button>
          <t-button size="small" variant="outline" :disabled="!drawStore.hasRange" @click="clearRange">
            清除选区
          </t-button>
        </div>

        <div v-if="drawStore.bbox" class="bbox-hint">
          选区:{{ drawStore.bbox.map((v) => v.toFixed(3)).join(', ') }}
        </div>
      </div>

      <aside class="side" :class="{ collapsed: !listVisible }">
        <div class="side-head">
          <span>地形任务列表</span>
          <button class="fold" @click="listVisible = !listVisible">
            {{ listVisible ? '收起' : '展开' }}
          </button>
        </div>
        <TerrainTaskList v-show="listVisible" />
      </aside>
    </main>

    <TerrainTaskForm v-model:visible="formVisible" @created="onCreated" />
  </div>
</template>

<style scoped>
.terrain-app { display: flex; flex-direction: column; height: 100vh; width: 100vw; overflow: hidden; }

/* 深色渐变顶栏,与主工具 AppTopBar 视觉一致 */
.topbar {
  flex: 0 0 auto; height: 48px; display: flex; align-items: center; justify-content: space-between;
  padding: 0 16px; color: #fff;
  background: linear-gradient(90deg, #0f172a, #0369a1);
}
.brand { display: flex; align-items: center; gap: 8px; }
.logo { font-size: 18px; }
.title { font-weight: 700; font-size: 15px; }
.menus { display: flex; align-items: center; gap: 12px; }
.nav-link { color: rgba(255,255,255,.85); font-size: 13px; text-decoration: none; }
.nav-link:hover { color: #fff; text-decoration: underline; }

.main { flex: 1 1 auto; display: flex; min-height: 0; }
.map-wrap { flex: 1 1 auto; position: relative; min-width: 0; }
.map-canvas { width: 100%; height: 100%; }

.draw-tools {
  position: absolute; top: 12px; right: 12px; z-index: 10;
  display: flex; gap: 8px;
  background: rgba(255,255,255,.92); border-radius: 8px; padding: 6px 8px;
  box-shadow: 0 3px 12px rgba(15,23,42,.18);
}
.bbox-hint {
  position: absolute; bottom: 12px; left: 12px; z-index: 10;
  background: rgba(15,23,42,.72); color: #fff; font-size: 12px;
  padding: 4px 10px; border-radius: 6px; font-family: Consolas, monospace;
}

.side {
  flex: 0 0 340px; display: flex; flex-direction: column;
  background: #f8fbff; border-left: 1px solid #e2e8f0;
  transition: flex-basis .2s ease;
}
.side.collapsed { flex-basis: 48px; }
.side-head {
  flex: 0 0 auto; height: 40px; display: flex; align-items: center; justify-content: space-between;
  padding: 0 12px; font-size: 13px; font-weight: 700; color: #0369a1;
  border-bottom: 1px solid #eef2f7; background: #fff;
}
.fold { border: 0; background: transparent; cursor: pointer; color: #64748b; font-size: 12px; }
.fold:hover { color: #0369a1; }
</style>
