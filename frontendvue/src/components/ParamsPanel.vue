<script setup>
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { useDrawStore } from '../stores/draw'
import { useTaskStore } from '../stores/task'
import { mapController } from '../composables/mapController'
import { crsOptions } from '../utils/crs'
import { fmtSize } from '../utils/format'
import { parseVectorFiles, looksLikeLonLat, reprojectGeojson } from '../utils/vector'
import InfoTip from './InfoTip.vue'
import ContainerPicker from './ContainerPicker.vue'
import SrsModal from './SrsModal.vue'
import { api } from '../api'

const drawStore = useDrawStore()
const taskStore = useTaskStore()

// 影像/地形分 tab;外层「同时下载地形」勾选决定提交时是否也建另一类任务
const activeTab = ref('image')
const alsoOther = ref(false)

const IMG_LEVELS = Array.from({ length: 18 }, (_, i) => i + 1)  // 1..18
const DEM_LEVELS = Array.from({ length: 17 }, (_, i) => i)      // 0..16(Esri Terrain3D)

// 表单标签宽度按 tab 分开给:统一宽度会两头不讨好——
// 影像/地形的标签都是 4 字,给宽了右侧输入框被挤;三维建筑有「单瓦片上限」
// 这类长标签,给窄了标签会折行、把整行布局挤乱。
const LABEL_W_RASTER = '76px'    // 影像 / 地形(DEM)
const LABEL_W_BUILDING = '112px' // 三维建筑

const crsOpts = crsOptions()

const imageProviderOptions = [
  { value: 'tianditu_img', label: '天地图影像' },
  { value: 'tianditu_vec', label: '天地图矢量底图' },
  { value: 'tianditu_ter', label: '天地图地形晕渲' },
]
const imageExportOptions = [
  { value: 'geotiff', label: '带坐标 GeoTIFF(每级一张)' },
  { value: 'tms', label: 'TMS 瓦片' },
  { value: 'osm', label: 'OSM 瓦片(Web 墨卡托)' },
]
const demExportOptions = [
  { value: 'geotiff', label: '高程 GeoTIFF(每级一张·真实海拔)' },
  { value: 'tms', label: 'TMS 瓦片(晕渲可视化)' },
  { value: 'osm', label: 'OSM 瓦片(晕渲可视化·Web 墨卡托)' },
  { value: 'tiles', label: '保留原始 LERC 瓦片' },
  { value: 'terrain', label: 'Cesium 地形切片(.terrain + layer.json)' },
  { value: 'contour', label: '等高线(矢量)' },
]

// 后端能力表(/api/capabilities):各 provider 可用的阶段与容器格式。
// 格式勾选项的中文说明仍留在前端(上面两个数组)——它们要贴合界面语境;
// 容器(文件格式)下拉完全由后端驱动,后端加一种容器界面自动出现。
const caps = ref({})
const capsOf = (provider) => caps.value[provider]?.stages || []

async function loadCaps() {
  try {
    const d = await api.capabilities()
    caps.value = d.providers || {}
  } catch (e) {
    // 拿不到能力表时容器选择区不渲染,格式勾选与提交仍可用(后端会回落默认容器)
    console.warn('读取导出能力失败,容器格式将用默认值', e)
  }
}

// 影像参数表单
const imgForm = reactive({
  name: '影像下载',
  provider: 'tianditu_img',
  levels: [...IMG_LEVELS],
  export: ['geotiff', 'tms'],
  crs: 'EPSG:4326',
  clip: false,
  annotate: false,
  // 各阶段的文件格式,如 { geotiff: 'cog', tms: 'mbtiles' };空表示用后端默认
  containers: {},
})

// 地形(DEM)参数表单。级别默认不勾选,由用户按探测到的最高级别自行选择。
const demForm = reactive({
  name: '地形下载',
  provider: 'esri_terrain',
  levels: [],
  export: ['geotiff'],
  crs: 'EPSG:4326',
  containers: {},
  contourInterval: 50,
})

// 三维建筑数据源。默认 OSM(Overpass):Overture 需跨境读 512 个 parquet 分片的
// footer 才能做 bbox 裁剪,境内实测单次查询十几小时,不可用。
const buildingProviderOptions = [
  { value: 'osm_buildings', label: 'OSM 建筑轮廓(境内可直连,推荐)' },
  { value: 'local_vector', label: '本地矢量面(上传房屋轮廓)' },
  { value: 'overture_buildings', label: 'Overture 建筑轮廓(需境外网络/代理)' },
]

// ---- 建筑数据来源状态 ----
// 取数三级回落:本地网格缓存 → 在线数据包(COS) → OpenStreetMap 在线查询。
// 数据准备(全国 pbf 导入 + 打包上传)是运维动作,由后端
// update-building-data.bat 手动执行,界面不暴露,故这里不查 pbf 状态。
const cacheInfo = ref(null)        // {items:[...], total_bytes, ...}
const bundleInfo = ref(null)       // {remote:{enabled,version,...}, local_dist:{...}}

async function refreshOfflineInfo() {
  try {
    // 在线数据状态要联网取清单,可能较慢,单独容错(失败不影响本地缓存展示)
    const [c, b] = await Promise.all([
      api.buildingCache(),
      api.bundleStatus().catch(() => null),
    ])
    cacheInfo.value = c
    if (b) bundleInfo.value = b
  } catch (_) { /* 后端未就绪时静默 */ }
}

async function clearBuildingCache() {
  try {
    const r = await api.clearBuildingCache('osm', null)
    MessagePlugin.success(`已清理 ${r.cleared_cells} 个缓存格`)
    refreshOfflineInfo()
  } catch (e) {
    MessagePlugin.error('清理失败:' + (e?.message || e))
  }
}

const cacheText = computed(() => {
  const c = cacheInfo.value
  if (!c) return ''
  const osm = (c.items || []).find((i) => i.source === 'osm')
  if (!osm || !osm.cells) return '0 格(尚未下载过)'
  return `${osm.cells} 格 · ${fmtSize(osm.bytes)}`
})

// ---- 在线数据包状态 ----
const remote = computed(() => bundleInfo.value?.remote || null)
const remoteOn = computed(() => !!remote.value?.enabled)

// 数据来源档位:三级回落里当前处于哪一级
const sourceLevel = computed(() => {
  const osm = (cacheInfo.value?.items || []).find((i) => i.source === 'osm')
  const cells = osm?.cells || 0
  const online = remoteOn.value && !!remote.value?.version
  if (online) {
    return cells > 0
      ? { text: '在线数据 + 本地缓存', theme: 'success' }
      : { text: '在线数据(按需下载)', theme: 'success' }
  }
  if (cells > 0) return { text: '仅本地缓存(缺的会在线查询)', theme: 'warning' }
  return { text: 'OpenStreetMap 在线查询(较慢)', theme: 'warning' }
})

// 远端覆盖范围文案
const remoteCoverText = computed(() => {
  const bb = remote.value?.coverage_bbox
  if (!bb || bb.length !== 4) return ''
  return `东经 ${bb[0].toFixed(1)}~${bb[2].toFixed(1)}、北纬 ${bb[1].toFixed(1)}~${bb[3].toFixed(1)}`
})

onMounted(refreshOfflineInfo)
onMounted(loadCaps)
onBeforeUnmount(() => { if (pbfTimer) clearInterval(pbfTimer) })

// ---- 本地矢量面上传状态 ----
const bldFileInput = ref(null)
const bldSrsVisible = ref(false)
const uploading = ref(false)
let pendingBldGeojson = null      // 待用户选源坐标系的 geojson
// 上传结果:{upload_id, feature_count, polygon_count, bbox, fields:[...]}
const uploadInfo = ref(null)

// 高度来源模式
const heightModeOptions = [
  { value: 'meters', label: '字段为高度(米)' },
  { value: 'floors', label: '字段为层数' },
  { value: 'none', label: '不用字段(全部估算)' },
]

// 字段下拉选项:标注填充率,便于判断字段可用性
const numericFieldOptions = computed(() => {
  const fs = uploadInfo.value?.fields || []
  return fs.filter((f) => f.numeric).map((f) => ({
    value: f.name,
    label: `${f.name}(填充 ${(f.fill_rate * 100).toFixed(0)}%)`,
  }))
})
const allFieldOptions = computed(() => {
  const fs = uploadInfo.value?.fields || []
  return fs.map((f) => ({
    value: f.name,
    label: `${f.name}(填充 ${(f.fill_rate * 100).toFixed(0)}%)`,
  }))
})

// 三维建筑参数表单。无级别概念(数据是矢量要素集)。
const bldForm = reactive({
  name: '三维建筑',
  provider: 'osm_buildings',
  base_height_mode: 'terrain',
  height_offset: 0,
  default_height: 6,
  max_per_tile: 2000,
  // 是否复用本地网格缓存(取消则该范围重新联网抓取,但仍会回写缓存)
  use_cache: true,
  // 上传地形 id:底面高优先用它采样,未覆盖处回落在线地形
  dem_upload_id: '',
  // 本地矢量面字段映射
  upload_id: '',
  height_field: '',
  height_mode: 'meters',
  height_scale: 1,
  floor_height: 3,
  name_field: '',
  keep_fields: [],
  // 建筑轮廓矢量的文件格式:{ fetch_buildings: 'gpkg' | 'shapefile' | 'geojson' }
  containers: {},
})

const isLocalVector = computed(() => bldForm.provider === 'local_vector')

const scaleHint = computed(() => {
  if (bldForm.height_mode === 'floors') {
    return `字段值 × ${bldForm.height_scale} = 层数,再 × ${bldForm.floor_height} 米得建筑高度。字段本身就是层数时系数填 1。`
  }
  return `字段值 × ${bldForm.height_scale} = 高度(米)。数据以厘米存填 0.01,以英尺存填 0.3048,已是米则填 1。`
})

// 选中的高度字段填充率过低时提示:大部分建筑会落到面积估算
const lowFillWarn = computed(() => {
  if (!uploadInfo.value || bldForm.height_mode === 'none' || !bldForm.height_field) return ''
  const f = (uploadInfo.value.fields || []).find((x) => x.name === bldForm.height_field)
  if (!f) return ''
  const pct = f.fill_rate * 100
  if (pct >= 60) return ''
  return `字段「${f.name}」仅 ${pct.toFixed(0)}% 的建筑有值,其余会按占地面积估算层数(或用兜底高度),楼高会显得均一。`
})

function pickBldFile() { bldFileInput.value?.click() }

// 上传流程复用范围导入那套解析:shpjs 解 shp/zip、togeojson 解 kml,
// proj4 按 .prj 或用户选择的 EPSG 转 WGS84。后端只收已转好的 GeoJSON。
async function onBldFileChange(e) {
  const files = Array.from(e.target.files || [])
  e.target.value = ''
  if (!files.length) return
  try {
    const res = await parseVectorFiles(files)
    if (!res) { MessagePlugin.warning('未解析到矢量内容'); return }
    const { geojson, prjText } = res
    if (looksLikeLonLat(geojson)) { await doUpload(geojson); return }
    if (prjText) {
      try {
        reprojectGeojson(geojson, prjText)
        if (looksLikeLonLat(geojson)) { await doUpload(geojson); return }
      } catch (_) { /* 落到手选坐标系 */ }
    }
    pendingBldGeojson = geojson
    bldSrsVisible.value = true
  } catch (err) {
    MessagePlugin.error('矢量解析失败:' + (err?.message || err))
  }
}

async function onBldSrsConfirm(srcEpsg) {
  try {
    reprojectGeojson(pendingBldGeojson, srcEpsg)
    bldSrsVisible.value = false
    const gj = pendingBldGeojson
    pendingBldGeojson = null
    await doUpload(gj)
  } catch (err) {
    MessagePlugin.error('坐标转换失败:' + (err?.message || err))
  }
}

async function doUpload(geojson) {
  uploading.value = true
  try {
    const info = await api.uploadVector(geojson)
    uploadInfo.value = info
    bldForm.upload_id = info.upload_id
    // 按后端的推荐标记预选高度字段:优先"米"字段,否则层数字段
    const fs = info.fields || []
    const hf = fs.find((f) => f.suggest_height)
    const ff = fs.find((f) => f.suggest_floors)
    if (hf) { bldForm.height_field = hf.name; bldForm.height_mode = 'meters' }
    else if (ff) { bldForm.height_field = ff.name; bldForm.height_mode = 'floors' }
    else { bldForm.height_field = ''; bldForm.height_mode = 'none' }
    bldForm.name_field = ''
    bldForm.keep_fields = []
    MessagePlugin.success(
      `已上传:${info.polygon_count} 个面要素,${(info.fields || []).length} 个字段`)
    // 顺带把数据范围画到地图上,便于确认位置
    if (info.bbox && mapController.value) {
      const [w, s, e, n] = info.bbox
      mapController.value.loadGeojson({
        type: 'Feature', properties: {},
        geometry: { type: 'Polygon', coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]] },
      })
    }
  } catch (err) {
    MessagePlugin.error('上传失败:' + (err?.message || err))
  } finally {
    uploading.value = false
  }
}

const bldProviderHint = computed(() => ({
  osm_buildings: 'OpenStreetMap 建筑轮廓,经 Overpass API 按范围查询,境内可直连、秒级返回。范围会自动按 0.05° 分块请求。城区覆盖尚可,郊区偏稀疏;高度取 height / building:levels 标签。',
  overture_buildings: 'Overture 融合了 OSM、微软 AI 提取轮廓、Esri 与谷歌开放建筑,覆盖率更高。但数据是托管在 AWS S3 的 512 个 parquet 分片,做范围裁剪需读遍全部分片的元数据——境内实测单次查询需十几小时,基本不可用。仅在境外服务器或有高速代理时选用(代理配置见 config.yaml 的 buildings.proxy)。',
}[bldForm.provider]))

// ---- 上传地形(建筑底面高采样用,优先于在线地形)----
const demFileInput = ref(null)
const demUploading = ref(false)
const demInfo = ref(null)   // {dem_id, crs, bounds_wgs84, res_m_approx, ...}

function pickDemFile() { demFileInput.value?.click() }

async function onDemFileChange(e) {
  const f = (e.target.files || [])[0]
  e.target.value = ''
  if (!f) return
  demUploading.value = true
  try {
    const info = await api.uploadDem(f)
    demInfo.value = info
    bldForm.dem_upload_id = info.dem_id
    MessagePlugin.success(`已上传地形:${info.width}×${info.height} · ${info.crs}`)
  } catch (err) {
    MessagePlugin.error('地形上传失败:' + (err?.message || err))
  } finally {
    demUploading.value = false
  }
}

function clearDem() {
  demInfo.value = null
  bldForm.dem_upload_id = ''
}

// 目标范围:本地矢量按上传数据范围,其余按所选下载范围
const demTargetBbox = computed(
  () => (isLocalVector.value ? (drawStore.bbox || uploadInfo.value?.bbox)
    : drawStore.bbox) || null)

// 上传地形对目标范围的覆盖率(与后端 coverage_ratio 同一算法)
const demCoverage = computed(() => {
  const a = demInfo.value?.bounds_wgs84
  const b = demTargetBbox.value
  if (!a || !b) return null
  const iw = Math.max(a[0], b[0]); const ie = Math.min(a[2], b[2])
  const is = Math.max(a[1], b[1]); const inn = Math.min(a[3], b[3])
  if (ie <= iw || inn <= is) return 0
  const target = (b[2] - b[0]) * (b[3] - b[1])
  if (target <= 0) return 0
  return Math.min((ie - iw) * (inn - is) / target, 1)
})

const demCoverText = computed(() => {
  const c = demCoverage.value
  if (c === null) return '尚未选择范围,无法比对覆盖情况'
  const pct = c * 100
  if (pct >= 99.9) return `完全覆盖所选范围(${pct.toFixed(0)}%),不会下载在线地形`
  if (pct <= 0) return '与所选范围没有交集,请确认地形数据的坐标系与位置'
  return `仅覆盖所选范围的 ${pct.toFixed(0)}%,未覆盖处将回落在线地形兜底`
})

// 参考地形的说明(收进 ⓘ);措辞随数据源变化——本地矢量时比对的是矢量范围
const demTipText = computed(() => (
  `地形数据的范围需与${isLocalVector.value ? '上传的矢量面范围' : '所选下载范围'}一致。`
  + '若只覆盖了一部分,已覆盖处用上传地形(更精细)、未覆盖处自动回落在线地形兜底,'
  + '不会因此丢建筑;但两种高程源的高程基准可能不同,交界处可能出现台阶。'
  + '需为带坐标系的单波段高程 GeoTIFF(数值是海拔米数)。'))

const demCoverTheme = computed(() => {
  const c = demCoverage.value
  if (c === null) return 'default'
  if (c >= 0.999) return 'success'
  if (c <= 0) return 'danger'
  return 'warning'
})

// 底面高模式:b3dm 是绝对定位几何,Cesium 加载 3D Tiles 不会自动贴地形,
// 底面海拔必须在生成阶段烘焙进顶点,故必须让用户明确选择。
const baseModeOptions = [
  { value: 'terrain', label: '采样地形(推荐)' },
  { value: 'flat', label: '固定为 0' },
  { value: 'offset', label: '统一偏移' },
]
const baseModeHint = computed(() => ({
  terrain: '逐栋采样 DEM 取地面海拔并烘焙进几何,开启地形后建筑贴合地面。会自动补下一份低级别 DEM 用于采样(不导出)。中国东西部高差极大,这是全域唯一可靠的模式。',
  flat: '底面固定在椭球面(高度 0)。仅适用于预览时不加载地形的纯白模场景;一旦加载地形,高海拔地区建筑会整体埋入地下。',
  offset: '全体建筑底面统一设为下方偏移值。仅适用于范围小且地形平坦的情形,跨城市或有起伏会一部分悬空、一部分沉底。',
}[bldForm.base_height_mode]))

// 地形最高可用级别探测:选区后请求后端,超过此级别的勾选框禁用(无数据)
const demMaxLevel = ref(null)   // null=未探测;数字=该范围最高有效级别
const probing = ref(false)
async function probeDemMaxLevel() {
  const b = drawStore.bbox
  if (!b) { demMaxLevel.value = null; return }
  probing.value = true
  try {
    const d = await api.demMaxLevel({ west: b[0], south: b[1], east: b[2], north: b[3], provider: demForm.provider })
    demMaxLevel.value = d.max_level
    // 去掉已选但超限的级别
    demForm.levels = demForm.levels.filter((z) => z <= d.max_level)
  } catch (_) {
    demMaxLevel.value = null
  } finally {
    probing.value = false
  }
}
function demLevelDisabled(z) {
  return demMaxLevel.value != null && z > demMaxLevel.value
}

// 手动维护级别勾选(不依赖 t-checkbox-group 的插槽注入,避免 TDesign 版本差异导致不渲染)
function toggleLevel(form, z, checked) {
  const set = new Set(form.levels)
  if (checked) set.add(z); else set.delete(z)
  form.levels = [...set].sort((a, b) => a - b)
}
function isLevelChecked(form, z) { return form.levels.includes(z) }

// 全选
const imgAllChecked = computed(() => imgForm.levels.length === IMG_LEVELS.length)
function imgToggleAll(c) { imgForm.levels = c ? [...IMG_LEVELS] : [] }
// 可选的地形级别(受探测到的最高级别约束)
const demSelectableLevels = computed(() =>
  demMaxLevel.value != null ? DEM_LEVELS.filter((z) => z <= demMaxLevel.value) : DEM_LEVELS)
const demAllChecked = computed(() =>
  demSelectableLevels.value.length > 0 && demForm.levels.length === demSelectableLevels.value.length)
function demToggleAll(c) { demForm.levels = c ? [...demSelectableLevels.value] : [] }

const submitting = ref(false)

// 各级别明细缓存:影像/地形网格不同,分开查询
const imgPerLevel = ref({})
const demPerLevel = ref({})

async function estimateFor(provider, levels, target) {
  const b = drawStore.bbox
  if (!b) { target.value = {}; return }
  try {
    const d = await api.estimate({
      west: b[0], south: b[1], east: b[2], north: b[3],
      levels: levels.join(','), provider,
    })
    const map = {}
    for (const it of (d.levels || [])) map[it.z] = { tiles: it.tiles, bytes: it.bytes }
    target.value = map
  } catch (_) { target.value = {} }
}
function refreshImgEstimate() { estimateFor(imgForm.provider, IMG_LEVELS, imgPerLevel) }
function refreshDemEstimate() { estimateFor(demForm.provider, DEM_LEVELS, demPerLevel) }

watch(() => [drawStore.bbox, imgForm.provider], refreshImgEstimate, { deep: true })
watch(() => drawStore.bbox, () => { refreshDemEstimate(); probeDemMaxLevel() }, { deep: true })

// 影像数据类型切换时,中间地图底图同步切换
watch(() => imgForm.provider, (p) => { mapController.value?.setOverlayByProvider(p) })

// OSM 切片依赖最高级 4326 拼接图,与 geotiff 阶段合并出的最高级那张等价。
// 勾选 OSM 时自动带上 geotiff:后端会直接复用合并结果作 OSM 源,省一次重复拼接。
watch(() => imgForm.export, (exp) => {
  if (exp.includes('osm') && !exp.includes('geotiff')) {
    imgForm.export = ['geotiff', ...exp]
  }
}, { deep: true })

// per 可能是 ref(JS 上下文)或已被模板自动解包的普通对象,统一取底层 map
function unwrapMap(per) {
  return (per && per.value !== undefined) ? per.value : (per || {})
}

function summaryOf(form, per, allowAnno) {
  const map = unwrapMap(per)
  let tiles = 0; let bytes = 0
  for (const z of form.levels) {
    const it = map[z]
    if (it) { tiles += it.tiles; bytes += it.bytes }
  }
  if (allowAnno && form.annotate) { tiles *= 2; bytes *= 2 }
  return { tiles, bytes }
}
const imgSummary = computed(() => summaryOf(imgForm, imgPerLevel, true))
const demSummary = computed(() => summaryOf(demForm, demPerLevel, false))

// 仅导出 Cesium 地形切片(terrain)却多选了层级时的提示:
// terrain 是层级化 TIN 金字塔,只用最高层级作源、由 Cesium 从 0 级逐级细化,
// 多选低层级只增加下载量、不提升切片精度。geotiff/tiles 每级独立成果不受此限。
const demTerrainMultiLevelHint = computed(() => {
  const exp = demForm.export
  const onlyTerrain = exp.length === 1 && exp[0] === 'terrain'
  return onlyTerrain && demForm.levels.length > 1
})

function levelSizeOf(per, z) {
  const it = unwrapMap(per)[z]
  return it ? `${it.tiles}张 ~${fmtSize(it.bytes)}` : ''
}

// 构造影像/地形任务 payload
function imgPayload() {
  return {
    name: imgForm.name || '影像下载',
    provider: imgForm.provider,
    bbox: drawStore.bbox,
    levels: [...imgForm.levels].sort((a, z) => a - z),
    export: imgForm.export.join(','),
    crs: imgForm.crs,
    geometry: drawStore.geometry || null,
    clip: !!(imgForm.clip && drawStore.geometry),
    annotate: imgForm.annotate,
    containers: { ...imgForm.containers },
  }
}
function demPayload() {
  return {
    name: demForm.name || '地形下载',
    provider: demForm.provider,
    bbox: drawStore.bbox,
    levels: [...demForm.levels].sort((a, z) => a - z),
    export: demForm.export.join(','),
    crs: demForm.crs,
    geometry: drawStore.geometry || null,
    clip: false,
    annotate: false,
    containers: { ...demForm.containers },
    contour_interval: Number(demForm.contourInterval) || 50,
  }
}

function bldPayload() {
  return {
    name: bldForm.name || '三维建筑',
    provider: bldForm.provider,
    // 本地矢量未画范围时给上传数据的 bbox(后端也会兜底取);
    // 画了范围则只切范围内那部分。
    bbox: drawStore.bbox || uploadInfo.value?.bbox || null,
    // 建筑无级别概念;后端会合成 [0] 让通用校验通过
    levels: [],
    export: 'b3dm',
    geometry: drawStore.geometry || null,
    clip: false,
    annotate: false,
    base_height_mode: bldForm.base_height_mode,
    height_offset: Number(bldForm.height_offset) || 0,
    default_height: Number(bldForm.default_height) || 6,
    max_per_tile: Number(bldForm.max_per_tile) || 2000,
    use_cache: !!bldForm.use_cache,
    // 仅 terrain 模式才带上传地形(后端也会校验这一点)
    dem_upload_id: (bldForm.base_height_mode === 'terrain'
      ? (bldForm.dem_upload_id || '') : ''),
    // 本地矢量面字段映射(其他数据源忽略这些)
    upload_id: bldForm.upload_id || '',
    height_field: bldForm.height_field || '',
    height_mode: bldForm.height_mode || 'none',
    height_scale: Number(bldForm.height_scale) || 1,
    floor_height: Number(bldForm.floor_height) || 3,
    name_field: bldForm.name_field || '',
    keep_fields: [...(bldForm.keep_fields || [])],
    containers: { ...bldForm.containers },
  }
}

// 选区跨度(平方度):建筑管线范围过大时后端会拒收,这里提前提示
const bldSpan = computed(() => {
  const b = drawStore.bbox
  if (!b) return 0
  return (b[2] - b[0]) * (b[3] - b[1])
})
const bldSpanTooBig = computed(() => bldSpan.value > 4)

// 提交按钮禁用条件:本地矢量看是否已上传,其余数据源看是否已选范围
const submitDisabled = computed(() => {
  if (activeTab.value === 'buildings' && isLocalVector.value) {
    return !bldForm.upload_id
  }
  return !drawStore.hasRange
})

// 外层勾选文案随当前 tab 变化(影像 tab→同时下载地形;地形 tab→同时下载影像)
// 三维建筑 tab 不提供配对下载(与栅格管线无共用中间产物),该勾选框隐藏
const alsoLabel = computed(() =>
  activeTab.value === 'image' ? '同时下载地形(DEM)' : '同时下载影像')

async function submit() {
  // 本地矢量面自带范围(数据就是范围),不必先画;其余数据源必须先选范围
  const localVectorReady = activeTab.value === 'buildings' && isLocalVector.value
  if (!drawStore.hasRange && !localVectorReady) {
    MessagePlugin.warning('请先选择下载范围')
    return
  }

  // 三维建筑独立提交
  if (activeTab.value === 'buildings') {
    if (isLocalVector.value) {
      if (!bldForm.upload_id) { MessagePlugin.error('请先上传矢量面数据'); return }
      if (bldForm.height_mode !== 'none' && !bldForm.height_field) {
        MessagePlugin.error('已选择按字段取高度,请指定高度字段')
        return
      }
    }
    // 本地矢量的数据量由文件界定,不受面积上限约束
    if (!isLocalVector.value && bldSpanTooBig.value) {
      MessagePlugin.error(`范围过大(约 ${bldSpan.value.toFixed(1)} 平方度,上限 4),请缩小范围`)
      return
    }
    submitting.value = true
    try {
      await taskStore.create(bldPayload())
      MessagePlugin.success('三维建筑任务已加入队列')
    } catch (err) {
      MessagePlugin.error('提交失败:' + (err?.message || err))
    } finally {
      submitting.value = false
    }
    return
  }

  // 当前 tab 为主项;勾选「同时下载另一类」则附加。各自独立成任务。
  const jobs = []
  if (activeTab.value === 'image') {
    jobs.push({ kind: 'image', build: imgPayload })
    if (alsoOther.value) jobs.push({ kind: 'terrain', build: demPayload })
  } else {
    jobs.push({ kind: 'terrain', build: demPayload })
    if (alsoOther.value) jobs.push({ kind: 'image', build: imgPayload })
  }

  for (const j of jobs) {
    const f = j.kind === 'image' ? imgForm : demForm
    const cn = j.kind === 'image' ? '影像' : '地形'
    if (!f.levels.length) { MessagePlugin.error(`${cn}:请至少勾选一个下载级别`); return }
    if (!f.export.length) { MessagePlugin.error(`${cn}:请至少选择一种导出格式`); return }
  }

  submitting.value = true
  try {
    for (const j of jobs) await taskStore.create(j.build())
    MessagePlugin.success(jobs.length > 1 ? '影像与地形任务已加入队列' : '已加入下载队列')
  } catch (err) {
    MessagePlugin.error('提交失败:' + (err?.message || err))
  } finally {
    submitting.value = false
  }
}
</script>
<template>
  <t-card title="2. 下载参数" :bordered="true" class="panel-card">
    <t-tabs v-model="activeTab" class="dl-tabs">
      <!-- 影像 tab -->
      <t-tab-panel value="image" label="影像">
        <t-form label-align="left" :label-width="LABEL_W_RASTER" class="tab-form">
          <t-form-item label="任务名称">
            <t-input v-model="imgForm.name" placeholder="任务名称" />
          </t-form-item>
          <t-form-item label="数据类型">
            <t-select v-model="imgForm.provider" :options="imageProviderOptions" />
          </t-form-item>
          <t-form-item label="下载级别">
            <div class="levels">
              <div class="lv-head">
                <label class="lv nlv">
                  <input type="checkbox" :checked="imgAllChecked"
                    @change="(e) => imgToggleAll(e.target.checked)" />
                  <span class="lv-z">全选</span>
                </label>
              </div>
              <div class="lv-scroll">
                <div class="lv-group">
                  <label v-for="z in IMG_LEVELS" :key="z" class="lv nlv">
                    <input type="checkbox" :checked="isLevelChecked(imgForm, z)"
                      @change="(e) => toggleLevel(imgForm, z, e.target.checked)" />
                    <span class="lv-z">{{ z }}</span>
                    <span v-if="levelSizeOf(imgPerLevel, z)" class="lv-size">{{ levelSizeOf(imgPerLevel, z) }}</span>
                  </label>
                </div>
              </div>
            </div>
          </t-form-item>
          <t-form-item label="导出格式">
            <t-checkbox-group v-model="imgForm.export" :options="imageExportOptions" />
          </t-form-item>
          <ContainerPicker :stages="capsOf(imgForm.provider)"
            :selected="imgForm.export" v-model="imgForm.containers" />
          <t-form-item v-if="imgForm.export.includes('osm')" label-width="0">
            <div class="export-note">OSM 切片需重投影,以最高级拼接图作源,已自动勾选 GeoTIFF——切片时直接复用合并结果,不重复拼接。GeoTIFF 主文件恒输出一份 EPSG:4326;若另选了其他坐标系,会再额外生成一份对应投影的成果。(TMS 与天地图瓦片同构,直接由缓存瓦片映射,不经 GeoTIFF。)</div>
          </t-form-item>
          <t-form-item label-width="0">
            <t-tooltip content="勾选后同步下载对应的路网/地名注记图层,并在导出时叠加烘焙进 GeoTIFF/TMS/OSM 成果(瓦片数翻倍)">
              <t-checkbox v-model="imgForm.annotate">叠加路网注记</t-checkbox>
            </t-tooltip>
          </t-form-item>
          <!-- 长标签项:用 FormItem 自带的 label-align="top" 让标签独占一行、
               控件从最左侧起占满。不能靠 CSS 压 margin —— TDesign 会给
               .t-form__controls 加**行内** style="margin-left: <labelWidth>",
               行内样式优先级更高,外部 CSS 的 margin-left:0 会被忽略。 -->
          <t-form-item label="输出坐标系(GeoTIFF)" label-align="top">
            <t-select v-model="imgForm.crs" :options="crsOpts" filterable />
          </t-form-item>
          <t-form-item v-if="drawStore.clippable" label-width="0">
            <t-checkbox v-model="imgForm.clip">裁剪 GeoTIFF 到矢量/多边形边界</t-checkbox>
          </t-form-item>
        </t-form>
        <div class="estimate">
          影像所选 {{ imgForm.levels.length }} 级 · 共 {{ imgSummary.tiles }} 张 ·
          约 {{ fmtSize(imgSummary.bytes) }}
          <span class="est-hint">(仅原始瓦片下载量,非导出成果大小)</span>
        </div>
      </t-tab-panel>

      <!-- 地形 tab -->
      <t-tab-panel value="terrain" label="地形(DEM)">
        <t-form label-align="left" :label-width="LABEL_W_RASTER" class="tab-form">
          <t-form-item label="任务名称">
            <t-input v-model="demForm.name" placeholder="任务名称" />
          </t-form-item>
          <t-form-item label="下载级别">
            <div class="levels">
              <div class="lv-head">
                <label class="lv nlv">
                  <input type="checkbox" :checked="demAllChecked"
                    @change="(e) => demToggleAll(e.target.checked)" />
                  <span class="lv-z">全选</span>
                </label>
                <span v-if="probing" class="lv-probe">探测最高级别中…</span>
                <span v-else-if="demMaxLevel != null" class="lv-probe ok">该范围最高 {{ demMaxLevel }} 级</span>
              </div>
              <div class="lv-scroll">
                <div class="lv-group">
                  <label v-for="z in DEM_LEVELS" :key="z" class="lv nlv"
                    :class="{ disabled: demLevelDisabled(z) }">
                    <input type="checkbox" :checked="isLevelChecked(demForm, z)"
                      :disabled="demLevelDisabled(z)"
                      @change="(e) => toggleLevel(demForm, z, e.target.checked)" />
                    <span class="lv-z">{{ z }}</span>
                    <span v-if="demLevelDisabled(z)" class="lv-size lv-nodata">无数据</span>
                    <span v-else-if="levelSizeOf(demPerLevel, z)" class="lv-size">{{ levelSizeOf(demPerLevel, z) }}</span>
                  </label>
                </div>
              </div>
            </div>
          </t-form-item>
          <t-form-item label="导出格式">
            <t-checkbox-group v-model="demForm.export" :options="demExportOptions" />
          </t-form-item>
          <ContainerPicker :stages="capsOf(demForm.provider)"
            :selected="demForm.export" v-model="demForm.containers" />
          <t-form-item v-if="demForm.export.includes('contour')" label="等高距(米)">
            <t-input-number v-model="demForm.contourInterval" :min="1" :max="1000"
              :step="10" theme="column" style="width: 120px" />
            <InfoTip content="相邻等高线的高差。30 米级 DEM 上 50 米较合适;设得过小(如 5 米)会因源数据精度不足产生大量锯齿线。"
              max-width="360px" />
          </t-form-item>
          <t-form-item v-if="demTerrainMultiLevelHint" label-width="0">
            <div class="dem-warn">⚠ 仅导出 Cesium 地形切片时,只用所选的最高层级作高程源,Cesium 会自动从 0 级逐级细化。多选低层级只会增加下载量,不会提升切片精度。如只要地形切片,选一个最高层级即可。</div>
          </t-form-item>
          <t-form-item label-width="0">
            <div class="dem-note">Esri Terrain3D 真实高程(EPSG:3857,LERC 解码为米值)。GeoTIFF 可在 QGIS 出等高线/坡度。Cesium 地形切片输出 WGS84 geodetic quantized-mesh-1.0(未压缩·无法线),供 CesiumJS CesiumTerrainProvider 加载。最高级别随范围而定(超出部分无数据,已置灰),中国多数区域约 13-15 级。</div>
          </t-form-item>
          <t-form-item label="输出坐标系(高程 GeoTIFF)" label-align="top">
            <t-select v-model="demForm.crs" :options="crsOpts" filterable />
          </t-form-item>
        </t-form>
        <div class="estimate">
          地形所选 {{ demForm.levels.length }} 级 · 共 {{ demSummary.tiles }} 张 ·
          约 {{ fmtSize(demSummary.bytes) }}
          <span class="est-hint">(仅原始瓦片下载量,非导出成果大小)</span>
        </div>
      </t-tab-panel>

      <t-tab-panel value="buildings" label="三维建筑">
        <t-form label-align="left" :label-width="LABEL_W_BUILDING" class="tab-form">
          <t-form-item label="任务名称">
            <t-input v-model="bldForm.name" placeholder="任务名称" />
          </t-form-item>
          <t-form-item>
            <template #label>
              数据源
              <InfoTip :content="bldProviderHint" max-width="380px" />
            </template>
            <t-select v-model="bldForm.provider" :options="buildingProviderOptions" />
          </t-form-item>

          <!-- ===== 建筑数据(仅 OSM 源)=====
               只呈现使用者关心的两件事:数据从哪来、本地存了多少。
               全国 pbf 导入与打包属于"准备数据"的运维动作,已移到后端
               update-building-data.bat,不在界面暴露(避免误点等半小时)。 -->
          <template v-if="bldForm.provider === 'osm_buildings'">
            <t-form-item>
              <template #label>
                数据来源
                <InfoTip max-width="360px"
                  content="建筑轮廓按 0.05° 网格缓存:下载时先查本地,本地没有的格子自动从在线数据获取(一个城区通常几十 KB),取过即存本地、之后不再联网。在线数据未覆盖的区域(如境外)会回落到 OpenStreetMap 在线查询,较慢。" />
              </template>
              <div class="src-box">
                <t-tag :theme="sourceLevel.theme" variant="light">{{ sourceLevel.text }}</t-tag>
                <div v-if="remoteOn && remote?.version" class="rb-sub">
                  在线数据 {{ remote.bundle_count }} 包 / {{ fmtSize(remote.total_bytes) }} ·
                  版本 {{ remote.version }}
                </div>
                <div v-if="remoteOn && remote?.version && remoteCoverText" class="rb-sub">
                  覆盖 {{ remoteCoverText }}
                </div>
                <div v-else-if="remoteOn" class="rb-bad">
                  在线数据读取失败<template v-if="remote?.error">:{{ remote.error }}</template>
                </div>
                <div class="rb-sub">本地已存 {{ cacheText }}</div>
              </div>
            </t-form-item>
            <t-form-item label-width="0">
              <div class="up-row">
                <t-button size="small" variant="text" theme="danger"
                  @click="clearBuildingCache">清理本地缓存</t-button>
              </div>
            </t-form-item>
            <t-form-item label-width="0">
              <t-checkbox v-model="bldForm.use_cache">
                优先使用本地缓存(取消则该范围重新获取)
              </t-checkbox>
            </t-form-item>
          </template>

          <!-- ===== 本地矢量面:上传 + 字段映射 ===== -->
          <template v-if="isLocalVector">
            <t-form-item>
              <template #label>
                房屋轮廓数据
                <InfoTip content="支持 GeoJSON / KML / Shapefile(选 .zip 或同时选 .shp/.dbf/.shx,建议附 .prj)。需为面要素(房屋轮廓)。文件在本地解析并转为 WGS84 后上传,属性字段会自动列出供选择。" />
              </template>
              <div class="up-row">
                <t-button size="small" variant="outline" :loading="uploading"
                  @click="pickBldFile">选择文件…</t-button>
                <span v-if="uploadInfo" class="up-ok">
                  已上传 {{ uploadInfo.polygon_count }} 个面
                </span>
                <span v-else class="up-none">未上传</span>
              </div>
              <input ref="bldFileInput" type="file" multiple style="display:none"
                accept=".geojson,.json,.kml,.shp,.dbf,.shx,.prj,.zip"
                @change="onBldFileChange" />
            </t-form-item>

            <template v-if="uploadInfo">
              <t-form-item label="高度来源">
                <t-radio-group v-model="bldForm.height_mode" :options="heightModeOptions" />
              </t-form-item>
              <t-form-item v-if="bldForm.height_mode !== 'none'" label="高度字段">
                <t-select v-model="bldForm.height_field" :options="numericFieldOptions"
                  placeholder="选择字段" filterable />
              </t-form-item>
              <t-form-item v-if="bldForm.height_mode !== 'none'">
                <template #label>
                  {{ bldForm.height_mode === 'floors' ? '层数换算系数' : '单位换算系数' }}
                  <InfoTip :content="scaleHint" />
                </template>
                <t-input-number v-model="bldForm.height_scale" :min="0.001" :max="1000"
                  :step="0.1" style="width: 100%" />
              </t-form-item>
              <t-form-item v-if="bldForm.height_mode === 'floors'" label="单层层高(米)">
                <t-input-number v-model="bldForm.floor_height" :min="0.1" :max="100"
                  :step="0.1" style="width: 100%" />
              </t-form-item>

              <t-form-item label="建筑名称字段(可选)">
                <t-select v-model="bldForm.name_field" :options="allFieldOptions"
                  placeholder="不设置" clearable filterable />
              </t-form-item>
              <t-form-item>
                <template #label>
                  保留到模型的字段
                  <InfoTip content="勾选的字段会写进 b3dm 的 Batch Table,在 Cesium 里点击建筑即可查看。字段名后的百分比是该字段在数据中的填充率,填充率低的字段大部分建筑会是空值。" />
                </template>
                <t-select v-model="bldForm.keep_fields" :options="allFieldOptions"
                  placeholder="选择要写入模型的属性字段" multiple filterable :min-collapsed-num="3" />
              </t-form-item>
              <t-form-item v-if="lowFillWarn" label-width="0">
                <div class="bld-warn">⚠ {{ lowFillWarn }}</div>
              </t-form-item>
            </template>
          </template>

          <t-form-item>
            <template #label>
              建筑底面高
              <!-- 说明随所选模式变化,直接绑 baseModeHint -->
              <InfoTip :content="baseModeHint" />
            </template>
            <t-radio-group v-model="bldForm.base_height_mode" :options="baseModeOptions" />
          </t-form-item>

          <!-- ===== 参考地形:可上传本地 GeoTIFF,优先于在线地形 ===== -->
          <template v-if="bldForm.base_height_mode === 'terrain'">
            <t-form-item>
              <template #label>
                参考地形
                <InfoTip :content="demTipText" max-width="360px" />
              </template>
              <div class="up-row">
                <t-button size="small" variant="outline" :loading="demUploading"
                  @click="pickDemFile">上传地形…</t-button>
                <t-button v-if="demInfo" size="small" variant="text" theme="danger"
                  @click="clearDem">移除</t-button>
                <span v-if="!demInfo" class="up-none">未上传,使用在线地形</span>
              </div>
              <input ref="demFileInput" type="file" style="display:none"
                accept=".tif,.tiff" @change="onDemFileChange" />
            </t-form-item>
            <template v-if="demInfo">
              <t-form-item label-width="0">
                <div class="src-box">
                  <div class="rb-sub">
                    {{ demInfo.filename }} · {{ demInfo.width }}×{{ demInfo.height }} ·
                    {{ demInfo.crs }} · 约 {{ demInfo.res_m_approx }} m
                  </div>
                  <t-tag :theme="demCoverTheme" variant="light">{{ demCoverText }}</t-tag>
                </div>
              </t-form-item>
            </template>
          </template>

          <t-form-item v-if="bldForm.base_height_mode !== 'flat'"
            :label="bldForm.base_height_mode === 'offset' ? '底面高度(米)' : '附加偏移(米)'">
            <t-input-number v-model="bldForm.height_offset" :step="1" style="width: 100%" />
          </t-form-item>
          <t-form-item>
            <template #label>
              兜底建筑高(米)
              <InfoTip content="建筑高度在国内数据里缺失率偏高(不少轮廓来自 AI 提取,无高度)。缺失时按「层数 × 3 米」推算,再缺失按占地面积估层,最后才用此兜底值。各来源占比会写入成果的 metadata.json。" />
            </template>
            <t-input-number v-model="bldForm.default_height" :min="1" :max="1000"
              :step="1" style="width: 100%" />
          </t-form-item>
          <t-form-item label-align="top">
            <template #label>
              单瓦片建筑数上限
              <InfoTip content="超过此数即四叉树细分。数值小→瓦片多、单个小、加载更渐进;数值大→瓦片少、单个大。2000 是较均衡的默认值。" />
            </template>
            <t-input-number v-model="bldForm.max_per_tile" :min="100" :max="50000"
              :step="100" style="width: 100%" />
          </t-form-item>
          <!-- 建筑轮廓矢量的文件格式。三维成果(b3dm)结构固定、无可选容器,
               故这里只会出现「拉取建筑轮廓」一行。 -->
          <ContainerPicker :stages="capsOf(bldForm.provider)"
            :selected="['fetch_buildings']" v-model="bldForm.containers" />
        </t-form>
        <div v-if="isLocalVector" class="estimate">
          <template v-if="uploadInfo">
            共 {{ uploadInfo.polygon_count }} 个面要素 ·
            {{ (uploadInfo.fields || []).length }} 个字段
            <span class="est-hint">{{ drawStore.hasRange ? '(已画范围,只切范围内的部分)' : '(未画范围,按数据自身范围全部切)' }}</span>
          </template>
          <template v-else>请先上传房屋轮廓数据</template>
        </div>
        <div v-else class="estimate">
          范围跨度约 {{ bldSpan.toFixed(3) }} 平方度
          <span v-if="bldSpanTooBig" class="bld-over">· 超出上限 4,请缩小范围</span>
          <span v-else class="est-hint">(建筑栋数在取数阶段确定)</span>
        </div>
      </t-tab-panel>
    </t-tabs>

    <t-checkbox v-if="activeTab !== 'buildings'" v-model="alsoOther" class="also-check">{{ alsoLabel }}</t-checkbox>
    <t-button theme="primary" block :loading="submitting"
      :disabled="submitDisabled" @click="submit">加入下载队列</t-button>

    <!-- 本地矢量上传时,坐标系无法自动判定则弹出手选 -->
    <SrsModal v-model:visible="bldSrsVisible" @confirm="onBldSrsConfirm" />
  </t-card>
</template>
<style scoped>
.panel-card { margin-bottom: 10px; }
.dl-tabs { margin-bottom: 6px; }

/* ---- 表单布局 ----
   TDesign 的表单是 **float 布局**(label 浮动在左,控件靠行内
   margin-left = labelWidth 让位),不是 flex。由此两条约束:
     1. 缩进只能通过 labelWidth 控制。外部 CSS 改 controls 的
        margin-left 无效——那是行内样式,优先级更高。
     2. 想让某一项"标签独占一行",用 FormItem 的 label-align="top";
        无标签的项用 label-width="0"。都交给组件自己算,别用 class,
        FormItem 不透传 class 到根元素。
   标签默认已是 nowrap + 与输入框等高的行高,无需重复设置,
   只把下内边距归零(那是给顶部对齐留的)。 */
.tab-form :deep(.t-form__label) { padding-bottom: 0 !important; }

/* 标签独占一行的项:不再套用同行时的行高与 nowrap */
.tab-form :deep(.t-form__label--top) {
  line-height: 1.5; white-space: normal; min-height: 0;
  padding-right: 0; padding-bottom: 3px !important;
}
/* 三维建筑 tab 说明文字与超限提示 */
.bld-over { color: #dc2626; font-weight: 600; }
.bld-warn { font-size: 12px; color: #b45309; line-height: 1.6;
  background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; padding: 6px 8px; }
/* 本地矢量上传行 */
.up-row { display: flex; align-items: center; gap: 8px; }
.up-ok { font-size: 12px; color: #16a34a; font-weight: 600; }
.up-none { font-size: 12px; color: #94a3b8; }
/* 建筑数据来源状态 */
.src-box { display: flex; flex-direction: column; gap: 3px; }
.rb-sub { font-size: 12px; color: #64748b; line-height: 1.6; }
.rb-bad { font-size: 12px; color: #dc2626; font-weight: 600; line-height: 1.6; }
.tab-form { margin-top: 8px; }
.estimate { font-size: 12px; color: #94a3b8; margin: 4px 0 8px; }
.est-hint { color: #cbd5e1; }
.also-check { margin: 4px 0 10px; }
.levels { display: flex; flex-direction: column; gap: 4px; }
.lv-head { flex: 0 0 auto; padding-bottom: 4px; border-bottom: 1px solid #eef2f7;
  display: flex; align-items: center; gap: 8px; }
.lv-scroll { max-height: 200px; overflow-y: auto; padding: 4px 2px 0; }
.lv-group { display: flex; flex-direction: column; gap: 2px; }
.levels .lv { margin-right: 0; }
.levels :deep(.lv .t-checkbox__label) {
  display: inline-flex; align-items: baseline; gap: 6px; width: 100%;
}
/* 原生 checkbox 级别项(不依赖 TDesign,保证渲染) */
.nlv { display: flex; align-items: center; gap: 6px; cursor: pointer;
  font-size: 13px; color: #334155; line-height: 1.9; }
.nlv input { cursor: pointer; margin: 0; }
.nlv.disabled { cursor: not-allowed; color: #cbd5e1; }
.nlv.disabled input { cursor: not-allowed; }
.lv-z { min-width: 18px; font-weight: 600; }
.lv-size { font-size: 11px; color: #94a3b8; }
.lv-nodata { color: #cbd5e1; font-style: italic; }
.lv-probe { font-size: 11px; color: #94a3b8; margin-left: auto; }
.lv-probe.ok { color: #0369a1; }
.dem-note { font-size: 11px; color: #0369a1; line-height: 1.6;
  background: #f0f9ff; border: 1px solid #e0f2fe; border-radius: 6px; padding: 6px 8px; }
.export-note { font-size: 11px; color: #0369a1; line-height: 1.6;
  background: #f0f9ff; border: 1px solid #e0f2fe; border-radius: 6px; padding: 6px 8px; }
.dem-warn { font-size: 11px; color: #b45309; line-height: 1.6;
  background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; padding: 6px 8px; }
</style>
