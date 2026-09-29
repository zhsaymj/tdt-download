<template>
  <div class="inspector-panel" :style="{ top: position.y + 'px' }">
    <div class="panel-header draggable-header">
      <h3>检查器</h3>
      <button class="close-btn" @click="$emit('close')">×</button>
    </div>
    <div class="panel-content">
      <div class="section">
        <h4>相机信息</h4>
        <div class="info-grid">
          <div class="info-item">
            <span class="label">经度:</span>
            <span class="value">{{ cameraInfo?.position?.longitude?.toFixed(6) || '-' }}</span>
          </div>
          <div class="info-item">
            <span class="label">纬度:</span>
            <span class="value">{{ cameraInfo?.position?.latitude?.toFixed(6) || '-' }}</span>
          </div>
          <div class="info-item">
            <span class="label">高度:</span>
            <span class="value">{{ cameraInfo?.position?.height?.toFixed(2) || '-' }} m</span>
          </div>
          <div class="info-item">
            <span class="label">航向:</span>
            <span class="value">{{ cameraInfo?.heading?.toFixed(2) || '-' }}°</span>
          </div>
          <div class="info-item">
            <span class="label">俯仰:</span>
            <span class="value">{{ cameraInfo?.pitch?.toFixed(2) || '-' }}°</span>
          </div>
          <div class="info-item">
            <span class="label">视场角:</span>
            <span class="value">{{ cameraInfo?.fov?.toFixed(2) || '-' }}°</span>
          </div>
        </div>
      </div>

      <div class="section">
        <h4>Tileset 信息</h4>
        <div v-if="tilesetInfo" class="info-grid">
          <div class="info-item">
            <span class="label">版本:</span>
            <span class="value">{{ tilesetInfo.version || '-' }}</span>
          </div>
          <div class="info-item">
            <span class="label">Up轴:</span>
            <span class="value">{{ tilesetInfo.gltfUpAxis || '-' }}</span>
          </div>
          <div class="info-item">
            <span class="label">几何误差:</span>
            <span class="value">{{ tilesetInfo.geometricError?.toFixed(2) || '-' }}</span>
          </div>
        </div>
        <div v-else class="no-data">
          未加载 Tileset
        </div>
      </div>

      <div class="section" v-if="groundDistance">
        <h4>贴地距离</h4>
        <div class="info-grid">
          <div class="info-item highlight">
            <span class="label">距地面高度:</span>
            <span class="value">{{ groundDistance.distanceToGround?.toFixed(2) || '-' }} m</span>
          </div>
          <div class="info-item">
            <span class="label">中心高度:</span>
            <span class="value">{{ groundDistance.centerHeight?.toFixed(2) || '-' }} m</span>
          </div>
          <div class="info-item">
            <span class="label">地面高度:</span>
            <span class="value">{{ groundDistance.groundHeight?.toFixed(2) || '-' }} m</span>
          </div>
          <div class="info-item">
            <span class="label">最低点:</span>
            <span class="value">{{ groundDistance.minHeight?.toFixed(2) || '-' }} m</span>
          </div>
          <div class="info-item">
            <span class="label">最高点:</span>
            <span class="value">{{ groundDistance.maxHeight?.toFixed(2) || '-' }} m</span>
          </div>
        </div>
      </div>

      <div class="section">
        <h4>性能指标</h4>
        <div class="info-grid">
          <div class="info-item">
            <span class="label">FPS:</span>
            <span class="value" :class="{ 'good': (metrics?.fps ?? 0) >= 30, 'bad': (metrics?.fps ?? 0) < 30 }">
              {{ metrics?.fps || '-' }}
            </span>
          </div>
          <div class="info-item">
            <span class="label">Tilesets:</span>
            <span class="value">{{ metrics?.tiles || '-' }}</span>
          </div>
          <div class="info-item">
            <span class="label">内存:</span>
            <span class="value">{{ formatMemory(metrics?.memory) }}</span>
          </div>
        </div>
      </div>

      <div class="section">
        <h4>调试选项</h4>
        <div class="debug-options">
          <label class="checkbox-item">
            <input type="checkbox" v-model="debugOptions.showBoundingVolume" @change="updateDebugOptions" />
            <span>显示包围盒</span>
          </label>
          <label class="checkbox-item">
            <input type="checkbox" v-model="debugOptions.showContentBoundingVolume" @change="updateDebugOptions" />
            <span>显示内容包围盒</span>
          </label>
          <label class="checkbox-item">
            <input type="checkbox" v-model="debugOptions.wireframe" @change="updateDebugOptions" />
            <span>线框模式</span>
          </label>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, inject, computed } from 'vue';
import type { CameraInfo, TilesetInfo, PerformanceMetrics } from '../../types';
import type { GroundDistance } from '../../composables/useTileset';

const props = defineProps<{
  position: { x: number; y: number };
}>();

const emit = defineEmits<{
  'close': [];
  'update-debug': [options: any];
}>();

const cameraInfo = inject<CameraInfo>('cameraInfo');
const tilesetInfo = inject<TilesetInfo>('tilesetInfo');
const metrics = inject<PerformanceMetrics>('performanceMetrics');
const groundDistance = inject<GroundDistance>('groundDistance');

const debugOptions = ref({
  showBoundingVolume: false,
  showContentBoundingVolume: false,
  wireframe: false
});

function formatMemory(bytes?: number): string {
  if (!bytes) return '-';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(2) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
}

function updateDebugOptions() {
  emit('update-debug', { ...debugOptions.value });
}
</script>

<style scoped>
.inspector-panel {
  position: absolute;
  right: 20px;
  background: rgba(0, 0, 0, 0.85);
  border: 1px solid rgba(255, 255, 255, 0.2);
  border-radius: 8px;
  color: #fff;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  backdrop-filter: blur(10px);
  min-width: 280px;
  z-index: 1000;
}

.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
  cursor: move;
}

.panel-header h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}

.close-btn {
  background: transparent;
  border: none;
  color: #fff;
  font-size: 20px;
  cursor: pointer;
  padding: 0;
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  transition: background 0.2s;
}

.close-btn:hover {
  background: rgba(255, 255, 255, 0.1);
}

.panel-content {
  padding: 12px 16px;
  max-height: 400px;
  overflow-y: auto;
}

.section {
  margin-bottom: 16px;
}

.section:last-child {
  margin-bottom: 0;
}

.section h4 {
  margin: 0 0 10px 0;
  font-size: 12px;
  font-weight: 600;
  color: rgba(255, 255, 255, 0.7);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.info-grid {
  display: grid;
  gap: 6px;
}

.info-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
}

.info-item .label {
  color: rgba(255, 255, 255, 0.6);
}

.info-item .value {
  color: #fff;
  font-weight: 500;
  font-family: 'SF Mono', Monaco, 'Courier New', monospace;
}

.info-item .value.good {
  color: #4ade80;
}

.info-item .value.bad {
  color: #f87171;
}

.info-item.highlight {
  background: rgba(59, 130, 246, 0.15);
  padding: 8px;
  border-radius: 4px;
  margin: -2px -8px;
}

.info-item.highlight .label {
  color: #3b82f6;
}

.info-item.highlight .value {
  color: #60a5fa;
  font-size: 14px;
}

.no-data {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.4);
  font-style: italic;
}

.debug-options {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.checkbox-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  cursor: pointer;
}

.checkbox-item input[type="checkbox"] {
  width: 14px;
  height: 14px;
  cursor: pointer;
}
</style>
