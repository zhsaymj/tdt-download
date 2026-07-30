<script setup>
// 关于弹窗:工具简介 + 技术栈 + 版本
defineProps({ visible: Boolean })
defineEmits(['update:visible'])

const APP_VERSION = 'v1.0'
const STACK = [
  { label: '后端', text: 'FastAPI · SQLite · GDAL' },
  { label: '前端', text: 'Vue3 · Pinia · OpenLayers · TDesign' },
  { label: '预览', text: 'Cesium · Natural Earth II 离线底图' },
  { label: '数据源', text: '天地图 EPSG:4326 瓦片 · ArcGIS 地形' },
]
</script>

<template>
  <t-dialog
    :visible="visible" header="关于" width="440px"
    :footer="false" @update:visible="$emit('update:visible', $event)"
  >
    <div class="about">
      <div class="hero">
        <span class="logo">🌐</span>
        <div class="hero-text">
          <div class="app-name">天地图下载处理工具</div>
          <div class="app-ver">{{ APP_VERSION }}</div>
        </div>
      </div>
      <p class="desc">
        在网页地图上绘制范围，下载天地图影像/矢量/地形瓦片，自动拼接为带坐标的
        GeoTIFF，并可导出 gdal2tiles geodetic TMS / OSM 瓦片包，支持在线预览。
      </p>
      <div class="stack">
        <div v-for="s in STACK" :key="s.label" class="stack-row">
          <span class="stack-label">{{ s.label }}</span>
          <span class="stack-text">{{ s.text }}</span>
        </div>
      </div>
    </div>
  </t-dialog>
</template>

<style scoped>
.about { color: #334155; }
.hero { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.logo { font-size: 34px; line-height: 1; }
.app-name { font-size: 16px; font-weight: 700; color: #0f172a; }
.app-ver { font-size: 12px; color: #94a3b8; margin-top: 2px; }
.desc { font-size: 13px; line-height: 1.7; color: #475569; margin-bottom: 14px; }
.stack { display: flex; flex-direction: column; gap: 8px;
  background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px 12px; }
.stack-row { display: flex; gap: 10px; font-size: 12px; line-height: 1.5; }
.stack-label { flex: 0 0 48px; color: #0369a1; font-weight: 700; }
.stack-text { flex: 1 1 auto; color: #475569; }
</style>
