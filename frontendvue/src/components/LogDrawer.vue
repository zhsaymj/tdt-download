<script setup>
// 运行日志抽屉:实时显示后端处理过程,由顶栏触发
import { ref, nextTick, watch } from 'vue'
import { useTaskStore } from '../stores/task'

const visible = defineModel('visible', { default: false })
const taskStore = useTaskStore()
const logBox = ref(null)

function scrollToBottom() {
  const el = logBox.value
  if (el) el.scrollTop = el.scrollHeight
}

// 抽屉打开时拉取历史日志并滚到底
watch(visible, async (v) => {
  if (!v) return
  await taskStore.loadLogs()
  await nextTick()
  scrollToBottom()
})

// 打开状态下新日志到达自动滚底
watch(() => taskStore.logs.length, () => {
  if (visible.value) nextTick(scrollToBottom)
})
</script>

<template>
  <t-drawer v-model:visible="visible" header="运行日志" size="520px" :footer="false">
    <div class="log-tip">实时显示后端处理过程(下载/合并/切片)。也可在后端目录 data/logs/app.log 查看完整日志。</div>
    <div ref="logBox" class="log-box">
      <div v-for="(l, i) in taskStore.logs" :key="i" :class="['log-line', l.level]">
        <span class="log-ts">{{ l.ts }}</span>
        <span class="log-lv">{{ l.level }}</span>
        <span class="log-msg">{{ l.msg }}</span>
      </div>
      <t-empty v-if="!taskStore.logs.length" description="暂无日志" />
    </div>
  </t-drawer>
</template>

<style scoped>
.log-tip { font-size: 12px; color: #64748b; margin-bottom: 8px; line-height: 1.6; }
.log-box { height: calc(100vh - 130px); overflow-y: auto; background: #0f172a;
  border-radius: 8px; padding: 10px; font-family: Consolas, monospace; font-size: 12px; }
.log-line { display: flex; gap: 8px; padding: 1px 0; color: #cbd5e1; line-height: 1.6;
  white-space: pre-wrap; word-break: break-all; }
.log-ts { color: #64748b; flex: 0 0 auto; }
.log-lv { flex: 0 0 auto; width: 48px; color: #38bdf8; }
.log-line.WARNING .log-lv { color: #fbbf24; }
.log-line.ERROR .log-lv, .log-line.CRITICAL .log-lv { color: #f87171; }
.log-msg { flex: 1 1 auto; }
</style>
