<script setup>
/**
 * 任务队列面板:取代原先常驻右侧 320px 的 TaskQueue。
 *
 * 用浮动面板而非模态抽屉——看进度时往往要同时对着地图核对成果范围,遮罩会挡住。
 * 内部复用现有 TaskQueue 组件:先把布局换过来,队列卡片与操作按钮沿用旧实现。
 */
import TaskQueue from './TaskQueue.vue'
import SidePanel from './SidePanel.vue'

defineProps({ visible: { type: Boolean, default: false } })
const emit = defineEmits(['update:visible'])
</script>

<template>
  <!-- flush:TaskQueue 的列表自己就是滚动容器,不要再套一层 -->
  <SidePanel :visible="visible" title="任务队列" side="right" width="440px" flush
    @update:visible="emit('update:visible', $event)">
    <TaskQueue class="in-panel" />
  </SidePanel>
</template>

<style scoped>
/**
 * TaskQueue 原本是固定 320px 的侧栏,放进面板要撑满并去掉自己的外壳。
 *
 * 注意选择器**不能**用 :deep():子组件根元素直接带上父组件的 scope 标记,
 * 而 :deep(.in-panel) 会编译成 `[data-v-父] .in-panel` 这样的**后代**选择器,
 * 匹配不到根元素本身 —— 之前 width:100% 就是这样失效的,导致 320px 的队列
 * 塞在 420px 面板里、右边空出一条。
 */
.in-panel {
  width: 100% !important;
  height: 100% !important;
  border-left: 0 !important;
  background: transparent !important;
  /* padding 保留:SidePanel 用了 flush 去掉自己的内边距,间距交给这里 */
}
/* 面板标题已显示「任务队列」,组件自带的标题重复了 */
.in-panel :deep(.qhead) { display: none; }
</style>
