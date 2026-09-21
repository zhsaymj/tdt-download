import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const providerSrc = readFileSync(resolve(here, '../utils/provider.js'), 'utf8')
const storeSrc = readFileSync(resolve(here, './service.js'), 'utf8')

test('服务分类常量集中在 provider.js', () => {
  assert.ok(providerSrc.includes('SERVICE_KINDS'),
    '分类常量应定义在 provider.js，避免各组件各抄一份')
  for (const k of ['model', 'imagery', 'vector', 'terrain']) {
    assert.ok(providerSrc.includes(k), `缺少分类 ${k}`)
  }
})

test('分类常量带中文标签', () => {
  assert.ok(providerSrc.includes('SERVICE_KIND_LABELS'),
    '缺少中文标签映射')
  for (const label of ['模型', '影像', '矢量', '地形']) {
    assert.ok(providerSrc.includes(label), `缺少标签 ${label}`)
  }
})

test('store 提供 accessUrl 拼接（主机名由前端补）', () => {
  assert.ok(storeSrc.includes('accessUrl'),
    '缺少 accessUrl：后端只返回路径，主机名要前端拼 location.origin')
  assert.ok(storeSrc.includes('location.origin'),
    'accessUrl 应基于 location.origin')
})

test('store 主动探活', () => {
  assert.ok(storeSrc.includes('/api/services/health'), '缺少探活调用')
})

test('移除服务时同步摘掉地图图层', () => {
  assert.ok(storeSrc.includes('removeAndDetach'),
    '缺少 removeAndDetach：只删注册记录会让图层变成无数据源的幽灵图层')
  assert.ok(storeSrc.includes('removeByService'),
    'removeAndDetach 应调用 overlayStore.removeByService')
})

test('瓦片地址原样保留占位符', () => {
  assert.ok(!storeSrc.includes('encodeURIComponent(path)'),
    '不能对 access_path 做整体编码，会转义 {z}{x}{y} 花括号')
})
