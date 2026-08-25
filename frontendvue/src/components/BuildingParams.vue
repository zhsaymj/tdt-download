<script setup>
/**
 * 三维建筑白模的参数(从旧 ParamsPanel 的 buildings tab 抽出)。
 *
 * 单独成组件的原因:它有 16 个字段(底面高模式、高度字段映射、上传地形、单瓦片
 * 上限…),混进 ProcessDialog 会让那个组件重新膨胀成旧 ParamsPanel 那样。
 * 通过 v-model 把参数整体交回父组件,父组件只管拼进 payload。
 *
 * 两种数据源的参数并不重叠:
 *   osm_buildings  轮廓来自在线/缓存,用户关心的是"数据从哪来、本地存了多少"
 *   local_vector   轮廓由用户上传,用户关心的是"哪个字段是高度、单位怎么换算"
 * 故按 provider 分支显示,而非一次铺开全部字段。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { api } from '../api'
import { fmtSize } from '../utils/format'
import { mapController } from '../composables/mapController'
import { useDrawStore } from '../stores/draw'
import { parseVectorFiles, looksLikeLonLat, reprojectGeojson } from '../utils/vector'
import InfoTip from './InfoTip.vue'
import SrsModal from './SrsModal.vue'

const props = defineProps({
  modelValue: { type: Object, default: null },
  /** 'osm_buildings' | 'local_vector' | 'overture_buildings' */
  provider: { type: String, default: 'osm_buildings' },
})
// bbox:本地矢量未画范围时,父组件用上传数据自身的范围提交(后端 bbox 必填)
const emit = defineEmits(['update:modelValue', 'update:bbox'])

const drawStore = useDrawStore()
const isLocalVector = computed(() => props.provider === 'local_vector')

const p = reactive({
  base_height_mode: 'terrain',
  height_offset: 0,
  default_height: 6,
  max_per_tile: 2000,
  use_cache: true,
  dem_upload_id: '',
  upload_id: '',
  height_field: '',
  height_mode: 'meters',
  height_scale: 1,
  floor_height: 3,
  name_field: '',
  keep_fields: [],
})

// ---- 本地矢量面上传 ----
const bldFileInput = ref(null)
const srsVisible = ref(false)
const uploading = ref(false)
let pendingGeojson = null           // 待用户选源坐标系的 geojson
// {upload_id, feature_count, polygon_count, bbox, fields:[{name,numeric,fill_rate,...}]}
const uploadInfo = ref(null)

const heightModeOptions = [
  { value: 'meters', label: '字段为高度(米)' },
  { value: 'floors', label: '字段为层数' },
  { value: 'none', label: '不用字段(全部估算)' },
]

// 字段下拉标注填充率:填充率低的字段大部分建筑取不到值,选之前就该看见
function fieldOpts(numericOnly) {
  const fs = uploadInfo.value?.fields || []
  return fs.filter((f) => !numericOnly || f.numeric).map((f) => ({
    value: f.name,
    label: `${f.name}(填充 ${(f.fill_rate * 100).toFixed(0)}%)`,
  }))
}
const numericFieldOptions = computed(() => fieldOpts(true))
const allFieldOptions = computed(() => fieldOpts(false))

const scaleHint = computed(() => (p.height_mode === 'floors'
  ? `字段值 × ${p.height_scale} = 层数,再 × ${p.floor_height} 米得建筑高度。字段本身就是层数时系数填 1。`
  : `字段值 × ${p.height_scale} = 高度(米)。数据以厘米存填 0.01,以英尺存填 0.3048,已是米则填 1。`))

// 高度字段填充率过低时提示:大部分建筑会落到面积估算,楼高会显得均一
const lowFillWarn = computed(() => {
  if (!uploadInfo.value || p.height_mode === 'none' || !p.height_field) return ''
  const f = (uploadInfo.value.fields || []).find((x) => x.name === p.height_field)
  if (!f) return ''
  const pct = f.fill_rate * 100
  if (pct >= 60) return ''
  return `字段「${f.name}」仅 ${pct.toFixed(0)}% 的建筑有值,其余会按占地面积估算层数(或用兜底高度),楼高会显得均一。`
})

function pickBldFile() { bldFileInput.value?.click() }

// 解析复用范围导入那套:shpjs 解 shp/zip、togeojson 解 kml,proj4 按 .prj
// 或用户选的 EPSG 转 WGS84。后端只收已转好的 GeoJSON。
async function onBldFileChange(e) {
  const files = Array.from(e.target.files || [])
  e.target.value = ''
  if (!files.length) return
  try {
    const res = await parseVectorFiles(files)
    if (!res) { MessagePlugin.warning('未解析到矢量内容'); return }
    const { geojson, prjText } = res
    if (looksLikeLonLat(geojson)) { await doUpload(geojson); return }
    if (prjText) {
      try {
        reprojectGeojson(geojson, prjText)
        if (looksLikeLonLat(geojson)) { await doUpload(geojson); return }
      } catch (_) { /* 落到手选坐标系 */ }
    }
    pendingGeojson = geojson
    srsVisible.value = true
  } catch (err) {
    MessagePlugin.error('矢量解析失败:' + (err?.message || err))
  }
}

async function onSrsConfirm(srcEpsg) {
  try {
    reprojectGeojson(pendingGeojson, srcEpsg)
    srsVisible.value = false
    const gj = pendingGeojson
    pendingGeojson = null
    await doUpload(gj)
  } catch (err) {
    MessagePlugin.error('坐标转换失败:' + (err?.message || err))
  }
}

async function doUpload(geojson) {
  uploading.value = true
  try {
    const info = await api.uploadVector(geojson)
    uploadInfo.value = info
    p.upload_id = info.upload_id
    // 按后端的推荐标记预选高度字段:优先"米"字段,否则层数字段
    const fs = info.fields || []
    const hf = fs.find((f) => f.suggest_height)
    const ff = fs.find((f) => f.suggest_floors)
    if (hf) { p.height_field = hf.name; p.height_mode = 'meters' }
    else if (ff) { p.height_field = ff.name; p.height_mode = 'floors' }
    else { p.height_field = ''; p.height_mode = 'none' }
    p.name_field = ''
    p.keep_fields = []
    MessagePlugin.success(
      `已上传:${info.polygon_count} 个面要素,${(info.fields || []).length} 个字段`)
    // 顺带把数据范围画到地图上,便于确认位置对不对
    if (info.bbox && mapController.value) {
      const [w, s, e, n] = info.bbox
      mapController.value.loadGeojson({
        type: 'Feature', properties: {},
        geometry: { type: 'Polygon', coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]] },
      })
    }
  } catch (err) {
    MessagePlugin.error('上传失败:' + (err?.message || err))
  } finally {
    uploading.value = false
  }
}

// ---- OSM 源:数据来源状态 ----
// 取数三级回落:本地网格缓存 → 在线数据包 → OpenStreetMap 在线查询。
// 数据准备(全国 pbf 导入 + 打包)是运维动作,走 update-building-data.bat,界面不暴露。
const cacheInfo = ref(null)
const bundleInfo = ref(null)

async function refreshOfflineInfo() {
  try {
    // 在线数据状态要联网取清单,可能较慢,单独容错(失败不影响本地缓存展示)
    const [c, b] = await Promise.all([
      api.buildingCache(),
      api.bundleStatus().catch(() => null),
    ])
    cacheInfo.value = c
    if (b) bundleInfo.value = b
  } catch (_) { /* 后端未就绪时静默 */ }
}

async function clearBuildingCache() {
  try {
    const r = await api.clearBuildingCache('osm', null)
    MessagePlugin.success(`已清理 ${r.cleared_cells} 个缓存格`)
    refreshOfflineInfo()
  } catch (e) {
    MessagePlugin.error('清理失败:' + (e?.message || e))
  }
}

const cacheText = computed(() => {
  const osm = (cacheInfo.value?.items || []).find((i) => i.source === 'osm')
  if (!cacheInfo.value) return ''
  if (!osm || !osm.cells) return '0 格(尚未下载过)'
  return `${osm.cells} 格 · ${fmtSize(osm.bytes)}`
})

const remote = computed(() => bundleInfo.value?.remote || null)
const remoteOn = computed(() => !!remote.value?.enabled)

// 当前处于三级回落的哪一级
const sourceLevel = computed(() => {
  const cells = ((cacheInfo.value?.items || [])
    .find((i) => i.source === 'osm')?.cells) || 0
  if (remoteOn.value && remote.value?.version) {
    return cells > 0
      ? { text: '在线数据 + 本地缓存', theme: 'success' }
      : { text: '在线数据(按需下载)', theme: 'success' }
  }
  if (cells > 0) return { text: '仅本地缓存(缺的会在线查询)', theme: 'warning' }
  return { text: 'OpenStreetMap 在线查询(较慢)', theme: 'warning' }
})

const remoteCoverText = computed(() => {
  const bb = remote.value?.coverage_bbox
  if (!bb || bb.length !== 4) return ''
  return `东经 ${bb[0].toFixed(1)}~${bb[2].toFixed(1)}、北纬 ${bb[1].toFixed(1)}~${bb[3].toFixed(1)}`
})

// ---- 参考地形:上传本地 GeoTIFF,优先于在线地形 ----
const demFileInput = ref(null)
const demUploading = ref(false)
const demInfo = ref(null)      // {dem_id, crs, bounds_wgs84, res_m_approx, ...}

function pickDemFile() { demFileInput.value?.click() }

async function onDemFileChange(e) {
  const f = (e.target.files || [])[0]
  e.target.value = ''
  if (!f) return
  demUploading.value = true
  try {
    const info = await api.uploadDem(f)
    demInfo.value = info
    p.dem_upload_id = info.dem_id
    MessagePlugin.success(`已上传地形:${info.width}×${info.height} · ${info.crs}`)
  } catch (err) {
    MessagePlugin.error('地形上传失败:' + (err?.message || err))
  } finally {
    demUploading.value = false
  }
}

function clearDem() {
  demInfo.value = null
  p.dem_upload_id = ''
}

// 目标范围:本地矢量按上传数据范围,其余按所画范围
const demTargetBbox = computed(() => (isLocalVector.value
  ? (drawStore.bbox || uploadInfo.value?.bbox)
  : drawStore.bbox) || null)

// 上传地形对目标范围的覆盖率(与后端 coverage_ratio 同一算法)
const demCoverage = computed(() => {
  const a = demInfo.value?.bounds_wgs84
  const b = demTargetBbox.value
  if (!a || !b) return null
  const iw = Math.max(a[0], b[0]); const ie = Math.min(a[2], b[2])
  const is = Math.max(a[1], b[1]); const inn = Math.min(a[3], b[3])
  if (ie <= iw || inn <= is) return 0
  const target = (b[2] - b[0]) * (b[3] - b[1])
  if (target <= 0) return 0
  return Math.min((ie - iw) * (inn - is) / target, 1)
})

const demCoverText = computed(() => {
  const c = demCoverage.value
  if (c === null) return '尚未确定范围,无法比对覆盖情况'
  const pct = c * 100
  if (pct >= 99.9) return `完全覆盖所选范围(${pct.toFixed(0)}%),不会下载在线地形`
  if (pct <= 0) return '与所选范围没有交集,请确认地形数据的坐标系与位置'
  return `仅覆盖所选范围的 ${pct.toFixed(0)}%,未覆盖处将回落在线地形兜底`
})

const demCoverTheme = computed(() => {
  const c = demCoverage.value
  if (c === null) return 'default'
  if (c >= 0.999) return 'success'
  if (c <= 0) return 'danger'
  return 'warning'
})

// 措辞随数据源变化——本地矢量时比对的是矢量范围
const demTipText = computed(() => (
  `地形数据的范围需与${isLocalVector.value ? '上传的矢量面范围' : '所选下载范围'}一致。`
  + '若只覆盖了一部分,已覆盖处用上传地形(更精细)、未覆盖处自动回落在线地形兜底,'
  + '不会因此丢建筑;但两种高程源的高程基准可能不同,交界处可能出现台阶。'
  + '需为带坐标系的单波段高程 GeoTIFF(数值是海拔米数)。'))

// 底面高模式:b3dm 是绝对定位几何,Cesium 加载 3D Tiles 不会自动贴地形,
// 底面海拔必须在生成阶段烘焙进顶点,故必须让用户明确选择。
const baseModeOptions = [
  { value: 'terrain', label: '采样地形(推荐)' },
  { value: 'flat', label: '固定为 0' },
  { value: 'offset', label: '统一偏移' },
]
const baseModeHint = computed(() => ({
  terrain: '逐栋采样 DEM 取地面海拔并烘焙进几何,开启地形后建筑贴合地面。会自动补下一份低级别 DEM 用于采样(不导出)。中国东西部高差极大,这是全域唯一可靠的模式。',
  flat: '底面固定在椭球面(高度 0)。仅适用于预览时不加载地形的纯白模场景;一旦加载地形,高海拔地区建筑会整体埋入地下。',
  offset: '全体建筑底面统一设为下方偏移值。仅适用于范围小且地形平坦的情形,跨城市或有起伏会一部分悬空、一部分沉底。',
}[p.base_height_mode]))

const providerHint = computed(() => ({
  osm_buildings: 'OpenStreetMap 建筑轮廓,经 Overpass API 按范围查询,境内可直连、秒级返回。范围会自动按 0.05° 分块请求。城区覆盖尚可,郊区偏稀疏;高度取 height / building:levels 标签。',
  local_vector: '用你自己的房屋轮廓面数据建白模,高度取自你指定的属性字段。适合已有规划/测绘成果的情形,精度与覆盖都优于在线数据。',
  overture_buildings: 'Overture 融合了 OSM、微软 AI 提取轮廓、Esri 与谷歌开放建筑,覆盖率更高。但数据是托管在 AWS S3 的 512 个 parquet 分片,做范围裁剪需读遍全部分片的元数据——境内实测单次查询需十几小时,基本不可用。仅在境外服务器或有高速代理时选用(代理配置见 config.yaml 的 buildings.proxy)。',
}[props.provider] || ''))

// 换数据源:上传状态属于 local_vector,切走后必须清掉,否则会把上一次的
// upload_id 带进 OSM 任务(后端只按 provider 判断是否校验它,不会报错但语义错)
watch(() => props.provider, () => {
  uploadInfo.value = null
  p.upload_id = ''
  p.height_field = ''
  p.name_field = ''
  p.keep_fields = []
})

// 底面高模式切走 terrain:后端明确拒绝非 terrain 模式带 dem_upload_id(400),
// 这里主动清掉,免得用户上传过地形后改模式就提交不了
watch(() => p.base_height_mode, (m) => {
  if (m !== 'terrain') clearDem()
})

// 参数与 bbox 变化即上抛,父组件无需关心内部字段
watch(p, () => emit('update:modelValue', { ...p }), { deep: true, immediate: true })
watch(() => uploadInfo.value?.bbox, (b) => emit('update:bbox', b || null),
  { immediate: true })

onMounted(refreshOfflineInfo)
</script>

<template>
  <div class="bp">
    <t-form-item label-width="0">
      <div class="phint">{{ providerHint }}</div>
    </t-form-item>

    <!-- ===== 本地矢量面:上传 + 字段映射 ===== -->
    <template v-if="isLocalVector">
      <t-form-item>
        <template #label>
          房屋轮廓数据
          <InfoTip content="支持 GeoJSON / KML / Shapefile(选 .zip 或同时选 .shp/.dbf/.shx,建议附 .prj)。需为面要素(房屋轮廓)。文件在本地解析并转为 WGS84 后上传,属性字段会自动列出供选择。"
            max-width="360px" />
        </template>
        <div class="up-row">
          <t-button size="small" variant="outline" :loading="uploading"
            @click="pickBldFile">选择文件…</t-button>
          <span v-if="uploadInfo" class="up-ok">
            已上传 {{ uploadInfo.polygon_count }} 个面
          </span>
          <span v-else class="up-none">未上传</span>
        </div>
        <input ref="bldFileInput" type="file" multiple style="display:none"
          accept=".geojson,.json,.kml,.shp,.dbf,.shx,.prj,.zip"
          @change="onBldFileChange" />
      </t-form-item>

      <template v-if="uploadInfo">
        <t-form-item label="高度来源">
          <t-radio-group v-model="p.height_mode" :options="heightModeOptions" />
        </t-form-item>
        <t-form-item v-if="p.height_mode !== 'none'" label="高度字段">
          <t-select v-model="p.height_field" :options="numericFieldOptions"
            placeholder="选择字段" filterable />
        </t-form-item>
        <t-form-item v-if="p.height_mode !== 'none'">
          <template #label>
            {{ p.height_mode === 'floors' ? '层数换算系数' : '单位换算系数' }}
            <InfoTip :content="scaleHint" max-width="360px" />
          </template>
          <t-input-number v-model="p.height_scale" :min="0.001" :max="1000"
            :step="0.1" style="width: 100%" />
        </t-form-item>
        <t-form-item v-if="p.height_mode === 'floors'" label="单层层高(米)">
          <t-input-number v-model="p.floor_height" :min="0.1" :max="100"
            :step="0.1" style="width: 100%" />
        </t-form-item>
        <t-form-item label="建筑名称字段(可选)">
          <t-select v-model="p.name_field" :options="allFieldOptions"
            placeholder="不设置" clearable filterable />
        </t-form-item>
        <t-form-item>
          <template #label>
            保留到模型的字段
            <InfoTip content="勾选的字段会写进 b3dm 的 Batch Table,在 Cesium 里点击建筑即可查看。字段名后的百分比是该字段在数据中的填充率,填充率低的字段大部分建筑会是空值。"
              max-width="360px" />
          </template>
          <t-select v-model="p.keep_fields" :options="allFieldOptions"
            placeholder="选择要写入模型的属性字段" multiple filterable :min-collapsed-num="3" />
        </t-form-item>
        <t-form-item v-if="lowFillWarn" label-width="0">
          <div class="bld-warn">⚠ {{ lowFillWarn }}</div>
        </t-form-item>
      </template>
    </template>

    <!-- ===== OSM 源:数据从哪来、本地存了多少 ===== -->
    <template v-else-if="provider === 'osm_buildings'">
      <t-form-item>
        <template #label>
          数据来源
          <InfoTip max-width="360px"
            content="建筑轮廓按 0.05° 网格缓存:下载时先查本地,本地没有的格子自动从在线数据获取(一个城区通常几十 KB),取过即存本地、之后不再联网。在线数据未覆盖的区域(如境外)会回落到 OpenStreetMap 在线查询,较慢。" />
        </template>
        <div class="src-box">
          <t-tag :theme="sourceLevel.theme" variant="light">{{ sourceLevel.text }}</t-tag>
          <div v-if="remoteOn && remote?.version" class="rb-sub">
            在线数据 {{ remote.bundle_count }} 包 / {{ fmtSize(remote.total_bytes) }} ·
            版本 {{ remote.version }}
          </div>
          <div v-if="remoteOn && remote?.version && remoteCoverText" class="rb-sub">
            覆盖 {{ remoteCoverText }}
          </div>
          <div v-else-if="remoteOn" class="rb-bad">
            在线数据读取失败<template v-if="remote?.error">:{{ remote.error }}</template>
          </div>
          <div class="rb-sub">本地已存 {{ cacheText }}</div>
        </div>
      </t-form-item>
      <t-form-item label-width="0">
        <t-checkbox v-model="p.use_cache">
          优先使用本地缓存(取消则该范围重新获取)
        </t-checkbox>
      </t-form-item>
      <t-form-item label-width="0">
        <t-button size="small" variant="text" theme="danger"
          @click="clearBuildingCache">清理本地缓存</t-button>
      </t-form-item>
    </template>

    <!-- ===== 通用:底面高 + 建模参数 ===== -->
    <t-form-item>
      <template #label>
        建筑底面高
        <InfoTip :content="baseModeHint" max-width="360px" />
      </template>
      <t-radio-group v-model="p.base_height_mode" :options="baseModeOptions" />
    </t-form-item>
    <t-form-item v-if="p.base_height_mode === 'offset'" label="底面高(米)">
      <t-input-number v-model="p.height_offset" :min="-1000" :max="10000"
        theme="column" style="width: 140px" />
    </t-form-item>

    <!-- 参考地形:仅采样地形模式有意义 -->
    <template v-if="p.base_height_mode === 'terrain'">
      <t-form-item>
        <template #label>
          参考地形
          <InfoTip :content="demTipText" max-width="360px" />
        </template>
        <div class="up-row">
          <t-button size="small" variant="outline" :loading="demUploading"
            @click="pickDemFile">上传地形…</t-button>
          <t-button v-if="demInfo" size="small" variant="text" theme="danger"
            @click="clearDem">移除</t-button>
          <span v-if="!demInfo" class="up-none">未上传,使用在线地形</span>
        </div>
        <input ref="demFileInput" type="file" style="display:none"
          accept=".tif,.tiff" @change="onDemFileChange" />
      </t-form-item>
      <t-form-item v-if="demInfo" label-width="0">
        <div class="src-box">
          <div class="rb-sub">
            {{ demInfo.filename }} · {{ demInfo.width }}×{{ demInfo.height }} ·
            {{ demInfo.crs }} · 约 {{ demInfo.res_m_approx }} m
          </div>
          <t-tag :theme="demCoverTheme" variant="light">{{ demCoverText }}</t-tag>
        </div>
      </t-form-item>
    </template>

    <t-form-item label="高度缺失时的兜底高(米)">
      <t-input-number v-model="p.default_height" :min="1" :max="1000"
        theme="column" style="width: 140px" />
    </t-form-item>
    <t-form-item label="单瓦片建筑数上限">
      <t-input-number v-model="p.max_per_tile" :min="100" :max="50000" :step="100"
        theme="column" style="width: 140px" />
    </t-form-item>

    <SrsModal v-model:visible="srsVisible" @confirm="onSrsConfirm" />
  </div>
</template>

<style scoped>
.bp { width: 100%; }
.phint { font-size: 12px; color: #94a3b8; line-height: 1.7; }
.up-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; width: 100%; }
.up-ok { font-size: 12px; color: #0f9960; }
.up-none { font-size: 12px; color: #94a3b8; }
.src-box {
  width: 100%; background: #f8fafc; border: 1px solid #e2e8f0;
  border-radius: 4px; padding: 6px 8px;
}
.rb-sub { font-size: 12px; color: #64748b; line-height: 1.7; }
.rb-bad { font-size: 12px; color: #d64541; line-height: 1.7; }
.bld-warn {
  background: #fffbeb; border: 1px solid #fde68a; border-radius: 4px;
  padding: 6px 8px; color: #92400e; font-size: 12px; line-height: 1.6; width: 100%;
}
</style>
