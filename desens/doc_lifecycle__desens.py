"""文档生命周期：重复副本**替换**（新替旧 + 全链路重跑）与**删除**（连根拔起 + 编号退役）。

## 一、副本判定（按需求）
入库时若文件名带 Windows 自动副本标记（`（2）`/`(2)`/`- 副本`/` 副本`…）：
  1. **先比标记之前的部分**：去掉标记后的基名必须与库里已有文档**完全一致**；
  2. **再比内容首尾**：读取文件**头部 + 尾部**（默认各 4KB）算指纹，必须一致；
  3. 两条都成立 → 认定"同一份文件的另一个副本" → **只保留一份**：
     · 用**新文件**替代旧文件（旧文档全部痕迹删除，旧 doc_no 退役不再复用）；
     · 新文件**所有工作重新跑一遍**（OCR/直读 → 脱敏 → hub → 概括/哈希 → 事实清单 → 图节点/边 → 向量），
       因此调用方要 `force=True` 绕过"内容未变则跳过"的去重闸门。

为什么是"首尾"而不是整文件哈希：整文件哈希对"中间被改过的新版本"会判为不同文件而并存，
而需求是"同一份文件的新副本要替换旧的并重跑"。首尾一致即视为同一份（中间若有改动，
重跑会把新内容全部重新投影，比并存两份正确）。

## 二、删除（按需求）
`delete_document(doc_key, ...)` 把所有"指向它"的痕迹一并清除：
  · 图：`graph_edges`（两端任一涉及它的节点，含其它文档指向它的边）→ `graph_nodes`（它自己的
    doc/table/row 节点；不再被任何事实引用的 entity 节点也一并清）
  · 事实/段落/文档/hub 资产索引：`l1_facts` / `l1_segments` / `l1_documents` / `hub_index`
  · 向量：`document_chunks`（RAG 分块与向量）
  · 内容指纹索引：`doc_content_index`
  · 落盘产物：`hub/<doc_key>.json` + `<stem>.l1.json` / `.features.json` / `.ai.log`、
    `output/<stem>__<内容指纹>/` 逐页缓存
  · 写**退役墓碑** `deleted_documents`：记录 doc_no / doc_key / 哈希 / 操作人 / 原因 / 被谁替换。
    **该 doc_no 永久留空、后面新文件不会填充它**（SERIAL 只增不减 + 墓碑可审计）。

源文件本身默认**不动**（"不破坏源文件夹"原则）；只有显式传 `delete_source=True` 才会删源文件。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

__all__ = [
    "COPY_MARKER_RE",
    "split_copy_marker",
    "head_tail_key",
    "find_base_documents",
    "plan_ingest",
    "delete_document",
    "list_deleted",
    "list_repository",
    "format_repository",
    "format_preview",
    "ensure_tables",
    "IngestPlan",
    "DeleteReport",
    "RepoItem",
    "FormatReport",
]

BASE_DIR = Path(__file__).resolve().parents[1]
# hub / output 都随工作区（仓库）切换：见 workspace__infra
from infra.workspace__infra import hub_root as _hub_root
from infra.workspace__infra import output_root as _output_root

HUB_DIR = _hub_root()
OUTPUT_DIR = _output_root()
HEAD_TAIL_BYTES = 4096

# Windows 复制文件时的自动命名："xxx（2）.docx" / "xxx (2).docx" / "xxx- 副本.docx" / "xxx 副本(2)"
COPY_MARKER_RE = re.compile(
    r"(?:[（(]\s*\d{1,3}\s*[)）]|[-_\s]*(?:副本|拷贝|复件|copyright)(?:[（(]\s*\d{1,3}\s*[)）])?)$"
)


def split_copy_marker(name: str) -> tuple[str, str]:
    """拆出 (基名, 标记)；没有副本标记时标记为空串。

    支持：`xxx（2）`、`xxx(2)`、`xxx- 副本`、`xxx 副本(2)`、`xxx（副本）`。
    只剥**末尾**标记，不动中间括号（如"合同（2026年）"这种正常括号不会被误剥）。
    """
    stem = Path(name).stem
    m = COPY_MARKER_RE.search(stem)
    if not m:
        return stem, ""
    base = stem[: m.start()].rstrip(" -_")
    if not base:                      # 全是标记 → 当没有标记
        return stem, ""
    return base, m.group(0)


def head_tail_key(path: str | Path, *, size: int = HEAD_TAIL_BYTES) -> str:
    """内容"首尾指纹"：文件长度 + 头部 size 字节 + 尾部 size 字节 的 sha256。"""
    p = Path(path)
    total = p.stat().st_size
    with p.open("rb") as f:
        head = f.read(size)
        if total > size:
            f.seek(max(0, total - size))
            tail = f.read(size)
        else:
            tail = b""
    h = hashlib.sha256()
    h.update(str(total).encode("ascii"))
    h.update(head)
    h.update(tail)
    return h.hexdigest()


def ensure_tables() -> None:
    """退役墓碑表（幂等）。"""
    from psycopg2 import sql as _sql

    from infra.database_serv__infra import APP_DB_CONFIG, get_admin_connection

    ddl = """
    CREATE TABLE IF NOT EXISTS deleted_documents (
        id SERIAL PRIMARY KEY,
        doc_no INT,                                  -- 退役的文档编号（永久留空，不复用）
        doc_key VARCHAR(300) NOT NULL,
        file_hash VARCHAR(64),
        hub_hash VARCHAR(64),
        content_hash VARCHAR(64),
        replaced_by VARCHAR(300),                    -- 被哪个新文档替代（副本替换时）
        reason TEXT,
        deleted_by VARCHAR(64),
        deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        removed JSONB NOT NULL DEFAULT '{}'::jsonb   -- 各表/文件的删除计数，便于审计
    )"""
    with get_admin_connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(ddl)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_deleted_doc_no ON deleted_documents (doc_no)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_deleted_doc_key "
                        "ON deleted_documents (doc_key)")
            cur.execute(
                _sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                    _sql.Identifier("deleted_documents"), _sql.Identifier(APP_DB_CONFIG["user"])
                )
            )
            cur.execute(
                _sql.SQL("GRANT USAGE, SELECT ON SEQUENCE {} TO {}").format(
                    _sql.Identifier("deleted_documents_id_seq"), _sql.Identifier(APP_DB_CONFIG["user"])
                )
            )


# =========================================================
# 副本判定
# =========================================================
@dataclass
class IngestPlan:
    action: str = "new"            # new | replace
    base_name: str = ""
    marker: str = ""
    head_tail: str = ""
    replace_doc_key: str | None = None
    replace_doc_no: int | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _existing_docs_by_base(base: str) -> list[dict]:
    """库里 doc_key 的"末段基名"（去掉副本标记、去掉扩展名式后缀）等于 base 的文档。"""
    from infra.database_serv__infra import get_connection

    out: list[dict] = []
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT doc_key, doc_no, hub_hash, file_hash FROM l1_documents")
        for doc_key, doc_no, hub_hash, file_hash in cur.fetchall():
            leaf = str(doc_key).rsplit("/", 1)[-1]
            if split_copy_marker(leaf)[0] == base:
                out.append({"doc_key": doc_key, "doc_no": doc_no,
                            "hub_hash": hub_hash, "file_hash": file_hash})
    return out


def find_base_documents(path: str | Path) -> list[dict]:
    """按"标记之前基名完全一致"找出库里同名的既有文档（不做内容判断）。"""
    base, marker = split_copy_marker(Path(path).name)
    docs = _existing_docs_by_base(base)
    for d in docs:
        d["marker"] = marker
        d["base_name"] = base
    return docs


def plan_ingest(path: str | Path) -> IngestPlan:
    """入库前的副本判定：是否需要"新替旧"。

    规则：文件名带副本标记 **且** 去掉标记后基名与库里文档**完全一致** **且**
    内容首尾指纹一致 → replace（替换 + 全链路重跑）；否则 new（正常入库）。
    """
    p = Path(path)
    base, marker = split_copy_marker(p.name)
    plan = IngestPlan(base_name=base, marker=marker)
    if not marker:
        plan.reason = "文件名无副本标记 → 按新文件入库"
        return plan
    docs = _existing_docs_by_base(base)
    if not docs:
        plan.reason = f"带副本标记 {marker!r}，但库里没有基名 {base!r} 的文档 → 按新文件入库"
        return plan
    key = None
    try:
        key = head_tail_key(p)
        plan.head_tail = key
    except Exception as exc:
        plan.reason = f"读取首尾指纹失败（{type(exc).__name__}: {exc}）→ 按新文件入库"
        return plan
    # 与每个同名文档比首尾：必须**两条都成立**才替换。
    # 旧文档的源文件若已不在（被移走/删除），无法做首尾比对 → 只靠名字不敢删，
    # 按新文件入库（宁可多留一份，也不能凭文件名误删）。
    for d in docs:
        src = _doc_source_path(d["doc_key"])
        if not src or not Path(src).exists():
            plan.reason = (f"基名与 {d['doc_key']} 一致，但旧文档的源文件已不在，"
                           f"无法比对内容首尾 → 按新文件入库（两份并存）")
            continue
        try:
            if head_tail_key(src) != key:
                plan.reason = (f"基名与 {d['doc_key']} 一致，但内容首尾指纹不同 → "
                               f"按新文件入库（两份并存）")
                continue
        except Exception:
            continue
        plan.action = "replace"
        plan.replace_doc_key = d["doc_key"]
        plan.replace_doc_no = d["doc_no"]
        plan.reason = (f"副本 {marker!r}：基名与 {d['doc_key']}（doc_no={d['doc_no']}）完全一致，"
                       f"内容首尾指纹一致 → 用新文件替代旧文件并全链路重跑")
        return plan
    if not plan.reason:
        plan.reason = f"带副本标记 {marker!r}，但未找到满足条件的同名文档 → 按新文件入库"
    return plan


def _doc_source_path(doc_key: str) -> str | None:
    from infra.database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT source_path FROM l1_documents WHERE doc_key = %s", (doc_key,))
        row = cur.fetchone()
    return row[0] if row else None


# =========================================================
# 删除（连根拔起）
# =========================================================
@dataclass
class DeleteReport:
    doc_key: str
    doc_no: int | None = None
    removed: dict = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    folded: list[str] = field(default_factory=list)   # 退役的编号（永久留空）
    replaced_by: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        parts = [f"{k}={v}" for k, v in self.removed.items() if v]
        return (f"删除 {self.doc_key}（doc_no={self.doc_no} 已退役留空）｜"
                + ("、".join(parts) if parts else "无库内记录")
                + (f"｜落盘文件 {len(self.files)} 个" if self.files else "")
                + (f"｜被 {self.replaced_by} 替代" if self.replaced_by else ""))


def _node_patterns(doc_key: str) -> list[str]:
    return [f"doc:{doc_key}", f"table:{doc_key}#%", f"row:{doc_key}#%"]


def _pair_node_doc(node_id: str) -> str | None:
    """节点编号 → 所属文档（用于精确清理边判定缓存，不用 LIKE）。"""
    s = str(node_id or "")
    if s.startswith("doc:"):
        return s[4:]
    if s.startswith(("table:", "row:")):
        return s.split(":", 1)[1].split("#", 1)[0]
    return None


def _drop_files(doc_key: str, file_hash: str | None) -> list[str]:
    """删除该文档的落盘产物（hub JSON + 伴生文件 + 逐页缓存目录）。"""
    import shutil

    removed: list[str] = []
    hub_json = HUB_DIR / f"{doc_key}.json"
    stem = Path(doc_key).name
    for p in [hub_json, hub_json.parent / f"{stem}.l1.json",
              hub_json.parent / f"{stem}.features.json",
              hub_json.parent / f"{stem}.ai.log"]:
        if p.exists():
            p.unlink()
            removed.append(str(p.relative_to(BASE_DIR)))
    if file_hash and OUTPUT_DIR.exists():
        for d in OUTPUT_DIR.glob(f"{stem}__{file_hash[:10]}"):
            shutil.rmtree(d, ignore_errors=True)
            removed.append(str(d.relative_to(BASE_DIR)))
    return removed


def delete_document(
    doc_key: str,
    *,
    user: dict | None = None,
    reason: str | None = None,
    replaced_by: str | None = None,
    delete_source: bool = False,
) -> DeleteReport:
    """删除一份文档的全部痕迹；其 doc_no 退役（永久留空，新文件不复用该编号）。"""
    ensure_tables()
    from infra.database_serv__infra import get_admin_connection, get_connection

    report = DeleteReport(doc_key=doc_key, replaced_by=replaced_by)
    counts = {k: 0 for k in ("l1_facts", "l1_segments", "l1_documents", "hub_index",
                             "graph_edges", "graph_nodes", "document_chunks",
                             "doc_content_index", "entity_nodes")}
    removed = dict(counts)

    # 先取退役信息
    src_path, file_hash, hub_hash, content_hash = None, None, None, None
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT doc_no, source_path, file_hash, hub_hash FROM l1_documents "
                    "WHERE doc_key = %s", (doc_key,))
        row = cur.fetchone()
        if row:
            report.doc_no, src_path, file_hash, hub_hash = row[0], row[1], row[2], row[3]
        cur.execute("SELECT content_hash FROM doc_content_index WHERE canonical_doc_key = %s",
                    (doc_key,))
        ch = cur.fetchone()
        content_hash = ch[0] if ch else None

    if report.doc_no is None and content_hash is None:
        # 库里没有记录：仍然尝试清理落盘产物与图上残留
        pass

    patterns = _node_patterns(doc_key)
    with get_admin_connection() as conn, conn.cursor() as cur:
        # 1) 图边：两端任一涉及它（含**其它文档指向它**的边）
        cur.execute(
            """DELETE FROM graph_edges
                WHERE src_node = %s OR dst_node = %s
                   OR src_node LIKE %s OR src_node LIKE %s
                   OR dst_node LIKE %s OR dst_node LIKE %s""",
            (f"doc:{doc_key}", f"doc:{doc_key}", patterns[1], patterns[2],
             patterns[1], patterns[2]))
        removed["graph_edges"] = cur.rowcount
        # 2) 图节点：它自己的 doc/table/row 节点
        cur.execute(
            "DELETE FROM graph_nodes WHERE node_id = %s OR node_id LIKE %s OR node_id LIKE %s",
            (f"doc:{doc_key}", patterns[1], patterns[2]))
        removed["graph_nodes"] = cur.rowcount
        # 3) 不再被任何事实引用的实体节点（"遍历所有节点，删除指向它的痕迹"）
        cur.execute(
            """DELETE FROM graph_nodes
                WHERE node_type = 'entity'
                  AND NOT EXISTS (SELECT 1 FROM l1_facts f
                                  WHERE f.value LIKE '%%' || graph_nodes.label || '%%')""")
        removed["entity_nodes"] = cur.rowcount
        # 4) 事实 / 段落 / 文档 / hub 资产索引
        for tbl in ("l1_facts", "l1_segments", "l1_documents", "hub_index"):
            cur.execute(f"DELETE FROM {tbl} WHERE doc_key = %s", (doc_key,))
            removed[tbl] = cur.rowcount
        # 5) 向量（RAG 分块）
        try:
            cur.execute("DELETE FROM document_chunks WHERE doc_key = %s", (doc_key,))
            removed["document_chunks"] = cur.rowcount
        except Exception:
            conn.rollback()
            removed["document_chunks"] = 0
        # 6) 内容指纹索引（同内容的副本记录也一并摘掉）
        try:
            cur.execute("""DELETE FROM doc_content_index
                            WHERE canonical_doc_key = %s OR duplicate_doc_keys ? %s""",
                        (doc_key, doc_key))
            removed["doc_content_index"] = cur.rowcount
        except Exception:
            conn.rollback()
            try:
                cur.execute("DELETE FROM doc_content_index WHERE canonical_doc_key = %s",
                            (doc_key,))
                removed["doc_content_index"] = cur.rowcount
            except Exception:
                conn.rollback()
        # 6.5) 建图状态与边判定缓存：删掉后若该文档日后再入库，会重新建图（不是残留旧判定）
        # 说明：pair_key 形如 "<节点A>\x1f<节点B>"；这里按"节点归属文档"精确匹配，
        # 不用 LIKE——doc_key 里常带 `_`（如 `148588.72_无锡…`），LIKE 的 `_` 是通配符会误删别人的缓存。
        try:
            cur.execute("DELETE FROM graph_build_state WHERE doc_key = %s", (doc_key,))
            removed["graph_build_state"] = cur.rowcount
        except Exception:
            conn.rollback()
        try:
            cur.execute("SELECT pair_key FROM graph_pair_cache")
            victims = []
            for (pk,) in cur.fetchall():
                a, _, b = str(pk).partition("\u001f")
                if _pair_node_doc(a) == doc_key or _pair_node_doc(b) == doc_key:
                    victims.append(pk)
            if victims:
                cur.execute("DELETE FROM graph_pair_cache WHERE pair_key = ANY(%s)", (victims,))
            removed["graph_pair_cache"] = len(victims)
        except Exception:
            conn.rollback()
        # 7) 退役墓碑：编号留空、可审计
        cur.execute(
            """INSERT INTO deleted_documents
               (doc_no, doc_key, file_hash, hub_hash, content_hash, replaced_by, reason, deleted_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (report.doc_no, doc_key, file_hash, hub_hash, content_hash, replaced_by,
             reason or ("被新副本替代" if replaced_by else "用户删除"),
             (user or {}).get("username")))
        conn.commit()

    report.removed = {k: v for k, v in removed.items() if v}
    report.files = _drop_files(doc_key, file_hash)
    if delete_source and src_path and Path(src_path).exists():
        try:
            Path(src_path).unlink()
            report.files.append(f"源文件:{src_path}")
        except Exception:
            pass
    return report


def list_deleted(limit: int = 50) -> list[dict]:
    ensure_tables()
    from infra.database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("""SELECT doc_no, doc_key, replaced_by, reason, deleted_by, deleted_at
                       FROM deleted_documents ORDER BY id DESC LIMIT %s""", (limit,))
        return [{"doc_no": r[0], "doc_key": r[1], "replaced_by": r[2], "reason": r[3],
                 "deleted_by": r[4],
                 "deleted_at": r[5].isoformat(sep=" ", timespec="seconds") if r[5] else None}
                for r in cur.fetchall()]


def retired_doc_nos() -> set[int]:
    """已被退役（永久留空）的 doc_no 集合。"""
    ensure_tables()
    from infra.database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT doc_no FROM deleted_documents WHERE doc_no IS NOT NULL")
        return {r[0] for r in cur.fetchall()}


# =========================================================
# 仓库状态（只列"用户拖进来的源文件"，不列生成的 JSON / 概括）
# =========================================================
@dataclass
class RepoItem:
    doc_no: int | None = None
    doc_key: str = ""
    name: str = ""                     # 源文件名（用户拖进来的那个文件）
    source_path: str = ""
    source_exists: bool = False
    category: str = ""
    hub_file: str = ""
    hub_exists: bool = False
    page_count: int = 0
    table_count: int = 0
    fact_count: int = 0
    segment_count: int = 0
    node_count: int = 0
    edge_count: int = 0
    chunk_count: int = 0
    summary: str = ""                  # 概括只作定位索引，取数仍回原文
    file_hash: str = ""
    hub_hash: str = ""
    content_hash: str = ""
    duplicate: bool = False            # 内容重复（非 canonical）
    created_at: str = ""
    updated_at: str = ""
    state: str = "已入库"              # 已入库 | 仅落盘产物（库里无记录）

    def to_dict(self) -> dict:
        return asdict(self)


def _hub_files_on_disk() -> dict[str, str]:
    """hub 下所有"文档 JSON"（排除 _mapping 与伴生文件）→ {doc_key: 相对路径}。"""
    found: dict[str, str] = {}
    if not HUB_DIR.exists():
        return found
    for p in HUB_DIR.rglob("*.json"):
        rel = p.relative_to(HUB_DIR)
        if rel.parts and rel.parts[0] == "_mapping":
            continue
        if p.name.endswith((".l1.json", ".features.json")):
            continue
        key = rel.as_posix()[: -len(".json")]
        found[key] = str(p.relative_to(BASE_DIR))
    return found


def _edge_owner_docs() -> dict[str, int]:
    """按节点编号把图边归属到文档：一张边可能连接两份文档，两边各计一次。"""
    from infra.database_serv__infra import get_connection

    counts: dict[str, int] = {}
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT src_node, dst_node FROM graph_edges")
        rows = cur.fetchall()
    for src, dst in rows:
        owners: set[str] = set()
        for node in (src, dst):
            s = str(node or "")
            for pref in ("doc:", "table:", "row:"):
                if s.startswith(pref):
                    body = s[len(pref):]
                    owners.add(body.split("#", 1)[0])
        for k in owners:
            counts[k] = counts.get(k, 0) + 1
    return counts


def _counts_by_doc(table: str, column: str = "doc_key") -> dict[str, int]:
    from infra.database_serv__infra import get_connection

    out: dict[str, int] = {}
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {column}, count(*) FROM {table} GROUP BY 1")
            for key, n in cur.fetchall():
                if key:
                    out[str(key)] = int(n)
    except Exception:
        return {}
    return out


def _leaf_lookup(counts: dict[str, int], doc_key: str) -> int:
    """先按完整 doc_key 匹配；匹配不到再按末段基名兜底（历史数据用过扁平 key）。"""
    if doc_key in counts:
        return counts[doc_key]
    leaf = doc_key.rsplit("/", 1)[-1]
    hit = [v for k, v in counts.items() if k.rsplit("/", 1)[-1] == leaf]
    return sum(hit) if hit else 0


def list_repository() -> dict:
    """仓库状态：**只列用户拖进来的源文件**（生成的 JSON/概括/向量不单独成行）。

    返回 `{"items": [...], "summary": {...}}`；items 每行的 `state`：
      · 已入库             → 库里有记录（source_exists 表示源文件还在不在原位）
      · 仅落盘产物         → hub 里有 JSON 但库里没记录（残留产物，可格式化清掉）
    """
    ensure_tables()
    from infra.database_serv__infra import get_connection

    nodes = _counts_by_doc("graph_nodes")
    edges = _edge_owner_docs()
    chunks = _counts_by_doc("document_chunks")
    segs = _counts_by_doc("l1_segments")
    facts = _counts_by_doc("l1_facts")

    items: list[RepoItem] = []
    seen: set[str] = set()
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT d.doc_no, d.doc_key, d.source_path, d.file_hash, d.hub_hash, d.category,
                      d.doc_summary, d.page_count, d.table_count, d.created_at, d.updated_at,
                      h.hub_file, h.content_hash
                 FROM l1_documents d
                 LEFT JOIN hub_index h ON h.doc_key = d.doc_key
                ORDER BY d.doc_no""")
        rows = cur.fetchall()
        dup: set[str] = set()
        try:
            cur.execute("SELECT canonical_doc_key, duplicate_doc_keys FROM doc_content_index")
            for canon, dups in cur.fetchall():
                for k in (dups or []):
                    dup.add(str(k))
                dup.discard(str(canon))
        except Exception:
            pass

    for (doc_no, doc_key, src, fhash, hhash, cat, summ, pages, tables,
         c_at, u_at, hub_file, chash) in rows:
        doc_key = str(doc_key)
        seen.add(doc_key)
        items.append(RepoItem(
            doc_no=doc_no, doc_key=doc_key,
            name=_source_name(src, doc_key), source_path=src or "",
            source_exists=bool(src) and Path(src).exists(),
            category=cat or "", hub_file=hub_file or "",
            hub_exists=(HUB_DIR / f"{doc_key}.json").exists(),
            page_count=pages or 0, table_count=tables or 0,
            fact_count=_leaf_lookup(facts, doc_key),
            segment_count=_leaf_lookup(segs, doc_key),
            node_count=_leaf_lookup(nodes, doc_key),
            edge_count=_leaf_lookup(edges, doc_key),
            chunk_count=_leaf_lookup(chunks, doc_key),
            summary=(summ or "").strip(), file_hash=fhash or "", hub_hash=hhash or "",
            content_hash=chash or "", duplicate=str(doc_key) in dup,
            created_at=_fmt_ts(c_at), updated_at=_fmt_ts(u_at),
            state="已入库"))

    for doc_key, rel in sorted(_hub_files_on_disk().items()):
        if doc_key in seen:
            continue
        items.append(RepoItem(doc_key=doc_key, name=Path(doc_key).name,
                              hub_file=rel, hub_exists=True,
                              state="仅落盘产物（库里无记录）"))

    summary = {
        "docs": sum(1 for i in items if i.state == "已入库"),
        "orphan": sum(1 for i in items if i.state != "已入库"),
        "source_missing": sum(1 for i in items if i.state == "已入库" and not i.source_exists),
        "hub_missing": sum(1 for i in items if i.state == "已入库" and not i.hub_exists),
        "duplicate": sum(1 for i in items if i.duplicate),
        "facts": sum(i.fact_count for i in items),
        "chunks": sum(i.chunk_count for i in items),
        "nodes": sum(i.node_count for i in items),
        "edges": sum(i.edge_count for i in items),
        "hub_files": len(_hub_files_on_disk()),
        "cache_dirs": _output_cache_dir_count(),
        "deleted": len(list_deleted(limit=10000)),
    }
    return {"items": [i.to_dict() for i in items], "summary": summary}


def _source_name(src: str | None, doc_key: str) -> str:
    if src:
        return Path(src).name
    return Path(doc_key).name


def _fmt_ts(ts) -> str:
    try:
        return ts.isoformat(sep=" ", timespec="seconds")
    except Exception:
        return str(ts or "")


def _output_cache_dir_count() -> int:
    if not OUTPUT_DIR.exists():
        return 0
    return sum(1 for d in OUTPUT_DIR.iterdir() if d.is_dir())


# =========================================================
# 文件格式化（清空派生数据；登记/映射默认保留）
# =========================================================
# 派生表：由文档产生的数据（格式化时清空）
DERIVED_TABLES = (
    "graph_edges", "graph_nodes", "graph_build_state", "graph_pair_cache",
    "l1_facts", "l1_segments", "l1_documents", "hub_index",
    "document_chunks", "doc_content_index",
    "file_feature_hashes", "project_archive", "project_proposals",
    "contract_projects", "invoice_ledger",
    "ledger_inbox", "deleted_documents",
)
# 登记表：用户配置/脱敏映射（默认保留）
KEPT_TABLES = (
    "entity_mapping_company", "entity_mapping_party", "entity_mapping_date",
    "entity_mapping_id_card", "entity_mapping_bank_card", "entity_mapping_tax_id",
    "entity_mapping_bank_name", "entity_mapping_bank_account",
    "entity_mapping_project", "entity_mapping_phone",
    "self_entity", "project_registry", "project_categories",
    "sys_users", "sys_permissions", "sys_role_permissions",
    "column_catalog", "feature_catalog",
)
MAPPING_TABLES = tuple(t for t in KEPT_TABLES if t.startswith("entity_mapping_"))


@dataclass
class FormatReport:
    tables: dict = field(default_factory=dict)
    hub_files: int = 0
    hub_sidecars: int = 0
    hub_logs: int = 0
    hub_mapping: int = 0
    cache_dirs: int = 0
    bytes_freed: int = 0
    kept: dict = field(default_factory=dict)
    mappings_purged: bool = False
    dry_run: bool = False
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        head = "【预演】将清空" if self.dry_run else "已格式化，清空"
        parts = [f"{k}={v}" for k, v in self.tables.items() if v]
        mb = self.bytes_freed / 1024 / 1024
        return (f"{head}：库内 {'、'.join(parts) if parts else '无记录'}；"
                f"hub JSON {self.hub_files} / 伴生 {self.hub_sidecars} / 日志 {self.hub_logs}；"
                f"逐页缓存 {self.cache_dirs} 个；释放 {mb:.1f} MB"
                + ("；脱敏映射一并清空" if self.mappings_purged else "；脱敏映射/登记已保留")
                + (f"；错误 {len(self.errors)} 条" if self.errors else ""))


def _walk_hub_artifacts() -> tuple[list[Path], list[Path], list[Path]]:
    """返回 (文档 JSON, 伴生文件 .l1.json/.features.json, 其它遗留)；`_mapping` 一律不动。"""
    docs: list[Path] = []
    sidecars: list[Path] = []
    others: list[Path] = []
    if not HUB_DIR.exists():
        return docs, sidecars, others
    for p in HUB_DIR.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(HUB_DIR)
        if rel.parts and rel.parts[0] == "_mapping":
            continue
        if p.name.endswith((".l1.json", ".features.json")):
            sidecars.append(p)
        elif p.name.endswith(".json"):
            docs.append(p)
        else:
            others.append(p)
    return docs, sidecars, others


def format_preview(*, include_mappings: bool = False) -> FormatReport:
    """格式化前的预演：只统计、不动任何东西。"""
    return _format_scan(dry_run=True, include_mappings=include_mappings)


def _format_scan(*, dry_run: bool, include_mappings: bool = False,
                 user: dict | None = None) -> FormatReport:
    import shutil

    from infra.database_serv__infra import get_admin_connection, get_connection

    rep = FormatReport(dry_run=dry_run, mappings_purged=include_mappings)
    docs, sidecars, others = _walk_hub_artifacts()
    rep.hub_files = len(docs)
    rep.hub_sidecars = len(sidecars)
    rep.hub_logs = len(others)
    map_dir = HUB_DIR / "_mapping"
    map_files = [p for p in map_dir.rglob("*") if p.is_file()] if map_dir.exists() else []
    rep.hub_mapping = len(map_files)
    rep.bytes_freed = sum(p.stat().st_size
                          for p in docs + sidecars + others if p.exists())

    cache_dirs = [d for d in OUTPUT_DIR.iterdir() if d.is_dir()] if OUTPUT_DIR.exists() else []
    rep.cache_dirs = len(cache_dirs)
    for d in cache_dirs:
        for p in d.rglob("*"):
            if p.is_file():
                try:
                    rep.bytes_freed += p.stat().st_size
                except Exception:
                    pass

    tables = list(DERIVED_TABLES) + (list(MAPPING_TABLES) if include_mappings else [])
    # 现有行数
    for t in tables + list(KEPT_TABLES):
        try:
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM {t}")
                n = int(cur.fetchone()[0])
        except Exception:
            n = 0
        if t in tables:
            rep.tables[t] = n
        else:
            rep.kept[t] = n
    if dry_run:
        return rep

    # ---- 真清 ----
    with get_admin_connection() as conn, conn.cursor() as cur:
        for t in tables:
            try:
                cur.execute(f"DELETE FROM {t}")
                rep.tables[t] = cur.rowcount
            except Exception as exc:
                conn.rollback()
                rep.errors.append(f"{t}: {type(exc).__name__}: {exc}")
        conn.commit()

    for p in docs + sidecars + others:
        try:
            p.unlink()
        except Exception as exc:
            rep.errors.append(f"{p}: {type(exc).__name__}: {exc}")
    if include_mappings:
        if map_files:
            for p in sorted(map_files):
                try:
                    rep.bytes_freed += p.stat().st_size
                    p.unlink()
                except Exception as exc:
                    rep.errors.append(f"{p}: {type(exc).__name__}: {exc}")
    for d in cache_dirs:
        shutil.rmtree(d, ignore_errors=True)
    # 清空后 hub 下只剩 _mapping：删掉空目录（_mapping 保留）
    if HUB_DIR.exists():
        for p in sorted(HUB_DIR.rglob("*"), key=lambda x: len(x.parts), reverse=True):
            if p.is_dir() and p.name != "_mapping":
                try:
                    p.rmdir()
                except OSError:
                    pass

    _log_format(rep, user)
    return rep


def format_repository(*, user: dict | None = None, include_mappings: bool = False) -> FormatReport:
    """文件格式化：清空 hub 产物、output 逐页缓存、向量与所有派生表格数据。

    · 清空：`hub/**`（除 `_mapping`）、`output/**`、`document_chunks`（向量）、
      `l1_*` / `hub_index` / `l1_facts` / `graph_nodes|edges` / `doc_content_index` /
      `file_feature_hashes` / `project_archive` / `project_proposals` / `contract_projects` /
      `deleted_documents`（台账与表格数据）。
    · 保留：**源文件不动**（"不破坏源文件夹"），脱敏映射、本公司主体、项目登记、
      权限与列/特征字典。
    · `include_mappings=True` 时才连脱敏映射一起清（编号会从 1 重新开始）。
    """
    return _format_scan(dry_run=False, include_mappings=include_mappings, user=user)


def _log_format(rep: FormatReport, user: dict | None) -> None:
    import datetime as _dt

    try:
        d = BASE_DIR / "logs" / "format"
        d.mkdir(parents=True, exist_ok=True)
        day = _dt.date.today().isoformat()
        rec = {"ts": _dt.datetime.now().isoformat(timespec="seconds"),
               "user": (user or {}).get("username"),
               "tables": rep.tables, "kept": rep.kept,
               "hub_files": rep.hub_files, "hub_sidecars": rep.hub_sidecars,
               "hub_logs": rep.hub_logs,
               "cache_dirs": rep.cache_dirs, "bytes_freed": rep.bytes_freed,
               "mappings_purged": rep.mappings_purged, "errors": rep.errors}
        with (d / f"format_{day}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
