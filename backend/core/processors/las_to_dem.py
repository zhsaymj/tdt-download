"""PDAL 适配器:LAS/LAZ 点云 → DSM/DEM GeoTIFF。

pipeline 结构(选项以 PDAL 2.x 文档核实的事实为准):
- DSM: readers.las → writers.gdal(output_type=max,取格网内最高点)
- DEM: readers.las → filters.smrf(地面分类)→
       filters.range(limits="Classification[2:2]",只留地面点)→
       writers.gdal(output_type=idw,插值成栅格)
已核实选项:writers.gdal 的 output_type/resolution/nodata/gdaldriver/filename、
readers.las 的 override_srs、filters.range 的 limits 语法。
writers.gdal 虽有自己的 override_srs/default_srs 选项,但官方注明输出 SRS
会自动继承自输入数据、无需设置——故 CRS 覆盖只写 readers.las.override_srs,
由 reader 沿 pipeline 传播到 writer(推荐路径)。

两个计划待定项的实现结论(2026-09):
1. `--nostream 视版本决定`:不加。`pdal pipeline` 会自动判定全部 stage
   是否支持流式(支持则走 stream 省内存),显式 --nostream 只会拖慢
   大数据量场景,无正确性收益。
2. `可选 filters.fillpings 空洞填充`:PDAL 官方 filter 名录中未核实到
   filters.fillpings,不臆造 stage;空洞暂时以 nodata 保留。后续增强可用
   writers.gdal 的 window_size 选项(对空格网用周边非空格网做 IDW 兜底
   插值),待真实 PDAL 环境验证后再加。

进度:pdal pipeline 执行期间不输出进度行,parse_progress 不实现
(继承基类返回 None);runner_3d 只能按阶段粒度报进度。

Task 8 集成契约(runner_3d 必读):
1. 一个类对应 pc_dsm / pc_dem 两个 stage:kind="dsm"/"dem";输出路径
   由 runner 按 formats.py 的 {name}_dsm.tif / {name}_dem.tif 给定,
   本适配器不做命名。
2. 必须以 cfg.tools.pdal_exe 构造(build_cmd 不回落配置,空 exe 抛错)。
3. 一期仅支持单 LAS/LAZ 文件输入;目录输入由 runner 逐文件循环,
   本适配器不做目录展开。
4. resolution 必须 >0;pc_resolution=0(按点云密度自动估算)由 runner
   结合 preflight() 返回的 bbox/points 估算后传入,本层不估算。
5. expected_outputs 依赖 build_pipeline/run_pipeline 记录的输出路径,
   除存在/非空(基类 run 检查)外还做 rasterio 可打开校验。
6. CRS 策略(crs 参数即任务 pc_crs 字段):
   - ""          → 沿用 LAS 头 SRS,不写 override_srs;
   - "EPSG:xxxx" → 写 readers.las.override_srs(覆盖 LAS 头);
   - "local"     → 不加任何 SRS,警告经实例属性 last_warnings 暴露,
                   由 runner 透传给用户。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import BaseProcessor, ProcResult, ProcessorError

#: pdal info --summary 读取 LAS 头的超时(秒);LAZ 需解压头部,留足余量
_INFO_TIMEOUT = 60


class LasToDem(BaseProcessor):
    """PDAL pipeline 的 LAS/LAZ→DSM/DEM 转换适配器。"""

    name = "pdal"

    def __init__(self, pdal_exe: str | Path | None = None):
        # pdal_exe 显式传入优先;否则 check_available 时取 cfg.tools.pdal_exe
        self._exe = str(pdal_exe) if pdal_exe else ""
        # build_pipeline 记录的输出 tif 路径,expected_outputs 校验用
        self._output: Path | None = None
        # build_pipeline 产生的软警告(如 pc_crs=local),供上层透传
        self.last_warnings: list[str] = []

    def _resolve_exe(self, cfg) -> str:
        if self._exe:
            return self._exe
        return (getattr(getattr(cfg, "tools", None), "pdal_exe", "") or "")

    def check_available(self, cfg) -> tuple[bool, str]:
        """路径存在(或 PATH 可寻)+ `--version` 可跑。"""
        exe = self._resolve_exe(cfg)
        if not exe:
            return False, ("未配置 pdal 可执行文件路径,"
                           "请在 config.yaml 的 tools.pdal_exe 中填写")
        run_exe = exe
        if not Path(exe).exists():
            # 允许只填命令名(如 "pdal"),从 PATH 解析
            found = shutil.which(exe)
            if not found:
                return False, f"pdal 可执行文件不存在(也不在 PATH):{exe}"
            run_exe = found
        try:
            proc = subprocess.run(
                [run_exe, "--version"], capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as e:
            return False, f"pdal 无法启动({e}),请检查路径与文件完整性"
        except subprocess.TimeoutExpired:
            return False, "pdal --version 试跑超时(15 秒),文件可能损坏"
        if proc.returncode != 0:
            return False, f"pdal --version 试跑失败(退出码 {proc.returncode})"
        return True, "pdal 可用"

    def build_pipeline(self, *, input, output, kind: str, resolution: float,
                       crs: str = "", nodata: float = -9999.0) -> str:
        """生成 PDAL pipeline JSON 字符串,并记录输出路径与 CRS 警告。

        kind: "dsm"(最高点)或 "dem"(smrf 地面分类后 idw 插值)。
        crs 语义见模块 docstring 第 6 条;resolution 必须为正。
        """
        self.last_warnings = []
        if kind not in ("dsm", "dem"):
            raise ProcessorError(
                self.name, None,
                hint=f"不支持的产物类型 kind={kind!r}(仅支持 dsm/dem)")
        if resolution <= 0:
            raise ProcessorError(
                self.name, None,
                hint="分辨率必须为正数(米);pc_resolution=0 的自动估算"
                     "由上层 runner 结合 preflight 结果完成")
        self._output = Path(output)

        reader: dict = {"type": "readers.las", "filename": str(input)}
        crs = (crs or "").strip()
        if crs.lower() == "local":
            self.last_warnings.append(
                "pc_crs=local:输出 GeoTIFF 不带坐标参考,仅可作本地成果使用;"
                "如需正确落点,请在任务参数中填 EPSG 码(如 EPSG:4547)。")
        elif crs:
            # writers.gdal 无 srs 选项,由 reader 的 override_srs 传播
            reader["override_srs"] = crs

        writer = {
            "type": "writers.gdal",
            # 用原始字符串而非 str(Path(...)):避免 Windows 下路径被转成反斜杠
            "filename": str(output),
            "gdaldriver": "GTiff",
            "data_type": "float32",
            "resolution": float(resolution),
            "nodata": float(nodata),
            "output_type": "max" if kind == "dsm" else "idw",
        }
        stages = [reader]
        if kind == "dem":
            stages.append({"type": "filters.smrf"})
            stages.append({"type": "filters.range",
                           "limits": "Classification[2:2]"})
        stages.append(writer)
        return json.dumps({"pipeline": stages}, ensure_ascii=False, indent=2)

    def build_cmd(self, *, pipeline_file) -> list[str]:
        """构造命令:`pdal pipeline <pipeline.json>`。

        不加 --nostream:PDAL 自动判定流式/标准模式(见模块 docstring)。
        """
        if not self._exe:
            raise ProcessorError(
                self.name, None,
                hint="未配置 pdal 可执行文件路径:请以 "
                     "cfg.tools.pdal_exe 构造本适配器")
        return [self._exe, "pipeline", str(pipeline_file)]

    def run_pipeline(self, *, input, output, kind: str, resolution: float,
                     crs: str = "", nodata: float = -9999.0,
                     cancel_event=None, on_progress=None,
                     on_stdout_line=None, on_stderr_line=None,
                     timeout: float | None = None) -> ProcResult:
        """一把梭:写 pipeline JSON 临时文件 → run 执行 → 清理临时文件。"""
        pipe = self.build_pipeline(input=input, output=output, kind=kind,
                                   resolution=resolution, crs=crs,
                                   nodata=nodata)
        # utf-8 + ensure_ascii=False,兼容中文路径
        fd, tmp = tempfile.mkstemp(suffix=".json", prefix="pdal_pipeline_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(pipe)
            cmd = self.build_cmd(pipeline_file=tmp)
            return self.run(cmd, out_dir=Path(output).parent,
                            cancel_event=cancel_event, on_progress=on_progress,
                            on_stdout_line=on_stdout_line,
                            on_stderr_line=on_stderr_line, timeout=timeout)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def expected_outputs(self, out_dir: Path) -> list[Path]:
        """预期产物:build_pipeline 记录的输出 tif。

        文件缺失/为空不在此抛错(交基类 run 的"未生成预期产物"判定);
        存在且非空但 rasterio 打不开 → ProcessorError(产物损坏)。
        """
        if self._output is None:
            raise ProcessorError(
                self.name, None,
                hint="内部错误:expected_outputs 需在 build_pipeline/"
                     "run_pipeline 之后调用")
        out = self._output
        if out.exists() and out.stat().st_size > 0:
            try:
                import rasterio  # 惰性导入:仅校验时依赖 rasterio
                with rasterio.open(out):
                    pass
            except Exception as e:
                raise ProcessorError(
                    self.name, 0,
                    hint=f"产物 {out.name} 已生成但无法用 rasterio 打开,"
                         f"文件可能损坏:{e}") from e
        return [out]

    @staticmethod
    def parse_info_summary(text: str) -> dict:
        """解析 `pdal info --summary` 的 JSON 输出 → {points, bbox, srs}。

        JSON 非法抛 ProcessorError;字段缺失回落 None/""。
        srs 为 dict 时优先取 proj4(紧凑、可机读),回落 prettywkt。
        """
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ProcessorError(
                LasToDem.name, None,
                hint=f"pdal info 输出不是合法 JSON:{e}") from e
        summary = data.get("summary") or {}
        srs = summary.get("srs")
        if isinstance(srs, dict):
            srs = srs.get("proj4") or srs.get("prettywkt") or ""
        return {"points": summary.get("count"),
                "bbox": summary.get("bbox") or None,
                "srs": srs or ""}

    def preflight(self, input, cfg=None) -> dict:
        """`pdal info --summary` 读 LAS 头:返回 {points, bbox, srs, warnings}。

        供提交前校验接口复用:pc_crs 缺省时可据 srs 是否为空提示用户。
        硬性问题(文件不存在/pdal 不可用/输出非法)抛 ProcessorError。
        """
        path = Path(input)
        if not path.is_file():
            raise ProcessorError(self.name, None,
                                 hint=f"输入文件不存在:{path}")
        exe = self._resolve_exe(cfg) if cfg is not None else self._exe
        if not exe:
            raise ProcessorError(
                self.name, None,
                hint="未配置 pdal 可执行文件路径(tools.pdal_exe),"
                     "无法预检 LAS 头")
        try:
            proc = subprocess.run(
                [exe, "info", "--summary", str(path)],
                capture_output=True, timeout=_INFO_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as e:
            raise ProcessorError(
                self.name, None, str(e),
                hint="pdal 无法启动,请检查 tools.pdal_exe 配置") from e
        except subprocess.TimeoutExpired:
            raise ProcessorError(
                self.name, None,
                hint=f"pdal info 执行超时({_INFO_TIMEOUT} 秒)")
        if proc.returncode != 0:
            raise ProcessorError(
                self.name, proc.returncode,
                _decode(proc.stderr),
                hint="pdal info 读取 LAS 头失败,文件可能损坏或不是 LAS/LAZ")
        info = self.parse_info_summary(_decode(proc.stdout))
        warnings = []
        if not info["srs"]:
            warnings.append(
                "LAS 头部不含 SRS 坐标参考:请在任务参数 pc_crs 中填 EPSG 码,"
                "否则输出 GeoTIFF 无坐标。")
        if not info["points"]:
            warnings.append("LAS 头部点数为 0 或缺失,文件可能为空或损坏。")
        info["warnings"] = warnings
        return info


def _decode(raw: bytes) -> str:
    """解码 pdal 输出:先 utf-8,失败回落 gbk(与 base._decode_line 同策略)。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")
