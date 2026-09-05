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


def desensitize_text(text: str, store: MappingDbStore) -> str:
    """注意匹配顺序：先处理 18 位身份证号，再处理 13-19 位银行卡号，
    避免银行卡的正则把身份证号也吃掉（替换成编码后就不再是纯数字，
    后面的规则自然不会再命中）。"""

    def repl_company(match: re.Match) -> str:
        return store.get_or_create_code("company", match.group(0))

    def _party_category(value: str) -> str:
        # 甲方/乙方后面如果本身是公司名称，归到 company 类别，跟文中其它地方
        # 出现的同一家公司共用一条映射；不是公司名称（比如个人姓名）才归 party。
        return "company" if COMPANY_RE.search(value) else "party"

    def repl_party_a(match: re.Match) -> str:
        # 取值顺序：组1(有冒号+下划线) -> 组2(有冒号+常规文本) -> 组3(无冒号+下划线)
        value = match.group(1) or match.group(2) or match.group(3)
        if not value:
            return match.group(0)
        value = value.strip()
        code = store.get_or_create_code(_party_category(value), value)
        return f"甲方：{code}"

    def repl_party_b(match: re.Match) -> str:
        # 取值顺序：组1(有冒号+下划线) -> 组2(有冒号+常规文本) -> 组3(无冒号+下划线)
        # 注意：PARTY_B_RE 是独立编译的正则，组号同样从 1 开始
        value = match.group(1) or match.group(2) or match.group(3)
        if not value:
            return match.group(0)
        value = value.strip()
        code = store.get_or_create_code(_party_category(value), value)
        return f"乙方：{code}"

    def repl_date(match: re.Match) -> str:
        return store.get_or_create_code("date", match.group(0))

    def repl_id(match: re.Match) -> str:
        raw = match.group(0)
        masked = raw[:6] + "******" + raw[-4:]
        code = store.get_or_create_secret_code("id_card", raw, masked)
        return f"{masked}[{code}]"

    def repl_bank(match: re.Match) -> str:
        raw = match.group(0)
        masked = raw[:6] + "*" * max(len(raw) - 10, 4) + raw[-4:]
        code = store.get_or_create_secret_code("bank_card", raw, masked)
        return f"{masked}[{code}]"

    # 甲方/乙方必须先处理：否则"甲方：北京xx有限公司"会先被 COMPANY_RE
    # 替换成公司编码，紧接着又被甲方/乙方规则把编码当文本再包一层编号，
    # 变成要解两次密才能还原真实名称。
    text = PARTY_A_RE.sub(repl_party_a, text)
    text = PARTY_B_RE.sub(repl_party_b, text)
    text = COMPANY_RE.sub(repl_company, text)
    text = DATE_RE.sub(repl_date, text)
    text = ID_CARD_RE.sub(repl_id, text)
    text = BANK_CARD_RE.sub(repl_bank, text)
    return text


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
                desensitized_pages.append(desensitize_text(text, store))
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
