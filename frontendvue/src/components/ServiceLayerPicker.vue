<script setup>
/**
 * 服务图层选择器。预览页（Cesium）与主界面图层面板（OpenLayers）共用。
 *
 * **只列已开启且未失效的服务**：关掉的服务选中后会立刻收到 403，失效的
 * 服务目录已经没了——都不该出现在可选列表里。
 *
 * 选中后只抛 `pick` 事件，由调用方决定怎么加：二维走 overlayStore（OpenLayers），
 * 三维走 Cesium 图层/provider。两条路径的数据源都来自服务记录，但渲染方式
 * 完全不同，放在这里分派会让组件同时依赖两套地图库。
 */
import { computed, onMounted, ref } from 'vue'
import { useServiceStore, KIND_TAG_STYLE } from '../stores/service'
import { SERVICE_KINDS, serviceKindLabel } from '../utils/provider'

const emit = defineEmits(['close', 'pick'])

const serviceStore = useServiceStore()
const filterKind = ref('all')
const keyword = ref('')

const KIND_OPTIONS = [
  { value: 'all', label: '全部' },
  ...SERVICE_KINDS.map((k) => ({ value: k, label: serviceKindLabel(k) })),
]

/** 已开启、源未失效、且命中筛选的服务 */
const rows = computed(() => serviceStore.enabled
  .filter((it) => !serviceStore.isBroken(it.id))
  .filter((it) => {
    if (filterKind.value !== 'all' && it.kind !== filterKind.value) return false
    const kw = keyword.value.trim().toLowerCase()
    if (!kw) return true
    return (it.name || '').toLowerCase().includes(kw)
      || (it.root || '').toLowerCase().includes(kw)
  }))

function shortPath(p) {
  const s = String(p || '')
  if (s.length <= 40) return s
  const parts = s.split(/[\\/]/).filter(Boolean)
  if (parts.length <= 3) return s
  return `…/${parts.slice(-3).join('/')}`
}

onMounted(async () => {
  await serviceStore.fetchAll()
  await serviceStore.fetchHealth()
})
</script>

<template>
  <div class="picker">
    <div class="hd">
      <span class="title">添加服务图层</span>
      <button class="x" @click="emit('close')">×</button>
    </div>

    <div class="filters">
      <select v-model="filterKind" class="sel">
        <option v-for="o in KIND_OPTIONS" :key="o.value" :value="o.value">
          {{ o.label }}
        </option>
      </select>
      <input v-model="keyword" class="inp" placeholder="搜索名称或路径" />
    </div>

    <div class="list">
      <button v-for="it in rows" :key="it.id" class="row" @click="emit('pick', it)">
        <span class="tag" :style="KIND_TAG_STYLE[it.kind]">
          {{ serviceKindLabel(it.kind) }}
        </span>
        <span class="body">
          <span class="nm" :title="it.name">{{ it.name }}</span>
          <span class="pth" :title="it.root">{{ shortPath(it.root) }}</span>
        </span>
      </button>
      <div v-if="!rows.length" class="empty">
        还没有开启的服务，或都被筛掉了。去顶栏「服务」菜单添加并开启。
      </div>
    </div>
  </div>
</template>

<style scoped>
.picker {
  width: 340px; background: #fff; border: 1px solid #dbe3ec;
  border-radius: 8px; box-shadow: 0 6px 20px rgba(15, 23, 42, .18);
  padding: 10px 12px; font-size: 13px;
}
.hd { display: flex; align-items: center; justify-content: space-between; }
.title { font-weight: 700; color: #0369a1; }
.x {
  border: 0; background: transparent; cursor: pointer; font-size: 18px;
  color: #64748b; line-height: 1;
}
.filters { display: flex; gap: 5px; margin: 6px 0; }
.sel {
  flex: 0 0 auto; border: 1px solid #dbe3ec; border-radius: 5px;
  padding: 3px 6px; font-size: 12px; font-family: inherit; background: #fff;
}
.inp {
  flex: 1 1 auto; min-width: 0; border: 1px solid #dbe3ec;
  border-radius: 5px; padding: 3px 7px; font-size: 12px; font-family: inherit;
}
.list {
  max-height: 46vh; overflow-y: auto; display: flex;
  flex-direction: column; gap: 3px;
}
.row {
  display: flex; align-items: center; gap: 7px; width: 100%; text-align: left;
  border: 1px solid #eef2f7; background: #fff; border-radius: 5px;
  padding: 4px 7px; cursor: pointer; font-family: inherit;
}
.row:hover { background: #f0f9ff; border-color: #7dd3fc; }
.tag {
  flex: 0 0 auto; font-size: 10px; border-radius: 3px;
  padding: 0 5px; line-height: 16px;
}
.body {
  flex: 1 1 auto; min-width: 0; display: flex;
  flex-direction: column; gap: 1px;
}
.nm {
  color: #334155; font-size: 12px; font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.pth {
  color: #94a3b8; font-size: 10px;
  font-family: ui-monospace, Consolas, monospace;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.empty { font-size: 12px; color: #94a3b8; padding: 6px 0; line-height: 1.7; }
</style>
