import test from 'node:test'
import assert from 'node:assert/strict'

import {
  BUILDING_PROVIDERS,
  DEM_PROVIDERS,
  MODEL3D_PROVIDERS,
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
