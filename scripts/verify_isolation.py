"""阶段 1 验收:任务运行期间 Web 接口是否仍然秒回。

改造前的问题:GDAL 的 build_overviews / reproject / clip 长时间持有 GIL,
饿死同进程的事件循环 —— 任务运行期间**所有** HTTP 请求与 WebSocket 一起挂起
(实测最长 9.5s 完全无响应)。

改造后任务跑在独立子进程,主进程只做调度,接口应始终保持响应。

用法:
  1. 另开终端启动服务:start.bat
  2. 在界面提交一个能跑几十秒的任务(大范围拼接 / 高级别)
  3. 运行本脚本:.venv/Scripts/python.exe tools/verify_isolation.py

判定:任务 running 期间 /api/tasks 的 P99 延迟应 < 1s。
"""
from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
DURATION = 60      # 采样时长(秒)
INTERVAL = 0.2     # 采样间隔(秒)


def fetch(path: str) -> tuple[float, object]:
    """请求一次,返回 (耗时毫秒, 解析后的 JSON)。"""
    t0 = time.perf_counter()
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        body = r.read()
    dt = (time.perf_counter() - t0) * 1000
    return dt, json.loads(body)


def running_count(payload) -> int:
    items = payload if isinstance(payload, list) else payload.get("items", [])
    return sum(1 for t in items if t.get("status") == "running")


def main() -> int:
    try:
        fetch("/api/tasks")
    except Exception as e:
        print(f"[错误] 连不上服务 {BASE}:{e}")
        print("       请先运行 start.bat 启动后端。")
        return 2

    samples: list[float] = []
    print(f"采样 {DURATION}s,每 {INTERVAL}s 请求一次 /api/tasks ...")
    deadline = time.time() + DURATION
    saw_running = False
    while time.time() < deadline:
        try:
            dt, payload = fetch("/api/tasks")
            samples.append(dt)
            if running_count(payload) > 0:
                saw_running = True
        except Exception as e:
            samples.append(30000.0)      # 超时记一个极大值,让结论反映真实问题
            print(f"  请求失败:{e}")
        time.sleep(INTERVAL)

    if not samples:
        print("[失败] 没有采到样本")
        return 2

    ordered = sorted(samples)
    p50 = statistics.median(ordered)
    p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
    worst = ordered[-1]

    print(f"\n样本 {len(samples)} 个"
          f"{'(期间观察到 running 任务)' if saw_running else '(★未观察到 running 任务)'}")
    print(f"  P50 = {p50:.0f}ms")
    print(f"  P99 = {p99:.0f}ms")
    print(f"  最慢 = {worst:.0f}ms")

    if not saw_running:
        print("\n[无效] 采样期间没有任务在跑 —— 请先提交一个长任务再运行本脚本。")
        return 2
    if p99 < 1000 and worst < 3000:
        print("\n[通过] 任务运行期间接口保持响应(改造前会出现 8s+ 停顿)")
        return 0
    print(f"\n[失败] P99={p99:.0f}ms 或最慢={worst:.0f}ms 超阈值")
    return 1


if __name__ == "__main__":
    sys.exit(main())
