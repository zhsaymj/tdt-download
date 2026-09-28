<script setup>
/**
 * 图层面板(悬浮于左下):管**当前地图上已加载的图层**,不列历史成果。
 *
 * 与「数据与成果」面板的分工按对象切分,而非按动作切分:
 *   数据与成果 → 从 75 个成果里挑出要看的图层(那里有搜索、类型筛选、大小)
 *   本面板     → 已叠上的这几个图层怎么看(可见性、透明度、上下遮盖、定位、移除)
 *
 * 早先本面板列全部成果的全部图层,两百多行、分组名全叫「影像下载」,而叠加对比真正
 * 需要的透明度与排序反倒没有位置放。现在这里通常只有 1~5 行。
 *
 * 列表顺序与图上顺序一致:最上面一行 = 图上最上层(store 的 items 是反的,渲染时
 * 反转一次),与 QGIS/ArcGIS 的图层树习惯相同。
 */
import { computed, ref, watch } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { useOverlayStore } from '../stores/overlay'
import { mapController } from '../composables/mapController'
import { useBasemapStore } from '../stores/basemap'
import { BASEMAP_OPTIONS } from '../utils/basemap'
import ServiceLayerPicker from './ServiceLayerPicker.vue'
import InfoTip from './InfoTip.vue'

const emit = defineEmits(['open-data'])
const overlayStore = useOverlayStore()
const basemapStore = useBasemapStore()
const showServicePicker = ref(false)

const collapsed = ref(true)
const basemapSelectOptions = BASEMAP_OPTIONS.map((x) => ({ value: x.value, label: x.label }))

watch(() => mapController.value, () => basemapStore.apply(), { immediate: true })

// 从「数据」面板勾上第一个图层时自动展开:否则图层叠上去了、控制入口还是折叠的,
// 用户得再点一次才知道能调透明度。
watch(() => overlayStore.count, (n, prev) => {
  if (n > prev && collapsed.value) collapsed.value = false
})

/** 从上到下渲染:图上最上层排在列表最前 */
const rows = computed(() => [...overlayStore.items].reverse())

/** 图层类型的中文说明 */
const KIND_TEXT = {
  tiles: '瓦片',
  cog: '栅格',
  vector: '矢量',
  vector_convert: '矢量',
  raster_only_bbox: '范围框',
}

/** 透明度滑块用整数百分比,避免浮点值在界面上显示成 0.7000000000000001 */
function pct(it) { return Math.round(it.opacity * 100) }
function onPct(key, v) { overlayStore.setOpacity(key, v / 100) }

/** 定位到图层。取不到范围时给提示,不能点了没反应 */
function locate(key) {
  if (!overlayStore.zoomTo(key)) MessagePlugin.warning('该图层没有可用的范围信息,无法定位')
}

function openData() {
  collapsed.value = true
  emit('open-data')
}

function openServices() { showServicePicker.value = true }

/**
 * 服务图层走与任务图层**完全相同的 add 入口**，因此透明度、上下移层、定位
 * 三项自动可用（都由 overlays.js 的通用实现提供，与来源无关）。
 *
 * 服务记录里的 overlay_desc 由后端生成，字段结构与 core/overlay.py 的输出
 * 逐字段对齐——这是"两条来源、一个渲染出口"的关键。
 */
function onPickService(svc) {
  const ok = overlayStore.add(
    { serviceId: svc.id, serviceName: svc.name }, svc.overlay_desc)
  if (!ok) {
    MessagePlugin.warning('该服务无法叠加显示（三维与地形只画范围框）')
  } else {
    MessagePlugin.success(`已添加：${svc.name}`)
  }
  showServicePicker.value = false
}
</script>

<template>
  <div class="layers" :class="{ collapsed }">
    <button class="fold" @click="collapsed = !collapsed">
      {{ collapsed ? '🗂 图层' : '× 图层' }}
      <span v-if="overlayStore.count" class="cnt">{{ overlayStore.count }}</span>
    </button>

    <div v-if="!collapsed" class="body">
      <div class="base-box">
        <div class="base-title">底图</div>
        <t-select :value="basemapStore.key" :options="basemapSelectOptions" size="small"
          @change="(v) => basemapStore.setKey(v)" />
        <div class="r2 base-tools">
          <t-slider :value="basemapStore.opacityPct" :min="0" :max="100" :step="5"
            class="sld" @change="(v) => basemapStore.setOpacityPct(v)" />
          <span class="pctv">{{ basemapStore.opacityPct }}%</span>
          <button class="mini" title="底图上移一层" :disabled="basemapStore.level === 2"
            @click="basemapStore.move(1)">⤒</button>
          <button class="mini" title="底图下移一层" :disabled="basemapStore.level === 0"
            @click="basemapStore.move(-1)">⤓</button>
        </div>
        <div class="r2 anno-row">
          <t-checkbox :checked="basemapStore.annotationVisible"
            @change="(v) => basemapStore.setAnnotationVisible(v)">
            路网注记
          </t-checkbox>
          <InfoTip
            content="叠加天地图路网注记(含地名、行政界等),始终位于底图的最上层。注记最高 18 级,更高层级不会有注记。"
            max-width="320px" />
        </div>
        <div class="base-note">底图不可移除和定位，可切换、调透明度并调整与成果图层的上下关系。</div>
      </div>

      <div v-for="(it, i) in rows" :key="it.key" class="item">
        <div class="r1">
          <label class="chk">
            <input type="checkbox" :checked="it.visible"
              @change="(e) => overlayStore.setVisible(it.key, e.target.checked)" />
            <span class="tag">{{ KIND_TEXT[it.desc.kind] || it.desc.kind }}</span>
            <!-- 图层名常见重复(75 个成果里一堆「影像下载」),所属任务名放 title -->
            <span class="lbl" :title="`${it.taskName} — ${it.desc.label}`">
              {{ it.desc.label }}
            </span>
          </label>
        </div>
        <div class="r2">
          <t-slider :value="pct(it)" :min="0" :max="100" :step="5"
            class="sld" @change="(v) => onPct(it.key, v)" />
          <span class="pctv">{{ pct(it) }}%</span>
          <!-- i 是渲染序(0 在最上);上移即往列表前挪,对应 store 的 moveUp -->
          <button class="mini" title="上移一层" :disabled="i === 0"
            @click="overlayStore.moveUp(it.key)">⤒</button>
          <button class="mini" title="下移一层" :disabled="i === rows.length - 1"
            @click="overlayStore.moveDown(it.key)">⤓</button>
          <button class="mini" title="缩放到该图层范围"
            @click="locate(it.key)">定位</button>
          <button class="mini del" title="从地图移除"
            @click="overlayStore.remove(it.key)">✕</button>
        </div>
      </div>

      <div v-if="!rows.length" class="empty">
        地图上还没有叠加图层。
        <button class="link" @click="openData">从「数据」面板添加</button>
        <button class="link" @click="openServices">或从「服务」列表添加</button>
      </div>
      <div v-else class="foot">
        <button class="link" @click="openData">＋ 从「数据」面板添加</button>
        <button class="link" @click="openServices">＋ 从「服务」列表添加</button>
        <button class="link danger" @click="overlayStore.clear()">全部移除</button>
      </div>

      <!-- 弹层朝上展开：面板本身停在地图左下角，往下弹会超出视口 -->
      <div v-if="showServicePicker" class="picker-wrap">
        <ServiceLayerPicker @close="showServicePicker = false"
          @pick="onPickService" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.layers {
  /* left 随左侧「处理」面板避让(--pad-left 由 App.vue 下传) */
  position: absolute; left: calc(10px + var(--pad-left, 0px)); bottom: 10px;
  z-index: 20; transition: left .22s ease;
  background: rgba(255, 255, 255, .97); border: 1px solid #dbe3ec;
  border-radius: 8px; box-shadow: 0 2px 10px rgba(15, 23, 42, .12);
  width: 340px; padding: 8px;
}
.layers.collapsed { width: auto; padding: 4px 8px; }
.fold {
  display: flex; align-items: center; gap: 5px;
  border: 0; background: transparent; cursor: pointer;
  color: #475569; font-size: 13px; padding: 2px 4px;
}
.cnt {
  color: #0369a1; background: #e0f2fe; border-radius: 8px;
  padding: 0 6px; font-size: 11px; line-height: 16px;
}
.body { margin-top: 6px; max-height: 46vh; overflow-y: auto; }
.base-box {
  border: 1px solid #dbeafe; border-radius: 6px; padding: 6px;
  margin-bottom: 6px; background: #f8fbff;
}
.base-title { font-size: 12px; color: #0369a1; font-weight: 700; margin-bottom: 4px; }
.base-tools { margin-top: 4px; }
.base-note { font-size: 11px; color: #94a3b8; line-height: 1.5; margin-top: 3px; }
.item {
  border: 1px solid #eef2f7; border-radius: 5px;
  padding: 4px 6px; margin-bottom: 5px; background: #fff;
}
.r1 { display: flex; align-items: center; min-width: 0; }
.r2 { display: flex; align-items: center; gap: 4px; margin-top: 2px; }
.chk {
  display: flex; align-items: center; gap: 5px; cursor: pointer;
  flex: 1 1 auto; min-width: 0; font-size: 12px;
}
.chk input { cursor: pointer; margin: 0; flex: 0 0 auto; }
.tag {
  flex: 0 0 auto; font-size: 10px; color: #0369a1;
  background: #e0f2fe; border-radius: 3px; padding: 0 4px; line-height: 16px;
}
.lbl {
  flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; color: #334155;
}
.sld { flex: 1 1 auto; min-width: 0; }
/* 滑块默认带上下外边距,在这一行里会把行高撑开 */
.sld :deep(.t-slider__container) { margin: 0; }
.pctv {
  flex: 0 0 auto; width: 34px; text-align: right;
  font-size: 11px; color: #64748b;
  font-family: ui-monospace, Consolas, monospace;
}
.mini {
  flex: 0 0 auto; border: 1px solid #dbe3ec; background: #fff;
  border-radius: 4px; cursor: pointer; font-size: 11px; color: #475569;
  padding: 1px 5px;
}
.mini:hover:not(:disabled) { background: #f0f9ff; border-color: #7dd3fc; }
.mini:disabled { color: #cbd5e1; cursor: default; }
.mini.del { color: #b91c1c; border-color: #fecaca; }
.empty { font-size: 12px; color: #94a3b8; padding: 2px 0; line-height: 1.8; }
.foot {
  display: flex; justify-content: space-between;
  border-top: 1px solid #eef2f7; padding-top: 4px; margin-top: 2px;
}
.link {
  border: 0; background: transparent; cursor: pointer;
  color: #0369a1; font-size: 12px; padding: 1px 2px;
}
.link:hover { text-decoration: underline; }
.link.danger { color: #b91c1c; }
/* 面板停在地图左下角，服务选择器朝上展开，否则会超出视口 */
.picker-wrap {
  position: absolute; bottom: calc(100% + 6px); left: 0; z-index: 30;
}
.anno-row { display: flex; align-items: center; gap: 6px; margin-top: 4px; }
</style>
