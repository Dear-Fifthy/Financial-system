"""扫描停止（协作式取消）：让"停止扫描"能在**页边界**安全中断整条流水线。

为什么是"协作式"而不是杀线程：
  · OCR 推理（PaddleOCR-VL）跑在 C++/CUDA 里，硬杀线程没有安全的收尾点，
    可能留下半截模型状态、半截 JSON；
  · 所以采用**检查点**方案：扫描/脱敏/AI 每一步之间调 `check()`，
    发现"已请求停止"就抛 `ScanCancelled`，让当前文件干净地退出：
      - 已落盘的页 JSON 保留（可复用），**源文件绝不动**；
      - 当前**正在推理的那一页**会先跑完（无法从模型内部中断），
        下一页开始前立即停下 —— 这是本方案的能力边界，UI 提示也照此说明；
      - 停止后不会进入 AI 台账/L1 概括/L1 事实等后续步骤（省 token/算力）。

作用域：一次"扫描会话"= 一个全局令牌。UI 起批量时 `begin_scan()`，
点"停止"时 `request_stop()`；下一个文件开始前若令牌已停止，UI 直接不再启动。

对外接口：
  begin_scan(scope) -> CancelToken     开一次会话（丢弃旧令牌）
  request_stop(reason, by) -> dict     请求停止（幂等；写审计日志）
  check(where)                         检查点：已停止则抛 ScanCancelled
  is_stopped() / status() / end_scan() 状态查询与收尾
  ScanCancelled                        取消异常（UI 据此把任务标成"已停止"）
  cancel_log_path()                    审计日志位置
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ScanCancelled",
    "CancelToken",
    "begin_scan",
    "request_stop",
    "check",
    "is_stopped",
    "status",
    "end_scan",
    "current",
    "cancel_log_path",
]

BASE_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = BASE_DIR / "logs" / "perf"


class ScanCancelled(Exception):
    """用户请求停止扫描（协作式取消）。UI 把该异常显示为"已停止"。"""

    def __init__(self, where: str = "", reason: str = "") -> None:
        self.where = where
        self.reason = reason
        super().__init__(f"已停止扫描（{where or '检查点'}）：{reason or '用户请求停止'}")


@dataclass
class CancelToken:
    scope: str = "scan"
    stop_event: threading.Event = field(default_factory=threading.Event)
    reason: str = ""
    requested_by: str = ""
    requested_at: float = 0.0
    started_at: float = field(default_factory=time.time)
    last_check: str = ""
    checks: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ---- 状态 ----
    @property
    def stopped(self) -> bool:
        return self.stop_event.is_set()

    def snapshot(self) -> dict:
        return {
            "scope": self.scope,
            "stopped": self.stopped,
            "reason": self.reason,
            "requested_by": self.requested_by,
            "requested_at": (time.strftime("%Y-%m-%d %H:%M:%S",
                                           time.localtime(self.requested_at))
                             if self.requested_at else None),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at)),
            "last_check": self.last_check,
            "checks": self.checks,
        }


_TOKEN: CancelToken | None = None
_TOKEN_LOCK = threading.Lock()


def begin_scan(scope: str = "scan") -> CancelToken:
    """开一次扫描会话：清掉上一次的"已停止"标记（新一批文件可以正常跑）。"""
    global _TOKEN
    with _TOKEN_LOCK:
        _TOKEN = CancelToken(scope=scope)
        token = _TOKEN
    _log({"event": "begin", **token.snapshot()})
    return token


def current() -> CancelToken | None:
    return _TOKEN


def is_stopped() -> bool:
    t = _TOKEN
    return bool(t and t.stopped)


def check(where: str = "") -> None:
    """检查点：如果已请求停止 → 抛 ScanCancelled。

    刻意做得极轻（一次 Event.is_set()），可以放心放在逐页循环里。
    """
    t = _TOKEN
    if t is None:
        return
    with t._lock:
        t.checks += 1
        if where:
            t.last_check = where
    if t.stopped:
        raise ScanCancelled(where=where or t.last_check, reason=t.reason)


def request_stop(reason: str = "用户点击停止扫描", by: str = "") -> dict:
    """请求停止（幂等）。返回令牌快照，供 UI 展示/落日志。"""
    global _TOKEN
    with _TOKEN_LOCK:
        if _TOKEN is None:
            _TOKEN = CancelToken(scope="scan")
        token = _TOKEN
        first = not token.stopped
        token.reason = reason
        token.requested_by = by
        if first:
            token.requested_at = time.time()
        token.stop_event.set()
    snap = token.snapshot()
    if first:
        _log({"event": "stop_requested", **snap})
    return snap


def end_scan(note: str = "") -> None:
    """会话收尾（UI 在队列清空时调用）；保留最终快照供展示。"""
    t = _TOKEN
    if t is not None:
        _log({"event": "end", "note": note, **t.snapshot()})


def status() -> dict:
    t = _TOKEN
    if t is None:
        return {"active": False, "stopped": False, "checks": 0}
    return {"active": True, **t.snapshot()}


def cancel_log_path() -> Path:
    return LOG_DIR / f"scan_stop_{time.strftime('%Y%m%d')}.jsonl"


def _log(record: dict) -> None:
    """停止/开始审计日志（只记状态，不含文档内容）。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **record}
        with cancel_log_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
