<script setup>
/**
 * 量测面板(悬浮于地图左上):点坐标 / 线距离 / 面面积。
 *
 * 与绘制工具条分开放:绘制的产物是"下载范围"(会进任务参数),量测的产物是读数
 * (看完即弃)。混在一个工具条里,用户容易把量测图形当成选好的范围。
 *
 * 默认折叠:量测是偶发动作,常驻一块面板会挤占地图。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { mapController } from '../composables/mapController'
import { cgcs2000Zone } from '../utils/crs'

const m = computed(() => mapController.value?.measure || null)
const items = computed(() => m.value?.items?.value || [])
const mode = computed(() => m.value?.mode?.value || null)

// color 与 composables/measure.js 里的 COLOR 保持一致:
// 列表标签用同色,才能和图上的图形/标注对上
const MODES = [
  { key: 'point', icon: '📍', label: '点坐标', color: '#2563eb' },
  { key: 'line', icon: '📏', label: '距离', color: '#16a34a' },
  { key: 'area', icon: '⬛', label: '面积', color: '#ea580c' },
]

const TIP = {
  point: '在地图上点击取点。',
  line: '依次点击折点,双击结束;右键移除上一拐点。',
  area: '依次点击顶点,双击结束;右键移除上一拐点。',
}

/** 结果行的类型标签底色跟随该类量测的配色 */
function tagStyle(type) {
  const c = MODES.find((x) => x.key === type)?.color
  return c ? { color: '#fff', background: c } : null
}

const zoneHint = computed(() => {
  const b = items.value.find((it) => it.type === 'point' && it.plane)
  return b ? cgcs2000Zone(b.lon)?.epsg : null
})

const collapsed = ref(true)

// 折叠时退出量测:面板收起后没有任何提示,地图却还在等点击,用户会以为界面卡了
watch(collapsed, (v) => { if (v) m.value?.stop() })

// 缩放控件叠在本面板上方,得知道面板实时高度才不会被展开的结果列表盖住。
// 面板向上展开、高度随结果条数变化,所以用 ResizeObserver 把高度写到 :root
// (缩放控件由 OpenLayers 生成、在 MapView 里,和这里没有父子关系,只能走全局变量)。
const rootEl = ref(null)
let ro = null

function syncHeight() {
  document.documentElement.style.setProperty(
    '--measure-h', `${rootEl.value?.offsetHeight || 0}px`)
}

watch(rootEl, (el) => {
  ro?.disconnect()
  ro = null
  if (el) {
    ro = new ResizeObserver(syncHeight)
    ro.observe(el)
  }
  syncHeight()
}, { immediate: true })

onBeforeUnmount(() => {
  ro?.disconnect()
  document.documentElement.style.removeProperty('--measure-h')
})
</script>

<template>
  <div v-if="m" ref="rootEl" class="measure" :class="{ collapsed }">
    <button class="fold" :title="collapsed ? '展开量测' : '收起量测'"
      @click="collapsed = !collapsed">
      {{ collapsed ? '📐 量测' : '× 量测' }}
      <span v-if="items.length" class="cnt">{{ items.length }}</span>
    </button>

    <template v-if="!collapsed">
    <div class="grp">
      <button v-for="x in MODES" :key="x.key" class="tool"
        :class="{ on: mode === x.key }" :title="x.label"
        :style="mode === x.key ? { background: x.color, borderColor: x.color, color: '#fff' } : null"
        @click="m.start(x.key)">{{ x.icon }}</button>
      <button class="tool" title="清除全部量测结果" :disabled="!items.length"
        @click="m.clearAll()">🗑</button>
    </div>
    <div v-if="mode" class="tip">{{ TIP[mode] }}再点按钮可退出量测。</div>

    <div v-if="items.length" class="list">
      <div v-for="it in items" :key="it.id" class="row">
        <div class="r-hd">
          <span class="tag" :style="tagStyle(it.type)">
            {{ MODES.find((x) => x.key === it.type)?.label }}
          </span>
          <!-- 点结果的读数是两行坐标,放在下方独占行显示,标题行只留操作按钮 -->
          <span v-if="it.type !== 'point'" class="val">{{ it.text }}</span>
          <span v-else class="val" />
          <button class="mini" title="定位到该结果" @click="m.locate(it.id)">定位</button>
          <button class="mini del" title="删除该结果" @click="m.removeItem(it.id)">✕</button>
        </div>
        <div v-if="it.type === 'point'" class="r-sub">
          <div>{{ it.lonLatText }}</div>
          <div>{{ it.planeText }}</div>
          <div v-if="it.cmText">{{ it.cmText }}</div>
        </div>
      </div>
    </div>
    <div v-else-if="!mode" class="tip">选一种量测方式后在地图上绘制。结果不保存,刷新即清。</div>
    <div v-if="zoneHint" class="tip">平面坐标按点位经度自动选带(不含带号编码)。</div>
    </template>
  </div>
</template>

<style scoped>
.measure {
  /* 停在右下角最底:缩放控件叠在正上方(见 MapView.vue)。
     right 随右侧面板宽度避让(--pad-right 由 App.vue 下传),否则面板一开就被盖住。 */
  position: absolute; right: calc(10px + var(--pad-right, 0px)); bottom: 10px;
  z-index: 20; transition: right .22s ease; width: 268px;
  background: rgba(255, 255, 255, .96); border: 1px solid #dbe3ec;
  border-radius: 8px; padding: 8px; box-shadow: 0 2px 10px rgba(15, 23, 42, .12);
}
.measure.collapsed { width: auto; padding: 4px 8px; }
.fold {
  display: flex; align-items: center; gap: 5px;
  border: 0; background: transparent; cursor: pointer;
  color: #475569; font-size: 13px; padding: 2px 4px;
}
.cnt {
  color: #15803d; background: #dcfce7; border-radius: 8px;
  padding: 0 6px; font-size: 11px; line-height: 16px;
}
.grp { display: flex; gap: 4px; margin-top: 6px; }
.tool {
  flex: 1 1 auto; border: 1px solid #dbe3ec; background: #fff;
  border-radius: 6px; cursor: pointer; font-size: 14px;
  padding: 5px 0; color: #334155; transition: background .15s, border-color .15s;
}
.tool:hover:not(:disabled) { background: #f0fdf4; border-color: #86efac; }
.tool.on { background: #16a34a; border-color: #16a34a; color: #fff; }
.tool:disabled { opacity: .4; cursor: not-allowed; }
.tip { font-size: 11px; color: #94a3b8; line-height: 1.6; margin-top: 5px; }
.list { margin-top: 6px; max-height: 34vh; overflow-y: auto; }
.row {
  border: 1px solid #eef2f7; border-radius: 5px; padding: 4px 6px;
  margin-bottom: 5px; background: #fff;
}
.r-hd { display: flex; align-items: center; gap: 5px; min-width: 0; }
.tag {
  flex: 0 0 auto; font-size: 10px; color: #15803d;
  background: #dcfce7; border-radius: 3px; padding: 0 4px; line-height: 16px;
}
.val {
  flex: 1 1 auto; min-width: 0; font-size: 12px; color: #334155;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-family: ui-monospace, Consolas, monospace;
}
.r-sub {
  margin-top: 3px; font-size: 11px; color: #64748b; line-height: 1.7;
  font-family: ui-monospace, Consolas, monospace; word-break: break-all;
}
.mini {
  flex: 0 0 auto; border: 1px solid #dbe3ec; background: #fff;
  border-radius: 4px; cursor: pointer; font-size: 11px; color: #475569;
  padding: 1px 5px;
}
.mini:hover { background: #f0f9ff; border-color: #7dd3fc; }
.mini.del { color: #b91c1c; border-color: #fecaca; }
</style>
