<script setup>
import { ref, computed, watch } from 'vue'
import { useTaskStore, STATUS_TEXT } from '../stores/task'
import { mapController } from '../composables/mapController'
import { fmtSize } from '../utils/format'
import { isBuildingProvider } from '../utils/provider'
import { api } from '../api'

const taskStore = useTaskStore()
const showRange = ref(true)

const t = computed(() => taskStore.activeTask)

// 成果大小功能暂时隐藏(保留后端端点与前端骨架备用):不再主动请求,避免无谓扫盘。
const sizeInfo = ref(null)
const sizeLoading = ref(false)

const FORMAT_TEXT = {
  geotiff: 'GeoTIFF', tms: 'TMS', osm: 'OSM', tiles: '原始瓦片',
  hillshade: '晕渲图', terrain: 'Cesium 地形', b3dm: '3D Tiles(b3dm)',
}
const PROVIDER_TEXT = {
  tianditu_img: '天地图影像', tianditu_vec: '天地图矢量底图', tianditu_ter: '天地图地形晕渲',
  esri_terrain: '全国地形 DEM(Esri Terrain3D)',
  overture_buildings: '三维建筑白模(Overture)',
  osm_buildings: '三维建筑白模(OSM/Overpass)',
  local_vector: '三维建筑白模(本地矢量面)',
}
// 本地矢量的高度来源模式
const HEIGHT_MODE_TEXT = {
  meters: '字段(米)', floors: '字段(层数)', none: '不用字段',
}
// 三维建筑底面高模式说明
const BASE_MODE_TEXT = {
  terrain: '采样地形(逐栋贴地)', flat: '固定为 0(椭球面)', offset: '统一偏移',
}
// 是否三维建筑任务:参数含义与栅格不同,详情面板分开展示
const isBuildings = (t) => isBuildingProvider(t?.provider)

// 导出格式:兼容旧值 both/geotiff+tms 与新的逗号分隔多值
function fmtExport(v) {
  if (!v) return '—'
  const s = String(v).toLowerCase()
  const parts = s === 'both' ? ['geotiff', 'tms'] : s.replace(/\+/g, ',').split(',')
  return parts.map((p) => FORMAT_TEXT[p.trim()] || p.trim()).filter(Boolean).join(' + ')
}

// 级别列表:连续则显示区间,否则逗号列出
function fmtLevels(t) {
  const lv = (t.levels && t.levels.length) ? [...t.levels].sort((a, b) => a - b)
    : Array.from({ length: t.z_max - t.z_min + 1 }, (_, i) => t.z_min + i)
  if (!lv.length) return '—'
  const continuous = lv.every((z, i) => i === 0 || z === lv[i - 1] + 1)
  return continuous && lv.length > 1 ? `${lv[0]} - ${lv[lv.length - 1]}` : lv.join(', ')
}

function fmtBbox(b) {
  return `西 ${b[0].toFixed(4)}｜南 ${b[1].toFixed(4)}｜东 ${b[2].toFixed(4)}｜北 ${b[3].toFixed(4)}`
}

// 切换显示范围
watch(showRange, (v) => {
  if (!t.value) return
  if (v) mapController.value?.showPreview(t.value.bbox, t.value.geometry)
  else mapController.value?.clearPreview()
})

// 详情任务变化时,若勾选显示则重画范围
watch(() => taskStore.activeId, () => {
  if (t.value && showRange.value) mapController.value?.showPreview(t.value.bbox, t.value.geometry)
})

function close() {
  taskStore.clearActive()
  mapController.value?.clearPreview()
}
function zoom() { if (t.value) mapController.value?.zoomTo(t.value.bbox) }
</script>

<template>
  <div v-if="t" class="detail">
    <div class="head">
      <span class="title">{{ t.name }}</span>
      <t-button variant="text" shape="square" size="small" @click="close">✕</t-button>
    </div>
    <div class="body">
      <div class="kv"><span class="k">状态</span><span class="v">{{ STATUS_TEXT[t.status] || t.status }}</span></div>
      <div class="kv"><span class="k">数据源</span><span class="v">{{ PROVIDER_TEXT[t.provider] || t.provider }}</span></div>

      <!-- 三维建筑:无级别/瓦片计数,展示建模参数 -->
      <template v-if="isBuildings(t)">
        <div class="kv"><span class="k">导出格式</span><span class="v">{{ fmtExport(t.export) }}</span></div>
        <div class="kv"><span class="k">建筑栋数</span><span class="v">{{ t.building_count || '待取数确定' }}</span></div>
        <div class="kv"><span class="k">底面高</span><span class="v">{{ BASE_MODE_TEXT[t.base_height_mode] || t.base_height_mode }}</span></div>
        <div class="kv" v-if="t.dem_upload_id">
          <span class="k">参考地形</span><span class="v">上传地形(未覆盖处回落在线)</span>
        </div>
        <div class="kv" v-if="t.height_offset"><span class="k">高度偏移</span><span class="v">{{ t.height_offset }} m</span></div>
        <div class="kv"><span class="k">兜底建筑高</span><span class="v">{{ t.default_height }} m</span></div>
        <div class="kv"><span class="k">单瓦片上限</span><span class="v">{{ t.max_per_tile }} 栋</span></div>
        <!-- 本地矢量面:展示字段映射 -->
        <template v-if="t.provider === 'local_vector'">
          <div class="kv"><span class="k">高度来源</span><span class="v">
            {{ HEIGHT_MODE_TEXT[t.height_mode] || t.height_mode }}
            <template v-if="t.height_mode !== 'none' && t.height_field">
              · {{ t.height_field }}
            </template>
          </span></div>
          <div class="kv" v-if="t.height_mode !== 'none' && t.height_scale !== 1">
            <span class="k">换算系数</span><span class="v">× {{ t.height_scale }}</span>
          </div>
          <div class="kv" v-if="t.height_mode === 'floors'">
            <span class="k">单层层高</span><span class="v">{{ t.floor_height }} m</span>
          </div>
          <div class="kv" v-if="t.name_field">
            <span class="k">名称字段</span><span class="v">{{ t.name_field }}</span>
          </div>
          <div class="kv" v-if="t.keep_fields && t.keep_fields.length">
            <span class="k">保留字段</span><span class="v">{{ t.keep_fields.join('、') }}</span>
          </div>
        </template>
      </template>

      <!-- 栅格(影像/DEM) -->
      <template v-else>
        <div class="kv"><span class="k">级别</span><span class="v">{{ fmtLevels(t) }}</span></div>
        <div class="kv"><span class="k">导出格式</span><span class="v">{{ fmtExport(t.export) }}</span></div>
        <div class="kv"><span class="k">坐标系</span><span class="v">{{ t.crs || 'EPSG:4326' }}</span></div>
        <div class="kv" v-if="t.provider !== 'esri_terrain'"><span class="k">裁剪</span><span class="v">{{ t.clip ? '是' : '否' }}</span></div>
        <div class="kv" v-if="t.provider !== 'esri_terrain'"><span class="k">路网注记</span><span class="v">{{ t.annotate ? '已叠加' : '否' }}</span></div>
        <div class="kv"><span class="k">瓦片</span><span class="v">{{ t.downloaded }}/{{ t.total }}<span v-if="t.failed"> (失败{{ t.failed }})</span></span></div>
        <div class="kv" v-if="t.est_bytes"><span class="k">预估下载</span><span class="v">~{{ fmtSize(t.est_bytes) }} <span class="est-note">(仅原始瓦片)</span></span></div>
      </template>
      <div class="kv"><span class="k">范围</span><span class="v">{{ fmtBbox(t.bbox) }}</span></div>
      <div class="kv" v-if="t.output_path"><span class="k">导出目录</span><span class="v path">{{ t.output_path }}</span></div>
    </div>

    <!-- 导出成果大小(按格式分类 + 合计) -->
    <div v-if="sizeInfo && sizeInfo.items && sizeInfo.items.length" class="sizes">
      <div class="sizes-title">成果大小</div>
      <div v-for="it in sizeInfo.items" :key="it.key" class="size-row">
        <span class="sk">{{ it.label }}</span>
        <span class="sv">{{ fmtSize(it.bytes) }}</span>
      </div>
      <div class="size-row total">
        <span class="sk">合计</span>
        <span class="sv">{{ fmtSize(sizeInfo.total_bytes) }}</span>
      </div>
    </div>
    <div v-else-if="sizeLoading" class="sizes-empty">成果大小统计中…</div>
    <t-checkbox v-model="showRange" class="toggle">在地图上显示下载范围</t-checkbox>
    <t-button variant="outline" block size="small" @click="zoom">缩放到范围</t-button>
  </div>
</template>

<style scoped>
.detail {
  position: absolute; top: 12px; right: 12px; z-index: 20; width: 290px;
  background: #fff; border: 1px solid #e2e8f0; border-radius: 10px;
  box-shadow: 0 6px 20px rgba(14,165,233,.14); padding: 14px; font-size: 13px;
}
.head { display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 10px; padding-bottom: 8px; border-bottom: 1px solid #eef2f7; }
.title { font-weight: 700; color: #0369a1; font-size: 14px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.body { color: #334155; }
.kv { display: flex; justify-content: space-between; gap: 8px; line-height: 1.9; }
.kv .k { color: #94a3b8; flex: 0 0 auto; }
.kv .v { color: #334155; text-align: right; word-break: break-all; }
.kv .v.path { font-size: 11px; }
.kv .v .est-note { color: #94a3b8; font-weight: 400; }
.toggle { margin: 12px 0 8px; }
</style>
