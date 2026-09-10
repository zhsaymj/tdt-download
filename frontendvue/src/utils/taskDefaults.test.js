import test from 'node:test'
import assert from 'node:assert/strict'

import {
  DEM_LEVELS,
  IMG_LEVELS,
  defaultTaskName,
  defaultContainersForStages,
  downloadDefaultsForProvider,
  ensureImageTmsLevels,
  formatPixelResolution,
  formatPixelSize,
  formatSampleSpacing,
  formatScale72Dpi,
  levelResolutionMeters,
  normalizeContainerMap,
} from './taskDefaults.js'

test('任务名称默认使用数据源和当前时间', () => {
  const now = new Date(2026, 7, 21, 13, 14, 15)
  assert.equal(defaultTaskName('tianditu_img', now), '天地图影像_20260821_131415')
})

test('GeoTIFF 与 DEM GeoTIFF 默认使用 COG 容器', () => {
  const stages = [
    { key: 'geotiff', containers: ['gtiff', 'cog'] },
    { key: 'tms', containers: ['tiles_dir', 'mbtiles'] },
    { key: 'dem', containers: ['gtiff', 'cog', 'png'] },
  ]
  assert.deepEqual(defaultContainersForStages(stages), {
    geotiff: 'cog',
    tms: 'tiles_dir',
    dem: 'cog',
  })
})

test('默认容器兼容后端返回的容器对象', () => {
  const stages = [
    { key: 'geotiff', containers: [{ key: 'gtiff', label: 'GeoTIFF' }, { key: 'cog', label: 'COG' }] },
    { key: 'tms', containers: [{ key: 'tiles_dir', label: '瓦片目录' }, { key: 'mbtiles', label: 'MBTiles' }] },
  ]
  assert.deepEqual(defaultContainersForStages(stages), {
    geotiff: 'cog',
    tms: 'tiles_dir',
  })
})

test('提交前容器映射会把残留对象归一化为 key', () => {
  assert.deepEqual(normalizeContainerMap({
    geotiff: { key: 'cog', label: 'COG' },
    tms: 'tiles_dir',
    empty: null,
  }), { geotiff: 'cog', tms: 'tiles_dir' })
})

test('影像下载默认启用 TMS、全级别、COG、路网注记和 WGS84', () => {
  const stages = [
    { key: 'geotiff', default_on: true, containers: ['gtiff', 'cog'] },
    { key: 'tms', default_on: true, containers: ['tiles_dir', 'mbtiles'] },
    { key: 'osm', default_on: false, containers: ['tiles_dir', 'mbtiles'] },
  ]
  const defaults = downloadDefaultsForProvider('tianditu_img', stages, new Date(2026, 7, 21, 8, 9, 10))
  assert.deepEqual(defaults.export, ['geotiff', 'tms'])
  assert.deepEqual(defaults.levels, IMG_LEVELS)
  assert.equal(defaults.containers.geotiff, 'cog')
  assert.equal(defaults.annotate, true)
  assert.equal(defaults.crs, 'EPSG:4326')
  assert.equal(defaults.name, '天地图影像_20260821_080910')
})

test('影像勾选 TMS 时默认补全 1 到最大级别', () => {
  assert.deepEqual(ensureImageTmsLevels('tianditu_vec', ['geotiff', 'tms'], []), IMG_LEVELS)
  assert.deepEqual(ensureImageTmsLevels('esri_terrain', ['geotiff', 'tms'], []), [])
  assert.deepEqual(ensureImageTmsLevels('tianditu_img', ['geotiff'], [10]), [10])
})

test('地形下载默认导出 GeoTIFF 和 Cesium 地形切片', () => {
  const stages = [
    { key: 'dem', default_on: true, containers: ['cog', 'gtiff'] },
    { key: 'terrain', default_on: false, containers: [] },
    { key: 'contour', default_on: false, containers: ['geojson'] },
  ]
  const defaults = downloadDefaultsForProvider('esri_terrain', stages)
  assert.deepEqual(defaults.export, ['geotiff', 'terrain'])
  assert.deepEqual(defaults.levels, [])
  assert.deepEqual(DEM_LEVELS.slice(0, 3), [0, 1, 2])
})

test('级别分辨率按层级和纬度换算为米', () => {
  assert.equal(Math.round(levelResolutionMeters(0)), 156543)
  assert.equal(Math.round(levelResolutionMeters(1)), 78272)
  assert.ok(levelResolutionMeters(13, 30) < levelResolutionMeters(13, 0))
  assert.equal(formatSampleSpacing(13, 30), '16.5 米')
  assert.equal(formatPixelResolution(13, 30), '16.5 米')
})

test('影像级别显示 72DPI 比例尺,地形级别显示成果像素尺寸', () => {
  // 16.5 米/像素 ÷ 0.000352778 米/像素(72DPI)≈ 1:46,700
  assert.match(formatScale72Dpi(13, 30), /^1:4[0-9],\d{3}$/)
  assert.equal(formatPixelSize(1072, 1008), '1072×1008')
  assert.equal(formatPixelSize(0, 256), '')
})
