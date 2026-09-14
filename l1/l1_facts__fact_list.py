"""L1 细粒度事实清单：把 hub 里的表格与键值行拆成"可取数的事实条目"，每条都带溯源路径。

设计口径（依你的分层要求）：
  · **概括只作定位索引，真正取数要回原文核对** → 每条事实都携带完整溯源：
      页号 / 表块序号 / 行列号 / Excel 坐标（若有）/ 脱敏文本字符区间 /
      原文区间 raw_char_*+raw_exact / 同行其它单元格（row_context，防"只看一个格子看错列"）；
  · 值本身是**已脱敏**的值（编号/掩码），明文不出 hub、不入库；
  · 只做**记录与定位**，不做推断：语义类别只按列头/键名标注（复用 table_desens 的列头语义表），
    拿不准就留空（宁缺勿错）。

事实两类：
  · cell：表格单元格 —— 归属表头 + 语义类别 + 行列/坐标 + 字符区间 + 原文区间 + 同行上下文；
  · kv  ：文本键值行 —— "标签：值"（合同编号/甲方/项目名称/开票日期…），带页内字符区间。

稳定主键 fact_id（幂等重跑不产生重复行）：
  · cell：`{doc_key}|cell|p{page}|t{table}|r{row}|c{col}`
  · kv  ：`{doc_key}|kv|p{page}|s{char_start}`
"""
from __future__ import annotations

import json
import re
from pathlib import Path

__all__ = [
    "EXTRACT_VERSION",
    "extract_facts",
    "ensure_table",
    "store_facts",
    "count_facts",
    "list_facts",
    "KV_MAX_LABEL",
    "KV_MAX_VALUE",
]

# 事实抽取版本：v2 = 表格"先剥标题/签章 → 判 kv → 定表头"分流改写
#   · kv 表按"标签 → 值"成对（v1 是按列走的，kv 表整列没有表头 → 语义全丢）；
#   · 新增 fact_kind = title / signature（题头/落款单独成事实，不参与取数）；
#   · 普通表加"表头不可信"护栏（防把标题/标签当列头）。
# 版本变了说明**语义与 v1 不同**：老 hub 产物重抽即可（fact_id 不含版本，UPSERT 原地覆盖）。
EXTRACT_VERSION = "v2"
KV_MAX_LABEL = 12          # 键名最长（超过视为句子，不做键值事实）
KV_MAX_VALUE = 300         # 值最长（防把整段正文当值）
_KV_RE = re.compile(r"^\s*(?P<label>[^：:\n]{1,12})[：:]\s*(?P<value>\S[^\n]{0,299})\s*$")
# OCR 页文本每行都带块标签前缀（如 "text:合同编号：HT-001"）——解析键值前先剥掉它，
# 否则会把 "text" 当成键名、整行当成值。只剥"全小写英文+下划线"的块标签。
_BLOCK_PREFIX_RE = re.compile(r"^[a-z][a-z_]{1,24}:")
# 键名合法性：以中英文开头，不含句读符号，避免把整句话当键名
_LABEL_OK_RE = re.compile(r"^[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9/\-（）() ]{0,11}$")
_LABEL_BAD = ("，", ",", "。", "；", ";", "、", "的", "是")
# 值以编号/掩码为主的事实（用于标注 value_is_code，便于下游"取数回原文"）
_CODE_RE = re.compile(r"\[(?:本公司·)?[A-Z]{2}\d{4}\]|(?<![A-Za-z])[A-Z]{2}\d{4}(?![0-9])")


def _label_ok(label: str) -> bool:
    """这一格像"字段名"吗（kv 配对的标签侧）？"""
    s = (label or "").strip().rstrip("：:=＝ ")
    if not s or len(s) > 24 or _CODE_RE.search(s):
        return False
    if any(ch.isdigit() for ch in s) and len(s) > 12:
        return False
    return True


def _looks_like_label(text: str) -> bool:
    """这一格本身就像标签（而不是值）——用于"表头不可信时不要把标签当值"、
    以及 kv 配对时"值不能又是标签"。

    注意**不能**把 `match_category()` 也算进来：值也可能命中类别词
    （`无锡鲲珩城市服务有限公司` 命中 company），那会导致真值被当成标签丢掉。
    `table_desens.looks_like_label` 已经是"像标签且不像该类别的合法值"的严格判据。
    """
    s = (text or "").strip()
    if not s:
        return False
    try:
        import desens.table_desens__desens as _td

        return bool(_td.looks_like_label(s))
    except Exception:
        pass
    if not _LABEL_OK_RE.match(s):
        return False
    return not any(bad in s for bad in _LABEL_BAD)


def _semantic_label(text: str) -> bool:
    """这一格是"**带语义的字段名**"吗（命中列头字典且像标签）？"""
    s = (text or "").strip()
    if not s:
        return False
    try:
        import desens.table_desens__desens as _td

        return bool(_td.is_semantic_label(s))
    except Exception:
        return False


def _semantic(header: str) -> str | None:
    """列头 → 语义类别（复用表格脱敏的列头语义表；认不出留 None，不猜）。"""
    try:
        import desens.table_desens__desens as table_desens__desens

        return table_desens__desens.match_category(header)
    except Exception:
        return None


def _cell_facts_for_table(t: dict, page_no: int, table_index: int) -> list[dict]:
    """表块 → 事实（**按布局分流**：kv 按标签配对；普通表按列头；标题/签章单列事实）。

    本轮修复（根因）：kv 表按设计不产出表头（`header=[]`、`rows`=全部行），
    而旧实现一律"列头 + 列值" → kv 表里**每一格都成了没有表头的值**，
    连标签文字本身（"项目付款进度""申请拨款金额"）都变成事实值，
    于是"金额在不在事实层"取决于表头，结果金额虽然被抽出来却失去了语义。
    现在：kv → `header=标签, value=值`；标签格不再产出值事实；
    标题/签章行单独成事实（`fact_kind=title/signature`），不干扰取数。
    """
    header = [str(h or "") for h in (t.get("header") or [])]
    rows = t.get("rows") or []
    layout = str(t.get("layout") or "").lower()
    hdr_anchors = t.get("header_anchors") or []
    cell_anchors = t.get("cell_anchors") or []
    hdr_by_col = {}
    for a in hdr_anchors:
        if a:
            hdr_by_col[a.get("col")] = a
    facts: list[dict] = []

    def _anchor(r_i: int, c_i: int) -> dict | None:
        row_anchors = cell_anchors[r_i - 1] if 0 <= r_i - 1 < len(cell_anchors) else []
        return row_anchors[c_i] if c_i < len(row_anchors) else None

    # ---- 标题行 / 签章行：单独成事实（供定位，不参与金额/编号取值）----
    for kind, key in (("title", "title_rows"), ("signature", "signature_rows")):
        for r_i, row in enumerate(t.get(key) or [], start=1):
            txt = " | ".join(str(c or "").strip() for c in row if str(c or "").strip())
            if not txt:
                continue
            facts.append({
                "fact_kind": kind, "page_no": page_no, "table_index": table_index,
                "row_index": r_i, "col_index": None, "coord": None,
                "header": None, "semantic": None, "value": txt,
                "value_norm": "".join(txt.split())[:120], "value_is_code": bool(_CODE_RE.search(txt)),
                "char_start": None, "char_end": None, "raw_char_start": None,
                "raw_char_end": None, "raw_exact": None,
                "row_context": {"role": kind, "row": [str(c or "") for c in row]},
            })

    # ---- kv 布局：标签 → 值成对 ----
    if layout == "kv":
        for r_i, row in enumerate(rows, start=1):
            cells = [str(c or "") for c in row]
            used: set[int] = set()      # 已被当成"值"用掉的格，不再当标签看
            i = 0
            while i < len(cells):
                label = cells[i].strip()
                if not label or i in used:
                    i += 1
                    continue
                if not _label_ok(label):
                    i += 1
                    continue
                # 值格：同行右侧第一个非空；没有则看下一行同列（纵向 kv）
                val, v_col, v_row = "", None, None
                for j in range(i + 1, len(cells)):
                    if cells[j].strip():
                        val, v_col, v_row = cells[j], j, r_i
                        used.add(j)
                        break
                if not val and r_i < len(rows):
                    nxt = [str(c or "") for c in rows[r_i]]
                    if i < len(nxt) and nxt[i].strip():
                        val, v_col, v_row = nxt[i], i, r_i + 1
                # 取到的"值"如果本身是**带语义的字段名**，说明这一格不是标签
                # （`付款方式 | □现金 ☑银行转账 | 开户行 | 宁波银行…` 里，
                #  "□现金 ☑银行转账" 的右侧是下一个标签"开户行"，不是它的值）。
                # 只排"带语义的标签"：`宣读人 | 张晓晓` 的值形似标签但命中不了字典。
                if val and _semantic_label(val):
                    i += 1
                    continue
                if val and val.strip() != label:
                    a = _anchor(v_row or r_i, v_col or 0) or {}
                    facts.append({
                        "fact_kind": "cell", "page_no": page_no, "table_index": table_index,
                        "row_index": v_row or r_i, "col_index": v_col,
                        "coord": (a or {}).get("coord"),
                        "header": label, "semantic": _semantic(label), "value": val,
                        "value_norm": "".join(val.split())[:120],
                        "value_is_code": bool(_CODE_RE.search(val)),
                        "char_start": (a or {}).get("char_start"),
                        "char_end": (a or {}).get("char_end"),
                        "raw_char_start": (a or {}).get("raw_char_start"),
                        "raw_char_end": (a or {}).get("raw_char_end"),
                        "raw_exact": (a or {}).get("raw_exact"),
                        "row_context": {"header": [label], "row": [label, val],
                                        "layout": "kv"},
                    })
                i += 1
        return facts

    # ---- 普通表：列头 + 列值（带"表头不可信"护栏）----
    head_nonempty = sum(1 for h in header if h.strip())
    widest = max((sum(1 for c in r if str(c or "").strip()) for r in rows), default=0)
    header_trusted = not (head_nonempty <= 1 and widest > 1)
    for r, row in enumerate(rows, start=1):
        row_anchors = cell_anchors[r - 1] if r - 1 < len(cell_anchors) else []
        row_ctx = {"header": header, "row": [str(c or "") for c in row]}
        for c, raw_val in enumerate(row):
            val = str(raw_val or "")
            if not val.strip():
                continue
            a = row_anchors[c] if c < len(row_anchors) else None
            hdr = (header[c] if c < len(header) else "") if header_trusted else ""
            # 护栏：没有可信表头时，**看起来就是标签的格子不再当成值**（防"标签变事实值"）
            if not hdr and _looks_like_label(val):
                continue
            facts.append({
                "fact_kind": "cell",
                "page_no": page_no,
                "table_index": table_index,
                "row_index": r,
                "col_index": c,
                "coord": (a or {}).get("coord"),
                "header": hdr,
                "semantic": _semantic(hdr),
                "value": val,
                "value_norm": "".join(val.split())[:120],
                "value_is_code": bool(_CODE_RE.search(val)),
                "char_start": (a or {}).get("char_start"),
                "char_end": (a or {}).get("char_end"),
                "raw_char_start": (a or {}).get("raw_char_start"),
                "raw_char_end": (a or {}).get("raw_char_end"),
                "raw_exact": (a or {}).get("raw_exact"),
                "row_context": row_ctx,
            })
    # 表头自身也作为一条事实（列语义的来源，供"这一列是什么"核对）
    for a in hdr_anchors:
        if not a:
            continue
        c = a.get("col")
        if c is None or c >= len(header) or not header[c].strip():
            continue
        facts.append({
            "fact_kind": "cell", "page_no": page_no, "table_index": table_index,
            "row_index": 0, "col_index": c, "coord": a.get("coord"),
            "header": header[c], "semantic": _semantic(header[c]),
            "value": header[c], "value_norm": "".join(header[c].split())[:120],
            "value_is_code": False,
            "char_start": a.get("char_start"), "char_end": a.get("char_end"),
            "raw_char_start": a.get("raw_char_start"), "raw_char_end": a.get("raw_char_end"),
            "raw_exact": a.get("raw_exact"), "row_context": {"header": header, "row": header},
        })
    return facts


def extract_facts(hub_doc: dict, doc_key: str | None = None) -> list[dict]:
    """从 hub JSON 抽出全部事实（cell + kv），每条都带 fact_id 与溯源路径。"""
    doc_key = str(doc_key or hub_doc.get("doc_key") or "")
    facts: list[dict] = []

    for ti, t in enumerate(hub_doc.get("tables") or [], start=1):
        page_no = int(t.get("page") or t.get("page_from") or 1)
        facts.extend(_cell_facts_for_table(t, page_no, ti))

    for pi, page_text in enumerate(hub_doc.get("pages") or [], start=1):
        text = str(page_text or "")
        pos = 0
        for line in text.split("\n"):
            body = line
            shift = 0
            m0 = _BLOCK_PREFIX_RE.match(body)     # 剥掉 OCR 块标签前缀（text:/table:/…）
            if m0:
                shift = m0.end()
                body = body[shift:]
            m = _KV_RE.match(body)
            if m:
                label = m.group("label").strip()
                value = m.group("value").strip()
                if _label_ok(label) and value and len(value) <= KV_MAX_VALUE:
                    start = pos + shift + m.start("value")
                    end = pos + shift + m.end("value")
                    facts.append({
                        "fact_kind": "kv", "page_no": pi, "table_index": None,
                        "row_index": None, "col_index": None, "coord": None,
                        "header": label, "semantic": _semantic(label),
                        "value": value, "value_norm": "".join(value.split())[:120],
                        "value_is_code": bool(_CODE_RE.search(value)),
                        "char_start": start, "char_end": end,
                        "raw_char_start": None, "raw_char_end": None, "raw_exact": None,
                        "row_context": {"line": line},
                    })
            pos += len(line) + 1     # +1：换行符

    for f in facts:
        kind = f["fact_kind"]
        if kind == "cell":
            f["fact_id"] = (f"{doc_key}|cell|p{f['page_no']}|t{f['table_index']}"
                            f"|r{f['row_index']}|c{f['col_index']}")
        elif kind in ("title", "signature"):
            # 标题/签章没有 char 锚点，必须带上 kind/表/行，否则同页多条会撞 fact_id
            f["fact_id"] = (f"{doc_key}|{kind}|p{f['page_no']}|t{f['table_index']}"
                            f"|r{f['row_index']}")
        else:
            f["fact_id"] = f"{doc_key}|kv|p{f['page_no']}|s{f['char_start']}"
        f["doc_key"] = doc_key
        f["extract_version"] = EXTRACT_VERSION
    return facts


def ensure_table() -> None:
    """懒建事实表（幂等；管理员连接建表 + 授权应用角色）。"""
    from psycopg2 import sql as _sql

    from infra.database_serv__infra import APP_DB_CONFIG, get_admin_connection

    ddl = """
    CREATE TABLE IF NOT EXISTS l1_facts (
        fact_id VARCHAR(400) PRIMARY KEY,
        doc_key VARCHAR(300) NOT NULL,
        extract_version VARCHAR(32) NOT NULL DEFAULT 'v1',
        fact_kind VARCHAR(16) NOT NULL,          -- cell | kv | title | signature
        page_no INT,
        table_index INT,
        row_index INT,
        col_index INT,
        coord VARCHAR(16),
        header VARCHAR(120),
        semantic VARCHAR(32),
        value TEXT NOT NULL,
        value_norm VARCHAR(160),
        value_is_code BOOLEAN NOT NULL DEFAULT FALSE,
        char_start INT,
        char_end INT,
        raw_char_start INT,
        raw_char_end INT,
        raw_exact BOOLEAN,
        row_context JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )"""
    with get_admin_connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(ddl)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_l1_facts_doc ON l1_facts (doc_key)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_l1_facts_semantic ON l1_facts (semantic)")
            cur.execute(
                _sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                    _sql.Identifier("l1_facts"), _sql.Identifier(APP_DB_CONFIG["user"])
                )
            )


def store_facts(doc_key: str, facts: list[dict]) -> int:
    """写入事实清单（UPSERT 幂等：同一 fact_id 覆盖；doc_key 下多余旧行删除）。返回条数。"""
    ensure_table()
    from infra.database_serv__infra import get_admin_connection

    ids = [f["fact_id"] for f in facts]
    with get_admin_connection() as conn, conn.cursor() as cur:
        if ids:
            cur.execute("DELETE FROM l1_facts WHERE doc_key = %s AND NOT (fact_id = ANY(%s))",
                        (doc_key, ids))
        else:
            cur.execute("DELETE FROM l1_facts WHERE doc_key = %s", (doc_key,))
        for f in facts:
            cur.execute(
                """INSERT INTO l1_facts
                   (fact_id, doc_key, extract_version, fact_kind, page_no, table_index,
                    row_index, col_index, coord, header, semantic, value, value_norm,
                    value_is_code, char_start, char_end, raw_char_start, raw_char_end,
                    raw_exact, row_context)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (fact_id) DO UPDATE
                   SET page_no = EXCLUDED.page_no,
                       table_index = EXCLUDED.table_index,
                       row_index = EXCLUDED.row_index,
                       col_index = EXCLUDED.col_index,
                       coord = EXCLUDED.coord,
                       header = EXCLUDED.header,
                       semantic = EXCLUDED.semantic,
                       value = EXCLUDED.value,
                       value_norm = EXCLUDED.value_norm,
                       value_is_code = EXCLUDED.value_is_code,
                       char_start = EXCLUDED.char_start,
                       char_end = EXCLUDED.char_end,
                       raw_char_start = EXCLUDED.raw_char_start,
                       raw_char_end = EXCLUDED.raw_char_end,
                       raw_exact = EXCLUDED.raw_exact,
                       row_context = EXCLUDED.row_context""",
                (f["fact_id"], doc_key, f.get("extract_version", EXTRACT_VERSION),
                 f["fact_kind"], f.get("page_no"), f.get("table_index"),
                 f.get("row_index"), f.get("col_index"), f.get("coord"),
                 (f.get("header") or "")[:120], f.get("semantic"), f["value"],
                 f.get("value_norm"), bool(f.get("value_is_code")),
                 f.get("char_start"), f.get("char_end"),
                 f.get("raw_char_start"), f.get("raw_char_end"), f.get("raw_exact"),
                 json.dumps(f.get("row_context") or {}, ensure_ascii=False)),
            )
        # 回写文档级计数：以前 `l1_documents.fact_count` 一直是 0（只有写入 l1_facts，
        # 从不回写），于是"这份文档抽到几条事实"根本查不出来，L4/建图也拿不到规模。
        # table_count 同理（表块数按"表序号最大值"取，不去重明细）。
        table_ids = {f.get("table_index") for f in facts if f.get("table_index")}
        cur.execute(
            "UPDATE l1_documents SET fact_count = %s, table_count = %s WHERE doc_key = %s",
            (len(facts), len(table_ids), doc_key),
        )
        conn.commit()
    return len(facts)


def count_facts(doc_key: str) -> int:
    ensure_table()
    from infra.database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM l1_facts WHERE doc_key = %s", (doc_key,))
        return int(cur.fetchone()[0])


def list_facts(doc_key: str, *, limit: int | None = None, semantic: str | None = None) -> list[dict]:
    """按文档取事实（供 L2/L3/核对链路查询）。"""
    ensure_table()
    from infra.database_serv__infra import get_connection

    sql = ("SELECT fact_id, fact_kind, page_no, table_index, row_index, col_index, coord, "
           "header, semantic, value, value_is_code, char_start, char_end, raw_char_start, "
           "raw_char_end, raw_exact, row_context FROM l1_facts WHERE doc_key = %s")
    params: list = [doc_key]
    if semantic:
        sql += " AND semantic = %s"
        params.append(semantic)
    sql += " ORDER BY page_no, table_index NULLS LAST, row_index NULLS LAST, col_index NULLS LAST"
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    out: list[dict] = []
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        for row in cur.fetchall():
            out.append({
                "fact_id": row[0], "fact_kind": row[1], "page_no": row[2], "table_index": row[3],
                "row_index": row[4], "col_index": row[5], "coord": row[6], "header": row[7],
                "semantic": row[8], "value": row[9], "value_is_code": row[10],
                "char_start": row[11], "char_end": row[12], "raw_char_start": row[13],
                "raw_char_end": row[14], "raw_exact": row[15], "row_context": row[16],
            })
    return out
