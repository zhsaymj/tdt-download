"""fanvanzh/3dtiles 适配器:OSGB 倾斜摄影 → 3D Tiles。

CLI 用法以 fanvanzh/3dtiles README 为准(2026-09 拉取确认):
    _3dtile.exe -f osgb -i <输入目录> -o <输出目录>
- 输入目录需为 smart3d 组织方式:Data/ 目录 + 同级 metadata.xml,
  工具会自动读取 metadata.xml 还原坐标(见 src/main.rs convert_osgb)。
- 产物:输出目录下 tileset.json 与 Data/Tile_*/** .b3dm。

进度输出形式说明(\r 风险登记项的结论):
README 未记载进度输出格式;src/main.rs / src/osgb.rs(Rust 层)只有
env_logger 按行日志,无进度行;C++ 层(osgb23dtile.cpp)因网络限制未能
完整确认,其内部使用 spdlog/printf,存在 \r 同行刷新的可能。
因此本适配器采取双保险(依据:base.py 的 _pump 按 \n 切行,\r 刷新
会聚成一行在 EOF 才到达,仅靠文本解析可能全程无进度):
1. parse_progress 按行解析 "converting x/y"、"xx%" 两类常见形式;
2. 提供 progress_by_output_count 兜底:按 out_dir 下已生成 b3dm
   数量 / 预估总数估算,供上层 runner 定时轮询。
待真实 exe + OSGB 样例验证后再收紧正则(报告已注明)。

Task 8 集成契约(runner_3d 必读,2026-09 审查经上游 master 源码核实):
1. 部分瓦片失败对适配器不可见:上游转换单个 Tile 失败仅记
   `failed: ...`/`ERROR` 日志后跳过继续,进程仍 exit 0,root tileset.json
   静默剔除失败 Tile。b3dm 非空检查挡不住"10 个 Tile 挂 2 个"。
   → runner 必须用 on_stdout_line/on_stderr_line 透传钩子扫描
   `failed:`/`ERROR` 行,出现即判阶段失败或至少向用户告警。
2. 文本进度大概率全程不命中(上游 Rust 层 rayon 并行、无 x/y 或 %
   进度行,只有 info/error 日志),实际进度依赖 progress_by_output_count。
   → runner 调用时 estimated_total 建议取「输入目录递归 .osgb 总数」
   (每个 osgb 节点约对应一个 b3dm,是可得的最接近代理);
   分母偏小会被 min(..., 1.0) 封顶导致进度条过早钉在 100%。
3. 必须以 cfg.tools.tiles3d_exe 构造实例(build_cmd 不回落配置)。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .base import BaseProcessor, ProcessorError

# "converting 3/10"、"process 1/4" 等 x/y 形式
_RE_RATIO = re.compile(r"(\d+)\s*/\s*(\d+)")
# "45%"、"45.5 %" 等百分比形式
_RE_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


class OsgbTo3dTiles(BaseProcessor):
    """fanvanzh/3dtiles 的 OSGB→3D Tiles 转换适配器。"""

    name = "3dtiles"

    def __init__(self, exe: str | Path | None = None):
        # exe 显式传入优先;否则 check_available 时取 cfg.tools.tiles3d_exe
        self._exe = str(exe) if exe else ""

    def _resolve_exe(self, cfg) -> str:
        if self._exe:
            return self._exe
        return (getattr(getattr(cfg, "tools", None), "tiles3d_exe", "") or "")

    def check_available(self, cfg) -> tuple[bool, str]:
        """路径存在 + `-h` 可跑(fanvanzh 基于 clap,支持 -h/--help)。"""
        exe = self._resolve_exe(cfg)
        if not exe:
            return False, ("未配置 3dtiles 可执行文件路径,"
                           "请在 config.yaml 的 tools.tiles3d_exe 中填写")
        if not Path(exe).exists():
            return False, f"3dtiles 可执行文件不存在:{exe}"
        try:
            proc = subprocess.run(
                [exe, "-h"], capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as e:
            return False, f"3dtiles 无法启动({e}),请检查路径与文件完整性"
        except subprocess.TimeoutExpired:
            return False, "3dtiles -h 试跑超时(15 秒),文件可能损坏"
        if proc.returncode != 0:
            return False, f"3dtiles -h 试跑失败(退出码 {proc.returncode})"
        return True, "3dtiles 可用"

    def build_cmd(self, *, input_dir, out_dir) -> list[str]:
        """构造命令:`3dtiles -f osgb -i <输入目录> -o <输出目录>`。"""
        if not self._exe:
            raise ProcessorError(
                self.name, None,
                hint="未配置 3dtiles 可执行文件路径:请以 "
                     "cfg.tools.tiles3d_exe 构造本适配器")
        return [self._exe, "-f", "osgb", "-i", str(input_dir),
                "-o", str(out_dir)]

    def parse_progress(self, line: str) -> float | None:
        """从一行输出解析进度(0~1)。

        支持 "converting 3/10"(x/y)与 "45%"(百分比)两类;
        解析不到返回 None(上层可用 progress_by_output_count 兜底)。
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
        """文件计数兜底进度:已生成 b3dm 数 / 预估总数(0~1)。

        estimated_total <= 0 时无法估算,返回 None。
        供上层 runner 定时轮询调用(parse_progress 长期无输出时)。
        """
        if estimated_total <= 0:
            return None
        count = sum(1 for _ in Path(out_dir).rglob("*.b3dm"))
        return min(count / estimated_total, 1.0)

    def expected_outputs(self, out_dir: Path) -> list[Path]:
        """预期产物:tileset.json + 全部 .b3dm;无 b3dm 直接判失败。"""
        out_dir = Path(out_dir)
        b3dms = sorted(out_dir.rglob("*.b3dm"))
        if not b3dms:
            raise ProcessorError(
                self.name, 0,
                hint="进程正常结束但未生成任何 .b3dm 瓦片,"
                     "请检查输入目录是否符合 smart3d 组织方式"
                     "(Data/ 目录 + 同名 .osgb)")
        return [out_dir / "tileset.json", *b3dms]

    def preflight(self, input_dir) -> list[str]:
        """输入预检:返回警告列表(不阻断);硬性问题抛 ProcessorError。

        - 目录不存在 / 递归无 .osgb → ProcessorError(中文提示)
        - metadata.xml 缺失 → 返回中文警告(fanvanzh 会自动读取它还原坐标,
          缺失时坐标可能按本地方处理;有些数据集确无此文件,故不阻断)
        """
        d = Path(input_dir)
        if not d.is_dir():
            raise ProcessorError(
                self.name, None, hint=f"输入目录不存在:{d}")
        if not any(d.rglob("*.osgb")):
            raise ProcessorError(
                self.name, None,
                hint=f"输入目录内未找到任何 .osgb 文件:{d}")
        warnings = []
        if not (d / "metadata.xml").exists():
            warnings.append(
                "未找到 metadata.xml:3dtiles 无法自动还原模型坐标,"
                "成果可能按本地坐标处理(位置偏移)。若数据为 smart3d 导出,"
                "请将 metadata.xml 放到输入目录(与 Data 目录同级)。")
        return warnings
