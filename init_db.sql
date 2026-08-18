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
    "特征向量" vector(1536),
    "创建时间" TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 4. 赋权
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO finance_app_role;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO finance_app_role;