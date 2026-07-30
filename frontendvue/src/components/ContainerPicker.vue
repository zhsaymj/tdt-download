<script setup>
/**
 * 各导出阶段的「文件格式」选择。
 *
 * 数据来自后端 /api/capabilities(由 backend/core/formats.py 注册表生成),
 * 不在前端硬编码格式列表 —— 后端加一种容器,这里自动出现。
 *
 * 只渲染「已勾选该导出格式」且「该阶段有多个容器可选」的行:
 * 单一容器(如 Cesium 地形切片)没得选,列出来只是噪音。
 */
import { computed } from 'vue'
import InfoTip from './InfoTip.vue'

const props = defineProps({
  /** 后端 capabilities 里该 provider 的 stages 数组 */
  stages: { type: Array, default: () => [] },
  /** 已勾选的导出格式名(imgForm.export 那种) */
  selected: { type: Array, default: () => [] },
  /** v-model:值形如 { geotiff: 'cog', contour: 'gpkg' } */
  modelValue: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['update:modelValue'])

/**
 * 导出格式名 → 阶段 key 的映射。与后端 formats._FORMAT_TO_STAGE 对应:
 * DEM 的 geotiff 与 hillshade 都由 dem 阶段产出,故两者共用 dem 的容器选择。
 */
function stageKeyOf(fmt) {
  if (fmt === 'hillshade') return 'dem'
  return fmt
}

const rows = computed(() => {
  const wanted = new Set()
  for (const f of props.selected) wanted.add(stageKeyOf(f))
  // DEM tab 勾的是 geotiff,但阶段 key 是 dem;两种命名都放行
  if (props.selected.includes('geotiff')) wanted.add('dem')
  return props.stages.filter(
    (s) => wanted.has(s.key) && (s.containers?.length || 0) > 1,
  )
})

function valueOf(stage) {
  return props.modelValue[stage.key] || stage.containers[0].key
}

function pick(stage, containerKey) {
  emit('update:modelValue', { ...props.modelValue, [stage.key]: containerKey })
}

/** 附属文件提示:shapefile 这类多文件格式,拷走时少一个 .prj 就丢坐标系 */
function hintOf(stage) {
  const cur = stage.containers.find((c) => c.key === valueOf(stage))
  if (!cur) return ''
  const parts = [cur.note].filter(Boolean)
  if (cur.sidecars?.length) {
    parts.push(`附属文件:${cur.sidecars.join(' ')}(拷贝时须一并带走)`)
  }
  return parts.join('；')
}
</script>

<template>
  <template v-for="s in rows" :key="s.key">
    <t-form-item :label="`${s.label} 文件格式`" label-align="top">
      <t-select
        :value="valueOf(s)"
        :options="s.containers.map((c) => ({ value: c.key, label: c.label }))"
        @change="(v) => pick(s, v)"
      />
      <InfoTip v-if="hintOf(s)" :content="hintOf(s)" max-width="360px" />
    </t-form-item>
  </template>
</template>
