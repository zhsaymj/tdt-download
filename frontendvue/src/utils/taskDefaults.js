export const IMG_LEVELS = Array.from({ length: 18 }, (_, i) => i + 1)
export const DEM_LEVELS = Array.from({ length: 17 }, (_, i) => i)

const WEB_MERCATOR_EQUATOR_RESOLUTION_M = 156543.03392804097
const WEB_MERCATOR_LAT_LIMIT = 85.05112878

const PROVIDER_LABELS = {
  tianditu_img: '天地图影像',
  tianditu_vec: '天地图矢量底图',
  tianditu_ter: '天地图地形晕渲',
  esri_terrain: '全国地形DEM',
  osm_buildings: '三维建筑白模',
  local_vector: '本地矢量白模',
  local_image: '本地影像',
  local_dem: '本地地形',
}

export function isDemProvider(provider) {
  return provider === 'esri_terrain' || provider === 'local_dem'
}

export function isTiandituRasterProvider(provider) {
  return ['tianditu_img', 'tianditu_vec', 'tianditu_ter'].includes(provider)
}

function pad2(v) { return String(v).padStart(2, '0') }

export function formatTimestamp(date = new Date()) {
  return `${date.getFullYear()}${pad2(date.getMonth() + 1)}${pad2(date.getDate())}_` +
    `${pad2(date.getHours())}${pad2(date.getMinutes())}${pad2(date.getSeconds())}`
}

export function defaultTaskName(provider, date = new Date()) {
  return `${PROVIDER_LABELS[provider] || '下载任务'}_${formatTimestamp(date)}`
}

function fmtNameOf(stageKey) { return stageKey === 'dem' ? 'geotiff' : stageKey }

export function containerKeyOf(container) {
  if (!container) return ''
  return typeof container === 'string' ? container : (container.key || '')
}

export function normalizeContainerMap(containers = {}) {
  const out = {}
  for (const [stage, value] of Object.entries(containers || {})) {
    const key = containerKeyOf(value)
    if (key) out[stage] = key
  }
  return out
}

export function defaultExportForStages(stages = []) {
  return stages.filter((s) => s.default_on).map((s) => fmtNameOf(s.key))
}

function ensureDemDefaultExports(provider, stages = [], exportFormats = []) {
  if (!isDemProvider(provider)) return exportFormats
  const stageKeys = new Set(stages.map((s) => s.key))
  const out = [...exportFormats]
  if (stageKeys.has('dem') && !out.includes('geotiff')) out.unshift('geotiff')
  if (stageKeys.has('terrain') && !out.includes('terrain')) out.push('terrain')
  return out
}

export function defaultContainersForStages(stages = []) {
  const out = {}
  for (const stage of stages) {
    const containers = (stage.containers || []).map(containerKeyOf).filter(Boolean)
    if (!containers.length) continue
    if ((stage.key === 'geotiff' || stage.key === 'dem') && containers.includes('cog')) {
      out[stage.key] = 'cog'
    } else {
      out[stage.key] = containers[0]
    }
  }
  return out
}

export function ensureImageTmsLevels(provider, exportFormats = [], levels = []) {
  if (isDemProvider(provider)) return levels
  if (!exportFormats.includes('tms')) return levels
  return levels.length ? levels : [...IMG_LEVELS]
}

export function defaultLevelsForProvider(provider, exportFormats = []) {
  if (isDemProvider(provider)) return []
  return ensureImageTmsLevels(provider, exportFormats, [...IMG_LEVELS])
}

export function defaultCrsForProvider() {
  return 'EPSG:4326'
}

export function defaultAnnotateForProvider(provider) {
  return isTiandituRasterProvider(provider)
}

export function downloadDefaultsForProvider(provider, stages = [], date = new Date()) {
  const exportFormats = ensureDemDefaultExports(provider, stages, defaultExportForStages(stages))
  return {
    name: defaultTaskName(provider, date),
    levels: defaultLevelsForProvider(provider, exportFormats),
    export: exportFormats,
    containers: defaultContainersForStages(stages),
    crs: defaultCrsForProvider(provider),
    annotate: defaultAnnotateForProvider(provider),
  }
}

/**
 * 第 level 级单像素代表的地面距离(米)。
 *
 * 地形(EPSG:3857)与影像(天地图 4326)两套网格的横向分辨率恰好同式:
 * 4326 第 z 级单像素 360/2^z/256 度,乘 111320·cos(lat) 得 156543.75·cos(lat)/2^z,
 * 与墨卡托赤道分辨率 156543.03·cos(lat)/2^z 只差 0.0005%,故共用一个函数。
 */
export function levelResolutionMeters(level, latitude = 0) {
  const z = Math.max(0, Number(level) || 0)
  const lat = Math.max(-WEB_MERCATOR_LAT_LIMIT, Math.min(WEB_MERCATOR_LAT_LIMIT, Number(latitude) || 0))
  return WEB_MERCATOR_EQUATOR_RESOLUTION_M * Math.cos(lat * Math.PI / 180) / (2 ** z)
}

function formatMeters(value) {
  if (value >= 100) return String(Math.round(value))
  if (value >= 10) return value.toFixed(1)
  return value.toFixed(2)
}

/** 地形:每个高程采样点之间的地面间距 */
export function formatSampleSpacing(level, latitude = 0) {
  return `${formatMeters(levelResolutionMeters(level, latitude))} 米`
}

/** 影像:单像素地面分辨率 */
export function formatPixelResolution(level, latitude = 0) {
  return `${formatMeters(levelResolutionMeters(level, latitude))} 米`
}

// 72 DPI 屏幕上单像素的物理尺寸(米):1 英寸 = 0.0254 m
const SCREEN_PIXEL_M_72DPI = 0.0254 / 72

/** 影像:该级在 72DPI 下的地图比例尺分母(1:N) */
export function formatScale72Dpi(level, latitude = 0) {
  const denom = levelResolutionMeters(level, latitude) / SCREEN_PIXEL_M_72DPI
  return `1:${Math.round(denom).toLocaleString('en-US')}`
}

/** 拼接成果的像素尺寸,如 4096×2048 */
export function formatPixelSize(width, height) {
  if (!width || !height) return ''
  return `${width}×${height}`
}

export const DEM_CRS_HINT = 'DEM 原始缓存与拼接中间图仍按 Esri 瓦片网格使用 EPSG:3857；最终 GeoTIFF 默认重投影为 WGS84(EPSG:4326)。Cesium 地形切片阶段会单独准备 EPSG:4326 高程源。'
