/**
 * 路网注记层在 z18 以上不渲染。
 *
 * 背景:注记只到 z18(见 `utils/provider.js::ANNOTATION_MAX_Z`)。超过之后 OL 会
 * 继续用 z18 的注记瓦片并**拉伸显示** —— 糊掉的路名/路网压在清晰的影像上,
 * 观感就是"放大后底图没变清晰"(用户反馈:切到 Google/Esri 放大到 18 级以上时)。
 *
 * 修法是给注记图层加 **layer 级** `maxZoom`(与 source 级那个是两回事:
 * source 级限瓦片网格,layer 级限可见性)。本文件两条:
 *   1. 源码接线 —— useMap 确实把 layer 级 maxZoom 传了;
 *   2. 行为契约 —— OL 的 layer 级 maxZoom 确实按视图缩放隐藏图层。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import TileLayer from 'ol/layer/Tile.js'
import { inView } from 'ol/layer/Layer.js'
import XYZ from 'ol/source/XYZ.js'

import { ANNOTATION_MAX_Z } from '../utils/provider.js'

const src = readFileSync(new URL('./useMap.js', import.meta.url), 'utf8')

/** 剥掉注释(只剥整行注释与块注释,避免吃掉 `https://` 这类字符串)。 */
function stripComments(text) {
  return text
    .replace(/^[ \t]*\/\/[^\n]*$/gm, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
}

test('★ 注记图层带 layer 级 maxZoom(源侧 + 图层侧各一处)', () => {
  const code = stripComments(src)
  const i = code.indexOf('function tiandituAnnoLayer')
  assert.notEqual(i, -1, '未找到 tiandituAnnoLayer')
  const body = code.slice(i, code.indexOf('\n  }', i) + 4)

  const hits = body.match(/maxZoom:\s*ANNOTATION_MAX_Z/g) || []
  assert.equal(hits.length, 2,
    `tiandituAnnoLayer 应有 2 处 maxZoom: ANNOTATION_MAX_Z` +
    `(source 级限瓦片网格 + layer 级限可见性),实际 ${hits.length} 处。` +
    `只有 source 级那处时,OL 会在 z19+ 把 z18 注记拉伸显示。`)
})

test('OL 的 layer 级 maxZoom 按视图缩放隐藏图层(上面那条依赖此语义)', () => {
  // 实测依据:ol/layer/Layer.js::inView() 里 `zoom <= layerState.maxZoom`
  // (inclusive)。这条锁住该语义不被 OL 升级悄悄改掉 —— 改了的话注记层
  // 在 z19+ 会重新显示,而源码断言看不出来。
  const layer = new TileLayer({
    source: new XYZ({ url: '/t/{z}/{x}/{y}', maxZoom: ANNOTATION_MAX_Z }),
    maxZoom: ANNOTATION_MAX_Z,
  })
  const state = layer.getLayerState()
  const visibleAt = (zoom) => inView(state, {
    resolution: 156543.03392804097 / 2 ** zoom,
    zoom,
    center: [0, 0],
    projection: null,
  })

  assert.equal(visibleAt(ANNOTATION_MAX_Z), true,
    `z${ANNOTATION_MAX_Z} 是上限本身,应仍渲染(inclusive)`)
  assert.equal(visibleAt(ANNOTATION_MAX_Z + 1), false,
    `z${ANNOTATION_MAX_Z + 1} 应隐藏`)
  assert.equal(visibleAt(21), false, 'z21 应隐藏')
})
