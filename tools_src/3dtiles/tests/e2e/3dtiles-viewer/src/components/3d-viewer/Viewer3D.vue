<template>
  <div id="viewer3dContainer" class="viewer3d-container">
    <slot></slot>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, provide, computed } from 'vue';
import * as Cesium from 'cesium';
import { useViewer3D } from '../../composables/useViewer3D';
import { useTileset } from '../../composables/useTileset';
import { useCamera } from '../../composables/useCamera';
import { usePerformance } from '../../composables/usePerformance';
import { TILESETS, DEFAULT_TILESET } from '../../config/tilesets.config';
import type { TilesetInfo } from '../../types';

const props = defineProps<{
  config?: any;
  autoLoadTileset?: boolean;
  tilesetUrl?: string;
}>();

const emit = defineEmits<{
  (e: 'viewer-ready', viewer: Cesium.Viewer): void;
  (e: 'tileset-loaded', tileset: Cesium.Cesium3DTileset): void;
  (e: 'tileset-error', error: Error): void;
}>();

const { viewer, isReady, initViewer, destroyViewer } = useViewer3D();
const { tileset, isLoading, error, tilesetInfo, groundDistance, loadTileset, unloadTileset, flyToTileset } = useTileset(viewer);
const { cameraInfo, startTracking: startCameraTracking } = useCamera(viewer);
const { metrics, startMonitoring: startPerformanceMonitoring } = usePerformance(viewer);

provide('viewer', computed(() => viewer.value));
provide('tileset', computed(() => tileset.value));
provide('tilesetInfo', computed(() => tilesetInfo.value));
provide('cameraInfo', computed(() => cameraInfo.value));
provide('performanceMetrics', computed(() => metrics.value));
provide('groundDistance', computed(() => groundDistance.value));

onMounted(async () => {
  await initViewer(props.config || {});

  if (viewer.value) {
    emit('viewer-ready', viewer.value);
    startCameraTracking();
    startPerformanceMonitoring();
  }

  if (props.autoLoadTileset !== false) {
    const url = props.tilesetUrl || (TILESETS[DEFAULT_TILESET]?.url || '');

    try {
      await loadTileset(url, TILESETS[DEFAULT_TILESET]);

      if (tileset.value) {
        emit('tileset-loaded', tileset.value);
        await flyToTileset();
      }
    } catch (err) {
      emit('tileset-error', err as Error);
    }
  }
});

onUnmounted(() => {
  destroyViewer();
});

defineExpose({
  viewer,
  tileset,
  isReady
});
</script>

<style scoped>
.viewer3d-container {
  width: 100%;
  height: 100%;
  position: relative;
}
</style>
