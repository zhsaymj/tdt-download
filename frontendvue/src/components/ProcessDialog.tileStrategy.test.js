/**
 * 「瓦片断层策略」在勾了 TMS **或** OSM 时都要出现(需求38-2)。
 *
 * TMS 与 OSM 在后端共用同一套断层策略、同一个 task 字段
 * (runner._source_tms_plan_for_task 读 task.tms_source_strategy)。
 * 原先前端只判 'tms' —— 只勾 OSM 时用户看不到这个选项,也就改不了策略,
 * 于是 OSM 永远只从最高级降采样("只切最高级",正是需求38-2 要改的)。
 *
 * 本项目对 .vue 的测试方式就是读源码做断言(见 ProcessDialog.sync.test.js)。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const src = readFileSync(new URL('./ProcessDialog.vue', import.meta.url), 'utf8')

/** 剥掉注释(只剥整行注释与块注释,避免吃掉 `https://` 这类字符串)。 */
function stripComments(text) {
  return text
    .replace(/^[ \t]*\/\/[^\n]*$/gm, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
}

test('★ 断层策略在勾了 TMS 或 OSM 时都显示', () => {
  const code = stripComments(src)
  const i = code.indexOf('const showTmsSourceStrategy')
  assert.notEqual(i, -1, '未找到 showTmsSourceStrategy')
  const body = code.slice(i, code.indexOf('\n', code.indexOf('form.export.includes', i)) + 1)
  assert.ok(body.includes("'tms'"),
    '断层策略应仍在勾 TMS 时显示')
  assert.ok(body.includes("'osm'"),
    '断层策略也该在勾 OSM 时显示 —— 两者共用同一套策略,只判 tms 会让 OSM 用户改不了')
})

test('选项与提示文案不再写死 TMS', () => {
  const code = stripComments(src)
  const i = code.indexOf('showTmsSourceStrategy', code.indexOf('t-form-item'))
  assert.notEqual(i, -1, '模板里没有渲染断层策略')
  const body = code.slice(i, i + 400)
  assert.ok(!body.includes('TMS 断层策略'),
    '标签仍写死"TMS 断层策略" —— OSM 也用它,应改成通用措辞')
})
