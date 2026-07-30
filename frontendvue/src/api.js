// 后端 REST API 封装

async function req(url, opts) {
  const r = await fetch(url, opts)
  if (!r.ok) {
    let detail = r.status
    try { detail = (await r.json()).detail || detail } catch (_) { /* ignore */ }
    throw new Error(detail)
  }
  return r.status === 204 ? null : r.json()
}

export const api = {
  getConfig: () => req('/api/config'),
  getLogs: (limit = 300) => req(`/api/logs?limit=${limit}`),
  listTasks: () => req('/api/tasks'),
  estimate: ({ west, south, east, north, levels, provider }) =>
    req(`/api/tasks/estimate?west=${west}&south=${south}&east=${east}&north=${north}&levels=${levels}&provider=${provider || 'tianditu_img'}`),
  demMaxLevel: ({ west, south, east, north, provider }) =>
    req(`/api/tasks/dem_max_level?west=${west}&south=${south}&east=${east}&north=${north}&provider=${provider || 'esri_terrain'}`),
  createTask: (payload) =>
    req('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  updateTask: (id, payload) =>
    req(`/api/tasks/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  pauseTask: (id) => req(`/api/tasks/${id}/pause`, { method: 'POST' }),
  resumeTask: (id) => req(`/api/tasks/${id}/resume`, { method: 'POST' }),
  retryStage: (id, key, purge = false) =>
    req(`/api/tasks/${id}/stage/${key}/retry?purge=${purge}`, { method: 'POST' }),
  taskSize: (id) => req(`/api/tasks/${id}/size`),
  deleteTask: (id, purge) => req(`/api/tasks/${id}?purge=${purge}`, { method: 'DELETE' }),

  // ---- 天地图 tk 使用池管理 ----
  listTokens: () => req('/api/tokens'),
  addToken: (payload) =>
    req('/api/tokens', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  updateToken: (id, payload) =>
    req(`/api/tokens/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  deleteToken: (id) => req(`/api/tokens/${id}`, { method: 'DELETE' }),
  reorderTokens: (orderedIds) =>
    req('/api/tokens/reorder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ordered_ids: orderedIds }),
    }),
  resetToken: (id) => req(`/api/tokens/${id}/reset`, { method: 'POST' }),

  // ---- 三维建筑 ----
  // 上传已转好的 WGS84 GeoJSON(房屋轮廓面),返回 upload_id 与字段统计
  uploadVector: (geojson) =>
    req('/api/buildings/upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(geojson),
    }),
  uploadFields: (uploadId) => req(`/api/buildings/upload/${uploadId}/fields`),
  buildingsDiagnose: () => req('/api/buildings/diagnose'),
  // 建筑网格缓存
  buildingCache: () => req('/api/buildings/cache'),
  clearBuildingCache: (source = 'osm', bbox = null) => {
    const q = bbox
      ? `?source=${source}&west=${bbox[0]}&south=${bbox[1]}&east=${bbox[2]}&north=${bbox[3]}`
      : `?source=${source}`
    return req(`/api/buildings/cache${q}`, { method: 'DELETE' })
  },
  // 在线建筑数据包状态(数据准备走 update-building-data.bat,不走接口)
  bundleStatus: (source = 'osm') => req(`/api/buildings/bundles?source=${source}`),

  // 上传地形 GeoTIFF(建筑底面高采样用)。
  // 直接发原始字节而非 multipart:后端用请求流边收边写盘,几百 MB 的地形
  // 不必整份进内存,也省掉 python-multipart 依赖。
  uploadDem: (file) =>
    req(`/api/buildings/dem/upload?filename=${encodeURIComponent(file.name)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: file,
    }),
  demInfo: (demId) => req(`/api/buildings/dem/${demId}`),
}

// 本地行政区索引(adcode/name/level 层级树,不含边界坐标)
export const loadAreaIndex = () => req('/area-index.json')

// 按 adcode 请求 DataV 行政区边界 geojson(不带 _full,仅该区自身边界)
export async function fetchAreaBoundary(adcode) {
  const r = await fetch(`https://geo.datav.aliyun.com/areas_v3/bound/${adcode}.json`)
  if (!r.ok) throw new Error(`行政区边界请求失败(${r.status})`)
  return r.json()
}
