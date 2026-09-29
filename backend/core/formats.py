"""导出格式注册表:把"哪种数据能出哪些格式"从散落各处的分支收敛到一处声明。

接入前(2026-07)的状况:`models.build_stage_defs` 按 provider 硬分三支、`runner` 的
`executors` 按 `is_dem` 二选一、`api/tasks.py` 有十余处 `is_dem/is_building`、
`_clear_stage_output` 按 key 手写 if-elif、前端 ParamsPanel 三个 tab 各自硬编码格式
列表。加一个格式要改五六处,且格式绑死在"谁下载的"而不是"数据是什么"——DEM 出不了
TMS 瓦片纯粹因为这层绑定,不是技术限制。

三个概念分开建模,这是本模块的全部要点:

**DataKind(数据类型)**:数据源产出什么。provider 声明自己的 kind,阶段声明接受哪些
kind。解耦后"新数据源能出哪些格式"由 kind 自动推出,不必逐处加分支。

**ExportStage(处理阶段)**:一次处理动作。就是现有的 geotiff/tms/osm/terrain 那一层,
key 与已落库的 stage key 保持一致(旧任务的 stages 字段要能继续读)。

**Container(容器格式)**:同一份成果写成什么文件。栅格容器 GTiff/COG,矢量容器
GeoJSON/GPKG/Shapefile。**这层必须与阶段分开**——否则 COG、GPKG、SHP 会各自变成一个
平铺的"格式",而它们其实是"同一个阶段的不同写出方式":合并 GeoTIFF 阶段写成 COG、
建筑轮廓阶段写成 SHP,是同一件事的两个实例。若平铺,阶段数会按 容器×阶段 组合爆炸,
且"一个阶段同时出 tif 和 cog"这种常见诉求无法表达。

尚未接入运行时(纯新增,不影响现有管线);接入顺序见模块末尾 ROADMAP。
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ---------- 数据类型 ----------

class DataKind:
    """数据的语义类型。决定可用的处理阶段,与 provider 无关。"""

    RASTER_IMAGE = "raster_image"    # 影像/底图栅格(RGB 三波段)
    RASTER_DEM = "raster_dem"        # 高程栅格(单波段浮点,真实海拔)
    VECTOR_POLYGON = "vector_polygon"  # 矢量面(建筑轮廓、选区)
    VECTOR_LINE = "vector_line"      # 矢量线(等高线)
    TILES_RASTER = "tiles_raster"    # 已切好的栅格瓦片目录(TMS/OSM)
    TILES_3D = "tiles_3d"            # 3D Tiles(b3dm)
    TILES_TERRAIN = "tiles_terrain"  # Cesium quantized-mesh 地形切片
    # 三维数据处理(本地文件源):输入为 osgb 模型/点云,产出细分的 3D Tiles 类型
    MESH_OSGB = "mesh_osgb"          # OSGB 倾斜摄影模型(本地目录)
    POINT_CLOUD = "point_cloud"      # 点云(las/laz 等本地文件)
    TILES_3D_MODEL = "tiles_3d_model"  # 3D Tiles 模型(由 osgb 转换)
    TILES_3D_POINT = "tiles_3d_point"  # 3D Tiles 点云(pnts)

    ALL = (RASTER_IMAGE, RASTER_DEM, VECTOR_POLYGON, VECTOR_LINE,
           TILES_RASTER, TILES_3D, TILES_TERRAIN,
           MESH_OSGB, POINT_CLOUD, TILES_3D_MODEL, TILES_3D_POINT)


# ---------- 容器格式(同一份成果的不同写出方式)----------

@dataclass(frozen=True)
class Container:
    """文件容器格式。

    key       落库/接口用的稳定标识
    label     界面展示名
    kinds     接受的数据类型
    ext       主文件扩展名(shapefile 另有 .shx/.dbf/.prj/.cpg 附属文件)
    writer    写出后端:rasterio / pyogrio / sqlite / builtin
    driver    对应 GDAL 驱动名(writer 为 rasterio/pyogrio 时有效)
    sidecars  附属文件扩展名(交付时必须一并拷走,少一个 .prj 就丢坐标系)
    note      界面提示与取舍说明
    """

    key: str
    label: str
    kinds: tuple[str, ...]
    ext: str
    writer: str
    driver: str = ""
    sidecars: tuple[str, ...] = ()
    note: str = ""


_RASTER = (DataKind.RASTER_IMAGE, DataKind.RASTER_DEM)
_VECTOR = (DataKind.VECTOR_POLYGON, DataKind.VECTOR_LINE)

# 管线标识。与 runner 的三条执行路径对应(影像/DEM 共用 runner.py,建筑走
# runner_buildings.py),用于区分同类型数据在不同管线里的归属。
PIPE_RASTER = "raster"
PIPE_BUILDING = "building"
# 三维数据处理管线(本地 osgb/点云输入,走 runner_3d)
PIPE_3D = "3d"

CONTAINERS: dict[str, Container] = {c.key: c for c in (
    # 栅格容器
    Container("cog", "COG(云优化 GeoTIFF)", _RASTER, ".tif", "rasterio", "COG",
              note="仍是合法 GeoTIFF,多了内部瓦片与金字塔,大图浏览快"),
    Container("gtiff", "GeoTIFF", _RASTER, ".tif", "rasterio", "GTiff",
              note="通用性最好,老软件都能读"),
    # PNG/JPEG 靠 worldfile(.pgw/.jgw)带坐标,不能存 CRS 本身,仅供出图
    Container("png", "PNG + worldfile", _RASTER, ".png", "rasterio", "PNG",
              sidecars=(".pgw", ".wld"),
              note="出图用;worldfile 只存像素尺寸与原点,不含坐标系定义"),
    Container("jpeg", "JPEG + worldfile", (DataKind.RASTER_IMAGE,), ".jpg",
              "rasterio", "JPEG", sidecars=(".jgw", ".wld"),
              note="有损压缩,仅影像可用;DEM 会丢高程精度故不开放"),
    Container("ascii_grid", "ASCII Grid", (DataKind.RASTER_DEM,), ".asc",
              "rasterio", "AAIGrid", sidecars=(".prj",),
              note="纯文本高程网格,老分析软件常用"),
    Container("xyz", "XYZ 高程点", (DataKind.RASTER_DEM,), ".xyz",
              "rasterio", "XYZ", note="逐点文本,体积大,仅小范围适用"),
    # 矢量容器
    Container("geojson", "GeoJSON", _VECTOR, ".geojson", "pyogrio", "GeoJSON",
              note="恒 WGS84 经纬度,前端直读"),
    Container("gpkg", "GeoPackage", _VECTOR, ".gpkg", "pyogrio", "GPKG",
              note="单文件、无字段名长度限制、UTF-8 无编码坑,优先选它"),
    Container("shapefile", "Shapefile", _VECTOR, ".shp", "pyogrio",
              "ESRI Shapefile",
              sidecars=(".shx", ".dbf", ".prj", ".cpg"),
              note="国内交付常硬性要求;字段名限 10 字符、单文件上限 2GB"),
    # 瓦片容器
    Container("tiles_dir", "瓦片目录", (DataKind.TILES_RASTER,), "", "builtin",
              note="{z}/{x}/{y} 目录结构,可直接挂 HTTP 服务"),
    Container("mbtiles", "MBTiles", (DataKind.TILES_RASTER,), ".mbtiles",
              "sqlite",
              note="把碎瓦片装进单个 sqlite,避免几万小文件拷盘极慢"),
)}


def containers_for(kind: str) -> list[Container]:
    """列出某数据类型可用的容器格式(界面下拉直接用这个,不再硬编码)。"""
    return [c for c in CONTAINERS.values() if kind in c.kinds]


# ---------- 处理阶段 ----------

@dataclass(frozen=True)
class ExportStage:
    """一个处理阶段的声明。

    key          阶段标识,**必须与已落库 stages 里的 key 一致**(旧任务要能继续读)
    label        进度界面展示名
    accepts      接受的输入数据类型
    produces     产出的数据类型(等高线等两级链路靠它串接)
    containers   可选的容器格式(空则该阶段自带固定输出结构)
    outputs      产出物路径模板;{name} 任务名、{z} 级别、{ext} 容器扩展名
    needs_levels 是否逐级别产出(影响进度分母与清理时的遍历)
    default_on   新建任务时是否默认勾选
    note         界面提示
    """

    key: str
    label: str
    accepts: tuple[str, ...]
    produces: str
    containers: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    needs_levels: bool = False
    default_on: bool = False
    note: str = ""
    #: 管线内执行顺序(小的先执行)。不能靠声明顺序:影像要 geotiff 最先,而 DEM 的
    #: 整幅图阶段 key 是历史遗留的 "dem"、声明位置在后,两条管线的期望顺序冲突。
    order: int = 100
    #: 管线归属。同一 DataKind 可能被两条管线用到:base_dem 产出 RASTER_DEM 但属
    #: 建筑管线的内部环节,不该出现在"下载 DEM 能出什么格式"的列表里。
    #: internal=True 的阶段由管线自身按需插入,不给用户勾选。
    pipeline: str = ""
    internal: bool = False
    #: 是否必须有下载的瓦片缓存。本地文件作输入源时没有瓦片缓存(见
    #: core/local_raster.py),这类阶段对它不可用——例如"导出原始 LERC 瓦片"
    #: 本质就是把缓存里的瓦片拷出来,没有缓存就无从导出。
    needs_tile_cache: bool = False


STAGES: dict[str, ExportStage] = {s.key: s for s in (
    # ---- 栅格:合并成整幅 ----
    # 只接受影像栅格:DEM 的整幅图走下面的 "dem" 阶段(历史 key 分立,见其注释)。
    # 解耦的收益体现在 tms/osm——它们原先只有影像能用,现在两种栅格都接。
    ExportStage("geotiff", "合并 GeoTIFF", (DataKind.RASTER_IMAGE,),
                DataKind.RASTER_IMAGE,
                containers=("cog", "gtiff", "png", "jpeg"),
                outputs=("{name}_z{z}{ext}",),
                needs_levels=True, default_on=True, order=10,
                pipeline=PIPE_RASTER,
                note="每个选中级别各出一张带坐标整幅图"),
    # ---- 栅格:切瓦片 ----
    # 原先仅影像可用;DEM 接入后靠已有的晕渲/伪彩渲染出可视化瓦片
    # outputs 只写目录形态;MBTiles 的产出名由 outputs_mbtiles 另给——
    # 瓦片阶段是唯一"容器改变产出结构"的情形(目录 → 单文件),而且两者可以
    # 并存(keep_tiles_dir),故不能靠单个模板 + 扩展名推导。
    ExportStage("tms", "切 TMS 瓦片", _RASTER, DataKind.TILES_RASTER,
                containers=("tiles_dir", "mbtiles"),
                outputs=("tms/",), default_on=True, order=30,
                pipeline=PIPE_RASTER,
                note="gdal2tiles geodetic 网格,与天地图 EPSG:4326 无损直映射"),
    ExportStage("osm", "切 OSM 瓦片", _RASTER, DataKind.TILES_RASTER,
                containers=("tiles_dir", "mbtiles"),
                outputs=("osm/",), order=40, pipeline=PIPE_RASTER,
                note="Web 墨卡托 XYZ,主流前端底图网格"),
    # ---- DEM 专属 ----
    ExportStage("tiles", "导出原始瓦片", (DataKind.RASTER_DEM,),
                DataKind.TILES_RASTER, outputs=("tiles/",), order=50,
                pipeline=PIPE_RASTER, needs_tile_cache=True,
                note="保留 Esri LERC 编码原始瓦片,不解码不重采样"),
    ExportStage("terrain", "切 Cesium 地形", (DataKind.RASTER_DEM,),
                DataKind.TILES_TERRAIN, outputs=("terrain/",), order=60,
                pipeline=PIPE_RASTER,
                note="quantized-mesh,配 layer.json"),
    ExportStage("contour", "提取等高线", (DataKind.RASTER_DEM,),
                DataKind.VECTOR_LINE,
                containers=("geojson", "gpkg", "shapefile"),
                outputs=("{name}_contour{ext}",), order=70,
                pipeline=PIPE_RASTER,
                note="DEM 的矢量衍生成果;等距可调"),
    # DEM 的高程整幅图。历史上 DEM 与影像用了不同的 stage key(影像 geotiff、
    # DEM dem),库里有 3 条存量任务存着 "dem",故必须保留此 key 而不能合并进
    # geotiff——合并会让旧任务的已完成阶段读不出来、重跑时重复拼接。
    # 二者语义相同(合并整幅栅格),差别仅在 DEM 另可出晕渲图。
    ExportStage("dem", "高程 GeoTIFF", (DataKind.RASTER_DEM,),
                DataKind.RASTER_DEM,
                containers=("cog", "gtiff", "ascii_grid", "xyz", "png"),
                outputs=("{name}_dem_z{z}{ext}", "{name}_hillshade_z{z}{ext}"),
                needs_levels=True, default_on=True, order=20,
                pipeline=PIPE_RASTER,
                note="真实海拔单波段;可另出晕渲图"),
    # ---- 三维建筑管线 ----
    # accepts 含 VECTOR_POLYGON:取数阶段本身没有"上游输入",但它必须出现在
    # stages_for(VECTOR_POLYGON) 里——它是矢量成果(GeoJSON/GPKG/SHP)的产出点,
    # 缺了它界面上建筑就选不到矢量容器。
    ExportStage("fetch_buildings", "拉取建筑轮廓", (DataKind.VECTOR_POLYGON,),
                DataKind.VECTOR_POLYGON,
                containers=("geojson", "gpkg", "shapefile"),
                outputs=("{name}_buildings{ext}",), default_on=True,
                order=10, pipeline=PIPE_BUILDING,
                note="取数阶段,同时是矢量成果的产出点"),
    ExportStage("base_dem", "准备底面高程", (DataKind.VECTOR_POLYGON,),
                DataKind.RASTER_DEM, outputs=("{name}_dem.tif",),
                order=20, pipeline=PIPE_BUILDING, internal=True,
                note="逐栋采样 DEM 写进顶点;b3dm 是绝对定位几何,不会自动贴地形"),
    ExportStage("build_mesh", "建筑白模建模", (DataKind.VECTOR_POLYGON,),
                DataKind.VECTOR_POLYGON, default_on=True,
                order=30, pipeline=PIPE_BUILDING, internal=True,
                note="轮廓挤出为体块并三角化"),
    ExportStage("tile_3d", "切 3D Tiles(b3dm)", (DataKind.VECTOR_POLYGON,),
                DataKind.TILES_3D, outputs=("3dtiles/",), default_on=True,
                order=40, pipeline=PIPE_BUILDING,
                note="GPU 批渲染,不受 Cesium Entity 数量限制"),
    # ---- 三维数据处理管线 ----
    # dem/tile_3d 的 key 已被栅格/建筑管线占用(ExportStage 单实例、pipeline
    # 互斥),三维阶段一律另起 key:convert_3d/pc_*(点云前缀)。输入均为本地
    # 文件,无瓦片缓存、不逐级别产出(needs_tile_cache/needs_levels 取默认 False)。
    ExportStage("convert_3d", "转换 3D Tiles", (DataKind.MESH_OSGB,),
                DataKind.TILES_3D_MODEL, outputs=("3dtiles/",),
                default_on=True, order=10, pipeline=PIPE_3D,
                note="osgb 倾斜摄影模型原样转为 3D Tiles,配 tileset.json"),
    ExportStage("pc_dsm", "生成 DSM", (DataKind.POINT_CLOUD,),
                DataKind.RASTER_DEM, outputs=("{name}_dsm.tif",),
                default_on=True, order=10, pipeline=PIPE_3D,
                note="数字表面模型:取首回波/全部点,含地表附着物"),
    ExportStage("pc_dem", "生成 DEM", (DataKind.POINT_CLOUD,),
                DataKind.RASTER_DEM, outputs=("{name}_dem.tif",),
                default_on=True, order=20, pipeline=PIPE_3D,
                note="数字高程模型:仅地面点,裸地高程"),
    ExportStage("pc_tile_3d", "切 3D Tiles(pnts)", (DataKind.POINT_CLOUD,),
                DataKind.TILES_3D_POINT, outputs=("3dtiles/",),
                default_on=True, order=30, pipeline=PIPE_3D,
                note="点云切为 pnts 瓦片,浏览器端可漫游"),
)}


# ---------- 导出格式名 → 阶段 key ----------
# 接口/落库里的 export 字段用的是"格式名",与阶段 key 并非一一对应:
#   - DEM 的 "geotiff" 与 "hillshade" 都由 dem 阶段产出(一次拼接两种成果)
#   - 影像的 "geotiff" 就是 geotiff 阶段
# 这层映射按 kind 区分,取代原先 build_stage_defs 里的 if is_dem 分支。
_FORMAT_TO_STAGE: dict[str, dict[str, str]] = {
    DataKind.RASTER_DEM: {
        "geotiff": "dem", "hillshade": "dem",
        "tiles": "tiles", "terrain": "terrain", "contour": "contour",
    },
    DataKind.RASTER_IMAGE: {
        "geotiff": "geotiff", "tms": "tms", "osm": "osm",
    },
    # 三维管线:"tile_3d" 格式名在建筑管线指 b3dm 阶段,在三维管线是另起的
    # 转换/切片阶段,按 kind 区分不会串;"dsm"/"dem" 为点云专有格式名
    DataKind.MESH_OSGB: {"tile_3d": "convert_3d"},
    DataKind.POINT_CLOUD: {"dsm": "pc_dsm", "dem": "pc_dem",
                           "tile_3d": "pc_tile_3d"},
}


#: 所有合法的导出格式名(供接口层白名单校验)。
#: 含 "b3dm":建筑管线的历史格式名,库里有存量值,阶段推导不看它但白名单需放行。
ALL_FORMAT_NAMES: frozenset[str] = frozenset(
    {f for table in _FORMAT_TO_STAGE.values() for f in table}
    | {"tms", "osm", "b3dm"}
)


def stage_key_for_format(kind: str, fmt: str) -> str | None:
    """把导出格式名解析成阶段 key;该 kind 不支持时返回 None。"""
    table = _FORMAT_TO_STAGE.get(kind)
    if table and fmt in table:
        return table[fmt]
    # 未在映射表里的格式名直接当阶段 key(tms/osm 对两种栅格同名同义)
    stage = STAGES.get(fmt)
    if stage and kind in stage.accepts:
        return fmt
    return None


def stage_label(stage_key: str, formats: list[str] | None = None) -> str:
    """取阶段展示名。dem 阶段的标签随勾选动态变化(与旧行为逐字一致)。"""
    if stage_key == "dem":
        fs = set(formats or ())
        want_dem, want_hs = "geotiff" in fs, "hillshade" in fs
        if want_dem and want_hs:
            return "高程 + 晕渲 GeoTIFF"
        if want_hs:
            return "晕渲图"
        return "高程 GeoTIFF"
    stage = STAGES.get(stage_key)
    return stage.label if stage else stage_key


def stages_for(kind: str, pipeline: str = "",
               include_internal: bool = False,
               has_tile_cache: bool = True) -> list[ExportStage]:
    """列出某数据类型可用的处理阶段(界面格式勾选列表的数据来源)。

    pipeline 限定管线(不传则不限)。默认排除 internal 阶段——它们由管线自动
    插入,不该出现在用户可勾选的格式列表里。
    has_tile_cache=False 时排除需要下载缓存的阶段(本地文件作输入源的情形)。
    """
    out = []
    for s in STAGES.values():
        if kind not in s.accepts:
            continue
        if pipeline and s.pipeline and s.pipeline != pipeline:
            continue
        if s.internal and not include_internal:
            continue
        if s.needs_tile_cache and not has_tile_cache:
            continue
        out.append(s)
    return sorted(out, key=lambda s: s.order)


def plan_stages(provider: str, formats: list[str],
                base_height_mode: str = "terrain") -> list[ExportStage]:
    """按数据源与勾选格式解析出该跑哪些阶段,已按执行顺序排好。

    这是 models.build_stage_defs 的实现内核——它只负责把这里返回的阶段包装成
    落库用的 dict。放在本模块是为了让"格式→阶段"的推导只有一处。
    """
    kind = kind_of(provider)
    if kind == DataKind.VECTOR_POLYGON:
        # 建筑管线的阶段由管线自身决定,不看 formats:取数与建模是必经环节,
        # 底面高仅 terrain 模式需要(其余模式用固定高度,无需采样 DEM)。
        keys = ["fetch_buildings"]
        if base_height_mode == "terrain":
            keys.append("base_dem")
        keys += ["build_mesh", "tile_3d"]
        return [STAGES[k] for k in keys]

    local = is_local_source(provider)
    picked: list[ExportStage] = []
    seen: set[str] = set()
    for fmt in formats:
        key = stage_key_for_format(kind, fmt)
        if key is None or key in seen:
            continue
        stage = STAGES[key]
        # 本地源没有瓦片缓存,"导出原始瓦片"这类阶段无从执行
        if local and stage.needs_tile_cache:
            continue
        seen.add(key)
        picked.append(stage)
    return sorted(picked, key=lambda s: s.order)


def chain_to(kind: str) -> list[ExportStage]:
    """列出能产出某数据类型的阶段。用于两级链路:
    要 GPKG 等高线 → contour 产出 VECTOR_LINE → 再由容器层写成 gpkg。
    """
    return [s for s in STAGES.values() if s.produces == kind]


# ---------- 数据源 → 数据类型 ----------
# 这张表取代散落在 api/tasks.py、core/runner.py、models.py 的十余处
# is_dem_provider()/is_building_provider() 分支。新增数据源只在此登记一行。

# ---------- 数据源 → 网格类型 ----------
# 坐标系不是"数据语义"而是"网格约定",故不新开 DataKind —— 那要改 DataKind.ALL
# 及所有 (RASTER_IMAGE,) 元组,侵入面大。这里单独一张表,与 PROVIDER_KIND 并列。
# 术语沿用项目服务层已有的说法(core/service_scan.py、core/service_bounds.py)。

#: 网格类型
GEO_GEODETIC = "geodetic"   # EPSG:4326 经纬度瓦片(天地图 TileMatrixSet=c)
GEO_MERCATOR = "mercator"   # EPSG:3857 Web 墨卡托 XYZ

#: provider key -> 网格类型。未登记者回落 geodetic(与旧行为一致:
#: 旧代码没有网格概念,影像一律按 4326 处理)。
PROVIDER_GRID: dict[str, str] = {
    # 天地图:EPSG:4326 经纬度瓦片
    "tianditu_img": GEO_GEODETIC,
    "tianditu_vec": GEO_GEODETIC,
    "tianditu_ter": GEO_GEODETIC,
    # Google 影像:EPSG:3857 墨卡托 XYZ
    "google_img": GEO_MERCATOR,
    "google_hybrid": GEO_MERCATOR,
    "google_road": GEO_MERCATOR,
    "google_terrain": GEO_MERCATOR,
    # Esri World Imagery:同为墨卡托 XYZ,与 Google 网格完全同构
    # (实测 165 张瓦片 9 窗口互相关,偏移 ≤4m 且不随位置变化)
    "esri_imagery": GEO_MERCATOR,
    # 现有 DEM 本来就是墨卡托网格,登记后可统一分流
    "esri_terrain": GEO_MERCATOR,
    "aws_terrain": GEO_MERCATOR,
}


def grid_of(provider: str) -> str:
    """取数据源的网格类型。提交前即可调用,无需构造 provider 实例。"""
    if provider == "img":          # 兼容早期落库的短 key
        provider = "tianditu_img"
    return PROVIDER_GRID.get(provider, GEO_GEODETIC)


#: 各导出格式的**原生网格**。未列出者不限(用源自己的默认网格)。
#:
#: tms 是 gdal2tiles geodetic(4326);osm 是 Web 墨卡托 XYZ(3857)。
#: 两种格式的网格不同构,所以"跨网格"的那一种必然要重投影。
_FORMAT_GRID: dict[str, str] = {
    "osm": GEO_MERCATOR,
    "tms": GEO_GEODETIC,
}

#: 数据源**原生提供**哪些网格(未登记者只有自己的默认网格)。
#:
#: 目前只有天地图两套都有:同一份影像提供 `img_c/vec_c/ter_c`(TileMatrixSet=c,
#: EPSG:4326)与 `img_w/vec_w/ter_w`(TileMatrixSet=w,EPSG:3857)两组服务。
#: Google/Esri 没有 geodetic 那套,故不登记 —— 它们出 tms 只能重投影(由需求37
#: 的 cubic 核兜着质量)。
#:
#: 注记(cia/cva/cta)不在这里:它们不直接作为下载目标,网格跟随调用方。
PROVIDER_GRIDS: dict[str, tuple[str, ...]] = {
    "tianditu_img": (GEO_GEODETIC, GEO_MERCATOR),
    "tianditu_vec": (GEO_GEODETIC, GEO_MERCATOR),
    "tianditu_ter": (GEO_GEODETIC, GEO_MERCATOR),
}


def download_grids_of(provider: str, export_formats) -> list[str]:
    """该任务实际要下载哪些网格(稳定顺序,geodetic 在前)。

    规则:从源自己的默认网格出发;对每个请求格式,若它需要另一种网格**且源确实
    提供那种网格**,就一并下载。于是:

        天地图 + tms          → [geodetic]        同构、无损直映射
        天地图 + osm          → [mercator]        3857→3857、不重投影
        天地图 + tms,osm      → [geodetic,mercator]  两套都下,各自无损
        Google + tms,osm      → [mercator]        只有 3857,tms 仍重投影

    **不落库**:纯函数,预估接口与 worker 调同一个即可保持一致 —— 若某一方自己
    再写一遍判定,两处必然漂移(本项目已踩过多次"名单抄多份")。
    """
    default = grid_of(provider)
    avail = PROVIDER_GRIDS.get(provider, ())
    required: set[str] = set()
    for fmt in (export_formats or []):
        need = _FORMAT_GRID.get(fmt)
        # 不限网格的格式(geotiff/dem/terrain/contour),或源没有那种原生网格
        # (只能重投影) → 都落到源自己的默认网格上。
        #
        # ⚠️ 默认网格**不是无条件要的**:天地图只勾 osm 时不需要 `_c` 那套
        # (没有 COG 要出),下了就是白下(下载量与 tk 配额都翻倍)。
        if need is None or need not in avail:
            required.add(default)
        else:
            required.add(need)
    if not required:
        # 空格式列表(提交侧会拦,这里兜底):至少把默认网格下了
        required.add(default)
    return sorted(required, key=lambda g: (g != GEO_GEODETIC, g))


def default_on_stage_keys(provider: str,
                          stages: list[ExportStage] | None = None) -> set[str]:
    """该数据源默认勾选的导出阶段 key。

    在注册表的静态 `default_on` 之上,**按源自身的网格**改写瓦片格式:

      天地图(EPSG:4326)        → tms(gdal2tiles geodetic),与源同构、无损直映射
      Google/Esri(EPSG:3857)  → osm(Web 墨卡托 XYZ),3857→3857 无损

    为什么不能只靠 `default_on`:那个标志是静态的,而"哪个瓦片网格无损"取决于
    源自己的网格。对墨卡托源出 geodetic TMS 必须先重投影 —— 实测高频能量只剩
    68%(bilinear),而前端显示 geodetic TMS 时 OL 还要再转一次 3857,端到端约
    44%,细笔画的文字标注明显发虚(需求37)。OSM 那条路文件无损,且前端按 3857
    渲染、不再重投影。

    stages 传该数据源**实际可用**的阶段(来自 stages_for);不传则按默认参数取。
    前端读 /api/capabilities 的 default_on 决定默认勾选,故 main.py 必须用本函数。
    """
    if stages is None:
        stages = stages_for(kind_of(provider))
    keys = {s.key for s in stages if s.default_on}
    avail = {s.key for s in stages}
    want = "osm" if grid_of(provider) == GEO_MERCATOR else "tms"
    drop = "tms" if want == "osm" else "osm"
    if want in avail:
        keys.add(want)
    if drop in avail:
        keys.discard(drop)
    return keys


#: 未登记数据源的级别回落(与旧行为一致:影像 1~18)
_DEFAULT_Z_CAP = 18


def z_cap_of(provider: str) -> int:
    """数据源的**服务级**最高级别。

    ⚠️ 这是级别范围的唯一判定处。此前 api/tasks.py 按 provider 配置判定、
    models.level_list 却硬编码 18,两者矛盾且 create_task 走后者 ——
    导致 Google 的 z21 与 Esri 的 z19 实际下不到(请求 [20,21] 会被过滤成
    空列表,任务直接建不出来),且不报错。

    注意这只是**服务级天花板**。Esri 影像的实际可用级别随地区变化,
    由 /api/tasks/imagery_max_level 按选区探测(见设计 §3.11)。
    """
    from ..config import settings
    from ..providers.terrain import DEM_LAYERS, is_dem_provider

    if provider == "img":
        provider = "tianditu_img"
    if is_dem_provider(provider):
        return DEM_LAYERS[provider][2]
    from ..providers.esri_imagery import is_esri_imagery_provider
    from ..providers.google import is_google_provider
    if is_google_provider(provider):
        return int(settings.google.max_zoom)
    if is_esri_imagery_provider(provider):
        return int(settings.esri_imagery.max_zoom)
    return _DEFAULT_Z_CAP


def z_floor_of(provider: str) -> int:
    """数据源的最低级别。DEM 从 0 起,影像从 1 起(z0 盖全球,对影像无意义)。"""
    from ..providers.terrain import is_dem_provider
    return 0 if is_dem_provider(provider) else 1


#: provider key -> DataKind
PROVIDER_KIND: dict[str, str] = {
    # 天地图影像/底图(EPSG:4326 经纬度瓦片)
    "tianditu_img": DataKind.RASTER_IMAGE,
    "tianditu_vec": DataKind.RASTER_IMAGE,
    "tianditu_ter": DataKind.RASTER_IMAGE,
    # Google 影像 4 图层与 Esri World Imagery(EPSG:3857 墨卡托 XYZ)。
    # kind 与天地图同为 RASTER_IMAGE —— 这是有意的:网格差异由 PROVIDER_GRID
    # 表达,kind 只管"数据是什么"。因此新数据源自动获得 geotiff/tms/osm
    # 三个阶段,且 worker 的 _resolve_runner 会自然路由到 runner.run_task,
    # 无需改动进程隔离层。
    "google_img": DataKind.RASTER_IMAGE,
    "google_hybrid": DataKind.RASTER_IMAGE,
    "google_road": DataKind.RASTER_IMAGE,
    "google_terrain": DataKind.RASTER_IMAGE,
    "esri_imagery": DataKind.RASTER_IMAGE,
    # 高程(Esri Terrain3D,LERC 编码,EPSG:3857)
    "esri_terrain": DataKind.RASTER_DEM,
    # 早期改用 Esri 前的 key,库里仍有 1 条存量任务(2026-07 实测),
    # 保留登记以免读旧任务时被回落成影像栅格
    "aws_terrain": DataKind.RASTER_DEM,
    # 建筑轮廓(矢量要素集,无瓦片行列号)
    "osm_buildings": DataKind.VECTOR_POLYGON,
    "overture_buildings": DataKind.VECTOR_POLYGON,
    "local_vector": DataKind.VECTOR_POLYGON,
    # 本地栅格文件作输入源(不下载,直接读用户机器上的文件)。
    # 影像与 DEM 分成两个 key 而不是一个 "local_raster" + 动态 kind:
    # kind_of() 是静态映射,build_stage_defs / validate / stages_for 全都依赖它,
    # 若 kind 要从任务字段动态取,这些函数都得改签名并处处传参。文件类型在导入时
    # 已判定好(见 core/local_raster.guess_kind),分成两个 key 即可复用整套逻辑。
    "local_image": DataKind.RASTER_IMAGE,
    "local_dem": DataKind.RASTER_DEM,
    # 本地三维数据(osgb 目录/点云文件),同理静态登记即可复用整套推导
    "local_osgb": DataKind.MESH_OSGB,
    "local_pointcloud": DataKind.POINT_CLOUD,
}

#: 本地文件输入源:无瓦片缓存、无下载阶段
LOCAL_PROVIDERS = frozenset({"local_image", "local_dem",
                             "local_osgb", "local_pointcloud"})


def is_local_source(provider: str) -> bool:
    """是否为"读本地文件"的数据源(与"联网下载"相对)。"""
    return provider in LOCAL_PROVIDERS


def kind_of(provider: str) -> str:
    """取数据源的数据类型。未登记的 provider 回落影像栅格(与旧行为一致:
    旧代码里 is_dem/is_building 都为假时走影像分支)。"""
    if provider == "img":          # 兼容早期落库的短 key
        provider = "tianditu_img"
    return PROVIDER_KIND.get(provider, DataKind.RASTER_IMAGE)


#: 走三维管线(OSGB / 点云)的 provider:没有瓦片行列号,必须走 runner_3d。
#: 集中定义,避免在 worker / queue 各自硬编码一份而漂移(漂移后果是静默错路由)。
def is_3d_provider(provider: str) -> bool:
    """该数据源是否走三维管线(runner_3d)。"""
    return kind_of(provider) in (DataKind.MESH_OSGB, DataKind.POINT_CLOUD)


def default_containers(kind: str) -> dict[str, str]:
    """每个阶段的默认容器(界面初始值与旧行为对齐:栅格默认 GTiff、矢量默认 GeoJSON)。"""
    out = {}
    for s in stages_for(kind):
        if not s.containers:
            continue
        out[s.key] = s.containers[0]
    return out


def resolve_outputs(stage_key: str, container_key: str, name: str,
                    levels: list[int] | None = None,
                    include_all_forms: bool = False) -> list[str]:
    """把阶段+容器解析成具体产出物相对路径(供清理旧产出与写 metadata 用)。

    瓦片阶段特殊:容器改变的是产出**结构**(目录 vs 单文件),而非仅扩展名,
    且两种形态可以并存(keep_tiles_dir)。故:
      - container=mbtiles → 返回 `{name}_{stage}.mbtiles`
      - include_all_forms=True → 目录与 mbtiles 都返回(清理旧产出时必须两者都清,
        否则切换容器重跑会留下上一次的另一种形态,成果目录里两份数据打架)
    """
    stage = STAGES.get(stage_key)
    if not stage:
        return []
    is_tiles = stage.produces == DataKind.TILES_RASTER and bool(stage.containers)
    if is_tiles:
        dir_form = [tpl.format(name=name, ext="") for tpl in stage.outputs]
        mb_form = [f"{name}_{stage_key}.mbtiles"]
        if include_all_forms:
            return dir_form + mb_form
        return mb_form if container_key == "mbtiles" else dir_form

    ext = CONTAINERS[container_key].ext if container_key in CONTAINERS else ""
    paths: list[str] = []
    for tpl in stage.outputs:
        if stage.needs_levels and "{z}" in tpl:
            for z in (levels or []):
                paths.append(tpl.format(name=name, z=z, ext=ext))
        else:
            paths.append(tpl.format(name=name, ext=ext))
    return paths


# ---------- 校验 ----------

def validate(provider: str, stage_keys: list[str],
             containers: dict[str, str] | None = None) -> list[str]:
    """校验"这个数据源能不能出这些格式",返回错误列表(空表示通过)。

    取代 api/tasks.py 里按 provider 逐条 if 的校验。
    """
    kind = kind_of(provider)
    # include_internal:落库的 stages 里含管线自动插入的内部阶段(base_dem/
    # build_mesh),校验旧任务时必须放行,否则 13 条存量建筑任务会被误判非法。
    allowed = {s.key for s in stages_for(kind, include_internal=True)}
    errors: list[str] = []
    for key in stage_keys:
        stage = STAGES.get(key)
        if stage is None:
            errors.append(f"未知的导出格式:{key}")
            continue
        if key not in allowed:
            errors.append(f"{STAGES[key].label} 不支持该数据源")
            continue
        cont = (containers or {}).get(key)
        if cont and cont not in stage.containers:
            errors.append(f"{stage.label} 不支持容器格式 {cont}")
    return errors


# ---------- ROADMAP:接入顺序(每步独立可验,不要一次全改)----------
#
# 1. 本模块纯新增,不改动任何现有行为。先补一个自检脚本确认表本身自洽
#    (容器 kinds 与阶段 containers 对得上、阶段 key 与 STAGE_LABELS 一致)。
# 2. models.build_stage_defs 改为读 STAGES(保持返回结构不变,旧库 stages 能继续读)。
#    此步不加新格式,只换实现,便于对比行为是否一致。
# 3. runner 的 executors 改为按 kind 组装;_clear_stage_output 改用 resolve_outputs。
# 4. 逐个接新格式:cog → mbtiles → DEM 出瓦片 → 矢量三容器 → contour。
#    每接一个都要过一次 PyInstaller 打包验证(pyogrio 带第二套 GDAL,DLL 收集
#    未在打包环境验证过)。
# 5. 前端 ParamsPanel 三 tab 的硬编码格式列表改为读后端接口(暴露 stages_for/
#    containers_for 的 JSON)。



