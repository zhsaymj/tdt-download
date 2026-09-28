export const BASEMAP_OPTIONS = [
  {
    value: 'tianditu_img',
    label: '天地图影像',
    types: ['img_w'],
    removable: false,
    zoomable: false,
  },
  {
    value: 'tianditu_vec',
    label: '天地图矢量底图',
    types: ['vec_w'],
    removable: false,
    zoomable: false,
  },
  {
    value: 'tianditu_ter',
    label: '天地图地形晕渲',
    types: ['ter_w'],
    removable: false,
    zoomable: false,
  },
  // Google / Esri 经后端转发(浏览器无法用后端的代理配置)。
  // types 为空数组:它们不是天地图 WMTS 图层组,由 useMap 走 XYZ 源构造。
  { value: 'google_img', label: 'Google 卫星影像', types: [], removable: true,
    zoomable: true, maxZoom: 21, viaBackend: true },
  { value: 'google_hybrid', label: 'Google 影像(含路网)', types: [], removable: true,
    zoomable: true, maxZoom: 21, viaBackend: true },
  { value: 'google_road', label: 'Google 路线图', types: [], removable: true,
    zoomable: true, maxZoom: 21, viaBackend: true },
  { value: 'google_terrain', label: 'Google 地形', types: [], removable: true,
    zoomable: true, maxZoom: 21, viaBackend: true },
  { value: 'esri_imagery', label: 'Esri World Imagery', types: [], removable: true,
    zoomable: true, maxZoom: 19, viaBackend: true },
]

export function basemapTypesFor(value) {
  return (BASEMAP_OPTIONS.find((x) => x.value === value) || BASEMAP_OPTIONS[0]).types
}

export function basemapZIndexForLevel(level) {
  return [0, 50, 89][Math.max(0, Math.min(2, Number(level) || 0))]
}

/**
 * 经后端转发的底图瓦片 URL 模板;不需要转发的返回 null。
 *
 * 后端转发的原因:浏览器无法使用后端的代理配置,而 Google/Esri 直连不通。
 * 天地图不走这里 —— 它由前端直连(有自己的 basemap_token 机制)。
 */
export function basemapTileUrl(value) {
  const opt = BASEMAP_OPTIONS.find((x) => x.value === value)
  if (!opt || !opt.viaBackend) return null
  return `/api/tiles/${value}/{z}/{x}/{y}`
}

/** 底图的最大缩放级别;未声明时按天地图的 18。 */
export function basemapMaxZoom(value) {
  const opt = BASEMAP_OPTIONS.find((x) => x.value === value)
  return (opt && opt.maxZoom) || 18
}

/**
 * 底图 → 配对的天地图注记图层(3857 网格)。
 *
 * 与后端 ANNOTATION_OF 同语义:影像配影像注记(cia)、矢量配矢量注记(cva)、
 * 地形配地形注记(cta)。Google/Esri 都是影像源,故配 cia。
 *
 * 用 `_w`(Web 墨卡托)而非 `_c`:前端地图视图本身是 3857
 * (见 useMap 的 fromLonLat),且 Google/Esri 底图也是 3857。
 *
 * 注记不再与底图绑在一起显示(见 BASEMAP_OPTIONS 的 types),
 * 改由图层面板的独立开关控制。
 */
const ANNOTATION_OF_BASEMAP = {
  tianditu_img: 'cia_w',
  tianditu_vec: 'cva_w',
  tianditu_ter: 'cta_w',
  google_img: 'cia_w',
  google_hybrid: 'cia_w',
  google_road: 'cia_w',
  google_terrain: 'cia_w',
  esri_imagery: 'cia_w',
}

/** 底图配对的注记图层名(如 'cia_w');无配对返回 null。 */
export function annotationLayerOf(basemapKey) {
  return ANNOTATION_OF_BASEMAP[basemapKey] || null
}
