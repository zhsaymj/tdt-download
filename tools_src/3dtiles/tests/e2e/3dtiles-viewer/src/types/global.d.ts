import * as Cesium from 'cesium';

declare global {
  interface Window {
    Cesium: typeof Cesium;
  }
}

export {};
