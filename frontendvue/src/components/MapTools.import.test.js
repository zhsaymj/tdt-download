import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'
import test from 'node:test'

const componentDir = dirname(fileURLToPath(import.meta.url))
const mapTools = readFileSync(resolve(componentDir, 'MapTools.vue'), 'utf8')
const buildingParams = readFileSync(resolve(componentDir, 'BuildingParams.vue'), 'utf8')

// parseVectorFiles 返回 { geojson, prjText } 包装体。曾经这里直接把包装体交给
// loadGeojson,OpenLayers 读不到 type 就抛 "Unsupported GeoJSON type: undefined",
// 地图工具条的「导入矢量」从来没成功过。这组断言盯的就是这条接线。
test('地图工具条导入矢量经 resolveVectorImport 拆包装后再加载', () => {
  // 只取 onFiles 的函数体:同文件里 onAreaChange 也有个叫 geo 的局部变量(行政区
  // 边界,来自后端,本来就是合法 geojson),整文件匹配会误伤
  const body = mapTools.slice(
    mapTools.indexOf('async function onFiles'),
    mapTools.indexOf('/** SrsModal 确认后'),
  )
  assert.ok(body.includes('resolveVectorImport(await parseVectorFiles(files))'),
    'onFiles 没有走 resolveVectorImport,包装体可能又被直接交给地图')
  assert.ok(!/loadGeojson\(\s*(geo|res|result)\s*\)/.test(body),
    'loadGeojson 收到了未经拆包装的解析结果')
  assert.ok(body.includes('loadGeojson(picked.geojson)'), '应加载拆出来的 geojson')
})

test('两个导入入口共用同一套解析判断', () => {
  assert.ok(buildingParams.includes('resolveVectorImport(await parseVectorFiles(files))'),
    '建筑矢量上传没有共用 resolveVectorImport')
})

test('地图工具条可选压缩包(与建筑上传入口一致)', () => {
  const accept = mapTools.match(/accept="([^"]+)"/)?.[1] || ''
  assert.ok(accept.split(',').includes('.zip'), `accept 缺少 .zip:${accept}`)
})

test('loadGeojson 对缺少 type 的入参给出可读报错', () => {
  const useMap = readFileSync(resolve(componentDir, '../composables/useMap.js'), 'utf8')
  assert.ok(useMap.includes("typeof geojson.type !== 'string'"),
    'loadGeojson 缺少 type 校验,包装体会再次抛 OpenLayers 的英文错')
})
