-- 1. 创建角色（若已存在则忽略）
-- ⚠️ 口令**不写在文件里**（本文件入库）：下面 `pw` 的初值是字典里的"口令占位符"
--    （形如 __APP + DB_PASS__ 的那个 token，见 .env.example 同名说明），由
--    infra/database_serv__infra.py 的 render_init_sql() 在执行前用 .env 的 APP_DB_PASS 替换。
--    注：注释里**故意不写完整的 token 字面量**——替换是全文替换，写在注释里等于
--    把真口令又抄进内存中的脚本一份。
--    正常入口（会自动注入，推荐）：
--        python -m infra.workspace__infra init-db --all
--        python -c "from infra.database_serv__infra import init_db; print(init_db())"
--    手工 psql 直接跑本文件不会被替换 → 脚本会**直接报错**，而不是建出一个
--    "口令恰好等于占位符"的角色（那种静默错误比报错难查得多）。
DO $$
DECLARE
    pw text := '__APP_DB_PASS__';
BEGIN
    IF pw = '__APP' || '_DB_PASS__' THEN
        RAISE EXCEPTION '口令占位符未被替换：请用 `python -m infra.workspace__infra init-db --all` 执行本脚本';
    END IF;
    BEGIN
        EXECUTE format('CREATE ROLE finance_app_role WITH LOGIN PASSWORD %L', pw);
    EXCEPTION WHEN duplicate_object THEN
        -- 角色已存在：把口令**同步**成 .env 的值（.env 是唯一真相源）。
        -- 于是"改了 .env 的 APP_DB_PASS → 跑 init-db --all"即可生效，
        -- 不必再手工 ALTER ROLE；也避免出现"改了 .env 却和库里不一致"。
        EXECUTE format('ALTER ROLE finance_app_role WITH LOGIN PASSWORD %L', pw);
        RAISE NOTICE '角色已存在，已按 .env 同步口令';
    END;
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
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_login_at TIMESTAMP,
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

-- 发票台账：**入账由用户在"入账审核"窗口决定**（发票识别只负责提名与预填字段，
-- 不再自动写入）。唯一键=发票号码，重复入账按最新版覆盖。
CREATE TABLE IF NOT EXISTS invoice_ledger (
    id SERIAL PRIMARY KEY,
    "发票号码" VARCHAR(64) UNIQUE NOT NULL,
    "发票代码" VARCHAR(64),
    "开票日期" VARCHAR(50),
    "销售方" VARCHAR(200),
    "购买方" VARCHAR(200),
    "金额" NUMERIC(15,2) DEFAULT 0.00,
    "税额" NUMERIC(15,2) DEFAULT 0.00,
    "价税合计" NUMERIC(15,2) DEFAULT 0.00,
    "合同编号" VARCHAR(100),
    "项目" VARCHAR(200),
    "备注" TEXT,
    "创建时间" TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

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
    ('entity:decrypt:bank_card'),
    ('entity:decrypt:tax_id'),
    ('entity:decrypt:bank_name'),
    ('entity:decrypt:bank_account'),
    -- 项目名称（最高管理员自定义加密）：解密项目编号需单独授权
    ('entity:decrypt:project'),
    -- 联系电话/手机号（PII）
    ('entity:decrypt:phone')
ON CONFLICT (permission_code) DO NOTHING;

-- 角色-权限绑定（幂等）：finance_staff 只读；financial_role 白名单；admin 全权限
INSERT INTO sys_role_permissions (role_type, permission_id)
SELECT 'finance_staff', permission_id FROM sys_permissions WHERE permission_code = 'field:read'
ON CONFLICT DO NOTHING;

INSERT INTO sys_role_permissions (role_type, permission_id)
SELECT 'financial_role', permission_id FROM sys_permissions
WHERE permission_code IN ('field:read','field:write','entity:decrypt:company','entity:decrypt:party','entity:decrypt:date','entity:decrypt:id_card','entity:decrypt:bank_card','entity:decrypt:tax_id','entity:decrypt:bank_name','entity:decrypt:bank_account','entity:decrypt:project','entity:decrypt:phone')
ON CONFLICT DO NOTHING;

-- ⚠️ admin 一律"授权目录里的**全部**权限"，不要写成白名单：
--    写成列举式的话，以后新增权限点（比如新类别的 entity:decrypt:xxx）会**忘记**给 admin，
--    而 `require_permission` 的内存兜底表里 admin 只有 4 个点 → 最高管理员反而被自己的
--    系统挡住（实测风险点）。这里每次 init_db 都按目录补全，幂等。
INSERT INTO sys_role_permissions (role_type, permission_id)
SELECT 'admin', permission_id FROM sys_permissions
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
-- 分支编号（需求）：同一主体的不同分支共用**主编号**，用后缀区分。
-- 例：PJ0007（主项目）/ PJ0007-01（一标段）；公司同理 CO0012 / CO0012-01（某分公司）。
-- 提示词里已声明该约定（见 desens_legend.BRANCH_RULE_TEXT），此处提供编码支撑。
ALTER TABLE entity_mapping_company ADD COLUMN IF NOT EXISTS parent_code VARCHAR(16);
ALTER TABLE entity_mapping_company ADD COLUMN IF NOT EXISTS branch_seq INT;
CREATE INDEX IF NOT EXISTS idx_company_parent ON entity_mapping_company (parent_code);

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

-- 新增（按需求）：纳税人识别号 / 开户银行 / 银行账号（含对公账号）
-- 三者均属敏感类别：只存 sha256 指纹 + Fernet 密文，同值同码（重复值复用同一编号）
CREATE TABLE IF NOT EXISTS entity_mapping_tax_id (
    id SERIAL PRIMARY KEY,
    code VARCHAR(16) UNIQUE NOT NULL,
    norm_key VARCHAR(64) UNIQUE NOT NULL,
    masked VARCHAR(64) NOT NULL,
    cipher TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_mapping_bank_name (
    id SERIAL PRIMARY KEY,
    code VARCHAR(16) UNIQUE NOT NULL,
    norm_key VARCHAR(64) UNIQUE NOT NULL,
    masked VARCHAR(64) NOT NULL,
    cipher TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_mapping_bank_account (
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

-- 12. 最高管理员（admin）单例约束：任何时刻至多一个 role_type='admin'
--     首个注册用户自动成为 admin（register_user 先查后插，本索引兜底并发竞态：
--     两人同时通过"无 admin"检查时，唯一索引只放行一个，失败者降级为普通角色）；
--     转让最高管理员时在同一事务内 旧主降级 -> 新主提升，本索引保证全程唯一。
--     注意：部分唯一索引的键必须是"所有 admin 行取值相同的列"——即谓词列
--     role_type 本身（不能用 user_id，主键天然唯一，永远不冲突）。
CREATE UNIQUE INDEX IF NOT EXISTS uq_sys_users_single_admin
    ON sys_users (role_type) WHERE role_type = 'admin';

-- 13. sys_users 扩展：账号停用标记 + 最近登录时间（老库幂等补齐；新库见上方建表语句）
--     is_active=FALSE 的账号登录被拒（authenticate_user 校验），用于"注销账户/停用"。
ALTER TABLE sys_users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE sys_users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP;

-- 14. L1 状态投影：文档与段落记录（**只做记录，不动源目录**）
--     文档 id = doc_no（稳定、跨运行不变）；段落 id = "doc_no-seg_index"（如 12-3）。
--     doc_key 取 hub 扁平 JSON 的文件名（不含扩展名）。file_hash/text_hash 用于去重与幂等。
CREATE TABLE IF NOT EXISTS l1_documents (
    doc_no        SERIAL PRIMARY KEY,               -- 文档唯一 id：n
    doc_key       VARCHAR(300) NOT NULL,
    extract_version VARCHAR(32) NOT NULL DEFAULT 'v1',
    file_hash     VARCHAR(64)  NOT NULL,            -- 源文件字节 sha256
    text_hash     VARCHAR(64)  NOT NULL,            -- 归一化文本 sha256
    hub_hash      VARCHAR(64),                      -- hub JSON 本体 sha256（内容指纹/幂等）
    category      VARCHAR(32),                      -- 分类（后置：检索/建表之后再补）
    doc_summary   TEXT,                             -- 文档概括：50~80 字
    page_count    INT NOT NULL DEFAULT 0,
    table_count   INT NOT NULL DEFAULT 0,           -- hub 里的表块数
    segment_count INT NOT NULL DEFAULT 0,           -- 段落数
    fact_count    INT NOT NULL DEFAULT 0,           -- 细粒度事实条数（l1_facts）
    source_path   TEXT,                             -- 源文件路径（仅记录，不修改）
    model         VARCHAR(64),                      -- 概括所用模型（deepseek-flash）
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (doc_key, extract_version)
);

-- 老库补齐（幂等）
ALTER TABLE l1_documents ADD COLUMN IF NOT EXISTS hub_hash VARCHAR(64);
ALTER TABLE l1_documents ADD COLUMN IF NOT EXISTS table_count INT NOT NULL DEFAULT 0;
ALTER TABLE l1_documents ADD COLUMN IF NOT EXISTS segment_count INT NOT NULL DEFAULT 0;
ALTER TABLE l1_documents ADD COLUMN IF NOT EXISTS fact_count INT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS l1_segments (
    seg_id        VARCHAR(80) PRIMARY KEY,          -- "doc_no-seg_index"
    doc_key       VARCHAR(300) NOT NULL,
    extract_version VARCHAR(32) NOT NULL DEFAULT 'v1',
    seg_index     INT NOT NULL,                     -- n1
    page_from     INT NOT NULL,                     -- 跨页则记录首尾页
    page_to       INT NOT NULL,
    char_count    INT NOT NULL,
    seg_summary   TEXT,                             -- 段落概括：≤50 字
    text_hash     VARCHAR(64) NOT NULL,
    path_json     JSONB NOT NULL DEFAULT '{}',      -- 指向原文的路径（doc_key/页/字符区间）
    model         VARCHAR(64),
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (doc_key, extract_version, seg_index)
);

-- 14b. hub 资产索引：**hub JSON 本体**的哈希 + 概括 + 特征哈希汇总（概括/哈希的对应表）
--      为什么单独一张表：hub JSON 里只有脱敏正文/表格/坐标（不含概括与哈希），
--      "这份 hub 是哪一版、有没有被投影过、概括/哈希是什么"需要一处可查；
--      伴生文件 hub/<stem>.l1.json 存同样内容，便于随 hub 直接读取。
CREATE TABLE IF NOT EXISTS hub_index (
    id SERIAL PRIMARY KEY,
    doc_key           VARCHAR(300) NOT NULL,
    extract_version   VARCHAR(32) NOT NULL DEFAULT 'v1',
    hub_file          VARCHAR(400) NOT NULL,
    hub_hash          VARCHAR(64)  NOT NULL,        -- hub JSON 字节 sha256
    content_hash      VARCHAR(64)  NOT NULL,        -- 脱敏正文归一化 sha256
    file_hash         VARCHAR(64),                  -- 源文件字节 sha256
    page_count        INT NOT NULL DEFAULT 0,
    table_count       INT NOT NULL DEFAULT 0,
    cell_count        INT NOT NULL DEFAULT 0,       -- 单元格级锚点数
    table_anchor_count INT NOT NULL DEFAULT 0,      -- 其中带原文区间的锚点数
    offset_map_count  INT NOT NULL DEFAULT 0,       -- 有偏移映射的页数
    segment_count     INT NOT NULL DEFAULT 0,
    doc_summary       TEXT,                         -- 文档概括（L1 产出）
    summary_model     VARCHAR(64),
    feature_hashes    JSONB NOT NULL DEFAULT '{}'::jsonb,  -- 特征哈希汇总（来自 file_feature_hashes）
    feature_hash_bundle VARCHAR(64),                -- 特征哈希整体 sha256（一眼判等）
    source_path       TEXT,
    sidecar_file      VARCHAR(400),                 -- hub/<stem>.l1.json
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (doc_key, extract_version)
);

-- 14c. L1 细粒度事实清单（取数层）：表格单元格 + 文本键值行，每条都带溯源路径
--      （页/表块/行列/Excel 坐标/脱敏文本区间/原文区间/同行上下文），
--      值为**已脱敏**的值；概括只作定位索引，取数靠这张表回原文核对。
CREATE TABLE IF NOT EXISTS l1_facts (
    fact_id        VARCHAR(400) PRIMARY KEY,        -- {doc_key}|cell|p1|t1|r2|c3 等（稳定）
    doc_key        VARCHAR(300) NOT NULL,
    extract_version VARCHAR(32) NOT NULL DEFAULT 'v1',
    fact_kind      VARCHAR(16) NOT NULL,            -- cell | kv
    page_no        INT,
    table_index    INT,
    row_index      INT,
    col_index      INT,
    coord          VARCHAR(16),                     -- Excel 坐标（如 B3；OCR 表为 NULL）
    header         VARCHAR(120),                    -- 列头 / 键名
    semantic       VARCHAR(32),                     -- 语义类别（company/tax_id/…；认不出为 NULL）
    value          TEXT NOT NULL,                   -- 脱敏后的值
    value_norm     VARCHAR(160),
    value_is_code  BOOLEAN NOT NULL DEFAULT FALSE,
    char_start     INT,
    char_end       INT,
    raw_char_start INT,
    raw_char_end   INT,
    raw_exact      BOOLEAN,
    row_context    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_l1_facts_doc ON l1_facts (doc_key);
CREATE INDEX IF NOT EXISTS idx_l1_facts_semantic ON l1_facts (semantic);

-- 14d. 图遍历层（L3）前置：节点与边
--      节点来自 l1_facts（doc/table/row/entity）；边来自"候选对 → AI 判定 → **本地重算校验**"，
--      status: validated（可被 L3 直接取用）/ hypothesis（假设边，需人工或更多证据）/ rejected。
CREATE TABLE IF NOT EXISTS graph_nodes (
    node_id     VARCHAR(400) PRIMARY KEY,
    node_type   VARCHAR(16) NOT NULL,               -- doc | table | row | entity
    doc_key     VARCHAR(300),
    label       VARCHAR(200),
    value       TEXT,
    semantic    VARCHAR(32),
    category    VARCHAR(32),
    page_no     INT,
    table_index INT,
    row_index   INT,
    coord       VARCHAR(16),
    fact_id     VARCHAR(400),
    path_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS graph_edges (
    edge_id        VARCHAR(40) PRIMARY KEY,
    src_node       VARCHAR(400) NOT NULL,
    dst_node       VARCHAR(400) NOT NULL,
    relation       VARCHAR(48) NOT NULL,
    status         VARCHAR(16) NOT NULL,            -- validated | hypothesis | rejected
    confidence     DOUBLE PRECISION NOT NULL DEFAULT 0,
    confidence_self DOUBLE PRECISION,               -- 模型自评（仅留痕，不采信）
    evidence       JSONB NOT NULL DEFAULT '{}'::jsonb,
    missing        JSONB NOT NULL DEFAULT '[]'::jsonb,
    conflict       BOOLEAN NOT NULL DEFAULT FALSE,
    method         VARCHAR(16) NOT NULL,            -- agent | api | rule
    model          VARCHAR(64),
    reason         TEXT,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (src_node, dst_node, relation)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON graph_edges (src_node);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON graph_edges (dst_node);
CREATE INDEX IF NOT EXISTS idx_edges_status ON graph_edges (status);

-- 15. 本公司（我方主体）：最高管理员登录时确认的公司全称，全局脱敏（编号带 [本公司·CO####] 标记）
--     名称只存 Fernet 密文 + sha256 指纹；编号复用公司类别（同一家公司全库同码）。
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
);

-- 14e. 内容级去重索引（防止同一文件换路径后重复扫描、生成第二套节点）
--      content_hash = 源文件字节 sha256；canonical_doc_key = 首次扫描建档的 doc_key；
--      source_paths = 该内容出现过的所有路径；duplicate_doc_keys = 被跳过的重复建档；
--      scan_count/skip_count = 实际扫描次数 / 跳过次数（可审计）。
CREATE TABLE IF NOT EXISTS doc_content_index (
    content_hash        VARCHAR(64) PRIMARY KEY,
    canonical_doc_key   VARCHAR(300) NOT NULL,
    canonical_hub_file  VARCHAR(400),
    source_paths        JSONB NOT NULL DEFAULT '[]'::jsonb,
    duplicate_doc_keys  JSONB NOT NULL DEFAULT '[]'::jsonb,
    scan_count          INT NOT NULL DEFAULT 1,
    skip_count          INT NOT NULL DEFAULT 0,
    first_seen          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_content_canonical ON doc_content_index (canonical_doc_key);

-- 14f. 退役墓碑：被删除 / 被新副本替代的文档记录
--      · doc_no 记下退役的编号 —— **永久留空、后面的新文件不填充这个编号**；
--      · replaced_by 记录"被哪份新文档替代"（副本替换场景）；
--      · removed 记录各表/文件的清理计数，便于审计。
CREATE TABLE IF NOT EXISTS deleted_documents (
    id SERIAL PRIMARY KEY,
    doc_no INT,
    doc_key VARCHAR(300) NOT NULL,
    file_hash VARCHAR(64),
    hub_hash VARCHAR(64),
    content_hash VARCHAR(64),
    replaced_by VARCHAR(300),
    reason TEXT,
    deleted_by VARCHAR(64),
    deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    removed JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_deleted_doc_no ON deleted_documents (doc_no);
CREATE INDEX IF NOT EXISTS idx_deleted_doc_key ON deleted_documents (doc_key);

-- 15. 项目名称加密登记（需求 4：最高管理员自定义加密 + 分类 + 与 AI 判断交叉验证）
--     · entity_mapping_project：编号 PJ#### + sha256 指纹 + Fernet 密文（明文不落盘）；
--     · project_registry：登记项（名称/简称均密文；分类"已有即选、未有可加"）；
--     · project_categories：分类字典；
--     · project_proposals：AI 提案（未命中登记表时入库，等管理员审批 = AI提案、我审批）。
CREATE TABLE IF NOT EXISTS entity_mapping_phone (
    id SERIAL PRIMARY KEY,
    code VARCHAR(16) UNIQUE NOT NULL,
    norm_key VARCHAR(64) UNIQUE NOT NULL,
    masked VARCHAR(64) NOT NULL,
    cipher TEXT NOT NULL
);

-- 编号发号水位（**跨删除单调**）：编号一旦发出去就不再回收。
-- 历史实现按 `MAX(id)+1` 发号，删掉末尾登记后水位回落 → 新实体会拿到**已被删掉的
-- 旧编号**，而历史 hub 正文里那个编号还指着老实体（实测清洗 22 条后 CO0126 被复用）。
CREATE TABLE IF NOT EXISTS entity_code_seq (
    category VARCHAR(32) PRIMARY KEY,
    last_seq INT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS entity_mapping_project (
    id SERIAL PRIMARY KEY,
    code VARCHAR(16) UNIQUE NOT NULL,
    norm_key VARCHAR(64) UNIQUE NOT NULL,
    masked VARCHAR(64) NOT NULL,
    cipher TEXT NOT NULL
);
-- 项目分支（标段/片区/期次）：PJ0007 与 PJ0007-01 共用主编号，见 desens_legend 的约定
ALTER TABLE entity_mapping_project ADD COLUMN IF NOT EXISTS parent_code VARCHAR(16);
ALTER TABLE entity_mapping_project ADD COLUMN IF NOT EXISTS branch_seq INT;
CREATE INDEX IF NOT EXISTS idx_project_parent ON entity_mapping_project (parent_code);
-- 项目登记表同样带分支列（管理员界面按"主项目+分支"登记）；放在 CREATE 之后，
-- 否则**空库首次初始化**会报 `project_registry 不存在`（ALTER 不能先于建表）
CREATE TABLE IF NOT EXISTS project_categories (
    id SERIAL PRIMARY KEY,
    name VARCHAR(64) UNIQUE NOT NULL,
    created_by VARCHAR(64),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

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
);

-- 项目登记表同样带分支列（管理员界面按"主项目+分支"登记）
ALTER TABLE project_registry ADD COLUMN IF NOT EXISTS parent_code VARCHAR(16);
ALTER TABLE project_registry ADD COLUMN IF NOT EXISTS branch_seq INT;

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
);

-- 动态建图状态：每份文档是否已建图 + 指纹（facts_count/hub_hash）。
-- 指纹没变 = 重复扫描/重复调用 → 跳过（不重复建）；变了 → 重跑并 UPSERT（更新照常）。
-- 同时作为"存量优先补齐"的待建队列来源（库里有、状态表里没有 = 从未建图）。
CREATE TABLE IF NOT EXISTS graph_build_state (
    doc_key VARCHAR(300) PRIMARY KEY,
    built_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    facts_count INT DEFAULT 0,
    hub_hash VARCHAR(64),
    nodes INT DEFAULT 0,
    candidates INT DEFAULT 0,
    stored INT DEFAULT 0,
    status VARCHAR(24) DEFAULT 'built',
    note TEXT
);

-- 边判定缓存（方向无关的 pair_key + 信号指纹）：指纹不变就不重复调模型，变了才重判。
CREATE TABLE IF NOT EXISTS graph_pair_cache (
    pair_key VARCHAR(600) PRIMARY KEY,
    signature VARCHAR(64) NOT NULL,
    relation VARCHAR(48),
    confidence_self DOUBLE PRECISION,
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    reason TEXT,
    model VARCHAR(64),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 待入账队列：识别负责"提名"，人负责"入账"（扫描不再自动写台账）。-- 扫描把候选（合同/发票）连同已脱敏字段放进来，用户在"入账审核"窗口决定入不入账；
-- 状态：pending 待入账 / posted 已入账 / rejected 不入账（保留记录可追溯）。
CREATE TABLE IF NOT EXISTS ledger_inbox (
    id SERIAL PRIMARY KEY,
    doc_key VARCHAR(300) NOT NULL UNIQUE,
    kind VARCHAR(20) NOT NULL DEFAULT 'other',
    category VARCHAR(50),
    target_table VARCHAR(64),
    title VARCHAR(300),
    source_file VARCHAR(400),
    source_path TEXT,
    hub_file TEXT,
    fields JSONB NOT NULL DEFAULT '{}'::jsonb,
    fields_source VARCHAR(30),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    note TEXT,
    created_by VARCHAR(64),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    decided_by VARCHAR(64),
    decided_at TIMESTAMP,
    posted_table VARCHAR(64),
    posted_key VARCHAR(200)
);

GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO finance_app_role;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO finance_app_role;
