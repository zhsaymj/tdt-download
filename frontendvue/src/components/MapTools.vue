<script setup>
/**
 * 地图上的绘制工具条(悬浮于右上)。
 *
 * 只放"在地图上操作"的工具:画矩形/多边形、导入矢量、选行政区、清除。参数设置
 * 一概不放这里——它们属于「处理」对话框。旧版 RangePanel 把范围选择与坐标系
 * 弹窗混在一个 153 行的常驻面板里,这里只留下与地图直接相关的部分。
 */
import { computed, onMounted, ref } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { mapController } from '../composables/mapController'
import { useDrawStore } from '../stores/draw'
import { loadAreaIndex, fetchAreaBoundary } from '../api'
import {
  parseVectorFiles, looksLikeLonLat, reprojectGeojson, geojsonToKml, downloadText,
} from '../utils/vector'
import { formatTimestamp } from '../utils/taskDefaults'
import SrsModal from './SrsModal.vue'

const emit = defineEmits(['request-process'])
const drawStore = useDrawStore()
const c = () => mapController.value

const collapsed = ref(false)
const fileInput = ref(null)
const srsVisible = ref(false)
const pendingGeojson = ref(null)

// ---- 行政区选择 ----
// 用 t-cascader(省/市/县三级)而非普通 select:索引本身是树,平铺成一层几千项没法选。
const areaOptions = ref([])
// check-strictly 允许选中任意一级(省/市/县),此时 v-model 是单个 adcode 而非路径数组
const areaValue = ref(null)
const areaLoading = ref(false)

/** 索引树 → cascader 选项(label/value/children) */
function toCascader(nodes) {
  return (nodes || []).map((n) => {
    const opt = { label: n.name, value: n.adcode }
    if (n.children?.length) opt.children = toCascader(n.children)
    return opt
  })
}

// 挂载即加载:cascader 的 focus 事件不可靠(点箭头展开面板时不一定触发),
// 等到用户点开才加载会出现"面板空着"的情况
onMounted(async () => {
  try {
    areaOptions.value = toCascader(await loadAreaIndex())
  } catch (e) {
    console.warn('行政区索引加载失败', e)
  }
})

/** 编辑范围顶点。没有范围时无从编辑,按钮置灰 */
function toggleEdit() {
  if (!drawStore.hasRange) return
  c()?.toggleEdit()
}

/** 清除:范围与行政区选择一起清,否则下拉还留着上次选的县名 */
function clearAll() {
  c()?.clearDraw()
  areaValue.value = null
}

async function onAreaChange(v) {
  // check-strictly 下 cascader 返回的是**单个 adcode 字符串**,不是路径数组。
  // 若按数组处理,"420000".length 取到 6、v[5] 得到 "0",就会去请求 0.json 而 404。
  const adcode = Array.isArray(v) ? v[v.length - 1] : v
  if (!adcode) return
  areaLoading.value = true
  try {
    const geo = await fetchAreaBoundary(adcode)
    if (geo) c()?.loadGeojson(geo)
  } catch (e) {
    MessagePlugin.error('行政区边界加载失败:' + (e?.message || e))
  } finally {
    areaLoading.value = false
  }
}

// ---- 导入矢量 ----
async function onFiles(e) {
  const files = Array.from(e.target.files || [])
  if (!files.length) return
  try {
    const geo = await parseVectorFiles(files)
    if (!geo) { MessagePlugin.error('未能从文件中解析出图形'); return }
    if (looksLikeLonLat(geo)) {
      c()?.loadGeojson(geo)
    } else {
      // 不像经纬度:多半是投影坐标,让用户指定源坐标系后再转
      pendingGeojson.value = geo
      srsVisible.value = true
    }
  } catch (err) {
    MessagePlugin.error('解析失败:' + (err?.message || err))
  } finally {
    e.target.value = ''       // 允许重复选同一文件
  }
}

/** SrsModal 确认后:按用户选的源坐标系把几何转成 WGS84 再加载 */
function onSrsConfirm(epsg) {
  const geo = pendingGeojson.value
  pendingGeojson.value = null
  srsVisible.value = false
  if (!geo || !epsg) return
  try {
    c()?.loadGeojson(reprojectGeojson(geo, epsg))
  } catch (e) {
    MessagePlugin.error('坐标转换失败:' + (e?.message || e))
  }
}

/** 导出当前范围面为 KML:矩形没有 geometry,用 clipGeometry 现造的矩形环兜底 */
function exportRange() {
  const geom = drawStore.clipGeometry
  if (!geom) { MessagePlugin.warning('当前没有可导出的范围'); return }
  try {
    const name = `范围_${formatTimestamp()}`
    downloadText(`${name}.kml`, geojsonToKml(geom, name))
    MessagePlugin.success('范围已导出为 KML')
  } catch (e) {
    MessagePlugin.error('导出失败:' + (e?.message || e))
  }
}

const rangeText = computed(() => {
  const b = drawStore.bbox
  if (!b) return ''
  const shape = { rect: '矩形', polygon: '多边形', vector: '矢量' }[drawStore.shape] || ''
  return `${shape} ${(b[2] - b[0]).toFixed(3)}° × ${(b[3] - b[1]).toFixed(3)}°`
})
</script>

<template>
  <div class="tools" :class="{ collapsed }">
    <button class="fold" :title="collapsed ? '展开工具' : '收起工具'"
      @click="collapsed = !collapsed">{{ collapsed ? '🧰' : '×' }}</button>

    <template v-if="!collapsed">
      <div class="grp">
        <button class="tool" title="画矩形范围" @click="c()?.startDrawRect()">▭</button>
        <button class="tool" title="画多边形范围" @click="c()?.startDrawPolygon()">⬠</button>
        <button class="tool" :class="{ on: drawStore.editing }"
          :disabled="!drawStore.hasRange"
          title="编辑范围:拖内部平移、拖角点改形;多边形可点边中点加点、右键顶点删点(≥3)"
          @click="toggleEdit">✎</button>
        <button class="tool" title="导入矢量文件(shp/geojson/kml)"
          @click="fileInput?.click()">📁</button>
        <button class="tool" title="清除范围" @click="clearAll">🗑</button>
      </div>

      <t-cascader v-model="areaValue" :options="areaOptions" class="area"
        placeholder="按行政区选择" clearable filterable check-strictly
        :loading="areaLoading" size="small" @change="onAreaChange" />

      <!-- 有范围时直接给出下一步入口:画完就能开始处理,不必再去顶栏菜单 -->
      <div v-if="drawStore.hasRange" class="ready">
        <span class="rtext">{{ rangeText }}</span>
        <div class="racts">
          <t-button size="small" theme="primary" class="ract"
            @click="emit('request-process')">处理此范围</t-button>
          <t-button size="small" variant="outline" class="ract"
            title="把当前范围面导出为 KML 文件" @click="exportRange">导出范围</t-button>
        </div>
      </div>
    </template>

    <input ref="fileInput" type="file" multiple class="hidden"
      accept=".shp,.dbf,.prj,.shx,.cpg,.geojson,.json,.kml" @change="onFiles" />
    <SrsModal v-model:visible="srsVisible" @confirm="onSrsConfirm" />
  </div>
</template>

<style scoped>
.tools {
  /* right 随右侧面板宽度避让(--pad-right 由 App.vue 下传),否则面板一开就把
     工具条完全盖住——而"边开面板边画范围"正是这些面板不做模态的原因 */
  position: absolute; top: 10px; right: calc(10px + var(--pad-right, 0px));
  z-index: 20; transition: right .22s ease;
  display: flex; flex-direction: column; gap: 8px;
  background: rgba(255, 255, 255, .96); border: 1px solid #dbe3ec;
  border-radius: 8px; padding: 8px; width: 210px;
  box-shadow: 0 2px 10px rgba(15, 23, 42, .12);
}
.tools.collapsed { width: auto; padding: 4px; }
/* 5 个按钮 + 右上折叠位:给足宽度,否则图标会被挤扁 */
.tools:not(.collapsed) { width: 236px; }
.fold {
  position: absolute; top: 4px; right: 4px;
  border: 0; background: transparent; cursor: pointer;
  color: #94a3b8; font-size: 14px; line-height: 1; padding: 2px 4px;
}
.tools.collapsed .fold { position: static; font-size: 18px; }
.fold:hover { color: #475569; }
.grp { display: flex; gap: 4px; padding-right: 22px; }
.tool {
  flex: 1 1 auto; border: 1px solid #dbe3ec; background: #fff;
  border-radius: 6px; cursor: pointer; font-size: 15px;
  padding: 5px 0; color: #334155; transition: background .15s, border-color .15s;
}
.tool:hover:not(:disabled) { background: #f0f9ff; border-color: #7dd3fc; }
/* 编辑中:高亮提示当前处于可拖拽状态,否则用户不知道自己在编辑模式 */
.tool.on { background: #0284c7; border-color: #0284c7; color: #fff; }
.tool:disabled { opacity: .4; cursor: not-allowed; }
.area { width: 100%; }
.ready {
  display: flex; flex-direction: column; gap: 6px;
  border-top: 1px solid #eef2f7; padding-top: 8px;
}
.rtext { font-size: 12px; color: #0369a1; }
/* 处理/导出各占一半宽:两个动作等权,不该让主按钮独占整行 */
.racts { display: flex; gap: 6px; }
.ract { flex: 1 1 0; min-width: 0; }
.hidden { display: none; }
</style>
