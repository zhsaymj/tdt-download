<script setup>
import { ref, computed, watch } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { useTaskStore, STATUS_TEXT } from '../stores/task'
import { mapController } from '../composables/mapController'
import { fmtSize } from '../utils/format'
import { isBuildingProvider, isDemProvider, isModel3dProvider } from '../utils/provider'
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
  tile_3d: '3D Tiles', dsm: 'DSM(点云)', dem: 'DEM(点云)',
}
const PROVIDER_TEXT = {
  tianditu_img: '天地图影像', tianditu_vec: '天地图矢量底图', tianditu_ter: '天地图地形晕渲',
  esri_terrain: '全国地形 DEM(Esri Terrain3D)',
  google_img: 'Google 卫星影像', google_hybrid: 'Google 影像(含路网)',
  google_road: 'Google 路线图', google_terrain: 'Google 地形',
  esri_imagery: 'Esri World Imagery',
  overture_buildings: '三维建筑白模(Overture)',
  osm_buildings: '三维建筑白模(OSM/Overpass)',
  local_vector: '三维建筑白模(本地矢量面)',
  local_osgb: '倾斜模型(本地 OSGB)',
  local_pointcloud: '点云(本地 LAS/LAZ)',
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
// 是否三维数据任务(本地 OSGB/点云):无级别/瓦片计数,bbox 是占位值,同样分开展示
const isModel3d = (t) => isModel3dProvider(t?.provider)

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

// ---- 面板拖拽:整个面板(交互控件除外)都是把手 ----
// 位移记在 dx/dy、用 transform 表达,不动 left —— 原来的 --pad-left 避让与
// left 过渡都保留。Pointer Events + setPointerCapture 是必须的:拖动时指针会
// 划过地图,不捕获的话地图抢走 pointermove,拖到一半就"断流"。
const drag = ref({ x: 0, y: 0 })
const dragging = ref(false)

/** 面板至少保留这么多像素在视口内,避免被拖到完全看不见 */
const KEEP_X = 60
const KEEP_Y = 40

function onDragStart(e) {
  if (e.button !== 0) return                 // 只响应左键
  // 点面板上的交互控件(关闭/打开目录/复选框/链接…)不启动拖拽,
  // 否则点「✕」也会先触发 setPointerCapture,把 click 一起吞掉,按钮失效。
  const target = e.target
  if (target instanceof Element
    && target.closest('button, input, label, a, textarea, select')) return
  const el = e.currentTarget
  const sx = e.clientX
  const sy = e.clientY
  const base = { ...drag.value }
  dragging.value = true
  el.setPointerCapture(e.pointerId)

  const move = (ev) => {
    // el 是 .detail 自身:按面板尺寸限位,保证至少留 KEEP 像素可见、永远拖得回
    const w = el.offsetWidth || 340
    const h = el.offsetHeight || 200
    const nx = base.x + (ev.clientX - sx)
    const ny = base.y + (ev.clientY - sy)
    drag.value = {
      x: Math.min(window.innerWidth - KEEP_X, Math.max(-(w - KEEP_X), nx)),
      y: Math.min(window.innerHeight - KEEP_Y, Math.max(-(h - KEEP_Y), ny)),
    }
  }
  const up = (ev) => {
    dragging.value = false
    if (el.hasPointerCapture(ev.pointerId)) el.releasePointerCapture(ev.pointerId)
    el.removeEventListener('pointermove', move)
    el.removeEventListener('pointerup', up)
    el.removeEventListener('pointercancel', up)
    // 拖到面板顶部(head)完全出视口时自动弹回顶部可见位置,
    // 否则头部连把手带标题一起看不见,想拖回来都找不到抓取点。
    if (drag.value.y < -120) drag.value = { ...drag.value, y: -60 }
  }
  el.addEventListener('pointermove', move)
  el.addEventListener('pointerup', up)
  el.addEventListener('pointercancel', up)
}

const dragStyle = computed(() => ({
  transform: `translate(${drag.value.x}px, ${drag.value.y}px)`,
}))

// 点云坐标系:空串=自动(读 LAS 头),local=本地坐标不转 ECEF,EPSG:xxxx 原样
function pcCrsText(t) {
  const v = String(t.pc_crs || '')
  if (!v) return '自动(读 LAS 头)'
  if (v === 'local') return '本地坐标(不转 ECEF)'
  return v
}
// 点云采样分辨率:>0 显示米数,否则为自动
function pcResolutionText(t) {
  const n = Number(t.pc_resolution)
  return n > 0 ? `${n} m` : '自动'
}

// 切换显示范围(model3d 的 bbox 是占位值,不画)
watch(showRange, (v) => {
  if (!t.value || isModel3d(t.value)) return
  if (v) mapController.value?.showPreview(t.value.bbox, t.value.geometry)
  else mapController.value?.clearPreview()
})

// 详情任务变化时,若勾选显示则重画范围(model3d 同上跳过)
watch(() => taskStore.activeId, () => {
  // 拖拽位置也复位:否则新任务的详情会开在上一个被拖走的地方
  drag.value = { x: 0, y: 0 }
  if (isModel3d(t.value)) return
  if (t.value && showRange.value) mapController.value?.showPreview(t.value.bbox, t.value.geometry)
})

function close() {
  taskStore.clearActive()
  mapController.value?.clearPreview()
}
function zoom() {
  // 全零 bbox 是占位值(三维任务),直飞会落到几内亚湾
  if (!t.value || t.value.bbox?.every((v) => v === 0)) return
  mapController.value?.zoomTo(t.value.bbox)
}
async function openOutputDir() {
  if (!t.value?.output_path) return
  try {
    await api.revealPath(t.value.output_path)
  } catch (e) {
    MessagePlugin.error('打开目录失败:' + (e?.message || e))
  }
}
</script>

<template>
  <div v-if="t" class="detail" :class="{ dragging }" :style="dragStyle" @pointerdown="onDragStart">
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

      <!-- 三维数据(OSGB/点云):无级别/瓦片计数,展示输入源与点云参数 -->
      <template v-else-if="isModel3d(t)">
        <div class="kv"><span class="k">导出格式</span><span class="v">{{ fmtExport(t.export) }}</span></div>
        <div class="kv"><span class="k">源路径</span><span class="v">{{ t.source_path }}</span></div>
        <template v-if="t.provider === 'local_pointcloud'">
          <div class="kv"><span class="k">点云坐标系</span><span class="v">{{ pcCrsText(t) }}</span></div>
          <div class="kv"><span class="k">采样分辨率</span><span class="v">{{ pcResolutionText(t) }}</span></div>
        </template>
      </template>

      <!-- 栅格(影像/DEM) -->
      <!-- 字段按三组排:输出配置 / 处理选项 / 下载量,组间用 .kv-sep 细线分开
           —— 需求40 新增全球底图与缓冲后,平铺一长串不好速读。 -->
      <template v-else>
        <div class="kv"><span class="k">级别</span><span class="v">{{ fmtLevels(t) }}</span></div>
        <div class="kv"><span class="k">导出格式</span><span class="v">{{ fmtExport(t.export) }}</span></div>
        <div class="kv"><span class="k">坐标系</span><span class="v">{{ t.crs || 'EPSG:4326' }}</span></div>
        <div class="kv-sep"></div>
        <template v-if="!isDemProvider(t.provider)">
          <div class="kv"><span class="k">裁剪</span><span class="v">{{ t.clip ? '是' : '否' }}</span></div>
          <div class="kv"><span class="k">路网注记</span><span class="v">{{ t.annotate ? '已叠加' : '否' }}</span></div>
          <div class="kv"><span class="k">全球底图</span><span class="v">{{ t.global_max_level > 0 ? `z1 - z${t.global_max_level}` : '否' }}</span></div>
          <div class="kv" v-if="t.global_max_level > 0"><span class="k">边缘缓冲</span><span class="v">{{ t.buffer_rings ?? 0 }} 圈</span></div>
        </template>
        <div class="kv-sep"></div>
        <div class="kv"><span class="k">瓦片</span><span class="v">{{ t.downloaded }}/{{ t.total }}<span v-if="t.failed"> (失败{{ t.failed }})</span></span></div>
        <div class="kv" v-if="t.est_bytes"><span class="k">预估下载</span><span class="v">~{{ fmtSize(t.est_bytes) }} <span class="est-note">(仅原始瓦片)</span></span></div>
      </template>
      <!-- model3d 的 bbox 是占位 [0,0,0,0],真实范围转换后才有,显示无意义 -->
      <div class="kv"><span class="k">范围</span><span class="v">{{ isModel3d(t) ? '转换完成后以预览为准' : fmtBbox(t.bbox) }}</span></div>
      <div class="kv path-row" v-if="t.output_path">
        <span class="k">导出目录</span>
        <span class="v path">
          <span class="path-text">{{ t.output_path }}</span>
          <t-button size="small" variant="outline" @click="openOutputDir">打开目录</t-button>
        </span>
      </div>
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
    <!-- model3d 没有有效范围(占位 bbox),范围显示与缩放入口一并隐藏 -->
    <t-checkbox v-if="!isModel3d(t)" v-model="showRange" class="toggle">在地图上显示下载范围</t-checkbox>
    <t-button v-if="!isModel3d(t)" variant="outline" block size="small" @click="zoom">缩放到范围</t-button>
  </div>
</template>

<style scoped>
.detail {
  /* 改停左上并随左侧面板避让:新布局的绘制工具条占了右上角,原先的 right:12px
     会与它重叠。--pad-left 由 App.vue 按面板开合下传。
     fixed 相对视口:absolute 会被 .map-main 的 overflow:hidden 裁剪,拖到顶部
     整段消失、拖不回来;fixed 才能盖过头部菜单栏。top 里 +44px 是顶栏高。 */
  position: fixed; top: calc(12px + 44px); left: calc(12px + var(--pad-left, 0px));
  /* z-index 1000:盖过头部菜单栏(AppTopBar 无 z-index)与所有常规面板;
     仍低于 TDesign 弹层(teleport 到 body,1500+)。 */
  z-index: 1000; width: 340px; transition: left .22s ease;
  background: #fff; border: 1px solid #e2e8f0; border-radius: 10px;
  box-shadow: 0 6px 20px rgba(14,165,233,.14); padding: 14px; font-size: 13px;
  cursor: grab; user-select: none; }
.head { display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 10px; padding-bottom: 8px; border-bottom: 1px solid #eef2f7; }
.detail.dragging { transition: none; cursor: grabbing; }   /* 拖拽时关过渡,否则跟手会拖影 */
.title { font-weight: 700; color: #0369a1; font-size: 14px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.body { color: #334155; }
.kv { display: flex; justify-content: space-between; gap: 8px; line-height: 1.9; }
.kv .k { color: #94a3b8; flex: 0 0 auto; }
.kv .v { color: #334155; text-align: right; word-break: break-all; }
.kv.path-row { align-items: flex-start; }
.kv .v.path { font-size: 11px; display: flex; flex-direction: column; align-items: flex-end; gap: 4px; }
.path-text { word-break: break-all; }
.kv .v .est-note { color: #94a3b8; font-weight: 400; }
/* 字段分组细线:输出配置 / 处理选项 / 下载量 */
.kv-sep { height: 1px; background: #eef2f7; margin: 6px 0; }
.toggle { margin: 12px 0 8px; }
</style>
