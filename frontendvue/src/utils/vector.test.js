import test from 'node:test'
import assert from 'node:assert/strict'
import { resolveVectorImport, looksLikeLonLat } from './vector.js'

// CGCS2000 3 度带中央经线 105°E(不带带号),与 crs.js 的 EPSG:4544 同一套参数
const PRJ_CM105 = 'PROJCS["CGCS2000_3_Degree_GK_CM_105E",'
  + 'GEOGCS["GCS_China_Geodetic_Coordinate_System_2000",'
  + 'DATUM["D_China_2000",SPHEROID["CGCS2000",6378137,298.257222101]],'
  + 'PRIMEM["Greenwich",0],UNIT["Degree",0.0174532925199433]],'
  + 'PROJECTION["Gauss_Kruger"],PARAMETER["False_Easting",500000],'
  + 'PARAMETER["False_Northing",0],PARAMETER["Central_Meridian",105],'
  + 'PARAMETER["Scale_Factor",1],PARAMETER["Latitude_Of_Origin",0],UNIT["Meter",1]]'

/** 造一个单面 FeatureCollection,环闭合 */
function poly(ring, name = 'f') {
  return {
    type: 'FeatureCollection',
    features: [{ type: 'Feature', properties: { name }, geometry: { type: 'Polygon', coordinates: [ring] } }],
  }
}

const LONLAT = poly([[105, 30], [105.1, 30], [105.1, 30.1], [105, 30]])
// 中央经线 105°E 上的 3 度带坐标(米),对应经纬度约 (105, 30)
const GK_105 = poly([[500000, 3319000], [509000, 3319000], [509000, 3328000], [500000, 3319000]])
// 带带号编码的 X(19500000),按"不带带号"的 .prj 去转会得到经度约 302° → 明显越界
const GK_ZONED = poly([[19500000, 3319000], [19509000, 3319000], [19509000, 3328000], [19500000, 3319000]])

test('导入范围:交给地图的必须是内层 geojson,不是 {geojson, prjText} 包装体', () => {
  // 这是线上报错 "Unsupported GeoJSON type: undefined" 的回归用例:
  // 包装体没有 type,OpenLayers 读不出要素类型就直接抛错
  const r = resolveVectorImport({ geojson: LONLAT, prjText: null })
  assert.ok(r, '不该返回 null')
  assert.equal(r.geojson.type, 'FeatureCollection')
  assert.equal(r.geojson.features.length, 1)
  assert.equal(r.needSrs, false)
})

test('裸 geojson 入参也认(调用方忘了拆包装时不再静默失败)', () => {
  const r = resolveVectorImport(LONLAT)
  assert.equal(r.geojson.type, 'FeatureCollection')
  assert.equal(r.needSrs, false)
})

test('多图层 zip:shpjs 返回的数组要合并成一个 FeatureCollection', () => {
  const r = resolveVectorImport({ geojson: [poly(LONLAT.features[0].geometry.coordinates, 'a'), poly(LONLAT.features[0].geometry.coordinates, 'b')] })
  assert.equal(r.geojson.type, 'FeatureCollection')
  assert.equal(r.geojson.features.length, 2)
  assert.equal(r.needSrs, false)
})

test('解析不出内容时返回 null,由调用方提示', () => {
  assert.equal(resolveVectorImport(null), null)
  assert.equal(resolveVectorImport({}), null)
  assert.equal(resolveVectorImport({ geojson: null, prjText: null }), null)
})

test('带 .prj 的投影坐标自动转成经纬度,不必弹坐标系选择', () => {
  const r = resolveVectorImport({ geojson: GK_105, prjText: PRJ_CM105 })
  assert.equal(r.needSrs, false)
  const [lon, lat] = r.geojson.features[0].geometry.coordinates[0][0]
  assert.ok(Math.abs(lon - 105) < 0.01, `lon=${lon}`)
  assert.ok(Math.abs(lat - 30) < 0.05, `lat=${lat}`)
  assert.equal(looksLikeLonLat(r.geojson), true)
})

test('.prj 不可信(转完仍越界)时弹窗手选,且原坐标不能被试转改坏', () => {
  const before = JSON.stringify(GK_ZONED)
  const r = resolveVectorImport({ geojson: GK_ZONED, prjText: PRJ_CM105 })
  assert.equal(r.needSrs, true)
  // 试转必须走副本:原地改的话,用户在弹窗里再选一次坐标系就成了二次投影
  assert.equal(JSON.stringify(r.geojson), before, '试转污染了原坐标')
})

test('.prj 解析失败时不抛错,退回手选坐标系', () => {
  const r = resolveVectorImport({ geojson: GK_105, prjText: 'NOT A PRJ' })
  assert.equal(r.needSrs, true)
  assert.equal(r.geojson.type, 'FeatureCollection')
})
