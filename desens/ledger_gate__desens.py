"""合同判定闸门（本轮按新口径重写）：**只判"是不是合同"，不再判断"能不能入账"**。

与旧版的区别（重要）：
  · 旧版：分类必须是"合同"，再用**结构证据**（甲乙方/期限/违约条款/金额…）凑够 2 项才放行
    —— 那是为了防"关键词误判的发票/汇总表被自动写进台账"；
  · 新版：判定口径收敛为 `contract_rules__desens` 的**一句话规则**：
      「文件是 PDF/Word」+「文件名或标题含"合同/协议"」。
    而**入账本身不再自动发生**：确认是合同后，只把台账字段送进"待入账队列"
    （`ledger_inbox__desens`），由用户在"入账审核"窗口逐条决定入不入账。
    所以闸门只需负责"分类是否可信"，不必再用结构证据兜底拦截。

对外接口（保持向后兼容，老调用点无需改名）：
  evaluate(pages, *, file_name=None, category=None, tables=None) -> GateDecision
  evaluate_hub(hub_doc) -> GateDecision
  should_write_ledger(result_or_gate) -> (bool, reason)   # 现在=「是否认定为合同」
  log_decision(doc_key, decision, extra=None)
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

__all__ = [
    "GateDecision",
    "evaluate",
    "evaluate_hub",
    "should_write_ledger",
    "MIN_STRONG_HITS",
    "log_decision",
]

# 兼容保留：新版不再用"强证据凑数"，此常量仅作历史引用（勿在新逻辑里依赖）
MIN_STRONG_HITS = 0
LOG_DIR = Path(__file__).resolve().parents[1] / "logs" / "ledger_gate"


@dataclass
class GateDecision:
    """闸门判定结果（可序列化，写进处理报告与日志）。"""
    passed: bool = False
    category: str = ""
    strong_hits: list[str] = field(default_factory=list)
    weak_hits: list[str] = field(default_factory=list)
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    decided_at: str = ""
    rule: str = "contract_rules:pdf_word+title_token"

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate(
    pages: list[str],
    *,
    file_name: str | None = None,
    category: str | None = None,
    tables: list[dict] | None = None,
) -> GateDecision:
    """判定"这份文档是不是合同"（PDF/Word + 命名/标题含"合同/协议"）。

    `category`、`tables` 保留在签名里只为兼容旧调用点（新规则不看它们）。
    """
    import desens.contract_rules__desens as contract_rules

    name = str(file_name or "")
    pages = [str(p or "") for p in (pages or [])]
    verdict = contract_rules.is_contract_doc(name, pages, tables)
    d = GateDecision(category=str(category or ""), passed=bool(verdict.passed),
                     decided_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    # 证据列表：把新口径的两条依据映射到旧字段名上，便于既有日志/界面继续显示
    if verdict.in_scope:
        d.strong_hits.append(f"pdf_word:{verdict.ext}")
    if verdict.hit_in == "filename":
        d.strong_hits.append(f"filename_token:{verdict.hit_token}")
    elif verdict.hit_in == "title":
        d.strong_hits.append(f"title_token:{verdict.hit_token}")
    d.weak_hits = [f"tokens:{'/'.join(verdict.tokens)}"]
    d.score = len(d.strong_hits)
    d.reasons = list(verdict.reasons)
    if not verdict.passed:
        d.reasons.append("→ 不入账队列（不是合同）；发票/其它文档不受影响")
    return d


def evaluate_hub(hub_doc: dict) -> GateDecision:
    """从 hub JSON（pages/source_file/category）直接判定。"""
    return evaluate(
        list(hub_doc.get("pages") or []),
        file_name=hub_doc.get("source_file"),
        category=hub_doc.get("category"),
        tables=list(hub_doc.get("tables") or []),
    )


def should_write_ledger(result_or_gate: dict | GateDecision | None) -> tuple[bool, str]:
    """**是否认定为合同**（旧接口名保留：调用点已改为"送待入账队列"而不是直接写库）。

    接受：处理结果 dict（取其中的 ledger_gate__desens）或 GateDecision。
    """
    if result_or_gate is None:
        return False, "没有闸门判定结果——按「不是合同」处理"
    if isinstance(result_or_gate, GateDecision):
        gate = result_or_gate.to_dict()
    else:
        gate = result_or_gate.get("ledger_gate__desens") if "ledger_gate__desens" in result_or_gate \
            else result_or_gate
    if not isinstance(gate, dict):
        return False, "闸门判定缺失或格式异常——按「不是合同」处理"
    if gate.get("passed") is True:
        return True, "认定为合同（PDF/Word + 命名/标题含合同/协议）"
    return False, "；".join(gate.get("reasons") or ["不是合同"])


def log_decision(doc_key: str, decision: GateDecision, *, extra: dict | None = None) -> None:
    """闸门判定审计日志（只记判定依据，不记正文）。"""
    import json
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"doc_key": doc_key, "ts": decision.decided_at, **decision.to_dict()}
        if extra:
            rec.update(extra)
        with (LOG_DIR / f"gate_{time.strftime('%Y%m%d')}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
