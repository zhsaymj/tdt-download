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
