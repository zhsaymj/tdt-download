// 坐标系:CGCS2000 proj4 定义注册 + 输出坐标系下拉选项
import proj4 from 'proj4'

// 向 proj4 注册 CGCS2000 定义(proj4 默认只认少数 EPSG)。GRS80 椭球,3 度带高斯克吕格。
export function registerProj4Defs() {
  proj4.defs('EPSG:4490', '+proj=longlat +ellps=GRS80 +no_defs')
  // 4534–4554:中央经线 75E–135E,坐标不含带号(false_easting=500000)
  for (let i = 0; i <= 20; i++) {
    const cm = 75 + i * 3
    proj4.defs(`EPSG:${4534 + i}`,
      `+proj=tmerc +lat_0=0 +lon_0=${cm} +k=1 +x_0=500000 +y_0=0 +ellps=GRS80 +units=m +no_defs`)
  }
  // 4513–4533:第 25–45 带,坐标含带号(false_easting=带号*1e6+500000)
  for (let i = 0; i <= 20; i++) {
    const zone = 25 + i
    const cm = zone * 3
    proj4.defs(`EPSG:${4513 + i}`,
      `+proj=tmerc +lat_0=0 +lon_0=${cm} +k=1 +x_0=${zone * 1000000 + 500000} +y_0=0 +ellps=GRS80 +units=m +no_defs`)
  }
}

/**
 * 按经度定 CGCS2000 3 度带:带号 25–45,中央经线 75°E–135°E(覆盖国境)。
 * 返回不含带号的那套编码(EPSG:4534–4554),量测显示的是平面坐标本身,
 * 加 1000 万的带号前缀只会让读数难认。范围外返回 null。
 */
export function cgcs2000Zone(lon) {
  const zone = Math.round(lon / 3)
  if (zone < 25 || zone > 45) return null
  return { zone, cm: zone * 3, epsg: `EPSG:${4534 + (zone - 25)}` }
}

/**
 * WGS84 经纬度 → CGCS2000 3 度带平面坐标(米)。范围外返回 null。
 * 两者椭球差异在本工具的量测精度下可忽略(米级以内),故直接按定义投影。
 * 需先调用 registerProj4Defs()。
 */
export function toCgcs2000(lon, lat) {
  const z = cgcs2000Zone(lon)
  if (!z) return null
  try {
    const [x, y] = proj4('EPSG:4326', z.epsg, [lon, lat])
    return { ...z, x, y }
  } catch (_) {
    return null
  }
}

/**
 * 点位坐标文本(经纬度行 / 平面坐标行 / 中央经线行)。
 * 地图标注与结果列表共用同一份格式,免得同一个点在两处读数长得不一样。
 *
 * height 传入时在坐标末尾追加高程(两处坐标同为该点高程,只是水平参照不同);
 * 2D 量测没有高程,不传即不显示。
 */
function heightSuffix(height) {
  return Number.isFinite(height) ? `,${height.toFixed(2)}m` : ''
}

export function fmtLonLatText(lon, lat, height) {
  return `【EPSG:4326】:${lon.toFixed(6)},${lat.toFixed(6)}${heightSuffix(height)}`
}

/** 平面坐标行。3 度带范围外明说,不要静默留空 */
export function fmtPlaneText(plane, height) {
  if (!plane) return '【CGCS2000】:超出 3 度带范围(75°E–135°E)'
  return `【${plane.epsg}】:${plane.x.toFixed(3)},${plane.y.toFixed(3)}${heightSuffix(height)}`
}

/** 中央经线单独一行:平面坐标带号是读数的前提,挤在坐标后面容易被忽略 */
export function fmtCentralMeridianText(plane) {
  return plane ? `【中央经线】:${plane.cm}°E` : ''
}

// 输出坐标系下拉选项:WGS84 + CGCS2000 3 度带(两套 EPSG 编码)
export function crsOptions() {
  const opts = [
    { value: 'EPSG:4326', label: 'WGS84 经纬度 (EPSG:4326)' },
    { value: 'EPSG:4490', label: 'CGCS2000 经纬度 (EPSG:4490)' },
  ]
  for (let i = 0; i <= 20; i++) {
    const cm = 75 + i * 3
    opts.push({ value: `EPSG:${4534 + i}`, label: `CGCS2000 3度带 中央经线${cm}°E (EPSG:${4534 + i})` })
  }
  for (let i = 0; i <= 20; i++) {
    const zone = 25 + i
    opts.push({ value: `EPSG:${4513 + i}`, label: `CGCS2000 3度带 第${zone}带/含带号 (EPSG:${4513 + i})` })
  }
  return opts
}
