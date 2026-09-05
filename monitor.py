"""性能监控模块：实时观察【线程列表 / 进程 CPU / GPU 占用】。

文件位置：项目根目录新增 monitor.py。
用途：诊断"扫描慢到底是程序问题还是 PaddleOCR-VL 本身"——
  - 周期性采样（默认每 3s）写入 logs/perf_monitor.log（logs/ 目录自动创建）；
  - table.py 启动时自动开启；benchmark_ocr.py 用它做基准判定。

判定口径（配合日志看）：
  - 页面处理期间 GPU 利用率持续高（>70%）→ 计算确在 GPU 上，慢是模型本身特性；
  - GPU 利用率≈0 且 CPU 占用高 → 程序路径问题（误走 CPU / 等待 / 线程堆积）；
  - 线程列表可看到 扫描线程/脱敏线程/看门狗 是否存活、是否有堆积。

不依赖 psutil：CPU 用 os.times() 差值估算；GPU 用 nvidia-smi 快照。
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"

# ---- 监控状态 ----
_state = {"running": False, "thread": None, "interval": 3, "path": None}
_prev = {"t": None, "cpu": 0.0}
_lock = threading.Lock()


def _process_cpu_percent() -> float | None:
    """用 os.times() 差值估算进程 CPU 占用率（多核机器可 >100%）。"""
    global _prev
    t = time.monotonic()
    times = os.times()
    cur = times.user + times.system
    with _lock:
        if _prev["t"] is None:
            _prev["t"], _prev["cpu"] = t, cur
            return None
        dt = max(t - _prev["t"], 1e-6)
        pct = (cur - _prev["cpu"]) / dt * 100.0
        _prev["t"], _prev["cpu"] = t, cur
    return round(pct, 1)


def gpu_snapshot() -> dict:
    """nvidia-smi 一次性快照：GPU 利用率/显存/功耗；失败返回空 dict。"""
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.free,power.draw",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            parts = [p.strip() for p in out.stdout.strip().split(",")]
            return {
                "gpu_util_pct": parts[0],
                "mem_used_mb": parts[1],
                "mem_free_mb": parts[2],
                "power_w": parts[3],
            }
    except Exception:
        pass
    return {}


def snapshot_line() -> str:
    """构造一行采样记录：时间 | 线程列表 | CPU% | GPU。"""
    threads = sorted({t.name for t in threading.enumerate()})
    cpu = _process_cpu_percent()
    gpu = gpu_snapshot()
    gpu_s = " ".join(f"{k}={v}" for k, v in gpu.items()) if gpu else "gpu=NA"
    return (
        f"{time.strftime('%H:%M:%S')} | cpu={cpu}% | {gpu_s} | threads={len(threads)} "
        f"[{', '.join(threads[:12])}]"
    )


def _write_line(line: str) -> None:
    path = _state["path"]
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _run() -> None:
    """监控守护线程：按间隔采样写日志。"""
    while _state["running"]:
        time.sleep(_state["interval"])
        try:
            _write_line(snapshot_line())
        except Exception:
            pass


def start_performance_monitor(interval: int = 3) -> Path:
    """启动性能监控（幂等：已启动则直接返回日志路径）。

    返回日志文件路径。采样：线程列表 + 进程 CPU% + GPU 利用率/显存/功耗。
    """
    if _state["running"]:
        return _state["path"]
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / "perf_monitor.log"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n===== 监控开始 {time.strftime('%Y-%m-%d %H:%M:%S')}（间隔 {interval}s）=====\n")
    _state.update({"running": True, "interval": interval, "path": path})
    _state["thread"] = threading.Thread(target=_run, daemon=True, name="perf-monitor")
    _state["thread"].start()
    return path


def stop_performance_monitor() -> None:
    """停止监控（写结束标记）。"""
    _state["running"] = False
    _write_line(f"===== 监控结束 {time.strftime('%Y-%m-%d %H:%M:%S')} =====")


def log_snapshot_now(tag: str) -> dict:
    """立即采样并写一行带标签的日志（供 predict 前后打点）。"""
    gpu = gpu_snapshot()
    _write_line(f"{time.strftime('%H:%M:%S')} | [{tag}] {snapshot_line()}")
    return gpu


if __name__ == "__main__":
    p = start_performance_monitor(interval=2)
    print(f"监控已启动，日志：{p}（Ctrl+C 停止）")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_performance_monitor()
