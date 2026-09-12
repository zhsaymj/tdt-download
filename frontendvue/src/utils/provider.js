// 数据源类型判定。
// 集中在一处而非各组件里做字符串匹配:早先用 provider.includes('overture')
// 判断"是否三维建筑任务",新增 OSM 数据源后四个组件同时漏判。

// 三维建筑数据源(与后端 providers/buildings.py 的 BUILDING_LAYERS 保持一致)
export const BUILDING_PROVIDERS = ['osm_buildings', 'overture_buildings', 'local_vector']

// DEM/地形数据源
export const DEM_PROVIDERS = ['esri_terrain']

// 本地三维数据源:OSGB 倾斜模型目录 / 点云文件(与后端 formats.py 的登记保持一致)
export const MODEL3D_PROVIDERS = ['local_osgb', 'local_pointcloud']

export function isBuildingProvider(provider) {
  return BUILDING_PROVIDERS.includes(String(provider || ''))
}

export function isDemProvider(provider) {
  return DEM_PROVIDERS.includes(String(provider || ''))
}

export function isModel3dProvider(provider) {
  return MODEL3D_PROVIDERS.includes(String(provider || ''))
}

// 任务类型:'buildings' | 'dem' | 'model3d' | 'image'
export function taskKindOf(task) {
  const p = task?.provider
  if (isBuildingProvider(p)) return 'buildings'
  if (isModel3dProvider(p)) return 'model3d'
  if (isDemProvider(p)) return 'dem'
  return 'image'
}
