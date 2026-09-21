import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const store = readFileSync(resolve(here, './overlay.js'), 'utf8')
const dataDialog = readFileSync(resolve(here, '../components/DataDialog.vue'), 'utf8')
const taskQueue = readFileSync(resolve(here, '../components/TaskQueue.vue'), 'utf8')

test('key 拆成两个函数，不复用同一个换语义', () => {
  assert.ok(store.includes('taskOverlayKey'), '缺少 taskOverlayKey')
  assert.ok(store.includes('serviceOverlayKey'), '缺少 serviceOverlayKey')
})

test('两个 key 空间用前缀隔开，不会撞', () => {
  assert.ok(store.includes('`task:'), 'task key 应带 task: 前缀')
  assert.ok(store.includes('`svc:'), 'service key 应带 svc: 前缀')
})

test('服务图层支持按来源批量清理', () => {
  assert.ok(store.includes('removeByService'), '缺少 removeByService')
})

test('服务图层不做任务 bbox 兜底', () => {
  assert.ok(store.includes('!it.taskId'),
    '_fallbackBbox 应对服务图层直接返回 null，不去查不存在的任务')
})

test('DataDialog 三处引用已同步改名', () => {
  assert.ok(dataDialog.includes('taskOverlayKey'),
    'DataDialog 的 overlayKey 引用未同步改名')
  assert.ok(!/\boverlayKey\(/.test(dataDialog),
    'DataDialog 仍有旧的 overlayKey 调用')
})

test('B1：任务队列删除时清掉该任务的图层', () => {
  assert.ok(taskQueue.includes('removeByTask'),
    'TaskQueue.doDelete 未调用 overlayStore.removeByTask —— 删任务后图层会残留且无法移除')
})
