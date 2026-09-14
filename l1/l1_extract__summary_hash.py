"""L1 抽取层：文件哈希 → 段落切分 → 概括 → 稳定 ID → 状态投影（只记录，不动源目录）。

按既定细节实现：
  1. 遍历文件夹中每个文件：**保持树形结构不变**，只读取 + 记录，绝不改名/移动/写入源目录；
  2. 每个文件先赋予哈希：file_hash（源字节 sha256）+ text_hash（归一化文本 sha256）；
     同内容 → 同哈希（去重与幂等的基础）。
  3. 段落切分：颗粒度 800~2000 字，优先级 **段落完整 > 语义完整 > 颗粒度**；
     单个块超上限时按句末标点做语义切分；记录页码，跨页记录首尾页。
  4. 概括：
     - 段落概括 ≤50 字；
     - 文档概括 50~80 字（由段落概括汇总生成，省 token；无段落时回退截断原文）；
     - 模型统一 DeepSeek Flash（读取链路 AI_MODEL）。
  5. 唯一 ID：文档 `n`（l1_documents.doc_no，数据库序列，跨运行稳定）；
     段落 `n-n1`（如 `12-3`）。
  6. 每条概括都带**指向原文的路径**（path_json：doc_key + 页码 + 页内字符区间）。
  7. 所有节点构建写日志：logs/graph/graph_build_<date>.jsonl。

幂等性（本轮不专门测，但结构上保证）：唯一键 (doc_key, extract_version) 与
(doc_key, extract_version, seg_index)，重跑走 UPSERT，doc_no 不变。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
# hub 根随工作区（界面叫「仓库」）走：见 workspace__infra（DSH_HUB_DIR + apply_active 热切换）
from infra.workspace__infra import hub_root as _hub_root

HUB_DIR = _hub_root()
GRAPH_LOG_DIR = BASE_DIR / "logs" / "graph"

EXTRACT_VERSION = "v1"
SEG_MIN_CHARS = 800          # 段落颗粒度下限（软约束：优先段落/语义完整）
SEG_MAX_CHARS = 2000         # 段落颗粒度上限（硬约束：超长块按句切分）
DOC_SUMMARY_MIN = 50         # 文档概括目标下限（不满足不报错）
DOC_SUMMARY_MAX = 80         # 文档概括硬上限
SEG_SUMMARY_MAX = 50         # 段落概括硬上限

_SENT_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])")


# =========================================================
# 哈希
# =========================================================
def hash_file(path: str | Path) -> str:
    """源文件字节 sha256（同文件同哈希）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_text(text: str) -> str:
    """归一化文本 sha256（去空白后哈希，容忍排版差异）。"""
    norm = "".join((text or "").split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def doc_key_of(hub_json_path: str | Path, data: dict | None = None) -> str:
    """hub JSON 的稳定主键：优先用 JSON 里记录的 doc_key（文件夹输入时含相对路径），
    旧数据/旧文件没有该字段时回退文件名 stem（向后兼容）。"""
    if isinstance(data, dict) and data.get("doc_key"):
        return str(data["doc_key"])
    return Path(hub_json_path).stem


def hub_fingerprint(hub_json_path: str | Path) -> dict:
    """hub 资产指纹：**hub JSON 本身**的哈希 + 结构统计（概括/哈希的对应记录入口）。

    为什么需要：hub JSON 里只有脱敏正文/表格/坐标，**不含**概括与哈希；
    而"这份 hub 有没有被改过、有没有被投影过、投影对应哪一版"必须有据可查。
    这里给出：
      · hub_hash      = hub JSON 文件字节 sha256（内容指纹，判等/幂等用）
      · content_hash  = 脱敏正文归一化后 sha256（与 l1_documents.text_hash 一致口径）
      · page_count / table_count / cell_count / offset_map_count / table_anchor_count
    """
    p = Path(hub_json_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    pages = list(data.get("pages") or [])
    tables = list(data.get("tables") or [])
    cell_count = 0
    anchor_count = 0
    for t in tables:
        for row in (t.get("cell_anchors") or []):
            for a in (row or []):
                if a:
                    cell_count += 1
                    if a.get("raw_char_start") is not None:
                        anchor_count += 1
        anchor_count += sum(1 for a in (t.get("header_anchors") or []) if a)
    return {
        "hub_hash": hash_file(p),
        "content_hash": hash_text("\n".join(pages)),
        "page_count": len(pages),
        "table_count": len(tables),
        "cell_count": cell_count,
        "table_anchor_count": anchor_count,
        "offset_map_count": len(data.get("offset_maps") or []),
        "table_data": tables,
    }


def build_summary_sidecar(hub_json_path: str | Path, projection: dict) -> Path:
    """把"概括 + 哈希"写成 hub 目录里的伴生文件 <stem>.l1.json。

    hub JSON 本体保持"纯 L0 内容"（脱敏正文/表格/坐标，内容哈希才稳定）；
    概括与哈希（L1 产出）放在伴生文件里，随 hub 一起存放、可被 L2/L3 直接读取。
    """
    p = Path(hub_json_path)
    sidecar = p.parent / f"{p.stem}.l1.json"
    payload = {
        "kind": "l1_summary",
        "doc_key": (projection.get("doc_key") or p.stem),
        "extract_version": projection.get("extract_version", EXTRACT_VERSION),
        "hub_file": p.name,
        "hub_hash": projection.get("hub_hash"),
        "content_hash": projection.get("content_hash"),
        "file_hash": projection.get("file_hash"),
        "text_hash": projection.get("text_hash"),
        "page_count": projection.get("page_count"),
        "table_count": projection.get("table_count"),
        "cell_count": projection.get("cell_count"),
        "fact_count": projection.get("fact_count"),
        "doc_no": projection.get("doc_no"),
        "doc_summary": projection.get("doc_summary"),
        "model": projection.get("model"),
        "facts": projection.get("facts") or [],
        "segments": [
            {
                "seg_id": s.get("seg_id"),
                "seg_index": s.get("seg_index"),
                "page_from": s.get("page_from"),
                "page_to": s.get("page_to"),
                "char_count": s.get("char_count"),
                "text_hash": s.get("text_hash"),
                "seg_summary": s.get("seg_summary"),
                "path_json": s.get("path_json"),
            }
            for s in (projection.get("segments") or [])
        ],
    }
    sidecar.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return sidecar


# =========================================================
# 段落切分（800~2000 字；段落完整 > 语义完整 > 颗粒度）
# =========================================================
def _split_long_block(block: str, max_chars: int) -> list[str]:
    """单块超上限：按句末标点做语义切分（保不住整段时保证句子完整）。"""
    pieces: list[str] = []
    rest = block
    while len(rest) > max_chars:
        window = rest[: max_chars + 1]
        cut = 0
        for m in _SENT_SPLIT_RE.finditer(window):
            cut = m.end()
        if cut <= 0:                      # 没有句末标点 → 退化为硬切
            cut = max_chars
        pieces.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        pieces.append(rest)
    return pieces


def segment_pages(pages: list[str], *, min_chars: int = SEG_MIN_CHARS,
                  max_chars: int = SEG_MAX_CHARS) -> list[dict]:
    """把逐页文本切成段落块。

    返回 [{text, page_from, page_to, char_start, char_end}]（页为 1 基，char 为**页内**偏移）：
      · page_from/char_start 指向首块所在页与页内起点；
      · page_to/char_end 指向末块所在页与页内终点（跨页时两者必然不同）。
    规则：按行（块）累积；加下一块会超上限 → 收束当前段；单块超上限 → 句级切分。
    """
    segments: list[dict] = []
    buf: list[str] = []
    buf_len = 0
    start_page = 1
    start_char = 0
    cur_page = 1
    cur_char = 0

    def flush(end_page: int, end_char: int) -> None:
        nonlocal buf, buf_len
        if buf:
            text = "\n".join(buf)
            segments.append({
                "text": text,
                "page_from": start_page,
                "page_to": end_page,
                "char_start": start_char,
                "char_end": end_char,
            })
        buf, buf_len = [], 0

    for page_index, page_text in enumerate(pages, start=1):
        offset = 0
        for raw_line in (page_text or "").split("\n"):
            line = raw_line.rstrip()
            offset += len(raw_line) + 1
            if not line.strip():
                continue
            pieces = _split_long_block(line, max_chars) if len(line) > max_chars else [line]
            for piece in pieces:
                piece_len = len(piece)
                if buf and buf_len + piece_len + 1 > max_chars:
                    flush(cur_page, cur_char)          # 收束：保证上一段完整
                    start_page, start_char = page_index, max(offset - piece_len, 0)
                if not buf:
                    start_page, start_char = page_index, max(offset - piece_len, 0)
                buf.append(piece)
                buf_len += piece_len + 1
                cur_page, cur_char = page_index, offset
    flush(cur_page, cur_char)
    return segments


# =========================================================
# 概括（DeepSeek Flash；可由测试注入桩函数）
# =========================================================
def _chat(system: str, user: str) -> str:
    """调用 **l1 链路**（隔离）：DeepSeek Flash，只取 content。

    隔离说明：不再借用 ai_parser__ai.chat_ai——从概括这一步起各链路完全分开，
    不共用任何进程级状态（旧链路的 _AI_LOG_PATH/_LAST_REASONING 与本链路无关）。
    测试中可替换本函数注入桩。

    ⚠️ 本轮修复两处（实测导致"概括全空"）：
      ① `max_tokens` 从 400 提到 2000：l1 链路是**推理模型**，400 会被推理过程吃光
         （长表格文档尤其明显），返回的 content 是空串；
      ② 空 content **当异常抛**：以前空串被当成"正常返回"直接落库，于是
         `l1_documents.doc_summary` 大面积为空，甚至把模型抱怨的话
         （"未收到各段概括内容…"）当成概括存了下来。
    """
    from ai.ai_client__ai import complete

    content, _reasoning = complete("l1", system, user, temperature=0.2, max_tokens=2000)
    if not (content or "").strip():
        raise RuntimeError("L1 概括返回空内容（多半被推理占满 max_tokens）")
    return content


def _clip(text: str, limit: int) -> str:
    """截断到 limit 字（尽量在句末标点处收口），供概括硬上限使用。"""
    s = (text or "").strip().replace("\n", " ")
    if len(s) <= limit:
        return s
    cut = 0
    for m in _SENT_SPLIT_RE.finditer(s[:limit]):
        cut = m.end()
    return (s[:cut] if cut >= limit * 0.6 else s[:limit]).rstrip()


def _legend() -> str:
    """脱敏编号说明段（告诉模型 CO/PT/PJ… 各代表什么；与代码同步，见 desens_legend）。"""
    from desens.desens_legend__desens import prompt_block

    return prompt_block()


def summarize_segment(text: str) -> str:
    """段落概括：≤50 字，保留关键实体/金额/日期/编号。"""
    system = ("你是财务文档概括员。只输出概括文本本身，不要解释、不要标点外的多余内容。"
              + _legend())
    user = ("用不超过 50 个汉字概括下面这段财务文档内容；"
            "保留其中的主体、金额、日期、编号等关键信息；不得编造。\n\n" + (text or "")[:4000])
    try:
        out = _chat(system, user)
    except Exception:
        out = (text or "")[:SEG_SUMMARY_MAX]      # 模型不可用时退化为截断（不阻断）
    return _clip(out, SEG_SUMMARY_MAX)


def summarize_document(segment_summaries: list[str], fallback_text: str = "") -> str:
    """文档概括：50~80 字。优先用段落概括汇总（省 token），无则回退原文片段。"""
    if segment_summaries:
        joined = "\n".join(f"- {s}" for s in segment_summaries if s)[:6000]
        user = ("根据下列各段概括，用 50~80 个汉字概括整份财务文档；"
                "保留关键主体、金额、日期、编号，不得编造。\n\n" + joined)
    else:
        user = ("用 50~80 个汉字概括下面这份财务文档；保留关键主体、金额、日期、编号，"
                "不得编造。\n\n" + (fallback_text or "")[:6000])
    system = "你是财务文档概括员。只输出概括文本本身。" + _legend()
    try:
        out = _chat(system, user)
    except Exception:
        out = (segment_summaries[0] if segment_summaries else fallback_text)[:DOC_SUMMARY_MAX]
    return _clip(out, DOC_SUMMARY_MAX)


# =========================================================
# 投影（数据库记录）+ 日志
# =========================================================
_TABLES_ENSURED = False


def _ensure_tables() -> None:
    """确保 L1 投影表存在（幂等；老库未跑 init_db 时也能用）。

    ⚠️ 本轮加固（实测踩坑）：这里每次都跑 `ALTER TABLE … ADD COLUMN IF NOT EXISTS`，
    而 ALTER 需要 **ACCESS EXCLUSIVE** 锁——只要有一个长事务（例如被中断的扫描进程
    留下 `idle in transaction` 的读事务）持有 ACCESS SHARE，DDL 就会**无限等待**，
    整条流水线卡死（实测卡 10 分钟以上，`pg_stat_activity` 显示 `wait_event=Lock`）。
    两道保险：
      ① 进程内只做一次（加锁；同一进程重复投影不再跑 DDL）；
      ② DDL 连接设 `lock_timeout=3s`：拿不到锁就跳过（列本来就存在，跳过无副作用），
         不再让"别人的长事务"拖死扫描。
    """
    global _TABLES_ENSURED
    if _TABLES_ENSURED:
        return
    from infra.database_serv__infra import get_admin_connection

    ddl_docs = """
    CREATE TABLE IF NOT EXISTS l1_documents (
        doc_no SERIAL PRIMARY KEY,
        doc_key VARCHAR(300) NOT NULL,
        extract_version VARCHAR(32) NOT NULL DEFAULT 'v1',
        file_hash VARCHAR(64) NOT NULL,
        text_hash VARCHAR(64) NOT NULL,
        hub_hash VARCHAR(64),
        category VARCHAR(32),
        doc_summary TEXT,
        page_count INT NOT NULL DEFAULT 0,
        table_count INT NOT NULL DEFAULT 0,
        segment_count INT NOT NULL DEFAULT 0,
        source_path TEXT,
        model VARCHAR(64),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (doc_key, extract_version)
    )"""
    ddl_segs = """
    CREATE TABLE IF NOT EXISTS l1_segments (
        seg_id VARCHAR(80) PRIMARY KEY,
        doc_key VARCHAR(300) NOT NULL,
        extract_version VARCHAR(32) NOT NULL DEFAULT 'v1',
        seg_index INT NOT NULL,
        page_from INT NOT NULL,
        page_to INT NOT NULL,
        char_count INT NOT NULL,
        seg_summary TEXT,
        text_hash VARCHAR(64) NOT NULL,
        path_json JSONB NOT NULL DEFAULT '{}',
        model VARCHAR(64),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (doc_key, extract_version, seg_index)
    )"""
    # hub 资产索引：**hub JSON 本体**的哈希 + 概括 + 特征哈希汇总（概括/哈希的对应表）
    ddl_hub = """
    CREATE TABLE IF NOT EXISTS hub_index (
        id SERIAL PRIMARY KEY,
        doc_key VARCHAR(300) NOT NULL,
        extract_version VARCHAR(32) NOT NULL DEFAULT 'v1',
        hub_file VARCHAR(400) NOT NULL,
        hub_hash VARCHAR(64) NOT NULL,
        content_hash VARCHAR(64) NOT NULL,
        file_hash VARCHAR(64),
        page_count INT NOT NULL DEFAULT 0,
        table_count INT NOT NULL DEFAULT 0,
        cell_count INT NOT NULL DEFAULT 0,
        table_anchor_count INT NOT NULL DEFAULT 0,
        offset_map_count INT NOT NULL DEFAULT 0,
        segment_count INT NOT NULL DEFAULT 0,
        fact_count INT NOT NULL DEFAULT 0,
        doc_summary TEXT,
        summary_model VARCHAR(64),
        feature_hashes JSONB NOT NULL DEFAULT '{}'::jsonb,
        feature_hash_bundle VARCHAR(64),
        source_path TEXT,
        sidecar_file VARCHAR(400),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (doc_key, extract_version)
    )"""
    # 老库补齐新列（幂等）
    alter_cols = [
        "ALTER TABLE l1_documents ADD COLUMN IF NOT EXISTS hub_hash VARCHAR(64)",
        "ALTER TABLE l1_documents ADD COLUMN IF NOT EXISTS table_count INT NOT NULL DEFAULT 0",
        "ALTER TABLE l1_documents ADD COLUMN IF NOT EXISTS segment_count INT NOT NULL DEFAULT 0",
        "ALTER TABLE l1_documents ADD COLUMN IF NOT EXISTS fact_count INT NOT NULL DEFAULT 0",
        "ALTER TABLE hub_index ADD COLUMN IF NOT EXISTS fact_count INT NOT NULL DEFAULT 0",
    ]
    with get_admin_connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            # 关键：DDL 只等 3 秒，拿不到锁就跳过（列/表本来就在，跳过没副作用）
            try:
                cur.execute("SET lock_timeout = '3s'")
            except Exception:
                pass
            try:
                cur.execute(ddl_docs)
                cur.execute(ddl_segs)
                cur.execute(ddl_hub)
                for stmt in alter_cols:
                    cur.execute(stmt)
                # 授权给应用角色（否则 finance_app_role 读不到这些新表，与 column_catalog 同理）
                from psycopg2 import sql as _sql

                from infra.database_serv__infra import APP_DB_CONFIG

                app_user = _sql.Identifier(APP_DB_CONFIG["user"])
                for tbl in ("l1_documents", "l1_segments", "hub_index"):
                    cur.execute(
                        _sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                            _sql.Identifier(tbl), app_user
                        )
                    )
                for seq in ("l1_documents_doc_no_seq", "hub_index_id_seq"):
                    cur.execute(
                        _sql.SQL("GRANT USAGE, SELECT ON SEQUENCE {} TO {}").format(
                            _sql.Identifier(seq), app_user
                        )
                    )
                _TABLES_ENSURED = True
            except Exception as exc:
                # 拿锁超时/权限不足都不影响主流程：表结构早已由 init_db 建好
                _graph_log({"action": "ensure_tables_skipped", "error": str(exc)})
                print(f"[L1 建表] 跳过（{type(exc).__name__}: {exc}）", flush=True)


def _graph_log(record: dict) -> None:
    """节点构建日志：logs/graph/graph_build_<date>.jsonl（追加式）。"""
    try:
        GRAPH_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = GRAPH_LOG_DIR / f"l1_summary_hash_build_{time.strftime('%Y%m%d')}.jsonl"
        record = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **record}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load_feature_hashes(doc_key: str) -> dict:
    """读取 ai_parser 已落库的特征哈希（file_feature_hashes）→ {feature_code: hash}。

    hub_index 里汇总它，是为了让"概括/哈希"在同一张表里可查：文档哈希在
    l1_documents/hub_index，特征哈希在 file_feature_hashes，两者用 doc_key 关联。

    ⚠️ 本轮修复：列名写错成 `feature_hash`（实际是 **`value_hash`**），
    SELECT 抛 UndefinedColumn 被 `except` 静默吞掉 → `hub_index.feature_hashes`
    **18 份文档全是 `{}`**、`feature_hash_bundle` 永远为 NULL——聚合层等于没接上特征。
    """
    try:
        from infra.database_serv__infra import get_connection

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT feature_code, value_hash FROM file_feature_hashes "
                        "WHERE doc_key = %s", (doc_key,))
            return {code: h for code, h in cur.fetchall()}
    except Exception:
        return {}


def _load_reusable_summaries(doc_key: str, hub_hash: str, seg_count: int) -> tuple[str, dict[int, str]] | None:
    """hub 内容没变时复用已存概括（省一次 AI）：返回 (doc_summary, {seg_index: summary})。

    条件：hub_index 里记录的 hub_hash 与当前一致，且段落概括条数对得上、都不是空。
    不满足就返回 None（走正常 AI 概括）。
    """
    try:
        from infra.database_serv__infra import get_connection

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT hub_hash, doc_summary, segment_count FROM hub_index "
                        "WHERE doc_key = %s AND extract_version = %s",
                        (doc_key, EXTRACT_VERSION))
            row = cur.fetchone()
            if not row or row[0] != hub_hash or not row[1] or int(row[2] or 0) != seg_count:
                return None
            cur.execute("SELECT seg_index, seg_summary FROM l1_segments "
                        "WHERE doc_key = %s AND extract_version = %s ORDER BY seg_index",
                        (doc_key, EXTRACT_VERSION))
            segs = {int(i): (s or "") for i, s in cur.fetchall()}
        if len(segs) != seg_count or any(not v for v in segs.values()):
            return None
        return row[1], segs
    except Exception:
        return None


def project_hub_file(
    hub_json_path: str | Path,
    *,
    source_path: str | None = None,
    model: str = "deepseek-flash",
    summarize: bool = True,
    force: bool = False,
) -> dict:
    """对一份 hub 扁平 JSON 做 L1 投影（哈希→分段→概括→入库→hub 资产索引→日志）。

    summarize=False：不调用 AI（AI 关闭/无 key 时），仍产出并记录**哈希与结构统计**
    （hub_hash / 内容哈希 / 段文本哈希），概括留空，保证哈希链路始终可用。
    force=False 且 hub 内容（hub_hash）与上次一致时：**复用已存概括**，不重复调 AI。

    返回 {"doc_no", "doc_key", "segments": [...], "doc_summary", "file_hash",
          "text_hash", "hub_hash", "sidecar"}。
    """
    from infra.database_serv__infra import get_admin_connection
    from desens.ai_guard__desens import require_hub_file   # AI 输入守卫：L1 的概括走 AI，只允许 hub/

    hub_json_path = require_hub_file(hub_json_path, purpose="L1 投影（含 AI 概括）")
    data = json.loads(hub_json_path.read_text(encoding="utf-8"))
    doc_key = doc_key_of(hub_json_path, data)
    # 只接受 hub 产出（旧库缺 kind 字段时，要求存在 pages 才继续），
    # 防止把 <stem>.features.json / 其它 JSON 误当文档投影。
    kind = data.get("kind")
    if kind is not None and kind != "hub_doc":
        raise ValueError(f"非 hub 产出 JSON（kind={kind}），已跳过")
    if "pages" not in data:
        raise ValueError("缺少 pages 字段，非 hub 产出 JSON，已跳过")
    pages: list[str] = list(data.get("pages") or [])
    full_text = "\n".join(pages)

    text_hash = hash_text(full_text)
    file_hash = hash_file(source_path) if source_path and os.path.exists(source_path) else text_hash
    fp = hub_fingerprint(hub_json_path)

    segs = segment_pages(pages)
    _ensure_tables()
    # ⚠️ 空字符串会把库里已有的 source_path 覆盖掉（`COALESCE` 只挡 NULL，不挡 ""）：
    #    实测有几次"只刷新概括/特征"的投影调用没传 source_path，导致 2 份文档的
    #    源路径被清空。这里统一把空串当 NULL 处理（COALESCE 才能保住旧值）。
    source_path = source_path or None

    # 文档行：doc_no 由序列分配；重跑同 doc_key+版本 → 复用同一 doc_no（稳定 id）
    with get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO l1_documents
               (doc_key, extract_version, file_hash, text_hash, hub_hash, category,
                page_count, table_count, segment_count, source_path, model)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (doc_key, extract_version) DO UPDATE
               SET file_hash = EXCLUDED.file_hash,
                   text_hash = EXCLUDED.text_hash,
                   hub_hash = EXCLUDED.hub_hash,
                   category = COALESCE(EXCLUDED.category, l1_documents.category),
                   page_count = EXCLUDED.page_count,
                   table_count = EXCLUDED.table_count,
                   segment_count = EXCLUDED.segment_count,
                   source_path = COALESCE(EXCLUDED.source_path, l1_documents.source_path),
                   model = EXCLUDED.model,
                   updated_at = CURRENT_TIMESTAMP
               RETURNING doc_no""",
            (doc_key, EXTRACT_VERSION, file_hash, text_hash, fp["hub_hash"],
             data.get("category"), len(pages), fp["table_count"], len(segs), source_path, model),
        )
        doc_no = cur.fetchone()[0]
        conn.commit()

    # 段落概括（逐段）+ 文档概括（由段落概括汇总）
    # 去重配套：hub 内容没变（hub_hash 一致）且已有概括 → 直接复用，不重复调 AI（省 token/时间）
    reused = (None if (force or not summarize)
              else _load_reusable_summaries(doc_key, fp["hub_hash"], len(segs)))
    seg_records: list[dict] = []
    seg_summaries: list[str] = []
    for i, seg in enumerate(segs, start=1):
        if reused and i in reused[1]:
            summary = reused[1][i]
        else:
            summary = summarize_segment(seg["text"]) if summarize else ""
        seg_id = f"{doc_no}-{i}"
        path_json = {
            "doc_key": doc_key,
            "page_from": seg["page_from"],
            "page_to": seg["page_to"],
            "char_start": seg["char_start"],
            "char_end": seg["char_end"],
            "hub_json": hub_json_path.name,
        }
        seg_records.append({
            "seg_id": seg_id,
            "seg_index": i,
            "page_from": seg["page_from"],
            "page_to": seg["page_to"],
            "char_count": len(seg["text"]),
            "seg_summary": summary,
            "text_hash": hash_text(seg["text"]),
            "path_json": path_json,
        })
        seg_summaries.append(summary)
        # 节点构建日志（段落）
        _graph_log({
            "action": "node", "kind": "segment", "doc_key": doc_key, "doc_no": doc_no,
            "seg_id": seg_id, "page_from": seg["page_from"], "page_to": seg["page_to"],
            "char_count": len(seg["text"]), "text_hash": seg_records[-1]["text_hash"],
            "model": model, "extract_version": EXTRACT_VERSION,
        })

    doc_summary = (reused[0] if reused
                   else (summarize_document(seg_summaries, fallback_text=full_text)
                         if summarize else ""))

    with get_admin_connection() as conn, conn.cursor() as cur:
        for rec in seg_records:
            cur.execute(
                """INSERT INTO l1_segments
                   (seg_id, doc_key, extract_version, seg_index, page_from, page_to,
                    char_count, seg_summary, text_hash, path_json, model)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (doc_key, extract_version, seg_index) DO UPDATE
                   SET seg_id = EXCLUDED.seg_id,
                       page_from = EXCLUDED.page_from,
                       page_to = EXCLUDED.page_to,
                       char_count = EXCLUDED.char_count,
                       seg_summary = EXCLUDED.seg_summary,
                       text_hash = EXCLUDED.text_hash,
                       path_json = EXCLUDED.path_json,
                       model = EXCLUDED.model""",
                (rec["seg_id"], doc_key, EXTRACT_VERSION, rec["seg_index"], rec["page_from"],
                 rec["page_to"], rec["char_count"], rec["seg_summary"], rec["text_hash"],
                 json.dumps(rec["path_json"], ensure_ascii=False), model),
            )
        cur.execute(
            "UPDATE l1_documents SET doc_summary=%s, updated_at=CURRENT_TIMESTAMP "
            "WHERE doc_key=%s AND extract_version=%s",
            (doc_summary, doc_key, EXTRACT_VERSION),
        )
        conn.commit()

    # 细粒度事实清单（L1 取数层）：表格单元格 + 文本键值行，每条带溯源路径。
    # 概括只作定位索引，真正的取数靠这些"事实"回原文核对（char/raw_char/行列/坐标）。
    facts: list[dict] = []
    try:
        import l1.l1_facts__fact_list as _facts

        facts = _facts.extract_facts({**data, "doc_key": doc_key}, doc_key)
        _facts.store_facts(doc_key, facts)
    except Exception as exc:
        _graph_log({"action": "facts_error", "doc_key": doc_key, "error": str(exc)})

    # hub 资产索引（概括/哈希的对应表）：hub 内容指纹 + 结构统计 + 概括 + 特征哈希汇总
    feature_hashes = _load_feature_hashes(doc_key)
    bundle = hashlib.sha256(
        json.dumps(feature_hashes, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest() if feature_hashes else None
    sidecar = build_summary_sidecar(hub_json_path, {
        "extract_version": EXTRACT_VERSION, "doc_no": doc_no, "model": model,
        "doc_key": doc_key,
        "doc_summary": doc_summary, "segments": seg_records,
        "hub_hash": fp["hub_hash"], "content_hash": fp["content_hash"],
        "file_hash": file_hash, "text_hash": text_hash,
        "page_count": len(pages), "table_count": fp["table_count"],
        "cell_count": fp["cell_count"], "fact_count": len(facts), "facts": facts,
    })
    with get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO hub_index
               (doc_key, extract_version, hub_file, hub_hash, content_hash, file_hash,
                page_count, table_count, cell_count, table_anchor_count, offset_map_count,
                segment_count, fact_count, doc_summary, summary_model, feature_hashes,
                feature_hash_bundle, source_path, sidecar_file)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (doc_key, extract_version) DO UPDATE
               SET hub_file = EXCLUDED.hub_file,
                   hub_hash = EXCLUDED.hub_hash,
                   content_hash = EXCLUDED.content_hash,
                   file_hash = EXCLUDED.file_hash,
                   page_count = EXCLUDED.page_count,
                   table_count = EXCLUDED.table_count,
                   cell_count = EXCLUDED.cell_count,
                   table_anchor_count = EXCLUDED.table_anchor_count,
                   offset_map_count = EXCLUDED.offset_map_count,
                   segment_count = EXCLUDED.segment_count,
                   fact_count = EXCLUDED.fact_count,
                   doc_summary = EXCLUDED.doc_summary,
                   summary_model = EXCLUDED.summary_model,
                   feature_hashes = EXCLUDED.feature_hashes,
                   feature_hash_bundle = EXCLUDED.feature_hash_bundle,
                   source_path = COALESCE(EXCLUDED.source_path, hub_index.source_path),
                   sidecar_file = EXCLUDED.sidecar_file,
                   updated_at = CURRENT_TIMESTAMP""",
            (doc_key, EXTRACT_VERSION, hub_json_path.name, fp["hub_hash"], fp["content_hash"],
             file_hash, len(pages), fp["table_count"], fp["cell_count"], fp["table_anchor_count"],
             fp["offset_map_count"], len(seg_records), len(facts), doc_summary, model,
             json.dumps(feature_hashes, ensure_ascii=False), bundle,
             source_path, sidecar.name),
        )
        conn.commit()

    _graph_log({
        "action": "node", "kind": "document", "doc_key": doc_key, "doc_no": doc_no,
        "page_count": len(pages), "segments": len(seg_records), "facts": len(facts),
        "file_hash": file_hash, "text_hash": text_hash, "hub_hash": fp["hub_hash"],
        "table_count": fp["table_count"], "cell_count": fp["cell_count"],
        "feature_hash_count": len(feature_hashes), "feature_hash_bundle": bundle,
        "sidecar": sidecar.name,
        "doc_summary_len": len(doc_summary), "model": model,
        "summary_reused": bool(reused),
        "extract_version": EXTRACT_VERSION,
    })

    return {
        "doc_no": doc_no, "doc_key": doc_key, "file_hash": file_hash,
        "text_hash": text_hash, "hub_hash": fp["hub_hash"], "content_hash": fp["content_hash"],
        "page_count": len(pages), "table_count": fp["table_count"], "cell_count": fp["cell_count"],
        "fact_count": len(facts),
        "summary_reused": bool(reused),
        "feature_hash_count": len(feature_hashes), "feature_hash_bundle": bundle,
        "sidecar": str(sidecar), "extract_version": EXTRACT_VERSION,
        "doc_summary": doc_summary, "segments": seg_records,
    }


def project_folder(folder: str | Path, *, pattern: str = "*.json") -> list[dict]:
    """遍历 hub 目录（**含子目录**）批量投影（只读；不动源目录结构）。

    hub 现在保留源文件夹结构（hub/<源文件夹子树>/<文件名>.json），故用 rglob 递归；
    源文件夹本身依旧只读、不改名、不移动。

    过滤规则（防串味）：
      · 只允许 hub/ 内的目录（AI 输入守卫：原始件/OCR 缓存/源目录一律拒绝）；
      · 跳过 ai_parser 的伴生产物 `*.features.json`（会与 hub JSON 同目录）；
      · 跳过概括/哈希伴生文件 `*.l1.json`（同上，非 hub 正文）；
      · 其余 JSON 交由 project_hub_file 校验 `kind == "hub_doc"`（或含 pages）。
    """
    from desens.ai_guard__desens import require_hub_dir

    folder = require_hub_dir(folder, purpose="L1 批量投影")
    results: list[dict] = []
    for path in sorted(folder.rglob(pattern)):
        if path.name.endswith((".features.json", ".l1.json")):
            _graph_log({"action": "skip", "reason": "伴生文件（非 hub 正文）", "path": path.name})
            continue
        try:
            results.append(project_hub_file(path))
        except Exception as exc:
            _graph_log({"action": "error", "doc_key": path.stem, "error": str(exc)})
    return results
