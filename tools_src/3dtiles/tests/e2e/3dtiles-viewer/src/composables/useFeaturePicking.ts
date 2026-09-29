import { shallowRef, onUnmounted, type Ref } from 'vue';
import * as Cesium from 'cesium';
import type { FeatureInfo } from '../types';

export function useFeaturePicking(viewer: Ref<Cesium.Viewer | null>) {
  const selectedFeature = shallowRef<FeatureInfo | null>(null);
  const hoveredFeature = shallowRef<FeatureInfo | null>(null);
  const isPickingEnabled = shallowRef<boolean>(true);

  let clickHandler: Cesium.ScreenSpaceEventHandler | null = null;
  let hoverHandler: Cesium.ScreenSpaceEventHandler | null = null;

  function createFeatureInfo(pickedObject: any): FeatureInfo | null {
    if (!pickedObject) return null;

    if (pickedObject instanceof Cesium.Cesium3DTileFeature) {
      const properties: Record<string, any> = {};
      const propertyIds = pickedObject.getPropertyIds();
      if (propertyIds) {
        propertyIds.forEach((id: string) => {
          properties[id] = pickedObject.getProperty(id);
        });
      }

      return {
        id: pickedObject.featureId?.toString() || '',
        type: 'Cesium3DTileFeature',
        properties,
        primitive: pickedObject.primitive
      };
    }

    if (pickedObject.id instanceof Cesium.Entity) {
      const entity = pickedObject.id;
      const properties: Record<string, any> = {};
      
      if (entity.properties) {
        entity.properties.propertyNames.forEach((name: string) => {
          properties[name] = entity.properties?.getValue(Cesium.JulianDate.now());
        });
      }

      return {
        id: entity.id?.toString() || entity.name || '',
        type: 'Entity',
        properties,
        position: entity.position?.getValue(Cesium.JulianDate.now())
      };
    }

    return null;
  }

  function onLeftClick(movement: Cesium.ScreenSpaceEventHandler.PositionedEvent) {
    if (!viewer.value || !isPickingEnabled.value) return;

    const pickedObject = viewer.value.scene.pick(movement.position);

    if (Cesium.defined(pickedObject)) {
      selectedFeature.value = createFeatureInfo(pickedObject);
    } else {
      selectedFeature.value = null;
    }
  }

  function onMouseMove(movement: Cesium.ScreenSpaceEventHandler.MotionEvent) {
    if (!viewer.value || !isPickingEnabled.value) return;

    const pickedObject = viewer.value.scene.pick(movement.endPosition);

    if (Cesium.defined(pickedObject)) {
      hoveredFeature.value = createFeatureInfo(pickedObject);
    } else {
      hoveredFeature.value = null;
    }
  }

  function enablePicking() {
    if (!viewer.value) return;

    if (!clickHandler) {
      clickHandler = new Cesium.ScreenSpaceEventHandler(viewer.value.canvas);
      clickHandler.setInputAction(onLeftClick, Cesium.ScreenSpaceEventType.LEFT_CLICK);
    }

    if (!hoverHandler) {
      hoverHandler = new Cesium.ScreenSpaceEventHandler(viewer.value.canvas);
      hoverHandler.setInputAction(onMouseMove, Cesium.ScreenSpaceEventType.MOUSE_MOVE);
    }

    isPickingEnabled.value = true;
  }

  function disablePicking() {
    if (clickHandler) {
      clickHandler.destroy();
      clickHandler = null;
    }

    if (hoverHandler) {
      hoverHandler.destroy();
      hoverHandler = null;
    }

    isPickingEnabled.value = false;
  }

  function clearSelection() {
    selectedFeature.value = null;
  }

  function clearHover() {
    hoveredFeature.value = null;
  }

  function selectFeature(feature: FeatureInfo) {
    selectedFeature.value = feature;
  }

  async function flyToFeature(feature: FeatureInfo, duration: number = 2) {
    if (!viewer.value) return;

    if (feature.position) {
      viewer.value.camera.flyTo({
        destination: feature.position,
        duration
      });
    } else if (feature.primitive) {
      await viewer.value.zoomTo(feature.primitive);
    }
  }

  onUnmounted(() => {
    disablePicking();
  });

  return {
    selectedFeature,
    hoveredFeature,
    isPickingEnabled,
    enablePicking,
    disablePicking,
    clearSelection,
    clearHover,
    selectFeature,
    flyToFeature
  };
}
