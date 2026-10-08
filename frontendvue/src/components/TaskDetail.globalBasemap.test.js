/**
 * 任务详情要显示新增的全球底图/边缘缓冲字段,并把面板加宽到 340px(需求40)。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const src = readFileSync(new URL('./TaskDetail.vue', import.meta.url), 'utf8')

function stripComments(text) {
  return text
    // HTML 注释也要剥:模板注释里出现过"全球底图"这类词,不剥会命中注释
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/^[ \t]*\/\/[^\n]*$/gm, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
}

test('★ 详情显示「全球底图」字段 ★', () => {
  const code = stripComments(src)
  assert.ok(code.includes('全球底图'),
    '任务详情没有显示 global_max_level —— 用户在详情里看不到这个设置')
  assert.ok(code.includes('t.global_max_level'),
    '没有引用 t.global_max_level')
})

test('★ 详情显示「边缘缓冲」字段 ★', () => {
  const code = stripComments(src)
  assert.ok(code.includes('边缘缓冲'),
    '任务详情没有显示 buffer_rings')
  assert.ok(code.includes('t.buffer_rings'),
    '没有引用 t.buffer_rings')
})

test('★ 面板宽度 340px ★', () => {
  const code = stripComments(src)
  const m = code.match(/\.detail\s*\{[^}]*width:\s*(\d+)px/)
  assert.notEqual(m, null, '找不到 .detail 的宽度定义')
  assert.equal(m[1], '340', `面板宽度应为 340px,实际 ${m[1]}px`)
})

test('DEM 源不显示这两个字段(后端对它不生效)', () => {
  const code = stripComments(src)
  // 两个新字段应排除 DEM —— 且用 isDemProvider 而非硬编码 esri_terrain
  // (硬编码会漏掉 local_dem)
  for (const mark of ['全球底图', '边缘缓冲']) {
    const i = code.indexOf(mark)
    assert.notEqual(i, -1)
    const before = code.slice(Math.max(0, i - 500), i)
    assert.ok(before.includes('isDemProvider'),
      `${mark} 字段没有用 isDemProvider 排除 DEM 源`)
  }
})
