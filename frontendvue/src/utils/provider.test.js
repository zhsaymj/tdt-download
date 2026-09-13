import test from 'node:test'
import assert from 'node:assert/strict'

import {
  BUILDING_PROVIDERS,
  DEM_PROVIDERS,
  FMT_NAME_OF_STAGE,
  MODEL3D_PROVIDERS,
  PREVIEWABLE_STAGE_KEYS,
  TILESET_STAGE_KEYS,
  fmtNameOf,
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
