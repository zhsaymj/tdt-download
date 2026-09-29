import { defineStore } from 'pinia'
import { mapController } from '../composables/mapController'

export const useBasemapStore = defineStore('basemap', {
  state: () => ({
    key: 'tianditu_img',
    opacity: 1,
    level: 0,
    // 路网注记开关。默认开启:天地图底图原本"影像+注记"一起显示,
    // 拆开后不默认开会让用户觉得"路网没了"。
    annotationVisible: true,
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
      ctrl.setAnnotationVisible?.(this.annotationVisible)
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
    setAnnotationVisible(on) {
      this.annotationVisible = !!on
      this.apply()
    },
  },
})
