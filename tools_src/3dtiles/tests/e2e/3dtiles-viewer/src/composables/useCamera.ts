import { shallowRef, onUnmounted, type Ref } from 'vue';
import * as Cesium from 'cesium';
import type { CameraInfo } from '../types';

export function useCamera(viewer: Ref<Cesium.Viewer | null>) {
  const cameraInfo = shallowRef<CameraInfo>({
    position: { longitude: 0, latitude: 0, height: 0 },
    heading: 0,
    pitch: 0,
    roll: 0,
    fov: 60
  });

  let removeCallback: (() => void) | null = null;

  function updateCameraInfo() {
    if (!viewer.value) return;

    const camera = viewer.value.camera;
    const position = camera.positionCartographic;

    cameraInfo.value = {
      position: {
        longitude: Cesium.Math.toDegrees(position.longitude),
        latitude: Cesium.Math.toDegrees(position.latitude),
        height: position.height
      },
      heading: Cesium.Math.toDegrees(camera.heading),
      pitch: Cesium.Math.toDegrees(camera.pitch),
      roll: Cesium.Math.toDegrees(camera.roll),
      fov: Cesium.Math.toDegrees((camera.frustum as Cesium.PerspectiveFrustum).fov ?? 0)
    };
  }

  function startTracking() {
    if (!viewer.value || removeCallback) return;

    removeCallback = viewer.value.scene.postRender.addEventListener(updateCameraInfo);
    updateCameraInfo();
  }

  function stopTracking() {
    if (removeCallback) {
      removeCallback();
      removeCallback = null;
    }
  }

  function flyTo(
    longitude: number,
    latitude: number,
    height: number,
    options?: {
      heading?: number;
      pitch?: number;
      roll?: number;
      duration?: number;
    }
  ) {
    if (!viewer.value) return;

    viewer.value.camera.flyTo({
      destination: Cesium.Cartesian3.fromDegrees(longitude, latitude, height),
      orientation: {
        heading: Cesium.Math.toRadians(options?.heading ?? 0),
        pitch: Cesium.Math.toRadians(options?.pitch ?? -90),
        roll: Cesium.Math.toRadians(options?.roll ?? 0)
      },
      duration: options?.duration ?? 3
    });
  }

  function setView(
    longitude: number,
    latitude: number,
    height: number,
    options?: {
      heading?: number;
      pitch?: number;
      roll?: number;
    }
  ) {
    if (!viewer.value) return;

    viewer.value.camera.setView({
      destination: Cesium.Cartesian3.fromDegrees(longitude, latitude, height),
      orientation: {
        heading: Cesium.Math.toRadians(options?.heading ?? 0),
        pitch: Cesium.Math.toRadians(options?.pitch ?? -90),
        roll: Cesium.Math.toRadians(options?.roll ?? 0)
      }
    });
  }

  function lookAt(
    longitude: number,
    latitude: number,
    height: number,
    heading: number,
    pitch: number,
    range: number
  ) {
    if (!viewer.value) return;

    viewer.value.camera.lookAt(
      Cesium.Cartesian3.fromDegrees(longitude, latitude, height),
      new Cesium.HeadingPitchRange(
        Cesium.Math.toRadians(heading),
        Cesium.Math.toRadians(pitch),
        range
      )
    );
  }

  function resetView() {
    if (!viewer.value) return;
    viewer.value.camera.flyHome();
  }

  onUnmounted(() => {
    stopTracking();
  });

  return {
    cameraInfo,
    startTracking,
    stopTracking,
    updateCameraInfo,
    flyTo,
    setView,
    lookAt,
    resetView
  };
}
