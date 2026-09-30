/**
 * 高级选项:全球底图勾选 + 层级/缓冲圈可调,提交与估算都透传后端新参数。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const src = readFileSync(new URL('./ProcessDialog.vue', import.meta.url), 'utf8')

function stripComments(text) {
  return text
    .replace(/^[ \t]*\/\/[^\n]*$/gm, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
}

test('默认关闭:globalBasemap 初始 false,提交 global_max_level=0', () => {
  const code = stripComments(src)
  const i = code.indexOf('globalBasemap:')
  assert.notEqual(i, -1, '没有 globalBasemap 初始值')
  const line = code.slice(i, code.indexOf('\n', i))
  assert.ok(line.includes('false'), `初始应为 false,实际:${line.trim()}`)
  assert.ok(code.includes('global_max_level: form.globalBasemap ? form.globalMaxLevel : 0'),
    '提交负载应在未勾选时传 0')
})

test('提交负载带 global_max_level 与 buffer_rings', () => {
  const code = stripComments(src)
  assert.ok(code.includes('global_max_level:'),
    '提交负载要带 global_max_level')
  assert.ok(code.includes('buffer_rings:'),
    '提交负载要带 buffer_rings')
})

test('估算请求带上新参数', () => {
  const code = stripComments(src)
  assert.ok(code.includes('global_max_level: form.globalBasemap ? form.globalMaxLevel : 0'),
    'loadEstimate 参数要带 global_max_level')
  assert.ok(code.includes('buffer_rings:'),
    'loadEstimate 参数要带 buffer_rings')
})

test('控件渲染在界面上', () => {
  const code = stripComments(src)
  assert.ok(code.includes('v-model="form.globalBasemap"'),
    '勾选框没有绑定 form.globalBasemap')
  assert.ok(code.includes('form.globalMaxLevel'),
    '层级下拉没有绑定 form.globalMaxLevel')
})

test('DEM 源不显示这个开关(后端对该链路不生效,显示即误导)', () => {
  const code = stripComments(src)
  const i = code.indexOf('v-model="form.globalBasemap"')
  assert.notEqual(i, -1)
  // 往前找这个 t-form-item 的 v-if
  const before = code.slice(Math.max(0, i - 300), i)
  assert.ok(before.includes('!isDem'),
    'DEM 任务会白下载全球低层级却导出不了 —— 开关必须对它隐藏')
})