<script setup>
/**
 * 地形任务列表面板:只展示 DEM 来源且导出含 terrain 的任务(不展示该用户在
 * 主下载工具里建的其他 DEM 导出任务,如只导出 geotiff/等高线的)。
 * 进度展示参照 TaskQueue.vue 的阶段进度条,但只保留「预览」「删除」两个操作——
 * 其余管理功能(暂停/补充导出/重新下载)仍去主任务队列做。
 */
import { computed, onMounted } from 'vue'
import { DialogPlugin, MessagePlugin } from 'tdesign-vue-next'
import { useTaskStore, STATUS_TEXT } from '../stores/task'
import { isDemProvider } from '../utils/provider'
import { fmtEta } from '../utils/format'

const taskStore = useTaskStore()

onMounted(async () => {
  await taskStore.load()
  taskStore.connectWs()
})

const STATUS_THEME = {
  pending: 'warning', running: 'primary', done: 'success',
  failed: 'danger', canceled: 'default', paused: 'default',
}

// 只看地形类任务:DEM 来源 + export 含 terrain(其余 DEM 导出如纯 geotiff 不展示)
const terrainTasks = computed(() => taskStore.tasks.filter(
  (t) => isDemProvider(t.provider) && String(t.export || '').includes('terrain')))

const STAGE_STATUS = {
  pending: '', running: 'active', done: 'success',
  failed: 'error', paused: 'warning', skipped: 'success',
}
function stageStatus(s) { return STAGE_STATUS[s.status] || '' }
const STAGE_STATUS_TEXT = {
  pending: '等待', running: '进行中', done: '完成',
  failed: '失败', paused: '暂停', skipped: '跳过',
}
function stagePct(s) {
  if (s.status === 'done' || s.status === 'skipped') return 100
  return Math.round(Number(s.percent) || 0)
}
function stageEta(s) { return s.status === 'running' ? fmtEta(s.eta_sec) : '' }
function totalEta(t) { return fmtEta(t.total_eta_sec) }
function pct(t) { return t.total ? Math.round((t.downloaded / t.total) * 100) : 0 }

function openPreview(t) {
  window.open(`/preview.html?id=${t.id}`, '_blank')
}

function onDelete(t) {
  const dlg = DialogPlugin.confirm({
    header: '删除任务',
    body: '是否同时删除已导出的成果目录及文件?\n\n「确定」= 删除目录和文件\n「取消」= 仅删任务记录,保留文件',
    confirmBtn: '删记录+文件',
    cancelBtn: '仅删记录',
    onConfirm: async () => { await doDelete(t.id, true); dlg.destroy() },
    onCancel: async () => { await doDelete(t.id, false); dlg.destroy() },
  })
}
async function doDelete(id, purge) {
  try {
    await taskStore.remove(id, purge)
    MessagePlugin.success('已删除')
  } catch (e) { MessagePlugin.error(e?.message || '删除失败') }
}
</script>

<template>
  <div class="list">
    <t-card
      v-for="t in terrainTasks" :key="t.id"
      class="task" :bordered="true" size="small"
    >
      <div class="row1">
        <span class="name">{{ t.name }}</span>
        <t-tag :theme="STATUS_THEME[t.status] || 'default'" variant="light" size="small">
          {{ STATUS_TEXT[t.status] || t.status }}
        </t-tag>
      </div>

      <div v-if="t.stages && t.stages.length" class="stages">
        <div v-for="s in t.stages" :key="s.key" class="stage">
          <div class="stage-head">
            <span class="stage-name">{{ s.label }}</span>
            <span :class="['stage-status', s.status]">{{ STAGE_STATUS_TEXT[s.status] || s.status }}</span>
            <span v-if="stageEta(s)" class="stage-eta">{{ stageEta(s) }}</span>
          </div>
          <t-progress theme="line" :percentage="stagePct(s)"
            :label="`${stagePct(s)}%`" :status="stageStatus(s)" />
        </div>
        <div v-if="totalEta(t) && ['running', 'pending'].includes(t.status)" class="total-eta">
          预计总剩余:{{ totalEta(t) }}
        </div>
      </div>
      <t-progress v-else :percentage="pct(t)" :label="true" size="small" />

      <t-space size="small" class="actions">
        <t-button v-if="t.status === 'done'"
          size="small" variant="outline" theme="success" @click="openPreview(t)">预览</t-button>
        <t-button size="small" variant="outline" theme="danger" @click="onDelete(t)">删除</t-button>
      </t-space>
    </t-card>
    <t-empty v-if="!terrainTasks.length" description="暂无地形任务" />
  </div>
</template>

<style scoped>
.list { display: flex; flex-direction: column; gap: 8px; padding: 10px; overflow-y: auto; height: 100%; }
.task { flex: 0 0 auto; }
.row1 { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }
.name { font-weight: 700; color: #334155; font-size: 13px;
  flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stages { display: flex; flex-direction: column; gap: 8px; margin-bottom: 8px; }
.stage-head { display: flex; align-items: center; gap: 6px; margin-bottom: 2px; }
.stage-name { font-size: 12px; color: #475569; font-weight: 600; flex: 1 1 auto;
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stage-status { font-size: 11px; flex: 0 0 auto; color: #94a3b8; }
.stage-status.running { color: #0ea5e9; font-weight: 600; }
.stage-status.done, .stage-status.skipped { color: #16a34a; }
.stage-status.failed { color: #dc2626; font-weight: 600; }
.stage-status.paused { color: #d97706; }
.stage-eta { font-size: 11px; color: #d97706; flex: 0 0 auto; }
.total-eta { font-size: 12px; color: #0369a1; font-weight: 600; margin-bottom: 8px; }
.actions { margin-top: 4px; }
</style>
