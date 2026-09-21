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

/**
 * 叠加图层的 key。来源有两种,前缀不同,值域天然不重叠。
 *
 * **拆成两个函数而不是复用同一个换语义**:服务图层没有 taskId,若共用一个函数
 * 靠调用方传不同含义的第一个参数,`_fallbackBbox` 里 `it.taskId` 的判断会失效
 * ——服务图层会去查一个不存在的任务。
 */
export function taskOverlayKey(taskId, layerId) { return `task:${taskId}:${layerId}` }
export function serviceOverlayKey(serviceId) { return `svc:${serviceId}` }

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
     *
     * src 是来源标识:`{ taskId, taskName }` 或 `{ serviceId, serviceName }`。
     * 两种来源的图层在后续所有操作里一视同仁(透明度/层级/定位都由通用实现
     * 提供),只有"范围从哪来"这一点不同。
     */
    add(src, desc) {
      const map = mapController.value?.map
      if (!map) return false
      const key = src.serviceId
        ? serviceOverlayKey(src.serviceId)
        : taskOverlayKey(src.taskId, desc.id)
      if (this.has(key)) return true
      if (!addOverlay(map, key, desc)) return false
      const first = !this.items.length
      this.items = [...this.items, {
        key,
        taskId: src.taskId || '',
        serviceId: src.serviceId || '',
        taskName: src.taskName || src.serviceName || '',
        desc, visible: true, opacity: 1,
      }]
      this._sync()
      if (first) this.zoomTo(key)
      return true
    },

    /**
     * 图层自身没有范围时的兜底:用所属任务的 bbox(任务必有范围)。
     *
     * **服务图层没有兜底**——服务侧的 desc 必须自带 bounds_wgs84(由后端
     * overlay_desc 保证),取不到就是取不到,不去猜。
     */
    _fallbackBbox(key) {
      const it = this.items.find((x) => x.key === key)
      if (!it || !it.taskId) return null
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

    /**
     * 服务被移除时清掉它的图层。
     *
     * 不清的话会留下"看着正常、实际已无数据源"的幽灵图层——已加载的瓦片
     * 仍在显示,未加载的静默失败,用户很难想到成因是服务已经没了。
     */
    removeByService(serviceId) {
      for (const it of this.items.filter((x) => x.serviceId === serviceId)) {
        removeOverlay(mapController.value?.map, it.key)
      }
      this.items = this.items.filter((it) => it.serviceId !== serviceId)
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
