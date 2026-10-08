/**
 * 地图实时信息(层级/比例尺/经纬度)直接显示在底部状态条上,不再在地图上
 * 单独浮一条蓝色信息条。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const dir = new URL('.', import.meta.url)
const src = readFileSync(new URL('./MapView.vue', import.meta.url), 'utf8')
const status = readFileSync(new URL('./AppStatusBar.vue', import.meta.url), 'utf8')
const store = readFileSync(new URL('../stores/mapInfo.js', import.meta.url), 'utf8')

function stripComments(text) {
  return text
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/^[ \t]*\/\/[^\n]*$/gm, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
}

test('★ 地图上不再单独显示实时信息条 ★', () => {
  const code = stripComments(src)
  assert.ok(!code.includes('map-info'),
    'MapView 里还有 .map-info —— 应该删掉,实时信息只留在底部状态条')
  assert.ok(code.includes('mapInfo.set'),
    'MapView 没有把实时信息写进 mapInfo store')
})

test('★ 底部状态条显示层级/比例尺/经纬度 ★', () => {
  const code = stripComments(status)
  assert.ok(code.includes('mapInfo.zoom'), '状态条没有显示层级')
  assert.ok(code.includes('mapInfo.scale'), '状态条没有显示比例尺')
  assert.ok(code.includes('mapInfo.lon'), '状态条没有显示经纬度')
})

test('mapInfo store 持有这四个字段', () => {
  assert.ok(store.includes('zoom'), 'store 缺 zoom')
  assert.ok(store.includes('scale'), 'store 缺 scale')
  assert.ok(store.includes('lon'), 'store 缺 lon')
  assert.ok(store.includes('lat'), 'store 缺 lat')
})
