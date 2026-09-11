<script setup>
/**
 * 应用骨架:地图为主视图,功能收进顶栏菜单与弹窗。
 *
 * 旧布局左侧参数面板 400px + 右侧任务队列 320px 常驻,地图不到总宽一半。而参数
 * 大多"设一次不再动"、队列大部分时间在显示已完成任务的历史进度,都不值得常驻。
 * 现在地图占满,只保留两个悬浮元素(图层面板、绘制工具)与一行状态栏。
 */
import { computed, onMounted, ref } from 'vue'
import { registerProj4Defs } from './utils/crs'
import AppTopBar from './components/AppTopBar.vue'
import AppStatusBar from './components/AppStatusBar.vue'
import MapView from './components/MapView.vue'
import LayerPanel from './components/LayerPanel.vue'
import MapTools from './components/MapTools.vue'
import MeasurePanel from './components/MeasurePanel.vue'
import ProcessDialog from './components/ProcessDialog.vue'
import DataDialog from './components/DataDialog.vue'
import TaskDialog from './components/TaskDialog.vue'
import TaskDetail from './components/TaskDetail.vue'
import TokenManager from './components/TokenManager.vue'
import LogDrawer from './components/LogDrawer.vue'
import AboutDialog from './components/AboutDialog.vue'

const tokenMgrVisible = ref(false)
const logVisible = ref(false)
const aboutVisible = ref(false)
const dataVisible = ref(false)
const taskVisible = ref(false)

// 处理面板:三种数据来源(下载/本地栅格/本地矢量)共用它,由 source 区分。
// 统一一个面板而非各来源一套,是为了让"选格式"这件事只有一份实现——
// 旧版格式定义散在 2 个文件、容器选择散在 3 个文件。
const processVisible = ref(false)
const processSource = ref(null)

function openProcess(source) {
  processSource.value = source
  processVisible.value = true
}

/**
 * 成果图层清单里的「转 COG」:把那个 tif 当**本地文件源**处理、导出 COG。
 * 不走补充导出——后端不允许改已导出阶段的容器格式(成果已按旧格式写出,
 * 就地替换会让 metadata 与磁盘文件对不上)。
 */
function onConvertCog(path) {
  openProcess({ kind: 'local_raster', path, preferCog: true })
}

// 右侧两个面板互斥:它们停靠同一边,同时开只会互相盖住
function openData() { taskVisible.value = false; dataVisible.value = true }
function openTasks() { dataVisible.value = false; taskVisible.value = true }

function onTaskCreated() {
  openTasks()
}

const PANEL_L = 400          // 与 ProcessDialog 的 width 一致
const PANEL_R = 440          // 与 DataDialog / TaskDialog 的 width 一致

/**
 * 把面板宽度作为 CSS 变量下传,让地图上的悬浮元素(工具条、图层面板、任务详情)
 * 自动避让——否则右侧面板一开就把右上角的绘制工具完全盖住,而"边开面板边画范围"
 * 正是不用模态抽屉的原因。
 */
const padStyle = computed(() => ({
  '--pad-left': (processVisible.value ? PANEL_L : 0) + 'px',
  '--pad-right': ((dataVisible.value || taskVisible.value) ? PANEL_R : 0) + 'px',
}))

onMounted(() => registerProj4Defs())
</script>

<template>
  <div class="app">
    <AppTopBar
      @new-download="openProcess({ kind: 'download' })"
      @new-local="openProcess({ kind: 'local_raster' })"
      @new-vector="openProcess({ kind: 'local_vector' })"
      @open-data="openData"
      @open-tasks="openTasks"
      @open-tokens="tokenMgrVisible = true"
      @open-logs="logVisible = true"
      @open-about="aboutVisible = true"
    />

    <main class="map-main" :style="padStyle">
      <MapView />
      <MapTools @request-process="openProcess({ kind: 'download' })" />
      <!-- 量测与绘制分开:前者产出读数,后者产出下载范围,合在一起容易混淆 -->
      <MeasurePanel />
      <!-- 图层面板只管已叠上的图层;「从哪份成果挑图层」在「数据」面板里 -->
      <LayerPanel @open-data="openData" />
      <TaskDetail />

      <!-- 侧面板放在地图容器内:它们靠 absolute 相对地图定位、浮在地图上,
           不加遮罩——用户需要边开着面板边在地图上画范围、看图层变化 -->
      <ProcessDialog v-model:visible="processVisible" :source="processSource"
        @created="onTaskCreated" />
      <!-- 非 tiled 的大图不能直读,成果的图层清单里给「转 COG」入口 -->
      <DataDialog v-model:visible="dataVisible" @process="openProcess"
        @convert-cog="onConvertCog" />
      <TaskDialog v-model:visible="taskVisible" />
    </main>

    <AppStatusBar @open-tasks="openTasks" />

    <TokenManager v-model:visible="tokenMgrVisible" />
    <LogDrawer v-model:visible="logVisible" />
    <AboutDialog v-model:visible="aboutVisible" />
  </div>
</template>

<style scoped>
.app {
  display: flex; flex-direction: column; height: 100%; width: 100%;
  overflow: hidden;
}
/* 地图占满顶栏与状态栏之间的全部空间;悬浮元素靠 absolute 定位其上。
   overflow:hidden 必须有:侧面板滑入时起始状态是 translateX(±100%),
   会短暂伸到容器外把文档撑宽 → 底部冒出横向滚动条、界面跟着抖一下。 */
.map-main {
  flex: 1 1 auto; position: relative; min-height: 0; width: 100%;
  overflow: hidden;
}
</style>
