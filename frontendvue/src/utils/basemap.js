export const BASEMAP_OPTIONS = [
  {
    value: 'tianditu_img',
    label: '天地图影像(叠加路网注记)',
    types: ['img_w', 'cia_w'],
    removable: false,
    zoomable: false,
  },
  {
    value: 'tianditu_vec',
    label: '天地图矢量底图(叠加路网注记)',
    types: ['vec_w', 'cva_w'],
    removable: false,
    zoomable: false,
  },
  {
    value: 'tianditu_ter',
    label: '天地图地形晕渲',
    types: ['ter_w', 'cta_w'],
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
