<script setup>
import { ref, computed, onMounted } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { mapController } from '../composables/mapController'
import { useDrawStore } from '../stores/draw'
import { parseVectorFiles, looksLikeLonLat, reprojectGeojson } from '../utils/vector'
import { loadAreaIndex, fetchAreaBoundary } from '../api'
import SrsModal from './SrsModal.vue'

const drawStore = useDrawStore()
const fileInput = ref(null)
const srsVisible = ref(false)
let pendingGeojson = null   // 待用户选源坐标系的 geojson

// ---- 行政区选择 ----
const areaOptions = ref([])   // 级联选项树
const areaValue = ref([])     // 当前选中路径 [省adcode, 市adcode, 县adcode]
const areaLoading = ref(false)

// 把本地索引树转成 t-cascader 选项(label/value/children)
function toCascader(nodes) {
  return (nodes || []).map((n) => {
    const opt = { label: n.name, value: n.adcode }
    if (n.children && n.children.length) opt.children = toCascader(n.children)
    return opt
  })
}

onMounted(async () => {
  try {
    const tree = await loadAreaIndex()
    areaOptions.value = toCascader(tree)
  } catch (e) {
    console.warn('行政区索引加载失败', e)
  }
})

// 选定行政区(取路径最后一级 adcode)后,请求边界并绘制为下载范围
async function onAreaChange(val) {
  const adcode = Array.isArray(val) ? val[val.length - 1] : val
  if (!adcode) return
  areaLoading.value = true
  try {
    const geojson = await fetchAreaBoundary(adcode)
    c()?.loadGeojson(geojson)
  } catch (e) {
    MessagePlugin.error('行政区边界加载失败:' + (e?.message || e))
  } finally {
    areaLoading.value = false
  }
}

const editText = computed(() => (drawStore.editing ? '结束编辑' : '编辑'))
const bboxText = computed(() => {
  const b = drawStore.bbox
  if (!b) return '尚未选择范围'
  return `西:${b[0].toFixed(4)}  南:${b[1].toFixed(4)}\n东:${b[2].toFixed(4)}  北:${b[3].toFixed(4)}`
})

const c = () => mapController.value

function drawRect() { c()?.startDrawRect() }
function drawPolygon() { c()?.startDrawPolygon() }
function toggleEdit() {
  if (!c()?.hasFeature()) { MessagePlugin.warning('请先绘制或导入范围'); return }
  c().toggleEdit()
}
function clearAll() { c()?.clearDraw(); areaValue.value = [] }

function pickFile() { fileInput.value?.click() }

async function onFileChange(e) {
  // 先固化成数组:e.target.files 是活引用,下面 value='' 会清空它
  const files = Array.from(e.target.files || [])
  e.target.value = ''
  try {
    const res = await parseVectorFiles(files)
    if (!res) { MessagePlugin.warning('未解析到矢量内容'); return }
    let { geojson, prjText } = res
    if (!c()) { MessagePlugin.error('地图未就绪,请稍后重试'); return }
    if (looksLikeLonLat(geojson)) {
      c().loadGeojson(geojson)
      MessagePlugin.success('已导入矢量范围')
      return
    }
    if (prjText) {
      try {
        reprojectGeojson(geojson, prjText)
        if (looksLikeLonLat(geojson)) {
          c().loadGeojson(geojson)
          MessagePlugin.success('已导入矢量范围')
          return
        }
      } catch (_) { /* 落到手选 */ }
    }
    // 需要手选源坐标系
    pendingGeojson = geojson
    srsVisible.value = true
  } catch (err) {
    MessagePlugin.error('矢量解析失败:' + (err?.message || err))
  }
}

function onSrsConfirm(srcEpsg) {
  try {
    reprojectGeojson(pendingGeojson, srcEpsg)
    c().loadGeojson(pendingGeojson)
    srsVisible.value = false
    pendingGeojson = null
  } catch (err) {
    MessagePlugin.error('坐标转换失败:' + (err?.message || err))
  }
}
</script>

<template>
  <t-card title="1. 选择范围" :bordered="true" class="panel-card">
    <t-space direction="vertical" size="small" style="width:100%">
      <t-cascader
        v-model="areaValue" :options="areaOptions" clearable filterable
        placeholder="选择省 / 市 / 县行政区" :loading="areaLoading"
        check-strictly @change="onAreaChange"
      />
      <t-space size="small">
        <t-button theme="primary" variant="base" @click="drawRect">绘制矩形</t-button>
        <t-button theme="primary" variant="base" @click="drawPolygon">绘制多边形</t-button>
      </t-space>
      <t-space size="small">
        <t-tooltip content="GeoJSON / KML;Shapefile 选 .zip 或同时选 .shp/.dbf/.shx(建议附 .prj)" placement="top">
          <t-button variant="outline" @click="pickFile">导入矢量</t-button>
        </t-tooltip>
        <t-tooltip content="拖内部平移,拖角点缩放/改形。多边形编辑可点边中点加点、右键顶点删点(≥3)" placement="top">
          <t-button variant="outline" :theme="drawStore.editing ? 'primary' : 'default'"
            :disabled="!drawStore.hasRange" @click="toggleEdit">{{ editText }}</t-button>
        </t-tooltip>
        <t-button variant="outline" theme="default" @click="clearAll">清除</t-button>
      </t-space>
      <input ref="fileInput" type="file" style="display:none" multiple
        accept=".geojson,.json,.kml,.shp,.dbf,.shx,.prj,.zip" @change="onFileChange" />
      <pre class="bbox">{{ bboxText }}</pre>
    </t-space>
  </t-card>

  <SrsModal v-model:visible="srsVisible" @confirm="onSrsConfirm" />
</template>

<style scoped>
.panel-card { margin-bottom: 12px; }
.bbox {
  font-family: Consolas, monospace; font-size: 12px; color: #475569;
  background: #f8fafc; border-radius: 6px; padding: 8px; margin: 0; white-space: pre-wrap;
}
</style>
