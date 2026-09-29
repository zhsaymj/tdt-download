<template>
  <div class="feature-info" v-if="feature">
    <div class="info-header">
      <h4>Feature 信息</h4>
      <button class="close-btn" @click="$emit('close')">×</button>
    </div>
    <div class="info-content">
      <div class="info-row">
        <span class="label">ID:</span>
        <span class="value">{{ feature.id || '-' }}</span>
      </div>
      <div class="info-row">
        <span class="label">类型:</span>
        <span class="value">{{ feature.type || '-' }}</span>
      </div>
      <div v-if="feature.properties && Object.keys(feature.properties).length > 0" class="properties-section">
        <h5>属性</h5>
        <div class="properties-list">
          <div v-for="(value, key) in feature.properties" :key="key" class="property-item">
            <span class="prop-key">{{ key }}:</span>
            <span class="prop-value">{{ formatValue(value) }}</span>
          </div>
        </div>
      </div>
      <div v-else class="no-properties">
        无属性信息
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { FeatureInfo } from '../../types';

defineProps<{
  feature: FeatureInfo | null;
}>();

defineEmits<{
  'close': [];
}>();

function formatValue(value: any): string {
  if (value === null || value === undefined) {
    return '-';
  }
  if (typeof value === 'number') {
    return value.toFixed(6);
  }
  if (typeof value === 'object') {
    return JSON.stringify(value);
  }
  return String(value);
}
</script>

<style scoped>
.feature-info {
  position: absolute;
  bottom: 20px;
  right: 20px;
  background: rgba(0, 0, 0, 0.85);
  border: 1px solid rgba(255, 255, 255, 0.2);
  border-radius: 8px;
  color: #fff;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  backdrop-filter: blur(10px);
  min-width: 280px;
  max-width: 400px;
  max-height: 300px;
  z-index: 1000;
}

.info-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 14px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}

.info-header h4 {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
}

.close-btn {
  background: transparent;
  border: none;
  color: #fff;
  font-size: 18px;
  cursor: pointer;
  padding: 0;
  width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  transition: background 0.2s;
}

.close-btn:hover {
  background: rgba(255, 255, 255, 0.1);
}

.info-content {
  padding: 10px 14px;
  overflow-y: auto;
  max-height: 220px;
}

.info-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 4px 0;
  font-size: 12px;
}

.info-row .label {
  color: rgba(255, 255, 255, 0.6);
}

.info-row .value {
  color: #fff;
  font-weight: 500;
}

.properties-section {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid rgba(255, 255, 255, 0.1);
}

.properties-section h5 {
  margin: 0 0 8px 0;
  font-size: 11px;
  font-weight: 600;
  color: rgba(255, 255, 255, 0.7);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.properties-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.property-item {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  font-size: 11px;
  padding: 3px 0;
}

.prop-key {
  color: rgba(255, 255, 255, 0.6);
  margin-right: 10px;
  flex-shrink: 0;
}

.prop-value {
  color: #fff;
  font-family: 'SF Mono', Monaco, 'Courier New', monospace;
  word-break: break-all;
  text-align: right;
}

.no-properties {
  font-size: 11px;
  color: rgba(255, 255, 255, 0.4);
  font-style: italic;
  margin-top: 8px;
}
</style>
