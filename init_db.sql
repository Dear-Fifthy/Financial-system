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
    contract_code VARCHAR(100) UNIQUE NOT NULL,
    contract_term VARCHAR(50),
    party_a VARCHAR(200) NOT NULL,
    income NUMERIC(15,2) DEFAULT 0.00,
    is_paid BOOLEAN DEFAULT FALSE,
    raw_ai_vector vector(1536),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 4. 赋权
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO finance_app_role;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO finance_app_role;