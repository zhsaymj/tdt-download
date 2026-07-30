<script setup>
import { ref } from 'vue'
import { crsOptions } from '../utils/crs'

defineProps({ visible: Boolean })
const emit = defineEmits(['update:visible', 'confirm'])

const options = crsOptions()
const selected = ref('EPSG:4544')

function confirm() { emit('confirm', selected.value) }
function cancel() { emit('update:visible', false) }
</script>

<template>
  <t-dialog
    :visible="visible"
    header="选择矢量源坐标系"
    :on-close="cancel"
    :footer="false"
    width="420px"
  >
    <p class="hint">该矢量坐标不在经纬度范围内,可能是投影坐标。请选择其原始坐标系:</p>
    <t-select v-model="selected" :options="options" filterable style="width:100%" />
    <div class="actions">
      <t-button variant="outline" @click="cancel">取消</t-button>
      <t-button theme="primary" @click="confirm">确定并转换</t-button>
    </div>
  </t-dialog>
</template>

<style scoped>
.hint { font-size: 12px; color: #64748b; margin-bottom: 12px; }
.actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
</style>
