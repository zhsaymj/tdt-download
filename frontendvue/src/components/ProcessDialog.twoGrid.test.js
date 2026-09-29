/**
 * 双网格预估与提示(设计 D5 / Task 5)。
 *
 * 天地图同时勾 tms+osm 时,后端会下**两套原生瓦片**(各自无重投影),而两套的
 * 瓦片数不同 —— 不是简单翻倍。预估必须按两套算,否则用户看到实际下载量是预估的
 * 两倍会以为出 bug。前端要做两件事:
 *
 *   1. 把 export 传给预估接口(网格由后端按 provider+格式推导,判定只该有一处)
 *   2. 按返回的 grids 显示提示,说明"为什么比我以为的多"
 *
 * 本项目对 .vue 的测试方式就是读源码做断言(见 ProcessDialog.sync.test.js)。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const src = readFileSync(new URL('./ProcessDialog.vue', import.meta.url), 'utf8')
const apiSrc = readFileSync(new URL('../api.js', import.meta.url), 'utf8')

/** 剥掉注释(只剥整行注释与块注释,避免吃掉 `https://` 这类字符串)。 */
function stripComments(text) {
  return text
    .replace(/^[ \t]*\/\/[^\n]*$/gm, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
}

test('★ 预估要把 export 传给后端', () => {
  const code = stripComments(src)
  const i = code.indexOf('async function loadEstimate')
  assert.notEqual(i, -1, '未找到 loadEstimate')
  const body = code.slice(i, code.indexOf('\n}', i) + 2)
  assert.ok(body.includes('export:'),
    'loadEstimate 应把格式传给预估接口 —— 不传的话后端只按单网格算,'
    + '双网格任务的预估会少一半')
  assert.ok(body.includes('form.export.join'),
    '应传当前勾选的格式')
})

test('★ 预估接口把 export 拼进查询串', () => {
  const code = stripComments(apiSrc)
  const i = code.indexOf('estimate:')
  assert.notEqual(i, -1, '未找到 estimate 接口')
  const body = code.slice(i, i + 200)
  assert.ok(body.includes('export'),
    'estimate 接口要接受并透传 export 参数')
})

test('★ 双网格时给出提示(说明为什么下载量约 2×)', () => {
  const code = stripComments(src)
  const i = code.indexOf('const twoGridNote')
  assert.notEqual(i, -1, '没有 twoGridNote —— 用户不知道为什么下载量变多')
  const body = code.slice(i, code.indexOf('async function loadEstimate', i))
  assert.ok(body.includes('estGrids.value.length < 2'),
    '提示应只在后端报出两套网格时出现')
  assert.ok(code.includes('v-if="twoGridNote"'),
    'twoGridNote 没有渲染到模板 —— 用户看不到')
})

test('切换数据源时网格状态要清掉(否则会串到下一个源)', () => {
  const code = stripComments(src)
  const i = code.indexOf('function resetFormState')
  assert.notEqual(i, -1, '未找到 resetFormState')
  const body = code.slice(i, code.indexOf('\n}', i) + 2)
  assert.ok(body.includes('estGrids.value = []'),
    'resetFormState 要清 estGrids,不然切到单网格源后还显示双网格提示')
})

// ---------- 重下对话框:同一口径(最终审查 Important 2) ----------
// 原实现只在 ProcessDialog 传了 export,重下侧没传 → 显示的是实际的一半,
// 而它自己提交时又把 export 发出去、由后端重算 total —— 同一个对话框里自相矛盾。

const rdSrc = readFileSync(new URL('./RedownloadDialog.vue', import.meta.url), 'utf8')

test('★ 重下对话框的预估也要传 export', () => {
  const code = stripComments(rdSrc)
  const i = code.indexOf('api.estimate')
  assert.notEqual(i, -1, '未找到重下对话框的预估调用')
  const body = code.slice(i, code.indexOf('})', i) + 2)
  assert.ok(body.includes('export'),
    '重下预估不传 export → 天地图 tms+osm 时只显示一半,与提交后的任务数矛盾')
  assert.ok(body.includes('form.export.join'), '应传当前勾选的格式')
})

test('★ 重下对话框也给出双网格提示', () => {
  const code = stripComments(rdSrc)
  assert.ok(code.includes('const twoGridNote'),
    '重下侧没有双网格提示 —— 用户不知道为什么下载量变多')
  assert.ok(code.includes('v-if="twoGridNote"'), 'twoGridNote 没有渲染')
})
