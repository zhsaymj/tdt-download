<script setup>
/**
 * DEM → Cesium 地形瓦片工具的独立页面骨架,复用主站(App.vue)的整体框架:
 * 顶栏 + 地图满屏 + 浮动工具条/任务详情/状态条,只是顶栏入口与两个浮动面板的
 * 内容收窄为"地形"单一数据源——不需要数据/服务/密钥/日志/关于等其它数据源
 * 才需要的面板。
 *
 * 直接复用主站现成组件:
 * - MapView.vue:地图容器,内部用 mapController 单例;本页面与 download.html
 *   是各自独立的 Vue 应用实例 + 独立的 <script type="module"> 入口,各自拥有
 *   独立的 JS 模块图,mapController 这个模块级 shallowRef 容器不会跨页面共享
 *   (已读 composables/mapController.js 源码确认:纯 `shallowRef(null)`,
 *   没有任何跨页通信或广播逻辑,每次页面加载都是全新的模块实例)。
 * - MapTools.vue:绘制工具条,只依赖 mapController/drawStore/行政区索引/矢量
 *   导入这些通用逻辑,唯一对外事件是通用语义的 request-process,没有耦合
 *   ProcessDialog 专属的东西,可以直接复用。
 * - MeasurePanel.vue:量测面板(点坐标/线距离/面面积),只依赖 mapController,
 *   与绘制工具条分开悬浮,同样是通用组件,可以直接复用。
 * - TaskDetail.vue:单任务详情悬浮卡片,只依赖 useTaskStore/mapController/
 *   isDemProvider 等通用判定,对地形任务(esri_terrain/local_dem)走的正是
 *   isDemProvider 分支,是通用组件,可以直接复用。
 * - AppStatusBar.vue:底部状态条,只依赖 useTaskStore/useMapInfoStore,通用。
 *
 * 顶栏未复用 AppTopBar.vue——它内置了四数据源下拉菜单与数据/服务/密钥/日志/
 * 关于入口,这些都是地形页面不需要的;照抄其视觉风格(深色渐变/按钮样式)自写
 * 一个精简顶栏,不去改动或扩展主站组件,避免牵连主站功能。
 */
import { computed, ref } from 'vue'
import { useTaskStore } from '../stores/task'
import MapView from '../components/MapView.vue'
import MapTools from '../components/MapTools.vue'
import MeasurePanel from '../components/MeasurePanel.vue'
import TaskDetail from '../components/TaskDetail.vue'
import AppStatusBar from '../components/AppStatusBar.vue'
import TerrainTaskForm from './TerrainTaskForm.vue'
import TerrainTaskList from './TerrainTaskList.vue'

const taskStore = useTaskStore()

/** 顶栏「任务」角标:进行中的地形任务数,不必打开面板就知道有没有在跑 */
const activeCount = computed(() => taskStore.tasks.filter(
  (t) => ['running', 'pending'].includes(t.status)).length)

const formVisible = ref(false)
const listVisible = ref(false)

/**
 * 两个面板互斥停靠同一张地图:新建面板停左、任务面板停右,本不会互相遮挡,
 * 但仍参照 App.vue 的 padStyle 机制下传 --pad-left/--pad-right,让地图上的
 * 绘制工具条、任务详情卡片随面板开合自动避让——否则任务面板一开就把右上角
 * 的绘制工具条盖住。
 */
const PANEL_L = 440
const PANEL_R = 440
const padStyle = computed(() => ({
  '--pad-left': (formVisible.value ? PANEL_L : 0) + 'px',
  '--pad-right': (listVisible.value ? PANEL_R : 0) + 'px',
}))

function openForm() { formVisible.value = true }
function openList() { listVisible.value = true }
function onCreated() { listVisible.value = true }
</script>

<template>
  <div class="app">
    <header class="topbar">
      <div class="brand">
        <span class="logo">⛰️</span>
        <span class="title">DEM 地形切片工具</span>
      </div>

      <nav class="menus">
        <button class="nav-btn primary" @click="openForm">+ 新建地形任务</button>
        <button class="nav-btn" @click="openList">
          任务<span v-if="activeCount" class="badge">{{ activeCount }}</span>
        </button>
      </nav>

      <nav class="actions">
        <a class="nav-link" href="/download.html">主下载工具</a>
      </nav>
    </header>

    <main class="map-main" :style="padStyle">
      <MapView />
      <MapTools @request-process="openForm" />
      <!-- 量测与绘制分开(与主站同理):产出读数,不进任务参数 -->
      <MeasurePanel />
      <TaskDetail />

      <!-- 侧面板放在地图容器内,靠 absolute 相对地图定位、浮在地图上,不加遮罩——
           用户需要边开着「新建」面板边在地图上画范围 -->
      <TerrainTaskForm v-model:visible="formVisible" @created="onCreated" />
      <TerrainTaskList v-model:visible="listVisible" />
    </main>

    <AppStatusBar @open-tasks="openList" />
  </div>
</template>

<style scoped>
/* 用 100vh/100vw 而非 100%:全局 style.css 只给 html/body/#app 声明了
   height:100%,本页挂载点是 #terrain,不在那份名单里,height:100% 链路会在
   此断掉导致地图高度塌缩为 0。改用视口单位可以不依赖那份全局规则,也不需要
   为此去改动边界之外的 style.css。 */
.app {
  display: flex; flex-direction: column; height: 100vh; width: 100vw;
  overflow: hidden;
}

/* 深色渐变顶栏,照抄 AppTopBar.vue 的视觉风格,保持两页一致 */
.topbar {
  flex: 0 0 44px; height: 44px;
  display: flex; align-items: center; gap: 18px;
  padding: 0 14px;
  background: linear-gradient(90deg, #1e293b, #0f172a);
  border-bottom: 1px solid #0b1220;
  box-shadow: 0 1px 6px rgba(0, 0, 0, .25);
  user-select: none;
}
.brand { display: flex; align-items: center; gap: 8px; flex: 0 0 auto; }
.logo { font-size: 18px; line-height: 1; }
.title { font-size: 15px; font-weight: 700; letter-spacing: .5px; color: #f1f5f9; }
.menus { display: flex; align-items: center; gap: 2px; flex: 1 1 auto; }
.actions { display: flex; align-items: center; gap: 2px; flex: 0 0 auto; }
.nav-btn {
  border: 0; background: transparent; cursor: pointer;
  color: #cbd5e1; font-size: 13px; font-family: inherit;
  padding: 6px 12px; border-radius: 6px;
  transition: background .15s, color .15s;
  display: inline-flex; align-items: center; gap: 6px;
}
.nav-btn:hover { background: rgba(148, 163, 184, .18); color: #fff; }
.nav-btn:active { background: rgba(148, 163, 184, .28); }
.nav-btn.primary { color: #7dd3fc; font-weight: 600; }
.nav-btn.primary:hover { background: rgba(56, 189, 248, .18); color: #e0f2fe; }
.badge {
  min-width: 16px; height: 16px; padding: 0 4px;
  border-radius: 8px; background: #0284c7; color: #fff;
  font-size: 11px; line-height: 16px; text-align: center;
}
.nav-link { color: rgba(255, 255, 255, .85); font-size: 13px; text-decoration: none; }
.nav-link:hover { color: #fff; text-decoration: underline; }

/* 地图占满顶栏与状态栏之间的全部空间;悬浮元素靠 absolute 定位其上。
   overflow:hidden 必须有:侧面板滑入时起始状态是 translateX(±100%),
   会短暂伸到容器外把文档撑宽 → 底部冒出横向滚动条、界面跟着抖一下。 */
.map-main {
  flex: 1 1 auto; position: relative; min-height: 0; width: 100%;
  overflow: hidden;
}
</style>