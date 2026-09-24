# Google / Esri 影像数据源 — 设计文档

> 日期:2026-09-24
> 范围:新增 Google 影像(非官方瓦片端点)与 Esri World Imagery 两个墨卡托影像数据源,支持下载、拼接与四种格式导出,并支持作为工具内底图预览。
> 前置:本设计基于 2026-09-24 的实测结论(见 §3),非实测的部分已明确标注。

## 1. 问题与目标

### 现状

本工具的影像下载只支持**天地图 EPSG:4326** 网格(`TileMatrixSet=c`):

- 坐标换算集中在 `core/tiling.py`,面向 4326 经纬度瓦片:第 z 级列数 `2^z`、行数 `2^(z-1)`。
- 拼接 `core/mosaic.py::mosaic_to_geotiff` 硬编码 `crs="EPSG:4326"` 并调用 `tr.mosaic_bounds()`(4326 专用)。
- TMS 导出 `core/tms.py` 是"天地图 4326 → gdal2tiles geodetic"的无损直映射。
- OSM/XYZ 导出 `core/osm.py` 做的是"4326 源 → 3857 重采样"。

项目已有第二套 3857 网格的完整先例(DEM 管线):`core/dem_tiling.py` 提供 `lonlat_to_xyz` / `mercator_range_for_bbox` / `mosaic_bounds_3857`,`core/dem.py::mosaic_dem_geotiff` 提供 3857 拼接。但 DEM 是单波段 LERC 高程,与 RGB 影像不能共用拼接实现。

### 目标

1. 新增 **Google 影像**数据源,四图层(`s` 卫星影像 / `y` 影像+路网 / `m` 路线图 / `p` 地形),级别开放到 z21。
2. 新增 **Esri World Imagery** 数据源,级别到 z18。
3. 两个源均支持导出 **GeoTIFF / OSM-XYZ / MBTiles / TMS geodetic** 四种格式。
4. 两个源均可作为工具内**底图预览**。
5. 两者均为 **EPSG:3857 Web 墨卡托**网格 —— 与现有天地图 4326 网格不同构,需要一条新的影像处理路径。

### 非目标(本次不做)

- **补齐金字塔**(从最高级降采样补出低缩放级别)。用户明确选择"只出已下载级别":下载了哪些级别就导出哪些级别。这意味着 OSM/XYZ、MBTiles、TMS 的瓦片包在低缩放级别是空的,分发给第三方 GIS 软件时低级别会显示空白 —— 这是已知且接受的取舍。
- 官方 Google Map Tiles API 途径(需 GCP key + session,用户明确不走)。
- 影像拼接缺陷修复(见 §3.6,属数据源固有问题)。

## 2. 核心设计决策

这些决策贯穿全文,先集中列出。

| # | 决策 | 理由 |
|---|---|---|
| D1 | **引入"网格"维度,沿用项目既有术语 `geodetic` / `mercator`** | 项目服务层已用这两个词(`core/service_scan.py:59`、`core/service_bounds.py:78`),不造新词。坐标系不是"数据语义"而是"网格约定",故不新开 `DataKind`(那要改 `DataKind.ALL` 及所有 `(RASTER_IMAGE,)` 元组,侵入面大)。 |
| D2 | **抽出 `core/mercator_tiling.py` 作为 3857 网格数学的唯一实现** | 现状 `dem_tiling.py:22-42` 与 `osm.py:54-66` 已重复实现同一套 XYZ 换算。不抽出就要写第三份。这是针对性改进,直接决定本次代码放在哪。 |
| D3 | **`mosaic_to_geotiff` 加 `crs` 参数而非复制新函数** | 该函数除 `crs` 与 bounds 来源外与网格无关,参数化约 4 行,默认值保持 `EPSG:4326` 完全向后兼容。 |
| D4 | **两个源共用一套 3857 管线** | 实测证实 Google 与 Esri 网格完全同构(同 z/x/y 像素偏移 0-1px),差别仅在 URL 与最大级别。 |
| D5 | **代理是这两个源的共同前提,不是 Google 专属** | 实测:Esri World Imagery **直连同样不通**,只有项目现用的 Esri Terrain3D DEM 直连可用。 |
| D6 | **预览走后端瓦片转发,且不写缓存** | 浏览器无法使用后端代理配置。预览与下载用途不同(预览要即时、可失败;下载要完整、可续传),混用缓存会让下载进度估算失真。 |
| D7 | **端点 URL 必须可配置,不硬编码** | 非官方端点会变更(实测 `khms` 端点 `v=1000` 已返回 404)。硬编码等于埋雷。 |
| D8 | **级别上限:Google z21,Esri z18,天地图 z18(现状不变)** | 用户指定。实测 Esri z19 仍有数据,封顶 18 是保守安全的取舍;日后放开只需改一处常量。 |
| D9 | **`google_road`(lyrs=m)的 `bands` 登记为 1** | 见 §3.4 发现 1。 |
| D10 | **配置不设 `key` 字段** | 见 §3.5,端点忽略该参数。 |

## 3. 实测结论(本设计的事实基础)

以下均为 2026-09-24 在本机(经 `127.0.0.1:6789` 代理)实测所得。

### 3.1 连通性

| 数据源 | 直连 | 走代理 |
|---|---|---|
| `mt1.google.com/vt/lyrs=s` | ❌ 超时(15s) | ✅ HTTP 200 |
| Esri `World_Imagery` | ❌ 不通 | ✅ HTTP 200 |
| Esri `clarity.maptiles.arcgis.com` | ❌ 不通 | ✅ HTTP 200 |
| Esri `Terrain3D`(项目现用 DEM) | ✅ **通**(2.1s) | ✅ 通 |

结论:**两个新源都需要代理**;项目现有 DEM 源不受影响。

### 3.2 坐标系 —— 两者完全同构

用 FFT 互相关在 165 张瓦片(15×11,z16)分 9 窗口测量 Google 与 Esri World Imagery 的像素偏移:

| 窗口 | 左上 | 中上 | 右上 | 左中 | 正中 | 右中 | 左下 | 中下 | 右下 |
|---|---|---|---|---|---|---|---|---|---|
| 偏移(px) | 0,+2 | -1,+1 | -1,+2 | 0,+2 | +1,+1 | 0,+1 | 0,+2 | 0,+1 | 0,+1 |
| 距离(m) | 4 | 2 | 4 | 4 | 2 | 2 | 4 | 2 | 2 |

9 窗口全部 ≤4m 且**不随位置变化**。若存在 GCJ-02 偏移(境内 300~600m),此处应显示约 200px 的位移。**结论:Google 影像无 GCJ-02 偏移,且与 Esri 网格同构。**

### 3.3 分辨率上限(实测,北京 116.386, 40.000)

梯度 = 相邻像素差均值,越高细节越多。

| 级别 | Google | Esri World Imagery | 判读 |
|---|---|---|---|
| z17 | 17.66 | 8.22 | Google 细节 2.1× |
| z18 | 18.10 | 10.60 | Google 细节 1.7× |
| z19 | 11.57 | 12.18 | 相当(Esri 上限) |
| z20 | 7.05 | 0.56 | **Esri 返回 "Map data not yet available" 占位图** |
| z21 | 5.54 | 0.56 | 同上 |

### 3.4 必须编码进实现的两个实测发现

**发现 1:`lyrs=m`(路线图)返回单波段调色板 PNG,不是 3 波段。**

实测 `https://mt1.google.com/vt/lyrs=m&...` 返回的栅格 `count == 1` 且带 colormap。若按 3 波段处理会抛 `DatasetIOShapeError`。项目 `core/mosaic.py::_read_tile` 已有调色板展开逻辑(`is_palette` 检测 + 色表 LUT),可直接复用,但 `bands` 必须正确登记为 1。

**发现 2:Esri z20+ 返回 HTTP 200 + "Map data not yet available" 占位图。**

实测 z20/z21 返回固定 2521 字节的灰底带字图,均值 204.7,并非 404。若不识别,占位图会被当作有效数据写入缓存,污染断点续传并拼接出错误成果。项目已有 `TileProvider.is_empty_tile()` 机制(Esri Terrain3D 就是先例,见 `providers/terrain.py:67`),需为 Esri 影像实现。

> 但本设计将 Esri 上限设为 z18(D8),故该占位图在正常使用中不会出现。仍需实现 `is_empty_tile()` 作为防御 —— 服务端行为可能变化,且 `max_zoom` 若日后放开即刻需要。

### 3.5 端点对 `key` 参数的反应

| 请求 | HTTP | 大小 |
|---|---|---|
| 无 key(基准) | 200 | 18084B |
| 带 dummy key | 200 | **18084B**(字节相同) |
| 带 `key=`(空) | 200 | **18084B**(字节相同) |
| `khms1.google.com/kh/v=1000/...` | **404** | — |

结论:非官方 `mt.google.com/vt` 端点**完全忽略 `key` 参数** —— 带/不带/空 key 返回字节完全相同的瓦片。因此配置中**不设 `key` 字段**,避免造成"填了 key 才有权限"的误解。`khms` 端点因 `v=` 版本号已失效,不采用。

### 3.6 风险(实测发现,需在成果说明中提示)

**Google 影像存在拼接接缝与纹理重复。** 在北京北四环一带(z16,经度 116.367~116.406 / 纬度 39.994~40.007)观察到:规律重复的建筑纹理、多条亮度突变的直线接缝。同一区域的 Esri 影像无此现象。

这是 **Google 数据源自身的数据质量问题,不是本工具代码缺陷**(坐标已实证正确,偏移 ≤4m)。建议在成果说明与界面提示中注明,并说明 Esri 源可作为替代。

## 4. 后端设计

### 4.1 模块结构

```
core/tiling.py            (已有,不动) 4326 网格数学      ← 天地图
core/mercator_tiling.py   (新增)       3857 网格数学      ← DEM + Google + Esri
core/dem_tiling.py        (改)  保留 DEM 专有的建议级别逻辑,引用 mercator_tiling
core/osm.py               (改)  引用 mercator_tiling,删除本地重复实现
core/mosaic.py            (改)  mosaic_to_geotiff 加 crs 参数
providers/google.py       (新增)
providers/esri_imagery.py (新增)
api/tiles.py              (新增) 预览瓦片转发
```

`core/mercator_tiling.py` 从 `core/dem_tiling.py` 抽出以下纯数学函数(保持签名不变,`dem_tiling` 改为 re-export 以免破坏现有调用):

- `lonlat_to_xyz(lon, lat, z) -> (x, y)`
- `tile_bounds_3857(x, y, z) -> (minx, miny, maxx, maxy)`
- `mercator_range_for_bbox(west, south, east, north, z) -> TileRange`
- `mosaic_bounds_3857(tr) -> (minx, miny, maxx, maxy)`
- `estimate_dem_tiles` → 重命名为 `estimate_mercator_tiles`(保留旧名作别名)

常量 `TILE_SIZE` / `MERC_MAX` / `LAT_LIMIT` 一并迁移。

**回归护栏**:`osm.py:54-66` 的 `lonlat_to_xyz` / `tile_bounds_3857` 与之实现相同,改为 import 后需保证 `test_*` 全绿。

### 4.2 数据源登记

`core/formats.py` 新增与 `PROVIDER_KIND` 并列的网格表:

```python
#: 网格类型
GEO_GEODETIC = "geodetic"   # EPSG:4326 经纬度瓦片(天地图 TileMatrixSet=c)
GEO_MERCATOR = "mercator"   # EPSG:3857 Web 墨卡托 XYZ

#: provider key -> 网格类型。未登记者回落 geodetic(与旧行为一致)
PROVIDER_GRID: dict[str, str] = {
    "tianditu_img": GEO_GEODETIC,
    "tianditu_vec": GEO_GEODETIC,
    "tianditu_ter": GEO_GEODETIC,
    "google_img": GEO_MERCATOR,
    "google_hybrid": GEO_MERCATOR,
    "google_road": GEO_MERCATOR,
    "google_terrain": GEO_MERCATOR,
    "esri_imagery": GEO_MERCATOR,
}

def grid_of(provider: str) -> str:
    """取数据源的网格类型。提交前即可调用,无需构造 provider。"""
    return PROVIDER_GRID.get(provider, GEO_GEODETIC)
```

`PROVIDER_KIND` 新增 5 行,全部为 `DataKind.RASTER_IMAGE`。

### 4.3 数据源实现

#### `providers/google.py`

| key | lyrs | 中文名 | ext | bands | zmin | zmax |
|---|---|---|---|---|---|---|
| `google_img` | `s` | Google 卫星影像 | jpg | 3 | 1 | 21 |
| `google_hybrid` | `y` | Google 影像(含路网) | jpg | 3 | 1 | 21 |
| `google_road` | `m` | Google 路线图 | png | **1** | 1 | 21 |
| `google_terrain` | `p` | Google 地形 | jpg | 3 | 1 | 21 |

```python
class GoogleProvider(TileProvider):
    def __init__(self, key: str, cfg: GoogleConfig):
        # 校验 key 在 GOOGLE_LAYERS 中;校验 cfg.url_template 非空
        ...
    @property
    def proxy(self) -> str | None:
        return self.cfg.proxy or None
    def tile_url(self, col, row, z) -> str:
        sub = next(self._sub)   # 子域名轮询,复用 tianditu 的 itertools.cycle 模式
        return self.cfg.url_template.format(s=sub, lyrs=self.lyrs, x=col, y=row, z=z)
    def max_zoom(self) -> int: return self.cfg.max_zoom   # 默认 21
    def is_empty_tile(self, data: bytes) -> bool:
        return False    # 实测 Google 无占位瓦片行为
```

#### `providers/esri_imagery.py`

| key | 中文名 | ext | bands | zmin | zmax |
|---|---|---|---|---|---|
| `esri_imagery` | Esri World Imagery | jpg | 3 | 1 | 18 |

```python
def is_empty_tile(self, data: bytes) -> bool:
    """Esri 超出可用级别返回 HTTP 200 + 约 2521 字节的 'Map data not yet available' 占位图。"""
    # 判定依据:体量极小 且 图像高度均匀(实测均值 204.7、唯一色值数 ≤ 80)
    # 保守实现:仅当体量 < 4KB 时才进一步判图,避免误伤真实的小瓦片
```

`tile_url` 用 `cfg.url_template.format(z=z, y=row, x=col)` —— 注意 ArcGIS 顺序是 `{z}/{y}/{x}`。

### 4.4 下载器改动

`providers/base.py::TileProvider` 新增属性(与既有 `headers` 设计对称):

```python
@property
def proxy(self) -> str | None:
    """下载该数据源瓦片时使用的 HTTP 代理(默认无;子类可覆盖)。"""
    return None
```

`core/downloader.py:70` 的 `ClientSession` 加 `proxy`:

```python
proxy = getattr(self.provider, "proxy", None)
async with aiohttp.ClientSession(headers=headers, timeout=timeout, proxy=proxy) as session:
```

> `getattr` 带默认值是为兼容 `CachedBuildingSource` 这类装饰器与测试替身。

### 4.5 拼接改动

`core/mosaic.py::mosaic_to_geotiff` 加 `crs` 参数:

```python
def mosaic_to_geotiff(provider, cache_dir, tr, out_path, tile_path_fn,
                      anno_tile_path_fn=None, on_row=None,
                      crs: str = "EPSG:4326") -> Path:
    ...
    if crs == "EPSG:3857":
        from .mercator_tiling import mosaic_bounds_3857
        west, south, east, north = mosaic_bounds_3857(tr)
    else:
        west, south, east, north = tr.mosaic_bounds()
    transform = from_bounds(west, south, east, north, width, height)
    profile = {..., "crs": crs, ...}
```

默认值 `EPSG:4326` 保证现有天地图调用**行为完全不变**。

### 4.6 执行管线分流

`core/runner.py:95-105` 的 provider 构造分支扩展:

```python
if is_dem_provider(task["provider"]):
    provider = build_terrain_provider(task["provider"])
elif is_google_provider(task["provider"]):
    provider = build_google_provider(task["provider"], settings.google)
elif is_esri_imagery_provider(task["provider"]):
    provider = build_esri_imagery_provider(settings.esri)
else:
    provider = build_provider(task["provider"], token_src)
```

影像阶段按网格分流(取代目前硬编码的 4326 调用):

| 阶段 | geodetic(现状) | mercator(新增) |
|---|---|---|
| 瓦片区间 | `tiling.range_for_bbox` | `mercator_tiling.mercator_range_for_bbox` |
| 拼接 | `mosaic_to_geotiff(...)` | `mosaic_to_geotiff(..., crs="EPSG:3857")` |
| TMS 导出 | `export_tms`(无损直映射) | `export_tms_from_source`(重采样,因 TMS 是 4326) |
| OSM 导出 | `export_osm`(4326→3857 重采样) | `export_osm`(网格同构,**保留 3857 快路径**) |
| MBTiles | `pack_mbtiles(scheme="tms")` | `pack_mbtiles(scheme="xyz")` |
| 估算/建议级别 | `tiling.estimate_levels` / `suggest_levels` | `mercator_tiling.*` 对应实现 |

> **OSM 导出的快路径**:mercator 源的拼接图本身就是 3857,与 XYZ 瓦片网格完全同构,理论上零重采样。但 `osm.py::export_osm` 当前以 WarpedVRT 做 4326→3857 重投影。为控制本次改动面,**一期沿用现有 `export_osm`**(重投影 3857→3857 是恒等变换,GDAL 会优化,结果正确);若实测性能不可接受,二期再加直映射快路径。

### 4.7 预览端点

`api/tiles.py`:

```
GET /api/tiles/{provider}/{z}/{x}/{y}
```

- 仅接受已登记且 `enabled: true` 的 provider key,防止变成任意 URL 代理(SSRF 风险)。
- 用 `aiohttp` 带 provider 的 `proxy` 取瓦片,流式回传并透传 `Content-Type`。
- **不写缓存**(D6)。
- 失败时返回 1×1 透明 PNG 占位,避免地图破图。
- 复用 `providers/` 的构造逻辑,保证预览与下载的端点配置单一来源。

### 4.8 配置

`config.py` 新增两个 dataclass:

```python
@dataclass
class GoogleConfig:
    enabled: bool = False
    proxy: str = ""            # 如 "127.0.0.1:6789";留空=直连
    url_template: str = "https://mt{s}.google.com/vt/lyrs={lyrs}&x={x}&y={y}&z={z}"
    subdomains: str = "0,1,2,3"
    max_zoom: int = 21

@dataclass
class EsriImageryConfig:
    enabled: bool = False
    proxy: str = ""
    url_template: str = ("https://services.arcgisonline.com/ArcGIS/rest/services"
                         "/World_Imagery/MapServer/tile/{z}/{y}/{x}")
    max_zoom: int = 18
```

`config.example.yaml` 同步补充,并在注释中写明:
- **两个源都需要代理**(实测直连均不通)。
- **不设 `key` 字段**:非官方端点忽略 key 参数。
- `url_template` 会变,失效应先改这里。

环境变量覆盖(沿用项目的 `TIANDITU_TOKEN` 模式):`GOOGLE_PROXY`、`ESRI_PROXY`。

### 4.9 错误处理

| 场景 | 处理 |
|---|---|
| 代理未配或不通 | 提交任务前预检(对齐 `/api/tools/diagnose` 的做法):`enabled` 为真但 `proxy` 为空且直连失败 → 明确报「Google/Esri 需要配置代理」,而非进入下载后静默全失败 |
| 端点失效(404/403) | 瓦片计入失败并重试;失败率超过阈值时任务判失败,提示「端点可能已变更,请检查 `url_template`」 |
| Esri 返回占位瓦片 | `is_empty_tile()` 识别后不写缓存,计为请求成功(该处确实无数据) |
| 预览时代理不通 | 返回透明占位图,不破坏地图渲染 |
| Google 拼接缺陷 | 数据源固有,成果说明中提示,引导用户改用 Esri 源(见 §3.6) |

## 5. 前端设计

### 5.1 数据源下拉(3 处)

| 文件 | 位置 | 改动 |
|---|---|---|
| `components/ProcessDialog.vue` | `:46` `providerOptions` | 加 5 项,归入「影像」分组 |
| `components/RedownloadDialog.vue` | `:24` `providerOptions` | 同上 |
| `components/TaskDetail.vue` | `:25` 显示名映射 | 加 5 个中文名 |

同时 `ProcessDialog.vue:495` 的级别上限判断、`:38` `AddExportDialog.vue` 的格式白名单需按 provider 取 `max_zoom`。

### 5.2 底图预览

`utils/basemap.js` 的 `BASEMAP_OPTIONS` 增加 Google / Esri 项,`types` 用新值(如 `google_img`),并标记 `zoomable: true`(级别到 21)。

`composables/useMap.js:88` 的 `setBasemap` 目前只构造天地图 WMTS 图层。改为按 provider 分派:

- 天地图 → 现有 WMTS 构造(不变)
- Google / Esri → OpenLayers `XYZ` 源指向 `/api/tiles/{provider}/{z}/{x}/{y}`,即走后端转发(D6)

> 注意:后端转发时浏览器不直接接触 Google,因此**前端无需代理配置**。

## 6. 测试策略

### 6.1 后端单测(标准库 `unittest`,放 `tests/`)

| 文件 | 覆盖 |
|---|---|
| `test_mercator_tiling.py` | XYZ 换算正确性;**与 `osm.py` 原实现结果逐点一致**(重构回归护栏);往返一致性(经纬度→瓦片→四至) |
| `test_google_provider.py` | URL 模板渲染(4 图层 × 子域名轮询)、`bands` 正确(`google_road`=1)、`proxy` 透传、`max_zoom`=21 |
| `test_esri_imagery_provider.py` | URL 模板 `{z}/{y}/{x}` 顺序正确、`max_zoom`=18、`is_empty_tile()` 对占位图返回 True 且对正常瓦片返回 False |
| `test_mosaic_crs.py` | `crs="EPSG:3857"` 时 transform/CRS 正确;**默认参数下输出与改动前逐字节一致**(向后兼容护栏) |
| `test_formats_grid.py` | `grid_of()` 映射与回落、新 provider 的 `kind_of()` 为 `RASTER_IMAGE`、阶段推导正确 |

`is_empty_tile` 的测试需用**实测抓取的真实占位瓦片字节**作夹具(2521B),避免构造臆想的测试数据。

### 6.2 前端测试(Node 内置 runner)

`utils/basemap.test.js` 扩展:新底图类型的 `basemapTypesFor` 返回值、层级排序。

### 6.3 手工验证清单

1. 配代理后,Google 四图层各下载一个小范围(如 0.01° × 0.01°,z15~17),确认 GeoTIFF 产出且带 3857 坐标。
2. 四种导出格式逐一产出并检查目录结构(TMS 是 `{L}/{x}/{y}`、OSM 是 `{z}/{x}/{y}`)。
3. 用 QGIS 打开 GeoTIFF,确认坐标与在线底图对齐(抽查偏移 ≤4m)。
4. 预览:切换底图为 Google / Esri,确认瓦片正常加载;断开代理后确认降级为占位图而非报错。
5. 未配代理时提交任务 → 确认给出明确错误提示而非静默全失败。
6. Esri 源分别请求 z18(应有数据)与 z20(应为占位图),确认后者不计入有效成果。

## 7. 分期

| 期 | 内容 | 交付 |
|---|---|---|
| 一 | `mercator_tiling.py` 抽取 + `mosaic_to_geotiff` 加 `crs` + 下载器加 `proxy` | 纯重构,现有测试全绿,行为无变化 |
| 二 | `providers/google.py` + `providers/esri_imagery.py` + 配置 + `formats.py` 登记 + runner/api 分流 | 可下载、可拼 GeoTIFF |
| 三 | 四种导出格式按网格分流 | 完整导出能力 |
| 四 | 预览端点 + 前端下拉/底图接入 | 端到端可用 |

一期是纯重构且是后续基础,建议单独提交以便回溯。

## 8. 待确认事项

无。所有关键决策已与用户确认:

- 数据源范围:Google 四图层 + Esri World Imagery
- 接入方式:HTTP 代理
- 导出格式:GeoTIFF / OSM-XYZ / MBTiles / TMS geodetic 四种
- 金字塔:只出已下载级别,不补级
- 级别:Google 21、Esri 18、天地图 18(现状)
- 预览:后端瓦片转发
- 官方 API:不走,只用非官方端点
