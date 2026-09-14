"""运行时"重活闸门"：把会吃 GPU/CPU 的重活串行化，避免同一时刻抢资源。

现状（检查结论，见 `describe()`）：
  · 扫描侧本来就是**线性单飞**：待选区一次只起一个 QThread 处理一个文件
    （table__ui.py `_start_next_task` 有 `_current_thread is not None` 早退，下一个
     文件只在 `thread.finished` 回调里启动）；
  · 单文件内部有 1 个 OCR 线程 + 1 个脱敏消费线程（后者只做 CPU/DB），互不争 GPU；
  · **唯一真实的抢资源风险**：AI 步骤里的后台 embedding 线程（RAG 模式）可能在
    **下一份文件的 OCR 已经开始时**还在跑（两者都要 GPU/CPU）→ 显存/算力被两份活分走。

本模块提供一把**全局重活锁**（Semaphore(1)）：OCR 扫描与 embedding/向量化都必须先拿锁，
于是"同一时刻只有一个重活"成为结构性保证，而不是靠调用顺序自觉。
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

__all__ = ["heavy_lane", "heavy_lane_guard", "stats", "describe", "reset_stats",
           "lane_status", "start_watchdog", "stop_watchdog", "incidents", "clear_incidents"]

_LANE = threading.Semaphore(1)
_LOCK = threading.Lock()          # 保护下面的簿记（不参与调度，避免自己成为瓶颈）
_HOLDER: dict | None = None       # {"name","since","thread","stack"}
_WAITERS: dict[str, int] = {}     # 用途 -> 正在等待的数量
_STATS = {
    "acquired": 0,          # 拿到锁的次数
    "waited": 0,            # 需要等待的次数
    "wait_ms_total": 0.0,   # 累计等待毫秒
    "max_wait_ms": 0.0,
    "holders": {},          # 各用途拿锁次数
}
_INCIDENTS: list[dict] = []       # 挤压/卡死事件（UI 可轮询展示）
_WATCHDOG: dict = {"thread": None, "stop": None, "warned": set()}
LOG_DIR = Path(__file__).resolve().parents[1] / "logs" / "perf"
WARN_S = float(os.getenv("LANE_WARN_S", "120"))      # 单个重活持续超过 → 告警
CRIT_S = float(os.getenv("LANE_CRIT_S", "600"))      # 超过 → 判为疑似卡死
WAIT_WARN_N = int(os.getenv("LANE_WAIT_WARN_N", "3"))  # 同时等待者 ≥ N → 挤压告警
CHECK_INTERVAL_S = float(os.getenv("LANE_CHECK_INTERVAL_S", "5"))


def _now() -> float:
    return time.time()


def _log_incident(kind: str, level: str, message: str, detail: dict) -> dict:
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "level": level,
           "message": message, **detail}
    with _LOCK:
        _INCIDENTS.append(rec)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / f"lane_watchdog_{time.strftime('%Y%m%d')}.jsonl").open(
                "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    print(f"[重活闸门·{level}] {message}", flush=True)
    return rec


@contextmanager
def heavy_lane(name: str = "heavy", *, timeout: float | None = None, cancel_check=None):
    """获取"重活"独占权（同一时刻仅一个持有者）。

    name：用途标识（如 ocr_scan / embed / project_batch），用于统计与日志。
    timeout：最长等待秒数；超时抛 TimeoutError（默认无限等待——宁可排队，也不要抢资源）。
    cancel_check：可选回调，等待期间每 ~0.25s 调一次；它抛异常（如 ScanCancelled）
      时**立即放弃等待**并把异常向上抛（用于"停止扫描"不必等前一个重活跑完）。
    """
    global _HOLDER
    t0 = time.perf_counter()
    with _LOCK:
        _WAITERS[name] = _WAITERS.get(name, 0) + 1
    got = False
    try:
        if cancel_check is None:
            got = _LANE.acquire(timeout=timeout) if timeout else _LANE.acquire()
        else:
            deadline = None if timeout is None else t0 + timeout
            while True:
                cancel_check()                       # 取消优先于排队
                slice_s = 0.25
                if deadline is not None:
                    left = deadline - time.perf_counter()
                    if left <= 0:
                        break
                    slice_s = max(0.05, min(slice_s, left))
                if _LANE.acquire(timeout=slice_s):
                    got = True
                    break
    finally:
        with _LOCK:
            _WAITERS[name] = max(0, _WAITERS.get(name, 0) - 1)
    waited_ms = (time.perf_counter() - t0) * 1000.0
    with _LOCK:
        if not got:
            _log_incident("lane_timeout", "ERROR", f"重活闸门等待超时（{name}，{timeout}s）",
                          {"name": name, "timeout": timeout})
            raise TimeoutError(f"重活闸门等待超时（{name}，{timeout}s）")
        if waited_ms > 1.0:
            _STATS["waited"] += 1
        _STATS["acquired"] += 1
        _STATS["wait_ms_total"] += waited_ms
        _STATS["max_wait_ms"] = max(_STATS["max_wait_ms"], waited_ms)
        _STATS["holders"][name] = _STATS["holders"].get(name, 0) + 1
        _HOLDER = {"name": name, "since": _now(), "thread": threading.current_thread().name,
                   "pid": os.getpid()}
        waiting_now = {k: v for k, v in _WAITERS.items() if v > 0}
    if waiting_now and sum(waiting_now.values()) >= WAIT_WARN_N:
        _log_incident("lane_contention", "WARN",
                      f"重活挤压：{name} 拿到锁时仍有 {sum(waiting_now.values())} 个等待者",
                      {"holder": name, "waiting": waiting_now,
                       "waited_ms": round(waited_ms, 1)})
    try:
        yield waited_ms
    finally:
        with _LOCK:
            if _HOLDER and _HOLDER.get("name") == name:
                _HOLDER = None
        _LANE.release()


def heavy_lane_guard(name: str = "heavy"):
    """装饰器版：把整个函数体放进重活闸门。"""
    def deco(fn):
        def wrapper(*args, **kwargs):
            with heavy_lane(name):
                return fn(*args, **kwargs)
        wrapper.__name__ = getattr(fn, "__name__", "wrapped")
        wrapper.__doc__ = getattr(fn, "__doc__", None)
        return wrapper
    return deco


def stats() -> dict:
    s = dict(_STATS)
    s["holders"] = dict(_STATS["holders"])
    s["wait_ms_total"] = round(s["wait_ms_total"], 1)
    s["max_wait_ms"] = round(s["max_wait_ms"], 1)
    s["concurrent_holders_max"] = 1 if s["acquired"] else 0   # 结构性上限就是 1
    return s


def lane_status() -> dict:
    """当前闸门状态（谁在跑、跑了多久、谁在等）——供 UI/日志展示。"""
    with _LOCK:
        holder = dict(_HOLDER) if _HOLDER else None
        waiting = {k: v for k, v in _WAITERS.items() if v > 0}
    if holder:
        holder["held_s"] = round(_now() - holder["since"], 1)
    return {"holder": holder, "waiting": waiting, "waiting_total": sum(waiting.values()),
            "warn_s": WARN_S, "crit_s": CRIT_S, "incidents": len(_INCIDENTS)}


def incidents(*, level: str | None = None) -> list[dict]:
    """挤压/卡死事件（新→旧）。level 传 WARN/CRITICAL/ERROR 可过滤。"""
    with _LOCK:
        items = list(_INCIDENTS)
    if level:
        items = [i for i in items if i.get("level") == level]
    return list(reversed(items))


def clear_incidents() -> None:
    with _LOCK:
        _INCIDENTS.clear()
        _WATCHDOG["warned"].clear()


def _watch_loop(stop: threading.Event) -> None:
    """看门狗：定期检查"单个重活跑了多久 + 有多少人在等"，超阈值就落日志（并记事件）。"""
    while not stop.wait(CHECK_INTERVAL_S):
        with _LOCK:
            holder = dict(_HOLDER) if _HOLDER else None
            waiting = sum(v for v in _WAITERS.values() if v > 0)
            warned = _WATCHDOG["warned"]
        if not holder:
            continue
        held = _now() - holder["since"]
        name = holder.get("name")
        if held >= CRIT_S and f"crit:{name}" not in warned:
            warned.add(f"crit:{name}")
            _log_incident("lane_stuck", "CRITICAL",
                          f"疑似卡死：重活「{name}」已持续 {held:.0f}s（阈值 {CRIT_S:.0f}s）"
                          + (f"，另有 {waiting} 个等待者" if waiting else "，无等待者"),
                          {"holder": holder, "held_s": round(held, 1), "waiting": waiting,
                           "thread": holder.get("thread")})
        elif held >= WARN_S and f"warn:{name}" not in warned:
            warned.add(f"warn:{name}")
            _log_incident("lane_slow", "WARN",
                          f"重活「{name}」已持续 {held:.0f}s（告警阈值 {WARN_S:.0f}s）",
                          {"holder": holder, "held_s": round(held, 1), "waiting": waiting})
        if waiting >= WAIT_WARN_N and f"wait:{name}" not in warned:
            warned.add(f"wait:{name}")
            _log_incident("lane_contention", "WARN",
                          f"重活挤压：{waiting} 个任务在等「{name}」（已跑 {held:.0f}s）",
                          {"holder": holder, "held_s": round(held, 1), "waiting": waiting})


def start_watchdog() -> bool:
    """启动看门狗（幂等）。返回是否本次真正启动。"""
    with _LOCK:
        t = _WATCHDOG.get("thread")
        if t is not None and t.is_alive():
            return False
        stop = threading.Event()
        thread = threading.Thread(target=_watch_loop, args=(stop,), daemon=True,
                                  name="lane-watchdog")
        _WATCHDOG["stop"] = stop
        _WATCHDOG["thread"] = thread
    thread.start()
    return True


def stop_watchdog() -> None:
    with _LOCK:
        stop = _WATCHDOG.get("stop")
        _WATCHDOG["thread"] = None
    if stop is not None:
        stop.set()


def reset_stats() -> None:
    _STATS.update({"acquired": 0, "waited": 0, "wait_ms_total": 0.0, "max_wait_ms": 0.0,
                   "holders": {}})


def describe() -> dict:
    """并发现状说明（供 UI/日志/自测引用，避免"以为在并发"或"以为没抢资源"）。"""
    return {
        "scan_mode": "linear-single-flight",       # 待选区：一次一个文件（QThread 串行）
        "per_file_threads": {
            "producer": "OCR / 直读（QThread，占 GPU）",
            "consumer": "脱敏 + 坐标装配（普通线程，CPU/DB，不占 GPU）",
            "watchdog": "慢页看门狗（scanner_core，5s 轮询，不推理）",
        },
        "heavy_lane": "OCR 扫描 与 embedding/向量化 共用一把全局锁 → 不会同时跑两份重活",
        "risk_fixed": "AI 步骤的后台 embedding 曾经可能与下一份文件的 OCR 重叠（抢 GPU/显存）",
    }
