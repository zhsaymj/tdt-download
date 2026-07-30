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
    clippable: (s) => s.shape === 'polygon' || s.shape === 'vector',
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
