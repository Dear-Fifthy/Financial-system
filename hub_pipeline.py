"""按文件驱动的分类 + 脱敏流水线。

设计目标（对应这次讨论）：
1. 分类和脱敏跟"单个文件"的扫描绑在一起——调用方（比如 table.py 的 ScanWorker）
   每处理完一个文件，就可以立刻调用一次 process_file_to_hub()，不需要等
   队列里其它文件也扫描完。
2. JSON 缓存（scanner_core.process_file 产出的逐页 json）只是内部核验用的中间产物，
   不面向前端展示；真正"看得见"的产物是本文件写到 hub/<分类>/ 下面的合并 JSON，
   一份源文件对应一份，按文件分，不是按页分。
3. 权限、加密都先做成"占位但可替换"的接口：check_permission() 现在无条件放行，
   加密密钥现在存本地文件，后面接真正的密钥管理/权限系统时，只改这两处实现，
   不用动调用方代码。

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
import re
from pathlib import Path

from cryptography.fernet import Fernet

from scanner_core import BASE_DIR, OUTPUT_DIR as CACHE_DIR, process_file

# =========================================================
# 目录 / 文件常量
# =========================================================
HUB_DIR = BASE_DIR / "hub"
MAPPING_FILE = HUB_DIR / "_mapping" / "entity_mapping.json"
KEY_FILE = BASE_DIR / "hub_encryption.key"  # ⚠️ 开发阶段占位，别提交进代码仓库


def ensure_hub_dirs() -> None:
    HUB_DIR.mkdir(exist_ok=True)
    MAPPING_FILE.parent.mkdir(parents=True, exist_ok=True)


# =========================================================
# 权限占位接口
# =========================================================
def check_permission(user: dict | None, action: str) -> bool:
    """权限检查占位接口，现在无条件放行（对应"初始版做成全权限"）。

    以后接权限系统（比如 Casbin/RBAC）时，只改这一个函数的实现即可。
    """
    return True


# =========================================================
# 加解密（身份证/银行卡等需要严格双向控制的字段）
# =========================================================
def _load_or_create_key() -> bytes:
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes()
    key = Fernet.generate_key()
    KEY_FILE.write_bytes(key)
    print(
        f"⚠️ 首次运行，已在本地生成加密密钥：{KEY_FILE}\n"
        f"   这只是开发阶段的占位方案，上线前务必换成从密钥管理服务/环境变量读取，"
        f"并把 {KEY_FILE.name} 加进 .gitignore，不要和数据库放在一起。"
    )
    return key


_FERNET = Fernet(_load_or_create_key())


def encrypt_value(value: str) -> str:
    return _FERNET.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_value(cipher_text: str) -> str:
    return _FERNET.decrypt(cipher_text.encode("ascii")).decode("utf-8")


# =========================================================
# 映射表：编号 <-> 真实值
# =========================================================
def _normalize(value: str) -> str:
    """去空白，作为去重比对的 key（严格精确匹配，不做模糊合并——
    避免把两个不同实体误判成同一个，模糊相似的情况建议走人工确认队列，
    这里先不自动处理）。"""
    return "".join(value.split())


_CODE_PREFIX = {
    "company": "CO",
    "party": "PT",
    "date": "DT",
    "id_card": "ID",
    "bank_card": "BC",
}
_CATEGORY_BY_PREFIX = {prefix: category for category, prefix in _CODE_PREFIX.items()}


class MappingStore:
    """本地 JSON 版映射表。company/party/date 存明文真实值(可逆映射)，
    id_card/bank_card 只存密文，不落明文。

    这是一个可替换的存储层——以后要换成数据库分表存储时，
    只需要重写这个类的 load/save/get_or_create_*，process_file_to_hub()
    等调用方不用改。
    """

    def __init__(self, data: dict) -> None:
        self._data = data

    @classmethod
    def load(cls) -> "MappingStore":
        ensure_hub_dirs()
        if MAPPING_FILE.exists():
            data = json.loads(MAPPING_FILE.read_text(encoding="utf-8"))
        else:
            data = {}
        return cls(data)

    def save(self) -> None:
        MAPPING_FILE.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get_or_create_code(self, category: str, real_value: str) -> str:
        """用于company/party/date这类不需要加密、但要可逆还原的字段。"""
        bucket = self._data.setdefault(category, {})
        key = _normalize(real_value)
        if key in bucket:
            return bucket[key]["code"]
        code = f"{_CODE_PREFIX.get(category, 'EN')}{len(bucket) + 1:04d}"
        bucket[key] = {"code": code, "real_value": real_value}
        return code

    def get_or_create_secret_code(
        self, category: str, real_value: str, masked_display: str
    ) -> str:
        """用于身份证/银行卡这类必须加密存储的字段，明文不落盘。"""
        bucket = self._data.setdefault(category, {})
        key = _normalize(real_value)
        if key in bucket:
            return bucket[key]["code"]
        code = f"{_CODE_PREFIX.get(category, 'EN')}{len(bucket) + 1:04d}"
        bucket[key] = {
            "code": code,
            "masked": masked_display,
            "cipher": encrypt_value(real_value),
        }
        return code

    def lookup_real_value(self, code: str) -> str | None:
        """按编码反查真实值，仅用于 company/party/date 这类非加密字段
        （id_card/bank_card 走 decrypt()，必须经过权限检查）。"""
        category = _CATEGORY_BY_PREFIX.get(code[:2])
        if category is None:
            return None
        for entry in self._data.get(category, {}).values():
            if entry["code"] == code:
                return entry.get("real_value")
        return None

    def decrypt(self, category: str, code: str, requesting_user: dict | None) -> str:
        if not check_permission(requesting_user, "decrypt_sensitive"):
            raise PermissionError("当前用户没有解密权限")
        for entry in self._data.get(category, {}).values():
            if entry["code"] == code:
                return decrypt_value(entry["cipher"])
        raise KeyError(f"未找到编码：{code}")


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

# 优化后的甲乙方匹配逻辑：支持下划线格式 $ \underline{\text{xxx}} $ 以及 甲方（xx）: xxx 格式
# 适配精准下划线格式 $ \underline{\text{内容}} $ 以及常规 甲方：xxx / 甲方（xx）: xxx 格式
PARTY_A_RE = re.compile(
    r"甲方(?:\uff08[^\uff09]+\uff09|\([^\)]+\))?[:\uff1a]\s*"
    r"(?:\$\s*\\underline\{\\text\{([^}]+)\}\}\s*\$|([^\n\uff0c,。\uff1b;]{2,60}))"
)

PARTY_B_RE = re.compile(
    r"乙方(?:\uff08[^\uff09]+\uff09|\([^\)]+\))?[:\uff1a]\s*"
    r"(?:\$\s*\\underline\{\\text\{([^}]+)\}\}\s*\$|([^\n\uff0c,。\uff1b;]{2,60}))"
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


def desensitize_text(text: str, store: MappingStore) -> str:
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
        # 优先提取组1($ \underline{\text{xxx}} $内的纯文本)，若无则提取组2(常规文本)
        value = match.group(1) or match.group(2)
        if not value:
            return match.group(0)
        value = value.strip()
        code = store.get_or_create_code(_party_category(value), value)
        return f"甲方：{code}"

    def repl_party_b(match: re.Match) -> str:
        # 优先提取组1($ \underline{\text{xxx}} $内的纯文本)，若无则提取组2(常规文本)
        value = match.group(1) or match.group(2)
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
def extract_contract_fields(file_stem: str, desensitized_pages: list[str], store: MappingStore) -> dict:
    """从脱敏后的文本里，把能可靠拿到的台账字段先填上。

    ⚠️ 目前只有"甲方"能可靠识别（复用 PARTY_A_RE 在脱敏文本里找编码再反查
    真实名称）。"合同编号"和"合同金额"格式五花八门，正则不靠谱，这里先用
    占位值垫上保证能入库，等第5步接上外部 AI API 解析之后，应该用解析出
    的真实值覆盖这两个字段（可以直接调 database_serv.api_save_contract_from_ai
    再写一次，靠"合同编号"做 ON CONFLICT 更新）。
    """
    combined = "\n".join(desensitized_pages)

    party_a_real = None
    match_a = PARTY_A_RE.search(combined)
    if match_a:
        party_a_real = store.lookup_real_value(match_a.group(1))

    return {
        "contract_code": f"PENDING-{file_stem}",  # 占位编号，等AI解析后应替换成真实合同编号
        "contract_term": None,  # 留给AI解析步骤补全
        "party_a": party_a_real or "待人工核对",
        "income": 0.0,  # 留给AI解析步骤补全
        "is_paid": False,
    }


def process_file_to_hub(file_path: Path, current_user: dict | None = None) -> dict:
    """单文件流水线：OCR/原生提取(写缓存) -> 按文件分类 -> 脱敏 -> 落盘到 hub。

    刻意做成"来一份处理一份"：调用方处理完一个文件就立刻调用一次，
    不等待队列里其它文件也扫描完。
    """
    ensure_hub_dirs()

    # 1. 复用现有 OCR/原生提取逻辑，产出分页 json 缓存（仅供内部核验）
    cache_paths = process_file(file_path, CACHE_DIR)

    pages_text: list[str] = []
    for page_path in cache_paths:
        try:
            page_json = json.loads(page_path.read_text(encoding="utf-8"))
        except Exception:
            page_json = {}
        pages_text.append(extract_text_from_page_json(page_json))

    # 2. 按整份文件分类（不是按页）
    category = classify_document(pages_text, file_path)

    # 3. 脱敏（映射表读写围绕这一份文件展开，处理完立即 save，
    #    不用等其它文件）
    store = MappingStore.load()
    desensitized_pages = [desensitize_text(text, store) for text in pages_text]
    store.save()

    # 4. 落盘：一份源文件 -> 一份合并 json，按分类归到不同文件夹
    category_dir = HUB_DIR / category
    category_dir.mkdir(parents=True, exist_ok=True)
    out_path = category_dir / f"{file_path.stem}.json"
    out_path.write_text(
        json.dumps(
            {
                "source_file": file_path.name,
                "category": category,
                "page_count": len(pages_text),
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
    if category == "合同":
        result["contract_fields"] = extract_contract_fields(file_path.stem, desensitized_pages, store)
    return result