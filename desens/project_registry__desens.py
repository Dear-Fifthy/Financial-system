"""项目名称加密登记（最高管理员自定义加密 + 分类 + 与 AI 判断的项目名交叉验证）。

需求（4）落地口径：
  · **加密由最高管理员自定义**：项目名称**必填**、简称**选填**；登记即加密（明文不落盘）；
  · 加密后可为其**分类**：分类"已有即选、未有可加"（`project_categories` 字典）；
  · 文档/链路里碰到项目名称（含 **AI 判断出的项目名称**）→ 与登记表**交叉验证**：
      命中（全称/简称）→ 一律用编号，明文不再出现；
      近似/未登记 → 记为 **AI 提案（待审批）**，管理员批准即登记（AI 提案、我审批）。

存储（隐私优先，复用既有实体映射机制）：
  · `entity_mapping_project`（见 database_serv）：code = `PJ####`（同值同码、幂等），
    norm_key = sha256(归一化名称)，cipher = Fernet 密文；明文不落盘；
    解密需权限 `entity:decrypt:project`。
  · `project_registry`：code + 名称密文/指纹 + 简称密文/指纹 + 分类 + 备注 + 启用 + 创建人/时间。
  · `project_categories`：分类字典（已有即选；未有可加，唯一）。
  · `project_proposals`：AI 提案（名称密文 + 指纹 + 来源文档 + 建议分类 + 状态）。

匹配口径：
  · 归一化 = 去空白 + 去脱敏编号（`[PJ0001]`/`CO0003` 之类）+ 全角标点归一；
  · 全称/简称**精确**归一化命中 → matched / alias；
  · 去掉"项目/工程"等尾缀后互为包含、或相似度 ≥ 0.75 → similar（列候选，等审批）；
  · 其余 → unregistered。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from infra.database_serv__infra import (
    MappingDbStore,
    decrypt_value,
    encrypt_value,
    get_admin_connection,
    get_connection,
)

__all__ = [
    "PREFIX",
    "DECRYPT_PERMISSION",
    "add_entry",
    "update_entry",
    "delete_entry",
    "list_entries",
    "add_category",
    "list_categories",
    "load_index",
    "invalidate_cache",
    "mask_text",
    "cross_validate",
    "propose",
    "list_proposals",
    "approve_proposal",
    "reject_proposal",
    "audit_tail",
]

PREFIX = "PJ"                       # 项目编号前缀：PJ0001（2 位前缀，兼容既有反查机制）
DECRYPT_PERMISSION = "entity:decrypt:project"
LOGO_DIR = Path(__file__).resolve().parents[1] / "logs" / "project"
SIMILAR_MIN_LEN = 3                 # 模糊比较的最小核心长度（太短不比，防误判）
SIMILAR_RATIO = 0.75

_CODE_TOKEN_RE = re.compile(r"\[(?:PJ|CO|PT|DT|ID|BC|TX|BK|BA)\d{2,}\]|(?<![A-Za-z])(?:PJ|CO|PT|DT|ID|BC|TX|BK|BA)\d{4}(?![0-9])")
_CORE_SUFFIXES = ("建设项目", "项目工程", "工程")

_CACHE: dict = {"ts": 0.0, "entries": [], "loaded": False}
_CACHE_TTL = 60.0                   # 秒；写操作会立即失效，跨进程变更最多滞后 TTL


# =========================================================
# 基础工具
# =========================================================
def _norm(value: str) -> str:
    """归一化：去空白/编号、全角转半角、去常见包装标点（用于指纹与精确匹配）。"""
    s = _CODE_TOKEN_RE.sub("", value or "")
    s = s.translate(str.maketrans("（）【】《》［］", "()[]<>[]"))
    s = re.sub(r"[\s\u3000]+", "", s)
    s = s.strip("\"'“”‘’()[]<>:：,，.。;；、-—_/\\|")
    return s


def _core(value: str) -> str:
    """去尾缀后的核心名（"南苑新村消防维保项目" → "南苑新村消防维保"）。"""
    s = _norm(value)
    changed = True
    while changed and s:
        changed = False
        for suffix in _CORE_SUFFIXES:
            if s.endswith(suffix) and len(s) > len(suffix) + 1:
                s = s[: -len(suffix)]
                changed = True
    return s


def _fp(value: str) -> str:
    return hashlib.sha256(_norm(value).encode("utf-8")).hexdigest()


def _ensure_tables() -> None:
    """懒建项目登记相关表（幂等；管理员连接建表 + 授权应用角色）。"""
    from infra.database_serv__infra import _ensure_entity_table   # 项目编号走实体映射表机制

    _ensure_entity_table("project")
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS project_categories (
            id SERIAL PRIMARY KEY,
            name VARCHAR(64) UNIQUE NOT NULL,
            created_by VARCHAR(64),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS project_registry (
            id SERIAL PRIMARY KEY,
            code VARCHAR(16) UNIQUE NOT NULL,
            name_fp VARCHAR(64) UNIQUE NOT NULL,
            name_cipher TEXT NOT NULL,
            name_len SMALLINT,
            short_fp VARCHAR(64) UNIQUE,
            short_cipher TEXT,
            category VARCHAR(64),
            note TEXT,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_by VARCHAR(64),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS project_proposals (
            id SERIAL PRIMARY KEY,
            name_fp VARCHAR(64) UNIQUE NOT NULL,
            name_cipher TEXT NOT NULL,
            doc_key VARCHAR(300),
            suggested_category VARCHAR(64),
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            decided_by VARCHAR(64),
            decided_at TIMESTAMP,
            decided_code VARCHAR(16)
        )
        """,
    ]
    from psycopg2 import sql as _sql

    from infra.database_serv__infra import APP_DB_CONFIG

    with get_admin_connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for stmt in ddl:
                cur.execute(stmt)
            for table in ("project_categories", "project_registry", "project_proposals"):
                cur.execute(
                    _sql.SQL("GRANT ALL PRIVILEGES ON {} TO {}").format(
                        _sql.Identifier(table), _sql.Identifier(APP_DB_CONFIG["user"])
                    )
                )
                cur.execute(
                    _sql.SQL("GRANT USAGE, SELECT ON SEQUENCE {} TO {}").format(
                        _sql.Identifier(f"{table}_id_seq"), _sql.Identifier(APP_DB_CONFIG["user"])
                    )
                )


def audit(action: str, **fields) -> None:
    """登记/审批审计日志（不含明文名称，只记编号/指纹/长度/操作者）。"""
    try:
        LOGO_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"ts": datetime.now().isoformat(timespec="seconds"), "action": action}
        payload.update(fields)
        with (LOGO_DIR / f"registry_{datetime.now():%Y%m%d}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def audit_tail(limit: int = 20) -> list[dict]:
    """读取最近审计记录（UI 展示用）。"""
    try:
        files = sorted(LOGO_DIR.glob("registry_*.jsonl"))
        if not files:
            return []
        lines = files[-1].read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(ln) for ln in lines if ln.strip()]
    except Exception:
        return []


def invalidate_cache() -> None:
    _CACHE["ts"] = 0.0
    _CACHE["entries"] = []
    _CACHE["loaded"] = False


# =========================================================
# 登记（自定义加密）
# =========================================================
def add_entry(
    name: str,
    *,
    short_name: str | None = None,
    category: str | None = None,
    note: str | None = None,
    user: dict | None = None,
) -> dict:
    """登记一个项目名称并加密（名称必填、简称选填；分类已有即选、未有可加）。

    幂等：同一名称（归一化后相同）永远得到同一编号；重复登记只更新简称/分类/备注。
    """
    clean = (name or "").strip()
    if not clean:
        raise ValueError("项目名称必填")
    if len(clean) > 64:
        raise ValueError("项目名称过长（≤64 字）")
    short = (short_name or "").strip() or None
    if short and len(short) > 32:
        raise ValueError("项目简称过长（≤32 字）")
    cat = (category or "").strip() or None
    _ensure_tables()

    store = MappingDbStore()
    code = store.get_or_create_project_code(clean)
    fp = _fp(clean)
    short_fp = _fp(short) if short else None
    user_name = (user or {}).get("username")

    if cat:
        add_category(cat, user=user)

    created = False
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT code, short_fp FROM project_registry WHERE name_fp = %s", (fp,))
        row = cur.fetchone()
        if row:
            code = row[0]
            cur.execute(
                """UPDATE project_registry
                      SET short_cipher = COALESCE(%s, short_cipher),
                          short_fp     = COALESCE(%s, short_fp),
                          category     = COALESCE(%s, category),
                          note         = COALESCE(%s, note),
                          is_active    = TRUE,
                          updated_at   = CURRENT_TIMESTAMP
                    WHERE name_fp = %s""",
                (encrypt_value(short) if short else None, short_fp, cat, note, fp),
            )
        else:
            cur.execute(
                """INSERT INTO project_registry
                       (code, name_fp, name_cipher, name_len, short_fp, short_cipher,
                        category, note, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (code, fp, encrypt_value(clean), len(clean), short_fp,
                 encrypt_value(short) if short else None, cat, note, user_name),
            )
            created = True
        conn.commit()
    invalidate_cache()
    audit("add" if created else "update", code=code, name_fp=fp[:16], name_len=len(clean),
          has_short=bool(short), category=cat, by=user_name)
    return {"code": code, "created": created, "name": clean, "short_name": short,
            "category": cat, "note": note, "is_active": True}


def update_entry(
    code: str,
    *,
    short_name: str | None = None,
    category: str | None = None,
    note: str | None = None,
    is_active: bool | None = None,
    user: dict | None = None,
) -> dict:
    """修改登记项（简称/分类/备注/启停）。名称本身不允许改（改了就是另一个项目，请新增）。"""
    _ensure_tables()
    sets, params = [], []
    if short_name is not None:
        short = short_name.strip() or None
        sets += ["short_cipher = %s", "short_fp = %s"]
        params += [encrypt_value(short) if short else None, _fp(short) if short else None]
    if category is not None:
        cat = category.strip() or None
        if cat:
            add_category(cat, user=user)
        sets.append("category = %s")
        params.append(cat)
    if note is not None:
        sets.append("note = %s")
        params.append(note)
    if is_active is not None:
        sets.append("is_active = %s")
        params.append(bool(is_active))
    if not sets:
        return {"ok": False, "msg": "没有需要修改的字段"}
    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.append(code)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE project_registry SET {', '.join(sets)} WHERE code = %s", params)
        n = cur.rowcount
        conn.commit()
    invalidate_cache()
    if n:
        audit("update_fields", code=code, fields=sets[:-1], by=(user or {}).get("username"))
    return {"ok": bool(n), "msg": "已更新" if n else f"未找到编号 {code}"}


def delete_entry(code: str, *, user: dict | None = None) -> dict:
    """删除登记项（实体映射里的编号保留，历史文档里的编号仍可反查/解密）。"""
    _ensure_tables()
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM project_registry WHERE code = %s", (code,))
        n = cur.rowcount
        conn.commit()
    invalidate_cache()
    if n:
        audit("delete", code=code, by=(user or {}).get("username"))
    return {"ok": bool(n), "msg": "已删除" if n else f"未找到编号 {code}"}


def list_entries(
    user: dict | None = None,
    *,
    include_inactive: bool = False,
) -> list[dict]:
    """登记项列表。名称是否可见取决于 `entity:decrypt:project` 权限（无权限只给编号）。"""
    from infra.database_serv__infra import require_permission

    _ensure_tables()
    can_decrypt, _msg = require_permission(user, DECRYPT_PERMISSION)
    where = "" if include_inactive else "WHERE is_active"
    out: list[dict] = []
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""SELECT code, name_cipher, name_len, short_cipher, category, note,
                       is_active, created_by, created_at
                  FROM project_registry {where} ORDER BY code"""
        )
        for code, name_cipher, name_len, short_cipher, category, note, active, by, ts in cur.fetchall():
            item = {
                "code": code,
                "name": decrypt_value(name_cipher) if can_decrypt else None,
                "name_masked": f"{'*' * int(name_len or 0)}（{name_len or 0}字）",
                "short_name": (decrypt_value(short_cipher) if (can_decrypt and short_cipher) else None),
                "category": category,
                "note": note,
                "is_active": active,
                "created_by": by,
                "created_at": ts.isoformat(sep=" ", timespec="seconds") if ts else None,
                "decrypted": can_decrypt,
            }
            out.append(item)
    return out


def add_category(name: str, *, user: dict | None = None) -> dict:
    """新增分类（未有可加）；已存在则直接返回（已有即选）。"""
    clean = (name or "").strip()
    if not clean:
        raise ValueError("分类名不能为空")
    _ensure_tables()
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO project_categories (name, created_by) VALUES (%s, %s) "
                    "ON CONFLICT (name) DO NOTHING", (clean, (user or {}).get("username")))
        created = cur.rowcount > 0
        conn.commit()
    if created:
        audit("add_category", category=clean, by=(user or {}).get("username"))
    return {"name": clean, "created": created}


def list_categories() -> list[str]:
    """分类字典（下拉框"已有即选"用）。"""
    _ensure_tables()
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT name FROM project_categories ORDER BY name")
        names = [r[0] for r in cur.fetchall()]
    # 登记表里历史用过的分类也一并列出（老数据没有字典行）
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT category FROM project_registry "
                    "WHERE category IS NOT NULL AND category <> '' ORDER BY category")
        for (c,) in cur.fetchall():
            if c not in names:
                names.append(c)
    return sorted(names)


# =========================================================
# 脱敏链路：项目名/简称 -> 编号
# =========================================================
def load_index(*, force: bool = False) -> list[dict]:
    """载入"已登记项目名/简称 -> 编号"索引（内存缓存，供脱敏与交叉验证使用）。

    隐私说明：为了让**脱敏**能识别明文项目名，必须在本机内存里持有明文；这些明文
    只存在于进程内存与 Fernet 密文（库内），不写日志、不进 hub、不发给 AI。
    """
    now = datetime.now().timestamp()
    # 注意用 loaded 标记判定缓存有效性：登记表**为空**时也必须命中缓存，
    # 否则每一页脱敏都会回库查一次（大文档 = 每页一次查询）。
    if not force and _CACHE["loaded"] and (now - _CACHE["ts"]) < _CACHE_TTL:
        return _CACHE["entries"]
    _ensure_tables()
    entries: list[dict] = []
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT code, name_cipher, short_cipher, category
                 FROM project_registry WHERE is_active ORDER BY code"""
        )
        for code, name_cipher, short_cipher, category in cur.fetchall():
            name = decrypt_value(name_cipher)
            short = decrypt_value(short_cipher) if short_cipher else None
            aliases = [a for a in (name, short) if a]
            entries.append({
                "code": code,
                "name": name,
                "short_name": short,
                "category": category,
                "display": f"[{code}]",
                "aliases": sorted(set(aliases), key=len, reverse=True),
            })
    entries.sort(key=lambda e: max((len(a) for a in e["aliases"]), default=0), reverse=True)
    _CACHE["entries"] = entries
    _CACHE["ts"] = now
    _CACHE["loaded"] = True
    return entries


def mask_text(text: str, store: object | None = None, tracker=None) -> str:
    """把文中已登记的项目名称/简称替换为编号（脱敏链路调用，**项目名优先**）。

    · 只在**登记过的**名称上替换（宁缺勿错：未登记的项目名不猜）；
    · 同一名称全文一致（同一编号），并把映射登记进 tracker.name_cache，
      让"复查②已登记实体补码"在后续页面同样生效。
    """
    if not text:
        return text
    entries = load_index()
    if not entries:
        return text
    pairs: list[tuple[str, str]] = []
    for e in entries:
        for alias in e["aliases"]:
            pairs.append((alias, e["display"]))
    if not pairs:
        return text
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    pattern = re.compile("|".join(re.escape(a) for a, _d in pairs))
    mapping = dict(pairs)

    def _rep(m: re.Match) -> str:
        key = m.group(0)
        rep = mapping[key]
        if tracker is not None and hasattr(tracker, "name_cache"):
            tracker.name_cache[key] = rep
        return rep

    return pattern.sub(_rep, text)


# =========================================================
# 与 AI 判断的项目名称交叉验证
# =========================================================
def cross_validate(ai_name: str, *, doc_key: str | None = None, user: dict | None = None) -> dict:
    """把 AI 判断出的项目名称与登记表交叉验证。

    返回：{"status": matched|alias|similar|unregistered, "code", "category",
           "via", "candidates": [...], "pending_approval": bool, "proposal_id": int|None}
    · matched/alias：命中已登记名称 → 调用方应改用 `code`（明文不再出现）；
    · similar：与某个登记项高度相似但不完全一致 → 列候选，记提案待审批；
    · unregistered：完全没登记 → 记提案待审批（AI 提案、我审批）。
    """
    clean = (ai_name or "").strip()
    result: dict = {"ai_name_len": len(clean), "status": "empty", "code": None,
                    "category": None, "via": None, "candidates": [],
                    "pending_approval": False, "proposal_id": None}
    if not clean:
        return result

    fp = _fp(clean)
    result["ai_name_fp"] = fp[:16]
    entries = load_index()
    for e in entries:
        if fp == _fp(e["name"]):
            result.update({"status": "matched", "code": e["code"], "category": e["category"],
                           "via": "full_name"})
            audit("cross_validate", status="matched", code=e["code"], ai_fp=fp[:16], doc=doc_key)
            return result
        if e["short_name"] and fp == _fp(e["short_name"]):
            result.update({"status": "alias", "code": e["code"], "category": e["category"],
                           "via": "short_name"})
            audit("cross_validate", status="alias", code=e["code"], ai_fp=fp[:16], doc=doc_key)
            return result

    core = _core(clean)
    for e in entries:
        for alias, kind in ((e["name"], "full_name"), (e["short_name"], "short_name")):
            if not alias:
                continue
            other = _core(alias)
            if not other:
                continue
            ratio = SequenceMatcher(None, core, other).ratio()
            contained = (len(core) >= SIMILAR_MIN_LEN and core in other) or \
                        (len(other) >= SIMILAR_MIN_LEN and other in core)
            if contained or ratio >= SIMILAR_RATIO:
                result["candidates"].append({
                    "code": e["code"], "category": e["category"], "via": kind,
                    "similarity": round(ratio, 3), "contained": bool(contained),
                })
    result["candidates"].sort(key=lambda c: c["similarity"], reverse=True)

    if result["candidates"]:
        result["status"] = "similar"
    else:
        result["status"] = "unregistered"
    prop = propose(clean, doc_key=doc_key, user=user,
                   reason=("与已登记项目高度相似，需人工确认" if result["candidates"]
                           else "登记表中无此项目"))
    result["proposal_id"] = prop.get("id")
    result["pending_approval"] = True
    audit("cross_validate", status=result["status"], ai_fp=fp[:16], doc=doc_key,
          candidates=[c["code"] for c in result["candidates"]])
    return result


def propose(
    name: str,
    *,
    doc_key: str | None = None,
    category: str | None = None,
    reason: str | None = None,
    user: dict | None = None,
) -> dict:
    """记录 AI 提案（同名称只保留一条待审提案；已审过的不再重复建）。"""
    clean = (name or "").strip()
    if not clean:
        return {"ok": False, "msg": "空提案"}
    _ensure_tables()
    fp = _fp(clean)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, status FROM project_proposals WHERE name_fp = %s", (fp,))
        row = cur.fetchone()
        if row:
            cur.execute(
                """UPDATE project_proposals
                      SET doc_key = COALESCE(%s, doc_key),
                          suggested_category = COALESCE(%s, suggested_category),
                          reason = COALESCE(%s, reason)
                    WHERE id = %s""",
                (doc_key, category, reason, row[0]),
            )
            conn.commit()
            return {"ok": True, "id": row[0], "status": row[1], "created": False}
        cur.execute(
            """INSERT INTO project_proposals
                   (name_fp, name_cipher, doc_key, suggested_category, reason)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (fp, encrypt_value(clean), doc_key, category, reason),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
    audit("propose", proposal_id=new_id, name_fp=fp[:16], doc=doc_key, by=(user or {}).get("username"))
    return {"ok": True, "id": new_id, "status": "pending", "created": True}


def list_proposals(
    status: str = "pending",
    *,
    user: dict | None = None,
) -> list[dict]:
    """AI 提案列表（`status=None` 表示全部）。名称可见性同登记项（需解密权限）。"""
    from infra.database_serv__infra import require_permission

    _ensure_tables()
    can_decrypt, _msg = require_permission(user, DECRYPT_PERMISSION)
    sql = ("SELECT id, name_cipher, doc_key, suggested_category, status, reason, "
           "created_at, decided_by, decided_code FROM project_proposals")
    params: tuple = ()
    if status:
        sql += " WHERE status = %s"
        params = (status,)
    sql += " ORDER BY created_at DESC, id DESC"
    out: list[dict] = []
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        for row in cur.fetchall():
            (pid, cipher, doc_key, cat, st, reason, created, by, decided_code) = row
            out.append({
                "id": pid,
                "name": decrypt_value(cipher) if can_decrypt else None,
                "name_masked": "待审批项目名（无解密权限）" if not can_decrypt else None,
                "doc_key": doc_key,
                "suggested_category": cat,
                "status": st,
                "reason": reason,
                "created_at": created.isoformat(sep=" ", timespec="seconds") if created else None,
                "decided_by": by,
                "decided_code": decided_code,
            })
    return out


def _backfill_archive(conn, name: str, code: str) -> int:
    """审批通过后把历史归档/台账里的明文项目名替换为编号（尽力而为：老库可能无此表/列）。"""
    changed = 0
    for table, column in (("project_archive", "project_name"),
                          ("contract_projects", '"项目"')):
        try:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {table} SET {column} = %s WHERE {column} = %s", (code, name))
                changed += cur.rowcount
        except Exception:
            conn.rollback()
    return changed


def approve_proposal(
    proposal_id: int,
    *,
    name: str | None = None,
    short_name: str | None = None,
    category: str | None = None,
    note: str | None = None,
    user: dict | None = None,
) -> dict:
    """批准 AI 提案 → 正式登记（加密 + 编号 + 分类），并回填历史明文项目名。"""
    _ensure_tables()
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT name_cipher, status FROM project_proposals WHERE id = %s", (proposal_id,))
        row = cur.fetchone()
    if not row:
        return {"ok": False, "msg": f"未找到提案 #{proposal_id}"}
    clean = (name or "").strip() or decrypt_value(row[0])
    entry = add_entry(clean, short_name=short_name, category=category, note=note, user=user)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE project_proposals
                  SET status = 'approved', decided_by = %s, decided_at = CURRENT_TIMESTAMP,
                      decided_code = %s
                WHERE id = %s""",
            ((user or {}).get("username"), entry["code"], proposal_id),
        )
        conn.commit()
        backfilled = _backfill_archive(conn, clean, entry["code"])
        conn.commit()
    audit("approve", proposal_id=proposal_id, code=entry["code"],
          backfilled=backfilled, by=(user or {}).get("username"))
    return {"ok": True, "code": entry["code"], "created": entry["created"],
            "backfilled": backfilled, "msg": f"已登记并加密：{entry['code']}"}


def reject_proposal(proposal_id: int, *, reason: str | None = None, user: dict | None = None) -> dict:
    """驳回 AI 提案（不登记、不加密；保留记录便于审计）。"""
    _ensure_tables()
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE project_proposals
                  SET status = 'rejected', decided_by = %s, decided_at = CURRENT_TIMESTAMP,
                      reason = COALESCE(%s, reason)
                WHERE id = %s""",
            ((user or {}).get("username"), reason, proposal_id),
        )
        n = cur.rowcount
        conn.commit()
    if n:
        audit("reject", proposal_id=proposal_id, reason=reason, by=(user or {}).get("username"))
    return {"ok": bool(n), "msg": "已驳回" if n else f"未找到提案 #{proposal_id}"}
