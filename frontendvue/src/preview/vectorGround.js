/**
 * 矢量图层的贴地渲染。
 *
 * 需求要求"用 primitive、贴地"。Cesium 的实际能力分布决定了这里必须混用两套：
 *
 *   面  GroundPrimitive            —— 是 primitive
 *   线  GroundPolylinePrimitive    —— 是 primitive
 *   点  **没有贴地的 primitive**    —— 破例用 Entity + CLAMP_TO_GROUND
 *
 * 点之所以破例：Cesium 不提供贴地的点 primitive。硬做只能在地形加载后逐个
 * 采样高程再设 z，异步时序极易出错（地形未就绪时高程全错、且地形每次加载
 * 完成都要重采样）。Entity 的 heightReference: CLAMP_TO_GROUND 是官方支持的
 * 贴地方案，这里是有意为之，不是随手写的。
 *
 * 另一个坑：GroundPrimitive / GroundPolylinePrimitive 的**静态工厂方法**
 * （fromPolygonHierarchy / fromRectangles / fromPositions）在 cesium 1.143
 * 中已全部移除，只能用构造函数 + GeometryInstance 手搓。构造前还必须
 * 先 await GroundPrimitive.initializeTerrainHeights()。
 *
 * 已知限制：GroundPrimitive 依赖地形做裁剪，**没有地形时退化为贴在椭球面**。
 * 这是 Cesium 的固有设计。
 */
import {
  Cartesian3, Color, ColorGeometryInstanceAttribute, Entity,
  GeometryInstance, GroundPrimitive, GroundPolylineGeometry,
  GroundPolylinePrimitive, HeightReference, PerInstanceColorAppearance,
  PointGraphics, PolygonGeometry, PolygonHierarchy, PolylineColorAppearance,
} from 'cesium'

/** 点要素的默认样式 */
const POINT_STYLE = { pixelSize: 8, color: Color.fromCssColorString('#f97316') }
/** 线要素的默认样式 */
const LINE_STYLE = { width: 2, color: Color.fromCssColorString('#f97316') }
/** 面要素的默认样式 */
const POLYGON_STYLE = { color: Color.fromCssColorString('#f97316').withAlpha(0.35) }

/**
 * 单图层点数超过它就回退为不贴地的点渲染。
 * 贴地的 Entity 每个点一份 GPU 资源，几万个点会明显掉帧。
 */
export const POINT_LIMIT = 5000

/** initializeTerrainHeights 只需且只能初始化一次，用 Promise 缓存 */
let terrainHeightsReady = null

function ensureTerrainHeights() {
  if (!terrainHeightsReady) {
    terrainHeightsReady = GroundPrimitive.initializeTerrainHeights()
      .catch((e) => {
        // 初始化失败不让整个矢量渲染挂掉：退化为不随地形起伏，仍能显示
        console.warn('initializeTerrainHeights 失败，矢量将不随地形起伏', e)
        terrainHeightsReady = null
      })
  }
  return terrainHeightsReady
}

/** 经纬度数组 -> 笛卡尔数组（忽略高程，贴地由 Cesium 处理） */
function toCartesians2D(coords) {
  const flat = []
  for (const c of coords) flat.push(c[0], c[1])
  return Cartesian3.fromDegreesArray(flat)
}

/** 面：GroundPrimitive + GeometryInstance（静态工厂已移除，只能手搓） */
function addPolygon(viewer, ring) {
  const prim = new GroundPrimitive({
    geometryInstances: new GeometryInstance({
      geometry: new PolygonGeometry({
        polygonHierarchy: new PolygonHierarchy(toCartesians2D(ring)),
      }),
      attributes: {
        color: ColorGeometryInstanceAttribute.fromColor(POLYGON_STYLE.color),
      },
    }),
    appearance: new PerInstanceColorAppearance({
      flat: true, translucent: POLYGON_STYLE.color.alpha < 1,
    }),
  })
  viewer.scene.primitives.add(prim)
  return prim
}

/** 线：GroundPolylinePrimitive */
function addLine(viewer, coords) {
  const prim = new GroundPolylinePrimitive({
    geometryInstances: new GeometryInstance({
      geometry: new GroundPolylineGeometry({
        positions: toCartesians2D(coords),
      }),
      attributes: {
        color: ColorGeometryInstanceAttribute.fromColor(LINE_STYLE.color),
      },
    }),
    appearance: new PolylineColorAppearance(),
  })
  viewer.scene.primitives.add(prim)
  return prim
}

/** 点：破例用 Entity —— Cesium 没有贴地的点 primitive */
function addPoints(viewer, coords) {
  const out = []
  for (const c of coords) {
    out.push(viewer.entities.add({
      position: Cartesian3.fromDegrees(c[0], c[1]),
      point: new PointGraphics({
        pixelSize: POINT_STYLE.pixelSize,
        color: POINT_STYLE.color,
        // 破例点：Cesium 无贴地点 primitive，只能走 Entity
        heightReference: HeightReference.CLAMP_TO_GROUND,
      }),
    }))
  }
  return out
}

/** 遍历任意 GeoJSON 几何，按类型分派到三个回调 */
function eachGeometry(geom, onPolygon, onLine, onPoint) {
  if (!geom) return
  const { type, coordinates: cs } = geom
  if (type === 'Point') { onPoint([cs]); return }
  if (type === 'MultiPoint') { onPoint(cs); return }
  if (type === 'LineString') { onLine(cs); return }
  if (type === 'MultiLineString') { cs.forEach(onLine); return }
  if (type === 'Polygon') { onPolygon(cs[0]); return }
  if (type === 'MultiPolygon') { cs.forEach((p) => onPolygon(p[0])); return }
  if (type === 'GeometryCollection') {
    (geom.geometries || []).forEach(
      (g) => eachGeometry(g, onPolygon, onLine, onPoint))
  }
}

/**
 * 把 GeoJSON 以贴地方式加入场景。
 *
 * 返回句柄，交给 removeVectorGround 释放。**必须释放**：GroundPrimitive 与
 * Entity 都持有 GPU 资源，服务被移除或页面卸载时不释放会泄漏。
 *
 * options.pointLimit 可覆盖默认的点数阈值。
 */
export async function addVectorGround(viewer, geojson, options = {}) {
  if (!viewer || !geojson) return null
  const pointLimit = options.pointLimit ?? POINT_LIMIT
  const features = geojson.type === 'FeatureCollection'
    ? (geojson.features || [])
    : [geojson.type === 'Feature' ? geojson : { geometry: geojson }]

  // 先数点数，决定点的渲染路径
  let pointCount = 0
  for (const f of features) {
    eachGeometry(f.geometry, () => {}, () => {},
      (cs) => { pointCount += cs.length })
  }
  const pointsDowngraded = pointCount > pointLimit

  // 只有走贴地 Entity 时才需要初始化地形高度
  if (!pointsDowngraded) await ensureTerrainHeights()

  const handle = {
    primitives: [], entities: [], pointsDowngraded, pointCount,
  }

  for (const f of features) {
    eachGeometry(f.geometry,
      (ring) => {
        if (!ring || ring.length < 3) return
        try {
          handle.primitives.push(addPolygon(viewer, ring))
        } catch (e) { console.warn('面要素渲染失败', e) }
      },
      (coords) => {
        if (!coords || coords.length < 2) return
        try {
          handle.primitives.push(addLine(viewer, coords))
        } catch (e) { console.warn('线要素渲染失败', e) }
      },
      (coords) => {
        handle.entities.push(...addPoints(viewer, coords))
      })
  }
  return handle
}

/** 释放句柄占用的资源。服务被移除或页面卸载时必须调用 */
export function removeVectorGround(viewer, handle) {
  if (!viewer || !handle) return
  for (const p of handle.primitives || []) {
    try { viewer.scene.primitives.remove(p) } catch (_) { /* 已移除 */ }
  }
  for (const e of handle.entities || []) {
    try { viewer.entities.remove(e) } catch (_) { /* 已移除 */ }
  }
  handle.primitives = []
  handle.entities = []
}
