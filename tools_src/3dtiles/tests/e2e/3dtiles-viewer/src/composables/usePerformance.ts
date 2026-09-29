import { shallowRef, onUnmounted, type Ref } from 'vue';
import * as Cesium from 'cesium';
import type { PerformanceMetrics } from '../types';

export function usePerformance(viewer: Ref<Cesium.Viewer | null>) {
  const metrics = shallowRef<PerformanceMetrics>({
    fps: 0,
    drawCalls: 0,
    triangles: 0,
    vertices: 0,
    memory: 0,
    tiles: 0,
    cacheSize: 0
  });

  const isMonitoring = shallowRef<boolean>(false);
  let updateInterval: number | null = null;
  let frameCount = 0;
  let lastTime = performance.now();
  let removeCallback: (() => void) | null = null;

  function updateMetrics() {
    if (!viewer.value) return;

    const scene = viewer.value.scene;
    const currentTime = performance.now();
    frameCount++;

    if (currentTime - lastTime >= 1000) {
      metrics.value = {
        ...metrics.value,
        fps: frameCount
      };
      frameCount = 0;
      lastTime = currentTime;
    }

    let tileCount = 0;
    const primitives = scene.primitives;
    for (let i = 0; i < primitives.length; i++) {
      const primitive = primitives.get(i);
      if ((primitive as any).tileset) {
        tileCount++;
      }
    }

    metrics.value = {
      ...metrics.value,
      tiles: tileCount,
      memory: (performance as any).memory?.usedJSHeapSize || 0
    };
  }

  function startMonitoring(interval: number = 1000) {
    if (isMonitoring.value || !viewer.value) return;

    isMonitoring.value = true;

    removeCallback = viewer.value.scene.postRender.addEventListener(updateMetrics);
    updateInterval = window.setInterval(updateMetrics, interval);
  }

  function stopMonitoring() {
    if (!isMonitoring.value) return;

    isMonitoring.value = false;

    if (removeCallback) {
      removeCallback();
      removeCallback = null;
    }

    if (updateInterval !== null) {
      clearInterval(updateInterval);
      updateInterval = null;
    }
  }

  function resetMetrics() {
    metrics.value = {
      fps: 0,
      drawCalls: 0,
      triangles: 0,
      vertices: 0,
      memory: 0,
      tiles: 0,
      cacheSize: 0
    };
  }

  onUnmounted(() => {
    stopMonitoring();
  });

  return {
    metrics,
    isMonitoring,
    startMonitoring,
    stopMonitoring,
    resetMetrics
  };
}
