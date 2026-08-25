import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

test('主地图不应创建 OpenStreetMap 兜底图层', () => {
  const source = readFileSync(new URL('./useMap.js', import.meta.url), 'utf8')
  assert.equal(source.includes('new OSM('), false)
  assert.equal(source.includes('tile.openstreetmap.org'), false)
  assert.equal(source.includes("from 'ol/source'"), false)
})
