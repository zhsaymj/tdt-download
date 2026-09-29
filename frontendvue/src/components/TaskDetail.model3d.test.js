import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'
import test from 'node:test'

// 任务详情面板对三维数据任务(本地 OSGB/点云)的显示适配:
// 无级别/瓦片计数,bbox 是占位 [0,0,0,0],源路径与点云参数才是有效信息。
const componentDir = dirname(fileURLToPath(import.meta.url))
const detailSource = readFileSync(resolve(componentDir, 'TaskDetail.vue'), 'utf8')

test('详情面板:集中判定复用 provider.js,数据源名有中文', () => {
  assert.ok(detailSource.includes('isModel3dProvider'), '缺少 isModel3dProvider 引入')
  assert.ok(detailSource.includes('const isModel3d = (t) => isModel3dProvider(t?.provider)'),
    '缺少 isModel3d 判定函数')
  assert.ok(detailSource.includes("local_osgb: '倾斜模型(本地 OSGB)'"),
    'PROVIDER_TEXT 缺少 local_osgb')
  assert.ok(detailSource.includes("local_pointcloud: '点云(本地 LAS/LAZ)'"),
    'PROVIDER_TEXT 缺少 local_pointcloud')
})

test('详情面板:FORMAT_TEXT 覆盖三维导出格式,fmtExport 复用即有中文', () => {
  assert.ok(detailSource.includes("tile_3d: '3D Tiles'"), 'FORMAT_TEXT 缺少 tile_3d')
  assert.ok(detailSource.includes("dsm: 'DSM(点云)'"), 'FORMAT_TEXT 缺少 dsm')
  assert.ok(detailSource.includes("dem: 'DEM(点云)'"), 'FORMAT_TEXT 缺少 dem')
})

test('详情面板:model3d 分支存在且排在栅格 else 之前', () => {
  const branchIdx = detailSource.indexOf('v-else-if="isModel3d(t)"')
  assert.notEqual(branchIdx, -1, '缺少 model3d 模板分支')
  const rasterIdx = detailSource.indexOf('<!-- 栅格(影像/DEM) -->')
  assert.notEqual(rasterIdx, -1, '缺少栅格分支注释')
  assert.ok(branchIdx < rasterIdx, 'model3d 分支应排在栅格 else 之前,否则永远落进栅格')
  // 分支内展示源路径;点云额外展示坐标系与采样分辨率
  assert.ok(detailSource.includes('t.source_path'), 'model3d 分支应展示 source_path')
  assert.ok(detailSource.includes("t.provider === 'local_pointcloud'"), '缺少点云参数子分支')
  assert.ok(detailSource.includes('自动(读 LAS 头)'), '缺少 pc_crs 空串文案')
  assert.ok(detailSource.includes('本地坐标(不转 ECEF)'), '缺少 pc_crs local 文案')
})

test('详情面板:范围行对 model3d 显示占位文案,不显示全零 bbox', () => {
  assert.ok(detailSource.includes('转换完成后以预览为准'), '缺少范围占位文案')
  assert.ok(
    detailSource.includes("isModel3d(t) ? '转换完成后以预览为准' : fmtBbox(t.bbox)"),
    '范围行未按 model3d 分流')
})

test('详情面板:范围显示 checkbox 与缩放按钮对 model3d 隐藏,zoom 有全零守卫', () => {
  // checkbox 与「缩放到范围」按钮各一处 v-if 排除
  const occurrences = detailSource.split('v-if="!isModel3d(t)"').length - 1
  assert.ok(occurrences >= 2, '范围显示/缩放入口应各有 v-if="!isModel3d(t)"')
  // 防御性守卫:占位 bbox 直飞会落到几内亚湾
  assert.ok(detailSource.includes('t.value.bbox?.every((v) => v === 0)'), 'zoom 缺少全零 bbox 守卫')
  // 两个 watch 对 model3d 早退,不往地图上画零点
  assert.ok(detailSource.includes('if (!t.value || isModel3d(t.value)) return'), 'showRange watch 未排除 model3d')
  assert.ok(detailSource.includes('if (isModel3d(t.value)) return'), 'activeId watch 未排除 model3d')
})
