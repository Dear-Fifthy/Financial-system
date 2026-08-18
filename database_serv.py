from __future__ import annotations

import hashlib
from pathlib import Path
import re
import psycopg2
from psycopg2.extras import RealDictCursor

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


# ===== 3. 动态表结构调整 API (手动/AI 习惯模式) =====
# 允许通过 api_alter_table_field 新增的字段类型白名单，避免 col_type 被
# 拼进 SQL 时夹带任意内容（这是之前 Part 2 提到的那个口子，顺手堵上）。
ALLOWED_COLUMN_TYPES = {
    "VARCHAR(50)", "VARCHAR(100)", "VARCHAR(200)", "VARCHAR(255)",
    "TEXT", "NUMERIC(15,2)", "BOOLEAN", "DATE", "TIMESTAMP", "INTEGER",
}


def api_alter_table_field(action_type: str, col_name: str, col_type: str = "VARCHAR(255)") -> tuple[bool, str]:
    """修改表字段（新增/删除列）。字段名支持中英文+数字+下划线，
    跟新的中文台账栏目风格保持一致。"""
    if not re.match(r"^[\w\u4e00-\u9fa5]+$", col_name):
        return False, "字段名只能包含中英文、数字和下划线！"

    try:
        with get_connection() as conn, conn.cursor() as cur:
            quoted_col = f'"{col_name}"'
            if action_type.upper() == "ADD":
                if col_type.upper() not in ALLOWED_COLUMN_TYPES:
                    return False, f"不支持的字段类型：{col_type}（仅支持：{', '.join(sorted(ALLOWED_COLUMN_TYPES))}）"
                sql = f"ALTER TABLE contract_projects ADD COLUMN {quoted_col} {col_type};"
            elif action_type.upper() == "DROP":
                sql = f"ALTER TABLE contract_projects DROP COLUMN {quoted_col};"
            else:
                return False, "未知的修改类型！"

            cur.execute(sql)
            conn.commit()
            return True, f"表结构成功调整：{action_type} {col_name}"
    except Exception as exc:
        return False, f"修改表结构失败: {exc}"


def api_ai_analyze_user_habits() -> dict:
    """预留：AI 分析过往人员操作习惯，提出字段修改建议。"""
    # 模拟 AI 识别到的习惯变更申请
    return {
        "reason": "检测到近 10 份扫描文本中均包含【发票开具状态】",
        "action": "ADD",
        "col_name": "发票状态",
        "col_type": "VARCHAR(50)",
    }