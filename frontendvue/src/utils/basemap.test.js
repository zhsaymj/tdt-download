import test from 'node:test'
import assert from 'node:assert/strict'

import { BASEMAP_OPTIONS, basemapTypesFor, basemapZIndexForLevel } from './basemap.js'

test('图层管理提供三个不可移除的天地图底图选项', () => {
  assert.deepEqual(BASEMAP_OPTIONS.map((x) => x.value), [
    'tianditu_img', 'tianditu_vec', 'tianditu_ter',
  ])
  assert.equal(BASEMAP_OPTIONS.every((x) => x.removable === false && x.zoomable === false), true)
})

test('底图选项携带对应底图与注记 WMTS 图层', () => {
  assert.deepEqual(basemapTypesFor('tianditu_img'), ['img_w', 'cia_w'])
  assert.deepEqual(basemapTypesFor('tianditu_vec'), ['vec_w', 'cva_w'])
  assert.deepEqual(basemapTypesFor('tianditu_ter'), ['ter_w', 'cta_w'])
})

test('底图层级可在成果图层下方、中间和上方移动', () => {
  assert.equal(basemapZIndexForLevel(0), 0)
  assert.equal(basemapZIndexForLevel(1), 50)
  assert.equal(basemapZIndexForLevel(2), 89)
})
