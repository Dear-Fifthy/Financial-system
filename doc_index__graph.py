# -*- coding: utf-8 -*-
"""文档索引卡（**粗召回层**）：特征明文 + 概括 + 分类 + 少量关键事实。

用途与分工（对应"特征哈希/概括到底干嘛用"）：
    · **特征哈希** `file_feature_hashes(doc_key, feature_code, value_hash)`
      —— 机器比对/去重/配对用；sha256 片段**不可逆**，任何 AI 都读不到；
    · **特征明文** `.features.json` → `features`（project/date_start/doc_type/
      money_flow/four_flow/voucher_kind/counterparty/payment_term/tax_kind…）
      —— 这才是能给 AI 看的"结构化身份"；本模块优先读它；
      · 兜底：`hub_index.feature_hashes`（JSONB，存的是哈希，只能当"有哪些特征"的清单）
    · **概括** `l1_documents.doc_summary` —— 语义化"这份讲什么"，只作定位索引；
    · **事实层** `l1_facts` —— 第二层细读/取数（本模块只摘几条"关键事实"做提示）。

典型用法：
    cards = load_cards()                        # 每份文档一张卡
    text  = render(cards)                       # 给 AI 的第一层材料（可整份塞进提示词）
    hits  = recall_by_topk(question, cards, k=4) # 第一层召回：top-k（另一条腿是图遍历）

命令行：`python doc_index__graph.py` → 写出 logs/analysis/文档索引卡.{md,json}
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# hub 根随工作区（仓库）切换：见 workspace__infra（DSH_HUB_DIR + apply_active 热切换）
from workspace__infra import hub_root as _hub_root

HUB_DIR = _hub_root()
OUT_MD = Path(__file__).resolve().parent / "logs" / "analysis" / "文档索引卡.md"
OUT_JSON = Path(__file__).resolve().parent / "logs" / "analysis" / "文档索引卡.json"

# 特征字段中文名（给 AI 看的可读标签）
_FEATURE_LABELS = {
    "project": "项目", "project_parent": "上级项目", "date_start": "开始日期",
    "date_end": "结束日期", "doc_type": "文档类型", "money_flow": "资金方向",
    "four_flow": "四流", "voucher_kind": "凭证类型", "counterparty": "对手方",
    "payment_term": "付款条款", "tax_kind": "税种/税率", "contract_no": "合同编号",
}
# 索引卡里每份文档最多带几条"关键事实"
KEY_FACT_LIMIT = 8
_KEY_FACT_HEADERS = ("合同名称", "合同金额", "合同总额", "申请拨款金额", "本次支付", "付款金额",
                     "合计", "总额", "金额", "开票日期", "申请日期", "服务期", "付款方式",
                     "单位全称", "采购订单编号", "项目名称", "扣款金额", "罚款")


def _doc_rows() -> list[dict]:
    from database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT doc_no, doc_key, category, doc_summary FROM l1_documents "
                    "ORDER BY doc_no")
        return [{"doc_no": r[0], "doc_key": r[1], "category": r[2] or "",
                 "summary": (r[3] or "").strip()} for r in cur.fetchall()]


def _features_of(doc_key: str) -> tuple[dict, str]:
    """→ (特征明文, 来源标记)。优先 `.features.json`；缺则回退 hub_index（只有哈希）。"""
    side = HUB_DIR / f"{doc_key}.features.json"
    if side.exists():
        try:
            data = json.loads(side.read_text(encoding="utf-8"))
            feats = data.get("features") or {}
            if feats:
                return feats, "features.json"
        except Exception:
            pass
    try:
        from database_serv__infra import get_connection

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT feature_hashes FROM hub_index WHERE doc_key = %s", (doc_key,))
            row = cur.fetchone()
        if row and isinstance(row[0], dict) and row[0]:
            return {k: "<hash>" for k in row[0]}, "hub_index(仅哈希)"
    except Exception:
        pass
    return {}, ""


def _key_facts(doc_no: int, limit: int = KEY_FACT_LIMIT) -> list[str]:
    """该文档的关键事实（金额/编号/日期类优先），形如 `表头=值`。"""
    from database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT header, value FROM l1_facts WHERE doc_key = "
                    "(SELECT doc_key FROM l1_documents WHERE doc_no = %s) "
                    "AND header IS NOT NULL AND header <> '' LIMIT 400", (doc_no,))
        rows = [(h or "", str(v or "")) for h, v in cur.fetchall()]
    out, seen = [], set()
    ranked = sorted(rows, key=lambda hv: (0 if hv[0] in _KEY_FACT_HEADERS else 1,
                                          0 if re.search(r"[\d¥￥]", hv[1]) else 1))
    for h, v in ranked:
        k = (h, v[:30])
        if k in seen or v in ("", h):
            continue
        seen.add(k)
        out.append(f"{h}={v[:60]}")
        if len(out) >= limit:
            break
    return out


def load_cards(*, with_facts: bool = True) -> list[dict]:
    """每份文档一张索引卡（含特征明文、概括、分类、关键事实）。"""
    cards = []
    for r in _doc_rows():
        feats, src = _features_of(r["doc_key"])
        cards.append({**r, "features": feats, "features_src": src,
                      "key_facts": _key_facts(r["doc_no"]) if with_facts else []})
    return cards


def card_text(card: dict) -> str:
    """单张卡 → 文本（给 AI 看的一小段）。"""
    feats = "; ".join(
        f"{_FEATURE_LABELS.get(k, k)}={v}" for k, v in (card.get("features") or {}).items()
        if v not in (None, "", [], {}))
    lines = ["[%s] %s ｜ 分类:%s" % (card["doc_no"], card["doc_key"], card["category"])]
    lines.append("     特征: " + (feats or "（未提取）")
                 + ("" if not card.get("features_src") or card["features_src"] == "features.json"
                    else "（注意：只有哈希清单，无明文）"))
    lines.append("     概括: " + (card["summary"] or "（无）")[:120])
    if card.get("key_facts"):
        lines.append("     关键事实: " + "; ".join(card["key_facts"]))
    return "\n".join(lines)


def render(cards: list[dict]) -> str:
    """整份索引卡（可直接作为"第一层材料"塞进提示词）。"""
    head = ("【文档索引卡】每份文档一行：编号 / 文件名 / 分类 / 特征（结构化身份）/"
            "概括（定位索引）/ 关键事实（示例，取数请回事实层与原文核对）")
    return "\n".join([head] + [card_text(c) for c in cards])


# ---------------------------------------------------------------- 第一层召回（top-k）
_Q_STOP = ("是多少", "有多少", "几份", "哪些", "相关", "的单据", "是多少元", "合计")


def _question_signals(question: str) -> dict:
    q = question or ""
    codes = set(re.findall(r"(?:本公司·)?([A-Z]{2}\d{4}(?:-\d{2})?)", q))
    amounts = set(re.findall(r"\d[\d,]*\.?\d*", q.replace(",", "")))
    periods = set(re.findall(r"\d{2,4}\.\d{1,2}\.\d{1,2}\s*[-—~]\s*\d{2,4}\.\d{1,2}\.\d{1,2}", q))
    cjk = []
    for chunk in re.findall(r"[\u4e00-\u9fa5]{2,10}", q):
        for n in (6, 4, 3, 2):
            for i in range(0, max(len(chunk) - n + 1, 0)):
                t = chunk[i:i + n]
                if t not in _Q_STOP and not t.endswith(_Q_STOP):
                    cjk.append(t)
    return {"codes": codes, "amounts": amounts, "periods": periods, "cjk": set(cjk)}


def score_card(question: str, card: dict, sig: dict | None = None) -> float:
    """问题 × 索引卡的打分（机械可复核：编号/金额/期间/中文词命中加权）。"""
    sig = sig or _question_signals(question)
    text = card["doc_key"] + " " + (card.get("summary") or "") + " " + \
        json.dumps(card.get("features") or {}, ensure_ascii=False) + " " + \
        " ".join(card.get("key_facts") or [])
    s = 0.0
    for c in sig["codes"]:
        if c in text:
            s += 6.0
    for a in sig["amounts"]:
        if len(a) >= 4 and a in text.replace(",", ""):
            s += 4.0
    for p in sig["periods"]:
        if p in text:
            s += 5.0
    for t in sig["cjk"]:
        if t in text:
            s += 0.5 + 0.25 * (len(t) - 2)
    # 类别词（发票/合同）命中分类字段
    for cat in ("发票", "合同", "物流凭证"):
        if cat in question and cat == card.get("category"):
            s += 3.0
    return round(s, 2)


def recall_by_topk(question: str, cards: list[dict] | None = None, *, k: int = 4) -> list[dict]:
    """第一层召回（top-k 策略）：返回 `[{doc_no, score, card}]`，按分数降序。"""
    cards = cards or load_cards(with_facts=False)
    sig = _question_signals(question)
    scored = [{"doc_no": c["doc_no"], "score": score_card(question, c, sig), "card": c}
              for c in cards]
    scored.sort(key=lambda x: (-x["score"], x["doc_no"]))
    return [x for x in scored[:k] if x["score"] > 0]


def write_artifact(md_path: Path = OUT_MD, json_path: Path = OUT_JSON) -> dict:
    cards = load_cards()
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("# 文档索引卡（特征明文 + 概括 + 关键事实）\n\n```\n"
                       + render(cards) + "\n```\n", encoding="utf-8")
    json_path.write_text(json.dumps(cards, ensure_ascii=False, indent=1), encoding="utf-8")
    have = sum(1 for c in cards if c.get("features_src") == "features.json")
    return {"docs": len(cards), "with_features": have, "md": str(md_path),
            "json": str(json_path)}


if __name__ == "__main__":
    print(json.dumps(write_artifact(), ensure_ascii=False, indent=1))
