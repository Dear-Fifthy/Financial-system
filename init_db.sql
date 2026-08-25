-- 1. 创建角色（若已存在则忽略）
DO $$ BEGIN
    CREATE ROLE finance_app_role WITH LOGIN PASSWORD 'finance_secret_123';
EXCEPTION WHEN duplicate_object THEN
    RAISE NOTICE '角色已存在，跳过创建';
END $$;

-- 2. 开启 pgvector 扩展（若未开启）
CREATE EXTENSION IF NOT EXISTS vector;

-- 3. 建表
CREATE TABLE IF NOT EXISTS sys_users (
    user_id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    phone VARCHAR(20) UNIQUE NOT NULL,
    role_type VARCHAR(20) DEFAULT 'finance_staff',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contract_projects (
    id SERIAL PRIMARY KEY,
    "合同编号" VARCHAR(100) UNIQUE NOT NULL,
    "合同期限" VARCHAR(50),
    "甲方" VARCHAR(200) NOT NULL,
    "合同金额" NUMERIC(15,2) DEFAULT 0.00,
    "是否已收款" BOOLEAN DEFAULT FALSE,
    "创建时间" TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 向量列已从台账移除（后续单独开向量库，见讨论）；幂等清理旧库残留列
ALTER TABLE contract_projects DROP COLUMN IF EXISTS "特征向量";

-- 4. 赋权
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO finance_app_role;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO finance_app_role;

-- 5. 权限目录与角色-权限绑定
-- 说明：角色标识沿用 sys_users.role_type（相当于 sys_roles 角色目录），
-- 这里只补"权限目录"sys_permissions 与"角色-权限绑定"sys_role_permissions 两张表；
-- 权限检查 = 查"登录用户(实体)的 role_type -> 权限点"的对应关系。
CREATE TABLE IF NOT EXISTS sys_permissions (
    permission_id SERIAL PRIMARY KEY,
    permission_code VARCHAR(64) UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS sys_role_permissions (
    role_type VARCHAR(20) NOT NULL,
    permission_id INTEGER NOT NULL REFERENCES sys_permissions(permission_id) ON DELETE CASCADE,
    PRIMARY KEY (role_type, permission_id)
);

-- 种子权限点（幂等）
INSERT INTO sys_permissions (permission_code) VALUES
    ('field:read'),
    ('field:write'),
    ('entity:decrypt:company'),
    ('entity:decrypt:party'),
    ('entity:decrypt:date'),
    ('entity:decrypt:id_card'),
    ('entity:decrypt:bank_card')
ON CONFLICT (permission_code) DO NOTHING;

-- 角色-权限绑定（幂等）：finance_staff 只读；financial_role / admin 全权限
INSERT INTO sys_role_permissions (role_type, permission_id)
SELECT 'finance_staff', permission_id FROM sys_permissions WHERE permission_code = 'field:read'
ON CONFLICT DO NOTHING;

INSERT INTO sys_role_permissions (role_type, permission_id)
SELECT 'financial_role', permission_id FROM sys_permissions
WHERE permission_code IN ('field:read','field:write','entity:decrypt:company','entity:decrypt:party','entity:decrypt:date','entity:decrypt:id_card','entity:decrypt:bank_card')
ON CONFLICT DO NOTHING;

INSERT INTO sys_role_permissions (role_type, permission_id)
SELECT 'admin', permission_id FROM sys_permissions
WHERE permission_code IN ('field:read','field:write','entity:decrypt:company','entity:decrypt:party','entity:decrypt:date','entity:decrypt:id_card','entity:decrypt:bank_card')
ON CONFLICT DO NOTHING;

-- 6. 实体映射库：分表存储"编号 <-> 真实值"
-- company/party/date：明文可逆（业务上需要直接反查）；
-- id_card/bank_card：只存 sha256 指纹 + Fernet 密文，明文不落盘，
-- 解密必须通过 entity:decrypt:* 权限检查。
CREATE TABLE IF NOT EXISTS entity_mapping_company (
    id SERIAL PRIMARY KEY,
    code VARCHAR(16) UNIQUE NOT NULL,
    norm_key VARCHAR(64) UNIQUE NOT NULL,
    real_value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_mapping_party (
    id SERIAL PRIMARY KEY,
    code VARCHAR(16) UNIQUE NOT NULL,
    norm_key VARCHAR(64) UNIQUE NOT NULL,
    real_value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_mapping_date (
    id SERIAL PRIMARY KEY,
    code VARCHAR(16) UNIQUE NOT NULL,
    norm_key VARCHAR(64) UNIQUE NOT NULL,
    real_value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_mapping_id_card (
    id SERIAL PRIMARY KEY,
    code VARCHAR(16) UNIQUE NOT NULL,
    norm_key VARCHAR(64) UNIQUE NOT NULL,
    masked VARCHAR(64) NOT NULL,
    cipher TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_mapping_bank_card (
    id SERIAL PRIMARY KEY,
    code VARCHAR(16) UNIQUE NOT NULL,
    norm_key VARCHAR(64) UNIQUE NOT NULL,
    masked VARCHAR(64) NOT NULL,
    cipher TEXT NOT NULL
);

-- 7. 补授新表权限（init_db 每次启动都会重跑本脚本，保证新表可被应用角色访问）
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO finance_app_role;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO finance_app_role;
