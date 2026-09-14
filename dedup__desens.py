"""内容级去重：防止同一个文件被反复扫描、污染节点（图/事实/摘要）。

检查结论（现状）：
  · **已有**：会话内**路径级**去重（`table__ui.py` 的 `_records`，同一个 resolved path 不重复入队）；
            各层**主键级**幂等（`l1_documents` / `l1_segments` / `l1_facts` / `graph_nodes` /
            `graph_edges` 都是 UNIQUE + UPSERT，同一 doc_key 重跑不会翻倍）。
  · **缺失**：**内容级**去重 —— 同一份文件换个路径（复制件、改名、放进另一个文件夹）会被
            再扫一遍：多一份 hub、多一套 facts/nodes/edges（**节点污染**），还白烧一次 OCR/概括。
  · **同时修掉的隐患**：`output/<文件名>.json` 缓存目录原来只按文件名 stem 命名，
            不同文件夹里的**同名文件会共用同一缓存目录并互相覆盖 page_*.json**
            （现改为 `output/<stem>__<内容指纹前10位>/`）。

本模块提供：
  · `content_key(path)`：源文件字节的流式 sha256（内容指纹，判等用）；
  · `decide(hash, doc_key, hub_file, force)`：new / unchanged_skip / duplicate_skip / forced；
  · `record_scan(...)`：把"内容指纹 → 首个 doc_key + 所有出现路径 + 跳过次数"记进
    `doc_content_index`（幂等、可审计）；
  · `list_entries()`：人工/日志查看"哪些内容重复、被跳过了几次"。

策略（默认，可用 force / 环境变量覆盖）：
  · 同内容 + 同路径（或同一个 doc_key）+ hub 还在 → **跳过**（"已扫描过，内容未变"）；
  · 同内容 + 不同路径 → **跳过并记录 duplicate_of**（不生成第二套节点）；
  · `force=True`（或 `RESCAN_FORCE=1`）→ 强制重扫（例如刚改了脱敏登记想重跑）。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

__all__ = [
    "content_key",
    "cache_dir_name",
    "decide",
    "record_scan",
    "list_entries",
    "dedup_report",
    "ensure_table",
    "force_enabled",
]

_CHUNK = 1024 * 1024


def content_key(path: str | Path) -> str:
    """源文件字节 sha256（流式读取，1MB 一块；同内容 → 同指纹）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_dir_name(file_path: str | Path, content_hash: str | None = None) -> str:
    """逐页缓存目录名：`<stem>__<内容指纹前10位>`。

    加内容指纹是为了避免"不同文件夹里的同名文件共用同一缓存目录、互相覆盖 page_*.json"；
    同内容同指纹 → 缓存天然可复用（配合内容级去重，同内容不会被扫第二次）。
    """
    stem = Path(file_path).stem
    tag = (content_hash or "")[:10]
    return f"{stem}__{tag}" if tag else stem


def force_enabled() -> bool:
    """环境变量强制重扫（`.env` 里 RESCAN_FORCE=1）：跳过所有内容级去重判断。"""
    return os.getenv("RESCAN_FORCE", "0").strip() in ("1", "true", "True", "yes")


def policy() -> str:
    """重复文件处理策略（按需求："确认是重复文件 → 覆盖而不是新增"）。

    · `replace`（默认）：判定为重复（同内容、**不同路径**）→ **删除原文档的全部痕迹**
      （hub 正文/概括/哈希/事实/节点/边/向量）并让新文件完整重跑一遍 —— 覆盖语义，
      旧编号退役留空；
    · `skip`：只跳过、保留原文档（旧行为，留作对照）。
    同一路径重复扫描（内容未变）始终走 `unchanged_skip`（幂等，不重复折腾）。
    """
    v = os.getenv("DUP_POLICY", "replace").strip().lower()
    return v if v in ("replace", "skip") else "replace"


def mask_config_tag() -> str:
    """脱敏登记配置指纹（项目登记 + 本公司登记的最近变更时间）。

    为什么要它：去重按"源文件内容"判等，但**脱敏配置变了**（新登记了项目名/本公司名）
    时，同一份源文件重新扫描会得到**不同的脱敏结果**——此时不能因为"内容没变"就跳过，
    否则 hub 里会一直是旧编号。把配置指纹存进去，配置一变就自动放行重扫。
    """
    try:
        from database_serv__infra import get_connection

        with get_connection() as conn, conn.cursor() as cur:
            parts: list[str] = []
            for sql in ("SELECT COALESCE(MAX(updated_at)::text, '') FROM project_registry",
                        "SELECT COALESCE(MAX(confirmed_at)::text, '') FROM self_entity"):
                try:
                    cur.execute(sql)
                    parts.append(str(cur.fetchone()[0] or ""))
                except Exception:
                    conn.rollback()
                    parts.append("")
            return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]
    except Exception:
        return ""


@dataclass
class DedupDecision:
    allow: bool              # True=继续处理；False=跳过
    action: str              # new | unchanged_skip | duplicate_skip | forced
    content_hash: str = ""
    canonical_doc_key: str | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def ensure_table() -> None:
    from psycopg2 import sql as _sql

    from database_serv__infra import APP_DB_CONFIG, get_admin_connection

    ddl = """
    CREATE TABLE IF NOT EXISTS doc_content_index (
        content_hash VARCHAR(64) PRIMARY KEY,
        canonical_doc_key VARCHAR(300) NOT NULL,
        canonical_hub_file VARCHAR(400),
        source_paths JSONB NOT NULL DEFAULT '[]'::jsonb,
        duplicate_doc_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
        scan_count INT NOT NULL DEFAULT 1,
        skip_count INT NOT NULL DEFAULT 0,
        mask_tag VARCHAR(64),
        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )"""
    with get_admin_connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(ddl)
            cur.execute("ALTER TABLE doc_content_index ADD COLUMN IF NOT EXISTS mask_tag VARCHAR(64)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_content_canonical "
                        "ON doc_content_index (canonical_doc_key)")
            cur.execute(
                _sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                    _sql.Identifier("doc_content_index"), _sql.Identifier(APP_DB_CONFIG["user"])
                )
            )


def _lookup(content_hash: str) -> dict | None:
    ensure_table()
    from database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("""SELECT content_hash, canonical_doc_key, canonical_hub_file,
                              source_paths, duplicate_doc_keys, scan_count, skip_count, mask_tag
                       FROM doc_content_index WHERE content_hash = %s""", (content_hash,))
        row = cur.fetchone()
    if not row:
        return None
    return {"content_hash": row[0], "canonical_doc_key": row[1], "canonical_hub_file": row[2],
            "source_paths": row[3] or [], "duplicate_doc_keys": row[4] or [],
            "scan_count": row[5], "skip_count": row[6], "mask_tag": row[7]}


def decide(
    content_hash: str,
    doc_key: str,
    *,
    hub_file: str | Path | None = None,
    force: bool = False,
) -> DedupDecision:
    """判断这份内容是否还要处理（决定是否跳过）。"""
    if force or force_enabled():
        return DedupDecision(True, "forced", content_hash, doc_key,
                             "强制重扫（force/RESCAN_FORCE=1）")
    row = _lookup(content_hash)
    if row is None:
        return DedupDecision(True, "new", content_hash, doc_key, "首次出现（内容指纹未登记）")
    # 脱敏登记变了（新增项目名/本公司名等）→ 同一份源文件重扫会得到不同结果，必须放行
    cur_tag = mask_config_tag()
    if row.get("mask_tag") and cur_tag and row["mask_tag"] != cur_tag:
        return DedupDecision(True, "config_changed", content_hash, doc_key,
                             "脱敏登记已变更（项目/本公司）——重新扫描以套用新编号")
    hub_exists = hub_file is not None and Path(hub_file).exists()
    if row["canonical_doc_key"] == doc_key:
        if hub_exists:
            return DedupDecision(False, "unchanged_skip", content_hash, doc_key,
                                 "同一文件、内容未变（hub 已在）——跳过重复扫描")
        return DedupDecision(True, "new", content_hash, doc_key,
                             "同内容但 hub 产物缺失——重新生成")
    return DedupDecision(
        False, "duplicate_skip", content_hash, row["canonical_doc_key"],
        f"内容与已扫描文件相同（canonical={row['canonical_doc_key']}，"
        f"已出现 {len(row['source_paths'])} 个路径）——跳过，不生成第二套节点",
    )


def record_scan(
    content_hash: str,
    doc_key: str,
    *,
    source_path: str | None = None,
    hub_file: str | None = None,
    skipped: bool = False,
    duplicate_of: str | None = None,
) -> dict:
    """登记一次扫描/跳过（幂等：路径去重追加，计数累加）。"""
    ensure_table()
    from database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("""SELECT canonical_doc_key, source_paths, duplicate_doc_keys
                       FROM doc_content_index WHERE content_hash = %s""", (content_hash,))
        row = cur.fetchone()
        tag = mask_config_tag()
        if row is None:
            paths = [source_path] if source_path else []
            dups = [duplicate_of] if (skipped and duplicate_of) else []
            cur.execute(
                """INSERT INTO doc_content_index
                   (content_hash, canonical_doc_key, canonical_hub_file, source_paths,
                    duplicate_doc_keys, scan_count, skip_count, mask_tag)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (content_hash, duplicate_of or doc_key, hub_file, json.dumps(paths, ensure_ascii=False),
                 json.dumps(dups, ensure_ascii=False), 1 if not skipped else 0,
                 1 if skipped else 0, tag),
            )
        else:
            canonical, paths, dups = row[0], list(row[1] or []), list(row[2] or [])
            if source_path and source_path not in paths:
                paths.append(source_path)
            extra_key = duplicate_of or (doc_key if skipped else None)
            if extra_key and extra_key != canonical and extra_key not in dups:
                dups.append(extra_key)
            cur.execute(
                """UPDATE doc_content_index
                      SET source_paths = %s, duplicate_doc_keys = %s,
                          canonical_hub_file = COALESCE(%s, canonical_hub_file),
                          scan_count = scan_count + %s, skip_count = skip_count + %s,
                          mask_tag = %s, last_seen = CURRENT_TIMESTAMP
                    WHERE content_hash = %s""",
                (json.dumps(paths, ensure_ascii=False), json.dumps(dups, ensure_ascii=False),
                 hub_file, 0 if skipped else 1, 1 if skipped else 0, tag, content_hash),
            )
        conn.commit()
    return {"content_hash": content_hash[:12], "doc_key": doc_key, "skipped": skipped}


def list_entries(*, only_duplicates: bool = False) -> list[dict]:
    ensure_table()
    from database_serv__infra import get_connection

    sql = ("SELECT content_hash, canonical_doc_key, canonical_hub_file, source_paths, "
           "duplicate_doc_keys, scan_count, skip_count, first_seen, last_seen "
           "FROM doc_content_index")
    if only_duplicates:
        sql += " WHERE skip_count > 0 OR jsonb_array_length(duplicate_doc_keys) > 0"
    sql += " ORDER BY last_seen DESC"
    out = []
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(sql)
        for r in cur.fetchall():
            out.append({"content_hash": r[0], "canonical_doc_key": r[1], "canonical_hub_file": r[2],
                        "source_paths": r[3] or [], "duplicate_doc_keys": r[4] or [],
                        "scan_count": r[5], "skip_count": r[6],
                        "first_seen": r[7].isoformat(sep=" ", timespec="seconds") if r[7] else None,
                        "last_seen": r[8].isoformat(sep=" ", timespec="seconds") if r[8] else None})
    return out


def dedup_report() -> dict:
    """汇总（UI/日志）：总内容数、重复内容数、被跳过的扫描次数。"""
    entries = list_entries()
    dups = [e for e in entries if e["skip_count"] > 0 or e["duplicate_doc_keys"]]
    return {
        "contents": len(entries),
        "duplicate_contents": len(dups),
        "skipped_scans": sum(e["skip_count"] for e in entries),
        "duplicates": dups,
    }
