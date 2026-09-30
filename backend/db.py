"""SQLite 连接与任务表初始化。

单机自用场景下用标准库 sqlite3 即可,不引入 ORM。
所有写操作通过 get_conn() 拿到带 Row 工厂的连接。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import settings

DB_PATH = settings.abs_path("./data/app.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    provider    TEXT NOT NULL,
    bbox        TEXT NOT NULL,          -- json: [west,south,east,north]
    z_min       INTEGER NOT NULL,
    z_max       INTEGER NOT NULL,
    export      TEXT NOT NULL DEFAULT 'geotiff',
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending/running/done/failed/canceled
    total       INTEGER NOT NULL DEFAULT 0,
    downloaded  INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    message     TEXT DEFAULT '',
    output_path TEXT DEFAULT '',
    geometry    TEXT DEFAULT '',        -- json: geojson 几何(矢量/多边形任务),空=矩形
    clip        INTEGER DEFAULT 0,      -- 是否裁剪到 geometry 边界
    crs         TEXT DEFAULT 'EPSG:4326', -- GeoTIFF 输出坐标系
    levels      TEXT DEFAULT '',        -- json: 选中的级别数组;空=按 z_min..z_max 连续
    use_cache   INTEGER DEFAULT 1,      -- 是否复用瓦片缓存(0=强制重新下载原始瓦片)
    annotate    INTEGER DEFAULT 0,      -- 是否叠加路网注记(同步下载注记图层并烘焙进成果)
    global_max_level INTEGER DEFAULT 0, -- 全球底图铺到第几层;0=不启用(设计 2026-09-30)
    buffer_rings     INTEGER DEFAULT 1, -- 每层范围外外扩圈数
    hillshade   TEXT DEFAULT '',        -- json: DEM 晕渲参数 {azimuth,altitude,z_factor}
    stages      TEXT DEFAULT '',        -- json: 阶段化进度数组(下载/合并/切片各阶段独立跟踪)
    est_bytes   INTEGER DEFAULT 0,      -- 预估原始瓦片下载量(字节,提交时按经验单瓦片大小算)
    base_height_mode TEXT DEFAULT 'terrain',  -- 三维建筑底面高模式:terrain/flat/offset
    height_offset    REAL DEFAULT 0.0,        -- 底面高附加偏移(米);offset 模式下即底面高
    default_height   REAL DEFAULT 6.0,        -- 高度全缺失时的兜底建筑高(米)
    max_per_tile     INTEGER DEFAULT 2000,    -- 单个 b3dm 瓦片建筑数上限(超过则四叉树细分)
    building_count   INTEGER DEFAULT 0,       -- 实际参与建模的建筑栋数(清洗后)
    -- 本地矢量面上传(local_vector 数据源)的字段映射
    upload_id        TEXT DEFAULT '',         -- data/uploads/<id>.geojson
    height_field     TEXT DEFAULT '',         -- 取高度的属性字段名
    height_mode      TEXT DEFAULT 'none',     -- meters(米)/floors(层数)/none
    height_scale     REAL DEFAULT 1.0,        -- 字段值换算系数(如厘米→米填 0.01)
    floor_height     REAL DEFAULT 3.0,        -- floors 模式的单层层高(米)
    name_field       TEXT DEFAULT '',         -- 作为建筑名的字段
    keep_fields      TEXT DEFAULT '',         -- json: 写入 b3dm Batch Table 的字段名数组
    dem_upload_id    TEXT DEFAULT '',         -- 上传的地形 GeoTIFF id(优先用于底面高采样)
    -- 各阶段的容器格式选择 json: {阶段key: 容器key},如 {"geotiff":"cog","contour":"gpkg"}
    -- 缺省时按 core.formats 里该阶段 containers 的首项(与旧行为一致:栅格 GTiff、矢量 GeoJSON)
    containers       TEXT DEFAULT '',
    contour_interval REAL DEFAULT 50.0,       -- 等高距(米)
    -- 打包 MBTiles 后是否同时保留散列瓦片目录(1=保留,默认)。
    -- 目录适合挂 HTTP 服务、mbtiles 适合分发,两者内容等价但用途不同;
    -- 默认保留是为了不静默删数据,代价是瓦片存两遍、磁盘翻倍。
    keep_tiles_dir   INTEGER DEFAULT 1,
    -- 本地文件输入源的绝对路径(provider 为 local_image / local_dem 时有效)。
    -- 存路径而非拷贝文件:本机自用,后端能直接读原文件,复制一份纯属浪费
    -- (一份 2GB 影像会让 data/ 再占 2GB)。代价是原文件被移动/删除后任务无法重跑,
    -- runner 启动时会校验存在性并给出明确报错。
    source_path      TEXT DEFAULT '',
    -- 本地影像 tif 出 TMS 时的断层补齐策略:
    -- contiguous=只用连续高层级兜底;preserve_inputs=保留每个输入层级并分段补齐。
    tms_source_strategy TEXT DEFAULT 'contiguous',
    -- 点云任务的 CRS 处理策略:''=自动读 LAS 头;'local'=按本地坐标;否则为 EPSG 码(如 EPSG:4547)。
    pc_crs       TEXT NOT NULL DEFAULT '',
    -- 点云出 DEM/DSM 的栅格分辨率(米),0=按点云密度自动估算。
    pc_resolution REAL NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token         TEXT NOT NULL UNIQUE,        -- 天地图密钥明文
    label         TEXT DEFAULT '',             -- 备注名(便于识别)
    order_index   INTEGER NOT NULL DEFAULT 0,  -- 使用顺序(升序轮询)
    request_count INTEGER NOT NULL DEFAULT 0,  -- 当日已用请求数
    count_date    TEXT DEFAULT '',             -- 计数所属自然日(YYYY-MM-DD),跨天还原
    max_requests  INTEGER NOT NULL DEFAULT 1500000,  -- 单 tk 每日请求上限
    enabled       INTEGER NOT NULL DEFAULT 1,  -- 是否启用(停用则跳过)
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- 本地数据服务:把本地目录发布成带稳定地址的数据服务。
-- 与 tasks 表的区别:服务指向**目录**,任务产出**成果**;任务记录删了成果就打不开,
-- 而服务长期有效,其他服务可以拿它的地址当地图数据源用。
CREATE TABLE IF NOT EXISTS services (
    id            TEXT PRIMARY KEY,            -- 8 位十六进制短 id,用作 URL 段
    name          TEXT NOT NULL,               -- 显示名(可改)
    kind          TEXT NOT NULL,               -- model / imagery / vector / terrain
    root          TEXT NOT NULL,               -- 服务根目录绝对路径(已 resolve)
    entry         TEXT NOT NULL DEFAULT '',    -- 相对 root 的入口;瓦片/地形为空
    grid          TEXT NOT NULL DEFAULT '',    -- geodetic / mercator / ''(非瓦片)
    flip_y        INTEGER NOT NULL DEFAULT 0,  -- 行号是否自南向北(TMS 为 1)
    minzoom       INTEGER NOT NULL DEFAULT 0,
    maxzoom       INTEGER NOT NULL DEFAULT 18,
    bounds_wgs84  TEXT NOT NULL DEFAULT '',    -- JSON 数组 [w,s,e,n],供定位
    bounds_approx INTEGER NOT NULL DEFAULT 0,  -- 范围是否为估算值
    tile_ext      TEXT NOT NULL DEFAULT 'png', -- 瓦片扩展名(拼访问地址用)
    enabled       INTEGER NOT NULL DEFAULT 0,  -- 是否对外提供;**默认关**
    source        TEXT NOT NULL DEFAULT 'manual',  -- output_scan / manual
    created_at    TEXT NOT NULL
);
"""

# 旧库升级:新增列(SQLite 不支持 IF NOT EXISTS 加列,靠 PRAGMA 判断)
_MIGRATIONS = {
    "geometry": "ALTER TABLE tasks ADD COLUMN geometry TEXT DEFAULT ''",
    "clip": "ALTER TABLE tasks ADD COLUMN clip INTEGER DEFAULT 0",
    "crs": "ALTER TABLE tasks ADD COLUMN crs TEXT DEFAULT 'EPSG:4326'",
    "levels": "ALTER TABLE tasks ADD COLUMN levels TEXT DEFAULT ''",
    "use_cache": "ALTER TABLE tasks ADD COLUMN use_cache INTEGER DEFAULT 1",
    "annotate": "ALTER TABLE tasks ADD COLUMN annotate INTEGER DEFAULT 0",
    "global_max_level": "ALTER TABLE tasks ADD COLUMN global_max_level INTEGER DEFAULT 0",
    "buffer_rings": "ALTER TABLE tasks ADD COLUMN buffer_rings INTEGER DEFAULT 1",
    "hillshade": "ALTER TABLE tasks ADD COLUMN hillshade TEXT DEFAULT ''",
    "stages": "ALTER TABLE tasks ADD COLUMN stages TEXT DEFAULT ''",
    "est_bytes": "ALTER TABLE tasks ADD COLUMN est_bytes INTEGER DEFAULT 0",
    # 三维建筑(Overture → b3dm)管线新增字段
    "base_height_mode": "ALTER TABLE tasks ADD COLUMN base_height_mode TEXT DEFAULT 'terrain'",
    "height_offset": "ALTER TABLE tasks ADD COLUMN height_offset REAL DEFAULT 0.0",
    "default_height": "ALTER TABLE tasks ADD COLUMN default_height REAL DEFAULT 6.0",
    "max_per_tile": "ALTER TABLE tasks ADD COLUMN max_per_tile INTEGER DEFAULT 2000",
    "building_count": "ALTER TABLE tasks ADD COLUMN building_count INTEGER DEFAULT 0",
    # 本地矢量面上传的字段映射
    "upload_id": "ALTER TABLE tasks ADD COLUMN upload_id TEXT DEFAULT ''",
    "height_field": "ALTER TABLE tasks ADD COLUMN height_field TEXT DEFAULT ''",
    "height_mode": "ALTER TABLE tasks ADD COLUMN height_mode TEXT DEFAULT 'none'",
    "height_scale": "ALTER TABLE tasks ADD COLUMN height_scale REAL DEFAULT 1.0",
    "floor_height": "ALTER TABLE tasks ADD COLUMN floor_height REAL DEFAULT 3.0",
    "name_field": "ALTER TABLE tasks ADD COLUMN name_field TEXT DEFAULT ''",
    "keep_fields": "ALTER TABLE tasks ADD COLUMN keep_fields TEXT DEFAULT ''",
    "dem_upload_id": "ALTER TABLE tasks ADD COLUMN dem_upload_id TEXT DEFAULT ''",
    # 导出容器格式选择(COG/GPKG/Shapefile/MBTiles 等,见 core.formats.CONTAINERS)
    "containers": "ALTER TABLE tasks ADD COLUMN containers TEXT DEFAULT ''",
    "contour_interval": "ALTER TABLE tasks ADD COLUMN contour_interval REAL DEFAULT 50.0",
    "keep_tiles_dir": "ALTER TABLE tasks ADD COLUMN keep_tiles_dir INTEGER DEFAULT 1",
    # 本地文件输入源(不下载,直接读用户磁盘上的文件)
    "source_path": "ALTER TABLE tasks ADD COLUMN source_path TEXT DEFAULT ''",
    "tms_source_strategy": "ALTER TABLE tasks ADD COLUMN tms_source_strategy TEXT DEFAULT 'contiguous'",
    # 点云任务:CRS 处理策略(''=自动读 LAS 头;'local'=本地坐标;否则为 EPSG 码)
    "pc_crs": "ALTER TABLE tasks ADD COLUMN pc_crs TEXT NOT NULL DEFAULT ''",
    # 点云出 DEM/DSM 的栅格分辨率(米),0=自动
    "pc_resolution": "ALTER TABLE tasks ADD COLUMN pc_resolution REAL NOT NULL DEFAULT 0",
}


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        # 兼容旧库:补齐缺失列
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        for col, ddl in _MIGRATIONS.items():
            if col not in cols:
                conn.execute(ddl)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    # 多进程(主进程 + N 个 worker)并发写时,WAL 允许并发读写但写之间仍互斥;
    # 不设超时会直接抛 "database is locked",设了则排队等待。
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn
