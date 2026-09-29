<template>
  <div id="app" class="app">
    <Viewer3D
      ref="viewerRef"
      :auto-load-tileset="true"
      :tileset-url="defaultTilesetUrl"
      @viewer-ready="onViewerReady"
      @tileset-loaded="onTilesetLoaded"
      @tileset-error="onTilesetError"
    >
      <ViewerToolbar
        @reset-view="resetView"
        @toggle-inspector="toggleInspector"
      />
      <InspectorPanel
        v-if="showInspector"
        :position="inspectorPosition"
        @close="showInspector = false"
        @update-debug="updateDebugOptions"
      />
      <FeatureInfo
        v-if="selectedFeature"
        :feature="selectedFeature"
        @close="selectedFeature = null"
      />
    </Viewer3D>
  </div>
</template>

<script setup lang="ts">
import { shallowRef, ref, onMounted } from 'vue';
import * as Cesium from 'cesium';
import Viewer3D from './components/3d-viewer/Viewer3D.vue';
import ViewerToolbar from './components/controls/ViewerToolbar.vue';
import InspectorPanel from './components/controls/InspectorPanel.vue';
import FeatureInfo from './components/controls/FeatureInfo.vue';
import { useFeaturePicking } from './composables/useFeaturePicking';
import { useCamera } from './composables/useCamera';
import type { FeatureInfo as FeatureInfoType, TilesetInfo } from './types';

const defaultTilesetUrl = '/output/tileset.json';

const viewerRef = shallowRef<{ viewer: Cesium.Viewer | null } | null>(null);
const viewer = shallowRef<Cesium.Viewer | null>(null);
const tileset = shallowRef<Cesium.Cesium3DTileset | null>(null);
const tilesetInfo = shallowRef<TilesetInfo | null>(null);

const showInspector = ref(true);
const inspectorPosition = ref({ x: window.innerWidth - 320, y: 80 });
const selectedFeature = shallowRef<FeatureInfoType | null>(null);

const { enablePicking } = useFeaturePicking(viewer);
const { resetView: resetCameraView } = useCamera(viewer);

function onViewerReady(viewerInstance: Cesium.Viewer) {
  viewer.value = viewerInstance;
  console.log('Viewer ready:', viewerInstance);

  enablePicking();
}

function onTilesetLoaded(tilesetInstance: Cesium.Cesium3DTileset) {
  tileset.value = tilesetInstance;
  console.log('Tileset loaded:', tilesetInstance);
}

function onTilesetError(error: Error) {
  console.error('Tileset error:', error);
}

function resetView() {
  resetCameraView();
}

function toggleInspector() {
  showInspector.value = !showInspector.value;
}

function updateDebugOptions(options: {
  showBoundingVolume: boolean;
  showContentBoundingVolume: boolean;
  wireframe: boolean;
}) {
  if (!tileset.value) return;

  tileset.value.debugShowBoundingVolume = options.showBoundingVolume;
  tileset.value.debugShowContentBoundingVolume = options.showContentBoundingVolume;
  tileset.value.debugWireframe = options.wireframe;
}

onMounted(() => {
  showInspector.value = true;
});
</script>

<style>
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

html, body {
  width: 100%;
  height: 100%;
  overflow: hidden;
}

#app {
  width: 100vw;
  height: 100vh;
  position: relative;
}

.app {
  width: 100%;
  height: 100%;
}
</style>
