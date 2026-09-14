"""表格字段感知脱敏：按**列头语义**决定该列怎么脱敏，并按**值格式**二次校验。

规则（对应需求）：
  1. 遍历表块字段：列头命中语义字典（甲方/乙方、员工姓名、身份证、开户行、
     银行账号、纳税人识别号…）→ 该列按对应类别脱敏；"之前设置过的敏感信息"都覆盖
     （company/party/id_card/bank_card/bank_account/bank_name/tax_id/date）。
  2. **值格式二次校验**：列头说"银行账号"，值却不是 12~19 位数字 → 不脱敏（跳过并计数），
     避免把日期/编号误当账号；反之列头不认识时不做猜测（宁缺勿错）。
  3. 脱敏一律走既有映射库：明文类别（company/party/date）→ 编号；敏感类别
     （id_card/bank_card/bank_account/bank_name/tax_id）→ sha256 指纹 + Fernet 密文，
     **同值同码**（重复值哈希相同 → 复用同一编号）。
  4. 只改单元格文本，保持表结构（表头/行数/合并区域）不变。
"""
from __future__ import annotations

import re

# 列头语义 -> 类别（顺序敏感：先匹配更具体的）
HEADER_RULES: list[tuple[tuple[str, ...], str]] = [
    (("纳税人识别号", "纳税识别号", "税号", "统一社会信用代码"), "tax_id"),
    (("身份证", "证件号", "证件号码"), "id_card"),
    (("开户行", "开户银行", "银行名称"), "bank_name"),
    (("银行账号", "对公账号", "账号", "帐号", "账户", "卡号", "银行卡"), "bank_account"),
    (("联系电话", "手机号", "手机号码", "联系方式", "电话"), "phone"),
    (("员工", "姓名", "经办人", "经办", "负责人", "联系人", "法人", "法定代表人",
      "申请人", "签收人", "签发人", "审核人", "批准人", "制表人", "复核人", "经办人员",
      # 签名/签字列：会议签到表、谈判记录表里的"签名"栏就是人名，不能因为列头
      # 不叫"姓名"就漏脱敏（实测 OCR 页 `签名|李玉丹` 整列留了明文）
      "签名", "签字", "签署人"), "party"),
    (("甲方", "乙方", "单位全称", "单位名称", "收款单位", "付款单位", "开票单位",
      "供货单位", "购货单位", "公司", "客户", "供应商", "购方", "销方", "抬头"), "company"),
    (("日期", "签订日", "开票日", "付款日", "到期日"), "date"),
]
# ⚠️ 歧义列头（本轮修复，实测事故）：
#   `单位` 在中文里既是"单位名称"又是**计量单位**。旧规则把裸"单位"也算公司标签，
#   于是苗木清单里"单位"列的 `株/棵/个` 整列被当公司脱敏 → `entity_mapping_company`
#   里出现了 `CO0113 = 株`；同理"甲方（公章）："这种**签章行标签**被当列头，
#   其下的日期栏（`20 年 月 日`）也被登记成 CO0112。
#   现在：裸"单位/公司/名称"等**不再单独命中**，必须带限定词（单位全称/单位名称/…）。
_AMBIGUOUS_HEADERS = ("单位", "公司", "名称", "抬头", "编号", "金额", "备注")
# 定表头时最多往下看几行（找"最宽且全是字段名"的那一行当表头）
_HEADER_SCAN = 4

_RE_ID = re.compile(r"^\d{17}[\dXx]$")
_RE_ACCT = re.compile(r"^\d{12,19}$")
_RE_TAX = re.compile(r"^[0-9A-Z]{15,20}$")
_RE_DATE = re.compile(r"^\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?$")
_RE_HAS_CJK = re.compile(r"[\u4e00-\u9fa5]")
FAIL_CLOSED = "【脱敏失败·已屏蔽】"
_CODE_TOKEN_RE = re.compile(r"\[(?:本公司·)?[A-Z]{2}\d{4}\]|(?<![A-Za-z])[A-Z]{2}\d{4}(?![0-9])")
_TRAILING_LABEL_PUNCT = ("：", ":", "＝", "=")
# 值长度上限（超出即视为"不是这个类别的值"→跳过）：
# 防"整段正文被当成公司名/人名"（曾导致 entity_mapping norm_key 超长报错）
_MAX_LEN = {"company": 40, "party": 20, "bank_name": 60, "date": 12}


def match_category(header: str) -> str | None:
    """列头 -> 脱敏类别；不认识返回 None（不做猜测）。

    歧义列头保护：**裸**"单位/公司/名称/编号/金额/备注/日期"这类词**不判类别**——
    它们在不同表里含义完全不同（"单位"可能是计量单位列，"名称"可能是品种名）。
    必须带限定词（单位全称/单位名称/合同编号/开票日期…）才认。
    """
    h = (header or "").strip().rstrip("：:=＝ ")
    if not h:
        return None
    if h in _AMBIGUOUS_HEADERS:
        return None
    # 行内标签样（如"甲方（公章）："）：把括号内容剥掉后再比，仍以限定词为准
    core = re.sub(r"[（(][^）)]*[）)]", "", h).strip().rstrip("：:=＝ ")
    if core in _AMBIGUOUS_HEADERS:
        return None
    for keys, category in HEADER_RULES:
        if any(k in core for k in keys):
            return category
    # 复合实体标签（"申请单位/建设单位/中标公司/服务企业"）：**带限定词**的
    # `X单位 / X公司 / X企业 / X集团`（长度 ≥3）算公司标签；
    # 裸"单位/公司/企业"已在上面被歧义表拦掉，所以这里不会把计量单位当公司。
    if len(core) >= 3 and re.search(r"(单位|公司|企业|集团)$", core):
        return "company"
    return None


def value_valid(raw: str, category: str) -> bool:
    """值格式校验：不匹配则该单元格跳过（防误脱敏）。"""
    s = (raw or "").strip()
    if not s:
        return False
    limit = _MAX_LEN.get(category)
    if limit and len(s) > limit:
        return False          # 过长：不是这个类别的值（防把整段正文当公司名入库）
    if category in ("id_card", "bank_card"):
        return bool(_RE_ID.match(s))
    if category == "bank_account":
        return bool(_RE_ACCT.match(re.sub(r"[\s\-]", "", s)))
    if category == "tax_id":
        return bool(_RE_TAX.match(s.replace(" ", "")))
    if category == "date":
        return bool(_RE_DATE.match(s))
    if category == "bank_name":
        # 银行名：含中文且带"银行"或≥4 字（避免把"无"之类当真）
        return bool(_RE_HAS_CJK.search(s)) and ("银行" in s or len(s) >= 4)
    if category == "phone":
        digits = re.sub(r"[^\d]", "", s)
        return bool(re.fullmatch(r"(?:1[3-9]\d{9}|0\d{9,11})", digits))
    # company / party：含中文、且不像日期/纯长数字（防把日期塞进甲方列时误脱）
    if _RE_DATE.match(s) or re.fullmatch(r"\d{12,}", re.sub(r"[\s\-]", "", s)):
        return False
    return bool(_RE_HAS_CJK.search(s))


def _mask_middle(digits: str, keep_head: int = 4, keep_tail: int = 4) -> str:
    if len(digits) <= keep_head + keep_tail:
        return "*" * len(digits)
    return digits[:keep_head] + "*" * (len(digits) - keep_head - keep_tail) + digits[-keep_tail:]


def _mask_name(name: str) -> str:
    s = (name or "").strip()
    if len(s) <= 2:
        return s[:1] + "*"
    if len(s) <= 4:
        return s[:1] + "*" * (len(s) - 2) + s[-1:]
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def desensitize_value(store, raw: str, category: str) -> str:
    """单个单元格脱敏（调用方需先用 value_valid 校验）。"""
    s = (raw or "").strip()
    if category == "id_card":
        digits = re.sub(r"[^\dXx]", "", s)
        masked = digits[:6] + "******" + digits[-4:]
        return f"{masked}[{store.get_or_create_secret_code('id_card', digits, masked)}]"
    if category == "bank_card":
        digits = re.sub(r"[^\d]", "", s)
        masked = _mask_middle(digits, 6, 4)
        return f"{masked}[{store.get_or_create_secret_code('bank_card', digits, masked)}]"
    if category == "bank_account":
        digits = re.sub(r"[^\d]", "", s)
        masked = _mask_middle(digits)
        return f"{masked}[{store.get_or_create_secret_code('bank_account', digits, masked)}]"
    if category == "tax_id":
        v = s.replace(" ", "")
        masked = _mask_middle(v)
        return f"{masked}[{store.get_or_create_secret_code('tax_id', v, masked)}]"
    if category == "bank_name":
        masked = _mask_name(s)
        return f"{masked}[{store.get_or_create_secret_code('bank_name', s, masked)}]"
    if category == "phone":
        digits = re.sub(r"[^\d]", "", s)
        masked = digits[:3] + "*" * max(len(digits) - 7, 3) + digits[-4:]
        return f"{masked}[{store.get_or_create_secret_code('phone', digits, masked)}]"
    # 明文类别：只给编号（与文本脱敏一致）；**本公司**用带标记的编号，与文本路径一致
    if category == "company":
        try:
            import self_entity__desens as self_entity

            for e in self_entity__desens.load_index():
                if e["name"] == s:
                    return e["display"]        # 形如 [本公司·CO0002]
        except Exception:
            pass
    code = store.get_or_create_code(category, s)
    return code


def _strip_meta(meta) -> dict:
    """单元格元数据去掉原文文本（只留 row/col/coord）——hub 不落明文。"""
    if not isinstance(meta, dict):
        return {}
    return {k: v for k, v in meta.items() if k != "text"}


def _scrub_cells(out: dict, table: dict) -> None:
    """隐私：`_cells[*]["text"]` 保存的是**原文**单元格文本（xlsx 直读时是明文），
    hub JSON 只保留坐标元数据，绝不落明文。"""
    cells = table.get("_cells")
    if not isinstance(cells, dict):
        return
    out["_cells"] = {
        "header": [_strip_meta(m) for m in (cells.get("header") or [])],
        "rows": [[_strip_meta(m) for m in row] for row in (cells.get("rows") or [])],
    }


def fail_closed_table(table: dict, error: Exception | None = None) -> dict:
    """脱敏失败时的**闭合**处理：整表值替换为占位符，绝不把明文放行。

    宁可这一页表格内容不可用（并记录 error 供排查），也不能让未脱敏的真值
    流进 hub 页文本 / hub JSON。
    """
    out = dict(table)
    out["rows"] = [
        [FAIL_CLOSED if str(c or "").strip() else c for c in (row or [])]
        for row in (table.get("rows") or [])
    ]
    stats = dict(table.get("desens_stats") or {})
    stats.update({
        "cells": sum(1 for row in (table.get("rows") or []) for c in row if str(c or "").strip()),
        "masked": 0, "skipped_format": 0, "columns": {},
        "fail_closed": True, "error": f"{type(error).__name__}: {error}" if error else "unknown",
    })
    out["desens_stats"] = stats
    _scrub_cells(out, table)
    out.pop("raw_lines", None)
    out.pop("header_anchors", None)
    out.pop("cell_anchors", None)
    return out


def _is_label(text: str) -> bool:
    """像"字段名"吗？（短、无长数字、不是编号、不是长句）——用来区分"标签"与"值"。

    必要性：**值也可能命中列头语义**（"宁波银行股份有限公司无锡分行"含"公司"→company），
    若不判标签，就会把值当标签、把真正的标签当值，导致该格的明文漏脱敏。
    """
    s = (text or "").strip()
    if not s or len(s) > 12:
        return False
    if re.search(r"\d{3,}", s) or _CODE_TOKEN_RE.search(s):
        return False
    if _RE_HAS_CJK.search(s):
        return not any(bad in s for bad in ("，", ",", "。", "；", ";", "、"))
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9 _/\-]{0,11}$", s))


_STRICT_VALUE_CHECK = None


def _looks_like_value(cat: str, s: str) -> bool:
    """这一格像**该类别的合法值**吗？（用于区分"标签"与"值"）

    与 `value_valid` 的分工：这里**更严**——公司值必须带机构后缀（有限公司/中心/银行…）、
    人名必须是 2~4 字中文姓名形态。踩坑（本轮）：`申请单位` 是标签，但因为
    `value_valid("company", …)` 对任何含中文的串都放行，它被当成"公司值"，
    于是 kv 打分不认为这一行有标签 → 整张 kv 表被判成普通表、标签行还被当表头。
    "要不要脱敏"另有文本规则安全网兜底，所以这里可以严；"标签/值"判错代价更大。
    """
    global _STRICT_VALUE_CHECK
    if _STRICT_VALUE_CHECK is None:
        try:
            import entity_repair__desens as _er

            _STRICT_VALUE_CHECK = _er.value_acceptable
        except Exception:
            _STRICT_VALUE_CHECK = False
    if _STRICT_VALUE_CHECK and cat in ("company", "party", "date"):
        try:
            return bool(_STRICT_VALUE_CHECK(cat, s)[0])
        except Exception:
            pass
    return value_valid(s, cat)


def _is_label_cell(cell: str) -> bool:
    """像"字段名"而不是"值"吗？（kv 布局里决定"这个格子是不是标签"）

    关键区分（真实踩坑）：`无锡鲲珩城市服务有限公司` 既是 12 字短句、又命中"公司"语义，
    单看这两条会被误判成**标签**——结果它的值没被脱敏，名字还被写进 `desens_stats.columns`
    元数据里泄漏。所以再加一条：**标签本身不该"像该类别的一个合法值"**，
    或者它必须带明确标签标点（结尾 `：`/`:`/`=`）。
    """
    s = (cell or "").strip()
    if not _is_label(s):
        return False
    cat = match_category(s)
    if cat is None:
        return True                    # 短、无数字、不像值 → 按标签对待（如"合同名称"）
    if s.endswith(_TRAILING_LABEL_PUNCT):
        return True                    # 明确带标签标点（如"收文单位："）
    return not _looks_like_value(cat, s)   # 本身不像该类别合法值 → 标签；像 → 当值处理


def looks_like_label(text: str) -> bool:
    """公开入口：这一格**本身像字段名**而不是像值吗？

    供下游复用（事实层在"表头不可信"时要靠它避免把标签写成值事实），
    与 `_is_label_cell` 同一判据，避免两处规则漂移。
    """
    return _is_label_cell(text)


def is_semantic_label(text: str) -> bool:
    """这一格是"**带语义的字段名**"吗（"开户行/账号/甲方"）？

    比 `looks_like_label` 更严：必须命中列头语义字典。用途是判 kv 里的"值格是不是
    下一个标签"——`付款方式 | □现金 ☑银行转账 | 开户行 | 宁波银行…` 这种一行两组
    标签|值，靠它才能知道"□现金 ☑银行转账"的右侧是**标签**而不是值。
    但不能用它判"值是不是标签"的一般情况：人名（"张晓晓"）形似标签却命中不了字典。
    """
    return bool(match_category(text)) and _is_label_cell(text)


def _sanitize_columns(columns: dict) -> dict:
    """隐私兜底：`desens_stats.columns` 是**会被写进 hub** 的元数据，
    绝不能把"看起来是值"的原文塞进去（真实泄漏点：公司名被当标签写进了这里）。"""
    out: dict = {}
    for label, cat in (columns or {}).items():
        s = str(label or "")
        if _is_label(s) and not value_valid(s, cat or "") and not _CODE_TOKEN_RE.search(s):
            out[s[:120]] = cat
        else:
            out[f"<值:{cat}>"] = cat
    return out


def is_kv_table(rows: list[list[str]]) -> bool:
    """判断这张表是不是"**字段名 | 值** 逐行排布"的布局（付款申请/报销单最常见）。

    为什么必须区分：这种表**第一行不是表头**（第一行本身就是"单位全称 | 某某公司"）。
    若按普通表处理，会把第一行的标签当成整列语义，于是"账号/开户行/金额"全被当成
    "公司名"处理——轻则语义错，重则把整段正文写进实体映射（曾直接报错）。

    判定改为**打分**（本轮）：见 `kv_score()`；阈值 0.5 且至少 2 个标签命中。
    调用方应**先剥掉标题/签章行**（`strip_non_data_rows`）再调用本函数——
    否则"第一行是标题"会把首列标签比例算低，判不出来。
    """
    return kv_score(rows)["is_kv"]


# =========================================================
# 行分类：标题 / 签章 / 数据（本轮新增）
# =========================================================
# 为什么需要：kv 表不产出表头（header=[]），标题行在 docx/OCR 路径会被当成"首行=表头"，
# 在事实层又会变成"没有 header 的值"。所以**先规定好怎么剥**，再判 kv、再定表头。
_SIGNATURE_STRONG_RE = re.compile(r"(签字|盖章|公章|签章|法定代表人)")
_SIGNATURE_WEAK_RE = re.compile(r"(日期|年\s*月\s*日)")
_DATE_VALUE_RE = re.compile(r"^\d{2,4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}\s*日?$")
_TITLE_PUNCT = ("：", ":", "；", ";", "，", ",")


def is_signature_row(row: list[str]) -> bool:
    """签章/落款行：`甲方（公章）：| 乙方（公章）：` 这类——不是数据、也不是标签对。

    踩坑（本轮）：早先把"含日期"就算签章行，结果**正常 kv 行**
    `申请单位 | 某某公司 | 申请日期 | 2026年9月1日` 被整行剥掉，金额/单位全丢。
    现在收紧：必须有**强签章词**（签字/盖章/公章/签章/法定代表人），
    或者整行**只由"日期类标签"或"日期类值"组成**（落款日期栏）。
    """
    cells = [str(c or "").strip() for c in row if str(c or "").strip()]
    if not cells or len(cells) > 4 or max(len(c) for c in cells) > 24:
        return False
    joined = " ".join(cells)
    if _SIGNATURE_STRONG_RE.search(joined):
        return True
    if not _SIGNATURE_WEAK_RE.search(joined):
        return False

    def _pure_label(c: str) -> bool:
        return bool(_SIGNATURE_WEAK_RE.search(c)) and not any(ch.isdigit() for ch in c)

    return all(_pure_label(c) or _DATE_VALUE_RE.match(c) for c in cells)


def _nonempty_count(row: list[str]) -> int:
    return sum(1 for c in row if str(c or "").strip())


def _field_label_row(row: list[str]) -> bool:
    """首格是不是**成词字段名**（"单位全称/合同名称/付款方式/开户行"）？

    用途：合并区判标题时排除"标签 | 跨列值"这种 kv 数据行。用 `entity_repair`
    的字段名词表（成词，不是"短就行"），避免把 `大长福（RJ合同）` 这种**分组表头**
    也算成字段名——那一次它是该被当标题剥掉的。
    """
    first = next((str(c or "").strip() for c in row if str(c or "").strip()), "")
    if not first:
        return False
    try:
        import entity_repair__desens as er

        return bool(er.looks_like_label(first))
    except Exception:
        return False


def strip_non_data_rows(
    rows: list[list[str]],
    *,
    grid_cols: int | None = None,
    merged_rows: set[int] | None = None,
    max_strip: int = 3,
) -> dict:
    """**先剥标题/签章行**：返回 {title, data, signature, layout, reasons}。

    判据（首行，或"表头候选行"逐个往下，最多 max_strip 行）：
      (a) 行内非空格 ≤1（跨列合并的大标题，如"付款申请"、"2026年结算台账"）；
      (b) 该行被合并区覆盖且跨 >1 列（xlsx: merged_ranges / docx: gridSpan / OCR: colspan）；
      (c) 非空格数 < 后续数据行非空格中位数的 1/2 且 ≤2；
      (d) 文本像标题（长度 >12、不含冒号/分号、不命中任何标签）且下一行非空格明显更多。
    剥出来的行**保留**在 `title` 里（调用方要渲染进页文本，不能丢）。
    签章行单独归 `signature`（它既不是标题也不是数据，避免被当表头覆盖整列语义）。
    """
    rows = [list(r) for r in (rows or [])]
    merged_rows = merged_rows or set()
    title: list[list[str]] = []
    signature: list[list[str]] = []
    reasons: list[str] = []
    data = rows
    for idx in range(min(max_strip, len(rows))):
        row = rows[idx]
        if not any(str(c or "").strip() for c in row):
            title.append(row)
            reasons.append(f"第{idx + 1}行整行空 → 标题区")
            data = rows[idx + 1:]
            continue
        n = _nonempty_count(row)
        rest = rows[idx + 1:]
        rest_counts = [_nonempty_count(r) for r in rest] or [0]
        rest_med = sorted(rest_counts)[len(rest_counts) // 2]
        joined = " | ".join(str(c or "").strip() for c in row if str(c or "").strip())
        # 签章行优先判（它既不是标题也不是数据；`甲方（公章）：| 乙方（公章）：` 这种
        # 若被当表头，整列语义会套到下面的签章/日期栏上——实测产出了 CO0112 脏登记）
        if is_signature_row(row):
            signature.append(row)
            reasons.append(f"第{idx + 1}行签章 → 签章区")
            data = rest
            continue
        is_title = False
        why = ""
        # (a) 只对**多列**块生效：单列块里"非空格 ≤1"是常态（说明块/单列清单），
        #     否则 `★注：…` 这类附注会被整块当标题剥掉（实测把 note 块吃空）
        if n <= 1 and (grid_cols or 0) > 1:
            is_title, why = True, "非空格 ≤1（跨列合并标题）"
        elif idx in merged_rows and (grid_cols or 0) > 1 and not _field_label_row(row):
            # 关键：**有合并 ≠ 是标题**。kv 表单里"标签 | 跨列的值"每一行都带横向合并
            # （实测 5片区付款申请整表 9 行全在 merged_rows 里），旧规则把 6 行数据全剥掉，
            # 只剩"申请人/申请日期"两行 → 连 kv 都判不出来。
            is_title, why = True, "被合并区覆盖且跨多列（且首格不是字段名）"
        elif n <= 2 and rest_med >= 2 and n * 2 < rest_med:
            is_title, why = True, f"非空格 {n} 远少于数据行中位 {rest_med}"
        elif (len(joined) > 12 and not any(p in joined for p in _TITLE_PUNCT)
              and match_category(joined) is None and rest_med > n):
            is_title, why = True, "长文本且无标签/冒号，下一行更宽"
        if not is_title:
            break
        title.append(row)
        reasons.append(f"第{idx + 1}行 → 标题区（{why}）")
        data = rest
    # 签章行**不止出现在表头之前**（合同落款常在表尾）：剥完前导行后，把剩余行里
    # 的签章行也移到 `signature`。它永远不是数据行，留在 `data` 里只会污染列语义。
    if data:
        kept: list[list[str]] = []
        for r in data:
            if is_signature_row(r):
                signature.append(r)
                reasons.append("表尾签章行 → 签章区")
            else:
                kept.append(r)
        data = kept
    return {"title": title, "signature": signature, "data": data,
            "stripped": len(title) + len(signature), "reasons": reasons}


def kv_score(rows: list[list[str]]) -> dict:
    """kv 布局打分（替代"首列≥60% 是标签"的硬条件）。

    hit_ratio       = 命中**标签语义**（列头字典）的标签格数 / 非空行数
    pair_row_ratio  = "结构上就是 标签|值 行"的行数 / 非空行数（**不要求命中字典**）
    label_col_ratio = 最左非空格落在**同一个列**（众数列）的行数占比 ← 表单的硬特征
    pair_ratio      = 命中的标签右侧（同行）或下一行同列存在非空值的比例
    kv_score        = 0.35*hit + 0.25*pair_row + 0.2*label_col + 0.2*pair

    判 kv 要**同时**满足：score ≥ 0.5、hit ≥ 1、pair_row_ratio ≥ 0.5、label_col_ratio ≥ 0.5。

    为什么加结构项（本轮实测踩坑）：
      · `付款申请` 这类表单大量标签不在语义字典里（合同名称/项目付款进度/付款方式/…），
        只按命中率打分 → 真 kv 表被判成普通表，"第一个数据行"被当表头，列语义全错；
      · 但结构项会让"每行都以文字开头"的普通表也变高，所以它只是**必要条件**：
        仍要求至少一个语义命中；
      · `label_col_ratio` 专门挡"会议签到表"这类：它的行是 `|小美|李玉丹|1|采购|`，
        左格既像标签又有右值（pair_row 假高），但**标签列在 0/1/3 之间跳**（不是表单），
        而真表单的标签几乎都在同一列（列众数占比高）。
    """
    rows = [r for r in (rows or []) if any(str(c or "").strip() for c in r)]
    if len(rows) < 2:
        return {"score": 0.0, "is_kv": False, "hit": 0, "hit_ratio": 0.0,
                "pair_row_ratio": 0.0, "label_col_ratio": 0.0, "left_ratio": 0.0,
                "pair_ratio": 0.0, "need": 1}
    hit = 0
    left_ok = 0
    pair_ok = 0
    pair_rows = 0
    label_rows = 0
    left_cols: list[int] = []
    for r_i, row in enumerate(rows):
        cells = [str(c or "").strip() for c in row]
        nonempty = [c for c in cells if c]
        if not nonempty:
            continue
        left_idx = next(i for i, c in enumerate(cells) if c)
        left = cells[left_idx]
        left_cols.append(left_idx)
        # 只统计"**标签格**"：值也可能命中类别词（"某某公司"命中 company），
        # 若把值也算命中，普通表的公司列会让整表看起来像 kv。
        left_is_label = bool(match_category(left)) and _is_label_cell(left)
        if left_is_label:
            left_ok += 1
        if _is_label_cell(left) and _row_has_value(cells, left_idx, rows, r_i):
            pair_rows += 1
        row_has_label = False
        for i, c in enumerate(cells):
            if c and match_category(c) and _is_label_cell(c):
                hit += 1
                row_has_label = True
                right = next((cells[j] for j in range(i + 1, len(cells)) if cells[j]), "")
                below = ""
                if not right and r_i + 1 < len(rows):
                    nxt = [str(x or "").strip() for x in rows[r_i + 1]]
                    if i < len(nxt):
                        below = nxt[i]
                # 值格不能本身又是**带语义的标签**（`付款方式 | 开户行 | 宁波银行…` 里，
                # 第一个格的"值"其实是下一个标签，说明它自己不是标签）。
                # 注意只排"带语义的标签"：`宣读人 | 张晓晓` 里的值形似标签但命中不了
                # 字典（人名），那不是"没有值"。
                if right and not is_semantic_label(right):
                    pair_ok += 1
                elif below and not is_semantic_label(below):
                    pair_ok += 1
        if row_has_label:
            label_rows += 1
    n = len(rows)
    hit_ratio = hit / n
    left_ratio = left_ok / n
    pair_row_ratio = pair_rows / n
    pair_ratio = pair_ok / max(hit, 1)
    modal = max(set(left_cols), key=left_cols.count) if left_cols else 0
    label_col_ratio = sum(1 for c in left_cols if c == modal) / max(len(left_cols), 1)
    score = (0.35 * hit_ratio + 0.25 * pair_row_ratio
             + 0.2 * label_col_ratio + 0.2 * pair_ratio)
    # 命中数阈值：只作"至少有一个语义标签"的下限（防空表/纯文本被当 kv）。
    # 行数多寡**不**再抬高这个下限——真正的护栏是下面两个结构比例。
    need = 1
    is_kv = bool(score >= 0.5 and hit >= need
                 and pair_row_ratio >= 0.5 and label_col_ratio >= 0.5)
    return {"score": round(score, 3), "is_kv": is_kv, "hit": hit,
            "label_rows": label_rows, "hit_ratio": round(hit_ratio, 3),
            "pair_row_ratio": round(pair_row_ratio, 3),
            "label_col_ratio": round(label_col_ratio, 3),
            "left_ratio": round(left_ratio, 3), "pair_ratio": round(pair_ratio, 3),
            "need": need}


def _looks_like_label(text: str) -> bool:
    """这一格像**字段名**（而非值）吗？（kv 结构项用；与事实层同口径）"""
    return _is_label_cell(text)


def _row_has_value(cells: list[str], left_idx: int, rows: list[list[str]], r_i: int) -> bool:
    """这一行在标签右侧（或下一行同列）有"像值"的非空格吗？（结构项用）

    "像值"= 不是**带语义的标签**（`宣读人 | 张晓晓 | 部门/子公司 | 瑞景公司` 里，
    张晓晓形似标签但是值；而 `付款方式 | 开户行` 里 开户行 是标签，不算值）。
    """
    for j in range(left_idx + 1, len(cells)):
        if cells[j] and not is_semantic_label(cells[j]):
            return True
    if r_i + 1 < len(rows):
        nxt = [str(x or "").strip() for x in rows[r_i + 1]]
        if left_idx < len(nxt) and nxt[left_idx] and not is_semantic_label(nxt[left_idx]):
            return True
    return False


def is_note_block(rows: list[list[str]]) -> bool:
    """说明/附注块：单元格很少、且多是长说明文字（如 `★注：…`、`1、苗木胸径…`）。"""
    rows = [r for r in (rows or []) if any(str(c or "").strip() for c in r)]
    if not rows:
        return False
    if max(_nonempty_count(r) for r in rows) > 2:
        return False
    long_cells = 0
    star_cells = 0
    for r in rows:
        for c in r:
            s = str(c or "").strip()
            if len(s) > 18:
                long_cells += 1
            if s.startswith(("★", "注", "说明", "备注", "附")):
                star_cells += 1
    return long_cells >= 2 or star_cells >= 1


def layout_of(rows: list[list[str]], *, grid_cols: int | None = None,
              merged_rows: set[int] | None = None) -> dict:
    """一张表块的**布局判定总入口**：先剥标题/签章 → 再判 kv → 再定表头。

    返回 {layout, title, signature, data, header, kv_score, reasons}；layout ∈
    kv / table / signature / note / noheader。调用方按它构建表块。
    """

    info = strip_non_data_rows(rows, grid_cols=grid_cols, merged_rows=merged_rows)
    data = info["data"]
    score = kv_score(data)
    layout = "table"
    header: list[str] = []
    body = data
    sig = info["signature"]
    if data:
        # **数据优先**：先按数据判 kv / 说明块 / 普通表。
        # 签章行只把"自己"摘出去，不能因为表尾有一行落款就把整张表判成签章区
        # （实测：OCR 的付款申请表单有落款行，被整表判成 signature → 一个事实都没有）。
        if score["is_kv"]:
            layout = "kv"
        elif is_note_block(data):
            layout, header = "note", []
        else:
            # 选表头：优先"**这一块里最宽、且每格都像字段名**"的行（最多看前 HEADER_SCAN 行）。
            # 为什么：会议签到表这类块前面还有 `时间|…|地点|…`、`内容：…`、`主持人：` 几行，
            # 直接拿第一行当表头 → 列名变成"时间/年 月 日/地点"，事实抽成 `[时间]=内容：…`。
            # 真正 6 列的表头（`序号|投标单位|签名|序号|谈判小组|签名`）是**最宽**的一行。
            hdr_idx = 0
            if len(data) >= 2:
                widest = max(_nonempty_count(r) for r in data)
                for k in range(min(_HEADER_SCAN, len(data) - 1)):
                    row_k = [str(c or "").strip() for c in data[k]]
                    if _nonempty_count(row_k) != widest:
                        continue
                    if all((not c) or _is_label_cell(c) or match_category(c) for c in row_k):
                        hdr_idx = k
                        break
            if hdr_idx:
                info["title"] = info["title"] + [list(r) for r in data[:hdr_idx]]
                info["reasons"] = info["reasons"] + [
                    f"表头前的 {hdr_idx} 行题头行 → 标题区（表头在第 {hdr_idx + 1} 行）"]
                data = data[hdr_idx:]
            head = [str(c or "").strip() for c in data[0]]
            head_nonempty = sum(1 for c in head if c)
            rest_nonempty = max((_nonempty_count(r) for r in data[1:]), default=0)
            # 护栏 1：表头只有 1 个非空格而数据行更宽 → 不许当表头（防"标题被当表头"）
            # 护栏 2：`data` 只剩 1 行时**不许当表头**——"有表头没数据"是自相矛盾的，
            #         实测把"标题被剥掉后唯一的一行数据"（`2026年9月|已付|30%`）整行
            #         变成了列头，body 变空，数据直接丢了。
            if len(data) < 2 or (head_nonempty <= 1 and rest_nonempty > 1):
                layout, header = "noheader", []
            else:
                header = head
                body = data[1:]  # 表头行**不再留在 body 里**（否则页文本会重复一行表头）
            # 签章区（落款 + 日期，数据行很少）→ 仍按签章处理，别把落款当表头
            if sig and len(data) <= 2:
                layout, header, body = "signature", [], data
    elif sig:
        # 签章/落款区：**不取表头**（否则"甲方（公章）："会变成列语义，套到下面的
        # 签章/日期栏上——实测产出了 CO0112 脏登记）。剩下的行按普通行交给文本规则。
        layout, header = "signature", []
    return {**info, "layout": layout, "header": header, "body": body, "kv_score": score}


def mask_prebuilt(table: dict, store) -> dict:
    """对**已经判好布局**的表块做分流脱敏（不重判）。

    用途：OCR 路径的布局在 `ocr_tables.tables_from_page_json` 里判好（那里没有 store），
    脱敏要等到 hub 流水线（有 store）才做。这里保证分流规则与 `mask_table_layout` 一致。
    """
    layout = str(table.get("layout") or "table").lower()
    if layout == "kv":
        return desensitize_kv_table(table, store)
    if layout in ("signature", "note", "noheader"):
        return safety_only_table(table, store)
    return desensitize_table(table, store)


def mask_table_layout(
    rows: list[list[str]],
    store,
    *,
    source: str = "",
    grid_cols: int | None = None,
    merged_rows=None,
    extra: dict | None = None,
) -> tuple[dict, dict]:
    """**唯一的布局分流入口**（xlsx / docx / OCR 三条读取路径共用，防止规则漂移）。

    顺序固定为：先剥标题/签章行 → 再判 kv → 再定表头 → 按布局脱敏。
    返回 `(raw_table, masked_table)`，两者都带 `layout` / `title_rows` / `signature_rows`，
    调用方负责把这两类行**渲染进页文本**（`title:` / `signature:` 前缀），信息不丢。
    """
    lo = layout_of(rows, grid_cols=grid_cols,
                   merged_rows=set(merged_rows or ()))
    layout = lo["layout"]
    # `body` = 去掉表头行之后的数据行（kv/signature/note/noheader 就是全部剥剩行）
    data = lo["body"] if lo["body"] else [list(r) for r in rows]
    base = {
        "source": source,
        "header": [],
        "rows": [list(r) for r in data],
        "header_merged": bool(merged_rows),
        "style_tags": [],
        "layout": layout,
        "title_rows": [list(r) for r in lo["title"]],
        "signature_rows": [list(r) for r in lo["signature"]],
        "layout_reasons": lo["reasons"],
        "kv_score": lo["kv_score"],
    }
    if extra:
        for k, v in extra.items():
            base.setdefault(k, v)
    if layout != "kv" and layout not in ("signature", "note", "noheader"):
        base["header"] = [str(h or "") for h in (lo["header"] or [])]
    return base, mask_prebuilt(base, store)


def safety_only_table(table: dict, store=None) -> dict:
    """**不套列头语义**、只用文本规则兜底的表块（signature / note / noheader 布局）。

    为什么需要这条路径：签章区、说明块、"无表头数据"都**没有可靠的列语义**——
    硬套列头语义会把 `甲方（公章）：` 当公司列（实测产出 `CO0112` 脏登记），
    把计量单位列当公司列（`CO0113 = 株`）。这类表交给文本规则（日期/证件/公司/人员正则）
    逐格脱敏即可，既不误判类别，也不会漏明文。
    """
    out = dict(table)
    out["desens_stats"] = {"cells": 0, "masked": 0, "skipped_format": 0,
                           "columns": {}, "layout": table.get("layout") or "safety"}
    _scrub_cells(out, table)
    out.pop("raw_lines", None)
    _safety_rescan(out, store)
    return out


def desensitize_kv_table(table: dict, store) -> dict:
    """"字段名 | 值"逐行布局的脱敏：按**标签/值成对**处理（0、2、4… 是标签，1、3、5… 是值）。

    为什么用"成对"而不是"猜哪个格子像标签"（两次真实踩坑）：
      · `无锡鲲珩城市服务有限公司` 既是短句又命中"公司"语义 → 被猜成标签，结果值没脱敏、
        名字还被写进 `desens_stats.columns` 元数据里泄漏；
      · `申请人` 反过来又"像一个人名" → 被猜成值，导致后面真正的姓名没脱敏。
    真实单据（付款申请/通知单/发票）就是"标签|值|标签|值"交替排列，直接按位置配对最稳：
      · 偶数位命中列头语义 → 用它脱敏紧随其后的**一个**值格；
      · 其余格子交给 `_safety_rescan`（文本规则）兜底。
    """
    rows = [list(r) for r in (table.get("rows") or [])]
    stats = {"cells": 0, "masked": 0, "skipped_format": 0, "columns": {}, "layout": "kv"}
    new_rows: list[list[str]] = []
    for row in rows:
        new_row = list(row)
        for i in range(0, len(new_row) - 1, 2):
            label = str(new_row[i] or "").strip()
            if not _is_label(label):
                continue
            cat = match_category(label)
            if not cat:
                continue
            stats["columns"][label] = cat
            val = str(new_row[i + 1] or "")
            if not val.strip() or val.strip() == label:
                continue
            stats["cells"] += 1
            if value_valid(val, cat):
                new_row[i + 1] = desensitize_value(store, val, cat)
                stats["masked"] += 1
            else:
                stats["skipped_format"] += 1
        new_rows.append(new_row)
    out = dict(table)
    out["rows"] = new_rows
    stats["columns"] = _sanitize_columns(stats["columns"])
    out["desens_stats"] = stats
    _scrub_cells(out, table)
    out.pop("raw_lines", None)
    _safety_rescan(out, store)
    return out


def _safety_rescan(out: dict, store) -> None:
    """单元格**安全网**：逐格再跑一次文本脱敏（幂等）。

    为什么需要：列头/标签语义只能覆盖"认识的标签 + 合规的值格式"；一旦遇到
    没见过的标签布局（如一行两组"标签|值"、或标签写错），那一格就会留明文。
    这里用文本规则兜底（长数字/证件/税号/银行名/公司名等），确保"表里的值"
    与"页文本"两条路径**结论一致**，不会再出现"页文本已脱敏、表结构里还留着明文"。
    """
    try:
        import hub_pipeline__desens as hp

        tracker = hp.DesensTracker()
        # 重新赋一个新列表：**绝不原地改写调用方的 header**（raw 表块还要用它做原文对账）
        hdr = out.get("header")
        if isinstance(hdr, list):
            out["header"] = [hp.desensitize_text(str(v or ""), store, tracker) for v in hdr]
        for row in (out.get("rows") or []):
            for i, v in enumerate(row):
                row[i] = hp.desensitize_text(str(v or ""), store, tracker)
    except Exception:
        pass


def desensitize_table(table: dict, store) -> dict:
    """按列头语义脱敏一张表块；返回统计与列映射（不改动传入对象）。"""
    header: list[str] = list(table.get("header") or [])
    col_categories: dict[int, str] = {}
    for i, h in enumerate(header):
        cat = match_category(h)
        if cat:
            col_categories[i] = cat

    new_rows: list[list[str]] = []
    stats = {"cells": 0, "masked": 0, "skipped_format": 0, "columns": {}}
    for row in (table.get("rows") or []):
        new_row = list(row)
        for i, cat in col_categories.items():
            if i >= len(new_row):
                continue
            val = new_row[i]
            if not str(val or "").strip():
                continue
            stats["cells"] += 1
            if not value_valid(str(val), cat):
                stats["skipped_format"] += 1
                continue
            new_row[i] = desensitize_value(store, str(val), cat)
            stats["masked"] += 1
        new_rows.append(new_row)

    stats["columns"] = {
        header[i] if i < len(header) else f"col{i}": cat for i, cat in col_categories.items()
    }
    out = dict(table)
    out["rows"] = new_rows
    stats["columns"] = _sanitize_columns(stats["columns"])   # 元数据里绝不落"值"原文
    out["desens_stats"] = stats
    _scrub_cells(out, table)   # 隐私：不把原文单元格文本带进 hub
    out.pop("raw_lines", None)  # 隐私：OCR 的 raw_lines 是未脱敏 HTML
    _safety_rescan(out, store)  # 安全网：逐格文本脱敏兜底（幂等）
    return out
