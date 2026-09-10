import { existsSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'
import test from 'node:test'

const componentDir = dirname(fileURLToPath(import.meta.url))
const frontendRoot = resolve(componentDir, '../..')
const componentSource = readFileSync(resolve(componentDir, 'ProcessDialog.vue'), 'utf8')
const taskDefaultsSource = readFileSync(resolve(frontendRoot, 'src/utils/taskDefaults.js'), 'utf8')

test('处理面板切换来源时会完整重置本地表单状态', () => {
  assert.match(componentSource, /function\s+resetFormState\s*\(/)
  assert.match(componentSource, /function\s+initForCurrentSource\s*\(/)

  for (const expected of [
    'fileInfo.value = null',
    'vecInfo.value = null',
    "errText.value = ''",
    "lastAutoName.value = ''",
    'bldParams.value = null',
    'bldBbox.value = null',
    'submitting.value = false',
    "form.tmsSourceStrategy = 'contiguous'",
    'form.useRange = false',
    "form.vecContainer = 'gpkg'",
    "form.bldVecContainer = 'gpkg'",
  ]) {
    assert.ok(componentSource.includes(expected), '缺少重置项: ' + expected)
  }
})

test('处理面板打开后切换 source 也会重新初始化', () => {
  assert.match(componentSource, /watch\(\s*\(\)\s*=>\s*\[/)
  assert.ok(componentSource.includes('props.source?.kind'))
  assert.ok(componentSource.includes('props.source?.path'))
  assert.ok(componentSource.includes('props.source?.preferCog'))
})

test('非下载来源不会触发下载 provider 默认值覆盖', () => {
  const start = componentSource.indexOf('watch(() => form.provider')
  assert.notEqual(start, -1, '未找到 provider watcher')
  const snippet = componentSource.slice(start, start + 260)
  assert.ok(snippet.includes('if (!isDownload.value) return'), 'provider watcher 缺少本地来源保护')
})

test('浏览器页签图标使用地球 emoji', () => {
  const faviconPath = resolve(frontendRoot, 'public/favicon.svg')
  assert.ok(existsSync(faviconPath), '缺少 public/favicon.svg')
  assert.ok(readFileSync(faviconPath, 'utf8').includes('🌐'))

  for (const file of ['index.html', 'preview.html']) {
    const html = readFileSync(resolve(frontendRoot, file), 'utf8')
    assert.match(html, /<link\s+rel="icon"\s+type="image\/svg\+xml"\s+href="\/favicon\.svg"\s*\/>/)
  }
})

test('层级列表按数据类型展示分辨率/比例尺/尺寸/大小四列', () => {
  assert.ok(componentSource.includes('formatSampleSpacing'))
  assert.ok(componentSource.includes('formatPixelResolution'))
  assert.ok(componentSource.includes('formatScale72Dpi'))
  assert.ok(componentSource.includes('formatPixelSize'))
  assert.ok(componentSource.includes('precisionOf(z)'))
  assert.ok(componentSource.includes('extraOf(z)'))
  assert.ok(componentSource.includes('levelColumns'))
  assert.ok(componentSource.includes("'高程级别', '采样间距', '总尺寸', '总大小'"))
  assert.ok(componentSource.includes("'影像级别', '像素分辨率', '比例尺(72DPI)', '总大小'"))
  assert.ok(taskDefaultsSource.includes('levelResolutionMeters'))
})

test('影像 TMS 支持选择分段保留输入层级策略', () => {
  assert.ok(componentSource.includes("tmsSourceStrategy: 'contiguous'"))
  assert.ok(componentSource.includes('tmsSourceStrategyOptions'))
  assert.ok(componentSource.includes('showTmsSourceStrategy'))
  assert.ok(componentSource.includes("!isBuildings.value && !isDem.value && form.export.includes('tms')"))
  assert.ok(componentSource.includes("value: 'preserve_inputs'"))
  assert.ok((componentSource.match(/tms_source_strategy: form.tmsSourceStrategy/g) || []).length >= 2)
  assert.ok(componentSource.includes('TMS 断层策略'))
  assert.ok(componentSource.includes("form.export.includes('tms')"))
})
