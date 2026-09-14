"""L3 查询层：图遍历取证 —— **返回值 + 置信度 + 溯源路径**。

流程（四步，全部可离线运行；AI 只用于可选的语义补全）：
  1. **语义补全** `parse_query(问题)` → QueryPlan：
       锚点：脱敏编号（CO/PJ/TX…、[本公司·CO####]）、单据号（KH-2026-02/FP-0001）、
             金额、项目名、文档类型（合同/发票/付款/物流）；
       关系意图：关键词 → 允许的关系集合（发票→contract_to_invoice、付款→payment_for_contract…）；
       方向：出现"对应哪份/来源/上游"→ 反向。
  2. **锚点定位** `locate(plan)`：在 `l1_facts` 里找命中锚点的事实 → 其所属 doc/row 节点
       （每个锚点带 anchor_confidence：编号/单号精确命中 1.0，项目名 0.95，金额 0.6…）。
  3. **图遍历** `traverse(...)`：沿 `graph_edges` 走链；**默认只用 validated 边**，
       `include_hypothesis=True` 时纳入假设边并**降权**（×0.7）；环路去重、深度受限。
  4. **结果** `query(...)`：chains（每跳关系/状态/置信度/依据 + 两侧**具体值及其坐标**）、
      confidence（锚点置信度 × 各跳置信度，多路径取最大）、unresolved（没走通的意图）、notes。

为什么强调溯源：概括/编号只是**定位索引**，取数与核对必须能回到原文——
每条证据都带 doc_key / fact_id / 页码 / 表块行列 / Excel 坐标 / 脱敏文本字符区间 /
原文区间（raw_char_* + raw_exact），可直接在 hub 页文本里切片验证。

人工确认：`confirm_edge()` / `reject_edge()` 把假设边转为 validated / rejected（假设→证实闭环）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from pathlib import Path as _Path

__all__ = [
    "RELATION_KEYWORDS",
    "parse_query",
    "locate",
    "traverse",
    "query",
    "chains_for_doc",
    "evidence_block",
    "fetch_span",
    "SpanBudget",
    "list_hypotheses",
    "confirm_edge",
    "reject_edge",
]

BASE_DIR = Path(__file__).resolve().parent
# 原文片段只允许从 hub（脱敏产物）取；hub 根随工作区（仓库）切换：见 workspace__infra
from workspace__infra import hub_root as _hub_root

HUB_DIR = _hub_root()
LOG_DIR = BASE_DIR / "logs" / "graph"
HYPOTHESIS_WEIGHT = 0.7          # 假设边降权
DEFAULT_CHAIN_RELATIONS = ("contract_to_invoice", "payment_for_contract",
                           "contract_to_logistics", "table_row_matches_doc")

_CODE_RE = re.compile(r"\[(?:本公司·)?[A-Z]{2}\d{4}\]|(?<![A-Za-z])(?:CO|PT|PJ|TX|BK|BA)\d{4}(?![0-9])")
_DOCNO_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,}[A-Z0-9]*(?:[-_][A-Z0-9]+)+)(?![A-Za-z0-9])")
_AMOUNT_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+\.\d{2})(?![\d])")
_TYPE_KEYWORDS = {"合同": "合同", "发票": "发票", "物流": "物流凭证", "运单": "物流凭证",
                  "付款": "付款", "收款": "付款", "凭证": "付款"}

RELATION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "contract_to_invoice": ("发票", "开票", "专票", "普票", "价税合计", "税额"),
    "payment_for_contract": ("付款", "收款", "回款", "资金", "转账", "支付", "结算"),
    "contract_to_logistics": ("物流", "运单", "发货", "提货", "运输", "快递", "签收"),
    "same_project": ("项目", "工程", "同一项目"),
    "counterparty_shared": ("对手方", "对方公司", "同一家", "往来单位"),
    "amount_consistent": ("金额", "对账", "一致", "核对"),
    "table_row_matches_doc": ("汇总表", "台账", "明细表", "表格", "表里", "这一行", "那一行"),
}
_REVERSE_HINTS = ("哪份合同", "哪一份合同", "对应哪", "来源", "上游", "上游合同", "是谁的")
_ANCHOR_BY_KIND = {
    "code": 1.0, "doc_no": 1.0, "project": 0.95, "type": 0.8, "amount": 0.6, "text": 0.7,
}


# =========================================================
# 1. 语义补全
# =========================================================
def parse_query(question: str, *, relations: list[str] | None = None) -> dict:
    """问题 → QueryPlan（纯规则；不调 AI，离线可复现）。"""
    q = (question or "").strip()
    codes = sorted({m.group(0).strip("[]").replace("本公司·", "") for m in _CODE_RE.finditer(q)})
    doc_nos = sorted({m.group(1) for m in _DOCNO_RE.finditer(q) if not _CODE_RE.fullmatch(m.group(1))})
    amounts = sorted({m.group(1) for m in _AMOUNT_RE.finditer(q)})
    types = sorted({v for k, v in _TYPE_KEYWORDS.items() if k in q})
    intents = [rel for rel, keys in RELATION_KEYWORDS.items() if any(k in q for k in keys)]
    reverse = any(h in q for h in _REVERSE_HINTS)
    # 去掉已识别的编号/单号/金额/类型词后，剩余中文串作为"项目名/关键词"候选
    rest = q
    for token in codes + doc_nos + amounts:
        rest = rest.replace(token, " ")
    for k in _TYPE_KEYWORDS:
        rest = rest.replace(k, " ")
    for keys in RELATION_KEYWORDS.values():
        for k in keys:
            rest = rest.replace(k, " ")
    keywords = [t for t in re.split(r"[\s，,。？?：:、的]+", rest) if len(t) >= 3]
    if relations:
        intents = list(relations)
    if not intents:
        intents = list(DEFAULT_CHAIN_RELATIONS)
    return {
        "question": q, "codes": codes, "doc_nos": doc_nos, "amounts": amounts,
        "types": types, "keywords": sorted(set(keywords)),
        "relations": intents, "reverse": reverse,
    }


# =========================================================
# 2. 锚点定位
# =========================================================
def _num(value: str) -> float | None:
    s = re.sub(r"[^\d.\-]", "", str(value or "").replace(",", ""))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except Exception:
        return None


_READ_CONN = None
_READ_LOCK = threading.Lock()


def _read_conn():
    """复用的**只读**连接（图查询是高频小读）。

    为什么必须复用：`get_connection()` 每次都是新建 TCP+认证，实测 65ms/次；
    一次图游走会发起 900+ 次事实查询（8 个锚点 × BFS 展开）→ 单题 60 秒里有 59 秒
    花在"连接握手"上。复用后同样的题降到 4 秒左右（UI 聊天面板同样受益）。
    只做 SELECT；连接失效（服务重启/超时）时自动重建一次。
    """
    global _READ_CONN
    with _READ_LOCK:
        if _READ_CONN is None or _READ_CONN.closed:
            from database_serv__infra import get_connection

            _READ_CONN = get_connection()
        return _READ_CONN


def _read_query(sql: str, params: list) -> list[tuple]:
    """在复用连接上执行只读查询（失败重连重试一次）。"""
    for attempt in range(2):
        try:
            conn = _read_conn()
            with _READ_LOCK:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
                conn.rollback()          # 只读：结束事务，保持连接干净
            return rows
        except Exception:
            global _READ_CONN
            with _READ_LOCK:
                try:
                    if _READ_CONN is not None:
                        _READ_CONN.close()
                except Exception:
                    pass
                _READ_CONN = None
            if attempt:
                raise
    return []


def _facts_like(*, value_like: str | None = None, doc_keys: list[str] | None = None,
                limit: int = 200) -> list[dict]:
    sql = ("SELECT fact_id, doc_key, fact_kind, page_no, table_index, row_index, col_index, "
           "coord, header, semantic, value, char_start, char_end, raw_char_start, raw_char_end, "
           "raw_exact FROM l1_facts")
    where, params = [], []
    if doc_keys:
        where.append("doc_key = ANY(%s)")
        params.append(doc_keys)
    if value_like is not None:
        where.append("(value ILIKE %s OR value_norm ILIKE %s)")
        params += [f"%{value_like}%", f"%{value_like}%"]
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " LIMIT %s"
    params.append(limit)
    cols = ("fact_id", "doc_key", "fact_kind", "page_no", "table_index", "row_index", "col_index",
            "coord", "header", "semantic", "value", "char_start", "char_end", "raw_char_start",
            "raw_char_end", "raw_exact")
    return [dict(zip(cols, row)) for row in _read_query(sql, params)]


def _row_node_of(fact: dict) -> str | None:
    if fact.get("table_index") and fact.get("row_index"):
        return f"row:{fact['doc_key']}#t{fact['table_index']}r{fact['row_index']}"
    return None


def locate(plan: dict, *, limit_per_anchor: int = 6) -> list[dict]:
    """锚点 → 节点（doc / row）+ 命中事实 + 锚点置信度。"""
    anchors: list[dict] = []
    seen: set[tuple] = set()

    def add(kind: str, value: str, facts: list[dict], node_hint: str | None = None):
        for f in facts[:limit_per_anchor]:
            node = node_hint or f"doc:{f['doc_key']}"
            key = (node, kind, value)
            if key in seen:
                continue
            seen.add(key)
            anchors.append({"anchor_kind": kind, "anchor_value": value, "node": node,
                            "doc_key": f["doc_key"], "fact": f,
                            "anchor_confidence": _ANCHOR_BY_KIND.get(kind, 0.7)})

    for code in plan["codes"]:
        add("code", code, _facts_like(value_like=code, limit=60))
    for no in plan["doc_nos"]:
        add("doc_no", no, _facts_like(value_like=no, limit=60))
        add("doc_no", no, _facts_like(doc_keys=[no], limit=3))       # 单号恰好等于 doc_key 时
    for amount in plan["amounts"]:
        hits = [f for f in _facts_like(value_like=amount.replace(",", ""), limit=120)]
        hits += [f for f in _facts_like(value_like=amount, limit=120)]
        add("amount", amount, hits)
    for kw in plan["keywords"]:
        add("project", kw, _facts_like(value_like=kw, limit=60))
    for tp in plan["types"]:
        from database_serv__infra import get_connection

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT doc_key FROM l1_documents WHERE category = %s ORDER BY doc_key",
                        (tp,))
            keys = [r[0] for r in cur.fetchall()]
        for k in keys:
            add("type", tp, [{"fact_id": None, "doc_key": k, "fact_kind": "doc", "page_no": None,
                              "table_index": None, "row_index": None, "col_index": None,
                              "coord": None, "header": None, "semantic": None, "value": None,
                              "char_start": None, "char_end": None}])
    # 表/文件意图：把命中的表行也作为锚点（row 节点优先）
    if "table_row_matches_doc" in plan["relations"]:
        for a in list(anchors):
            rn = _row_node_of(a["fact"])
            if rn:
                anchors.append({**a, "node": rn, "anchor_kind": "table_row"})
    # ---- 本轮新增：自然语言问题的两类锚点（这是 G4 取数答不出来的根因）----
    # 问题里没有编号/金额时，旧 locate 一无所获 → 证据块为空 → AI 连金额都没见过。
    for a in _text_anchors(plan, limit=limit_per_anchor):
        key = (a["node"], a["anchor_kind"], a["anchor_value"])
        if key not in seen:
            seen.add(key)
            anchors.append(a)
    return anchors


# 字段名（表头）词表：问题里出现这些词 → 到 l1_facts.header 里找（**只认真正的字段名**，
# 不做模糊猜测）。可按需在 .env 里用 FIELD_WORDS 覆盖（逗号分隔）。
_FIELD_WORDS = (
    "申请拨款金额", "合同金额", "合同总额", "含税金额", "不含税金额", "总额", "合计金额",
    "本次支付", "付款金额", "订单金额", "扣款金额", "罚款", "罚单", "税率", "开票日期",
    "申请日期", "付款方式", "开户行", "开户银行", "银行账号", "账号", "纳税人识别号",
    "统一社会信用代码", "采购订单编号", "合同编号", "申请人",
)


def _field_words(question: str) -> list[str]:
    """问题里出现的**表头名 → 实际表头**（含同义词展开）。

    例："合计金额"在表里的真实表头是"总额"；"金额"要同时试总额/合计/申请拨款金额…
    只做"有名有姓"的展开，不做模糊猜测。
    """
    words = list(_FIELD_WORDS)
    extra = os.getenv("FIELD_WORDS", "").strip()
    if extra:
        words += [w.strip() for w in extra.split(",") if w.strip()]
    alias = {
        "合计金额": ("总额", "合计", "合计金额", "金额"),
        "金额": ("总额", "合计", "申请拨款金额", "合同金额", "合同总额", "含税金额",
                 "不含税金额", "付款金额", "订单金额", "扣款金额", "罚款"),
        "本次支付": ("本次支付", "付款金额", "项目付款进度"),
        "扣款": ("扣款金额", "罚款", "罚单", "考核扣款", "考核罚款"),
        "合计扣款": ("扣款金额", "罚款", "罚单"),
    }
    out: list[str] = []
    for w in words:
        if w not in (question or ""):
            continue
        for cand in alias.get(w, (w,)):
            if cand not in out:
                out.append(cand)
    for k, v in alias.items():
        if k in (question or ""):
            for cand in v:
                if cand not in out:
                    out.append(cand)
    return out


def _keyword_docs(plan: dict) -> list[str]:
    """问题关键词命中的文档（用于把"字段词锚点"收窄到具体文档）。

    三级收窄（**先严后宽**，避免一个写偏的词就把范围全丢掉）：
      ① 所有关键词都要在 doc_key 里；
      ② 只用最长的 2 个关键词；
      ③ 命中 ≥2 个关键词的文档（按命中数排序）。
    """
    kws = [k for k in (plan.get("keywords") or [])
           if len(k) >= 2 and not k.endswith(("是多少", "有多少"))]
    if not kws:
        return []
    from database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT doc_key FROM l1_documents ORDER BY doc_key")
        keys = [r[0] for r in cur.fetchall()]
    hits = [k for k in keys if all(w in k for w in kws)]
    if hits:
        return hits
    top2 = sorted(kws, key=len, reverse=True)[:2]
    hits = [k for k in keys if all(w in k for w in top2)]
    if hits:
        return hits
    scored = [(sum(1 for w in kws if w in k), k) for k in keys]
    return [k for n, k in sorted(scored, key=lambda x: -x[0]) if n >= 2]


def _text_anchors(plan: dict, *, limit: int = 6) -> list[dict]:
    """按**字段词**与**文档名子串**补锚点（自然语言取数题的入口）。

    为什么必须补：`parse_query` 只认编号/单号/金额数字/项目名/类型；"…的申请拨款金额
    是多少元"这类问题解析出来 codes/amounts 全空 → 锚点 0 个 → 证据块空 → AI 拿不到金额
    （实测 A 类 G4 只对了 1/6，唯一对的那题还是靠文件名里的金额）。
    两类锚点都**只命中事实层已存在的数据**，不做猜测；命中不了的字段名直接跳过。
    """
    out: list[dict] = []
    q = plan.get("question") or ""
    name_docs = _doc_name_matches(q, limit=3)
    # 关键词收窄失败时用"文档名命中"当收窄范围（例："一串红等草花采购清单"→[374]）
    doc_scope = _keyword_docs(plan) or name_docs

    def push(kind: str, value: str, fact: dict, conf: float) -> None:
        out.append({"anchor_kind": kind, "anchor_value": value,
                    "node": f"doc:{fact['doc_key']}", "doc_key": fact["doc_key"],
                    "fact": fact, "anchor_confidence": conf})

    # ① 字段词锚点：问题里出现字段名 → 按 header 精确匹配（+范围内收窄）
    #    上限 12 条：锚点越多，BFS 起点越多、图游走越慢（实测 18 条锚点会让单题涨到 70s）
    for w in _field_words(q):
        facts = _facts_by_header(w, doc_scope or None, limit=6)
        if not facts and doc_scope:
            facts = _facts_by_header(w, None, limit=3)      # 收窄后没了 → 回退少量全局
        for f in facts:
            push("field", w, f, 0.8)
            if len(out) >= 12:
                break
        if len(out) >= 12:
            break

    # ② 文档名锚点：问题里的中文子串命中 doc_key → 取该文档的**关键事实**（含金额）
    for key in name_docs:
        for f in _key_facts_of_doc(key, limit=max(limit, 10)):
            push("doc_name", key.rsplit("/", 1)[-1], f, 0.75)
    return out


def _facts_by_header(header: str, doc_keys: list[str] | None, *, limit: int = 40) -> list[dict]:
    sql = ("SELECT fact_id, doc_key, fact_kind, page_no, table_index, row_index, col_index, "
           "coord, header, semantic, value, char_start, char_end, raw_char_start, raw_char_end, "
           "raw_exact FROM l1_facts WHERE header = %s")
    params: list = [header]
    if doc_keys:
        sql += " AND doc_key = ANY(%s)"
        params.append(doc_keys)
    sql += " ORDER BY doc_key LIMIT %s"
    params.append(limit)
    cols = ("fact_id", "doc_key", "fact_kind", "page_no", "table_index", "row_index", "col_index",
            "coord", "header", "semantic", "value", "char_start", "char_end", "raw_char_start",
            "raw_char_end", "raw_exact")
    return [dict(zip(cols, row)) for row in _read_query(sql, params)]


def _key_facts_of_doc(doc_key: str, *, limit: int = 6) -> list[dict]:
    """某文档的"关键事实"：优先带数字/金额/编号的那几条（供自然语言取数使用）。"""
    sql = ("SELECT fact_id, doc_key, fact_kind, page_no, table_index, row_index, col_index, "
           "coord, header, semantic, value, char_start, char_end, raw_char_start, raw_char_end, "
           "raw_exact FROM l1_facts WHERE doc_key = %s AND header IS NOT NULL "
           "AND header <> '' AND value <> '' LIMIT 400")
    cols = ("fact_id", "doc_key", "fact_kind", "page_no", "table_index", "row_index", "col_index",
            "coord", "header", "semantic", "value", "char_start", "char_end", "raw_char_start",
            "raw_char_end", "raw_exact")
    rows = [dict(zip(cols, r)) for r in _read_query(sql, [doc_key])]
    def rank(f: dict) -> int:
        v = str(f.get("value") or "")
        h = str(f.get("header") or "")
        # 金额/编号类最优先（取数题真正要找的东西），其次才是别的带数字事实
        if ("元" in v or "¥" in v or "￥" in v) or h in _FIELD_WORDS:
            return 0
        if re.search(r"\d", v):
            return 1
        return 2
    rows.sort(key=rank)
    return rows[:limit]


def _doc_name_matches(question: str, *, limit: int = 3) -> list[str]:
    """问题里的中文子串命中 doc_key（取最长的若干个；命中长度 <4 不算）。"""
    from database_serv__infra import get_connection

    chunks = [c for c in re.findall(r"[\u4e00-\u9fa5]{3,20}", question or "")]
    cands: set[str] = set()
    for n in (10, 8, 6, 5, 4):
        for chunk in chunks:
            for i in range(0, max(len(chunk) - n + 1, 0)):
                cands.add(chunk[i:i + n])
    cands = {c for c in cands if len(c) >= 4 and not c.endswith(("是多少", "有多少", "金额是"))}
    if not cands:
        return []
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT doc_key FROM l1_documents ORDER BY length(doc_key) DESC")
        keys = [r[0] for r in cur.fetchall()]
    scored: list[tuple[int, str]] = []
    for k in keys:
        best = max((len(c) for c in cands if c in k), default=0)
        if best >= 4:
            scored.append((best, k))
    scored.sort(key=lambda x: -x[0])
    return [k for _s, k in scored[:limit]]


# =========================================================
# 3. 图遍历
# =========================================================
def _edges_of(node_ids: set[str], *, include_hypothesis: bool) -> list[dict]:
    """取与给定节点相连的**可走**边：validated 恒可走；假设边仅在 include_hypothesis 时纳入；
    **rejected（含人工驳回）永远不参与遍历**。"""
    import edge_build__graph_edges as eb

    allowed = {"validated", "hypothesis"} if include_hypothesis else {"validated"}
    return [e for e in eb.list_edges()
            if e["status"] in allowed and (e["src"] in node_ids or e["dst"] in node_ids)]


def _node_doc(node_id: str) -> str | None:
    if node_id.startswith("doc:"):
        return node_id[4:]
    if node_id.startswith(("row:", "table:")):
        return node_id.split(":", 1)[1].split("#", 1)[0]
    return None


def _justify(edge: dict, limit: int = 3) -> list[dict]:
    """取该边两侧的**具体值事实**（带坐标），即"这条边凭什么成立"。"""
    values = (edge.get("evidence") or {}).get("values") or {}
    wanted: list[str] = []
    for key in ("amounts", "doc_nos", "projects", "codes", "tax_codes"):
        for v in values.get(key, []) or []:
            wanted.append(str(v))
    wanted = [w for w in wanted if w]
    out: list[dict] = []
    for side in (edge["src"], edge["dst"]):
        if side.startswith(("doc:", "row:", "table:")):
            doc_key = _node_doc(side)
            facts = _facts_like(doc_keys=[doc_key], limit=120)
            if side.startswith("row:"):
                ti, ri = side.split("#t", 1)[1].split("r", 1)
                facts = [f for f in facts if str(f.get("table_index")) == ti
                         and str(f.get("row_index")) == ri]
        else:
            facts = _facts_like(value_like=side.split(":", 1)[1], limit=20)
        for f in facts:
            val = str(f.get("value") or "")
            if any(w == val or w in val for w in wanted):
                out.append({"side": side, "fact": f})
                break
        for f in facts[:2]:
            if all(x["fact"]["fact_id"] != f["fact_id"] for x in out):
                out.append({"side": side, "fact": f})
                break
    _ = limit
    return out


def traverse(anchor: dict, plan: dict, *, include_hypothesis: bool = False,
             max_depth: int = 3, max_chains: int = 6, max_paths: int = 400) -> list[dict]:
    """从锚点节点出发沿边遍历，返回证据链（含每跳与置信度）。

    `max_paths`：BFS 的**工作量上限**。只看前 6 条链（max_chains）但把深度 3 内
    所有路径都展开是浪费——实测一个锚点就能跑出上千条路径（单题 40~70 秒）。
    达到上限即停止扩展（保留已得到的路径，按置信度排序后仍取 top-N）。
    """
    start = anchor["node"]
    start_doc = anchor["doc_key"]
    wanted_doc_types = set(plan.get("types") or [])
    wanted_relations = set(plan.get("relations") or [])
    chains: list[dict] = []
    # BFS：路径 = [(edge, 方向, 节点)]，节点去重防环
    queue: list[tuple[str, list[dict], set[str]]] = [(start, [], {start})]
    # ⚠️ 性能（实测）：`_justify(edge)` 每条边走 2 次 DB 查询，`_edges_of(node)` 每访问
    # 一个节点 1 次查询；BFS 会把**同一条边/同一个节点**重复展开成百上千次 →
    # 单次 query 曾要 88 秒。这里按 edge_id / node 记忆化，并加工作量上限。
    just_cache: dict[str, list[dict]] = {}
    edge_cache: dict[str, list[dict]] = {}
    while queue and len(chains) < max_paths:
        node, path, visited = queue.pop(0)
        if len(path) >= max_depth:
            continue
        edges = edge_cache.get(node)
        if edges is None:
            edges = _edges_of({node}, include_hypothesis=include_hypothesis)
            edge_cache[node] = edges
        for e in edges:
            forward = e["src"] == node
            nxt = e["dst"] if forward else e["src"]
            if nxt in visited:
                continue
            jf = just_cache.get(e["edge_id"])
            if jf is None:
                jf = _justify(e)
                just_cache[e["edge_id"]] = jf
            hop = {"edge": e, "from": node, "to": nxt, "forward": forward,
                   "relation": e["relation"], "status": e["status"],
                   "confidence": e["confidence"] * (HYPOTHESIS_WEIGHT if e["status"] != "validated" else 1.0),
                   "evidence": sorted(((e.get("evidence") or {}).get("kinds") or {}).keys()),
                   "justify": jf}
            new_path = path + [hop]
            chains.append({
                "anchor": {"node": start, "doc_key": start_doc,
                           "kind": anchor["anchor_kind"], "value": anchor["anchor_value"],
                           "confidence": anchor["anchor_confidence"]},
                "hops": new_path,
                "end_node": nxt,
                "end_doc": _node_doc(nxt),
                "depth": len(new_path),
            })
            queue.append((nxt, new_path, visited | {nxt}))

    # 过滤：问题指明了目标类型 → 终点类型要匹配；指明了关系 → 路径里至少命中一个
    cat_cache: dict[str, str] = {}

    def _cat(doc_key: str | None) -> str:
        if not doc_key:
            return ""
        hit = cat_cache.get(doc_key)
        if hit is not None:
            return hit
        from database_serv__infra import get_connection

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT category FROM l1_documents WHERE doc_key = %s", (doc_key,))
            row = cur.fetchone()
        val = (row[0] or "") if row else ""
        cat_cache[doc_key] = val
        return val

    kept: list[dict] = []
    for c in chains:
        rels = {h["relation"] for h in c["hops"]}
        if wanted_doc_types and not (_cat(c["end_doc"]) in wanted_doc_types
                                     or rels & wanted_relations):
            continue
        if not wanted_doc_types and not (rels & wanted_relations):
            continue
        conf = c["anchor"]["confidence"]
        for h in c["hops"]:
            conf *= h["confidence"]
        c["confidence"] = round(conf, 4)
        kept.append(c)
    kept.sort(key=lambda c: (-c["confidence"], c["end_node"]))
    return kept[:max_chains]


# =========================================================
# 4. 结果组装
# =========================================================
def query(question: str, *, include_hypothesis: bool = False, max_depth: int = 3,
          limit: int = 5, relations: list[str] | None = None) -> dict:
    """L3 主入口：问题 → {chains, values, confidence, unresolved, notes}（值 + 置信度 + 溯源）。"""
    plan = parse_query(question, relations=relations)
    anchors = locate(plan)
    chains: list[dict] = []
    for a in anchors:
        chains.extend(traverse(a, plan, include_hypothesis=include_hypothesis,
                               max_depth=max_depth))
    # 去重：同一组边（正向/反向遍历会各发现一次）只保留一条 —— 
    # 用"边集合 + 两端节点集合"作为键，优先保留起点即锚点（正向）的那条，避免镜像重复。
    uniq: dict[tuple, dict] = {}
    for c in chains:
        key = (frozenset(h["edge"]["edge_id"] for h in c["hops"]),
               frozenset({c["anchor"]["node"], c["end_node"]}))
        cur = uniq.get(key)
        if cur is None:
            uniq[key] = c
            continue
        better = (c["confidence"] > cur["confidence"]
                  or (abs(c["confidence"] - cur["confidence"]) < 1e-9
                      and c["hops"][0]["forward"] and not cur["hops"][0]["forward"]))
        if better:
            uniq[key] = c
    chains = sorted(uniq.values(), key=lambda c: (-c["confidence"], c["end_node"]))[:limit]

    covered = {h["relation"] for c in chains for h in c["hops"]}
    unresolved = [r for r in plan["relations"] if r not in covered]
    hypotheses = sorted({h["edge"]["edge_id"] for c in chains for h in c["hops"]
                         if h["status"] != "validated"})
    values = []
    for c in chains:
        for h in c["hops"]:
            for j in h["justify"]:
                f = j["fact"]
                values.append({
                    "chain_confidence": c["confidence"], "side": j["side"],
                    "doc_key": f.get("doc_key"), "header": f.get("header"),
                    "value": f.get("value"), "fact_id": f.get("fact_id"),
                    "page_no": f.get("page_no"), "table_index": f.get("table_index"),
                    "row_index": f.get("row_index"), "col_index": f.get("col_index"),
                    "coord": f.get("coord"), "char_start": f.get("char_start"),
                    "char_end": f.get("char_end"), "raw_char_start": f.get("raw_char_start"),
                    "raw_char_end": f.get("raw_char_end"), "raw_exact": f.get("raw_exact"),
                })
    best = chains[0]["confidence"] if chains else 0.0
    if not plan["codes"] and not plan["doc_nos"] and not plan["keywords"] and not plan["amounts"] \
            and not plan["types"]:
        notes = ["问题里没有可识别的锚点（编号/单号/金额/项目名/文档类型）——无法定位，未做任何推测。"]
    elif not chains:
        notes = ["没有找到可用的证据链（可能：边还没构建、关系未命中、或证据不足）。"
                 "未编造结论；可换用编号/单号精确提问，或先跑边构建。"]
    else:
        notes = []
    if hypotheses:
        notes.append(f"路径中包含 {len(hypotheses)} 条**假设边**（尚未人工确认），已降权；"
                     "如需确认请用 confirm_edge。")
    result = {
        "question": question, "plan": plan,
        # ⚠️ 锚点必须带上**命中的那条事实**：以前只留 node/kind/value，
        #    下游（给 AI 的证据块）就拿不到"这个编号/金额出现在哪份文档的哪一格"，
        #    实测直接把 AI 的召回压垮（问 BK0001 时它一份单据都列不出来）。
        "anchors": [{"node": a["node"], "kind": a["anchor_kind"], "value": a["anchor_value"],
                     "doc_key": a.get("doc_key"), "fact": a.get("fact"),
                     "confidence": a["anchor_confidence"]} for a in anchors],
        "chains": chains, "values": values, "confidence": round(best, 4),
        "hypothesis_edges": hypotheses, "unresolved": unresolved, "notes": notes,
    }
    _log({"action": "query", "question": question, "anchors": len(anchors),
          "chains": len(chains), "confidence": result["confidence"],
          "unresolved": unresolved, "include_hypothesis": include_hypothesis})
    return result


def chains_for_doc(doc_key: str, *, include_hypothesis: bool = False,
                   limit: int = 10) -> list[dict]:
    """某份文档的全部关系（L3/UI 用）：正向+反向。"""
    import edge_build__graph_edges as eb

    node = f"doc:{doc_key}"
    allowed = {"validated", "hypothesis"} if include_hypothesis else {"validated"}
    out = []
    for e in eb.list_edges():
        if e["status"] not in allowed:
            continue
        if node not in (e["src"], e["dst"]):
            continue
        other = e["dst"] if e["src"] == node else e["src"]
        out.append({"edge_id": e["edge_id"], "relation": e["relation"], "status": e["status"],
                    "confidence": e["confidence"], "other": other,
                    "other_doc": _node_doc(other),
                    "evidence": sorted(((e.get("evidence") or {}).get("kinds") or {}).keys()),
                    "justify": _justify(e)})
    out.sort(key=lambda x: (-x["confidence"], x["other"]))
    return out[:limit]


def _doc_no_of(doc_key: str | None) -> str:
    """doc_key → 文档编号（证据块里显示成 `[368]`，AI 答题口径统一用编号）。

    带进程内缓存；查不到就返回 `?`（不影响其它内容）。
    """
    if not doc_key:
        return "?"
    cache = _DOC_NO_CACHE
    if doc_key in cache:
        return cache[doc_key]
    val = "?"
    try:
        from database_serv__infra import get_connection

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT doc_no FROM l1_documents WHERE doc_key = %s LIMIT 1", (doc_key,))
            row = cur.fetchone()
        if row and row[0] is not None:
            val = str(row[0])
    except Exception:
        val = "?"
    cache[doc_key] = val
    return val


_DOC_NO_CACHE: dict[str, str] = {}


def evidence_block(result: dict, *, max_chains: int = 5,
                   allow_span_fetch: bool | None = None) -> str:
    """把 L3 结果转成给对话模型看的**参考材料**（含溯源，明确"取数须回原文核对"）。

    **有界原文取用**（需求）：当某条边两侧**没有可直接引用的数值事实**（justify 为空，
    常见于"发票金额/开票日期只在表格或文件名里、没被抽成 fact"）但**置信度较高**时，
    允许从 hub 页文本里取**一小段**原文（脱敏后）作为补充依据——但：
      · 只对高置信（≥ SPAN_MIN_CONF）且确实缺值的位置取；
      · 每问最多 SPAN_MAX_BLOCKS 段、每段最多 SPAN_WINDOW 字、总计最多 SPAN_MAX_CHARS 字；
      · **绝不遍历全部原文**（不做整页/整库拼接）。
    参数 allow_span_fetch=None 时读环境变量 SPAN_FETCH（默认 1=启用）。
    """
    if not result.get("chains") and not result.get("anchors"):
        return "（图遍历未取到证据链：" + ("；".join(result.get("notes") or []) or "无") + "）"
    if allow_span_fetch is None:
        allow_span_fetch = os.getenv("SPAN_FETCH", "1").strip() not in ("0", "false", "no")
    budget = SpanBudget()
    lines = ["【图遍历取证】（沿已验证边；每条都带溯源坐标，概括仅作定位索引）"]
    # ---- 锚点事实：编号/金额**直接命中的原始事实**（"出现在哪几份文档的哪一格"）----
    # 这是**事实层（l1_facts）**的记录，不是摘要、也不是哈希：带页码/表行列/坐标/字符区间，
    # 可直接回原文核对。没有它，AI 只能看到"关系链上的节点"，会把"没有被连边但确实含该编号"
    # 的文档整片漏掉（实测 BK0001 召回 0）。
    anchor_lines: list[str] = []
    seen_anchor: set[tuple] = set()
    for a in result.get("anchors") or []:
        f = a.get("fact") or {}
        if not f:
            continue
        key = (f.get("doc_key"), f.get("header"), f.get("value"), f.get("row_index"),
               f.get("col_index"), f.get("char_start"))
        if key in seen_anchor:
            continue
        seen_anchor.add(key)
        loc = []
        if f.get("page_no"):
            loc.append(f"p{f['page_no']}")
        if f.get("table_index") is not None and f.get("row_index") is not None:
            loc.append(f"表{f['table_index']}行{f['row_index']}列{f.get('col_index')}")
        if f.get("coord"):
            loc.append(str(f["coord"]))
        if f.get("char_start") is not None:
            loc.append(f"chars[{f['char_start']},{f['char_end']}]")
        anchor_lines.append(
            f"  · [{_doc_no_of(f.get('doc_key'))}] {f.get('header') or '（无表头）'}"
            f" = {f.get('value')}（{', '.join(loc) or '无坐标'}）")
    if anchor_lines:
        lines.append("【锚点事实】问题里的编号/金额在事实层直接命中的位置"
                     "（下列每一份文档都**确实出现**该编号，必须全部计入“相关单据”）：")
        lines.extend(anchor_lines[:60])
        if len(anchor_lines) > 60:
            lines.append(f"  ……（另有 {len(anchor_lines) - 60} 条同类命中，已截断）")
    for i, c in enumerate(result["chains"][:max_chains], start=1):
        path = f"{c['anchor']['node']}"
        for h in c["hops"]:
            arrow = "-->" if h["forward"] else "<--"
            path += f" {arrow}{h['relation']}({h['confidence']:.2f}){arrow} {h['to']}"
        lines.append(f"{i}) 置信度 {c['confidence']:.2f}｜路径：{path}")
        for h in c["hops"]:
            if not h["justify"]:
                # 该跳没有可直接引用的值 → 有界取原文片段兜底
                span = _hop_span(h["edge"], c, h, budget) if allow_span_fetch else None
                if span:
                    lines.append(f"     （无直接取值，附原文片段）{span}")
            for j in h["justify"]:
                f = j["fact"]
                loc = []
                if f.get("page_no"):
                    loc.append(f"p{f['page_no']}")
                if f.get("coord"):
                    loc.append(str(f["coord"]))
                if f.get("char_start") is not None:
                    loc.append(f"chars[{f['char_start']},{f['char_end']}]")
                lines.append(f"     {j['side']}｜{f.get('header') or ''}={f.get('value')}"
                             f"（{', '.join(loc) or '无坐标'}）")
            lines.append(f"     依据：{', '.join(h['evidence']) or '（无）'}")
    if result.get("unresolved"):
        lines.append(f"未走通的意图：{', '.join(result['unresolved'])}")
    if budget.used:
        lines.append(f"（原文片段预算：取了 {budget.used} 段 / {budget.chars} 字，"
                     f"上限 {budget.max_blocks} 段 / {budget.max_chars} 字——"
                     f"只为补「没有直接取值」的位置，未遍历全文）")
    lines.append("注意：以上均为**脱敏后**的值；需要明文或要核对真值，请按上面的文档/页码/坐标回原文调取，不要凭编号臆测。")
    return "\n".join(lines)


# =========================================================
# 有界原文取用（高置信但无直接取值时，取一小段脱敏原文）
# =========================================================
class SpanBudget:
    """一次问答的原文片段预算（段数 + 总字数），防止"顺手把全文塞进 prompt"。"""

    def __init__(self) -> None:
        self.max_blocks = int(os.getenv("SPAN_MAX_BLOCKS", "3") or 3)
        self.max_chars = int(os.getenv("SPAN_MAX_CHARS", "1200") or 1200)
        self.window = int(os.getenv("SPAN_WINDOW", "220") or 220)
        self.min_conf = float(os.getenv("SPAN_MIN_CONF", "0.6") or 0.6)
        self.used = 0
        self.chars = 0

    def take(self, text: str) -> str | None:
        """申请一段预算；超预算返回 None（调用方跳过）。"""
        if self.used >= self.max_blocks or not text:
            return None
        room = self.max_chars - self.chars
        if room <= 40:
            return None
        piece = text[: min(self.window, room)]
        self.used += 1
        self.chars += len(piece)
        return piece


def fetch_span(doc_key: str, *, page_no: int | None = None,
               char_start: int | None = None, char_end: int | None = None,
               window: int = 220, budget: SpanBudget | None = None) -> dict | None:
    """从 hub 页文本取**一小段**原文（脱敏后），带页码与字符区间。

    · 只读 hub（AI 输入边界：ai_guard 只允许 hub/，本函数同样只在 hub 里取）；
    · 优先按给定的字符区间（事实坐标）取 ±window/2；没有坐标就取该页开头 window 字；
    · 一律经预算裁剪（budget.take），保证"每问只取几段、不全量遍历"。
    """
    import json as _json

    path = HUB_DIR / f"{doc_key}.json"
    if not path.exists():
        return None
    try:
        doc = _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    pages = doc.get("pages") or []
    if not pages:
        return None
    idx = max(1, min(int(page_no or 1), len(pages))) - 1
    text = str(pages[idx] or "")
    if char_start is not None and char_end is not None:
        lo = max(0, int(char_start) - window // 2)
        hi = min(len(text), int(char_end) + window // 2)
    else:
        lo, hi = 0, min(len(text), window)
    piece = text[lo:hi]
    if budget is not None:
        piece = budget.take(piece)
        if piece is None:
            return None
    return {"doc_key": doc_key, "page_no": idx + 1, "char_start": lo,
            "char_end": lo + len(piece), "text": piece}


def _hop_span(edge: dict, chain: dict, hop: dict, budget: SpanBudget) -> str | None:
    """为"缺值"的一跳取原文片段：优先边证据里记录的页码/字符区间，其次该文档页首。"""
    if hop["confidence"] < budget.min_conf:
        return None
    side = hop["to"] if hop["to"].startswith(("doc:", "row:", "table:")) else hop["from"]
    doc_key = _node_doc(side if side.startswith("doc:") else f"doc:{_node_doc(side) or ''}")
    if not doc_key:
        doc_key = chain.get("end_doc") or chain["anchor"].get("doc_key")
    if not doc_key:
        return None
    vals = (edge.get("evidence") or {}).get("values") or {}
    page_no = char_start = char_end = None
    for f in (edge.get("evidence") or {}).get("facts") or []:
        page_no = f.get("page_no") or page_no
        char_start = f.get("char_start", char_start)
        char_end = f.get("char_end", char_end)
    _ = vals
    got = fetch_span(doc_key, page_no=page_no, char_start=char_start, char_end=char_end,
                     window=budget.window, budget=budget)
    if not got:
        return None
    return (f"[{_Path(doc_key).name} p{got['page_no']} chars[{got['char_start']},"
            f"{got['char_end']}]] {got['text'].replace(chr(10), ' / ')}")


# =========================================================
# 假设边人工确认（假设 → 证实/驳回）
# =========================================================
def list_hypotheses() -> list[dict]:
    import edge_build__graph_edges as eb

    return [e for e in eb.list_edges(status="hypothesis")]


def confirm_edge(edge_id: str, *, user: dict | None = None, note: str | None = None) -> dict:
    """人工确认假设边 → validated（并解除冲突标记）。"""
    from database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE graph_edges
                       SET status='validated', conflict=FALSE, method='manual',
                           reason=COALESCE(%s, reason), updated_at=CURRENT_TIMESTAMP
                       WHERE edge_id=%s""", (note, edge_id))
        n = cur.rowcount
        conn.commit()
    if n:
        _log({"action": "confirm_edge", "edge_id": edge_id, "by": (user or {}).get("username")})
    return {"ok": bool(n), "msg": "已确认" if n else f"未找到边 {edge_id}"}


def reject_edge(edge_id: str, *, user: dict | None = None, note: str | None = None) -> dict:
    """人工驳回假设边 → rejected（保留记录便于审计）。"""
    from database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE graph_edges
                       SET status='rejected', method='manual', reason=COALESCE(%s, reason),
                           updated_at=CURRENT_TIMESTAMP
                       WHERE edge_id=%s""", (note, edge_id))
        n = cur.rowcount
        conn.commit()
    if n:
        _log({"action": "reject_edge", "edge_id": edge_id, "by": (user or {}).get("username")})
    return {"ok": bool(n), "msg": "已驳回" if n else f"未找到边 {edge_id}"}


def _log(record: dict) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **record}
        with (LOG_DIR / f"graph_walk_{time.strftime('%Y%m%d')}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
