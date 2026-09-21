/**
 * 本地数据服务的状态。
 *
 * 服务是**持久配置**（落 sqlite），不是临时会话——其他服务要拿去当地图数据源
 * 用，地址必须跨重启不变。
 *
 * 与任务图层的关系：服务图层最终以与任务图层**完全相同的 desc 结构**进入
 * overlays.js（由后端 GET /api/services 的 overlay_desc 字段给出），从而零改动
 * 复用既有的透明度/层级/定位能力。
 */
import { defineStore } from 'pinia'

/** 服务类别 -> 标签底色（沿用项目既有配色令牌，不引入新配色） */
export const KIND_TAG_STYLE = {
  model: { background: '#ede9fe', color: '#5b21b6' },
  imagery: { background: '#e0f2fe', color: '#0369a1' },
  vector: { background: '#fef3c7', color: '#92400e' },
  terrain: { background: '#dcfce7', color: '#15803d' },
}

/** 统一的 JSON 请求小工具：失败时把后端的 detail 文案抛出来 */
async function reqJson(url, options) {
  const r = await fetch(url, options)
  const d = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(d.detail || `请求失败（${r.status}）`)
  return d
}

export const useServiceStore = defineStore('service', {
  state: () => ({
    /** 已注册的服务（后端顺序：创建时间倒序） */
    items: [],
    /** { [id]: { ok, reason } } 探活结果 */
    health: {},
    /** 扫 output 得到的未注册候选 */
    candidates: [],
    loading: false,
    error: '',
  }),
  getters: {
    /** 已开启的服务（预览页/主界面只列这些——关掉的服务选中会立刻 403） */
    enabled: (s) => s.items.filter((it) => it.enabled),
    /** 失效的服务：源目录已不存在 */
    broken: (s) => s.items.filter((it) => s.health[it.id] && !s.health[it.id].ok),
    countEnabled: (s) => s.items.filter((it) => it.enabled).length,
    isBroken: (s) => (id) => !!(s.health[id] && !s.health[id].ok),
  },
  actions: {
    /**
     * 访问地址 = 主机名 + 后端给的路径。
     *
     * 主机名由前端补：后端只知道 server.host（可能是 127.0.0.1），而实际访问者
     * 可能是局域网 IP 或域名。
     *
     * 瓦片模板里的 {z}{x}{y} **原样保留**——整体 encodeURI 会把花括号转义成
     * %7B%7D，前端拿到就不认识这个模板了。
     */
    accessUrl(svc) {
      const origin = typeof location !== 'undefined' ? location.origin : ''
      return origin + (svc?.access_path || '')
    },

    async fetchAll() {
      this.loading = true
      this.error = ''
      try {
        const d = await reqJson('/api/services')
        this.items = d.services || []
      } catch (e) {
        this.error = e?.message || String(e)
        this.items = []
      } finally {
        this.loading = false
      }
    },

    /** 打开面板时批量探活一次（纯本地 exists()，毫秒级），不做常驻轮询 */
    async fetchHealth() {
      try {
        const d = await reqJson('/api/services/health')
        this.health = d.health || {}
      } catch (_) { /* 探活失败不阻断面板 */ }
    },

    async fetchCandidates() {
      try {
        const d = await reqJson('/api/services/candidates')
        this.candidates = d.candidates || []
      } catch (_) {
        this.candidates = []
      }
    },

    async scanDir(path) {
      const d = await reqJson('/api/services/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      })
      return d.candidates || []
    },

    async register(cand, name = '') {
      const d = await reqJson('/api/services', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: name || cand.label || '',
          kind: cand.kind, root: cand.root, entry: cand.entry || '',
          grid: cand.grid || '', flip_y: !!cand.flip_y,
          minzoom: cand.minzoom ?? 0, maxzoom: cand.maxzoom ?? 18,
          bounds_wgs84: cand.bounds_wgs84 || null,
          bounds_approx: !!cand.bounds_approx,
          tile_ext: cand.tile_ext || 'png',
          source: cand.source || 'manual',
        }),
      })
      await this.fetchAll()
      await this.fetchHealth()
      return d
    },

    async setEnabled(id, enabled) {
      await reqJson(`/api/services/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      })
      await this.fetchAll()
    },

    async rename(id, name) {
      await reqJson(`/api/services/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      })
      await this.fetchAll()
    },

    /** 只删注册记录，后端不动磁盘文件 */
    async remove(id) {
      await reqJson(`/api/services/${id}`, { method: 'DELETE' })
      this.items = this.items.filter((it) => it.id !== id)
      delete this.health[id]
    },

    /**
     * 移除服务并同步摘掉它在地图上的图层。
     *
     * 服务移除后端点即 404，留着图层会让它变成"看着正常、实际已无数据源"的
     * 幽灵图层（已加载的瓦片仍在显示，未加载的静默失败）。UI 的移除按钮
     * 一律走这个方法。
     */
    async removeAndDetach(id) {
      await this.remove(id)
      // 动态 import 避免 store 之间形成循环依赖
      const { useOverlayStore } = await import('./overlay')
      useOverlayStore().removeByService(id)
    },

    /** 清理全部失效服务。调用方需先提醒用户：外接盘未挂载会造成假性失效 */
    async removeAllBroken() {
      const ids = this.broken.map((it) => it.id)
      for (const id of ids) {
        try { await this.removeAndDetach(id) } catch (_) { /* 继续清下一个 */ }
      }
      return ids.length
    },
  },
})
