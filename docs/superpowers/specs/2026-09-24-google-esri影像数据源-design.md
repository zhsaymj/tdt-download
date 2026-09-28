# Google / Esri 影像数据源 — 设计文档

> 日期:2026-09-24(2026-09-25 两次修订:① 适配进程隔离架构 ② 代理环境实测验证)
> 范围:新增 Google 影像(非官方瓦片端点)与 Esri World Imagery 两个墨卡托影像数据源,支持下载、拼接与四种格式导出,并支持作为工具内底图预览。
> 前置:本设计基于 2026-09-24 的实测结论(见 §3),非实测的部分已明确标注。

## 0. 修订说明

### 0.1 第二轮修订:进程隔离架构适配(2026-09-25)

初版设计成文时,下载/拼接仍在主进程执行。此后项目完成**进程隔离改造**(`core/scheduler.py` + `core/worker.py`,`worker.num_workers` 默认 2),耗时操作移入 worker 子进程,主进程只做调度与 HTTP/WS。本次按新架构修订,共 7 处,其中 1 处是**实测确认的错误**:

| # | 修订点 | 性质 | 位置 |
|---|---|---|---|
| R1 | `ClientSession(proxy=...)` 必须带 scheme,原设计的 `"127.0.0.1:6789"` 会抛 `InvalidURL` | **实测纠错** | §4.4 |
| R2 | 预览转发在**主进程**执行,须与 worker 的下载器分开限流,否则代理会被下载打满 | 架构适配 | §4.7 |
| R3 | 配置经 `spawn` 传给 worker 的路径需明确(worker 重新 import `settings`,读的是自己那份) | 架构适配 | §4.8 |
| R4 | 代理预检必须在**主进程提交前**做,不能留给 worker | 架构适配 | §4.9 |
| R5 | z21 瓦片规模超出现有并发/预算假设,需级别预算与提交前拦截 | 实测补充 | §4.10(新增)**→ 拦截部分已被 0.3 撤销,见 D14** |
| R6 | `suggest_levels` 不能直接套 `suggest_dem_levels`(级别下限与预算不同) | 实测补充 | §4.6 |
| R7 | 分期顺序调整:代理贯通改为一期,先打通「能拿到瓦片」 | 计划调整 | §7 |

### 0.2 第三轮修订:代理环境实测验证(2026-09-25,本机 Clash 127.0.0.1:6789)

第二轮修订时本机代理未开启,§3.1 之后的数据源行为均沿用 2026-09-24 的记录。本轮在代理开启后重新实测,**其中 3 项推翻了初版的结论**:

| # | 修订点 | 性质 | 位置 |
|---|---|---|---|
| V1 | **Esri 占位图不只在 z20+**,任何"该处无影像"的位置(海洋/极地/西部无人区)在**任意级别**都可能返回。初版"正常使用中不会出现"的判断是**错的** | **推翻结论** | §3.4 修正、§3.10 |
| V2 | **Esri 最高可用级别随地区变化**:西藏/青海/新疆大片区域 **z18 即无数据**(最高 z17),城市可到 z19。D8 的"封顶 18 是保守安全取舍"对西部区域**无效** | **推翻结论** | §3.11、D15(新增) |
| V3 | Esri z19 经判定为**真实新增细节**(非 z18 上采样),城区封顶 18 会损失真实细节 | **新证据** | §3.13、§9 Q4 |
| V4 | **Google 无影像区返回 404 + 标准错误页**,确认其无 200 占位图行为(`is_empty_tile → False` 正确);但 404 有"无数据"与"端点失效"两种含义,初版的错误处理会误报 | 新证据 + 缺口 | §3.12、§4.9 修正 |
| V5 | **占位图是恒定字节**(跨大洲 4 地、多级别 sha 完全一致),可精确匹配;但朴素的"低梯度/少色值"判据会**误伤真实深海瓦片** | 新证据 + 纠错 | §3.10 |
| V6 | 代理环境下 **TLS 连接会被重置**(`ConnectionResetError`),需确认重试覆盖 | 新证据 | §3.9、§4.4 |
| V7 | 直连确认**仍不通**(初版结论成立);本轮一次"直连成功"是我的测试脚本把 proxy 写死导致的假象,已纠正 | 复验 | §3.1 |

### 0.3 第四轮修订:两项决策落定(2026-09-25,用户确认)

第三轮实测把两个待确认项推到了台面上,用户已决定:

| # | 决定 | 内容 | 位置 |
|---|---|---|---|
| Q4 | **Esri 上限 18 → 19** | 依据 V3 的"z19 是真实细节"证据上调。补充实测:z20 仅美国境内有数据,亚欧城市止于 z19。西部区域由 D15 的区域探测保护,不受影响。 | D8、§4.3、§4.8 |
| Q1 | **取消单任务瓦片数上限** | 移除原设计的 `MERCATOR_TILE_LIMIT = 200_000` 硬拦截。§4.10 由"三道防线"改写为"规模如实呈现 + Esri 级别剔除",并补录了各选区的实际规模量级(3° 选区 z21 = 5.3 亿张 / ≈9.6 TB)与残留风险。 | D14、§4.10、§6.1、§7 |

> 两条都是**用户的产品取舍**,不再开放讨论。特别注意 Q1:实现时**不要**按第二轮修订的旧描述把硬上限加回来 —— `test_estimate_mercator.py` 有一条回归护栏专门盯着"大范围不被拒绝"。

> 本轮实测的原始瓦片已存为夹具(§6.1),含一个**关键负样本**:678 字节的真实深海瓦片,朴素判据会把它误判成占位图。

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
| D8 | **级别上限:Google z21,Esri z19,天地图 z18(现状不变)** | 用户指定。Esri 原定 18,经 V3 实测(z19 是真实新增细节而非 z18 上采样,城区高频能量高 33~46%)后由用户上调至 **z19**(2026-09-25 确认)。实际可用级仍由 D15 按区域探测,`max_zoom` 只是探测的服务级天花板。 |
| D9 | **`google_road`(lyrs=m)的 `bands` 登记为 1** | 见 §3.4 发现 1。 |
| D10 | **配置不设 `key` 字段** | 见 §3.5,端点忽略该参数。 |
| D11 | **代理串统一在 provider 内归一化为带 scheme 的 URL** | 实测 `ClientSession(proxy="127.0.0.1:6789")` 抛 `InvalidURL`(见 §3.7)。归一化放 provider 而非下载器:下载器不该知道"代理是怎么配的",且预览端点(主进程)与下载器(worker)要共用同一份归一化结果。 |
| D12 | **预览转发与下载走两条独立的 HTTP 通路,各自限流** | 预览在主进程、下载在 worker,是**两个进程**,任何进程内的信号量都管不住对方。共用一个代理出口,下载峰值 16 并发(2 worker × 8)会把预览挤到超时。见 §4.7。 |
| D13 | **代理连通性预检在主进程提交时做,不放 worker** | worker 里失败只能表现为"任务跑起来又全部瓦片失败",用户看到的是进度条走到 0% 然后失败,排查成本高。主进程预检可以直接拒绝提交并给出可操作的错误文案。见 §4.9。 |
| D14 | **瓦片数不设硬上限,规模信息靠预估如实呈现** | 用户明确要求**不设单任务瓦片数上限**(2026-09-25)。原设计的 200,000 张硬拦截已取消。改为:提交前的预估接口如实报出逐级瓦片数与估算体积,由用户自行判断 —— 这是唯一且已存在的安全网。§4.10 记录了取消上限后各选区的实际规模,供界面文案与风险说明引用。 |
| D15 | **Esri 最高级别必须按选区探测,不能只用全局常量** | V2 实测:西藏/青海/新疆大片区域 z18 就是占位图(最高 z17),城市可到 z19。用户选了 z18,在西部会整片下到占位图。项目对同一家服务商的 DEM 已有同款先例(`providers/terrain.py::probe_max_level`,注释明写"最高 LOD 随地理位置变化"),**直接复用该模式**而不是重新发明。 |
| D16 | **`is_empty_tile` 用"精确指纹 + 保守启发式"两级判定** | V5:占位图跨大洲字节完全一致,可精确匹配(零误判);但 Esri 若改版会失效,故保留一个保守启发式兜底。**启发式的阈值必须用实测负样本校准** —— 678 字节的真实深海瓦片会把"体量小 + 色值少"这类朴素规则打成误判(§3.10)。 |
| D17 | **Google 的 404 分流为"该瓦片无数据",而非一律计入失败** | V4:Google 对无影像位置返回标准 404 错误页。若按初版"计入失败并提示端点可能变更",用户选一片海域会收到误导性错误,且每张瓦片白跑 3 次重试。 |

## 3. 实测结论(本设计的事实基础)

以下均为 2026-09-24 在本机(经 `127.0.0.1:6789` 代理)实测所得。

### 3.1 连通性(V7:2026-09-25 复验,结论不变)

| 数据源 | 直连 | 走代理 |
|---|---|---|
| `mt1.google.com/vt/lyrs=s` | ❌ 超时(15s) | ✅ HTTP 200 |
| Esri `World_Imagery` | ❌ 不通 | ✅ HTTP 200 |
| Esri `clarity.maptiles.arcgis.com` | ❌ 不通 | ✅ HTTP 200 |
| Esri `Terrain3D`(项目现用 DEM) | ✅ **通**(2.1s) | ✅ 通 |

结论:**两个新源都需要代理**;项目现有 DEM 源不受影响。

**2026-09-25 复验**。代理开启(Clash,`127.0.0.1:6789`)后以 `trust_env=False` 重测,直连仍然全部失败:

| 目标 | 直连 ×3 | 走代理 |
|---|---|---|
| `mt1.google.com/vt/lyrs=s` | 3/3 TimeoutError | 200,9071B |
| Esri `World_Imagery` | 3/3 TimeoutError | 200,14180B |
| `api.ipify.org`(对照) | 3/3 TimeoutError | 200,出口 IP `5.34.218.64` |

DNS 能正常解析(`mt1.google.com` → `142.250.73.78`,真实 Google IP),失败发生在 TCP 连接阶段 —— 与本机代理的出口规则一致。

> **一次方法论纠错**:本轮首次对照测试曾出现"直连也返回 200 且字节完全相同",看似推翻了 3.1。复查发现是我的测试脚本在 `probe()` 内部把 `proxy=PROXY` 写死,"直连对照组"实际仍走了代理。改用 `trust_env=False` 并显式不传 proxy 后,直连稳定失败。**记录在此以免后人重复踩**:aiohttp 的 `trust_env` 默认为 `False`(本轮实测确认),故"不传 proxy"就是真直连,不会被系统代理影响。

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

**发现 2:Esri 在"无影像"处返回 HTTP 200 + 固定占位图。**

实测返回固定 2521 字节的灰底带字图(`Map data not yet available`),均值 204.7,并非 404。若不识别,占位图会被当作有效数据写入缓存,污染断点续传并拼接出错误成果。项目已有 `TileProvider.is_empty_tile()` 机制(Esri Terrain3D 就是先例,见 `providers/terrain.py:67`),需为 Esri 影像实现。

> ⚠️ **本判断已被 V1 推翻(2026-09-25)**。初版认为"占位图只在 z20+ 出现,而设计封顶 z18,故正常使用中不会出现,只需作防御性实现"。实测证明:**占位图出现在任何"该处无影像"的位置与级别**,与缩放级别无关。详见 §3.10。

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

### 3.7 aiohttp 代理参数的形式要求(2026-09-25 实测,纠正初版设计)

初版 §4.4 写的是把配置里的 `proxy`(形如 `"127.0.0.1:6789"`)直接传给 `ClientSession`。实测该写法**不可用**:

```
aiohttp 3.11.11
ClientSession(proxy='127.0.0.1:6789')  → InvalidURL: 127.0.0.1:6789
ClientSession(proxy='http://127.0.0.1:6789') → 正常,HTTP 200
```

另外两点一并实测确认:

- `ClientSession.__init__` **确实接受** `proxy` 参数(3.11.11 的签名里有),session 级代理可用,不必逐请求传。
- session 级 `proxy` 对 **https 目标同样生效**(走 CONNECT 隧道):指向必然拒绝的 `http://127.0.0.1:1` 请求 `https://mt1.google.com/...`,报的是 `ClientProxyConnectionError`(而非 DNS/超时),证明请求确实经过了代理而非直连。

> 结论:配置值必须归一化。`BuildsConfig.proxy` 现有用法(`core/buildings.py` 走的是 requests/duckdb,形式要求不同)不能照搬到 aiohttp。归一化函数见 §4.4。

### 3.8 z21 的实际瓦片规模(2026-09-25 实测)

以 0.05°×0.05°(约 4.3km × 5.5km,一个城区)的选区为例,用 `mercator_range_for_bbox` 实算:

| 级别 | 瓦片数 | 网格 |
|---|---|---|
| z16 | 120 | 10×12 |
| z18 | 1,824 | 38×48 |
| z19 | 7,104 | 74×96 |
| z20 | 28,032 | 146×192 |
| z21 | **111,544** | 292×382 |

当前并发预算:`num_workers=2` × `download.concurrency=8` = **峰值 16 并发**,全部经同一个本机代理。z21 的 11 万张瓦片按此并发是数小时量级,且拼接出的整幅图为 74752×97792 像素(约 21.9 GPixel,3 波段未压缩约 65GB,必然走 BigTIFF)。

这不是"不能做",而是**必须让用户在提交前就知道**。规模如何呈现见 §4.10(注:硬上限已由用户取消,改以预估如实显示)。

### 3.9 代理环境的行为特征(2026-09-25 实测)

代理:`Clash V-Ninja`,系统代理 `127.0.0.1:6789`,已开启"始终使用默认绕过"(`localhost;127.*;192.168.*;10.*;...`)。

**4 个 Google 图层 + Esri World Imagery 均正常**(经代理,HTTPS):

| 目标 | status | size | Content-Type |
|---|---|---|---|
| `mt1.google.com/vt/lyrs=s`(卫星) | 200 | 18,084 | image/jpeg |
| `mt2.google.com/vt/lyrs=y`(混合) | 200 | 26,937 | image/jpeg |
| `mt3.google.com/vt/lyrs=m`(路线) | 200 | 12,894 | **image/png** |
| `mt0.google.com/vt/lyrs=p`(地形) | 200 | 16,828 | image/jpeg |
| Esri `World_Imagery` | 200 | 16,368 | image/jpeg |
| Esri `Terrain3D`(现有 DEM) | 200 | 41,616 | application/octet-stream |
| `khms1.google.com/kh/v=1000`(旧端点) | **404** | 114 | application/json |

> `khms` 端点 404 复验了 D7(端点 URL 必须可配置)。**D9 的波段判断也复验通过**:`lyrs=m` 的 `count=1`、`driver=PNG`、有 colormap(`lyrs=s/y/p` 均为 `count=3`、JPEG、无 colormap)。

**关键副作用:代理会引入 TLS 连接重置(V6)。**

实测经代理请求 Google 时偶发:

```
ConnectionResetError → ClientConnectorError: Cannot connect to host mt1.google.com:443
```

这是**代理链路特有**的瞬态故障(直连时表现为超时,不会出现 TLS 重置)。核对现有下载器的重试元组(`core/downloader.py:148`):

```python
except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
```

`ClientConnectorError` 继承自 `aiohttp.ClientError`,**已被覆盖**,无需改动。但这条实测值记录在此,因为代理环境下这类瞬态的**频率会明显升高**,`download.max_retries=3` 是否够用需要在一期实测后确认(见 §9 Q5)。

> 另注:Clash 的绕过列表对**后端下载无影响** —— 我们显式传 `proxy`,aiohttp 不会去读系统绕过规则;绕过规则只作用于"系统代理"这条路径。

### 3.10 Esri 占位图的精确指纹与判定策略(V1 / V5,本次最重要)

#### 实测:占位图是"一处无数据"的标志,与级别无关

`World_Imagery` 在**影像缺失**的位置返回 2521 字节的固定占位图(HTTP 200)。实测出现位置:

| 位置 | z15 | z16 | z17 | z18 | z19 | z20 |
|---|---|---|---|---|---|---|
| 北京北四环 | — | 真实 | 真实 | 真实 | 真实 | **占位** |
| 西藏阿里 | 真实 | 真实 | 真实 | **占位** | 占位 | 占位 |
| 青海可可西里 | 真实 | 真实 | 真实 | **占位** | 占位 | 占位 |
| 新疆塔克拉玛干 | 真实 | 真实 | 真实 | **占位** | 占位 | 占位 |
| 南海海域 | — | **占位** | — | — | — | — |
| 南极冰盖 | — | **占位** | — | — | — | — |
| 格陵兰冰盖 | — | **占位** | — | — | — | — |
| 太平洋深海 | — | 真实(678B) | — | — | — | — |

两类触发条件:
1. **超出该地区最高可用级别**(北京 z20、阿里 z18+)
2. **该处本来就没有影像**(海洋、极地)

> 第二类是本轮最重要的发现。初版假设"封顶 z18 后占位图不会出现",但用户只要选到海域、极地或西部无人区,在**任何级别**都会命中。故 `is_empty_tile()` 不是防御性实现,而是**正常路径的必需组件**。

#### 指纹:跨大洲字节完全一致

| 采样位置 | 级别 | size | sha256(前 16) |
|---|---|---|---|
| 北京 | z20 | 2521 | `9eafd300d6139318` |
| 上海 | z20 | 2521 | `9eafd300d6139318` |
| 新疆喀什 | z20 | 2521 | `9eafd300d6139318` |
| 西藏阿里 | z20 | 2521 | `9eafd300d6139318` |
| 南海 / 南极 / 格陵兰 | z16 | 2521 | `9eafd300d6139318` |

**完全一致**。完整 sha256:

```
9eafd300d61393184a4abc1d458564cfd1cd9b6f9c4e9c74687045c0a0e5b858
```

这意味着可以**精确匹配、零误判**。

#### 为什么不能用朴素的"低梯度 / 少色值"判据

初版 §4.3 提议的判据是"体量 < 4KB **且** 图像高度均匀(唯一色值数 ≤ 80)"。实测这个判据会**误伤真实瓦片**:

| 瓦片 | size | mean | std | 唯一色值 | 朴素判据结果 |
|---|---|---|---|---|---|
| **Esri 占位图** | 2521 | 204.73 | 5.37 | 79 | 命中(期望) |
| **真实深海瓦片** | **678** | **19.99** | 14.96 | **11** | **❌ 误判为空** |
| 真实沙漠瓦片 | 3578 | 154.68 | 28.55 | 156 | 不命中 |
| 真实城区瓦片 | 16368 | 131.17 | 46.94 | 254 | 不命中 |
| 真实西藏瓦片(z17) | 22828 | 120.58 | 62.94 | 256 | 不命中 |

**真实深海瓦片只有 678 字节、唯一色值仅 11 个** —— 两个条件都满足初版判据,会被当成"无数据"丢弃。后果是深海区域在成果里变成 nodata 空洞,且瓦片不被缓存(每次重跑都要重新下载)。这个负样本已存为夹具(§6.1)。

#### 采用的两级判定(D16)

```python
#: 实测 Esri World_Imagery "Map data not yet available" 占位图的 sha256。
#: 该图跨大洲、跨级别字节完全一致(2026-09-25 实测,4 地 × 多级别同 sha),
#: 故精确匹配即可零误判。值来自 tests/fixtures/esri_wi_placeholder_2521B.jpg。
_PLACEHOLDER_SHA256 = (
    "9eafd300d61393184a4abc1d458564cfd1cd9b6f9c4e9c74687045c0a0e5b858")

def is_empty_tile(self, data: bytes) -> bool:
    """Esri 超出该区域可用级别、或该处本就无影像时,返回 200 + 固定占位图。

    两级判定:
      1. 精确指纹(主):sha256 命中即判定。零误判,但 Esri 改版会失效。
      2. 保守启发式(兜底):为指纹失效准备,阈值由实测正负样本校准。

    启发式必须**同时**满足三个条件,且各留足裕量 —— 单看体量或色值数会把
    真实深海瓦片(678B、11 个色值)误判成占位图(实测,见 §3.10)。
    """
    if hashlib.sha256(data).hexdigest() == _PLACEHOLDER_SHA256:
        return True
    # 兜底:体量贴近 2521、亮度很高(占位图 204.7 vs 真实最亮 154.7)、
    # 且几乎无纹理(占位图 std 5.37 vs 真实最小 14.96)。
    if not (2000 <= len(data) <= 3000):
        return False
    try:
        arr = _decode_small(data)          # 解码失败 -> 不是占位图,交给上层判失败
    except Exception:
        return False
    return float(arr.mean()) > 190.0 and float(arr.std()) < 10.0
```

阈值裕量(实测值支撑):

| 条件 | 占位图 | 真实瓦片极值 | 阈值 | 裕量 |
|---|---|---|---|---|
| mean | 204.73 | 最高 154.68 | > 190 | 两侧各留 15~35 |
| std | 5.37 | 最低 14.96 | < 10 | 两侧各留 4.6~5 |

> 启发式**宁可漏判不可误判**:漏判(把占位图当真实)的后果是成果出现灰色方块,肉眼可见、可重跑;误判(把真实瓦片当无数据)的后果是**静默的数据空洞**,且瓦片不入缓存、每次重跑都重新下载。故阈值刻意偏向"判为真实"。

### 3.11 最高可用级别随地区变化(V2,推翻 D8 的地区假设)

#### Esri World Imagery

按区域逐级探测(`ok` = 有真实影像,`空` = 2521B 占位图):

> **⚠️ 采样方法更正(2026-09-28,实现期)**。下表初版由**单张中心瓦片**得出,
> 与探测实现里发现的 bug 同源:单点采样会把"该处恰好是占位图 / 恰好取不到"
> 误读成"该级无数据"。已用 **5×5 网格均匀采样**重验,修正了拉萨与乌鲁木齐
> 两行(它们实际能到 z19)。下表为更正后的值。

| 区域 | z17 | z18 | z19 | 实际最高 |
|---|---|---|---|---|
| 上海城区 | 25/25 | 24/25 | **25/25 真实** | ≥19 |
| 北京城区 | 25/25 | 24/25 | **25/25 真实** | ≥19 |
| **拉萨城区** | 25/25 | 25/25 | **25/25 真实** | **≥19** ← 更正 |
| **乌鲁木齐** | 25/25 | 25/25 | **25/25 真实** | **≥19** ← 更正 |
| 纽约 / 伦敦 / 东京 / 悉尼 | ok | ok | ok | ≥19 |
| 喀什 | 25/25 | **25/25 真实** | 0/25(全占位) | 18 |
| 漠河 | 25/25 | **25/25 真实** | 0/25(全占位) | 18 |
| 格尔木 | 25/25 | **0/25(全占位)** | 0/25 | **17** |
| 可可西里 | 25/25 | **0/25(全占位)** | 0/25 | **17** |
| 塔克拉玛干 | 25/25 | **0/25(全占位)** | 0/24 | **17** |
| 阿里 | 25/25 | **0/25(全占位)** | 0/22 | **17** |

(数值为"5×5 采样中,z 级有真实影像的瓦片数 / 出错的瓦片数"。)


**三点结论:**

1. **城市普遍能到 z19**(上海/北京/拉萨/乌鲁木齐/纽约/伦敦/东京/悉尼),不止初版以为的 18。
2. **西部无人区只到 z17**。西藏/青海/新疆的无人区在 **z18 就是整片占位图** —— 用户按原 D8 选 z18 会得到一整片灰色。**这是 D15(按区域探测)的立论依据,已用 25/25 全占位的结果确证。**
3. **z18 既不是安全上限,也不是实际上限**。它是"城市能过、西部过不去"的中间值,恰好是最容易让人误判的一个。

> **采样方法教训(记录备查)**:级别可用性必须用**多瓦片采样**判定,单张瓦片
> 会被"该处恰好无影像"或"该次请求恰好失败"污染。实现时同一张真实瓦片连取
> 4 次实测有 1 次失败 —— 单次尝试足以把 z19 误判成 z18(见 §4.3 的探测实现)。
> 实现里已按 3 次重试 + 5×5 网格采样验证;若日后要更严格的判定,
> 应把探测本身也改成多瓦片表决。

#### 补充实测:z19 以上只有美国境内还有数据

D8 上调至 z19 后,又补测了主要城市在 z19~z21 的表现:

| 城市 | z19 | z20 | z21 |
|---|---|---|---|
| 北京 / 上海 / 深圳 | ok | 占位 | 占位 |
| 伦敦 / 东京 / 巴黎 / 新加坡 | ok | 占位 | 占位 |
| **纽约** | ok | **ok**(11,119B) | 占位 |
| **洛杉矶** | ok | **ok**(13,688B) | 占位 |

**z19 是亚洲/欧洲城市的实际上限;z20 仅美国境内有。** 由于 D15 已按区域探测,把服务级 `max_zoom` 设成 19 是合理的(本项目面向国内使用,美国 z20 不是目标场景);若日后需要,改这一个常量即可,探测逻辑无需改动。

这与项目现有 DEM 的行为**完全同构**。`providers/terrain.py::probe_max_level` 的注释原文:

> "Esri 超出某区域最高 LOD 时返回空瓦片(约 67 字节、HTTP 200)而非 404,且最高 LOD 随地理位置变化(新疆等西部区域实测只到 14 级,城市可到 16 级)"

**同一家服务商、同一套 LOD 逻辑**。故 D15 直接复用该模式,而不是重新设计。

#### Google(对照:不存在地区性降级)

| 区域 | z16 | z17 | z18 | z19 | z20 | z21 | z22 |
|---|---|---|---|---|---|---|---|
| 北京 / 拉萨 / 乌鲁木齐 / 纽约 / 伦敦 | ok | ok | ok | ok | ok | **ok** | ok |
| 撒哈拉腹地 | ok | ok | ok | ok | ok | **ok** | 404 |
| 格陵兰腹地 / 南极内陆 | ok | ok | ok | ok | ok | **ok** | 404 |
| 太平洋中部 | 404 | 404 | 404 | 404 | 404 | 404 | 404 |

- **陆地上 z21 处处可用**,包括拉萨、乌鲁木齐等西部城市 —— 不存在 Esri 那样的地区性降级,故 Google **不需要** `probe_max_level`。
- **z22 仅部分地区有**(城市有、撒哈拉/格陵兰/南极无),故用户定的 z21 封顶恰好落在"全球陆地可用"的临界值上,**是有数据支撑的选择**。
- 海洋/极地全域 404(见 §3.12)。

### 3.12 Google 的 404 语义(V4)

Google 对"该处无影像"返回 **404 + 标准 Google 错误页**,而非占位图:

```
mt0~mt3.google.com 均返回 404,size=1603,Content-Type: text/html; charset=UTF-8
正文:<!DOCTYPE html> ... <title>Error 404 (Not Found)!!1</title> ...
```

**确认无 200 占位图行为** —— 初版 §4.3 给 Google 的 `is_empty_tile() → return False` 是**正确的**。

但 404 带来一个初版没考虑的问题:**404 有两种含义**。

| 含义 | 表现 | 应如何处理 |
|---|---|---|
| **(a) 该瓦片无影像** | 该瓦片 404,同批次其他瓦片 200 | 视为"无数据",**不重试、不计失败**,跳过 |
| **(b) 端点失效 / 鉴权变更** | 该批次**全部**瓦片 404 | 判任务失败,提示检查 `url_template` |

初版 §4.9 只有 (b) 的处理("失败率超阈值 → 提示端点可能已变更")。若用户选区含大片海域,会命中 (a) 却收到 (b) 的提示 —— 错误信息完全误导。且现状是每张 404 瓦片要白跑 3 次重试 + 指数退避(`max_retries=3`),海域面积大时开销可观。

分流办法见 §4.4(D17)。

> 实测的各位置 404 体量在 1602–1607 之间浮动、sha 互不相同(错误页内容随请求变化),**故不能靠 sha 或体量识别 404 页**;只能按"状态码 + 批次内比例"来分流。

### 3.13 Esri z19 是真实细节,不是上采样(V3)

D8 封顶 Esri 到 z18 的理由是"保守安全"。若 z19 只是 z18 的插值放大,封顶就没有损失;实测否定了这一点。

做法:取北京/上海的 z18 瓦片双线性放大 2 倍到 512×512,与覆盖同一范围的 4 张 z19 子瓦片拼接结果对比(都转为灰度,取 4×4 块均值残差的 std 作为高频能量):

| 区域 | z18 放大后高频 | z19 拼接高频 | 比值 | 平均绝对差 |
|---|---|---|---|---|
| 北京 | 9.81 | 14.29 | **1.46** | 27.20 |
| 上海 | 20.28 | 26.98 | **1.33** | 35.52 |

z19 的高频能量比 z18 放大结果高 **33%~46%**,平均绝对差 27~35 灰阶 —— **z19 载有真实的新增细节**,不是上采样。故城区封顶 z18 会实打实地损失分辨率(代价是 4 倍瓦片量)。

这是 D8 需要用户重新确认的依据,见 §9 Q4。

## 4. 后端设计

### 4.1 模块结构

```
core/tiling.py            (已有,不动) 4326 网格数学      ← 天地图
core/mercator_tiling.py   (新增)       3857 网格数学      ← DEM + Google + Esri
                                       含 suggest_mercator_levels (R6)
core/dem_tiling.py        (改)  保留 DEM 专有的建议级别逻辑,引用 mercator_tiling
core/osm.py               (改)  引用 mercator_tiling,删除本地重复实现
core/mosaic.py            (改)  mosaic_to_geotiff 加 crs 参数
core/downloader.py        (改)  session 级 proxy (R1) + missing_statuses 分流 (D17)
providers/base.py         (改)  proxy 属性 + normalize_proxy + missing_statuses (R1/D17)
providers/google.py       (新增)
providers/esri_imagery.py (新增) 含 is_empty_tile 两级判定 + probe_max_level (D15/D16)
api/tiles.py              (新增) 预览瓦片转发(主进程,独立限流)(R2)
api/tools.py              (改)  加代理连通性诊断 (R4)
api/tasks.py              (改)  提交预检 + 区域级别探测/剔除 + estimate/suggest 分流
main.py                   (改)  lifespan 里起/关预览 session (R2)
config.py                 (改)  GoogleConfig/EsriImageryConfig + _CONFIG_TEMPLATE
tests/fixtures/           (新增) 5 个实抓瓦片夹具,含关键负样本 (V5)
core/scheduler.py         ── 不动 ──  新 provider 对调度层零侵入
core/worker.py            ── 不动 ──  kind=RASTER_IMAGE 自然路由到 runner
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
        return normalize_proxy(self.cfg.proxy)     # R1:必须归一化,不能直接返回 cfg.proxy
    def tile_url(self, col, row, z) -> str:
        sub = next(self._sub)   # 子域名轮询,复用 tianditu 的 itertools.cycle 模式
        return self.cfg.url_template.format(s=sub, lyrs=self.lyrs, x=col, y=row, z=z)
    def max_zoom(self) -> int: return self.cfg.max_zoom   # 默认 21
    def is_empty_tile(self, data: bytes) -> bool:
        return False    # 实测 Google 无 200 占位图行为(V4 复验:无数据区返回 404)
    def missing_statuses(self) -> frozenset[int]:
        return frozenset({404})   # V4/D17:404 = 该瓦片无影像,详见 §4.4
```

> `is_empty_tile` 返回 `False` 不是"没实现",而是实测结论:Google 在无影像处返回 **404 + 错误页**,从不返回 200 占位图(§3.12 在 4 个位置 × 7 个级别上验证)。

#### `providers/esri_imagery.py`

| key | 中文名 | ext | bands | zmin | zmax |
|---|---|---|---|---|---|
| `esri_imagery` | Esri World Imagery | jpg | 3 | 1 | 18 → 见 §9 Q4 |

```python
def is_empty_tile(self, data: bytes) -> bool:
    """Esri 在"该处无影像"时返回 HTTP 200 + 2521 字节固定占位图。

    ⚠️ 初版判据("体量 < 4KB 且 唯一色值数 ≤ 80")已被实测否定 ——
    678 字节的真实深海瓦片(唯一色值 11)会被它误判成占位图。
    完整实测数据、阈值裕量与实现见 §3.10。要点:两级判定,
    精确 sha256 指纹为主、保守启发式为兜底。

    注意这个方法在**任意级别**都会命中,不只是超限级别:
    海洋/极地/西部无人区在任何级别都返回占位图(§3.10)。
    """
```

`tile_url` 用 `cfg.url_template.format(z=z, y=row, x=col)` —— 注意 ArcGIS 顺序是 `{z}/{y}/{x}`。

#### `probe_max_level`:按区域探测实际最高级别(D15)

**必须实现,不能省。** §3.11 实测:西藏/青海/新疆无人区在 z18 整片返回占位图(最高只到 z17),而城市可到 z19。用户按全局常量选级别,在西部会下到一整片灰色。

实现直接对齐 `providers/terrain.py::probe_max_level`(同服务商、同 LOD 逻辑,见 §3.11 末):

```python
def probe_max_level(bbox: tuple[float, float, float, float],
                    key: str = "esri_imagery") -> int | None:
    """探测 Esri World Imagery 在某范围的最高可用级别。

    从服务最高级往下,取范围中心瓦片逐级探测,返回第一个有真实影像的级别。

    与 terrain.probe_max_level 的差别:
      - 判空用 is_empty_tile()(占位图)而非 is_empty_lerc()
      - ⚠️ **不能**照抄它的 urllib 直连:Esri 影像必须走代理,否则全部超时,
        会被误判成"该范围没有影像"而把级别一路降到最低。
        (terrain 那份直连可用,故它用 urllib 是合理的;这里不行。)

    返回 None 表示**无法判定**(网络不通/代理未配)。调用方据此放行用户选的
    级别,而不是误判成"该范围没有影像"把级别降到 0 —— 与 terrain 版
    返回值语义一致(terrain.py:129 的注释)。
    """
```

> **代理是这里最容易写错的地方**:`terrain.probe_max_level` 用 `urllib.request` 直连(Terrain3D 直连可用),照抄过来会让 Esri 影像的探测**永远失败并静默降级到最低级别**。必须走 provider 的 `proxy`,建议统一用下载器同一套通路(aiohttp + `provider.proxy`),而不是复制 urllib 写法。

**前端接入**:复用现有 `/api/dem_max_level` 的形态,新增 `/api/imagery_max_level`(或把现有接口泛化为 `?provider=`),前端据返回的 `max_level` 禁用超限级别。现有 `api/tasks.py:151` 可直接参照。

> 探测是**逐级网络请求**,必须放 `asyncio.to_thread`(与现有 `_probe_dem_max_level` 一致),否则阻塞事件循环。且建议按量化 bbox 缓存结果(如 0.1° 网格)—— 用户反复调整选区时不该每次都重探。

### 4.4 下载器改动(R1:已按实测纠错)

#### 代理串归一化

初版直接把配置值传给 `ClientSession` 是错的(§3.7 实测 `InvalidURL`)。新增一个归一化函数,放 `providers/base.py` 供两处共用(worker 里的下载器、主进程里的预览端点):

```python
def normalize_proxy(raw: str | None) -> str | None:
    """把配置里的代理串归一化为 aiohttp 可用的 URL;空值返回 None(直连)。

    aiohttp 的 proxy 参数必须是带 scheme 的完整 URL —— 传 "127.0.0.1:6789"
    会抛 InvalidURL(实测 3.11.11)。用户在 config.yaml 里习惯只写 host:port
    (buildings.proxy 现有配置就是这个形式),故这里补 scheme 而不是让用户改写法。

    socks5:// 原样返回但**不受支持** —— aiohttp 不内置 SOCKS(需 aiohttp-socks),
    这里不静默降级成 http(那会连到 SOCKS 端口发 HTTP 请求,报错极难懂),
    而是原样传下去让 aiohttp 自己报 scheme 不支持。
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if "://" in raw:
        return raw
    return f"http://{raw}"
```

`providers/base.py::TileProvider` 新增属性(与既有 `headers` 设计对称):

```python
@property
def proxy(self) -> str | None:
    """下载该数据源瓦片时使用的 HTTP 代理 URL(默认无;子类可覆盖)。

    返回值已归一化(带 scheme),可直接传给 aiohttp。
    """
    return None
```

Google / Esri provider 的实现:

```python
@property
def proxy(self) -> str | None:
    return normalize_proxy(self.cfg.proxy)   # 不是 self.cfg.proxy or None
```

#### `core/downloader.py` 改动

```python
proxy = getattr(self.provider, "proxy", None)
async with aiohttp.ClientSession(headers=headers, timeout=timeout,
                                 proxy=proxy) as session:
```

两点说明:

- `getattr` 带默认值是为兼容 `CachedBuildingSource` 这类装饰器与测试替身。
- **必须用 session 级 `proxy` 而非逐请求传**:`_one` 里有重试循环,逐请求传要在三处 `session.get` 都带上,漏一处就是"重试时突然直连"——表现为偶发超时,极难定位。§3.7 已实测 session 级对 https 生效。

#### 与 `trust_env` 的关系(显式不启用)

`ClientSession` 另有 `trust_env` 参数(读 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量)。**本设计不启用它**(本轮实测确认其默认值就是 `False`):

- 天地图/DEM 源直连可用(§3.1),若 `trust_env=True`,用户机器上为别的软件设的全局代理会把天地图下载也绕进代理 —— 那是**现有功能的回归**,且是静默的。
- 代理只该作用于明确需要它的 provider,这正是把它做成 provider 属性而非全局开关的原因。

#### 404 语义分流(D17,新增)

V4 实测:Google 对无影像位置返回 **404 + 标准错误页**,而 404 有两种含义(§3.12)。现状会把两者都当成"失败并重试 3 次",既浪费又误报。

provider 侧新增一个声明,表达"这些状态码代表该瓦片无数据":

```python
def missing_statuses(self) -> frozenset[int]:
    """该数据源用这些 HTTP 状态码表示"此瓦片无数据"(默认空)。

    与 is_empty_tile 的区别:后者判的是 200 响应的**内容**(占位图),
    本方法判的是**状态码**。Google 用 404 表达无数据(实测,§3.12);
    天地图与 Esri 都用 200 + 内容,故默认返回空集。
    """
    return frozenset()
```

下载器 `_one` 的状态判断相应扩展:

```python
async with session.get(url) as resp:
    if resp.status in self.provider.missing_statuses():
        # 该处确实无数据(非瞬态故障):**不重试**、不计失败,直接返回成功。
        # 与 is_empty_tile 的处理一致 —— 请求本身是成功的。
        # 不重试是关键:海域/极地范围大时这是绝大多数瓦片,
        # 按原逻辑每张要白跑 3 次重试 + 指数退避(实测一个 0.05° 纯海域
        # 范围约 1800 张 => 5400 次无效请求)。
        return True
    if resp.status == 200:
        ...现有逻辑(含 is_empty_tile)...
    # 其余状态码:失败,进入重试
```

**端点失效(b 种含义)的兜底仍然保留**:若整批瓦片全部 404(`missing_statuses` 命中率 100%),说明更可能是 `url_template` 失效而非"整个范围都没影像"。由 §4.9 的失败率/成功率闸门统一兜住 —— 建议在任务收尾时判:`ok == 0 and missing > 0` → 判失败并提示检查 `url_template`,而不是报"成功但成果为空"。

> 这个兜底不能省。若只做 `missing_statuses` 分流而不加全批 404 的判定,端点失效会表现为"任务成功、成果全空",比报错更难排查。

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
| **最高可用级别探测** | 不适用(天地图无降级) | **Esri 必须探测**(D15/§3.11);Google 不需要(实测陆地处处到 z21) |

> **OSM 导出的快路径**:mercator 源的拼接图本身就是 3857,与 XYZ 瓦片网格完全同构,理论上零重采样。但 `osm.py::export_osm` 当前以 WarpedVRT 做 4326→3857 重投影。为控制本次改动面,**一期沿用现有 `export_osm`**(重投影 3857→3857 是恒等变换,GDAL 会优化,结果正确);若实测性能不可接受,二期再加直映射快路径。

#### 分流点的落位(进程隔离下的确认)

上表所有分流都发生在 `core/runner.py::run_task` **内部**,即 worker 子进程里。这一点对本设计是有利的,值得明确:

- 新增 provider **不需要改** `core/worker.py::_resolve_runner`。它按 `formats.is_3d_provider` 分发,Google/Esri 的 kind 是 `RASTER_IMAGE`,自然落到 `runner.run_task`,与天地图同一条路径。
- 新增 provider **不需要改** `core/scheduler.py`。调度器只认 `task_id`,不关心 provider。
- 故本次后端改动对进程隔离层是**零侵入**的,风险集中在 `runner.py` 内部的分支正确性上,由单测覆盖。

#### R6:`suggest_levels` 不能直接套 `suggest_dem_levels`

`api/tasks.py:130` 的现状是"DEM 走 `suggest_dem_levels`,其余走 `suggest_levels`"。mercator 影像源既不能走 `suggest_levels`(那是 4326 网格的列行数),也**不能直接复用 `suggest_dem_levels`** —— 实测把它的 `z_max_cap` 改成 21 后:

```
suggest_dem_levels(bbox_0.05°, z_max_cap=21)
→ recommended: [18], tiles: 1824, max_useful: 21, budget_limited: True
→ 级别表从 z0 起,共 22 项
```

两处不合影像语义:

| 问题 | `suggest_dem_levels` 的值 | 影像应为 | 原因 |
|---|---|---|---|
| 级别下限 | 0 | **1** | z0 单张盖全球,影像下它毫无意义;天地图影像的 `z_floor` 就是 1(`api/tasks.py:122`) |
| 瓦片预算 | 2000 | **待定,建议 8000** | 2000 是为 LERC 解码慢而定的(见 `dem_tiling.py` 该函数注释);影像瓦片是 jpg,解码快得多,沿用 2000 会把推荐级别压得过低 |

处理:`mercator_tiling.py` 里新增 `suggest_mercator_levels(bbox, z_max_cap, z_floor=1, tile_budget=8000)`,把 `suggest_dem_levels` 的判据逻辑提取为共用实现,两者各传自己的参数。**不是**给 `suggest_dem_levels` 加参数后共用同一个函数名 —— DEM 那份的 docstring 明确写了预算选择的理由,混在一起会让两套取舍互相污染。

> `tile_budget=8000` 是推断值,非实测。一期实现后应按实际拼接耗时校准,校准结论回写本节。

### 4.7 预览端点(R2:按进程隔离架构修订)

`api/tiles.py`:

```
GET /api/tiles/{provider}/{z}/{x}/{y}
```

基本约定(初版已定,不变):

- 仅接受已登记且 `enabled: true` 的 provider key,防止变成任意 URL 代理(SSRF 风险)。
- 用 `aiohttp` 带 provider 的 `proxy` 取瓦片,透传 `Content-Type`。
- **不写缓存**(D6)。
- 失败时返回 1×1 透明 PNG 占位,避免地图破图。
- 复用 `providers/` 的构造逻辑,保证预览与下载的端点配置单一来源。

#### 这个端点跑在主进程里(本次修订的核心)

进程隔离把**下载**移出了主进程,但预览转发是 HTTP 请求的一部分,**仍在主进程**。这带来三条此前不存在的约束。

**约束一:必须独立限流,不能依赖下载器的信号量(D12)**

下载器的 `asyncio.Semaphore(concurrency)` 在 worker 进程里,对主进程的预览请求**完全无效**。而两者共用同一个本机代理出口:

- 下载峰值:2 worker × 8 并发 = 16 条连接(§3.8)
- 预览:OpenLayers 平移一次可并发请求 20+ 张瓦片,无上限

两者叠加会打满代理。故预览端点需要自己的并发闸:

```python
#: 预览转发的并发上限。刻意小于 download.concurrency:
#: 预览是"看一眼",下载是"要完整成果"——争抢代理时应当让下载优先。
#: 这个信号量是主进程模块级单例,与 worker 里下载器的信号量是两个独立的闸,
#: 二者之和才是代理的实际峰值压力(设计预算:16 + 4 = 20)。
_PREVIEW_SEM = asyncio.Semaphore(4)
```

**约束二:超时必须短,且远小于下载超时**

`download.timeout` 默认 30s,预览绝不能用这个值:主进程的事件循环要服务所有 HTTP 与 WebSocket,20 个挂 30s 的转发请求会拖垮交互响应 —— 这正是进程隔离改造要解决的问题,不该从这里漏回来。

```python
#: 预览取瓦片超时(秒)。短:拿不到就返回占位图,让地图继续可用,
#: 而不是让用户对着转圈的瓦片等 30 秒。
_PREVIEW_TIMEOUT = 8
```

**约束三:复用单个 ClientSession,不要每请求新建**

每次请求新建 `ClientSession` 会重建连接池与代理隧道(https 要重新 CONNECT 握手),预览这种高频小请求下开销显著。做法:在 lifespan 里建一个模块级 session,随应用关闭。

```python
# api/tiles.py
_session: aiohttp.ClientSession | None = None

async def startup() -> None:
    """在 main.py 的 lifespan 里调用。

    session 必须在运行中的事件循环里创建 —— aiohttp 的 ClientSession 会绑定
    创建时的 loop,模块导入期(无 loop)创建会在首次使用时报
    "Timeout context manager should be used inside a task"。
    """
    global _session
    _session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=_PREVIEW_TIMEOUT))

async def shutdown() -> None:
    global _session
    if _session is not None:
        await _session.close()
        _session = None
```

> 注意 `proxy` **不能**设在这个共享 session 上:不同 provider 可能配不同代理(`google.proxy` 与 `esri.proxy` 是两个字段)。故此处 session 只统一超时,`proxy` 逐请求传:
> ```python
> async with _session.get(url, proxy=provider.proxy) as resp: ...
> ```
> 这与下载器"session 级 proxy"的选择相反,理由也相反:下载器一个 session 只服务一个 provider(重试时必须一致),预览端点一个 session 服务所有 provider。

**在 main.py 的接入位置**

```python
# lifespan 内,task_queue.start() 之后
await tiles_api.startup()
...
finally:
    await tiles_api.shutdown()     # 在 task_queue.shutdown() 之前
    ...
```

> 顺序:预览 session 先关。它只依赖事件循环,而 `task_queue.shutdown()` 会 `await asyncio.to_thread` 做数十秒的 join —— 放在后面关会让 session 在这段时间里悬着(日志出现 "Unclosed client session")。

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
    max_zoom: int = 19
    #: 是否按选区探测该地区的实际最高级别(D15)。默认开启。
    #: 实测西藏/青海/新疆无人区 z18 即无影像(最高 z17),城市可到 z19 ——
    #: 不探测的话用户按 max_zoom 选级别,在西部会下到一整片灰色占位图。
    #: 关掉只在"确定范围内级别一致"或想省去探测请求时考虑。
    probe_max_zoom: bool = True
    #: 探测按量化 bbox 缓存的有效期(小时)。0 = 不缓存。
    probe_cache_hours: float = 24.0
```

`config.example.yaml` 与 `config.py::_CONFIG_TEMPLATE` **都要**同步补充(后者是首次启动自动生成的模板,只改前者会让新用户拿到的 config.yaml 缺这两节),并在注释中写明:
- **两个源都需要代理**(实测直连均不通)。
- 代理可写 `127.0.0.1:6789` 或 `http://127.0.0.1:6789`,两种都行(代码会补 scheme)。**不支持 socks5**。
- **不设 `key` 字段**:非官方端点忽略 key 参数。
- `url_template` 会变,失效应先改这里。
- **Esri 的 `max_zoom`(默认 19)只是服务级天花板**,实际可用级别由 `probe_max_zoom` 按区域探测决定(§3.11)。z19 是亚欧城市的实际最高级;z20 仅美国境内有,如需可改此常量,探测逻辑无需改动。
- **本工具不限制单任务瓦片数**。大范围(如 3° × 3° 下 z21)会产生数亿张瓦片、TB 级体积,规模信息在提交前的预估里如实显示(§4.10)。

环境变量覆盖(沿用项目的 `TIANDITU_TOKEN` 模式):`GOOGLE_PROXY`、`ESRI_PROXY`。

#### R3:配置如何到达 worker(spawn 下的确认)

实测本项目的 multiprocessing 启动方式是 **spawn**(Windows 默认)。这意味着 worker 子进程是全新解释器,不继承主进程内存:

```
主进程: import backend.config → load_config() → settings(单例)
worker:  spawn 全新解释器 → runner.py 里 from ..config import settings
         → 在子进程中**重新执行** load_config() → 读同一份 config.yaml
```

对本设计的三个推论:

1. **新增的 `google`/`esri` 配置节无需任何传递机制**。worker 重新 import `settings` 时会自己从 `config.yaml` 读到,与现有 `download.concurrency`、`tools.*` 完全同一条路径。设计上不需要把配置塞进 `ControlMessage`。
2. **环境变量覆盖(`GOOGLE_PROXY`/`ESRI_PROXY`)在 spawn 下依然有效**:子进程继承父进程的环境变量,`load_config()` 在子进程里同样会读到。与 `NUM_WORKERS` 的现有行为一致。
3. **代价:改 config.yaml 后正在跑的 worker 不会重载**。这是现有行为(改 `download.concurrency` 也一样),不是本次引入的问题,但对代理尤其容易踩 —— 用户"改完代理立刻重试任务"可能仍用旧值。故 §4.9 的预检错误文案里要提示重启服务。

### 4.9 错误处理(R4:预检落到主进程)

| 场景 | 处理 | 发生位置 |
|---|---|---|
| 代理未配或不通 | **提交前预检**,直接拒绝提交(见下) | 主进程 |
| **Google 404(该瓦片无影像)** | `missing_statuses()` 分流:**不重试、不计失败**;瓦片不入缓存(见 §4.4 D17) | worker |
| **Google 全批 404(端点可能失效)** | `ok == 0 且 missing > 0` → 任务判失败,提示「端点可能已变更,请检查 `url_template`」 | worker |
| 其他端点错误(403/5xx) | 瓦片计入失败并重试;失败率超阈值时任务判失败,提示检查 `url_template` 与代理 | worker |
| **Esri 返回占位图** | `is_empty_tile()` 识别后不写缓存,计为请求成功。**⚠️ 任意级别都可能发生**(海洋/极地/西部无人区),不是仅超限级别(§3.10) | worker |
| **Esri 该区域级别不够** | 提交前探测最高可用级别并剔除超限级别(§3.11 D15),避免整片下到占位图 | 主进程 |
| 预览时代理不通 | 返回透明占位图,不破坏地图渲染 | 主进程 |
| 代理链路 TLS 重置 | 现有重试元组已覆盖(`ClientConnectorError ⊂ aiohttp.ClientError`),无需改动;但代理下频率升高,`max_retries` 是否够用待实测(§3.9 Q5) | worker |
| Google 拼接缺陷 | 数据源固有,成果说明中提示,引导用户改用 Esri 源(见 §3.6) | — |
| **任务规模过大** | **不拦截**。仅由预估接口如实显示瓦片数与体积(§4.10),用户自行判断 | 主进程 |

> 级别上限(Google 21 / Esri 19 / 天地图 18)由前端按 provider 常量控制;**不再有瓦片数上限这条拦截**。级别剔除仅针对 Esri 的"该区域无此级别数据",与任务规模无关(§4.10)。

#### 代理预检必须在主进程(D13)

**为什么不能留给 worker**:worker 里代理不通的表现是"任务变 running → 瓦片逐张重试 3 次 → 全部失败 → 任务 failed"。用户看到的是进度条停在 0% 然后红字失败,而真正原因(代理没开)埋在日志里。更糟的是这个过程会占满一个 worker 槽位若干分钟(`max_retries=3` × 指数退避 × N 张瓦片)。

**新增 `/api/tools/diagnose` 的两项**,复用现有诊断接口的形态(`api/tools.py` 已有 `_TOOLS` 表与 `_diagnose_one`,但那是探测本机可执行文件;代理探测是网络请求,另起函数):

```python
async def _diagnose_proxy(name: str, cfg) -> dict:
    """探测一个网络数据源的可达性:未启用直接跳过,启用则实际取一张瓦片。

    取真实瓦片而非 HEAD 根域名:端点失效(§3.5 的 khms v=1000 → 404)与代理
    不通是两种不同故障,只有真正请求一张瓦片才能区分,而这正是用户最需要
    区分的两种情况(前者改 url_template,后者开代理)。
    """
    item = {"enabled": cfg.enabled, "proxy": "", "reachable": False,
            "error": "", "hint": ""}
    if not cfg.enabled:
        return item
    proxy = normalize_proxy(cfg.proxy)
    item["proxy"] = proxy or "(直连)"
    # 取一张必然存在的低级别瓦片(z2,世界级,任何时候都有数据)
    ...
    # 失败时按症状给可操作的 hint:
    #   ClientProxyConnectionError → "代理地址不可连接,确认代理软件已启动"
    #   TimeoutError + 无代理      → "该数据源需要代理,请在 config.yaml 配置"
    #   404/403                    → "端点可能已变更,请检查 url_template"
```

**提交任务时的拦截**(`api/tasks.py::api_create_task`):

```python
# Google/Esri 需要代理才能下载。不预检的话失败会推迟到 worker 里,
# 表现为"任务跑起来又全部瓦片失败",且白占一个 worker 槽位数分钟。
if grid_of(data.provider) == GEO_MERCATOR and not is_dem_provider(data.provider):
    ok, msg = await _precheck_network_provider(data.provider)
    if not ok:
        raise HTTPException(400, msg)
```

错误文案要含**改完之后怎么办**:

> Google 影像下载失败:无法连接代理 `http://127.0.0.1:6789`。
> 请确认代理软件已启动,然后在 `config.yaml` 的 `google.proxy` 中确认地址。
> **修改配置后需重启服务**才会生效(见 §4.8 的 spawn 说明)。

> 预检本身要限时(建议 6s):它发生在提交请求的同步路径上,不能让"点提交"卡住十几秒。超时按"判不了"处理 —— 与 `probe_max_level` 返回 `None` 时放行的现有取舍一致(`providers/terrain.py:129`),**不要**把网络抖动当成"代理不通"而拒绝提交。

### 4.10 规模信息与级别剔除(用户决定:不设瓦片数上限)

**取消硬上限(2026-09-25 用户确认)。** 原设计的 `MERCATOR_TILE_LIMIT = 200_000` 硬拦截**不实现**。理由由用户判断:这是单机自用工具,规模取舍交给使用者。

#### 不设上限后各选区的实际规模(必须让用户看得到)

Google z21、单张 jpg 按 18KB 估(实测 9~27KB):

| 选区 | z18 | z19 | z20 | z21 | z15~21 合计 | 瓦片体积 |
|---|---|---|---|---|---|---|
| 0.01°(约 1km) | 80 | 320 | 1,170 | 4,543 | 6,151 | 0.1 MB |
| 0.05°(约 5km,一个城区) | 1,862 | 7,104 | 28,032 | 111,544 | 149,182 | 2.7 MB |
| 0.25°(约 27km) | 43,737 | 173,740 | 693,279 | 2,768,300 | 3,693,707 | 66.5 MB |
| 1°(约 110km) | 693,279 | 2,771,214 | 11,084,856 | 44,320,162 | 59,097,845 | ≈1.0 TB |
| 3°(约 330km,省级) | 6,233,805 | 24,935,220 | 99,720,729 | **398,842,617** | **531,780,458** | **≈9.6 TB** |

拼接 GeoTIFF 的规模(z21 全下):

| 选区 | 像素 | 未压缩体积 |
|---|---|---|
| 0.05° | 74,752 × 97,792(7,310 MPix) | 21.9 GB |
| 0.25° | 372,992 × 486,400(181,423 MPix) | 544.3 GB |

> 拼接本身**不占满内存** —— `mosaic_to_geotiff` 是逐瓦片行写入的,已走 BigTIFF(`mosaic.py:104`)。真正的约束是**磁盘**与**时间**,不是内存。

#### 因此这两件事从"可选优化"变成"必需"

**① 预估接口必须如实报数(唯一的规模提示)**

`/api/tasks/estimate` 对 mercator 源走 `estimate_mercator_tiles`,前端按现有方式展示逐级瓦片数与估算体积。现有 `_estimate_detail` 已有 `is_dem_provider` 分支(`api/tasks.py:32`),扩成按 `grid_of()` 分流即可,不新增机制。

> 硬上限取消后,**这是用户提交前唯一能看到规模的途径**。表里 3° 选区的 5.3 亿张瓦片/9.6 TB 不是小数,界面必须把它摆出来 —— 但不做拦截,由用户决定。

**② 建议级别(`suggest_mercator_levels`,见 §4.6 R6)**

作用是**默认值不会选到 z21** —— 实测 0.05° 选区在 8000 张预算下推荐到 z19 左右,而不是顶到 21。硬上限取消后,这一道的相对重要性更高了:大多数用户不会改默认值,默认值合理地低,就不会无意间触发 TB 级任务。

#### Esri 专属:提交前剔除超出区域能力的级别(与规模无关,独立保留)

上文的规模问题管"别下太多";这一条管"**别下一堆空瓦片**",两者正交,故不受 Q1 决定影响。

西藏阿里选 z18 只有约 1,824 张瓦片(规模上完全无害),但**全部是占位图**,成果是一片灰。

故 Esri 的任务在提交时应:① 若 `probe_max_zoom` 开启,探测选区最高可用级别;② 用户勾选的级别中超出该值的,在界面上禁用并给出提示;③ 服务端作为兜底,把超限级别**从任务里剔除**(而非拒绝提交)。

> 剔除而非拒绝:用户在西藏选了 z15-z18,z18 无数据时,下 z15-z17 是合理的期望,不该整个任务被拒。剔除后要在任务详情里明确记录"已跳过 z18(该区域最高 z17)",让用户知道发生了什么。

#### 取消上限后的残留风险(记录备查,不额外加限制)

| 风险 | 说明 | 现有缓解 |
|---|---|---|
| 磁盘写满 | 3° 选区 z21 全下约 9.6 TB 瓦片 | 无自动保护。瓦片缓存在 `data/tiles/`,写满后瓦片请求失败计入失败,任务最终判失败 |
| worker 槽位长时间占用 | 默认只有 2 个 worker,一个 TB 级任务会占住其一数小时至数天 | `scheduler` 无优先级/抢占概念(见 §8)。期间其他任务排队等 |
| 单阶段超时 | 现有管线无任务级超时 | 无。可随时「暂停/取消」(每 worker 独立信号队列,`scheduler.py:172`) |

> 这三条**不是**要求新增功能,只是把取消上限后用户实际承担的东西写清楚。若日后觉得需要,最自然的做法是给 `scheduler` 加优先级而非恢复硬上限 —— 但那是另一个需求。

## 5. 前端设计

### 5.1 数据源下拉(3 处)

| 文件 | 位置 | 改动 |
|---|---|---|
| `components/ProcessDialog.vue` | `:46` `providerOptions` | 加 5 项,归入「影像」分组 |
| `components/RedownloadDialog.vue` | `:24` `providerOptions` | 同上 |
| `components/TaskDetail.vue` | `:25` 显示名映射 | 加 5 个中文名 |

同时 `ProcessDialog.vue:495` 的级别上限判断、`:38` `AddExportDialog.vue` 的格式白名单需按 provider 取 `max_zoom`。

#### Esri 的级别上限来自探测结果,不是常量(V2/D15)

这是本次修订对前端影响最大的一处。现状是"级别上限 = provider 的 `max_zoom` 常量";对 Esri 影像,必须改成**按选区探测**:

| 数据源 | 级别上限来源 |
|---|---|
| 天地图 | 常量 18(现状不变) |
| Google | 常量 21(实测陆地处处可用,无地区降级,§3.11) |
| **Esri 影像** | **按选区调后端探测**(西藏/青海/新疆最高 z17,城市可到 z19) |
| Esri DEM | 已有 `/api/dem_max_level` 探测(现状不变) |

交互形态直接对齐现有 DEM 的做法(`ProcessDialog.vue` 已有 DEM 的探测调用与级别禁用逻辑),不要新造一套:

1. 选区确定后(用户画完框)调用探测接口;
2. 返回 `max_level` 后,下拉里超过它的级别**置灰并标注**(如"该区域无 z18 数据");
3. 探测返回 `null`(网络/代理不通)时**不禁用任何级别** —— 与现有 DEM 的取舍一致,不能把网络问题当成"该区域没数据";
4. 探测中的 loading 态与防抖:用户拖拽选区会连续触发,需按量化 bbox 去重(后端也缓存)。

> 若不做这一步,用户在西部选 z18 会得到一个"成功但全是灰块"的成果 —— 比报错更难发现问题。这属于**必须做**,不是优化项。

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
| `test_esri_imagery_provider.py` | URL 模板 `{z}/{y}/{x}` 顺序正确、`max_zoom`=19、`is_empty_tile()` 判定见下、`probe_max_level` 的分级回退与 `None` 语义 |
| `test_mosaic_crs.py` | `crs="EPSG:3857"` 时 transform/CRS 正确;**默认参数下输出与改动前逐字节一致**(向后兼容护栏) |
| `test_formats_grid.py` | `grid_of()` 映射与回落、新 provider 的 `kind_of()` 为 `RASTER_IMAGE`、阶段推导正确 |
| `test_proxy_normalize.py` | **(新增,R1)** `normalize_proxy`:`"127.0.0.1:6789"` → `"http://127.0.0.1:6789"`;带 scheme 原样返回;空串/None → `None`;`socks5://` 原样返回(不静默改 http) |
| `test_preview_tiles.py` | **(新增,R2)** 未登记 provider 返回 4xx(SSRF 护栏)、`enabled=False` 被拒、上游失败时返回透明 PNG 而非 5xx、`proxy` 逐请求传递正确 |
| `test_mercator_suggest.py` | **(新增,R6)** `suggest_mercator_levels` 的 `z_floor=1`(不含 z0)、预算生效;**且 `suggest_dem_levels` 的返回值与改动前逐字段一致**(提取共用实现的回归护栏) |
| `test_missing_status.py` | **(新增,V4/D17)** `missing_statuses()` 默认空集、Google 返回 `{404}`;**404 不计失败且不重试**(用 mock 断言只请求 1 次,而非 `max_retries+1` 次);全批 404 时任务判失败 |
| `test_estimate_mercator.py` | **(新增,§4.10)** `estimate_mercator_tiles` 按 `grid_of()` 分流;**大范围不被拒绝**(如 3° × 3° z21 的 5.3 亿张须正常返回预估,回归护栏 —— 防止后人好心加回硬上限) |

#### 已抓取的真实瓦片夹具(本轮实测产物)

路径 `tests/fixtures/`,**都是经代理实抓的真实响应字节**,不要事后重新构造:

| 文件 | 字节 | mean | std | 唯一色值 | 用途 |
|---|---|---|---|---|---|
| `esri_wi_placeholder_2521B.jpg` | 2521 | 204.73 | 5.37 | 79 | **正样本**:`is_empty_tile` 应返回 True |
| `esri_wi_real_ocean_678B.jpg` | 678 | 19.99 | 14.96 | 11 | **关键负样本**:真实深海瓦片,`is_empty_tile` 必须返回 False |
| `esri_wi_real_sahara.jpg` | 3578 | 154.68 | 28.55 | 156 | 负样本:体量小且明亮的真实瓦片 |
| `esri_wi_real_beijing.jpg` | 16368 | 131.17 | 46.94 | 254 | 负样本:常规城区 |
| `esri_wi_real_tibet_z17.jpg` | 22828 | 120.58 | 62.94 | 256 | 负样本:西部高纹理 |

占位图完整 sha256(供实现里的精确指纹常量核对):

```
9eafd300d61393184a4abc1d458564cfd1cd9b6f9c4e9c74687045c0a0e5b858
```

> **`esri_wi_real_ocean_678B.jpg` 是本组夹具里最重要的一个**。它是唯一能让朴素判据翻车的样本:678 字节(比占位图还小)、唯一色值 11(比占位图还少)。若 `is_empty_tile` 的实现只用"体量小 + 色值少",这个测试会红 —— 这正是它存在的意义。裁剪/替换这组夹具时要保留它。

> 夹具是**实抓数据**,Esri 若清理或改版可能不再复现。判据测试应当这样组织:正样本(占位图)必须永远返回 True;负样本(真实瓦片)必须永远返回 False。若哪天 Esri 换了占位图导致正样本测试红,说明该更新指纹常量与夹具,而不是放宽判据。

#### 进程隔离相关的测试注意(本次修订新增)

现有 `tests/test_scheduler.py` / `tests/test_worker_dispatch.py` 是进程隔离的回归护栏。本次改动**不碰** scheduler/worker(见 §4.6「分流点的落位」),故:

- **不需要**为新 provider 加 scheduler/worker 层的测试。
- 但要确认这两个文件**保持全绿** —— 若它们因本次改动变红,说明改动意外侵入了进程隔离层,应当停下来看为什么,而不是改测试。
- 预览端点测试走 `fastapi.testclient.TestClient`(项目已有 httpx 依赖)。注意 `with TestClient(app)` 会跑 lifespan,而 lifespan 里有 `task_queue.start()` —— 现有 scheduler 的 `start()` 已做幂等守卫(`scheduler.py:57`),但测试里应当 mock 掉上游 HTTP 而非真的走代理。

### 6.2 前端测试(Node 内置 runner)

`utils/basemap.test.js` 扩展:新底图类型的 `basemapTypesFor` 返回值、层级排序。

### 6.3 手工验证清单

1. 配代理后,Google 四图层各下载一个小范围(如 0.01° × 0.01°,z15~17),确认 GeoTIFF 产出且带 3857 坐标。
2. 四种导出格式逐一产出并检查目录结构(TMS 是 `{L}/{x}/{y}`、OSM 是 `{z}/{x}/{y}`)。
3. 用 QGIS 打开 GeoTIFF,确认坐标与在线底图对齐(抽查偏移 ≤4m)。
4. 预览:切换底图为 Google / Esri,确认瓦片正常加载;断开代理后确认降级为占位图而非报错。
5. 未配代理时提交任务 → 确认给出明确错误提示而非静默全失败。
6. Esri 源在北京请求 z18(应有数据)与 z20(应为占位图),确认后者不计入有效成果。
7. 代理配成 `127.0.0.1:6789`(**不带 scheme**)→ 确认下载正常(R1 的归一化生效)。这一条是本次修订的核心验证点。
8. **(R2)进程隔离不回归**:提交一个 Google 大范围任务(z18、数千张),任务运行期间:
   - 反复平移/缩放地图(预览持续请求),确认地图仍可交互、不卡死;
   - 同时打开任务列表、点进任务详情、看 WS 进度推送,确认 HTTP 与 WebSocket 都不挂起。
   > 这是进程隔离改造要保住的核心性质。若预览转发写成同步或超时过长,会从主进程这一侧把它破坏掉。
9. **(R2)预览不与下载抢代理**:上一条运行中观察预览瓦片是否大面积变占位图。若是,说明 `_PREVIEW_SEM`/`_PREVIEW_TIMEOUT` 的预算需调整,把结论回写 §4.7。
10. **(§4.10)规模如实显示且不拦截**:选 0.25° 范围勾 z15~z21 → 确认预估里显示约 369 万张瓦片 / 66MB,**且可以正常提交**(不被拒绝);界面上规模数字清晰可读。
    > 这条同时验证"不设上限"与"规模可见"两个要求 —— 只满足前者(不拦但也不显示)是不可接受的。
11. **(R3)配置重载语义**:任务运行中改 `config.yaml` 的 proxy → 确认新任务仍用旧值(符合 §4.8 推论 3),且预检错误文案里提示了重启。
12. **(R6)建议级别**:对 0.05° 与 0.5° 两种范围调 `/api/tasks/suggest_levels`,确认推荐级别不含 z0、且推荐总瓦片数在预算内。

#### 本轮实测新增的验收项(V 系列)

13. **(V1)占位图在正常级别出现**:选**南海海域**(如 114.0~114.1°E / 15.0~15.1°N)下 z16。确认:
    - 瓦片缓存目录里**没有** 2521 字节的文件(未污染缓存);
    - 任务不报失败(该处确实无数据);
    - 成果 GeoTIFF 对应区域是 nodata 而非灰色方块。
14. **(V1)真实深海瓦片不被误判**:选**太平洋**(如 140°W / 20°S)下 z16,**同一任务内**应有少量真实瓦片(实测该处 678 字节)。确认这些瓦片**被正常写入缓存并拼接**,成果里能看到深海影像而非空洞。
    > 这是 §3.10 判据的核心验收 —— 如果实现用了朴素判据,这一条会失败而第 13 条仍通过,两条必须都过。
15. **(V2/D15)区域级别探测**:分别对**上海**(预期 z19 有数据)与**西藏阿里**(预期最高 z17)调探测接口,确认返回的 `max_level` 与 §3.11 表格一致;对阿里选区勾 z18 → 确认级别被禁用/剔除,且任务详情记录了跳过原因。
15b. **(D8 上调)** 对上海选区勾 **z19** → 确认可下载且成果有真实细节(与 z18 成果对比,细节应更丰富 —— 这正是上调的依据,§3.13)。
16. **(V4/D17)Google 404 分流**:
    - 选一片**含海域**的范围(如渤海近岸 119.5°E / 38.5°N)下 z16 → 确认任务**不报"端点可能已变更"**,而是正常完成、海域部分为 nodata;
    - 观察请求日志,确认 404 瓦片**只请求 1 次**(而非 `max_retries+1 = 4` 次);
    - 再把 `url_template` 故意改错(如把域名改成不存在的)→ 确认全批 404 时**会**判失败并提示检查 `url_template`。
17. **(V6)代理瞬态故障**:连续下载数百张瓦片,观察日志里 `ClientConnectorError`/TLS 重置的出现频率,判断 `max_retries=3` 是否够用(结论回写 §3.9 与 §9 Q5)。

## 7. 分期(R7:调整)

| 期 | 内容 | 交付 | 验收 |
|---|---|---|---|
| 一 | `mercator_tiling.py` 抽取 + `mosaic_to_geotiff` 加 `crs` + `normalize_proxy` + 下载器加 `proxy` + **`missing_statuses` 分流** | 纯重构 + 代理能力 + 404 语义 | 现有测试全绿(含 `test_scheduler`/`test_worker_dispatch`),行为无变化;`test_missing_status` 通过 |
| 二 | `providers/google.py` + `providers/esri_imagery.py`(含 `is_empty_tile` 两级判定)+ 配置(两处模板)+ `formats.py` 登记 + runner 分流 | 可下载、可拼 GeoTIFF、空瓦片不发散 | 清单 1、3、6、7、**13、14、16** |
| 三 | `probe_max_level` + `/api/imagery_max_level` + 预检 + `suggest_mercator_levels` + estimate 分流 | 提交路径安全、规模可见 | 清单 5、10、11、12、**15、15b** |
| 四 | 四种导出格式按网格分流 | 完整导出能力 | 清单 2 |
| 五 | 预览端点 + 前端下拉/底图接入 + **前端按探测结果禁用级别** | 端到端可用 | 清单 4、8、9、17 |

与第二轮修订的调整:

- **`missing_statuses` 分流并入一期**。它与 `proxy` 一样属于"下载器基础能力",且一行状态判断的改动,和 404 重试浪费是同一个代码位置。放二期会和 provider 一起改,分散注意力。
- **`is_empty_tile` 的验收提前到二期**,不再等到导出阶段。第 13/14 条(占位图不入缓存 + 深海瓦片不误判)是二期就必须过的门 —— 否则拼出来的 GeoTIFF 从第一张起就是错的,后面所有导出格式都在错成果上做验证。
- **`probe_max_level` 留在三期**。它是"提交路径安全"的一部分,且前端要配套改动,与级别剔除同批做最自然。

与初版的两处调整(仍然有效):

- **`normalize_proxy` 提前到一期**。初版把代理改动放一期、provider 放二期,但代理是这两个源的**共同前提**(D5)—— 一期若不含代理归一化,二期第一次真实取瓦片就会踩 §3.7 的 `InvalidURL`,而那时同时在改 provider、配置、formats、runner 四处,排查面被放大。一期结束时应当能用一个临时脚本经代理取到一张 Google 瓦片。
- **预检与级别处理独立成三期,先于导出格式**。初版把它们并在错误处理里没有单独排期。虽然硬上限已取消(规模问题不再拦截),但"提交后立刻发现代理不通"和"下到一整片灰色占位图"这两件事仍比"少一种导出格式"严重,应当在能下载之后**立刻**补上。

> 三期**不再包含**瓦片数硬上限(用户已取消)。三期剩下的拦截只有 Esri 的区域级别剔除,以及代理预检 —— 两者都不是"拒绝提交",分别是"剔除无效级别"与"提前报错"。不要按旧版本的设计把 `MERCATOR_TILE_LIMIT` 加回来:`test_estimate_mercator.py` 有一条回归护栏专门盯着这件事。

与初版的两处调整:

- **`normalize_proxy` 提前到一期**。初版把代理改动放一期、provider 放二期,但代理是这两个源的**共同前提**(D5)—— 一期若不含代理归一化,二期第一次真实取瓦片就会踩 §3.7 的 `InvalidURL`,而那时同时在改 provider、配置、formats、runner 四处,排查面被放大。一期结束时应当能用一个临时脚本经代理取到一张 Google 瓦片。
- **预检与级别拦截独立成三期,先于导出格式**。初版把它们并在错误处理里没有单独排期。实际上"提交一个 11 万张瓦片的任务把 worker 堵住数小时"是比"少一种导出格式"更严重的问题,应当在能下载之后**立刻**补上,而不是等四种格式都做完。

一期是纯重构(除 `normalize_proxy` 为纯新增),建议单独提交以便回溯。

## 8. 与进程隔离架构的关系(总结)

本次修订后,新数据源与进程隔离层的边界:

```
主进程                                    worker 子进程(×2)
──────────────────────────────────       ──────────────────────────────
api/tasks.py   提交校验 + 代理预检        core/runner.py  按 grid 分流
               + 区域最高级别探测/剔除        (§4.9/4.10)      瓦片区间/拼接/导出
               (规模不拦截,仅如实预估)      core/downloader.py
api/tiles.py   预览转发(独立限流)           session 级 proxy     (§4.4)
               (§4.7)                        missing_statuses 分流 (§4.4 D17)
api/tools.py   代理连通性诊断 (§4.9)
core/scheduler.py  ← 本次零改动 →        core/worker.py  ← 本次零改动 →
```

**三条要守住的性质:**

1. **不侵入 scheduler/worker**。新 provider 的 kind 是 `RASTER_IMAGE`,`_resolve_runner` 自然路由到 `runner.run_task`,与天地图同路径。若实现时发现"必须改 worker.py",说明抽象放错了位置,应当回看 §4.6。
2. **主进程不做耗时网络操作**。预览转发与预检都在主进程,故都必须**短超时 + 独立限流**。进程隔离改造把 GDAL 的阻塞移走了,不能从 HTTP 转发这一侧把阻塞加回来。
3. **代理压力是两个进程之和**。设计预算:下载 16(2×8) + 预览 4 = 20 条并发。调整 `worker.num_workers` 或 `download.concurrency` 时要记得这个出口是共享的 —— 单机自用的本地代理未必扛得住把 worker 调到 8。

**另需注意:探测类网络请求也全在主进程**(代理预检、区域最高级别探测)。它们同样受性质 2 约束:必须 `asyncio.to_thread` + 短超时 + 按 bbox 缓存。区域级别探测是**逐级多次请求**(最坏 z19→z0 逐级),比预检重得多,缓存不是优化而是必需。

## 9. 待确认事项

初版的关键决策均已与用户确认:

- 数据源范围:Google 四图层 + Esri World Imagery
- 接入方式:HTTP 代理
- 导出格式:GeoTIFF / OSM-XYZ / MBTiles / TMS geodetic 四种
- 金字塔:只出已下载级别,不补级
- 级别:Google 21、**Esri 19**、天地图 18(现状)—— Esri 由 18 上调至 19,2026-09-25 确认
- 预览:后端瓦片转发
- 官方 API:不走,只用非官方端点

### 已决策(2026-09-25)

| # | 事项 | 决定 | 影响 |
|---|---|---|---|
| **Q1** | **单任务瓦片数上限** | **不设上限** | 移除原设计的 `MERCATOR_TILE_LIMIT = 200_000` 硬拦截。规模信息改由预估接口如实呈现(§4.10),用户自行判断。残留风险(磁盘写满、worker 槽位长时间占用、无任务级超时)已在 §4.10 末记录,**不额外加限制**。 |
| **Q4** | **Esri 服务级上限** | **从 z18 上调至 z19** | 依据 V3 实测:z19 是真实新增细节(高频能量比 z18 上采样高 33~46%),不是插值放大。`EsriImageryConfig.max_zoom = 19`。西部区域仍由 D15 的探测保护,不受影响。补充实测:z20 仅美国境内有数据(纽约/洛杉矶),亚欧城市止于 z19 —— 故 19 对国内使用是合适的天花板。 |

### 待实测校准的参数(不阻塞开工)

| # | 事项 | 暂定值 | 校准方式 |
|---|---|---|---|
| Q2 | mercator 影像的建议级别瓦片预算(§4.6 R6) | `tile_budget=8000` | 推断值。偏大则默认推荐级别过高、首次下载很慢;偏小则用户总要手动往上调。一期后按实际拼接耗时校准。清单第 12 条。 |
| Q3 | 预览并发 / 超时(§4.7) | `_PREVIEW_SEM=4`、`_PREVIEW_TIMEOUT=8s` | 推断值。本地代理性能差异大。清单第 9 条实测后回写。 |
| Q5 | `download.max_retries` 是否为代理调高(§3.9 V6) | 维持 3 | 代理链路会引入 TLS 重置(现有重试元组已覆盖)。若实测失败率明显高于直连,调到 5 或为代理 provider 单独配置。清单第 17 条。 |

> 三项都不阻塞开工。Q1/Q4 已由用户决定,不再开放。
