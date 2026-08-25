<script setup>
/**
 * 数据与成果面板:统一列出三类东西,每条给出"能对它做什么"的入口。
 *
 *   ① 当前选区    地图上画的范围 → 可直接下载、或作为本地文件的处理范围
 *   ② 成果         已完成任务的产物 → 可叠加到地图、补充格式、三维预览、打开目录、删除
 *
 * 与图层面板的分工按**对象**切分,不按动作切分:这里是"从一堆成果里找到要的那份,
 * 然后对它做点什么"(找东西需要的搜索、类型筛选、级别、大小都在这儿),图层面板管
 * "已经叠上去的那几个图层怎么看"(可见性、透明度、上下遮盖)。
 *
 * 早先反过来:图层面板列全部成果的全部图层,75 个成果两百多行还没有搜索,
 * 而这里只放操作按钮。结果是找图层的地方没有筛选,能筛选的地方不能叠图层。
 */
import { computed, ref, watch } from 'vue'
import { DialogPlugin, MessagePlugin } from 'tdesign-vue-next'
import { useDrawStore } from '../stores/draw'
import { useTaskStore } from '../stores/task'
import { useOverlayStore, overlayKey } from '../stores/overlay'
import { fmtSize } from '../utils/format'
import { taskKindOf, isBuildingProvider } from '../utils/provider'
import { mapController } from '../composables/mapController'
import { api } from '../api'
import SidePanel from './SidePanel.vue'
import AddExportDialog from './AddExportDialog.vue'

const props = defineProps({ visible: { type: Boolean, default: false } })
const emit = defineEmits(['update:visible', 'process', 'convert-cog'])

const drawStore = useDrawStore()
const taskStore = useTaskStore()
const overlayStore = useOverlayStore()

// ---- 选区 ----
const SHAPE_TEXT = { rect: '矩形', polygon: '多边形', vector: '导入矢量' }
const bboxText = computed(() => {
  const b = drawStore.bbox
  if (!b) return ''
  return `${b[0].toFixed(4)}, ${b[1].toFixed(4)} — ${b[2].toFixed(4)}, ${b[3].toFixed(4)}`
})
/** 选区跨度(度)。粗略给个量级,让用户判断范围是否画错了数量级 */
const spanText = computed(() => {
  const b = drawStore.bbox
  if (!b) return ''
  return `跨 ${(b[2] - b[0]).toFixed(3)}° × ${(b[3] - b[1]).toFixed(3)}°`
})

// ---- 成果 ----
const kw = ref('')
const KIND_FILTER = [
  { value: 'all', label: '全部' },
  { value: 'image', label: '影像' },
  { value: 'dem', label: '地形' },
  { value: 'buildings', label: '三维' },
]
const kindFilter = ref('all')

const KIND_TEXT = { image: '影像', dem: '地形', buildings: '三维' }

/** 有成果的任务:失败任务也可能有部分成果,一并列出 */
const results = computed(() => {
  const k = kw.value.trim().toLowerCase()
  return taskStore.tasks.filter((t) => {
    if (!['done', 'failed'].includes(t.status)) return false
    if (kindFilter.value !== 'all' && taskKindOf(t) !== kindFilter.value) return false
    if (k && !String(t.name || '').toLowerCase().includes(k)) return false
    return true
  })
})

/** taskId → size 明细。按需拉取:81 个任务全扫一遍磁盘要好几秒 */
const sizes = ref({})
async function loadSize(t) {
  if (sizes.value[t.id]) return
  try {
    const d = await api.taskSize(t.id)
    sizes.value = { ...sizes.value, [t.id]: d }
  } catch (_) { /* 目录已被手工删掉时静默 */ }
}

/**
 * taskId → 可叠加图层清单。同样按需拉取,理由和 size 一样:全量扫 81 个任务的
 * 成果目录(还要读栅格元信息)要好几秒。
 *
 * 失败只记在该任务名下、在展开区里显示一行可重试的提示,不弹 toast——早先展开面板
 * 时会把所有任务挨个请求一遍,一次接口异常就叠出几十个 toast 盖住半个地图。
 */
const layers = ref({})
const layerErr = ref({})
async function loadLayers(t, force = false) {
  if (layers.value[t.id] && !force) return
  layerErr.value = { ...layerErr.value, [t.id]: null }
  try {
    const d = await api.taskLayers(t.id)
    layers.value = { ...layers.value, [t.id]: d.layers || [] }
  } catch (e) {
    layerErr.value = { ...layerErr.value, [t.id]: e?.message || String(e) }
  }
}

/** 展开的成果 id(展开才拉大小明细与图层清单) */
const openId = ref(null)
async function toggleOpen(t) {
  if (openId.value === t.id) { openId.value = null; return }
  openId.value = t.id
  // 两个请求互不依赖,并发发出;allSettled 保证一个失败不影响另一个
  await Promise.allSettled([loadSize(t), loadLayers(t)])
}

// ---- 叠加到地图 ----
/** 可勾选叠加的类型(preview3d 是三维数据,没有二维表示) */
function overlayable(L) { return L.kind !== 'preview3d' }
/** 范围框类型缺 bounds 时无从画起,禁掉勾选 */
function overlayDisabled(L) {
  return L.kind === 'raster_only_bbox' && !L.bounds_wgs84
}
function layerOn(t, L) { return overlayStore.has(overlayKey(t.id, L.id)) }
function toggleLayer(t, L, on) {
  if (on) {
    if (!overlayStore.add(t.id, t.name, L)) {
      MessagePlugin.warning('该图层无法叠加显示')
    }
  } else {
    overlayStore.remove(overlayKey(t.id, L.id))
  }
}

/** 图层类型的中文说明 */
const LAYER_KIND_TEXT = {
  tiles: '瓦片',
  cog: '栅格',
  vector: '矢量',
  vector_convert: '矢量(需转换)',
  raster_only_bbox: '仅范围框',
  preview3d: '三维',
}

function fmtLevels(t) {
  const lv = (t.levels && t.levels.length) ? [...t.levels].sort((a, b) => a - b) : []
  if (!lv.length) return ''
  const continuous = lv.every((z, i) => i === 0 || z === lv[i - 1] + 1)
  return continuous && lv.length > 1 ? `${lv[0]}-${lv[lv.length - 1]} 级` : `${lv.join(',')} 级`
}

/** 该任务是否有可三维预览的成果(瓦片化数据才有意义) */
function previewable(t) {
  return (t.stages || []).some(
    (s) => ['tms', 'osm', 'terrain', 'tile_3d'].includes(s.key)
      && ['done', 'skipped'].includes(s.status))
}
function openPreview(t) { window.open(`/preview.html?id=${t.id}`, '_blank') }

/** 定位到成果范围:顺带把范围画出来,便于确认位置 */
function locate(t) {
  mapController.value?.showPreview(t.bbox, t.geometry)
  mapController.value?.zoomTo(t.bbox)
}

/** 清除选区:store 与地图上的图形都要清,只清 store 图形会留在地图上 */
function clearRange() {
  drawStore.clear()
  mapController.value?.clearDraw()
  mapController.value?.clearPreview()
}

/** 用该成果的范围作为新的选区:常见诉求是"在同一范围换个数据源再下一次" */
function useAsRange(t) {
  if (!t.bbox || t.bbox.length !== 4) { MessagePlugin.warning('该成果没有范围信息'); return }
  drawStore.setRange({
    bbox: [...t.bbox],
    geometry: t.geometry || null,
    shape: t.geometry ? 'vector' : 'rect',
  })
  mapController.value?.loadGeojson(t.geometry || {
    type: 'Feature', properties: {},
    geometry: {
      type: 'Polygon',
      coordinates: [[
        [t.bbox[0], t.bbox[1]], [t.bbox[2], t.bbox[1]],
        [t.bbox[2], t.bbox[3]], [t.bbox[0], t.bbox[3]], [t.bbox[0], t.bbox[1]],
      ]],
    },
  })
  mapController.value?.zoomTo(t.bbox)
  MessagePlugin.success('已把该成果范围设为当前选区')
}

// 补充导出格式:往原任务追加阶段,复用已有瓦片缓存与合并成果,不重新下载。
// 三维建筑是固定管线(取数→建模→切片),没有"可选格式"的概念,故不提供。
const addExportVisible = ref(false)
const addExportTask = ref(null)
function canAddExport(t) {
  return ['done', 'failed'].includes(t.status) && !isBuildingProvider(t.provider)
}
function openAddExport(t) {
  addExportTask.value = t
  addExportVisible.value = true
}

// 旧版裁剪影像会把 nodata 写成 0,QGIS 中会把合法的深色像素误渲成白点。
// 修复是原地改元数据 + 写掩膜,放在成果动作里手动触发。
const repairing = ref({})
function canRepairNodata(t) {
  return ['done', 'failed'].includes(t.status)
    && taskKindOf(t) !== 'buildings'
    && !!t.output_path
}
function onRepairNodata(t) {
  const dlg = DialogPlugin.confirm({
    header: '修复影像白点',
    body: '将原地修复该任务输出目录中的旧版 RGB GeoTIFF：清除 nodata=0 并写入掩膜,不重写像素。大图可能需要几十秒。',
    confirmBtn: '开始修复',
    cancelBtn: '取消',
    onConfirm: async () => {
      dlg.destroy()
      await doRepairNodata(t)
    },
  })
}
async function doRepairNodata(t) {
  repairing.value = { ...repairing.value, [t.id]: true }
  try {
    const r = await api.repairNodata(t.id)
    const fixed = Number(r.fixed || 0)
    const px = Number(r.recovered_pixels || 0).toLocaleString('zh-CN')
    if (fixed > 0) {
      MessagePlugin.success(`已修复 ${fixed} 个 GeoTIFF,恢复 ${px} 个像素`)
    } else {
      MessagePlugin.info('未发现需要修复的旧版 GeoTIFF')
    }
    const nextSizes = { ...sizes.value }
    delete nextSizes[t.id]
    sizes.value = nextSizes
    if (openId.value === t.id) {
      await Promise.allSettled([loadSize(t), loadLayers(t, true)])
    }
  } catch (e) {
    MessagePlugin.error('修复失败:' + (e?.message || e))
  } finally {
    const next = { ...repairing.value }
    delete next[t.id]
    repairing.value = next
  }
}

async function reveal(t) {
  if (!t.output_path) { MessagePlugin.warning('该任务没有输出目录'); return }
  try {
    await api.revealPath(t.output_path)
  } catch (e) {
    MessagePlugin.error('打开目录失败:' + (e?.message || e))
  }
}

function onDelete(t) {
  const dlg = DialogPlugin.confirm({
    header: '删除成果',
    body: '是否同时删除磁盘上的成果目录及文件?\n\n'
      + '「删记录+文件」= 连同 ' + (t.output_path || '输出目录') + ' 一起删除,不可恢复\n'
      + '「仅删记录」= 只从列表移除,文件保留在磁盘上',
    confirmBtn: '删记录+文件',
    cancelBtn: '仅删记录',
    onConfirm: async () => { await doDelete(t.id, true); dlg.destroy() },
    onCancel: async () => { await doDelete(t.id, false); dlg.destroy() },
  })
}
async function doDelete(id, purge) {
  try {
    await taskStore.remove(id, purge)
    // 该成果的图层要从地图上撤掉:成果记录已经没了,留在图上的图层再也无法
    // 通过界面找到并移除(图层面板只按 store 渲染,而添加入口在这份列表里)
    overlayStore.removeByTask(id)
    if (openId.value === id) openId.value = null
    MessagePlugin.success(purge ? '已删除记录与文件' : '已删除记录,文件保留')
  } catch (e) {
    MessagePlugin.error('删除失败:' + (e?.message || e))
  }
}

// 面板打开时刷新一次:任务可能在面板关闭期间跑完了
watch(() => props.visible, (v) => { if (v) taskStore.load().catch(() => {}) })
</script>

<template>
  <SidePanel :visible="visible" title="数据与成果" side="right" width="440px"
    @update:visible="emit('update:visible', $event)">
    <div class="dd">
      <!-- ===== ① 当前选区 ===== -->
      <div class="sec">当前选区</div>
      <div v-if="drawStore.hasRange" class="card range">
        <div class="r1">
          <span class="tag rt">{{ SHAPE_TEXT[drawStore.shape] || '范围' }}</span>
          <span class="span">{{ spanText }}</span>
        </div>
        <div class="dim mono">{{ bboxText }}</div>
        <div class="ops">
          <t-button size="small" theme="primary"
            @click="emit('process', { kind: 'download' })">下载此范围</t-button>
          <t-button size="small" variant="outline"
            @click="mapController?.zoomTo(drawStore.bbox)">定位</t-button>
          <t-button size="small" variant="text" theme="danger"
            @click="clearRange">清除</t-button>
        </div>
      </div>
      <div v-else class="card hint">
        还没有选区。用地图右上的工具画一个矩形/多边形,或导入矢量边界。
        <div class="ops">
          <t-button size="small" variant="outline"
            @click="emit('process', { kind: 'local_raster' })">处理本地栅格</t-button>
          <t-button size="small" variant="outline"
            @click="emit('process', { kind: 'local_vector' })">转换本地矢量</t-button>
        </div>
      </div>

      <!-- ===== ② 成果 ===== -->
      <div class="sec">
        成果
        <span class="cnt">{{ results.length }}</span>
      </div>
      <div class="filter">
        <t-input v-model="kw" size="small" placeholder="按名称搜索" clearable />
        <t-radio-group v-model="kindFilter" size="small" variant="default-filled"
          :options="KIND_FILTER" />
      </div>

      <div v-for="t in results" :key="t.id" class="card res" :class="{ open: openId === t.id }">
        <div class="r1" @click="toggleOpen(t)">
          <span class="tag" :class="taskKindOf(t)">{{ KIND_TEXT[taskKindOf(t)] }}</span>
          <b class="nm" :title="t.name">{{ t.name }}</b>
          <span v-if="t.status === 'failed'" class="tag bad">部分失败</span>
          <span class="fold">{{ openId === t.id ? '▾' : '▸' }}</span>
        </div>
        <div class="dim meta">
          <span v-if="fmtLevels(t)">{{ fmtLevels(t) }}</span>
          <span>{{ t.export || '—' }}</span>
          <span>{{ (t.created_at || '').replace('T', ' ').slice(0, 16) }}</span>
        </div>

        <template v-if="openId === t.id">
          <div class="dim path" :title="t.output_path">{{ t.output_path || '(无输出目录)' }}</div>
          <!-- 成果大小明细:哪类占了多少,便于判断该删哪个 -->
          <div v-if="sizes[t.id]" class="sizes">
            <div v-for="it in sizes[t.id].items" :key="it.key" class="szrow">
              <span class="szl">{{ it.label }}</span>
              <span class="szv">{{ fmtSize(it.bytes) }}</span>
            </div>
            <div class="szrow total">
              <span class="szl">合计</span>
              <span class="szv">{{ fmtSize(sizes[t.id].total_bytes) }}</span>
            </div>
            <div v-if="!sizes[t.id].items.length" class="dim">
              目录内没有成果文件(可能已被手工删除)
            </div>
          </div>

          <!-- 叠加到地图:勾上即加入图层面板,那里再调可见性/透明度/上下遮盖 -->
          <div class="lsec">叠加到地图</div>
          <div v-if="layerErr[t.id]" class="lerr">
            读取图层失败:{{ layerErr[t.id] }}
            <button class="link" @click="loadLayers(t, true)">重试</button>
          </div>
          <template v-else-if="layers[t.id]">
            <div v-for="L in layers[t.id]" :key="L.id" class="lrow">
              <!-- 三维成果没有二维表示,只能开预览窗口 -->
              <template v-if="!overlayable(L)">
                <span class="tag t3d">{{ LAYER_KIND_TEXT[L.kind] || L.kind }}</span>
                <span class="llbl" :title="L.label">{{ L.label }}</span>
                <t-button size="small" variant="text" theme="success"
                  @click="openPreview(t)">预览</t-button>
              </template>
              <template v-else>
                <label class="lchk">
                  <input type="checkbox" :checked="layerOn(t, L)"
                    :disabled="overlayDisabled(L)"
                    @change="(e) => toggleLayer(t, L, e.target.checked)" />
                  <span class="tag">{{ LAYER_KIND_TEXT[L.kind] || L.kind }}</span>
                  <span class="llbl" :title="L.label">{{ L.label }}</span>
                </label>
                <span v-if="L.bytes" class="lsz">{{ fmtSize(L.bytes) }}</span>
                <!-- 非 tiled 的大图直读会把整份数据拖下来,给个转 COG 的入口。
                     走「本地文件」处理(把这个 tif 当输入源导出 COG),不是补充
                     导出——后端不允许改已导出阶段的容器格式。 -->
                <t-button v-if="L.kind === 'raster_only_bbox' && L.path"
                  size="small" variant="text" theme="warning"
                  title="把该文件作为本地源处理、导出为 COG,之后即可叠加显示"
                  @click="emit('convert-cog', L.path)">转 COG</t-button>
              </template>
            </div>
            <div v-if="!layers[t.id].length" class="dim">无可叠加成果</div>
          </template>
          <div v-else class="dim">正在读取图层…</div>

          <div class="ops">
            <t-button size="small" variant="outline" @click="locate(t)">定位</t-button>
            <t-button size="small" variant="outline"
              @click="useAsRange(t)">用作选区</t-button>
            <t-button v-if="canAddExport(t)" size="small" variant="outline"
              @click="openAddExport(t)">补充格式</t-button>
            <t-button v-if="canRepairNodata(t)" size="small" variant="outline" theme="warning"
              :loading="!!repairing[t.id]" @click="onRepairNodata(t)">修复白点</t-button>
            <t-button v-if="previewable(t)" size="small" variant="outline" theme="success"
              @click="openPreview(t)">三维预览</t-button>
            <t-button size="small" variant="outline" @click="reveal(t)">打开目录</t-button>
            <t-button size="small" variant="text" theme="danger"
              @click="onDelete(t)">删除</t-button>
          </div>
        </template>
      </div>

      <t-empty v-if="!results.length"
        :description="kw || kindFilter !== 'all' ? '没有匹配的成果' : '暂无成果'" />
    </div>

    <AddExportDialog v-model:visible="addExportVisible" :task="addExportTask" />
  </SidePanel>
</template>

<style scoped>
.dd { font-size: 13px; }
.sec {
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; font-weight: 600; color: #0f172a;
  padding-bottom: 4px; border-bottom: 1px solid #e2e8f0; margin: 4px 0 8px;
}
.sec:not(:first-child) { margin-top: 16px; }
.cnt {
  font-weight: 400; color: #64748b; background: #f1f5f9;
  border-radius: 8px; padding: 0 6px; font-size: 11px;
}
.card {
  border: 1px solid #e2e8f0; border-radius: 6px; padding: 8px;
  margin-bottom: 8px; background: #fff;
}
.card.range { background: #f0f9ff; border-color: #bae6fd; }
.card.hint { color: #64748b; font-size: 12px; line-height: 1.7; }
.card.res .r1 { cursor: pointer; }
.card.res.open { border-color: #7dd3fc; }
.r1 { display: flex; align-items: center; gap: 6px; min-width: 0; }
.nm { flex: 1 1 auto; min-width: 0; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.fold { flex: 0 0 auto; color: #94a3b8; font-size: 11px; }
.span { font-size: 12px; color: #0369a1; }
.tag {
  flex: 0 0 auto; font-size: 10px; border-radius: 3px;
  padding: 0 4px; line-height: 16px; color: #0369a1; background: #e0f2fe;
}
.tag.dem { color: #b45309; background: #fef3c7; }
.tag.buildings { color: #7c3aed; background: #ede9fe; }
.tag.bad { color: #b91c1c; background: #fee2e2; }
.tag.rt { color: #0369a1; background: #dbeafe; }
.dim { color: #64748b; font-size: 12px; }
.mono { font-family: ui-monospace, Consolas, monospace; font-size: 11px; }
.meta { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 3px; }
.path { word-break: break-all; margin: 6px 0; }
.sizes {
  background: #f8fafc; border: 1px solid #eef2f7; border-radius: 4px;
  padding: 4px 8px; margin-bottom: 8px;
}
.szrow { display: flex; justify-content: space-between; font-size: 12px;
  color: #475569; line-height: 1.8; }
.szrow.total { border-top: 1px solid #e2e8f0; font-weight: 600; color: #0f172a; }
.szv { font-family: ui-monospace, Consolas, monospace; }

/* 叠加到地图小节 */
.lsec {
  font-size: 12px; font-weight: 600; color: #334155;
  margin: 8px 0 4px;
}
.lrow {
  display: flex; align-items: center; gap: 6px;
  min-width: 0; padding: 1px 0;
}
.lchk {
  display: flex; align-items: center; gap: 5px; cursor: pointer;
  flex: 1 1 auto; min-width: 0; font-size: 12px;
}
.lchk input { cursor: pointer; margin: 0; flex: 0 0 auto; }
.lchk input:disabled { cursor: default; }
.llbl {
  flex: 1 1 auto; min-width: 0; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; color: #334155;
}
.lsz {
  flex: 0 0 auto; font-size: 11px; color: #94a3b8;
  font-family: ui-monospace, Consolas, monospace;
}
.tag.t3d { color: #7c3aed; background: #ede9fe; }
.lerr { font-size: 12px; color: #b91c1c; line-height: 1.7; }
.link {
  border: 0; background: transparent; cursor: pointer;
  color: #0369a1; font-size: 12px; padding: 1px 2px;
}
.link:hover { text-decoration: underline; }
.filter { display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }
.ops { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
</style>
