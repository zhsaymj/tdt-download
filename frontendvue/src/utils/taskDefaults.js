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

export function terrainPrecisionMeters(level, latitude = 0) {
  const z = Math.max(0, Number(level) || 0)
  const lat = Math.max(-WEB_MERCATOR_LAT_LIMIT, Math.min(WEB_MERCATOR_LAT_LIMIT, Number(latitude) || 0))
  return WEB_MERCATOR_EQUATOR_RESOLUTION_M * Math.cos(lat * Math.PI / 180) / (2 ** z)
}

function formatMeters(value) {
  if (value >= 100) return String(Math.round(value))
  if (value >= 10) return value.toFixed(1)
  return value.toFixed(2)
}

export function formatTerrainPrecision(level, latitude = 0) {
  return `精度约 ${formatMeters(terrainPrecisionMeters(level, latitude))} 米/像素`
}

export const DEM_CRS_HINT = 'DEM 原始缓存与拼接中间图仍按 Esri 瓦片网格使用 EPSG:3857；最终 GeoTIFF 默认重投影为 WGS84(EPSG:4326)。Cesium 地形切片阶段会单独准备 EPSG:4326 高程源。'
