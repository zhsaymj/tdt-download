"""py3dtiles 适配器:LAS/LAZ 点云 → 3D Tiles(pnts)。

CLI 核实结论与确认状态(2026-09):
- 信息来源:PyPI sdist py3dtiles-12.1.1 源码(convert.py 的 _init_parser/
  主循环、utils.py 的 mkdir_or_raise/str_to_CRS、
  tilers/point/point_tiler.py、tilers/base_tiler/tiler.py),
  辅以 py3dtiles.org v12.1.0 文档/FAQ 与 CHANGELOG。
- 确认状态:参数拼写与产物布局已经源码确认;但本环境无 py3dtiles 独立
  venv 与 LAS 样例,未做真实转换验证(计划 Step 5 留待用户;若性能不可
  接受,二期可换 point-tiler/gocesiumtiler,管线不变)。
- 调用形式(最保守参数集,一期固定):
      <python> -m py3dtiles convert <输入.las> --out <输出目录>
  python 为 tools.py3dtiles_python 配置的独立 venv 解释器(避免污染主环境)。
  其余已核实参数(--overwrite/--srs_in/--srs_out/--no-rgb/--extra-fields/
  --color_scale/--jobs/--cache_size/--force-srs-in/--disable-processpool/
  --pyproj-always-xy/-v/--spec-version)一期均不暴露:
  - --overwrite 不传:mkdir_or_raise 接受已存在的空目录;非空目录报
    FileExistsError 属预期保护(防误清数据),目录新鲜度由 runner 保证;
  - v10 起 classification/intensity 需 --extra-fields 才进产物,一期固定
    默认不携带(产物只含 xyz + rgb 若源有);--no-rgb 不传;
  - --spec-version 默认 "1.0" 产 pnts(>=1.1 产 gltf/glb),一期不动。
- 产物布局:<out>/tileset.json + <out>/points/*.pnts
  (PointTiler.name="points";tiler.py docstring 确认 content_uri 相对
  <out>)。

CRS 行为(源码核实的重要风险登记,point_tiler.get_transformer):
- 不传 --srs_out → transformer=None,完全不重投影,点与 boundingVolume
  保持 LAS 原坐标——3D Tiles 规范要求最终为 ECEF,此类产物在 Cesium 中
  落点错误(除非 LAS 本身已是 ECEF);
- 传 --srs_out 但 LAS 头无 SRS(国内数据常见)→ 抛 SrsInMissingException,
  需同时给 --srs_in;
- 传 --srs_out 4978 且 LAS 头有 SRS → 正确转 ECEF,并有旋转矩阵优化。
一期仍固定最保守参数集不接 SRS 参数(任务纪律),Step 5 真实样例验证后
由 Task 8/二期决定是否把 --srs_in/--srs_out 接到任务 pc_crs 字段。

进度说明(与 osgb 适配器同理的双保险):
py3dtiles 默认 verbose=0 时 stdout 几乎无输出(仅 Warning/Error),
"Writing 3dtiles" 等摘要均需 -v;且全程无 x/y 或 % 数字进度行。
因此:
1. parse_progress 尽力解析 x/y 与 %(沿用 osgb 正则,实际大概率不命中);
2. progress_by_output_count 按 out_dir 下已生成 .pnts 数量兜底(pnts 由
   worker 进程增量写出,计数是有效代理),供上层 runner 定时轮询。

Task 8 集成契约(runner_3d 必读):
1. 一期仅支持单 LAS/LAZ 文件输入(build_cmd 的 input_file);目录输入由
   runner 逐文件循环,本适配器不做目录展开。
2. 必须以 cfg.tools.py3dtiles_python(独立 venv 的 python.exe 路径)构造;
   build_cmd 不回落配置,空解释器抛 ProcessorError。
3. 文本进度大概率全程不命中,实际进度依赖 progress_by_output_count;
   pnts 总数事先不可知(取决于点数与八叉树深度),estimated_total 只能给
   粗略估计(分母偏小会被 min(..., 1.0) 封顶致进度条过早钉 100%);
   runner 亦可退化为按文件粒度报进度(单文件转换为原子阶段)。
4. 输出目录须为空目录或不存在:非空目录 py3dtiles 会报
   FileExistsError(本适配器刻意不传 --overwrite,防误清数据)。
5. CRS 落点风险见上文「CRS 行为」:默认参数下产物保持 LAS 原坐标,
   Cesium 落点大概率错误;若用户反馈落点异常,优先接入 --srs_out 4978
   与任务 pc_crs 字段(缺 SRS 的 LAS 还需 --srs_in)。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .base import BaseProcessor, ProcessorError

# "converting 3/10"、"process 1/4" 等 x/y 形式(保底能力,py3dtiles 默认无此行)
_RE_RATIO = re.compile(r"(\d+)\s*/\s*(\d+)")
# "45%"、"45.5 %" 等百分比形式
_RE_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


class LasTo3dTiles(BaseProcessor):
    """py3dtiles 的 LAS/LAZ→3D Tiles(pnts)转换适配器。"""

    name = "py3dtiles"

    def __init__(self, python: str | Path | None = None):
        # python 显式传入优先;否则 check_available 时取 cfg.tools.py3dtiles_python
        self._python = str(python) if python else ""

    def _resolve_python(self, cfg) -> str:
        if self._python:
            return self._python
        return (getattr(getattr(cfg, "tools", None), "py3dtiles_python", "") or "")

    def check_available(self, cfg) -> tuple[bool, str]:
        """解释器路径存在 + `python -m py3dtiles -h` 可跑(退出码 0)。

        退出码非 0 的典型原因是该解释器未安装 py3dtiles 模块
        (Task 1 诊断接口只判"能否启动",本方法额外判模块可用)。
        """
        python = self._resolve_python(cfg)
        if not python:
            return False, ("未配置 py3dtiles 解释器路径,"
                           "请在 config.yaml 的 tools.py3dtiles_python 中填写")
        if not Path(python).exists():
            return False, f"py3dtiles 解释器不存在:{python}"
        try:
            proc = subprocess.run(
                [python, "-m", "py3dtiles", "-h"], capture_output=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as e:
            return False, f"py3dtiles 解释器无法启动({e}),请检查路径与文件完整性"
        except subprocess.TimeoutExpired:
            return False, "py3dtiles -h 试跑超时(15 秒),解释器或环境可能损坏"
        if proc.returncode != 0:
            return False, (f"py3dtiles -h 试跑失败(退出码 {proc.returncode}):"
                           "该解释器可能未安装 py3dtiles,请确认 "
                           "tools.py3dtiles_python 指向装有 py3dtiles 的独立 venv")
        return True, "py3dtiles 可用"

    def build_cmd(self, *, input_file, out_dir) -> list[str]:
        """构造命令:`python -m py3dtiles convert <输入.las> --out <输出目录>`。

        最保守参数集(一期固定):只传输入文件与输出目录,其余参数
        (SRS/字段/overwrite 等)均不传,理由见模块 docstring。
        """
        if not self._python:
            raise ProcessorError(
                self.name, None,
                hint="未配置 py3dtiles 解释器路径:请以 "
                     "cfg.tools.py3dtiles_python 构造本适配器")
        return [self._python, "-m", "py3dtiles", "convert",
                str(input_file), "--out", str(out_dir)]

    def parse_progress(self, line: str) -> float | None:
        """从一行输出解析进度(0~1)。

        支持 "3/10"(x/y)与 "45%"(百分比)两类;解析不到返回 None。
        py3dtiles 默认 verbose=0 无数字进度行(见模块 docstring),
        实际进度用 progress_by_output_count 兜底。
        """
        m = _RE_RATIO.search(line)
        if m:
            cur, total = int(m.group(1)), int(m.group(2))
            if total > 0 and 0 <= cur <= total:
                return cur / total
        m = _RE_PERCENT.search(line)
        if m:
            pct = float(m.group(1))
            if 0.0 <= pct <= 100.0:
                return pct / 100.0
        return None

    @staticmethod
    def progress_by_output_count(out_dir, estimated_total: int) -> float | None:
        """文件计数兜底进度:已生成 pnts 数 / 预估总数(0~1)。

        estimated_total <= 0 时无法估算,返回 None。
        供上层 runner 定时轮询调用(parse_progress 长期无输出时)。
        """
        if estimated_total <= 0:
            return None
        count = sum(1 for _ in Path(out_dir).rglob("*.pnts"))
        return min(count / estimated_total, 1.0)

    def expected_outputs(self, out_dir: Path) -> list[Path]:
        """预期产物:tileset.json + 全部 .pnts;无 pnts 直接判失败。"""
        out_dir = Path(out_dir)
        pnts = sorted(out_dir.rglob("*.pnts"))
        if not pnts:
            raise ProcessorError(
                self.name, 0,
                hint="进程正常结束但未生成任何 .pnts 瓦片,"
                     "请检查输入文件是否为有效的 LAS/LAZ 点云")
        return [out_dir / "tileset.json", *pnts]
