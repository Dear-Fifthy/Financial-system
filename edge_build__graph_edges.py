"""L1 边构建（图遍历层的前置）：以事实清单为节点源，发现**跨文件/表**关系边。

流程（严格三段，不采信模型自述）：
  1. **节点**：`l1_facts` + `l1_documents` → `graph_nodes`
       doc:<doc_key>            文档节点（带分类）
       table:<doc_key>#t{n}     表块节点
       row:<doc_key>#t{n}r{m}   表行节点（"表/文件"关系的表侧）
       entity:<CODE>            实体节点（CO/PT/PJ/TX/BK/BA…编号，跨文件同一实体同一个节点）
  2. **候选对**（确定性，不花 token）：跨文档两两配对（doc↔doc），以及"表行↔其它文档"
       （row↔doc）；只保留**至少一个可核验信号**相同/相近的对（编号相同、金额相等、
       项目名相同、单据号互现），并给出候选关系提示 + 证据包。
  3. **判定 → 校验**：
       · AI（本轮**接对话**：agent 通道，见下）只负责给"关系名 + 自评置信度 + 依据类型"；
       · **校验与置信度一律本地重算**（不采信模型自述）：按关系规则要求必需证据成立，
         逐项加权得到 confidence；必需证据不齐 → `hypothesis`（假设边，不供 L3 直接取用），
         完全无证据 → `rejected`；同一对节点出现不同关系 → 双方都标 `conflict` 并降级为假设。

AI 通道（`EDGE_LLM_MODE`）：
  · `api`   —— 配了 `EDGE_AI_*` key 时走 Qwen3.8-Flash（`ai_client__ai.complete("edge", …)`）；
  · `agent` —— **本轮用**：不做网络调用，把提示词写成请求文件
                `logs/graph/edge_requests_<batch>.jsonl`，等"对话侧"回填
                `logs/graph/agent_replies_<batch>.json` 后由 `ingest_replies()` 收敛；
  · `auto`（默认）—— 有 key 走 api，没有就走 agent（即"暂时接对话"）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

__all__ = [
    "GRAPH_DIR",
    "RELATIONS",
    "EVIDENCE_KINDS",
    "ensure_tables",
    "build_nodes",
    "list_nodes",
    "collect_candidates",
    "prepare",
    "ingest_replies",
    "build_edges",
    "entry_points",
    # 动态（增量）建图
    "build_for_doc",
    "collect_candidates_for_doc",
    "pending_docs",
    "build_pending",
    "graph_state",
    "enqueue_for_build",
    "build_queue_status",
    "purge_pair_cache_for_doc",
    "request_stop_build",
    "build_stop_requested",
    "clear_build_stop",
    "list_edges",
    "count_edges",
    "edge_summary",
    "llm_mode",
]

BASE_DIR = Path(__file__).resolve().parent
GRAPH_DIR = BASE_DIR / "logs" / "graph"
EXTRACT_VERSION = "v1"

# 允许的关系（封闭集合：模型只能从中选；不在集合里的判定按 rejected 处理）
RELATIONS: dict[str, dict] = {
    # doc↔doc
    "contract_to_invoice": {
        "label": "合同→发票（开票关系）",
        "required_any": [("project_value_shared", "counterparty_code_shared"),
                         ("amount_equal", "tax_code_shared", "doc_no_shared")],
        "boost": {"amount_equal": 0.20, "project_value_shared": 0.15,
                  "counterparty_code_shared": 0.10, "tax_code_shared": 0.10,
                  "doc_no_shared": 0.15},
    },
    "payment_for_contract": {
        "label": "付款→合同（资金流）",
        "required_any": [("amount_equal",), ("project_value_shared", "counterparty_code_shared",
                                             "doc_no_shared")],
        "boost": {"amount_equal": 0.25, "doc_no_shared": 0.15, "project_value_shared": 0.10,
                  "counterparty_code_shared": 0.10},
    },
    "contract_to_logistics": {
        "label": "合同→物流（货物流）",
        "required_any": [("doc_no_shared", "project_value_shared")],
        "boost": {"doc_no_shared": 0.25, "project_value_shared": 0.15,
                  "counterparty_code_shared": 0.10},
    },
    "same_project": {
        "label": "同一项目",
        "required_any": [("project_value_shared",)],
        "boost": {"project_value_shared": 0.30, "counterparty_code_shared": 0.10},
    },
    "counterparty_shared": {
        "label": "同一对手方",
        "required_any": [("counterparty_code_shared",)],
        "boost": {"counterparty_code_shared": 0.25, "amount_equal": 0.10},
    },
    "amount_consistent": {
        "label": "金额一致",
        "required_any": [("amount_equal",)],
        "boost": {"amount_equal": 0.30, "project_value_shared": 0.10,
                  "counterparty_code_shared": 0.10},
    },
    # row↔doc（表/文件）
    "table_row_matches_doc": {
        "label": "表行↔文件（表里这一行对应这份文件）",
        "required_any": [("amount_equal", "tax_code_shared", "doc_no_shared",
                          "counterparty_code_shared")],
        "boost": {"amount_equal": 0.20, "tax_code_shared": 0.20, "doc_no_shared": 0.20,
                  "counterparty_code_shared": 0.10, "project_value_shared": 0.10},
    },
    "unrelated": {"label": "无关系（不建边）", "required_any": [], "boost": {}},
}

EVIDENCE_KINDS = (
    "project_value_shared", "counterparty_code_shared", "tax_code_shared",
    "amount_equal", "doc_no_shared", "same_doc_type_pair",
)

_CODE_TOKENS = {
    "counterparty_code_shared": re.compile(r"\[本公司·CO\d{4}\]|(?<![A-Za-z])CO\d{4}(?![0-9])"),
    "tax_code_shared": re.compile(r"\[TX\d{4}\]"),
}
_PROJECT_HEADERS = ("项目名称", "项目", "工程名称")
_NUMBER_HEADERS = ("合同编号", "发票号码", "发票号", "付款单号", "运单号", "单号", "编号")
_AMOUNT_HEADERS = ("金额", "价税合计", "合计", "合同金额", "付款金额", "开票金额", "总额")
_TYPE_PAIRS = {("合同", "发票"), ("合同", "物流凭证"), ("合同", "付款凭证")}


# =========================================================
# 基础
# =========================================================
def llm_mode() -> str:
    """当前边判定通道：api（有 EDGE key）/ agent（本轮"接对话"）。"""
    mode = os.getenv("EDGE_LLM_MODE", "auto").strip().lower()
    if mode in ("api", "agent"):
        return mode
    try:
        from ai_client__ai import load_chain_config

        key = (load_chain_config("edge") or {}).get("api_key", "")
    except Exception:
        key = ""
    return "api" if key else "agent"


def _num(value: str) -> float | None:
    """从"1,234,567.89 元"这类文本里取数值（取不到返回 None）。"""
    s = re.sub(r"[^\d.\-]", "", str(value or "").replace(",", ""))
    if not s or s in ("-", ".", "-."):
        return None
    try:
        return float(s)
    except Exception:
        return None


def _edge_id(src: str, dst: str, relation: str) -> str:
    return hashlib.sha1(f"{src}|{dst}|{relation}".encode("utf-8")).hexdigest()[:20]


def _log(record: dict) -> None:
    try:
        GRAPH_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **record}
        with (GRAPH_DIR / f"graph_edges_{time.strftime('%Y%m%d')}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# =========================================================
# 表
# =========================================================
def ensure_tables() -> None:
    from psycopg2 import sql as _sql

    from database_serv__infra import APP_DB_CONFIG, get_admin_connection

    ddl_nodes = """
    CREATE TABLE IF NOT EXISTS graph_nodes (
        node_id VARCHAR(400) PRIMARY KEY,
        node_type VARCHAR(16) NOT NULL,          -- doc | table | row | entity
        doc_key VARCHAR(300),
        label VARCHAR(200),
        value TEXT,
        semantic VARCHAR(32),
        category VARCHAR(32),
        page_no INT,
        table_index INT,
        row_index INT,
        coord VARCHAR(16),
        fact_id VARCHAR(400),
        path_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )"""
    ddl_edges = """
    CREATE TABLE IF NOT EXISTS graph_edges (
        edge_id VARCHAR(40) PRIMARY KEY,
        src_node VARCHAR(400) NOT NULL,
        dst_node VARCHAR(400) NOT NULL,
        relation VARCHAR(48) NOT NULL,
        status VARCHAR(16) NOT NULL,             -- validated | hypothesis | rejected
        confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
        confidence_self DOUBLE PRECISION,        -- 模型自评（仅留痕，不采信）
        evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
        missing JSONB NOT NULL DEFAULT '[]'::jsonb,
        conflict BOOLEAN NOT NULL DEFAULT FALSE,
        method VARCHAR(16) NOT NULL,             -- agent | api | rule
        model VARCHAR(64),
        reason TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (src_node, dst_node, relation)
    )"""
    ddl_state = """
    CREATE TABLE IF NOT EXISTS graph_build_state (
        doc_key VARCHAR(300) PRIMARY KEY,
        built_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        facts_count INT DEFAULT 0,
        hub_hash VARCHAR(64),
        nodes INT DEFAULT 0,
        candidates INT DEFAULT 0,
        stored INT DEFAULT 0,
        status VARCHAR(24) DEFAULT 'built',
        note TEXT
    )"""
    ddl_cache = """
    CREATE TABLE IF NOT EXISTS graph_pair_cache (
        pair_key VARCHAR(600) PRIMARY KEY,
        signature VARCHAR(64) NOT NULL,
        relation VARCHAR(48),
        confidence_self DOUBLE PRECISION,
        evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
        reason TEXT,
        model VARCHAR(64),
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )"""
    with get_admin_connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(ddl_nodes)
            cur.execute(ddl_edges)
            cur.execute(ddl_state)
            cur.execute(ddl_cache)
            for col, typ in (("confidence_self", "DOUBLE PRECISION"),
                             ("missing", "JSONB NOT NULL DEFAULT '[]'::jsonb"),
                             ("conflict", "BOOLEAN NOT NULL DEFAULT FALSE")):
                cur.execute(f"ALTER TABLE graph_edges ADD COLUMN IF NOT EXISTS {col} {typ}")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_edges_src ON graph_edges (src_node)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_edges_dst ON graph_edges (dst_node)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_edges_status ON graph_edges (status)")
            for tbl in ("graph_nodes", "graph_edges", "graph_build_state", "graph_pair_cache"):
                cur.execute(
                    _sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                        _sql.Identifier(tbl), _sql.Identifier(APP_DB_CONFIG["user"])
                    )
                )


# =========================================================
# 节点
# =========================================================
def _facts_of(doc_key: str) -> list[dict]:
    import l1_facts__fact_list

    return l1_facts__fact_list.list_facts(doc_key)


def _doc_category(doc_key: str) -> str:
    try:
        from database_serv__infra import get_connection

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT category FROM l1_documents WHERE doc_key = %s", (doc_key,))
            row = cur.fetchone()
            return (row[0] or "") if row else ""
    except Exception:
        return ""


def build_nodes(doc_keys: list[str], *, replace: bool = True,
                prune_orphan_entities: bool = True) -> dict:
    """按事实清单建节点（幂等：UPSERT）。

    replace 语义（动态建图的关键参数）：
      · True （默认，= 旧的"全量重建"入口 prepare/build_edges 用）：
        全量 UPSERT + **删除不在本批里的节点**——注意：只喂部分 doc_key 会把其它
        文档的节点删掉，所以只适合"重建全库"。
      · False（增量，= 动态建图用）：只 UPSERT 本批文档的节点；**只清理本批文档
        自己的过期节点**（例如重扫后行号变了），其它文档与实体节点一律不动。
    prune_orphan_entities：增量模式下顺带清掉"已无任何事实引用"的实体节点
      （等价于删除文档时的清理由，避免编号改名后留下孤儿实体）。
    """
    import l1_facts__fact_list

    ensure_tables()
    l1_facts__fact_list.ensure_table()
    from database_serv__infra import get_admin_connection

    nodes: dict[str, dict] = {}
    entity_ids: set[str] = set()
    for doc_key in doc_keys:
        cat = _doc_category(doc_key)
        nodes[f"doc:{doc_key}"] = {
            "node_type": "doc", "doc_key": doc_key, "label": doc_key,
            "value": doc_key, "category": cat, "path_json": {"doc_key": doc_key},
        }
        table_seen: set[int] = set()
        for f in _facts_of(doc_key):
            if f["fact_kind"] == "cell" and f.get("table_index") is not None:
                ti = int(f["table_index"])
                if ti not in table_seen:
                    table_seen.add(ti)
                    nid = f"table:{doc_key}#t{ti}"
                    nodes[nid] = {
                        "node_type": "table", "doc_key": doc_key, "label": f"表块 t{ti}",
                        "value": "", "category": cat, "page_no": f.get("page_no"),
                        "table_index": ti,
                        "path_json": {"doc_key": doc_key, "table_index": ti},
                    }
                if f.get("row_index"):
                    rid = f"row:{doc_key}#t{ti}r{f['row_index']}"
                    nodes.setdefault(rid, {
                        "node_type": "row", "doc_key": doc_key,
                        "label": f"第{f['row_index']}行", "value": "",
                        "category": cat, "page_no": f.get("page_no"), "table_index": ti,
                        "row_index": f["row_index"],
                        "path_json": {"doc_key": doc_key, "table_index": ti,
                                      "row_index": f["row_index"]},
                    })
        # 实体节点（跨文件同一编号 → 同一节点，供 L3 穿行）
        for f in _facts_of(doc_key):
            for kind, rx in _CODE_TOKENS.items():
                for m in rx.finditer(str(f.get("value") or "")):
                    token = m.group(0).strip("[]")
                    if token.startswith("本公司·"):
                        token = token.split("·", 1)[1]
                    entity_ids.add(f"entity:{token}")
                    nodes.setdefault(f"entity:{token}", {
                        "node_type": "entity", "doc_key": None, "label": token,
                        "value": token, "fact_id": f["fact_id"],
                        "path_json": {"doc_key": doc_key, "fact_id": f["fact_id"]},
                    })

    with get_admin_connection() as conn, conn.cursor() as cur:
        ids = list(nodes)
        if replace:
            if ids:
                cur.execute("DELETE FROM graph_nodes WHERE node_id <> ALL(%s)", (ids,))
            else:
                cur.execute("DELETE FROM graph_nodes")
        else:
            # 增量：只清"本批文档自己的"过期节点（其它文档 / 实体节点不动）
            doc_node_ids = [nid for nid, n in nodes.items() if n.get("doc_key")]
            if doc_node_ids:
                cur.execute(
                    """DELETE FROM graph_nodes
                        WHERE doc_key = ANY(%s) AND node_id <> ALL(%s)""",
                    (doc_keys, doc_node_ids))
            if prune_orphan_entities:
                cur.execute(
                    """DELETE FROM graph_nodes
                        WHERE node_type = 'entity'
                          AND NOT EXISTS (SELECT 1 FROM l1_facts f
                                          WHERE f.value LIKE '%%' || graph_nodes.label || '%%')""")
        for nid, n in nodes.items():
            cur.execute(
                """INSERT INTO graph_nodes
                   (node_id, node_type, doc_key, label, value, semantic, category, page_no,
                    table_index, row_index, coord, fact_id, path_json)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (node_id) DO UPDATE
                   SET node_type = EXCLUDED.node_type, doc_key = EXCLUDED.doc_key,
                       label = EXCLUDED.label, value = EXCLUDED.value,
                       semantic = EXCLUDED.semantic, category = EXCLUDED.category,
                       page_no = EXCLUDED.page_no, table_index = EXCLUDED.table_index,
                       row_index = EXCLUDED.row_index, coord = EXCLUDED.coord,
                       fact_id = EXCLUDED.fact_id, path_json = EXCLUDED.path_json""",
                (nid, n["node_type"], n.get("doc_key"), (n.get("label") or "")[:200],
                 n.get("value"), n.get("semantic"), n.get("category"), n.get("page_no"),
                 n.get("table_index"), n.get("row_index"), n.get("coord"),
                 n.get("fact_id"), json.dumps(n.get("path_json") or {}, ensure_ascii=False)),
            )
        conn.commit()
    return {"nodes": len(nodes), "by_type": _count_by(nodes, "node_type")}


def _count_by(nodes: dict, key: str) -> dict:
    out: dict[str, int] = {}
    for n in nodes.values():
        out[str(n.get(key))] = out.get(str(n.get(key)), 0) + 1
    return out


def list_nodes(node_type: str | None = None) -> list[dict]:
    from database_serv__infra import get_connection

    sql = ("SELECT node_id, node_type, doc_key, label, value, semantic, category, page_no, "
           "table_index, row_index, coord, fact_id, path_json FROM graph_nodes")
    params: tuple = ()
    if node_type:
        sql += " WHERE node_type = %s"
        params = (node_type,)
    sql += " ORDER BY node_type, node_id"
    out = []
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        for r in cur.fetchall():
            out.append({"node_id": r[0], "node_type": r[1], "doc_key": r[2], "label": r[3],
                        "value": r[4], "semantic": r[5], "category": r[6], "page_no": r[7],
                        "table_index": r[8], "row_index": r[9], "coord": r[10], "fact_id": r[11],
                        "path_json": r[12]})
    return out


# =========================================================
# 候选对（确定性；不花 token）
# =========================================================
def _signals(doc_key: str) -> dict:
    """一份文档的可核验信号：编号 / 金额 / 项目名 / 单据号 / 分类。"""
    facts = _facts_of(doc_key)
    codes, tax_codes, amounts, projects, doc_nos = set(), set(), set(), set(), set()
    for f in facts:
        val = str(f.get("value") or "")
        for m in _CODE_TOKENS["counterparty_code_shared"].finditer(val):
            token = m.group(0).strip("[]")
            codes.add(token.split("·", 1)[1] if token.startswith("本公司·") else token)
        for m in _CODE_TOKENS["tax_code_shared"].finditer(val):
            tax_codes.add(m.group(0).strip("[]"))
        header = str(f.get("header") or "")
        if any(k in header for k in _AMOUNT_HEADERS) or f.get("semantic") in ("amount",):
            n = _num(val)
            if n is not None and abs(n) > 0:
                amounts.add(round(n, 2))
        if any(k in header for k in _PROJECT_HEADERS) and len(val.strip()) >= 3:
            projects.add(val.strip())
        if any(k in header for k in _NUMBER_HEADERS) and len(val.strip()) >= 3:
            doc_nos.add(val.strip())
    return {"doc_key": doc_key, "category": _doc_category(doc_key), "codes": codes,
            "tax_codes": tax_codes, "amounts": amounts, "projects": projects, "doc_nos": doc_nos,
            "facts": facts}


def _row_signals(doc_key: str, table_index: int, row_index: int, facts: list[dict]) -> dict:
    rows = [f for f in facts if f.get("table_index") == table_index
            and f.get("row_index") == row_index]
    amounts, tax_codes, doc_nos, projects, codes = set(), set(), set(), set(), set()
    for f in rows:
        val = str(f.get("value") or "")
        header = str(f.get("header") or "")
        for m in _CODE_TOKENS["counterparty_code_shared"].finditer(val):
            token = m.group(0).strip("[]")
            codes.add(token.split("·", 1)[1] if token.startswith("本公司·") else token)
        for m in _CODE_TOKENS["tax_code_shared"].finditer(val):
            tax_codes.add(m.group(0).strip("[]"))
        if any(k in header for k in _AMOUNT_HEADERS):
            n = _num(val)
            if n is not None:
                amounts.add(round(n, 2))
        if any(k in header for k in _PROJECT_HEADERS) and len(val.strip()) >= 3:
            projects.add(val.strip())
        if any(k in header for k in _NUMBER_HEADERS) and len(val.strip()) >= 3:
            doc_nos.add(val.strip())
    return {"doc_key": doc_key, "amounts": amounts, "tax_codes": tax_codes, "doc_nos": doc_nos,
            "projects": projects, "codes": codes, "facts": rows,
            "row_label": " | ".join(str(f.get("value") or "") for f in rows
                                    if f.get("col_index") is not None
                                    and f.get("row_index") == row_index)[:200]}


def _shared(a: dict, b: dict) -> dict:
    kinds = {
        "project_value_shared": bool(a["projects"] & b["projects"]),
        "counterparty_code_shared": bool(a["codes"] & b["codes"]),
        "tax_code_shared": bool(a["tax_codes"] & b["tax_codes"]),
        "amount_equal": bool(a["amounts"] & b["amounts"]),
        "doc_no_shared": bool(a["doc_nos"] & b["doc_nos"]),
        "same_doc_type_pair": (a.get("category", ""), b.get("category", "")) in _TYPE_PAIRS,
    }
    shared_values = {
        "projects": sorted(a["projects"] & b["projects"]),
        "codes": sorted(a["codes"] & b["codes"]),
        "tax_codes": sorted(a["tax_codes"] & b["tax_codes"]),
        "amounts": sorted(a["amounts"] & b["amounts"]),
        "doc_nos": sorted(a["doc_nos"] & b["doc_nos"]),
    }
    return {"kinds": kinds, "values": shared_values}


def _packet(sig: dict, limit: int = 14) -> list[dict]:
    """给模型的证据包（脱敏后的值 + 位置，不带明文）。"""
    out = []
    for f in (sig.get("facts") or [])[:limit]:
        out.append({
            "header": f.get("header"), "value": f.get("value"),
            "semantic": f.get("semantic"), "coord": f.get("coord"),
            "row": f.get("row_index"), "col": f.get("col_index"),
            "page": f.get("page_no"), "kind": f.get("fact_kind"),
        })
    return out


def collect_candidates(doc_keys: list[str], *, include_rows: bool = True,
                       max_candidates: int = 60) -> list[dict]:
    """候选对：doc↔doc（文件/文件）+ row↔doc（表/文件）。只保留有信号的对。"""
    sigs = {k: _signals(k) for k in doc_keys}
    cands: list[dict] = []

    for i, a in enumerate(doc_keys):
        for b in doc_keys[i + 1:]:
            sh = _shared(sigs[a], sigs[b])
            if not any(sh["kinds"].values()):
                continue
            hints = [r for r, rule in RELATIONS.items()
                     if r != "unrelated" and all(
                         any(sh["kinds"][k] for k in group) for group in rule["required_any"])]
            cands.append({
                "kind": "doc_doc", "src": f"doc:{a}", "dst": f"doc:{b}",
                "src_doc": a, "dst_doc": b, "signals": sh, "relation_hints": hints or [],
                "src_packet": _packet(sigs[a]), "dst_packet": _packet(sigs[b]),
                "src_meta": {"category": sigs[a]["category"], "doc_key": a},
                "dst_meta": {"category": sigs[b]["category"], "doc_key": b},
            })

    if include_rows:
        row_nodes = [n for n in list_nodes("row")]
        for rn in row_nodes:
            rdoc = rn["doc_key"]
            rsig = _row_signals(rdoc, rn["table_index"], rn["row_index"],
                                _facts_of(rdoc))
            scored: list[tuple[int, str, dict]] = []
            for other in doc_keys:
                if other == rdoc:
                    continue          # 表/文件：只连**其它文件**（表内自身关系不算边）
                sh = _shared(rsig, sigs[other])
                score = sum(1 for v in sh["kinds"].values() if v)
                if score == 0:
                    continue
                scored.append((score, other, sh))
            # 每行只保留信号最强的 2 个文件（避免"一行 × 全库"的候选爆炸，
            # 也避免把弱信号对塞给判定模型）
            scored.sort(key=lambda x: (-x[0], x[1]))
            for score, other, sh in scored[:2]:
                cands.append({
                    "kind": "row_doc", "src": rn["node_id"], "dst": f"doc:{other}",
                    "src_doc": rdoc, "dst_doc": other, "signals": sh,
                    "relation_hints": ["table_row_matches_doc"],
                    "src_packet": _packet(rsig), "dst_packet": _packet(sigs[other]),
                    "src_meta": {"table_row": rn["node_id"], "label": rn.get("label"),
                                 "row_preview": rsig["row_label"]},
                    "dst_meta": {"category": sigs[other]["category"], "doc_key": other},
                })
    return cands[:max_candidates]


# =========================================================
# 提示词 + 请求/回复（agent = 本轮"接对话"）
# =========================================================
EDGE_SYSTEM = """你是财务单据关系的判定器。输入是两份材料（文件或表的一行）各自抽取出的
【已脱敏事实】（编号 CO/PT/PJ/TX… 已替代真实名称，掩码首尾是真实片段）。
请只输出一个 JSON 对象：
{"relation": "<关系名>", "confidence_self": 0~1, "evidence": ["<依据类型>", ...], "reason": "≤60字"}
可选关系名（只能选一个）：
  contract_to_invoice    合同→发票（开票关系）
  payment_for_contract   付款→合同（资金流）
  contract_to_logistics  合同→物流（货物流）
  same_project           同一项目
  counterparty_shared    同一对手方
  amount_consistent      金额一致
  table_row_matches_doc  表里这一行对应这份文件
  unrelated              无关系（证据不足时选它，不要硬连）
可选依据类型（evidence 里只能出现这些字符串）：
  project_value_shared / counterparty_code_shared / tax_code_shared / amount_equal /
  doc_no_shared / same_doc_type_pair
要求：证据必须先看"值是否相同/编号是否相同/金额是否相等"，再给关系；
编号相同=同一实体，金额相等≠同一笔业务（还要看项目/对手方/单据号）；
不确定就选 unrelated 并把 confidence_self 调低。不要输出解释文字，只输出 JSON。"""


def _branch_rule_text() -> str:
    """分支编号约定（提示词用）；取不到就返回空串，不影响判定。"""
    try:
        from desens_legend__desens import branch_rule

        return branch_rule()
    except Exception:
        return ""


_BRANCH_RULE = _branch_rule_text()


def build_prompt(cand: dict) -> tuple[str, str]:
    payload = {
        "kind": cand["kind"],
        "side_a": {"node": cand["src"], **cand.get("src_meta", {}),
                   "facts": cand["src_packet"]},
        "side_b": {"node": cand["dst"], **cand.get("dst_meta", {}),
                   "facts": cand["dst_packet"]},
        "shared_signals": {k: v for k, v in cand["signals"]["kinds"].items() if v},
        "shared_values": {k: v for k, v in cand["signals"]["values"].items() if v},
        "relation_hints": cand.get("relation_hints", []),
    }
    # 追加"主编号 + 分支后缀"约定：判定 same_project / counterparty_shared 时，
    # 必须把"同一主体不同标段/片区/分公司"认成同一主体（详见 desens_legend）。
    system = EDGE_SYSTEM
    if _BRANCH_RULE:
        system = f"{EDGE_SYSTEM}\n\n【编号读法补充】\n{_BRANCH_RULE}"
    return system, json.dumps(payload, ensure_ascii=False, indent=2)


def _request_id(cand: dict) -> str:
    """请求 id 只依赖"节点对+类型"，与具体编号文本无关 → 回复文件可跨运行复用。"""
    return hashlib.sha1(f"{cand['kind']}|{cand['src']}|{cand['dst']}".encode("utf-8")).hexdigest()[:16]


def prepare(doc_keys: list[str], *, batch: str = "default", include_rows: bool = True) -> dict:
    """阶段一：建节点 → 收集候选 → 写请求文件（agent 模式）/ 直接调 api（api 模式）。"""
    node_stats = build_nodes(doc_keys)
    cands = collect_candidates(doc_keys, include_rows=include_rows)
    mode = llm_mode()
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    req_path = GRAPH_DIR / f"graph_edge_requests_{batch}.jsonl"
    requests: list[dict] = []
    answers: dict[str, dict] = {}
    for cand in cands:
        system, user = build_prompt(cand)
        rid = _request_id(cand)
        requests.append({"request_id": rid, "kind": cand["kind"], "src": cand["src"],
                         "dst": cand["dst"], "src_doc": cand["src_doc"],
                         "dst_doc": cand["dst_doc"], "system": system, "user": user,
                         "signals": cand["signals"]["kinds"]})
        if mode == "api":
            try:
                from ai_client__ai import complete

                content, _reasoning = complete("edge", system, user, temperature=0.0,
                                               max_tokens=400)
                answers[rid] = _parse_reply(content)
            except Exception as exc:
                answers[rid] = {"relation": "unrelated", "confidence_self": 0.0,
                                "evidence": [], "reason": f"api_error: {exc}"}
    req_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in requests),
                        encoding="utf-8")
    out = {"mode": mode, "nodes": node_stats, "candidates": len(requests),
           "requests_path": str(req_path), "requests": requests}
    if answers:
        reply_path = GRAPH_DIR / f"graph_agent_replies_{batch}.json"
        reply_path.write_text(json.dumps(answers, ensure_ascii=False, indent=2), encoding="utf-8")
        out["replies_path"] = str(reply_path)
        out["result"] = ingest_replies(reply_path, requests=requests, batch=batch)
    return out


def _parse_reply(content: str | dict) -> dict:
    """解析模型回复（容忍 ```json 包裹与多余文字）。"""
    if isinstance(content, dict):
        return content
    text = str(content or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {"relation": "unrelated", "confidence_self": 0.0, "evidence": [],
                "reason": "unparsable"}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return {"relation": "unrelated", "confidence_self": 0.0, "evidence": [],
                "reason": "unparsable"}
    rel = str(data.get("relation") or "unrelated").strip()
    ev = data.get("evidence") or []
    if not isinstance(ev, list):
        ev = []
    return {
        "relation": rel if rel in RELATIONS else "unrelated",
        "confidence_self": float(data.get("confidence_self") or 0.0),
        "evidence": [str(e) for e in ev if str(e) in EVIDENCE_KINDS],
        "reason": str(data.get("reason") or "")[:200],
    }


# =========================================================
# 校验 + 入库（置信度本地重算，不采信模型自述）
# =========================================================
def _recompute(cand: dict, relation: str) -> dict:
    """本地重算证据与置信度（模型只提供关系名与依据类型）。

    返回 {"evidence": {...}, "missing": [...], "confidence": float, "status": str}
    """
    rule = RELATIONS.get(relation) or RELATIONS["unrelated"]
    kinds = cand["signals"]["kinds"]
    if relation == "unrelated":
        return {"evidence": {k: v for k, v in kinds.items() if v},
                "missing": [], "confidence": 0.0, "status": "rejected"}
    missing: list[str] = []
    for group in rule["required_any"]:
        if not any(kinds.get(k) for k in group):
            missing.append("|".join(group))
    matched = [k for k in rule["boost"] if kinds.get(k)]
    confidence = min(0.95, 0.35 + 0.25 * (len(rule["required_any"]) - len(missing))
                     + sum(rule["boost"][k] for k in matched) / max(len(rule["boost"]), 1))
    if not missing:
        status = "validated"
    elif matched:
        status = "hypothesis"
    else:
        status = "rejected"
    return {"evidence": {k: v for k, v in kinds.items() if v},
            "missing": missing, "confidence": round(confidence, 3), "status": status}


def _store_edge(cand: dict, verdict: dict, check: dict, *, method: str, model: str) -> dict:
    from database_serv__infra import get_admin_connection

    relation = verdict["relation"]
    edge_id = _edge_id(cand["src"], cand["dst"], relation)
    if relation == "unrelated":
        return {"stored": False, "edge_id": edge_id, "relation": relation,
                "status": "rejected", "confidence": 0.0}
    with get_admin_connection() as conn, conn.cursor() as cur:
        # 同一对节点**任一方向**上的既有边：既用于冲突判定，也用于"不重复建反向边"
        cur.execute("""SELECT edge_id, relation FROM graph_edges
                       WHERE (src_node=%s AND dst_node=%s) OR (src_node=%s AND dst_node=%s)""",
                    (cand["src"], cand["dst"], cand["dst"], cand["src"]))
        existing = cur.fetchall()
        conflicts = [(eid, rel) for eid, rel in existing if rel != relation]
        same_rel = next((eid for eid, rel in existing if rel == relation), None)
        status = check["status"]
        for old_id, _old_rel in conflicts:
            cur.execute("UPDATE graph_edges SET conflict = TRUE, "
                        "status = CASE WHEN status='validated' THEN 'hypothesis' ELSE status END, "
                        "updated_at = CURRENT_TIMESTAMP WHERE edge_id = %s", (old_id,))
        if conflicts and status == "validated":
            status = "hypothesis"
        evidence_json = json.dumps(
            {"kinds": check["evidence"],
             "values": {k: v for k, v in cand["signals"]["values"].items() if v},
             "claimed_by_model": verdict.get("evidence") or []}, ensure_ascii=False)
        if same_rel:
            # 已有同关系边（含刚才反向遍历到的）→ **更新原行**，方向保持首次判定，
            # 这样"重复建图/反向再判一次"都不会多出一条边（需求：重复不入但更新照常）。
            cur.execute(
                """UPDATE graph_edges
                      SET status=%s, confidence=%s, confidence_self=%s, evidence=%s,
                          missing=%s, conflict=%s, method=%s, model=%s, reason=%s,
                          updated_at=CURRENT_TIMESTAMP
                    WHERE edge_id=%s""",
                (status, check["confidence"], float(verdict.get("confidence_self") or 0.0),
                 evidence_json, json.dumps(check["missing"], ensure_ascii=False),
                 bool(conflicts), method, model, verdict.get("reason"), same_rel))
        else:
            cur.execute(
                """INSERT INTO graph_edges
                   (edge_id, src_node, dst_node, relation, status, confidence, confidence_self,
                    evidence, missing, conflict, method, model, reason)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (src_node, dst_node, relation) DO UPDATE
                   SET status = EXCLUDED.status, confidence = EXCLUDED.confidence,
                       confidence_self = EXCLUDED.confidence_self, evidence = EXCLUDED.evidence,
                       missing = EXCLUDED.missing, conflict = EXCLUDED.conflict,
                       method = EXCLUDED.method, model = EXCLUDED.model, reason = EXCLUDED.reason,
                       updated_at = CURRENT_TIMESTAMP""",
                (edge_id, cand["src"], cand["dst"], relation, status, check["confidence"],
                 float(verdict.get("confidence_self") or 0.0), evidence_json,
                 json.dumps(check["missing"], ensure_ascii=False), bool(conflicts), method, model,
                 verdict.get("reason")),
            )
        conn.commit()
    _log({"action": "edge", "edge_id": edge_id, "src": cand["src"], "dst": cand["dst"],
          "relation": relation, "status": status, "confidence": check["confidence"],
          "confidence_self": verdict.get("confidence_self"), "missing": check["missing"],
          "conflict": bool(conflicts), "method": method,
          "updated_existing": bool(same_rel)})
    return {"stored": True, "edge_id": edge_id, "relation": relation, "status": status,
            "confidence": check["confidence"], "conflict": bool(conflicts),
            "updated_existing": bool(same_rel), "missing": check["missing"]}


def _lookup_candidate(requests: list[dict], rid: str) -> dict | None:
    for r in requests:
        if r["request_id"] == rid:
            return r
    return None


def ingest_replies(
    replies_path: str | Path,
    *,
    requests: list[dict] | None = None,
    batch: str = "default",
    model: str = "agent:conversation",
) -> dict:
    """阶段二：读取回复（agent 模式：对话侧回填的 JSON）→ 本地重算 → 入库。

    回复文件形如 {"<request_id>": {"relation": "...", "confidence_self": 0.9,
                                  "evidence": ["amount_equal", ...], "reason": "..."}}
    """
    path = Path(replies_path)
    replies = json.loads(path.read_text(encoding="utf-8"))
    if requests is None:
        req_path = GRAPH_DIR / f"graph_edge_requests_{batch}.jsonl"
        requests = [json.loads(ln) for ln in req_path.read_text(encoding="utf-8").splitlines()
                    if ln.strip()]
    results = []
    for req in requests:
        rid = req["request_id"]
        raw = replies.get(rid)
        verdict = _parse_reply(raw) if raw is not None else {
            "relation": "unrelated", "confidence_self": 0.0, "evidence": [],
            "reason": "no_reply"}
        cand = {
            "kind": req["kind"], "src": req["src"], "dst": req["dst"],
            "src_doc": req["src_doc"], "dst_doc": req["dst_doc"],
            "signals": {"kinds": req["signals"], "values": {}},
        }
        # 从请求里取回共享值（请求文件里不存 values，这里按节点重算，保证"不采信文本"）
        cand["signals"]["values"] = _shared_values_from_nodes(req)
        check = _recompute(cand, verdict["relation"])
        results.append(_store_edge(cand, verdict, check, method="agent", model=model))
    return {"answered": len(results), "validated": sum(1 for r in results if r["status"] == "validated"),
            "hypothesis": sum(1 for r in results if r["status"] == "hypothesis"),
            "rejected": sum(1 for r in results if r["status"] == "rejected"),
            "results": results}


def _shared_values_from_nodes(req: dict) -> dict:
    """按节点重算共享值（用于入库留痕；不依赖模型给出的文本）。"""
    def side(nid: str) -> dict:
        if nid.startswith("doc:"):
            return _signals(nid[4:])
        if nid.startswith("row:"):
            doc_key, tail = nid[4:].split("#t", 1)
            ti, ri = tail.split("r", 1)
            return _row_signals(doc_key, int(ti), int(ri), _facts_of(doc_key))
        return {"projects": set(), "codes": set(), "tax_codes": set(), "amounts": set(),
                "doc_nos": set()}

    sh = _shared(side(req["src"]), side(req["dst"]))
    return sh["values"]


def build_edges(doc_keys: list[str], *, batch: str = "default", include_rows: bool = True) -> dict:
    """一次性走完（api 模式）；agent 模式下返回待回复的请求。"""
    return prepare(doc_keys, batch=batch, include_rows=include_rows)


# =========================================================
# 动态（增量）建图：一个文档入库就立刻建它的关系；存量文档按待建队列补齐
# =========================================================
# 设计要点（对应需求：动态建图 / 存量优先 / 重复不入但更新照常）：
#   1. **入一个建一个**：`build_for_doc(doc_key)` 只做"这份文档 ↔ 其它文档"的候选与边，
#      节点用增量 UPSERT（`build_nodes(replace=False)`），不碰别人的节点/边；
#   2. **存量优先**：`pending_docs()` 列出"库里有、但没建过图或事实已变"的文档，
#      按 doc_no 从旧到新（先来的先补），`build_pending()` 逐个补建；
#   3. **重复不入、更新照常**：
#        · 同一份内容重复扫描 → 上游 dedup 直接跳过 → 这里根本不会被调用（不入）；
#        · 文档被重扫（内容/脱敏配置变了）→ `graph_build_state` 指纹不一致 → 重跑，
#          节点/边全部 UPSERT（不产生重复行），本批文档的过期节点同步清理；
#        · 边带**方向无关的 pair_key + 信号指纹**缓存（`graph_pair_cache`）：
#          指纹没变就不再调模型（省 token），指纹变了才重判并**更新**原边。
import hashlib as _hashlib
import threading as _threading

_BUILD_STOP = _threading.Event()

# ---- 单飞后台建图队列 ----
# 为什么必须排队（实测教训）：EDGE 链路是推理模型，**一次候选判定约 35 秒**
# （qwen3.8-flash 思考型；10 个候选 ≈ 6 分钟）。如果"每份文档各起一个线程"，
# 一次拖 18 份文件就会同时开 18 条 AI 链：撞限流、CPU/网络打满、日志交错。
# 所以改成**一个后台 worker + 一个队列**：同一时刻只有一份文档在建图。
import queue as _queue

_QUEUE: "_queue.Queue[str]" = _queue.Queue()
_QUEUE_SEEN: set[str] = set()
_QUEUE_LOCK = _threading.Lock()
_WORKER: "_threading.Thread | None" = None
_QUEUE_STATS = {"enqueued": 0, "built": 0, "skipped": 0, "failed": 0,
                "stopped": 0, "last_doc": "", "last_msg": ""}


def enqueue_for_build(doc_key: str, *, reason: str = "scan") -> dict:
    """把文档放入**单飞**后台建图队列（同一时刻只有一份文档在建图）。

    去重：同一 doc_key 在队列里只排一次（重复扫描/重复触发不会重复花 token）。
    """
    if not doc_key:
        return {"queued": False, "msg": "缺少 doc_key"}
    with _QUEUE_LOCK:
        if doc_key in _QUEUE_SEEN:
            return {"queued": False, "msg": "已在建图队列中（不重复排队）",
                    "queue": _QUEUE.qsize()}
        _QUEUE_SEEN.add(doc_key)
        _QUEUE.put(doc_key)
        _QUEUE_STATS["enqueued"] += 1
        size = _QUEUE.qsize()
    # ⚠️ 必须在**锁外**启动 worker：_ensure_worker 自己也要拿同一把 Lock，
    #    否则 threading.Lock 非可重入 → 永久死锁（实测踩过，进程挂死）。
    started = _ensure_worker()
    _log({"action": "enqueue_build", "doc_key": doc_key, "reason": reason,
          "queue": size, "worker_started": started})
    return {"queued": True, "queue": size, "worker_started": started}


def _ensure_worker() -> bool:
    """确保后台 worker 在跑（幂等）。返回是否本次真正启动。"""
    global _WORKER
    with _QUEUE_LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return False
        _WORKER = _threading.Thread(target=_build_worker, daemon=True, name="edge-build-queue")
        _WORKER.start()
        return True


def _build_worker() -> None:
    """后台 worker：逐份建图；空闲 30 秒后退出（下次入队再启，避免常驻）。"""
    while True:
        try:
            doc_key = _QUEUE.get(timeout=30)
        except _queue.Empty:
            return
        try:
            if build_stop_requested():
                _QUEUE_STATS["stopped"] += 1
                _log({"action": "build_queue_stopped", "doc_key": doc_key})
                continue
            res = build_for_doc(doc_key, batch="queue")
            if res.get("skipped"):
                _QUEUE_STATS["skipped"] += 1
            else:
                _QUEUE_STATS["built"] += 1
            _QUEUE_STATS["last_doc"] = doc_key
            _QUEUE_STATS["last_msg"] = str(res.get("msg") or "")
            print(f"[动态建图] {doc_key} → {res.get('msg')}", flush=True)
        except Exception as exc:                       # noqa: BLE001
            _QUEUE_STATS["failed"] += 1
            _QUEUE_STATS["last_msg"] = f"{type(exc).__name__}: {exc}"
            print(f"[动态建图] {doc_key} 失败（不影响扫描）：{type(exc).__name__}: {exc}",
                  flush=True)
            _log({"action": "build_for_doc_error", "doc_key": doc_key,
                  "error": f"{type(exc).__name__}: {exc}"})
        finally:
            with _QUEUE_LOCK:
                _QUEUE_SEEN.discard(doc_key)
            _QUEUE.task_done()


def build_queue_status() -> dict:
    """后台建图队列状态（UI 展示：还有几份在排队/跑）。"""
    with _QUEUE_LOCK:
        alive = bool(_WORKER is not None and _WORKER.is_alive())
        return {"queue": _QUEUE.qsize(), "in_flight": len(_QUEUE_SEEN),
                "worker_alive": alive, "stop_requested": build_stop_requested(),
                **_QUEUE_STATS}


def request_stop_build(reason: str = "") -> None:
    """请求停止"补齐存量"的长任务（动态建图不受影响：它一份文档一次，很快）。"""
    _BUILD_STOP.set()
    _log({"action": "build_stop_requested", "reason": reason})


def build_stop_requested() -> bool:
    return _BUILD_STOP.is_set()


def clear_build_stop() -> None:
    _BUILD_STOP.clear()


def _fingerprint(doc_key: str) -> dict:
    """文档的建图指纹：事实条数 + hub 哈希（hub 变了说明正文/脱敏结果变了）。"""
    from database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT fact_count, hub_hash FROM l1_documents WHERE doc_key = %s",
                    (doc_key,))
        row = cur.fetchone()
    return {"facts_count": int((row[0] if row else 0) or 0),
            "hub_hash": str((row[1] if row else "") or "")}


_PAIR_SEP = "\u001f"      # ASCII US 分隔符：可安全存进 PG text（0x00 不行，实测会 ValueError）


def _pair_key(src: str, dst: str) -> str:
    """方向无关的对键：同一对节点无论谁先建图，键都相同 → 不会重复建反向边。"""
    a, b = sorted([str(src), str(dst)])
    return f"{a}{_PAIR_SEP}{b}"


def purge_pair_cache_for_doc(doc_key: str) -> int:
    """清掉与该文档相关的边判定缓存（删除文档时调用，避免残留旧判定）。"""
    from database_serv__infra import get_admin_connection

    n = 0
    with get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT pair_key FROM graph_pair_cache")
        victims = []
        for (pk,) in cur.fetchall():
            a, _, b = str(pk).partition(_PAIR_SEP)
            if _node_doc(a) == doc_key or _node_doc(b) == doc_key:
                victims.append(pk)
        if victims:
            cur.execute("DELETE FROM graph_pair_cache WHERE pair_key = ANY(%s)", (victims,))
            n = cur.rowcount
        conn.commit()
    return n


def _pair_signature(cand: dict) -> str:
    """候选对的"信号指纹"：节点对 + 命中信号种类 + 共享值。

    指纹不变 → 关系判定结果必然不变（判定只看这些输入）→ 直接复用缓存，不再调模型；
    指纹变化（金额/编号/项目变了）→ 重判并**更新**原边（不新增行）。
    """
    kinds = cand.get("signals", {}).get("kinds") or {}
    values = cand.get("signals", {}).get("values") or {}
    payload = {
        "pair": _pair_key(cand["src"], cand["dst"]),
        "kinds": sorted(k for k, v in kinds.items() if v),
        "values": {k: sorted(str(x) for x in (v or [])) for k, v in values.items() if v},
        "relation_hints": sorted(cand.get("relation_hints") or []),
    }
    return _hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True)
                         .encode("utf-8")).hexdigest()


def _cache_get(pair_key: str, signature: str) -> dict | None:
    from database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT signature, relation, confidence_self, evidence, reason, model "
                    "FROM graph_pair_cache WHERE pair_key = %s", (pair_key,))
        row = cur.fetchone()
    if not row or row[0] != signature:
        return None
    return {"relation": row[1], "confidence_self": row[2], "evidence": row[3] or [],
            "reason": row[4] or "", "model": row[5] or "", "from_cache": True}


def _cache_put(pair_key: str, signature: str, verdict: dict, model: str) -> None:
    from database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO graph_pair_cache
               (pair_key, signature, relation, confidence_self, evidence, reason, model,
                updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
               ON CONFLICT (pair_key) DO UPDATE
                 SET signature = EXCLUDED.signature, relation = EXCLUDED.relation,
                     confidence_self = EXCLUDED.confidence_self, evidence = EXCLUDED.evidence,
                     reason = EXCLUDED.reason, model = EXCLUDED.model,
                     updated_at = CURRENT_TIMESTAMP""",
            (pair_key, signature, verdict.get("relation"), verdict.get("confidence_self"),
             json.dumps(verdict.get("evidence") or [], ensure_ascii=False),
             verdict.get("reason"), model))
        conn.commit()


def collect_candidates_for_doc(doc_key: str, *, include_rows: bool = True,
                               max_pairs: int | None = None) -> list[dict]:
    """只收集"这份文档参与"的候选对（doc↔doc + 本文档行↔其它文档）。

    与全量 `collect_candidates` 的区别：不枚举 docs×docs 的所有组合，
    只做新文档与存量的一轮，因此"入一个节点就建一次"是 O(N) 而不是 O(N²)。
    """
    from database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT doc_key FROM l1_documents WHERE doc_key <> %s ORDER BY doc_no",
                    (doc_key,))
        others = [r[0] for r in cur.fetchall()]
    me = _signals(doc_key)
    cands: list[dict] = []
    for other in others:
        sig = _signals(other)
        sh = _shared(me, sig)
        if not any(sh["kinds"].values()):
            continue
        hints = [r for r, rule in RELATIONS.items()
                 if r != "unrelated" and all(any(sh["kinds"][k] for k in group)
                                             for group in rule["required_any"])]
        cands.append({
            "kind": "doc_doc", "src": f"doc:{doc_key}", "dst": f"doc:{other}",
            "src_doc": doc_key, "dst_doc": other, "signals": sh,
            "relation_hints": hints or [],
            "src_packet": _packet(me), "dst_packet": _packet(sig),
            "src_meta": {"category": me["category"], "doc_key": doc_key},
            "dst_meta": {"category": sig["category"], "doc_key": other},
        })
    if include_rows:
        facts = _facts_of(doc_key)
        sigs = {o: _signals(o) for o in others}      # 预取一次，避免每行都重算别人的信号
        seen_rows: set[tuple[int, int]] = set()
        for f in facts:
            if f.get("table_index") is None or f.get("row_index") is None:
                continue
            key = (int(f["table_index"]), int(f["row_index"]))
            if key in seen_rows:                     # 同一行有多个单元格 → 只处理一次
                continue
            seen_rows.add(key)
            ti, ri = key
            rsig = _row_signals(doc_key, ti, ri, facts)
            scored: list[tuple[int, str, dict]] = []
            for other in others:
                sh = _shared(rsig, sigs[other])
                score = sum(1 for v in sh["kinds"].values() if v)
                if score:
                    scored.append((score, other, sh))
            scored.sort(key=lambda x: (-x[0], x[1]))
            for score, other, sh in scored[:2]:     # 每行最多连 2 份文件（防候选爆炸）
                cands.append({
                    "kind": "row_doc", "src": f"row:{doc_key}#t{ti}r{ri}",
                    "dst": f"doc:{other}", "src_doc": doc_key, "dst_doc": other,
                    "signals": sh, "relation_hints": ["table_row_matches_doc"],
                    "src_packet": _packet(rsig), "dst_packet": _packet(sigs[other]),
                    "src_meta": {"table_row": f"row:{doc_key}#t{ti}r{ri}",
                                 "row_preview": rsig["row_label"]},
                    "dst_meta": {"category": sigs[other].get("category", ""), "doc_key": other},
                })
            if max_pairs and len(cands) >= max_pairs:
                break
    return cands[:max_pairs] if max_pairs else cands


def _judge_candidate(cand: dict) -> tuple[dict | None, str, bool]:
    """判定一个候选对：命中缓存 → 直接复用；否则调 EDGE 链路。

    返回 (verdict, model, from_cache)；agent 模式（无 key）返回 (None, "agent", False)
    并把请求追加到请求文件，等人工/外部 agent 回填后 ingest_replies 收敛。
    """
    pair_key = _pair_key(cand["src"], cand["dst"])
    signature = _pair_signature(cand)
    cached = _cache_get(pair_key, signature)
    if cached:
        return cached, cached.get("model") or "cache", True
    system, user = build_prompt(cand)
    if llm_mode() != "api":
        GRAPH_DIR.mkdir(parents=True, exist_ok=True)
        req_path = GRAPH_DIR / f"graph_edge_requests_incremental.jsonl"
        req = {"request_id": _request_id(cand), "kind": cand["kind"], "src": cand["src"],
               "dst": cand["dst"], "src_doc": cand["src_doc"], "dst_doc": cand["dst_doc"],
               "system": system, "user": user, "signals": cand["signals"]["kinds"]}
        with req_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(req, ensure_ascii=False) + "\n")
        return None, "agent", False
    from ai_client__ai import complete, load_chain_config

    model = (load_chain_config("edge") or {}).get("model", "edge")
    content, _reasoning = complete("edge", system, user, temperature=0.0, max_tokens=400)
    verdict = _parse_reply(content)
    _cache_put(pair_key, signature, verdict, str(model))
    return verdict, str(model), False


def _state_upsert(doc_key: str, info: dict) -> None:
    fp = _fingerprint(doc_key)
    from database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO graph_build_state
               (doc_key, built_at, facts_count, hub_hash, nodes, candidates, stored, status, note)
               VALUES (%s, CURRENT_TIMESTAMP, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (doc_key) DO UPDATE
                 SET built_at = CURRENT_TIMESTAMP, facts_count = EXCLUDED.facts_count,
                     hub_hash = EXCLUDED.hub_hash, nodes = EXCLUDED.nodes,
                     candidates = EXCLUDED.candidates, stored = EXCLUDED.stored,
                     status = EXCLUDED.status, note = EXCLUDED.note""",
            (doc_key, fp["facts_count"], fp["hub_hash"], int(info.get("nodes") or 0),
             int(info.get("candidates") or 0), int(info.get("stored") or 0),
             str(info.get("status") or "built"), str(info.get("note") or "")[:500]))
        conn.commit()


def build_for_doc(doc_key: str, *, include_rows: bool = True, force: bool = False,
                  max_pairs: int | None = None, batch: str = "incremental") -> dict:
    """**动态建图**：一份文档入库后立刻建它的节点与关系（增量、幂等）。

    返回值里 `skipped=True` 表示"指纹没变，已是最新"（重复扫描/重复调用 → 不入、不重复建）；
    `reused` 是命中 pair 缓存（指纹没变、没调模型）的候选数。

    max_pairs：单份文档最多判多少个候选对（防止"一行×全库"式爆炸）。
      默认取环境变量 `EDGE_MAX_PAIRS_PER_DOC`，未设则 30。
      注意实测吞吐：EDGE 链路是推理模型，**一个候选约 35 秒**，30 个候选 ≈ 17 分钟，
      所以调小它可以明显加快（代价是可能漏掉末尾那些弱候选）。
    """
    ensure_tables()
    if max_pairs is None:
        try:
            max_pairs = int(os.getenv("EDGE_MAX_PAIRS_PER_DOC", "30"))
        except ValueError:
            max_pairs = 30
    if not doc_key:
        return {"ok": False, "msg": "缺少 doc_key"}
    fp = _fingerprint(doc_key)
    if not force:
        try:
            from database_serv__infra import get_connection

            with get_connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT facts_count, hub_hash, status FROM graph_build_state "
                            "WHERE doc_key = %s", (doc_key,))
                row = cur.fetchone()
            if row and row[0] == fp["facts_count"] and (row[1] or "") == fp["hub_hash"] \
                    and row[2] in ("built", "skipped", "partial"):
                return {"ok": True, "skipped": True, "doc_key": doc_key,
                        "msg": f"已是最新（facts={fp['facts_count']}，未重复建图）"}
        except Exception:
            pass

    node_stats = build_nodes([doc_key], replace=False)
    cands = collect_candidates_for_doc(doc_key, include_rows=include_rows, max_pairs=max_pairs)
    stored = reused = pending = failed = 0
    errors: list[str] = []
    for cand in cands:
        if build_stop_requested():
            break
        try:
            verdict, model, from_cache = _judge_candidate(cand)
        except Exception as exc:          # 单个候选判定失败不影响这份文档的其余候选
            failed += 1
            errors.append(f"{cand['src'][:40]}↔{cand['dst'][:40]}: "
                          f"{type(exc).__name__}: {exc}")
            continue
        if verdict is None:                      # agent 模式：只写请求文件
            pending += 1
            continue
        reused += 1 if from_cache else 0
        check = _recompute(cand, verdict["relation"])
        try:
            res = _store_edge(cand, verdict, check,
                              method="cache" if from_cache else "api", model=model)
        except Exception as exc:
            failed += 1
            errors.append(f"store {cand['src'][:40]}: {type(exc).__name__}: {exc}")
            continue
        if res.get("stored"):
            stored += 1
    status = "built" if not (pending or failed) else "partial"
    note = (f"节点{node_stats.get('nodes')} 候选{len(cands)} 入库{stored} 复用{reused}"
            + (f" 待人工回复{pending}" if pending else "")
            + (f" 失败{failed}" if failed else ""))
    _state_upsert(doc_key, {"nodes": node_stats.get("nodes"), "candidates": len(cands),
                            "stored": stored, "status": status, "note": note})
    _log({"action": "build_for_doc", "doc_key": doc_key, "nodes": node_stats.get("nodes"),
          "candidates": len(cands), "stored": stored, "reused": reused, "pending": pending,
          "failed": failed, "errors": errors[:3], "mode": llm_mode(), "batch": batch})
    return {"ok": True, "skipped": False, "doc_key": doc_key, "nodes": node_stats,
            "candidates": len(cands), "stored": stored, "reused": reused, "pending": pending,
            "failed": failed, "errors": errors, "status": status, "msg": note}


def pending_docs(*, include_changed: bool = True, limit: int | None = None) -> list[dict]:
    """待建图清单（存量优先）：库里有、但没建过图 / 事实变了 的文档，旧的在前。"""
    ensure_tables()
    from database_serv__infra import get_connection

    sql = """SELECT d.doc_key, d.doc_no, d.fact_count, COALESCE(d.hub_hash, ''), d.category,
                    s.facts_count, COALESCE(s.hub_hash, ''), s.status, s.built_at, s.note
               FROM l1_documents d
               LEFT JOIN graph_build_state s ON s.doc_key = d.doc_key
              ORDER BY d.doc_no"""
    out: list[dict] = []
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(sql)
        for (doc_key, doc_no, fc, hh, cat, s_fc, s_hh, status, built_at, note) in cur.fetchall():
            # ⚠️ 别用 `s_fc or -1`：facts_count=0 是**合法值**（无表格事实的文档），
            # 会被 `or` 当假值 → 每次都被判成"已变"。必须显式判 None。
            facts_now = int(fc or 0)
            facts_built = None if s_fc is None else int(s_fc)
            hash_now = hh or ""
            hash_built = None if s_hh is None else (s_hh or "")
            if status is None:
                why = "从未建图"
            elif str(status) == "error":
                why = f"上次失败（{note or '未记录原因'}）"
            elif include_changed and (facts_built != facts_now or hash_built != hash_now):
                why = f"事实/正文已变（facts {facts_built}→{facts_now}）"
            elif str(status) == "partial":
                why = f"上次未完成（{note or ''}）"
            else:
                continue
            out.append({"doc_key": doc_key, "doc_no": doc_no, "category": cat,
                        "reason": why, "status": status, "built_at":
                        built_at.isoformat(sep=" ", timespec="seconds") if built_at else None,
                        "note": note})
    return out[:limit] if limit else out


def build_pending(*, max_docs: int | None = None, force: bool = False,
                  progress=None, batch: str = "backlog") -> dict:
    """**补齐存量**：把"之前有文件但没建关系"的文档逐个补建（旧文件优先）。

    注意：本函数**不动**停止标记——调用方（UI/自动补齐）若要开一次新任务，先自己
    `clear_build_stop()`；否则运行中 `request_stop_build()`（"停止补齐"按钮）会被这里清掉。
    """
    pend = pending_docs()
    todo = pend[:max_docs] if max_docs else pend
    done, skipped, failed = 0, 0, 0
    details: list[dict] = []
    for i, item in enumerate(todo, start=1):
        if build_stop_requested():
            break
        doc_key = item["doc_key"]
        if progress:
            try:
                progress(i, len(todo), doc_key, item.get("reason"))
            except Exception:
                pass
        try:
            res = build_for_doc(doc_key, force=force, batch=batch)
            done += 1
            details.append({"doc_key": doc_key, "ok": True, "msg": res.get("msg"),
                            "stored": res.get("stored", 0), "reused": res.get("reused", 0)})
        except Exception as exc:
            failed += 1
            details.append({"doc_key": doc_key, "ok": False,
                            "msg": f"{type(exc).__name__}: {exc}"})
            _state_upsert(doc_key, {"status": "error", "note": f"{type(exc).__name__}: {exc}"})
    summary = {"total": len(pend), "processed": len(details), "ok": done, "failed": failed,
               "stopped": build_stop_requested(), "details": details}
    _log({"action": "build_pending", **{k: v for k, v in summary.items() if k != "details"}})
    return summary


def graph_state() -> dict:
    """建图总览：节点/边计数 + 待建队列 + 上次构建时间（UI 按钮文案与自检用）。"""
    ensure_tables()
    from database_serv__infra import get_connection

    counts = count_edges()
    nodes = {"doc": 0, "table": 0, "row": 0, "entity": 0}
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT node_type, COUNT(*) FROM graph_nodes GROUP BY 1")
        for t, n in cur.fetchall():
            nodes[str(t)] = int(n)
        cur.execute("SELECT COUNT(*), MAX(built_at) FROM graph_build_state")
        built, last = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM graph_pair_cache")
        cached = cur.fetchone()[0]
    pend = pending_docs()
    return {"edges": counts, "nodes": nodes, "node_total": sum(nodes.values()),
            "built_docs": int(built or 0), "last_built_at":
            last.isoformat(sep=" ", timespec="seconds") if last else None,
            "pair_cache": int(cached or 0), "pending": len(pend),
            "pending_head": [p["doc_key"] for p in pend[:5]]}


# =========================================================
# 查询
# =========================================================
_EDGE_COLS = ("edge_id, src_node, dst_node, relation, status, confidence, confidence_self, "
              "evidence, missing, conflict, method, model, reason, created_at")


def entry_points() -> dict:
    """**建图/遍历的入口清单（检查结论，供 UI/文档/自测引用）**。

    为什么要有这个函数：实测"对话层每次遍历 0 条链"的根因不是遍历算法，而是
    **graph_nodes / graph_edges 一直是空表**——即"写入侧没有任何触发者"。
    这里把入口现状固化成可查询的记录，避免下次又靠猜。

    读侧（存在，已在用）：
      · chat_engine__ai.generate → graph_query.query + evidence_block（对话层；
        仅当 RETRIEVAL_METHOD=graph）
      · graph_query.chains_for_doc（看某份文档的上下游关系）
      · project_admin__ui.HypothesisEdgeDialog → list_hypotheses/confirm_edge/reject_edge
        （假设边人工证实/驳回；**前提是已经有边**）

    写侧（本模块；必须有人调用才会产生图）：
      · build_nodes(doc_keys)        → graph_nodes（doc/table/row/entity 四类节点）
      · collect_candidates(doc_keys) → 候选对（按共享信号筛）
      · prepare(doc_keys, batch=…)   → 建节点 + 写请求（api 模式直接调 EDGE_AI_*）
      · ingest_replies(reply_file,…) → 收敛为 graph_edges（本地重算置信度/冲突）
      · build_edges(doc_keys, …)     → = prepare 的别名

    触发者现状（root cause）：
      · **无自动触发**：hub_pipeline__desens 扫描完只做 L1（事实/概括/哈希），
        从不调用本模块；table__ui 里也没有"建图/建边"按钮、没有定时任务。
        ⇒ 只要没人手动跑一次，graph_* 就是 0 行，L3 遍历必然 0 条链。
      · 手动跑法（`llm_mode()` 返回 api 时会自动用 EDGE_AI_* 判定）：
            import edge_build__graph_edges as eb
            eb.build_edges([<doc_key>, …], batch="manual")
        agent 模式（未配 EDGE key）则先写
        `logs/graph/graph_edge_requests_<batch>.jsonl`，人工/外部 agent 回
        `logs/graph/graph_agent_replies_<batch>.json` 后再 `ingest_replies`。
    """
    counts = count_edges()
    return {
        "read_side": {
            "对话层": "chat_engine__ai.generate → graph_query.query/evidence_block",
            "文档关系": "graph_query.chains_for_doc(doc_key)",
            "假设边人工确认": "project_admin__ui.HypothesisEdgeDialog",
        },
        "write_side": {
            "建节点": "build_nodes(doc_keys)",
            "候选对": "collect_candidates(doc_keys)",
            "生成/请求": "prepare(doc_keys, batch=…) / build_edges(…)",
            "收敛入库": "ingest_replies(reply_file, requests=…, batch=…)",
        },
        "triggers": {
            "自动": "**无**（hub_pipeline 不调本模块；UI 无按钮）← 0 链的直接原因",
            "手动": "eb.build_edges([doc_key, …], batch='manual')",
        },
        "mode": llm_mode(),
        "counts": counts,
        "empty_reason": ("图是空的：graph_nodes/graph_edges 均 0 行 → L3 遍历必然 0 条链"
                         if not counts.get("total") else ""),
    }


def list_edges(*, status: str | None = None, relation: str | None = None,
               doc_key: str | None = None) -> list[dict]:
    from database_serv__infra import get_connection

    sql = f"SELECT {_EDGE_COLS} FROM graph_edges"
    where, params = [], []
    if status:
        where.append("status = %s")
        params.append(status)
    if relation:
        where.append("relation = %s")
        params.append(relation)
    if doc_key:
        where.append("(src_node LIKE %s OR dst_node LIKE %s)")
        params += [f"%{doc_key}%", f"%{doc_key}%"]
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY status, relation, src_node, dst_node"
    out = []
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        for r in cur.fetchall():
            out.append({"edge_id": r[0], "src": r[1], "dst": r[2], "relation": r[3],
                        "status": r[4], "confidence": r[5], "confidence_self": r[6],
                        "evidence": r[7], "missing": r[8], "conflict": r[9],
                        "method": r[10], "model": r[11], "reason": r[12],
                        "created_at": r[13].isoformat(sep=" ", timespec="seconds") if r[13] else None})
    return out


def count_edges() -> dict:
    from database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, COUNT(*) FROM graph_edges GROUP BY status")
        by_status = {s: n for s, n in cur.fetchall()}
        cur.execute("SELECT relation, COUNT(*) FROM graph_edges GROUP BY relation")
        by_rel = {r: n for r, n in cur.fetchall()}
    return {"total": sum(by_status.values()), "by_status": by_status, "by_relation": by_rel}


def edge_summary() -> list[dict]:
    """人读清单：边 + 两端标签 + 置信度 + 依据 + 冲突。"""
    nodes = {n["node_id"]: n for n in list_nodes()}
    out = []
    for e in list_edges():
        s, d = nodes.get(e["src"], {}), nodes.get(e["dst"], {})
        out.append({
            "edge_id": e["edge_id"], "relation": e["relation"], "status": e["status"],
            "confidence": e["confidence"], "conflict": e["conflict"],
            "src": e["src"], "src_label": s.get("label") or s.get("value") or "",
            "src_type": s.get("node_type"), "src_category": s.get("category"),
            "dst": e["dst"], "dst_label": d.get("label") or d.get("value") or "",
            "dst_type": d.get("node_type"), "dst_category": d.get("category"),
            "evidence": list((e["evidence"] or {}).get("kinds", {}).keys()),
            "missing": e["missing"], "reason": e["reason"],
        })
    return out
