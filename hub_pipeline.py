"""按文件驱动的分类 + 脱敏流水线。

设计目标（对应这次讨论）：
1. 分类和脱敏跟"单个文件"的扫描绑在一起——调用方（比如 table.py 的 ScanWorker）
   每处理完一个文件，就可以立刻调用一次 process_file_to_hub()，不需要等
   队列里其它文件也扫描完。
2. JSON 缓存（scanner_core.process_file 产出的逐页 json）只是内部核验用的中间产物，
   不面向前端展示；真正"看得见"的产物是本文件写到 hub/<分类>/ 下面的合并 JSON，
   一份源文件对应一份，按文件分，不是按页分。
3. 实体映射（编号 <-> 真实值）已从本地 JSON 迁移到数据库分表存储
   （database_serv.MappingDbStore）：
   - company/party/date 明文入库，业务上可直接反查；
   - id_card/bank_card 只存 sha256 指纹 + Fernet 密文，解密必须通过
     require_permission('entity:decrypt:<类别>')；
   - hub/_mapping/entity_mapping.json 只在首次导入时被读取一次，
     本文件不再导出映射 JSON（加密方法保持原样：Fernet + hub_encryption.key）。

依赖：
    pip install cryptography --break-system-packages
（PyMuPDF 依赖已经在 pdf_native_extractor.py 里要求过，这里不重复）

⚠️ 需要你本地核对的地方（我这边看不到真实数据长什么样）：
- CATEGORY_KEYWORDS 里的关键词是我按常见发票/合同/物流凭证抬头拍的，
  建议拿几份真实样本跑一遍，把漏判/误判的关键词补进去。
- ID_CARD_RE / BANK_CARD_RE 是通用的"18位数字/13-19位数字"规则，
  没有做校验位（身份证第18位校验码、银行卡 Luhn 校验），如果需要更严格
  的验证再加，现在优先保证"不漏识别"。
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
from pathlib import Path

from scanner_core import BASE_DIR, OUTPUT_DIR as CACHE_DIR, process_file
from database_serv import MappingDbStore

# =========================================================
# 目录 / 文件常量
# =========================================================
HUB_DIR = BASE_DIR / "hub"


def ensure_hub_dirs() -> None:
    """确保 hub 中转目录存在（分类子目录按需创建）。"""
    HUB_DIR.mkdir(exist_ok=True)


# =========================================================
# 分类
# =========================================================
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "发票": ["发票", "增值税专用发票", "增值税普通发票", "价税合计", "纳税人识别号", "开票日期"],
    "合同": ["合同", "协议书", "甲方", "乙方", "本合同", "签订日期", "违约责任"],
    "物流凭证": ["运单", "提货单", "发货单", "承运人", "收货人", "快递单号", "物流"],
}


def extract_text_from_page_json(page_json: dict | None) -> str:
    """兼容原生文字页（有 full_text）和 OCR 页，防范 NoneType 问题。"""
    if not isinstance(page_json, dict):
        return ""
    
    res = page_json.get("res")
    if not isinstance(res, dict):
        return ""

    if res.get("full_text"):
        return str(res["full_text"])
    
    parsing_list = res.get("parsing_res_list") or []
    lines = []
    for block in parsing_list:
        if isinstance(block, dict):
            label = block.get("block_label") or ""
            content = block.get("block_content") or ""
            lines.append(f"{label}:{content}")
    return "\n".join(lines)


def classify_document(pages_text: list[str], file_path: Path | None = None) -> str:
    """按整份文件分类，而不是按单页——抬头/结尾权重更高。"""
    if not pages_text:
        return "其它"

    # 1. 优先对文件名进行关键字提取判断
    if file_path:
        file_name = file_path.name
        for category, keywords in CATEGORY_KEYWORDS.items():
            if any(kw in file_name for kw in keywords):
                return category

    # 2. 优先对 block_label 中的 header/doc_title 进行判断
    header_doc_title_text = []
    for page in pages_text:
        for line in page.split("\n"):
            if line.startswith("header:") or line.startswith("doc_title:"):
                header_doc_title_text.append(line)
    header_text_combined = "\n".join(header_doc_title_text)

    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in header_text_combined for kw in keywords):
            return category

    # 3. 严格精确判断含有 $\underline{\text{南苑新村}}$ 等格式特征的内容
    full_text = "\n".join(pages_text)
    if r"$\underline{\text{南苑新村}}$" in full_text or r"\underline{\text{" in full_text:
        for category, keywords in CATEGORY_KEYWORDS.items():
            if any(kw in full_text for kw in keywords):
                return category

    # 4. 兜底判定
    head = pages_text[0][:300]
    tail = pages_text[-1][-300:]
    best_category = "其它"
    best_score = 0
    for category, keywords in CATEGORY_KEYWORDS.items():
        head_tail_hits = sum((head + tail).count(kw) for kw in keywords)
        full_hits = sum(full_text.count(kw) for kw in keywords)
        score = head_tail_hits * 3 + full_hits
        if score > best_score:
            best_score = score
            best_category = category

    return best_category if best_score > 0 else "其它"


# =========================================================
# 脱敏
# =========================================================
ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
BANK_CARD_RE = re.compile(r"(?<!\d)\d{13,19}(?!\d)")
DATE_RE = re.compile(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?")

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

PARTY_B_RE = re.compile(
    r"乙方(?:\uff08[^\uff09]+\uff09|\([^\)]+\))?"
    r"(?:"
    r"[:\uff1a]\s*"
    r"(?:\$\s*\\underline\{\\text\{([^}]+)\}\}\s*\$|([^\n\uff0c,。\uff1b;]{2,60}))"
    r"|"
    r"\s*\$\s*\\underline\{\\text\{([^}]+)\}\}\s*\$"
    r")"
)

COMPANY_RE = re.compile(
    r"(?:(?<=^)|(?<=[\s，,。；;：:（(“\"、]))"
    r"[\u4e00-\u9fa5A-Za-z0-9（）()·]{2,40}?"
    r"(?:有限责任公司|有限公司|集团有限公司|集团|事务所|人民政府|管理局|委员会|中心)",
    re.MULTILINE,
)
# ⚠️ 已知局限：中文没有空格分词，纯正则没法在完全没有标点/标签的自由句子里
# 精确框定公司名边界（比如"本次付款方为北京xx有限公司"这种没有冒号/逗号
# 分隔的写法，仍可能把前面的字也吞进去）。这里用"非贪婪 + 前面必须是标点/
# 空白/行首"的方式，覆盖真实合同里最常见的"甲方：/收款单位：/供货方：xxx"
# 这类有明确标签或标点分隔的写法。如果实际文档里公司名经常出现在完全无
# 标点的自由句子中，建议这部分交给第5步接的外部 AI API 做实体识别，
# 正则只作为结构化字段（有冒号/顿号分隔）的快速识别。
# 缓解（v3）：以"甲方：/乙方："为干净锚点（其后必须是完整名称），公司名按
# "最后一个公司后缀词"截断、个人按左括号截断后入库（见 _tail_cut_company /
# _tail_cut_person）；自由文本中的同名公司经 _resolve_company_span 复用该
# 干净编号，避免行首贪婪吞前缀造成的同公司多编号。


def desensitize_text(
    text: str,
    store: MappingDbStore,
    tracker: DesensTracker | None = None,
) -> str:
    """单页文本脱敏。注意匹配顺序：先处理 18 位身份证号，再处理 13-19 位
    银行卡号，避免银行卡的正则把身份证号也吃掉（替换成编码后就不再是
    纯数字，后面的规则自然不会再命中）。

    脱敏完整性复查（标准正则全部替换完后再补两遍，对应"前方被脱敏的值，
    后方是否还以明文出现"的核对）：
      ① 数字缝合补漏：容忍空格/换行/连字符等"排版分隔符"夹在数字中间。
         同一证件/卡号若换了排版格式（折行、4 位分组、中间插符号）再次
         出现，也能补脱敏——re.sub 只能消灭"同格式"重复，格式一变就会漏；
      ② 已登记实体复查：标准正则因"无标点边界"漏掉的公司/人员/日期
         （自由句子里无冒号/顿号分隔的第二次出现等），按本文件已登记的真实值
         精确补码（同一真实值用同一个编号，全文一致）。

    复杂度：①、② 各为单遍 O(len(text)) 的正则扫描，绝不做"每个已脱敏值 ×
    整篇文本"的逐值重扫（那种是 O(值数量 × 文本长度)）。tracker 提供跨页
    缓存：同一份文件多页共用，保证同一真实值全文编号/掩码一致。
    """
    digit_cache = tracker.digit_cache if tracker is not None else {}
    name_cache = tracker.name_cache if tracker is not None else {}

    def repl_company(match: re.Match) -> str:
        # 自由文本兜底（COMPANY_RE 命中）。原则：尽量把替换收敛到"干净公司名"：
        #   1) 先按公司后缀词截掉粘连装饰；
        #   2) 若截断结果以本文件已登记（多来自甲方/乙方锚定）的公司名为后缀，
        #      只替换该后缀段、保留前缀原文（如"本合同由"），编号必然与锚定处一致；
        #   3) 否则把截断后的干净公司名登记后整段替换（防明文泄漏优先）。
        return _resolve_company_span(match.group(0), store, name_cache)

    def _party_category(value: str) -> str:
        # 甲方/乙方后面如果本身是公司名称，归到 company 类别，跟文中其它地方
        # 出现的同一家公司共用一条映射；不是公司名称（比如个人姓名）才归 party。
        # （v2：判定改用"公司后缀词锚定"而非 COMPANY_RE 边界匹配，见
        #   _tail_cut_company——与甲方/乙方锚定截断使用同一套后缀清单，语义一致）
        return "company" if _tail_cut_company(value)[1] else "party"

    def repl_party_a(match: re.Match) -> str:
        # 取值顺序：组1(有冒号+下划线) -> 组2(有冒号+常规文本) -> 组3(无冒号+下划线)
        value = match.group(1) or match.group(2) or match.group(3)
        if not value:
            return match.group(0)
        # 甲方/乙方 之后必须是完整名称（合同写法约定）：
        #   · 公司 → 截到"最后一个公司后缀词"（有限公司/集团…），把 OCR 粘连的
        #     （盖章）/证件号/后续正文排除出名称——这是"干净来源"，入库值不带垃圾前缀；
        #   · 个人 → 截到第一个左括号（其后多为（签字）（身份证号：…）等装饰）。
        # 干净名称入库后，同一公司/个人在自由文本中的再次出现才能按值复用同一编号。
        value = value.strip()
        clean, is_company = _tail_cut_company(value)
        if not is_company:
            clean = _tail_cut_person(clean)
        if not clean:
            return match.group(0)
        code = store.get_or_create_code(_party_category(clean), clean)
        name_cache[clean] = code  # 登记：复查②/自由文本后缀复用同一编号
        return f"甲方：{code}"

    def repl_party_b(match: re.Match) -> str:
        # 取值顺序：组1(有冒号+下划线) -> 组2(有冒号+常规文本) -> 组3(无冒号+下划线)
        # 注意：PARTY_B_RE 是独立编译的正则，组号同样从 1 开始
        value = match.group(1) or match.group(2) or match.group(3)
        if not value:
            return match.group(0)
        value = value.strip()
        clean, is_company = _tail_cut_company(value)
        if not is_company:
            clean = _tail_cut_person(clean)
        if not clean:
            return match.group(0)
        code = store.get_or_create_code(_party_category(clean), clean)
        name_cache[clean] = code
        return f"乙方：{code}"

    def repl_date(match: re.Match) -> str:
        value = match.group(0)
        code = store.get_or_create_code("date", value)
        name_cache[value] = code
        return code

    def repl_id(match: re.Match) -> str:
        # 证件号是连续数字（正则已保证），直接进缓存，复查①换格式出现时复用同编号
        return _desens_id_rep(store, match.group(0), digit_cache)

    def repl_bank(match: re.Match) -> str:
        return _desens_bank_rep(store, match.group(0), digit_cache)

    # 甲方/乙方必须先处理：否则"甲方：北京xx有限公司"会先被 COMPANY_RE
    # 替换成公司编码，紧接着又被甲方/乙方规则把编码当文本再包一层编号，
    # 变成要解两次密才能还原真实名称。
    text = PARTY_A_RE.sub(repl_party_a, text)
    text = PARTY_B_RE.sub(repl_party_b, text)
    text = COMPANY_RE.sub(repl_company, text)
    text = DATE_RE.sub(repl_date, text)
    text = ID_CARD_RE.sub(repl_id, text)
    text = BANK_CARD_RE.sub(repl_bank, text)

    # ---- 复查①：数字缝合补漏（换格式再出现的证件/卡号）----
    text = _tolerant_digit_mask(text, store, digit_cache)
    # ---- 复查②：已登记中文实体精确复查（无标点边界再出现的公司/人员/日期）----
    text = _name_rescan(text, name_cache)
    return text


# =========================================================
# 脱敏复查辅助（DesensTracker / 数字缝合 / 实体补码）
# =========================================================
class DesensTracker:
    """同一份文件的脱敏追踪器（跨页共用）。

    职责：
      1. digit_cache：证件号/卡号(已去分隔符的数字串) -> "掩码[编号]"。
         保证同一号码跨页/换格式出现时复用同一个编号，掩码一致；
      2. name_cache：公司/人员/日期真实值 -> 编号。
         复查②用它对"标准正则漏掉、仍在文本中的明文"补码。
    线程说明：扫描线程只入队原文，消费者线程独占调用本类，天然单线程，无需加锁。
    """

    def __init__(self) -> None:
        self.digit_cache: dict[str, str] = {}
        self.name_cache: dict[str, str] = {}


def _desens_id_rep(store: MappingDbStore, digits: str, cache: dict[str, str]) -> str:
    """18 位证件号（已去分隔符）-> "掩码[编号]"，同一号码复用同一缓存结果。

    digits 必须已是纯数字串（可含末尾 X）。掩码规则与原 repl_id 一致：
    前 6 + ****** + 后 4。
    """
    rep = cache.get(digits)
    if rep is not None:
        return rep
    masked = digits[:6] + "******" + digits[-4:]
    code = store.get_or_create_secret_code("id_card", digits, masked)
    rep = f"{masked}[{code}]"
    cache[digits] = rep
    return rep


def _desens_bank_rep(store: MappingDbStore, digits: str, cache: dict[str, str]) -> str:
    """13~19 位银行卡号（已去分隔符）-> "掩码[编号]"，同一卡号复用同一缓存结果。

    掩码规则与原 repl_bank 一致：前 6 + 中段全星 + 后 4。
    """
    rep = cache.get(digits)
    if rep is not None:
        return rep
    masked = digits[:6] + "*" * max(len(digits) - 10, 4) + digits[-4:]
    code = store.get_or_create_secret_code("bank_card", digits, masked)
    rep = f"{masked}[{code}]"
    cache[digits] = rep
    return rep


# 数字间允许的"排版分隔符"（空格/换行/制表/连字符/下划线/斜杠/点/全角点）。
# *、[、]（掩码产物）不在其中，因此已掩码的 "110101******1234[ID0001]"
# 不会被误拼回；普通汉字/字母同样不是分隔符，遇到即断。
_TOLERANT_DIGIT_RE = re.compile(r"(?<![\dXx])[\dXx](?:[\s\-_/．.·]{0,2}[\dXx])*")
_ID_FULL_RE = re.compile(r"\d{17}[\dXx]")     # 18 位：17 数字 + 数字/X
_BANK_FULL_RE = re.compile(r"\d{13,19}")      # 纯数字卡号


def _tolerant_digit_mask(text: str, store: MappingDbStore, cache: dict[str, str]) -> str:
    """复查①：数字缝合补漏（单遍 O(len(text))）。

    标准 ID/卡号正则要求"连续纯数字段"；同一号码一旦被折行、按 4 位分组或
    中间插入空格/连字符就会漏掉，以明文流出。这里先按"允许少量排版分隔符"
    把数字串拼回（len 13~19，18 位按证件处理），再按值脱敏。
    用单次正则扫描实现，不做"每个已脱敏值 × 全文"的逐值查找。
    """
    def _fix(m: re.Match) -> str:
        digits = re.sub(r"[^\dXx]", "", m.group(0))  # 拼回去掉分隔符的纯数字串
        if _ID_FULL_RE.fullmatch(digits):
            return _desens_id_rep(store, digits, cache)
        if _BANK_FULL_RE.fullmatch(digits):
            return _desens_bank_rep(store, digits, cache)
        return m.group(0)  # 非敏感长度（过短/过长），原样保留
    return _TOLERANT_DIGIT_RE.sub(_fix, text)


def _name_rescan(text: str, name_cache: dict[str, str]) -> str:
    """复查②：已登记中文实体（公司/人员/日期）在文中仍以明文出现 → 补码。

    触发场景：同一真实值第一次出现在"甲方：xxx/行首"等有标点边界的位置，
    被标准正则命中；第二次出现在自由句子里（前面是普通汉字、无标点/行首），
    COMPANY_RE 的边界规则不命中，原文残留——此时按已登记的真实值精确补码。

    实现：把全部已登记值按长度降序拼成一次 alternation，单遍扫描 O(len(text))。
    长度降序保证同一位置既匹配短名又匹配长名时取长名（如"张三" vs
    "张三丰科技有限公司"），避免把长名拆残。同一真实值映射同一编号，全文一致。
    """
    if not name_cache:
        return text
    keys = sorted({k for k in name_cache if k}, key=len, reverse=True)
    if not keys:
        return text
    pattern = re.compile("|".join(re.escape(k) for k in keys))
    return pattern.sub(lambda m: name_cache[m.group(0)], text)


# =========================================================
# 甲方/乙方锚定提取辅助（解决"同公司捕获不干净 → 同公司多编号"）
# =========================================================
# 原则：合同里"甲方：/乙方："之后的文本必须是**完整名称**（公司或个人），
# 这是最可靠的干净来源。名称的右边界如何确定？
#   · 公司：名称以"公司后缀词"收尾（有限公司/集团/事务所/中心…），
#     取**最后一个**后缀词的结尾为右边界，可把 OCR 粘连的（盖章）/证件号/
#     后续正文排除出名称；
#   · 个人：无后缀词，右边界取第一个左括号（其后通常是（签字）（身份证号：…）
#     等装饰），或原正则已排除的标点（，。；;等）。
# 干净名称入库后，自由文本中同名公司的再次出现通过 _resolve_company_span
# 按"已登记名后缀匹配"复用同一编号——从根上避免行首贪婪把"由/收到…"等
# 前缀吞进映射造成的同公司跨页不同编号（原模块注释"已知局限"的缓解）。
_COMPANY_TAIL_TOKENS = (
    # 按长度降序排列的候选后缀词（长词优先，避免"集团有限公司"被"有限公司"腰斩）
    "股份有限公司", "有限责任公司", "集团有限公司",
    "有限公司", "分公司", "人民政府", "事务所", "管理局", "委员会",
    "中心", "集团",
)
_COMPANY_TAIL_RE = re.compile("|".join(_COMPANY_TAIL_TOKENS))


def _tail_cut_company(raw: str) -> tuple[str, bool]:
    """把一段文本按"最后一个公司后缀词"截断。

    返回 (截断后的干净文本, 是否判定为公司)。
    · 找到后缀词 → 返回 text[:后缀词结尾]（把（盖章）/证件号/后续正文等
      粘连内容排除），判定为公司；
    · 找不到 → 原样返回，判定为个人（由调用方再走个人截断）。
    """
    text = (raw or "").strip()
    last = None
    for m in _COMPANY_TAIL_RE.finditer(text):
        last = m
    if last is None:
        return text, False
    return text[: last.end()].strip(), True


def _tail_cut_person(raw: str) -> str:
    """个人（无公司后缀）名称右边界：截到第一个全角/半角左括号。

    括号后通常是（签字）（身份证号：…）（盖章）等装饰，不属于姓名本身；
    其余标点（，。；;等）已被 PARTY_A_RE/PARTY_B_RE 的文本分支排除。
    """
    text = (raw or "").strip()
    for ch in ("（", "("):
        idx = text.find(ch)
        if idx > 0:
            text = text[:idx].strip()
    return text


def _resolve_company_span(
    span: str,
    store: MappingDbStore,
    name_cache: dict[str, str],
) -> str:
    """把 COMPANY_RE 捕获的一段文本解析为尽量干净的替换文本（自由文本兜底）。

    步骤：
      1) _tail_cut_company 截到最后一个公司后缀词（去掉（盖章）等粘连装饰）；
      2) 截断后若以"本文件已登记"的公司名（多来自甲方/乙方锚定）为后缀，
         只替换该后缀段、保留前缀原文（如"本合同由"），编号与锚定处一致；
      3) 否则把截断出的干净公司名登记后替换。
    返回替换文本（前缀原文 + 公司编号，或仅编号）。
    """
    clean, is_company = _tail_cut_company(span)
    if not is_company:
        return span  # 防御：COMPANY_RE 命中但截不出后缀（不应发生），原样保留
    # 最长已登记后缀匹配（clean 是 span 的前缀段；prefix = clean 中去掉该名称的剩余）
    best_key: str | None = None
    for known in name_cache:
        if clean.endswith(known) and (best_key is None or len(known) > len(best_key)):
            best_key = known
    if best_key is not None:
        return clean[: len(clean) - len(best_key)] + name_cache[best_key]
    # 未登记：登记干净公司名（首见），整段替换（防明文泄漏优先）
    code = store.get_or_create_code("company", clean)
    name_cache[clean] = code
    return code


# =========================================================
# 单文件端到端入口
# =========================================================
def extract_contract_fields(file_stem: str, desensitized_pages: list[str], store: MappingDbStore) -> dict:
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

    from ai_parser import extract_contract_code_from_filename  # 惰性导入（同规则取编号）

    contract_code = extract_contract_code_from_filename(file_stem)
    return {
        # 文件名识别不到编号时用"无编号-"标记（绝不用 PENDING，避免与 AI 键冲突）
        "contract_code": contract_code or f"无编号-{file_stem}",
        "contract_term": None,
        "party_a": party_a_real or "待人工核对",
        "income": 0.0,
        "is_paid": False,
    }


def process_file_to_hub(file_path: Path, current_user: dict | None = None) -> dict:
    """单文件流水线（流水线重叠版）。

    重叠原理：扫描(OCR，GPU 密集)与后处理(脱敏，CPU/DB)通过队列在两个线程里
    并行执行——每页 OCR 一完成，立刻把该页文本交给消费者线程脱敏，不等整份
    文件扫完（已确认 PaddleOCR-VL 在本文代码里是按页调用 predict、逐页出结果的）。
    分类因为要看整份文件的抬头/结尾，放在两条流水线都结束后做。
    """
    ensure_hub_dirs()
    store = MappingDbStore.load()
    # 整份文件跨页共用的脱敏追踪器：同一真实值（证件/卡号/公司名）全文只用一个
    # 编号/掩码；复查补漏也能命中"前页已脱敏、后页又换格式出现"的明文。
    tracker = DesensTracker()

    # ---- 流水线缓冲与结果容器（单消费者，天然保持页序）----
    page_queue: queue.Queue = queue.Queue()
    desensitized_pages: list[str] = []      # 消费者线程按页序写入
    cache_paths: list[Path] = []            # 逐页 json 缓存路径（生产端回调记录）
    consumer_errors: list[Exception] = []

    def on_page_json(page_path: Path) -> None:
        """生产端回调（运行在扫描线程）：每页 JSON 落盘即提取文本并入队。"""
        cache_paths.append(page_path)
        try:
            page_json = json.loads(page_path.read_text(encoding="utf-8"))
        except Exception:
            page_json = {}
        page_queue.put(extract_text_from_page_json(page_json))

    def consumer() -> None:
        """消费端线程：从队列取文本做脱敏，与生产端 OCR 重叠执行。"""
        try:
            while True:
                text = page_queue.get()
                if text is None:  # 结束哨兵
                    return
                desensitized_pages.append(desensitize_text(text, store, tracker))
        except Exception as exc:
            consumer_errors.append(exc)

    consumer_thread = threading.Thread(target=consumer, daemon=True, name="hub-desensitize")
    consumer_thread.start()
    try:
        # 生产端：逐页扫描（每页 OCR 完立刻回调，不等后续页）
        process_file(file_path, CACHE_DIR, on_page_json=on_page_json)
    finally:
        page_queue.put(None)  # 通知消费者收尾（即使扫描异常也要出队）
        consumer_thread.join(timeout=120)
    if consumer_errors:
        raise consumer_errors[0]

    # 2. 按整份文件分类（不是按页，需要抬头/结尾）
    category = classify_document(desensitized_pages, file_path)

    # 3. 落盘：一份源文件 -> 一份合并 json，按分类归到不同文件夹
    category_dir = HUB_DIR / category
    category_dir.mkdir(parents=True, exist_ok=True)
    out_path = category_dir / f"{file_path.stem}.json"
    out_path.write_text(
        json.dumps(
            {
                "source_file": file_path.name,
                "category": category,
                "page_count": len(desensitized_pages),
                "pages": desensitized_pages,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    result = {
        "category": category,
        "hub_json_path": out_path,
        "cache_json_paths": cache_paths,
    }
    # 台账写入职责划分（修复"双写/重复行"）：
    # - AI 启用：台账由 AI 路径（process_hub_file -> fill_contract_ledger，文件名编号 +
    #   AI 填项目/备注/自定义栏目）写入；这里**不再**产出正则版 contract_fields，
    #   否则 table.py 会把正则行（无项目/备注、明文甲方）再写一次或覆盖 AI 行；
    # - AI 关闭：才保留正则兜底 contract_fields（编号已改为文件名提取，不再 PENDING）。
    ai_active = os.getenv("AI_ENABLED", "0") == "1" and bool(os.getenv("AI_API_KEY", ""))
    if category == "合同" and not ai_active:
        result["contract_fields"] = extract_contract_fields(file_path.stem, desensitized_pages, store)

    # 5. AI 协作（可选）：特征哈希归档 + (合同)台账填写 + 条款摘要 + 分块向量化。
    #    开关：.env 中 AI_ENABLED=1 且已配置 AI_API_KEY；任何一步失败都不阻断主流程，
    #    结果/错误放入 result["ai_report"] / result["ai_error"] 供 UI 展示。
    if ai_active:
        try:
            from ai_parser import process_hub_file

            result["ai_report"] = process_hub_file(out_path, current_user)
        except Exception as exc:
            result["ai_error"] = f"{type(exc).__name__}: {exc}"

    return result
