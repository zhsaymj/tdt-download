import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const app = readFileSync(resolve(here, './App.vue'), 'utf8')

test('右侧三个面板的互斥集中在一处', () => {
  assert.ok(app.includes('function showRightPanel'),
    '缺少统一的右侧面板切换函数——互斥逻辑散在各入口会漏（服务面板加进来时就漏过一次）')
  for (const k of ['data', 'tasks', 'services']) {
    assert.ok(app.includes(`'${k}'`), `showRightPanel 未处理 ${k}`)
  }
})

test('三个打开函数都走统一入口', () => {
  for (const fn of ['openData', 'openTasks', 'openServices']) {
    assert.ok(app.includes(`const ${fn} = () => showRightPanel(`)
      || app.includes(`function ${fn}() { showRightPanel(`),
      `${fn} 未走 showRightPanel`)
  }
})

test('顶栏服务入口不再内联赋值', () => {
  // 内联写 serviceVisible = true 会绕过互斥，与另两个面板叠在一起
  assert.ok(!app.includes('@open-services="serviceVisible = true"'),
    '服务入口仍是内联赋值，会绕过互斥')
  assert.ok(app.includes('@open-services="openServices"'),
    '服务入口未接到 openServices')
})

test('面板避让右侧空间时把服务面板算进去', () => {
  assert.ok(app.includes('serviceVisible'),
    'padStyle 未考虑服务面板，地图悬浮元素会被盖住')
})
