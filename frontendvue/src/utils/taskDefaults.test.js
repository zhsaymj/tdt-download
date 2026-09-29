import test from 'node:test'
import assert from 'node:assert/strict'

import {
  DEM_LEVELS,
  ESRI_IMAGERY_LEVELS,
  GOOGLE_LEVELS,
  IMG_LEVELS,
  defaultAnnotateForProvider,
  defaultTaskName,
  defaultContainersForStages,
  downloadDefaultsForProvider,
  ensureImageTmsLevels,
  formatPixelResolution,
  formatPixelSize,
  formatSampleSpacing,
  formatScale72Dpi,
  levelResolutionMeters,
  levelsForProvider,
  needsRegionProbe,
  normalizeContainerMap,
} from './taskDefaults.js'
import { canAnnotate } from './provider.js'

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


// ---------- Google / Esri 影像的级别常量 ----------

test('GOOGLE_LEVELS 覆盖 1-21', () => {
  assert.equal(GOOGLE_LEVELS[0], 1)
  assert.equal(GOOGLE_LEVELS[GOOGLE_LEVELS.length - 1], 21)
  assert.equal(GOOGLE_LEVELS.length, 21)
})

test('ESRI_IMAGERY_LEVELS 覆盖 1-19', () => {
  assert.equal(ESRI_IMAGERY_LEVELS[0], 1)
  assert.equal(ESRI_IMAGERY_LEVELS[ESRI_IMAGERY_LEVELS.length - 1], 19)
  assert.equal(ESRI_IMAGERY_LEVELS.length, 19)
})

test('levelsForProvider 按数据源给出级别列表', () => {
  assert.deepEqual(levelsForProvider('tianditu_img'), IMG_LEVELS)
  assert.deepEqual(levelsForProvider('google_img'), GOOGLE_LEVELS)
  assert.deepEqual(levelsForProvider('google_road'), GOOGLE_LEVELS)
  assert.deepEqual(levelsForProvider('esri_imagery'), ESRI_IMAGERY_LEVELS)
  assert.deepEqual(levelsForProvider('esri_terrain'), DEM_LEVELS)
})

test('levelsForProvider 未知数据源回落影像级别', () => {
  assert.deepEqual(levelsForProvider('whatever'), IMG_LEVELS)
})

test('needsRegionProbe 只对 Esri 影像为真', () => {
  // Google 实测陆地处处可到 z21,无地区性降级,不必探测
  assert.equal(needsRegionProbe('google_img'), false)
  assert.equal(needsRegionProbe('esri_imagery'), true)
  assert.equal(needsRegionProbe('tianditu_img'), false)
  assert.equal(needsRegionProbe('esri_terrain'), false)
})

test('新数据源的任务名有中文标签', () => {
  const now = new Date(2026, 7, 21, 13, 14, 15)
  assert.equal(defaultTaskName('google_img', now), 'Google卫星影像_20260821_131415')
  assert.equal(defaultTaskName('esri_imagery', now), 'EsriWorldImagery_20260821_131415')
})


// ---------- 叠加路网注记的默认勾选 ----------

test('★ 所有影像数据源默认勾选「叠加路网注记」', () => {
  // 需求13-5 当年写的是"影像数据默认勾选",但判据是 isTiandituRasterProvider,
  // 只覆盖天地图三兄弟;Google/Esri 加进来后没跟上,这两个源一直默认不勾。
  for (const p of ['tianditu_img', 'tianditu_vec', 'tianditu_ter']) {
    assert.equal(defaultAnnotateForProvider(p), true, p)
  }
  for (const p of ['google_img', 'esri_imagery']) {
    assert.equal(defaultAnnotateForProvider(p), true, p)
  }
})

test('不支持注记的数据源默认不勾', () => {
  // DEM 没有注记这个概念;本地文件源不联网,没有注记可下。
  for (const p of ['esri_terrain', 'local_dem', 'local_image',
                   'local_vector', 'local_osgb', 'local_pointcloud']) {
    assert.equal(defaultAnnotateForProvider(p), false, p)
  }
})

test('默认勾选判据与 canAnnotate 恒等(不再各写一份名单)', () => {
  // 这条锁的是"单一来源":早先 taskDefaults 自己判 isTiandituRasterProvider、
  // provider.js 另判 canAnnotate,两份名单必然漂移(本项目已踩过三次)。
  // 断言恒等而非逐个写死 —— 以后加数据源只改 canAnnotate 一处即可。
  const all = ['tianditu_img', 'tianditu_vec', 'tianditu_ter',
               'google_img', 'google_hybrid', 'google_road', 'google_terrain',
               'esri_imagery', 'esri_terrain', 'local_image', 'local_dem',
               'local_vector', 'local_osgb', 'local_pointcloud',
               'osm_buildings', 'overture_buildings', '', undefined]
  for (const p of all) {
    assert.equal(defaultAnnotateForProvider(p), canAnnotate(p), String(p))
  }
})

test('影像下载默认值里的 annotate 跟着数据源走', () => {
  const stages = [
    { key: 'geotiff', default_on: true, containers: ['gtiff', 'cog'] },
    { key: 'tms', default_on: true, containers: ['tiles_dir', 'mbtiles'] },
  ]
  for (const p of ['tianditu_img', 'google_img', 'esri_imagery']) {
    assert.equal(downloadDefaultsForProvider(p, stages).annotate, true, p)
  }
  assert.equal(downloadDefaultsForProvider('esri_terrain', stages).annotate, false)
})
