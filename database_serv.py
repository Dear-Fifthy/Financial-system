from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import psycopg2
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
    """获取应用专属数据库连接"""
    return psycopg2.connect(**APP_DB_CONFIG)


def hash_pwd(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def register_user(username: str, password: str, email: str, phone: str) -> tuple[bool, str]:
    email_regex = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
    phone_regex = r"^1[3-9]\d{9}$"

    if not re.match(email_regex, email):
        return False, "邮箱格式不正确！"
    if not re.match(phone_regex, phone):
        return False, "手机号码格式不正确（需为11位大陆手机号）！"

    try:
        # 使用 finance_app_role 连接并执行 INSERT 提交
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sys_users (username, password_hash, email, phone) VALUES (%s, %s, %s, %s)",
                (username, hash_pwd(password), email, phone),
            )
            conn.commit()  # 提交事务
            return True, "注册成功！"
    except Exception as exc:
        return False, f"注册失败: {exc}"

def authenticate_user(username: str, password: str) -> tuple[bool, dict | str]:
    """用户登录验证。"""
    try:
        with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT user_id, username, role_type FROM sys_users WHERE username = %s AND password_hash = %s",
                (username, hash_pwd(password)),
            )
            user = cur.fetchone()
            if user:
                return True, dict(user)
            return False, "用户名或密码错误！"
    except Exception as exc:
        return False, f"数据库连接异常: {exc}"


# ===== 2. AI 识别合同数据写入 API =====
def api_save_contract_from_ai(ai_parsed_json: dict) -> tuple[bool, str]:
    """将 AI 提取的合同 JSON 数据存入数据库。

    ai_parsed_json 的 key 仍然用英文（contract_code/contract_term/party_a/
    income/is_paid），只是内部 Python 变量名方便维护；实际写入数据库的
    列名（台账栏目）已经是中文，财务人员直接查表/导出时看到的就是中文。
    """
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO contract_projects ("合同编号", "合同期限", "甲方", "合同金额", "是否已收款")
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT ("合同编号") DO UPDATE
                SET "合同期限" = EXCLUDED."合同期限",
                    "甲方" = EXCLUDED."甲方",
                    "合同金额" = EXCLUDED."合同金额",
                    "是否已收款" = EXCLUDED."是否已收款";
            """,
                (
                    ai_parsed_json.get("contract_code"),
                    ai_parsed_json.get("contract_term"),
                    ai_parsed_json.get("party_a"),
                    ai_parsed_json.get("income", 0.0),
                    ai_parsed_json.get("is_paid", False),
                ),
            )
            conn.commit()
            return True, "数据已同步保存至 PostgreSQL 数据库！"
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

# 角色 -> 权限集合（内存兜底映射）。
# 说明：role_type 来自 sys_users（默认 finance_staff）；financial_role 为内置
# 业务角色（最高权限），admin 为角色管理者。正式权限以数据库表
# sys_permissions + sys_role_permissions 为准（见 require_permission），
# 本字典仅在数据库未初始化时兜底。
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "finance_staff": {FIELD_READ_PERMISSION},
    "financial_role": {FIELD_READ_PERMISSION, FIELD_WRITE_PERMISSION},
    "admin": {FIELD_READ_PERMISSION, FIELD_WRITE_PERMISSION},
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
            return True, fields
    except Exception as exc:
        return False, f"读取字段失败: {exc}"


def api_alter_table_field(
    user: dict | None, action_type: str, table_name: str,
    col_name: str, col_type: str = "VARCHAR(255)",
) -> tuple[bool, str]:
    """调整表字段（ADD 新增 / DROP 删除列）。

    权限：field:write —— 仅 financial_role / admin。
    字段名支持中英文+数字+下划线；表名必须命中 ALLOWED_TABLES 白名单；
    新增时 col_type 必须命中 ALLOWED_COLUMN_TYPES 白名单。
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

    try:
        with get_connection() as conn, conn.cursor() as cur:
            quoted_col = f'"{col_name}"'
            if action_type.upper() == "ADD":
                if col_type.upper() not in ALLOWED_COLUMN_TYPES:
                    return False, f"不支持的字段类型：{col_type}（仅支持：{', '.join(sorted(ALLOWED_COLUMN_TYPES))}）"
                sql = f"ALTER TABLE {table_name} ADD COLUMN {quoted_col} {col_type};"
            elif action_type.upper() == "DROP":
                sql = f"ALTER TABLE {table_name} DROP COLUMN {quoted_col};"
            else:
                return False, "未知的修改类型！"

            cur.execute(sql)
            conn.commit()
            return True, f"表结构成功调整：{action_type} {col_name}"
    except Exception as exc:
        return False, f"修改表结构失败: {exc}"


def api_rename_table_field(user: dict | None, table_name: str, old_name: str, new_name: str) -> tuple[bool, str]:
    """重命名表字段。

    权限：field:write —— 仅 financial_role / admin。
    ⚠️ 注意：重命名会影响写入该列的代码（如 api_save_contract_from_ai 里
    写死的列名），建议只对用户扩展的自定义字段重命名；内置字段
    （合同编号/甲方/合同金额等）如需改名，必须同步修改入库 SQL。
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

    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(f'ALTER TABLE {table_name} RENAME COLUMN "{old_name}" TO "{new_name}";')
            conn.commit()
            return True, f"字段重命名成功：{old_name} -> {new_name}"
    except Exception as exc:
        return False, f"重命名字段失败: {exc}"


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