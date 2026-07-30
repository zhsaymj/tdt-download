# 方案：DEM GeoTIFF → Cesium quantized-mesh 地形切片

## 1. 目标

在现有「天地图下载处理工具」中集成 **DEM 高程 GeoTIFF → Cesium `.terrain` 切片** 能力：
产出 `layer.json`（最外层索引）+ `{z}/{x}/{y}.terrain`（quantized-mesh-1.0 瓦片），
可直接被 CesiumJS 的 `CesiumTerrainProvider` 加载。

- **技术路线**：纯 Python 内嵌（`pymartini` + `quantized-mesh-encoder`），保持工程「单机纯 Python、pip 即用、零系统依赖」基线。
- **入口形态**：地形（DEM）tab 的导出格式（`demExportOptions`）新增一项 `terrain`，与现有 `geotiff`/`hillshade`/`tiles` 并列。DEM 任务下载完成后，自动从高程 GeoTIFF 生成切片。

> **实现状态（2026-07-08 更新）**：第 1、2 步已实跑验证并落地核心模块
> `backend/core/terrain_tiles.py`；以下技术前提均已核实，原方案中的 3 处偏差已修正
> （层级映射、gzip、顶点编码坐标系），详见各节 ✅/⚠️ 标注。

## 2. 现状与可行性（已验证）

| 事项 | 结论 |
|------|------|
| venv Python 版本 | 3.11（cp311） |
| `pymartini` | 0.5.1，有 cp311 win_amd64 预编译 wheel ✅ 已实装 |
| `quantized-mesh-encoder` | 0.5.0，有 cp311 win_amd64 预编译 wheel，仅额外依赖 `attrs`（已装）✅ 已实装 |
| numpy | 2.2.1，已满足 |
| 系统依赖 | 无（两库均预编译，`pip install` 直接可用）✅ 已验证 |

现有可复用范式：
- `core/dem.py`：LERC → **EPSG:3857 float32 高程 GeoTIFF**（切片的数据源）。
- `core/osm.py`：从 GeoTIFF 逐瓦片 `rasterio.warp.reproject` 重投影切片（切片网格遍历、投影转换范式）。
- `core/tms.py`：`write_tilemapresource` 写最外层索引文件、`L = z-1` 级号换算、行号翻转（layer.json 与网格换算范式）。
- `core/runner.py::_export_dem`：DEM 导出按 `formats` 分支产出多种成果（terrain 作为新分支挂入）。
- `core/metadata.py`：成果清单（terrain 需补充说明字段）。

## 3. 关键技术点

### 3.1 坐标系与瓦片网格（易错点）

- 高程 GeoTIFF 源用 **EPSG:4326** float32 单波段。DEM 下载拼接产出的是 EPSG:3857
  （`dem.py`），terrain 分支切片前必须先 `reproject_geotiff(..., "EPSG:4326")`
  得到 4326 专用源（参照 osm.py 的重投影范式），再按 geodetic 网格切。
- Cesium 默认地形用 **WGS84 geodetic（EPSG:4326）** 的 `GeographicTilingScheme`：
  - level 0 有 **2×1** 两张根瓦片（tile `0/0`、`1/0`），覆盖全球 [-180,-90,180,90]。
  - 第 L 级：列数 `2^(L+1)`、行数 `2^L`，单张瓦片经纬跨度 `180/2^L`。
  - 瓦片 **x 自西向东**（0 在 -180°），**y 自南向北**（0 在 -90°，TMS 式）。
- ⚠️ **修正（原方案错误）**：`Cesium level L = 天地图 z - 1` 这条关系 **不适用** 于 DEM。
  `z-1` 只在天地图 4326 瓦片与 geodetic 网格同构时成立（`tms.py` 场景）；而 DEM 走
  **墨卡托 XYZ**（`mercator_range_for_bbox`），墨卡托 z 与 geodetic L 无整数分辨率对应。
  正确做法：重投影到 4326 后，由 **输出目标分辨率** 独立定级——
  `terrain_tiles.level_for_resolution(deg_per_px)`（解 `180/2^L/256 ≈ deg/px`）
  推出最高级 `Lmax`，输出 `[Lmax-k, …, Lmax]`。**不复用 `tms.py` 的 `tms_level`。**
- ⚠️ **修正（易错点）**：geodetic y **不需要翻转**。`osm.py`/`tms.py` 因源网格 y 自北向南
  才翻转；geodetic 网格 y 本就自南向北（TMS 式），`geodetic_range_for_bbox` 算出的 y
  直接就是 `.terrain` 行号。

### 3.2 每张瓦片的生成流程

对每个级别 `L`、每张覆盖 bbox 的瓦片 `(x, y)`：

1. 计算瓦片 4326 地理范围 `(west, south, east, north)`（geodetic 网格公式）。
2. 从 4326 高程源 `reproject` 重采样出该瓦片的高程窗口。
   - **网格尺寸**：pymartini 要求 `2^k + 1` 的正方形（如 257×257）。取 `grid=257`，重投影到 257×257。
   - nodata / 范围外像素填为 0（海平面）或就近值。
3. `Martini(257)` → `.create_tile(grid_f32)` → `.get_mesh(max_error)` 得到 `vertices`（(col,row) 索引）+ `triangles`。
4. ✅ **已实跑验证的官方链路**（修正原方案「手动组装/局部归一化」的模糊描述）：
   - `pymartini.rescale_positions(vertices, grid, bounds=(w,s,e,n), flip_y=True)`
     把 (col,row) 顶点 + 高程转成 **(lon, lat, height)** 数组，`flip_y=True` 因数组行 0
     在北、瓦片 y 自南向北；
   - `quantized_mesh_encoder.encode(f, positions, triangles, bounds=(w,s,e,n))`：
     positions **传经纬高即可**，encode 内部 `compute_header` 自动调 `to_ecef` 转地心
     坐标、`interp_positions` 按 bounds 归一化到 0–32767。**无需手工组装 ECEF。**
   - 首版不写法线，`layer.json` 不声明 `octvertexnormals`。
5. 写 `{out}/{L}/{x}/{y}.terrain`。⚠️ **修正：不做 gzip 压缩**，直接写未压缩 quantized-mesh
   字节。原因：本工程用 FastAPI `StaticFiles` 静态挂载 `/output`，不会自动加
   `Content-Encoding: gzip` 头；写 gzip 字节会导致 Cesium 拿到未声明编码的二进制而解析失败。
   未压缩 `.terrain` 合法，CesiumJS 按 arraybuffer 直接读，代价仅体积偏大。

> ✅ 已用 `地形下载_dem_z15.tif` 单瓦片 + 小范围批量实跑：字节结构合法、QM 头
> vertexCount 与网格一致、ECEF center 半径 ≈ 6.37e6 m、layer.json `available` 与实际
> 文件数交叉校验一致。**端到端 CesiumJS 加载确认无错位/空洞留待集成后人工验证。**

### 3.3 layer.json（最外层索引）

参照 quantized-mesh-1.0 规范生成，关键字段：

```json
{
  "tilejson": "2.1.0",
  "format": "quantized-mesh-1.0",
  "version": "1.0.0",
  "scheme": "tms",
  "tiles": ["{z}/{x}/{y}.terrain"],
  "projection": "EPSG:4326",
  "bounds": [-180, -90, 180, 90],
  "available": [ /* 每级的 [{startX,startY,endX,endY}] 瓦片区间 */ ]
}
```

- `available` 按每个级别、由 bbox 换算出的瓦片 `x/y` 区间生成（y 用翻转后的 TMS 行号）。
- `scheme: tms` 对应 y 自南向北。

## 4. 改动清单

### 4.1 新增 `backend/core/terrain_tiles.py`（核心）✅ 已实现

纯 Python 模块，不依赖上层。已实现函数：

- `geodetic_span(L) -> float`：第 L 级瓦片经纬跨度。
- `geodetic_tile_bounds(x, y, L) -> (w,s,e,n)`：geodetic 网格瓦片 4326 范围。
- `geodetic_range_for_bbox(bbox, L) -> (x_min,x_max,y_min,y_max)`：bbox 覆盖的瓦片区间（y 已是最终行号，不翻转，含边界钳制）。
- `level_for_resolution(deg_per_px) -> int`：由 4326 源分辨率反解 geodetic 级号（替代错误的 `z-1`）。
- `_read_tile_grid(src_data, transform, crs, nodata, bounds, grid=257) -> np.float32`：从整幅 4326 高程源 reproject 出瓦片 257×257 窗口，nodata/异常值填 0。
- `_encode_terrain(grid, bounds, max_error) -> bytes`：Martini TIN → rescale_positions → encode，**未压缩** quantized-mesh 字节。
- `export_terrain(dem_4326_path, levels, bbox, out_dir, on_level=None, max_error=5.0, grid=257) -> (out_dir, exported_levels)`：主入口，校验源为 4326，遍历级别与瓦片写 `.terrain`。
- `write_layer_json(out_dir, bbox, levels)`：写最外层 `layer.json`（`available` 数组下标与级号对齐，未切级占位空区间）。

### 4.2 修改 `backend/core/runner.py::_export_dem`

- `want_terrain = "terrain" in formats`。
- terrain 需要 4326 高程源。`reproject_geotiff` 是 **原地覆盖**，不能直接改 geotiff 成果，
  故拼一张 **专用临时 4326 源**（最高级别一张即可，切片按级重采样）：
  - 取最高级别墨卡托区间 `mercator_range_for_bbox(*bbox, max(levels))` →
    `mosaic_dem_geotiff` 拼一张临时 3857 tif（若 geotiff 分支已拼同级则复用，避免重复）；
  - 复制为 `{out_dir}/_terrain_src_4326.tif` 后 `reproject_geotiff(..., "EPSG:4326")`；
  - 用 `level_for_resolution` 由该 4326 源分辨率定 `Lmax`；
  - ⚠️ **必须导 `range(0, Lmax+1)` 完整金字塔，不能只导高层**。quantized-mesh 是
    层级化 TIN，Cesium 四叉树从 level 0 根瓦片逐级细化;缺 level 0 会导致 Cesium
    请求根瓦片(如 `0/0/0.terrain` 或 `0/1/0.terrain`)404、到不了数据层。
    低层每级仅 1~几张、极快，代价小。(最初误按"相邻 3 级"导致 404,已修正。)
  - 调 `export_terrain(src4326, range(0,Lmax+1), bbox, out_dir/"terrain")` + `write_layer_json`；
  - 把 `terrain` 目录加入 `outputs`，删除临时源 tif。
- `steps_total` 计入 terrain 步数（每级 1 步，经 `on_level` 回调驱动 `report()`），
  上报「切 Cesium 地形第 N 级」。

### 4.3 修改 `backend/core/metadata.py`

- `export_formats` 已透传，无需改结构；`terrain` 出现时在 outputs 里体现 `terrain/` 目录。
- 可选：`data_type`/说明补充 `"quantized-mesh-1.0"` 标注（小改）。

### 4.4 修改前端 `frontendvue/src/components/ParamsPanel.vue`

⚠️ **前端现状修正**：`demExportOptions` 当前实际只有 `geotiff`、`tiles` 两项
（后端虽支持 `hillshade`，但前端未暴露）。在这两项后新增：

```js
{ value: 'terrain', label: 'Cesium 地形切片(.terrain + layer.json)' },
```

并在 `dem-note` 补一句说明（输出 WGS84 geodetic quantized-mesh-1.0，供 CesiumJS
`CesiumTerrainProvider` 加载；未压缩、无法线）。

### 4.5 依赖 `requirements.txt`

新增并在 venv 实装：

```
pymartini==0.5.1
quantized-mesh-encoder==0.5.0
```

## 5. 验证计划

1. ✅ **单元级（已完成）**：用 `output/地形下载_1/地形下载_dem_z15.tif`（实测已是 4326）
   单瓦片跑通 Martini→rescale_positions→encode，QM 头 vertexCount/ECEF 半径正确。
2. ✅ **批量级（已完成）**：小范围切 L=13/14 共 26 张 `.terrain` + `layer.json`，
   `available` 区间与实际文件数交叉校验一致、无空文件。
3. **集成级（待做）**：起后端，跑一个小范围 DEM 任务勾选 `terrain`，确认成果目录结构、
   metadata、进度上报正常，临时 4326 源 tif 已清理。
4. **端到端（待做）**：用最小 CesiumJS 页面 `CesiumTerrainProvider({ url })` 指向 terrain
   目录，确认地形加载、无错位、无空洞、高程量级正确（唯一只能人工肉眼确认的风险点）。

## 6. 风险与备选

- **quantized-mesh 高度/顶点编码细节**：`quantized-mesh-encoder` 的 `encode` 对 positions 的坐标系约定需实跑校准；若首版顶点组装有偏差，先用单瓦片在 Cesium 里逐级排查。
- **投影方案分歧**：若后续需要 Web 墨卡托地形（`WebMercatorTilingScheme`），网格换算改用 `dem_tiling.py` 的 3857 公式即可，模块已按此预留分层。
- **精度/性能**：pymartini 的 `max_error`（米）控制 TIN 精细度，默认给一个折中值（如按级别递减），大范围高级别时注意瓦片数量（每升 1 级 ×4）。
- **备选路线**（本方案未采用）：cesium-terrain-builder Docker 镜像 `tumgis/ctb-quantized-mesh` 地形质量更好，但破坏纯 Python 基线，仅在纯 Python 精度不满足时再考虑。
