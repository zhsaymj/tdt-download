import { defineStore } from 'pinia'
import { mapController } from '../composables/mapController'

export const useBasemapStore = defineStore('basemap', {
  state: () => ({
    key: 'tianditu_img',
    opacity: 1,
    level: 0,
  }),
  getters: {
    opacityPct: (s) => Math.round(s.opacity * 100),
  },
  actions: {
    apply() {
      const ctrl = mapController.value
      if (!ctrl) return
      ctrl.setBasemap?.(this.key)
      ctrl.setBasemapOpacity?.(this.opacity)
      ctrl.setBasemapLevel?.(this.level)
    },
    setKey(key) {
      this.key = key || 'tianditu_img'
      this.apply()
    },
    setOpacityPct(pct) {
      this.opacity = Math.min(1, Math.max(0, Number(pct) / 100))
      this.apply()
    },
    move(delta) {
      this.level = Math.max(0, Math.min(2, this.level + delta))
      this.apply()
    },
  },
})
