# 后端计算与 Web 服务进程隔离 — 设计文档

> 需求来源:用户反馈「正在处理数据（合并 tif）时，前端操作的请求接口都没有反应」
> 日期:2026-09-24
> 范围:后端进程模型重构。**不改前端**，不改 `runner*.py` 的业务逻辑。

## 1. 问题与目标

### 1.1 现状

后端是**单进程**架构：

```
uvicorn（单进程，start.bat 无 --workers）
  └─ TaskQueue._loop()          ← asyncio task，与 HTTP 请求同一个事件循环
       └─ run_task()
            └─ await asyncio.to_thread(executors[key], ctx)   ← 默认线程池
                 └─ rasterio → GDAL C 扩展
```

`asyncio.to_thread` 只解决「**IO 阻塞**事件循环」，**不解决 GIL 抢占**。GDAL 的 C 代码长时间不释放 GIL 时，事件循环拿不到 CPU，前端所有请求（含 WebSocket）一起挂起。

### 1.2 实测证据

用缓存中现成的 1600 张 z18 瓦片（拼成 10240×10240、270MB LZW），在后台线程跑心跳，观察心跳是否被饿死：

| 操作（270MB GeoTIFF） | 耗时 | 期间心跳次数 | **最大间隔** | 判定 |
|---|---|---|---|---|
| `build_overviews`（4 级金字塔） | 9.04s | **18 次** | **8267ms** | 独占 GIL |
| `reproject_geotiff`（4326→3857） | 52.00s | 1778 次 | **9497ms** | 独占 GIL |
| `clip_to_geometry`（按几何裁） | 8.81s | 121 次 | **4103ms** | 独占 GIL |
| 拼图逐行写瓦片（对照） | 9.98s | 462 次 | 53ms | 正常 |
| OSM 逐瓦片切片循环（8 线程，对照） | 38.19s | 1762 次 | 197ms | 线程争抢，非硬卡死 |

`mosaic_to_geotiff` 内部分段定位：

```
首行写入完成        0.67s  心跳  27 次  最大间隔    37ms
半程              10.36s  心跳 458 次  最大间隔    68ms   ← 写瓦片正常
全部瓦片写入完成     9.98s  心跳 462 次  最大间隔    53ms   ← 写瓦片正常
build_overviews完成 6.47s  心跳   1 次  最大间隔     0ms   ← 6.47 秒只被调度 1 次
```

### 1.3 用户反馈的第二类症状同样可归因

用户报告「切 TMS / OSM 瓦片时卡」。实测切片循环本身不阻塞（最大间隔 197ms），元凶是**切片前的前置步骤**：

- `runner.py::_stage_osm` 重建 OSM 源图时调 `mosaic_to_geotiff` → 内含 `build_overviews`
- `terrain_tiles.py:173,205` 对源图调 `build_overviews`

即：**同一类「GDAL 批量操作」，只是隐藏在别的阶段背后**。这说明「逐个包住已知的阻塞调用」是打地鼠式做法。

### 1.4 目标

1. **通用隔离**：所有计算跑在独立子进程，主进程只做调度。未来新增任何计算步骤自动免疫，无需再测再包。
2. **多任务并行**：当前队列是单 worker 串行，改为可并行执行多个任务。
3. **资源底线保护**：磁盘/内存不足时不再派发新任务，避免写爆盘。
4. **前端零改动**：API 与 WS 协议不变。

## 2. 核心设计决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | **常驻 worker 进程池，非每任务新建** | 实测 worker 就绪成本仅 0.45~1.1s（`import rasterio` 0.42s + PROJ 首载 0.03s；后端全量 import 1.06s），而任务本身是分钟级。常驻让后续任务零延迟。 |
| D2 | **worker 闲置 5 分钟后退出** | 复用避免重复付冷启动成本；超时释放内存，长期不开工具时不白占。 |
| D3 | **主进程一行 GDAL 都不碰** | 这是「通用隔离」的落点。若保留任何计算在主进程，就要持续判断哪些会阻塞。 |
| D4 | **`run_task(task_id, emit)` 签名不变** | 它本来就是干净的进程边界。子进程内直接调用它，现有 runner 测试可原样复用。 |
| D5 | **`task_queue` 对外 API 不变** | `enqueue`/`request_pause`/`request_cancel`/`control_of`/`clear_control`/`subscribe`/`broadcast` 签名保持，`api/tasks.py`（7 处调用）、`api/ws.py`、`main.py` **零改动**。 |
| D6 | **token 用「额度租约」而非各进程独立计数** | 见 §5。若各 worker 独立计数，N 个 worker 会把同一个 key 各用到 150 万上限，**合计超天地图当日配额**。 |
| D7 | **token 计数只在真实发起 HTTP 请求时发生** | 用户明确要求。现有实现已满足（`downloader.py:97` 缓存命中时 `return True`，早于第 101 行的 `tile_url()`），本设计将其固化为**契约**并加测试守护。 |
| D8 | **资源准入只拦「新派发」，不打断运行中任务** | 中途终止大拼接会留半成品，比慢一点糟糕得多。 |
| D9 | **引入 psutil 做资源监控** | 实测 `import psutil` 0.107s、`virtual_memory()` 0.267ms/次、`disk_usage()` 0.058ms/次。资源检查是"每个调度决策调几次"的量级，开销可忽略；跨平台且比 ctypes 调 Win32 代码更短。 |

## 3. 进程模型与职责边界

```
┌─────────────────── 主进程（Web 服务）───────────────────┐
│  FastAPI / uvicorn                                       │
│  ├─ 全部 /api/* 端点、/ws/progress                       │
│  ├─ TaskScheduler：任务分发 + 资源准入 + 暂停取消状态机   │
│  ├─ TokenPool（唯一权威计数）                             │
│  └─ 广播中心：把 worker 回传的进度推给 WebSocket          │
└────────┬──────────────────────────────┬──────────────────┘
         │ control_q[w] (投递 task_id)   │ event_q (回传事件)
         ▼                              ▲
┌────────────────┐  ┌────────────────┐
│  worker #1     │  │  worker #2     │   ← 常驻子进程，按需拉起 / 超时回收
│  run_task()    │  │  run_task()    │
│  GDAL/GIL 全在 │  │  GDAL/GIL 全在 │      这里阻塞不影响主进程
└────────────────┘  └────────────────┘
```

**worker 数量 ≠ 任务数量**。worker 是并行度上限（默认 2），任务是队列，第 3 个及以后的任务在队列等待空闲 worker，不新建进程。

### 改动边界

| 文件 | 改动 |
|---|---|
| `core/queue.py` | `TaskQueue` 从 `asyncio.Queue` 改为跨进程队列 + 池管理门面（对外 API 不变） |
| `core/runner*.py` | **仅** `should_stop()` 的取值来源：`task_queue.control_of(task_id)` → worker 本地镜像 dict |
| `db.py` | 补 `PRAGMA busy_timeout=5000` |
| `main.py` / `run_app.py` | lifespan 启动调度器；打包入口加 `multiprocessing.freeze_support()` |
| `config.yaml` | 新增 `worker:` 节（见 §8） |
| `requirements.txt` | 新增 `psutil` |

`api/tasks.py`、`api/ws.py`、`core/downloader.py`、`core/tms.py`、`core/osm.py`、`core/mosaic.py` 等**均不改动**。

## 4. 跨进程协议

### 4.1 通道设计

```
主进程 ──control_q[w1]──▶ worker #1     (run / pause / cancel / lease_grant / shutdown)
       ──control_q[w2]──▶ worker #2
       ◀──event_q────────  worker #1 ┐   (event / log / finished / lease_req)
       ◀────────────────  worker #2 ┘
```

**控制通道专属，事件通道共享**：共享的控制队列会产生路由歧义（主进程发「暂停 task1」，可能被 worker #2 抢到，而 task1 在 worker #1 上）。专属队列无此问题，且主进程天然知道每个 worker 在跑什么。事件方向无歧义，共享即可。

### 4.2 消息契约

全部为可 pickle 的基本类型（dict/str/int），**不允许传对象**。

| 方向 | kind | 载荷 | 说明 |
|---|---|---|---|
| 主→worker | `run` | `task_id` | 派发任务 |
| | `pause` / `cancel` | `task_id` | 协作式控制，写入 worker 本地镜像 |
| | `lease_grant` | `req_id, epoch, token, quota` | token 租约应答 |
| | `shutdown` | — | 空闲超时或退出时收工 |
| worker→主 | `event` | 原 `emit` 的 msg | 进度/状态，原样转发给 WS |
| | `log` | `level, msg, ts` | 转发进主进程环形缓冲 |
| | `finished` | `task_id` | worker 转入空闲，调度器据此回收 |
| | `lease_req` | `req_id, want` | 申请 token 额度 |

### 4.3 runner 侧的唯一改动

worker 起一个守护线程监听 `control_q`，收到 `pause`/`cancel` 就更新本地镜像：

```python
def should_stop() -> bool:
    return _local_control.get(task_id) in ("pause", "cancel")   # 原: task_queue.control_of(task_id)
```

现有 `_Stopped` 异常路径、`_handle_stop` 落库逻辑**一行不改**。

`provider` / `downloader` / `ctx` 内的回调均不可 pickle，故**不进子进程** —— worker 收到 `task_id` 后自己从 DB 读任务、重建这些对象。这与现有 `run_task` 开头的 `get_task(task_id)` 行为一致。

## 5. token 额度租约

### 5.1 问题

`use_token()` 被**每张瓦片**调用一次（`providers/tianditu.py:73` 的 `tile_url` 内），百万级任务调用百万次。若各 worker 独立持池，N 个 worker 会把同一个 key 各用到 150 万上限，**合计 N×150 万，直接超天地图当日配额**。

### 5.2 方案

```
worker: 本地租约用完 → event_q 发 lease_req(want=5000)
主进程: 用现有 TokenPool 原子扣 5000 → control_q 回 lease_grant(token, quota, epoch)
worker: 本地扣减 5000 次后，再申请下一批
任务结束: 归还剩余额度
```

| 指标 | 值 |
|---|---|
| IPC 次数 | 瓦片总数 ÷ 5000（百万级任务约 200 次，可忽略） |
| 计数精度 | 最坏超扣 `worker数 × 5000`（占日配额 0.67%），任务结束时归还 |
| 主进程改动 | `TokenPool` 逻辑不动，外包一层 `acquire_lease(n)` |

### 5.3 epoch 号

主进程发生「全池用尽 → 重置计数从头循环」时，worker 手上可能仍有未用完的租约。给每次重置加一个 epoch：

- `lease_grant` 携带当前 epoch
- worker 收到时若 epoch 与本地不符，**丢弃本地租约重新申请**
- 归还时携带 epoch，主进程据 `epoch != current` 弃置过期归还

防止过期租约的计数写到错误的周期上。

### 5.4 计数契约（D7 的落地）

**只有真实发起 HTTP 请求的瓦片才计数**：

| 场景 | 是否计数 | 依据 |
|---|---|---|
| 缓存命中（`use_cache=True` 且文件存在非空） | **否** | `downloader.py:97` 提前 return，未走到 `tile_url()` |
| 强制重下（`use_cache=False`） | 是 | 走到 `tile_url()` |
| 请求失败后重试 | 每次重试都计数 | 每次重试都真实发起了 HTTP 请求 |
| 返回空瓦片占位（`is_empty_tile`） | 是 | 请求本身成功，服务端已消耗配额 |
| 前端底图预览 | 否 | 用 `basemap_token`，走前端直连 |

此契约需**新增测试守护**，防止后续重构时被破坏（例如把 `tile_url()` 提到缓存判断之前）。

### 5.5 worker 侧计数实现

worker 不直接调 `token_pool.use_token()`，改调本地 `LeaseClient.use_token()`：从当前租约扣减，耗尽时同步阻塞向主进程申请下一批。对外接口与 `TokenPool.use_token()` 一致，故 `providers/tianditu.py` 无需改动（它接收的是 `TokenSource` 可调用对象）。

**租约中途到期**：主进程授予租约时，同时给出该 token 的**剩余可用额度** `quota = min(lease_size, max_requests - count)`。若 worker 用完租约时该 token 已接近日上限，worker 申请下一批，主进程的 `_advance_to_available` 会自动切到下一个可用 key —— 复用现有轮询逻辑，无需新代码。

**粒度说明**：一次租约授予**单个 token** 的额度，不做多 key 混合租约。理由是与现有 `_advance_to_available` 的轮询语义一致，避免两套切换逻辑。

## 6. 调度与资源准入

### 6.1 调度器职责

主进程的 `TaskScheduler` 取代原单 worker 循环：

1. **队列管理**：`pending` → 就绪（资源够 + 有空闲 worker）→ 派发
2. **资源准入**：剩余磁盘/内存低于阈值 → 不派发（任务留在队列，**不是失败**）
3. **worker 生命周期**：空闲超时回收、崩溃检测、任务状态修复

### 6.2 资源准入规则

| 检查项 | 配置项 | 默认值 | 不满足时 |
|---|---|---|---|
| 剩余磁盘 | `worker.min_free_disk_gb` | 5 GB | 不派发，前端提示「磁盘不足，等待释放」 |
| 剩余内存 | `worker.min_free_mem_gb` | 2 GB | 同上 |
| 并行数 | `worker.max_parallel_tasks` | 2 | 排到队列等待 |

**已在跑的任务不受影响**（D8）。

> 后续增量（不在本轮范围）：提交时用 `estimate_total_tiles` 预估产出，若 > 剩余磁盘直接拒绝提交。比全局阈值精准。

### 6.3 暂停 / 取消完整链路

```
前端 POST /api/tasks/{id}/pause
  → 主进程 Scheduler 记录状态 + 查该任务所在 worker
  → control_q[worker] 发 {kind:"pause", task_id}
  → worker 守护线程写入本地镜像 dict
  → runner 的 should_stop() 读到 → 抛 _Stopped
  → 现有 _handle_stop 落库并 emit（一行不改）
  → event_q 回传 → 主进程广播给 WS
```

任务**尚未派发**时点暂停：主进程直接改状态，不发往任何 worker（worker 不知道该任务存在）。

## 7. 日志与数据库

### 7.1 日志

worker 侧装 forwarding handler：**不写文件、不碰环形缓冲**，只把日志塞进 `event_q`；主进程统一落文件 + 推 WS。

原因：`core/logs.py` 的环形缓冲是进程内 `deque`，worker 内的日志主进程看不到；多进程同时写同一日志文件会造成行交错。

### 7.2 SQLite

`db.py` 现只设 `journal_mode=WAL`。WAL 支持多进程读写，但**并发写仍会抛 `database is locked`**。补 `PRAGMA busy_timeout=5000`（写操作排队重试而非直接失败）。

## 8. 配置变更

`config.yaml` 新增 `worker:` 节，`backend/config.py` 加对应 dataclass：

```yaml
worker:
  # 并行执行的任务数上限
  max_parallel_tasks: 2
  # 空闲多久后回收 worker 进程(秒)
  idle_timeout: 300
  # 剩余磁盘低于此值时不再派发新任务(GB)
  min_free_disk_gb: 5
  # 剩余内存低于此值时不再派发新任务(GB)
  min_free_mem_gb: 2
  # token 额度租约大小(每次向主进程申请的瓦片请求额度)
  token_lease_size: 5000
```

## 9. 错误处理与生命周期

| 场景 | 检测方式 | 处置 |
|---|---|---|
| worker 空闲超时 | 自身计时 | 主动发 `shutdown` 后正常退出，调度器移除并按需再拉 |
| worker 崩溃（OOM/异常） | 调度器心跳检查 `is_alive()` | 该任务置 `paused`，message 标注「worker 异常退出」，用户点开始靠缓存续传 |
| worker 启动失败 | 启动超时 | 记录日志并降级提示，不阻塞服务启动 |
| 主进程退出 | lifespan 的 `finally` | 先发 `shutdown` 等 worker 收工，超时则 `terminate()` |
| 子进程内任务抛异常 | 现有 `run_task` 的 try/except | 行为不变，状态经 `event_q` 回传 |

## 10. 测试策略

各单元独立可测。**关键设计决策（D5）：`task_queue` 对外 API 不变，因此现有 160+ 用例中涉及 API 层的部分无需改动。**

| 单元 | 位置 | 测法 |
|---|---|---|
| `TaskScheduler` | 主进程 | mock worker 池，单测派发/准入/状态机，**不真起进程** |
| `WorkerPool` | 主进程 | 起 1 个 worker，验证生命周期、崩溃检测、空闲回收 |
| `WorkerRuntime` | 子进程内 | 直接调 `run_task(task_id, emit)`，**现有 runner 测试原样复用** |
| token 租约 | 两侧 | 主进程测原子扣减与 epoch 弃置；worker 测额度耗尽后申请 |
| 计数契约（D7） | 下载器 | **新增**：缓存命中不调 `use_token`、强制重下调用、重试多次计数 |
| 协议消息 | 两侧 | 每个 kind 的编解码 + 非法输入 |

新增模块（边界清晰、各司其职）：

```
core/queue.py          保留 task_queue 单例 = 门面（API 兼容）
core/scheduler.py      新增 TaskScheduler：派发 + 准入 + 状态机
core/worker_pool.py    新增 WorkerPool：进程生命周期
core/worker_runtime.py 新增 worker 内主循环 + 控制监听线程
core/token_lease.py    新增租约客户端/服务端
```

## 11. 验收标准

| 指标 | 改造前（实测） | 目标 | 测法 |
|---|---|---|---|
| 任务运行期间 `GET /api/tasks` | 存在 8s+ 完全无响应窗口 | P99 < 1s，无 > 3s 停顿 | 跑大拼接任务 + 每 200ms 轮询接口，记录延迟序列 |
| 任务运行期间 WS 进度推送 | 停顿 8s+ | 无 > 3s 断流 | 记录消息到达间隔 |
| 并行任务数 | 恒为 1 | 2 个任务同时 `running` | 连续提交 2 个任务，查 DB 状态 |
| 暂停 / 取消响应 | 立即 | < 2s 生效 | 点暂停后 2s 内落库为 `paused` |
| **阶段 1 产出等价** | — | 与改造前**逐字节一致** | 同样输入跑两次，比对文件哈希 |

第一条为核心指标，直接对应原始症状。改造前先跑一次基线存档，便于前后对比。

## 12. 实施顺序与风险

| 阶段 | 内容 | 验证方式 |
|---|---|---|
| **1** | 进程隔离，**worker 数固定为 1**（不做并行） | 跑全量现有测试 + 真实大拼接任务，**产出与改造前一致** |
| **2** | 并行（worker 数可配）+ token 租约 | 2 任务并行测试 + 租约计数测试 + 计数契约测试 |
| **3** | 资源准入 + psutil | 模拟低磁盘，验证拒绝派发且任务不失败 |
| **4** | 打包验证 | 构建 exe，真机双击运行跑一个任务 |

**阶段 1 风险最高**（改动最大、行为必须完全等价），故单独成阶段，验收标准是「产出与改造前一致」而非「看起来能跑」。

### 已知坑（实施时逐条核对）

| 坑 | 后果 | 处置 |
|---|---|---|
| `multiprocessing.freeze_support()` 漏加 | 打包后子进程无限重启 | 阶段 4 必测；本次调研中已实际踩到（子进程反复重启导致测试卡死 5 分钟） |
| 消息含不可 pickle 对象 | worker 启动即崩 | 契约只允许 dict/str/int；provider 由 worker 自己从 DB 重建 |
| Windows spawn 下子进程 `__main__` 不可导入 | 子进程反复重启 | worker 入口放独立模块，不放脚本顶层 |
| SQLite 并发写 | `database is locked` | `PRAGMA busy_timeout=5000` |
| worker 内日志进不了环形缓冲 | 前端「日志」抽屉空白 | forwarding handler 转发到主进程 |
| 磁盘被并行任务写满 | 任务失败且留半成品 | 准入阈值 5GB + 不打断运行中任务 |
| `token_pool` 模块级单例被 worker 二次实例化 | 配额统计失真 | worker 不 import `token_pool`，改用 `LeaseClient` |

## 13. 影响面

| 项 | 影响 |
|---|---|
| 前端 | **零改动**（API 与 WS 协议不变） |
| `runner*.py` 业务逻辑 | **零改动**（仅 `should_stop` 取值来源） |
| 打包产物 | 体积略增（psutil）；`onedir` 模式下 worker 复用同一份依赖，不翻倍 |
| 内存 | 空闲 worker 常驻 **60 MB**（实测：裸解释器 18.2 MB → +rasterio 44.6 MB → +psutil/numpy/aiohttp 60.1 MB）。常驻 worker 不随任务结束释放，空闲 5 分钟后回收。执行期峰值随图像尺寸增长（拼接按「瓦片行」分块，单行缓冲随图宽线性增长），由准入阈值兜底 |
| 磁盘 | 无新增占用；并行会加速成果产出导致消耗变快，由准入阈值兜底 |
