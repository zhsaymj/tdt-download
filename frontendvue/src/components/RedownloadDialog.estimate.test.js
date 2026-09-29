/**
 * 重新下载面板的估算:注记交给后端算,且勾选框变化后重算。
 *
 * 背景(两处既有问题,需求35-2 把注记改成默认勾选后更容易被看到):
 *
 *  1. `refreshEstimate` 的 estimate 请求**没带 annotate**,然后在
 *     `selectedSummary` 里 `tiles *= 2; bytes *= 2`。于是:
 *       - 每级明细列(levelSize 读 perLevel)是**未含注记**的值,
 *         总计却是 2 倍 —— 自相矛盾;
 *       - 级别含 z19+ 时 ×2 是错的(天地图注记只到 18 级)。
 *     ProcessDialog 早已改成"传 annotate 给后端、前端不乘"(见那边的注释),
 *     本文件是那个改动漏掉的地方。
 *
 *  2. `refreshEstimate` 只在「打开对话框 / 切数据源」时调用,没监听
 *     `form.annotate` —— 勾选框变化后数字不会更新。
 *
 * 本项目对 .vue 的测试方式就是读源码做断言(见 ProcessDialog.sync.test.js)。
 */
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'
import test from 'node:test'

const componentDir = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(resolve(componentDir, 'RedownloadDialog.vue'), 'utf8')

/** 剥掉注释再断言(只剥整行注释与块注释,避免吃掉 `https://` 这类字符串)。 */
function stripComments(text) {
  return text
    .replace(/^[ \t]*\/\/[^\n]*$/gm, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
}

const code = stripComments(src)

test('★ 估算把 annotate 传给后端', () => {
  const i = code.indexOf('async function refreshEstimate')
  assert.notEqual(i, -1, '未找到 refreshEstimate')
  const body = code.slice(i, code.indexOf('\n}', i) + 2)
  assert.ok(body.includes('annotate'),
    'refreshEstimate 的 estimate 请求应带 annotate —— ' +
    '否则明细列与总计对不上,且 z19+ 会虚高')
})

test('★ 不再在本地把总数 ×2', () => {
  const i = code.indexOf('const selectedSummary')
  assert.notEqual(i, -1, '未找到 selectedSummary')
  const body = code.slice(i, code.indexOf('\n})', i) + 3)
  assert.ok(!body.includes('*= 2'),
    'selectedSummary 里仍有整体 ×2 —— 注记只到 18 级,该由后端逐级算')
})

test('★ 勾选框变化时重新估算', () => {
  const i = code.indexOf('watch(() => form.annotate')
  assert.notEqual(i, -1,
    '没有监听 form.annotate 的 watch —— 取消勾选后数字不会变')
  const body = code.slice(i, i + 200)
  assert.ok(body.includes('refreshEstimate'),
    'watch(form.annotate) 应调用 refreshEstimate 重新估算')
})

test('注记可用性判定复用 provider.js,不另写名单', () => {
  // canAnnotate 是"这个源能不能叠注记"的唯一判定处
  const i = src.indexOf("from '../utils/provider'")
  assert.notEqual(i, -1, 'RedownloadDialog 未从 utils/provider 引入任何东西')
  const window = src.slice(Math.max(0, i - 200), i)
  assert.ok(window.includes('canAnnotate'),
    'RedownloadDialog 应从 utils/provider 引入 canAnnotate')
})
