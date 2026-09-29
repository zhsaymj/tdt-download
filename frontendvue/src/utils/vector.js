// 矢量解析:shp/geojson/kml → WGS84 geojson,含源坐标系识别与转换
import proj4 from 'proj4'
// shpjs v6:默认导出 = getShapefile(处理 zip);parseShp/parseDbf/combine 为具名导出
import shp, { parseShp, parseDbf, combine } from 'shpjs'
import { kml as kmlToGeoJSON } from '@tmcw/togeojson'
// 写 'ol/format.js' 而非 'ol/format':ol 未声明 exports 字段,目录形式在 Node 里
// 是 ERR_UNSUPPORTED_DIR_IMPORT,单元测试直接 import 本模块会挂(浏览器/Vite 两者都行)
import { GeoJSON, KML } from 'ol/format.js'

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

// geojson 深拷贝。GeoJSON 按规范就是纯 JSON,走 JSON 往返没有兼容性风险;
// 用副本试转是为了让"转了但结果不可信"能干净回退(见 resolveVectorImport)。
function cloneGeojson(g) {
  return JSON.parse(JSON.stringify(g))
}

/**
 * 把 parseVectorFiles 的结果收敛成「一个可直接用的 geojson + 要不要让用户手选源坐标系」。
 *
 * 返回值:{ geojson, needSrs }。needSrs 为 true 时 geojson 保持原坐标,调用方弹 SrsModal,
 * 用户选完 EPSG 后自己 reprojectGeojson。解析不出内容返回 null。
 *
 * 入参两种形态都收:{ geojson, prjText } 包装,或裸 geojson。历史上调用方忘了拆包装,
 * 直接把包装体喂给 OpenLayers,读不到 type 就抛 "Unsupported GeoJSON type: undefined"
 * ——这里兜住,免得同一个坑再踩一次。
 */
export function resolveVectorImport(parsed) {
  const raw = (Array.isArray(parsed) || (parsed && typeof parsed.type === 'string'))
    ? parsed
    : parsed?.geojson
  if (!raw) return null

  // shpjs 解含多个 shp 的压缩包时返回 FeatureCollection 数组,合并成一个再往下走
  const geojson = Array.isArray(raw)
    ? { type: 'FeatureCollection', features: raw.flatMap((g) => g?.features || []) }
    : raw

  if (looksLikeLonLat(geojson)) return { geojson, needSrs: false }

  // 不像经纬度但带了 .prj:先按 .prj 试转一次,能落到经纬度范围就直接用。
  // 试转必须走副本——reprojectGeojson 是原地改的,若转完仍越界(说明 .prj 与数据
  // 对不上),留着被改坏的坐标再让用户手选一次坐标系就成了二次投影。
  if (parsed.prjText) {
    try {
      const trial = reprojectGeojson(cloneGeojson(geojson), parsed.prjText)
      if (looksLikeLonLat(trial)) return { geojson: trial, needSrs: false }
    } catch (_) { /* .prj 解不了,落到手选坐标系 */ }
  }
  return { geojson, needSrs: true }
}

// 解析文件列表为 geojson,返回 { geojson, prjText }。
// geojson 可能是数组(多图层 zip),交给 resolveVectorImport 收敛后再用。
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
