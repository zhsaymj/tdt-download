// 数据源类型判定。
// 集中在一处而非各组件里做字符串匹配:早先用 provider.includes('overture')
// 判断"是否三维建筑任务",新增 OSM 数据源后四个组件同时漏判。

// 三维建筑数据源(与后端 providers/buildings.py 的 BUILDING_LAYERS 保持一致)
export const BUILDING_PROVIDERS = ['osm_buildings', 'overture_buildings', 'local_vector']

// DEM/地形数据源
export const DEM_PROVIDERS = ['esri_terrain']

export function isBuildingProvider(provider) {
  return BUILDING_PROVIDERS.includes(String(provider || ''))
}

export function isDemProvider(provider) {
  return DEM_PROVIDERS.includes(String(provider || ''))
}

// 任务类型:'buildings' | 'dem' | 'image'
export function taskKindOf(task) {
  const p = task?.provider
  if (isBuildingProvider(p)) return 'buildings'
  if (isDemProvider(p)) return 'dem'
  return 'image'
}
