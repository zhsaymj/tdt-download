import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'
import test from 'node:test'

const componentDir = dirname(fileURLToPath(import.meta.url))
const frontendRoot = resolve(componentDir, '../..')
const componentSource = readFileSync(resolve(componentDir, 'ProcessDialog.vue'), 'utf8')
const addExportSource = readFileSync(resolve(componentDir, 'AddExportDialog.vue'), 'utf8')
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

test('阶段 key → 格式名映射集中到 utils/provider,提交与补导两处共用', () => {
  // 映射值本身在 provider.test.js 锁定;这里锁引用关系:
  // 早先 AddExportDialog 只抄了 dem→geotiff 一条本地映射,三维任务补导时对不上阶段,
  // 故 FMT_NAME_OF_STAGE 集中到 utils/provider,两处不得再各抄一份。
  assert.ok(componentSource.includes("import { fmtNameOf } from '../utils/provider'"),
    'ProcessDialog 应使用共享映射')
  assert.ok(addExportSource.includes("import { fmtNameOf, isModel3dProvider } from '../utils/provider'"),
    'AddExportDialog 应使用共享映射')
  assert.ok(!addExportSource.includes("stageKey === 'dem' ? 'geotiff'"),
    'AddExportDialog 不得保留本地残缺映射')
  assert.ok(!componentSource.includes('FMT_NAME_OF_STAGE'),
    'ProcessDialog 不得保留本地映射表')
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

test('点云 inspect 成功后清空旧 CRS 输入,避免残留值覆盖新文件自带坐标系', () => {
  // 场景:先选无 CRS 的点云 A 并填了 EPSG → 重选带 CRS 的点云 B → 必填块隐藏,
  // 若不清空,buildPayload 会把旧值静默带上。清空必须发生在 inspect 成功分支里。
  const start = componentSource.indexOf('api.localInspectPointCloud(')
  assert.notEqual(start, -1)
  const end = componentSource.indexOf('// 默认全选', start)
  assert.notEqual(end, -1)
  const branch = componentSource.slice(start, end)
  assert.ok(branch.includes("form.pcCrsEpsg = ''"), 'inspect 成功未清 pcCrsEpsg')
  assert.ok(branch.includes('form.pcCrsLocal = false'), 'inspect 成功未清 pcCrsLocal')
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
  // EPSG 代码须纯数字(与后端 _PC_CRS_RE 对齐,前端先拦一轮)
  assert.ok(componentSource.includes('/^\\d+$/'), '缺少 EPSG 纯数字校验')
  assert.match(componentSource, /EPSG 代码应为纯数字/)
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
  // 分辨率放高级设置,默认 0 = 自动;上限与后端 le=1000 对齐
  assert.ok(componentSource.includes('pcResolution'))
  assert.match(componentSource, /0\s*=\s*按点云密度自动估算|0\s*米?\s*=.*自动|自动估算/)
  assert.ok(componentSource.includes(':max="1000"'), '分辨率输入缺少 :max="1000"')
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
