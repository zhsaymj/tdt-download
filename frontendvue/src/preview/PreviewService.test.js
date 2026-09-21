import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(resolve(here, './PreviewApp.vue'), 'utf8')

test('id 可选：无 id 不再直接报错', () => {
  assert.ok(!src.includes("errorMsg.value = '缺少任务 id 参数'"),
    '缺少 id 时仍会报错，空白预览进不去')
})

test('viewer 创建必须在任务加载之前', () => {
  const viewerAt = src.indexOf("new Viewer('cesium-container'")
  const fetchAt = src.indexOf('/api/tasks/${id}')
  assert.ok(viewerAt > 0 && fetchAt > 0, '未找到创建或加载位置')
  assert.ok(viewerAt < fetchAt,
    'viewer 必须先于任务加载创建，否则空白预览无 viewer、measure 崩在 null')
})

test('fetch 结果要判 ok（404 不抛错会静默渲染空预览）', () => {
  assert.ok(src.includes('res.ok'), '未判 res.ok：不存在的 id 会静默得到 /output/undefined')
})

test('有添加服务图层入口', () => {
  assert.ok(src.includes('ServiceLayerPicker'), '缺少服务图层选择器')
  assert.ok(src.includes('添加服务图层'), '缺少入口文案')
})

test('applyTerrain 改为 key 到 provider 的映射，不留硬编码 else 兜底', () => {
  assert.ok(src.includes('terrainProviders'),
    'applyTerrain 仍是硬编码三元，服务地形会静默回落平面椭球')
  assert.ok(!src.includes('else viewer.terrainProvider = flatTerrain'),
    '仍存在吞掉未知 key 的 else 分支')
})

test('三维瓦片集用 Map 管理，且移除时释放', () => {
  assert.ok(src.includes('tilesets') && src.includes('tilesets.set'),
    'tileset3d 仍是单变量，多个瓦片集时先加载的关不掉')
  assert.ok(src.includes('primitives.remove'),
    '没有 primitives.remove，移除服务时会泄漏 GPU 资源')
})

test('复用既有的服务图层选择器', () => {
  assert.ok(src.includes("from '../components/ServiceLayerPicker.vue'"),
    '应复用共享的选择器组件')
})

test('相机飞行参数化，服务图层飞自己的范围', () => {
  assert.ok(src.includes('function flyToBbox('),
    'flyToBbox 未参数化，服务图层会飞到任务范围或干脆不飞')
})

test('预览页入口装了 Pinia', () => {
  // 预览页是独立入口，本来自成一体没装 Pinia。加服务图层后要用
  // useServiceStore，漏装会在运行时抛 "reading '_s'"——构建和单元测试
  // 都发现不了，只有真开页面才报。
  const entry = readFileSync(resolve(here, '../preview.js'), 'utf8')
  assert.ok(entry.includes('createPinia'), 'preview.js 未引入 createPinia')
  assert.ok(entry.includes('app.use(createPinia())'),
    'preview.js 未注册 Pinia，用 store 的组件会运行时报错')
})
