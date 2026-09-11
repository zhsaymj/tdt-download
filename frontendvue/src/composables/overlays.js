/**
 * 把成果叠加到二维地图上。
 *
 * 图层类型由后端 /api/tasks/{id}/layers 判定(见 backend/core/overlay.py),
 * 这里只负责按 kind 构造对应的 OpenLayers 图层:
 *   tiles            → XYZ 源(瓦片目录或后端 mbtiles 端点)
 *   cog              → ol/source/GeoTIFF 直读。OL 10.3 自带 geotiff.js,靠 HTTP
 *                      Range 请求按需读块,故**不必切片**(实测 /output 的
 *                      StaticFiles 返回 206,前提成立)
 *   vector           → GeoJSON 直读
 *   vector_convert   → 后端转 GeoJSON 后再读(浏览器读不了 gpkg/shp)
 *   raster_only_bbox → 只画范围框(非 tiled 的大图直读会拖整份数据)
 *
 * 层级:底图 0、叠加 10~89、绘制/预览 100+(见 DRAW_Z),保证画的范围永远在最上面。
 */
import TileLayer from 'ol/layer/Tile'
import WebGLTileLayer from 'ol/layer/WebGLTile'
import VectorLayer from 'ol/layer/Vector'
import XYZ from 'ol/source/XYZ'
import GeoTIFF from 'ol/source/GeoTIFF'
import VectorSource from 'ol/source/Vector'
import { GeoJSON } from 'ol/format'
import { Style, Stroke, Fill, Circle as CircleStyle } from 'ol/style'
import { fromExtent as polygonFromExtent } from 'ol/geom/Polygon'
import Feature from 'ol/Feature'
import { transformExtent } from 'ol/proj'
import { createXYZ } from 'ol/tilegrid'

/** 叠加层的 zIndex 起点;绘制层用 100 以上,始终压在叠加层之上 */
const OVERLAY_Z = 10
export const DRAW_Z = 100

/** 叠加层可用的 zIndex 跨度:必须留在 DRAW_Z 之下,否则会盖住用户画的选区 */
const Z_SPAN = DRAW_Z - 1 - OVERLAY_Z

/** 已叠加的图层:key → { layer, desc } */
const added = new Map()

function bboxToFeature(bounds4326) {
  const ext = transformExtent(bounds4326, 'EPSG:4326', 'EPSG:3857')
  return new Feature(polygonFromExtent(ext))
}

/** 只画范围框:用于不适合直读的栅格,以及三维成果的位置提示 */
function makeBboxLayer(desc, z) {
  const src = new VectorSource()
  if (desc.bounds_wgs84) src.addFeature(bboxToFeature(desc.bounds_wgs84))
  return new VectorLayer({
    source: src, zIndex: z,
    style: new Style({
      stroke: new Stroke({ color: '#a855f7', width: 2, lineDash: [8, 4] }),
      fill: new Fill({ color: 'rgba(168,85,247,0.06)' }),
    }),
  })
}

/**
 * 瓦片图层。两种网格要分开处理,混用会错位:
 *
 * - scheme 'xyz'(OSM 瓦片):Web 墨卡托,与地图同投影,XYZ 源默认行为即可。
 * - scheme 'tms'(本工具的 TMS 包):gdal2tiles **geodetic** 网格,是 EPSG:4326、
 *   0 级 2×1 瓦片。地图是 3857,故必须显式给 projection + 自定义 tileGrid,
 *   让 OL 客户端重投影;直接当 XYZ 用会整体错位。
 *   且 TMS 行号自南向北,与 XYZ 相反,要翻转 y。
 *
 * mbtiles 端点已在后端统一按 XYZ 的 y 接收(翻转在后端做),故它走 'xyz' 分支
 * 即可——但它的**级别**仍是 geodetic 的 gdal 级,故 tms 类型的 mbtiles 也要
 * 用 geodetic 网格。后端返回的 scheme 已区分好这一点。
 */
function makeTileLayer(desc, z) {
  const minZoom = desc.minzoom ?? 0
  const maxZoom = desc.maxzoom ?? 22
  const flip = !!desc.flip_y

  const fillUrl = (tz, tx, ty) => desc.url
    .replace('{z}', tz).replace('{x}', tx)
    .replace('{y}', flip ? (1 << tz) - 1 - ty : ty)

  if (desc.grid === 'geodetic') {
    // gdal2tiles geodetic:EPSG:4326、0 级 2 列 × 1 行。地图是 3857,靠 OL 客户端
    // 重投影显示;不给 projection/tileGrid 直接当 XYZ 用会整体错位。
    const tileGrid = createXYZ({
      extent: [-180, -90, 180, 90],
      tileSize: 256,
      maxZoom,
      maxResolution: 180 / 256,
    })
    return new TileLayer({
      zIndex: z,
      source: new XYZ({
        projection: 'EPSG:4326',
        tileGrid,
        minZoom,
        maxZoom,
        tileUrlFunction: ([tz, tx, ty]) =>
          (tz < minZoom || tz > maxZoom) ? undefined : fillUrl(tz, tx, ty),
      }),
    })
  }
  return new TileLayer({
    zIndex: z,
    source: new XYZ({
      minZoom,
      maxZoom,
      tileUrlFunction: ([tz, tx, ty]) =>
        (tz < minZoom || tz > maxZoom) ? undefined : fillUrl(tz, tx, ty),
    }),
  })
}

/**
 * COG / 普通 tif 直读。
 * 单波段(高程、灰度)必须给拉伸区间,否则 3000~4800 的高程值会被当成 0~255
 * 截断、整幅显示为白。vmin/vmax 由后端抽样按 2%~98% 分位算好。
 */
function makeCogLayer(desc, z) {
  const source = new GeoTIFF({
    sources: [{ url: desc.url }],
    // 不做重投影插值,按原样读;normalize 关掉以便自己控制拉伸
    normalize: desc.bands >= 3,
    interpolate: false,
    convertToRGB: 'auto',
  })
  const opts = { source, zIndex: z }
  if (desc.bands === 1 && desc.vmin != null && desc.vmax != null) {
    const span = Math.max(desc.vmax - desc.vmin, 1e-6)
    // 线性拉伸到 0~1 的灰度。alpha 恒 1:单波段源没有第 2 个波段可当透明通道
    // (写 ['band', 2] 会取不到值、整幅变透明);nodata 区由 source 的掩膜处理。
    const g = ['clamp', ['/', ['-', ['band', 1], desc.vmin], span], 0, 1]
    opts.style = { color: ['array', g, g, g, 1] }
  }
  return new WebGLTileLayer(opts)
}

function makeVectorLayer(desc, z) {
  return new VectorLayer({
    zIndex: z,
    source: new VectorSource({
      url: desc.url,
      format: new GeoJSON({ dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857' }),
    }),
    style: new Style({
      stroke: new Stroke({ color: '#f97316', width: 1.2 }),
      fill: new Fill({ color: 'rgba(249,115,22,0.18)' }),
      image: new CircleStyle({
        radius: 3, fill: new Fill({ color: '#f97316' }),
      }),
    }),
  })
}

/** 按描述构造图层;不支持的 kind 返回 null */
function build(desc, z) {
  switch (desc.kind) {
    case 'tiles': return makeTileLayer(desc, z)
    case 'cog': return makeCogLayer(desc, z)
    case 'vector':
    case 'vector_convert': return makeVectorLayer(desc, z)
    case 'raster_only_bbox': return makeBboxLayer(desc, z)
    default: return null
  }
}

/**
 * 叠加一个图层。key 需全局唯一(通常是 `${taskId}:${layer.id}`)。
 *
 * zIndex 不在这里定,由 applyOrder 按 store 里的数组顺序统一重排。早先用
 * `OVERLAY_Z + added.size` 会给出重复值:加 A、B 后移除 A 再加 C,B 与 C 都是 11,
 * 谁盖谁由添加顺序偶然决定,排序也就无从谈起。
 */
export function addOverlay(map, key, desc) {
  if (!map || added.has(key)) return added.get(key)?.layer || null
  let layer = null
  try {
    layer = build(desc, OVERLAY_Z)
  } catch (e) {
    console.warn('构造叠加图层失败', desc, e)
    return null
  }
  if (!layer) return null
  map.addLayer(layer)
  added.set(key, { layer, desc })
  return layer
}

export function removeOverlay(map, key) {
  const hit = added.get(key)
  if (!hit) return
  try { map.removeLayer(hit.layer) } catch (_) { /* 已移除 */ }
  added.delete(key)
}

export function hasOverlay(key) { return added.has(key) }

/**
 * 按给定顺序重排 zIndex:keys[0] 在最下,末尾在最上(与图层面板从上到下的
 * 视觉顺序相反,面板渲染时反转一次即可)。
 *
 * 步长通常为 1;图层数超过可用跨度时按比例压缩,保证不越过 DRAW_Z——绘制的选区框
 * 必须始终可见,否则用户没法确认叠加成果和选区对不对得上。
 */
export function applyOrder(keys) {
  const n = keys.length
  if (!n) return
  const step = n > Z_SPAN ? Z_SPAN / n : 1
  keys.forEach((key, i) => {
    const hit = added.get(key)
    if (hit) hit.layer.setZIndex(OVERLAY_Z + Math.round(i * step))
  })
}

export function setOverlayVisible(key, on) {
  added.get(key)?.layer.setVisible(!!on)
}

/** 透明度 0~1。瓦片/COG/矢量/范围框都是 ol/layer,setOpacity 通用 */
export function setOverlayOpacity(key, v) {
  added.get(key)?.layer.setOpacity(Math.min(1, Math.max(0, v)))
}

/**
 * 缩放到某图层范围。返回是否定位成功。
 *
 * 只有栅格类图层的 desc 带 bounds_wgs84(后端 _inspect_raster 读的),瓦片目录、
 * MBTiles 与矢量都没有——而本工具的主产物恰是 TMS 瓦片包,所以必须用任务 bbox
 * 兜底,否则最常见的图层点「定位」毫无反应。
 */
export function zoomToOverlay(map, key, fallbackBbox = null) {
  const hit = added.get(key)
  if (!hit || !map) return false
  const b = hit.desc.bounds_wgs84 || fallbackBbox
  if (!b || b.length !== 4) return false
  map.getView().fit(transformExtent(b, 'EPSG:4326', 'EPSG:3857'),
    { padding: [40, 40, 40, 40], duration: 300, maxZoom: 19 })
  return true
}

export function clearOverlays(map) {
  for (const key of [...added.keys()]) removeOverlay(map, key)
}
