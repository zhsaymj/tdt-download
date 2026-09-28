<script setup>
/**
 * 统一的「处理」对话框:下载 / 本地栅格 / 本地矢量 / 本地三维四种来源共用一套表单。
 *
 * 取代原先 1476 行、104 个表单项、4 个 tab 的 ParamsPanel。旧版四个 tab 的表单
 * 高度重叠——「任务名称」写了 4 遍、「导出格式」3 遍、「下载级别」「等高距」各 2 遍,
 * 格式定义散在 2 个文件、容器选择散在 3 个文件。这里合并为**一个** form 模型:
 * 参数显示与否由"当前处理的数据是什么类型"决定,而类型能力全部来自后端
 * /api/capabilities(即 core/formats.py 注册表),后端加格式界面自动跟上。
 */
import { computed, reactive, ref, watch } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { useDrawStore } from '../stores/draw'
import { useTaskStore } from '../stores/task'
import { useBasemapStore } from '../stores/basemap'
import { crsOptions } from '../utils/crs'
import { fmtNum, fmtSize } from '../utils/format'
import {
  ANNOTATION_MAX_Z, annotationExcessLevels, annotationUsable, canAnnotate,
  fmtNameOf,
} from '../utils/provider'
import {
  DEM_CRS_HINT, IMG_LEVELS,
  defaultContainersForStages, defaultTaskName,
  downloadDefaultsForProvider, ensureImageTmsLevels,
  formatPixelResolution, formatPixelSize, formatSampleSpacing, formatScale72Dpi,
  levelsForProvider, needsRegionProbe, normalizeContainerMap,
} from '../utils/taskDefaults'
import { api } from '../api'
import InfoTip from './InfoTip.vue'
import ContainerPicker from './ContainerPicker.vue'
import BuildingParams from './BuildingParams.vue'
import SidePanel from './SidePanel.vue'

const props = defineProps({
  visible: { type: Boolean, default: false },
  /** { kind: 'download' | 'local_raster' | 'local_vector' | 'local_3d' } */
  source: { type: Object, default: null },
})
const emit = defineEmits(['update:visible', 'created'])

const drawStore = useDrawStore()
const taskStore = useTaskStore()
const basemapStore = useBasemapStore()
const crsOpts = crsOptions()

/** 下载数据源。三维建筑归到这里而不是单独 tab:它同样是"选范围 + 取数 + 出成果" */
const providerOptions = [
  { value: 'tianditu_img', label: '天地图影像', group: '影像' },
  { value: 'tianditu_vec', label: '天地图矢量底图', group: '影像' },
  { value: 'tianditu_ter', label: '天地图地形晕渲', group: '影像' },
  { value: 'google_img', label: 'Google 卫星影像', group: '影像' },
  { value: 'esri_imagery', label: 'Esri World Imagery', group: '影像' },
  { value: 'esri_terrain', label: '全国地形 DEM(Esri Terrain3D)', group: '地形' },
  { value: 'osm_buildings', label: '三维建筑白模(OSM)', group: '三维建筑' },
  { value: 'local_vector', label: '三维建筑白模(本地矢量面)', group: '三维建筑' },
]

// 单一表单模型:字段是四个旧 form 的并集,显示与否由数据类型决定
const form = reactive({
  name: '',
  provider: 'tianditu_img',
  levels: [...IMG_LEVELS],
  export: [],
  containers: {},
  crs: 'EPSG:4326',
  clip: true,
  annotate: false,
  keepTilesDir: true,
  tmsSourceStrategy: 'contiguous',
  contourInterval: 50,
  // 本地文件
  path: '',
  useRange: false,
  vecContainer: 'gpkg',
  // 建筑轮廓矢量的成果格式(建筑任务的 fetch_buildings 阶段)
  bldVecContainer: 'gpkg',
  // 三维(local_3d):数据类型(osgb/pointcloud)与点云坐标系、采样分辨率
  d3Type: 'osgb',
  pcCrsEpsg: '',
  pcCrsLocal: false,
  pcResolution: 0,
})

// ---- 后端能力表:各 provider 可用的阶段与容器 ----
const caps = ref({})
async function loadCaps() {
  if (Object.keys(caps.value).length) return
  try { caps.value = (await api.capabilities()).providers || {} } catch (_) { /* 容器区不渲染,格式仍可用 */ }
}

/** 当前处理的是哪种来源 */
const kind = computed(() => props.source?.kind || 'download')
const isDownload = computed(() => kind.value === 'download')
const isLocalRaster = computed(() => kind.value === 'local_raster')
const isLocalVector = computed(() => kind.value === 'local_vector')
const isLocal3D = computed(() => kind.value === 'local_3d')

/** 三维来源内的数据类型:OSGB 目录 / 点云(LAS/LAZ 文件或目录) */
const d3TypeOptions = [
  { value: 'osgb', label: 'OSGB 倾斜模型(目录)' },
  { value: 'pointcloud', label: '点云(LAS/LAZ 文件或目录)' },
]

// ---- 本地文件检查结果 ----
const fileInfo = ref(null)      // 栅格 inspect
const vecInfo = ref(null)       // 矢量 inspect
const osgbInfo = ref(null)      // OSGB 目录 inspect({path, warning})
const pcInfo = ref(null)        // 点云 inspect({files, count, bbox, srs, error})
const busy = ref(false)
const errText = ref('')
const dialogOk = ref(false)
const lastAutoName = ref('')

/** 实际生效的 provider:本地栅格由文件判定,其余取用户所选 */
const activeProvider = computed(() => {
  if (isLocalRaster.value) {
    return fileInfo.value?.kind === 'raster_dem' ? 'local_dem' : 'local_image'
  }
  if (isLocal3D.value) {
    return form.d3Type === 'osgb' ? 'local_osgb' : 'local_pointcloud'
  }
  return form.provider
})

const isBuildings = computed(() => ['osm_buildings', 'local_vector', 'overture_buildings']
  .includes(form.provider) && isDownload.value)
const isDem = computed(() => activeProvider.value === 'esri_terrain'
  || activeProvider.value === 'local_dem')
const crsHint = computed(() => isDem.value
  ? DEM_CRS_HINT
  : '默认 WGS84(EPSG:4326)，便于与天地图经纬度成果、TMS 预览和 GIS 软件直接对齐。')

/** 该 provider 可用的阶段(后端驱动) */
const stages = computed(() => caps.value[activeProvider.value]?.stages || [])

/** 格式勾选项:label 用更贴合语境的中文,后端 label 兜底 */
const FMT_LABELS = {
  geotiff: '带坐标 GeoTIFF(每级一张)',
  tms: 'TMS 瓦片(EPSG:4326)',
  osm: 'OSM 瓦片(Web 墨卡托)',
  tiles: '保留原始 LERC 瓦片',
  terrain: 'Cesium 地形切片',
  contour: '等高线(矢量)',
  tile_3d: '3D Tiles',
  dsm: 'DSM(数字表面模型)',
  dem: 'DEM(数字高程模型,仅地面点)',
}
const exportOptions = computed(() => stages.value.map((s) => {
  const v = fmtNameOf(s.key)
  return { value: v, label: FMT_LABELS[v] || s.label }
}))

/**
 * 建筑轮廓矢量的容器选项。建筑管线不走 export 勾选(格式固定 b3dm),但
 * fetch_buildings 阶段会另存一份轮廓矢量,格式可选——清单同样来自后端能力表。
 */
const bldVecOptions = computed(() => {
  const s = stages.value.find((x) => x.key === 'fetch_buildings')
  return (s?.containers || []).map((c) => ({ value: c.key, label: c.label }))
})

// 级别列表按数据源取:Google 到 21、Esri 影像到 19、天地图 18、DEM 0-16
const levelList = computed(() => levelsForProvider(form.provider))
const picksMbtiles = computed(() => ['tms', 'osm'].some(
  (k) => form.export.includes(k) && form.containers[k] === 'mbtiles'))
const tmsSourceStrategyOptions = [
  { value: 'contiguous', label: '连续高层兜底(默认)' },
  { value: 'preserve_inputs', label: '保留每个输入层级并分段补齐' },
]
const showTmsSourceStrategy = computed(() =>
  !isBuildings.value && !isDem.value && form.export.includes('tms'))

function applyAutoName(force = false) {
  const next = defaultTaskName(form.provider)
  if (force || !form.name || form.name === lastAutoName.value) {
    form.name = next
    lastAutoName.value = next
  }
}

function applyDownloadDefaults(forceName = false) {
  const d = downloadDefaultsForProvider(form.provider, stages.value)
  form.levels = d.levels
  form.export = d.export
  form.containers = d.containers
  form.crs = d.crs
  form.annotate = d.annotate
  applyAutoName(forceName)
}

// ---- 选文件 ----
async function browse(pickKind) {
  errText.value = ''
  try {
    // 三维:OSGB 只能选目录;点云可选 LAS/LAZ 文件(多选)或整个目录
    const pick = isLocal3D.value
      ? ((form.d3Type === 'osgb' || pickKind === 'dir')
        ? { kind: 'dir', multiple: false }
        : { kind: 'pointcloud', multiple: true })
      : { kind: isLocalVector.value ? 'vector' : 'raster', multiple: false }
    const d = await api.localPick(pick)
    if (d.paths?.length) { form.path = pick3dPath(d.paths); await inspect() }
  } catch (e) {
    errText.value = '打开文件对话框失败:' + (e?.message || e)
  }
}

/**
 * 点云多选文件 → 共同父目录。系统文件对话框的多选必在同一目录,而后端
 * source_path 只收单个文件或目录——多选时退成按目录处理,由后端递归收集。
 */
function pick3dPath(paths) {
  if (!isLocal3D.value || paths.length <= 1) return paths[0]
  return String(paths[0]).replace(/[\\/][^\\/]+$/, '')
}

/** 三维成果默认名:目录名 / 去扩展名的 LAS 文件名 */
function autoName3d(p) {
  const seg = String(p).replace(/[\\/]+$/, '').split(/[\\/]/).pop() || ''
  return seg.replace(/\.(las|laz)$/i, '')
}

async function inspect() {
  const p = (form.path || '').trim()
  fileInfo.value = null
  vecInfo.value = null
  osgbInfo.value = null
  pcInfo.value = null
  errText.value = ''
  if (!p) return
  busy.value = true
  try {
    if (isLocal3D.value) {
      if (form.d3Type === 'osgb') {
        // inspect_osgb:路径非法/目录无 .osgb 走 HTTP 400,由 catch 进 errText
        const d = await api.localInspectOsgb(p)
        osgbInfo.value = d
        form.export = ['tile_3d']
      } else {
        // inspect_pointcloud 风格不同:「无 LAS」「pdal 不可用」等经 200 响应的
        // error 字段返回(不抛异常),必须显式按 error 分支,不能当成功往下走
        const d = await api.localInspectPointCloud(p)
        pcInfo.value = d
        if (d.error) { errText.value = d.error; return }
        // 换文件后清空旧 CRS 输入:新文件自带 SRS 时必填块隐藏(pcCrsMissing=false),
        // 残留值会被 buildPayload 静默带上,覆盖文件自带的坐标系
        form.pcCrsEpsg = ''
        form.pcCrsLocal = false
        // 默认全选 default_on 的阶段(dsm/dem/tile_3d)
        form.export = stages.value.filter((s) => s.default_on)
          .map((s) => fmtNameOf(s.key))
      }
      if (!form.name) form.name = autoName3d(p)
      return
    }
    if (isLocalVector.value) {
      const d = await api.localInspectVector(p)
      vecInfo.value = d
      form.vecContainer = d.containers?.[0]?.key || 'gpkg'
      if (!form.name) form.name = String(d.filename || '').replace(/\.[^.]+$/, '')
    } else {
      const d = await api.localInspect(p)
      fileInfo.value = d
      form.export = (d.stages || []).filter((s) => s.default_on)
        .map((s) => fmtNameOf(s.key))
      form.containers = { ...form.containers, ...defaultContainersForStages(d.stages || []) }
      if (!form.name) form.name = String(d.filename || '').replace(/\.[^.]+$/, '')
    }
  } catch (e) {
    errText.value = e?.message || String(e)
  } finally {
    busy.value = false
  }
}

/** 点云检查通过(error 为空)才展示信息面板与后续表单项 */
const pcInfoOk = computed(() => !!(pcInfo.value && !pcInfo.value.error))

/** LAS 头无 CRS(srs === null)→ 必须由用户指定:EPSG 代码或按本地坐标 */
const pcCrsMissing = computed(() => isLocal3D.value
  && form.d3Type === 'pointcloud'
  && pcInfoOk.value && pcInfo.value.srs === null)

/** 允许填 4547 或 EPSG:4547,统一剥前缀,提交时再拼 EPSG: */
function pcEpsgOf() {
  return String(form.pcCrsEpsg || '').replace(/^\s*epsg[::]?\s*/i, '').trim()
}

/** 点云 bbox 摘要:pdal 给 {minx..maxz},两位小数够核对量级了 */
function pcBboxText(b) {
  if (!b) return ''
  const f = (v) => Number(v).toFixed(2)
  return `X ${f(b.minx)} ~ ${f(b.maxx)},Y ${f(b.miny)} ~ ${f(b.maxy)},Z ${f(b.minz)} ~ ${f(b.maxz)}`
}

// ---- 级别建议(仅下载)----
const suggest = ref(null)
async function loadSuggest() {
  const b = drawStore.bbox
  if (!b || !isDownload.value || isBuildings.value) { suggest.value = null; return }
  try {
    suggest.value = await api.suggestLevels({
      west: b[0], south: b[1], east: b[2], north: b[3], provider: form.provider,
    })
  } catch (_) { suggest.value = null }
}
function ratioOf(z) {
  return suggest.value?.levels?.find((r) => r.z === z)?.ratio ?? null
}
function lowRatio(z) {
  const r = ratioOf(z)
  return r != null && r < 0.25
}

// ---- DEM 可用最高级别(仅在线地形)----
// Esri Terrain3D 各区域最高 LOD 不同(新疆一带只到 14 级),超限时服务返回
// HTTP 200 + 空瓦片,拼出来是一张全无数据的高程图。这里探测后置灰超限级别。
// null = 探测失败(网络问题),此时不禁用任何级别,由后端提交时兜底下调。
const demMaxLevel = ref(null)
async function loadDemMaxLevel() {
  demMaxLevel.value = null
  const b = drawStore.bbox
  if (!b || !isDownload.value || form.provider !== 'esri_terrain') return
  try {
    const d = await api.demMaxLevel({
      west: b[0], south: b[1], east: b[2], north: b[3], provider: form.provider,
    })
    demMaxLevel.value = d?.max_level ?? null
  } catch (_) { demMaxLevel.value = null }
  // 已勾上的超限级别要摘掉:留着提交也会被后端下调,不如当场如实反映
  if (demMaxLevel.value != null) {
    const kept = form.levels.filter((z) => z <= demMaxLevel.value)
    if (kept.length !== form.levels.length) form.levels = kept
  }
}
// ---- Esri 影像可用最高级别(仅 esri_imagery)----
// (声明与 demMaxLevel 并列放在这里,供下方 reset 提前引用)
// 实测 Esri World Imagery 各区域最高级别不同:城市(含拉萨/乌鲁木齐)到 19、
// 喀什/漠河 18、西藏青海新疆无人区仅 17。超限时返回 200 + 占位图,拼出来是
// 一整片灰色。这里探测后置灰超限级别。
// null = 探测失败(网络/代理问题),此时不禁用任何级别 —— 与 DEM 的约定一致。
const imgMaxLevel = ref(null)
async function loadImgMaxLevel() {
  imgMaxLevel.value = null
  const b = drawStore.bbox
  if (!b || !isDownload.value || !needsRegionProbe(form.provider)) return
  try {
    const d = await api.imageryMaxLevel({
      west: b[0], south: b[1], east: b[2], north: b[3], provider: form.provider,
    })
    imgMaxLevel.value = d?.max_level ?? null
  } catch (_) { imgMaxLevel.value = null }
  // 已勾上的超限级别要摘掉:留着提交也会被后端剔除,不如当场如实反映
  if (imgMaxLevel.value != null) {
    const kept = form.levels.filter((z) => z <= imgMaxLevel.value)
    if (kept.length !== form.levels.length) form.levels = kept
  }
}

function levelUnavailable(z) {
  if (demMaxLevel.value != null && z > demMaxLevel.value) return true
  return imgMaxLevel.value != null && z > imgMaxLevel.value
}

// ---- 各级瓦片数/大小预估 ----
// 每级都要标出大小:级别每加一级瓦片数翻四倍,不显示的话用户很难预判
// 勾到 18 级会下多久、占多大。只按选区算,与勾选无关,故一次取全级别。
const est = ref(null)
async function loadEstimate() {
  const b = drawStore.bbox
  if (!b || !isDownload.value || isBuildings.value) { est.value = null; return }
  try {
    const d = await api.estimate({
      west: b[0], south: b[1], east: b[2], north: b[3],
      levels: levelList.value.join(','), provider: form.provider,
    })
    const m = {}
    for (const r of d.levels || []) m[r.z] = r
    est.value = m
  } catch (_) { est.value = null }
}
function tilesOf(z) { return est.value?.[z]?.tiles ?? null }
function sizeOf(z) {
  const b = est.value?.[z]?.bytes
  return b == null ? '' : fmtSize(b)
}
/** 选区中心纬度:分辨率随纬度收缩,用中心纬比用赤道值贴近实际 */
const centerLat = computed(() => {
  const b = drawStore.bbox
  return b ? (Number(b[1]) + Number(b[3])) / 2 : 0
})
/** 地形=采样间距;影像=像素分辨率。两者同一公式,只是叫法与语义不同 */
function precisionOf(z) {
  return isDem.value
    ? formatSampleSpacing(z, centerLat.value)
    : formatPixelResolution(z, centerLat.value)
}
/** 影像第三列:72DPI 下的比例尺;地形第三列:拼接成果像素尺寸 */
function extraOf(z) {
  const r = est.value?.[z]
  return isDem.value
    ? formatPixelSize(r?.width, r?.height)
    : formatScale72Dpi(z, centerLat.value)
}
/** 级别表头随数据类型变化(地形讲采样间距/尺寸,影像讲分辨率/比例尺) */
const levelColumns = computed(() => (isDem.value
  ? ['高程级别', '采样间距', '总尺寸', '总大小']
  : ['影像级别', '像素分辨率', '比例尺(72DPI)', '总大小']))
function levelTitle(z) {
  if (levelUnavailable(z)) {
    if (imgMaxLevel.value != null && z > imgMaxLevel.value) {
      return `${z} 级:该范围 Esri 影像最高只到 ${imgMaxLevel.value} 级，此级别没有影像数据`
    }
    return `${z} 级:该范围的地形数据源最高只到 ${demMaxLevel.value} 级，此级别没有高程数据`
  }
  const cols = levelColumns.value
  const parts = [`${z} 级`]
  const tiles = tilesOf(z)
  if (tiles != null) parts.push(`${fmtNum(tiles)} 张瓦片`)
  parts.push(`${cols[1]} ${precisionOf(z)}`)
  const extra = extraOf(z)
  if (extra) parts.push(`${cols[2]} ${extra}`)
  const size = sizeOf(z)
  if (size) parts.push(`${cols[3]}约 ${size}`)
  return parts.join(' · ')
}
/** 已勾选级别的合计(瓦片数 + 大小),注记翻倍与后端提交口径一致 */
const estTotal = computed(() => {
  if (!est.value || !form.levels.length) return null
  let tiles = 0
  let bytes = 0
  for (const z of form.levels) {
    const r = est.value[z]
    if (!r) continue
    tiles += r.tiles || 0
    bytes += r.bytes || 0
  }
  // 注记最高 18 级:全部超限时置灰并说明,部分超限则提示影响哪些级别。
  // 不让它变成"勾了没效果"。
  const annotationTip = computed(() => {
    if (!annotationUsable(form.levels)) {
      return `所选级别均高于 ${ANNOTATION_MAX_Z} 级,该范围没有注记数据。`
    }
    const excess = annotationExcessLevels(form.levels)
    if (excess.length) {
      return `注记最高 ${ANNOTATION_MAX_Z} 级,${excess.map((z) => 'z' + z).join('、')} 不会有注记。`
    }
    return '同步下载天地图注记图层并烘焙进成果。'
  })

  const mul = form.annotate && canAnnotate(form.provider) ? 2 : 1
  return { tiles: tiles * mul, bytes: bytes * mul }
})

// ---- 级别勾选 ----
/** 可勾选级别:排除该范围没有数据的 DEM 超限级别 */
const availableLevels = computed(() => levelList.value.filter((z) => !levelUnavailable(z)))
function toggleLevel(z, on) {
  if (levelUnavailable(z)) return
  const s = new Set(form.levels)
  on ? s.add(z) : s.delete(z)
  form.levels = [...s].sort((a, b) => a - b)
}
const allChecked = computed(() => availableLevels.value.length > 0
  && availableLevels.value.every((z) => form.levels.includes(z)))
function toggleAll(on) { form.levels = on ? [...availableLevels.value] : [] }

function resetFormState() {
  form.name = ''
  form.provider = 'tianditu_img'
  form.levels = [...IMG_LEVELS]
  form.export = []
  form.containers = {}
  form.crs = 'EPSG:4326'
  form.clip = true
  form.annotate = false
  form.keepTilesDir = true
  form.tmsSourceStrategy = 'contiguous'
  form.contourInterval = 50
  form.path = ''
  form.useRange = false
  form.vecContainer = 'gpkg'
  form.bldVecContainer = 'gpkg'
  form.d3Type = 'osgb'
  form.pcCrsEpsg = ''
  form.pcCrsLocal = false
  form.pcResolution = 0
  fileInfo.value = null
  vecInfo.value = null
  osgbInfo.value = null
  pcInfo.value = null
  busy.value = false
  errText.value = ''
  lastAutoName.value = ''
  suggest.value = null
  est.value = null
  demMaxLevel.value = null
  imgMaxLevel.value = null
  bldParams.value = null
  bldBbox.value = null
  submitting.value = false
}

async function initForCurrentSource() {
  if (!props.visible) return
  resetFormState()
  await loadCaps()
  dialogOk.value = (await api.localDialogAvailable().catch(() => ({}))).available || false
  if (isDownload.value) {
    applyDownloadDefaults(true)
    await loadSuggest()
    await loadEstimate()
    await loadDemMaxLevel()
    await loadImgMaxLevel()
    return
  }

  // source.path 由「转 COG」等入口预填,此时直接检查、省掉用户再选一次文件
  form.path = props.source?.path || ''
  if (!form.path) return
  await inspect()
  if (props.source?.preferCog && fileInfo.value) {
    // 预置成 COG:这条路径就是为了把非 tiled 的 tif 转成可叠加的 COG
    const key = isDem.value ? 'dem' : 'geotiff'
    form.containers = { ...form.containers, [key]: 'cog' }
    form.export = ['geotiff']
  }
}

// ---- 打开和来源切换时初始化 ----
watch(
  () => [props.visible, props.source, props.source?.kind, props.source?.path, props.source?.preferCog],
  async ([visible]) => {
    if (!visible) return
    await initForCurrentSource()
  },
)

// 换数据源:级别与格式都要按新类型重置(影像 1-18、DEM 0-16,格式清单也不同)
watch(() => form.provider, async () => {
  if (!isDownload.value) return
  applyDownloadDefaults()
  if (['tianditu_img', 'tianditu_vec', 'tianditu_ter'].includes(form.provider)) {
    basemapStore.setKey(form.provider)
  }
  await loadSuggest()
  await loadEstimate()
  await loadDemMaxLevel()
    await loadImgMaxLevel()
})

// 三维来源内切换数据类型:路径与检查结果全部作废,导出勾选待重新检查
watch(() => form.d3Type, () => {
  if (!isLocal3D.value) return
  form.path = ''
  osgbInfo.value = null
  pcInfo.value = null
  form.export = []
  form.pcCrsEpsg = ''
  form.pcCrsLocal = false
  form.name = ''
  errText.value = ''
})

watch(() => drawStore.bbox, async () => {
  await loadSuggest()
  await loadEstimate()
  await loadDemMaxLevel()
    await loadImgMaxLevel()
}, { deep: true })

/** 勾 OSM 自动带上 GeoTIFF:OSM 切片以最高级拼接图作源,后端会直接复用 */
watch(() => form.export, (exp) => {
  if (isDem.value || isBuildings.value || isLocal3D.value) return
  if (exp.includes('osm') && !exp.includes('geotiff')) {
    form.export = ['geotiff', ...exp]
    return
  }
  const levels = ensureImageTmsLevels(form.provider, exp, form.levels)
  if (!form.levels.length && levels.length) {
    form.levels = levels
  }
})

/** ③区(名称+格式)的显示条件:下载始终显示;本地来源要等检查通过 */
const mainReady = computed(() => {
  if (isDownload.value) return true
  if (isLocalRaster.value) return !!fileInfo.value
  if (isLocal3D.value) {
    return form.d3Type === 'osgb' ? !!osgbInfo.value : pcInfoOk.value
  }
  return false
})

// ---- 提交 ----
const bldParams = ref(null)        // BuildingParams 子组件的当前参数
// 本地矢量面自带范围:未画选区时用它提交(后端 bbox 必填,但对本地矢量允许
// 用上传数据自身范围——数据现成,不该强迫用户再框一次)
const bldBbox = ref(null)
const submitting = ref(false)

/** 三类来源的 payload 只在"数据从哪来"上不同,其余参数完全共用 */
function buildPayload() {
  const base = {
    name: form.name || '未命名',
    export: form.export.join(','),
    containers: normalizeContainerMap(form.containers),
    contour_interval: Number(form.contourInterval) || 50,
    keep_tiles_dir: !!form.keepTilesDir,
    crs: form.crs,
    annotate: false,
  }
  if (isLocalRaster.value) {
    return {
      ...base,
      provider: activeProvider.value,
      source_path: form.path,
      bbox: (form.useRange && drawStore.bbox) ? drawStore.bbox : [],
      levels: [],
      tms_source_strategy: form.tmsSourceStrategy,
      geometry: null,
      clip: !!(form.clip && form.useRange && drawStore.bbox),
    }
  }
  if (isLocal3D.value) {
    // 三维:选区/级别/裁切都不适用,后端按 source_path 直接处理
    return {
      ...base,
      provider: activeProvider.value,
      source_path: form.path,
      bbox: [],
      levels: [],
      geometry: null,
      clip: false,
      // 点云坐标系:'' = LAS 头自带;'local' = 按本地坐标;'EPSG:xxxx' = 指定
      pc_crs: form.d3Type === 'osgb' ? ''
        : (form.pcCrsLocal ? 'local' : (pcEpsgOf() ? `EPSG:${pcEpsgOf()}` : '')),
      pc_resolution: Number(form.pcResolution) || 0,
    }
  }
  if (isBuildings.value) {
    return {
      ...base,
      ...(bldParams.value || {}),
      provider: form.provider,
      // 本地矢量未画范围时退到上传数据的 bbox;都没有则给 [0,0,0,0],
      // 后端会按"范围无效"报 400(bbox 是必填 list,送 null 会 422)
      bbox: drawStore.bbox || bldBbox.value || [0, 0, 0, 0],
      levels: [],
      export: 'b3dm',
      geometry: drawStore.geometry || null,
      clip: false,
      // 建筑轮廓矢量的成果格式(fetch_buildings 阶段:geojson/gpkg/shapefile)
      containers: { fetch_buildings: form.bldVecContainer },
    }
  }
  // 下载(影像/地形)
  return {
    ...base,
    provider: form.provider,
    bbox: drawStore.bbox,
    levels: [...form.levels].sort((a, b) => a - b),
    // 勾了裁切就送裁切几何:矩形没有自己的 geometry,clipGeometry 用 bbox 造矩形环
    geometry: (form.clip ? drawStore.clipGeometry : drawStore.geometry) || null,
    clip: !!(form.clip && drawStore.clipGeometry),
    annotate: canAnnotate(form.provider) ? form.annotate : false,
    tms_source_strategy: form.tmsSourceStrategy,
  }
}

// ---- 三维外部工具自检 ----
// 三维处理器是外部 exe / 独立 venv,没装好任务必败,不如提交前就拦下
const TOOL_LABELS = {
  tiles3d: '3dtiles 转换器(tools.tiles3d_exe)',
  pdal: 'PDAL(tools.pdal_exe)',
  py3dtiles: 'py3dtiles(tools.py3dtiles_python)',
}
/** 本次三维任务实际用到的外部工具 */
function required3dTools() {
  // OSGB 固定走 tiles3d;点云按勾选:DEM/DSM 要 PDAL,3D Tiles 要 py3dtiles
  const tools = form.d3Type === 'osgb' ? ['tiles3d'] : []
  if (form.d3Type !== 'osgb') {
    if (form.export.some((f) => ['dsm', 'dem'].includes(f))) tools.push('pdal')
    if (form.export.includes('tile_3d')) tools.push('py3dtiles')
  }
  return tools
}
/** 自检:所需工具任一不可用则中文报错并返回 false(阻止提交) */
async function check3dTools() {
  let diag
  try {
    diag = await api.toolsDiagnose()
  } catch (e) {
    MessagePlugin.error('三维处理器自检失败:' + (e?.message || e))
    return false
  }
  for (const key of required3dTools()) {
    const t = diag[key] || {}
    if (t.configured && t.exists && t.runnable) continue
    MessagePlugin.error(t.configured
      ? `${TOOL_LABELS[key]}不可用:${t.error || '请检查 config.yaml 中的路径配置'}`
      : `${TOOL_LABELS[key]}未配置:请在 config.yaml 的 tools 段填写路径并重启服务`)
    return false
  }
  return true
}

async function submit() {
  // 矢量走单步转换,不进任务队列
  if (isLocalVector.value) {
    if (!vecInfo.value) { MessagePlugin.error('请先选择矢量文件'); return }
    submitting.value = true
    try {
      const r = await api.localConvertVector({
        path: form.path, container: form.vecContainer, name: form.name || '',
      })
      MessagePlugin.success('已转换:' + r.files.join('、'))
      emit('update:visible', false)
    } catch (e) {
      errText.value = '转换失败:' + (e?.message || e)
    } finally { submitting.value = false }
    return
  }

  if (isLocalRaster.value && !fileInfo.value) {
    MessagePlugin.error('请先选择要处理的栅格文件'); return
  }
  if (isLocal3D.value) {
    const ready = form.d3Type === 'osgb' ? !!osgbInfo.value : pcInfoOk.value
    if (!ready) {
      MessagePlugin.error(form.d3Type === 'osgb'
        ? '请先选择 OSGB 目录' : '请先选择点云文件或目录')
      return
    }
    if (pcCrsMissing.value && !form.pcCrsLocal) {
      const epsg = pcEpsgOf()
      if (!epsg) {
        MessagePlugin.error('LAS 文件未携带坐标系信息,请填写 EPSG 代码或勾选「按本地坐标」')
        return
      }
      // 后端 _PC_CRS_RE 只收 EPSG:数字,前端当场拦下省一轮请求
      if (!/^\d+$/.test(epsg)) {
        MessagePlugin.error('EPSG 代码应为纯数字,如 4547')
        return
      }
    }
    // 外部处理器缺失时任务必败,提交前拦下(diagnose 必须先于建任务)
    if (!(await check3dTools())) return
  }
  if (isDownload.value && !drawStore.hasRange && form.provider !== 'local_vector') {
    MessagePlugin.warning('请先在地图上选择范围'); return
  }
  if (!isBuildings.value && !form.export.length) {
    MessagePlugin.error('请至少选择一种导出格式'); return
  }
  // 建筑参数在提交前拦一遍:后端这几项都是 400,但错误信息要用户回到面板找,
  // 不如在原地提示
  if (isBuildings.value) {
    const bp = bldParams.value || {}
    if (form.provider === 'local_vector') {
      if (!bp.upload_id) { MessagePlugin.error('请先上传房屋轮廓矢量面'); return }
      if (bp.height_mode !== 'none' && !bp.height_field) {
        MessagePlugin.error('已选择按字段取高度,请指定高度字段'); return
      }
    }
    if (!drawStore.bbox && !bldBbox.value) {
      MessagePlugin.warning('请先在地图上选择范围'); return
    }
  }
  if (isDownload.value && !isBuildings.value && !form.levels.length) {
    MessagePlugin.error('请至少选择一个级别'); return
  }

  submitting.value = true
  try {
    const created = await taskStore.create(buildPayload())
    // 后端可能因数据源在该范围没有高程数据而下调了级别,如实告知
    if (created?.level_note) MessagePlugin.warning(created.level_note)
    MessagePlugin.success('任务已加入队列')
    emit('created', created)
    emit('update:visible', false)
  } catch (e) {
    MessagePlugin.error('提交失败:' + (e?.message || e))
  } finally { submitting.value = false }
}

const title = computed(() => ({
  download: '下载并处理', local_raster: '处理本地栅格', local_vector: '转换本地矢量',
  local_3d: '处理三维数据',
}[kind.value] || '处理数据'))
</script>

<template>
  <SidePanel :visible="visible" :title="title" side="left" width="400px"
    @update:visible="emit('update:visible', $event)">
    <t-form label-align="top" class="pd">
      <!-- ① 数据:处理什么 -->
      <template v-if="isDownload">
        <t-form-item label="数据源">
          <t-select v-model="form.provider" :options="providerOptions" />
        </t-form-item>
        <t-form-item v-if="!drawStore.hasRange" label-width="0">
          <div class="warn">请先用地图右上的工具画一个范围</div>
        </t-form-item>
      </template>

      <template v-else>
        <t-form-item v-if="isLocal3D" label="数据类型">
          <t-radio-group v-model="form.d3Type" :options="d3TypeOptions" />
        </t-form-item>
        <t-form-item :label="isLocal3D
          ? (form.d3Type === 'osgb' ? 'OSGB 目录' : '点云文件 / 目录') : '源文件'">
          <div class="pick">
            <t-input v-model="form.path"
              :placeholder="isLocal3D && form.d3Type === 'osgb'
                ? '选择或粘贴 OSGB 目录完整路径' : '选择或粘贴文件完整路径'"
              @blur="inspect" @keyup.enter="inspect" />
            <template v-if="dialogOk">
              <t-button v-if="isLocal3D && form.d3Type === 'pointcloud'"
                theme="default" :loading="busy" @click="browse('files')">选文件…</t-button>
              <t-button theme="default" :loading="busy" @click="browse('dir')">
                {{ isLocal3D ? '选目录…' : '浏览…' }}
              </t-button>
            </template>
          </div>
        </t-form-item>
        <t-form-item v-if="fileInfo || vecInfo || osgbInfo || pcInfoOk" label-width="0">
          <div class="info">
            <template v-if="fileInfo">
              <b>{{ isDem ? '高程数据' : '影像数据' }}</b>
              <span v-if="!fileInfo.kind_confident" class="wtag">类型判定不确定,请核对</span>
              <div class="dim">{{ fileInfo.kind_reason }}</div>
              <div class="dim">
                {{ fileInfo.width }}×{{ fileInfo.height }} · {{ fileInfo.bands }} 波段
                {{ fileInfo.dtype }} · {{ fileInfo.crs }} · {{ fmtSize(fileInfo.bytes) }}
              </div>
            </template>
            <template v-else-if="vecInfo">
              <b>矢量数据</b> <span class="dim">{{ vecInfo.geometry_type }}</span>
              <div class="dim">
                {{ vecInfo.features }} 个要素 · {{ vecInfo.fields.length }} 个字段 ·
                {{ vecInfo.crs }} · {{ fmtSize(vecInfo.bytes) }}
              </div>
            </template>
            <template v-else-if="osgbInfo">
              <b>OSGB 倾斜模型</b>
              <div class="dim">{{ osgbInfo.path }}</div>
              <div v-if="osgbInfo.warning" class="wnote">{{ osgbInfo.warning }}</div>
            </template>
            <template v-else-if="pcInfoOk">
              <b>点云数据</b>
              <div class="dim">
                {{ pcInfo.files.length }} 个 LAS/LAZ 文件
                <template v-if="pcInfo.count != null">
                  · 首个文件 {{ fmtNum(pcInfo.count) }} 个点
                </template>
              </div>
              <div v-if="pcInfo.bbox" class="dim">范围 {{ pcBboxText(pcInfo.bbox) }}</div>
              <div class="dim">坐标系 {{ pcInfo.srs || '未知(LAS 文件头未携带)' }}</div>
            </template>
          </div>
        </t-form-item>
        <!-- LAS 头无 CRS:EPSG 或「按本地坐标」必选一个,否则后端无法配准 -->
        <t-form-item v-if="pcCrsMissing" label="点云坐标系(必填)">
          <div class="pc-crs">
            <t-input v-model="form.pcCrsEpsg" placeholder="EPSG 代码,如 4547"
              :disabled="form.pcCrsLocal" style="width: 170px" />
            <t-checkbox v-model="form.pcCrsLocal">按本地坐标</t-checkbox>
            <InfoTip content="LAS 文件头未携带坐标系信息。点云本身是投影坐标(如 CGCS2000 3 度带)时填对应 EPSG 代码;是局部工程坐标(自定义原点)时勾选「按本地坐标」,成果不做坐标配准。"
              max-width="360px" />
          </div>
        </t-form-item>
      </template>

      <t-form-item v-if="errText" label-width="0">
        <div class="err">{{ errText }}</div>
      </t-form-item>

      <!-- ② 矢量:只需选目标格式 -->
      <template v-if="isLocalVector && vecInfo">
        <t-form-item label="成果名称">
          <t-input v-model="form.name" placeholder="成果名称" />
        </t-form-item>
        <t-form-item label="转为格式">
          <t-select v-model="form.vecContainer"
            :options="vecInfo.containers.map((c) => ({ value: c.key, label: c.label }))" />
          <InfoTip content="矢量只做格式转换,不涉及下载与切片,单步完成、不进任务队列。成果统一转为 WGS84 经纬度。"
            max-width="360px" />
        </t-form-item>
      </template>

      <!-- ③ 栅格/下载/三维:名称 + 级别 + 格式 -->
      <template v-if="!isLocalVector && mainReady">
        <t-form-item label="任务名称">
          <t-input v-model="form.name" placeholder="任务名称" />
        </t-form-item>

        <!-- 建筑参数由子组件承载(它有 16 个字段,混进来会让本组件再次膨胀) -->
        <template v-if="isBuildings">
          <BuildingParams v-model="bldParams" :provider="form.provider"
            @update:bbox="bldBbox = $event" />
          <t-form-item label="建筑轮廓矢量格式">
            <t-select v-model="form.bldVecContainer" :options="bldVecOptions" />
            <InfoTip content="白模(3D Tiles)之外,取到的建筑轮廓面会另存一份矢量成果,便于在 GIS 里核对或二次加工。"
              max-width="360px" />
          </t-form-item>
        </template>

        <template v-else>
          <t-form-item v-if="isDownload" label="下载级别" class="level-item">
            <div class="lv-box">
              <div class="lv-head">
              <label class="lv lv-all"><input type="checkbox" :checked="allChecked"
                @change="(e) => toggleAll(e.target.checked)" /> 全选</label>
              <span v-if="suggest?.recommended?.length" class="sug">
                建议 {{ suggest.recommended.join('、') }} 级
                <a @click.prevent="form.levels = [...suggest.recommended]">采用</a>
              </span>
            </div>
            <!-- 四列表格:级别 / 分辨率(采样间距) / 比例尺(总尺寸) / 总大小。
                 只给大小不给分辨率的话,用户没法判断"这一级够不够用" -->
            <div class="lv-cols">
              <span v-for="(c, i) in levelColumns" :key="i" class="lv-col">{{ c }}</span>
            </div>
            <div class="lv-grid">
              <label v-for="z in levelList" :key="z" class="lv"
                :class="{ low: lowRatio(z), off: levelUnavailable(z) }"
                :title="levelTitle(z)">
                <input type="checkbox" :checked="form.levels.includes(z)"
                  :disabled="levelUnavailable(z)"
                  @change="(e) => toggleLevel(z, e.target.checked)" />
                <span class="lv-z">第 {{ z }} 级<span v-if="lowRatio(z)" class="lowtag"
                  :title="`仅 ${(ratioOf(z) * 100).toFixed(1)}% 内容落在选区内`">·</span></span>
                <template v-if="levelUnavailable(z)">
                  <span class="lv-sz">无数据</span><span /><span />
                </template>
                <template v-else>
                  <span class="lv-sz">{{ precisionOf(z) }}</span>
                  <span class="lv-sz">{{ extraOf(z) }}</span>
                  <span class="lv-sz">{{ sizeOf(z) }}</span>
                </template>
              </label>
            </div>
            <div v-if="demMaxLevel != null && demMaxLevel < levelList[levelList.length - 1]"
              class="lv-note">
              该范围的地形数据源最高只到 {{ demMaxLevel }} 级，更高级别没有高程数据，已置灰。
            </div>
            <div v-if="estTotal" class="lv-total">
              已选 {{ form.levels.length }} 级，共 {{ fmtNum(estTotal.tiles) }} 张瓦片，
              约 {{ fmtSize(estTotal.bytes) }}
              <span v-if="form.annotate && canAnnotate(form.provider)" class="dim">（含注记）</span>
            </div>
            </div>
          </t-form-item>
          <t-form-item v-else-if="fileInfo" label-width="0">
            <div class="dim">按文件原生分辨率处理(约 {{ fileInfo.native_level }} 级)</div>
          </t-form-item>

          <t-form-item label="导出格式">
            <!-- OSGB 只有 3D Tiles 一种出路,固定勾选不给改;点云三选一以上 -->
            <t-checkbox v-if="isLocal3D && form.d3Type === 'osgb'"
              :model-value="true" disabled>3D Tiles</t-checkbox>
            <t-checkbox-group v-else v-model="form.export" :options="exportOptions" />
          </t-form-item>
          <ContainerPicker :stages="stages" :selected="form.export"
            v-model="form.containers" />
          <t-form-item v-if="picksMbtiles" label-width="0">
            <t-checkbox v-model="form.keepTilesDir">同时保留散列瓦片目录</t-checkbox>
            <InfoTip content="MBTiles 单文件便于分发;瓦片目录可直接挂 HTTP 服务。两者内容等价,同时保留则瓦片存两遍。"
              max-width="360px" />
          </t-form-item>
          <t-form-item v-if="form.export.includes('contour')" label="等高距(米)">
            <t-input-number v-model="form.contourInterval" :min="1" :max="1000"
              :step="10" theme="column" style="width: 130px" />
          </t-form-item>

          <!-- 高级选项默认折叠:核实过旧版 104 个表单项里大部分是不常改的 -->
          <t-collapse :default-value="[]" class="adv">
            <t-collapse-panel value="adv" header="高级选项">
              <t-form-item v-if="!isLocal3D" label="输出坐标系">
                <t-select v-model="form.crs" :options="crsOpts" filterable />
                <InfoTip :content="crsHint" max-width="360px" />
              </t-form-item>
              <t-form-item v-if="isLocal3D && form.d3Type === 'pointcloud'"
                label="采样分辨率(米)">
                <t-input-number v-model="form.pcResolution" :min="0" :max="1000"
                  :step="0.5" theme="column" style="width: 130px" />
                <InfoTip content="0 = 按点云密度自动估算。DSM/DEM 栅格的像素大小:改大处理更快、改小成果更精细。"
                  max-width="360px" />
              </t-form-item>
              <t-form-item v-if="isLocalRaster && drawStore.hasRange" label-width="0">
                <t-checkbox v-model="form.useRange">只处理所画范围</t-checkbox>
                <InfoTip content="默认处理整幅文件。勾选后只处理文件与所画范围的交集。"
                  max-width="340px" />
              </t-form-item>
              <t-form-item v-if="showTmsSourceStrategy"
                label="TMS 断层策略">
                <t-radio-group v-model="form.tmsSourceStrategy"
                  :options="tmsSourceStrategyOptions" />
                <InfoTip content="连续高层兜底:只使用从最高层开始连续的原始层级,断层后的低层不参与。保留每个输入层级:每个输入 tif 保留自身层级,并向下补到下一个输入层级之上,如 18/17/16/13 会切成 18、17、14-16、1-13。"
                  max-width="420px" />
              </t-form-item>
              <t-form-item v-if="isDownload ? drawStore.clippable : form.useRange"
                label-width="0">
                <t-checkbox v-model="form.clip">裁剪成果到范围边界</t-checkbox>
                <InfoTip content="瓦片是固定网格,边界由级别决定、不会刚好落在选区上——级别越低超出越多。勾选后成果按选区裁切,超出部分透明或裁掉。"
                  max-width="360px" />
              </t-form-item>
              <t-form-item v-if="isDownload && canAnnotate(form.provider)" label-width="0">
                <t-checkbox v-model="form.annotate" :disabled="!annotationUsable(form.levels)">叠加路网注记</t-checkbox>
                <InfoTip :content="annotationTip" max-width="360px" />
              </t-form-item>
            </t-collapse-panel>
          </t-collapse>
        </template>
      </template>

      <!-- 底部操作 -->
      <div class="foot">
        <t-button variant="outline" @click="emit('update:visible', false)">取消</t-button>
        <t-button theme="primary" :loading="submitting" @click="submit">
          {{ isLocalVector ? '开始转换' : '开始处理' }}
        </t-button>
      </div>
    </t-form>
  </SidePanel>
</template>

<style scoped>
.pd { font-size: 13px; }
.pick { display: flex; gap: 6px; width: 100%; }
.pick :deep(.t-input) { flex: 1 1 auto; min-width: 0; }
.info {
  background: #f0f9ff; border: 1px solid #bae6fd; border-radius: 4px;
  padding: 6px 8px; width: 100%; line-height: 1.7;
}
.dim { color: #64748b; font-size: 12px; word-break: break-all; }
.wtag { color: #d97706; font-size: 11px; margin-left: 6px; }
.wnote { color: #d97706; font-size: 12px; line-height: 1.6; }
.pc-crs { display: flex; align-items: center; gap: 10px; width: 100%; }
.warn {
  background: #fffbeb; border: 1px solid #fde68a; border-radius: 4px;
  padding: 6px 8px; color: #92400e; font-size: 12px;
}
.err {
  background: #fef2f2; border: 1px solid #fecaca; border-radius: 4px;
  padding: 6px 8px; color: #d64541; font-size: 12px; line-height: 1.6;
}
.level-item :deep(.t-form__controls-content) {
  display: block; width: 100%; min-width: 0;
}
.lv-box { display: flex; flex-direction: column; width: 100%; min-width: 0; }
.lv-head {
  display: flex; align-items: flex-start; gap: 10px; width: 100%;
  padding-bottom: 6px; border-bottom: 1px solid #eef2f7; margin-bottom: 6px;
  min-width: 0;
}
.lv-all { flex: 0 0 auto; }
.sug {
  font-size: 11px; color: #0369a1; margin-left: auto;
  flex: 1 1 auto; min-width: 0; text-align: right; line-height: 1.6;
}
.sug a { color: #0284c7; cursor: pointer; text-decoration: underline; }
/* 级别自上而下排列,只让级别列表内部滚动;全选/建议和合计固定在外面 */
.lv-grid {
  display: flex; flex-direction: column; gap: 3px; width: 100%;
  max-height: 260px; overflow-y: auto; overflow-x: hidden; padding-right: 4px;
  border: 1px solid #eef2f7; border-radius: 5px; background: #fff;
}
.lv {
  display: inline-flex; align-items: center; gap: 4px; cursor: pointer;
  font-size: 12px; color: #334155;
}
/* 表头与行共用同一套列宽,否则数字与标题对不齐 */
.lv-cols, .lv-grid .lv {
  display: grid;
  grid-template-columns: 22px 62px minmax(0, 1fr) minmax(0, 1fr) minmax(0, 76px);
  align-items: center; min-width: 0;
}
.lv-cols {
  padding: 0 10px 4px 6px; font-size: 11px; color: #94a3b8;
}
/* 表头第一格空出复选框列 */
.lv-cols .lv-col:first-child { grid-column: 1 / span 2; }
.lv-col { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.lv-grid .lv {
  padding: 4px 6px; border-radius: 4px;
}
.lv-grid .lv:hover { background: #f8fafc; }
.lv.low { color: #94a3b8; }
.lv-z { white-space: nowrap; }
.lv-sz {
  white-space: nowrap; color: #64748b; text-align: left;
  overflow: hidden; text-overflow: ellipsis; min-width: 0;
}
.lowtag { color: #d97706; font-weight: 700; }
.lv.off { color: #cbd5e1; cursor: not-allowed; }
.lv.off .lv-sz { color: #cbd5e1; }
.lv-note { margin-top: 6px; color: #d97706; font-size: 12px; line-height: 1.6; }
.lv-total { margin-top: 6px; color: #475569; font-size: 12px; line-height: 1.6; }
.adv { margin-bottom: 10px; }
.adv :deep(.t-collapse-panel__body) { padding: 8px 0 0; }
.foot {
  display: flex; justify-content: flex-end; gap: 8px;
  border-top: 1px solid #eef2f7; padding-top: 12px; margin-top: 4px;
}
</style>
