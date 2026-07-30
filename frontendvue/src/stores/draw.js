// 选择范围的共享状态:bbox、几何、形状类型
import { defineStore } from 'pinia'

export const useDrawStore = defineStore('draw', {
  state: () => ({
    bbox: null,       // [west, south, east, north] WGS84
    geometry: null,   // geojson 几何(多边形/矢量用于裁剪),矩形为 null
    shape: null,      // 'rect' | 'polygon' | 'vector'
    editing: false,
  }),
  getters: {
    hasRange: (s) => !!s.bbox,
    // 有范围就能裁。矩形同样需要:瓦片是固定网格,边界由级别决定,不可能刚好落在
    // 选区上——低层级尤其明显(天地图第 7 级单张瓦片跨 2.8°,能盖住整个市域)。
    // 不裁的话成果边界是瓦片网格边界,而非用户画的范围。
    clippable: (s) => !!s.bbox,
    // 裁切用的几何:多边形/矢量用自身几何;矩形没有 geometry,用 bbox 现造一个
    // 矩形环。这样后端裁切链路(clip_to_geometry / tile_alpha_mask)不必区分形状。
    clipGeometry: (s) => {
      if (s.geometry) return s.geometry
      if (!s.bbox) return null
      const [w, so, e, n] = s.bbox
      return {
        type: 'Polygon',
        coordinates: [[[w, so], [e, so], [e, n], [w, n], [w, so]]],
      }
    },
  },
  actions: {
    setRange({ bbox, geometry, shape }) {
      this.bbox = bbox
      this.geometry = geometry
      this.shape = shape
    },
    updateBbox(bbox) { this.bbox = bbox },
    updateGeometry(geometry) { this.geometry = geometry },
    setEditing(v) { this.editing = v },
    clear() {
      this.bbox = null
      this.geometry = null
      this.shape = null
      this.editing = false
    },
  },
})
