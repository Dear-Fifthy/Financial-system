"""OCR 前释放资源：把前面阶段占用的内存/显存尽量还回去，让 OCR 拿到最干净的机器。

做什么（全部 best-effort，失败绝不影响主流程）：
  1. **回收 Python 侧缓存**：清空本项目里的内存缓存（项目/本公司登记索引、去重缓存等），
     再 `gc.collect()`；
  2. **Windows 工作集裁剪**：`EmptyWorkingSet` 把空闲页还给系统（"释放内存"的直接手段）；
  3. **GPU 分配器缓存**：`paddle.device.cuda.empty_cache()`（PaddleOCR 用的就是 Paddle），
     若主进程里意外有 torch 也一并清（正常不会 import，embedding 走子进程）；
  4. **关闭遗留的 embedding 子进程**：rag_store 用 `subprocess.run` 短进程，正常已退出；
     这里额外清理可能残留的 `embed_worker__rag.py` 子进程（避免它占 CPU/内存拖慢 OCR）；
  5. **记录前后对比**：进程 RSS + Paddle 显存 allocated/reserved/total，
     写 `logs/perf/resource_release_<日期>.jsonl`，并在日志里打印一行摘要。

调用时机：扫描队列里"需要 OCR 的那一组"开始之前（见 table__ui.py 的询问弹窗），
以及 `scanner_core` 第一次真的要 OCR 时兜底调用一次。
"""
from __future__ import annotations

import gc
import json
import os
import importlib
import sys
import time
from pathlib import Path

__all__ = ["release_before_ocr", "last_report", "snapshot"]

LOG_DIR = Path(__file__).resolve().parents[1] / "logs" / "perf"
_MIN_INTERVAL_S = 20.0          # 同一进程内重复调用的最小间隔（避免每份文件都做一遍）
_LAST: dict = {"ts": 0.0, "report": {}}


def _rss_mb() -> float | None:
    """当前进程物理内存占用（MB）：优先 psutil，其次 Windows API，最后 POSIX。"""
    try:
        import psutil

        return round(psutil.Process().memory_info().rss / 1024 / 1024, 1)
    except Exception:
        pass
    try:
        import ctypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters),
            counters.cb)
        if ok:
            return round(counters.WorkingSetSize / 1024 / 1024, 1)
    except Exception:
        pass
    try:
        import resource  # POSIX

        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:
        return None


def _trim_working_set() -> bool:
    """Windows：把工作集里的空闲页还给系统（真正的"释放物理内存"）。

    先试 EmptyWorkingSet；失败再试 SetProcessWorkingSetSize(-1,-1)（两者都需要
    PROCESS_SET_QUOTA 权限，权限不足时返回 False —— best-effort，不影响主流程）。
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.EmptyWorkingSet(handle):
            return True
        if kernel32.SetProcessWorkingSetSize(handle, ctypes.c_size_t(-1), ctypes.c_size_t(-1)):
            return True
    except Exception:
        pass
    return False


def _gpu_stats() -> dict:
    """Paddle 显存统计（**只在已经加载过 paddle/torch 时才读取**）。

    刻意不主动 import paddle：那会把整套 Paddle 运行时拉进内存（几百 MB），
    与"OCR 前释放内存"完全相反。
    """
    out: dict = {}
    paddle = sys.modules.get("paddle")
    if paddle is not None:
        try:
            out["device"] = str(paddle.device.get_device())
        except Exception:
            pass
        try:
            out["allocated_mb"] = round(paddle.device.cuda.memory_allocated() / 1024 / 1024, 1)
            out["reserved_mb"] = round(paddle.device.cuda.memory_reserved() / 1024 / 1024, 1)
        except Exception:
            pass
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            if torch.cuda.is_available():
                out["torch_allocated_mb"] = round(torch.cuda.memory_allocated() / 1024 / 1024, 1)
        except Exception:
            pass
    return out


def _clear_cuda_cache() -> dict:
    done = {"paddle_empty_cache": False}
    paddle = sys.modules.get("paddle")
    if paddle is not None:
        try:
            paddle.device.cuda.empty_cache()
            done["paddle_empty_cache"] = True
        except Exception:
            pass
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
                done["torch_empty_cache"] = True
        except Exception:
            pass
    return done


def _warmup() -> None:
    """预热"步骤里会 import 的模块"和 CUDA 上下文，让前后对比不受初始化开销干扰。

    注意：Paddle 的 CUDA 上下文是**第一次调用 CUDA API 时**才建立的（会占几百 MB
    进程内存与显存）。若把这一步算进"释放前"，就会出现"释放后反而更高"的假象；
    这里先摸一次，再快照。
    """
    for name in ("psutil", "desens.project_registry__desens", "desens.self_entity__desens", "subprocess", "ctypes"):
        try:
            __import__(name)
        except Exception:
            continue
    paddle = sys.modules.get("paddle")
    if paddle is not None:
        try:
            paddle.device.cuda.empty_cache()
            paddle.device.cuda.memory_allocated()
            paddle.device.cuda.memory_reserved()
        except Exception:
            pass


def _clear_app_caches() -> list[str]:
    """清本项目自己的内存缓存（登记索引/去重缓存等）。"""
    cleared: list[str] = []
    for mod_name, fn_name in (("desens.project_registry__desens", "invalidate_cache"),
                              ("desens.self_entity__desens", "invalidate_cache"),
                              ("desens.dedup__desens", None),
                              ("graph.edge_build__graph_edges", None)):
        try:
            mod = __import__(mod_name)
            if fn_name and hasattr(mod, fn_name):
                getattr(mod, fn_name)()
                cleared.append(f"{mod_name}.{fn_name}")
        except Exception:
            continue
    return cleared


def _kill_embed_workers() -> int:
    """结束残留的 embedding 子进程（rag_store 走短进程，正常已退出）。"""
    killed = 0
    try:
        import sys

        from infra import proc__infra as _proc

        if os.name != "nt":
            return 0
        out = _proc.run(["wmic", "process", "where", "name='python.exe'",
                         "get", "ProcessId,CommandLine", "/format:csv"],
                        capture_output=True, text=True, timeout=15)
        for line in (out.stdout or "").splitlines():
            if "embed_worker__rag.py" in line and str(sys.executable) not in line.split(",")[0]:
                pid = line.rstrip().rsplit(",", 1)[-1].strip()
                if pid.isdigit():
                    _proc.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
                    killed += 1
    except Exception:
        pass
    return killed


def snapshot() -> dict:
    """当前资源快照（供日志/UI 展示）。"""
    return {"rss_mb": _rss_mb(), "gpu": _gpu_stats()}


def release_before_ocr(*, force: bool = False, reason: str = "") -> dict:
    """OCR 前释放内存/显存；返回前后对比报告（同时写日志）。

    force=False 时，若距上次释放不足 `_MIN_INTERVAL_S` 秒则跳过（避免每份文件都做一遍）。
    """
    now = time.time()
    if not force and (now - _LAST["ts"]) < _MIN_INTERVAL_S:
        return {**_LAST["report"], "skipped": "距上次释放过近（<%.0fs）" % _MIN_INTERVAL_S}

    _warmup()                              # 先预热，避免把 import 增长算进"释放"对比
    before = snapshot()
    steps: dict = {}
    steps["app_caches"] = _clear_app_caches()
    try:
        steps["gc_collected"] = gc.collect()
    except Exception:
        steps["gc_collected"] = 0
    steps["working_set_trimmed"] = _trim_working_set()
    steps.update(_clear_cuda_cache())
    steps["embed_workers_killed"] = _kill_embed_workers()
    time.sleep(0.05)                       # 给系统一点时间回收，读数才可信
    after = snapshot()

    rss_before, rss_after = before.get("rss_mb"), after.get("rss_mb")
    gpu_before, gpu_after = before.get("gpu", {}), after.get("gpu", {})
    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason or "开始 OCR 前释放资源",
        "rss_before_mb": rss_before, "rss_after_mb": rss_after,
        "rss_delta_mb": (round(rss_after - rss_before, 1)
                         if (rss_before is not None and rss_after is not None) else None),
        "gpu_before": gpu_before, "gpu_after": gpu_after,
        "gpu_reserved_delta_mb": (round(gpu_before.get("reserved_mb", 0)
                                        - gpu_after.get("reserved_mb", 0), 1)
                                  if gpu_before.get("reserved_mb") is not None
                                  and gpu_after.get("reserved_mb") is not None else None),
        "steps": steps,
        "note": "显存（reserved/allocated）是 OCR 的关键资源；RSS 不一定下降（Python 内存池"
                "会保留已分配页，工作集裁剪需 PROCESS_SET_QUOTA 权限）。",
    }
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / f"resource_release_{time.strftime('%Y%m%d')}.jsonl").open(
                "a", encoding="utf-8") as f:
            f.write(json.dumps(report, ensure_ascii=False) + "\n")
    except Exception:
        pass
    _LAST["ts"] = now
    _LAST["report"] = report
    print(f"[资源释放] RSS {report['rss_before_mb']}→{report['rss_after_mb']} MB"
          f"（Δ{report['rss_delta_mb']}）｜显存 reserved "
          f"{report['gpu_before'].get('reserved_mb')}→{report['gpu_after'].get('reserved_mb')} MB"
          f"（释放 {report['gpu_reserved_delta_mb']} MB）｜步骤 {steps}", flush=True)
    return report


def last_report() -> dict:
    return dict(_LAST["report"])
