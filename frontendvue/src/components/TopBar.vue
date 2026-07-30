<script setup>
// 深色专业顶栏:工具名 + 密钥管理/运行日志/关于 入口
const emit = defineEmits(['open-tokens', 'open-logs', 'open-about'])
</script>

<template>
  <header class="topbar">
    <div class="brand">
      <span class="logo">🌐</span>
      <span class="title">天地图下载工具</span>
    </div>
    <nav class="actions">
      <button class="nav-btn" @click="emit('open-tokens')">密钥管理</button>
      <button class="nav-btn" @click="emit('open-logs')">运行日志</button>
      <button class="nav-btn" @click="emit('open-about')">关于</button>
    </nav>
  </header>
</template>

<style scoped>
.topbar {
  flex: 0 0 48px; height: 48px;
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 16px;
  background: linear-gradient(90deg, #1e293b, #0f172a);
  border-bottom: 1px solid #0b1220;
  box-shadow: 0 1px 6px rgba(0, 0, 0, .25);
  user-select: none;
}
.brand { display: flex; align-items: center; gap: 10px; }
.logo { font-size: 20px; line-height: 1; }
.title {
  font-size: 16px; font-weight: 700; letter-spacing: .5px;
  color: #f1f5f9;
}
.actions { display: flex; align-items: center; gap: 4px; }
.nav-btn {
  border: 0; background: transparent; cursor: pointer;
  color: #cbd5e1; font-size: 13px; font-family: inherit;
  padding: 6px 12px; border-radius: 6px;
  transition: background .15s, color .15s;
}
.nav-btn:hover { background: rgba(148, 163, 184, .18); color: #fff; }
.nav-btn:active { background: rgba(148, 163, 184, .28); }
</style>
