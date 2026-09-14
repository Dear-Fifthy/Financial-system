"""本公司（我方主体）登记：全局脱敏 + 编号特殊提示。

需求（本次）：
  · **全局**：在最高管理员登录时确认"公司完整名称"，确认即**记入脱敏**——
    此后任意文档里出现该公司全称（**不需要**"甲方/乙方"之类锚点）都会全局替换；
  · **编号特殊提示**：本公司的脱敏编号带标记 `[本公司·CO0001]`（编号本身复用实体映射
    的公司编号 CO####，保证"同一家公司全文/全库同码"），AI 提示词里也说明该标记含义；
  · 存储隐私：名称以 Fernet 密文入库（只存 sha256 指纹做查重/命中），明文不落盘；
    读取明文需要 `entity:decrypt:company`（与公司类别一致）。

与"项目名称加密"的分工：
  · 本公司 = 我方主体（通常是文档里的"甲方/收款方/开票方"），全局、无锚点命中；
  · 项目名称 = 业务对象（见 project_registry），两者互不影响、各自独立登记。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from infra.database_serv__infra import (
    MappingDbStore,
    decrypt_value,
    encrypt_value,
    get_admin_connection,
    get_connection,
)
from desens.desens_legend__desens import SELF_TAG, self_display

__all__ = [
    "DECRYPT_PERMISSION",
    "ensure_table",
    "add_entry",
    "remove_entry",
    "set_primary",
    "list_entries",
    "primary_names",
    "needs_confirmation",
    "mark_confirmed",
    "invalidate_cache",
    "load_index",
    "mask_text",
    "display_for",
    "audit_tail",
]

DECRYPT_PERMISSION = "entity:decrypt:company"
LOG_DIR = Path(__file__).resolve().parents[1] / "logs" / "self_entity"
_CACHE: dict = {"ts": 0.0, "entries": [], "loaded": False}
_CACHE_TTL = 60.0


def ensure_table() -> None:
    """懒建本公司登记表（幂等；管理员连接建表 + 授权应用角色）。"""
    from psycopg2 import sql as _sql

    from infra.database_serv__infra import APP_DB_CONFIG

    with get_admin_connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS self_entity (
                    id SERIAL PRIMARY KEY,
                    code VARCHAR(16) UNIQUE NOT NULL,
                    name_fp VARCHAR(64) UNIQUE NOT NULL,
                    name_cipher TEXT NOT NULL,
                    name_len SMALLINT,
                    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
                    note TEXT,
                    confirmed_by VARCHAR(64),
                    confirmed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                _sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                    _sql.Identifier("self_entity"), _sql.Identifier(APP_DB_CONFIG["user"])
                )
            )
            cur.execute(
                _sql.SQL("GRANT USAGE, SELECT ON SEQUENCE {} TO {}").format(
                    _sql.Identifier("self_entity_id_seq"), _sql.Identifier(APP_DB_CONFIG["user"])
                )
            )


def _norm(value: str) -> str:
    return re.sub(r"[\s\u3000]+", "", value or "")


def _fp(value: str) -> str:
    return hashlib.sha256(_norm(value).encode("utf-8")).hexdigest()


def audit(action: str, **fields) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"ts": datetime.now().isoformat(timespec="seconds"), "action": action}
        payload.update(fields)
        with (LOG_DIR / f"self_entity_{datetime.now():%Y%m%d}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def audit_tail(limit: int = 20) -> list[dict]:
    try:
        files = sorted(LOG_DIR.glob("self_entity_*.jsonl"))
        if not files:
            return []
        return [json.loads(ln) for ln in files[-1].read_text(encoding="utf-8").splitlines()[-limit:]
                if ln.strip()]
    except Exception:
        return []


def invalidate_cache() -> None:
    _CACHE["ts"] = 0.0
    _CACHE["entries"] = []
    _CACHE["loaded"] = False


# =========================================================
# 登记
# =========================================================
def add_entry(
    name: str,
    *,
    is_primary: bool = True,
    note: str | None = None,
    user: dict | None = None,
) -> dict:
    """登记本公司完整名称（登录确认时调用）：名称必填，登记即加密（同值同码）。"""
    clean = (name or "").strip()
    if not clean:
        raise ValueError("公司完整名称必填")
    if len(clean) < 4:
        # 过短的"名称"（如"公司"）会在大段文本里大面积误伤，直接拒绝
        raise ValueError("公司名称过短（至少 4 个字符），请填写完整名称")
    if len(clean) > 100:
        raise ValueError("公司名称过长（≤100 字）")
    ensure_table()
    store = MappingDbStore()
    code = store.get_or_create_code("company", clean)   # 复用公司编号：全文/全库同码
    fp = _fp(clean)
    by = (user or {}).get("username")
    created = False
    with get_connection() as conn, conn.cursor() as cur:
        if is_primary:
            cur.execute("UPDATE self_entity SET is_primary = FALSE WHERE is_primary")
        cur.execute("SELECT code FROM self_entity WHERE name_fp = %s", (fp,))
        row = cur.fetchone()
        if row:
            cur.execute(
                """UPDATE self_entity SET confirmed_by = %s, confirmed_at = CURRENT_TIMESTAMP,
                          note = COALESCE(%s, note), is_primary = %s
                    WHERE name_fp = %s""",
                (by, note, bool(is_primary), fp),
            )
        else:
            cur.execute(
                """INSERT INTO self_entity
                       (code, name_fp, name_cipher, name_len, is_primary, note, confirmed_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (code, fp, encrypt_value(clean), len(clean), bool(is_primary), note, by),
            )
            created = True
        conn.commit()
    invalidate_cache()
    audit("confirm", code=code, name_fp=fp[:16], name_len=len(clean), primary=bool(is_primary), by=by)
    return {"code": code, "created": created, "name": clean, "is_primary": bool(is_primary)}


def remove_entry(code: str, *, user: dict | None = None) -> dict:
    ensure_table()
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM self_entity WHERE code = %s", (code,))
        n = cur.rowcount
        conn.commit()
    invalidate_cache()
    if n:
        audit("remove", code=code, by=(user or {}).get("username"))
    return {"ok": bool(n), "msg": "已移除" if n else f"未找到 {code}"}


def set_primary(code: str, *, user: dict | None = None) -> dict:
    ensure_table()
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE self_entity SET is_primary = FALSE WHERE is_primary")
        cur.execute("UPDATE self_entity SET is_primary = TRUE, confirmed_by = %s, "
                    "confirmed_at = CURRENT_TIMESTAMP WHERE code = %s",
                    ((user or {}).get("username"), code))
        n = cur.rowcount
        conn.commit()
    invalidate_cache()
    if n:
        audit("set_primary", code=code, by=(user or {}).get("username"))
    return {"ok": bool(n)}


def list_entries(user: dict | None = None) -> list[dict]:
    """本公司登记列表；名称可见性取决于 `entity:decrypt:company` 权限。"""
    from infra.database_serv__infra import require_permission

    ensure_table()
    can_decrypt, _msg = require_permission(user, DECRYPT_PERMISSION)
    out: list[dict] = []
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT code, name_cipher, name_len, is_primary, note, confirmed_by, confirmed_at
                 FROM self_entity ORDER BY is_primary DESC, code"""
        )
        for code, cipher, name_len, primary, note, by, ts in cur.fetchall():
            out.append({
                "code": code,
                "name": decrypt_value(cipher) if can_decrypt else None,
                "name_masked": f"{'*' * int(name_len or 0)}（{name_len or 0}字）",
                "display": self_display(code),
                "is_primary": primary,
                "note": note,
                "confirmed_by": by,
                "confirmed_at": ts.isoformat(sep=" ", timespec="seconds") if ts else None,
                "decrypted": can_decrypt,
            })
    return out


def primary_names() -> list[str]:
    """主公司名称（内存解密，供提示词/展示；不写日志）。"""
    ensure_table()
    names: list[str] = []
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT name_cipher FROM self_entity WHERE is_primary ORDER BY code")
        names = [decrypt_value(r[0]) for r in cur.fetchall()]
    return names


def needs_confirmation() -> bool:
    """登录时是否需要请管理员确认本公司名称（无登记 = 需要）。"""
    ensure_table()
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM self_entity")
        return cur.fetchone()[0] == 0


def mark_confirmed(code: str, user: dict | None = None) -> None:
    """登录时"确认无需改动"：刷新确认时间/人（审计留痕）。"""
    ensure_table()
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE self_entity SET confirmed_by = %s, confirmed_at = CURRENT_TIMESTAMP "
                    "WHERE code = %s", ((user or {}).get("username"), code))
        conn.commit()
    audit("reconfirm_at_login", code=code, by=(user or {}).get("username"))


def display_for(code: str) -> str:
    return self_display(code)


# =========================================================
# 脱敏链路：全局替换（无锚点）
# =========================================================
def load_index(*, force: bool = False) -> list[dict]:
    """载入"本公司名称 -> 带标记编号"索引（内存缓存；空表也命中缓存，避免每页回库）。"""
    now = datetime.now().timestamp()
    if not force and _CACHE["loaded"] and (now - _CACHE["ts"]) < _CACHE_TTL:
        return _CACHE["entries"]
    ensure_table()
    entries: list[dict] = []
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT code, name_cipher, is_primary FROM self_entity ORDER BY code")
        for code, cipher, primary in cur.fetchall():
            name = decrypt_value(cipher)
            entries.append({"code": code, "name": name, "is_primary": primary,
                            "display": self_display(code)})
    entries.sort(key=lambda e: len(e["name"]), reverse=True)   # 长名优先，防短名切碎长名
    _CACHE["entries"] = entries
    _CACHE["ts"] = now
    _CACHE["loaded"] = True
    return entries


def mask_text(text: str, store: object | None = None, tracker=None) -> str:
    """把文中出现的本公司全称全局替换为带标记编号 `[本公司·CO####]`（无锚点也命中）。

    幂等：替换结果里只剩编号与标记，不含原名，二次调用不会再变。
    """
    if not text:
        return text
    entries = load_index()
    if not entries:
        return text
    pattern = re.compile("|".join(re.escape(e["name"]) for e in entries if e["name"]))
    mapping = {e["name"]: e["display"] for e in entries if e["name"]}

    def _rep(m: re.Match) -> str:
        rep = mapping[m.group(0)]
        if tracker is not None and hasattr(tracker, "name_cache"):
            tracker.name_cache[m.group(0)] = rep
        return rep

    return pattern.sub(_rep, text)
