<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import {
  Viewer, TileMapServiceImageryProvider, UrlTemplateImageryProvider,
  Rectangle, GeographicTilingScheme, WebMercatorTilingScheme, Ion,
  CesiumTerrainProvider, EllipsoidTerrainProvider,
  Cesium3DTileset, Color, Cartesian3,
} from 'cesium'

// 离线自用:不使用任何 Cesium Ion 在线资源
Ion.defaultAccessToken = ''

const loading = ref(true)
const errorMsg = ref('')
const taskName = ref('')
const taskPath = ref('')
const layers = ref([])   // [{ key, label, layer, show }]
const buildingHint = ref('')   // 三维建筑相关提示(底面高与地形不匹配时)

// 全国 30 米地形(在线服务)。与本任务导出的地形切片互斥——
// viewer.terrainProvider 只能挂一个,同时开启没有意义。
const NATIONAL_TERRAIN_URL =
  'https://gisearth-1301434080.cos.ap-nanjing.myqcloud.com/MapData/terrain'

// 互斥的地形图层 key(同时最多一个生效)
const TERRAIN_KEYS = ['terrain', 'terrain-national']

let viewer = null
let taskBbox = null       // [west, south, east, north]
let terrainProvider = null       // 本任务导出的地形提供者
let nationalTerrain = null       // 全国 30m 地形提供者(首次勾选时才加载)
let rangeEntity = null    // 下载范围矩形轮廓 entity
let tileset3d = null      // 三维建筑 b3dm 瓦片集
const flatTerrain = new EllipsoidTerrainProvider()   // 关闭地形时回落的平面椭球
const terrainLoading = ref(false)   // 全国地形加载中(首次勾选需联网)
const terrainError = ref('')        // 全国地形加载失败提示(面板内,不阻断预览)

// 是否同时存在两个地形项(本任务切片 + 全国 30m):此时才需提示互斥关系
const hasBothTerrain = computed(
  () => layers.value.filter((it) => TERRAIN_KEYS.includes(it.key)).length > 1)

// 绘制下载范围:贴地半透明矩形(填充)+ 贴地形折线(边框)。
// Cesium 贴地矩形(ground primitive)不支持 outline,故边框改用 clampToGround 折线,
// 这样范围框会随地形起伏贴合表面,而非悬空平面。已存在则先移除。
function drawRange(bbox) {
  if (!viewer || !bbox || bbox.length !== 4) return
  if (rangeEntity) { viewer.entities.remove(rangeEntity); rangeEntity = null }
  const [w, s, e, n] = bbox
  rangeEntity = viewer.entities.add({
    // 填充:贴地半透明矩形
    rectangle: {
      coordinates: Rectangle.fromDegrees(w, s, e, n),
      material: Color.CYAN.withAlpha(0.10),
    },
    // 边框:闭合折线,贴地形表面
    polyline: {
      positions: Cartesian3.fromDegreesArray([w, s, e, s, e, n, w, n, w, s]),
      width: 2,
      material: Color.CYAN.withAlpha(0.95),
      clampToGround: true,
    },
  })
}

// 相机飞到成果范围。
// 有三维建筑时用倾斜视角(正俯视看不出立体感),否则正俯视看栅格成果更合适。
function flyToBbox() {
  if (!viewer || !taskBbox || taskBbox.length !== 4) return
  const rect = Rectangle.fromDegrees(...taskBbox)
  if (!tileset3d) {
    viewer.camera.flyTo({ destination: rect, duration: 1.2 })
    return
  }
  // 三维建筑:从范围中心南侧、按范围尺度取一个高度,俯角 45° 斜视
  const [w, s, e, n] = taskBbox
  const cx = (w + e) / 2
  const cy = (s + n) / 2
  const spanDeg = Math.max(e - w, n - s)
  const dist = Math.max(spanDeg * 111000 * 1.4, 800)   // 范围越大退得越远
  viewer.camera.flyTo({
    destination: Cartesian3.fromDegrees(cx, cy - spanDeg * 0.6, dist * 0.7),
    orientation: { heading: 0, pitch: -Math.PI / 4, roll: 0 },
    duration: 1.5,
  })
}

// 从绝对输出路径取目录名,拼成 /output/<dir> 静态路径
function outputBase(outputPath) {
  const parts = String(outputPath).split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1]
}

// 解析导出格式(兼容旧 both / geotiff+tms 与逗号分隔)
function parseFormats(v) {
  const s = String(v || '').toLowerCase()
  if (s === 'both') return ['geotiff', 'tms']
  return s.replace(/\+/g, ',').split(',').map((x) => x.trim()).filter(Boolean)
}

// 全国 30m 地形按需加载:默认不勾选,不该在打开预览时就发请求。
async function ensureNationalTerrain() {
  if (nationalTerrain) return nationalTerrain
  terrainLoading.value = true
  try {
    nationalTerrain = await CesiumTerrainProvider.fromUrl(NATIONAL_TERRAIN_URL, {
      // 该服务 layer.json 声明了 octvertexnormals 扩展,请求法线可获得
      // 正确的地形光照/山体阴影(本任务自切的地形没写法线,故那边为 false)
      requestVertexNormals: true,
      requestWaterMask: false,
    })
    return nationalTerrain
  } finally {
    terrainLoading.value = false
  }
}

// 应用地形选择:把除 activeKey 外的地形项全部取消,并挂上对应 provider。
// activeKey 为 null 表示不启用任何地形(回落平面椭球)。
function applyTerrain(activeKey) {
  for (const it of layers.value) {
    if (TERRAIN_KEYS.includes(it.key)) it.show = it.key === activeKey
  }
  if (!viewer) return
  if (activeKey === 'terrain') viewer.terrainProvider = terrainProvider || flatTerrain
  else if (activeKey === 'terrain-national') viewer.terrainProvider = nationalTerrain || flatTerrain
  else viewer.terrainProvider = flatTerrain
}

async function toggle(item) {
  // 地形类:互斥单选。勾选其一即自动取消另一个;再点已选中的则关闭地形。
  if (TERRAIN_KEYS.includes(item.key)) {
    const turningOn = !item.show
    if (!turningOn) { applyTerrain(null); return }
    if (item.key === 'terrain-national') {
      try {
        await ensureNationalTerrain()
      } catch (e) {
        console.warn('全国 30m 地形加载失败', e)
        // 不用 errorMsg:那是全屏遮罩,会挡住整个预览。加载失败只提示、不阻断。
        terrainError.value = '全国 30 米地形加载失败(网络或跨域限制):'
          + String(e?.message || e).slice(0, 120)
        setTimeout(() => { terrainError.value = '' }, 8000)
        return
      }
    }
    applyTerrain(item.key)
    flyToBbox()
    return
  }

  item.show = !item.show
  if (item.key === 'terrain-range') {
    // 下载范围框:开关 entity 显隐
    if (rangeEntity) rangeEntity.show = item.show
  } else if (item.key === 'buildings') {
    // 三维建筑瓦片集:Cesium3DTileset 有独立的 show 属性
    if (tileset3d) tileset3d.show = item.show
  } else if (item.layer) {
    item.layer.show = item.show
  }
  // 勾选显示时,定位到成果范围
  if (item.show) flyToBbox()
}

async function init() {
  const id = new URLSearchParams(location.search).get('id')
  if (!id) { errorMsg.value = '缺少任务 id 参数'; loading.value = false; return }

  let task
  try {
    task = await (await fetch(`/api/tasks/${id}`)).json()
  } catch (e) {
    errorMsg.value = '读取任务信息失败:' + (e?.message || e)
    loading.value = false
    return
  }
  taskName.value = task.name || id
  taskPath.value = task.output_path || ''
  const base = `/output/${encodeURIComponent(outputBase(task.output_path))}`

  // 只加载"已完成切片阶段"对应的图层。有 stages 时按阶段状态判断;
  // 旧任务无 stages 时回退按导出格式(整任务已完成)。
  const stages = Array.isArray(task.stages) ? task.stages : []
  const stageDone = (key) => stages.some(
    (s) => s.key === key && (s.status === 'done' || s.status === 'skipped'))
  const ready = stages.length
    ? {
        tms: stageDone('tms'), osm: stageDone('osm'), terrain: stageDone('terrain'),
        buildings: stageDone('tile_3d'),
      }
    : (() => {
        const f = parseFormats(task.export)
        return {
          tms: f.includes('tms'), osm: f.includes('osm'), terrain: f.includes('terrain'),
          buildings: f.includes('b3dm'),
        }
      })()

  // 天地图底图密钥(后端已保底:basemap_token 缺省回落 token)
  let basemapToken = ''
  try {
    const cfg = await (await fetch('/api/config')).json()
    basemapToken = cfg.basemap_token || ''
  } catch (_) { /* 取不到则天地图图层不可用 */ }

  viewer = new Viewer('cesium-container', {
    baseLayer: false, baseLayerPicker: false, geocoder: false,
    homeButton: false, sceneModePicker: true, navigationHelpButton: false,
    timeline: false, animation: false, fullscreenButton: true, infoBox: false,
    selectionIndicator: false,
  })
  viewer.scene.globe.baseColor = window.Cesium?.Color?.DARKSLATEGRAY || undefined

  // 底图(最底层):NaturalEarthII 离线 geodetic TMS
  try {
    const bm = await TileMapServiceImageryProvider.fromUrl('/basemap', {
      tilingScheme: new GeographicTilingScheme(),
    })
    viewer.imageryLayers.addImageryProvider(bm)
  } catch (e) {
    console.warn('底图加载失败', e)
  }

  // 天地图影像(第二层,叠在离线底图之上、成果之下)。默认不勾选,主动勾选才显示。
  // 图层顺序由添加顺序决定,这里在 TMS/OSM 之前添加,保证成果始终在天地图之上。
  if (basemapToken) {
    try {
      const tdt = new UrlTemplateImageryProvider({
        url:
          `https://t{s}.tianditu.gov.cn/img_w/wmts?` +
          `SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=img` +
          `&STYLE=default&TILEMATRIXSET=w&FORMAT=tiles` +
          `&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&tk=${basemapToken}`,
        subdomains: ['0', '1', '2', '3', '4', '5', '6', '7'],
        maximumLevel: 18,
      })
      const layer = viewer.imageryLayers.addImageryProvider(tdt)
      layer.show = false   // 默认不显示
      layers.value.push({ key: 'tianditu', label: '天地图影像(在线)', layer, show: false })
    } catch (e) { console.warn('天地图图层加载失败', e) }
  }

  // 叠加图层(成果,始终位于天地图之上)
  if (ready.tms) {
    try {
      const p = await TileMapServiceImageryProvider.fromUrl(`${base}/tms`, {
        tilingScheme: new GeographicTilingScheme(),
      })
      const layer = viewer.imageryLayers.addImageryProvider(p)
      layers.value.push({ key: 'tms', label: 'TMS 瓦片(EPSG:4326)', layer, show: true })
    } catch (e) { console.warn('TMS 叠加失败', e) }
  }
  if (ready.osm) {
    try {
      const p = new UrlTemplateImageryProvider({
        url: `${base}/osm/{z}/{x}/{y}.png`,
      })
      const layer = viewer.imageryLayers.addImageryProvider(p)
      // 同时有 tms 时默认隐藏 osm,避免叠盖,由用户切换对比
      const show = !ready.tms
      layer.show = show
      layers.value.push({ key: 'osm', label: 'OSM 瓦片(Web 墨卡托)', layer, show })
    } catch (e) { console.warn('OSM 叠加失败', e) }
  }

  // Cesium quantized-mesh 地形切片:挂到 viewer.terrainProvider(叠在底图影像下起伏)
  if (ready.terrain) {
    try {
      terrainProvider = await CesiumTerrainProvider.fromUrl(`${base}/terrain`, {
        requestVertexNormals: false,   // 首版切片未写法线
        requestWaterMask: false,
      })
      viewer.terrainProvider = terrainProvider
      layers.value.push({
        key: 'terrain', label: '本任务地形切片(quantized-mesh)',
        layer: null, show: true,
      })
    } catch (e) { console.warn('地形加载失败', e) }
  }

  // 全国 30 米地形(在线):所有预览都提供,默认不勾选、勾选时才联网加载。
  // 与「本任务地形切片」互斥(见 TERRAIN_KEYS / applyTerrain)。
  layers.value.push({
    key: 'terrain-national', label: '全国 30 米地形(在线)',
    layer: null, show: false,
  })

  // 三维建筑白模(b3dm 3D Tiles):作为 primitive 加入场景,不是影像图层
  if (ready.buildings) {
    try {
      tileset3d = await Cesium3DTileset.fromUrl(`${base}/3dtiles/tileset.json`, {
        // 建筑量大时限制内存占用;maximumScreenSpaceError 越大越省、越粗
        maximumScreenSpaceError: 16,
        skipLevelOfDetail: true,
      })
      viewer.scene.primitives.add(tileset3d)
      layers.value.push({
        key: 'buildings',
        label: `三维建筑白模${task.building_count ? `(${task.building_count} 栋)` : ''}`,
        layer: null, show: true,
      })
      // 底面高为 terrain 模式时,建筑高程已烘焙为真实海拔,须开地形才贴合;
      // 若本任务没有地形切片,提示用户成果可能悬空/沉底的原因。
      if (task.base_height_mode === 'terrain') {
        buildingHint.value = ready.terrain
          ? '建筑底面按 Esri DEM 逐栋采样烘焙。与「本任务地形切片」同源、贴合最准;切到「全国 30 米地形」时因高程源不同,可能出现轻微悬空或下沉。'
          : '建筑底面按真实海拔烘焙,需加载地形才会贴地。本任务未导出地形切片,可勾选「全国 30 米地形」查看——但它与建筑采样所用的 Esri DEM 非同源,贴合会有偏差;要精确贴合请另建同范围的地形任务。'
      }
    } catch (e) {
      console.warn('三维建筑加载失败', e)
    }
  }

  // 相机定位到成果范围
  taskBbox = Array.isArray(task.bbox) && task.bbox.length === 4 ? task.bbox : null

  // 地形任务:同步绘制下载范围边框(贴地形起伏),并加一个可开关的图层行
  if (ready.terrain && taskBbox) {
    drawRange(taskBbox)
    layers.value.push({ key: 'terrain-range', label: '地形下载范围', layer: null, show: true })
  }

  flyToBbox()
  loading.value = false
}

onMounted(init)
onBeforeUnmount(() => { if (viewer && !viewer.isDestroyed()) viewer.destroy() })
</script>

<template>
  <div class="preview">
    <div id="cesium-container" class="globe"></div>

    <div class="topbar">
      <span class="title">成果预览:{{ taskName || '—' }}</span>
      <span v-if="taskPath" class="path" :title="taskPath">📁 {{ taskPath }}</span>
    </div>

    <div v-if="layers.length" class="panel">
      <div class="panel-title">叠加图层</div>
      <label v-for="it in layers" :key="it.key" class="layer-row">
        <input type="checkbox" :checked="it.show" @change="toggle(it)" />
        <span>{{ it.label }}</span>
        <span v-if="it.key === 'terrain-national' && terrainLoading" class="loading-tag">加载中…</span>
      </label>
      <div class="tip">层序(下→上):NaturalEarthII(离线) → 天地图 → 成果(TMS/OSM) → 三维建筑</div>
      <div v-if="hasBothTerrain" class="tip">两个地形数据互斥,勾选其一会自动取消另一个。</div>
      <div v-if="terrainError" class="warn">{{ terrainError }}</div>
      <div v-if="buildingHint" class="warn">{{ buildingHint }}</div>
    </div>

    <div v-if="loading" class="mask">加载中…</div>
    <div v-if="errorMsg" class="mask error">{{ errorMsg }}</div>
  </div>
</template>

<style scoped>
.preview { position: fixed; inset: 0; }
.globe { position: absolute; inset: 0; }
.topbar {
  position: absolute; top: 0; left: 0; right: 0; height: 44px; z-index: 10;
  display: flex; align-items: center; gap: 16px; padding: 0 16px;
  background: linear-gradient(90deg, rgba(3,105,161,.92), rgba(6,182,212,.82));
  color: #fff; pointer-events: none;
}
.title { font-weight: 700; font-size: 15px; flex: 0 0 auto; }
.path {
  font-size: 12px; color: rgba(255,255,255,.85); pointer-events: auto;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-family: Consolas, monospace;
}
.panel {
  position: absolute; top: 56px; right: 12px; z-index: 10; min-width: 200px;
  background: rgba(255,255,255,.95); border: 1px solid #e2e8f0; border-radius: 10px;
  box-shadow: 0 6px 20px rgba(14,165,233,.18); padding: 12px 14px; font-size: 13px;
}
.panel-title { font-weight: 700; color: #0369a1; margin-bottom: 8px; }
.layer-row { display: flex; align-items: center; gap: 8px; padding: 4px 0; cursor: pointer; }
.loading-tag { font-size: 11px; color: #0ea5e9; flex: 0 0 auto; }
.tip { margin-top: 8px; font-size: 11px; color: #94a3b8; }
.warn { margin-top: 8px; font-size: 11px; color: #b45309; line-height: 1.6;
  background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; padding: 6px 8px;
  max-width: 260px; }
.mask {
  position: absolute; inset: 0; z-index: 20; display: flex;
  align-items: center; justify-content: center; color: #fff; font-size: 16px;
  background: rgba(15,23,42,.55);
}
.mask.error { color: #fecaca; }
</style>
