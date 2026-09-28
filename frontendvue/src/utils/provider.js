// 数据源类型判定。
// 集中在一处而非各组件里做字符串匹配:早先用 provider.includes('overture')
// 判断"是否三维建筑任务",新增 OSM 数据源后四个组件同时漏判。

// 三维建筑数据源(与后端 providers/buildings.py 的 BUILDING_LAYERS 保持一致)
export const BUILDING_PROVIDERS = ['osm_buildings', 'overture_buildings', 'local_vector']

// DEM/地形数据源
export const DEM_PROVIDERS = ['esri_terrain']

/**
 * 仅作底图预览、**不出现在下载列表**的影像源。
 *
 * 这三个是 Google 的制图渲染层(含路网/路线图/地形),不是原始影像,
 * 作为下载成果意义不大;但仍保留在底图下拉里 —— 它们当参考底图很有用。
 *
 * 后端仍登记这些 provider(PROVIDER_GRID/PROVIDER_KIND),因为底图预览走
 * /api/tiles/{provider}/... 需要它们;此处约束的只是"下载列表"这一层。
 */
export const BASEMAP_ONLY_IMAGE_PROVIDERS = [
  'google_hybrid', 'google_road', 'google_terrain',
]

/** EPSG:3857 墨卡托影像源。与天地图的 4326 网格不同构。 */
export const MERCATOR_IMAGE_PROVIDERS = [
  'google_img', 'google_hybrid', 'google_road', 'google_terrain',
  'esri_imagery',
]

/**
 * 天地图注记的最高级别。与后端 `core/runner.ANNOTATION_MAX_Z` 对应,改动需同步。
 *
 * 实测 z19+ 返回 HTTP 200 + 213 字节空图(0 不透明像素),不是 404。
 * Google 开放到 z21,故 z19~z21 的成果没有注记可叠。
 */
export const ANNOTATION_MAX_Z = 18

/**
 * 该数据源是否支持"叠加路网注记"。
 *
 * 注记是天地图提供的同网格透明覆盖层(cia/cva/cta,与底图按类型配对)。
 * Google/Esri **也支持**:它们走 3857,而天地图注记有 3857 版本(cia_w),
 * 同格可直接对取、零重采样(见 providers/tianditu.py 的 build_annotation_provider)。
 *
 * 不支持的只有:DEM(无此概念)与本地文件源(不联网,没有注记可下)。
 */
/** 本地文件源:不联网,没有注记可下。 */
export const LOCAL_FILE_PROVIDERS = [
  'local_image', 'local_dem', 'local_vector', 'local_osgb', 'local_pointcloud',
]

export function canAnnotate(provider) {
  if (isDemProvider(provider)) return false
  if (LOCAL_FILE_PROVIDERS.includes(provider)) return false
  return true
}

// 本地三维数据源:OSGB 倾斜模型目录 / 点云文件(与后端 formats.py 的登记保持一致)
export const MODEL3D_PROVIDERS = ['local_osgb', 'local_pointcloud']

// 可预览阶段白名单:任一阶段完成(done/skipped)即可打开三维预览,不必整任务完成。
// TaskQueue(「预览」按钮)与 DataDialog(「三维预览」按钮)共用;新增可预览阶段只改这里——
// 早先这份名单在 TaskQueue/DataDialog/PreviewApp 三处各抄一份(见文件头教训)。
export const PREVIEWABLE_STAGE_KEYS = ['tms', 'osm', 'terrain', 'tile_3d', 'convert_3d', 'pc_tile_3d']

// 产出 3dtiles/ 瓦片集的阶段(三维建筑 tile_3d;三维数据 convert_3d/pc_tile_3d):
// PreviewApp 据此决定是否加载 Cesium3DTileset。同样只在这里维护。
export const TILESET_STAGE_KEYS = ['tile_3d', 'convert_3d', 'pc_tile_3d']

// 阶段 key → 提交/补导用的格式名。DEM 整幅图阶段 key 是历史遗留的 `dem`,格式名是
// `geotiff`;三维阶段 key 带管线前缀(convert_3d/pc_*),映射到后端 formats.py
// _FORMAT_TO_STAGE 里的格式名。ProcessDialog(提交)与 AddExportDialog(补导)共用——
// 早先 AddExportDialog 只抄了 dem→geotiff 一条,三维任务补导时格式名对不上阶段。
export const FMT_NAME_OF_STAGE = {
  dem: 'geotiff',
  convert_3d: 'tile_3d',
  pc_dsm: 'dsm',
  pc_dem: 'dem',
  pc_tile_3d: 'tile_3d',
}

export function fmtNameOf(stageKey) {
  return FMT_NAME_OF_STAGE[stageKey] || stageKey
}

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

// ---- 本地数据服务的分类(与后端 core/service_scan.py 的 KIND_* 一致) ----
// 集中在此而非各组件里硬编码:本项目已有过三次"名单抄多份、漏改一处"的教训
// (见本文件头部说明)。
export const SERVICE_KINDS = ['model', 'imagery', 'vector', 'terrain']

export const SERVICE_KIND_LABELS = {
  model: '模型',
  imagery: '影像',
  vector: '矢量',
  terrain: '地形',
}

export function serviceKindLabel(kind) {
  return SERVICE_KIND_LABELS[String(kind || '')] || String(kind || '')
}

/** 所选级别里超出注记上限的部分(为空表示全部可用)。 */
export function annotationExcessLevels(levels) {
  return (levels || [])
    .filter((z) => Number(z) > ANNOTATION_MAX_Z)
    .sort((a, b) => a - b)
}

/**
 * 该级别集合能否叠加注记。
 *
 * 全部超限时注记一张都下不了 —— 界面据此置灰勾选框,
 * 避免出现"勾了但完全没效果"。部分超限仍可用,由界面提示影响哪些级别。
 */
export function annotationUsable(levels) {
  return (levels || []).some((z) => Number(z) <= ANNOTATION_MAX_Z)
}
