<script setup>
/**
 * 地形任务新建表单:DEM 来源二选一(在线 Esri Terrain3D / 本地 DEM 文件),
 * 范围来自地图画框,固定只导出 terrain 一种格式(不带 geotiff/tms 等其他格式)。
 *
 * 展示容器用 SidePanel(左侧浮动、非模态),与主站 ProcessDialog 一致——这样
 * 用户可以边开着本面板边在地图上画选区,不必关掉表单再画。业务逻辑(来源/级别/
 * 探测/建议/max_error)不变,只是外层从 t-dialog 换成 SidePanel。
 */
import { reactive, computed, ref, watch } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { api } from '../api'
import { useDrawStore } from '../stores/draw'
import { useTaskStore } from '../stores/task'
import { defaultTaskName, levelsForProvider, DEM_LEVELS } from '../utils/taskDefaults'
import SidePanel from '../components/SidePanel.vue'

const props = defineProps({
  visible: { type: Boolean, default: false },
})
const emit = defineEmits(['update:visible', 'created'])

const drawStore = useDrawStore()
const taskStore = useTaskStore()

const sourceKind = ref('online')   // 'online' | 'local'

const form = reactive({
  name: '',
  maxError: 5.0,
})

// 打开表单时如果名字为空,按当前来源给一个默认名(与主下载工具的命名习惯一致)
watch(() => props.visible, (v) => {
  if (v && !form.name) {
    form.name = defaultTaskName(sourceKind.value === 'online' ? 'esri_terrain' : 'local_dem')
  }
})
watch(sourceKind, () => {
  form.name = defaultTaskName(sourceKind.value === 'online' ? 'esri_terrain' : 'local_dem')
})

// ---- 在线来源:Esri Terrain3D,范围来自地图画框 ----
// 级别清单与主下载工具同源(levelsForProvider('esri_terrain') === DEM_LEVELS,0~16)。
// 默认全选:不下载 levels 为空数组会让后端 TaskCreate.level_list() 回落到
// z_min=0,z_max=0,只切 z0 一级(单像素约 78km),地形必然是平的 —— 这就是本次要修的 bug。
const levelList = levelsForProvider('esri_terrain')
const onlineLevels = ref([...DEM_LEVELS])

const demMaxLevel = ref(null)
const probingLevel = ref(false)
async function probeMaxLevel() {
  demMaxLevel.value = null
  const b = drawStore.bbox
  if (!b || sourceKind.value !== 'online') return
  probingLevel.value = true
  try {
    const d = await api.demMaxLevel({ west: b[0], south: b[1], east: b[2], north: b[3], provider: 'esri_terrain' })
    demMaxLevel.value = d?.max_level ?? null
  } catch (_) { demMaxLevel.value = null }
  finally { probingLevel.value = false }
  // 已勾上的超限级别要摘掉:留着提交也会被后端下调,不如当场如实反映
  // (demMaxLevel 为 null 表示探测失败,此时不摘任何级别,由后端提交时兜底下调)
  if (demMaxLevel.value != null) {
    const kept = onlineLevels.value.filter((z) => z <= demMaxLevel.value)
    if (kept.length !== onlineLevels.value.length) onlineLevels.value = kept
  }
}

// 级别建议:该范围各级别的有效数据占比,占比过低说明大半落在选区外(仅提示,不强制)
const suggest = ref(null)
async function loadSuggest() {
  suggest.value = null
  const b = drawStore.bbox
  if (!b || sourceKind.value !== 'online') return
  try {
    suggest.value = await api.suggestLevels({ west: b[0], south: b[1], east: b[2], north: b[3], provider: 'esri_terrain' })
  } catch (_) { suggest.value = null }
}

watch([() => props.visible, () => drawStore.bbox, sourceKind], () => {
  if (props.visible && sourceKind.value === 'online') {
    probeMaxLevel()
    loadSuggest()
  }
}, { immediate: true })

/** 该级别是否因超出探测到的最高级别而不可用(disabled) */
function levelUnavailable(z) {
  return demMaxLevel.value != null && z > demMaxLevel.value
}
/** 可勾选级别:排除该范围没有数据的超限级别 */
const availableLevels = computed(() => levelList.filter((z) => !levelUnavailable(z)))

// ---- 本地来源:选文件 + 校验是否为高程数据 ----
const dialogOk = ref(false)
const localPath = ref('')
const localBusy = ref(false)
const localError = ref('')
const localInfo = ref(null)   // localInspect 返回体,确认 kind === 'raster_dem'

async function checkDialogAvailable() {
  try { dialogOk.value = (await api.localDialogAvailable().catch(() => ({}))).available || false }
  catch (_) { dialogOk.value = false }
}
checkDialogAvailable()

async function browseLocal() {
  localError.value = ''
  try {
    const d = await api.localPick({ kind: 'raster', multiple: false })
    if (d.paths?.length) { localPath.value = d.paths[0]; await inspectLocal() }
  } catch (e) {
    localError.value = '打开文件对话框失败:' + (e?.message || e)
  }
}

async function inspectLocal() {
  const p = localPath.value.trim()
  localInfo.value = null
  localError.value = ''
  if (!p) return
  localBusy.value = true
  try {
    const d = await api.localInspect(p)
    if (d.kind !== 'raster_dem') {
      localError.value = '这不是高程数据,请选择 DEM 文件'
      return
    }
    localInfo.value = d
    if (!form.name) form.name = String(d.filename || '').replace(/\.[^.]+$/, '')
  } catch (e) {
    localError.value = e?.message || String(e)
  } finally {
    localBusy.value = false
  }
}

// ---- 提交 ----
const submitting = ref(false)

const canSubmit = computed(() => {
  if (submitting.value) return false
  if (sourceKind.value === 'online') return !!drawStore.bbox && onlineLevels.value.length > 0
  return !!(localInfo.value && localInfo.value.kind === 'raster_dem')
})

function buildPayload() {
  const base = {
    name: form.name || '未命名',
    export: 'terrain',
    terrain_max_error: Number(form.maxError) || 5.0,
  }
  if (sourceKind.value === 'online') {
    return {
      ...base,
      provider: 'esri_terrain',
      bbox: drawStore.bbox,
      levels: [...onlineLevels.value].sort((a, b) => a - b),
      geometry: null,
      clip: false,
    }
  }
  // 本地 DEM 文件只有一个原生分辨率,后端 _create_local_task 会自行取源文件
  // 原生级别,这里传空 levels 是正确行为,不要补默认值。
  return {
    ...base,
    provider: 'local_dem',
    source_path: localPath.value,
    bbox: [],
    levels: [],
    geometry: null,
    clip: false,
  }
}

async function submit() {
  if (!canSubmit.value) return
  submitting.value = true
  try {
    await taskStore.create(buildPayload())
    MessagePlugin.success('已创建地形任务')
    emit('created')
    emit('update:visible', false)
  } catch (e) {
    MessagePlugin.error(e?.message || '创建失败')
  } finally {
    submitting.value = false
  }
}

function close() { emit('update:visible', false) }
</script>

<template>
  <SidePanel :visible="visible" title="新建地形任务" side="left" width="440px"
    @update:visible="(v) => emit('update:visible', v)">
    <t-form label-align="top" :label-width="100">
      <t-form-item label="DEM 来源">
        <t-radio-group v-model="sourceKind">
          <t-radio-button value="online">在线下载(Esri Terrain3D)</t-radio-button>
          <t-radio-button value="local">本地 DEM 文件</t-radio-button>
        </t-radio-group>
      </t-form-item>

      <template v-if="sourceKind === 'online'">
        <t-form-item label="下载范围">
          <span v-if="drawStore.bbox" class="hint ok">
            已选范围:{{ drawStore.bbox.map((v) => v.toFixed(3)).join(', ') }}
          </span>
          <span v-else class="hint warn">请先在地图上画一个矩形范围</span>
        </t-form-item>
        <t-form-item v-if="drawStore.bbox" label="可用级别">
          <span v-if="probingLevel" class="hint">探测中…</span>
          <span v-else-if="demMaxLevel != null" class="hint">该范围地形数据最高到 {{ demMaxLevel }} 级</span>
          <span v-else class="hint warn">未能探测到最高级别(不影响创建,后端会自动兜底)</span>
        </t-form-item>
        <t-form-item v-if="drawStore.bbox" label="下载级别">
          <t-space direction="vertical" style="width: 100%">
            <t-checkbox-group v-model="onlineLevels" :options="availableLevels.map((z) => ({
              label: String(z), value: z,
            }))" />
            <span v-if="suggest?.recommended?.length" class="hint">
              建议级别:{{ suggest.recommended.join('、') }}
              <a href="#" @click.prevent="onlineLevels = [...suggest.recommended]">采用</a>
            </span>
            <span v-if="onlineLevels.length === 0" class="hint warn">请至少选择一个级别</span>
          </t-space>
        </t-form-item>
      </template>

      <template v-else>
        <t-form-item label="DEM 文件">
          <t-space direction="vertical" style="width: 100%">
            <div class="pick">
              <t-input v-model="localPath" placeholder="本地 DEM 文件绝对路径"
                @blur="inspectLocal" />
              <t-button v-if="dialogOk" variant="outline" @click="browseLocal">浏览…</t-button>
              <t-button variant="text" :disabled="!localPath || localBusy" @click="inspectLocal">检查</t-button>
            </div>
            <span v-if="localBusy" class="hint">检查中…</span>
            <span v-else-if="localError" class="hint err">{{ localError }}</span>
            <span v-else-if="localInfo" class="hint ok">
              已确认为高程数据:{{ localInfo.filename }}
            </span>
          </t-space>
        </t-form-item>
      </template>

      <t-form-item label="任务名称">
        <t-input v-model="form.name" placeholder="任务名称" />
      </t-form-item>

      <t-form-item label="地形精度">
        <t-space direction="vertical" style="width: 100%">
          <t-input-number v-model="form.maxError" :min="0.01" :max="1000" :step="0.5" />
          <span class="hint">Cesium 地形三角网简化误差(米)。越小越精细、瓦片越大;默认 5.0。</span>
        </t-space>
      </t-form-item>
    </t-form>

    <!-- SidePanel 没有内置确认/取消按钮(主站 ProcessDialog 同样的处理方式:
         浮动面板不是模态对话框,操作按钮放进面板内容区底部) -->
    <div class="form-actions">
      <t-button theme="primary" :loading="submitting" :disabled="!canSubmit"
        @click="submit">创建任务</t-button>
      <t-button variant="outline" @click="close">取消</t-button>
    </div>
  </SidePanel>
</template>

<style scoped>
.hint { font-size: 12px; color: #94a3b8; }
.hint.ok { color: #16a34a; }
.hint.warn { color: #d97706; }
.hint.err { color: #dc2626; }
/* 与主站 ProcessDialog 的本地文件选择行同款弹性布局,避免写死宽度在
   440px 面板内挤出按钮 */
.pick { display: flex; gap: 6px; width: 100%; }
.pick :deep(.t-input) { flex: 1 1 auto; min-width: 0; }
.form-actions {
  display: flex; gap: 8px; justify-content: flex-end;
  margin-top: 16px; padding-top: 12px; border-top: 1px solid #eef2f7;
}
</style>