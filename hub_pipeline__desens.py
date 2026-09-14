"""按文件驱动的分类 + 脱敏流水线。

设计目标（对应这次讨论）：
1. 分类和脱敏跟"单个文件"的扫描绑在一起——调用方（比如 table__ui.py 的 ScanWorker）
   每处理完一个文件，就可以立刻调用一次 process_file_to_hub()，不需要等
   队列里其它文件也扫描完。
2. JSON 缓存（scanner_core__scan.process_file 产出的逐页 json）只是内部核验用的中间产物，
   不面向前端展示；真正"看得见"的产物是本文件写到 hub/ 下的合并 JSON
   （**扁平存放**，一份源文件对应一份、按文件分不是按页分；分类写入 JSON 的
   category 字段与数据库记录，不再按分类建子文件夹）。
3. 实体映射（编号 <-> 真实值）已从本地 JSON 迁移到数据库分表存储
   （database_serv__infra.MappingDbStore）：
   - company/party/date 明文入库，业务上可直接反查；
   - id_card/bank_card 只存 sha256 指纹 + Fernet 密文，解密必须通过
     require_permission('entity:decrypt:<类别>')；
   - hub/_mapping/entity_mapping.json 只在首次导入时被读取一次，
     本文件不再导出映射 JSON（加密方法保持原样：Fernet + hub_encryption.key）。

依赖：
    pip install cryptography --break-system-packages
（PyMuPDF 依赖已经在 pdf_native_extractor__scan.py 里要求过，这里不重复）

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

from scanner_core__scan import BASE_DIR, OUTPUT_DIR as CACHE_DIR, process_file
from database_serv__infra import MappingDbStore
import project_registry__desens   # 项目名称自定义加密（管理员登记 → 编号）
import self_entity__desens        # 本公司（我方主体）：全局脱敏 + 编号特殊提示

# =========================================================
# 目录 / 文件常量
# =========================================================
# hub 根：默认"仓库根目录 / hub"；工作区（界面叫「仓库」）切换时由
# `workspace__infra` 通过环境变量 `DSH_HUB_DIR` 指定，`apply_active()` 也会就地改这个
# 常量（支持进程内热切换）。所以**不要**再把它写成固定路径。
from workspace__infra import hub_root as _hub_root

HUB_DIR = _hub_root()


def ensure_hub_dirs() -> None:
    """确保 hub 中转目录存在。

    hub 落盘规则（本轮调整）：
      · **保留源文件夹结构**：文件夹输入 → `hub/<源文件夹子树>/<文件名>.json`，
        一个文件夹不会被拆成 hub 根目录下的一堆散文件；
      · 直接拖入的单个文件（没有源文件夹）→ `hub/<文件名>.json`（保持扁平）；
      · 不再按**分类**建目录（旧范式）：`hub/RAG test/` 是历史分类目录的归档位，
        **任何代码都不得向该目录写入**。
    """
    HUB_DIR.mkdir(exist_ok=True)


def hub_target(file_path: Path, source_root: Path | None = None) -> tuple[Path, str, str]:
    """(hub JSON 路径, doc_key, 源文件夹相对路径)。

    doc_key 取"源文件夹起的相对路径（不含扩展名）"：
      · 文件夹输入：`南苑新村/合同/KH-YF-2026-02` —— 天然带文件夹归属，
        且不同子目录里的同名文件不会互相覆盖（扁平 stem 会撞 doc_key）；
      · 单文件输入：就是文件名 stem（与历史数据/旧代码完全兼容）。
    源文件夹相对路径用 POSIX 分隔符记录（跨平台稳定，便于入库与查询）。
    """
    rel_dir = ""
    if source_root is not None:
        try:
            rel_dir = file_path.resolve().relative_to(Path(source_root).resolve()).parent.as_posix()
        except Exception:
            rel_dir = ""
    if rel_dir in (".", "/"):
        rel_dir = ""
    # 保留目录名保护：hub 下的 "RAG test"（旧分类目录归档）与 "_mapping"（映射库）
    # 不允许被源文件夹同名覆盖，遇到时加一层前缀隔离。
    if rel_dir:
        first = rel_dir.split("/")[0]
        if first in ("RAG test", "_mapping"):
            rel_dir = f"源文件夹/{rel_dir}"
    out_dir = HUB_DIR / rel_dir if rel_dir else HUB_DIR
    doc_key = f"{rel_dir}/{file_path.stem}" if rel_dir else file_path.stem
    return out_dir / f"{file_path.stem}.json", doc_key, rel_dir


# =========================================================
# 分类
# =========================================================
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "发票": ["发票", "增值税专用发票", "增值税普通发票", "价税合计", "纳税人识别号", "开票日期"],
    # ⚠️ "合同"**不再**走关键词打分（见下）：旧的 ["合同","协议书","甲方","乙方","本合同",
    # "签订日期","违约责任"] 让"带合同编号列的 Excel 汇总表""引用合同金额的付款申请"
    # 都被判成合同。合同判定改为 contract_rules__desens（PDF/Word + 命名/标题含"合同/协议"）。
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
    """按整份文件分类，而不是按单页——抬头/结尾权重更高。

    分类顺序（本轮修正）：
      1. **合同**：`contract_rules__desens.is_contract_doc` —— 文件类型必须是 PDF/Word，
         且**文件名或标题**含"合同/协议"（不再看正文里的"甲方/合同编号/违约"等词，
         避免发票、汇总表、付款申请被误判）；
      2. 发票 / 物流凭证：沿用关键词（文件名 → 抬头/标题块 → 全文打分）；
      3. 都不命中 → 其它。
    """
    pages_text = [str(p or "") for p in (pages_text or [])]
    if not pages_text:
        return "其它"
    file_name = file_path.name if file_path else ""

    # 1. 合同（新口径：PDF/Word + 命名/标题含"合同/协议"）
    try:
        import contract_rules__desens as contract_rules

        verdict = contract_rules.is_contract_doc(file_name, pages_text)
        if verdict.passed:
            return "合同"
    except Exception:
        pass

    # 2. 发票 / 物流凭证：关键词命中（文件名 → 抬头/标题 → 全文打分）
    full_text = "\n".join(pages_text)
    if file_name:
        for category, keywords in CATEGORY_KEYWORDS.items():
            if any(kw in file_name for kw in keywords):
                return category

    header_doc_title_text = []
    for page in pages_text:
        for line in page.split("\n"):
            if line.startswith(("header:", "doc_title:", "title:")):
                header_doc_title_text.append(line)
    header_text_combined = "\n".join(header_doc_title_text)
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in header_text_combined for kw in keywords):
            return category

    # 3. 兜底判定（只在非合同类别之间比分数）
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


def explain_classification(
    pages_text: list[str], file_path: Path | str | None = None
) -> dict:
    """**分类体检**：这个文件为什么被判成这一类？命中在哪一步、命中了哪些关键词。

    本轮修正后的口径（重要）：
      · **合同**不再看正文关键词，只由 `contract_rules__desens` 判定：
        文件类型必须是 PDF/Word，且**文件名或标题**含"合同/协议"。
        所以"带合同编号列的 xlsx""引用合同金额的付款申请"不会再被判成合同；
      · 发票 / 物流凭证仍走关键词（文件名 → 抬头块 → 首尾+全文打分）。

    返回：
      category      最终类别
      stage         命中步骤：contract_rule / filename / header_doc_title / head_tail_score / other
      reason        人读说明
      contract      合同判定明细（ContractVerdict.to_dict()）
      hits          各步骤命中的关键词
      scores        兜底打分（head/tail 命中×3 + 全文命中）
      keyword_table 关键词表（便于核对规则本身）
    """
    pages_text = [str(p or "") for p in (pages_text or [])]
    full_text = "\n".join(pages_text)
    name = Path(file_path).name if file_path else ""
    hits: dict[str, dict[str, list[str]]] = {"filename": {}, "header_doc_title": {}}
    out = {"category": "其它", "stage": "other", "reason": "无内容或未命中任何关键词",
           "hits": hits, "scores": {}, "keyword_table": CATEGORY_KEYWORDS,
           "filename": name, "head_preview": "", "tail_preview": "",
           "contract": None}

    if not pages_text:
        return out
    out["head_preview"] = pages_text[0][:300]
    out["tail_preview"] = pages_text[-1][-300:]

    # 0) 合同（新口径：PDF/Word + 命名/标题含"合同/协议"）—— 优先于一切关键词
    try:
        import contract_rules__desens as contract_rules

        verdict = contract_rules.is_contract_doc(name, pages_text)
        out["contract"] = verdict.to_dict()
        if verdict.passed:
            out.update({"category": "合同", "stage": "contract_rule",
                        "reason": "；".join(verdict.reasons)})
            return out
    except Exception as exc:
        out["contract"] = {"error": f"{type(exc).__name__}: {exc}"}

    # 1) 文件名（非合同类别）
    if name:
        for category, keywords in CATEGORY_KEYWORDS.items():
            matched = [k for k in keywords if k in name]
            if matched:
                hits["filename"][category] = matched
        if hits["filename"]:
            category = next(iter(hits["filename"]))
            out.update({"category": category, "stage": "filename",
                        "reason": f"文件名「{name}」包含 {hits['filename'][category]}"})
            return out

    # 2) header / doc_title 块
    header_lines = []
    for page in pages_text:
        for line in page.split("\n"):
            if line.startswith(("header:", "doc_title:", "title:")):
                header_lines.append(line)
    header_text = "\n".join(header_lines)
    if header_text:
        for category, keywords in CATEGORY_KEYWORDS.items():
            matched = [k for k in keywords if k in header_text]
            if matched:
                hits["header_doc_title"][category] = matched
        if hits["header_doc_title"]:
            category = next(iter(hits["header_doc_title"]))
            out.update({"category": category, "stage": "header_doc_title",
                        "reason": f"抬头/标题块（header:/doc_title:）包含 "
                                  f"{hits['header_doc_title'][category]}"})
            return out

    # 3) 兜底打分（首尾 300 字命中×3 + 全文命中）
    head, tail = pages_text[0][:300], pages_text[-1][-300:]
    scores: dict[str, int] = {}
    for category, keywords in CATEGORY_KEYWORDS.items():
        head_tail_hits = sum((head + tail).count(kw) for kw in keywords)
        full_hits = sum(full_text.count(kw) for kw in keywords)
        scores[category] = head_tail_hits * 3 + full_hits
    out["scores"] = scores
    best = max(scores, key=lambda c: scores[c]) if scores else "其它"
    if scores.get(best, 0) > 0:
        out.update({"category": best, "stage": "head_tail_score",
                    "reason": f"兜底打分最高：{best}={scores[best]}"
                              f"（首尾命中×3 + 全文命中；各类得分 {scores}）"})
    return out


def explain_hub_file(hub_json_path: Path | str) -> dict:
    """对**已落盘的 hub JSON** 做分类体检（用文件里记录的 pages/source_file）。"""
    data = json.loads(Path(hub_json_path).read_text(encoding="utf-8"))
    info = explain_classification(list(data.get("pages") or []),
                                 data.get("source_file") or Path(hub_json_path).name)
    info["hub_category"] = data.get("category")
    info["doc_key"] = data.get("doc_key")
    info["source_file"] = data.get("source_file")
    # 表格列头也带上（xlsx 被判"合同"时，通常是列头命中）
    info["table_headers"] = [t.get("header") for t in (data.get("tables") or [])]
    return info


# =========================================================
# 脱敏
# =========================================================
ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
# 银行卡：13~19 位连续数字。⚠️ 必须排除"字母编号"——单据号写成 PO-2026…0017 / RJ-2026…
# 时，数字段本身满足长度条件，会被误当卡号登记（实测 BC0009/BC0010）。因此左侧不允许
# 紧跟字母或"字母+连字符"，右侧不允许紧跟字母/数字。
BANK_CARD_RE = re.compile(r"(?<![0-9A-Za-z])(?<![A-Za-z]-)\d{13,19}(?![0-9A-Za-z])")
DATE_RE = re.compile(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?")

# 甲/乙方匹配 + 正则兜底的台账字段抽取已搬到 **contract_rules__desens**（轻量模块）：
# 「待入账 · 从 hub 刷新」要用它做字段预填，而本模块导入期会拉 paddle/torch
# （实测：刷新按钮首次点击因此要 80s）。这里保留同名导入，既有调用方无需改动。
from contract_rules__desens import PARTY_A_RE, extract_contract_fields  # noqa: E402

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
    r"(?:有限责任公司|有限公司|集团有限公司|集团|事务所|人民政府|管理局|委员会|事业部|中心)",
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


# =========================================================
# 新增脱敏规则（按需求）：纳税人识别号 / 开户银行 / 银行账号（含对公账号）
# =========================================================
# 纳税人识别号（锚点式）：只要有"纳税人识别号/税号"等标签，其后的 15~20 位数字/字母一律脱敏。
TAX_ID_ANCHOR_RE = re.compile(
    r"(?:纳税人识别号|纳税识别号|纳税人识别码|统一社会信用代码|税号)\s*[:：]?\s*([0-9A-Z]{15,20})"
)
# 统一社会信用代码（无标签）：18 位，字母表排除 I O S V Z，且**至少含一个大写字母**
# （纯 18 位数字是身份证，交给 ID_CARD_RE，避免类别错判）。
TAX_ID_RE = re.compile(
    r"(?<![0-9A-Z])(?=[0-9A-HJ-NP-RT-UW-Y]{0,17}[A-HJ-NP-RT-UW-Y])"
    r"[0-9A-HJ-NP-RT-UW-Y]{18}(?![0-9A-Z])"
)
# 开户银行：① 锚点式（开户行/开户银行/银行名称 + 其后名称）；② 已知银行名（含分支后缀）
BANK_NAME_ANCHOR_RE = re.compile(
    r"(?:开户行|开户银行|开户机构|银行名称|开户网点)\s*[:：]?\s*"
    r"([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,24}?)(?=[\s，,。；;：:、]|$)"
)
_KNOWN_BANKS = (
    "中国人民银行", "中国工商银行", "中国农业银行", "中国建设银行", "中国银行",
    "交通银行", "招商银行", "中信银行", "光大银行", "华夏银行", "民生银行",
    "兴业银行", "浦发银行", "上海浦东发展银行", "广发银行", "平安银行",
    "中国邮政储蓄银行", "邮储银行", "北京银行", "上海银行", "江苏银行",
    "南京银行", "宁波银行", "杭州银行", "农村商业银行", "农商银行",
    "村镇银行", "城市商业银行", "农村信用合作社", "信用社",
)
BANK_NAME_RE = re.compile(
    # ⚠️ 分支后缀必须用 (?:…) 包住整个"已知行名"分组：
    #    以前写成 "A|B|C(?:分行|支行…)?" → 后缀只对**最后一个**候选生效，
    #    于是"宁波银行股份有限公司无锡分行"只匹配到"宁波银行"，尾缀"股份有限公司无锡分行"
    #    留成明文（再被公司规则误吃）。现在整组都能带后缀。
    "(?:" + "|".join(re.escape(b) for b in sorted(_KNOWN_BANKS, key=len, reverse=True)) + ")"
    + r"(?:[\u4e00-\u9fa5]{0,10}(?:分行|支行|营业部|分理处|办事处|储蓄所))?"
)
# 银行账号：① 锚点式（账号/帐号/账户/卡号 + 其后 12~19 位，容忍空格/连字符）
#          ② 上下文式（"银行" 后 15 个字以内跟 12~19 位数字）——含对公账号，不强制 Luhn
ACCOUNT_ANCHOR_RE = re.compile(
    r"(?:账号|帐号|账户|银行账号|对公账号|卡号|银行账户)\s*[:：]?\s*((?:\d[\s\-]?){11,18}\d)"
)
BANK_CONTEXT_ACCOUNT_RE = re.compile(r"银行[^\d]{0,15}((?:\d[\s\-]?){11,18}\d)")
# 18 位数字既可能是身份证、也可能是**对公银行账号**。左侧上下文出现这些词时按账号处理
# （实测：标签被银行名规则改写后 "开户行账号：106535801040010703" 只被身份证规则接住 → ID0001）
_ACCOUNT_HINT_RE = re.compile(r"(账号|帐号|账户|开户行账|银行账|卡号|联行)")


def _bank_name_plausible(value: str) -> bool:
    """锚点式捕获到的"银行名"真的像银行名吗？

    踩坑（实测 BK0008/BK0010）：单元格只写"开户行名称"/"开户行账号"这种**标签**时，
    锚点"开户行"后面的"名称"/"账号"被当成银行名登记（掩码 '名*'/'账*'）。
    """
    s = (value or "").strip()
    if len(s) < 2:
        return False
    label_tails = ("名称", "账号", "帐号", "账户", "卡号", "开户行", "开户银行", "开户机构",
                   "银行名称", "开户网点", "联行号", "行号", "备注", "说明")
    if s in label_tails or any(s.endswith(w) and len(s) <= len(w) + 1 for w in label_tails):
        return False
    if any(b in s for b in _KNOWN_BANKS):
        return True
    return bool(re.search(r"(银行|分行|支行|信用社|储蓄所|营业部|分理处|办事处)$", s))
# 联系电话/手机号（PII）：手机 1[3-9]xxxxxxxxx；座机 0xxx-xxxxxxx（含分机前的横线/空格）
PHONE_RE = re.compile(r"(?<!\d)(?:1[3-9]\d{9}|0\d{2,3}[\-\s]?\d{7,8}(?:[\-\s]?\d{1,6})?)(?!\d)")


def _build_mask_token_re() -> re.Pattern:
    """识别"已经是脱敏编号"的串（供 `_already_masked` 用）。

    两类都要认：
      · **带括号**的编号：任意类别 `[TX0001]`、项目名 `[PJ0001]`、本公司 `[本公司·CO0001]`；
      · **裸编号**：明文类别（company/party/date）的替换结果本身就是裸编号，
        如 `甲方：CO0003`。二次脱敏时若不把它当编号，就会被 甲方/乙方 规则
        再包一层（CO0003 → PT0007），同一个真实值出现两个编号。
    前缀一律从 database_serv 取，新增类别自动生效（不手写前缀清单）。
    """
    from database_serv__infra import ENTITY_TABLES, SECRET_CATEGORIES, _CODE_PREFIX

    all_prefixes = sorted({p for p in _CODE_PREFIX.values() if p})
    plain_prefixes = sorted({
        _CODE_PREFIX[c] for c in ENTITY_TABLES
        if c not in SECRET_CATEGORIES and _CODE_PREFIX.get(c)
    })
    parts = []
    if all_prefixes:
        parts.append(r"\[(?:本公司·)?(?:" + "|".join(all_prefixes) + r")\d{2,}\]")
    if plain_prefixes:
        parts.append(r"(?<![A-Za-z])(?:" + "|".join(plain_prefixes) + r")\d{4}(?![0-9])")
    return re.compile("|".join(parts) or r"(?!x)x")


# 已有的脱敏编号（含本公司标记 [本公司·CO0001] 与裸编号 CO0003）
_MASK_TOKEN_RE = _build_mask_token_re()


def _already_masked(value: str) -> bool:
    """值里是否已含脱敏编号：是则调用方应原样保留（防"编号被再包一层编号"）。"""
    return bool(_MASK_TOKEN_RE.search(value or ""))


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
        # 已经是脱敏编号（如"甲方：[本公司·CO0001]"）→ 原样保留，绝不二次编号
        if _already_masked(value):
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
        if _already_masked(value):     # 同上：编号不二次编号
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
        # 证件号是连续数字（正则已保证），直接进缓存，复查①换格式出现时复用同编号。
        # ⚠️ 18 位数字既可能是身份证、也可能是**对公银行账号**：左侧上下文出现账号类词时
        # 按账号处理（实测 "开户行账号：106535801040010703" 被身份证规则接住 → ID0001，
        # 同一串数字同时还登记了 BA0008，属类别错判）。
        left = text[max(0, match.start() - 12): match.start()]
        if _ACCOUNT_HINT_RE.search(left):
            return _desens_account_rep(store, match.group(0), digit_cache)
        return _desens_id_rep(store, match.group(0), digit_cache)

    def repl_bank(match: re.Match) -> str:
        # 与 `_tolerant_digit_mask` 同一道上下文闸门：单据号（PO-/RJ-、编号/单号/票号…）
        # 不是银行卡，别登记成 BC####（实测 "PO-2026…0017" → BC0009、"票据号 2026…" → BC0010）
        left = text[max(0, match.start() - 9): match.start()]
        if re.search(r"[A-Za-z]-?$", left) or _DOC_NO_HINT_RE.search(left):
            return match.group(0)
        return _desens_bank_rep(store, match.group(0), digit_cache)

    def repl_tax(match: re.Match) -> str:
        # 纳税人识别号：见即脱敏（含对公税号），同值同码
        return _desens_tax_rep(store, match.group(0), digit_cache)

    def repl_tax_anchor(match: re.Match) -> str:
        value = match.group(1)
        rep = _desens_tax_rep(store, value, digit_cache)
        return match.group(0)[: match.start(1) - match.start(0)] + rep

    def repl_bank_name(match: re.Match) -> str:
        is_anchor = match.re is BANK_NAME_ANCHOR_RE
        value = (match.group(1) if is_anchor else match.group(0)).strip()
        if not value:
            return match.group(0)
        # 锚点式捕获到的可能只是**标签的尾巴**（单元格写成"开户行名称"/"开户行账号"时，
        # 锚点"开户行"后面剩的"名称"/"账号"被当成银行名登记：实测 BK0008='名*'、
        # BK0010='账*'）。更糟的是标签被改写后，"账号：18位数字"再也匹配不上账号规则，
        # 18 位账号于是被身份证规则吃掉（实测 ID0001）。这里直接**不放行**：
        # 不像银行名 → 原样保留，不登记、不改写。
        if is_anchor and not _bank_name_plausible(value):
            return match.group(0)
        code = store.get_or_create_secret_code("bank_name", value, _mask_bank_name(value))
        name_cache[value] = code          # 复查②可对重复出现的银行名补码
        prefix = match.group(0)[: match.start(1) - match.start(0)] if is_anchor else ""
        return prefix + f"{_mask_bank_name(value)}[{code}]"

    def repl_account(match: re.Match) -> str:
        raw = match.group(1)
        prefix = match.group(0)[: match.start(1) - match.start(0)]
        return prefix + _desens_account_rep(store, raw, digit_cache)

    def repl_phone(match: re.Match) -> str:
        # 联系电话/手机号：保留前 3 后 4，其余打星 + [PH####]（同号同码）
        digits = re.sub(r"[^\d]", "", match.group(0))
        if len(digits) < 7:
            return match.group(0)
        rep = digit_cache.get("phone:" + digits)
        if rep is None:
            masked = (digits[:3] + "*" * max(len(digits) - 7, 3) + digits[-4:]) \
                if len(digits) >= 11 else (digits[:3] + "*" * (len(digits) - 4) + digits[-4:])
            code = store.get_or_create_secret_code("phone", digits, masked)
            rep = f"{masked}[{code}]"
            digit_cache["phone:" + digits] = rep
        return rep

    # ---- 项目名称（最高管理员自定义加密）：必须最先跑 ----
    # 理由：项目名常**包含**公司名/地名（如"腾讯大厦消防维保工程"），若先跑甲方/公司
    # 规则，项目名会被拆散成 "CO0003大厦消防维保工程"，管理员登记的项目名就再也匹配
    # 不上（登记即加密失效）。管理员登记的项目名是**权威口径**，优先于通用规则。
    # 只替换登记过的名称（宁缺勿错：未登记的项目名不做猜测，交由 AI 提案 + 人工审批）。
    text = project_registry__desens.mask_text(text, store, tracker)

    # ---- 本公司（我方主体）：紧随其后、全局命中（不需要"甲方/乙方"锚点）----
    # 管理员登录时确认的公司全称在这里全局替换为带标记编号 [本公司·CO####]：
    # ① 无锚点也能脱敏（原规则只在"甲方：xxx"等结构化位置命中，正文里的本方名称会漏）；
    # ② 编号带"本公司"提示，AI 提示词里也说清含义，避免 AI 把我方当成第三方对手方。
    text = self_entity__desens.mask_text(text, store, tracker)

    # ---- 开户银行必须排在"公司"之前（修复）：银行名几乎都写成"XX银行股份有限公司[分行]"，
    #      先跑公司规则会把它当公司编号吃掉，只剩尾缀（实测 "宁波银行股份有限公司无锡分行"
    #      → "CO0085无锡分行"）。银行名有明确的锚点（开户行/开户银行）与已知行名清单，
    #      语义更specific，先处理。
    text = BANK_NAME_ANCHOR_RE.sub(repl_bank_name, text)
    text = BANK_NAME_RE.sub(repl_bank_name, text)

    # 甲方/乙方必须先处理：否则"甲方：北京xx有限公司"会先被 COMPANY_RE
    # 替换成公司编码，紧接着又被甲方/乙方规则把编码当文本再包一层编号，
    # 变成要解两次密才能还原真实名称。
    text = PARTY_A_RE.sub(repl_party_a, text)
    text = PARTY_B_RE.sub(repl_party_b, text)
    text = COMPANY_RE.sub(repl_company, text)
    text = DATE_RE.sub(repl_date, text)
    # 其余三类：顺序放在身份证/银行卡之前——
    #   ① 纳税人识别号（锚点式先跑，纯数字税号也能命中；再跑无标签的信用代码）
    #   ② 银行账号（锚点式 + "银行"上下文式，含对公账号）
    #   ③ 联系电话/手机号（PII）
    # 这样 18 位统一社会信用代码不会被 ID_CARD_RE 误判为身份证，账号也不被银行卡规则先吃掉。
    text = TAX_ID_ANCHOR_RE.sub(repl_tax_anchor, text)
    text = TAX_ID_RE.sub(repl_tax, text)
    text = ACCOUNT_ANCHOR_RE.sub(repl_account, text)
    text = BANK_CONTEXT_ACCOUNT_RE.sub(repl_account, text)
    text = PHONE_RE.sub(repl_phone, text)
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


def _desens_tax_rep(store: MappingDbStore, raw: str, cache: dict[str, str]) -> str:
    """纳税人识别号 -> "掩码[编号]"（前 4 + 星 + 后 4），同值同码。

    规则：只要识别出纳税人识别号（统一社会信用代码 或 标签后的税号）即脱敏；
    不做校验位硬门槛（电子发票格式固定，漏脱敏的代价远大于误脱敏）。
    """
    digits = re.sub(r"[^0-9A-Za-z]", "", raw or "")
    if not digits:
        return raw
    rep = cache.get("tax:" + digits)
    if rep is not None:
        return rep
    masked = digits[:4] + "*" * max(len(digits) - 8, 4) + digits[-4:]
    code = store.get_or_create_secret_code("tax_id", digits, masked)
    rep = f"{masked}[{code}]"
    cache["tax:" + digits] = rep
    return rep


def _desens_account_rep(store: MappingDbStore, raw: str, cache: dict[str, str]) -> str:
    """银行账号（含对公账号）-> "掩码[编号]"，同值同码。

    去分隔符后按数字串归一化入库（"6222 0001" 与 "62220001" 视为同一账号）；
    不强制 Luhn（对公账号多不满足），只要有"账号/账户/卡号"锚点或"银行"上下文即脱敏。
    """
    digits = re.sub(r"[^0-9]", "", raw or "")
    if not digits:
        return raw
    rep = cache.get("acct:" + digits)
    if rep is not None:
        return rep
    masked = digits[:4] + "*" * max(len(digits) - 8, 4) + digits[-4:]
    code = store.get_or_create_secret_code("bank_account", digits, masked)
    rep = f"{masked}[{code}]"
    cache["acct:" + digits] = rep
    return rep


def _mask_bank_name(name: str) -> str:
    """开户银行名掩码：保留首尾各 2 字，中间打星。"""
    s = (name or "").strip()
    if len(s) <= 4:
        return s[:1] + "*" * max(len(s) - 1, 1)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


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
# 含小数的数字串 = 金额（"3636558.96"、"327981.651376147"），不是证件/卡号；
# 前后紧邻算术运算符同理。
# ⚠️ 本轮修复：小数位早先只认 1~2 位（`\.\d{1,2}`），于是**多位的实数金额**
# （excel 算出来的 327981.651376147）躲过排除、被缝合成 15 位数字 → 当成银行卡脱敏，
# 结果"不含税金额"整列在 hub 里变成 `327981*****6147[BC0001]`，取数直接失败。
# 现在：小数点后只要是数字就算金额（卡号绝不会带小数点）。
_DECIMAL_RE = re.compile(r"\.\d+")
_MATH_NEIGHBOURS = set("×*=+%")
# 单据编号上下文（数字缝合前的左侧小窗口）：命中即不当卡号
_DOC_NO_HINT_RE = re.compile(
    r"(票据号|凭证号|发票号码|票据|编号|单号|订单|票号|号码|发文|代码|券号|No\.?|NO\.?)"
    r"\s*[:：]?\s*[A-Za-z\-]{0,4}$")


def _tolerant_digit_mask(text: str, store: MappingDbStore, cache: dict[str, str]) -> str:
    """复查①：数字缝合补漏（单遍 O(len(text))）。

    标准 ID/卡号正则要求"连续纯数字段"；同一号码一旦被折行、按 4 位分组或
    中间插入空格/连字符就会漏掉，以明文流出。这里先按"允许少量排版分隔符"
    把数字串拼回（len 13~19，18 位按证件处理），再按值脱敏。
    用单次正则扫描实现，不做"每个已脱敏值 × 全文"的逐值查找。

    ⚠️ 修复（误报）：算术表达式/金额会被"缝合"成 13~19 位数字串而被当成卡号
    （实测 "（3636558.96×0.9-8400=789814.72）" 被误判成银行卡并写入映射库）。
    现在两条排除：① 串内含小数（`.dd`）→ 金额；② 前后紧邻 × * = + % → 表达式。
    """
    def _fix(m: re.Match) -> str:
        raw = m.group(0)
        if _DECIMAL_RE.search(raw):
            return raw                      # 含小数：金额/比率，不是证件号/卡号
        before = text[m.start() - 1] if m.start() > 0 else ""
        after = text[m.end()] if m.end() < len(text) else ""
        if before in _MATH_NEIGHBOURS or after in _MATH_NEIGHBOURS:
            return raw                      # 紧邻运算符：表达式，不是号码
        # 单据编号（PO-202606…0017 / RJ-2026.2.11 / 采购订单编号…）不是卡号：
        # 拼接前看左侧上下文——字母、字母+连字符、或"编号/单号/订单/票号"字样即放过。
        left9 = text[max(0, m.start() - 9): m.start()]
        if re.search(r"[A-Za-z]$", left9) or re.search(r"[A-Za-z]-$", left9) \
                or _DOC_NO_HINT_RE.search(left9):
            return raw
        digits = re.sub(r"[^\dXx]", "", raw)  # 拼回去掉分隔符的纯数字串
        if _ID_FULL_RE.fullmatch(digits):
            return _desens_id_rep(store, digits, cache)
        if _BANK_FULL_RE.fullmatch(digits):
            return _desens_bank_rep(store, digits, cache)
        return raw                          # 非敏感长度（过短/过长），原样保留
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
    "事业部", "中心", "集团",
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
# 注：`extract_contract_fields()` 已随 `PARTY_A_RE` 一起搬到轻量模块
#     `contract_rules__desens`（见文件上方的导入），此处不再重复定义。


def start_edge_build_async(doc_key: str, *, current_user: dict | None = None,
                           include_rows: bool = True) -> dict:
    """**动态建图**：把"这份文档建节点 + 建它与存量文档的关系"放进后台单飞队列。

    为什么后台：边判定要调 EDGE_AI_*（推理模型，实测**一个候选约 35 秒**），放在扫描
    线程里会拖慢整条流水线；
    为什么是"每份文档一次"而不是"整批一次"：
      · 入一个建一个，扫完就有图可查，不必等整批跑完（用户的需求）；
      · 单份失败/停止只影响这一份，未扫的文档留在待建队列，下次按旧→新补齐。
    为什么用队列而不是"每份一个线程"：一次拖 18 份文件会同时开 18 条 AI 链
    （撞限流/资源打满），队列保证**同一时刻只有一份在建图**（`edge_build.enqueue_for_build`）。

    agent 模式（未配 EDGE key）下 `build_for_doc` 只把请求追加到
    `logs/graph/graph_edge_requests_incremental.jsonl`，等人工/外部 agent 回填。
    """
    _ = (current_user, include_rows)
    try:
        import edge_build__graph_edges as eb

        return eb.enqueue_for_build(doc_key, reason="scan")
    except Exception as exc:
        return {"queued": False, "error": f"{type(exc).__name__}: {exc}"}


def rescan_hub_classifications(    *,
    dry_run: bool = False,
    stage_inbox: bool = True,
    current_user: dict | None = None,
) -> dict:
    """按**新合同口径**重新判定已有 hub 文档的分类（不重新 OCR、不动源文件）。

    用途：合同规则改成"PDF/Word + 命名/标题含合同/协议"后，**库里已有的旧分类**
    （关键词时代把付款申请/xlsx 汇总表判成"合同"）需要一次批量校正，否则：
      · 仓库里 category 仍是错的；
      · 入账审核窗口看不到这些候选（或看到错的目标台账）。

    做三件事（`dry_run=True` 时只算不改）：
      1. 逐份读 hub JSON → 用 `classify_document`（新规则）重新判类；
      2. 变了就更新 hub JSON 的 `category` 与 `l1_documents.category`；
      3. `stage_inbox=True` 时把合同/发票候选送进待入账队列（合同附正则预填字段）。

    返回：{"changed": n, "items": [{doc_key, old, new, changed, staged}], "errors": [...]}
    """
    from pathlib import Path as _Path

    out: dict = {"changed": 0, "items": [], "errors": [], "dry_run": dry_run,
                 "staged": 0}
    store = None
    if stage_inbox and not dry_run:
        try:
            store = MappingDbStore.load()
        except Exception as exc:
            out["errors"].append(f"脱敏映射加载失败：{type(exc).__name__}: {exc}")

    import ledger_gate__desens
    import ledger_inbox__desens

    src_map: dict[str, str] = {}
    try:
        from database_serv__infra import get_connection

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT doc_key, source_path FROM l1_documents")
            src_map = {str(k): (v or "") for k, v in cur.fetchall()}
    except Exception as exc:
        out["errors"].append(f"读取 l1_documents 失败：{type(exc).__name__}: {exc}")

    for p in sorted(HUB_DIR.rglob("*.json")):
        rel = p.relative_to(HUB_DIR)
        if rel.parts and rel.parts[0] == "_mapping":
            continue
        if p.name.endswith((".l1.json", ".features.json")):
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            out["errors"].append(f"{p.name}: 读取失败 {type(exc).__name__}: {exc}")
            continue
        if doc.get("kind") != "hub_doc":
            continue
        doc_key = str(doc.get("doc_key") or "")
        if not doc_key:
            continue
        pages = [str(x or "") for x in (doc.get("pages") or [])]
        source_file = str(doc.get("source_file") or "")
        old = str(doc.get("category") or "")
        new = classify_document(pages, _Path(source_file) if source_file else None)
        item = {"doc_key": doc_key, "source_file": source_file,
                "old": old, "new": new, "changed": new != old, "staged": None}
        if new != old and not dry_run:
            try:
                doc["category"] = new
                p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as exc:
                out["errors"].append(f"{doc_key}: 写回 hub 失败 {type(exc).__name__}: {exc}")
            try:
                from database_serv__infra import get_connection

                with get_connection() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE l1_documents SET category=%s WHERE doc_key=%s",
                                (new, doc_key))
                    conn.commit()
            except Exception as exc:
                out["errors"].append(f"{doc_key}: 更新 l1_documents 失败 {type(exc).__name__}: {exc}")
        if new != old:
            out["changed"] += 1

        if stage_inbox and not dry_run:
            try:
                gate = ledger_gate__desens.evaluate(
                    pages, file_name=source_file, category=new,
                    tables=list(doc.get("tables") or []))
                fields: dict = {}
                if gate.passed and store is not None:
                    try:
                        fields = extract_contract_fields(
                            _Path(source_file).stem if source_file else p.stem, pages, store)
                    except Exception:
                        fields = {}
                staged = ledger_inbox__desens.stage_from_result(
                    {"doc_key": doc_key, "category": new, "hub_json_path": str(p),
                     "source_path": src_map.get(doc_key, ""),
                     "contract_fields": fields,
                     "ledger_gate__desens": gate.to_dict()},
                    user=current_user)
                item["staged"] = staged.get("kind") if staged.get("ok") else staged.get("msg")
                if staged.get("ok"):
                    out["staged"] += 1
            except Exception as exc:
                out["errors"].append(f"{doc_key}: 送待入账失败 {type(exc).__name__}: {exc}")
        out["items"].append(item)
    return out


def _cell_coord(table: dict, r: int, c: int) -> str | None:
    """从表块 `_cells` 元数据取 Excel 坐标（r=0 为表头；0 基 c → 1 基列号）。"""
    cells = table.get("_cells")
    if not isinstance(cells, dict):
        return None
    try:
        bucket = (cells.get("header") or []) if r == 0 else (cells.get("rows") or [])[r - 1]
        for meta in bucket:
            if isinstance(meta, dict) and meta.get("col") == c + 1:
                return meta.get("coord")
    except Exception:
        return None
    return None


def build_page_texts(
    page_json: dict,
    raw_tables: list[dict],
    masked_tables: list[dict],
    store,
    tracker,
    page_no: int,
) -> tuple[str, str]:
    """组装一页的 (原文文本, 脱敏后文本)，并**在装配过程中**写入单元格级锚点。

    为什么表格块不走文本正则脱敏（重要隐私缺口修复）：
      OCR 页的 `block_content` 里表格是原始 HTML（含未脱敏的真值）。若按老做法
      "整页文本丢进文本脱敏"，公司名这类**需要上下文锚点**的值可能匹配不到，
      于是 hub 页文本里就会残留明文（而 table_desens 已经把表格结构脱敏了，
      两边不一致）。现在表格块一律改用**已字段感知脱敏的单元格**渲染，
      明文既不会进页文本，也不会进 hub JSON。

    坐标好处：单元格的脱敏坐标（char_start/char_end）与原文坐标
    （raw_char_start/raw_char_end）在渲染时**精确算出**，不靠回查字符串猜。
    原文文本 `raw_text` 只在内存里用于构造偏移映射，不落 hub。

    ⚠️ 修复（真实泄露）：**绝不信任上游的 `desensitized` 标记**。以前只要页里有
    `full_text` 就当成"已脱敏"直通，结果 docx 直读层把表格行原样拼进页文本（却标了
    desensitized=True）时，明文就整段落进了 hub。现在：
      · 有 `parsing_res_list`（OCR / 原生 PDF）→ 逐块脱敏 + 表格字段感知渲染；
      · 只有整页 `full_text`（Office 直读）→ **整页再脱敏一遍**（脱敏是幂等的，
        已脱敏文本不会二次编号），并在原文侧保留其原样，供偏移映射/溯源使用。
    """
    res = page_json.get("res") or {}
    if not res.get("parsing_res_list"):
        raw = str(res.get("full_text") or "")
        return raw, desensitize_text(raw, store, tracker)

    by_order: dict[int, tuple[dict, dict]] = {}
    for i, rt in enumerate(raw_tables):
        mk = masked_tables[i] if i < len(masked_tables) else rt
        by_order[(rt.get("anchor") or {}).get("block_order")] = (rt, mk)

    raw_parts: list[str] = []
    fin_parts: list[str] = []
    counters = {"raw": 0, "fin": 0}

    def push(final_text: str, raw_text: str | None = None) -> tuple[int, int]:
        """同时向两侧追加文本；返回 (脱敏侧起点, 原文侧起点)。"""
        rt = final_text if raw_text is None else raw_text
        fs, rs = counters["fin"], counters["raw"]
        fin_parts.append(final_text)
        raw_parts.append(rt)
        counters["fin"] += len(final_text)
        counters["raw"] += len(rt)
        return fs, rs

    def emit_extra_rows(rt: dict, mk: dict, key: str, tag: str) -> None:
        """标题/签章行：**两侧同步渲染**（脱敏侧用掩码、原文侧用真值），偏移才对得上。

        与 office_reader 的渲染顺序保持一致（title → signature → header → rows），
        否则同一份文档走直读和走 OCR 会得到两种页文本。
        """
        rrows = list(rt.get(key) or [])
        mrows = list(mk.get(key) or [])
        for i in range(max(len(rrows), len(mrows))):
            rrow = [str(c or "") for c in (rrows[i] if i < len(rrows) else [])]
            mrow = [str(c or "") for c in (mrows[i] if i < len(mrows) else [])]
            push("\n" + tag + ":")
            opened = False
            for c in range(max(len(rrow), len(mrow))):
                mv = mrow[c] if c < len(mrow) else ""
                if not mv.strip():
                    continue
                rv = rrow[c] if c < len(rrow) else mv
                if opened:
                    push(" | ")
                push(mv, rv)
                opened = True

    def emit_table(rt: dict, mk: dict) -> None:
        emit_extra_rows(rt, mk, "title_rows", "title")
        emit_extra_rows(rt, mk, "signature_rows", "signature")
        raw_hdr = list(rt.get("header") or [])
        mk_hdr = list(mk.get("header") or [])
        # 表头单独起一行并加 `header:` 前缀——否则当上面有 title: 行时，
        # 表头会和标题行**粘在同一行**（实测 `title:主持人：序号 | 投标单位 | …`）
        if any(str(h or "").strip() for h in mk_hdr):
            push("\nheader:")
        hdr_anchors: list[dict] = []
        opened = False
        for c in range(max(len(raw_hdr), len(mk_hdr))):
            mv = str(mk_hdr[c]) if c < len(mk_hdr) else ""
            if not mv.strip():
                continue
            rv = str(raw_hdr[c]) if c < len(raw_hdr) else mv
            if opened:
                push(" | ")
            fs, rs = push(mv, rv)
            hdr_anchors.append({
                "row": 0, "col": c, "coord": _cell_coord(mk, 0, c), "page": page_no,
                "char_start": fs, "char_end": fs + len(mv),
                "raw_char_start": rs, "raw_char_end": rs + len(rv),
                "raw_exact": rv == mv,
            })
            opened = True
        if hdr_anchors:
            mk["header_anchors"] = hdr_anchors

        raw_rows = list(rt.get("rows") or [])
        mk_rows = list(mk.get("rows") or [])
        cell_anchors: list[list[dict | None]] = []
        for r in range(max(len(raw_rows), len(mk_rows))):
            push("\n")
            opened = False
            rrow = raw_rows[r] if r < len(raw_rows) else []
            mrow = mk_rows[r] if r < len(mk_rows) else []
            row_anchors: list[dict | None] = []
            for c in range(max(len(rrow), len(mrow))):
                mv = str(mrow[c]) if c < len(mrow) else ""
                if not mv.strip():
                    row_anchors.append(None)
                    continue
                rv = str(rrow[c]) if c < len(rrow) else mv
                if opened:
                    push(" | ")
                fs, rs = push(mv, rv)
                row_anchors.append({
                    "row": r + 1, "col": c, "coord": _cell_coord(mk, r + 1, c), "page": page_no,
                    "char_start": fs, "char_end": fs + len(mv),
                    "raw_char_start": rs, "raw_char_end": rs + len(rv),
                    "raw_exact": rv == mv,
                })
                opened = True
            cell_anchors.append(row_anchors)
        if cell_anchors:
            mk["cell_anchors"] = cell_anchors

    for order, block in enumerate(res.get("parsing_res_list") or []):
        if not isinstance(block, dict):
            continue
        label = str(block.get("block_label") or "")
        content = str(block.get("block_content") or "")
        if counters["fin"] or counters["raw"]:
            push("\n")
        push(f"{label}:")
        pair = by_order.get(order)
        if "table" in label.lower() and pair is not None:
            emit_table(pair[0], pair[1])
        else:
            push(desensitize_text(content, store, tracker), content)
    return "".join(raw_parts), "".join(fin_parts)


def process_file_to_hub(
    file_path: Path,
    current_user: dict | None = None,
    *,
    source_root: Path | None = None,
    force: bool = False,
) -> dict:
    """单文件流水线（流水线重叠版）。

    source_root：本次输入所属的**源文件夹根**（文件夹拖入时由 scanner_core__scan.expand_inputs
    给出）。有它时 hub 按源文件夹结构落盘（hub/<子树>/<文件名>.json），doc_key 取
    相对路径；没有它（单个文件）时落 hub 根、doc_key 就是文件名 stem（兼容旧数据）。

    force：跳过**内容级去重**强制重扫（默认 False；`.env` 的 RESCAN_FORCE=1 可全局打开）。

    重叠原理：扫描(OCR，GPU 密集)与后处理(脱敏 + 坐标装配，CPU/DB)通过队列在两个
    线程里并行执行——每页 OCR 一完成，立刻把该页交给消费者线程，不等整份文件扫完
    （已确认 PaddleOCR-VL 在本文代码里是按页调用 predict、逐页出结果的）。
    分类因为要看整份文件的抬头/结尾，放在两条流水线都结束后做。
    """
    ensure_hub_dirs()
    # ---- 停止扫描（协作式取消）：整条流水线的检查点都走 scan_control__scan.check ----
    # 语义：在**页边界**停下（当前正在推理的那一页会跑完——模型内部无法中断），
    # 已落盘的页 JSON 保留可复用，**源文件绝不动**；停止后不再进入 AI/概括/事实等步骤。
    import scan_control__scan as scan_ctl

    scan_ctl.check("文件开始处理前")
    # ---- 内容级去重（防重复扫描 + 节点污染）：先算源文件内容指纹，再决定要不要干活 ----
    # 路径级去重只挡"同一路径重复入队"；换个路径的复制件靠这里挡。
    import dedup__desens

    content_hash = dedup__desens.content_key(file_path)
    out_path, doc_key, source_dir = hub_target(file_path, source_root)

    # ---- 副本替换（需求）：文件名带 Windows 副本标记（（2）/（3）/副本…）时，
    #      先判"标记之前的基名是否完全一致"，再比"内容首尾"；两条都成立 →
    #      认定同一份文件的新副本：**删除旧文档的全部痕迹**（旧 doc_no 退役留空），
    #      然后用新文件**把全部工作重新跑一遍**（force=True 绕过"内容未变则跳过"）。
    replaced: dict | None = None
    try:
        import doc_lifecycle__desens

        plan = doc_lifecycle__desens.plan_ingest(file_path)
        if plan.action == "replace" and plan.replace_doc_key:
            rep = doc_lifecycle__desens.delete_document(
                plan.replace_doc_key, user=current_user,
                reason=f"被同名新副本替代：{file_path.name}",
                replaced_by=doc_key,
            )
            replaced = rep.to_dict()
            force = True          # 所有工作重跑（OCR/脱敏/概括/事实/图/向量）
            print(f"[副本替换] {plan.reason}｜{rep.summary()}", flush=True)
    except Exception as exc:
        print(f"[副本替换] 判定失败（不影响入库）：{type(exc).__name__}: {exc}", flush=True)

    decision = dedup__desens.decide(content_hash, doc_key, hub_file=out_path, force=force)

    # ---- 确认是重复文件 → **覆盖**（需求）：同内容、不同路径的重复件默认不再"跳过"，
    #      而是删掉原文档的全部痕迹（hub 正文/概括/哈希/事实/节点/边/向量）并让新文件
    #      完整重跑一遍：覆盖语义，旧 doc_no 退役留空，绝不"两份并存/新增一套"。
    if (not decision.allow) and decision.action == "duplicate_skip" \
            and dedup__desens.policy() == "replace":
        try:
            import doc_lifecycle__desens

            rep = doc_lifecycle__desens.delete_document(
                decision.canonical_doc_key, user=current_user,
                reason=f"重复文件覆盖：{file_path.name}",
                replaced_by=doc_key,
            )
            replaced = rep.to_dict()
            force = True
            print(f"[重复覆盖] 内容与 {decision.canonical_doc_key} 相同 → 删除旧文档并重跑："
                  f"{rep.summary()}", flush=True)
            decision = dedup__desens.decide(content_hash, doc_key, hub_file=out_path, force=True)
        except Exception as exc:
            print(f"[重复覆盖] 失败（按跳过处理）：{type(exc).__name__}: {exc}", flush=True)

    if not decision.allow:
        dedup__desens.record_scan(content_hash, doc_key, source_path=str(file_path),
                          hub_file=str(out_path), skipped=True,
                          duplicate_of=decision.canonical_doc_key)
        return {
            "category": "", "doc_key": doc_key, "hub_json_path": None,
            "source_file": file_path.name, "source_dir": source_dir,
            "cache_json_paths": [], "content_hash": content_hash,
            "skipped": True, "skip_action": decision.action,
            "duplicate_of": decision.canonical_doc_key, "skip_reason": decision.reason,
        }

    store = MappingDbStore.load()
    # 整份文件跨页共用的脱敏追踪器：同一真实值（证件/卡号/公司名）全文只用一个
    # 编号/掩码；复查补漏也能命中"前页已脱敏、后页又换格式出现"的明文。
    tracker = DesensTracker()

    # ---- 流水线缓冲与结果容器（单消费者，天然保持页序）----
    page_queue: queue.Queue = queue.Queue()
    desensitized_pages: list[str] = []      # 消费者线程按页序写入
    cache_paths: list[Path] = []            # 逐页 json 缓存路径（生产端回调记录）
    consumer_errors: list[Exception] = []
    doc_tables: list[dict] = []             # 全文档表块（含单元格锚点 + 原文坐标）
    offset_maps: list[list[list[int]]] = [] # 每页"脱敏后→原文"映射（相等片段）

    def on_page_json(page_path: Path) -> None:
        """生产端回调（运行在扫描线程）：每页 JSON 落盘即解析表块并入队。"""
        cache_paths.append(page_path)
        try:
            page_json = json.loads(page_path.read_text(encoding="utf-8"))
        except Exception:
            page_json = {}
        # 表格结构：Office 直读产物优先；否则解析 OCR 的 table 块（带页码/bbox 锚点）
        raw_tables: list[dict] = []
        masked_tables: list[dict] = []
        try:
            from ocr_tables__desens import collect_tables_from_page_json

            for tbl in collect_tables_from_page_json(page_json):
                raw_tables.append(tbl)
                # 隐私：OCR 表格来自**未脱敏**的原始页 JSON（页文本才走过脱敏），
                # 因此这里对"尚无脱敏统计"的表块补做字段感知脱敏（列头语义 + 值格式校验）。
                if "desens_stats" in tbl:
                    masked_tables.append(tbl)
                    continue
                try:
                    import table_desens__desens as _td

                    masked_tables.append(_td.mask_prebuilt(tbl, store))
                except Exception as exc:
                    # 失败**闭合**：宁可用占位符也不让明文流进页文本/hub
                    import table_desens__desens as _td

                    masked_tables.append(_td.fail_closed_table(tbl, exc))
        except Exception:
            pass
        page_queue.put((page_json, raw_tables, masked_tables))

    def consumer() -> None:
        """消费端线程：装配"脱敏页文本 + 单元格锚点 + 脱敏↔原文偏移映射"。

        注意：Office 直读页（xlsx/docx）在 office_reader 里已做字段感知脱敏，
        build_page_texts 走 full_text 直通，**不会二次脱敏**。
        """
        try:
            while True:
                item = page_queue.get()
                if item is None:  # 结束哨兵
                    return
                scan_ctl.check("脱敏装配（页）")      # 停止扫描检查点
                page_json, raw_tables, masked_tables = item
                idx = len(desensitized_pages)
                page_no = idx + 1
                raw_text, final_text = build_page_texts(
                    page_json, raw_tables, masked_tables, store, tracker, page_no
                )
                # ① 脱敏↔原文偏移映射（相等片段；被替换的掩码/编号区段不在此列）
                try:
                    import offset_map__desens as _om

                    segs = _om.build_map(raw_text, final_text)
                except Exception:
                    segs = []
                # ② 兜底补锚点：docx 表块等没有坐标来源的表，用页文本回查补 char 区间
                try:
                    import ocr_tables__desens as _ot

                    _ot.attach_page_anchors(masked_tables, final_text, page_no)
                    _ot.add_raw_spans(masked_tables, segs, len(raw_text))
                except Exception:
                    pass
                # ③ 隐私：表块里不得残留原文行（OCR 的 raw_lines 是未脱敏 HTML）
                for t in masked_tables:
                    t.pop("raw_lines", None)
                doc_tables.extend(masked_tables)
                offset_maps.append(segs)
                desensitized_pages.append(final_text)
        except Exception as exc:
            consumer_errors.append(exc)

    consumer_thread = threading.Thread(target=consumer, daemon=True, name="hub-desensitize")
    consumer_thread.start()
    try:
        # 生产端：逐页扫描（每页 OCR 完立刻回调，不等后续页）
        # 传入已算好的 content_hash：缓存目录名带上内容指纹，避免"不同文件夹里的同名
        # 文件共用同一缓存目录、互相覆盖 page_*.json"。
        process_file(file_path, CACHE_DIR, on_page_json=on_page_json, content_hash=content_hash)
    finally:
        page_queue.put(None)  # 通知消费者收尾（即使扫描异常也要出队）
        consumer_thread.join(timeout=120)
    if consumer_errors:
        raise consumer_errors[0]
    scan_ctl.check("扫描完成、落盘前")     # 停止扫描检查点（此时还没写 hub）

    # 2. 按整份文件分类（不是按页，需要抬头/结尾）——分类只作为**记录**，
    #    不再用于建文件夹（清除"按分类建 hub 子文件夹"的旧范式）。
    category = classify_document(desensitized_pages, file_path)

    # 3. 落盘：一份源文件 -> 一份合并 JSON，**保留源文件夹结构**：
    #    文件夹输入 → hub/<源文件夹子树>/<文件名>.json（不在 hub 根把文件夹拆成散文件）；
    #    单文件输入 → hub/<文件名>.json。
    #    仍然 **不按分类** 建目录（旧范式已清除）：分类只记录在 JSON 的 category 字段
    #    与数据库记录里，避免"同一文件在多个分类目录各一份"的幂等性问题。
    out_path, doc_key, source_dir = hub_target(file_path, source_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                # 类型标识：L1 只处理带此标识的 hub 产出，
                # 避免把 ai_parser 的 <stem>.features.json 等当作文档吞掉
                "kind": "hub_doc",
                # doc_key：L1/检索/事实清单的稳定主键。文件夹输入时含源文件夹相对路径
                # （如 南苑新村/合同/KH-YF-2026-02），因此不同子目录里的同名文件不会撞键。
                "doc_key": doc_key,
                "source_file": file_path.name,
                "source_dir": source_dir,          # 源文件夹相对路径（POSIX，""=根）
                "rel_path": (f"{source_dir}/{file_path.name}" if source_dir else file_path.name),
                "category": category,
                "page_count": len(desensitized_pages),
                "pages": desensitized_pages,
                # 表格结构（xlsx/docx 直读产出；已字段感知脱敏），供 L1/建图使用。
                # 每个单元格/表头带 cell_anchor（page/row/col/coord/char_start/char_end）
                # 与 raw_span（★脱敏后页文本坐标 -> 原文页文本坐标★）。
                "tables": doc_tables,
                # 脱敏↔原文偏移映射：offset_maps[页索引] = [[final_start, raw_start, length], ...]
                # 只记录"逐字相同"的片段；掩码/编号（如 [TX0001]）区段不在此列，用 map_span
                # 可把脱敏文本里的任意区间映射回原文行/列（回原文核对的关键，L1 事实溯源用）。
                "offset_maps": offset_maps,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    result = {
        "category": category,
        "hub_json_path": out_path,
        "doc_key": doc_key,
        "source_dir": source_dir,
        "cache_json_paths": cache_paths,
    }
    if replaced:
        result["replaced_document"] = replaced      # 供 UI 展示"新替旧"细节
    ai_active = os.getenv("AI_ENABLED", "0") == "1" and bool(os.getenv("AI_API_KEY", ""))
    # 台账写入职责划分（历史沿革 + 本轮变更，改前请读完）：
    # - **本轮**：不论 AI 开或关，流水线都**不写任何台账**。它只做两件事：
    #     ① 由闸门回答"这是不是合同"（contract_rules：PDF/Word + 命名/标题含合同/协议）；
    #     ② 是合同时产出正则版 contract_fields 作**预填值**，随结果进"待入账队列"。
    #   真正写库发生在用户于「入账审核」窗口点"入账"时（ledger_inbox__desens.approve）。
    # - AI 启用时预填值优先取 AI 报告里的台账字段（ai_parser 同样不再写库）。
    import ledger_gate__desens

    scan_ctl.check("写台账/进入 AI 步骤前")   # 停止扫描检查点（省 token/算力）
    gate = ledger_gate__desens.evaluate(
        desensitized_pages, file_name=file_path.name, category=category,
        tables=doc_tables,
    )
    result["ledger_gate__desens"] = gate.to_dict()
    ledger_gate__desens.log_decision(file_path.stem, gate,
                             extra={"hub_json": out_path.name, "ai_active": ai_active})
    # 入账口径（本轮）：**流水线不写任何台账**。闸门只回答"这是不是合同"；
    # 是合同才产出正则版台账字段，供后面的"待入账队列"预填；真正入账由用户在
    # 「入账审核」窗口决定（ledger_inbox__desens.approve）。
    if gate.passed:
        result["is_contract"] = True
        if category == "合同" and not ai_active:
            result["contract_fields"] = extract_contract_fields(file_path.stem,
                                                                desensitized_pages, store)
    else:
        result["ledger_skipped"] = "；".join(gate.reasons)

    # 5. AI 协作（可选）：特征哈希归档 + 合同台账字段**预填** + 分块向量化。
    #    开关：.env 中 AI_ENABLED=1 且已配置 AI_API_KEY；任何一步失败都不阻断主流程，
    #    结果/错误放入 result["ai_report"] / result["ai_error"] 供 UI 展示。
    #    ⚠️ AI 也只产出字段、不写台账（入账由用户审核）。
    if ai_active:
        try:
            from ai_parser__ai import process_hub_file

            result["ai_report"] = process_hub_file(out_path, current_user)
        except Exception as exc:
            result["ai_error"] = f"{type(exc).__name__}: {exc}"

    # 6. L1 投影（哈希 + 分段 + 概括 + hub 资产索引 + 概括/哈希伴生文件）。
    #    这是"概括/哈希"的落库入口：hub JSON 本体只放脱敏正文/表格/坐标，
    #    概括与哈希进 l1_documents / l1_segments / hub_index 三张表，并写
    #    hub/<stem>.l1.json 伴生文件（随 hub 存放，L2/L3 可直接读）。
    #    AI 关闭时不调用 AI（summarize=False），但仍记录哈希与结构统计——
    #    哈希链路不依赖 AI，必须始终可用。失败不阻断主流程。
    try:
        from l1_extract__summary_hash import project_hub_file

        scan_ctl.check("L1 概括/哈希前")      # 停止扫描检查点
        result["l1_report"] = project_hub_file(
            out_path, source_path=str(file_path), summarize=ai_active,
            force=force,          # 强制重扫时也重新概括；否则 hub 未变则复用已有概括（省 AI）
        )
        result["hub_hash"] = result["l1_report"]["hub_hash"]
    except Exception as exc:
        result["l1_error"] = f"{type(exc).__name__}: {exc}"

    # 内容指纹登记：记录"这份内容 → 这个 doc_key + 这个路径"（重复内容才能被后续跳过）
    try:
        dedup__desens.record_scan(content_hash, doc_key, source_path=str(file_path),
                          hub_file=str(out_path))
        result["content_hash"] = content_hash
    except Exception as exc:
        result["dedup_error"] = f"{type(exc).__name__}: {exc}"

    # 7. **待入账队列**（本轮核心变更）：识别只"提名"，不写台账。
    #    合同（PDF/Word + 命名/标题含合同/协议）与发票连同**已脱敏**的预填字段入队，
    #    由用户在「入账审核」窗口逐条决定入不入账。失败不阻断主流程。
    try:
        import ledger_inbox__desens

        staged = ledger_inbox__desens.stage_from_result(result, user=current_user)
        result["ledger_inbox"] = staged
    except Exception as exc:
        result["ledger_inbox_error"] = f"{type(exc).__name__}: {exc}"

    # 8. **动态建图**（需求）：这份文档的事实刚写好，就立刻建它的节点与关系
    #    （增量：只做"本文档 ↔ 存量文档"，不碰别人的节点/边；幂等：指纹没变就跳过）。
    #    · 放**后台线程**：边判定要走 EDGE_AI_* 网络调用，不能拖慢扫描；
    #    · 停止扫描不影响它：它只处理"已经跑完的这份文档"，停止后未扫的文档会留在
    #      "待建图队列"里，由下次建图/补齐时按旧→新补上；
    #    · agent 模式（未配 EDGE key）只写请求文件，不阻塞。
    try:
        start_edge_build_async(doc_key, current_user=current_user)
        result["edge_build"] = "已排入后台建图（增量）"
    except Exception as exc:
        result["edge_build_error"] = f"{type(exc).__name__}: {exc}"

    return result
