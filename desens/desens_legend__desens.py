"""脱敏编号图例：所有 AI 提示词共用一份"编号代表什么类别"的说明。

为什么必须动态生成：
  编号前缀 → 类别的映射只在 `database_serv` 里定义一处（`ENTITY_TABLES` /
  `_CODE_PREFIX`）。提示词里若手写"CO/PT 是公司/人员"，一旦新增类别
  （如 PJ 项目、TX 税号、BK 开户行）就会漏，AI 会把编号当成人名/公司名照抄，
  甚至据此编造事实。因此提示词一律经本模块取图例，与代码 **永远同步**。

用法：
    from desens_legend__desens import prompt_block
    system = "你是……。" + prompt_block()
"""
from __future__ import annotations

import re

__all__ = [
    "CATEGORY_LABELS",
    "SELF_TAG",
    "SELF_MARK_RE",
    "self_display",
    "legend_lines",
    "legend_text",
    "prompt_block",
    "BRANCH_RULE_TEXT",
    "branch_rule",
]

# 类别 -> 人类可读含义（新增类别时在这里补一行；缺省回退类别名本身）
CATEGORY_LABELS: dict[str, str] = {
    "company": "公司/单位全称",
    "party": "人员姓名",
    "date": "日期",
    "id_card": "身份证号",
    "bank_card": "银行卡号",
    "tax_id": "统一社会信用代码/纳税人识别号",
    "bank_name": "开户银行名称（含支行）",
    "bank_account": "银行账号（含对公账号）",
    "project": "项目名称（最高管理员登记加密）",
    "phone": "联系电话/手机号",
}

# 本公司（我方主体）编号的特殊标记：形如 [本公司·CO0001]
SELF_TAG = "本公司"
SELF_MARK_RE = re.compile(r"\[" + SELF_TAG + r"·([A-Z]{2}\d{4})\]")


def self_display(code: str) -> str:
    """本公司编号的展示形式：编号本身不变，但加上"本公司"特殊提示。"""
    return f"[{SELF_TAG}·{code}]"


def legend_lines() -> list[str]:
    """生成 "PREFIX#### = 含义" 列表（顺序按 database_serv 的类别定义，稳定可复现）。"""
    from infra.database_serv__infra import ENTITY_TABLES, _CODE_PREFIX

    lines: list[str] = []
    for category in ENTITY_TABLES:
        prefix = _CODE_PREFIX.get(category)
        if not prefix:
            continue
        label = CATEGORY_LABELS.get(category, category)
        lines.append(f"{prefix}#### = {label}")
    return lines


def legend_text() -> str:
    """一行式图例（日志/轻量场景用）。"""
    return "；".join(legend_lines())


def prompt_block() -> str:
    """可直接拼到 system 提示词末尾的【脱敏编号说明】段（含掩码与本公司标记说明）。"""
    items = "\n".join(f"  · {line}" for line in legend_lines())
    return (
        "\n\n【脱敏编号说明（务必按此理解，不要当成真实名称）】\n"
        f"{items}\n"
        f"  · {self_display('CO####')} = 我方公司（最高管理员登记确认的本公司名称，"
        "看到它请按\"本公司/我方\"处理，不要当成第三方对手方）；\n"
        "  · 形如 9144**********7X1Q[TX0001]、6222********1234[BC0002] 的值："
        "首尾保留的几位是真实片段，完整真值另有加密存档——**不要**据此补全或编造完整号码；\n"
        "  · 编号是同一实体在全库的唯一代号：同一编号 = 同一家公司/同一个人/同一个项目，"
        "不同编号 = 不同实体；需要引用某实体时**原样照抄编号**，不要改写、不要翻译。"
        + BRANCH_RULE_TEXT
    )


# =========================================================
# 主编号 + 分支后缀 约定（同一实体的不同分支/标段/片区）
# =========================================================
# 为什么要在提示词里说清：真实项目名/公司名常以"同一主体 + 不同标段/片区/期次"出现
# （如「马山环卫大物业绿化养护项目-三标段」「…（原5片区）」「XX集团北京分公司」）。
# 若不给约定，模型对同一主体的两种写法会判成**不同项目/不同公司**（漏关联），
# 或者反过来把不同项目当成同一个（错关联）。约定：**主编号相同、后缀不同 = 同一主体的不同分支**。
BRANCH_RULE_TEXT = (
    "\n  · **主编号 + 分支后缀**：同一实体（项目/公司）的不同分支共用**同一个主编号**，"
    "用后缀区分，例如 `PJ0007`（主项目）、`PJ0007-01`（一标段）、`PJ0007-02`（二标段）；"
    "公司同理，如 `CO0012`（总公司）、`CO0012-01`（某分公司）。\n"
    "    判读规则：**主编号相同 → 同一主体的不同分支，视为强相关（同项目/同集团），"
    "不要当成两个无关实体**；主编号不同 → 不同主体。比较名称时先比主编号，再比后缀；"
    "后缀（标段/片区/期次/分公司）**不能**单独当作独立主体的证据。"
)


def branch_rule() -> str:
    """分支编号约定文本（供边判定等短提示词按需追加，避免重复整段图例）。"""
    return BRANCH_RULE_TEXT.strip()
