<script setup>
// 底部状态栏:WS 连接指示 + 任务统计 + 端口
import { computed } from 'vue'
import { useTaskStore } from '../stores/task'

const taskStore = useTaskStore()

const runningCount = computed(
  () => taskStore.tasks.filter((t) => ['running', 'pending'].includes(t.status)).length,
)
const doneCount = computed(
  () => taskStore.tasks.filter((t) => t.status === 'done').length,
)
const port = computed(() => location.port || (location.protocol === 'https:' ? '443' : '80'))
</script>

<template>
  <footer class="statusbar">
    <span class="item conn" :class="{ on: taskStore.wsConnected }">
      <span class="dot"></span>
      {{ taskStore.wsConnected ? '已连接' : '连接中…' }}
    </span>
    <span class="sep">·</span>
    <span class="item">运行中 {{ runningCount }}</span>
    <span class="sep">·</span>
    <span class="item">已完成 {{ doneCount }}</span>
    <span class="spacer"></span>
    <span class="item">端口 :{{ port }}</span>
  </footer>
</template>

<style scoped>
.statusbar {
  flex: 0 0 26px; height: 26px;
  display: flex; align-items: center; gap: 8px;
  padding: 0 12px;
  background: #f1f5f9; border-top: 1px solid #e2e8f0;
  font-size: 12px; color: #64748b;
  user-select: none;
}
.item { display: inline-flex; align-items: center; gap: 5px; }
.sep { color: #cbd5e1; }
.spacer { flex: 1 1 auto; }
.conn .dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: #94a3b8; transition: background .2s;
}
.conn.on .dot { background: #22c55e; box-shadow: 0 0 0 3px rgba(34, 197, 94, .18); }
.conn.on { color: #16a34a; }
</style>
