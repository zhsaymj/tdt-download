import { shallowRef, onUnmounted, type Ref } from 'vue';
import * as Cesium from 'cesium';
import type { TilesetConfig, TilesetInfo } from '../types';

export interface GroundDistance {
  minHeight: number;
  maxHeight: number;
  centerHeight: number;
  groundHeight: number;
  distanceToGround: number;
}

export function useTileset(viewer: Ref<Cesium.Viewer | null>) {
  const tileset = shallowRef<Cesium.Cesium3DTileset | null>(null);
  const isLoading = shallowRef<boolean>(false);
  const error = shallowRef<Error | null>(null);
  const tilesetInfo = shallowRef<TilesetInfo | null>(null);
  const groundDistance = shallowRef<GroundDistance | null>(null);

  async function loadTileset(url: string, options?: TilesetConfig) {
    if (!viewer.value) {
      error.value = new Error('Viewer not initialized');
      return;
    }

    isLoading.value = true;
    error.value = null;

    try {
      const tilesetOptions: Cesium.Cesium3DTileset.ConstructorOptions = {
        maximumScreenSpaceError: options?.maximumScreenSpaceError ?? 16,
        skipLevelOfDetail: options?.skipLevelOfDetail ?? true,
        skipLevels: options?.skipLevels ?? 0,
        immediatelyLoadDesiredLevelOfDetail: options?.immediatelyLoadDesiredLevelOfDetail ?? false,
        loadSiblings: options?.loadSiblings ?? false,
        cullWithChildrenBounds: options?.cullWithChildrenBounds ?? true,
        dynamicScreenSpaceError: options?.dynamicScreenSpaceError ?? false,
      };

      const newTileset = await Cesium.Cesium3DTileset.fromUrl(url, tilesetOptions);

      viewer.value.scene.primitives.add(newTileset);
      tileset.value = newTileset;

      tilesetInfo.value = {
        url: url,
        version: (newTileset.asset as any)?.version || '1.0',
        gltfUpAxis: (newTileset.asset as any)?.gltfUpAxis || 'Y',
        geometricError: newTileset.root?.geometricError || 0,
        boundingSphere: newTileset.boundingSphere
      };

      calculateGroundDistance();

      return newTileset;
    } catch (err) {
      error.value = err as Error;
      throw err;
    } finally {
      isLoading.value = false;
    }
  }

  function calculateGroundDistance(): void {
    if (!tileset.value || !viewer.value) {
      groundDistance.value = null;
      return;
    }

    const boundingSphere = tileset.value.boundingSphere;
    const center = boundingSphere.center;
    const radius = boundingSphere.radius;

    const centerCartographic = Cesium.Cartographic.fromCartesian(center);
    const centerHeight = centerCartographic.height;
    const longitude = centerCartographic.longitude;
    const latitude = centerCartographic.latitude;

    const positions = [
      Cesium.Cartographic.fromRadians(longitude, latitude)
    ];

    const promise = Cesium.sampleTerrainMostDetailed(
      viewer.value.terrainProvider,
      positions
    );

    promise.then((samples) => {
      if (samples && samples.length > 0) {
        const groundHeight = samples[0]?.height ?? 0;
        const distanceToGround = centerHeight - groundHeight;
        const minHeight = centerHeight - radius;
        const maxHeight = centerHeight + radius;

        groundDistance.value = {
          minHeight,
          maxHeight,
          centerHeight,
          groundHeight,
          distanceToGround
        };
      }
    }).catch(() => {
      const groundHeight = 0;
      const distanceToGround = centerHeight - groundHeight;
      const minHeight = centerHeight - radius;
      const maxHeight = centerHeight + radius;

      groundDistance.value = {
        minHeight,
        maxHeight,
        centerHeight,
        groundHeight,
        distanceToGround
      };
    });
  }

  function unloadTileset() {
    if (tileset.value && viewer.value) {
      viewer.value.scene.primitives.remove(tileset.value);
      tileset.value = null;
    }
    tilesetInfo.value = null;
  }

  async function flyToTileset(duration: number = 2) {
    if (!tileset.value || !viewer.value) return;

    await viewer.value.zoomTo(tileset.value, new Cesium.HeadingPitchRange(
      0,
      -0.5,
      tileset.value.boundingSphere.radius * 2.0
    ));
  }

  function setDebugOptions(options: Partial<InspectorState>) {
    if (!tileset.value) return;

    const t = tileset.value;
    if ('debugWireframe' in options) t.debugWireframe = options.debugWireframe as boolean;
    if ('debugShowBoundingVolume' in options) t.debugShowBoundingVolume = options.debugShowBoundingVolume as boolean;
    if ('debugShowContentBoundingVolume' in options) t.debugShowContentBoundingVolume = options.debugShowContentBoundingVolume as boolean;
    if ('debugShowViewerRequestVolume' in options) t.debugShowViewerRequestVolume = options.debugShowViewerRequestVolume as boolean;
    if ('debugColorizeTiles' in options) t.debugColorizeTiles = options.debugColorizeTiles as boolean;
    if ('debugShowMemoryUsage' in options) t.debugShowMemoryUsage = options.debugShowMemoryUsage as boolean;
  }

  onUnmounted(() => {
    unloadTileset();
  });

  return {
    tileset,
    isLoading,
    error,
    tilesetInfo,
    groundDistance,
    loadTileset,
    unloadTileset,
    flyToTileset,
    setDebugOptions,
    calculateGroundDistance
  };
}

interface InspectorState {
  debugWireframe?: boolean;
  debugShowBoundingVolume?: boolean;
  debugShowContentBoundingVolume?: boolean;
  debugShowViewerRequestVolume?: boolean;
  debugColorizeTiles?: boolean;
  debugShowMemoryUsage?: boolean;
}
