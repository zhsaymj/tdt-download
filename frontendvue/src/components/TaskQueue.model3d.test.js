import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'
import test from 'node:test'

const componentDir = dirname(fileURLToPath(import.meta.url))
const frontendRoot = resolve(componentDir, '../..')
const taskQueueSource = readFileSync(resolve(componentDir, 'TaskQueue.vue'), 'utf8')
const dataDialogSource = readFileSync(resolve(componentDir, 'DataDialog.vue'), 'utf8')
const previewSource = readFileSync(resolve(frontendRoot, 'src/preview/PreviewApp.vue'), 'utf8')

test('任务卡片:三维数据任务显示第四种类型标签与青色配色', () => {
  // 集中判定复用 provider.js,不在组件里再做字符串匹配
  assert.ok(taskQueueSource.includes('isModel3dProvider'), '缺少 isModel3dProvider 引入')
  assert.ok(taskQueueSource.includes("'三维数据'"), '缺少三维数据类型文案')
  assert.ok(taskQueueSource.includes("'kind-m3d'"), 'kindClass 缺少 kind-m3d 分支')
  assert.ok(taskQueueSource.includes("'m3d'"), 'kindTagClass 缺少 m3d 分支')
  assert.ok(taskQueueSource.includes('.kind-tag.m3d'), '缺少 kind-tag.m3d 配色')
  assert.ok(taskQueueSource.includes('.task.kind-m3d'), '缺少 kind-m3d 卡片色条')
  // 与既有三色明显区分的青色
  assert.ok(taskQueueSource.includes('#0d9488'), '三维数据配色应为青色 #0d9488')
})

test('任务卡片:三维数据 meta 行显示数据类型与源路径末段', () => {
  // 三维任务无级别/瓦片计数(total 恒 0),沿用栅格行会显示「级别 — · 0/0」
  assert.ok(taskQueueSource.includes('OSGB 倾斜模型'), '缺少 OSGB 数据类型文案')
  // 源路径可能以 \ 或 / 分隔、可能带尾部分隔符,取最后非空段
  assert.ok(taskQueueSource.includes('source_path'), 'meta 行应展示 source_path 末段')
  assert.match(taskQueueSource, /split\(\/\[\\\\\/\]\/\)/, '源路径末段应同时按 \\ 与 / 切分')
  assert.ok(taskQueueSource.includes('.filter(Boolean)'), '应过滤空段以容忍尾部分隔符')
})

test('任务卡片:previewable 白名单覆盖三维切片阶段', () => {
  const start = taskQueueSource.indexOf('function previewable')
  assert.notEqual(start, -1)
  const body = taskQueueSource.slice(start, taskQueueSource.indexOf('function openPreview', start))
  for (const key of ["'tms'", "'osm'", "'terrain'", "'tile_3d'", "'convert_3d'", "'pc_tile_3d'"]) {
    assert.ok(body.includes(key), 'previewable 缺少阶段 key: ' + key)
  }
})

test('任务卡片:三维四阶段无增量续传语义,隐藏「续切」只留「删除并重试」', () => {
  assert.ok(taskQueueSource.includes('NO_RESUME_STAGES'), '缺少 NO_RESUME_STAGES 常量')
  for (const key of ["'convert_3d'", "'pc_dsm'", "'pc_dem'", "'pc_tile_3d'"]) {
    assert.ok(taskQueueSource.includes(key), 'NO_RESUME_STAGES 缺少: ' + key)
  }
  // 「续切」按钮追加排除条件;「删除并重试」不动(仍是 canRetryStage 单条件)
  assert.ok(taskQueueSource.includes('!NO_RESUME_STAGES.has(s.key)'), '续切按钮未排除三维阶段')
  const resumeIdx = taskQueueSource.indexOf('!NO_RESUME_STAGES.has(s.key)')
  // 注意匹配模板里的点击绑定:脚本段的函数定义同样含 onPurgeRetryStage(t, s) 字样
  const purgeIdx = taskQueueSource.indexOf('@click.stop="onPurgeRetryStage(t, s)"')
  assert.ok(resumeIdx !== -1 && purgeIdx !== -1 && resumeIdx < purgeIdx, '续切应排在删除并重试之前')
})

test('成果面板:类型筛选与标签文本覆盖 model3d,buildings 的「三维」文案不动', () => {
  assert.ok(dataDialogSource.includes("{ value: 'model3d', label: '三维数据' }"),
    'KIND_FILTER 缺少 model3d 项')
  assert.ok(dataDialogSource.includes("model3d: '三维数据'"), 'KIND_TEXT 缺少 model3d')
  assert.ok(dataDialogSource.includes("buildings: '三维'"), 'buildings 文案被改动')
  assert.ok(dataDialogSource.includes('.tag.model3d'), '缺少 .tag.model3d 配色')
})

test('成果面板:previewable 覆盖三维切片阶段,修复白点对 model3d 任务隐藏', () => {
  const start = dataDialogSource.indexOf('function previewable')
  assert.notEqual(start, -1)
  const body = dataDialogSource.slice(start, dataDialogSource.indexOf('function openPreview', start))
  for (const key of ["'convert_3d'", "'pc_tile_3d'"]) {
    assert.ok(body.includes(key), 'previewable 缺少阶段 key: ' + key)
  }
  // 修复针对旧版 RGB 裁剪影像:点云 DEM/DSM 是单波段浮点、OSGB 任务无 tif,按钮只会误导
  assert.ok(dataDialogSource.includes("!['buildings', 'model3d'].includes(taskKindOf(t))"),
    'canRepairNodata 未排除 model3d')
})

test('预览页:三维任务的瓦片集就绪判定覆盖 convert_3d/pc_tile_3d', () => {
  for (const key of ["stageDone('tile_3d')", "stageDone('convert_3d')", "stageDone('pc_tile_3d')"]) {
    assert.ok(previewSource.includes(key), 'ready 判定缺少: ' + key)
  }
})

test('预览页:三维任务从 layers 接口取 tileset url,标签按 provider 区分', () => {
  // 多文件点云的主 tileset.json 在 3dtiles/001_<文件名>/ 子目录,由后端 layers 接口给出
  assert.ok(previewSource.includes('/api/tasks/${task.id}/layers'), '缺少 layers 接口请求')
  assert.ok(previewSource.includes("L.kind === 'preview3d' && L.url"), '缺少 preview3d+url 选取')
  // 取不到时回落根路径(OSGB/单文件点云约定)
  assert.ok(previewSource.includes('/3dtiles/tileset.json'), '缺少 tileset.json 回落路径')
  assert.ok(previewSource.includes('倾斜模型 3D Tiles'), '缺少 OSGB 标签')
  assert.ok(previewSource.includes('点云 3D Tiles'), '缺少点云标签')
  assert.ok(previewSource.includes('三维建筑白模'), '建筑白模标签被改动')
})

test('预览页:全零占位 bbox 视为无效,三维任务改用 zoomTo 瓦片集定位', () => {
  // 三维任务的真实范围 runner 阶段才解析,库里是占位 [0,0,0,0],直飞会落到几内亚湾
  assert.ok(previewSource.includes('taskBbox.every((v) => v === 0)'), '缺少全零 bbox 守卫')
  assert.ok(previewSource.includes('viewer.zoomTo(tileset3d)'), '缺少 zoomTo 瓦片集定位')
})
