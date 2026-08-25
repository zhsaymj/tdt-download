export const IMG_LEVELS = Array.from({ length: 18 }, (_, i) => i + 1)
export const DEM_LEVELS = Array.from({ length: 17 }, (_, i) => i)

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
  const exportFormats = defaultExportForStages(stages)
  return {
    name: defaultTaskName(provider, date),
    levels: defaultLevelsForProvider(provider, exportFormats),
    export: exportFormats,
    containers: defaultContainersForStages(stages),
    crs: defaultCrsForProvider(provider),
    annotate: defaultAnnotateForProvider(provider),
  }
}

export const DEM_CRS_HINT = 'DEM 原始缓存与拼接中间图仍按 Esri 瓦片网格使用 EPSG:3857；最终 GeoTIFF 默认重投影为 WGS84(EPSG:4326)。Cesium 地形切片阶段会单独准备 EPSG:4326 高程源。'
