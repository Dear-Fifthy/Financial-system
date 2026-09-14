"""AI 对话引擎边界：提示词版本 / 记忆方式 / 范围 的多版本注册表 + decode 管线。

设计背景（评估结论）：
    · RAG 与"图节点记忆"未定夺——本文件先固化"引擎边界"：UI 只调用
      generate(scope, version, memory, user_text, history)，拿回的是
      "未经展示净化"的原始文本；后续无论接向量检索还是图节点/规则层，
      都只改版本实现/记忆上下文构造，不动窗口与存储代码。
    · 提示词/记忆提取要做"多版本并比较"：版本用代码内注册表登记（每条消息
      在日志里记录 version/memory/scope 与模型名），UI 下拉可切换，靠
      logs/chat 下的会话日志横向对比——比开多个 git 分支跑代码更实用。
    · decode（需求）：所有生成文本在展示前必须经 decode_answer() 净化
      （编码规范化/去 BOM/去零宽字符等），raw 与 decoded 双份写入会话日志，
      便于审计与复现版本差异。
    · **思考过程**：对话模型（qwen3.8-max）默认返回 message.reasoning_content。
      本引擎把它随 meta 一起交回（`reasoning` / `reasoning_len` / `reasoning_tokens`），
      由 UI 折叠展示、由 chat_store 写入会话目录的 thinking.jsonl 审计文件；
      该行为可用 CHAT_AI_RECORD_THINKING=0 关闭（只留长度统计）。
    · 对话 AI 与读取/解析 AI 完全分离（安全约定）：读取链路走 ai_parser 的
      AI_API_KEY（DeepSeek），**本对话引擎独立使用 CHAT_AI_* 配置**——
      默认对接阿里云百炼 Qwen3.8-Max（OpenAI 兼容接口），Key 只放 .env，
      绝不写进代码/仓库/日志。两条链路互不共用密钥与模型。

本模块无 Qt 依赖；网络调用为内置 OpenAI 兼容客户端（惰性导入 requests），
便于纯逻辑测试。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import ai.retrieval_config__ai as retrieval_config__ai

BASE_DIR_ENGINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# =========================================================
# 对话 AI 独立配置（CHAT_AI_*，与读取 AI 的 AI_* 完全隔离）
# =========================================================
CHAT_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# 暂定模型：阿里云 Qwen3.8-Max（旗舰 API 模型，另有快照版如 qwen3.8-max-0902；
# 开放权重版名称为 Qwen3.8，注意区分）。最终以你在百炼控制台开通的模型名为准，
# 可用 .env 的 CHAT_AI_MODEL 覆盖。
CHAT_DEFAULT_MODEL = "qwen3.8-max"
# 对话链路超时上限：qwen3.8-max 带思考的问答 90s 经常不够（同步阻塞式请求），
# 上限统一抬到 150s；仍可用 .env 的 CHAT_AI_TIMEOUT 覆盖。
CHAT_DEFAULT_TIMEOUT_S = 150
CHAT_DEFAULT_MAX_TOKENS = 2000


def load_chat_config() -> dict:
    """读取对话 AI 配置（.env 的 CHAT_AI_* 键；key 只放 .env）。

    返回含：api_key / base_url / model / timeout / max_tokens /
    enable_thinking / thinking_budget / record_thinking。
    AI_API_KEY（读取链路）与本配置无任何关系——两条链路密钥独立。

    思考过程相关（实测 qwen3.8-max：不加任何参数也会返回 message.reasoning_content）：
      · CHAT_AI_ENABLE_THINKING：留空=不传该字段（用模型默认，实测默认就有思考）；
        `1`=显式 enable_thinking=true；`0`=显式 false（关思考，省 token、更快）；
      · CHAT_AI_THINKING_BUDGET：思考 token 上限（实测传 256 会让思考更短且更贴中文）；
      · CHAT_AI_RECORD_THINKING：是否**记录**思考文本（默认 1=记录，0=只留长度统计）；
      · CHAT_AI_MAX_TOKENS：回答 token 上限（思考也算在内，故默认给 2000 余量）。
    """
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=os.path.join(BASE_DIR_ENGINE, ".env"))

    def _int(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default

    think_raw = os.getenv("CHAT_AI_ENABLE_THINKING", "").strip().lower()
    enable_thinking = True if think_raw in ("1", "true", "yes", "on") else (
        False if think_raw in ("0", "false", "no", "off") else None)
    budget = _int("CHAT_AI_THINKING_BUDGET", 0)
    return {
        "api_key": os.getenv("CHAT_AI_API_KEY", "").strip(),
        "base_url": os.getenv("CHAT_AI_BASE_URL", CHAT_DEFAULT_BASE_URL).rstrip("/"),
        "model": os.getenv("CHAT_AI_MODEL", CHAT_DEFAULT_MODEL).strip(),
        "timeout": _int("CHAT_AI_TIMEOUT", CHAT_DEFAULT_TIMEOUT_S),
        "max_tokens": _int("CHAT_AI_MAX_TOKENS", CHAT_DEFAULT_MAX_TOKENS),
        "enable_thinking": enable_thinking,          # None = 不传，交给模型默认
        "thinking_budget": budget or None,
        "record_thinking": os.getenv("CHAT_AI_RECORD_THINKING", "1").strip()
        not in ("0", "false", "no", "off"),
    }


@dataclass
class ChatReply:
    """一次对话调用的完整返回（含**思考过程**）。

    reasoning：模型自述的思考/推理过程（`message.reasoning_content`）。实测
    qwen3.8-max **默认就会返回**它，此前本模块只取 content、把思考丢掉了；
    现在保留下来供"记录 + 折叠展示 + 审计"。
    """
    content: str = ""
    reasoning: str = ""
    model: str = ""
    usage: dict = field(default_factory=dict)
    reasoning_tokens: int = 0
    finish_reason: str = ""
    request_id: str = ""

    def to_meta(self) -> dict:
        """写进 meta 的摘要（不含思考正文，正文由调用方决定是否落盘）。"""
        return {
            "model": self.model,
            "usage": self.usage,
            "reasoning_tokens": self.reasoning_tokens,
            "reasoning_len": len(self.reasoning or ""),
            "finish_reason": self.finish_reason,
            "request_id": self.request_id,
        }


def _extract_reasoning(message: dict) -> str:
    """从响应 message 里取思考文本（兼容各家字段名与"分片数组"形态）。"""
    for key in ("reasoning_content", "reasoning", "thinking", "reasoning_text"):
        val = message.get(key)
        if not val:
            continue
        if isinstance(val, str):
            return val
        if isinstance(val, list):                      # 少数网关返回 [{text:...}, …]
            parts = []
            for item in val:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
                else:
                    parts.append(str(item))
            return "".join(parts)
        return str(val)
    return ""


def _extract_content(message: dict) -> str:
    """取正文（兼容 str 与 [{type:text,text:…}] 两种 message.content 形态）。"""
    val = message.get("content")
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, list):
        parts = []
        for item in val:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "".join(parts).strip()
    return str(val or "").strip()


def thinking_status() -> str:
    """一行式说明当前思考配置（UI 提示/日志用）。"""
    cfg = load_chat_config()
    if cfg["enable_thinking"] is True:
        mode = "显式开启"
    elif cfg["enable_thinking"] is False:
        mode = "显式关闭"
    else:
        mode = "模型默认（实测返回思考）"
    budget = f"｜思考上限 {cfg['thinking_budget']} tokens" if cfg["thinking_budget"] else ""
    rec = "记录" if cfg["record_thinking"] else "只记长度"
    return f"思考：{mode}{budget}｜思考文本：{rec}"


def _chat_completions(system: str, user: str, temperature: float = 0.2,
                      max_tokens: int | None = None) -> ChatReply:
    """调用对话 AI 的 OpenAI 兼容 chat/completions，返回 ChatReply（正文 + **思考过程**）。

    说明：
      · key 只经 Authorization 头发送，**不打印、不落盘**；
      · 思考过程字段（reasoning_content）**只在内存里传递**，是否落盘由调用方决定；
      · 正文为空时（个别推理模型只输出思考）抛错，并把思考长度写进错误信息便于排查。
    """
    import requests

    cfg = load_chat_config()
    if not cfg["api_key"]:
        raise RuntimeError(
            "未配置 CHAT_AI_API_KEY！请在 .env 填写对话 AI 的 Key"
            "（独立于读取 AI 的 AI_API_KEY）"
        )
    body: dict = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens or cfg["max_tokens"],
        "stream": False,
    }
    if cfg["enable_thinking"] is not None:
        body["enable_thinking"] = bool(cfg["enable_thinking"])
    if cfg["thinking_budget"]:
        body["thinking_budget"] = int(cfg["thinking_budget"])
    resp = requests.post(
        f"{cfg['base_url']}/chat/completions",
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        json=body,
        timeout=cfg["timeout"],
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        choice = (data["choices"] or [])[0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"对话 AI 返回格式异常: {exc}") from exc
    usage = data.get("usage") or {}
    reply = ChatReply(
        content=_extract_content(message),
        reasoning=_extract_reasoning(message),
        model=str(data.get("model") or cfg["model"]),
        usage=usage,
        reasoning_tokens=int(((usage.get("completion_tokens_details") or {})
                              .get("reasoning_tokens") or 0)),
        finish_reason=str(choice.get("finish_reason") or ""),
        request_id=str(data.get("id") or ""),
    )
    if not reply.content:
        raise RuntimeError(
            "对话 AI 返回了空 content（可能是推理模型只输出思考）"
            f"；本次思考过程 {len(reply.reasoning)} 字"
            + ("（已记录，可在会话日志/思考折叠块查看）" if reply.reasoning else "")
        )
    return reply


# =========================================================
# 范围（scope）：AI 作答的"指定范围"。检索层（RAG/图）未定时为占位描述；
# 接入后改为"检索上下文注入"，版本实现不变。
# =========================================================
SCOPE_OPTIONS: list[tuple[str, str]] = [
    ("未指定（仅依据对话内容）", "AI 只依据本对话内容作答，不引用外部资料。"),
    ("本机台账（占位）", "计划范围：contract_projects 台账与已归档特征。检索层接入前为占位。"),
    ("当前任务文件（占位）", "计划范围：主窗口当前选中任务对应的 hub 脱敏文本。检索层接入前为占位。"),
    ("全部已索引文档（占位）", "计划范围：document_chunks 向量库 + 关联/规则层。检索层接入前为占位。"),
]


# =========================================================
# 提示词版本注册表（多版本比较）
# =========================================================
def _legend() -> str:
    """脱敏编号说明段：告诉模型 CO/PT/PJ/TX… 各代表什么（含本公司标记）。"""
    try:
        from desens.desens_legend__desens import prompt_block

        return prompt_block()
    except Exception:
        return ""


@dataclass
class PromptVersion:
    key: str
    label: str
    desc: str

    def build_system(self, scope_label: str, scope_desc: str, reference: str = "") -> str:
        """构造本次调用的 system 提示词（各版本在此实现差异）。

        reference：检索层（RAG）注入的【参考材料】文本；空串 = 无检索结果。
        """
        raise NotImplementedError


class _V0Basic(PromptVersion):
    def __init__(self) -> None:
        super().__init__(
            key="v0-basic",
            label="v0-基础问答（无检索）",
            desc="只依据给定范围说明与本对话内容作答；不知道就明说，不编造。",
        )

    def build_system(self, scope_label: str, scope_desc: str, reference: str = "") -> str:
        text = (
            "你是财务文档助手。当前作答范围：【%s】。%s\n"
            "要求：只依据范围内的资料与本对话上下文作答；资料不足时明确说明，"
            "不要编造金额/日期/编号等事实。回答使用中文，简明、分点。"
        ) % (scope_label, scope_desc)
        text += _legend()
        if reference:
            # RAG 记忆方式打开时：检索结果作为材料追加（来源均为脱敏文本）
            text += (
                "\n\n【检索到的参考材料】\n" + reference +
                "\n\n要求：优先依据上述参考材料作答；材料未覆盖的部分明确说明。"
            )
        return text


class _V1ScopeQa(PromptVersion):
    """v1：范围感知问答。RAG 接入后，检索结果会填充到【参考材料】段，
    便于与 v0（无材料）对照效果。"""

    def __init__(self) -> None:
        super().__init__(
            key="v1-scope-qa",
            label="v1-范围问答（参考材料）",
            desc="提示词含【参考材料】段：RAG 检索结果注入其中；无材料时明示。",
        )

    def build_system(self, scope_label: str, scope_desc: str, reference: str = "") -> str:
        ref_section = reference if reference else "（未检索到相关内容，请依据范围与对话作答）"
        return (
            "你是财务文档助手。当前作答范围：【%s】。%s\n"
            "【参考材料】\n%s\n"
            "要求：优先以参考材料与范围内资料作答；材料不足时明确说明，不编造事实；中文、分点。"
            "%s"
        ) % (scope_label, scope_desc, ref_section, _legend())


PROMPT_VERSIONS: dict[str, PromptVersion] = {
    v.key: v for v in (_V0Basic(), _V1ScopeQa())
}


# =========================================================
# 记忆方式注册表（"从什么里取上文/背景"的多版本比较）
# =========================================================
# 主用方式 = 图遍历（graph）：不再依赖 embedding 向量检索，而是沿证据链/因果边
# 做图遍历（该链路用 LLM API Key，与本地 bge 向量化路径完全不同）。
# 旧的向量检索 RAG（rag）保留可用，便于对照；摘要记忆暂为占位。
# 全局检索方式（唯一入口 retrieval_config；默认 graph）：
# 方法选择**不在对话面板按会话选**，而是全局生效于整条检索/核对链路。
DEFAULT_MEMORY_KEY = retrieval_config__ai.DEFAULT


@dataclass
class MemoryVariant:
    key: str
    label: str
    desc: str
    supported: bool = False  # False = 占位（图/摘要层尚未接入）


MEMORY_VARIANTS: dict[str, MemoryVariant] = {
    v.key: v
    for v in (
        MemoryVariant("none", "无记忆（仅本会话上文）", "把最近 N 轮对话原样拼进上下文。", supported=True),
        MemoryVariant("summary", "摘要记忆（占位）", "把旧会话压缩成摘要再作答。", supported=False),
        MemoryVariant(
            "rag", "向量检索 RAG（旧·保留对照）",
            "问题本地向量化 → 检索 document_chunks 脱敏文本，注入【参考材料】。"
            "与图遍历链路完全不同，仅作对照保留。",
            supported=True,
        ),
        MemoryVariant(
            "graph", "图遍历（证据链/因果边·主用）",
            "以事实清单为节点、以已校验关系为边做图遍历取证：返回 值+置信度+溯源路径"
            "（文档/页码/表格坐标/字符区间），概括只作定位索引、取数回原文核对；非 embedding。",
            supported=True,
        ),
    )
}


# =========================================================
# decode 管线（需求点 1：生成文本展示前必须经过 decode）
# =========================================================
def decode_answer(raw: str) -> str:
    """展示层净化：把引擎原始输出转成可直接显示的文本。

    职责边界：content/reasoning 分离与 JSON 抽取在 ai_parser 完成；
    这里只做"通用文本净化"：类型规范、去 BOM/零宽、折行整理。
    后续如需 Markdown 渲染/思考过程折叠，在此叠加新解码器（保持管线单一入口）。
    """
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = str(raw)
    # 去 BOM 与零宽字符（部分模型输出/代理可能夹带）
    text = text.replace("\ufeff", "").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    # 类型规范：极端情况下 content 可能是 JSON 字符串，这里不做二次解析——
    # 结构性解码由调用方（如 _ask_json）负责，本层只保"能安全展示"
    return text.strip()


# =========================================================
# 出站实体守卫（对话外发前：本地匹配 → 唯一命中自动脱敏码 /
#                                    多义命中留给用户本地选择）
# =========================================================
# 规则（对应需求，v4 三级匹配）：
#   1) 强命中：注册名(去空白)整段包含在文本中（用户写了完整名称）；
#   2) 别名/关键词命中：把注册名建成"连续子串(2~6 字)倒排索引"，用户文本按
#      中文段滑动同长窗口查索引——**简称只写中间 2~4 个字**（如
#      "无锡市XX电子科技有限公司" 只写 "XX电子"）也能命中；停用词表剔除
#      公司后缀/行业/地名/对话通用词，区分度上限防止"科技/电子"全库拉人；
#   3) difflib 只作"短文本整体兜底"：≤40 字且高度像某注册名（漏字/多字笔误），
#      且未命中其它实体时，整段替换为该实体码（不再拿整条消息 vs 整名做主力匹配）。
#   无论哪层命中，只要命中区间互相重叠（子类/多结果，如 父公司 vs 北京分公司），
#   就归为 ambiguous 组由 UI 本地选择（展示完整名称，不展示码），
#   用户也可选择"不脱敏原样发送"；数字（证件/卡号，含分隔符变体）一律自动掩码。
# 说明：本层只做"外发前"守卫——会话本地日志仍保留原文（meta.outbound.original），
# 便于人工审计；真正发给模型的是掩码后的 payload。
import difflib
import re as _re

_OUTBOUND_FUZZY_RATIO = 0.7       # 短文本整体相似度兜底阈值（唯一漏字/多字场景）
_OUTBOUND_FUZZY_MAX_TEXT = 40     # 超过该长度的文本不做"整段替换"式模糊兜底
_DIGIT_SEP = r"\s\-_/．.·"        # 数字间允许的排版分隔符
_DIGIT_RUN_RE = _re.compile(rf"(?<![\dXx])[\dXx](?:[{_DIGIT_SEP}]{{0,2}}[\dXx])*")
_ID_RE = _re.compile(r"\d{17}[\dXx]")
_BANK_RE = _re.compile(r"\d{13,19}")

# ---- 别名/关键词层参数（v4：解决"简称只写中间 2~4 个字"的命中问题）----
# 原理：把每个注册名的"连续子串(2~6 字)"建成倒排索引；用户文本按中文连续段
# 滑动同样长度的窗口查索引——简称只要是注册名里的连续片段（字号+行业通常连续），
# 就能命中。不再拿"整条消息 vs 整名"做 difflib（那对简称的 ratio 永远太低）。
_ALIAS_TOKEN_MIN = 2              # 别名窗口最短长度
_ALIAS_TOKEN_MAX = 6              # 别名窗口最长长度
_ALIAS_MAX_HITS_PER_TOKEN = 8     # 某 token 命中注册名超过该数 = 无区分度 → 忽略
_CJK_RUN_RE = _re.compile(r"[\u4e00-\u9fa5]+")
# 停用词/通用词：公司后缀、行业通用词、行政区地名、常见对话词——不作为"简称"触发，
# 否则 "公司/科技/电子" 会把整个映射库拉出来弹窗
_ALIAS_STOPWORDS: frozenset[str] = frozenset({
    # 组织/公司后缀
    "公司", "有限", "责任", "股份", "控股", "集团", "分公司", "事务所", "中心",
    "有限公司", "有限责任公司", "股份有限公司", "集团有限公司", "人民政府",
    # "XX有限公司"类名称的公共 2~3 字片段（否则别名层会互相"认领"对方名称区域）
    "有限公", "限公司", "限公",
    # 行业/通用修饰词（中文公司名高频段）
    "科技", "技术", "电子", "信息", "网络", "软件", "数据", "智能", "自动化",
    "工程", "建设", "建筑", "设计", "咨询", "服务", "管理", "实业", "发展",
    "投资", "贸易", "物流", "运输", "供应链", "能源", "材料", "设备", "机械",
    "化工", "制药", "生物", "医疗", "医药", "食品", "农业", "环保", "文化",
    "传媒", "广告", "地产", "物业", "租赁", "汽车", "建筑", "装饰", "教育",
    "旅游", "酒店", "金融", "保险", "证券", "银行", "信托", "基金", "租赁",
    # 常见行政区地名（前缀，不构成字号）
    "北京", "上海", "广州", "深圳", "天津", "重庆", "杭州", "南京", "苏州",
    "无锡", "宁波", "青岛", "大连", "厦门", "济南", "郑州", "长沙", "沈阳",
    "合肥", "福州", "昆明", "哈尔滨", "石家庄", "南宁", "长春", "太原",
    "贵阳", "南昌", "兰州", "海口", "银川", "西宁", "呼和浩特", "乌鲁木齐",
    "西藏", "新疆", "宁夏", "广西", "内蒙", "中国", "江苏", "浙江", "广东",
    "山东", "四川", "湖北", "湖南", "福建", "河北", "河南", "陕西", "安徽",
    "省", "市", "区", "县",
    # 对话/财务通用词（避免把句子当公司简称）
    "合同", "发票", "付款", "收款", "金额", "项目", "日期", "编号", "总额",
    "进度", "回款", "账期", "结算", "核对", "检查", "介绍", "请问", "怎么",
    "如何", "什么", "我们", "你们", "他们", "这个", "那个", "情况", "关于",
    "审核", "审批", "流程", "状态", "已经", "还有", "需要", "是否", "可以",
    "谢谢", "麻烦", "一下", "相关", "资料", "文件", "内容", "问题", "信息",
})


def _load_entity_registry() -> list[dict]:
    """读取本地明文实体映射（company/party）作为匹配语料：code + 真实值。

    说明：只取明文类别（id_card/bank_card 的明文不落盘、不可用于名称匹配）；
    每调用拉取一次（本地库、行数少，保证与库内最新一致）。
    """
    from infra.database_serv__infra import get_connection

    rows: list[dict] = []
    with get_connection() as conn, conn.cursor() as cur:
        for table, kind in (("entity_mapping_company", "company"), ("entity_mapping_party", "party")):
            cur.execute(f"SELECT code, real_value FROM {table}")  # 表名来自固定常量，无注入
            for code, real in cur.fetchall():
                real = (real or "").strip()
                if not real:
                    continue
                rows.append({
                    "code": code,
                    "real": real,
                    "norm": "".join(real.split()),
                    "kind": kind,
                })
    return rows


def _norm_to_orig_positions(text: str) -> list[int]:
    """原文 -> 去空白后的字符到原文索引的映射（供精确替换定位）。"""
    idx: list[int] = []
    for i, ch in enumerate(text):
        if not ch.isspace():
            idx.append(i)
    return idx


def _find_norm_spans(text: str, needle: str) -> list[tuple[int, int]]:
    """在原文里找 needle(去空白) 的全部出现位置，返回 (start, end) 原文切片（含原空白）。"""
    if not needle:
        return []
    pos = _norm_to_orig_positions(text)
    norm_text = "".join(text.split())
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        i = norm_text.find(needle, start)
        if i < 0:
            break
        # end = 命中的最后一个非空字符在原文中的下标 + 1（中间若夹原空白也一并覆盖）
        spans.append((pos[i], pos[i + len(needle) - 1] + 1))
        start = i + len(needle)  # 不重叠续找
    return spans


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """把 (start,end) 列表合并成不相交的最小覆盖区间（相邻/重叠合并）。

    别名层的一次命中会产生大量互相重叠的窗口 span，这里压成每个实体一块区域，
    避免后续替换/聚类时区间互相打架。
    """
    if not spans:
        return []
    out: list[tuple[int, int]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:          # 与上一区间重叠/相接 → 延伸
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _alias_windows(text: str) -> list[tuple[str, int, int]]:
    """把文本切成"中文连续段"上的 2..6 字滑动窗口，返回 (token, 原文起点, 终点)。

    只对连续汉字段滑窗（跳过标点/空白/数字），保证 token 就是原文的一段连续切片。
    """
    out: list[tuple[str, int, int]] = []
    for m in _CJK_RUN_RE.finditer(text):
        run_start = m.start()
        run = m.group(0)
        run_len = len(run)
        max_len = min(_ALIAS_TOKEN_MAX, run_len)
        for size in range(_ALIAS_TOKEN_MIN, max_len + 1):
            for i in range(0, run_len - size + 1):
                token = run[i : i + size]
                if token in _ALIAS_STOPWORDS:
                    continue
                out.append((token, run_start + i, run_start + i + size))
    return out


def _build_alias_index(entries: list[dict]) -> dict[str, list[int]]:
    """建别名倒排索引：注册名(去空白)的每个 2..6 字连续子串 -> 条目下标。

    停用词在建索引时剔除；区分度（命中数上限）在查询侧校验。
    """
    index: dict[str, list[int]] = {}
    for idx, e in enumerate(entries):
        norm = e["norm"]
        n = len(norm)
        max_len = min(_ALIAS_TOKEN_MAX, n)
        for size in range(_ALIAS_TOKEN_MIN, max_len + 1):
            for i in range(0, n - size + 1):
                token = norm[i : i + size]
                if token in _ALIAS_STOPWORDS:
                    continue
                index.setdefault(token, []).append(idx)
    return index


def _match_alias_spans(text: str, entries: list[dict], index: dict[str, list[int]]) -> list[dict]:
    """别名层匹配：文本窗口查索引 → 每个实体的命中区域（合并后 spans）。

    返回与强命中同构的 [{code, real, spans}]，供 analyze_outbound 并入统一聚类。
    区分度规则：某 token 命中注册名超过 _ALIAS_MAX_HITS_PER_TOKEN 个 → 无区分度，
    整个 token 不参与（如全库都含的"科技"不会把所有人都拉出来）。
    """
    hits_by_idx: dict[int, list[tuple[int, int]]] = {}
    for token, s, e in _alias_windows(text):
        idxs = index.get(token)
        if not idxs:
            continue
        if len(idxs) > _ALIAS_MAX_HITS_PER_TOKEN:
            continue  # 通用词（区分度不足），跳过整个 token
        for idx in set(idxs):
            hits_by_idx.setdefault(idx, []).append((s, e))
    result: list[dict] = []
    for idx, spans in hits_by_idx.items():
        e = entries[idx]
        result.append({"code": e["code"], "real": e["real"], "spans": _merge_spans(spans)})
    return result


def _prune_alias_claims(
    hits: list[dict], strong_by_code: dict[str, list[tuple[int, int]]]
) -> list[dict]:
    """剪掉别名层的"乱认领"：某实体的别名区间若落在**其它实体整名强命中**
    的区域内，则该区间归属存疑（文字上属于那个整名），直接丢弃。

    典型场景：文本同时含 A、B 两家完整名称，别名窗口"有限公"类公共片段会让
    A 认领 B 名称里的一段、B 认领 A 里的一段 → 两家被错误并成 ambiguous。
    别名只在没有更强归属的区域才有效（真·简称场景没有强命中，不受影响）。
    """
    pruned: list[dict] = []
    for hit in hits:
        keep: list[tuple[int, int]] = []
        for s, e in hit["spans"]:
            contested = False
            for code, strong_spans in strong_by_code.items():
                if code == hit["code"]:
                    continue  # 自己整名区域内的别名区间不算争抢
                if any(_span_overlap((s, e), ss) for ss in strong_spans):
                    contested = True
                    break
            if not contested:
                keep.append((s, e))
        if keep:
            pruned.append({"code": hit["code"], "real": hit["real"], "spans": _merge_spans(keep)})
    return pruned


def _mask_digit_text(text: str) -> str:
    """证件/卡号（含分隔符变体）自动掩码：前6+******+后4（证件）/
    前6+星+后4（卡号）。纯本地星号掩码，不写库、不出码。"""
    def _fix(m) -> str:
        d = _re.sub(r"[^\dXx]", "", m.group(0))
        if _ID_RE.fullmatch(d):
            return d[:6] + "******" + d[-4:]
        if _BANK_RE.fullmatch(d):
            return d[:6] + "*" * max(len(d) - 10, 4) + d[-4:]
        return m.group(0)
    return _DIGIT_RUN_RE.sub(_fix, text)


def analyze_outbound(text: str) -> dict:
    """对用户输入做外发前守卫分析（不联网、不弹窗）。

    匹配顺序（v4）：
      ① 强命中：注册名整段包含；
      ② 别名命中：注册名连续子串(2~6字)倒排索引 ←→ 文本中文段滑窗（简称/中段 2~4 字）；
      ③ 短文本整体 difflib 兜底（≤40 字、唯一、相似 ≥0.7 → 整段替换）。
    各层命中按 code 合并 spans，再做重叠分组：
      无重叠唯一 → auto（自动替换）；重叠多义 → ambiguous（UI 弹窗选择）。
    返回：
      {
        "auto":      [{"code":…, "spans": [(s,e),…]}],
        "ambiguous": [{"candidates": [{"code","real","spans"},…]}],
        "fuzzy_auto": bool, "_fuzzy_code": str|None,
      }
    """
    text = text or ""
    entries = _load_entity_registry()
    if not entries:
        return {"auto": [], "ambiguous": [], "fuzzy_auto": False}

    alias_index = _build_alias_index(entries)

    # 1) 强命中（注册名整段包含）
    by_code: dict[str, dict] = {}
    strong_by_code: dict[str, list[tuple[int, int]]] = {}
    for e in entries:
        spans = _find_norm_spans(text, e["norm"])
        if spans:
            by_code[e["code"]] = {"code": e["code"], "real": e["real"], "spans": spans}
            strong_by_code[e["code"]] = spans

    # 2) 别名命中：先剪掉"落在其它实体整名区域内"的乱认领，再按 code 并入
    alias_hits = _prune_alias_claims(_match_alias_spans(text, entries, alias_index), strong_by_code)
    for hit in alias_hits:
        cur = by_code.get(hit["code"])
        if cur is not None:
            cur["spans"] = _merge_spans(cur["spans"] + hit["spans"])
        else:
            by_code[hit["code"]] = hit
    matched = list(by_code.values())

    # 3) 短文本 difflib 兜底（分级阈值）：
    #    - 完全没有命中：文本整体像某注册名（≥0.7，容忍漏字/多字）→ 整段替换；
    #    - 已唯一命中同一实体：需 ≥0.9（文本几乎就是该名，如漏尾字），
    #      避免把"请核对XX公司的…情况"这类带问句的短文本整段误删；
    #    - 命中多个实体：不做整段替换（走区间替换/弹窗）。
    fuzzy_entry = None
    if len(text) <= _OUTBOUND_FUZZY_MAX_TEXT:
        text_norm = "".join(text.split())
        best = None
        for e in entries:
            if len(e["norm"]) < 4:
                continue
            ratio = difflib.SequenceMatcher(None, e["norm"], text_norm).ratio()
            if best is None or ratio > best[0]:
                best = (ratio, e)
        if best:
            ratio, cand = best
            if not matched:
                use_whole = ratio >= _OUTBOUND_FUZZY_RATIO
            elif len(matched) == 1 and matched[0]["code"] == cand["code"]:
                use_whole = ratio >= 0.9  # 同一实体、且文本几乎等于该名
            else:
                use_whole = False
            if use_whole:
                fuzzy_entry = cand

    # 4) 重叠分组：相互重叠的强/别名命中聚成一个 ambiguous 组（子类/多结果）
    auto: list[dict] = []
    ambiguous: list[dict] = []
    used: list[bool] = [False] * len(matched)
    for i, m1 in enumerate(matched):
        if used[i]:
            continue
        group_spans = set(m1["spans"])
        group = [m1]
        used[i] = True
        for j in range(i + 1, len(matched)):
            m2 = matched[j]
            if used[j]:
                continue
            # 有任一段重叠即并入同一组
            if any(_span_overlap(a, b) for a in group_spans for b in m2["spans"]):
                group.append(m2)
                group_spans.update(m2["spans"])
                used[j] = True
        if len(group) == 1:
            auto.append({"code": group[0]["code"], "spans": sorted(group[0]["spans"])})
        else:
            # 候选按名称长度降序（默认选最长=最完整名称）；每个候选自带 spans，
            # 只替换用户选中的那个（不替换整组并集，避免区间错位）
            group.sort(key=lambda g: len(g["real"]), reverse=True)
            ambiguous.append({
                "candidates": [
                    {"code": g["code"], "real": g["real"], "spans": sorted(g["spans"])}
                    for g in group
                ],
            })

    return {"auto": auto, "ambiguous": ambiguous, "fuzzy_auto": fuzzy_entry is not None,
            "_fuzzy_code": fuzzy_entry["code"] if fuzzy_entry else None}


def _span_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def compose_payload(text: str, plan: dict, resolutions: dict[int, str | None] | None = None) -> str:
    """把守卫分析结果应用成最终外发文本。

    resolutions：ambiguous 各组的用户选择（组索引 -> 选定 code；None=不脱敏原样发送）。
    自动组与已选组统一替换为脱敏码；未选/跳过的不动；最后做数字掩码。
    注意：所有替换先按"右→左"一次性应用（逐个替换会因长度变化错位后续 span）。
    """
    pairs: list[tuple[int, int, str]] = []
    for item in (plan or {}).get("auto", []):
        for s, e in item["spans"]:
            pairs.append((s, e, item["code"]))
    resolutions = resolutions or {}
    for idx, amb in enumerate((plan or {}).get("ambiguous", [])):
        code = resolutions.get(idx)
        if not code:
            continue
        # 只替换用户选中的候选自身 spans（候选各自带 spans，避免并集区间错位）
        for cand in amb.get("candidates", []):
            if cand["code"] == code:
                for s, e in cand["spans"]:
                    pairs.append((s, e, code))
                break
    payload = text
    if pairs:
        out = text
        for s, e, code in sorted(pairs, key=lambda t: -t[0]):
            out = out[:s] + code + out[e:]
        payload = out
    # 唯一模糊命中 → 整段替换为码（该分支只在无强命中时出现，安全覆盖）
    if (plan or {}).get("fuzzy_auto") and (plan or {}).get("_fuzzy_code"):
        payload = (plan or {}).get("_fuzzy_code")
    # 数字自动掩码（无需人工选择）
    return _mask_digit_text(payload)


def _replace_spans(text: str, spans: list[tuple[int, int]], repl: str) -> str:
    """按 (start,end) 切片从右向左替换（避免偏移错乱）。"""
    out = text
    for s, e in sorted(spans, key=lambda t: -t[0]):
        out = out[:s] + repl + out[e:]
    return out


# =========================================================
# RAG 检索（传统向量检索，memory=rag）
# =========================================================
_RAG_TOP_K = 5            # 注入的命中块数
_RAG_CHUNK_PREVIEW = 600  # 每块注入的字符上限（控制 token）


def _rag_retrieve(question: str) -> tuple[str, dict, str | None]:
    """传统 RAG 检索：问题本地向量化 → document_chunks top-k → 参考材料文本。

    返回 (参考材料文本, rag_meta, 错误信息)。
    · 命中为空 → 返回提示"未检索到相关文档"，不报错（模型据对话作答即可）；
    · 表不存在/向量化后端缺失等异常 → 返回错误（由调用方决定降级策略）。
    · 安全：document_chunks 只存脱敏文本（hub 流水线保证），此处不再二次脱敏。
    """
    try:
        from rag.rag_store__rag import embed_texts, retrieve_top_k
    except Exception as exc:  # pragma: no cover - 依赖缺失
        return "", {}, f"无法加载 rag_store（{exc}）"

    try:
        vectors = embed_texts([question])
    except Exception as exc:
        return "", {}, f"问题向量化失败（{exc}）——RAG 需先安装本地 embedding 后端或配置 RAG_EMBED_BACKEND=api"
    if not vectors:
        return "", {}, "问题向量化返回为空"

    try:
        rows = retrieve_top_k(vectors[0], top_k=_RAG_TOP_K)
    except Exception as exc:
        return "", {}, f"向量检索失败（{exc}）"

    if not rows:
        return "（未检索到相关文档；请说明。）", {"rag_hits": 0, "rag_docs": []}, None

    lines: list[str] = []
    doc_keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        doc_key = str(r.get("doc_key", "?"))
        if doc_key not in seen:
            seen.add(doc_key)
            doc_keys.append(doc_key)
        chunk_text = str(r.get("chunk_text", ""))[:_RAG_CHUNK_PREVIEW]
        lines.append(f"[{doc_key} 第{r.get('chunk_index', 0)}块] {chunk_text}")
    return "\n".join(lines), {"rag_hits": len(rows), "rag_docs": doc_keys}, None


# =========================================================
# 引擎入口
# =========================================================
def resolve_scope(scope_label: str) -> tuple[str, str]:
    """scope 下拉文案 -> (label, desc)；未知文案兜底为"未指定"。"""
    for label, desc in SCOPE_OPTIONS:
        if label == scope_label:
            return label, desc
    return SCOPE_OPTIONS[0]


def build_history_text(history: list[dict], max_turns: int = 6) -> str:
    """把最近对话压缩成"上文"文本（只取最近 max_turns 条，控制 token）。"""
    recent = history[-max_turns * 2 :]  # 一条消息计 user 或 ai
    lines = []
    for m in recent:
        who = "用户" if m.get("role") == "user" else "助手"
        lines.append(f"{who}：{(m.get('content') or '')[:2000]}")
    return "\n".join(lines)


def generate(
    user_text: str,
    *,
    username: str = "",
    scope_label: str = "未指定（仅依据对话内容）",
    prompt_key: str = "v0-basic",
    memory_key: str | None = None,
    history: list[dict] | None = None,
) -> tuple[str | None, dict | None, str | None]:
    """引擎统一入口（引擎边界）。

    入参：用户输入 + 范围 + 提示词版本 + 记忆方式 + 会话历史。
    返回：(decoded 文本, meta, 错误信息)——三者至多一个非空组合：
      · 成功：decoded 文本 + meta（版本/记忆/范围/模型名等，供日志与对比）；
      · 失败：error 非空，text/meta 为 None（例如对话 AI 未配置 Key、记忆方式未接入）。
    说明：RAG/图检索尚未接入时，记忆方式只支持 none；其余返回明确占位错误，
    便于 UI 提示"该记忆方式尚未接入"，同时把选择记录进日志（多版本比较的基线）。
    """
    if not user_text or not user_text.strip():
        return None, None, "请输入内容后再发送。"

    scope_label_final, scope_desc = resolve_scope(scope_label)
    version = PROMPT_VERSIONS.get(prompt_key)
    if version is None:
        version = PROMPT_VERSIONS["v0-basic"]
    # 未显式指定时取**全局检索方式**（默认图遍历）——不再按会话选择
    effective_key = memory_key or retrieval_config__ai.global_method()
    memory = MEMORY_VARIANTS.get(effective_key)
    if memory is None:
        memory = MEMORY_VARIANTS["none"]
    if not memory.supported:
        return (
            None,
            None,
            f"记忆方式「{memory.label}」尚未启用。"
            "图遍历依赖 L1 图层（节点/证据链/因果边）建成后开放；"
            "在此之前可先用「无记忆」或「向量检索 RAG（旧·保留对照）」。",
        )

    # 对话 AI 独立于读取 AI：走 CHAT_AI_* 配置（Qwen3.8-Max），不经过 ai_parser
    cfg = load_chat_config()

    # ===== 需求判断（第 1 层，见 query_planner__ai）=====
    # 先判断"这个问题要什么"：本机能数的（多少份/哪些文件/总览）直接答，**不调 AI、不进图遍历**；
    # 需要资料的，由规划给出"要哪几类证据"，按类别召回后再由对话 AI 作答。
    # 任何失败/关闭都回退到下面的老路径，绝不影响回答。
    plan_meta: dict = {}
    plan_text = ""
    planner_enabled_flag = False
    try:
        import ai.query_planner__ai as planner

        planner_enabled_flag = planner.planner_enabled()
        _plan = planner.plan(user_text.strip())
        if _plan:
            plan_meta = planner.meta_of(_plan)
            outcome = planner.execute({**_plan, "question": user_text.strip()})
            plan_meta["planner_hits"] = (outcome.get("hits") or [])[:12]
            if outcome.get("direct"):
                # 计数/清单/总览：本机统计直接作答（0 token、0 幻觉）
                meta_direct = {
                    "prompt_key": version.key, "prompt_label": version.label,
                    "memory_key": "local_stats", "memory_label": "本机统计（未调用 AI）",
                    "scope_label": scope_label_final,
                    "model": "", "provider": "",
                    "raw_len": len(outcome["text"]), "decoded_len": len(outcome["text"]),
                    "engine": "chat_engine__ai.v3+planner",
                    "ai_called": False, "usage": {},
                    "reasoning": "", "reasoning_len": 0, "reasoning_tokens": 0,
                    "reasoning_recorded": False,
                    "question_masked": user_text.strip(),
                    **plan_meta,
                }
                return decode_answer(outcome["text"]), meta_direct, None
            plan_text = outcome.get("text") or ""
    except Exception as exc:
        plan_meta = {"planner_error": f"{type(exc).__name__}: {exc}"}

    # 记忆方式 = 传统 RAG：先本地向量化问题并检索 document_chunks，注入参考材料
    reference = plan_text        # 规划给到的证据块（计数/字段/按类别召回/图遍历）
    rag_meta: dict = {}
    if memory.key == "rag":
        ref_text, rag_meta, rag_err = _rag_retrieve(user_text.strip())
        if rag_err:
            return None, None, f"RAG 检索失败：{rag_err}"
        reference = (reference + "\n\n" + ref_text).strip() if reference else ref_text
    elif memory.key == "graph" and not reference:
        # 记忆方式 = 图遍历（主用）：L3 取证块（值+置信度+溯源路径）作为参考材料。
        # 已经由需求判断取到证据（按类别召回/字段查询）时不再重复走图，避免两条腿打架。
        try:
            from graph.graph_query__graph_walk import evidence_block, query as graph_query

            g = graph_query(user_text.strip())
            reference = evidence_block(g)
            rag_meta = {"chains": len(g.get("chains") or []),
                        "confidence": g.get("confidence"),
                        "unresolved": g.get("unresolved") or [],
                        "hypothesis_edges": g.get("hypothesis_edges") or [],
                        "anchors": g.get("anchors") or []}
        except Exception as exc:
            rag_meta = {"error": f"{type(exc).__name__}: {exc}"}
            reference = f"（图遍历取证失败，未注入证据：{rag_meta['error']}）"

    system = version.build_system(scope_label_final, scope_desc, reference)
    # 「当前仓库规模」无条件注入（本机统计、2 行、零 AI 成本）：
    # 只要模型知道真实份数，就不会因为"这题没走计数动作"而答"无法得知/查不到"。
    # 规划失败（planner_error）时**也要注入**——那正是最需要兜底的时刻。
    if planner_enabled_flag:
        try:
            import ai.query_planner__ai as planner

            system += planner.system_context()
        except Exception:
            pass
    history_text = build_history_text(history or [])
    user_payload = user_text.strip()
    if history_text:
        user_payload = f"【最近对话上文】\n{history_text}\n\n【本次问题】\n{user_payload}"

    try:
        reply = _chat_completions(system=system, user=user_payload, temperature=0.2)
    except Exception as exc:
        # 统一转成返回错误（未配 Key/网络/鉴权/格式异常等都走到这里，
        # 不向 worker 线程抛异常——否则 UI 会卡在"生成中"）
        return None, None, f"对话 AI 调用失败：{exc}"
    decoded = decode_answer(reply.content)
    reasoning = decode_answer(reply.reasoning)
    meta = {
        "prompt_key": version.key,
        "prompt_label": version.label,
        "memory_key": memory.key,
        "memory_label": memory.label,
        "scope_label": scope_label_final,
        "model": reply.model or cfg.get("model", ""),
        "provider": cfg.get("base_url", "").replace("https://", "").split("/")[0],
        "raw_len": len(reply.content or ""),
        "decoded_len": len(decoded),
        "engine": "chat_engine__ai.v3+planner" if plan_meta else "chat_engine__ai.v3",
        "ai_called": True,
        # ---- 思考过程（本轮新增）----
        # 实测 qwen3.8-max 默认返回 message.reasoning_content；这里把它原样带回，
        # 由 UI/存储决定记录与折叠展示（不记录时只留长度与 token 数）。
        "reasoning": reasoning if cfg.get("record_thinking", True) else "",
        "reasoning_len": len(reasoning),
        "reasoning_tokens": reply.reasoning_tokens,
        "reasoning_recorded": bool(reasoning and cfg.get("record_thinking", True)),
        "usage": reply.usage,
        "finish_reason": reply.finish_reason,
        "request_id": reply.request_id,
        "question_masked": user_text.strip(),      # 便于思考日志对齐"当时问的是什么"
    }
    if rag_meta:
        meta.update(rag_meta)  # rag_hits / rag_docs：记录本次命中的文档，供审计与对比
    if plan_meta:
        meta.update(plan_meta)  # 需求判断留痕：动作/来源/依据/命中文档
    return decoded, meta, None
