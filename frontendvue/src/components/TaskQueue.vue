<script setup>
import { onMounted, ref } from 'vue'
import { DialogPlugin, MessagePlugin } from 'tdesign-vue-next'
import { useTaskStore, STATUS_TEXT } from '../stores/task'
import { mapController } from '../composables/mapController'
import { fmtEta, fmtSize } from '../utils/format'
import { isBuildingProvider } from '../utils/provider'
import RedownloadDialog from './RedownloadDialog.vue'
import AddExportDialog from './AddExportDialog.vue'

const taskStore = useTaskStore()

// 重新下载弹窗
const redownloadVisible = ref(false)
const redownloadTask = ref(null)
function openRedownload(t) {
  redownloadTask.value = t
  redownloadVisible.value = true
}

// 补充导出格式弹窗:往原任务追加新阶段,复用已有中间成果、不重新下载。
// 三维建筑的阶段是固定管线(取数→建模→切片),没有"可选格式"的概念,故不提供。
const addExportVisible = ref(false)
const addExportTask = ref(null)
function canAddExport(t) {
  return ['done', 'failed'].includes(t.status) && !isBuildingProvider(t.provider)
}
function openAddExport(t) {
  addExportTask.value = t
  addExportVisible.value = true
}

onMounted(async () => {
  await taskStore.load()
  taskStore.connectWs()
})

const STATUS_THEME = {
  pending: 'warning', running: 'primary', done: 'success',
  failed: 'danger', canceled: 'default', paused: 'default',
}

// 三维建筑底面高模式的中文简称(卡片元信息用)
const BASE_MODE_TEXT = { terrain: '贴地形', flat: '固定0', offset: '统一偏移' }

function pct(t) { return t.total ? Math.round((t.downloaded / t.total) * 100) : 0 }

// 阶段进度条状态色:t-progress 的 status 仅接受 success/error/warning/active。
// 空串表示默认(灰蓝)进度条,用于 pending。
const STAGE_STATUS = {
  pending: '', running: 'active', done: 'success',
  failed: 'error', paused: 'warning', skipped: 'success',
}
function stageStatus(s) { return STAGE_STATUS[s.status] || '' }
const STAGE_STATUS_TEXT = {
  pending: '等待', running: '进行中', done: '完成',
  failed: '失败', paused: '暂停', skipped: '跳过',
}
// 阶段进度条 percentage:skipped/done 记满,其余用 percent
function stagePct(s) {
  if (s.status === 'done' || s.status === 'skipped') return 100
  return Math.round(Number(s.percent) || 0)
}
// 单阶段剩余时间(仅运行中显示)
function stageEta(s) {
  return s.status === 'running' ? fmtEta(s.eta_sec) : ''
}
// 总剩余时间(任务级)
function totalEta(t) { return fmtEta(t.total_eta_sec) }
// 是否显示重试按钮:失败阶段可单独重跑
function canRetryStage(s) { return s.status === 'failed' }

// 续切:保留已切成果,跳过已存在瓦片,只补未完成的
async function onRetryStage(t, s) {
  try { await taskStore.retryStage(t.id, s.key, false) }
  catch (e) { MessagePlugin.error(e?.message || '重试失败') }
}
// 删除并重试:先清空该阶段旧产出,再从头切
async function onPurgeRetryStage(t, s) {
  try { await taskStore.retryStage(t.id, s.key, true) }
  catch (e) { MessagePlugin.error(e?.message || '删除并重试失败') }
}

// 任务类型:三维建筑 / 地形(DEM) / 影像,用于卡片上明显区分
function isBuildings(t) { return isBuildingProvider(t.provider) }
function isDem(t) {
  if (isBuildings(t)) return false
  return String(t.provider || '').startsWith('esri') || String(t.provider || '').includes('terrain')
}
function taskKind(t) { return isBuildings(t) ? '三维' : (isDem(t) ? '地形' : '影像') }
// 卡片配色类名:三维/地形/影像 三色区分
function kindClass(t) { return isBuildings(t) ? 'kind-bld' : (isDem(t) ? 'kind-dem' : 'kind-img') }
function kindTagClass(t) { return isBuildings(t) ? 'bld' : (isDem(t) ? 'dem' : 'img') }

function fmtLevels(t) {
  const lv = (t.levels && t.levels.length) ? [...t.levels].sort((a, b) => a - b)
    : Array.from({ length: t.z_max - t.z_min + 1 }, (_, i) => t.z_min + i)
  if (!lv.length) return '—'
  const continuous = lv.every((z, i) => i === 0 || z === lv[i - 1] + 1)
  return continuous && lv.length > 1 ? `${lv[0]}-${lv[lv.length - 1]}` : lv.join(',')
}

// 是否可预览:任一切片阶段(tms/osm/terrain)已完成即可预览(不必整任务完成)。
// 旧任务无 stages 时回退按 export + done 判断。
function previewable(t) {
  const stages = t.stages || []
  if (stages.length) {
    return stages.some((s) => ['tms', 'osm', 'terrain', 'tile_3d'].includes(s.key)
      && (s.status === 'done' || s.status === 'skipped'))
  }
  if (t.status !== 'done') return false
  const e = String(t.export || '').toLowerCase()
  return e === 'both' || e.includes('tms') || e.includes('osm') || e.includes('terrain')
}
function openPreview(t) {
  window.open(`/preview.html?id=${t.id}`, '_blank')
}

function openDetail(t) {
  taskStore.setActive(t.id)
  mapController.value?.showPreview(t.bbox, t.geometry)
  mapController.value?.zoomTo(t.bbox)
}

async function onPause(id) {
  try { await taskStore.pause(id) } catch (e) { MessagePlugin.error(e?.message || '暂停失败') }
}
async function onResume(id) {
  try { await taskStore.resume(id) } catch (e) { MessagePlugin.error(e?.message || '开始失败') }
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
    if (mapController.value && taskStore.activeId == null) mapController.value.clearPreview()
    MessagePlugin.success('已删除')
  } catch (e) { MessagePlugin.error(e?.message || '删除失败') }
}
</script>

<template>
  <div class="queue">
    <div class="qhead">
      <h1>任务队列</h1>
    </div>
    <div class="list">
      <t-card
        v-for="t in taskStore.tasks" :key="t.id"
        :class="['task', kindClass(t), { active: t.id === taskStore.activeId }]"
        :bordered="true" size="small" @click="openDetail(t)"
      >
        <div class="row1">
          <span :class="['kind-tag', kindTagClass(t)]">{{ taskKind(t) }}</span>
          <span class="name">{{ t.name }}</span>
          <t-tag :theme="STATUS_THEME[t.status] || 'default'" variant="light" size="small">
            {{ STATUS_TEXT[t.status] || t.status }}
          </t-tag>
        </div>
        <!-- 三维建筑无级别/瓦片计数,元信息展示建筑栋数与底面高模式 -->
        <div v-if="isBuildings(t)" class="meta">
          {{ t.building_count ? `${t.building_count} 栋建筑` : '建筑数待定' }}
          · 底面{{ BASE_MODE_TEXT[t.base_height_mode] || t.base_height_mode }}
        </div>
        <div v-else class="meta">
          级别 {{ fmtLevels(t) }} · {{ t.downloaded }}/{{ t.total }}
          <span v-if="t.failed"> · 失败{{ t.failed }}</span>
          <span v-if="t.est_bytes" class="est"> · 预估下载 ~{{ fmtSize(t.est_bytes) }}</span>
        </div>

        <!-- 阶段化进度:每阶段一行,从上到下按执行顺序 -->
        <div v-if="t.stages && t.stages.length" class="stages">
          <div v-for="s in t.stages" :key="s.key" class="stage">
            <div class="stage-head">
              <span class="stage-name">{{ s.label }}</span>
              <span :class="['stage-status', s.status]">{{ STAGE_STATUS_TEXT[s.status] || s.status }}</span>
              <span v-if="stageEta(s)" class="stage-eta">{{ stageEta(s) }}</span>
              <t-button v-if="canRetryStage(s)" size="small" variant="text" theme="primary"
                class="stage-retry" @click.stop="onRetryStage(t, s)">续切</t-button>
              <t-button v-if="canRetryStage(s)" size="small" variant="text" theme="danger"
                class="stage-retry" @click.stop="onPurgeRetryStage(t, s)">删除并重试</t-button>
            </div>
            <t-progress theme="line" :percentage="stagePct(s)"
              :label="`${stagePct(s)}%`" :status="stageStatus(s)" />
          </div>
          <div v-if="totalEta(t) && ['running','pending'].includes(t.status)" class="total-eta">
            预计总剩余:{{ totalEta(t) }}
          </div>
        </div>
        <!-- 旧任务无阶段:回退单进度条 -->
        <t-progress v-else :percentage="pct(t)" :label="true" size="small" />

        <t-space size="small" class="actions" @click.stop>
          <t-button v-if="previewable(t)"
            size="small" variant="outline" theme="success" @click="openPreview(t)">预览</t-button>
          <t-button v-if="['running','pending'].includes(t.status)"
            size="small" variant="outline" @click="onPause(t.id)">暂停</t-button>
          <t-button v-if="['paused','failed','canceled'].includes(t.status)"
            size="small" variant="outline" theme="primary" @click="onResume(t.id)">开始</t-button>
          <t-button v-if="canAddExport(t)"
            size="small" variant="outline" theme="primary"
            @click="openAddExport(t)">补充格式</t-button>
          <t-button v-if="['done','failed','canceled','paused'].includes(t.status)"
            size="small" variant="outline" @click="openRedownload(t)">重新下载</t-button>
          <t-button size="small" variant="outline" theme="danger" @click="onDelete(t)">删除</t-button>
        </t-space>
      </t-card>
      <t-empty v-if="!taskStore.tasks.length" description="暂无任务" />
    </div>

    <RedownloadDialog v-model:visible="redownloadVisible" :task="redownloadTask" />
    <AddExportDialog v-model:visible="addExportVisible" :task="addExportTask" />
  </div>
</template>

<style scoped>
.queue { width: 320px; height: 100%; display: flex; flex-direction: column;
  background: #f8fbff; border-left: 1px solid #e2e8f0; padding: 10px; }
.qhead { display: flex; align-items: center; margin-bottom: 8px; flex: 0 0 auto; }
.qhead h1 { font-size: 15px; font-weight: 700; color: #0369a1; margin: 0; }
.list { flex: 1 1 auto; overflow-y: auto; padding-right: 4px; }
.task { margin-bottom: 8px; cursor: pointer; transition: box-shadow .15s, border-color .15s; }
.task:hover { border-color: #0ea5e9; box-shadow: 0 3px 12px rgba(14,165,233,.16); }
.task.active { border-color: #0ea5e9; box-shadow: 0 3px 14px rgba(14,165,233,.24); }
.row1 { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }
.name { font-weight: 700; color: #334155; font-size: 13px;
  flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 任务类型标识:地形/影像 */
.kind-tag { flex: 0 0 auto; font-size: 11px; font-weight: 700; line-height: 1;
  padding: 3px 6px; border-radius: 4px; color: #fff; }
.kind-tag.dem { background: #b45309; }   /* 地形:琥珀 */
.kind-tag.img { background: #0ea5e9; }   /* 影像:天蓝 */
.kind-tag.bld { background: #7c3aed; }   /* 三维建筑:紫 */
/* 卡片左侧色条,进一步强化区分 */
.task.kind-dem { border-left: 3px solid #b45309; }
.task.kind-img { border-left: 3px solid #0ea5e9; }
.task.kind-bld { border-left: 3px solid #7c3aed; }
.meta { font-size: 12px; color: #94a3b8; margin-bottom: 8px; }
.meta .est { color: #0369a1; }
.actions { margin-top: 10px; }
/* 阶段化进度列表 */
.stages { display: flex; flex-direction: column; gap: 8px; }
.stage-head { display: flex; align-items: center; gap: 6px; margin-bottom: 2px; }
.stage-name { font-size: 12px; color: #475569; font-weight: 600; flex: 1 1 auto;
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stage-status { font-size: 11px; flex: 0 0 auto; color: #94a3b8; }
.stage-status.running { color: #0ea5e9; font-weight: 600; }
.stage-status.done, .stage-status.skipped { color: #16a34a; }
.stage-status.failed { color: #dc2626; font-weight: 600; }
.stage-status.paused { color: #d97706; }
.stage-eta { font-size: 11px; color: #d97706; flex: 0 0 auto; }
.stage-retry { flex: 0 0 auto; padding: 0 4px !important; height: auto !important; }
.total-eta { font-size: 12px; color: #0369a1; font-weight: 600; margin-top: 2px; }
</style>
