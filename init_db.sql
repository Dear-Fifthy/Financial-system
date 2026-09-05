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
    "项目" VARCHAR(200),
    "合同期限" VARCHAR(50),
    "甲方" VARCHAR(200) NOT NULL,
    "合同金额" NUMERIC(15,2) DEFAULT 0.00,
    "是否已收款" BOOLEAN DEFAULT FALSE,
    "是否已开票" BOOLEAN DEFAULT FALSE,
    "备注" TEXT,
    "创建时间" TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 向量列已从台账移除（后续单独开向量库，见讨论）；幂等清理旧库残留列
ALTER TABLE contract_projects DROP COLUMN IF EXISTS "特征向量";
-- 台账扩展列（旧库幂等补齐；新库已在 CREATE TABLE 中定义）：
--   "项目"      项目归档名（可大/小项目，层级见 project_archive）
--   "是否已开票" 开票状态，默认未开票，可手动切换
--   "备注"      备注栏（必须存在：AI 依据历史备注学习备注习惯）
ALTER TABLE contract_projects ADD COLUMN IF NOT EXISTS "项目" VARCHAR(200);
ALTER TABLE contract_projects ADD COLUMN IF NOT EXISTS "是否已开票" BOOLEAN DEFAULT FALSE;
ALTER TABLE contract_projects ADD COLUMN IF NOT EXISTS "备注" TEXT;

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

-- 8. 特征哈希库（AI 特征分类用）
-- feature_catalog：特征目录（必选/可选），新增特征只需在此登记种子
CREATE TABLE IF NOT EXISTS feature_catalog (
    feature_code VARCHAR(32) PRIMARY KEY,
    feature_name VARCHAR(64) NOT NULL,
    is_mandatory BOOLEAN NOT NULL DEFAULT FALSE,
    category VARCHAR(32) NOT NULL DEFAULT 'general',
    description TEXT
);

-- 种子特征（幂等）：项目/日期/类型/应收应付 必选；四流/凭证类别 分类用；其余可扩充
INSERT INTO feature_catalog (feature_code, feature_name, is_mandatory, category, description) VALUES
    ('project',      '项目',          TRUE,  'project',    '项目名称（大/小项目分层归档）'),
    ('project_parent', '父项目',       FALSE, 'project',    '大项目哈希（小项目的父级）'),
    ('date_start',   '起始日期',       TRUE,  'date',       'YYYY-MM-DD（分层哈希的日粒度）'),
    ('date_end',     '终止日期',       TRUE,  'date',       'YYYY-MM-DD（分层哈希的日粒度）'),
    ('date_start_year',  '起始年',     FALSE, 'date',       'YYYY（按年检索用）'),
    ('date_start_month', '起始月',     FALSE, 'date',       'YYYY-MM（按月排序用）'),
    ('date_end_year',    '终止年',     FALSE, 'date',       'YYYY（按年检索用）'),
    ('date_end_month',   '终止月',     FALSE, 'date',       'YYYY-MM（按月排序用）'),
    ('doc_type',     '类型',          TRUE,  'type',       '发票/合同/物流凭证/其它'),
    ('money_flow',   '应收/应付',      TRUE,  'money',      '收入/支出方向'),
    ('four_flow',    '四流',          FALSE, 'four_flow',  '合同流/发票流/资金流/货物流 对应关系'),
    ('voucher_kind', '凭证/账簿/报告/其它', FALSE, 'voucher', '归档分类'),
    ('counterparty', '对手方',        FALSE, 'general',    '对方公司/个人（用实体编码表示）'),
    ('payment_term', '付款条款',      FALSE, 'general',    '付款条件/账期'),
    ('tax_kind',     '税种/税率',     FALSE, 'general',    '增值税等')
ON CONFLICT (feature_code) DO NOTHING;

-- file_feature_hashes：每份文件的特征哈希（doc_key + 特征 + 哈希值 + 子序号）
-- value_hash：sha256(归一化值) 截断；seq：分公司/子公司/子项目同抬头下的子序号
CREATE TABLE IF NOT EXISTS file_feature_hashes (
    id BIGSERIAL PRIMARY KEY,
    doc_key VARCHAR(200) NOT NULL,
    feature_code VARCHAR(32) NOT NULL REFERENCES feature_catalog(feature_code),
    value_hash VARCHAR(80) NOT NULL,
    seq INT NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (doc_key, feature_code, seq)
);

-- project_archive：项目归档（大项目 -> 小项目 层级；分公司/子公司同抬头哈希 + 子序号）
CREATE TABLE IF NOT EXISTS project_archive (
    project_id SERIAL PRIMARY KEY,
    parent_id INT REFERENCES project_archive(project_id),
    project_name VARCHAR(200) NOT NULL,
    name_hash VARCHAR(80) NOT NULL UNIQUE,
    seq INT NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 9. RAG 向量库（分块文本 + 向量，只存脱敏内容）
-- 维度必须与 RAG_EMBED_DIM 一致（bge-small-zh = 512）；旧库若已是 1024，
-- rag_store.ensure_table() 会自动迁移（删索引->改类型->重建索引）
CREATE TABLE IF NOT EXISTS document_chunks (
    id BIGSERIAL PRIMARY KEY,
    doc_key VARCHAR(200) NOT NULL,
    chunk_index INT NOT NULL,
    chunk_text TEXT NOT NULL,
    meta JSONB NOT NULL DEFAULT '{}',
    embedding vector(512),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (doc_key, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON document_chunks USING hnsw (embedding vector_cosine_ops);

-- 10. 新增权限点：台账状态人工切换（已收款/已开票）
INSERT INTO sys_permissions (permission_code) VALUES ('ledger:status')
ON CONFLICT (permission_code) DO NOTHING;

INSERT INTO sys_role_permissions (role_type, permission_id)
SELECT 'financial_role', permission_id FROM sys_permissions WHERE permission_code = 'ledger:status'
ON CONFLICT DO NOTHING;

INSERT INTO sys_role_permissions (role_type, permission_id)
SELECT 'admin', permission_id FROM sys_permissions WHERE permission_code = 'ledger:status'
ON CONFLICT DO NOTHING;

-- 11. 收尾再补授一次权限（覆盖上面新建的向量表/索引）
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO finance_app_role;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO finance_app_role;
