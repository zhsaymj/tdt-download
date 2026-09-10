<script setup>
import { reactive, ref, computed, watch, nextTick } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { useTaskStore } from '../stores/task'
import { crsOptions } from '../utils/crs'
import { fmtSize } from '../utils/format'
import {
  formatPixelResolution, formatPixelSize, formatSampleSpacing, formatScale72Dpi,
} from '../utils/taskDefaults'
import { isBuildingProvider } from '../utils/provider'
import { api } from '../api'

const props = defineProps({
  visible: { type: Boolean, default: false },
  task: { type: Object, default: null },  // 源任务(沿用其范围/几何)
})
const emit = defineEmits(['update:visible'])

const taskStore = useTaskStore()

// 全部数据类型。重下面板按源任务类型过滤(见 providerOptions),不允许跨类切换——
// 重下沿用源任务的范围数据,切到另一类数据无意义。
const ALL_PROVIDER_OPTIONS = [
  { value: 'tianditu_img', label: '天地图影像' },
  { value: 'tianditu_vec', label: '天地图矢量底图' },
  { value: 'tianditu_ter', label: '天地图地形晕渲' },
  { value: 'esri_terrain', label: '全国地形 DEM(Esri Terrain3D)' },
  { value: 'osm_buildings', label: '三维建筑白模(OSM,境内直连)' },
  { value: 'local_vector', label: '三维建筑白模(本地矢量面)' },
  { value: 'overture_buildings', label: '三维建筑白模(Overture,需境外网络)' },
]
const imageExportOptions = [
  { value: 'geotiff', label: '带坐标 GeoTIFF(每级一张)' },
  { value: 'tms', label: 'TMS 瓦片' },
  { value: 'osm', label: 'OSM 瓦片(Web 墨卡托)' },
]
const demExportOptions = [
  { value: 'geotiff', label: '高程 GeoTIFF(每级一张·真实海拔)' },
  { value: 'tiles', label: '保留原始 LERC 瓦片' },
  { value: 'terrain', label: 'Cesium 地形切片(.terrain + layer.json)' },
]
const crsOpts = crsOptions()

const form = reactive({
  name: '', provider: 'tianditu_img', levels: [], export: [],
  crs: 'EPSG:4326', clip: false, use_cache: true, annotate: false,
  tms_source_strategy: 'contiguous',
  // 三维建筑参数
  base_height_mode: 'terrain', height_offset: 0, default_height: 6, max_per_tile: 2000,
  // 本地矢量面字段映射
  upload_id: '', height_field: '', height_mode: 'none', height_scale: 1,
  floor_height: 3, name_field: '', keep_fields: [],
  // 上传地形(沿用原任务的;重下不提供换地形,需换请新建任务)
  dem_upload_id: '',
})

// 本地矢量:重下时可改字段映射(不必重新上传)。字段选项按 upload_id 拉回。
const isLocalVector = computed(() => form.provider === 'local_vector')
const uploadFields = ref([])
const fieldsLoading = ref(false)
const heightModeOptions = [
  { value: 'meters', label: '字段为高度(米)' },
  { value: 'floors', label: '字段为层数' },
  { value: 'none', label: '不用字段(全部估算)' },
]
const rdNumericFields = computed(() => uploadFields.value.filter((f) => f.numeric)
  .map((f) => ({ value: f.name, label: `${f.name}(填充 ${(f.fill_rate * 100).toFixed(0)}%)` })))
const rdAllFields = computed(() => uploadFields.value
  .map((f) => ({ value: f.name, label: `${f.name}(填充 ${(f.fill_rate * 100).toFixed(0)}%)` })))

async function loadUploadFields(uploadId) {
  if (!uploadId) { uploadFields.value = []; return }
  fieldsLoading.value = true
  try {
    const info = await api.uploadFields(uploadId)
    uploadFields.value = info.fields || []
  } catch (e) {
    uploadFields.value = []
    MessagePlugin.warning('原上传数据的字段读取失败(可能已过期):' + (e?.message || e))
  } finally {
    fieldsLoading.value = false
  }
}
const submitting = ref(false)
// 预填期间抑制 provider watch 的默认重置,避免覆盖源任务的级别/导出格式
const filling = ref(false)

const isDem = computed(() => form.provider === 'esri_terrain')
// 三维建筑任务:参数与栅格完全不同(无级别/导出格式),模板走独立分支
const isBuildings = computed(() => isBuildingProvider(form.provider))

// 底面高模式选项与说明(与 ParamsPanel 保持一致)
const baseModeOptions = [
  { value: 'terrain', label: '采样地形(推荐)' },
  { value: 'flat', label: '固定为 0' },
  { value: 'offset', label: '统一偏移' },
]
const baseModeHint = computed(() => ({
  terrain: '逐栋采样 DEM 取地面海拔并烘焙进几何,开启地形后建筑贴合地面。会自动补下一份低级别 DEM 用于采样(不导出)。',
  flat: '底面固定在椭球面(高度 0)。仅适用于预览时不加载地形的场景;加载地形时高海拔地区会埋入地下。',
  offset: '全体建筑底面统一设为下方偏移值。仅适用于范围小且地形平坦的情形。',
}[form.base_height_mode]))
// 数据类型选项按源任务类型过滤:同类之间才可切换(重下沿用源范围,跨类无意义)
const providerOptions = computed(() => {
  if (isBuildings.value) {
    // 本地矢量与网络数据源不可互切:切入本地矢量没有上传数据必然失败;
    // 从本地矢量切到网络源会丢掉用户的字段映射,语义上也应另建任务。
    if (isLocalVector.value) {
      return ALL_PROVIDER_OPTIONS.filter((o) => o.value === 'local_vector')
    }
    // 网络数据源之间允许互切(Overture 取数失败后改用 OSM 重跑是常见需求)
    return ALL_PROVIDER_OPTIONS.filter(
      (o) => isBuildingProvider(o.value) && o.value !== 'local_vector')
  }
  if (isDem.value) {
    return ALL_PROVIDER_OPTIONS.filter((o) => o.value === 'esri_terrain')
  }
  return ALL_PROVIDER_OPTIONS.filter(
    (o) => o.value !== 'esri_terrain' && !isBuildingProvider(o.value))
})
const ALL_LEVELS = computed(() =>
  isDem.value ? Array.from({ length: 17 }, (_, i) => i)   // 0..16(Esri Terrain3D)
    : Array.from({ length: 18 }, (_, i) => i + 1))
const exportOptions = computed(() => (isDem.value ? demExportOptions : imageExportOptions))
const tmsSourceStrategyOptions = [
  { value: 'contiguous', label: '连续高层兜底(默认)' },
  { value: 'preserve_inputs', label: '保留每个输入层级并分段补齐' },
]

const allChecked = computed(() => form.levels.length === ALL_LEVELS.value.length)
function toggleAll(checked) { form.levels = checked ? [...ALL_LEVELS.value] : [] }
// 手动维护级别勾选(不依赖 t-checkbox-group 插槽注入)
function toggleLevel(z, checked) {
  const set = new Set(form.levels)
  if (checked) set.add(z); else set.delete(z)
  form.levels = [...set].sort((a, b) => a - b)
}

// 切换数据源:DEM 与影像级别范围/导出不同,重置为合法默认。
// 仅在用户手动切换时生效;预填(filling)期间跳过,避免覆盖源任务参数。
watch(() => form.provider, (p, old) => {
  if (filling.value) return
  // 建筑任务不允许跨类切换,也没有级别/导出格式可重置
  if (isBuildingProvider(p)) return
  const toDem = p === 'esri_terrain'; const fromDem = old === 'esri_terrain'
  if (toDem !== fromDem) {
    form.levels = [...ALL_LEVELS.value]
    form.export = toDem ? ['geotiff', 'tiles'] : ['geotiff', 'tms']
    if (toDem) { form.annotate = false; form.clip = false }
  }
})

// 勾选 OSM 时自动带上 geotiff:OSM 切片以最高级 4326 拼接图为源,
// 输出 4326 且未裁剪时直接复用合并结果,避免重复拼接。预填期间跳过。
watch(() => form.export, (exp) => {
  if (filling.value || isDem.value) return
  if (exp.includes('osm') && !exp.includes('geotiff')) {
    form.export = ['geotiff', ...exp]
  }
})

// 各级别明细 z -> { tiles, bytes }
const perLevel = ref({})
async function refreshEstimate() {
  const b = props.task?.bbox
  if (!b) { perLevel.value = {}; return }
  try {
    const d = await api.estimate({
      west: b[0], south: b[1], east: b[2], north: b[3],
      levels: ALL_LEVELS.value.join(','), provider: form.provider,
    })
    const map = {}
    for (const it of (d.levels || [])) {
      map[it.z] = { tiles: it.tiles, bytes: it.bytes, width: it.width, height: it.height }
    }
    perLevel.value = map
  } catch (_) { perLevel.value = {} }
}
const selectedSummary = computed(() => {
  let tiles = 0; let bytes = 0
  for (const z of form.levels) {
    const it = perLevel.value[z]
    if (it) { tiles += it.tiles; bytes += it.bytes }
  }
  if (form.annotate) { tiles *= 2; bytes *= 2 }
  return { tiles, bytes }
})
function levelSize(z) {
  const it = perLevel.value[z]
  return it ? fmtSize(it.bytes) : ''
}
// 与新建面板同一套口径:地形讲采样间距/成果尺寸,影像讲像素分辨率/比例尺
const centerLat = computed(() => {
  const b = props.task?.bbox
  return b ? (Number(b[1]) + Number(b[3])) / 2 : 0
})
const levelColumns = computed(() => (isDem.value
  ? ['高程级别', '采样间距', '总尺寸', '总大小']
  : ['影像级别', '像素分辨率', '比例尺(72DPI)', '总大小']))
function precisionOf(z) {
  return isDem.value
    ? formatSampleSpacing(z, centerLat.value)
    : formatPixelResolution(z, centerLat.value)
}
function extraOf(z) {
  const it = perLevel.value[z]
  return isDem.value
    ? formatPixelSize(it?.width, it?.height)
    : formatScale72Dpi(z, centerLat.value)
}

// 仅导出 Cesium 地形切片(terrain)却多选层级时的提示(同主面板逻辑)
const terrainMultiLevelHint = computed(() => {
  const onlyTerrain = form.export.length === 1 && form.export[0] === 'terrain'
  return isDem.value && onlyTerrain && form.levels.length > 1
})

// 把导出字段(可能是 both/geotiff+tms/逗号串)解析成数组
function parseExport(v) {
  const s = String(v || '').toLowerCase()
  if (s === 'both') return ['geotiff', 'tms']
  return s.replace(/\+/g, ',').split(',').map((x) => x.trim()).filter(Boolean)
}

// 弹窗打开时,用源任务参数预填
watch(() => props.visible, async (v) => {
  if (v && props.task) {
    const t = props.task
    // 预填期间置标志,抑制 provider watch 的默认重置(否则会覆盖级别/导出格式)
    filling.value = true
    // 就地重下保留原名;新建则加"_重下"后缀避免目录混淆
    const inPlaceMode = ['paused', 'failed', 'canceled'].includes(t.status)
    form.name = inPlaceMode ? t.name : `${t.name}_重下`
    form.provider = t.provider || 'tianditu_img'
    form.levels = (t.levels && t.levels.length)
      ? [...t.levels]
      : Array.from({ length: t.z_max - t.z_min + 1 }, (_, i) => t.z_min + i)
    form.export = parseExport(t.export)
    form.crs = t.crs || 'EPSG:4326'
    form.clip = !!t.clip
    form.annotate = !!t.annotate
    form.use_cache = true
    form.tms_source_strategy = t.tms_source_strategy || 'contiguous'
    // 三维建筑参数预填
    form.base_height_mode = t.base_height_mode || 'terrain'
    form.height_offset = Number(t.height_offset) || 0
    form.default_height = Number(t.default_height) || 6
    form.max_per_tile = Number(t.max_per_tile) || 2000
    // 本地矢量字段映射预填
    form.upload_id = t.upload_id || ''
    form.height_field = t.height_field || ''
    form.height_mode = t.height_mode || 'none'
    form.height_scale = Number(t.height_scale) || 1
    form.floor_height = Number(t.floor_height) || 3
    form.name_field = t.name_field || ''
    form.keep_fields = Array.isArray(t.keep_fields) ? [...t.keep_fields] : []
    form.dem_upload_id = t.dem_upload_id || ''
    // 等 provider watch 队列刷新后再解除抑制,确保预填值不被覆盖
    await nextTick()
    filling.value = false
    // 建筑任务无瓦片估算,跳过(否则会拿建筑 provider 去请求瓦片估算接口)
    if (!isBuildings.value) refreshEstimate()
    // 本地矢量:拉回字段选项,供用户改高度字段/保留字段
    if (form.provider === 'local_vector') loadUploadFields(form.upload_id)
  }
})

watch(() => form.provider, () => {
  if (props.visible && !isBuildings.value) refreshEstimate()
})

const hasGeometry = computed(() => !!props.task?.geometry)

// 就地重下:paused/failed/canceled 修改原任务;done 则新建任务
const inPlace = computed(() =>
  ['paused', 'failed', 'canceled'].includes(props.task?.status))
const dialogHeader = computed(() =>
  inPlace.value ? '重新下载(修改当前任务参数)' : '重新下载(基于当前范围新建任务)')
const submitText = computed(() => (inPlace.value ? '重新下载' : '新建下载任务'))

function close() { emit('update:visible', false) }

async function submit() {
  // 三维建筑:校验与载荷都与栅格不同,单独处理
  if (isBuildings.value) {
    submitting.value = true
    const bp = {
      name: form.name || '三维建筑',
      provider: form.provider,
      bbox: props.task.bbox,
      levels: [],
      export: 'b3dm',
      geometry: props.task.geometry || null,
      clip: false,
      annotate: false,
      base_height_mode: form.base_height_mode,
      height_offset: Number(form.height_offset) || 0,
      default_height: Number(form.default_height) || 6,
      max_per_tile: Number(form.max_per_tile) || 2000,
      upload_id: form.upload_id || '',
      height_field: form.height_field || '',
      height_mode: form.height_mode || 'none',
      height_scale: Number(form.height_scale) || 1,
      floor_height: Number(form.floor_height) || 3,
      name_field: form.name_field || '',
      keep_fields: [...(form.keep_fields || [])],
      // 沿用原任务的上传地形;切到非 terrain 模式时清空(后端会校验)
      dem_upload_id: (form.base_height_mode === 'terrain'
        ? (form.dem_upload_id || '') : ''),
    }
    if (isLocalVector.value && bp.height_mode !== 'none' && !bp.height_field) {
      MessagePlugin.error('已选择按字段取高度,请指定高度字段')
      submitting.value = false
      return
    }
    try {
      if (inPlace.value) {
        await taskStore.updateParams(props.task.id, bp)
        MessagePlugin.success('已按新参数重新生成')
      } else {
        await taskStore.create(bp)
        MessagePlugin.success('已新建三维建筑任务')
      }
      close()
    } catch (err) {
      MessagePlugin.error('提交失败:' + (err?.message || err))
    } finally {
      submitting.value = false
    }
    return
  }

  if (!form.levels.length) { MessagePlugin.error('请至少勾选一个下载级别'); return }
  if (!form.export.length) { MessagePlugin.error('请至少选择一种导出格式'); return }
  submitting.value = true
  const payload = {
    name: form.name || '影像下载',
    provider: form.provider,
    bbox: props.task.bbox,                 // 沿用源任务范围
    levels: [...form.levels].sort((a, z) => a - z),
    export: form.export.join(','),
    crs: form.crs,
    geometry: props.task.geometry || null, // 沿用源任务几何
    clip: !!(form.clip && props.task.geometry),
    use_cache: form.use_cache,
    annotate: form.annotate,
    tms_source_strategy: form.tms_source_strategy,
  }
  try {
    if (inPlace.value) {
      await taskStore.updateParams(props.task.id, payload)
      MessagePlugin.success('已按新参数重新下载')
    } else {
      await taskStore.create(payload)
      MessagePlugin.success('已新建重新下载任务')
    }
    close()
  } catch (err) {
    MessagePlugin.error('提交失败:' + (err?.message || err))
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <t-dialog
    :visible="visible" :header="dialogHeader"
    :width="480" :footer="false" @close="close" @update:visible="close"
  >
    <t-form label-align="top" class="rd-form">
      <t-form-item v-if="!inPlace" label="任务名称">
        <t-input v-model="form.name" placeholder="任务名称" />
      </t-form-item>
      <t-form-item label="数据类型">
        <t-select v-model="form.provider" :options="providerOptions" />
      </t-form-item>

      <!-- ===== 三维建筑参数(与栅格参数互斥) ===== -->
      <template v-if="isBuildings">
        <!-- 本地矢量面:沿用原上传数据,可改字段映射 -->
        <template v-if="isLocalVector">
          <t-form-item>
            <div class="rd-note">
              沿用原任务上传的矢量数据(id {{ form.upload_id || '—' }})。
              <span v-if="fieldsLoading">字段读取中…</span>
              <span v-else-if="!uploadFields.length" class="rd-warn-inline">字段不可读(上传文件可能已过期,需新建任务重新上传)</span>
            </div>
          </t-form-item>
          <template v-if="uploadFields.length">
            <t-form-item label="高度来源">
              <t-radio-group v-model="form.height_mode" :options="heightModeOptions" />
            </t-form-item>
            <t-form-item v-if="form.height_mode !== 'none'" label="高度字段">
              <t-select v-model="form.height_field" :options="rdNumericFields"
                placeholder="选择字段" filterable />
            </t-form-item>
            <t-form-item v-if="form.height_mode !== 'none'"
              :label="form.height_mode === 'floors' ? '层数换算系数' : '单位换算系数'">
              <t-input-number v-model="form.height_scale" :min="0.001" :max="1000"
                :step="0.1" style="width: 100%" />
            </t-form-item>
            <t-form-item v-if="form.height_mode === 'floors'" label="单层层高(米)">
              <t-input-number v-model="form.floor_height" :min="0.1" :max="100"
                :step="0.1" style="width: 100%" />
            </t-form-item>
            <t-form-item label="建筑名称字段(可选)">
              <t-select v-model="form.name_field" :options="rdAllFields"
                placeholder="不设置" clearable filterable />
            </t-form-item>
            <t-form-item label="保留到模型的字段">
              <t-select v-model="form.keep_fields" :options="rdAllFields"
                placeholder="选择要写入模型的属性字段" multiple filterable :min-collapsed-num="3" />
            </t-form-item>
          </template>
        </template>

        <t-form-item label="建筑底面高">
          <t-radio-group v-model="form.base_height_mode" :options="baseModeOptions" />
        </t-form-item>
        <t-form-item>
          <div class="rd-note">{{ baseModeHint }}</div>
        </t-form-item>
        <t-form-item v-if="form.base_height_mode !== 'flat'"
          :label="form.base_height_mode === 'offset' ? '底面高度(米)' : '附加偏移(米)'">
          <t-input-number v-model="form.height_offset" :step="1" style="width: 100%" />
        </t-form-item>
        <t-form-item label="兜底建筑高(米)">
          <t-input-number v-model="form.default_height" :min="1" :max="1000" :step="1"
            style="width: 100%" />
        </t-form-item>
        <t-form-item label="单瓦片建筑数上限">
          <t-input-number v-model="form.max_per_tile" :min="100" :max="50000" :step="100"
            style="width: 100%" />
        </t-form-item>
        <t-form-item>
          <div class="rd-note">改动底面高相关参数会重做建模与切片;只改单瓦片建筑数则复用已拉取的轮廓与建模结果,仅重切瓦片。建筑轮廓数据始终复用,不会重复跨境取数。</div>
        </t-form-item>
      </template>

      <!-- ===== 栅格(影像/DEM)参数 ===== -->
      <t-form-item v-if="!isBuildings" label="下载级别">
        <div class="levels">
          <div class="lv-head">
            <label class="lv nlv">
              <input type="checkbox" :checked="allChecked"
                @change="(e) => toggleAll(e.target.checked)" />
              <span class="lv-z">全选</span>
            </label>
          </div>
          <div class="lv-cols">
            <span v-for="(c, i) in levelColumns" :key="i" class="lv-col">{{ c }}</span>
          </div>
          <div class="lv-scroll">
            <div class="lv-group">
              <label v-for="z in ALL_LEVELS" :key="z" class="lv nlv lv-row">
                <input type="checkbox" :checked="form.levels.includes(z)"
                  @change="(e) => toggleLevel(z, e.target.checked)" />
                <span class="lv-z">第 {{ z }} 级</span>
                <span class="lv-size">{{ precisionOf(z) }}</span>
                <span class="lv-size">{{ extraOf(z) }}</span>
                <span class="lv-size">{{ levelSize(z) }}</span>
              </label>
            </div>
          </div>
          <div class="rd-summary">
            所选 {{ form.levels.length }} 级 · 共 {{ selectedSummary.tiles }} 张 ·
            约 {{ fmtSize(selectedSummary.bytes) }}
          </div>
          <div class="rd-esthint">仅原始瓦片下载量,非最终成果大小(GeoTIFF/TMS/OSM 经压缩/重编码后不同)</div>
        </div>
      </t-form-item>
      <t-form-item v-if="!isBuildings" label="导出格式">
        <t-checkbox-group v-model="form.export" :options="exportOptions" />
      </t-form-item>
      <t-form-item v-if="!isBuildings && !isDem && form.export.includes('tms')"
        label="TMS 断层策略">
        <t-radio-group v-model="form.tms_source_strategy"
          :options="tmsSourceStrategyOptions" />
        <div class="rd-note">连续高层兜底会忽略断层后的低层源;分段保留会让每个输入 tif 保留自身层级,并向下补到下一个输入层级之上。</div>
      </t-form-item>
      <t-form-item v-if="!isBuildings && !isDem && form.export.includes('osm')">
        <div class="rd-note">OSM 切片需要最高级拼接图作源,已自动勾选 GeoTIFF。GeoTIFF 主文件恒为 EPSG:4326,OSM 切片直接复用它、不重复拼接(裁剪时复用裁剪前的未裁剪源,同样免自拼)。</div>
      </t-form-item>
      <t-form-item v-if="terrainMultiLevelHint">
        <div class="rd-warn">⚠ 仅导出 Cesium 地形切片时,只用所选的最高层级作高程源,Cesium 会自动从 0 级逐级细化。多选低层级只会增加下载量,不会提升切片精度。如只要地形切片,选一个最高层级即可。</div>
      </t-form-item>
      <t-form-item v-if="!isBuildings"
        :label="isDem ? '输出坐标系(高程 GeoTIFF)' : '输出坐标系(GeoTIFF)'">
        <t-select v-model="form.crs" :options="crsOpts" filterable />
      </t-form-item>
      <t-form-item v-if="!isBuildings && !isDem && hasGeometry">
        <t-checkbox v-model="form.clip">裁剪 GeoTIFF 到矢量/多边形边界</t-checkbox>
      </t-form-item>
      <t-form-item v-if="!isBuildings && !isDem">
        <t-checkbox v-model="form.annotate">叠加路网注记(瓦片数翻倍)</t-checkbox>
      </t-form-item>
      <t-form-item v-if="!isBuildings">
        <t-checkbox v-model="form.use_cache">使用缓存数据(不勾选则重新下载原始瓦片)</t-checkbox>
      </t-form-item>
    </t-form>
    <div class="rd-actions">
      <t-button variant="outline" @click="close">取消</t-button>
      <t-button theme="primary" :loading="submitting" @click="submit">{{ submitText }}</t-button>
    </div>
  </t-dialog>
</template>

<style scoped>
.rd-form { max-height: 56vh; overflow-y: auto; padding-right: 4px; }
.rd-warn-inline { color: #b45309; font-weight: 600; }
.levels { display: flex; flex-direction: column; gap: 4px; }
.lv-head { flex: 0 0 auto; padding-bottom: 4px; border-bottom: 1px solid #eef2f7; }
.lv-scroll { max-height: 200px; overflow-y: auto; padding: 4px 2px 0; }
.lv-group { display: flex; flex-direction: column; gap: 2px; }
.nlv { display: flex; align-items: center; gap: 6px; cursor: pointer;
  font-size: 13px; color: #334155; line-height: 1.9; }
.nlv input { cursor: pointer; margin: 0; }
.levels .lv { margin-right: 0; }
.levels :deep(.lv .t-checkbox__label) {
  display: inline-flex; align-items: baseline; gap: 6px; width: 100%;
}
.lv-z { min-width: 18px; font-weight: 600; }
.lv-size { font-size: 11px; color: #94a3b8; }
/* 表头与行共用列宽,保证分辨率/比例尺/大小三列对齐 */
.lv-cols, .lv-row {
  display: grid;
  grid-template-columns: 18px 62px minmax(0, 1fr) minmax(0, 1fr) minmax(0, 72px);
  align-items: center; gap: 6px; min-width: 0;
}
.lv-cols { padding: 4px 2px 0; font-size: 11px; color: #94a3b8; }
.lv-cols .lv-col:first-child { grid-column: 1 / span 2; }
.lv-col, .lv-row .lv-size, .lv-row .lv-z {
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.rd-summary { font-size: 12px; color: #0369a1; margin-top: 6px; font-weight: 600; }
.rd-esthint { font-size: 11px; color: #94a3b8; font-weight: 400; }
.rd-warn { font-size: 11px; color: #b45309; line-height: 1.6;
  background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; padding: 6px 8px; }
.rd-note { font-size: 11px; color: #0369a1; line-height: 1.6;
  background: #f0f9ff; border: 1px solid #e0f2fe; border-radius: 6px; padding: 6px 8px; }
.rd-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 12px; }
</style>
