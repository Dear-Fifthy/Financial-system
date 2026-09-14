"""性能监控模块：实时观察【线程列表 / 进程 CPU / GPU 占用】。

文件位置：infra/monitor__infra.py（按层归入子目录）。
用途：诊断"扫描慢到底是程序问题还是 PaddleOCR-VL 本身"——
  - 周期性采样（默认每 3s）写入 logs/perf_monitor.log（logs/ 目录自动创建）；
  - table__ui.py 启动时自动开启；benchmark_ocr__scan.py 用它做基准判定。

判定口径（配合日志看）：
  - 页面处理期间 GPU 利用率持续高（>70%）→ 计算确在 GPU 上，慢是模型本身特性；
  - GPU 利用率≈0 且 CPU 占用高 → 程序路径问题（误走 CPU / 等待 / 线程堆积）；
  - 线程列表可看到 扫描线程/脱敏线程/看门狗 是否存活、是否有堆积。

不依赖 psutil：CPU 用 os.times() 差值估算；GPU 用 nvidia-smi 快照。
"""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path

if __package__ in (None, ""):          # 直接 `python <层>/<模块>.py` 跑：把仓库根放回 sys.path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))


import os
import subprocess
import threading
import time
from pathlib import Path

from infra import proc__infra as _proc       # 静默子进程：避免 pythonw 下弹黑窗口

BASE_DIR = Path(__file__).resolve().parents[1]
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
    """nvidia-smi 一次性快照：GPU 利用率/显存/功耗；失败返回空 dict。

    ⚠️ 必须走 `_proc.run`：本模块每 3 秒采一次，用 pythonw（无控制台）启动时
    裸调 `subprocess.run` 会**每 3 秒弹出一个黑色控制台窗口**（用户实测）。
    """
    try:
        out = _proc.run(
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


def prune_old_logs(max_age_hours: int = 24) -> list[str]:
    """清理 logs/ 下超过 max_age_hours 的监控日志（默认 24 小时）。

    范围限制：只处理 logs/ **直属** 的 *.log（perf_monitor.log 等监控输出），
    不进入子目录——尤其不碰 logs/chat 下的会话数据（业务数据，非监控日志）。
    做法：旧文件清空保留（避免句柄/权限问题导致删除失败）。
    返回被清理的文件名列表；任何异常都不影响主流程。
    """
    cleared: list[str] = []
    try:
        LOG_DIR.mkdir(exist_ok=True)
        cutoff = time.time() - max_age_hours * 3600
        for path in LOG_DIR.glob("*.log"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.write_text("", encoding="utf-8")
                    cleared.append(path.name)
            except OSError:
                continue
    except Exception:
        pass
    return cleared


def start_performance_monitor(interval: int = 3) -> Path:
    """启动性能监控（幂等：已启动则直接返回日志路径）。

    启动时先按 24 小时保留策略清理旧监控日志（见 prune_old_logs）。
    采样：线程列表 + 进程 CPU% + GPU 利用率/显存/功耗。
    """
    if _state["running"]:
        return _state["path"]
    LOG_DIR.mkdir(exist_ok=True)
    cleared = prune_old_logs(24)
    path = LOG_DIR / "perf_monitor.log"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n===== 监控开始 {time.strftime('%Y-%m-%d %H:%M:%S')}（间隔 {interval}s）=====\n")
        if cleared:
            f.write(f"（已清理超过 24h 的旧监控日志：{', '.join(cleared)}）\n")
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
