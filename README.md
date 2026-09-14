# 财务文档工作台（Finance Doc Workbench）

把一堆**表格 / PDF / 图片 / Word** 财务单据，变成**可检索、可核对、可入账**的结构化数据；
全程**本地 OCR + 本地脱敏**，AI 只能看到脱敏产物，任何写库动作都要人确认。

- 界面：PySide6 桌面端（主窗 + 若干管理对话框）
- 数据：PostgreSQL（每套"仓库"独立一个库）
- AI：四条互相隔离的链路（概括 / 建边 / 图查询 / 对话），密钥各自独立
- 平台：Windows（当前只在 Windows 上验证过）
- **思路简介在docs里~（我觉得最好看看）**

---

## 一、功能

### 1. 文档入库（扫描 → 结构化）

- 支持拖入 **PDF / 图片（png jpg bmp tif webp）/ Word（docx，doc 走转换）/ Excel（xlsx，xls 走转换）**，也可以整文件夹拖入（保留子目录结构）。
- **能直读就绝不 OCR**：docx/xlsx 直接解析，只有图片和扫描件才进 OCR（PaddleOCR-VL，本地推理）。
- Excel 里"看起来是数字其实是日期"的单元格，先按单元格格式还原成日期，避免把日期列打成卡号。
- OCR 表格会展开合并单元格（colspan/rowspan），保留**单元格坐标**（页/表/行列）。
- **单页 OCR 硬上限 120 秒**：超时直接放弃这一份文件（不写半成品），随后冷却几分钟再允许新扫描——避免界面卡死十几分钟。
- **协作式停止**：点"停止扫描"后当前页跑完即止，且**不动源文件**；已落盘的页缓存可复用。
- **内容级去重**：同一文件重复拖入 / 换路径再拖入 / 文件名带"（2）副本"，会被识别出来（跳过或按覆盖语义把旧的整套痕迹退役后重跑），不会出现两份并存。
- 每份文件产出：一份脱敏 JSON + 一份 L1 伴生文件（概括/哈希）+ 一份 AI 日志。

### 2. 脱敏（编号化，AI 只见编号）

- **编号体系**：`CO` 公司 / `PT` 人员 / `DT` 日期 / `ID` 身份证 / `BC` 银行卡 / `TX` 税号 / `BK` 开户行 / `BA` 银行账号 / `PJ` 项目 / `PH` 电话；本公司显示为 `[本公司·CO0001]`。
- 映射表分两类：公司/人员/日期**明文可逆**（业务要反查）；身份证/银行卡/税号/开户行/账号/项目/电话**只存指纹 + 密文**（解密需单独授权）。
- **编号只增不减**（发号水位表），避免清洗脏登记后编号被复用、老文档里编号含义悄悄改变。
- 结构化脱敏：先判"这块是什么"（标题/签章/kv 表/普通表/说明块）再按布局脱敏，签章与说明块不硬套列头语义。
- 自由文本脱敏：证件/卡号/日期/税号/账号/电话/公司/人员逐类替换；再做两遍补漏（换排版的数字、已登记实体的再次出现）。
- 同一真实值在整份文件（含跨页）只用同一个编号。
- 异常时**向"多打码"倒**（宁可整块打码，也不留明文）。
- 完整性自检：正文里的编号必须都能在映射表里查到解释（**孤儿 = 0**）；脏登记/死登记可扫描并可清理（先备份、写审计）。

### 3. 分层与索引

| 层 | 内容 | 用途 |
|---|---|---|
| L0 | hub 里的脱敏正文 + 表结构 + 坐标 | **唯一允许 AI 读取的输入源**（越界直接拒绝） |
| L1 | 概括 + 特征哈希 | 只作定位索引（"大概哪份文档讲什么"） |
| L2 | 事实清单（带文档/页/表/行列/坐标/字符区间） | **取数只从这里**，然后回原文核对 |
| L3 | 关系图（节点 + 边 + 置信度 + 依据） | 追溯单据之间的关系 |
| L4 | 问答（需求判断 + 检索取证 + 作答 + 复核） | 和人交互 |

### 4. 待入账审核（AI 提名、人工入账）

- 扫描**不会自动写台账**：判定为合同/发票的文档进"待入账"队列（含已脱敏字段预填）。
- 「**从 hub 刷新**」：直接读 hub 里已有 JSON 重新提名（不重扫、不 OCR、不调 AI；库被清过、hub 从别处拷来也能补）。
- 「**AI 读取并填表**」：让 AI 读脱敏正文 + 台账历史"项目/备注"习惯，把字段填好（**只填不写库**）。
  合同 → 合同台账字段；发票 → 发票台账字段（**都由 AI 读正文抽取**，同样走 `.env` 的 `AI_*` 链路与同一个模型；
  金额自动清洗成纯数字、空值写 NULL、唯一键缺失时明确提示人工补）。
- 「批量重判」：按新口径对库里已有文档重新跑合同判定。
- 人工可改判类型、改字段、点「入账」或「驳回」；每次操作留痕。

### 5. 台账

- 两张台账：**合同台账**、**发票台账**；字段随台账切换（合同看编号/项目/金额/已收款/已开票；发票看发票号码/开票日期/销售方/购买方/金额/税额/价税合计…）。
- 「台账状态」：勾选切换"已收款/已开票"（发票台账没有这两列，会明确提示去合同台账改）。
- 「**台账全字段管理**」（仅最高管理员）：看全部字段（含 id/创建时间）、**双击改任意一格**（按类型校验、按主键定位、逐格写审计）。
- 「**导出为 Excel / CSV**」：导出到指定位置，表头就是台账列名（xlsx 冻结首行；csv 用 utf-8-sig 防乱码）。
- 台账**列结构**可增删改（列名白名单 + 实际表列校验）。

### 6. 关系图与问答

- **建图**：扫描完一份即后台建图（单飞队列，一次只建一份，空闲自动退出）；也可手动「建图/补齐…」补存量。
- 候选对由本机按共享信号筛（合同号/金额/项目/对手方/日期…），交由 AI 判定关系，**置信度与状态由本机规则重算**（不采信模型自评）；假设边需人工确认才参与遍历。
- **图遍历查询**：从编号/单号锚点沿证据链取证，输出"值 + 置信度 + 溯源路径"。
- **索引卡召回**：每份文档一行（特征明文 + 概括 + 关键事实）做粗召回。
- **需求判断层**：先判断"这问题要什么"——能本机算的（多少份、哪些文件、字段值）直接 SQL 作答（0 token）；要资料的先确定"需要哪几类证据"，按类别召回后由对话模型作答（先讲一般规则，再讲"在你库里我看到"）。
- **检查 AI 复核**：只输出"支持/不确定/可能有错"，用于提醒人工，不当放行闸门。
- 对话记录、思考过程、每次检索命中的文档都落盘可查。

### 7. 仓库（工作区）隔离

- **一个"仓库" = 一套完整环境**：独立数据库 + 独立 hub/输入输出目录 + **独立加密密钥** + 独立用户表。
- 切换仓库需要重新登录（账号体系也换了）；A 仓库的密文用 B 仓库的密钥解不开。
- 破坏性操作（清空/删除仓库、删除文档、删除登记）一律"**改名留档 + 写审计 + 可回滚**"。

### 8. 权限

- 三层角色：**最高管理员 / 财务主管 / 财务专员**；13 个权限点（读字段、改表结构、切台账状态 + 10 类敏感字段解密）。
- 角色是"业务层抽象职责"，账号只绑角色 → **换人不换权限配置**（离职停用、新人绑同一角色即可）。
- 服务端强制校验（界面禁用只是体验层）；仅最高管理员的功能：用户管理、项目/本公司加密、Hub 状态与格式化、脱敏登记表、**台账全字段管理与导出**。

### 9. 诊断与自检（命令行工具）

模块按层归入子目录，命令统一用 `python -m <层>.<模块>`（在仓库根执行）：

| 命令 | 作用 |
|---|---|
| `python -m infra.workspace__infra list / info / self-test` | 仓库列表/体检、隔离自检（文档互斥、编号独立、密钥互不可解、权限全覆盖） |
| `python -m infra.sample_corpus__infra --workspace sample --verify` | 合成样例语料的装载、真值自检、两条召回腿对比（不需要 OCR/AI） |
| `python -m infra.project_audit__infra` | 项目体检：临时文件、空文件、孤儿模块、目录结构、漏进版本库的敏感文件/凭据 |
| `python -m ai.query_planner__ai --rules / --overview / --plan "问题"` | 需求判断：规则路由 / 当前仓库统计 / 完整规划 |
| `python -m graph.doc_index__graph` | 生成"文档索引卡"（特征明文 + 概括 + 关键事实） |
| `python -m scan.scanner_entrance__scan` | 无界面批量扫描 `input/` |
| `python -m scan.benchmark_ocr__scan`、`python -m infra.monitor__infra` | OCR 压测、性能采样 |

> 也可以直接按文件路径跑（每个可独立运行的入口都带 bootstrap 头，会把仓库根放回 `sys.path`）：
> `.\.venv\Scripts\python.exe scan\scanner_entrance__scan.py`。

---

## 二、依赖

### 1. 运行环境

| 项 | 要求 | 说明 |
|---|---|---|
| 操作系统 | **Windows 10/11** | 用到了 `os.startfile`，`.doc/.xls` 转换依赖本机 Office/WPS COM 或 LibreOffice |
| Python | **3.13.x**（开发环境 3.13.14） | 建议用虚拟环境 |
| 显卡 | NVIDIA GPU（显存按 OCR 需求配置） | OCR 走 PaddlePaddle-GPU；显存不足会走 CPU 但极慢，可用 `.env` 的 `OCR_DEVICE` 强制 |
| PostgreSQL | **18.x** + 扩展 **`vector`（pgvector 0.8.6）** | 库里另需 `plpgsql`（默认自带）；建表脚本见 `init_db.sql` |
| 磁盘 | 预留若干 GB | `output/` 放逐页 OCR 缓存，`hub/` 放脱敏产物 |

### 2. Python 包（版本为本机实测通过）

| 包 | 实测版本 | 用途 |
|---|---|---|
| `PySide6` | 6.11.1 | 全部界面 |
| `psycopg2-binary` | 2.9.12 | PostgreSQL 访问 |
| `paddlepaddle-gpu` | 3.3.1 | OCR 推理框架（`import paddle`） |
| `paddleocr` | 3.7.0 | **PaddleOCR-VL**，本地 OCR |
| `PyMuPDF`（`import fitz`） | 1.28.0 | PDF 原生文本抽取（抽不到才走 OCR） |
| `openpyxl` | 3.1.5 | xlsx 直读 + 台账导出 Excel |
| `pandas` | 3.0.5 | 表格读取（部分路径） |
| `cryptography` | 50.0.0 | 敏感字段 Fernet 加密 |
| `python-dotenv` | 1.2.2 | 读取 `.env` |
| `requests` | 2.34.2 | 调用各 AI 链路 |
| `sentence-transformers` | 6.0.0 | 向量检索（RAG）用的 embedding |
| `torch` | 2.13.0 | sentence-transformers 的运行时 |
| `psutil` | 7.2.2 | 性能采样（内存/进程） |

> 仓库暂未提供 `requirements.txt`。上面这些版本是本机验证过的组合，可直接按版本安装；
> 也可以只装你需要的部分（例如不用向量检索就可以省掉 `sentence-transformers` + `torch`）。
> `.docx` 与 `.xlsx` 的读取**不依赖** `python-docx`/`xlrd`，走标准库 XML 解析。

### 3. 外部服务与密钥（按需，全部可关）

| 链路 | 环境变量前缀 | 推荐模型 | 用途 | 不配会怎样 |
|---|---|---|---|---|
| 读取/概括/填表 | `AI_*` | `deepseek-flash` | 分段概括、特征提取、台账字段填写 | 概括与特征留空，其它功能照常 |
| 建边 | `EDGE_AI_*` | `qwen3.8-flash` | 判断两份文档的关系 | 建不了新边，已有边仍可查 |
| 图查询/规划 | `GRAPH_AI_*`（**独立 key，不回退**） | `qwen3.8-flash` | 图遍历取证 + 需求判断 | 需求判断关闭，退回"一刀切检索" |
| 对话 | `CHAT_AI_*` | `qwen3.8-max` | 最终作答 | 无法对话 |
| 向量检索（可选） | `RAG_EMBED_*` / `AI_RAG_ENABLED` | bge 系列本地模型 | 索引卡之外的语义召回 | 只用图与索引卡，不影响主流程 |

> 说明：AI 调用是**出网**的，只发送 hub 里的脱敏文本；模型 key 只写在 `.env`（已被 `.gitignore` 忽略）。
> 首次使用向量检索需要联网下载 embedding 模型。

---

## 三、快速开始

```powershell
# 1) 建虚拟环境并装依赖（版本见上表）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install PySide6 psycopg2-binary paddlepaddle-gpu paddleocr `
    PyMuPDF openpyxl pandas cryptography python-dotenv requests psutil

# 2) 配置 .env（从模板复制，按需填写）
#    至少要填：POSTGRES_ADMIN_PASSWORD（管理员，用于建库/建表）
#              APP_DB_PASS（应用角色口令；init_db.sql 里只有占位符，建库时由它注入）
Copy-Item .env.example .env

# 3) 准备数据库（会自动建库、建表、建角色、灌权限目录）
#    口令从 .env 注入，绝不写进 init_db.sql（入库文件）；
#    手工 `psql -f init_db.sql` 不会被注入，脚本会直接报错提醒。
.\.venv\Scripts\python.exe -c "from infra.database_serv__infra import init_db; print(init_db())"

# 4) 把当前环境登记为第一个"仓库"（沿用现有库与目录，零迁移）
.\.venv\Scripts\python.exe -m infra.workspace__infra register-legacy --name 我的账套

# 5) 启动界面（或直接双击 launch_table.bat）
.\.venv\Scripts\python.exe ui\table__ui.py
```

首次登录：**第一个注册的账号自动成为最高管理员**（之后注册的都是财务专员，
可在「设置 → 用户管理」里调整）。

> **改口令**：只改 `.env` 的 `APP_DB_PASS`，再跑 `python -m infra.workspace__infra init-db --all`
> 即同步到数据库角色（`init_db.sql` 在角色已存在时会执行 `ALTER ROLE … PASSWORD`）。
> 改完请重启程序——口令是在导入期读进内存的。

## 四、配置项（`.env`，共约 60 项，按用途分组）

| 分组 | 键 | 说明 |
|---|---|---|
| 数据库 | `POSTGRES_ADMIN_PASSWORD` `DB_HOST` `DB_PORT` `DB_ADMIN_USER` `DB_ADMIN_NAME` `APP_DB_NAME` `APP_DB_USER` `APP_DB_PASS` | 管理员连接用于 DDL；应用连接权限受限 |
| OCR | `OCR_DEVICE` `OCR_ENGINE` `OCR_REQUIRED_VRAM_MB` `OCR_MIN_FREE_VRAM_MB` `OCR_FAIL_THRESHOLD` `OCR_PAGE_TIMEOUT_S` `OCR_SLOW_REBUILD_LIMIT` `OCR_REBUILD_COOLDOWN_S` `OCR_REBUILD_MAX` | 设备/显存门槛、熔断、单页超时与放弃冷却 |
| AI 总开关 | `AI_ENABLED` | 关闭后不调用任何 AI（概括/特征留空，其它照常） |
| 读取链路 | `AI_API_KEY` `AI_BASE_URL` `AI_MODEL` `AI_MODEL_JSON` `AI_TIMEOUT` | 概括/特征/填表 |
| 建边链路 | `EDGE_AI_*` `EDGE_MAX_PAIRS_PER_DOC` `EDGE_AUTO_BACKLOG` | 候选上限、启动自动补齐开关 |
| 图查询链路 | `GRAPH_AI_*` | **独立 key**，不复用其它链路 |
| 对话链路 | `CHAT_AI_*`（含 `ENABLE_THINKING` `THINKING_BUDGET` `RECORD_THINKING` `MAX_TOKENS` `TIMEOUT`） | 思考过程可记录/可关 |
| 检索 | `RETRIEVAL_METHOD`（`graph`/`rag`）`QUERY_PLANNER` `QUERY_PLANNER_RULES` | 全局检索方式；需求判断层与其规则快路开关 |
| 向量检索 | `AI_RAG_ENABLED` `RAG_EMBED_*` | embedding 后端/维度/分块 |
| 行为 | `CONTRACT_TITLE_TOKENS` `DUP_POLICY` `RESCAN_FORCE` `AI_LOG_REASONING` `AI_LOG_REASONING_MAX` | 合同标题词、重复策略、强制重扫、AI 日志是否记思考正文 |
| 仓库 | `DSH_HUB_DIR` `ACTIVE_WORKSPACE`（由程序维护） | 当前仓库的 hub 根与环境指针 |

## 五、目录结构

```
├─ ui/           界面层（15 个）
│   ├─ table__ui.py                     主窗口（入口）
│   ├─ ui_kit__ui.py                    自适应布局/字号/分栏工具
│   └─ chat_panel__ui.py、*_admin__ui.py 对话面板与各管理窗口
├─ scan/         扫描层（9 个）
│   ├─ scanner_core__scan.py            扫描核心（OCR/超时/熔断）
│   ├─ scanner_entrance__scan.py        无界面批量入口
│   └─ table_split__scan.py、ocr_priority__scan.py … 版面切分/优先级/资源闸门
├─ desens/       脱敏层（16 个，最大的一层）
│   ├─ hub_pipeline__desens.py          扫描→脱敏→落盘流水线
│   ├─ table_desens__desens.py          表格布局判定与脱敏
│   ├─ office_reader__desens.py         docx/xlsx 直读
│   └─ ocr_tables__desens.py、offset_map__desens.py … 表格解析/坐标偏移
├─ l1/           L1 层（2 个）
│   ├─ l1_extract__summary_hash.py      概括 + 特征哈希
│   └─ l1_facts__fact_list.py           事实清单（取数层）
├─ graph/        图层（3 个）
│   ├─ doc_index__graph.py              文档索引卡
│   ├─ edge_build__graph_edges.py       关系边构建
│   └─ graph_query__graph_walk.py       图遍历查询
├─ rag/          RAG 层（2 个）
│   ├─ rag_store__rag.py                分块/存储/召回
│   └─ embed_worker__rag.py             embedding 子进程
├─ ai/           AI 层（5 个）
│   ├─ ai_client__ai.py                 四条隔离链路的客户端
│   ├─ ai_parser__ai.py                 字段解析/填表
│   ├─ chat_engine__ai.py               对话链路
│   └─ query_planner__ai.py             需求判断（规则 + AI 规划）
├─ infra/        基础设施层（5 个）
│   ├─ database_serv__infra.py          数据库与权限 API
│   ├─ workspace__infra.py              仓库（工作区）隔离
│   ├─ project_audit__infra.py          项目体检
│   └─ sample_corpus__infra.py、monitor__infra.py
├─ init_db.sql                         建库/建表/权限种子（口令只留 __APP_DB_PASS__ 占位符）
├─ .env.example                        配置模板（口令一律 your_*_here 占位符）
├─ launch_table.bat                    双击启动（调 ui\table__ui.py）
├─ docs/                               设计说明书
├─ sample_docs/                        合成样例语料（本地测试用，默认不入库）
├─ hub/                                脱敏产物（AI 唯一可读根）
├─ input/ output/ logs/                源文件投放 / 逐页缓存 / 日志（均不入库）
└─ workspaces/                         各仓库独立目录（含各自密钥，绝不入库）
```

> 模块命名约定：`<名字>__<层次>.py`（**文件名不改**），并按层次放进同名子目录；
> 层次取值 `desens`（脱敏）/ `scan`（扫描）/ `l1`（概括与事实）/ `graph`（图）/
> `rag`（向量）/ `ai`（AI 链路）/ `ui`（界面）/ `infra`（基础设施）。
> 少数历史命名（`doc_index__graph`、`edge_build__graph_edges`、`graph_query__graph_walk`、
> `l1_extract__summary_hash`、`l1_facts__fact_list`）后缀与所在层不同，是有意保留的，
> 体检里的"结构检查"为它们开了白名单。
> 导入统一写 `from <层>.<模块> import …`；仓库根一律用 `Path(__file__).resolve().parents[1]`
> （不要再写 `Path(__file__).parent`——那会指到层目录去）。

## 六、已知限制

- 只在 **Windows** 上验证；`.doc/.xls` 转换依赖本机 Office/WPS COM，且**可能无响应地卡住**，建议先转换好再入库。
- OCR 被放弃的那次推理线程无法真正终止，只能靠冷却期避免继续争显存。
- 「AI 建议字段」按钮目前是**占位**（返回固定建议），不是真 AI。
-  FASTAPI有待接入，并且需要进行高并发测试。
