<script setup>
// 表单标签旁的信息图标:hover 才显示说明。
// 用途:参数面板里的长段说明常驻显示会把面板撑得很高,收进图标后
// 需要时再看。仅放"解释性"文字;状态类提示(如覆盖率、警告)仍应常驻可见。
defineProps({
  content: { type: String, required: true },
  placement: { type: String, default: 'top-right' },
  // 提示框最大宽度,长文案可调大
  maxWidth: { type: String, default: '320px' },
})
</script>

<template>
  <t-tooltip :content="content" :placement="placement" theme="light"
    :overlay-style="{ maxWidth }" show-arrow>
    <span class="info-tip" role="img" aria-label="说明">i</span>
  </t-tooltip>
</template>

<style scoped>
.info-tip {
  display: inline-flex; align-items: center; justify-content: center;
  width: 14px; height: 14px; margin-left: 4px;
  border: 1px solid #94a3b8; border-radius: 50%;
  color: #64748b; background: #fff;
  font-size: 10px; font-style: italic; font-weight: 700;
  line-height: 1; cursor: help; user-select: none;
  vertical-align: middle; flex: 0 0 auto;
  transition: color .15s, border-color .15s;
}
.info-tip:hover { color: #0ea5e9; border-color: #0ea5e9; }
</style>
