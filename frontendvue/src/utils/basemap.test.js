import test from 'node:test'
import assert from 'node:assert/strict'

import { BASEMAP_OPTIONS, basemapTileUrl, basemapMaxZoom, basemapTypesFor, basemapZIndexForLevel } from './basemap.js'

test('图层管理提供三个不可移除的天地图底图选项', () => {
  // 天地图三项仍在最前且属性不变。Google/Esri 是后加的可移除底图
  // (见下方「BASEMAP_OPTIONS 含 Google 与 Esri 影像」),故这里只断言
  // 天地图子集,不再对全表断言 —— 原断言「每一项都不可移除、不可缩放」
  // 在新增可移除底图后已不成立。
  const tdt = BASEMAP_OPTIONS.filter((x) => x.value.startsWith('tianditu_'))
  assert.deepEqual(tdt.map((x) => x.value), [
    'tianditu_img', 'tianditu_vec', 'tianditu_ter',
  ])
  assert.equal(tdt.every((x) => x.removable === false && x.zoomable === false), true)
  // 天地图仍是前三项,界面上顺序不变
  assert.deepEqual(BASEMAP_OPTIONS.slice(0, 3).map((x) => x.value), [
    'tianditu_img', 'tianditu_vec', 'tianditu_ter',
  ])
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


test('BASEMAP_OPTIONS 含 Google 与 Esri 影像', () => {
  const vals = BASEMAP_OPTIONS.map((x) => x.value)
  assert.ok(vals.includes('google_img'))
  assert.ok(vals.includes('esri_imagery'))
})

test('Google/Esri 底图标记为经后端转发', () => {
  for (const v of ['google_img', 'esri_imagery']) {
    const opt = BASEMAP_OPTIONS.find((x) => x.value === v)
    assert.equal(opt.viaBackend, true, `${v} 应标记 viaBackend`)
  }
})

test('天地图底图不经后端转发(前端直连,有自己的 token)', () => {
  const opt = BASEMAP_OPTIONS.find((x) => x.value === 'tianditu_img')
  assert.ok(!opt.viaBackend)
})

test('Google 底图可缩放到 21', () => {
  const opt = BASEMAP_OPTIONS.find((x) => x.value === 'google_img')
  assert.equal(opt.zoomable, true)
  assert.equal(opt.maxZoom, 21)
})

test('Esri 影像底图上限 19', () => {
  const opt = BASEMAP_OPTIONS.find((x) => x.value === 'esri_imagery')
  assert.equal(opt.maxZoom, 19)
})

test('basemapTileUrl 给出后端转发地址', () => {
  assert.equal(basemapTileUrl('google_img'), '/api/tiles/google_img/{z}/{x}/{y}')
  assert.equal(basemapTileUrl('esri_imagery'), '/api/tiles/esri_imagery/{z}/{x}/{y}')
})

test('basemapTileUrl 对天地图返回 null(不走转发)', () => {
  assert.equal(basemapTileUrl('tianditu_img'), null)
})

test('basemapMaxZoom 未声明时回落 18', () => {
  assert.equal(basemapMaxZoom('tianditu_img'), 18)
  assert.equal(basemapMaxZoom('google_img'), 21)
})
