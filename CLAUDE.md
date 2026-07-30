# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

天地图下载处理工具:在网页地图上绘制范围，下载天地图 EPSG:4326 影像瓦片，自动拼接为带坐标的 GeoTIFF，并可导出 gdal2tiles geodetic TMS 瓦片包。B/S 架构、单机自用。后端 FastAPI + SQLite，前端 Vue3 + OpenLayers。

## 常用命令

```bash
# 启动后端（Windows，含虚拟环境激活与静态前端挂载）
start.bat

# 手动启动后端
.venv/Scripts/python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000

# 前端开发（frontendvue 目录下，Vite 开发服务器，代理 /api 与 /ws 到 8000 端口）
cd frontendvue && npm run dev        # http://localhost:5173

# 前端构建（产物落到 frontendvue/dist，后端会自动挂载它）
cd frontendvue && npm run build

# 重建 Python 虚拟环境（新机器）
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

访问入口:后端运行后打开 http://127.0.0.1:8000（生产/自用），或前端 dev 时用 5173 端口（带热更新）。

> 项目当前没有测试套件，也未配置 lint 工具。

## 架构要点

### 下载→拼接管线（核心数据流）

任务生命周期串联在 `backend/core/` 下，理解这条链路是理解全项目的关键:

1. **提交**(`api/tasks.py` → `models.create_task`):校验密钥/级别/范围 → `tiling.estimate_total_tiles` 预估瓦片数 → 按任务名 `reserve_output_dir` 锁定唯一输出目录 → 写库 → `task_queue.enqueue`。
2. **排队**(`core/queue.py`):进程内单 worker 顺序消费任务队列，`TaskQueue` 是全局单例；任务内部再做瓦片级并发。暂停/取消通过 `_control` dict 协作式控制（`request_pause`/`request_cancel`/`control_of`）。
3. **执行**(`core/runner.py::run_task`):逐级 `range_for_bbox` 计算瓦片区间 → `downloader.download_range` 并发下载 → `mosaic_to_geotiff` 拼接最大级别为 GeoTIFF → 可选 `clip_to_geometry` 裁剪、`reproject_geotiff` 重投影 → 可选 `export_tms` 导出 TMS 瓦片包。进度通过 `emit` 回调经队列广播给 WebSocket，限流 0.5s 落库一次。
4. **进度推送**(`api/ws.py`):前端连 `/ws/progress`，订阅队列广播，收 `progress`/`task` 消息。

### 天地图瓦片坐标方案（易错点）

天地图 `img_c`/`vec_c` 用 `TileMatrixSet="c"`（EPSG:4326 经纬度），**不是** Web 墨卡托:

- 原点左上角 (lon=-180, lat=90)，第 z 级列数 `2^z`、行数 `2^(z-1)`，单张瓦片经纬跨度 `360/2^z`。
- 行列换算集中在 `core/tiling.py`（`lonlat_to_tile`/`tile_bounds`/`range_for_bbox`）。改动坐标逻辑务必先读该文件顶部注释。
- 导出 TMS 时(`core/tms.py`)与 gdal2tiles geodetic 网格同构、无损直映射:gdal 级 `L = z-1`，`tx = col`，`ty = 2^(z-1)-1-row`（纵向翻转，因 TMS 行自南向北）。

### 断点续传与缓存

瓦片缓存在 `{cache_dir}/{provider.key}/{z}/{col}_{row}.{ext}`（默认 `data/tiles/`）。`downloader._one` 遇到已存在且非空的文件直接跳过，这是断点续传的基础。服务重启时 `models.reset_stale_running` 把残留 running/pending 任务标记为 paused，用户点「开始」即靠缓存续传。删除任务不清缓存（除非 `purge=True` 删导出目录）。

### 数据源抽象

`providers/base.py::TileProvider` 是抽象基类（`key`/`ext`/`bands`/`tile_url`）。当前只实现 `providers/tianditu.py`（img/vec/ter 三图层，t0~t7 子域名轮询）。下载器与拼接管线只依赖抽象接口，新增 DEM 等数据源时实现同一接口并在 `build_provider` 登记即可，无需改上层。

### 配置

`backend/config.py` 加载 `config.yaml`（缺失项回落 dataclass 默认值），密钥可用环境变量 `TIANDITU_TOKEN`/`TIANDITU_BASEMAP_TOKEN` 覆盖（不落盘）。`config.yaml` 已 gitignore，含真实密钥；示例见 `config.example.yaml`。`basemap_token` 与下载 `token` 分开是为分别控天地图配额，前端底图用前者、下载用后者。

### 数据库

标准库 `sqlite3`（不用 ORM），单表 `tasks`。`db.py` 用 `_MIGRATIONS` dict + `PRAGMA table_info` 手动做旧库加列迁移（SQLite 不支持 `ADD COLUMN IF NOT EXISTS`）。新增字段需同时改 `_SCHEMA`、`_MIGRATIONS`、`models` 的读写与 `_row_to_dict`。

### 前端结构（frontendvue）

Vue3 + Pinia + OpenLayers + TDesign。两个 store 分工:`stores/draw.js` 管选区（bbox/geometry/shape）、`stores/task.js` 管任务列表并持有 WebSocket 连接（`connectWs` 收进度增量更新，断线 2s 重连）。地图逻辑在 `composables/`（`useMap.js`/`mapController.js`）与 `components/MapView.vue`。坐标系定义在 `utils/crs.js`:用 proj4 注册 CGCS2000 3 度带（EPSG:4534–4554 不含带号 / 4513–4533 含带号），供输出 CRS 下拉。

> 存在两套前端:根目录 `frontend/`（旧版纯静态 OpenLayers 单页）与 `frontendvue/`（现用 Vue3 版）。`backend/main.py` 优先挂 `frontendvue/dist`，未构建时回退 `frontend/`。开发新功能改 `frontendvue/`。
