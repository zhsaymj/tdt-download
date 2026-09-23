import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(resolve(here, './ServicePanel.vue'), 'utf8')
const topbar = readFileSync(resolve(here, './AppTopBar.vue'), 'utf8')

test('顶栏有服务入口', () => {
  assert.ok(topbar.includes('open-services'), '顶栏缺少服务菜单事件')
  assert.ok(topbar.includes('服务'), '顶栏缺少服务文案')
})

test('面板有移除按钮（失效时仍可用）', () => {
  assert.ok(src.includes('移除'), '缺少移除入口')
  assert.ok(src.includes('removeAndDetach'), '移除应同时摘掉地图图层')
})

test('面板提供清理全部失效服务', () => {
  assert.ok(src.includes('removeAllBroken'), '缺少批量清理失效服务')
})

test('面板有复制访问地址', () => {
  assert.ok(src.includes('clipboard'), '复制应走 navigator.clipboard')
  assert.ok(src.includes('accessUrl'), '复制内容应来自 accessUrl')
})

test('失效服务禁用开启与复制', () => {
  assert.ok(src.includes('isBroken'), '缺少失效判定')
})

test('有本地目录选择入口', () => {
  assert.ok(src.includes('/api/local/pick'), '复用了既有系统文件对话框')
})

test('分类标签复用集中常量', () => {
  assert.ok(src.includes('serviceKindLabel'), '未复用 provider.js 的标签映射')
  assert.ok(src.includes('KIND_TAG_STYLE'), '未复用 store 的标签配色')
})

test('容器复用 SidePanel，与「数据」「任务」面板一致', () => {
  // 三个右侧面板必须同款：SidePanel 是 absolute 相对地图容器定位，
  // 高度只占地图区。若换成 position:fixed 的自定义容器，面板会顶到浏览器
  // 顶部、盖住顶栏，与另两个面板视觉不一致。
  assert.ok(src.includes("from './SidePanel.vue'"), '未复用 SidePanel')
  assert.ok(src.includes('<SidePanel'), '模板未用 SidePanel 包裹')
  assert.ok(!/position:\s*fixed/.test(src),
    '不应再出现 position:fixed 的自定义面板容器')
  assert.ok(src.includes('440px'), '宽度应与 DataDialog/TaskDialog 一致（440px）')
})
