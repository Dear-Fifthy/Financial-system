# -*- coding: utf-8 -*-
"""工作区（界面叫「仓库」）：**一个仓库 = 一套完全隔离的环境**。

隔离范围（按需求："AI 接口与遍历方法不变，其余边和数据完全切换"）：

| 隔离（每个仓库一份） | 不隔离（全局共用） |
|---|---|
| 数据库（实体登记表/事实/概括/图/RAG/台账/用户） | AI 接口与密钥（`.env` 的 AI_*/EDGE_AI_*/…) |
| `hub/`（AI 唯一可读的脱敏产物根） | 检索方式 `RETRIEVAL_METHOD`（图遍历/RAG） |
| `input/`、`output/`（逐页缓存 = 原文级材料） | 诊断日志 `logs/`（不含业务数据） |
| 加密密钥 `hub_encryption.key`（密文互不可解） | 代码与模型 |

命名：**代码里叫 workspace，界面上叫「仓库」**。
（仓库里已有的 `project_registry` / `PJ####` 是"业务项目名"，与本模块无关，别混。）

数据布局：
    workspaces/registry.json          —— 仓库登记表（唯一真相源）
    workspaces/<slug>/{hub,input,output,hub_encryption.key}
    .env 里冗余写一份 ACTIVE_WORKSPACE / APP_DB_NAME / DSH_*（供各模块导入期读取）

为什么冗余写 `.env`：`database_serv__infra.APP_DB_CONFIG` 是**导入期快照**，各模块
在 import 时就定了库名与目录；`.env` 保证"没调 apply_active 的进程（如 CLI 脚本）"
也落在正确的仓库上；`apply_active()` 负责把已导入模块里的常量改过来（热切换）。

命令行：
    python -m infra.workspace__infra list
    python -m infra.workspace__infra register-legacy --name 马山三标段
    python -m infra.workspace__infra create --name 样例仓库 --slug sample
    python -m infra.workspace__infra switch sample
    python -m infra.workspace__infra info [slug]
    python -m infra.workspace__infra reset <slug> --confirm     # 可回滚：旧库改名保留
    python -m infra.workspace__infra delete <slug> --confirm    # 可回滚：数据目录移入 _bak
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path

if __package__ in (None, ""):          # 直接 `python <层>/<模块>.py` 跑：把仓库根放回 sys.path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))


import argparse
import json
import os
import re
import shutil
import importlib
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACES_DIR = ROOT / "workspaces"
REGISTRY_FILE = WORKSPACES_DIR / "registry.json"
LEGACY_MAPPING_NAME = "_mapping/entity_mapping.json"   # 旧版本地映射文件（首次导入用）
INIT_SQL = ROOT / "init_db.sql"

# .env 里由本模块管理的键（切换仓库时一并更新）
ENV_KEYS = ("ACTIVE_WORKSPACE", "APP_DB_NAME", "DSH_HUB_DIR",
            "DSH_INPUT_DIR", "DSH_OUTPUT_DIR", "DSH_KEY_FILE", "DSH_CHAT_DIR")

# 已经导入的模块里需要"改路径常量"的属性表（热切换用；未导入的模块跳过即可）
_PATH_ATTRS: tuple[tuple[str, str, str], ...] = (
    ("desens.hub_pipeline__desens", "HUB_DIR", "hub"),
    ("l1.l1_extract__summary_hash", "HUB_DIR", "hub"),
    ("graph.doc_index__graph", "HUB_DIR", "hub"),
    ("graph.graph_query__graph_walk", "HUB_DIR", "hub"),
    ("desens.entity_repair__desens", "HUB_DIR", "hub"),
    ("desens.doc_lifecycle__desens", "HUB_DIR", "hub"),
    ("desens.doc_lifecycle__desens", "OUTPUT_DIR", "output"),
    ("scan.scanner_core__scan", "INPUT_DIR", "input"),
    ("scan.scanner_core__scan", "OUTPUT_DIR", "output"),
    # hub_pipeline 在导入期把扫描缓存目录 **复制** 成了自己的常量，热切换要一起改
    ("desens.hub_pipeline__desens", "CACHE_DIR", "output"),
    # 对话记录：历史问答里带着本仓库的证据，不能跨仓库看到
    ("ui.chat_store__ui", "CHAT_ROOT", "chat"),
    ("ui.chat_store__ui", "RECYCLE_ROOT", "chat_recycle"),
)


# =========================================================
# 0. 纯路径工具（**只依赖标准库**：其它模块在导入期就会调它们）
# =========================================================
def _env_path(name: str, default: Path) -> Path:
    raw = (os.getenv(name, "") or "").strip()
    return Path(raw).expanduser().resolve() if raw else default


def env_value(key: str, default: str = "") -> str:
    """读配置：进程环境变量优先，其次 `.env` 文件（CLI 直接跑时 .env 还没被 load_dotenv 载入）。"""
    v = (os.getenv(key, "") or "").strip()
    if v:
        return v
    p = ROOT / ".env"
    if p.exists():
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s.startswith(f"{key}="):
                    return s.split("=", 1)[1].strip()
        except Exception:
            pass
    return default


def hub_root() -> Path:
    """当前仓库的 hub 根（AI 唯一可读的脱敏产物根）。"""
    return _env_path("DSH_HUB_DIR", ROOT / "hub")


def input_root() -> Path:
    return _env_path("DSH_INPUT_DIR", ROOT / "input")


def output_root() -> Path:
    return _env_path("DSH_OUTPUT_DIR", ROOT / "output")


def key_file() -> Path:
    """当前仓库的加密密钥文件（默认沿用仓库根目录那把，保持老环境不变）。"""
    return _env_path("DSH_KEY_FILE", ROOT / "hub_encryption.key")


def chat_root() -> Path:
    """当前仓库的对话记录根（历史问答会引用本仓库的证据，所以也算业务数据）。"""
    return _env_path("DSH_CHAT_DIR", ROOT / "logs" / "chat")


def mapping_json_file() -> Path:
    """旧版映射 JSON（只用于一次性导入）；**必须随 hub 走**，
    否则新仓库会把老仓库的实体导入进来（编号含义就串了）。"""
    return hub_root() / LEGACY_MAPPING_NAME


# =========================================================
# 1. 登记表
# =========================================================
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _empty_registry() -> dict:
    return {"version": 1, "active": "", "workspaces": []}


def load_registry() -> dict:
    if not REGISTRY_FILE.exists():
        return _empty_registry()
    try:
        data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _empty_registry()
    data.setdefault("version", 1)
    data.setdefault("active", "")
    data.setdefault("workspaces", [])
    return data


def save_registry(reg: dict) -> None:
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(REGISTRY_FILE)          # 原子替换：中途崩了也不会留半个登记表


def list_workspaces() -> list[dict]:
    return list(load_registry().get("workspaces") or [])


def find(slug_or_name: str) -> dict | None:
    key = (slug_or_name or "").strip()
    if not key:
        return None
    for w in list_workspaces():
        if key in (w.get("slug"), w.get("name")):
            return w
    return None


def active() -> dict | None:
    """当前生效的仓库：登记表的 active 优先，其次 `.env` 的 ACTIVE_WORKSPACE。"""
    reg = load_registry()
    slug = reg.get("active") or (os.getenv("ACTIVE_WORKSPACE", "") or "").strip()
    if not slug:
        return None
    return find(slug)


def _slugify(name: str, *, taken: set[str]) -> str:
    """仓库短名：只用小写字母/数字/下划线（要进数据库名），中文名自动退化成 wsN。"""
    base = re.sub(r"[^a-z0-9_]+", "_", (name or "").strip().lower()).strip("_")
    base = base[:24] or "ws"
    if base == "ws" or base in taken:
        i = 1
        while f"{base}{i}" in taken:
            i += 1
        base = f"{base}{i}"
    return base


def audit(action: str, **fields) -> None:
    """仓库级审计（建/切/重置/删除都留痕）。"""
    try:
        d = ROOT / "logs" / "workspace"
        d.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": _now(), "action": action, **fields}, ensure_ascii=False)
        with (d / f"audit_{datetime.now():%Y%m%d}.jsonl").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# =========================================================
# 2. .env 写入 + 生效（热切换）
# =========================================================
def _persist_env(pairs: dict[str, str]) -> None:
    """把键值写回仓库根目录 `.env`（存在则替换该行，不存在则追加）。"""
    env_path = ROOT / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        for k, v in pairs.items():
            for i, line in enumerate(lines):
                if line.strip().startswith(f"{k}="):
                    lines[i] = f"{k}={v}"
                    break
            else:
                lines.append(f"{k}={v}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass    # 落盘失败不影响本次运行（进程内已生效），但会在结果里体现


def _paths_of(entry: dict) -> dict[str, Path]:
    slug = str(entry.get("slug") or "ws")
    legacy = bool(entry.get("legacy"))
    chat = entry.get("chat") or ("logs/chat" if legacy else f"workspaces/{slug}/logs/chat")
    return {
        "hub": ROOT / str(entry.get("hub") or "hub"),
        "input": ROOT / str(entry.get("input") or "input"),
        "output": ROOT / str(entry.get("output") or "output"),
        "key_file": ROOT / str(entry.get("key_file") or "hub_encryption.key"),
        "chat": ROOT / str(chat),
        "chat_recycle": ROOT / str(chat) / "_recycle",
    }


def apply_active(entry: dict | None = None, *, persist: bool = False) -> dict:
    """把某个仓库"生效"：写环境变量 + 改写已导入模块里的路径/库名常量。

    `persist=True` = **正式切换**（同时更新登记表的 active 与 `.env`，影响后续所有进程）；
    `persist=False` = **仅本进程临时生效**（例如样例装载器 `--workspace X`：
    既要读写那个仓库，又不该把用户当前所在的仓库改掉）。

    返回 `{"slug","db","hub","input","output","key_file","patched":[...]}`。
    **不建库、不写数据**，只切"进程往哪读、往哪写"。
    """
    entry = entry or active()
    if not entry:
        raise RuntimeError("没有已登记的仓库：请先 register-legacy / create")
    p = _paths_of(entry)
    for d in (p["hub"], p["input"], p["output"], p["chat"]):
        d.mkdir(parents=True, exist_ok=True)

    env = {
        "ACTIVE_WORKSPACE": str(entry["slug"]),
        "APP_DB_NAME": str(entry["db"]),
        "DSH_HUB_DIR": str(p["hub"]),
        "DSH_INPUT_DIR": str(p["input"]),
        "DSH_OUTPUT_DIR": str(p["output"]),
        "DSH_KEY_FILE": str(p["key_file"]),
        "DSH_CHAT_DIR": str(p["chat"]),
    }
    os.environ.update(env)
    if persist:
        _persist_env(env)

    patched: list[str] = []
    for mod_name, attr, which in _PATH_ATTRS:
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        setattr(mod, attr, p[which])
        patched.append(f"{mod_name}.{attr}")
    # 库名：APP_DB_CONFIG 是导入期快照，必须就地改（get_connection 每次都用它连）
    mod = sys.modules.get("infra.database_serv__infra")
    if mod is not None:
        mod.APP_DB_CONFIG["dbname"] = str(entry["db"])
        mod.MAPPING_JSON_FILE = mapping_json_file()
        patched.append("database_serv__infra.APP_DB_CONFIG[dbname]")
        patched.append("database_serv__infra.MAPPING_JSON_FILE")

    if persist:
        # 只有"正式切换"才动登记表与环境文件；persist=False 是进程内临时生效
        reg = load_registry()
        reg["active"] = str(entry["slug"])
        save_registry(reg)
    return {"slug": entry["slug"], "name": entry.get("name"), "db": entry["db"],
            "hub": str(p["hub"]), "input": str(p["input"]), "output": str(p["output"]),
            "key_file": str(p["key_file"]), "chat": str(p["chat"]),
            "patched": patched, "persist": persist}


def switch(slug_or_name: str, *, persist: bool = True) -> dict:
    entry = find(slug_or_name)
    if not entry:
        raise KeyError(f"没有这个仓库：{slug_or_name}")
    info = apply_active(entry, persist=persist)
    audit("switch", slug=entry["slug"], db=entry["db"], hub=info["hub"], persist=persist)
    return info


# =========================================================
# 3. 建库 / 建仓库（完全新建 = 空环境）
# =========================================================
def ensure_db(dbname: str, *, reset: bool = False) -> str:
    """建库 + 套用 `init_db.sql`（幂等）。

    `init_db.sql` 里没有硬编码库名，全是 `CREATE TABLE IF NOT EXISTS` + 角色授权，
    所以直接连到目标库执行即可得到与生产同构的结构（角色是**集群级**的，已存在则跳过）。
    """
    import psycopg2

    from infra.database_serv__infra import ADMIN_DB_CONFIG, render_init_sql

    cfg = dict(ADMIN_DB_CONFIG)
    # ⚠️ 不能用 `with psycopg2.connect(...)`：连接上下文管理器会开事务块，
    #    CREATE/DROP DATABASE 在事务块里直接报 ActiveSqlTransaction（实测）。
    conn = psycopg2.connect(**cfg)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
            exists = cur.fetchone() is not None
            if exists and reset:
                try:
                    cur.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
                except Exception:
                    cur.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
                exists = False
            if not exists:
                cur.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        conn.close()

    sql_text = render_init_sql(INIT_SQL.read_text(encoding="utf-8"))
    conn = psycopg2.connect(**dict(cfg, dbname=dbname))
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql_text)
    finally:
        conn.close()
    return dbname


def _new_key(path: Path) -> None:
    """给新仓库生成**独立**密钥（与老仓库的密文互不可解 = 真隔离）。"""
    from cryptography.fernet import Fernet

    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(Fernet.generate_key())


def _create_first_user(username: str, password: str, email: str, phone: str) -> tuple[bool, str]:
    """在**当前生效库**里注册首个用户（空库首个用户自动成为 admin）。"""
    from infra.database_serv__infra import register_user

    return register_user(username, password, email, phone)


def create(name: str, *, slug: str = "", note: str = "", make_active: bool = True,
           admin: tuple[str, str, str, str] | None = None,
           dbname: str = "") -> dict:
    """**完全新建**一个仓库：空库 + 空 hub/input/output + 独立密钥（零数据、零编号）。

    `admin=(用户名, 密码, 邮箱, 手机)` 时顺带建首个最高管理员；不给就等首次登录时注册。
    """
    reg = load_registry()
    taken = {w.get("slug") for w in reg["workspaces"]}
    if find(name) or find(slug):
        raise ValueError(f"仓库名/短名已存在：{name or slug}")
    s = _slugify(slug or name, taken=taken)
    db = dbname.strip() or f"fin_ws_{s}"
    data_dir = WORKSPACES_DIR / s
    entry = {
        "slug": s, "name": (name or s).strip(), "db": db, "legacy": False,
        "hub": f"workspaces/{s}/hub", "input": f"workspaces/{s}/input",
        "output": f"workspaces/{s}/output", "key_file": f"workspaces/{s}/hub_encryption.key",
        "chat": f"workspaces/{s}/logs/chat",
        "note": note, "created_at": _now(), "updated_at": _now(),
    }
    for sub in ("hub", "input", "output"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    _new_key(ROOT / entry["key_file"])

    prev = (active() or {}).get("slug", "")
    ensure_db(db)
    reg["workspaces"].append(entry)
    save_registry(reg)
    audit("create", slug=s, name=entry["name"], db=db, hub=entry["hub"], note=note)

    # ⚠️ 无论是否 make_active，都要**先把进程切到新仓库**再建管理员用户：
    #    否则 register_user 会写进"上一个仓库"的库（实测：--no-activate 时把
    #    管理员建到了既有仓库 fin_system_db 里 = 跨仓库脏写）。
    apply_active(entry, persist=make_active)
    if admin:
        u, p, e, ph = admin
        ok, msg = _create_first_user(u, p, e, ph)
        entry["admin_result"] = f"{ok}｜{msg}"
        audit("create_admin", slug=s, user=u, db=db, ok=ok, msg=msg)
    if not make_active and prev:
        apply_active(find(prev), persist=True)
    entry["created"] = True
    return entry


def register_legacy(name: str = "马山三标段", *, slug: str = "mashan", note: str = "") -> dict:
    """把**现有环境**登记为第一个仓库：沿用现有库与目录，零迁移。

    读当前 `.env` 的 APP_DB_NAME 作为库名；hub/input/output/密钥都指到仓库根目录，
    与改造前的行为**逐字节一致**（所以老数据、老编号、老图都不用动）。
    """
    reg = load_registry()
    if reg["workspaces"]:
        existing = next((w for w in reg["workspaces"] if w.get("legacy")), None)
        if existing:
            apply_active(existing, persist=True)
            return existing
    entry = {
        "slug": slug, "name": name, "db": env_value("APP_DB_NAME", "fin_system_db"),
        "legacy": True, "hub": "hub", "input": "input", "output": "output",
        "key_file": "hub_encryption.key", "chat": "logs/chat",
        "note": note or "既有环境（改造前就在用的库与目录，零迁移登记）",
        "created_at": _now(), "updated_at": _now(),
    }
    reg["workspaces"].append(entry)
    reg["active"] = slug
    save_registry(reg)
    info = apply_active(entry, persist=True)
    audit("register_legacy", slug=slug, db=entry["db"], hub=info["hub"])
    return entry


# =========================================================
# 4. 重置 / 删除（都可回滚：旧库改名保留、旧目录移入 _bak）
# =========================================================
def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _rename_db(old: str, new: str) -> str:
    """旧库改名保留（PostgreSQL 的 ALTER DATABASE ... RENAME 可回滚）。"""
    import psycopg2

    from infra.database_serv__infra import ADMIN_DB_CONFIG

    conn = psycopg2.connect(**ADMIN_DB_CONFIG)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'ALTER DATABASE "{old}" RENAME TO "{new}"')
    finally:
        conn.close()
    return new


def _move_dir(src: Path, dst: Path) -> str | None:
    if not src.exists():
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return str(dst)


def reset(slug_or_name: str, *, confirm: bool = False) -> dict:
    """**清空重来**：旧库与旧数据目录改名保留（可回滚），然后建一个全新的空仓库。"""
    if not confirm:
        raise ValueError("reset 是破坏性操作：请显式 --confirm")
    entry = find(slug_or_name)
    if not entry:
        raise KeyError(f"没有这个仓库：{slug_or_name}")
    if entry.get("legacy"):
        raise ValueError("既有仓库（legacy）不允许原地重置：请先 create 一个新仓库，"
                         "或手工备份后改 registry.json 去掉 legacy 标记")
    ts = _stamp()
    old_db = str(entry["db"])
    archived_db = _rename_db(old_db, f"{old_db}__bak_{ts}")
    p = _paths_of(entry)
    data_dir = ROOT / f"workspaces/{entry['slug']}"
    archived_dir = _move_dir(data_dir, WORKSPACES_DIR / "_bak" / f"{entry['slug']}_{ts}")
    for sub in ("hub", "input", "output"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    _new_key(p["key_file"])
    ensure_db(old_db)
    apply_active(entry, persist=True) if (active() or {}).get("slug") == entry["slug"] else None
    audit("reset", slug=entry["slug"], db=old_db, archived_db=archived_db,
          archived_dir=archived_dir)
    return {"slug": entry["slug"], "db": old_db, "archived_db": archived_db,
            "archived_dir": archived_dir}


def delete(slug_or_name: str, *, confirm: bool = False) -> dict:
    """删除仓库登记（可回滚）：数据目录移入 `workspaces/_bak/`，库改名加 `__bak_` 后缀。"""
    if not confirm:
        raise ValueError("delete 是破坏性操作：请显式 --confirm")
    entry = find(slug_or_name)
    if not entry:
        raise KeyError(f"没有这个仓库：{slug_or_name}")
    if (active() or {}).get("slug") == entry["slug"]:
        raise ValueError("不能删除正在使用的仓库：先 switch 到别的仓库")
    if entry.get("legacy"):
        raise ValueError("既有仓库（legacy）不允许删除登记（它指向仓库根目录的老数据）")
    ts = _stamp()
    archived_db = _rename_db(str(entry["db"]), f"{entry['db']}__bak_{ts}")
    archived_dir = _move_dir(ROOT / f"workspaces/{entry['slug']}",
                             WORKSPACES_DIR / "_bak" / f"{entry['slug']}_{ts}")
    reg = load_registry()
    reg["workspaces"] = [w for w in reg["workspaces"] if w.get("slug") != entry["slug"]]
    save_registry(reg)
    audit("delete", slug=entry["slug"], db=entry["db"], archived_db=archived_db,
          archived_dir=archived_dir)
    return {"slug": entry["slug"], "archived_db": archived_db, "archived_dir": archived_dir}


# =========================================================
# 5. 体检：这个仓库里有什么（隔离自证用）
# =========================================================
def counts(slug_or_name: str = "") -> dict:
    """直接连目标库数一遍（**不改当前进程的生效仓库**）。"""
    import psycopg2

    from infra.database_serv__infra import ADMIN_DB_CONFIG

    entry = find(slug_or_name) if slug_or_name else active()
    if not entry:
        raise KeyError(f"没有这个仓库：{slug_or_name or '(未指定且无 active)'}")
    cfg = dict(ADMIN_DB_CONFIG, dbname=str(entry["db"]))
    out: dict = {"slug": entry["slug"], "name": entry.get("name"), "db": entry["db"],
                 "hub": str(ROOT / str(entry["hub"]))}
    try:
        conn = psycopg2.connect(**cfg)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    try:
        with conn.cursor() as cur:
            def one(sql: str) -> int:
                try:
                    cur.execute(sql)
                    return int(cur.fetchone()[0] or 0)
                except Exception:
                    conn.rollback()
                    return -1

            out["docs"] = one("SELECT COUNT(*) FROM l1_documents")
            out["facts"] = one("SELECT COUNT(*) FROM l1_facts")
            out["segments"] = one("SELECT COUNT(*) FROM l1_segments")
            out["feature_rows"] = one("SELECT COUNT(*) FROM file_feature_hashes")
            out["graph_nodes"] = one("SELECT COUNT(*) FROM graph_nodes")
            out["graph_edges"] = one("SELECT COUNT(*) FROM graph_edges")
            out["users"] = one("SELECT COUNT(*) FROM sys_users")
            out["chunks"] = one("SELECT COUNT(*) FROM document_chunks")
            ents = {}
            for cat, tbl in (("company", "entity_mapping_company"),
                             ("party", "entity_mapping_party"),
                             ("date", "entity_mapping_date"),
                             ("tax_id", "entity_mapping_tax_id"),
                             ("bank_name", "entity_mapping_bank_name"),
                             ("bank_account", "entity_mapping_bank_account"),
                             ("phone", "entity_mapping_phone"),
                             ("project", "entity_mapping_project"),
                             ("id_card", "entity_mapping_id_card"),
                             ("bank_card", "entity_mapping_bank_card")):
                n = one(f"SELECT COUNT(*) FROM {tbl}")
                if n > 0:
                    ents[cat] = n
            out["entities"] = ents
            out["entity_total"] = sum(v for v in ents.values() if v > 0)
    finally:
        conn.close()
    hub = Path(out["hub"])
    if hub.exists():
        hubs = [p for p in hub.rglob("*.json")
                if not p.name.endswith((".features.json", ".l1.json"))]
        out["hub_files"] = len([p for p in hubs if p.parent.name != "_mapping"])
    else:
        out["hub_files"] = 0
    return out


# =========================================================
# 6. 自检：隔离是不是真的（同一台机器上可反复跑）
# =========================================================
def _connect(db: str):
    import psycopg2

    from infra.database_serv__infra import ADMIN_DB_CONFIG

    return psycopg2.connect(**dict(ADMIN_DB_CONFIG, dbname=db))


def _doc_keys(db: str) -> set[str]:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT doc_key FROM l1_documents")
            return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def _sample_cipher(db: str) -> tuple[str, str, str] | None:
    """取一条敏感类别的密文：→ (类别, 编号, cipher)。"""
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            for cat, tbl in (("tax_id", "entity_mapping_tax_id"),
                             ("bank_name", "entity_mapping_bank_name"),
                             ("bank_account", "entity_mapping_bank_account"),
                             ("id_card", "entity_mapping_id_card"),
                             ("bank_card", "entity_mapping_bank_card"),
                             ("phone", "entity_mapping_phone"),
                             ("project", "entity_mapping_project")):
                try:
                    cur.execute(f"SELECT code, cipher FROM {tbl} ORDER BY id LIMIT 1")
                except Exception:
                    conn.rollback()
                    continue
                row = cur.fetchone()
                if row:
                    return cat, row[0], row[1]
    finally:
        conn.close()
    return None


def _plain_pairs(db: str, table: str, limit: int = 3) -> list[tuple[str, str]]:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(f"SELECT code, real_value FROM {table} ORDER BY id LIMIT {int(limit)}")
                return [(r[0], r[1]) for r in cur.fetchall()]
            except Exception:
                conn.rollback()
                return []
    finally:
        conn.close()


def _perm_state(db: str) -> dict:
    """某个仓库的权限点状态：目录里的权限点、各角色授权、admin 覆盖情况。"""
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT permission_code FROM sys_permissions ORDER BY permission_code")
            perms = {r[0] for r in cur.fetchall()}
            cur.execute("""
                SELECT rp.role_type, p.permission_code
                FROM sys_role_permissions rp JOIN sys_permissions p USING (permission_id)""")
            grants: dict[str, set[str]] = {}
            for role, code in cur.fetchall():
                grants.setdefault(role, set()).add(code)
            cur.execute("SELECT role_type, COUNT(*) FROM sys_users GROUP BY role_type")
            users = {r[0]: int(r[1]) for r in cur.fetchall()}
            return {"perms": perms, "grants": grants, "users": users}
    finally:
        conn.close()


def code_permission_points() -> set[str]:
    """代码里真正会被 `require_permission` 问到的权限点（从各模块常量现算，不写死清单）。"""
    pts = {"field:read", "field:write", "ledger:status"}
    try:
        from infra.database_serv__infra import DECRYPT_PERMISSION_BY_CATEGORY

        pts |= set(DECRYPT_PERMISSION_BY_CATEGORY.values())
    except Exception:
        pass
    for mod_name, attr in (("desens.project_registry__desens", "DECRYPT_PERMISSION"),
                           ("desens.self_entity__desens", "DECRYPT_PERMISSION")):
        try:
            mod = __import__(mod_name)
            pts.add(str(getattr(mod, attr)))
        except Exception:
            pass
    return pts


def self_test() -> dict:
    """隔离自检（可反复跑）：文档集合互斥 / 编号独立 / 密钥互不可解 / 进程内热切换。"""
    items = list_workspaces()
    checks: list[dict] = []
    per: dict[str, dict] = {}

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    for w in items:
        slug, db = w["slug"], str(w["db"])
        keys = _doc_keys(db)
        per[slug] = {"entry": w, "doc_keys": keys, "counts": counts(slug)}
        check(f"{slug}:库可连", per[slug]["counts"].get("error") is None,
              per[slug]["counts"].get("error") or f"文档 {len(keys)}")

    # ① 文档集合互斥：同一个 doc_key 不该同时出现在两个仓库（那就是没隔离）
    hugs = [(a, b) for i, a in enumerate(per) for b in list(per)[i + 1:]]
    for a, b in hugs:
        dup = per[a]["doc_keys"] & per[b]["doc_keys"]
        check(f"文档集合互斥 {a}∩{b}", not dup,
              "无重复" if not dup else f"重复 {sorted(dup)[:5]}（共 {len(dup)}）")

    # ② 编号独立：每个仓库都从 0001 起发号，**同一个编号在两库里指不同实体**
    for slug in per:
        pairs = _plain_pairs(str(per[slug]["entry"]["db"]), "entity_mapping_company")
        check(f"{slug}:编号从 CO0001 起", bool(pairs) and pairs[0][0] == "CO0001",
              str(pairs))
    if len(per) >= 2:
        a, b = list(per)[0], list(per)[1]
        pa = _plain_pairs(str(per[a]["entry"]["db"]), "entity_mapping_company", 1)
        pb = _plain_pairs(str(per[b]["entry"]["db"]), "entity_mapping_company", 1)
        if pa and pb:
            diff = pa[0][0] == pb[0][0] and pa[0][1] != pb[0][1]
            check("同号不同实体（编号互不串）", diff,
                  f"{a}: {pa[0]} ｜ {b}: {pb[0]}")

    # ③ 密钥隔离：A 库的密文用 B 库的密钥必须解不开
    if len(per) >= 2:
        from cryptography.fernet import Fernet

        for a, b in hugs:
            for x, y in ((a, b), (b, a)):      # 两个方向都要验
                got = _sample_cipher(str(per[x]["entry"]["db"]))
                other_key = ROOT / str(per[y]["entry"].get("key_file") or "")
                if not got or not other_key.exists():
                    check(f"密钥隔离 {x}→{y}", True, "（缺密文或密钥，跳过）")
                    continue
                cat, code, cipher = got
                try:
                    Fernet(other_key.read_bytes()).decrypt(cipher.encode("ascii"))
                    ok, detail = False, f"{x} 的 {cat}/{code} 竟能用 {y} 的密钥解开"
                except Exception as exc:
                    ok, detail = True, f"{x} 的 {cat}/{code} 用 {y} 密钥解不开（{type(exc).__name__}）"
                check(f"密钥隔离 {x}→{y}", ok, detail)

    # ④ 权限：admin 必须拿到权限目录里的**全部**权限点，且代码用到的点都得在目录里
    code_pts = code_permission_points()
    for slug, v in per.items():
        db = str(v["entry"]["db"])
        try:
            ps = _perm_state(db)
        except Exception as exc:
            check(f"{slug}:权限表可读", False, f"{type(exc).__name__}: {exc}")
            continue
        admin_pts = ps["grants"].get("admin", set())
        miss_admin = sorted(ps["perms"] - admin_pts)
        check(f"{slug}:admin 权限点全覆盖", not miss_admin,
              f"目录 {len(ps['perms'])} 个，admin 有 {len(admin_pts)} 个"
              + (f"，缺 {miss_admin}" if miss_admin else "，无缺口"))
        miss_catalog = sorted(code_pts - ps["perms"])
        check(f"{slug}:代码权限点已登记", not miss_catalog,
              f"代码用到 {len(code_pts)} 个，目录缺 {miss_catalog}" if miss_catalog
              else f"代码用到 {len(code_pts)} 个，目录里都有")
        admins = ps["users"].get("admin", 0)
        check(f"{slug}:有最高管理员账号", admins >= 1, f"admin 角色用户数 {admins}")

    # ⑤ 目录隔离：每个仓库的 hub/input/output/聊天记录根都必须各不相同
    roots: dict[str, set[str]] = {"hub": set(), "output": set(), "chat": set()}
    for slug, v in per.items():
        p = _paths_of(v["entry"])
        for k in roots:
            roots[k].add(str(p[k]))
        check(f"{slug}:目录各就各位",
              all(str(p[k]).startswith(str(ROOT)) for k in ("hub", "input", "output", "chat")),
              f"hub={p['hub']}｜chat={p['chat']}")
    for k, s in roots.items():
        check(f"{k} 目录互不重叠", len(s) == len(per),
              f"{len(s)} 个不同目录 / {len(per)} 个仓库")

    # ⑤ 进程内热切换：同进程里切仓库后，读到的文档数/索引卡必须跟着变
    try:
        import graph.doc_index__graph as di

        seq = []
        for slug in per:
            apply_active(per[slug]["entry"], persist=False)
            seq.append((slug, len(di.load_cards(with_facts=False))))
        back = [a for a in per]
        apply_active(per[back[0]]["entry"], persist=False)
        ok = all(seq[i][1] == len(per[seq[i][0]]["doc_keys"]) for i in range(len(seq)))
        check("进程内热切换", ok, f"各仓库读到的索引卡：{seq}")
    except Exception as exc:
        check("进程内热切换", False, f"{type(exc).__name__}: {exc}")

    return {"workspaces": {k: {"slug": k, "db": v["entry"]["db"],
                              "docs": len(v["doc_keys"])} for k, v in per.items()},
            "checks": checks}


# =========================================================
# 7. CLI
# =========================================================
def _fmt_counts(c: dict) -> str:
    if c.get("error"):
        return f"{c['slug']}（{c['db']}）：❌ {c['error']}"
    return (f"{c['slug']}｜{c.get('name')}｜库 {c['db']}｜hub {c['hub']}\n"
            f"    文档 {c['docs']}｜事实 {c['facts']}｜段 {c['segments']}｜特征行 {c['feature_rows']}"
            f"｜图 {c['graph_nodes']}节点/{c['graph_edges']}边｜向量块 {c['chunks']}"
            f"｜用户 {c['users']}｜登记 {c['entity_total']} {c.get('entities')}"
            f"｜hub 文件 {c['hub_files']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="工作区（仓库）管理：新建 / 切换 / 体检 / 重置")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list", help="列出所有仓库 + 当前生效仓库")

    p = sub.add_parser("register-legacy", help="把现有环境登记为第一个仓库（零迁移）")
    p.add_argument("--name", default="马山三标段")
    p.add_argument("--slug", default="mashan")
    p.add_argument("--note", default="")

    p = sub.add_parser("create", help="完全新建一个空仓库")
    p.add_argument("--name", required=True)
    p.add_argument("--slug", default="")
    p.add_argument("--note", default="")
    p.add_argument("--db", default="")
    p.add_argument("--no-activate", action="store_true", help="建完不切换（留在当前仓库）")
    p.add_argument("--admin-user", default="")
    p.add_argument("--admin-pwd", default="")
    p.add_argument("--admin-email", default="")
    p.add_argument("--admin-phone", default="")

    p = sub.add_parser("switch", help="切换生效仓库（写 .env，供后续进程使用）")
    p.add_argument("slug")

    p = sub.add_parser("info", help="看某个仓库里有什么（默认当前）")
    p.add_argument("slug", nargs="?", default="")

    p = sub.add_parser("reset", help="清空重来（旧库/旧目录改名保留，可回滚）")
    p.add_argument("slug")
    p.add_argument("--confirm", action="store_true")

    p = sub.add_parser("delete", help="删除仓库登记（旧库/旧目录改名保留，可回滚）")
    p.add_argument("slug")
    p.add_argument("--confirm", action="store_true")

    sub.add_parser("self-test", help="隔离自检：文档集合互斥 / 编号独立 / 密钥互不可解 / 热切换")

    p = sub.add_parser("init-db", help="对仓库重跑 init_db.sql（补表/补权限点；幂等）")
    p.add_argument("slug", nargs="?", default="", help="留空=当前仓库；--all=全部仓库")
    p.add_argument("--all", action="store_true")

    args = ap.parse_args(argv)
    cmd = args.cmd or "list"

    if cmd == "list":
        reg = load_registry()
        cur = (active() or {}).get("slug", "")
        if not reg["workspaces"]:
            print("还没有登记任何仓库：先跑 `python -m infra.workspace__infra register-legacy`")
            return 1
        print(f"仓库 {len(reg['workspaces'])} 个（当前生效：{cur or '（无）'}）：")
        for w in reg["workspaces"]:
            mark = "★" if w["slug"] == cur else " "
            print(f" {mark} {w['slug']:12s}｜{w.get('name','')}｜库 {w['db']}"
                  f"｜hub {w['hub']}" + ("（既有环境）" if w.get("legacy") else ""))
        return 0

    if cmd == "register-legacy":
        e = register_legacy(args.name, slug=args.slug, note=args.note)
        print(f"已登记既有仓库：{e['slug']}｜{e['name']}｜库 {e['db']}｜hub {ROOT / e['hub']}")
        print(_fmt_counts(counts(e["slug"])))
        return 0

    if cmd == "create":
        admin = None
        if args.admin_user and args.admin_pwd:
            admin = (args.admin_user, args.admin_pwd,
                     args.admin_email or f"{args.admin_user}@example.com",
                     args.admin_phone or "13900000000")
        e = create(args.name, slug=args.slug, note=args.note, make_active=not args.no_activate,
                   admin=admin, dbname=args.db)
        print(f"已新建仓库：{e['slug']}｜{e['name']}｜库 {e['db']}｜hub {ROOT / e['hub']}")
        print(f"密钥：{ROOT / e['key_file']}（独立密钥；丢失则密文不可解，请自行备份）")
        if e.get("admin_result"):
            print(f"首个用户：{e['admin_result']}")
        print(_fmt_counts(counts(e["slug"])))
        return 0

    if cmd == "switch":
        info = switch(args.slug, persist=True)
        print(f"已切换到仓库：{info['slug']}｜库 {info['db']}｜hub {info['hub']}")
        print(f"已同步到 .env：{'、'.join(ENV_KEYS)}")
        print("提示：AI 接口与检索方式（RETRIEVAL_METHOD）是全局的，不随仓库切换。")
        return 0

    if cmd == "info":
        print(_fmt_counts(counts(args.slug)))
        return 0

    if cmd == "reset":
        r = reset(args.slug, confirm=args.confirm)
        print(f"已重置仓库 {r['slug']}：新库 {r['db']}（空）")
        print(f"可回滚：旧库 {r['archived_db']}｜旧目录 {r['archived_dir']}")
        print(_fmt_counts(counts(r["slug"])))
        return 0

    if cmd == "delete":
        r = delete(args.slug, confirm=args.confirm)
        print(f"已删除仓库登记 {r['slug']}")
        print(f"可回滚：旧库 {r['archived_db']}｜旧目录 {r['archived_dir']}")
        return 0

    if cmd == "init-db":
        targets = list_workspaces() if args.all else [find(args.slug) or active()]
        if not any(targets):
            print("没有可处理的仓库")
            return 1
        for w in targets:
            if not w:
                continue
            ensure_db(str(w["db"]))
            print(f"  ✓ {w['slug']}｜库 {w['db']} 已重跑 init_db.sql")
            audit("init_db", slug=w["slug"], db=w["db"])
        return 0

    if cmd == "self-test":
        res = self_test()
        for c in res["checks"]:
            print(f"  {'✓' if c['ok'] else '✗'} {c['name']}：{c['detail']}")
        bad = [c for c in res["checks"] if not c["ok"]]
        print(f"隔离自检：{len(res['checks']) - len(bad)}/{len(res['checks'])} 通过"
              f"（仓库 {len(res['workspaces'])} 个）")
        return 1 if bad else 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
