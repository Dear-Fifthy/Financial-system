"""合同判定：**只看文件命名与标题，且文件类型必须是 PDF / Word**。

口径（按最新决策，取代原来的"关键词打分 + 结构证据≥2"）：
  1. 文件扩展名必须是 **PDF 或 Word**（.pdf/.doc/.docx/.docm/.rtf/.wps）——
     `.xlsx`/`.xls` 这类**表格永远不是合同**（实测"合同金额表.xlsx"因列头带
     "年费用(合同）"被关键词分类判成合同，正是要修掉的情况）；
  2. 文件名 **或** 文档标题（`doc_title:`/`header:`/`paragraph_title:` 块；
     没有这些块时取首页第一条真正的标题行）里出现"合同/协议"字样。

为什么规则要这么"死"：
  · 分类原来是**关键词打分**——"合同"这一类里含"甲方/乙方/合同编号"等
    **发票、汇总表、付款申请也会出现**的词，于是大量单据被误判成合同；
  · 现在**入账不再自动执行**（改为用户审核后入账），所以"宁可多收一点、
    由人来最后定性"是安全的：误判的代价只是多一条待审记录，而不是写错台账。

关键词表可用环境变量覆盖：`CONTRACT_TITLE_TOKENS=合同,协议`（逗号分隔）。
对外接口：
  is_contract_doc(file_name, pages, tables=None) -> ContractVerdict
  title_text(pages, limit)                        标题区文本（供闸门/日志复用）
  CONTRACT_TOKENS / SCOPE_EXTENSIONS
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

__all__ = [
    "ContractVerdict",
    "is_contract_doc",
    "title_text",
    "CONTRACT_TOKENS",
    "SCOPE_EXTENSIONS",
    "WORD_EXTENSIONS",
    "PDF_EXTENSIONS",
    "PARTY_A_RE",
    "extract_contract_fields",
]

# ---------------------------------------------------------
# 甲方匹配 + 正则兜底的台账字段抽取
# ---------------------------------------------------------
# 为什么放在这个**轻量**模块（从 hub_pipeline 搬来）：待入账的「从 hub 刷新」要用它做
# 字段预填，而 hub_pipeline 在导入期会拉 paddle/torch（实测首次刷新因此要 80s）。
# hub_pipeline 里保留同名导出（`from contract_rules__desens import ...`），调用方不用改。
#
# 甲/乙方匹配：兼容两种常见写法——
#   1) 有冒号：甲方：xxx / 甲方（使用单位）：xxx / 甲方（xx）: xxx
#      （xxx 可以是普通文本，也可以是 $ \underline{\text{xxx}} $ 下划线格式）
#   2) 无冒号、仅下划线：甲方（使用单位） $ \underline{\text{xxx}} $
#      （实测合同常见写法；修复：旧正则强制要求冒号，导致这种写法整条漏配。
#        注意无冒号分支只允许下划线格式，避免把"甲方（盖章）"后紧跟的正文/印章误吞）
PARTY_A_RE = re.compile(
    r"甲方(?:\uff08[^\uff09]+\uff09|\([^\)]+\))?"
    r"(?:"
    r"[:\uff1a]\s*"
    r"(?:\$\s*\\underline\{\\text\{([^}]+)\}\}\s*\$|([^\n\uff0c,。\uff1b;]{2,60}))"
    r"|"
    r"\s*\$\s*\\underline\{\\text\{([^}]+)\}\}\s*\$"
    r")"
)


def extract_contract_fields(file_stem: str, desensitized_pages: list[str], store) -> dict:
    """【AI 关闭时的正则兜底】从脱敏文本里取能可靠识别的台账字段。

    ⚠️ 仅当 AI 未启用时才由 process_file_to_hub 产出（AI 启用时台账写入
    归 AI 路径所有，避免双写/重复行）。合同编号只从文件名提取（与 AI 路径
    同一规则），绝不再用 PENDING 占位——PENDING 会造成新旧键不一致、
    同合同无法覆盖、产生重复行。
    """
    combined = "\n".join(desensitized_pages)

    party_a_real = None
    match_a = PARTY_A_RE.search(combined)
    if match_a:
        # 脱敏后文本形如"甲方：CO0002"，组1(下划线内)为空、组2(常规文本)是编码；
        # 兼容下划线格式时组1/组3才是内容。取其一。
        value = match_a.group(1) or match_a.group(2) or match_a.group(3)
        if value:
            party_a_real = store.lookup_real_value(value)

    from ai_parser__ai import extract_contract_code_from_filename  # 惰性导入（同规则取编号）

    contract_code = extract_contract_code_from_filename(file_stem)
    return {
        # 文件名识别不到编号时用"无编号-"标记（绝不用 PENDING，避免与 AI 键冲突）
        "contract_code": contract_code or f"无编号-{file_stem}",
        "contract_term": None,
        "party_a": party_a_real or "待人工核对",
        "income": 0.0,
        "is_paid": False,
    }

# PDF / Word：合同判定只在**这两类**文件里进行
PDF_EXTENSIONS = {".pdf"}
WORD_EXTENSIONS = {".doc", ".docx", ".docm", ".rtf", ".wps"}
SCOPE_EXTENSIONS = PDF_EXTENSIONS | WORD_EXTENSIONS

_DEFAULT_TOKENS = ("合同", "协议")


def _tokens() -> tuple[str, ...]:
    raw = os.getenv("CONTRACT_TITLE_TOKENS", "").strip()
    if not raw:
        return _DEFAULT_TOKENS
    items = tuple(t.strip() for t in raw.replace("，", ",").split(",") if t.strip())
    return items or _DEFAULT_TOKENS


# 注意：这是**函数**结果快照，供 UI/日志展示"当前口径"；判定时每次重读环境变量。
CONTRACT_TOKENS: tuple[str, ...] = _tokens()

# 表外标题/抬头类块前缀（本轮把 `title:` 也纳入：表块被剥出来的题头行
# ——"付款申请""苗木采购合同"——正是判定文档类别最可靠的抬头）
_TITLE_BLOCK_PREFIXES = ("doc_title:", "header:", "paragraph_title:", "figure_title:",
                         "title:")
# 行首"栏目名"（不是标题）：如 "合同编号："/"合同名称：苗木采购合同"/"合同金额：¥…"。
# 这类行是**表格式栏目标签**，付款申请里到处都是；若不排除，
# "合同名称 | 马山…" 会让付款申请被当成合同（实测踩过）。
_LABEL_LINE_RE = re.compile(
    r"^\s*(合同|协议)?\s*(编号|号码|名称|金额|价款|期限|有效期|总额|总金额|日期|类型|状态)"
    r"\s*[:：]"
)
# 只有冒号、没有内容的空标签行（"合同编号：" 单独一行）也排除
_EMPTY_LABEL_RE = re.compile(r"^\s*[\u4e00-\u9fa5A-Za-z0-9（）()、·\-_/]{0,16}[:：]\s*$")


def _title_text(pages: list[str] | None, limit: int = 3) -> str:
    """标题/抬头区文本（用于"命名/标题里有没有 xxx合同"的判定）。

    取两部分拼接：
      1. `doc_title:` / `header:` / `paragraph_title:` 块（OCR 版式最可靠的标题来源）；
      2. 首页起**前 6 行非表格文本**（Office 直读没有块标签，真标题就在这儿）；
    并跳过 `table:`/`row:` 行与"栏目名"行（`合同编号：`/`合同名称：…`/`合同金额`）——
    否则付款申请正文里的"合同名称 | 苗木采购合同"会被误当成标题命中。

    例（实测）：
      · 苗木采购合同.docx 首页：`合同编号：`（跳过）→ `苗木采购合同`（命中）；
      · 5片区劳务付款申请.docx 首页：`付款申请`（不命中，合同名只出现在 table: 行里）。
    """
    pages = [str(p or "") for p in (pages or [])]
    parts: list[str] = []
    for page in pages[:limit]:
        for line in page.split("\n"):
            if line.startswith(_TITLE_BLOCK_PREFIXES):
                parts.append(line)
    head: list[str] = []
    for page in pages[:2]:
        for line in page.split("\n"):
            s = line.strip()
            if not s or s.startswith(("table:", "row:")):
                continue
            if s.startswith(_TITLE_BLOCK_PREFIXES):
                continue
            if _LABEL_LINE_RE.match(s) or _EMPTY_LABEL_RE.match(s):
                continue                     # 栏目标签行，不是标题
            head.append(s[:200])
            if len(head) >= 6:
                break
        if len(head) >= 6:
            break
    parts.extend(head)
    return "\n".join(parts)


def title_text(pages: list[str] | None, limit: int = 3) -> str:
    """标题/抬头区文本（对外接口）。

    ⚠️ 只取"第一条非表格行"是不够的（旧实现踩过）：苗木采购合同的首页第一行是
    `合同编号：` 这个**栏目标签**，真标题 `苗木采购合同` 在第二行 → 真合同反而被判成
    "不是合同"。所以这里取"标题块 + 首页前 6 行非栏目行"。
    """
    return _title_text(pages, limit)


def title_lines(pages: list[str] | None, limit: int = 3) -> list[str]:
    """标题区**逐行**返回（供按行判定，见 `_hit_in_title`）。

    ⚠️ 为什么要逐行判定而不是整段包含：
      扫描件 PDF 的表格单元格在页文本里是**裸行**（没有 `table:` 前缀），
      实测一份"付款申请.pdf"的抬头区里有 `合同金额`、`合同BK0008` 这类
      表头/单元格碎片——整段 `in` 判定会把付款申请误判成合同。
    """
    return [ln for ln in title_text(pages, limit).split("\n") if ln.strip()]


def _hit_in_title(pages: list[str] | None, tokens: tuple[str, ...]) -> tuple[str, str]:
    """标题区里是否有"标题样"的 xxx合同：**行尾**必须是"合同/协议书"。

    口径（贴合需求"命名/标题里是否含有 xxx合同"）：
      · `苗木采购合同` → 命中（行尾是合同）；
      · `合同金额` / `合同BK0008` / `合同编号：` → 不命中（合同不在行尾，是栏目/单元格）；
      · `doc_title:合同` 这种**版面判定为标题**的块，允许整行就是"合同"两个字。
    返回 (命中的字样, 命中的那一行)，未命中返回 ("", "")。
    """
    for raw in title_lines(pages):
        is_block = raw.startswith(_TITLE_BLOCK_PREFIXES)
        core = raw.split(":", 1)[1] if is_block else raw
        core = core.strip().rstrip("：: 　")
        for token in tokens:
            if not token:
                continue
            if is_block and core == token:
                return token, raw
            if core.endswith(token) or core.endswith(token + "书"):
                return token, raw
    return "", ""


@dataclass
class ContractVerdict:
    passed: bool = False                 # 是否认定为合同
    ext: str = ""                        # 文件扩展名（小写）
    in_scope: bool = False               # 扩展名是否属于 PDF/Word
    file_name: str = ""
    title: str = ""                      # 用于判定的标题文本
    hit_in: str = ""                     # filename | title | ""（命中的位置）
    hit_token: str = ""                  # 命中的字样（合同/协议…）
    tokens: tuple[str, ...] = field(default_factory=_tokens)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tokens"] = list(self.tokens)
        return d


def is_contract_doc(
    file_name: str | Path | None,
    pages: list[str] | None,
    tables: list[dict] | None = None,
) -> ContractVerdict:
    """判定"这是不是一份合同"：**PDF/Word + 命名或标题含"合同/协议"**。

    两条依据（严格按需求口径）：
      1. 扩展名 ∈ {.pdf,.doc,.docx,.docm,.rtf,.wps}；
      2. **文件名包含**"合同/协议"，或**标题区有"xxx合同/协议书"样式的标题行**
         （标题行必须以"合同/协议(书)"结尾，避免"合同金额/合同编号"这类栏目命中）。
    返回 ContractVerdict（含判定依据，可直接写日志/展示）。
    """
    tokens = _tokens()
    name = Path(str(file_name or "")).name
    ext = Path(name).suffix.lower()
    title = title_text(pages)
    v = ContractVerdict(ext=ext, file_name=name, title=title.strip(), tokens=tokens)

    if not name:
        v.reasons.append("没有文件名，无法判定")
        return v
    if ext not in SCOPE_EXTENSIONS:
        v.reasons.append(
            f"文件类型不是 PDF/Word（{ext or '无扩展名'}）——按新口径只有 PDF/Word 才可能是合同"
        )
        return v
    v.in_scope = True

    stem = Path(name).stem
    for token in tokens:                       # ① 文件名包含
        if token and token in stem:
            v.passed, v.hit_in, v.hit_token = True, "filename", token
            break
    if not v.passed:                           # ② 标题行以"合同/协议书"结尾
        token, line = _hit_in_title(pages, tokens)
        if token:
            v.passed, v.hit_in, v.hit_token = True, "title", token
            v.title = f"{v.title}\n（命中的标题行：{line[:80]}）"
    if v.passed:
        where = "文件名" if v.hit_in == "filename" else "文档标题"
        v.reasons.append(
            f"文件类型为 {ext}（PDF/Word）且{where}含「{v.hit_token}」→ 认定为合同"
        )
    else:
        v.reasons.append(
            f"文件名与标题都没有出现 {'/'.join(tokens)} 字样（文件名 {name!r}，"
            f"标题区 {(v.title or '（空）')[:60]!r}）→ 不是合同"
        )
    return v
