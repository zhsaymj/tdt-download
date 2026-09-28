import test from 'node:test'
import assert from 'node:assert/strict'

import {
  BASEMAP_ONLY_IMAGE_PROVIDERS,
  BUILDING_PROVIDERS,
  DEM_PROVIDERS,
  FMT_NAME_OF_STAGE,
  MODEL3D_PROVIDERS,
  PREVIEWABLE_STAGE_KEYS,
  TILESET_STAGE_KEYS,
  fmtNameOf,
  MERCATOR_IMAGE_PROVIDERS,
  ANNOTATION_MAX_Z,
  annotationExcessLevels,
  annotationUsable,
  canAnnotate,
  isBuildingProvider,
  isDemProvider,
  isModel3dProvider,
  taskKindOf,
} from './provider.js'

test('本地三维数据(OSGB/点云)归为 model3d,不再落进 image', () => {
  assert.equal(taskKindOf({ provider: 'local_osgb' }), 'model3d')
  assert.equal(taskKindOf({ provider: 'local_pointcloud' }), 'model3d')
})

test('isModel3dProvider 只认两个本地三维数据源', () => {
  assert.deepEqual(MODEL3D_PROVIDERS, ['local_osgb', 'local_pointcloud'])
  assert.ok(isModel3dProvider('local_osgb'))
  assert.ok(isModel3dProvider('local_pointcloud'))
  assert.ok(!isModel3dProvider('tianditu_img'))
  assert.ok(!isModel3dProvider('osm_buildings'))
  assert.ok(!isModel3dProvider('esri_terrain'))
  assert.ok(!isModel3dProvider(undefined))
  assert.ok(!isModel3dProvider(null))
})

test('PREVIEWABLE_STAGE_KEYS:任一完成即可预览的阶段白名单(集中维护,组件不再各抄一份)', () => {
  assert.deepEqual(PREVIEWABLE_STAGE_KEYS, ['tms', 'osm', 'terrain', 'tile_3d', 'convert_3d', 'pc_tile_3d'])
})

test('TILESET_STAGE_KEYS:产出 3dtiles/ 瓦片集的阶段,必然也可预览', () => {
  assert.deepEqual(TILESET_STAGE_KEYS, ['tile_3d', 'convert_3d', 'pc_tile_3d'])
  for (const k of TILESET_STAGE_KEYS) {
    assert.ok(PREVIEWABLE_STAGE_KEYS.includes(k), k + ' 应在可预览白名单内')
  }
})

test('FMT_NAME_OF_STAGE:阶段 key → 格式名,覆盖三维阶段且不动栅格 dem→geotiff', () => {
  // 与后端 formats.py _FORMAT_TO_STAGE 的格式名对齐;ProcessDialog(提交)与
  // AddExportDialog(补导)共用这一份,改一处即全改。
  assert.deepEqual(FMT_NAME_OF_STAGE, {
    dem: 'geotiff',
    convert_3d: 'tile_3d',
    pc_dsm: 'dsm',
    pc_dem: 'dem',
    pc_tile_3d: 'tile_3d',
  })
  assert.equal(fmtNameOf('dem'), 'geotiff')
  assert.equal(fmtNameOf('pc_tile_3d'), 'tile_3d')
  assert.equal(fmtNameOf('convert_3d'), 'tile_3d')
  // 未登记的阶段 key 原样返回(如 tms/osm 等栅格阶段)
  assert.equal(fmtNameOf('tms'), 'tms')
  assert.equal(fmtNameOf('tile_3d'), 'tile_3d')
})

test('既有 buildings/dem/image 分支不受影响', () => {
  for (const p of BUILDING_PROVIDERS) {
    assert.equal(taskKindOf({ provider: p }), 'buildings', p)
    assert.ok(isBuildingProvider(p), p)
    assert.ok(!isModel3dProvider(p), p)
  }
  for (const p of DEM_PROVIDERS) {
    assert.equal(taskKindOf({ provider: p }), 'dem', p)
    assert.ok(isDemProvider(p), p)
    assert.ok(!isModel3dProvider(p), p)
  }
  assert.equal(taskKindOf({ provider: 'tianditu_img' }), 'image')
  assert.equal(taskKindOf({ provider: 'local_image' }), 'image')
  assert.equal(taskKindOf({}), 'image')
})


// ---------- 叠加路网注记的适用性 ----------

test('天地图影像源支持叠加注记', () => {
  for (const k of ['tianditu_img', 'tianditu_vec', 'tianditu_ter']) {
    assert.equal(canAnnotate(k), true, k)
  }
})

test('Google / Esri 影像源支持叠加注记', () => {
  // 天地图注记有 3857 版本(cia_w),与这两个源同格,可直接对取、零重采样。
  for (const k of ['google_img', 'google_hybrid', 'google_road',
                   'google_terrain', 'esri_imagery']) {
    assert.equal(canAnnotate(k), true, k)
  }
})

test('注记最高 18 级', () => {
  // 实测 z19+ 返回 200+213B 空图;与后端 runner.ANNOTATION_MAX_Z 对应
  assert.equal(ANNOTATION_MAX_Z, 18)
})

test('所选级别全高于 18 时注记不可用', () => {
  assert.equal(annotationUsable([19, 20, 21]), false)
  assert.equal(annotationUsable([17, 18, 19]), true)
  assert.equal(annotationUsable([16, 17]), true)
  assert.equal(annotationUsable([]), false)
})

test('超限级别列表', () => {
  assert.deepEqual(annotationExcessLevels([17, 18, 19, 21]), [19, 21])
  assert.deepEqual(annotationExcessLevels([16, 17]), [])
})

test('DEM 不支持叠加注记', () => {
  assert.equal(canAnnotate('esri_terrain'), false)
})

test('本地文件源不支持叠加注记', () => {
  // 不联网,没有注记可下
  assert.equal(canAnnotate('local_image'), false)
})

test('墨卡托源列表覆盖全部 Google/Esri 影像', () => {
  for (const k of ['google_img', 'google_hybrid', 'google_road',
                   'google_terrain', 'esri_imagery']) {
    assert.ok(MERCATOR_IMAGE_PROVIDERS.includes(k), k)
  }
})

test('仅底图的三个源在墨卡托列表里', () => {
  for (const k of BASEMAP_ONLY_IMAGE_PROVIDERS) {
    assert.ok(MERCATOR_IMAGE_PROVIDERS.includes(k), k)
  }
})
