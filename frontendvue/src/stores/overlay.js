// 当前叠加在地图上的图层。
//
// 为什么要提到 store:加/删图层的入口在「数据与成果」面板(那里有搜索与筛选,
// 找得到东西),而可见性/透明度/顺序的控制在左下图层面板。两个组件读写同一份
// 状态,放在任一组件的局部 ref 里都要靠 props 往上下传。
//
// items 的数组顺序就是绘制顺序:末尾在最上面。图层面板从上往下渲染时反转一次,
// 与 GIS 软件的图层树习惯一致(列表最上面 = 图上最上面)。
import { defineStore } from 'pinia'
import { mapController } from '../composables/mapController'
import {
  addOverlay, removeOverlay, applyOrder,
  setOverlayVisible, setOverlayOpacity, zoomToOverlay,
} from '../composables/overlays'
import { useTaskStore } from './task'

/** key 约定:任务 id + 图层 id,跨任务唯一 */
export function overlayKey(taskId, layerId) { return `${taskId}:${layerId}` }

export const useOverlayStore = defineStore('overlay', {
  state: () => ({
    /** { key, taskId, taskName, desc, visible, opacity } 的有序数组 */
    items: [],
  }),
  getters: {
    keys: (s) => s.items.map((it) => it.key),
    has: (s) => (key) => s.items.some((it) => it.key === key),
    count: (s) => s.items.length,
  },
  actions: {
    /** 重排地图上的 zIndex,使其与 items 顺序一致。每次增删/移序后都要调 */
    _sync() {
      applyOrder(this.items.map((it) => it.key))
    },

    /**
     * 叠加一份成果图层。返回是否成功(不可叠加的 kind 会失败,由调用方提示)。
     * 首个图层自动定位;已有图层时不动视野——叠第二个图层的目的就是同视野对比,
     * 把视野拽走反而妨碍。
     */
    add(taskId, taskName, desc) {
      const map = mapController.value?.map
      if (!map) return false
      const key = overlayKey(taskId, desc.id)
      if (this.has(key)) return true
      if (!addOverlay(map, key, desc)) return false
      const first = !this.items.length
      this.items = [...this.items, {
        key, taskId, taskName, desc, visible: true, opacity: 1,
      }]
      this._sync()
      if (first) this.zoomTo(key)
      return true
    },

    /** 图层自身没有范围时的兜底:用所属任务的 bbox(任务必有范围) */
    _fallbackBbox(key) {
      const it = this.items.find((x) => x.key === key)
      if (!it) return null
      const t = useTaskStore().byId(it.taskId)
      const b = t?.bbox
      return Array.isArray(b) && b.length === 4 ? b : null
    },

    remove(key) {
      const map = mapController.value?.map
      removeOverlay(map, key)
      this.items = this.items.filter((it) => it.key !== key)
      this._sync()
    },

    /** 任务被删除时清掉它的图层,否则图层会留在地图上且再也无法从界面移除 */
    removeByTask(taskId) {
      for (const it of this.items.filter((x) => x.taskId === taskId)) {
        removeOverlay(mapController.value?.map, it.key)
      }
      this.items = this.items.filter((it) => it.taskId !== taskId)
      this._sync()
    },

    setVisible(key, on) {
      const it = this.items.find((x) => x.key === key)
      if (!it) return
      it.visible = !!on
      setOverlayVisible(key, it.visible)
    },

    setOpacity(key, v) {
      const it = this.items.find((x) => x.key === key)
      if (!it) return
      it.opacity = v
      setOverlayOpacity(key, v)
    },

    /** 上移 = 往数组末尾挪(更靠上层) */
    moveUp(key) { this._swap(key, +1) },
    moveDown(key) { this._swap(key, -1) },
    _swap(key, dir) {
      const i = this.items.findIndex((x) => x.key === key)
      const j = i + dir
      if (i < 0 || j < 0 || j >= this.items.length) return
      const next = [...this.items]
      ;[next[i], next[j]] = [next[j], next[i]]
      this.items = next
      this._sync()
    },

    /** 定位到图层范围;返回是否成功(失败由调用方提示,不能静默) */
    zoomTo(key) {
      return zoomToOverlay(mapController.value?.map, key, this._fallbackBbox(key))
    },

    clear() {
      const map = mapController.value?.map
      for (const it of this.items) removeOverlay(map, it.key)
      this.items = []
    },
  },
})
