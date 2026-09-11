import test from 'node:test'
import assert from 'node:assert/strict'
import { registerProj4Defs, cgcs2000Zone, toCgcs2000 } from './crs.js'

registerProj4Defs()

test('按经度选出 CGCS2000 3 度带,国境外返回 null', () => {
  // 成都 104.07°E → 带号 35(中央经线 105°E)
  assert.deepEqual(cgcs2000Zone(104.07), { zone: 35, cm: 105, epsg: 'EPSG:4544' })
  assert.equal(cgcs2000Zone(0), null)
  assert.equal(cgcs2000Zone(160), null)
})

test('中央经线上的点 X 恰为 500000(不含带号编码)', () => {
  const r = toCgcs2000(105, 30)
  assert.equal(r.epsg, 'EPSG:4544')
  assert.ok(Math.abs(r.x - 500000) < 1e-6, `x=${r.x}`)
  // 赤道起算的子午线弧长,30°N 约 3319000 m
  assert.ok(r.y > 3.3e6 && r.y < 3.35e6, `y=${r.y}`)
})

test('中央经线以东 X 大于 500000,以西小于', () => {
  assert.ok(toCgcs2000(105.5, 30).x > 500000)
  assert.ok(toCgcs2000(104.5, 30).x < 500000)
})

test('3 度带范围外不做投影', () => {
  assert.equal(toCgcs2000(0, 0), null)
})
