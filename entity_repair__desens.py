"""脱敏登记表的**清洗、拒收规则、人工登记与分支编号**（最高管理员用）。

背景（实测根因，见 2026-09-13 诊断）：
  · `entity_mapping_company` 里混进了大量**根本不是公司**的条目：
      `CO0113 = 株`、`CO0112 = 20 年 月 日`、`CO0118 = 项目付款进度`、
      `CO0121 = ¥789814.72 元`、`CO0127 = 申请人`、`CO0128 = 张佳平`（人在 party 表里已有 PT0009）…
  · 直接原因两条：
      ① **歧义列头**：`单位` 在 HEADER_RULES 里映射到 company，于是苗木清单里
         "单位"这一**计量单位列**（株/棵/个）整列被当公司脱敏 → `株` 拿到 CO 编号；
         "甲方（公章）：/乙方（公章）：" 被当列头 → 其下**签章日期栏**（20 年 月 日）也被当公司；
      ② **值校验太宽**：`value_valid()` 对 company/party 只要求"含中文"，
         于是标签文字、金额串、说明文字全数通过 → 登记入库。
  · `PT0011 = '| 乙方'` 是人员表里的同类脏数据。

本模块提供四件事：
  1. **拒收规则** `value_acceptable(category, value)`：
     公司必须有公司后缀结构；人员必须是干净的中文姓名；一律拒收
     标签词/计量单位/金额串/日期残片/含说明标点/表格残片。登记入口统一走它。
  2. **脏登记扫描** `scan_dirty()` / `cross_category_duplicates()`：
     按规则给出"为什么可疑"，供管理员逐条确认（不自动删，避免误伤真名）。
  3. **删除/清洗** `cleanup(codes, ...)`：
     删除登记 + 写审计日志 `logs/desens_repair/repair_<date>.jsonl` +
     **扫描 hub 里哪些文档还引用该编号**（删前告知，避免出现悬空编号）。
  4. **人工登记 + 分支编号** `register(...)`：
     支持「主编号 + 分支后缀」（`PJ0007` / `PJ0007-01`，公司同理 `CO0012-01`），
     与提示词里的约定一致（见 desens_legend.BRANCH_RULE_TEXT）。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

__all__ = [
    "value_acceptable",
    "looks_like_label",
    "looks_like_unit_or_noise",
    "scan_dirty",
    "cross_category_duplicates",
    "cleanup",
    "register",
    "ensure_columns",
    "hub_references",
    "branch_code",
    "list_entries",
    "LOG_DIR",
]

BASE_DIR = Path(__file__).resolve().parent
# hub 根随工作区（仓库）切换：见 workspace__infra
from workspace__infra import hub_root as _hub_root

HUB_DIR = _hub_root()
LOG_DIR = BASE_DIR / "logs" / "desens_repair"

# ---------- 规则语料 ----------
# 公司后缀（structure 判据）：公司必须有这些结构之一，否则不认（宁可漏码，不可乱码）
_COMPANY_SUFFIX = (
    "有限公司", "有限责任公司", "股份有限公司", "集团有限公司", "集团", "公司",
    "事务所", "会计师事务所", "律师事务所", "研究院", "设计院", "学院", "大学",
    "研究院有限公司", "厂", "商店", "商行", "门市部", "合作社", "工作室",
    "人民政府", "管理局", "管理委员会", "委员会", "中心", "事业部", "分公司",
    "办事处", "分行", "支行", "营业部", "分理处", "储蓄所", "税务局", "供电局",
    "自来水公司", "医院", "学校", "幼儿园", "银行",
)
# 计量单位 / 数量词（绝不该成为公司或人名）
_UNIT_WORDS = frozenset({
    "株", "棵", "枝", "盆", "袋", "包", "箱", "件", "套", "个", "只", "条", "块",
    "张", "台", "辆", "吨", "公斤", "千克", "克", "斤", "米", "厘米", "毫米",
    "平方米", "立方米", "公里", "升", "毫升", "度", "次", "人", "份", "项", "批",
    "元", "万元", "天", "月", "年", "小时", "工日", "台班", "杯", "桶", "卷",
})
# 表格残片/说明性标点：出现即判"不是实体名"
_NOISE_CHARS = ("¥", "￥", "：", ":", "|", "｜", "，", ",", "。", "；", ";", "（盖章",
                "（公章", "（签字", "签字", "盖章", "以下", "以下简称")
_LABEL_WORDS = (
    "单位全称", "单位名称", "购方", "销方", "甲方", "乙方", "合同名称", "合同编号",
    "合同金额", "项目名称", "项目付款进度", "申请拨款金额", "付款方式", "开户行",
    "开户银行", "银行账号", "纳税人识别号", "统一社会信用代码", "申请人", "申请日期",
    "管理处核实意见", "备注", "序号", "品种", "规格", "数量", "单价", "总额", "合计",
    "价税合计", "金额", "税额", "开票日期", "开票人", "收款人", "复核人", "制表人",
    "审核人", "批准人", "经办人", "法定代表人", "地址", "电话", "联系方式", "养护期",
    "服务期", "日期", "期间", "季度", "月份", "下载次数", "企业名称", "公司地址",
    "单位", "名称", "编号", "类型", "状态", "说明",
    # 表单/会议记录类的常见字段名（本轮新增）：否则这些行在"合并区=标题"的判据下
    # 会被整行剥成标题，`时间/地点/主持人` 这些 kv 行就再也抽不成事实了
    "时间", "地点", "会议时间", "会议地点", "主持人", "参会人员", "与会人员",
    "列席部门", "投标单位", "谈判小组", "签名",
)
_DATE_FRAGMENT_RE = re.compile(r"(?:\d{1,4}\s*年|\d{1,2}\s*月|\d{1,2}\s*日|"
                               r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2})")
_AMOUNT_RE = re.compile(r"[¥￥]?\s*\d[\d,]*\.?\d*\s*(?:元|万元)")
_CJK_NAME_RE = re.compile(r"^[\u4e00-\u9fa5·]{2,4}$")
_COMPANY_SHAPE_RE = re.compile(
    r"^[\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,40}(?:" +
    "|".join(re.escape(s) for s in sorted(_COMPANY_SUFFIX, key=len, reverse=True)) + r")"
    r"(?:[\u4e00-\u9fa5A-Za-z0-9（）()·\-]{0,20})?$")
_SURNAME_HINT = ("张", "王", "李", "赵", "刘", "陈", "杨", "黄", "周", "吴", "徐", "孙",
                 "马", "朱", "胡", "郭", "林", "何", "高", "罗", "郑", "梁", "谢", "宋",
                 "唐", "许", "韩", "冯", "邓", "曹", "彭", "曾", "肖", "田", "董", "潘",
                 "袁", "蔡", "蒋", "余", "于", "杜", "叶", "程", "苏", "魏", "吕", "丁",
                 "任", "沈", "姚", "卢", "姜", "崔", "钟", "谭", "陆", "汪", "范", "金",
                 "石", "廖", "贾", "夏", "韦", "付", "方", "白", "邹", "孟", "熊", "秦",
                 "邱", "江", "尹", "薛", "闫", "段", "雷", "侯", "龙", "史", "陶", "黎",
                 "贺", "顾", "毛", "郝", "龚", "邵", "万", "钱", "严", "覃", "武", "戴",
                 "莫", "孔", "向", "汤")


def looks_like_label(text: str) -> bool:
    """这段文本是不是"字段名/标签"（而不是值）？"""
    s = (text or "").strip().rstrip("：:=＝ ")
    if not s:
        return False
    if s in _LABEL_WORDS:
        return True
    return any(s == w or s.endswith(w) and len(s) <= len(w) + 2 for w in _LABEL_WORDS)


def looks_like_unit_or_noise(text: str) -> tuple[bool, str]:
    """计量单位/金额串/日期残片/表格残片 → 不可能是公司名或人名。"""
    s = (text or "").strip()
    if not s:
        return True, "空值"
    if s in _UNIT_WORDS:
        return True, f"计量单位/数量词（{s}）"
    if _AMOUNT_RE.search(s):
        return True, "金额串（含 ¥/元）"
    if _DATE_FRAGMENT_RE.search(s):
        return True, "日期残片（年/月/日）"
    for ch in _NOISE_CHARS:
        if ch in s:
            return True, f"含表格/说明性字符 {ch!r}"
    if s.startswith(("已付", "已收", "大写", "本次", "备注", "说明", "注：")):
        return True, "说明性文字"
    if len(s) <= 1:
        return True, "单字（标签/单位常见）"
    return False, ""


def value_acceptable(category: str, value: str) -> tuple[bool, str]:
    """登记前的**结构校验**：这类别的值长这样吗？（拒绝则不得写入映射表）

    返回 (是否可接受, 原因)。规则偏保守：宁可漏掉一个不规范的实体（以后人工登记），
    也不要把标签/单位/金额写进实体表——因为写错的代价是"全库编号语义被污染"。
    """
    s = (value or "").strip()
    ok_noise, why = looks_like_unit_or_noise(s)
    if category in ("company", "party") and ok_noise and s not in _UNIT_WORDS:
        return False, why
    if category in ("company", "party") and looks_like_label(s):
        return False, f"这是字段名/标签（{s}）"
    if category == "company":
        if s in _UNIT_WORDS:
            return False, f"计量单位（{s}）"
        if not _COMPANY_SHAPE_RE.match(s):
            return False, "不含公司/机构后缀（如 有限公司/集团/中心/银行…）"
        return True, ""
    if category == "party":
        core = re.sub(r"[（(].*?[)）]", "", s).strip()
        if not _CJK_NAME_RE.match(core):
            return False, "不是 2~4 字中文姓名形态"
        if not core.startswith(_SURNAME_HINT):
            return False, "首字不在常见姓氏表内（防把词条当人名）"
        return True, ""
    if category == "date":
        if not _DATE_FRAGMENT_RE.search(s):
            return False, "不像日期"
        return True, ""
    # 其它类别（税号/银行/账号/证件/电话/项目）交给各自的正则校验，这里不拦
    return True, ""


# ---------- 表/列 ----------
def _tables() -> dict:
    from database_serv__infra import ENTITY_TABLES

    return dict(ENTITY_TABLES)


def ensure_columns() -> None:
    """给 mapping 表补 `parent_code` / `branch_seq`（分支编号用；幂等）。"""
    from database_serv__infra import get_admin_connection

    with get_admin_connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for tbl in ("entity_mapping_project", "entity_mapping_company"):
                try:
                    cur.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS parent_code VARCHAR(16)")
                    cur.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS branch_seq INT")
                except Exception:
                    conn.rollback()


def branch_code(parent_code: str, seq: int) -> str:
    """分支编号：主编号 + `-NN`（与提示词约定一致：PJ0007-01）。"""
    return f"{str(parent_code).strip()}-{int(seq):02d}"


def list_entries(category: str, *, limit: int = 2000) -> list[dict]:
    """列出某类别的登记（项目类只给编号，明文不落盘的类别不外显真值）。"""
    from database_serv__infra import SECRET_CATEGORIES, get_connection

    table = _tables().get(category)
    if not table:
        return []
    secret = category in SECRET_CATEGORIES
    cols = "code, norm_key" + ("" if secret else ", real_value")
    try:
        extra = ", parent_code, branch_seq"
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {cols}{extra} FROM {table} ORDER BY id LIMIT %s", (limit,))
            rows = cur.fetchall()
    except Exception:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {cols} FROM {table} ORDER BY id LIMIT %s", (limit,))
            rows = [tuple(r) + (None, None) for r in cur.fetchall()]
    out = []
    for r in rows:
        out.append({"category": category, "code": r[0],
                    "value": "" if secret else (r[1] or ""),
                    "masked": "<密文/编号展示>" if secret else "",
                    "parent_code": r[len(r) - 2] if len(r) >= 3 else None,
                    "branch_seq": r[len(r) - 1] if len(r) >= 3 else None})
    return out


def _value_of(category: str, table: str) -> dict:
    """{code: 明文值}（仅明文类别；敏感类别只给 code，无法判断内容）。"""
    from database_serv__infra import SECRET_CATEGORIES, get_connection

    if category in SECRET_CATEGORIES:
        return {}
    out: dict[str, str] = {}
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT code, real_value FROM {table}")
        for code, val in cur.fetchall():
            out[str(code)] = str(val or "")
    return out


# ---------- 扫描 ----------
_LABEL_FRAGMENTS = ("名称", "账号", "帐号", "账户", "卡号", "开户行", "开户银行",
                    "开户机构", "银行名称", "开户网点", "联行号", "行号", "备注", "说明",
                    "单位", "公司", "日期", "金额", "编号")


def _looks_like_label_fragment(cat: str, value: str) -> tuple[bool, str]:
    """银行名/卡号等**非 company/party** 类别里的"标签碎片"。

    实测来源：单元格只写"开户行名称"/"开户行账号"时，文本规则把锚点后面的
    "名称"/"账号"当成银行名登记（掩码 '名*'/'账*' → BK0008/BK0010）。
    company/party 有 `value_acceptable` 把关，银行/卡/证件类别原先没有任何结构校验，
    所以这类碎片只能在这里补一道检查。
    """
    s = (value or "").strip()
    if cat == "bank_name":
        if s in _LABEL_FRAGMENTS or any(s.endswith(w) and len(s) <= len(w) + 1
                                        for w in _LABEL_FRAGMENTS):
            return True, f"银行名是标签碎片（{s}）"
        if _masked_only(s):
            return True, "掩码里没有可辨识的银行名（疑似从标签/占位文字切出来）"
    if cat in ("bank_card", "id_card", "bank_account") and _masked_only(s):
        return True, "掩码里没有可辨识的数字主体"
    return False, ""


def _masked_only(masked: str) -> bool:
    """掩码里是否只剩"首尾各 1 个字 + 星号"这类无法辨识的碎片。"""
    s = (masked or "").strip()
    if not s:
        return True
    core = s.replace("*", "").replace("•", "")
    return len(core) <= 2


def scan_dirty(categories: tuple[str, ...] | None = None) -> list[dict]:
    """扫描可疑登记：给出"为什么可疑"，不自动删（管理员逐条确认）。

    ⚠️ 口径（本轮修正）：以前**只扫 company/party**，于是银行名碎片（`BK0008='名*'`）、
    18 位账号被当身份证（`ID0001`）、订单号被当银行卡（`BC0009`）、跨类别重复
    （同一家银行既有 `CO0125` 又有 `BK0001`）一律扫不出来——清理时就被漏掉。
    现在默认扫**全部类别**：结构校验 + 标签碎片 + 跨类别重复。
    """
    cats = categories or tuple(_tables().keys())
    out: list[dict] = []
    tables = _tables()
    from database_serv__infra import SECRET_CATEGORIES

    for cat in cats:
        table = tables.get(cat)
        if not table:
            continue
        if cat in SECRET_CATEGORIES:
            # 敏感类别不能解密，但 **masked 列是明文**，用它做形态检查即可：
            # 掩码只剩 1~2 个可辨识字（'名*' / '账*'）就是标签碎片。
            for code, _nk, masked in _rows_of(table):
                frag, why2 = _looks_like_label_fragment(cat, masked)
                if frag:
                    out.append({"category": cat, "code": code, "value": masked, "reason": why2})
            continue
        for code, val in _value_of(cat, table).items():
            ok, why = value_acceptable(cat, val)
            if not ok:
                out.append({"category": cat, "code": code, "value": val, "reason": why})
                continue
            frag, why2 = _looks_like_label_fragment(cat, val)
            if frag:
                out.append({"category": cat, "code": code, "value": val, "reason": why2})
    for dup in cross_category_duplicates():
        for cat, code in dup["entries"]:
            out.append({"category": cat, "code": code, "value": dup["value"],
                        "reason": f"同一值跨类别重复登记：{dup['entries']}"})
    return out


def _rows_of(table: str) -> list[tuple[str, str, str]]:
    """[(code, norm_key, 显示值)]；敏感类别没有 real_value → 用 masked（不解密）。"""
    from database_serv__infra import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("select column_name from information_schema.columns where table_name=%s",
                    (table,))
        cols = {r[0] for r in cur.fetchall()}
        valcol = "real_value" if "real_value" in cols else "masked"
        cur.execute(f"select code, norm_key, {valcol} from {table}")
        return [(str(c), str(nk or ""), str(v or "")) for c, nk, v in cur.fetchall()]


def cross_category_duplicates() -> list[dict]:
    """同一真实值同时登记在多个类别。

    典型（实测）：`宁波银行股份有限公司无锡分行` 既有 company 的 `CO0125` 又有
    bank_name 的 `BK0001`；18 位对公账号同时登记成 `BA0008` 与 `ID0001`。
    做法：**用 norm_key/指纹比对，不依赖解密**——明文类别的 norm_key 就是归一化明文
    （或超长值的 sha256），敏感类别的 norm_key 就是 sha256；两者都能映射到同一个
    sha256 空间，于是明文↔敏感也能比。
    """
    import hashlib

    from database_serv__infra import SECRET_CATEGORIES

    tables = _tables()
    by_fp: dict[str, list[dict]] = {}
    for cat, table in tables.items():
        for code, nk, val in _rows_of(table):
            if not nk:
                continue
            fp = nk if cat in SECRET_CATEGORIES else hashlib.sha256(nk.encode("utf-8")).hexdigest()
            by_fp.setdefault(fp, []).append({"category": cat, "code": code, "value": val})
    return [{"value": e[0]["value"], "entries": [(x["category"], x["code"]) for x in e]}
            for e in by_fp.values() if len({x["category"] for x in e}) > 1]


def dead_registrations(*, limit: int = 400) -> list[dict]:
    """从未被任何 hub 引用的登记（"死登记"）。

    用途：清理时容易漏掉的一类——编号错了、但正文早已被重扫/替换，界面上再也看不到它，
    `scan_dirty` 也扫不到（它只看值形态）。实测：金额被当银行卡登记的
    `BC0005~BC0008`（327981.6515647…）就是这种，没有任何 hub 引用。
    """
    out: list[dict] = []
    for cat, table in _tables().items():
        for code, _nk, val in _rows_of(table):
            if len(out) >= limit:
                return out
            if not hub_references(code, limit_files=limit):
                out.append({"category": cat, "code": code, "value": val,
                            "reason": "无任何 hub 引用（可能是旧规则的死登记）"})
    return out


def hub_references(code: str, *, limit_files: int = 200) -> list[str]:
    """哪些 hub 文档里还引用着这个编号（删除前告知；删登记不等于删正文里的编号）。"""
    hits: list[str] = []
    if not HUB_DIR.exists():
        return hits
    for p in list(HUB_DIR.rglob("*.json"))[:limit_files]:
        if p.name.endswith((".l1.json", ".features.json")):
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if code in txt:
            hits.append(p.relative_to(HUB_DIR).as_posix()[: -len(".json")])
    return hits


# ---------- 清洗 ----------
def cleanup(codes: list[str], *, category: str, user: dict | None = None,
            reason: str = "管理员清洗脏登记", dry_run: bool = False) -> dict:
    """删除登记（连带分支列），并写审计日志 + 列出仍引用该编号的 hub 文档。

    ⚠️ 语义说明：删登记**不会**改 hub 正文里的编号（那是历史脱敏结果）。
    如果想彻底干净，需要在删除后**重新扫描**相关文档（重新脱敏会按新规则生成编号）。
    """
    from database_serv__infra import get_connection

    table = _tables().get(category)
    if not table:
        return {"ok": False, "msg": f"未知类别 {category}"}
    ensure_columns()
    report = {"ok": True, "category": category, "dry_run": dry_run, "deleted": [],
              "missing": [], "hub_refs": {}, "reason": reason}
    with get_connection() as conn, conn.cursor() as cur:
        for code in codes:
            cur.execute(f"SELECT id FROM {table} WHERE code = %s", (code,))
            row = cur.fetchone()
            if not row:
                report["missing"].append(code)
                continue
            refs = hub_references(code)
            report["hub_refs"][code] = refs
            report["deleted"].append({"code": code, "hub_refs": len(refs)})
            if dry_run:
                continue
            cur.execute(f"DELETE FROM {table} WHERE code = %s", (code,))
        if not dry_run:
            conn.commit()
    audit({"action": "cleanup", "category": category, "codes": codes,
           "dry_run": dry_run, "reason": reason, "by": (user or {}).get("username"),
           "hub_refs": {k: len(v) for k, v in report["hub_refs"].items()}})
    return report


def register(category: str, real_value: str, *, user: dict | None = None,
             parent_code: str | None = None, branch_label: str | None = None) -> dict:
    """人工登记一个实体（管理员手动补登 / 登记项目分支）。

    · 先过 `value_acceptable` 结构校验（拒绝标签/单位/金额串）；
    · `parent_code` + `branch_label` 给出时，编号按"主编号-NN"生成（分支语义）；
    · 明文类别写 real_value；敏感类别走既有 `get_or_create_secret_code`（只存指纹+密文）。
    """
    from database_serv__infra import (
        SECRET_CATEGORIES,
        MappingDbStore,
        _CODE_PREFIX,
        get_connection,
    )

    table = _tables().get(category)
    if not table:
        return {"ok": False, "msg": f"未知类别 {category}"}
    value = (real_value or "").strip()
    if not value:
        return {"ok": False, "msg": "值不能为空"}
    ok, why = value_acceptable(category, value)
    if not ok:
        return {"ok": False, "msg": f"结构校验未通过：{why}"}
    ensure_columns()
    store = MappingDbStore()
    prefix = _CODE_PREFIX.get(category, "EN")

    if parent_code and category in ("project", "company"):
        import hashlib

        main = str(parent_code).strip()
        norm = "".join(value.split())
        fingerprint = hashlib.sha256(norm.encode("utf-8")).hexdigest()
        # 幂等：同一值永远同一编号（映射表的核心不变量）。
        # 若该值已登记（哪怕挂在别的主编号下），直接返回既有编号，不再建第二条分支——
        # 否则会撞 norm_key 唯一约束（实测：同一项目名重复登记报 UniqueViolation）。
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT code, parent_code FROM {table} WHERE norm_key = %s",
                        (fingerprint,))
            row = cur.fetchone()
        if row:
            return {"ok": True, "code": row[0], "parent_code": row[1], "existing": True,
                    "msg": f"该值已登记为 {row[0]}（同值同码，未重复建分支）"}
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COALESCE(MAX(branch_seq), 0) FROM {table} "
                        f"WHERE parent_code = %s", (main,))
            seq = int(cur.fetchone()[0] or 0) + 1
        code = branch_code(main, seq)
        if category in SECRET_CATEGORIES:
            # 敏感类别（项目）：与 get_or_create_secret_code 同构写入
            # —— 只存 sha256 指纹 + Fernet 密文；masked 用编号本身（项目名不露片段）。
            from database_serv__infra import encrypt_value

            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    f"""INSERT INTO {table} (code, norm_key, masked, cipher,
                                             parent_code, branch_seq)
                        VALUES (%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (code) DO NOTHING""",
                    (code, fingerprint, code, encrypt_value(value), main, seq))
                conn.commit()
        else:
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    f"""INSERT INTO {table} (code, norm_key, real_value,
                                             parent_code, branch_seq)
                        VALUES (%s,%s,%s,%s,%s)
                        ON CONFLICT (code) DO NOTHING""",
                    (code, norm, value, main, seq))
                conn.commit()
        audit({"action": "register_branch", "category": category, "code": code,
               "parent": main, "label": branch_label, "by": (user or {}).get("username")})
        return {"ok": True, "code": code, "parent_code": main, "branch_seq": seq,
                "value": "" if category in SECRET_CATEGORIES else value,
                "msg": f"已登记分支 {code}（主编号 {main}，分支 {seq}）"}

    if category in SECRET_CATEGORIES:
        code = store.get_or_create_secret_code(category, value, None)
    else:
        code = store.get_or_create_code(category, value)
    audit({"action": "register", "category": category, "code": code,
           "by": (user or {}).get("username")})
    return {"ok": True, "code": code, "value": "" if category in SECRET_CATEGORIES else value,
            "msg": f"已登记 {code}"}


def audit(record: dict) -> None:
    """审计日志（只记编号与类别，明文类别才记值；敏感类别不记）。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **record}
        with (LOG_DIR / f"repair_{time.strftime('%Y%m%d')}.jsonl").open(
                "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass
