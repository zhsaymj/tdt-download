import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'
import test from 'node:test'

const componentDir = dirname(fileURLToPath(import.meta.url))
const frontendRoot = resolve(componentDir, '../..')
const componentSource = readFileSync(resolve(componentDir, 'ProcessDialog.vue'), 'utf8')
const apiSource = readFileSync(resolve(frontendRoot, 'src/api.js'), 'utf8')
const taskDefaultsSource = readFileSync(resolve(frontendRoot, 'src/utils/taskDefaults.js'), 'utf8')
const topBarSource = readFileSync(resolve(componentDir, 'AppTopBar.vue'), 'utf8')
const appSource = readFileSync(resolve(frontendRoot, 'src/App.vue'), 'utf8')

test('支持 local_3d 来源,来源内用 radio 再选 OSGB / 点云数据类型', () => {
  assert.ok(componentSource.includes("kind.value === 'local_3d'"), '缺少 isLocal3D 判定')
  assert.ok(componentSource.includes('isLocal3D'))
  // 数据类型 radio:OSGB 目录 / 点云
  assert.ok(componentSource.includes("d3Type: 'osgb'"), 'form 缺少 d3Type 默认 osgb')
  assert.ok(componentSource.includes('d3TypeOptions'))
  assert.ok(componentSource.includes('v-model="form.d3Type"'), '模板缺少数据类型 radio')
  assert.ok(componentSource.includes("value: 'pointcloud'"))
  // 两个三维 provider 的推导
  assert.ok(componentSource.includes("'local_osgb'"))
  assert.ok(componentSource.includes("'local_pointcloud'"))
  // 标题映射
  assert.ok(componentSource.includes("local_3d: '处理三维数据'"))
})

test('阶段 key → 格式名映射覆盖三维阶段,且不动栅格 dem→geotiff', () => {
  assert.ok(componentSource.includes("dem: 'geotiff'"), '栅格 dem→geotiff 映射被改动')
  assert.ok(componentSource.includes("convert_3d: 'tile_3d'"))
  assert.ok(componentSource.includes("pc_dsm: 'dsm'"))
  assert.ok(componentSource.includes("pc_dem: 'dem'"))
  assert.ok(componentSource.includes("pc_tile_3d: 'tile_3d'"))
})

test('buildPayload 的 local_3d 分支带 source_path 与点云参数', () => {
  const start = componentSource.indexOf(
    'if (isLocal3D.value) {', componentSource.indexOf('function buildPayload'))
  assert.notEqual(start, -1, 'buildPayload 缺少 local_3d 分支')
  const end = componentSource.indexOf('if (isBuildings.value)', start)
  const branch = componentSource.slice(start, end)
  for (const expected of [
    'provider: activeProvider.value',
    'source_path: form.path',
    'bbox: []',
    'levels: []',
    'pc_crs',
    'pc_resolution: Number(form.pcResolution) || 0',
  ]) {
    assert.ok(branch.includes(expected), 'local_3d payload 缺少: ' + expected)
  }
  // OSGB 检查通过后固定导出 3D Tiles
  assert.ok(componentSource.includes("form.export = ['tile_3d']"))
})

test('browse 按数据类型选择目录或点云文件,多选文件按共同父目录处理', () => {
  assert.ok(componentSource.includes("kind: 'dir'"), '缺少目录选择')
  assert.ok(componentSource.includes("kind: 'pointcloud'"), '缺少点云文件选择')
  assert.ok(componentSource.includes('pick3dPath'), '缺少多选路径规整')
})

test('选中后自动检查:OSGB 走 inspect_osgb,点云走 inspect_pointcloud 并按 error 字段分支', () => {
  assert.ok(componentSource.includes('api.localInspectOsgb('))
  assert.ok(componentSource.includes('api.localInspectPointCloud('))
  // inspect_pointcloud 的错误经 200 响应的 error 字段返回(非 HTTP 异常)
  assert.ok(componentSource.includes('errText.value = d.error'))
  // 点云默认全选 dsm/dem/tile_3d(取 capabilities 里 default_on 的阶段)
  assert.match(componentSource, /stages\.value\.filter\(\(s\)\s*=>\s*s\.default_on\)/)
})

test('LAS 头无 CRS(srs === null)时必填 EPSG 或按本地坐标', () => {
  assert.ok(componentSource.includes('pcCrsMissing'))
  assert.ok(componentSource.includes('.srs === null'), '缺少 srs === null 判定')
  assert.ok(componentSource.includes('按本地坐标'))
  assert.ok(componentSource.includes('EPSG 代码'), '缺少 EPSG 输入提示')
  // 按本地坐标 → pc_crs 传 local;填代码 → 归一为 EPSG:数字
  assert.ok(componentSource.includes("'local'"))
  assert.ok(componentSource.includes('`EPSG:${'))
  // 提交前拦截
  assert.match(componentSource, /请填写 EPSG 代码或勾选/)
})

test('提交三维任务前调 tools/diagnose,所需工具不可用则阻止提交', () => {
  assert.ok(apiSource.includes("toolsDiagnose: () => req('/api/tools/diagnose')"))
  assert.ok(componentSource.includes('api.toolsDiagnose('))
  assert.ok(componentSource.includes('t.configured && t.exists && t.runnable'),
    '可用性判定须 configured && exists && runnable')
  for (const tool of ["'tiles3d'", "'pdal'", "'py3dtiles'"]) {
    assert.ok(componentSource.includes(tool), '缺少工具键: ' + tool)
  }
  // OSGB 只需 tiles3d;点云 DEM/DSM→pdal、3D Tiles→py3dtiles
  assert.match(componentSource, /d3Type === 'osgb'\s*\?\s*\['tiles3d'\]/)
  // diagnose 必须在建任务之前
  const diagIdx = componentSource.indexOf('await check3dTools()')
  const createIdx = componentSource.indexOf('taskStore.create(buildPayload())')
  assert.notEqual(diagIdx, -1, 'submit 缺少 check3dTools 调用')
  assert.ok(diagIdx < createIdx, 'diagnose 必须先于 taskStore.create')
})

test('api.js 封装了三维检查接口', () => {
  assert.ok(apiSource.includes("'/api/local/inspect_osgb'"))
  assert.ok(apiSource.includes("'/api/local/inspect_pointcloud'"))
})

test('taskDefaults 登记三维 provider 中文名', () => {
  assert.ok(taskDefaultsSource.includes("local_osgb: '本地 OSGB'"))
  assert.ok(taskDefaultsSource.includes("local_pointcloud: '本地点云'"))
})

test('顶栏新增「三维数据」入口,打开 local_3d 来源的处理对话框', () => {
  assert.ok(topBarSource.includes('三维数据'), '顶栏缺少三维数据菜单项')
  assert.ok(topBarSource.includes("emit('new-3d')") || topBarSource.includes("'new-3d'"))
  assert.ok(appSource.includes("@new-3d=\"openProcess({ kind: 'local_3d' })\""))
})

test('导出面板:OSGB 固定只读 3D Tiles;点云 DSM/DEM/3D Tiles 三选带中文标签', () => {
  // OSGB 只读勾选
  assert.match(componentSource, /<t-checkbox[^>]*disabled[^>]*>3D Tiles<\/t-checkbox>/)
  // 格式中文标签
  assert.ok(componentSource.includes("tile_3d: '3D Tiles'"))
  assert.ok(componentSource.includes("dsm: 'DSM(数字表面模型)'"))
  assert.ok(componentSource.includes("dem: 'DEM(数字高程模型,仅地面点)'"))
  // 分辨率放高级设置,默认 0 = 自动
  assert.ok(componentSource.includes('pcResolution'))
  assert.match(componentSource, /0\s*=\s*按点云密度自动估算|0\s*米?\s*=.*自动|自动估算/)
})

test('resetFormState 重置三维相关状态', () => {
  const start = componentSource.indexOf('function resetFormState')
  assert.notEqual(start, -1)
  const end = componentSource.indexOf('}', componentSource.indexOf('submitting.value = false', start))
  const body = componentSource.slice(start, end)
  for (const expected of [
    "form.d3Type = 'osgb'",
    'osgbInfo.value = null',
    'pcInfo.value = null',
    "form.pcCrsEpsg = ''",
    'form.pcCrsLocal = false',
    'form.pcResolution = 0',
  ]) {
    assert.ok(body.includes(expected), 'resetFormState 缺少: ' + expected)
  }
})
