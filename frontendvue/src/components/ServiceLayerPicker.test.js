import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const picker = readFileSync(resolve(here, './ServiceLayerPicker.vue'), 'utf8')
const layerPanel = readFileSync(resolve(here, './LayerPanel.vue'), 'utf8')

test('只列已开启的服务', () => {
  assert.ok(picker.includes('serviceStore.enabled'), '未按 enabled 过滤')
  assert.ok(picker.includes('isBroken'), '未排除失效服务')
})

test('支持按类型与关键字过滤', () => {
  assert.ok(picker.includes('SERVICE_KINDS'), '类型筛选未复用集中常量')
  assert.ok(picker.includes('keyword'), '缺少关键字过滤')
})

test('复用分类标签映射与配色', () => {
  assert.ok(picker.includes('serviceKindLabel'), '未复用标签映射')
  assert.ok(picker.includes('KIND_TAG_STYLE'), '未复用标签配色')
})

test('选择后只抛事件，由调用方决定怎么加（二维/三维不同）', () => {
  assert.ok(picker.includes("emit('pick'"), '缺少 pick 事件')
})

test('图层面板提供两个添加来源', () => {
  assert.ok(layerPanel.includes('从「数据」面板添加'), '缺少数据来源入口')
  assert.ok(layerPanel.includes('从「服务」列表添加'), '缺少服务来源入口')
  assert.ok(layerPanel.includes('ServiceLayerPicker'), '未引入选择器')
})

test('服务图层走统一的 add 入口（透明度/层级/定位才能自动继承）', () => {
  assert.ok(layerPanel.includes('overlayStore.add'), '未走统一入口')
  assert.ok(layerPanel.includes('serviceId'), '未用 serviceId 标识来源')
})
