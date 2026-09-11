import test from 'node:test'
import assert from 'node:assert/strict'
import { cullLabelBoxes, overlapRatio } from './labelCull.js'

/** 构造像素包围盒,与 measure.js relayout 内的口径一致(left/top + 宽高) */
function box(left, top, w = 100, h = 40) {
  return { left, top, right: left + w, bottom: top + h, area: w * h }
}

const VIEWPORT = [800, 600]

test('不相交时重叠比例为 0', () => {
  assert.equal(overlapRatio(box(0, 0), box(200, 0)), 0)
  assert.equal(overlapRatio(box(0, 0), box(0, 200)), 0)
})

test('重叠比例按候选自身面积算,不是 IoU', () => {
  const big = { left: 0, top: 0, right: 400, bottom: 200, area: 400 * 200 }
  const small = box(380, 180, 40, 40)   // 与 big 交 20x20 = 400
  assert.equal(overlapRatio(small, big), 400 / 1600)
  assert.equal(overlapRatio(big, small), 400 / 80000)
})

test('互不相交的标注全部保留', () => {
  const hidden = cullLabelBoxes([box(0, 0), box(200, 0), box(400, 0)], VIEWPORT)
  assert.deepEqual(hidden, [false, false, false])
})

test('重叠不超过阈值仍然保留', () => {
  // 横向错开 90px:重叠宽 10px = 10% < 20%
  assert.deepEqual(cullLabelBoxes([box(0, 0), box(90, 0)], VIEWPORT, 0.2), [false, false])
})

test('重叠超过阈值时隐藏优先级低的那个', () => {
  // 横向错开 70px:重叠 30% > 20%,后者让位
  assert.deepEqual(cullLabelBoxes([box(0, 0), box(70, 0)], VIEWPORT, 0.2), [false, true])
})

test('正好等于阈值不隐藏(严格大于才剔除)', () => {
  // 重叠宽 20px = 20%,不大于 0.2
  assert.deepEqual(cullLabelBoxes([box(0, 0), box(80, 0)], VIEWPORT, 0.2), [false, false])
})

test('完全重合时只保留优先级最高的一个', () => {
  const hidden = cullLabelBoxes([box(100, 100), box(100, 100), box(100, 100)], VIEWPORT)
  assert.deepEqual(hidden, [false, true, true])
})

test('大标注不会因为小标注压住一角就被剔除', () => {
  const big = { left: 0, top: 0, right: 400, bottom: 200, area: 400 * 200 }
  const small = box(380, 180, 40, 40)
  // big 优先:自身只被压 0.5% 保留;small 被压 25% > 20% 隐藏
  assert.deepEqual(cullLabelBoxes([big, small], VIEWPORT, 0.2), [false, true])
  // small 优先:big 仍只被压 0.5%,两个都留
  assert.deepEqual(cullLabelBoxes([small, big], VIEWPORT, 0.2), [false, false])
})

test('被隐藏的标注不再参与遮挡判定,不连带挤掉第三个', () => {
  // A 保留;B 与 A 重叠 30% 被隐藏;C 与 B 重叠但与 A 只差 10%,应保留
  assert.deepEqual(
    cullLabelBoxes([box(0, 0), box(70, 0), box(90, 0)], VIEWPORT, 0.2),
    [false, true, false],
  )
})

test('视口外的标注既不隐藏也不参与遮挡判定', () => {
  // 两个都在视口外,即使重合也不标隐藏(反正看不见)
  assert.deepEqual(cullLabelBoxes([box(-500, -500), box(-495, -495)], VIEWPORT), [false, false])
  // 视口外的排在前面,不该把视口内的挤掉
  assert.deepEqual(cullLabelBoxes([box(900, 0), box(0, 0)], VIEWPORT), [false, false])
})

test('部分入界的标注参与判定', () => {
  assert.deepEqual(cullLabelBoxes([box(-50, 0), box(20, 0)], VIEWPORT, 0.2), [false, true])
})

test('纵向重叠同样生效', () => {
  // 标注高 40,纵向错开 25px:重叠 15/40 = 37.5% > 20%
  assert.deepEqual(cullLabelBoxes([box(0, 0), box(0, 25)], VIEWPORT, 0.2), [false, true])
})

test('默认阈值为 20%', () => {
  assert.deepEqual(cullLabelBoxes([box(0, 0), box(70, 0)], VIEWPORT), [false, true])
  assert.deepEqual(cullLabelBoxes([box(0, 0), box(85, 0)], VIEWPORT), [false, false])
})

test('空列表不报错', () => {
  assert.deepEqual(cullLabelBoxes([], VIEWPORT), [])
})
