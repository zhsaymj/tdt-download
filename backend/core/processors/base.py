"""三维外部处理器适配层基类:CLI 运行封装 + 抽象接口。

本层只负责"把一个外部命令跑好"(进度透传/取消/错误归一化/产物校验),
不碰任务落库——取消与失败到任务状态的转换由上层 runner_3d(Task 8)完成。
"""
from __future__ import annotations

import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

# CREATE_NO_WINDOW 仅 Windows 平台存在,getattr 防御(其他平台回落 0)。
# 加它是为了后台运行外部工具时不弹出黑色控制台窗口。
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ProcessorError 中保留的 stderr 末尾长度
_STDERR_TAIL = 2000


class ProcessorError(Exception):
    """外部工具执行失败:工具名 + 退出码 + 截断后的 stderr + 面向用户的中文提示。

    属性:
        tool:      工具显示名(如 3dtiles / pdal / py3dtiles)
        exit_code: 进程退出码;超时等无退出码的失败为 None
                   (产物缺失时进程已正常退出,为真实退出码 0)
        stderr:    stderr 末尾 2000 字符(供日志与诊断)
    """

    def __init__(self, tool: str, exit_code: int | None = None,
                 stderr: str = "", hint: str = ""):
        self.tool = tool
        self.exit_code = exit_code
        self.stderr = (stderr or "")[-_STDERR_TAIL:]
        msg = f"外部工具 {tool} 执行失败"
        if exit_code is not None:
            msg += f"(退出码 {exit_code})"
        if hint:
            msg += f":{hint}"
        if self.stderr:
            msg += f"\n--- 错误输出(末尾 {len(self.stderr)} 字符)---\n{self.stderr}"
        super().__init__(msg)


class ProcessorCancelled(Exception):
    """cancel_event 置位导致进程被终止(任务暂停/取消)。

    约定:runner_3d 捕获本异常后,按注入的 should_stop() 返回的
    控制原因("pause"/"cancel")转换为 runner_buildings._Stopped
    语义,由顶层落库为 paused/canceled。本层不直接区分暂停与取消。
    """


@dataclass
class ProcResult:
    """一次外部工具执行的结果。outputs 由 BaseProcessor.run 校验后填充。"""

    ok: bool
    outputs: list[str] = field(default_factory=list)  # 产物路径(已做存在性/非空检查)
    message: str = ""


def _decode_line(raw: bytes) -> str:
    """按行解码外部工具输出:先 utf-8,失败回落 gbk(replace 容错)。

    选这个方案而非 mbcs:本机可能是中文(GBK)也可能不是,mbcs 随系统区域
    设置漂移;而 utf-8 工具(py3dtiles 等 python 系)与 GBK 工具(Windows 中文
    系统的 C++ CLI)二段式猜测即可同时覆盖,行为确定、可测试。
    按行解码是安全的:utf-8/gbk 的多字节序列中都不含 0x0A,按 b'\\n' 切行
    不会劈开字符。
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


class _StderrTail:
    """只保留末尾若干字符的 stderr 收集器(错误报告用,防内存膨胀)。

    只有 stderr 读取线程一个写入者,无需加锁。
    """

    def __init__(self, limit: int = _STDERR_TAIL):
        self._buf = ""
        self._limit = limit

    def append(self, s: str):
        self._buf = (self._buf + s)[-self._limit:]

    def text(self) -> str:
        return self._buf


def _pump(stream, callback, tail: _StderrTail | None):
    """读取线程:逐行读管道,解码后回调;stderr 同时留尾。"""
    try:
        for raw in iter(stream.readline, b""):
            line = _decode_line(raw).rstrip("\r\n")
            if tail is not None:
                tail.append(line + "\n")
            if callback is not None:
                try:
                    callback(line)
                except Exception:
                    # 进度解析/日志回调出错不应杀死读取线程(会导致管道堵死)
                    from ..logs import logger
                    logger.warning("处理器输出行回调异常:%s", line[:200],
                                   exc_info=True)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def run_cli(cmd: list[str], *, cwd=None, cancel_event: threading.Event | None = None,
            on_stdout_line=None, on_stderr_line=None,
            timeout: float | None = None, tool: str = "") -> ProcResult:
    """subprocess.Popen 封装:执行外部命令并逐行透传输出。

    - Windows 下 CREATE_NO_WINDOW(不弹控制台窗口)
    - stdout/stderr 各一个线程逐行读取并回调(双线程避免管道缓冲写满死锁;
      进度解析走 stdout,日志透传走 stderr)
    - cancel_event 置位 → terminate → 等 5s → 仍未退出则 kill,
      抛 ProcessorCancelled(_Stopped 语义由调用方 runner_3d 转换)
    - timeout 为整体超时(秒,None 不限),超时 kill 并抛 ProcessorError
    - 非零退出 → ProcessorError(stderr 末尾 2000 字符)

    tool: 工具显示名,用于错误消息;缺省取 cmd[0] 的文件名。
    取消/超时/失败前都会先等读取线程收尾,保证输出行不丢、线程不泄漏。
    """
    tool = tool or Path(cmd[0]).name
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
        )
    except OSError as e:
        # exe 无法启动(路径不存在/权限不足/损坏/架构不符),
        # 与其他失败一样归一化为 ProcessorError
        raise ProcessorError(tool, None, str(e),
                             hint="无法启动,请检查 tools 配置中的路径") from e
    tail = _StderrTail()
    t_out = threading.Thread(target=_pump,
                             args=(proc.stdout, on_stdout_line, None),
                             daemon=True)
    t_err = threading.Thread(target=_pump,
                             args=(proc.stderr, on_stderr_line, tail),
                             daemon=True)
    t_out.start()
    t_err.start()

    def _join_readers():
        # 进程退出/被杀后管道关闭,读取线程很快结束;超时兜底防极端挂死
        t_out.join(timeout=5)
        t_err.join(timeout=5)

    started = time.monotonic()
    while True:
        rc = proc.poll()
        if rc is not None:
            _join_readers()
            if rc != 0:
                raise ProcessorError(tool, rc, tail.text())
            return ProcResult(ok=True)

        if cancel_event is not None and cancel_event.is_set():
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            _join_readers()
            raise ProcessorCancelled(f"{tool} 已终止(任务暂停/取消)")

        if timeout is not None and time.monotonic() - started > timeout:
            proc.kill()
            proc.wait()
            _join_readers()
            raise ProcessorError(tool, None, tail.text(),
                                 hint=f"执行超时({timeout:g} 秒),已强制结束")

        time.sleep(0.05)


class BaseProcessor(ABC):
    """外部处理器适配基类。

    子类实现:命令构造(build_cmd)、可用性检查(check_available)、
    预期产物清单(expected_outputs);进度解析(parse_progress)可选。
    """

    #: 工具显示名(用于错误消息与日志)
    name: str = ""

    @abstractmethod
    def check_available(self, cfg) -> tuple[bool, str]:
        """检查工具可用性(路径已配置且存在/可试跑)。返回 (是否可用, 说明)。"""

    @abstractmethod
    def build_cmd(self, *args, **kwargs) -> list[str]:
        """构造命令行参数列表。"""

    def parse_progress(self, line: str) -> float | None:
        """从一行 stdout 解析进度(0~1);无法解析返回 None。默认不解析。"""
        return None

    @abstractmethod
    def expected_outputs(self, out_dir: Path) -> list[Path]:
        """预期产物清单,run 结束后逐个校验存在且非空。"""

    def run(self, cmd: list[str], *, out_dir, cwd=None,
            cancel_event: threading.Event | None = None,
            on_progress=None, on_stderr_line=None, on_stdout_line=None,
            timeout: float | None = None) -> ProcResult:
        """执行命令 → stdout 行透传 + 进度解析 → 产物校验。

        - 透传:stdout/stderr 逐行先经 on_stdout_line/on_stderr_line 透传
          (日志用;fanvanzh/pdal/py3dtiles 的有效日志主要走 stdout)
        - 进度:stdout 每行透传后再经 parse_progress 解析,结果非 None
          则回调 on_progress(0~1)
        - 取消:cancel_event 置位时 run_cli 抛 ProcessorCancelled,
          由 runner_3d 转换为 _Stopped(暂停/取消语义),本层不落库
        - 失败:非零退出/超时抛 ProcessorError;进程正常结束但产物
          缺失或为 0 字节,同样抛 ProcessorError(中文提示)
        """
        def _on_stdout(line: str):
            if on_stdout_line is not None:
                on_stdout_line(line)
            if on_progress is None:
                return
            p = self.parse_progress(line)
            if p is not None:
                on_progress(p)

        result = run_cli(cmd, cwd=cwd, cancel_event=cancel_event,
                         on_stdout_line=_on_stdout,
                         on_stderr_line=on_stderr_line,
                         timeout=timeout, tool=self.name)

        outputs = self.expected_outputs(Path(out_dir))
        missing = [p for p in outputs
                   if not (p.exists() and p.stat().st_size > 0)]
        if missing:
            names = ", ".join(p.name for p in missing)
            raise ProcessorError(
                self.name or Path(cmd[0]).name, 0,
                hint=f"进程正常结束但未生成预期产物:{names}")
        result.outputs = [str(p) for p in outputs]
        result.message = f"{self.name} 处理完成" if self.name else ""
        return result
