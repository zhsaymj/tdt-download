import type { Ref } from 'vue';
import * as Cesium from 'cesium';

export interface Viewer3DConfig {
  container?: string | HTMLElement;
  terrain?: boolean;
  terrainUrl?: string;
  baseLayer?: boolean;
  baseLayerPicker?: boolean;
  geocoder?: boolean;
  homeButton?: boolean;
  sceneModePicker?: boolean;
  navigationHelpButton?: boolean;
  animation?: boolean;
  timeline?: boolean;
  fullscreenButton?: boolean;
  vrButton?: boolean;
  infoBox?: boolean;
  selectionIndicator?: boolean;
  shadows?: boolean;
  shouldAnimate?: boolean;
}

export interface Viewer3DState {
  viewer: Cesium.Viewer | null;
  isReady: boolean;
  config: Viewer3DConfig;
}

export interface TilesetConfig {
  url: string;
  maximumScreenSpaceError?: number;
  maximumMemoryUsage?: number;
  skipLevelOfDetail?: boolean;
  skipLevels?: number;
  immediatelyLoadDesiredLevelOfDetail?: boolean;
  loadSiblings?: boolean;
  cullWithChildrenBounds?: boolean;
  dynamicScreenSpaceError?: boolean;
  dynamicScreenSpaceErrorDensity?: number;
  dynamicScreenSpaceErrorFactor?: number;
}

export interface TilesetInfo {
  url: string;
  version?: string;
  gltfUpAxis?: string;
  geometricError?: number;
  boundingSphere?: Cesium.BoundingSphere;
}

export interface PerformanceMetrics {
  fps: number;
  drawCalls: number;
  triangles: number;
  vertices: number;
  memory: number;
  tiles: number;
  cacheSize: number;
}

export interface InspectorState {
  debugWireframe: boolean;
  debugShowBoundingVolume: boolean;
  debugShowContentBoundingVolume: boolean;
  debugShowViewerRequestVolume: boolean;
  debugColorizeTiles: boolean;
  debugShowMemoryUsage: boolean;
  debugShowGeometricError: boolean;
}

export interface CameraInfo {
  position: {
    longitude: number;
    latitude: number;
    height: number;
  };
  heading: number;
  pitch: number;
  roll: number;
  fov: number;
}

export interface FeatureInfo {
  id: string;
  type: string;
  properties: Record<string, any>;
  position?: Cesium.Cartesian3;
  primitive?: any;
}

export interface LoadProgress {
  phase: 'scanning' | 'processing' | 'building' | 'complete';
  current: number;
  total: number;
  message: string;
}

export interface LocalTilesetResult {
  url: string;
  blobUrl: string;
  file: File;
  fileMap: Map<string, string>;
}

export interface RecentItem {
  url: string;
  isLocal: boolean;
  timestamp: number;
}
