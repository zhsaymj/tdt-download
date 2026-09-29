/**
 * 勾了 TMS、而数据源是 Web 墨卡托时,界面要给出"会重投影、文字会软"的提示(需求37)。
 *
 * 根因:geodetic TMS(EPSG:4326)与 3857 源不同构,导出必须先重投影 ——
 * 实测高频能量只剩 68%,而前端显示 geodetic TMS 时 OL 还要再转一次 3857,
 * 端到端约 44%,细笔画的文字标注明显发虚。
 *
 * 后端已按网格把这类源的**默认**瓦片格式改成 OSM(见
 * backend/core/formats.py::default_on_stage_keys)。这条守的是另一半:
 * 用户手动勾回 TMS 时,至少能看见代价,而不是默默拿到一份发虚的成果。
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

test('★ 墨卡托源勾 TMS 时给出重投影提示', () => {
  const code = stripComments(src)
  const i = code.indexOf('const tmsReprojectWarn')
  assert.notEqual(i, -1,
    '没有 tmsReprojectWarn —— 用户勾 TMS 时看不到"文字会发虚"的代价')
  const body = code.slice(i, code.indexOf('\n})', i) + 3)
  assert.ok(body.includes("form.export.includes('tms')"),
    '提示应只在勾了 TMS 时出现')
  assert.ok(body.includes('MERCATOR_IMAGE_PROVIDERS'),
    '提示应按"源是墨卡托"判定,而不是写死数据源名单')
  assert.ok(body.includes('osm') || body.includes('OSM'),
    '提示应指出无损的替代格式是 OSM')
})

test('提示确实渲染在界面上(定义了却不用等于没有)', () => {
  const code = stripComments(src)
  assert.ok(code.includes('v-if="tmsReprojectWarn"'),
    'tmsReprojectWarn 没有渲染到模板 —— 用户看不到')
})

test('天地图源不提示(它与 geodetic 网格同构,本就无损)', () => {
  // 判定靠 MERCATOR_IMAGE_PROVIDERS,天地图不在其中 —— 这条锁住别改成
  // "所有源都提示",那会让天地图用户看到一个不存在的代价。
  const code = stripComments(src)
  const i = code.indexOf('const tmsReprojectWarn')
  const body = code.slice(i, code.indexOf('\n})', i) + 3)
  assert.ok(!body.includes('tianditu'),
    '提示里不该出现天地图判定 —— 它走 MERCATOR_IMAGE_PROVIDERS 即可')
})
