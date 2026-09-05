"""AI 解析模块：接入 DeepSeek API（deepseek-v4-flash），完成特征哈希分类 / 台账填写 / 条款摘要。

文件位置：项目根目录新增 ai_parser.py。
职责与函数清单：
  - load_ai_config()            ：从 .env 读取 AI_API_KEY / AI_BASE_URL / AI_MODEL（不硬编码）
  - chat_ai()                   ：调用 DeepSeek chat/completions（requests）
  - _ask_json()                 ：要求模型输出严格 JSON，失败自动重试一次
  - hash_feature_value()        ：特征值 -> 本地 SHA-256 哈希（归一化后）
  - project_hash() / subitem_hash() ：大项目哈希 / 子项目(分公司/子公司)哈希+子序号
  - extract_features()          ：让 AI 从脱敏文本提取特征值（项目/日期/类型/应收应付/四流/…）
  - classify_and_store()        ：提取 -> 哈希 -> 落库（file_feature_hashes + project_archive）
  - table_metadata_for_ai()     ：表格(Excel)只给 AI 看【sheet 名 + 表头 + 项目列】，数据格不暴露
  - fill_contract_ledger()      ：合同 -> 填台账【项目栏 + 备注栏】（只读历史项目/备注学习习惯）
  - summarize_clauses()         ：条款摘要（期限/金额/付款/违约责任/其它要点）
  - chunk_and_embed()           ：分块 + 向量化（委托 rag_store）
  - process_hub_file()          ：单份 hub JSON 的完整 AI 处理编排
  - main()                      ：命令行调试入口

安全约定：
  1. API key 只从 .env 读取，绝不写进代码/仓库；
  2. 发给 AI 的所有文本均为"脱敏后"内容（编码/掩码），真实人名/卡号不出本机；
  3. 特征哈希在本机计算（AI 只回传特征值文本，不做哈希）；
  4. 台账只允许 AI 读写【项目栏 + 备注栏】，其它列数据不经过 AI。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path

import requests
from dotenv import load_dotenv

from database_serv import (
    api_get_ledger_project_notes,
    api_get_table_fields,
    api_save_contract_from_ai,
    api_save_feature_hashes,
    api_upsert_project,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")


# =========================================================
# 1. 配置（.env，不硬编码）
# =========================================================
def load_ai_config() -> dict:
    """读取 AI 配置。AI_API_KEY 必填（从 .env 读，不入库不进仓库）。

    AI_MODEL：常规对话/摘要用；AI_MODEL_JSON：结构化 JSON 提取用
    （部分推理型模型在复杂 JSON 任务上只输出思考、content 为空，
    此时可给 AI_MODEL_JSON 配一个非推理模型，如 deepseek-chat）。
    """
    default_model = os.getenv("AI_MODEL", "deepseek-v4-flash")
    return {
        "api_key": os.getenv("AI_API_KEY", ""),
        "base_url": os.getenv("AI_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        "model": default_model,
        "model_json": os.getenv("AI_MODEL_JSON", default_model),
        "timeout": int(os.getenv("AI_TIMEOUT", "90")),
    }


def ai_enabled() -> bool:
    """是否启用 AI 处理：AI_ENABLED=1 且已配置 API key。"""
    return os.getenv("AI_ENABLED", "0") == "1" and bool(os.getenv("AI_API_KEY", ""))


# =========================================================
# 1.5 AI 日志（完整请求/返回/思考内容记录到控制台 + 文件）
# =========================================================
_AI_LOG_PATH: Path | None = None


def set_ai_log_path(path: Path | None) -> None:
    """设置 AI 日志文件路径（每份文档一个：与其 hub JSON 同目录，如 xxx.ai.log）。"""
    global _AI_LOG_PATH
    _AI_LOG_PATH = path


def _ai_log(message: str) -> None:
    """写 AI 日志：控制台打印 + 追加到日志文件（若已设置）。"""
    print(message, flush=True)
    if _AI_LOG_PATH is not None:
        try:
            with open(_AI_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(message + "\n")
        except OSError:
            pass


# =========================================================
# 2. DeepSeek API 客户端
# =========================================================
def chat_ai(
    system: str,
    user: str,
    temperature: float = 0.2,
    max_tokens: int = 2000,
    model: str | None = None,
) -> str:
    """调用 DeepSeek chat/completions，返回回复文本。

    日志策略：记录完整请求参数、AI 完整回复（content）与思考内容
    （reasoning_content，若模型返回），写入 _ai_log（控制台+文件）。
    model 参数为空时使用 AI_MODEL。
    """
    cfg = load_ai_config()
    if not cfg["api_key"]:
        raise RuntimeError("未配置 AI_API_KEY！请在 .env 中填写（AI_API_KEY=sk-...）")
    use_model = model or cfg["model"]

    _ai_log(f"[AI 请求] model={use_model} temperature={temperature} max_tokens={max_tokens}")
    _ai_log(f"[AI 请求] system({len(system)}字): {system[:300]}")
    _ai_log(f"[AI 请求] user({len(user)}字): {user[:500]}")

    resp = requests.post(
        f"{cfg['base_url']}/chat/completions",
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        json={
            "model": use_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        },
        timeout=cfg["timeout"],
    )
    resp.raise_for_status()
    data = resp.json()
    message = data["choices"][0]["message"]
    content = (message.get("content") or "").strip()
    reasoning = message.get("reasoning_content") or ""
    # 记录最近一次思考内容（供 _ask_json 在 content 无 JSON 时兜底解析）
    globals()["_LAST_REASONING"] = reasoning
    _ai_log(f"[AI 返回] content({len(content)}字) 完整回复：\n{content}")
    if reasoning:
        _ai_log(f"[AI 思考] reasoning({len(reasoning)}字) 推理内容：\n{reasoning}")
    return content


def _ask_json(system: str, user: str, temperature: float = 0.1) -> dict:
    """要求 AI 输出严格 JSON，解析失败自动重试一次。

    带推理的模型（如 deepseek-v4-flash）会先产出大量 reasoning_content、
    content 可能很短——因此：max_tokens 给足；解析顺序 content -> reasoning
    -> 大括号截取；最后兜底再问一次。"""
    for attempt in range(2):
        raw = chat_ai(
            system,
            user,
            temperature=0.0,
            max_tokens=8192,
            model=load_ai_config()["model_json"],  # JSON 提取可用独立模型
        )
        for candidate in (raw,):  # raw = content；下方兜底用思考内容
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                m = re.search(r"\{.*\}", candidate, re.DOTALL)
                if m:
                    try:
                        return json.loads(m.group(0))
                    except json.JSONDecodeError:
                        pass
        # 兜底：从最近一次思考内容里找 JSON（见 chat_ai 的 _LAST_REASONING）
        last_reasoning = globals().get("_LAST_REASONING", "")
        if last_reasoning:
            m = re.search(r"\{.*\}", last_reasoning, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
        if attempt == 0:
            user = f"上一次输出不是合法 JSON。请直接输出一个 JSON 对象，不要任何解释：\n{user}"
        else:
            raise RuntimeError(f"AI 输出无法解析为 JSON：{raw[:200]}")
    raise RuntimeError("AI JSON 解析失败")


# =========================================================
# 3. 特征哈希（本机计算，AI 只回传特征值）
# =========================================================
def _norm(value: str) -> str:
    """去空白归一化（与实体映射库一致）。"""
    return "".join(str(value or "").split())


def hash_feature_value(feature_code: str, value: str) -> str:
    """特征值 -> 本地哈希：sha256(f"{code}:{归一化值}") 前 16 位。

    只在本机计算，AI 永远拿不到也不参与哈希。
    """
    return hashlib.sha256(f"{feature_code}:{_norm(value)}".encode("utf-8")).hexdigest()[:16]


def project_hash(project_name: str) -> str:
    """大项目哈希（抬头）：sha256("P:"+归一化项目名) 前 16 位。

    规则：分公司/子公司共用同一抬头哈希，用子序号 seq 区分（见 api_upsert_project）。
    """
    return hash_feature_value("project", project_name)


def subitem_hash(parent_hash: str, sub_name: str) -> str:
    """子项目/子公司哈希：以父哈希为前缀再加子名，保证"同抬头不同子序号"。"""
    return hashlib.sha256(f"{parent_hash}:{_norm(sub_name)}".encode("utf-8")).hexdigest()[:16]


def _date_parts(date_str: str) -> dict | None:
    """把日期字符串解析为 {year, month, day}（只保留年月日）。

    兼容 2026-01-01 / 2026/1/1 / 2026年1月1日 等写法。
    """
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?", str(date_str or ""))
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return {"year": f"{y:04d}", "month": f"{y:04d}-{mo:02d}", "day": f"{y:04d}-{mo:02d}-{d:02d}"}


def _normalize_date_only(value) -> str:
    """合同期限只保留日期（年月日），去掉时分秒等尾巴。"""
    parts = _date_parts(str(value or ""))
    if parts:
        return parts["day"]
    return str(value or "")


_CONTRACT_CODE_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*")


def extract_contract_code_from_filename(file_name: str) -> str | None:
    """只从文件标题中识别合同编号：取"英文/数字/连字符"组成的最长前缀段
    （如 KH-YF-2026-02，遇到中文即截断），且必须包含数字、长度>=3；
    识别不到返回 None（由调用方请用户输入）。
    """
    stem = Path(file_name).stem
    best = None
    for m in _CONTRACT_CODE_RE.finditer(stem):
        seg = m.group(0)
        if len(seg) >= 3 and re.search(r"[0-9]", seg):
            if best is None or len(seg) > len(best):
                best = seg
            if m.start() == 0:  # 文件名开头命中即为最优，直接采用
                break
    return best.upper() if best else None


# =========================================================
# 4. 特征提取（AI）与特征哈希分类落库
# =========================================================
FEATURE_EXTRACT_SYSTEM = """你是财务文档特征提取器。
输入是【已脱敏】的文档文本（公司/人员已替换为 CO/PT 编码，身份证/银行卡已掩码）。
只输出一个 JSON 对象，字段为特征代码，取值必须从文本中提取或合理推断，不确定填 null：
{
  "project": "项目名称（大项目/小项目尽量区分，如 南苑新村/消防维保）",
  "date_start": "起始日期，只保留年月日，如 2026-01-01",
  "date_end": "终止日期，只保留年月日，如 2026-12-31（单日单据则与起始相同）",
  "doc_type": "发票|合同|物流凭证|其它",
  "money_flow": "应收|应付|收支不明",
  "four_flow": "合同流/发票流/资金流/货物流 中文本涉及到的，如 [合同流,发票流]",
  "voucher_kind": "凭证|账簿|报告|其它",
  "counterparty": "对手方编码(如 CO0002)或名称",
  "payment_term": "付款条款要点，如 服务期结束后15个工作日内一次性支付",
  "tax_kind": "税种/税率，如 增值税6%"
}
【最重要】你可以在内心思考，但你的最终回复必须且只能是这个 JSON 对象本身——不要任何解释文字，不要 Markdown 代码块标记。"""


def extract_features(doc_text: str) -> dict:
    """让 AI 从脱敏文本提取特征值（只回传特征值文本，不做哈希）。"""
    payload = _ask_json(FEATURE_EXTRACT_SYSTEM, doc_text[:12000])
    return {k: (v if v is not None else "") for k, v in payload.items() if isinstance(v, (str, list))}


def classify_and_store(doc_key: str, doc_text: str, current_user: dict | None = None) -> dict:
    """特征哈希分类：AI 提取特征值 -> 本机哈希 -> 落库。

    返回 {"hashes": {feature_code: value_hash}, "projects": [...]}。
    必选特征（项目/日期/类型/应收应付）缺失时仅告警不中断。
    """
    features = extract_features(doc_text)
    hashes: dict[str, str] = {}
    projects: list[dict] = []

    # 项目：大/小项目分层（大项目哈希 + 子项目哈希），分公司/子公司同抬头+子序号
    proj = str(features.get("project", "")).strip()
    if proj:
        big = project_hash(proj)
        hashes["project"] = big
        projects.append({"name": proj, "name_hash": big, "parent_name": None, "parent_hash": None, "seq": 0})
        # 若 AI 给出了"大项目/小项目"分隔（含 / 或 -），拆成两级
        parts = [p.strip() for p in re.split(r"[/\-／]", proj) if p.strip()]
        if len(parts) >= 2:
            parent = parts[0]
            parent_h = project_hash(parent)
            hashes["project_parent"] = parent_h
            for idx, sub in enumerate(parts[1:], start=1):
                sub_h = subitem_hash(parent_h, sub)
                projects.append(
                    {"name": sub, "name_hash": sub_h, "parent_name": parent, "parent_hash": parent_h, "seq": idx}
                )

    # 日期分层哈希：起始/终止 各拆 年/月/日 三级，检索时按粒度选择哈希
    # （按年搜索查 year 哈希、按月份排序查 month 哈希、精确到日查 day 哈希）。
    # 说明：整体 date_range 单一哈希只能做"完全相同"匹配，按粒度检索会失效，
    # 故改为分层存储（一次算好入库，比事后细分再提取更便宜）。
    for side in ("start", "end"):
        raw = features.get(f"date_{side}")
        if not raw:
            continue
        parts = _date_parts(str(raw))
        if not parts:
            continue
        for gran in ("year", "month", "day"):
            hashes[f"date_{side}_{gran}"] = hash_feature_value(f"date_{gran}", parts[gran])

    # 其余特征直接哈希
    for code in ("doc_type", "money_flow", "voucher_kind"):
        if features.get(code):
            hashes[code] = hash_feature_value(code, str(features[code]))
    # 四流：列表特征逐个保存——feature_code 统一用目录里的 'four_flow'，
    # 用 seq 区分多项（外键目录只有 'four_flow'，'four_flow:1' 这类键会违反外键）
    four_flow = features.get("four_flow")
    if isinstance(four_flow, list):
        for i, item in enumerate(four_flow, start=1):
            hashes[f"four_flow:{i}"] = hash_feature_value("four_flow", str(item))
    elif isinstance(four_flow, str) and four_flow:
        hashes["four_flow:1"] = hash_feature_value("four_flow", four_flow)

    # 落库：复合键拆分——"four_flow:N" 必须用目录键 "four_flow" + seq=N 保存
    # （feature_catalog 外键只认目录键）；其余键先按库内实际目录过滤，
    # 未登记的键跳过并记日志（避免外键错误被静默吞掉）。
    from database_serv import api_get_feature_catalog

    ok_cat, catalog_rows = api_get_feature_catalog(current_user)
    catalog_codes = {r["feature_code"] for r in catalog_rows} if ok_cat else set()
    plain_all = {k: v for k, v in hashes.items() if ":" not in k}
    plain_hashes = {k: v for k, v in plain_all.items() if k in catalog_codes}
    skipped = set(plain_all) - set(plain_hashes)
    if skipped:
        _ai_log(f"[特征哈希] ⚠️ 以下特征码不在目录中，已跳过入库：{sorted(skipped)}")
    flow_items = {int(k.split(":")[1]): v for k, v in hashes.items() if k.startswith("four_flow:")}
    if plain_hashes:
        ok_save, msg_save = api_save_feature_hashes(current_user, doc_key, plain_hashes, seq=0)
        _ai_log(f"[特征哈希] 普通特征落库：{ok_save} | {msg_save}")
    for seq_no, v in sorted(flow_items.items()):
        api_save_feature_hashes(current_user, doc_key, {"four_flow": v}, seq=seq_no)
    for p in projects:
        api_upsert_project(
            current_user, p["name"], p["name_hash"], p["parent_name"], p["parent_hash"], p["seq"]
        )
    return {"hashes": hashes, "projects": projects, "features": features}


# =========================================================
# 5. 表格元数据视图（只给 AI 看表头/项目列/sheet 名）
# =========================================================
def table_metadata_for_ai(path: Path) -> dict:
    """读取表格文件，只产出【sheet 名 + 各 sheet 表头 + 项目列取值】。

    权限设计：数据单元格（金额/明细等）绝不传给 AI；只暴露结构信息，
    满足"AI 只能阅读表头、项目、sheet 名称"的要求。
    支持 xlsx/xls（openpyxl/pandas）。
    """
    import pandas as pd

    xls = pd.ExcelFile(path)
    result = {"sheets": {}}
    for sheet_name in xls.sheet_names:
        df = xls.parse(sheet_name, nrows=5)  # 只读前几行拿表头
        headers = [str(c) for c in df.columns.tolist()]
        project_col = next((c for c in df.columns if "项目" in str(c)), None)
        project_values = []
        if project_col is not None:
            project_values = [str(v) for v in df[project_col].dropna().tolist()][:20]
        result["sheets"][sheet_name] = {
            "headers": headers,
            "project_column": project_col,
            "project_values": project_values,
        }
    return result


# =========================================================
# 6. 合同台账填写（只读写 项目栏 + 备注栏）
# =========================================================
# 代码能消费的固定"英文键 <-> 中文栏目"映射表。
# 注意：这不是写死的台账栏目清单——它是代码字段键名表；提示词模板里
# 不出现任何具体栏目名，字段清单由 build_ledger_fill_system() 依据
# 【实际读到的栏目】动态生成（{columns} 与 {field_lines} 占位）。
FIELD_KEY_MAP = {
    "合同编号": "contract_code",
    "项目": "project",
    "合同期限": "contract_term",
    "甲方": "party_a",
    "合同金额": "income",
    "是否已收款": "is_paid",
    "是否已开票": "is_invoiced",
    "备注": "remark",
}

LEDGER_FILL_SYSTEM_TEMPLATE = """你是合同台账登记助手。
当前台账实际存在的栏目为：【{columns}】。
输入是一份【已脱敏】合同的文本（含【文件来源】文件名——文件名中的"半年度/年度"与括号日期如（2026.1.1-2026.6.30）是可靠的期限与日期线索，务必使用），以及当前台账的【历史 项目栏+备注栏】记录。
要求：
  1. 只输出 JSON，字段名固定用英文，且只输出以下【实际存在的栏目】对应的字段：
{field_lines}
     台账里没有的栏目不要输出；
  2. 合同编号不要输出——由系统从文件名提取（如 KH-YF-2026-02），AI 不得编造；
  3. 日期类栏目只保留日期（年月日），不要时分秒；
  4. 项目栏：优先匹配历史项目栏已有项目（同项目复用同一名称），新项目按文本命名；
  5. 备注栏：学习历史备注的写法习惯（格式/措辞），为本合同写一条风格一致的备注；不必关注备注里的信息是否真实，只要风格一致即可；
     如果历史没有备注样本，写简洁客观的说明（关键条款+风险点）；
  6. 不要编造金额，提取不到填 null；简单的年/月/季度计算可以自己进行：比如合同期限是 2026-01-01 至 2026-12-31，合同金额是 1200000，则月均金额是 100000，季度均金额是 300000；如果合同金额是 1200000，合同期限是 2026-01-01 至 2026-03-31，合同开始时间是 2026-01-01，合同结束时间是 2026-03-31，则月均金额是 400000，季度均金额是 1200000；
  7.见到类似“CO0002”或“PT0003”的编码，这是对手方/人员/日期的脱敏编码，原样照抄即可；不必怀疑这是错误，将其当作真实的对方/人员名称/银行卡号/身份证号/日期等；
  8.可以进行适当推理；但是依据必须来源于文本内容或历史备注样本，不得凭空编造；如果文本中没有明确的数值/日期/条款等信息，AI 可以推理出一个合理的值，但必须在日志中说明依据和推理过程；
  6b. 【计费标准 -> 总额】文本给出"X元/计费期"（如 21500元/半年）时：期限恰好覆盖 1 个计费期
     （如【文件来源】表明半年度合同 + 费率 21500元/半年）→ "合同金额" = 21500；
     期限覆盖 n 个计费期 → = X × n；期限完全无法确定 → 填 null 并把"X元/计费期"写进备注；
  6c. 【日期来源】合同开始/截止日期优先取【文件来源】文件名括号内日期
     （如（2026.1.1-2026.6.30）→ 开始 2026-01-01、截止 2026-06-30），其次取文本明确日期；
【最重要】JSON 的键必须**原样照抄**上面列出的键：英文键（如 project、income）就用英文，中文键（如 城市）就用中文，不得改写、不得遗漏、不得添加未列出的键。
你可以在内心思考，但你的最终回复必须且只能是这个 JSON 对象本身——不要任何解释、不要 Markdown 代码块标记。"""
# （调试用 print 已删除：栏目必须实时读库，勿在此打印固定清单）


def build_ledger_fill_system(current_user: dict | None, table_name: str = "contract_projects") -> str:
    """动态构造台账填写提示词：栏目清单与 JSON 字段清单【实时读取】台账实际栏目。

    读取失败时**直接抛错**（不再静默回退固定清单——那会掩盖"看似动态实则写死"）。
    """
    ok, fields = api_get_table_fields(current_user, table_name)
    if not ok or not fields:
        raise RuntimeError(f"读取台账栏目失败（{fields}），无法构造台账填写提示词")
    # 系统列（自增主键/创建时间等）由数据库自动处理，不交给 AI 输出
    system_columns = {"id", "创建时间"}
    columns = [f["column_name"] for f in fields if f["column_name"] not in system_columns]
    if not columns:
        raise RuntimeError("台账除系统列外没有可用栏目！")
    lines = []
    for c in columns:
        key = FIELD_KEY_MAP.get(c, f'"{c}"')  # 新增栏目：用中文栏目名本身作 JSON 键
        lines.append(f"     {key}({c})")
    return LEDGER_FILL_SYSTEM_TEMPLATE.format(
        columns="、".join(columns),
        field_lines="\n".join(lines),
    )


def fill_contract_ledger(hub_json_path: Path, current_user: dict | None = None) -> dict:
    """合同 -> 填台账：读取历史【项目+备注】-> AI 学习习惯 -> 写项目/备注等字段。

    权限边界：AI 只接触 api_get_ledger_project_notes 返回的【项目+备注】两列，
    台账其它列数据不进入 AI 上下文。
    合同编号规则：只从**文件标题**提取"英文/数字/连字符"段（如 KH-YF-2026-02），
    AI 不参与编号；识别不到时返回 need_code=True，由调用方请用户输入后补存。
    覆盖语义：同编号已存在时由 api_save_contract_from_ai 以最新版覆盖。
    """
    doc = json.loads(hub_json_path.read_text(encoding="utf-8"))
    doc_text = "\n".join(doc.get("pages", []))

    # 合同编号：只识别文件标题中的 英文-数字 部分
    source_name = doc.get("source_file") or hub_json_path.name
    contract_code = extract_contract_code_from_filename(source_name)

    # 只取历史"项目+备注"两列（本机查询，仅这两列传给 AI）
    ok, history = api_get_ledger_project_notes(current_user)
    history_text = json.dumps(history, ensure_ascii=False)[:8000] if ok else "（无历史）"
    _ai_log(f"[台账填写] 历史项目/备注记录 {len(history) if ok else 0} 条（仅这两列进入 AI 上下文）")

    # 动态提示词：先读台账实际栏目再拼进提示词（不写死）
    system = build_ledger_fill_system(current_user)
    _ai_log(f"[台账填写] 提示词中的台账栏目：{system[system.find('【') + 1: system.find('】')]}")

    payload = _ask_json(
        system,
        f"【文件来源】{source_name}\n\n【合同文本】\n{doc_text[:12000]}\n\n【历史 项目栏+备注栏】\n{history_text}",
    )
    _ai_log(f"[台账填写] AI 输出的 JSON 键：{sorted(payload.keys())}")
    # 诊断"AI 输出与 pg 栏目不符"：把 AI 的键映射回栏目名，标出对应栏目已不存在的
    # （典型如旧提示词时代的 contract_term -> 合同期限，而表中已无此列）。
    _key_to_col = {v: k for k, v in FIELD_KEY_MAP.items()}
    ok_c, cols_now = api_get_table_fields(current_user)
    actual_cols = {f["column_name"] for f in cols_now} if ok_c else set()
    stale_keys = [k for k in payload if k in _key_to_col and _key_to_col[k] not in actual_cols]
    if stale_keys:
        _ai_log(
            f"[台账填写] ⚠️ AI 输出了旧栏目的键（对应栏目已不存在，将被忽略）：{stale_keys}。"
            "若 [AI 请求] 里的提示词是旧版，说明应用运行了旧代码；若提示词已是新版，"
            "说明模型沿用了旧习惯输出旧键。"
        )
    fields = {
        "contract_code": contract_code,
        "contract_term": _normalize_date_only(payload.get("contract_term")),  # 只保留日期（年月日）
        "party_a": payload.get("party_a"),
        "income": payload.get("income"),
        "is_paid": bool(payload.get("is_paid", False)),
        "is_invoiced": bool(payload.get("is_invoiced", False)),
        "project": payload.get("project"),
        "remark": payload.get("remark"),
    }
    # 用户新增的自定义栏目：AI 用栏目名本身作 JSON 键输出，这里原样收集，
    # 交给 api_save_contract_from_ai 做"实际表列校验 + 动态写入"。
    known_keys = set(FIELD_KEY_MAP.values())
    extra_fields = {
        k: v for k, v in payload.items()
        if k not in known_keys and isinstance(v, (str, int, float, bool)) and v not in (None, "")
    }
    if extra_fields:
        fields["extra_fields"] = extra_fields
        _ai_log(f"[台账填写] 自定义栏目值：{extra_fields}")

    # 文件名识别不到合同编号：不入库，返回 need_code 由调用方请用户输入
    if not contract_code:
        _ai_log(f"[台账填写] 文件标题未识别到合同编号（{source_name}），等待用户输入")
        return {
            "ok": False,
            "need_code": True,
            "msg": "文件标题中未识别到合同编号，请人工输入",
            "fields": fields,
        }

    ok, msg = api_save_contract_from_ai(fields)
    _ai_log(f"[台账保存] 编号={contract_code} | {msg}（项目={fields['project']!r}，备注长度={len(fields['remark'] or '')}）")
    return {"ok": ok, "msg": msg, "fields": fields}


# =========================================================
# 7. 条款摘要
# =========================================================
SUMMARY_SYSTEM = """你是合同条款摘要员。输入【已脱敏】合同文本。
只输出 JSON：{"summary": "..."}，摘要须包含：合同期限、合同金额、
付款条件、违约责任要点、其它重要条款，每条一行，客观不评述。"""


def summarize_clauses(doc_text: str) -> str:
    """生成条款摘要（期限/金额/付款/违约/其它）。"""
    payload = _ask_json(SUMMARY_SYSTEM, doc_text[:12000])
    return str(payload.get("summary", ""))


# =========================================================
# 8. 分块 + 向量化（委托 rag_store）
# =========================================================
def chunk_and_embed(hub_json_path: Path, current_user: dict | None = None) -> dict:
    """分块 + embedding + 入库（RAG 索引）。

    说明：DeepSeek 无 embedding 接口，向量化走 rag_store 的本地 bge / 外部
    embedding API（RAG_EMBED_BACKEND 切换），块内容为脱敏文本。
    """
    from rag_store import index_hub_json

    n, msg = index_hub_json(hub_json_path)
    return {"chunks": n, "msg": msg}


# =========================================================
# 9. 单份 hub JSON 的完整 AI 编排
# =========================================================
def process_hub_file(hub_json_path: Path, current_user: dict | None = None) -> dict:
    """对一份 hub 脱敏 JSON 依次执行：特征哈希分类 -> (合同)台账填写 -> 条款摘要 -> 分块向量化。

    任何一步失败都不阻断后续步骤，结果汇总返回。
    产出两份与 hub JSON 同目录的伴生文件：
      xxx.ai.log        —— AI 完整请求/返回/思考内容日志
      xxx.features.json —— 特征提取值 + 特征哈希 + 项目归档（便于查看）
    """
    doc = json.loads(hub_json_path.read_text(encoding="utf-8"))
    doc_text = "\n".join(doc.get("pages", []))
    doc_key = hub_json_path.stem
    category = doc.get("category", "")
    report: dict = {"doc_key": doc_key, "category": category}

    # AI 日志：与 hub JSON 同目录
    set_ai_log_path(hub_json_path.parent / f"{doc_key}.ai.log")
    _ai_log(f"===== AI 处理开始：{hub_json_path.name}（分类：{category}）=====")

    # a) 特征哈希分类（所有类型文件都做）
    try:
        report["features"] = classify_and_store(doc_key, doc_text, current_user)
        # 特征哈希日志：与 hub JSON 同目录，方便查看
        feat_path = hub_json_path.parent / f"{doc_key}.features.json"
        feat_path.write_text(
            json.dumps(
                {
                    "doc_key": doc_key,
                    "features": report["features"].get("features", {}),
                    "hashes": report["features"].get("hashes", {}),
                    "projects": report["features"].get("projects", []),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        _ai_log(f"[特征哈希] 已写入 {feat_path.name}（{len(report['features'].get('hashes', {}))} 条哈希）")
    except Exception as exc:
        report["features_error"] = f"{type(exc).__name__}: {exc}"
        _ai_log(f"[特征哈希] 失败：{exc}")

    # b) 合同：填台账（项目栏+备注栏；同编号以最新版覆盖）
    if category == "合同":
        try:
            report["ledger"] = fill_contract_ledger(hub_json_path, current_user)
        except Exception as exc:
            report["ledger_error"] = f"{type(exc).__name__}: {exc}"
            _ai_log(f"[台账填写] 失败：{exc}")

    # c) 条款摘要（合同才需要；其它类型跳过）
    if category == "合同":
        try:
            report["summary"] = summarize_clauses(doc_text)
            _ai_log(f"[条款摘要] 已生成（{len(report['summary'])} 字）")
        except Exception as exc:
            report["summary_error"] = f"{type(exc).__name__}: {exc}"
            _ai_log(f"[条款摘要] 失败：{exc}")

    # d) 分块 + 向量化入库（后台线程执行，不阻塞台账/摘要输出）
    #    理由：首次 embedding 需联网下载 bge 模型（约100MB），可能耗时数分钟；
    #    台账等前面步骤完成后先输出，embedding 在后台完成并写 AI 日志。
    def _embed_in_background(path: Path) -> None:
        try:
            from rag_store import index_hub_json

            n, msg = index_hub_json(path)
            _ai_log(f"[分块向量化·后台] 完成：{msg}")
        except Exception as exc:
            _ai_log(f"[分块向量化·后台] 失败：{type(exc).__name__}: {exc}")

    threading.Thread(target=_embed_in_background, args=(hub_json_path,), daemon=True, name="embed-bg").start()
    report["embed"] = {"status": "后台执行中", "msg": "已放入后台线程，不阻塞台账输出（日志见 xxx.ai.log）"}

    _ai_log(f"===== AI 处理结束：{hub_json_path.name} =====")
    return report

# =========================================================
# 说明：本文件不再提供命令行入口（原 --file/--mode CLI 已删除，
# 直接运行本文件不会做任何事、不会报 argparse 错误）。
# AI 各步骤的调用入口：
#   - 正常使用：table.py 拖入文件后由 hub_pipeline 自动触发（AI_ENABLED=1）；
#   - 手动单步调试：在 python 交互式环境里调用本模块函数，例如
#       from ai_parser import process_hub_file
#       from pathlib import Path
#       process_hub_file(Path("hub/合同/xxx.json"))
# =========================================================
