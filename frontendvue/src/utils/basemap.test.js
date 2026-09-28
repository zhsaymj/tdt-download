import test from 'node:test'
import assert from 'node:assert/strict'

import {
  BASEMAP_OPTIONS, annotationLayerOf, basemapMaxZoom, basemapTileUrl,
  basemapTypesFor, basemapZIndexForLevel,
} from './basemap.js'

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

test('底图选项只携带底图图层,注记已摘出', () => {
  // 注记改由图层面板的独立开关控制(见 annotationLayerOf),
  // 不再与底图绑成一个图层组 —— 原断言期望 ['img_w','cia_w'] 这类组合。
  assert.deepEqual(basemapTypesFor('tianditu_img'), ['img_w'])
  assert.deepEqual(basemapTypesFor('tianditu_vec'), ['vec_w'])
  assert.deepEqual(basemapTypesFor('tianditu_ter'), ['ter_w'])
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


// ---------- 注记不再绑在底图上 ----------

test('天地图三项底图不再自带注记层', () => {
  // 注记改由图层面板的独立开关控制,不再随底图一起显示。
  const ANNO_LAYERS = ['cia_w', 'cva_w', 'cta_w']
  for (const v of ['tianditu_img', 'tianditu_vec', 'tianditu_ter']) {
    const opt = BASEMAP_OPTIONS.find((x) => x.value === v)
    for (const t of opt.types) {
      assert.ok(!ANNO_LAYERS.includes(t), `${v} 不应再含注记层 ${t}`)
    }
  }
})

test('天地图底图标签不再声称叠加注记', () => {
  // 去掉注记层后标签里的"(叠加路网注记)"会变成假话,必须同步改
  for (const v of ['tianditu_img', 'tianditu_vec', 'tianditu_ter']) {
    const opt = BASEMAP_OPTIONS.find((x) => x.value === v)
    assert.ok(!opt.label.includes('注记'), `${v} 标签仍写着注记:${opt.label}`)
  }
})

test('annotationLayerOf 按底图配对注记层', () => {
  assert.equal(annotationLayerOf('tianditu_img'), 'cia_w')
  assert.equal(annotationLayerOf('tianditu_vec'), 'cva_w')
  assert.equal(annotationLayerOf('tianditu_ter'), 'cta_w')
})

test('Google/Esri 底图配对影像注记', () => {
  for (const v of ['google_img', 'google_hybrid', 'google_road',
                   'google_terrain', 'esri_imagery']) {
    assert.equal(annotationLayerOf(v), 'cia_w', v)
  }
})

test('未知底图无配对注记', () => {
  assert.equal(annotationLayerOf('nope'), null)
  assert.equal(annotationLayerOf(undefined), null)
  assert.equal(annotationLayerOf(''), null)
})
