# 验证记录 — Google / Esri 影像数据源

> 日期:2026-09-28
> 环境:Windows,本机代理 Clash `127.0.0.1:6789`
> 计划:`docs/superpowers/plans/2026-09-25-google-esri-imagery.md`
> 设计:`docs/superpowers/specs/2026-09-24-google-esri影像数据源-design.md`

## 基线(开工前)

| 套件 | 用例数 | 结果 |
|---|---|---|
| 后端 `unittest discover tests` | 322 | 全通过 |
| 前端 `node --test` | 124 | 全通过 |

工作区当时另有一批未提交的 3dtiles 改动(非本次范围)。本次所有提交均用显式
文件路径,未卷入这些改动。

## 阶段一:网格数学与下载器基础能力(Task 1-4)

| 项 | 结果 |
|---|---|
| `mercator_tiling.py` 抽取 | 16 项单测通过;`dem_tiling`/`osm` 改为 re-export |
| `mosaic_to_geotiff` 加 `crs` | 4 项通过;默认参数行为不变 |
| `normalize_proxy` | 13 项通过 |
| 下载器代理 + 404 分流 | 8 项通过 |
| 后端全量 | 322 → 363,全绿 |

**阶段一验收门**(不带 scheme 的配置值经代理取真实瓦片):

```
归一化: '127.0.0.1:6789' -> 'http://127.0.0.1:6789'
status=200 size=18084 ct=image/jpeg
```

`size=18084` 与设计阶段 §3.9 记录的字节数完全一致。

> 实现期发现:Task 4 的重试测试首跑 45 秒(指数退避 `min(2**attempt, 8)` 实打实
> 等待)。已把 `asyncio.sleep` 打桩 —— 要验的是"请求了几次"而非退避时长,
> 断言未改。45s → 0.043s。

## 阶段二:下载与拼接(Task 5-10)

| 项 | 结果 |
|---|---|
| 配置项(两处模板同步) | 加载正确;`_CONFIG_TEMPLATE` 与 `config.example.yaml` 均为合法 YAML |
| Google provider | 19 项通过 |
| Esri provider + 占位图两级判定 | 22 项通过(含关键负样本) |
| `formats.py` 网格登记 | 11 项通过 |
| runner 网格分流 | 12 项通过;**28 项进程隔离测试保持绿** |
| 后端全量 | 363 → 427,全绿 |

### 验收 A:真实下载 + 拼接坐标系

数据源 `google_img`,范围 116.380~116.392 / 39.995~40.005,z16:

```
provider=google_img bands=3 ext=jpg proxy=http://127.0.0.1:6789
z16 瓦片区间 3x3 = 9 张
下载: 成功=9 失败=0

crs    = EPSG:3857
shape  = (768, 768) bands=3
bounds = (12955159.050, 4865063.976, 12956993.539, 4866898.465)
bounds 与 mosaic_bounds_3857 一致 OK

转回经纬度 = (116.37817, 39.99396, 116.39465, 40.00658)
中心偏差 = 35m(经) 30m(纬)
```

坐标系为 3857、bounds 为米制且与 `mosaic_bounds_3857` 逐位一致;转回经纬度
后落在选区内。中心偏差 35m/30m 属正常——瓦片区间按网格对齐而非按选区对齐,
z16 单张瓦片约 611m。

### 验收 B:空瓦片语义(设计清单 13/14/16)

| 用例 | 数据源 | 位置 | 请求 | ok/fail | 落盘 | 占位图 |
|---|---|---|---|---|---|---|
| 13 占位图不入缓存 | esri_imagery | 南海 114.00~114.03 / 15.00~15.03 | 42 | 42/0 | **0** | **0** |
| 14 真实深海不误判 | esri_imagery | 太平洋 -140.00~-139.97 / -20.00~-19.97 | 49 | 49/0 | **7** | **0** |
| 16 Google 404 分流 | google_img | 渤海 119.50~119.53 / 38.50~38.53 | 48 | 48/0 | 0 | 0 |

**用例 14 是最强的一条证据**:同一个瓦片区间里,42 张占位图被正确拒绝、
7 张真实深海瓦片被正确保留 —— 判据在两个方向上同时成立,而非只满足一侧。
若判据用"体量小 + 色值少"这类朴素规则,这 7 张(678 字节量级、唯一色值约 11)
会一并被丢弃,成果出现静默空洞。

三条用例的 `fail` 均为 0:无数据不等于失败,请求本身是成功的。

### 验收 C:google_road 单波段路径(D9)

```
provider=google_road bands=1 ext=png
下载 9 张: ok=9 fail=0
原始瓦片: driver=PNG count=1 colorinterp=['palette'] 有调色板=True
拼接成果: crs=EPSG:3857 shape=(768, 768) bands=1
  像素值范围 = 5..255
```

确认 `lyrs=m` 确实是单波段调色板 PNG;拼接未抛 `DatasetIOShapeError`,
且像素值非全 0(调色板已由 `mosaic._read_tile` 展开)。

## 阶段四:四种导出格式(Task 16-17)

用真实管线跑完整阶段链路(models.create_task + runner.run_task),不经 UI。

| 任务 | 数据源 | 级别 | download | geotiff | tms | osm |
|---|---|---|---|---|---|---|
| verify_export_google | google_img | z15,16 | 4/0 | done | **8 张**(L14+L15) | 17 张 |
| verify_export_esri | esri_imagery | z15,16 | 4/0 | done | **6 张** | 16 张 |
| verify_mbtiles | google_img | z15,16 | 4/0 | — | MBTiles 8 张 | MBTiles 13 张 |

- 输出结构:`tms/{级别}/{x}/{y}.png`(级别 = 输入 z-1,与 CLAUDE.md 记录的
  gdal2tiles geodetic 约定一致)、`osm/{z}/{x}/{y}.png`。
- GeoTIFF crs 均为 EPSG:3857,转回经纬度落在选区内。
- TMS 瓦片内容有效率 11%~66%、取值 0~255 —— 与"小选区落在较大瓦片内"的
  预期一致(投影错则应为 ~0%,即空白)。
- MBTiles 两份均可被 sqlite 读出,`bounds` 是 WGS84 经纬度(MBTiles 规范
  要求),`format=png`。

### 实现期发现并修复的 bug(TMS 静默空产出)

首次跑 `google_img` 时 **TMS 阶段报 "8/8 张"、状态 done,而 tms/ 目录一张
瓦片都没有**。

根因:`export_tms_from_source` 要求源图是 **EPSG:4326** —— 它用
`range_for_bbox`(4326 网格)枚举输出瓦片,再与源图 bounds 求交。墨卡托源的
bounds 是米(±2e7)而瓦片四至是经纬度(±180),交集**恒为空**,一张都切不出来,
函数却照常返回成功。

修法沿用 DEM 的既有做法(`_tms_from_dem` 也是先渲染 4326 源再切):新增
`_mercator_raster_source`,把 3857 拼接图重投影成 4326 副本再喂进去。

另加空产出守卫 `_tms_output_looks_empty`:计划里有瓦片而**结束时目录为空**
即报错,让这类静默失败立刻可见。

> 该守卫的判据被自己新写的测试纠正过一次:初版用"本次没新增文件"判定,
> 而断点续切会原地覆盖、文件数不增,会**误报失败**。改为"结束时目录为空"。

## 待后续阶段验收的项

| 项 | 所属 Task |
|---|---|
| 四种导出格式目录结构与 TMS 对齐 | Task 17 |
| 区域最高级别探测(上海 z19 / 阿里 z17) | Task 15 |
| 代理预检错误文案 | Task 14 |
| 预览端点与断代理降级 | Task 18 |
| 进程隔离不回归(任务运行中界面可用) | Task 21 |
| 规模如实显示且不拦截 | Task 21 |
| 3857 源裁剪 | Task 20b |

## 参数校准结论

| 参数 | 设计暂定值 | 实测结论 |
|---|---|---|
| `suggest` 的 `tile_budget` | 8000 | 待 Task 21 |
| `_PREVIEW_CONCURRENCY` | 4 | 待 Task 21 |
| `_PREVIEW_TIMEOUT` | 8s | 待 Task 21 |
| `download.max_retries` | 3 | 阶段二 148 张瓦片请求中无瞬态失败,暂不需调整 |
