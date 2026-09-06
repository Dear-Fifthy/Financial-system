from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import uuid
import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
from cryptography.fernet import Fernet

# =========================================================
# 1. 明确区分：超级管理员认证 与 应用角色认证
# =========================================================

import os
from pathlib import Path
from dotenv import load_dotenv

# 自动加载项目根目录下的 .env 文件
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

# 从环境变量中读取配置（若获取不到则使用安全默认值）
POSTGRES_ADMIN_PASSWORD = os.getenv("POSTGRES_ADMIN_PASSWORD", "")

ADMIN_DB_CONFIG = {
    "dbname": os.getenv("DB_ADMIN_NAME", "postgres"),
    "user": os.getenv("DB_ADMIN_USER", "postgres"),
    "password": POSTGRES_ADMIN_PASSWORD,
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "5432"),
}

APP_DB_CONFIG = {
    "dbname": os.getenv("APP_DB_NAME", "fin_system_db"),
    "user": os.getenv("APP_DB_USER", "finance_app_role"),
    "password": os.getenv("APP_DB_PASS", "finance_secret_123"),
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "5432"),
}


# =========================================================
# 1.5 加解密工具（身份证/银行卡等需要严格双向控制的字段）
# 从 hub_pipeline 平移而来，加密方法保持原样：Fernet 对称加密，
# 同一个 hub_encryption.key 即可解密还原（已实测往返一致，
# 且能拒绝伪造密文——带 HMAC 认证，不是"无盐裸加密"）。
# =========================================================
KEY_FILE = Path(__file__).resolve().parent / "hub_encryption.key"  # ⚠️ 开发期占位，已 gitignore


def _load_or_create_key() -> bytes:
    """读取本地密钥文件；不存在则生成新密钥（开发期占位方案）。"""
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes()
    key = Fernet.generate_key()
    KEY_FILE.write_bytes(key)
    print(
        f"⚠️ 首次运行，已在本地生成加密密钥：{KEY_FILE}\n"
        f"   这只是开发阶段的占位方案，上线前务必换成从密钥管理服务/环境变量读取，"
        f"并把 {KEY_FILE.name} 加进 .gitignore，不要和数据库放在一起。"
    )
    return key


_FERNET = Fernet(_load_or_create_key())


def encrypt_value(value: str) -> str:
    """明文 -> Fernet 密文（ASCII 字符串，可直接存库）。"""
    return _FERNET.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_value(cipher_text: str) -> str:
    """Fernet 密文 -> 明文（同一把 key 才能解，失败会抛 InvalidToken）。"""
    return _FERNET.decrypt(cipher_text.encode("ascii")).decode("utf-8")


# =========================================================
# 2. 正确的认证与建表流程
# =========================================================
def init_db() -> tuple[bool, str]:
    """使用超级管理员认证，建立数据库、角色并执行 init_db.sql 赋权"""
    try:
        # A. 第一步认证：连入系统默认 postgres 库创建 fin_system_db
        conn_admin = psycopg2.connect(**ADMIN_DB_CONFIG)
        conn_admin.autocommit = True
        with conn_admin.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = 'fin_system_db';")
            if not cur.fetchone():
                cur.execute("CREATE DATABASE fin_system_db;")
        conn_admin.close()

        # B. 第二步认证：连入 fin_system_db 执行 init_db.sql
        sql_file = Path(__file__).parent / "init_db.sql"
        if not sql_file.exists():
            return False, f"找不到初始化文件: {sql_file.resolve()}"

        sql_script = sql_file.read_text(encoding="utf-8")

        admin_fin_config = ADMIN_DB_CONFIG.copy()
        admin_fin_config["dbname"] = "fin_system_db"

        with psycopg2.connect(**admin_fin_config) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(sql_script)

        print("✅ 数据库认证、角色创建及 init_db.sql 初始化成功！")
        return True, "数据库环境与角色创建成功！"

    except Exception as exc:
        err_msg = f"管理员认证或执行 SQL 失败: {exc}"
        print(f"❌ {err_msg}")
        return False, err_msg  # 👈 必须明确 return，不能只 print！


# =========================================================
# 3. 业务数据提交 (使用认证通过后的 finance_app_role)
# =========================================================
def get_connection():
    """获取应用专属数据库连接（数据读写用，权限受限）。"""
    return psycopg2.connect(**APP_DB_CONFIG)


def get_admin_connection():
    """获取"管理员 + 业务库"连接（表结构 DDL 用）。

    PostgreSQL 规定 ALTER TABLE 必须由表属主执行；业务表属主是 postgres
    管理员（init_db 时创建），而 finance_app_role 只有 DML 权限。
    因此"调整字段/重命名字段"这类 DDL 走本连接（管理员凭据来自 .env），
    业务层权限仍由 require_permission(field:write) 把关。
    """
    cfg = ADMIN_DB_CONFIG.copy()
    cfg["dbname"] = os.getenv("APP_DB_NAME", "fin_system_db")
    return psycopg2.connect(**cfg)


def hash_pwd(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def register_user(username: str, password: str, email: str, phone: str) -> tuple[bool, str]:
    email_regex = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
    phone_regex = r"^1[3-9]\d{9}$"

    if not re.match(email_regex, email):
        return False, "邮箱格式不正确！"
    if not re.match(phone_regex, phone):
        return False, "手机号码格式不正确（需为11位大陆手机号）！"

    first_admin = False  # 本次注册是否以"首个 admin"身份创建（供 except 分支安全引用）

    try:
        # 使用 finance_app_role 连接并执行 INSERT 提交
        with get_connection() as conn, conn.cursor() as cur:
            # 首个注册用户 = 最高管理员（admin）：仅当库中尚无人拥有 admin 角色时生效
            #（新装空库场景；并发竞态由 uq_sys_users_single_admin 部分唯一索引兜底，
            #  见 init_db.sql——同一时刻只允许一个 admin，失败者自动降级重试）。
            cur.execute("SELECT 1 FROM sys_users WHERE role_type = 'admin' LIMIT 1")
            first_admin = cur.fetchone() is None
            role = "admin" if first_admin else "finance_staff"
            cur.execute(
                "INSERT INTO sys_users (username, password_hash, email, phone, role_type) "
                "VALUES (%s, %s, %s, %s, %s)",
                (username, hash_pwd(password), email, phone, role),
            )
            conn.commit()  # 提交事务
            if first_admin:
                return True, "注册成功（首个用户已自动成为最高管理员 admin）！"
            return True, "注册成功！"
    except Exception as exc:
        # 并发"首个注册"竞态：两人同时通过"无 admin"检查，唯一索引只放行一人；
        # 冲突者以普通角色（finance_staff）自动重试一次。
        from psycopg2 import errors as _pg_errors

        if first_admin and isinstance(exc, _pg_errors.UniqueViolation):
            try:
                with get_connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO sys_users (username, password_hash, email, phone, role_type) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (username, hash_pwd(password), email, phone, "finance_staff"),
                    )
                    conn.commit()
                return True, "注册成功！"
            except Exception as exc2:
                return False, f"注册失败: {exc2}"
        return False, f"注册失败: {exc}"

def authenticate_user(username: str, password: str) -> tuple[bool, dict | str]:
    """用户登录验证。

    校验通过后刷新 last_login_at（供"用户管理"界面显示最近登录）。
    账号被停用（is_active=FALSE，注销账户/管理员停用）时拒绝登录并给出明确提示。
    """
    try:
        with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT user_id, username, role_type, is_active, last_login_at "
                "FROM sys_users WHERE username = %s AND password_hash = %s",
                (username, hash_pwd(password)),
            )
            user = cur.fetchone()
            if user:
                if not user["is_active"]:
                    return False, "账号已被停用，请联系最高管理员！"
                cur.execute(
                    "UPDATE sys_users SET last_login_at = NOW() WHERE user_id = %s",
                    (user["user_id"],),
                )
                conn.commit()
                return True, dict(user)
            return False, "用户名或密码错误！"
    except Exception as exc:
        return False, f"数据库连接异常: {exc}"


# =========================================================
# 4.5 用户与最高管理员管理 API（桌面端"用户管理"界面用）
# ---------------------------------------------------------
# 说明：FastAPI 阶段引入服务端会话后，这里的部分逻辑（在线状态/吊销会话）会
# 迁移到 user_sessions；当前桌面单机阶段的"用户状态"= 账号状态(正常/停用) +
# 最近登录时间 + 是否本机当前登录。
# 约束（服务端强制，UI 只负责展示与调用）：
#   · admin 单例（uq_sys_users_single_admin 索引）；
#   · 最高管理员不能直接注销/停用自己——必须先转让；
#   · 转让在同一事务内 旧主降级(financial_role) -> 新主提升(admin)；
#   · 注销/转让需本人密码二次确认。
# =========================================================
def _require_admin(user: dict | None) -> tuple[bool, str]:
    """仅最高管理员（admin）可执行操作的前置检查。"""
    if not user:
        return False, "未登录，无法执行该操作！"
    if user.get("role_type") != "admin":
        return False, "该操作仅最高管理员（admin）可执行！"
    return True, ""


def _verify_user_password(username: str, password: str) -> bool:
    """校验某用户密码是否正确（注销/转让的二次确认用）。"""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM sys_users WHERE username = %s AND password_hash = %s",
            (username, hash_pwd(password)),
        )
        return cur.fetchone() is not None


def api_list_users(user: dict | None) -> tuple[bool, list[dict] | str]:
    """最高管理员：列出全部用户（不含密码哈希）。

    每行：user_id/username/role_type/is_active/last_login_at/created_at。
    """
    ok, msg = _require_admin(user)
    if not ok:
        return False, msg
    try:
        with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT user_id, username, role_type, is_active, last_login_at, created_at "
                "FROM sys_users ORDER BY user_id"
            )
            return True, [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        return False, f"读取用户列表失败: {exc}"


def api_toggle_user_active(user: dict | None, target_username: str) -> tuple[bool, str]:
    """最高管理员：停用/启用指定账号（软停用，不删行）。

    约束：不能停用自己（最高管理员必须先转让才能交权）。
    """
    ok, msg = _require_admin(user)
    if not ok:
        return False, msg
    if not target_username or target_username == user.get("username"):
        return False, "不能停用当前登录的最高管理员账号；如需交权请先「转让最高管理员」！"
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT is_active FROM sys_users WHERE username = %s", (target_username,)
            )
            row = cur.fetchone()
            if not row:
                return False, f"用户「{target_username}」不存在！"
            new_val = not row[0]
            cur.execute(
                "UPDATE sys_users SET is_active = %s WHERE username = %s",
                (new_val, target_username),
            )
            conn.commit()
            action = "启用" if new_val else "停用"
            return True, f"已{action}用户「{target_username}」！"
    except Exception as exc:
        return False, f"操作失败: {exc}"


def api_transfer_admin(
    user: dict | None, target_username: str, password: str
) -> tuple[bool, str]:
    """最高管理员转让（原子事务）：目标升为 admin，本人降为 financial_role。

    校验：本人确为 admin；目标存在、已启用、非本人、非 admin；
    需输入本人密码二次确认。成功后调用方（桌面端）应退出登录。
    事务顺序：先本人降级（admin 数 → 0）再目标升级（→1），全程满足单例索引。
    """
    ok, msg = _require_admin(user)
    if not ok:
        return False, msg
    if not target_username or target_username == user.get("username"):
        return False, "不能转让给自己！请选择另一名用户。"
    if not _verify_user_password(user.get("username", ""), password):
        return False, "密码错误，转让已取消！"
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT role_type, is_active FROM sys_users WHERE username = %s",
                (target_username,),
            )
            row = cur.fetchone()
            if not row:
                return False, f"用户「{target_username}」不存在！"
            if row[0] == "admin":
                return False, f"「{target_username}」已是最高管理员，无需转让！"
            if not row[1]:
                return False, f"「{target_username}」已被停用，请先启用再转让！"
            # 顺序：旧主降级 -> 新主提升（单例索引全程约束 ≤1 个 admin）
            cur.execute(
                "UPDATE sys_users SET role_type = 'financial_role' WHERE user_id = %s",
                (user["user_id"],),
            )
            cur.execute(
                "UPDATE sys_users SET role_type = 'admin' WHERE username = %s",
                (target_username,),
            )
            conn.commit()
            return True, f"最高管理员已转让给「{target_username}」，请重新登录！"
    except Exception as exc:
        return False, f"转让失败: {exc}"


def api_deactivate_self(user: dict | None, password: str) -> tuple[bool, str]:
    """当前用户注销自己的账号（软停用，is_active=FALSE）。

    最高管理员不可直接注销——必须先通过 api_transfer_admin 转让最高管理员。
    """
    if not user:
        return False, "未登录，无法执行该操作！"
    username = user.get("username", "")
    if user.get("role_type") == "admin":
        return False, "最高管理员不能直接注销账号；请先转让最高管理员后再注销！"
    if not _verify_user_password(username, password):
        return False, "密码错误，注销已取消！"
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE sys_users SET is_active = FALSE WHERE username = %s", (username,)
            )
            conn.commit()
            return True, "账号已注销（停用）。如误操作请联系最高管理员启用。"
    except Exception as exc:
        return False, f"注销失败: {exc}"


# ===== 2. AI 识别合同数据写入 API =====
# 固定栏目的"英文键 -> 中文列名"（这些列建表即有）；用户新增的自定义栏目
# 通过 ai_parsed_json["extra_fields"] 传入，列名须命中实际表列才写入。
# 注意：是否已收款/是否已开票 **不在此列**——状态默认"未"（列 DEFAULT FALSE），
# 且只允许人工在台账状态界面切换，AI 不得写入。
_FIXED_COLUMN_KEYS = [
    ("contract_code", "合同编号"),
    ("contract_term", "合同期限"),
    ("party_a", "甲方"),
    ("income", "合同金额"),
    ("project", "项目"),
    ("remark", "备注"),
]
# 覆盖语义：以下列"新值非空才覆盖"（防止重新处理冲掉旧备注）
_COALESCE_COLUMNS = {"项目", "备注"}


def _get_actual_table_columns(table_name: str) -> set[str]:
    """读取表中实际存在的列名（information_schema，实时）。"""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table_name,),
        )
        return {row[0] for row in cur.fetchall()}


def api_save_contract_from_ai(ai_parsed_json: dict, table_name: str = "contract_projects") -> tuple[bool, str]:
    """将 AI 提取的合同 JSON 数据存入数据库（同合同编号：最新版覆盖）。

    固定栏目用英文键（见 _FIXED_COLUMN_KEYS）；用户新增的自定义栏目经
    ai_parsed_json["extra_fields"] 传入（键=中文栏目名），**只写入表里实际
    存在的列**（先实时读 information_schema 校验），列名过白名单正则，防注入。

    覆盖语义：合同期限/甲方/合同金额 直接覆盖；是否已收款/是否已开票/项目/备注
    新值非空才覆盖（人工状态保护）。返回消息注明"新增/覆盖"。
    """
    bad = _check_table_allowed(table_name)
    if bad:
        return False, bad
    try:
        # 实时读取表里实际存在的列（用户可能通过字段调整功能删除/新增栏目）
        actual_cols = _get_actual_table_columns(table_name)

        # 固定列：只保留表里实际存在的列（"合同期限"等被删后不再写入）
        fixed: dict[str, object] = {}
        for key, col in _FIXED_COLUMN_KEYS:
            if col in actual_cols:
                fixed[col] = ai_parsed_json.get(key)
        if "合同编号" not in fixed:
            return False, f"台账缺少唯一键列【合同编号】，无法写入！"
        if "合同金额" in fixed:
            fixed["合同金额"] = ai_parsed_json.get("income", 0.0)
        # 是否已收款/是否已开票 不在此写入：状态默认"未"、只允许人工切换

        # 自定义栏目：仅接受"实际存在的列"且列名合法（防注入）
        extras: dict[str, object] = {}
        for col, val in (ai_parsed_json.get("extra_fields") or {}).items():
            if (
                col in actual_cols
                and col not in fixed
                and isinstance(col, str)
                and re.match(r"^[\w\u4e00-\u9fa5]+$", col)
            ):
                extras[col] = val

        col_names = [f'"{c}"' for c in fixed] + [f'"{c}"' for c in extras]
        placeholders = ", ".join(["%s"] * len(col_names))
        update_sets = [
            f'"{c}" = EXCLUDED."{c}"' for c in fixed if c not in _COALESCE_COLUMNS
        ] + [
            f'"{c}" = COALESCE(EXCLUDED."{c}", {table_name}."{c}")' for c in fixed if c in _COALESCE_COLUMNS
        ] + [
            f'"{c}" = EXCLUDED."{c}"' for c in extras
        ]
        sql = (
            f'INSERT INTO {table_name} ({", ".join(col_names)}) VALUES ({placeholders}) '
            f'ON CONFLICT ("合同编号") DO UPDATE SET ' + ", ".join(update_sets)
            + " RETURNING (xmax = 0) AS inserted"
        )
        params = [fixed[c] for c in fixed] + [extras[c] for c in extras]

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            inserted = bool(cur.fetchone()[0])
            conn.commit()
        code = ai_parsed_json.get("contract_code")
        if inserted:
            return True, f"已新增台账行（合同编号：{code}）"
        return True, f"同合同编号已存在，已按最新版覆盖（合同编号：{code}）"
    except Exception as exc:
        return False, f"存入数据库失败: {exc}"


# =========================================================
# 3. 权限模型（初始版简化实现，后续替换为 RBAC 映射库）
# =========================================================
# 业务表白名单：目前只有合同台账；以后发票/物流等业务接入时，
# 在这里登记新表即可（键=表名，值=界面显示名）。
# 表名必须走白名单，绝不能直接拼进 SQL（防注入）。
ALLOWED_TABLES: dict[str, str] = {
    "contract_projects": "合同台账",
}

# 字段权限点定义：读/写分离（financial_role 拥有最高权限）
FIELD_READ_PERMISSION = "field:read"    # 读取字段（所有已登录用户）
FIELD_WRITE_PERMISSION = "field:write"  # 调整字段（仅 financial_role / admin）
LEDGER_STATUS_PERMISSION = "ledger:status"  # 台账状态人工切换（已收款/已开票）

# 角色 -> 权限集合（内存兜底映射）。
# 说明：role_type 来自 sys_users（默认 finance_staff）；financial_role 为内置
# 业务角色（最高权限），admin 为角色管理者。正式权限以数据库表
# sys_permissions + sys_role_permissions 为准（见 require_permission），
# 本字典仅在数据库未初始化时兜底。
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "finance_staff": {FIELD_READ_PERMISSION},
    "financial_role": {FIELD_READ_PERMISSION, FIELD_WRITE_PERMISSION, LEDGER_STATUS_PERMISSION},
    "admin": {FIELD_READ_PERMISSION, FIELD_WRITE_PERMISSION, LEDGER_STATUS_PERMISSION},
}


def require_permission(user: dict | None, permission: str) -> tuple[bool, str]:
    """统一权限检查入口（RBAC 映射库版）。

    user：登录接口返回的用户字典（含 role_type）。
    permission：权限点编码（见 ROLE_PERMISSIONS 的 key，来自 sys_permissions 目录）。
    返回 (是否允许, 提示信息)。数据库未初始化时自动回退内存映射，保证功能可用。
    """
    if not user:
        return False, "未登录，无法执行该操作！"
    role = user.get("role_type", "")
    # 优先查数据库权限表（sys_role_permissions <-> sys_permissions）
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM sys_role_permissions rp
                JOIN sys_permissions p ON p.permission_id = rp.permission_id
                WHERE rp.role_type = %s AND p.permission_code = %s
                """,
                (role, permission),
            )
            if cur.fetchone():
                return True, ""
    except Exception:
        pass  # DB 未就绪，走下面的内存映射兜底
    if permission in ROLE_PERMISSIONS.get(role, set()):
        return True, ""
    return False, f"当前角色({role})没有权限执行此操作！"


def _check_table_allowed(table_name: str) -> str | None:
    """校验业务表是否在白名单内；非法返回错误提示，合法返回 None。"""
    if table_name not in ALLOWED_TABLES:
        return f"不支持的业务表：{table_name}（仅支持：{', '.join(sorted(ALLOWED_TABLES))}）"
    return None


# =========================================================
# 4. 字段管理 API（读取/调整分离，权限受控）
# =========================================================
# 允许通过字段调整接口新增的字段类型白名单，避免 col_type 被
# 拼进 SQL 时夹带任意内容（这是之前 Part 2 提到的那个口子，顺手堵上）。
ALLOWED_COLUMN_TYPES = {
    "VARCHAR(50)", "VARCHAR(100)", "VARCHAR(200)", "VARCHAR(255)",
    "TEXT", "NUMERIC(15,2)", "BOOLEAN", "DATE", "TIMESTAMP", "INTEGER",
}


# =========================================================
# 4.0 列目录表 column_catalog（物理列 = 唯一事实源，目录只做语义标注）
# =========================================================
# 背景：字段调整对话框要允许"内置列可删/可改"（内置列只是初始化一次），
# 但代码需要知道每列的语义（哪个是唯一键/人工状态/系统列，AI 是否可见）。
# 因此引入列目录表 column_catalog：
#   · logical_key：英文稳定逻辑键（如 contract_code），只在程序内部/本表使用，
#     永不作为物理列名；物理列名永远是中文，以 information_schema 为准；
#   · 自愈对账：目录行不随 ALTER 事务绑定删除——物理列是唯一事实源。
#     load_column_catalog() 每次读取前自动比对：
#       目录有、物理无 → 删行（列被 DROP 或外部删除）；
#       物理有、目录无 → 补行（命中种子名按种子语义，否则按通用自定义列）。
#     因此"删除列 = 纯 ALTER TABLE DROP COLUMN"，无需手工删目录行，
#     也保证交给 AI/对话框的列清单永远与物理列一一对应、不多不少。
#   · 种子让位：种子列若已被用户删除，对账时不会悄悄重建（不复活已删列）。
# 权限：目录表由管理员连接创建并授权给应用角色；日常读取走应用连接。
# ⚠️ 已知局限（文档化）：绕过本应用（如 pgAdmin）直接改名/删列时，
#    目录无法自动推断语义迁移，只会按"删旧列 + 新增通用列"处理；
#    本应用内的改名/删除必须走下方 api_rename_table_field /
#    api_alter_table_field / api_set_key_column（会同步目录）。
_CATALOG_DDL = """
CREATE TABLE IF NOT EXISTS column_catalog (
    catalog_id    SERIAL PRIMARY KEY,
    table_name    VARCHAR(64)  NOT NULL,
    logical_key   VARCHAR(64)  NOT NULL,
    col_name      VARCHAR(64)  NOT NULL,
    data_type     VARCHAR(32)  NOT NULL,
    is_key_col    BOOLEAN NOT NULL DEFAULT FALSE,
    is_status_col BOOLEAN NOT NULL DEFAULT FALSE,
    is_system_col BOOLEAN NOT NULL DEFAULT FALSE,
    ai_visible    BOOLEAN NOT NULL DEFAULT TRUE,
    history_feed  BOOLEAN NOT NULL DEFAULT FALSE,
    is_seed       BOOLEAN NOT NULL DEFAULT FALSE,
    sort_order    INT NOT NULL DEFAULT 0,
    UNIQUE (table_name, col_name),
    UNIQUE (table_name, logical_key)
);
"""

# 种子列语义（col_name = 建表时的初始中文列名；对账发现物理列存在且目录缺失时回填）。
# is_key_col    = 唯一键列（台账覆盖/去重依据），当前只有合同编号；
# is_status_col = 人工状态列（是否已收款/是否已开票，只允许人工切换，AI 不得写）；
# is_system_col = 系统列（id/创建时间），禁止删除/改名；
# ai_visible    = 是否允许 AI 读写（标志先随种子落库，供后续"AI 提示词动态列"阶段启用，
#                 当前 AI/台账代码仍按旧逻辑运行，本阶段不改其行为）；
# history_feed  = 是否进 AI 的历史（项目+备注）上下文。
_SEED_COLUMNS = [
    {"logical_key": "contract_code", "col_name": "合同编号", "data_type": "VARCHAR(100)",
     "is_key_col": True, "ai_visible": False},
    {"logical_key": "party_a", "col_name": "甲方", "data_type": "VARCHAR(200)", "ai_visible": True},
    {"logical_key": "income", "col_name": "合同金额", "data_type": "NUMERIC(15,2)", "ai_visible": True},
    {"logical_key": "contract_term", "col_name": "合同期限", "data_type": "VARCHAR(50)", "ai_visible": True},
    {"logical_key": "project", "col_name": "项目", "data_type": "VARCHAR(200)",
     "ai_visible": True, "history_feed": True},
    {"logical_key": "is_paid", "col_name": "是否已收款", "data_type": "BOOLEAN",
     "is_status_col": True, "ai_visible": False},
    {"logical_key": "is_invoiced", "col_name": "是否已开票", "data_type": "BOOLEAN",
     "is_status_col": True, "ai_visible": False},
    {"logical_key": "remark", "col_name": "备注", "data_type": "TEXT",
     "ai_visible": True, "history_feed": True},
    {"logical_key": "sys_id", "col_name": "id", "data_type": "BIGSERIAL",
     "is_system_col": True, "ai_visible": False},
    {"logical_key": "sys_created", "col_name": "创建时间", "data_type": "TIMESTAMP",
     "is_system_col": True, "ai_visible": False},
]
_SEED_BY_COL = {s["col_name"]: s for s in _SEED_COLUMNS}

# 兜底保护：即使目录表初始化失败，系统列也不允许被删/改名（按名字硬保护）。
_SYSTEM_COLUMN_FALLBACK = {"id", "创建时间"}


def _ensure_catalog_table() -> None:
    """确保 column_catalog 表存在并授权（管理员连接，幂等）。"""
    with get_admin_connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(_CATALOG_DDL)
            cur.execute(
                sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                    sql.Identifier("column_catalog"),
                    sql.Identifier(APP_DB_CONFIG["user"]),
                )
            )
            cur.execute(
                sql.SQL("GRANT USAGE, SELECT ON SEQUENCE {} TO {}").format(
                    sql.Identifier("column_catalog_catalog_id_seq"),
                    sql.Identifier(APP_DB_CONFIG["user"]),
                )
            )


def _read_catalog_rows(table_name: str) -> list[dict]:
    """读取目录全部行（应用连接；表不存在/无权限时抛异常，由调用方处理）。"""
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT catalog_id, logical_key, col_name, data_type, is_key_col, "
            "is_status_col, is_system_col, ai_visible, history_feed, is_seed, sort_order "
            "FROM column_catalog WHERE table_name = %s "
            "ORDER BY sort_order, catalog_id",
            (table_name,),
        )
        return [dict(r) for r in cur.fetchall()]


def _physical_column_names(table_name: str) -> set[str]:
    """读取业务表当前真实存在的物理列名（information_schema，应用连接）。"""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table_name,),
        )
        return {row[0] for row in cur.fetchall()}


def _write_catalog_reconcile(table_name: str, physical: set[str]) -> list[dict]:
    """对账修正（管理员连接）：删掉已不存在的目录行，补上物理新增列。

    物理有、目录无的列，补行规则：
      - 命中种子名 且 该逻辑键未被占用 → 按种子语义补（唯一键/状态/系统标志保留）；
      - 否则 → 通用自定义列（logical_key 自动生成 c_xxxx，ai_visible=True）。
    返回修正后的目录行。
    """
    with get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT catalog_id, logical_key, col_name FROM column_catalog "
            "WHERE table_name = %s",
            (table_name,),
        )
        existing = cur.fetchall()
        # 1) 目录有、物理无 → 删行
        for cid, _lk, zh in existing:
            if zh not in physical:
                cur.execute("DELETE FROM column_catalog WHERE catalog_id = %s", (cid,))
        # 2) 物理有、目录无 → 补行
        known = {zh for _cid, _lk, zh in existing}
        used_keys = {lk for _cid, lk, _zh in existing}
        cur.execute(
            "SELECT COALESCE(MAX(sort_order), 0) FROM column_catalog WHERE table_name = %s",
            (table_name,),
        )
        next_order = cur.fetchone()[0]
        for zh in sorted(physical - known):
            next_order += 1
            seed = _SEED_BY_COL.get(zh)
            if seed is not None and seed["logical_key"] not in used_keys:
                s = seed
                cur.execute(
                    "INSERT INTO column_catalog (table_name, logical_key, col_name, data_type, "
                    "is_key_col, is_status_col, is_system_col, ai_visible, history_feed, "
                    "is_seed, sort_order) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (table_name, col_name) DO NOTHING",
                    (table_name, s["logical_key"], zh, s["data_type"],
                     s.get("is_key_col", False), s.get("is_status_col", False),
                     s.get("is_system_col", False), s.get("ai_visible", True),
                     s.get("history_feed", False), True, next_order),
                )
            else:
                # 通用自定义列：逻辑键自动生成（不可猜测、唯一、与中文名解耦）
                cur.execute(
                    "INSERT INTO column_catalog (table_name, logical_key, col_name, data_type, "
                    "ai_visible, is_seed, sort_order) VALUES (%s,%s,%s,%s,TRUE,FALSE,%s) "
                    "ON CONFLICT (table_name, col_name) DO NOTHING",
                    (table_name, f"c_{uuid.uuid4().hex[:8]}", zh, "TEXT", next_order),
                )
        conn.commit()
    return _read_catalog_rows(table_name)


def load_column_catalog(table_name: str = "contract_projects") -> list[dict]:
    """读取业务表的列目录（语义标注清单），读取前自动确保表存在并对账自愈。

    返回每行含：catalog_id/logical_key/col_name/data_type/is_key_col/
    is_status_col/is_system_col/ai_visible/history_feed/is_seed/sort_order。
    任何一步失败都不抛错：打印告警并返回 []（调用方需容忍空目录，
    系统列保护由 _SYSTEM_COLUMN_FALLBACK 名称兜底）。
    """
    # 首次运行（表不存在）时建表 + 授权
    try:
        rows = _read_catalog_rows(table_name)
    except Exception:
        try:
            _ensure_catalog_table()
            rows = _read_catalog_rows(table_name)
        except Exception as exc:
            print(f"⚠️ [column_catalog] 目录表初始化失败，字段保护降级为名称兜底：{exc}")
            return []
    # 对账：目录行集 != 物理列集 时才走管理员连接修正
    try:
        physical = _physical_column_names(table_name)
    except Exception as exc:
        print(f"⚠️ [column_catalog] 读取物理列失败（返回现有目录行）：{exc}")
        return rows
    known = {r["col_name"] for r in rows}
    if known != physical:
        try:
            rows = _write_catalog_reconcile(table_name, physical)
        except Exception as exc:
            print(f"⚠️ [column_catalog] 对账修正失败（不影响主流程）：{exc}")
    return rows


def _catalog_by_col(table_name: str) -> dict[str, dict]:
    """col_name -> 目录行 的便捷映射（用于字段 API 的语义判断）。"""
    return {r["col_name"]: r for r in load_column_catalog(table_name)}


def api_get_table_fields(user: dict | None, table_name: str = "contract_projects") -> tuple[bool, list[dict] | str]:
    """读取指定业务表的现有字段（仅字段名/类型/是否可空，不返回任何数据内容）。

    权限：field:read —— 所有已登录用户。
    返回：(True, [{"column_name":..., "data_type":..., "is_nullable":...}, ...]) 或 (False, 错误信息)。
    说明：information_schema 对登录用户默认可读，且这里表名参数化、表名走白名单，
    不会引入注入风险。
    """
    ok, msg = require_permission(user, FIELD_READ_PERMISSION)
    if not ok:
        return False, msg
    bad = _check_table_allowed(table_name)
    if bad:
        return False, bad
    try:
        with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY ordinal_position
                """,
                (table_name,),
            )
            fields = [dict(row) for row in cur.fetchall()]
        # 附加列目录语义标注（只加键、不改原有三个键，兼容现有全部调用方）：
        # logical_key / is_key_col / is_status_col / is_system_col / is_seed / ai_visible。
        # 目录读取失败时返回原始字段结构（与旧版行为完全一致）。
        catalog = _catalog_by_col(table_name)
        for f in fields:
            meta = catalog.get(f["column_name"])
            f["logical_key"] = (meta or {}).get("logical_key")
            f["is_key_col"] = bool(meta and meta.get("is_key_col"))
            f["is_status_col"] = bool(meta and meta.get("is_status_col"))
            f["is_system_col"] = bool(meta and meta.get("is_system_col"))
            f["is_seed"] = bool(meta and meta.get("is_seed"))
            f["ai_visible"] = True if not meta else bool(meta.get("ai_visible", True))
        return True, fields
    except Exception as exc:
        return False, f"读取字段失败: {exc}"


def api_alter_table_field(
    user: dict | None, action_type: str, table_name: str,
    col_name: str, col_type: str = "VARCHAR(255)",
) -> tuple[bool, str]:
    """调整表字段（ADD 新增 / DROP 删除列）。

    权限：field:write —— 仅 financial_role / admin（业务层校验）。
    DDL 执行层：用 get_admin_connection()（管理员连接）——ALTER TABLE 必须
    由表属主执行，finance_app_role 应用角色没有 DDL 权限。
    字段名支持中英文+数字+下划线；表名必须命中 ALLOWED_TABLES 白名单；
    新增时 col_type 必须命中 ALLOWED_COLUMN_TYPES 白名单。

    列目录化后的语义（v2）：
      · ADD：ALTER 后立即在 column_catalog 登记一行（逻辑键自动生成 c_xxxx、
        类型=白名单原值、ai_visible=True）——新列下次打开字段对话框/AI 提示词
        即可见；
      · DROP：只执行 ALTER TABLE DROP COLUMN，**不**手工删目录行——目录行由
        下次 load_column_catalog() 对账时自动清理（物理列是唯一事实源）。
        保护规则：系统列（id/创建时间）不可删；当前唯一键列不可直接删，
        需先用 api_set_key_column() 把唯一键移交给其它列。
    返回：(是否成功, 提示信息)。
    """
    ok, msg = require_permission(user, FIELD_WRITE_PERMISSION)
    if not ok:
        return False, msg
    bad = _check_table_allowed(table_name)
    if bad:
        return False, bad
    if not re.match(r"^[\w\u4e00-\u9fa5]+$", col_name):
        return False, "字段名只能包含中英文、数字和下划线！"

    action = action_type.upper()
    if action == "ADD":
        if col_type.upper() not in ALLOWED_COLUMN_TYPES:
            return False, f"不支持的字段类型：{col_type}（仅支持：{', '.join(sorted(ALLOWED_COLUMN_TYPES))}）"
    elif action == "DROP":
        # 删除前的语义保护（目录读取失败时用系统列名字兜底）
        meta = _catalog_by_col(table_name).get(col_name) or {}
        is_system = bool(meta.get("is_system_col")) or col_name in _SYSTEM_COLUMN_FALLBACK
        is_key = bool(meta.get("is_key_col"))
        if is_system:
            return False, f"「{col_name}」是系统列（id/创建时间），不可删除！"
        if is_key:
            return False, (
                f"「{col_name}」是当前唯一键列（台账覆盖/去重依据）。"
                "如需删除，请先在字段列表中选择另一列点击「设为唯一键」移交后再删除。"
            )
    else:
        return False, "未知的修改类型！"

    try:
        with get_admin_connection() as conn, conn.cursor() as cur:
            quoted_col = f'"{col_name}"'
            if action == "ADD":
                cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {quoted_col} {col_type};")
                # 同步登记目录行：逻辑键自动生成，类型用白名单原值（便于后续 AI 类型提示）
                cur.execute(
                    "INSERT INTO column_catalog (table_name, logical_key, col_name, data_type, "
                    "ai_visible, is_seed) VALUES (%s, %s, %s, %s, TRUE, FALSE) "
                    "ON CONFLICT (table_name, col_name) DO NOTHING",
                    (table_name, f"c_{uuid.uuid4().hex[:8]}", col_name, col_type),
                )
            else:  # DROP：纯 DDL，目录行由下次 load_column_catalog() 对账清理
                cur.execute(f"ALTER TABLE {table_name} DROP COLUMN {quoted_col};")
            conn.commit()
            return True, f"表结构成功调整：{action} {col_name}"
    except Exception as exc:
        return False, f"修改表结构失败: {exc}"


def api_rename_table_field(user: dict | None, table_name: str, old_name: str, new_name: str) -> tuple[bool, str]:
    """重命名表字段（内置列同样允许改名——内置列只是"初始化一次"的种子）。

    权限：field:write —— 仅 financial_role / admin（业务层校验）。
    DDL 执行层：用 get_admin_connection()（ALTER TABLE 需表属主权限）。
    列目录化后的语义（v2）：
      · 系统列（id/创建时间）禁止改名；系统列之外的任何列（含合同编号/甲方等
        内置列）均可改名；
      · 物理列改名后，同步 UPDATE column_catalog 的 col_name（按逻辑键匹配），
        唯一键/状态/历史等语义标志随逻辑键保留，不会因改名丢失；
      · 目录行缺失时（理论不出现，因读取前已对账）只告警不阻断。
    ⚠️ 已知局限（文档化）：本阶段台账写入/AI 提示词等代码仍按旧版写死的
    中文列名运行——若改名的列被这些代码引用，相关功能会报错/跳过；
    全部列读写改为"目录驱动"是下一阶段（不影响本阶段字段调整能力的落地）。
    返回：(是否成功, 提示信息)。
    """
    ok, msg = require_permission(user, FIELD_WRITE_PERMISSION)
    if not ok:
        return False, msg
    bad = _check_table_allowed(table_name)
    if bad:
        return False, bad
    if not re.match(r"^[\w\u4e00-\u9fa5]+$", old_name) or not re.match(r"^[\w\u4e00-\u9fa5]+$", new_name):
        return False, "字段名只能包含中英文、数字和下划线！"
    if old_name == new_name:
        return False, "新旧字段名相同，无需修改！"

    # 语义保护：系统列不可改名（目录读取失败时用系统列名字兜底）
    meta = _catalog_by_col(table_name).get(old_name) or {}
    if bool(meta.get("is_system_col")) or old_name in _SYSTEM_COLUMN_FALLBACK:
        return False, f"「{old_name}」是系统列（id/创建时间），不可改名！"
    # 新名字不得与现有物理列重复
    try:
        if new_name in _physical_column_names(table_name):
            return False, f"栏目「{new_name}」已存在，无法改名为重名！"
    except Exception:
        pass  # 读取物理列失败时交给数据库层报错

    try:
        with get_admin_connection() as conn, conn.cursor() as cur:
            cur.execute(f'ALTER TABLE {table_name} RENAME COLUMN "{old_name}" TO "{new_name}";')
            # 同步目录：按逻辑键匹配（col_name 是展示层，改名不改变语义）
            cur.execute(
                "UPDATE column_catalog SET col_name = %s "
                "WHERE table_name = %s AND col_name = %s",
                (new_name, table_name, old_name),
            )
            if cur.rowcount == 0:
                print(f"⚠️ [column_catalog] 重命名 {old_name} 时目录未匹配到行（跳过目录同步）")
            conn.commit()
            return True, f"字段重命名成功：{old_name} -> {new_name}"
    except Exception as exc:
        return False, f"重命名字段失败: {exc}"


def api_set_key_column(user: dict | None, table_name: str, col_name: str) -> tuple[bool, str]:
    """把指定列设为当前唯一键列（唯一键移交）。

    场景：删除内置唯一键列（合同编号）前必须先调用本函数，把"覆盖/去重依据"
    移交到另一列；移交后该列被标记 is_key_col，原键列取消标志（可再删除）。

    权限：field:write —— 仅 financial_role / admin。
    实现（管理员连接，顺序执行，任一失败即中止并给出原因）：
      1. ALTER COLUMN SET NOT NULL       —— 唯一键列不允许空值；
      2. CREATE UNIQUE INDEX（按列名哈希命名，幂等）—— 列值必须唯一，
         存在重复值时 PostgreSQL 抛错，此处转成友好提示；
      3. UPDATE column_catalog 移交 is_key_col 标志（同表内互斥）。
    返回：(是否成功, 提示信息)。
    """
    ok, msg = require_permission(user, FIELD_WRITE_PERMISSION)
    if not ok:
        return False, msg
    bad = _check_table_allowed(table_name)
    if bad:
        return False, bad
    if not re.match(r"^[\w\u4e00-\u9fa5]+$", col_name):
        return False, "字段名只能包含中英文、数字和下划线！"

    meta = _catalog_by_col(table_name).get(col_name)
    if not meta:
        return False, f"栏目「{col_name}」不存在或列目录未就绪，无法设为唯一键！"
    if meta.get("is_system_col") or col_name in _SYSTEM_COLUMN_FALLBACK:
        return False, f"「{col_name}」是系统列，不可设为唯一键！"
    if meta.get("is_key_col"):
        return False, f"「{col_name}」已是当前唯一键列，无需重复设置！"

    # 唯一索引名按列名哈希生成（跨进程稳定，避免同名索引指向旧列导致无法重建）
    index_name = f"uq_{table_name}_{hashlib.sha1(col_name.encode('utf-8')).hexdigest()[:8]}"
    try:
        with get_admin_connection() as conn, conn.cursor() as cur:
            cur.execute(f'ALTER TABLE {table_name} ALTER COLUMN "{col_name}" SET NOT NULL;')
            cur.execute(
                f'CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table_name} ("{col_name}");'
            )
            # 移交标志：同表内 is_key_col 互斥（旧键列自动取消）
            cur.execute(
                "UPDATE column_catalog SET is_key_col = (col_name = %s) "
                "WHERE table_name = %s",
                (col_name, table_name),
            )
            conn.commit()
            return True, f"已把「{col_name}」设为唯一键列（原唯一键列已取消该标志）"
    except Exception as exc:
        err_text = str(exc)
        hint = ""
        if "null value" in err_text or "contains null" in err_text:
            hint = "（该列存在空值，请先补全/清空空行）"
        elif "duplicate key" in err_text or "Duplicate" in err_text:
            hint = "（该列存在重复值，请先清理重复后再试）"
        return False, f"设为唯一键失败: {exc}{hint}"


def api_ai_analyze_user_habits() -> dict:
    """预留：AI 分析过往人员操作习惯，提出字段修改建议。"""
    # 模拟 AI 识别到的习惯变更申请
    return {
        "reason": "检测到近 10 份扫描文本中均包含【发票开具状态】",
        "action": "ADD",
        "col_name": "发票状态",
        "col_type": "VARCHAR(50)",
    }


# =========================================================
# 5. 实体映射库（编号 <-> 真实值，数据库分表存储）
# =========================================================
# 类别 -> 表名；SECRET_CATEGORIES 为敏感类别（表内只存指纹+密文，不落明文）。
ENTITY_TABLES: dict[str, str] = {
    "company": "entity_mapping_company",
    "party": "entity_mapping_party",
    "date": "entity_mapping_date",
    "id_card": "entity_mapping_id_card",
    "bank_card": "entity_mapping_bank_card",
}
SECRET_CATEGORIES = {"id_card", "bank_card"}

_CODE_PREFIX = {
    "company": "CO",
    "party": "PT",
    "date": "DT",
    "id_card": "ID",
    "bank_card": "BC",
}
_CATEGORY_BY_PREFIX = {prefix: category for category, prefix in _CODE_PREFIX.items()}
DECRYPT_PERMISSION_BY_CATEGORY = {category: f"entity:decrypt:{category}" for category in ENTITY_TABLES}

# 旧版本地 JSON 映射文件路径（仅用于一次性导入）
MAPPING_JSON_FILE = Path(__file__).resolve().parent / "hub" / "_mapping" / "entity_mapping.json"


def _normalize(value: str) -> str:
    """去空白归一化，作为映射去重比对的 key（严格精确匹配，不做模糊合并——
    避免把两个不同实体误判成同一个）。"""
    return "".join(value.split())


class MappingDbStore:
    """数据库版实体映射库（替代 hub_pipeline 里的本地 JSON 版 MappingStore）。

    接口与旧版完全一致：get_or_create_code / get_or_create_secret_code /
    lookup_real_value / decrypt，调用方（desensitize_text 等）无需改动。

    存储规则：
    - company / party / date：明文入库（业务上需要直接反查真实名称）；
    - id_card / bank_card：只存 sha256(归一化值) 指纹 + Fernet 密文，
      明文不落盘；解密必须通过 require_permission('entity:decrypt:<类别>')。
    首次使用时若映射表为空且存在旧 entity_mapping.json，自动导入。
    """

    def __init__(self) -> None:
        pass

    @classmethod
    def load(cls) -> "MappingDbStore":
        """兼容旧接口：数据库版始终是最新状态，无需加载文件。

        首次调用时自动把旧 entity_mapping.json 导入数据库（幂等）。
        """
        store = cls()
        store._migrate_once()
        return store

    # ---------- 内部工具 ----------
    @staticmethod
    def _table(category: str) -> str:
        """类别 -> 表名；未知类别直接抛错，防止拼 SQL。"""
        table = ENTITY_TABLES.get(category)
        if table is None:
            raise KeyError(f"未知映射类别：{category}")
        return table

    @staticmethod
    def _next_code(conn, table: str, prefix: str) -> str:
        """生成下一个编号：CO0001 风格，序号取当前表最大 id + 1。"""
        with conn.cursor() as cur:
            cur.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}")
            return f"{prefix}{cur.fetchone()[0] + 1:04d}"

    def _migrate_once(self) -> None:
        """旧 JSON -> 数据库 一次性导入（幂等：company 表非空即跳过）。"""
        if not MAPPING_JSON_FILE.exists():
            return
        try:
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM entity_mapping_company")
                if cur.fetchone()[0] > 0:
                    return  # 已导入过
        except Exception:
            return  # 表还没建（init_db 未跑）时静默跳过
        import_entity_mapping_json()

    # ---------- 对外接口 ----------
    def get_or_create_code(self, category: str, real_value: str) -> str:
        """明文类别（company/party/date）：按归一化值查重，不存在则插入。"""
        table = self._table(category)
        norm_key = _normalize(real_value)
        prefix = _CODE_PREFIX.get(category, "EN")
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT code FROM {table} WHERE norm_key = %s", (norm_key,))
                row = cur.fetchone()
                if row:
                    return row[0]
                code = self._next_code(conn, table, prefix)
                cur.execute(
                    f"INSERT INTO {table} (code, norm_key, real_value) VALUES (%s, %s, %s)",
                    (code, norm_key, real_value),
                )
                conn.commit()
                return code

    def get_or_create_secret_code(self, category: str, real_value: str, masked_display: str) -> str:
        """敏感类别（id_card/bank_card）：只存指纹+密文，明文不落盘。"""
        table = self._table(category)
        norm_key = _normalize(real_value)
        fingerprint = hashlib.sha256(norm_key.encode("utf-8")).hexdigest()
        prefix = _CODE_PREFIX.get(category, "EN")
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT code FROM {table} WHERE norm_key = %s", (fingerprint,))
                row = cur.fetchone()
                if row:
                    return row[0]
                code = self._next_code(conn, table, prefix)
                cur.execute(
                    f"INSERT INTO {table} (code, norm_key, masked, cipher) VALUES (%s, %s, %s, %s)",
                    (code, fingerprint, masked_display, encrypt_value(real_value)),
                )
                conn.commit()
                return code

    def lookup_real_value(self, code: str) -> str | None:
        """按编码反查真实值（仅明文类别）；敏感类别请走 decrypt()。"""
        category = _CATEGORY_BY_PREFIX.get(code[:2] if code else "")
        if category is None or category in SECRET_CATEGORIES:
            return None
        table = self._table(category)
        try:
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(f"SELECT real_value FROM {table} WHERE code = %s", (code,))
                row = cur.fetchone()
                return row[0] if row else None
        except Exception:
            return None

    def decrypt(self, category: str, code: str, requesting_user: dict | None) -> str:
        """解密敏感字段真实值：必须通过 require_permission('entity:decrypt:<类别>')。"""
        permission = DECRYPT_PERMISSION_BY_CATEGORY.get(category)
        if permission is None:
            raise KeyError(f"未知映射类别：{category}")
        ok, msg = require_permission(requesting_user, permission)
        if not ok:
            raise PermissionError(msg)
        table = self._table(category)
        try:
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(f"SELECT cipher FROM {table} WHERE code = %s", (code,))
                row = cur.fetchone()
        except Exception as exc:
            raise RuntimeError(f"读取密文失败: {exc}") from exc
        if not row:
            raise KeyError(f"未找到编码：{code}")
        return decrypt_value(row[0])

    def _insert_secret(self, category: str, real_value: str, masked: str, cipher: str) -> None:
        """导入旧 JSON 时直接沿用原密文（同一把 key，无需重新加密）。"""
        table = self._table(category)
        norm_key = _normalize(real_value)
        fingerprint = hashlib.sha256(norm_key.encode("utf-8")).hexdigest()
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT code FROM {table} WHERE norm_key = %s", (fingerprint,))
            if cur.fetchone():
                return
            code = self._next_code(conn, table, _CODE_PREFIX.get(category, "EN"))
            cur.execute(
                f"INSERT INTO {table} (code, norm_key, masked, cipher) VALUES (%s, %s, %s, %s)",
                (code, fingerprint, masked, cipher),
            )
            conn.commit()


def import_entity_mapping_json() -> tuple[int, str]:
    """手动触发：把旧版 hub/_mapping/entity_mapping.json 导入数据库映射表。

    返回 (新增条数, 说明)。幂等：已存在的归一化键自动跳过。
    """
    if not MAPPING_JSON_FILE.exists():
        return 0, "未找到 entity_mapping.json，跳过导入"
    data = json.loads(MAPPING_JSON_FILE.read_text(encoding="utf-8"))
    store = MappingDbStore()
    count = 0
    for category, bucket in data.items():
        if category not in ENTITY_TABLES:
            continue  # 未知类别跳过（向前兼容）
        for real_value, entry in bucket.items():
            if category in SECRET_CATEGORIES:
                store._insert_secret(category, real_value, entry.get("masked", ""), entry.get("cipher", ""))
            else:
                store.get_or_create_code(category, real_value)
            count += 1
    return count, f"已导入 {count} 条映射（重复键自动跳过）"


# =========================================================
# 6. 台账扩展 API（AI 协作 + 人工状态管理）
# =========================================================
def api_get_ledger_project_notes(
    user: dict | None, table_name: str = "contract_projects", limit: int = 200
) -> tuple[bool, list[dict] | str]:
    """只读取台账的【项目栏 + 备注栏】两列（供 AI 学习备注习惯、匹配已有项目）。

    权限：field:read（所有已登录用户）。
    ⚠️ 刻意只返回这两列：AI 可见范围被限制在"项目+备注"，
    金额/甲方等其它列不经过此接口传给 AI。
    """
    ok, msg = require_permission(user, FIELD_READ_PERMISSION)
    if not ok:
        return False, msg
    bad = _check_table_allowed(table_name)
    if bad:
        return False, bad
    try:
        with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f'SELECT "项目", "备注" FROM {table_name} WHERE "项目" IS NOT NULL '
                f"ORDER BY id DESC LIMIT %s",
                (limit,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            return True, rows
    except Exception as exc:
        return False, f"读取项目/备注失败: {exc}"


def api_list_contracts(user: dict | None, table_name: str = "contract_projects") -> tuple[bool, list[dict] | str]:
    """列出台账全部合同（状态管理对话框用：编号/项目/金额/已收款/已开票）。"""
    ok, msg = require_permission(user, FIELD_READ_PERMISSION)
    if not ok:
        return False, msg
    bad = _check_table_allowed(table_name)
    if bad:
        return False, bad
    try:
        with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f'SELECT "合同编号", "项目", "合同金额", "是否已收款", "是否已开票" '
                f"FROM {table_name} ORDER BY id DESC"
            )
            return True, [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        return False, f"读取台账失败: {exc}"


def api_set_contract_status(
    user: dict | None,
    contract_code: str,
    paid: bool | None = None,
    invoiced: bool | None = None,
    table_name: str = "contract_projects",
) -> tuple[bool, str]:
    """人工切换台账状态：是否已收款 / 是否已开票（默认未，可手动改为已）。

    权限：ledger:status（financial_role / admin）。
    """
    ok, msg = require_permission(user, LEDGER_STATUS_PERMISSION)
    if not ok:
        return False, msg
    bad = _check_table_allowed(table_name)
    if bad:
        return False, bad
    if paid is None and invoiced is None:
        return False, "未指定要修改的状态！"
    try:
        sets, params = [], []
        if paid is not None:
            sets.append(f'"是否已收款" = %s')
            params.append(bool(paid))
        if invoiced is not None:
            sets.append(f'"是否已开票" = %s')
            params.append(bool(invoiced))
        params.append(contract_code)
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                f'UPDATE {table_name} SET ' + ", ".join(sets) + ' WHERE "合同编号" = %s',
                params,
            )
            conn.commit()
            return cur.rowcount > 0, "台账状态已更新！" if cur.rowcount else "未找到该合同编号！"
    except Exception as exc:
        return False, f"更新台账状态失败: {exc}"


def api_save_feature_hashes(
    user: dict | None, doc_key: str, features: dict[str, str], seq: int = 0
) -> tuple[bool, str]:
    """保存一份文件的特征哈希（feature_code -> value_hash）。

    权限：field:read（AI 归档由本机内部调用，登录用户上下文即可）。
    先删旧再插入，保证幂等重建。
    """
    ok, msg = require_permission(user, FIELD_READ_PERMISSION)
    if not ok:
        return False, msg
    if not features:
        return False, "没有可保存的特征！"
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM file_feature_hashes WHERE doc_key = %s AND seq = %s", (doc_key, seq)
            )
            for feature_code, value_hash in features.items():
                cur.execute(
                    "INSERT INTO file_feature_hashes (doc_key, feature_code, value_hash, seq) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (doc_key, feature_code, value_hash, seq),
                )
            conn.commit()
            return True, f"已保存 {len(features)} 条特征哈希（{doc_key}）"
    except Exception as exc:
        return False, f"保存特征哈希失败: {exc}"


def api_get_feature_catalog(user: dict | None) -> tuple[bool, list[dict] | str]:
    """读取特征目录（AI 特征分类的可选范围）。"""
    ok, msg = require_permission(user, FIELD_READ_PERMISSION)
    if not ok:
        return False, msg
    try:
        with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT feature_code, feature_name, is_mandatory, category, description "
                "FROM feature_catalog ORDER BY is_mandatory DESC, feature_code"
            )
            return True, [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        return False, f"读取特征目录失败: {exc}"


def api_upsert_project(
    user: dict | None,
    project_name: str,
    name_hash: str,
    parent_name: str | None = None,
    parent_hash: str | None = None,
    seq: int = 0,
) -> tuple[bool, str]:
    """项目归档：大/小项目层级（分公司/子公司同抬头哈希 + 子序号）。

    权限：field:read（AI 归档由本机内部调用）。
    """
    ok, msg = require_permission(user, FIELD_READ_PERMISSION)
    if not ok:
        return False, msg
    try:
        with get_connection() as conn, conn.cursor() as cur:
            parent_id = None
            if parent_hash:
                cur.execute("SELECT project_id FROM project_archive WHERE name_hash = %s", (parent_hash,))
                row = cur.fetchone()
                if row:
                    parent_id = row[0]
                elif parent_name:
                    cur.execute(
                        "INSERT INTO project_archive (project_name, name_hash, seq) VALUES (%s, %s, %s) "
                        "ON CONFLICT (name_hash) DO NOTHING RETURNING project_id",
                        (parent_name, parent_hash, 0),
                    )
                    row = cur.fetchone()
                    parent_id = row[0] if row else None
            cur.execute(
                "INSERT INTO project_archive (parent_id, project_name, name_hash, seq) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (name_hash) DO UPDATE SET parent_id = EXCLUDED.parent_id, seq = EXCLUDED.seq",
                (parent_id, project_name, name_hash, seq),
            )
            conn.commit()
            return True, f"项目已归档：{project_name}"
    except Exception as exc:
        return False, f"项目归档失败: {exc}"