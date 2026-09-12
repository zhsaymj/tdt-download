# 三维数据处理集成开发计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在当前天地图下载处理工具中新增本地三维数据处理能力，一期支持三条链路：OSGB→3D Tiles、LAS/LAZ→3D Tiles、LAS/LAZ→DEM/DSM GeoTIFF，复用现有任务队列、阶段进度、日志与 Cesium 预览。

**Architecture:** 外部开源处理器适配层（`backend/core/processors/`，每个工具一个 adapter，一期仅 CLI 调用，不做源码深嵌）+ 独立三维管线 `backend/core/runner_3d.py`（仿照 `runner_buildings.py` 阶段化设计，不进栅格管线）+ `formats.py` 注册表新增三维 DataKind/ExportStage/Provider。外部二进制路径走 `config.yaml` 的 `tools.*`，约定随包分发到 `tools/` 目录。

**Tech Stack:** FastAPI + sqlite3（标准库）、Vue3 + Pinia + OpenLayers + Cesium + TDesign、fanvanzh/3dtiles（OSGB→3D Tiles CLI）、PDAL（LAS→DEM/DSM pipeline）、py3dtiles（LAS→3D Tiles Python 包）。

**功能边界（一期不做）：** LAS→OSGB；OSGB 直接提取 DEM/DSM；dsm2dtm；对任何外部工具做源码 fork/库化（二期按转换效果痛点再评估）。

---

## 关键集成点速查

- `backend/core/formats.py` — DataKind / STAGES / PROVIDER_KIND / `plan_stages()`；管线常量现有 `PIPE_RASTER`、`PIPE_BUILDING`，新增 `PIPE_3D = "3d"`。
- `backend/core/runner_buildings.py` — runner_3d.py 的模板：阶段化、`_Stopped` 暂停/取消、成果命名函数集中、中间文件落盘可恢复。
- `backend/models.py` — `TaskCreate` pydantic 模型 + `create_task` 调 `build_stage_defs`。
- `backend/db.py` — `_SCHEMA` + `_MIGRATIONS` + `PRAGMA table_info` 手动加列。
- `backend/config.py` — dataclass 配置 + `_merge` + `_CONFIG_TEMPLATE`；新增 `ToolsConfig`。
- `backend/api/local.py` — `_require_local` 回环校验、`_checked_path` 目前仅接受文件，OSGB 目录需扩展。
- `backend/core/file_dialog.py` — tkinter 对话框，目前只有 `askopenfilename`，需加 `askdirectory`；patterns 需加 LAS/LAZ。
- `frontendvue/src/components/ProcessDialog.vue` — `props.source.kind` 现有 `download | local_raster | local_vector`，新增 `local_3d`。
- `frontendvue/src/utils/provider.js` — `taskKindOf()` 三色判定，新增三维类型。
- `frontendvue/src/preview/PreviewApp.vue` — 已有 `Cesium3DTileset.fromUrl(${base}/3dtiles/tileset.json)` 与 `stageDone('tile_3d')`，三维任务直接复用，原则上不改。
- 输出约定：`output/{任务名}/3dtiles/tileset.json`、`output/{任务名}/{任务名}_dem.tif`、`{任务名}_dsm.tif`（与 runner_buildings 的命名风格一致）。

---

### Task 1: 配置层 — ToolsConfig 与工具诊断接口

外部处理器路径全部走 config，启动时可诊断。

**Files:**
- Modify: `backend/config.py`
- Modify: `config.example.yaml`
- Test: `tests/test_tools_config.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_tools_config.py
import unittest
from backend.config import load_config

class ToolsConfigTest(unittest.TestCase):
    def test_default_tools_paths_empty(self):
        cfg = load_config(None)  # 不存在的路径 -> 全默认
        self.assertEqual(cfg.tools.tiles3d_exe, "")
        self.assertEqual(cfg.tools.pdal_exe, "")
        self.assertEqual(cfg.tools.py3dtiles_python, "")
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_tools_config -v
```

Expected: FAIL（`load_config` 无 `tools` 属性 / 签名不符）

- [ ] **Step 3: 在 backend/config.py 新增 ToolsConfig**

```python
@dataclass
class ToolsConfig:
    tiles3d_exe: str = ""        # fanvanzh/3dtiles 可执行文件路径（tools/3dtiles/3dtiles.exe）
    pdal_exe: str = ""           # pdal CLI 路径
    py3dtiles_python: str = ""   # 装有 py3dtiles 的 python 解释器（独立 venv，避免污染主环境）
    dsm2dtm_python: str = ""     # 预留，一期不用
```

> 字段名不用 `3dtiles_exe` 是因为 Python 标识符不能数字开头。yaml 键与字段同名（`tools.tiles3d_exe` 等）。在 `Config` dataclass 加 `tools: ToolsConfig = field(default_factory=ToolsConfig)`，`_CONFIG_TEMPLATE` 补 `tools:` 段注释示例。

- [ ] **Step 4: config.example.yaml 同步**

```yaml
tools:
  tiles3d_exe: ""        # 例如 tools/3dtiles/3dtiles.exe
  pdal_exe: ""           # 例如 tools/pdal/bin/pdal.exe
  py3dtiles_python: ""   # 例如 tools/py3dtiles-venv/Scripts/python.exe
```

- [ ] **Step 5: 新增诊断接口 `GET /api/tools/diagnose`**

在 `backend/api/` 新路由（可挂到 `api/local.py` 或新建 `api/tools.py`）：逐项检查配置路径存在性 + `--version`/`-h` 试跑（超时 5s），返回 `{name: {configured, path, exists, runnable, version, error}}`。前端提交三维任务前调用。

- [ ] **Step 6: 测试通过 + 诊断接口手工验证**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_tools_config -v
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
# 浏览器/curl 访问 http://127.0.0.1:8000/api/tools/diagnose
```

- [ ] **Step 7: Commit**

```powershell
git add backend/config.py config.example.yaml backend/api/ tests/test_tools_config.py
git commit -m "feat: add tools config and diagnose endpoint for external 3D processors"
```

---

### Task 2: formats.py 注册三维类型

**Files:**
- Modify: `backend/core/formats.py`
- Test: `tests/test_formats_3d.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_formats_3d.py
import unittest
from backend.core.formats import DataKind, STAGES, PROVIDER_KIND, plan_stages

class Formats3DTest(unittest.TestCase):
    def test_new_datakinds_exist(self):
        self.assertEqual(DataKind.MESH_OSGB, "mesh_osgb")
        self.assertEqual(DataKind.POINT_CLOUD, "point_cloud")
        self.assertEqual(DataKind.TILES_3D_MODEL, "tiles_3d_model")
        self.assertEqual(DataKind.TILES_3D_POINT, "tiles_3d_point")

    def test_local_providers_registered(self):
        self.assertEqual(PROVIDER_KIND["local_osgb"], DataKind.MESH_OSGB)
        self.assertEqual(PROVIDER_KIND["local_pointcloud"], DataKind.POINT_CLOUD)

    def test_plan_stages_osgb(self):
        stages = [s.key for s in plan_stages("local_osgb", {"tile_3d"})]
        self.assertEqual(stages, ["convert_3d"])

    def test_plan_stages_pointcloud(self):
        stages = [s.key for s in plan_stages("local_pointcloud", {"dem", "dsm", "tile_3d"})]
        self.assertEqual(stages, ["pc_dem", "pc_dsm", "pc_tile_3d"])  # 按 order 排序

    def test_no_key_conflict_with_existing(self):
        # dem/tile_3d 已被栅格/建筑管线占用(ExportStage 单实例、pipeline 字段互斥),三维必须另起 key
        self.assertEqual(STAGES["dem"].pipeline, "raster")
        self.assertEqual(STAGES["tile_3d"].pipeline, "building")
```

- [ ] **Step 2: 确认失败**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_formats_3d -v
```

- [ ] **Step 3: formats.py 修改**

- `DataKind` 新增：`MESH_OSGB = "mesh_osgb"`、`POINT_CLOUD = "point_cloud"`、`TILES_3D_MODEL = "tiles_3d_model"`、`TILES_3D_POINT = "tiles_3d_point"`。
- 新增管线常量 `PIPE_3D = "3d"`。
- `STAGES` 新增（均 `pipeline=PIPE_3D`、`needs_tile_cache=False`、`needs_levels=False`）。**现有 `dem`（高程 GeoTIFF，PIPE_RASTER）与 `tile_3d`（b3dm，PIPE_BUILDING）key 已被占用，ExportStage 是单实例且 pipeline 字段互斥，不能复用，三维一律另起 key：**
  - `convert_3d`（"转换 3D Tiles"，accepts MESH_OSGB，produces TILES_3D_MODEL，outputs `3dtiles/`，order=10，default_on=True）
  - `pc_dsm`（"生成 DSM"，accepts POINT_CLOUD，produces RASTER_DEM，outputs `{name}_dsm.tif`，order=10，default_on=True）
  - `pc_dem`（"生成 DEM"，accepts POINT_CLOUD，produces RASTER_DEM，outputs `{name}_dem.tif`，order=20，default_on=True）
  - `pc_tile_3d`（"切 3D Tiles(pnts)"，accepts POINT_CLOUD，produces TILES_3D_POINT，outputs `3dtiles/`，order=30，default_on=True）
- `_FORMAT_TO_STAGE` 新增 `DataKind.MESH_OSGB: {"tile_3d": "convert_3d"}` 与 `DataKind.POINT_CLOUD: {"dsm": "pc_dsm", "dem": "pc_dem", "tile_3d": "pc_tile_3d"}`；`ALL_FORMAT_NAMES` 自动覆盖，需确认 `"dsm"` 加入白名单。
- `PROVIDER_KIND` 新增：`"local_osgb": DataKind.MESH_OSGB`、`"local_pointcloud": DataKind.POINT_CLOUD`；`LOCAL_PROVIDERS` 追加二者。
- `plan_stages()` 确认对新 provider 按 export 集合过滤即可，无需特判。

- [ ] **Step 4: 测试通过**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_formats_3d tests.test_format_defaults -v
```

- [ ] **Step 5: Commit**

```powershell
git add backend/core/formats.py tests/test_formats_3d.py
git commit -m "feat: register 3D data kinds, stages and local providers in formats registry"
```

---

### Task 3: 任务模型与数据库字段

三维任务需要记录：输入路径（复用 `source_path`）、输入是目录还是文件、点云 CRS 处理策略（`pc_crs`，如 `EPSG:4547` 或 `local`）、DEM/DSM 栅格分辨率（`pc_resolution`，默认空=按点云密度自适应）、LAS→3DTiles 是否保留分类/颜色（一期可不做参数，先固定全开）。

**Files:**
- Modify: `backend/db.py`
- Modify: `backend/models.py`
- Test: `tests/test_models_3d.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_models_3d.py
# 验证 TaskCreate 接受 pc_crs/pc_resolution，create_task 落库后 _row_to_dict 可读回
```

- [ ] **Step 2: 确认失败**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_models_3d -v
```

- [ ] **Step 3: db.py 加迁移**

`_SCHEMA` 的 CREATE TABLE 与 `_MIGRATIONS` dict 同步加：
- `pc_crs TEXT NOT NULL DEFAULT ''`（空=自动读 LAS 头；`local`=按本地坐标；否则为 EPSG 码）
- `pc_resolution REAL NOT NULL DEFAULT 0`（0=自动）

- [ ] **Step 4: models.py**

`TaskCreate` 加同名可选字段；`create_task`/`update_task`/`_row_to_dict` 同步读写。`create_task` 中对 `local_osgb`/`local_pointcloud` provider 跳过 `estimate_total_tiles` 与级别校验（参考现有 local_image/local_dem 的处理分支，照抄其跳过逻辑）。

- [ ] **Step 5: 测试通过（含旧库迁移用例：临时建一个无新列的 db，走 migrate 后列存在）**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_models_3d -v
```

- [ ] **Step 6: Commit**

```powershell
git add backend/db.py backend/models.py tests/test_models_3d.py
git commit -m "feat: add point-cloud task fields (pc_crs, pc_resolution) with db migration"
```

---

### Task 4: processors 适配层基类

**Files:**
- Create: `backend/core/processors/__init__.py`
- Create: `backend/core/processors/base.py`
- Test: `tests/test_processors_base.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_processors_base.py
# 验证：ProcessorError 归一化（exit code/ stderr 截断/中文消息）；
# run_cli 支持取消事件（threading.Event 置位后进程被 terminate）；
# stdout 行回调能驱动进度解析。
```

- [ ] **Step 2: 确认失败**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_processors_base -v
```

- [ ] **Step 3: base.py 实现**

```python
class ProcessorError(Exception):
    """工具名 + 退出码 + 截断后的 stderr + 面向用户的中文提示"""

@dataclass
class ProcResult:
    ok: bool
    outputs: list[str]      # 产物路径（已做存在性/非空检查）
    message: str = ""

def run_cli(cmd, *, cwd=None, cancel_event=None, on_stdout_line=None,
            on_stderr_line=None, timeout=None) -> ProcResult:
    """subprocess.Popen 封装：
    - CREATE_NO_WINDOW（Windows）
    - 逐行读 stdout/stderr 回调（供进度解析与日志透传）
    - cancel_event 置位 -> terminate -> 等 5s -> kill，抛 _Stopped 语义由调用方转换
    - 非零退出 -> ProcessorError（stderr 末尾 2000 字符）
    """
```

另定义 `BaseProcessor` 抽象：`name`、`check_available(cfg) -> (bool, msg)`、`build_cmd(...)`、`parse_progress(line) -> float|None`、`expected_outputs(out_dir) -> list[Path]`、`run(...)`。

- [ ] **Step 4: 测试通过（用一个会打印进度的 python -c 假命令做替身）**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_processors_base -v
```

- [ ] **Step 5: Commit**

```powershell
git add backend/core/processors tests/test_processors_base.py
git commit -m "feat: add processor adapter base with cancel-aware cli runner"
```

---

### Task 5: OSGB→3D Tiles adapter（fanvanzh/3dtiles）

**Files:**
- Create: `backend/core/processors/osgb_to_3dtiles.py`
- Test: `tests/test_osgb_to_3dtiles.py`

- [ ] **Step 1: 失败测试**

命令构造与产物检测纯函数可测（不真跑 exe）：

```python
def test_build_cmd(tmp):
    p = OsgbTo3dTiles(exe="tools/3dtiles/3dtiles.exe")
    cmd = p.build_cmd(input_dir="D:/osgb_sample", out_dir="out/3dtiles")
    assert cmd[0].endswith("3dtiles.exe")
    assert "-i" in cmd and "D:/osgb_sample" in cmd
    assert "-o" in cmd and "out/3dtiles" in cmd

def test_expected_outputs_requires_tileset():
    # 产物必须含 3dtiles/tileset.json 且至少一个 .b3dm，否则判失败
```

> 注意：fanvanzh/3dtiles 实际参数为 `-i <osgb目录> -o <输出目录>`（以其 README 为准，实现时先 `-h` 确认再定稿命令构造）。metadata.xml 坐标处理：若输入目录含 `metadata.xml` 必须一并传入/保留在同目录（fanvanzh 会自动读取），adapter 的 `preflight()` 检查 metadata.xml 缺失时给中文警告（坐标可能按本地方处理）。

- [ ] **Step 2: 确认失败**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_osgb_to_3dtiles -v
```

- [ ] **Step 3: 实现 adapter**

`check_available`：路径存在 + `-h` 可跑。`parse_progress`：fanvanzh 输出 `converting x/y` 类行（实现时以真实输出正则为准，解析不到就按文件计数兜底：扫描 out_dir 下 b3dm 数量 / 预估总数）。`expected_outputs`：`{out}/tileset.json` 存在且非空、至少 1 个 `.b3dm`。

- [ ] **Step 4: 测试通过**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_osgb_to_3dtiles -v
```

- [ ] **Step 5: 真实样例手工验证（技术验证顺序第 1 步）**

准备小型 OSGB 样例目录 → 配置 `tools.tiles3d_exe` → 手动调 adapter 跑出 `tileset.json` → 启动后端，在 PreviewApp 用现有 `Cesium3DTileset.fromUrl` 加载（可临时把产物拷到某个已完成建筑任务的 output 下验证，或等 Task 8 管线完成后走正式路径）。验证点：坐标不飘、高程正确、纹理不丢、加载流畅。

- [ ] **Step 6: Commit**

```powershell
git add backend/core/processors/osgb_to_3dtiles.py tests/test_osgb_to_3dtiles.py
git commit -m "feat: add fanvanzh/3dtiles adapter for osgb to 3d tiles"
```

---

### Task 6: LAS/LAZ→DEM/DSM adapter（PDAL pipeline）

**Files:**
- Create: `backend/core/processors/las_to_dem.py`
- Test: `tests/test_las_to_dem.py`

- [ ] **Step 1: 失败测试**

```python
def test_build_pipeline_dsm():
    p = LasToDem(pdal_exe="pdal")
    pipe = p.build_pipeline(input="a.las", output="out/a_dsm.tif",
                            kind="dsm", resolution=1.0, crs="EPSG:4547")
    # pipeline JSON 含 writers.gdal，gdal 的 output_type=max（DSM）；
    # dem 则含 filters.smrf/ground 分类 + output_type=idw/mean
    d = json.loads(pipe)
    assert d["pipeline"][-1]["type"] == "writers.gdal"
```

- [ ] **Step 2: 确认失败**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_las_to_dem -v
```

- [ ] **Step 3: 实现**

- pipeline 生成：DSM = `readers.las` → `writers.gdal`（`output_type: max`, `resolution`, `nodata`, 可选 `filters.fillpings`/空洞填充）；DEM = `readers.las` → `filters.smrf`（地面分类）→ `filters.range`（Classification[2:2]）→ `writers.gdal`（`output_type: idw` 或 `mean`）。
  > 实施订正（Task 6 审查核实）：PDAL 无 `filters.fillpings`，不臆造；空洞填充后续可用 `writers.gdal` 的 `window_size`。
- CRS：input LAS 头部有 SRS 则沿用；任务 `pc_crs` 非空时写 `readers.las.override_srs`/`writers.gdal` 的 srs。`pc_crs == "local"` 则不加 srs 并给警告。
  > 实施订正：只写 `readers.las.override_srs`（官方注明输出 SRS 自动继承自输入，writer 无需另设）。
- 把 pipeline JSON 写临时文件，`pdal pipeline tmp.json` 执行；`--nostream` 视版本决定（实现时确认）。
- `preflight`：`pdal info --summary` 读 LAS 头，返回点数/bbox/SRS 供提交前校验接口复用。
- 产物检测：输出 tif 存在、非空、可被 rasterio 打开（环境已有 rasterio/GDAL）。

- [ ] **Step 4: 测试通过**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_las_to_dem -v
```

- [ ] **Step 5: 真实样例手工验证（技术验证顺序第 2 步）**

小型 LAS/LAZ → 输出 DSM/DEM GeoTIFF → 走现有「本地 DEM 导入」流程（local_dem provider）验证 terrain/contour 阶段可用、坐标落点正确。

- [ ] **Step 6: Commit**

```powershell
git add backend/core/processors/las_to_dem.py tests/test_las_to_dem.py
git commit -m "feat: add PDAL pipeline adapter for las to dem/dsm geotiff"
```

---

### Task 7: LAS/LAZ→3D Tiles adapter（py3dtiles）

**Files:**
- Create: `backend/core/processors/las_to_3dtiles.py`
- Test: `tests/test_las_to_3dtiles.py`

- [ ] **Step 1: 失败测试**

命令构造 + 产物检测（tileset.json + 至少一个 .pnts）。py3dtiles 通过 `tools.py3dtiles_python` 解释器调用：`python -m py3dtiles convert ...`（以其实际 CLI 为准，实现时先 `python -m py3dtiles -h` 确认）。
> 实施订正（Task 7 spec 审查经上游 v12.1.1 源码核实）：包内无 `__main__.py`，正确形式是 `python -m py3dtiles.command_line convert <输入> --out <输出目录>`；另核实到两条必须带入 Task 8 的事实：①不传 `--srs_out 4978` 产物不重投影、Cesium 落点错误（缺 SRS 的 LAS 还需 `--srs_in`）——**Task 8 须把 `--srs_in/--srs_out 4978` 接到任务 pc_crs 字段**（三态语义参照 las_to_dem）；②py3dtiles 无增量续传、非空输出目录报 FileExistsError——**Task 8 重跑 pc_tile_3d 阶段前须清空阶段输出目录**。

- [ ] **Step 2: 确认失败 → Step 3: 实现 → Step 4: 测试通过**

进度解析：py3dtiles 按文件/点数打印，解析不到就按输出目录 pnts 计数兜底。颜色/分类字段保留为固定默认（一期不暴露参数）。

- [ ] **Step 5: 真实样例手工验证（技术验证顺序第 3 步）**

同一点云分别转 3D Tiles，记录输出体积、加载速度、坐标落点、颜色/分类保留情况；若 py3dtiles 性能不可接受，登记 issue 备查 point-tiler/gocesiumtiler（二期替换 adapter 即可，管线不变）。

- [ ] **Step 6: Commit**

```powershell
git add backend/core/processors/las_to_3dtiles.py tests/test_las_to_3dtiles.py
git commit -m "feat: add py3dtiles adapter for las to 3d tiles"
```

---

### Task 8: runner_3d.py 独立管线

**Files:**
- Create: `backend/core/runner_3d.py`
- Modify: `backend/core/queue.py`（按 provider 分发到 runner_3d，参考 runner_buildings 的接入方式）
- Test: `tests/test_runner_3d.py`

- [ ] **Step 1: 失败测试**

mock 三个 processor，验证：local_osgb 任务只跑 convert_3d 阶段；local_pointcloud 按 export 集合跑 pc_dem/pc_dsm/pc_tile_3d；`_Stopped` 时落 paused；产物缺失时阶段 fail 且任务 failed。

- [ ] **Step 2: 确认失败**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_runner_3d -v
```

- [ ] **Step 3: 实现（仿 runner_buildings.py）**

- 成果命名函数集中：`tiles3d_output(task)` → `output/{name}/3dtiles/tileset.json`；`dem_output(task)` → `{name}_dem.tif`；`dsm_output(task)` → `{name}_dsm.tif`。
- 每阶段：`stage.start` → preflight（工具可用性/输入存在性，失败给中文错误）→ adapter.run（cancel_event 接队列 `control_of`）→ 产物检测 → `stage.finish`。
- 阶段进度：adapter `parse_progress` 回报百分比 → StageTracker.update；解析不到给 indeterminate + 日志行透传。
- 输入为目录的点云：runner 负责枚举 `*.las/*.laz` 列表，逐文件处理（多文件 DEM 时先各自栅格化再 rasterio merge，或 merge 到同一 writers.gdal——实现时以 PDAL 多输入支持为准）。
- queue.py 分发：`provider in ("local_osgb", "local_pointcloud")` → `runner_3d.run_task`。

- [ ] **Step 4: 测试通过**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_runner_3d -v
```

- [ ] **Step 5: Commit**

```powershell
git add backend/core/runner_3d.py backend/core/queue.py tests/test_runner_3d.py
git commit -m "feat: add runner_3d pipeline dispatching osgb/pointcloud tasks"
```

---

### Task 9: 本地文件选择扩展（目录 + 点云 + 提交前检查）

**Files:**
- Modify: `backend/core/file_dialog.py`
- Modify: `backend/api/local.py`
- Test: `tests/test_local_pick_3d.py`

- [x] **Step 1: 失败测试**

- `PickReq.kind == "dir"` → 走 `askdirectory` 分支（mock tkinter）。
- `_checked_path` 支持目录（OSGB）：存在且是目录、内含至少一个 `.osgb`（**实现为全递归 rglob**：真实 ContextCapture 结构为 `Data/Tile_*/ *.osgb` 需 ≥2 层，规格"递归一层"不够用）→ ok；缺 `metadata.xml` → 返回 warning 字段。
- 新接口 `POST /api/local/inspect_pointcloud`：body 为路径，调 LasToDem.preflight 返回 `{files, count, bbox, srs, error}`；CRS 缺失时 `srs=null` 前端提示选择 EPSG 或本地坐标。

- [x] **Step 2: 确认失败**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_local_pick_3d -v
```

- [x] **Step 3: 实现**

- file_dialog.py：`POINTCLOUD_PATTERNS = [("LAS/LAZ 点云", "*.las *.laz")]`；`pick(kind)` 分发 `file|dir`；`dir` 用 `tk.filedialog.askdirectory`。
- local.py：`PickReq.kind` Literal 加 `"dir"` 与 `"pointcloud"`（后者为 POINTCLOUD_PATTERNS 的唯一消费方，Task 10 前端点云多选必需）；`_checked_path` 拆 `_checked_file` / `_checked_dir`；新增 osgb 目录检查与点云 inspect 接口（均 `_require_local` 保护）。
- 保留手输路径兜底（现有输入框直填路径仍可用）。

- [x] **Step 4: 测试通过**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_local_pick_3d -v
```

- [x] **Step 5: Commit**

```powershell
git add backend/core/file_dialog.py backend/api/local.py tests/test_local_pick_3d.py
git commit -m "feat: support directory/osgb and las inspect in local pick apis"
```

---

### Task 10: 前端 ProcessDialog 三维来源

**Files:**
- Modify: `frontendvue/src/components/ProcessDialog.vue`
- Modify: `frontendvue/src/api/*`（补 localPick dir 与 inspectPointCloud 封装，按现有 api 模块结构放）
- Modify: `frontendvue/src/utils/taskDefaults.js`
- Test: `frontendvue/src/components/ProcessDialog.local3d.test.js`（新建）

- [ ] **Step 1: 失败测试（源码字符串断言风格，参照 ProcessDialog.reset.test.js）**

```js
// 断言：sourceOptions 含 local_3d；buildPayload 对 local_osgb 带 source_path+export={tile_3d:true}；
// local_pointcloud 的 export 面板显示 tile_3d/dsm/dem 三项
```

- [ ] **Step 2: 确认失败**

```powershell
cd frontendvue; node --test src/components/ProcessDialog.local3d.test.js
```

- [ ] **Step 3: 实现**

- `props.source.kind` 支持 `"local_3d"`；来源内再选数据类型：OSGB 目录 / 点云文件或目录（radio）。
- `browse()`：OSGB → `api.localPick({kind:'dir'})`；点云 → `kind:'file'` + LAS/LAZ 过滤 或 `kind:'dir'`。
- 选中点云后自动调 `inspectPointCloud`：显示文件数/点数/bbox/CRS；CRS 缺失时弹出必填项（EPSG 输入 + 「按本地坐标」勾选），写入 `pc_crs`。
- 输出勾选：OSGB 固定显示「3D Tiles」（只读勾选）；点云显示 3D Tiles / DSM / DEM 三选，默认全选；「高级设置」折叠面板放分辨率（`pc_resolution`，默认自动）。
- 提交前调 `/api/tools/diagnose`：所需工具不可用时中文报错并阻止提交（OSGB→tiles3d_exe；点云 DEM/DSM→pdal_exe；点云 3D Tiles→py3dtiles_python）。
- taskDefaults.js：`PROVIDER_LABELS` 加 `local_osgb: '本地 OSGB'`、`local_pointcloud: '本地点云'`；`defaultTaskName` 按目录名/文件名生成。
- 入口：主界面「本地数据处理」菜单/按钮组新增「三维数据」项，打开 ProcessDialog 时传 `source:{kind:'local_3d'}`。

- [ ] **Step 4: 测试通过**

```powershell
cd frontendvue; node --test src/components/ProcessDialog.local3d.test.js
```

- [ ] **Step 5: Commit**

```powershell
git add frontendvue/src/components/ProcessDialog.vue frontendvue/src/api frontendvue/src/utils/taskDefaults.js frontendvue/src/components/ProcessDialog.local3d.test.js
git commit -m "feat: add local 3d source to process dialog with preflight checks"
```

---

### Task 11: 任务卡片 / 成果面板显示适配

**Files:**
- Modify: `frontendvue/src/utils/provider.js`
- Modify: `frontendvue/src/components/TaskQueue.vue`
- Modify: `frontendvue/src/components/DataDialog.vue`
- Modify: `frontendvue/src/preview/PreviewApp.vue`
- Test: `frontendvue/src/utils/provider.test.js`（无则新建）

- [ ] **Step 1: 失败测试**

```js
// taskKindOf({provider:'local_osgb'}) === 'model3d'
// taskKindOf({provider:'local_pointcloud'}) === 'model3d'
```

- [ ] **Step 2: 确认失败**

```powershell
cd frontendvue; node --test src/utils/provider.test.js
```

- [ ] **Step 3: 实现**

- provider.js：`MODEL3D_PROVIDERS = ['local_osgb', 'local_pointcloud']`；`taskKindOf` 加分支返回 `'model3d'`；`isModel3dProvider` 导出。
- TaskQueue.vue / DataDialog.vue：kindClass 三色加第四种色（如紫色）；`previewable(t)` 判定数组在现有 `['tms','osm','terrain','tile_3d']` 基础上追加 `'convert_3d'`、`'pc_tile_3d'`。
- PreviewApp.vue 小改：`stageDone('tile_3d')` 的判定扩展为 `stageDone('tile_3d') || stageDone('convert_3d') || stageDone('pc_tile_3d')`（三维任务 stages 里只有新 key）；`3dtiles/tileset.json` 路径约定一致，Cesium 加载逻辑不动。点云 DEM/DSM 产物如需在成果面板显示下载链接，复用现有栅格成果行（按 `{name}_dem.tif`/`{name}_dsm.tif` 命名约定匹配，实现时核对 DataDialog 的成果枚举逻辑是否硬编码文件后缀）。

- [ ] **Step 4: 测试通过**

```powershell
cd frontendvue; node --test src/utils/provider.test.js
```

- [ ] **Step 5: Commit**

```powershell
git add frontendvue/src/utils/provider.js frontendvue/src/components/TaskQueue.vue frontendvue/src/components/DataDialog.vue frontendvue/src/preview/PreviewApp.vue frontendvue/src/utils/provider.test.js
git commit -m "feat: show 3d task kind in queue and data panels"
```

---

### Task 12: 端到端验证与文档

**Files:**
- Modify: `docs/input/新增需求/需求描述.md`（在需求27末尾勾选验收结论，如需）
- Test: 手工端到端

- [ ] **Step 1: 全量后端测试**

```powershell
.venv\Scripts\python.exe -m unittest discover tests -v
```

- [ ] **Step 2: 前端构建 + 测试**

```powershell
cd frontendvue; node --test src/; npm run build
```

- [ ] **Step 3: 端到端真实样例（按技术验证顺序）**

1. OSGB 小样例：提交 local_osgb 任务 → tile_3d 阶段完成 → 任务卡片点预览 → Cesium 加载 tileset.json，核对坐标/高程/纹理。
2. LAS/LAZ 小样例：提交 local_pointcloud（DSM+DEM）→ 产物 `{name}_dsm.tif`/`{name}_dem.tif` 可下载 → 用「本地 DEM 导入」接力验证 terrain/contour。
3. 同点云：提交 local_pointcloud（3D Tiles）→ Cesium 加载，记录体积/加载速度/颜色分类保留；不达标则登记 point-tiler 备选。
4. 长任务：验证进度推进、暂停、取消、失败重跑（删除任务不清缓存语义沿用）、服务重启后 paused 任务可恢复。
5. 错误路径：未配置工具路径、OSGB 缺 metadata.xml、LAS 缺 CRS、磁盘空间不足（可 mock）——均给出明确中文错误。

- [ ] **Step 4: 更新 CLAUDE.md 架构要点（三维管线一段：runner_3d + processors + tools 配置约定）**

- [ ] **Step 5: Commit**

```powershell
git add docs CLAUDE.md
git commit -m "docs: record 3d processing pipeline architecture and verification results"
```

---

## 执行注意

1. **一期不做源码深嵌**：所有外部工具只通过 CLI/独立 venv 调用；二期若遇转换效果/性能痛点，按 adapter 逐个替换为 fork/库化，管线与任务模型不变。
2. **阶段 key 不复用**：已确认现有 `dem`（PIPE_RASTER）与 `tile_3d`（PIPE_BUILDING）被占用且 ExportStage 单实例、pipeline 字段互斥，三维固定用 `convert_3d`/`pc_dem`/`pc_dsm`/`pc_tile_3d` 四个新 key；前端 `previewable` 与 PreviewApp 的 stageDone 判定按 Task 11 同步追加。
3. **tools/ 分发约定**：外部二进制放 `tools/3dtiles/`、`tools/pdal/`、`tools/py3dtiles-venv/`，config 指向相对路径；`tools/` 加 .gitignore（体积大，不进库），打包脚本处理。
4. **Windows 细节**：subprocess 全部 `CREATE_NO_WINDOW`；路径统一 `Path` 对象；PDAL/fanvanzh 的输出编码按 GBK 容错解码（`errors="replace"`）。
5. **每完成一个 Task 跑一次对应测试，全绿再 commit**；Task 5/6/7 的真实样例验证不通过时，停下来修 adapter 再进入下一 Task。
