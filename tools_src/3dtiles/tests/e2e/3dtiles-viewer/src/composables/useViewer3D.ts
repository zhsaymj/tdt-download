import { shallowRef, onUnmounted } from 'vue';
import * as Cesium from 'cesium';
import type { Viewer3DConfig } from '../types';

export function useViewer3D() {
  const viewer = shallowRef<Cesium.Viewer | null>(null);
  const isReady = shallowRef<boolean>(false);

  async function initViewer(config: Viewer3DConfig = {}) {
    const container = config.container || 'viewer3dContainer';

    const viewerOptions: Cesium.Viewer.ConstructorOptions = {
      baseLayerPicker: config.baseLayerPicker ?? false,
      geocoder: config.geocoder ?? false,
      homeButton: config.homeButton ?? false,
      sceneModePicker: config.sceneModePicker ?? false,
      navigationHelpButton: config.navigationHelpButton ?? false,
      animation: config.animation ?? false,
      timeline: config.timeline ?? false,
      fullscreenButton: config.fullscreenButton ?? false,
      vrButton: config.vrButton ?? false,
      infoBox: config.infoBox ?? false,
      selectionIndicator: config.selectionIndicator ?? false,
      shadows: config.shadows ?? true,
      shouldAnimate: config.shouldAnimate ?? true,
      terrainProvider: undefined
    };

    if (config.baseLayer === false) {
      viewerOptions.baseLayer = false;
    }

    viewer.value = new Cesium.Viewer(container, viewerOptions);

    viewer.value.scene.screenSpaceCameraController.enableCollisionDetection = false;
    viewer.value.scene.logarithmicDepthBuffer = true;
    viewer.value.camera.frustum.near = 0.1;

    if (config.terrain !== false) {
      try {
        const terrainProvider = await Cesium.createWorldTerrainAsync({
          requestVertexNormals: true,
          requestWaterMask: true
        });
        viewer.value.terrainProvider = terrainProvider;
        console.log('Terrain loaded successfully');
      } catch (error) {
        console.warn('Failed to load terrain:', error);
      }
    }

    viewer.value.scene.globe.depthTestAgainstTerrain = false;

    isReady.value = true;
  }

  function destroyViewer() {
    if (viewer.value) {
      viewer.value.destroy();
      viewer.value = null;
    }
    isReady.value = false;
  }

  onUnmounted(() => {
    destroyViewer();
  });

  return {
    viewer,
    isReady,
    initViewer,
    destroyViewer
  };
}
