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
]

export function basemapTypesFor(value) {
  return (BASEMAP_OPTIONS.find((x) => x.value === value) || BASEMAP_OPTIONS[0]).types
}

export function basemapZIndexForLevel(level) {
  return [0, 50, 89][Math.max(0, Math.min(2, Number(level) || 0))]
}
