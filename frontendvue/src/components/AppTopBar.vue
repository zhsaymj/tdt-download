<script setup>
/**
 * 顶栏:功能入口集中于此,让地图占满其余空间。
 *
 * 旧版把参数面板(400px)与任务队列(320px)常驻两侧,地图不到总宽一半。而本工具
 * 的参数大多是"设一次就不再动"的,任务队列大部分时间在显示已完成任务的历史进度
 * ——两者都不值得常驻。改为菜单 + 弹窗,地图全屏。
 */
import { computed } from 'vue'
import { useTaskStore } from '../stores/task'

const emit = defineEmits([
  'new-download', 'new-local', 'new-vector',
  'open-data', 'open-tasks', 'open-tokens', 'open-logs', 'open-about',
])

const taskStore = useTaskStore()

/** 进行中的任务数,做顶栏角标——不必打开队列就知道有没有在跑 */
const activeCount = computed(() => taskStore.tasks.filter(
  (t) => ['running', 'pending'].includes(t.status)).length)
</script>

<template>
  <header class="topbar">
    <div class="brand">
      <span class="logo">🌐</span>
      <span class="title">天地图工具</span>
    </div>

    <nav class="menus">
      <!-- 新建:三种数据来源走同一条主线,选完都进「处理」对话框 -->
      <t-dropdown :min-column-width="180" trigger="click">
        <button class="nav-btn primary">+ 新建</button>
        <t-dropdown-menu>
          <t-dropdown-item @click="emit('new-download')">
            在地图上画范围下载
          </t-dropdown-item>
          <t-dropdown-item @click="emit('new-local')">
            选本地栅格文件处理
          </t-dropdown-item>
          <t-dropdown-item @click="emit('new-vector')">
            选本地矢量文件转换
          </t-dropdown-item>
        </t-dropdown-menu>
      </t-dropdown>

      <button class="nav-btn" @click="emit('open-data')">数据</button>
      <button class="nav-btn" @click="emit('open-tasks')">
        任务<span v-if="activeCount" class="badge">{{ activeCount }}</span>
      </button>
    </nav>

    <nav class="actions">
      <button class="nav-btn" @click="emit('open-tokens')">密钥</button>
      <button class="nav-btn" @click="emit('open-logs')">日志</button>
      <button class="nav-btn" @click="emit('open-about')">关于</button>
    </nav>
  </header>
</template>

<style scoped>
.topbar {
  flex: 0 0 44px; height: 44px;
  display: flex; align-items: center; gap: 18px;
  padding: 0 14px;
  background: linear-gradient(90deg, #1e293b, #0f172a);
  border-bottom: 1px solid #0b1220;
  box-shadow: 0 1px 6px rgba(0, 0, 0, .25);
  user-select: none;
}
.brand { display: flex; align-items: center; gap: 8px; flex: 0 0 auto; }
.logo { font-size: 18px; line-height: 1; }
.title { font-size: 15px; font-weight: 700; letter-spacing: .5px; color: #f1f5f9; }
.menus { display: flex; align-items: center; gap: 2px; flex: 1 1 auto; }
.actions { display: flex; align-items: center; gap: 2px; flex: 0 0 auto; }
.nav-btn {
  border: 0; background: transparent; cursor: pointer;
  color: #cbd5e1; font-size: 13px; font-family: inherit;
  padding: 6px 12px; border-radius: 6px;
  transition: background .15s, color .15s;
  display: inline-flex; align-items: center; gap: 6px;
}
.nav-btn:hover { background: rgba(148, 163, 184, .18); color: #fff; }
.nav-btn:active { background: rgba(148, 163, 184, .28); }
.nav-btn.primary { color: #7dd3fc; font-weight: 600; }
.nav-btn.primary:hover { background: rgba(56, 189, 248, .18); color: #e0f2fe; }
.badge {
  min-width: 16px; height: 16px; padding: 0 4px;
  border-radius: 8px; background: #0284c7; color: #fff;
  font-size: 11px; line-height: 16px; text-align: center;
}
</style>
