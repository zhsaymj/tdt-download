<script setup>
/**
 * 浮在地图上的侧面板(滑入滑出),不是模态抽屉。
 *
 * 为什么不用 t-drawer:抽屉会加一层遮罩,把地图压暗并**阻断交互**。而这些面板恰恰
 * 需要用户边看地图边操作——「处理」面板里提示"请先画一个范围",用户就得在面板开着
 * 的时候去地图上画;图层面板勾选图层后要立刻看到地图变化。遮罩让这些都做不到。
 *
 * 只有日志面板仍用抽屉:它是"看完就关"的只读内容,不需要同时操作地图。
 */
import { computed } from 'vue'

const props = defineProps({
  visible: { type: Boolean, default: false },
  title: { type: String, default: '' },
  /** 停靠边 */
  side: { type: String, default: 'left' },
  width: { type: String, default: '400px' },
  /**
   * 内容自带滚动容器时置 true:去掉本组件的内边距与滚动。
   * 否则会出现两层滚动容器(如任务队列自己的列表已经是 overflow-y:auto),
   * 滚轮行为不可预期、还会多出一条滚动条。
   */
  flush: { type: Boolean, default: false },
})
const emit = defineEmits(['update:visible'])

const style = computed(() => ({
  width: props.width,
  [props.side]: 0,
}))
</script>

<template>
  <!-- 用 v-show 而非 v-if:面板内的表单状态(已填的参数、已检查的文件信息)不该因为
       关一次面板就丢失,用户往往是"关掉去画范围,再打开继续填" -->
  <transition :name="`slide-${side}`">
    <section v-show="visible" class="panel" :class="side" :style="style">
      <header class="head">
        <span class="title">{{ title }}</span>
        <button class="close" title="关闭" @click="emit('update:visible', false)">×</button>
      </header>
      <div class="body" :class="{ flush }">
        <slot />
      </div>
    </section>
  </transition>
</template>

<style scoped>
.panel {
  position: absolute; top: 0; bottom: 0; z-index: 30;
  display: flex; flex-direction: column;
  background: #fff;
  box-shadow: 0 0 18px rgba(15, 23, 42, .18);
}
.panel.left { border-right: 1px solid #dbe3ec; }
.panel.right { border-left: 1px solid #dbe3ec; }
.head {
  flex: 0 0 auto; height: 42px; padding: 0 8px 0 14px;
  display: flex; align-items: center; justify-content: space-between;
  border-bottom: 1px solid #eef2f7; background: #f8fbff;
}
.title { font-size: 14px; font-weight: 600; color: #0f172a; }
.close {
  border: 0; background: transparent; cursor: pointer;
  color: #94a3b8; font-size: 20px; line-height: 1;
  padding: 2px 8px; border-radius: 4px;
}
.close:hover { background: rgba(148, 163, 184, .2); color: #334155; }
.body { flex: 1 1 auto; overflow-y: auto; padding: 12px 14px; min-height: 0; }
/* 内容自管滚动:不再叠一层滚动容器与内边距 */
.body.flush { overflow: hidden; padding: 0; }

/* 滑入滑出:只动 transform,不触发重排 */
.slide-left-enter-active, .slide-left-leave-active,
.slide-right-enter-active, .slide-right-leave-active {
  transition: transform .22s ease, opacity .22s ease;
}
.slide-left-enter-from, .slide-left-leave-to { transform: translateX(-100%); opacity: .6; }
.slide-right-enter-from, .slide-right-leave-to { transform: translateX(100%); opacity: .6; }
</style>
