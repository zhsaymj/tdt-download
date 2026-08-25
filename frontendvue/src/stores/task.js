// 任务列表状态 + WebSocket 实时进度
import { defineStore } from 'pinia'
import { MessagePlugin } from 'tdesign-vue-next'
import { api } from '../api'

export const STATUS_TEXT = {
  pending: '排队中', running: '下载中', done: '已完成',
  failed: '失败', canceled: '已取消', paused: '已暂停',
}

export const useTaskStore = defineStore('task', {
  state: () => ({
    tasks: [],          // 任务数组(按创建时间倒序,后端已排序)
    activeId: null,     // 详情面板当前任务 id
    ws: null,
    wsConnected: false, // WS 连接状态(供状态栏指示灯)
    logs: [],           // 运行日志(最近若干条,WS 实时追加)
  }),
  getters: {
    activeTask: (s) => s.tasks.find((t) => t.id === s.activeId) || null,
    byId: (s) => (id) => s.tasks.find((t) => t.id === id) || null,
  },
  actions: {
    async load() {
      this.tasks = await api.listTasks()
    },
    async retryStage(id, key, purge = false) { await api.retryStage(id, key, purge); await this.load() },
    async loadLogs() {
      try { this.logs = (await api.getLogs(400)).logs || [] } catch (_) { /* ignore */ }
    },
    async create(payload) {
      const created = await api.createTask(payload)
      await this.load()
      this.activeId = created?.id || this.tasks[0]?.id || null
      return created
    },
    async updateParams(id, payload) {
      await api.updateTask(id, payload)
      await this.load()
    },
    async pause(id) { await api.pauseTask(id); await this.load() },
    async resume(id) { await api.resumeTask(id); await this.load() },
    async remove(id, purge) {
      await api.deleteTask(id, purge)
      this.tasks = this.tasks.filter((t) => t.id !== id)
      if (this.activeId === id) this.activeId = null
    },
    setActive(id) { this.activeId = id },
    clearActive() { this.activeId = null },

    // WebSocket:实时更新进度/状态
    connectWs() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      const ws = new WebSocket(`${proto}://${location.host}/ws/progress`)
      ws.onopen = () => { this.wsConnected = true }
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data)
        if (msg.type === 'ping') return
        // tk 使用池提示(切换/全池用尽):不关联具体任务,单独弹提示
        if (msg.type === 'token') {
          if (msg.event === 'exhausted') MessagePlugin.warning(msg.message, 6000)
          else if (msg.message) MessagePlugin.info(msg.message)
          return
        }
        // 运行日志:实时追加到日志缓冲(上限 500 条,防止无限增长)
        if (msg.type === 'log') {
          this.logs.push({ ts: msg.ts, level: msg.level, msg: msg.msg })
          if (this.logs.length > 500) this.logs.splice(0, this.logs.length - 500)
          return
        }
        const t = this.tasks.find((x) => x.id === msg.id)
        if (!t) { this.load(); return }
        if (msg.type === 'progress') {
          t.downloaded = msg.downloaded
          t.failed = msg.failed
          t.total = msg.total
          if (msg.message) t.message = msg.message
        } else if (msg.type === 'stage') {
          // 阶段化进度:整组阶段 + 总剩余时间
          t.stages = msg.stages
          t.total_eta_sec = msg.total_eta_sec
        } else if (msg.type === 'task') {
          t.status = msg.status
          if (msg.message) t.message = msg.message
          if (msg.output_path) t.output_path = msg.output_path
          // 终态清除总剩余时间;stages 由 stage 消息维护
          if (['done', 'failed', 'canceled'].includes(msg.status)) t.total_eta_sec = null
        }
      }
      ws.onclose = () => { this.wsConnected = false; setTimeout(() => this.connectWs(), 2000) }
      this.ws = ws
    },
  },
})
