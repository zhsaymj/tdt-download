/**
 * 重下对话框的全球底图:参数要预填(否则重跑静默丢设置) + 与裁剪互斥。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const src = readFileSync(new URL('./RedownloadDialog.vue', import.meta.url), 'utf8')

function stripComments(text) {
  return text
    .replace(/^[ \t]*\/\/[^\n]*$/gm, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
}

test('★ 重跑时要预填原任务的全球底图设置 ★', () => {
  const code = stripComments(src)
  assert.ok(/form\.globalBasemap\s*=\s*\(t\.global_max_level/.test(code),
    '没有从原任务预填 globalBasemap —— 重跑会静默丢掉该设置')
  assert.ok(/form\.globalMaxLevel\s*=\s*t\.global_max_level/.test(code),
    '没有预填 globalMaxLevel')
  assert.ok(/form\.bufferRings\s*=\s*\(t\.buffer_rings/.test(code),
    '没有预填 bufferRings')
})

test('★ 与裁剪互斥:预填与勾选两处都要保证 ★', () => {
  const code = stripComments(src)
  // 勾选时自动取消裁剪
  const wi = code.indexOf('watch(() => form.globalBasemap')
  assert.notEqual(wi, -1, '找不到 globalBasemap 的 watch')
  const wbody = code.slice(wi, code.indexOf('})', wi) + 2)
  assert.ok(/form\.clip\s*=\s*false/.test(wbody),
    '勾上全球底图时应自动取消裁剪')
  // 裁剪框在全球底图开启时禁用
  const ci = code.indexOf('裁剪 GeoTIFF 到矢量/多边形边界')
  assert.notEqual(ci, -1)
  const head = code.slice(Math.max(0, ci - 200), ci)
  assert.ok(head.includes(':disabled="form.globalBasemap"'),
    '裁剪框没有在全球底图开启时禁用')
})

test('控件渲染在界面上', () => {
  const code = stripComments(src)
  assert.ok(code.includes('v-model="form.globalBasemap"'),
    '勾选框没有绑定')
  assert.ok(code.includes('form.globalMaxLevel'),
    '层级下拉没有绑定')
})