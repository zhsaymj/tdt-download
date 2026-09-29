/**
 * 「叠加路网注记」勾选框变化后,估算必须重新拉取。
 *
 * 背景:注记增量由**后端**按 ≤z18 逐级算(loadEstimate 传 annotate,
 * 见 ProcessDialog 里那段注释)。但 loadEstimate 只在「打开对话框 / 切数据源 /
 * 改范围」时被调用,没有监听 form.annotate —— 于是取消勾选后数字仍停在上一次
 * 含注记的结果上(实测:默认勾上时打开、再取消,总数没变)。
 *
 * 需求35-2 把注记改成所有影像源默认勾选后,"默认勾上 → 用户取消"成了常态,
 * 这条路径会被频繁走到,所以补上。
 *
 * 本项目对 .vue 的测试方式就是读源码做断言(见 ProcessDialog.sync.test.js)。
 */
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'
import test from 'node:test'

const componentDir = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(resolve(componentDir, 'ProcessDialog.vue'), 'utf8')

/**
 * 剥掉注释再断言 —— 否则说明性文字里出现这些字样会误判(本项目栽过两次)。
 *
 * 只剥**整行注释**与块注释,不做全局 `//[^\n]*`:那样会把代码里
 * `'https://...'` 这类字符串的后半行一起吃掉。
 */
function stripComments(text) {
  return text
    .replace(/^[ \t]*\/\/[^\n]*$/gm, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
}

const code = stripComments(src)

test('★ 勾选框变化时重新估算(否则取消勾选后数字不会变)', () => {
  const i = code.indexOf('watch(() => form.annotate')
  assert.notEqual(i, -1,
    '没有监听 form.annotate 的 watch —— 取消勾选后估算不会重算')
  const body = code.slice(i, i + 200)
  assert.ok(body.includes('loadEstimate'),
    'watch(form.annotate) 应调用 loadEstimate 重新估算')
})

test('估算仍把 annotate 交给后端,前端不自己乘', () => {
  // 这条守的是既有的正确设计:前端若再乘一遍,对 z19+ 会虚高一倍
  // (天地图注记只到 18 级)。
  const i = code.indexOf('async function loadEstimate')
  assert.notEqual(i, -1, '未找到 loadEstimate')
  const body = code.slice(i, code.indexOf('\n}', i) + 2)
  assert.ok(body.includes('annotate'),
    'loadEstimate 应把 annotate 传给后端估算接口')
})

test('前端不再对注记做整体 ×2', () => {
  // 注记只影响 ≤18 的级别,"整体 ×2" 对 z19+ 是错的。
  assert.ok(!code.includes('tiles *= 2'),
    '出现整体 ×2 —— 注记只到 18 级,该由后端逐级算')
})
