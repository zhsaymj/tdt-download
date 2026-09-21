<script setup>
/**
 * 服务管理面板（右侧抽屉）。
 *
 * 分工：把本地目录"发布"成带稳定地址的数据服务，供预览页、主界面图层列表
 * 以及其他本机服务使用。与「数据与成果」面板的区别是：那边是**任务**的成果，
 * 这边是**目录**的服务——任务记录删了成果就打不开，而服务指向目录，长期有效。
 */
import { computed, onMounted, ref } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { useServiceStore, KIND_TAG_STYLE } from '../stores/service'
import { SERVICE_KINDS, serviceKindLabel } from '../utils/provider'

const emit = defineEmits(['close'])

const store = useServiceStore()
const filterKind = ref('all')
const keyword = ref('')
const showCandidates = ref(true)
const scanPath = ref('')
const scanning = ref(false)
const renaming = ref('')      // 正在改名的服务 id
const renameText = ref('')

const KIND_OPTIONS = [
  { value: 'all', label: '全部' },
  ...SERVICE_KINDS.map((k) => ({ value: k, label: serviceKindLabel(k) })),
]

function match(it) {
  if (filterKind.value !== 'all' && it.kind !== filterKind.value) return false
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return true
  return (it.name || '').toLowerCase().includes(kw)
    || (it.root || '').toLowerCase().includes(kw)
}

const rows = computed(() => store.items.filter(match))
const candidates = computed(() => store.candidates.filter((c) => {
  if (filterKind.value !== 'all' && c.kind !== filterKind.value) return false
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return true
  return (c.label || '').toLowerCase().includes(kw)
    || (c.root || '').toLowerCase().includes(kw)
}))

/** 长路径中段省略，完整路径放 title（对齐 LayerPanel 的既有做法） */
function shortPath(p) {
  const s = String(p || '')
  if (s.length <= 42) return s
  const parts = s.split(/[\\/]/).filter(Boolean)
  if (parts.length <= 3) return s
  return `…/${parts.slice(-3).join('/')}`
}

async function copyUrl(svc) {
  const url = store.accessUrl(svc)
  try {
    await navigator.clipboard.writeText(url)
    MessagePlugin.success('地址已复制')
  } catch (_) {
    // 非安全上下文（http 且非 localhost）下 clipboard 不可用，
    // 回落到把地址显示出来让用户手工复制
    MessagePlugin.warning(`复制失败，请手工复制：${url}`)
  }
}

async function toggle(svc) {
  try {
    await store.setEnabled(svc.id, !svc.enabled)
  } catch (e) {
    MessagePlugin.error(e?.message || '操作失败')
  }
}

async function doRemove(svc) {
  try {
    // 走 removeAndDetach：同时摘掉该服务在地图上的图层
    await store.removeAndDetach(svc.id)
    MessagePlugin.success('已移除（磁盘文件未删除）')
  } catch (e) {
    MessagePlugin.error(e?.message || '移除失败')
  }
}

/**
 * 批量清理前提醒外接盘场景：盘没挂上时服务会集体假性失效，
 * 直接清掉会丢失配置。
 */
function confirmCleanBroken() {
  const n = store.broken.length
  if (!n) return
  const ok = window.confirm(
    `将移除 ${n} 个失效服务。\n\n` +
    '注意：若这些服务的源目录在外接盘上，盘未挂载时会看起来"失效"。\n' +
    '移除只删除注册记录，不会删除磁盘文件。\n\n确认移除？')
  if (!ok) return
  store.removeAllBroken().then((cnt) => MessagePlugin.success(`已移除 ${cnt} 个`))
}

async function browseAndScan() {
  try {
    const r = await fetch('/api/local/pick', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: 'dir', multiple: false }),
    })
    const d = await r.json()
    const picked = (d.paths || [])[0]
    if (!picked) return
    scanPath.value = picked
    await doScan()
  } catch (e) {
    MessagePlugin.error(`打开目录选择失败：${e?.message || e}`)
  }
}

async function doScan() {
  const p = scanPath.value.trim()
  if (!p) { MessagePlugin.warning('请先选择或填写目录'); return }
  scanning.value = true
  try {
    const found = await store.scanDir(p)
    store.candidates = found
    showCandidates.value = true
    if (!found.length) {
      MessagePlugin.warning(
        '未在该目录下识别到可发布的成果。识别依据：tileset.json（模型）、' +
        'layer.json（地形）、tilemapresource.xml 或数字分层瓦片目录（影像）、' +
        '.geojson/.kml（矢量）')
    } else {
      MessagePlugin.success(`识别到 ${found.length} 个成果`)
    }
  } catch (e) {
    MessagePlugin.error(e?.message || '扫描失败')
  } finally {
    scanning.value = false
  }
}

async function register(cand) {
  try {
    await store.register(cand)
    store.candidates = store.candidates.filter((c) => c !== cand)
    MessagePlugin.success('已添加，默认关闭；需要时点「开启」')
  } catch (e) {
    MessagePlugin.error(e?.message || '登记失败')
  }
}

function startRename(svc) {
  renaming.value = svc.id
  renameText.value = svc.name
}

async function commitRename(svc) {
  const name = renameText.value.trim()
  renaming.value = ''
  if (!name || name === svc.name) return
  try {
    await store.rename(svc.id, name)
  } catch (e) {
    MessagePlugin.error(e?.message || '改名失败')
  }
}

onMounted(async () => {
  await store.fetchAll()
  await Promise.all([store.fetchHealth(), store.fetchCandidates()])
})
</script>

<template>
  <div class="svc-panel">
    <div class="hd">
      <span class="title">服务</span>
      <button class="x" @click="emit('close')">×</button>
    </div>

    <!-- 添加 -->
    <div class="add-row">
      <input v-model="scanPath" class="inp" placeholder="本地目录绝对路径" />
      <button class="btn" @click="browseAndScan">浏览…</button>
      <button class="btn primary" :disabled="scanning" @click="doScan">
        {{ scanning ? '扫描中…' : '扫描' }}
      </button>
    </div>

    <!-- 失效提醒 -->
    <div v-if="store.broken.length" class="warn">
      <span>发现 {{ store.broken.length }} 个失效服务（源目录已不存在）</span>
      <button class="link" @click="confirmCleanBroken">清理全部失效</button>
    </div>

    <!-- 筛选 -->
    <div class="filters">
      <select v-model="filterKind" class="sel">
        <option v-for="o in KIND_OPTIONS" :key="o.value" :value="o.value">
          {{ o.label }}
        </option>
      </select>
      <input v-model="keyword" class="inp" placeholder="关键字" />
    </div>

    <div v-if="store.error" class="err">{{ store.error }}</div>

    <!-- 已注册 -->
    <div class="list">
      <div v-for="it in rows" :key="it.id" class="card"
        :class="{ broken: store.isBroken(it.id), off: !it.enabled }">
        <div class="r1">
          <span class="dot" :class="{ on: it.enabled && !store.isBroken(it.id) }" />
          <span class="tag" :style="KIND_TAG_STYLE[it.kind]">
            {{ serviceKindLabel(it.kind) }}
          </span>
          <input v-if="renaming === it.id" v-model="renameText" class="rename"
            @keyup.enter="commitRename(it)" @blur="commitRename(it)" />
          <span v-else class="nm" title="点击改名" @click="startRename(it)">
            {{ it.name }}
          </span>
        </div>
        <div class="pth" :title="it.root">{{ shortPath(it.root) }}</div>
        <div v-if="store.isBroken(it.id)" class="bad">
          源目录已不存在<span v-if="store.health[it.id]?.reason">
            ：{{ store.health[it.id].reason }}</span>
        </div>
        <div v-else-if="it.bounds_approx" class="note">
          范围为估算值（瓦片对齐或扫描超限）
        </div>
        <div class="acts">
          <button class="mini" :disabled="!it.enabled || store.isBroken(it.id)"
            @click="copyUrl(it)">复制地址</button>
          <button class="mini" :disabled="store.isBroken(it.id)" @click="toggle(it)">
            {{ it.enabled ? '关闭' : '开启' }}
          </button>
          <!-- 失效时仍可移除：目录没了正是最需要移除的时候 -->
          <button class="mini del" @click="doRemove(it)">移除</button>
        </div>
      </div>
      <div v-if="!rows.length && !store.loading" class="empty">
        还没有服务。用上方「浏览…」选一个本地目录，或从下方候选里挑。
      </div>
    </div>

    <!-- 候选 -->
    <div class="cands">
      <button class="fold" @click="showCandidates = !showCandidates">
        {{ showCandidates ? '▾' : '▸' }} 未注册的成果（{{ candidates.length }}）
      </button>
      <template v-if="showCandidates">
        <div v-for="(c, i) in candidates"
          :key="`${c.kind}:${c.root}:${c.entry}:${i}`" class="card cand">
          <div class="r1">
            <span class="tag" :style="KIND_TAG_STYLE[c.kind]">
              {{ serviceKindLabel(c.kind) }}
            </span>
            <span class="nm" :title="c.label">{{ c.label }}</span>
          </div>
          <div class="pth" :title="c.root">{{ shortPath(c.root) }}</div>
          <div class="acts">
            <button class="mini primary" @click="register(c)">添加</button>
          </div>
        </div>
        <div v-if="!candidates.length" class="empty">没有未注册的成果</div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.svc-panel {
  position: fixed; top: 0; right: 0; bottom: 0; width: 380px; z-index: 60;
  display: flex; flex-direction: column; gap: 6px;
  background: #fff; border-left: 1px solid #dbe3ec;
  box-shadow: -4px 0 18px rgba(15, 23, 42, .12);
  padding: 10px 12px; overflow-y: auto; font-size: 13px;
}
.hd { display: flex; align-items: center; justify-content: space-between; }
.title { font-weight: 700; color: #0369a1; }
.x {
  border: 0; background: transparent; cursor: pointer;
  font-size: 18px; color: #64748b; line-height: 1;
}
.add-row { display: flex; gap: 5px; }
.inp {
  flex: 1 1 auto; min-width: 0; border: 1px solid #dbe3ec; border-radius: 5px;
  padding: 4px 7px; font-size: 12px; font-family: inherit;
}
.sel {
  flex: 0 0 auto; border: 1px solid #dbe3ec; border-radius: 5px;
  padding: 4px 6px; font-size: 12px; font-family: inherit; background: #fff;
}
.btn {
  flex: 0 0 auto; border: 1px solid #dbe3ec; background: #fff; cursor: pointer;
  border-radius: 5px; font-size: 12px; color: #475569; padding: 4px 9px;
}
.btn:hover:not(:disabled) { background: #f0f9ff; border-color: #7dd3fc; }
.btn.primary { background: #0284c7; border-color: #0284c7; color: #fff; }
.btn.primary:hover:not(:disabled) { background: #0369a1; }
.btn:disabled { opacity: .5; cursor: not-allowed; }
.warn {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  font-size: 11px; color: #b45309; background: #fffbeb;
  border: 1px solid #fde68a; border-radius: 6px; padding: 5px 8px;
}
.link { border: 0; background: transparent; cursor: pointer; color: #b45309;
  font-size: 11px; text-decoration: underline; }
.filters { display: flex; gap: 5px; }
.err { font-size: 11px; color: #b91c1c; }
.list, .cands { display: flex; flex-direction: column; gap: 6px; }
.cands { border-top: 1px solid #eef2f7; padding-top: 6px; }
.fold {
  border: 0; background: transparent; cursor: pointer; text-align: left;
  color: #0369a1; font-size: 12px; padding: 2px 0;
}
.card {
  border: 1px solid #eef2f7; border-radius: 6px; padding: 6px 8px; background: #fff;
}
.card.off { background: #fafafa; }
.card.broken { background: #fef2f2; border-color: #fecaca; opacity: .85; }
.card.cand { background: #f8fbff; border-color: #dbeafe; }
.r1 { display: flex; align-items: center; gap: 6px; min-width: 0; }
.dot {
  flex: 0 0 auto; width: 7px; height: 7px; border-radius: 50%; background: #cbd5e1;
}
.dot.on { background: #16a34a; }
.tag {
  flex: 0 0 auto; font-size: 10px; border-radius: 3px;
  padding: 0 5px; line-height: 16px;
}
.nm {
  flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; color: #334155; font-weight: 600; cursor: text;
}
.rename {
  flex: 1 1 auto; min-width: 0; border: 1px solid #7dd3fc; border-radius: 4px;
  padding: 1px 5px; font-size: 12px; font-family: inherit;
}
.pth {
  margin-top: 2px; font-size: 11px; color: #94a3b8;
  font-family: ui-monospace, Consolas, monospace;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.bad { margin-top: 3px; font-size: 11px; color: #b91c1c; line-height: 1.5; }
.note { margin-top: 3px; font-size: 11px; color: #b45309; }
.acts { display: flex; gap: 4px; margin-top: 5px; }
.mini {
  border: 1px solid #dbe3ec; background: #fff; border-radius: 4px;
  cursor: pointer; font-size: 11px; color: #475569; padding: 1px 7px;
}
.mini:hover:not(:disabled) { background: #f0f9ff; border-color: #7dd3fc; }
.mini:disabled { opacity: .45; cursor: not-allowed; }
.mini.del { color: #b91c1c; border-color: #fecaca; }
.mini.primary { background: #0284c7; border-color: #0284c7; color: #fff; }
.empty { font-size: 12px; color: #94a3b8; padding: 4px 0; line-height: 1.7; }
</style>
