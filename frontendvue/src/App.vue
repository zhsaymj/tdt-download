<script setup>
import { onMounted, ref } from 'vue'
import { registerProj4Defs } from './utils/crs'
import TopBar from './components/TopBar.vue'
import StatusBar from './components/StatusBar.vue'
import MapView from './components/MapView.vue'
import RangePanel from './components/RangePanel.vue'
import ParamsPanel from './components/ParamsPanel.vue'
import TaskQueue from './components/TaskQueue.vue'
import TaskDetail from './components/TaskDetail.vue'
import TokenManager from './components/TokenManager.vue'
import LogDrawer from './components/LogDrawer.vue'
import AboutDialog from './components/AboutDialog.vue'

const tokenMgrVisible = ref(false)
const logVisible = ref(false)
const aboutVisible = ref(false)

onMounted(() => registerProj4Defs())
</script>

<template>
  <div class="app">
    <TopBar
      @open-tokens="tokenMgrVisible = true"
      @open-logs="logVisible = true"
      @open-about="aboutVisible = true"
    />

    <div class="body">
      <aside class="left-panel">
        <RangePanel />
        <ParamsPanel />
      </aside>

      <main class="map-main">
        <MapView />
        <TaskDetail />
      </main>

      <TaskQueue />
    </div>

    <StatusBar />

    <TokenManager v-model:visible="tokenMgrVisible" />
    <LogDrawer v-model:visible="logVisible" />
    <AboutDialog v-model:visible="aboutVisible" />
  </div>
</template>

<style scoped>
.app { display: flex; flex-direction: column; height: 100%; width: 100%; }
.body { flex: 1 1 auto; display: flex; min-height: 0; }
.left-panel {
  width: 400px; height: 100%; overflow-y: auto;
  background: #f8fbff; border-right: 1px solid #e2e8f0; padding: 10px;
}
.map-main { flex: 1; height: 100%; position: relative; min-width: 0; }
</style>
