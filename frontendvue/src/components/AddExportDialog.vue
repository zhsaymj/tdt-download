<script setup>
/**
 * 给已完成任务补充导出格式。
 *
 * 与「重新下载」的区别:重下是建一个新任务(重新走一遍下载),这里是往**原任务**
 * 追加新阶段——下载的瓦片缓存与合并好的 GeoTIFF 都还在,新格式所需的上游数据
 * 已经具备,只是此前没有入口把新阶段接进去。实测补 TMS 只用 0.3 秒,OSM 直接
 * 复用已合并的 GeoTIFF 作源、不重拼。
 */
import { computed, reactive, ref, watch } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { useTaskStore } from '../stores/task'
import { api } from '../api'
import InfoTip from './InfoTip.vue'

const props = defineProps({
  visible: { type: Boolean, default: false },
  task: { type: Object, default: null },
})
const emit = defineEmits(['update:visible'])

const taskStore = useTaskStore()
const caps = ref({})
const submitting = ref(false)
const form = reactive({
  export: [],
  containers: {},
  contourInterval: 50,
  tmsSourceStrategy: 'contiguous',
})

const tmsSourceStrategyOptions = [
  { value: 'contiguous', label: '连续高层兜底(默认)' },
  { value: 'preserve_inputs', label: '保留每个输入层级并分段补齐' },
]
function isRasterImageProvider(provider) {
  return ['tianditu_img', 'tianditu_vec', 'tianditu_ter', 'local_image'].includes(provider)
}

/** 该任务已导出过的格式(不可重复补充,故置灰) */
const existing = computed(() => {
  const s = String(props.task?.export || '').toLowerCase()
  return s.replace(/\+/g, ',').split(',').map((x) => x.trim()).filter(Boolean)
})

/** 阶段 key → 格式名(DEM 的整幅图阶段 key 是 dem,格式名是 geotiff) */
function fmtNameOf(stageKey) {
  return stageKey === 'dem' ? 'geotiff' : stageKey
}

const stages = computed(() => caps.value[props.task?.provider]?.stages || [])
const options = computed(() => stages.value.map((s) => {
  const v = fmtNameOf(s.key)
  return {
    value: v,
    label: s.label + (existing.value.includes(v) ? '(已导出)' : ''),
    disabled: existing.value.includes(v),
  }
}))
/** 选中格式里有多个容器可选的阶段 */
const containerRows = computed(() => stages.value.filter(
  (s) => form.export.includes(fmtNameOf(s.key)) && (s.containers?.length || 0) > 1))

watch(() => props.visible, async (v) => {
  if (!v) return
  form.export = []
  form.containers = {}
  form.contourInterval = Number(props.task?.contour_interval) || 50
  form.tmsSourceStrategy = props.task?.tms_source_strategy || 'contiguous'
  if (!Object.keys(caps.value).length) {
    try { caps.value = (await api.capabilities()).providers || {} } catch (_) { /* 拿不到就只显示空列表 */ }
  }
})

async function submit() {
  if (!form.export.length) {
    MessagePlugin.warning('请选择要补充的格式')
    return
  }
  submitting.value = true
  try {
    const r = await api.addExport(props.task.id, {
      export: form.export.join(','),
      containers: { ...form.containers },
      contour_interval: Number(form.contourInterval) || 50,
      tms_source_strategy: form.tmsSourceStrategy,
    })
    MessagePlugin.success('已加入队列,补充导出:' + r.added.join('、'))
    emit('update:visible', false)
    await taskStore.load()
  } catch (e) {
    MessagePlugin.error('提交失败:' + (e?.message || e))
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <t-dialog :visible="visible" header="补充导出格式" width="520px"
    :confirm-btn="{ content: '开始', loading: submitting }"
    @confirm="submit" @close="emit('update:visible', false)">
    <div v-if="task" class="ae-body">
      <div class="ae-hint">
        为已完成的任务「{{ task.name }}」补充新格式。
        <InfoTip content="复用该任务已有的瓦片缓存与合并成果,不重新下载。已导出过的格式不能重复补充;若要换容器格式(如把 GeoTIFF 换成 COG),请用「重新下载」建新任务——原成果已按旧格式写出,就地替换会让成果与说明文件对不上。"
          max-width="380px" />
      </div>
      <t-form label-align="top">
        <t-form-item label="新增格式">
          <t-checkbox-group v-model="form.export" :options="options" />
        </t-form-item>
        <t-form-item v-for="s in containerRows" :key="s.key"
          :label="`${s.label} 文件格式`">
          <t-select
            :value="form.containers[s.key] || s.containers[0].key"
            :options="s.containers.map((c) => ({ value: c.key, label: c.label }))"
            @change="(v) => (form.containers = { ...form.containers, [s.key]: v })" />
        </t-form-item>
        <t-form-item v-if="form.export.includes('contour')" label="等高距(米)">
          <t-input-number v-model="form.contourInterval" :min="1" :max="1000"
            :step="10" theme="column" style="width: 130px" />
        </t-form-item>
        <t-form-item v-if="isRasterImageProvider(task.provider) && form.export.includes('tms')"
          label="TMS 断层策略">
          <t-radio-group v-model="form.tmsSourceStrategy"
            :options="tmsSourceStrategyOptions" />
          <div class="ae-note">连续高层兜底会忽略断层后的低层源;分段保留会让每个输入 tif 保留自身层级,并向下补到下一个输入层级之上。</div>
        </t-form-item>
      </t-form>
    </div>
  </t-dialog>
</template>

<style scoped>
.ae-body { font-size: 13px; }
.ae-hint { color: #475569; line-height: 1.7; margin-bottom: 10px; }
.ae-note { font-size: 12px; color: #0369a1; line-height: 1.6; margin-top: 6px; }
</style>
