/**
 * 底图 ↔ 数据源 双向同步的源码断言。
 *
 * 本项目对 .vue 的测试方式就是读源码做断言(见 ProcessDialog.reset.test.js),
 * 本次沿用。这几条都是**回归护栏**:本次改的正是"硬编码列表"与"写死初始值",
 * 断言要盯住它们不再回来。
 */
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'
import test from 'node:test'

const componentDir = dirname(fileURLToPath(import.meta.url))
const componentSource = readFileSync(
  resolve(componentDir, 'ProcessDialog.vue'), 'utf8')

test('从 utils/basemap 引入 isBasemapKey', () => {
  // 不精确匹配整行 import:import 成员一多就会折行,整行匹配必然失配
  // (本项目为此栽过两次)。改为看模块路径之前的 import 文本窗口。
  const idx = componentSource.indexOf("from '../utils/basemap'")
  assert.notEqual(idx, -1, 'ProcessDialog 未从 utils/basemap 引入任何东西')
  const window = componentSource.slice(Math.max(0, idx - 200), idx)
  assert.ok(window.includes('isBasemapKey'),
    'ProcessDialog 应从 utils/basemap 引入 isBasemapKey')
})

test('DOWNLOAD_KEYS 从 providerOptions 派生,不另抄一份', () => {
  assert.ok(
    componentSource.includes(
      'const DOWNLOAD_KEYS = new Set(providerOptions.map((o) => o.value))'),
    'DOWNLOAD_KEYS 应从 providerOptions 派生')
})

test('★ 方向一:数据源 → 底图 用 isBasemapKey 判定,不再硬编码天地图三项', () => {
  // 原实现写死 ['tianditu_img','tianditu_vec','tianditu_ter'],
  // 导致切到 Google/Esri 时底图不动。
  assert.ok(
    !componentSource.includes(
      "['tianditu_img', 'tianditu_vec', 'tianditu_ter'].includes(form.provider)"),
    '仍在硬编码天地图三项 —— Google/Esri 切不过去')
  assert.ok(
    componentSource.includes('isBasemapKey(form.provider)'),
    '方向一应改用 isBasemapKey(form.provider)')
})

test('★ 方向二:底图 → 数据源 的 watch 存在且带两道守卫', () => {
  const idx = componentSource.indexOf('watch(() => basemapStore.key')
  assert.notEqual(idx, -1, '未找到监听 basemapStore.key 的 watcher')
  const snippet = componentSource.slice(idx, idx + 400)
  assert.ok(snippet.includes('if (!isDownload.value) return'),
    '方向二缺少非下载来源保护')
  assert.ok(snippet.includes('DOWNLOAD_KEYS.has(k)'),
    '方向二缺少"该底图可下载"成员判定')
  assert.ok(snippet.includes('if (form.provider === k) return'),
    '方向二缺少同值守卫(会导致图层反复重建)')
})

test('★ resetFormState 的数据源初始值跟随底图,不再写死', () => {
  assert.ok(
    !componentSource.includes("form.provider = 'tianditu_img'"),
    "resetFormState 仍写死 form.provider = 'tianditu_img' —— 关闭重开会丢失同步")
  assert.ok(
    componentSource.includes('DOWNLOAD_KEYS.has(basemapStore.key)'),
    '初始值应取 DOWNLOAD_KEYS.has(basemapStore.key)')
})

test('数据源下拉旁有"跟随底图"的说明', () => {
  // 强耦合的缓解措施(设计 §6):让用户知道切底图会改下载源
  assert.ok(componentSource.includes('跟随图层底图'),
    '缺少"跟随图层底图"的界面提示')
})

test('既有护栏不回归:provider watcher 仍保护非下载来源', () => {
  // ProcessDialog.reset.test.js 用 260 字符窗口切 watch(() => form.provider
  // 找这句。本任务改了同一处,要确保窗口仍覆盖得到。
  const start = componentSource.indexOf('watch(() => form.provider')
  assert.notEqual(start, -1)
  const snippet = componentSource.slice(start, start + 260)
  assert.ok(snippet.includes('if (!isDownload.value) return'))
})
