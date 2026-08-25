<script setup>
/**
 * 状态栏:一行显示当前任务进度,取代原先常驻 320px 的任务队列。
 *
 * 只显示"正在跑的那一个"——队列里其余任务与历史记录点「任务」菜单看。旧版把
 * 完整队列常驻,但大部分时间它在显示已完成任务的历史进度条,不值得占那么宽。
 */
import { computed } from 'vue'
import { useTaskStore } from '../stores/task'
import { fmtEta } from '../utils/format'

const emit = defineEmits(['open-tasks'])
const taskStore = useTaskStore()

/** 当前该展示的任务:优先运行中,其次排队中 */
const current = computed(() => {
  const ts = taskStore.tasks
  return ts.find((t) => t.status === 'running')
    || ts.find((t) => t.status === 'pending')
    || null
})

/** 运行中任务的当前阶段(阶段化进度比总进度更能说明"正在做什么") */
const stage = computed(() => {
  const t = current.value
  if (!t) return null
  return (t.stages || []).find((s) => s.status === 'running') || null
})

const pct = computed(() => {
  const s = stage.value
  if (s) return Math.round(s.percent || 0)
  const t = current.value
  return t ? Math.round(t.progress || 0) : 0
})

const pendingCount = computed(() => taskStore.tasks.filter(
  (t) => t.status === 'pending').length)
const doneCount = computed(() => taskStore.tasks.filter(
  (t) => t.status === 'done').length)
const failedCount = computed(() => taskStore.tasks.filter(
  (t) => t.status === 'failed').length)
</script>

<template>
  <footer class="statusbar">
    <span class="item conn" :class="{ on: taskStore.wsConnected }">
      <span class="dot"></span>
    </span>

    <!-- 有任务在跑:显示它的阶段与进度 -->
    <template v-if="current">
      <button class="cur" @click="emit('open-tasks')">
        <b>{{ current.name }}</b>
        <span class="stage">{{ stage ? stage.label : '等待中' }}</span>
        <span class="bar"><i :style="{ width: pct + '%' }"></i></span>
        <span class="pct">{{ pct }}%</span>
        <!-- fmtEta 的返回值已含「剩」字,这里不要再加 -->
        <span v-if="stage?.eta_sec" class="eta">{{ fmtEta(stage.eta_sec) }}</span>
      </button>
      <span v-if="pendingCount > 1" class="item dim">
        另有 {{ pendingCount - (current.status === 'pending' ? 1 : 0) }} 个排队
      </span>
    </template>
    <template v-else>
      <span class="item dim">就绪</span>
    </template>

    <span class="spacer"></span>
    <button class="item link" @click="emit('open-tasks')">
      已完成 {{ doneCount }}<template v-if="failedCount">
        · <span class="fail">失败 {{ failedCount }}</span>
      </template>
    </button>
  </footer>
</template>

<style scoped>
.statusbar {
  flex: 0 0 28px; height: 28px;
  display: flex; align-items: center; gap: 10px;
  padding: 0 12px;
  background: #f1f5f9; border-top: 1px solid #e2e8f0;
  font-size: 12px; color: #64748b;
  user-select: none;
}
.item { display: inline-flex; align-items: center; gap: 5px; }
.dim { color: #94a3b8; }
.spacer { flex: 1 1 auto; }
.conn .dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: #94a3b8; transition: background .2s;
}
.conn.on .dot { background: #22c55e; box-shadow: 0 0 0 3px rgba(34, 197, 94, .18); }

.cur {
  display: inline-flex; align-items: center; gap: 8px;
  border: 0; background: transparent; cursor: pointer;
  font: inherit; color: #334155; padding: 2px 6px; border-radius: 4px;
  max-width: 60%; overflow: hidden;
}
.cur:hover { background: rgba(148, 163, 184, .18); }
.cur b { font-weight: 600; white-space: nowrap; }
.stage { color: #64748b; white-space: nowrap; }
.bar {
  width: 120px; height: 5px; border-radius: 3px; background: #e2e8f0;
  overflow: hidden; flex: 0 0 auto;
}
.bar i { display: block; height: 100%; background: #0284c7; transition: width .3s; }
.pct { color: #0369a1; font-variant-numeric: tabular-nums; }
.eta { color: #94a3b8; white-space: nowrap; }
.link {
  border: 0; background: transparent; cursor: pointer; font: inherit;
  color: #64748b; padding: 2px 6px; border-radius: 4px;
}
.link:hover { background: rgba(148, 163, 184, .18); color: #334155; }
.fail { color: #d64541; }
</style>
