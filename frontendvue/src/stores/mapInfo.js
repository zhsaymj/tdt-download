import { defineStore } from 'pinia'

/**
 * 地图实时信息:当前视口层级 / 比例尺 / 鼠标经纬度。
 *
 * 单独一个轻量 store,而不是塞进 basemap —— basemap 管"配置 + 下发地图",
 * 这里只是地图被动上报的显示状态,职责不同。生产者是 MapView(onInfo 回调),
 * 消费者是 AppStatusBar(底部状态条)。
 */
export const useMapInfoStore = defineStore('mapInfo', {
  state: () => ({
    zoom: '—',
    scale: '—',
    lon: null,
    lat: null,
  }),
  actions: {
    set({ zoom, scale, lon, lat }) {
      this.zoom = zoom
      this.scale = scale
      this.lon = lon
      this.lat = lat
    },
  },
})
