"""待入账队列（账务入账改为**用户审核后执行**，不再自动写台账）。

为什么要有这一层（本轮需求）：
  · 之前是"扫到合同字段就自动写 contract_projects 台账"——一旦分类判错（发票、汇总表、
    付款申请都被关键词误判成合同），错行就自动进了台账，事后很难追；
  · 现在改成：**识别负责"提名"，人负责"入账"**。
    扫描流水线只把候选（合同/发票）连同**已脱敏**的台账字段放进 `ledger_inbox`（待入账），
    由"入账审核"界面逐条展示 → 用户点「入账」才真正写进对应台账表。

两类候选的落点：
  · 合同（PDF/Word + 命名/标题含"合同/协议"）→ `contract_projects`（合同台账）
  · 发票 → `invoice_ledger`（发票台账）
  · 其它/未识别 → 只登记，`target_table` 为空，界面只允许"不入账"

对外接口：
  ensure_table()
  stage(...)                     入库/更新一条待入账（同一 doc_key 只保留一条 pending）
  stage_from_result(result, user) 流水线结果 → 自动判定类型 + 抽字段 + 入库
  list_items(status=None) / get_item(id) / counts()
  update_fields(id, fields, user)
  approve(id, user, fields=None, key=None)   入账（写台账）
  reject(id, user, reason)                   不入账
  extract_invoice_fields(pages)              发票字段抽取（正则，脱敏文本上运行）
  LOG_DIR / log_event()
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ensure_table",
    "stage",
    "stage_from_result",
    "list_items",
    "get_item",
    "counts",
    "update_fields",
    "approve",
    "reject",
    "set_kind",
    "extract_invoice_fields",
    "hub_json_files",
    "refresh_from_hub",
    "ai_fill",
    "TARGET_TABLES",
    "LOG_DIR",
]

BASE_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = BASE_DIR / "logs" / "ledger_inbox"

# 类型 → 目标台账表
TARGET_TABLES = {"contract": "contract_projects", "invoice": "invoice_ledger"}
STATUS_LABELS = {"pending": "待入账", "posted": "已入账", "rejected": "不入账"}


def ensure_table() -> None:
    from psycopg2 import sql as _sql

    from infra.database_serv__infra import APP_DB_CONFIG, get_admin_connection

    ddl = """
    CREATE TABLE IF NOT EXISTS ledger_inbox (
        id SERIAL PRIMARY KEY,
        doc_key VARCHAR(300) NOT NULL UNIQUE,
        kind VARCHAR(20) NOT NULL DEFAULT 'other',       -- contract | invoice | other
        category VARCHAR(50),
        target_table VARCHAR(64),
        title VARCHAR(300),
        source_file VARCHAR(400),
        source_path TEXT,
        hub_file TEXT,
        fields JSONB NOT NULL DEFAULT '{}'::jsonb,
        fields_source VARCHAR(30),                       -- ai | regex | manual | empty
        status VARCHAR(20) NOT NULL DEFAULT 'pending',   -- pending | posted | rejected
        note TEXT,
        created_by VARCHAR(64),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        decided_by VARCHAR(64),
        decided_at TIMESTAMP,
        posted_table VARCHAR(64),
        posted_key VARCHAR(200)
    )"""
    with get_admin_connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(ddl)
            for idx, col in (("idx_ledger_inbox_status", "status"),
                             ("idx_ledger_inbox_kind", "kind")):
                cur.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON ledger_inbox ({col})")
            cur.execute(
                _sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                    _sql.Identifier("ledger_inbox"), _sql.Identifier(APP_DB_CONFIG["user"]))
            )
            cur.execute(
                _sql.SQL("GRANT USAGE, SELECT ON SEQUENCE {} TO {}").format(
                    _sql.Identifier("ledger_inbox_id_seq"),
                    _sql.Identifier(APP_DB_CONFIG["user"]))
            )


def _log_event(rec: dict) -> None:
    """待入账审计日志（只记状态与字段键名，不记正文）。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **rec}
        with (LOG_DIR / f"inbox_{time.strftime('%Y%m%d')}.jsonl").open(
                "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# =========================================================
# 字段抽取（发票；在**已脱敏**页文本上运行，抽到的编号/号码是掩码后的值）
# =========================================================
_INVOICE_PATTERNS = [
    ("invoice_no", [r"发票号码\s*[:：]?\s*([0-9A-Za-z\[\]\-\*]{4,40})",
                    r"号码\s*[:：]?\s*([0-9]{6,30})"]),
    ("invoice_code", [r"发票代码\s*[:：]?\s*([0-9A-Za-z\[\]\-\*]{4,30})"]),
    ("invoice_date", [r"开票日期\s*[:：]?\s*([0-9]{4}\s*[年\-/.]\s*[0-9]{1,2}\s*[月\-/.]\s*[0-9]{1,2})",
                      r"开票日期\s*[:：]?\s*(\[DT[0-9]+\])"]),
    ("seller", [r"销售方[^\n]{0,10}?[:：]?\s*([^\s|，,；;]{1,40})",
                r"销\s*方[^\n]{0,10}?[:：]?\s*([^\s|，,；;]{1,40})"]),
    ("buyer", [r"购买方[^\n]{0,10}?[:：]?\s*([^\s|，,；;]{1,40})",
               r"购\s*方[^\n]{0,10}?[:：]?\s*([^\s|，,；;]{1,40})"]),
    ("total", [r"价税合计[^\n]{0,20}?[（(]?小写[）)]?\s*[:：]?\s*[¥￥]?\s*([0-9][0-9,\.]{0,20})",
               r"价税合计\s*[:：]?\s*[¥￥]?\s*([0-9][0-9,\.]{0,20})"]),
    ("amount", [r"金额\s*[:：]?\s*[¥￥]?\s*([0-9][0-9,\.]{0,20})",
                r"合\s*计\s*[:：]?\s*[¥￥]?\s*([0-9][0-9,\.]{0,20})"]),
    ("tax", [r"税额\s*[:：]?\s*[¥￥]?\s*([0-9][0-9,\.]{0,20})"]),
    ("contract_code", [r"合同编号\s*[:：]?\s*([A-Za-z0-9\u4e00-\u9fa5\-_/]{2,40})"]),
]
_DATE_CN_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?")


def _number(text: str) -> float | None:
    try:
        v = float(str(text).replace(",", "").strip())
        return v
    except Exception:
        return None


def extract_invoice_fields(pages: list[str] | None) -> dict:
    """从（脱敏后的）页文本里抽发票要点；抽不到就不放该键。"""
    text = "\n".join(str(p or "") for p in (pages or []))
    out: dict = {}
    for key, pats in _INVOICE_PATTERNS:
        for pat in pats:
            m = re.search(pat, text)
            if not m:
                continue
            val = (m.group(1) or "").strip()
            if not val:
                continue
            if key in ("amount", "tax", "total"):
                num = _number(val)
                if num is None:
                    continue
                out[key] = num
            else:
                out[key] = val
            break
    dm = _DATE_CN_RE.search(text)
    if dm and "invoice_date" not in out:
        out["invoice_date"] = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
    return out


def _kind_of(category: str, verdict: dict | None = None) -> str:
    if (verdict or {}).get("passed"):
        return "contract"
    if str(category or "") == "合同":
        return "contract"
    if str(category or "") == "发票":
        return "invoice"
    return "other"


# =========================================================
# 入库 / 查询
# =========================================================
def stage(
    doc_key: str,
    *,
    kind: str = "other",
    category: str = "",
    title: str = "",
    source_file: str = "",
    source_path: str = "",
    hub_file: str = "",
    fields: dict | None = None,
    fields_source: str = "",
    user: dict | None = None,
    note: str = "",
    overwrite_decided: bool = False,
) -> dict:
    """登记一条待入账（同一 doc_key 幂等：已存在则更新字段，不重复建条目）。

    已入账/已驳回的条目默认**不动**（避免重扫把已决定的结果冲掉）；真要覆盖传
    `overwrite_decided=True`。
    """
    ensure_table()
    from infra.database_serv__infra import get_connection

    kind = kind if kind in ("contract", "invoice", "other") else "other"
    target = TARGET_TABLES.get(kind)
    fields = fields or {}
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, status FROM ledger_inbox WHERE doc_key = %s", (doc_key,))
        row = cur.fetchone()
        if row and row[1] != "pending" and not overwrite_decided:
            return {"ok": False, "id": row[0], "status": row[1],
                    "msg": f"已在「{STATUS_LABELS.get(row[1], row[1])}」状态，未改动"}
        if row:
            cur.execute(
                """UPDATE ledger_inbox
                      SET kind=%s, category=%s, target_table=%s, title=%s, source_file=%s,
                          source_path=%s, hub_file=%s, fields=%s, fields_source=%s,
                          status='pending', note=%s
                    WHERE id=%s""",
                (kind, category, target, title[:300], source_file, source_path, hub_file,
                 json.dumps(fields, ensure_ascii=False), fields_source, note, row[0]))
            new_id, action = row[0], "updated"
        else:
            cur.execute(
                """INSERT INTO ledger_inbox
                   (doc_key, kind, category, target_table, title, source_file, source_path,
                    hub_file, fields, fields_source, status, note, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s) RETURNING id""",
                (doc_key, kind, category, target, title[:300], source_file, source_path,
                 hub_file, json.dumps(fields, ensure_ascii=False), fields_source, note,
                 (user or {}).get("username")))
            new_id, action = cur.fetchone()[0], "created"
        conn.commit()
    rec = {"event": f"stage_{action}", "id": new_id, "doc_key": doc_key, "kind": kind,
           "target_table": target, "fields_keys": sorted(fields.keys()),
           "fields_source": fields_source, "by": (user or {}).get("username")}
    _log_event(rec)
    return {"ok": True, "id": new_id, "action": action, "kind": kind, "target_table": target}


def stage_from_result(result: dict, user: dict | None = None, *,
                      overwrite_decided: bool = False) -> dict:
    """扫描流水线结果 → 待入账队列（**这是流水线唯一与台账有关的动作**）。

    判定：`ledger_gate__desens`（= contract_rules）通过 → 合同；分类=发票 → 发票；
    其余 → 其它（只登记，不入账）。
    字段来源：AI 报告里的台账字段（ai）> 正则抽出的合同字段（regex）> 发票正则（regex）。
    `overwrite_decided=True` 时连"已入账/不入账"的条目也重置回待入账（"重新提名"用）。
    """
    if not isinstance(result, dict):
        return {"ok": False, "msg": "结果不是 dict"}
    doc_key = result.get("doc_key") or ""
    hub_path = result.get("hub_json_path")
    if not doc_key:
        return {"ok": False, "msg": "缺少 doc_key"}
    category = str(result.get("category") or "")
    gate = result.get("ledger_gate__desens") or {}
    kind = _kind_of(category, {"passed": bool((gate or {}).get("passed"))})

    pages: list[str] = []
    source_file = ""
    if hub_path:
        try:
            doc = json.loads(Path(hub_path).read_text(encoding="utf-8"))
            pages = [str(p or "") for p in (doc.get("pages") or [])]
            source_file = str(doc.get("source_file") or "")
        except Exception:
            pass

    fields: dict = {}
    fields_source = "empty"
    ai = result.get("ai_report") if isinstance(result.get("ai_report"), dict) else {}
    ai_ledger = (ai or {}).get("ledger") or {}
    if kind == "contract":
        if isinstance(ai_ledger.get("fields"), dict) and ai_ledger["fields"]:
            fields, fields_source = dict(ai_ledger["fields"]), "ai"
        elif isinstance(result.get("contract_fields"), dict) and result["contract_fields"]:
            fields, fields_source = dict(result["contract_fields"]), "regex"
    elif kind == "invoice":
        inv = extract_invoice_fields(pages)
        if inv:
            fields, fields_source = inv, "regex"

    title = ""
    if pages:
        try:
            import desens.contract_rules__desens as contract_rules

            title = contract_rules.title_text(pages).strip().split("\n")[0][:200]
        except Exception:
            title = pages[0][:120]
    note = ""
    if kind == "other":
        note = f"分类={category or '其它'}：未确认为合同/发票，仅登记（不入账）"
    elif not fields:
        note = "未抽到台账字段，可人工填写后入账"

    res = stage(doc_key, kind=kind, category=category, title=title or source_file,
                source_file=source_file or str(result.get("source_file") or ""),
                source_path=str(result.get("source_path") or ""),
                hub_file=str(hub_path or ""), fields=fields, fields_source=fields_source,
                user=user, note=note, overwrite_decided=overwrite_decided)
    res["kind"] = kind
    return res


def _row_to_item(row) -> dict:
    (rid, doc_key, kind, category, target, title, source_file, source_path, hub_file,
     fields, fields_source, status, note, created_by, created_at, decided_by, decided_at,
     posted_table, posted_key) = row
    return {
        "id": rid, "doc_key": doc_key, "kind": kind, "category": category,
        "target_table": target, "title": title, "source_file": source_file,
        "source_path": source_path, "hub_file": hub_file,
        "fields": fields or {}, "fields_source": fields_source,
        "status": status, "status_label": STATUS_LABELS.get(status, status),
        "note": note, "created_by": created_by,
        "created_at": created_at.isoformat(sep=" ", timespec="seconds") if created_at else "",
        "decided_by": decided_by,
        "decided_at": decided_at.isoformat(sep=" ", timespec="seconds") if decided_at else "",
        "posted_table": posted_table, "posted_key": posted_key,
    }


_SELECT = """SELECT id, doc_key, kind, category, target_table, title, source_file,
                    source_path, hub_file, fields, fields_source, status, note, created_by,
                    created_at, decided_by, decided_at, posted_table, posted_key
               FROM ledger_inbox"""


def list_items(status: str | None = "pending", *, kind: str | None = None,
               limit: int = 500) -> list[dict]:
    ensure_table()
    from infra.database_serv__infra import get_connection

    where, args = [], []
    if status:
        where.append("status = %s")
        args.append(status)
    if kind:
        where.append("kind = %s")
        args.append(kind)
    sql = _SELECT + (" WHERE " + " AND ".join(where) if where else "") \
        + " ORDER BY id DESC LIMIT %s"
    args.append(limit)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(args))
        return [_row_to_item(r) for r in cur.fetchall()]


def get_item(item_id: int) -> dict | None:
    ensure_table()
    from infra.database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(_SELECT + " WHERE id = %s", (item_id,))
        row = cur.fetchone()
    return _row_to_item(row) if row else None


def counts() -> dict:
    ensure_table()
    from infra.database_serv__infra import get_connection

    out = {"pending": 0, "posted": 0, "rejected": 0,
           "pending_contract": 0, "pending_invoice": 0, "pending_other": 0}
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, kind, count(*) FROM ledger_inbox GROUP BY 1, 2")
        for status, kind, n in cur.fetchall():
            out[status] = out.get(status, 0) + int(n)
            if status == "pending":
                out[f"pending_{kind}"] = out.get(f"pending_{kind}", 0) + int(n)
    return out


def update_fields(item_id: int, fields: dict, user: dict | None = None, *,
                  source: str = "manual") -> dict:
    """人工/AI 修改待入账字段（入账前校对）。

    `source`：manual=人工改的；ai=AI 读 hub 后填的（界面上要能区分，便于复核）。
    """
    ensure_table()
    from infra.database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, fields FROM ledger_inbox WHERE id = %s", (item_id,))
        row = cur.fetchone()
        if not row:
            return {"ok": False, "msg": "条目不存在"}
        if row[0] != "pending":
            return {"ok": False, "msg": f"当前状态为 {STATUS_LABELS.get(row[0], row[0])}，不能修改"}
        merged = dict(row[1] or {})
        merged.update(fields or {})
        cur.execute("UPDATE ledger_inbox SET fields=%s, fields_source=%s WHERE id=%s",
                    (json.dumps(merged, ensure_ascii=False), source or "manual", item_id))
        conn.commit()
    _log_event({"event": "update_fields", "id": item_id, "source": source or "manual",
                "fields_keys": sorted((fields or {}).keys()),
                "by": (user or {}).get("username")})
    return {"ok": True, "fields": merged}


def approve(item_id: int, user: dict | None = None, *, fields: dict | None = None,
            key: str | None = None) -> dict:
    """**入账**：把这条待入账写进它的目标台账表（由用户点按钮触发）。"""
    item = get_item(item_id)
    if not item:
        return {"ok": False, "msg": "条目不存在"}
    if item["status"] != "pending":
        return {"ok": False, "msg": f"当前状态为 {item['status_label']}，不能重复入账"}
    kind = item["kind"]
    target = TARGET_TABLES.get(kind)
    if not target:
        return {"ok": False, "msg": "该条目未确认为合同/发票，没有对应台账可入账"}

    payload = dict(item["fields"] or {})
    payload.update(fields or {})
    if kind == "contract":
        if key:
            payload["contract_code"] = key
        if not str(payload.get("contract_code") or "").strip():
            return {"ok": False, "msg": "缺少合同编号（可在编辑框里补一个再入账）"}
    else:
        if key:
            payload["invoice_no"] = key
        if not str(payload.get("invoice_no") or "").strip():
            return {"ok": False, "msg": "缺少发票号码（可在编辑框里补一个再入账）"}

    from infra.database_serv__infra import api_save_contract_from_ai, api_save_invoice_from_ai

    if kind == "contract":
        ok, msg = api_save_contract_from_ai(payload, table_name=target)
        posted_key = str(payload.get("contract_code") or "")
    else:
        ok, msg = api_save_invoice_from_ai(payload, table_name=target)
        posted_key = str(payload.get("invoice_no") or "")

    if not ok:
        _log_event({"event": "approve_failed", "id": item_id, "kind": kind,
                    "target": target, "msg": msg, "by": (user or {}).get("username")})
        return {"ok": False, "msg": msg}

    ensure_table()
    from infra.database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE ledger_inbox
                  SET status='posted', fields=%s, decided_by=%s, decided_at=CURRENT_TIMESTAMP,
                      posted_table=%s, posted_key=%s
                WHERE id=%s""",
            (json.dumps(payload, ensure_ascii=False), (user or {}).get("username"),
             target, posted_key[:200], item_id))
        conn.commit()
    _log_event({"event": "approved", "id": item_id, "kind": kind, "doc_key": item["doc_key"],
                "target": target, "posted_key": posted_key, "msg": msg,
                "by": (user or {}).get("username")})
    return {"ok": True, "msg": msg, "target": target, "posted_key": posted_key}


def reject(item_id: int, user: dict | None = None, reason: str = "") -> dict:
    """**不入账**：标记驳回（保留记录可追溯，不写任何台账）。"""
    item = get_item(item_id)
    if not item:
        return {"ok": False, "msg": "条目不存在"}
    if item["status"] != "pending":
        return {"ok": False, "msg": f"当前状态为 {item['status_label']}，不能重复处理"}
    ensure_table()
    from infra.database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE ledger_inbox
                  SET status='rejected', note=%s, decided_by=%s, decided_at=CURRENT_TIMESTAMP
                WHERE id=%s""",
            (reason or item.get("note") or "用户选择不入账", (user or {}).get("username"),
             item_id))
        conn.commit()
    _log_event({"event": "rejected", "id": item_id, "kind": item["kind"],
                "doc_key": item["doc_key"], "reason": reason,
                "by": (user or {}).get("username")})
    return {"ok": True, "msg": "已标记为不入账"}


def set_kind(item_id: int, kind: str, user: dict | None = None) -> dict:
    """人工改判类型（合同/发票/其它）——识别漏判时由用户在审核窗口纠正。

    为什么需要：规则口径是"PDF/Word + 命名/标题含合同/协议"，一份真合同若标题里
    没有"合同"二字就会被提名成"其它"。此时**不该由 AI 擅自入账**，而应让用户在
    审核窗口把它改成"合同"再入账——"识别提名、人工定性"这条线保持完整。
    """
    if kind not in TARGET_TABLES and kind != "other":
        return {"ok": False, "msg": f"未知类型：{kind}"}
    item = get_item(item_id)
    if not item:
        return {"ok": False, "msg": "条目不存在"}
    if item["status"] != "pending":
        return {"ok": False, "msg": f"当前状态为 {item['status_label']}，不能改类型"}
    ensure_table()
    from infra.database_serv__infra import get_connection

    target = TARGET_TABLES.get(kind)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE ledger_inbox SET kind=%s, target_table=%s WHERE id=%s",
                    (kind, target, item_id))
        conn.commit()
    _log_event({"event": "set_kind", "id": item_id, "from": item["kind"], "to": kind,
                "by": (user or {}).get("username")})
    return {"ok": True, "kind": kind, "target_table": target,
            "msg": f"已改为「{KIND_LABEL.get(kind, kind)}」（目标台账：{target or '无'}）"}


KIND_LABEL = {"contract": "合同", "invoice": "发票", "other": "其它"}


# =========================================================
# 「从 hub 刷新」：直接读 hub 里的 JSON 产物，重新提名可能需要入账的合同/发票
# ---------------------------------------------------------
# 与 `hub_pipeline.rescan_hub_classifications`（界面上的「批量重判」）的区别：
#   · 批量重判：以**库里的文档行**为准，重跑分类并回写分类；
#   · 这次：以**磁盘上的 hub 产物**为准（`hub/**/*.json`），不依赖库里有行、不重扫、
#     不 OCR、不调 AI——所以库被清过、hub 从别处拷进来、或扫描中断只落了 hub 的情况下，
#     照样能把"可能要入账的合同"重新捞出来。
# 幂等：同一 doc_key 只保留一条 pending；已入账/已驳回的默认不动。
# =========================================================
def hub_json_files() -> list[Path]:
    """当前仓库 hub 里的**文档产物** JSON（排除伴生文件与 _mapping）。"""
    try:
        from desens.ai_guard__desens import hub_dir

        hub = hub_dir()
    except Exception:
        hub = BASE_DIR / "hub"
    if not hub.exists():
        return []
    out = []
    for p in sorted(hub.rglob("*.json")):
        if p.name.endswith((".features.json", ".l1.json")):
            continue
        if p.parent.name == "_mapping":
            continue
        out.append(p)
    return out


def _doc_key_of(hub_path: Path, doc: dict) -> str:
    """doc_key：优先用 JSON 里的（含相对目录），否则按 hub 内的相对路径推。"""
    key = str(doc.get("doc_key") or "").strip()
    if key:
        return key
    try:
        from desens.ai_guard__desens import hub_dir

        rel = hub_path.relative_to(hub_dir()).as_posix()
    except Exception:
        rel = hub_path.name
    return rel[:-5] if rel.endswith(".json") else rel


def _source_paths(doc_keys: list[str]) -> dict[str, str]:
    """从 hub_index 批量取源文件路径（只为"打开源文件"按钮好用；取不到不影响流程）。"""
    if not doc_keys:
        return {}
    try:
        from infra.database_serv__infra import get_connection

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT doc_key, source_path FROM hub_index WHERE doc_key = ANY(%s)",
                        (list(doc_keys),))
            return {k: (v or "") for k, v in cur.fetchall()}
    except Exception:
        return {}


def refresh_from_hub(user: dict | None = None, *, include_other: bool = False,
                     overwrite_decided: bool = False) -> dict:
    """**从 hub 已有 JSON 重新读取**可能需要入账的合同/发票，幂等地写进待入账队列。

    判定复用与流水线**同一套**东西：`ledger_gate__desens.evaluate`（合同准入闸门）
    + `_kind_of`（类型）+ `stage_from_result`（字段预填：合同走正则兜底、发票走发票正则）。
    不重新 OCR、不动源文件、不调用 AI。

    返回 {scanned, contract, invoice, other, created, updated, skipped_decided,
          skipped_other, no_pages, errors[], files[]}
    """
    import desens.ledger_gate__desens as ledger_gate__desens

    ensure_table()
    stats: dict = {"scanned": 0, "contract": 0, "invoice": 0, "other": 0,
                   "created": 0, "updated": 0, "skipped_decided": 0, "skipped_other": 0,
                   "no_pages": 0, "errors": [], "files": []}

    files = hub_json_files()
    parsed: list[tuple[Path, dict, str]] = []
    for p in files:
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            stats["errors"].append(f"{p.name}: 读取失败 {type(exc).__name__}: {exc}")
            continue
        if not isinstance(doc, dict) or doc.get("kind") not in (None, "hub_doc"):
            continue
        if "pages" not in doc:
            continue
        parsed.append((p, doc, _doc_key_of(p, doc)))

    src_map = _source_paths([k for _p, _d, k in parsed])
    store = None
    try:      # 正则兜底里要用它反查甲方（取不到就让字段留空，不阻断）
        from infra.database_serv__infra import MappingDbStore

        store = MappingDbStore()
    except Exception:
        store = None

    for p, doc, doc_key in parsed:
        stats["scanned"] += 1
        pages = [str(x or "") for x in (doc.get("pages") or [])]
        if not pages or not "".join(pages).strip():
            stats["no_pages"] += 1
            continue
        source_file = str(doc.get("source_file") or "")
        category = str(doc.get("category") or "")
        try:
            gate = ledger_gate__desens.evaluate(pages, file_name=source_file,
                                                category=category,
                                                tables=list(doc.get("tables") or []))
        except Exception as exc:
            stats["errors"].append(f"{doc_key}: 闸门判定失败 {type(exc).__name__}: {exc}")
            continue
        kind = _kind_of(category, gate.to_dict())
        stats[kind if kind in ("contract", "invoice") else "other"] += 1
        if kind == "other" and not include_other:
            stats["skipped_other"] += 1
            continue

        fields: dict = {}
        if kind == "contract" and gate.passed and store is not None:
            try:
                # 用**轻量**模块 contract_rules：绝不 import hub_pipeline（它会拉
                # paddle/torch，实测让"从 hub 刷新"首次点击要等 80s）
                from desens.contract_rules__desens import extract_contract_fields

                fields = extract_contract_fields(
                    source_file and Path(source_file).stem or p.stem, pages, store)
            except Exception:
                fields = {}
        try:
            res = stage_from_result(
                {"doc_key": doc_key, "category": category, "hub_json_path": str(p),
                 "source_path": src_map.get(doc_key, ""), "contract_fields": fields,
                 "ledger_gate__desens": gate.to_dict()},
                user=user, overwrite_decided=overwrite_decided)
        except Exception as exc:
            stats["errors"].append(f"{doc_key}: 送待入账失败 {type(exc).__name__}: {exc}")
            continue
        if res.get("ok"):
            stats[res.get("action", "created")] = stats.get(res.get("action", "created"), 0) + 1
            stats["files"].append({"doc_key": doc_key, "kind": kind,
                                   "action": res.get("action"), "id": res.get("id")})
        else:
            stats["skipped_decided"] += 1
    _log_event({"event": "refresh_from_hub", "by": (user or {}).get("username"),
                "scanned": stats["scanned"], "created": stats["created"],
                "updated": stats["updated"], "skipped_decided": stats["skipped_decided"],
                "skipped_other": stats["skipped_other"], "errors": len(stats["errors"])})
    return stats


def ai_fill(item_id: int, user: dict | None = None) -> dict:
    """**让 AI 读这份 hub 产物并把台账字段填好**（只填不写库；写库仍要用户点「入账」）。

    · 合同 → `ai_parser__ai.fill_contract_ledger`；
    · 发票 → `ai_parser__ai.fill_invoice_ledger`（**也是 AI 读正文抽取，不用正则**）；
      两条走的是**同一条 AI 链路、同一 key 与模型**（`.env` 的 `AI_*`）。
    · 其它类型 / 已决定条目 / hub 产物不在 → 明确拒绝并说明原因。
    密钥：本函数不读任何密钥，也不把密钥写进任何日志（AI 日志出口另有遮蔽）。
    """
    item = get_item(item_id)
    if not item:
        return {"ok": False, "msg": "条目不存在"}
    if item["status"] != "pending":
        return {"ok": False, "msg": f"当前状态为 {item['status_label']}，不能再改字段"}
    kind = item["kind"]
    if kind == "other":
        return {"ok": False, "msg": "该条目未确认为合同/发票（可在界面左上「类型（可改判）」里先改成合同/发票）"}

    hub = str(item.get("hub_file") or "")
    path = Path(hub) if hub else None
    if path is None or not path.exists():
        from desens.ai_guard__desens import hub_dir

        cand = hub_dir() / f"{item['doc_key']}.json"
        path = cand if cand.exists() else None
    if path is None:
        return {"ok": False, "msg": "这条没有 hub 产物（可能已被删除或从未落盘），AI 无从读取；"
                                    "请重新扫描该文件后再试"}

    try:
        import ai.ai_parser__ai as ap

        if kind == "contract":
            rep = ap.fill_contract_ledger(Path(path), user)
            what = "合同台账"
        else:
            rep = ap.fill_invoice_ledger(Path(path), user)
            what = "发票台账"
    except Exception as exc:
        return {"ok": False, "msg": f"AI 读取失败：{type(exc).__name__}: {exc}"}

    fields = dict(rep.get("fields") or {})
    if not fields:
        return {"ok": False, "msg": rep.get("msg") or "AI 没有返回可用字段"}
    need_key = bool(rep.get("need_key") or rep.get("need_code"))
    if not rep.get("ok") and not need_key:
        return {"ok": False, "msg": rep.get("msg") or "AI 未产出可用字段"}

    merged = update_fields(item_id, fields, user=user, source="ai")
    if not merged.get("ok"):
        return merged
    note = ""
    if need_key:
        note = (f"\n注意：正文里没读到{what}的唯一键"
                + ("（发票号码）" if kind == "invoice" else "（合同编号随文件名）")
                + "，请人工补上再入账。")
    return {"ok": True, "fields": merged.get("fields"), "need_key": need_key,
            "msg": f"AI 已按{what}栏目填好字段（**未写台账**，请核对后点「入账」）。" + note}
