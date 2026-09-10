// 矢量解析:shp/geojson/kml → WGS84 geojson,含源坐标系识别与转换
import proj4 from 'proj4'
// shpjs v6:默认导出 = getShapefile(处理 zip);parseShp/parseDbf/combine 为具名导出
import shp, { parseShp, parseDbf, combine } from 'shpjs'
import { kml as kmlToGeoJSON } from '@tmcw/togeojson'
import { GeoJSON, KML } from 'ol/format'

// GeoJSON 规范几何类型(首字母大写)。部分工具导出小写(如 "polygon"),
// OpenLayers 按规范只认标准写法,故导入后统一规范化,避免静默读不出几何。
const GEOM_TYPE_MAP = {
  point: 'Point', multipoint: 'MultiPoint',
  linestring: 'LineString', multilinestring: 'MultiLineString',
  polygon: 'Polygon', multipolygon: 'MultiPolygon',
  geometrycollection: 'GeometryCollection',
}

function normalizeGeometry(g) {
  if (!g || !g.type) return
  const std = GEOM_TYPE_MAP[String(g.type).toLowerCase()]
  if (std) g.type = std
  if (g.type === 'GeometryCollection' && Array.isArray(g.geometries)) {
    g.geometries.forEach(normalizeGeometry)
  }
}

// 规范化整个 geojson 的几何类型(FeatureCollection/Feature/几何三层),原地修改
export function normalizeGeojsonTypes(geojson) {
  if (!geojson || !geojson.type) return geojson
  const t = String(geojson.type).toLowerCase()
  if (t === 'featurecollection') {
    ; (geojson.features || []).forEach((f) => f && normalizeGeometry(f.geometry))
  } else if (t === 'feature') {
    normalizeGeometry(geojson.geometry)
  } else {
    normalizeGeometry(geojson)
  }
  return geojson
}

// 遍历 geojson 内所有几何
function eachGeometry(geojson, fn) {
  const geoms = geojson.type === 'FeatureCollection'
    ? geojson.features.map((f) => f.geometry).filter(Boolean)
    : geojson.type === 'Feature' ? [geojson.geometry] : [geojson]
  geoms.forEach((g) => g && g.coordinates && fn(g))
}

// 粗判坐标是否落在经纬度范围(是否已是 WGS84)
export function looksLikeLonLat(geojson) {
  let ok = true, checked = 0
  const scan = (c) => {
    if (!ok || checked > 200) return
    if (typeof c[0] === 'number') {
      checked++
      if (Math.abs(c[0]) > 180.5 || Math.abs(c[1]) > 90.5) ok = false
    } else {
      c.forEach(scan)
    }
  }
  eachGeometry(geojson, (g) => scan(g.coordinates))
  return ok
}

// 用 proj4 把坐标从 srcDef(EPSG 字符串或 .prj WKT)转到 WGS84,原地修改
export function reprojectGeojson(geojson, srcDef) {
  const tr = proj4(srcDef, 'EPSG:4326')
  const conv = (c) => {
    if (typeof c[0] === 'number') {
      const [x, y] = tr.forward([c[0], c[1]])
      c[0] = x; c[1] = y
    } else {
      c.forEach(conv)
    }
  }
  eachGeometry(geojson, (g) => conv(g.coordinates))
  return geojson
}

// 解析单个 <coordinates> 文本为坐标数组 [[lon,lat],...](丢弃高程)
function parseCoordText(text) {
  return text.trim().split(/\s+/).map((tuple) => {
    const [lon, lat] = tuple.split(',').map(Number)
    return [lon, lat]
  }).filter((c) => Number.isFinite(c[0]) && Number.isFinite(c[1]))
}

// 兜底 KML 解析:togeojson 解析不出几何时(如大疆 WPML 航线),
// 直接从 DOM 提取 Polygon/LineString/Point 的 <coordinates>。
function kmlFallback(dom) {
  const features = []
  const getText = (el, tag) => {
    const n = el.getElementsByTagName(tag)
    return n.length ? n[0].textContent : null
  }
  // Polygon(取 outerBoundaryIs/LinearRing 的 coordinates)
  for (const poly of dom.getElementsByTagName('Polygon')) {
    const c = getText(poly, 'coordinates')
    if (!c) continue
    let ring = parseCoordText(c)
    if (ring.length < 3) continue
    // 闭合环
    const [f, l] = [ring[0], ring[ring.length - 1]]
    if (f[0] !== l[0] || f[1] !== l[1]) ring = [...ring, f]
    features.push({ type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: [ring] } })
  }
  // LineString
  for (const ls of dom.getElementsByTagName('LineString')) {
    const c = getText(ls, 'coordinates')
    if (!c) continue
    const coords = parseCoordText(c)
    if (coords.length < 2) continue
    features.push({ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: coords } })
  }
  // Point(仅在没有面/线时才用,避免航点污染)
  if (!features.length) {
    for (const pt of dom.getElementsByTagName('Point')) {
      const c = getText(pt, 'coordinates')
      if (!c) continue
      const coords = parseCoordText(c)
      if (coords.length) features.push({ type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: coords[0] } })
    }
  }
  return { type: 'FeatureCollection', features }
}

// 把 geojson 几何/要素写成 KML 文本(输入须为 WGS84,KML 规范同样是 WGS84)
export function geojsonToKml(geojson, name = 'range') {
  const src = (geojson.type === 'FeatureCollection' || geojson.type === 'Feature')
    ? geojson
    : { type: 'Feature', properties: {}, geometry: geojson }
  const features = new GeoJSON().readFeatures(src)
  if (!features.length) throw new Error('没有可导出的图形')
  features.forEach((f, i) => {
    f.setProperties({ name: features.length > 1 ? `${name}_${i + 1}` : name })
  })
  return new KML().writeFeatures(features)
}

// 触发浏览器下载文本文件
export function downloadText(filename, text,
  mime = 'application/vnd.google-earth.kml+xml') {
  const url = URL.createObjectURL(new Blob([text], { type: `${mime};charset=utf-8` }))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  // 立即 revoke 会让部分浏览器取消下载,延后一拍
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

// 解析文件列表为 geojson,返回 { geojson, prjText }
export async function parseVectorFiles(fileList) {
  const files = Array.from(fileList || [])
  if (!files.length) return null
  const byExt = (ext) => files.find((f) => f.name.toLowerCase().endsWith(ext))

  const zipFile = byExt('.zip')
  const shpFile = byExt('.shp')
  const kmlFile = byExt('.kml')
  const gjFile = byExt('.geojson') || byExt('.json')
  const prjFile = byExt('.prj')
  const prjText = prjFile ? await prjFile.text() : null

  let geojson = null
  if (zipFile) {
    geojson = await shp(await zipFile.arrayBuffer())
  } else if (shpFile) {
    const dbfFile = byExt('.dbf')
    const shpBuf = await shpFile.arrayBuffer()
    const dbfBuf = dbfFile ? await dbfFile.arrayBuffer() : undefined
    const geoms = parseShp(shpBuf)
    const recs = dbfBuf ? parseDbf(dbfBuf) : []
    geojson = combine([geoms, recs])
  } else if (gjFile) {
    geojson = JSON.parse(await gjFile.text())
  } else if (kmlFile) {
    const dom = new DOMParser().parseFromString(await kmlFile.text(), 'text/xml')
    geojson = kmlToGeoJSON(dom) // KML 规范即 WGS84
    // togeojson 解析不出几何时(如大疆 WPML 航线),用兜底提取
    if (!geojson || !geojson.features || !geojson.features.length) {
      geojson = kmlFallback(dom)
    }
  } else {
    throw new Error('无法识别的矢量文件。支持 GeoJSON / KML / Shapefile(.zip 或 .shp+.dbf+.shx,建议附 .prj)。')
  }
  // 规范化几何类型(兼容小写 polygon 等非标准导出)
  normalizeGeojsonTypes(geojson)
  return { geojson, prjText }
}
