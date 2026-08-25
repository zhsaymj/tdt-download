import { existsSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'
import test from 'node:test'

const componentDir = dirname(fileURLToPath(import.meta.url))
const frontendRoot = resolve(componentDir, '../..')
const componentSource = readFileSync(resolve(componentDir, 'ProcessDialog.vue'), 'utf8')

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
