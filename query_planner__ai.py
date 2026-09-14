# -*- coding: utf-8 -*-
"""问答的「需求判断」层：先判断"这个问题要什么"，再决定动作。

为什么要它（实测问题）：现在问答只有**一条腿**——面板上选了图遍历，就所有问题都走
图遍历。于是：
  · "库里有多少份文件？" 这种**本机数一下就知道**的问题，去图里找锚点 → 锚点 0 条
    → 回来一句"没有找到可用的证据链"（明明 3 行 SQL 就能答）；
  · "发票金额是否一定等于合同金额？如果不等于，还有哪些可能" 这种问题，
    既需要**常识层面的规则**（分次开票/扣款/部分付款/税率差…），
    又需要**本库的具体资料**（开票资料、扣款资料、付款资料）——
    一刀切检索会把两者混在一起，AI 既不知道"该去找什么"，也拿不到该找的东西。

两层设计（**规则快路 + AI 判断**，都可关）：

    第 0 层  规则路由（零 token）：明显是"计数/清单/总览"的问题，直接本机统计作答。
    第 1 层  AI 规划（graph 链路，JSON 白名单）：问题 → 动作 + 需要哪几类证据 + 参数。
    第 2 层  执行器：按动作走本机 SQL / 索引卡按类别召回 / 现有图遍历；
             证据块交给 chat 链路作答（计数类直接作答，不经过 AI）。

安全与可回退（对应"AI 不能当硬闸门"）：
  · 动作只允许白名单里的值；JSON 解析失败、超时、动作非法 → **回退到原来的一刀切检索**，
    绝不因为规划失败就不回答；
  · `.env` 的 `QUERY_PLANNER=0` 一次性关掉整个层，行为与接入前完全一致；
  · 每次规划都留痕（动作/参数/依据/命中文档），进对话 meta 与 `logs/ai/graph_*.log`。

命令行的自检（不用跑 24 题）：
    python query_planner__ai.py --rules      # 看规则路由对一批问句的判定
    python query_planner__ai.py --overview   # 看当前仓库统计块
    python query_planner__ai.py --plan "库里有多少份文件？"   # 走完整规划（会调一次 graph 链）
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# =========================================================
# 动作白名单（模型只能从这些里选；执行器只认这些）
# =========================================================
ACTION_SPECS: dict[str, str] = {
    "count_docs": "统计**文档份数**（可按分类过滤）。本机 SQL，直接作答。",
    "list_docs": "列出文档清单（文件名/分类/页数）。本机 SQL，直接作答。",
    "count_facts": "统计事实/段落/特征等**库内规模**。本机 SQL，直接作答。",
    "count_edges": "统计图规模（节点/边/关系类型）。本机 SQL，直接作答。",
    "overview": "给一份**仓库总览**（文档/事实/图/登记/用户/占用）。本机 SQL，直接作答。",
    "lookup_field": "按字段名在**事实层**取具体值（如 合同金额/开票日期/扣款金额）。",
    "recall_docs": "按**需要的证据类别**召回文档（如 开票资料/扣款资料/付款资料/合同）。",
    "graph_traverse": "按编号/单号沿证据链图遍历取证（问题里有 HT-/FK-/KK-/发票号时用）。",
    "answer_from_common_sense": "先按**一般财务规则**作答（不依赖本库资料），再指出要看哪些资料。",
    "clarify": "问题口径不清（问的是哪个仓库/哪段时间/哪种单据），先反问口径再答。",
}

DIRECT_ACTIONS = ("count_docs", "list_docs", "count_facts", "count_edges", "overview")

# 证据类别 → 召回时匹配的（文档分类 / 文件名 / 概括）关键词
CLASS_HINTS: dict[str, tuple[str, ...]] = {
    "合同": ("合同", "协议"),
    "开票资料": ("发票", "开票信息"),
    "发票": ("发票",),
    "扣款资料": ("扣款通知", "扣款", "罚款", "违约金"),
    "付款资料": ("付款申请", "付款", "结算"),
    "结算资料": ("结算", "对账"),
    "物流签收": ("物流", "签收", "送货"),
    "台账": ("台账",),
}

# 规则路由：命中就直接本机统计作答（不进检索、不调 AI）
#
# ⚠️ 设计要点（本轮修正，实测教训）：**正则只用来"校验覆盖度"，不用来"理解问题"**。
#    早先的写法是"看到 多少 + 份 就当计数题"，结果 17 个刁钻问句里有 13 个被误判：
#      "上月新增了几份合同？" / "扣款金额超过 1000 的有几份？" / "有多少份文件还没入账？"
#      / "金额最大的那份合同是多少钱？" …
#    全都会被直接回一个**全量数字**（口径完全不同），比不答更糟。
#    现在的判据是"**闭合词表覆盖**"：
#      · 把问句切成词，凡是不在 `_VOCAB`（+本次命中的分类词）里的中文/字母/数字 → 视为**残留**；
#      · 有任何残留 → 这条问题**没被完全理解** → 一律交给 AI 规划（或反问口径），不直答。
#    于是"词表外的表达"天然落到 AI 那条路上：正则不理解的，绝不硬答。
_COUNT_WORDS = r"(多少|几|共|合计|总数|总计|一共|数量|有\d*几)"
_DOC_WORDS = r"(文件|文档|资料|份|张)"
_EDGE_WORDS = r"(边|关系|节点|证据链)"
_FACT_WORDS = r"(事实|字段值|段落|条目)"
_LIST_WORDS = r"(哪些|哪几|列出|清单|都有什么|有什么|列表)"
_OVERVIEW_WORDS = r"(概览|总览|总体情况|仓库(状态|里有什么)|库里有什么|情况怎么样)"
# 出现这些词说明是"要分析/要结论"的问题，绝不能当成计数题直答
_ANALYTIC_WORDS = re.compile(
    r"(为什么|为何|是否|是不是|能否|能不能|如何|怎么|怎样|哪些可能|还有什么可能|"
    r"区别|原因|依据|风险|建议|应该|等于|一致|合理|会影响|意味着)")

# 闭合词表：只有**完全由这些词（+命中的分类词）组成**的问句才允许直答。
# 这是"保守优先"的取舍——宁可多花一次 flash 规划，也不要回一个口径不对的数字。
_VOCAB_PHRASES = (
    "当前仓库", "当前库", "本机", "数据库", "仓库", "库里", "库内", "库中", "系统里", "这里",
    "一共", "总共", "总共有", "共有", "总共", "合计", "总计", "数量", "总数", "数目", "多少",
    "几个", "几份", "几条", "几个", "几", "多少份", "多少条", "多少张", "多少种", "数",
    "文件", "文档", "资料", "单据", "记录", "条目", "份", "张", "条", "个", "种",
    "合同", "协议", "发票", "开票", "开票信息", "扣款", "扣款通知", "通知", "付款",
    "付款申请", "结算", "结算表", "台账", "物流", "签收", "清单",
    "图", "图里", "节点", "边", "关系", "证据链", "事实", "段落", "字段", "特征",
    "总览", "概览", "总体", "情况", "状态", "内容", "有什么", "有哪些", "哪些", "什么",
    "列出", "列表", "列一下", "统计", "查看", "查", "看", "一下", "帮我", "请", "请问",
    "有", "是", "的", "了", "吗", "呢", "啊", "都", "里", "中", "内", "和", "与", "给",
    "我", "你", "它", "现在", "目前",
)
_VOCAB = frozenset(_VOCAB_PHRASES)
# 标点/空白：**子句分隔符一律视为残留**（多子句问题通常不止一个诉求）
_PUNCT = "，。、；;,.!！?？:：（）()【】[]“”\"'‘’ \t\n\r"
_RESIDUE_CHARS = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]")


def _residue(question: str, extra: tuple[str, ...] = ()) -> str:
    """把问句按闭合词表切分，返回**没被解释**的部分（最长匹配优先）。

    返回空串 = 这条问题完全落在词表内（可以被规则安全处理）；
    返回非空 = 有词表外的内容（条件/分析/歧义）→ 交给 AI，不要直答。
    """
    q = (question or "").strip()
    vocab = _VOCAB | {w for w in extra if w}
    out: list[str] = []
    i = 0
    while i < len(q):
        ch = q[i]
        if ch in _PUNCT:
            if ch in "，。、；;":
                out.append(ch)      # 子句分隔符算残留（多诉求）
            i += 1
            continue
        hit = ""
        for n in range(min(6, len(q) - i), 0, -1):
            piece = q[i:i + n]
            if piece in vocab:
                hit = piece
                break
        if hit:
            i += len(hit)
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def rules_forced_off() -> bool:
    """`QUERY_PLANNER_RULES=0`：连规则快路都不用，所有问题都交给 AI 规划（最保守）。"""
    return os.getenv("QUERY_PLANNER_RULES", "1").strip().lower() in ("0", "false", "no", "off")


_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
           "八": 8, "九": 9, "十": 10}


def planner_enabled() -> bool:
    """总开关：`.env` 的 QUERY_PLANNER（默认开）。"""
    return os.getenv("QUERY_PLANNER", "1").strip().lower() not in ("0", "false", "no", "off")


# =========================================================
# 本机统计**不支持**的条件（时间范围/阈值/去重/状态筛选…）。
# 出现这些词时，即便动作是 count_docs，也**不能**把全量数字当成用户要的答案：
# 要么老实说"这个口径本机不支持"，要么把 AI 规划改成 recall/clarify。
_UNSUPPORTED_COND = re.compile(
    r"(上月|本月|上个月|这个月|去年|今年|上季度|本季度|上周|本周|最近|最新|今日|今天|"
    r"超过|大于|小于|不低于|不超过|至少|至多|多于|少于|等于|不等于|"
    r"未[入账开票收付]|没[有入账开票收付]|已[入账开票收付]|待[入账开票收付]|"
    r"重复|唯一|去重|最大|最小|平均|占比|比例|差异|不一致|对不上|缺失|异常|"
    r"扫描|类型|分别|各自|趋势|对比|挂账|作废|重开|红冲)")


def _unsupported_condition(question: str) -> str:
    m = _UNSUPPORTED_COND.search(question or "")
    return m.group(0) if m else ""
# =========================================================
def _db():
    from database_serv__infra import get_connection

    return get_connection()


def _hub_dir() -> Path:
    try:
        from workspace__infra import hub_root

        return hub_root()
    except Exception:
        return ROOT / "hub"


def overview() -> dict:
    """当前仓库统计：文档/事实/图/登记/用户/hub 文件 + 分类分布。"""
    out: dict = {"docs": 0, "by_category": {}, "facts": 0, "segments": 0,
                 "feature_rows": 0, "nodes": 0, "edges": 0, "users": 0,
                 "entities": 0, "hub_files": 0, "chunks": 0, "workspace": ""}
    try:
        import workspace__infra as ws

        ent = ws.active() or {}
        out["workspace"] = ent.get("name") or ent.get("slug") or ""
    except Exception:
        pass
    try:
        with _db() as conn, conn.cursor() as cur:
            def one(sql: str) -> int:
                try:
                    cur.execute(sql)
                    return int(cur.fetchone()[0] or 0)
                except Exception:
                    conn.rollback()
                    return 0

            out["docs"] = one("SELECT COUNT(*) FROM l1_documents")
            cur.execute("SELECT COALESCE(category,'其它'), COUNT(*) FROM l1_documents "
                        "GROUP BY 1 ORDER BY 2 DESC")
            out["by_category"] = {r[0]: int(r[1]) for r in cur.fetchall()}
            out["facts"] = one("SELECT COUNT(*) FROM l1_facts")
            out["segments"] = one("SELECT COUNT(*) FROM l1_segments")
            out["feature_rows"] = one("SELECT COUNT(*) FROM file_feature_hashes")
            out["nodes"] = one("SELECT COUNT(*) FROM graph_nodes")
            out["edges"] = one("SELECT COUNT(*) FROM graph_edges")
            out["users"] = one("SELECT COUNT(*) FROM sys_users")
            out["chunks"] = one("SELECT COUNT(*) FROM document_chunks")
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    hub = _hub_dir()
    if hub.exists():
        out["hub_files"] = sum(
            1 for p in hub.rglob("*.json")
            if not p.name.endswith((".features.json", ".l1.json"))
            and p.parent.name != "_mapping")
    p = ROOT / "logs"
    out["logs_mb"] = round(sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                           / 1048576, 2) if p.exists() else 0.0
    return out


def render_overview(ov: dict | None = None) -> str:
    ov = ov or overview()
    cats = "；".join(f"{k} {v}" for k, v in (ov.get("by_category") or {}).items()) or "（无）"
    ws_name = ov.get("workspace") or "（未登记）"
    return (
        "【本机统计·当前仓库】\n"
        f"仓库：{ws_name}｜hub 文档 {ov['docs']} 份｜hub 文件 {ov['hub_files']} 个\n"
        f"分类分布：{cats}\n"
        f"事实 {ov['facts']} 条｜段落 {ov['segments']} 段｜特征行 {ov['feature_rows']} 条｜"
        f"图 {ov['nodes']} 节点 / {ov['edges']} 边｜向量块 {ov['chunks']}｜"
        f"用户 {ov['users']} 个｜诊断日志 {ov.get('logs_mb', 0)} MB\n"
        "口径说明：以上是**本机数出来的**（不经过 AI），计数范围仅当前仓库；"
        "取具体金额/日期请回原文核对。"
    )


def short_stats() -> str:
    """一行"本机统计"（随证据一起注入）。

    为什么每次取证都带这一行：只要模型**知道真实份数**，它就不会因为"没走计数动作"
    而答"无法得知/没有找到证据链"。实测漏判场景（规则没接住 → AI 规划）里，
    模型有可能选 graph_traverse 或纯常识作答——这一行保证份数信息**永远在场**。
    """
    try:
        ov = overview()
    except Exception:
        return ""
    cats = "；".join(f"{k} {v}" for k, v in (ov.get("by_category") or {}).items())
    return (f"【本机统计·可直接引用】当前仓库 {ov.get('workspace') or '未登记'}："
            f"文档 {ov['docs']} 份（{cats or '无分类'}）｜事实 {ov['facts']} 条｜"
            f"图 {ov['nodes']} 节点 / {ov['edges']} 边。"
            f"（口径：已入库的脱敏产物，不是源目录文件数）")


def system_context() -> str:
    """给 system 提示词的一小段"当前仓库规模"（让 AI 说话有边界、不瞎猜份数）。"""
    try:
        ov = overview()
    except Exception:
        return ""
    cats = "；".join(f"{k} {v}" for k, v in (ov.get("by_category") or {}).items())
    return ("\n\n【当前仓库规模（本机统计，可直接引用）】\n"
            f"文档 {ov['docs']} 份（{cats or '无分类'}）｜事实 {ov['facts']} 条｜"
            f"图 {ov['nodes']} 节点 / {ov['edges']} 边。\n"
            "注意：这是**库内已入库的脱敏产物**的数量，不是源目录里的文件数；"
            "涉及具体金额/日期/条款时必须回原文（参考材料）核对。")


def list_docs(limit: int = 60, category: str = "") -> dict:
    rows = []
    try:
        with _db() as conn, conn.cursor() as cur:
            if category:
                cur.execute("SELECT doc_key, COALESCE(category,'其它'), page_count, doc_summary "
                            "FROM l1_documents WHERE category LIKE %s ORDER BY doc_no LIMIT %s",
                            (f"%{category}%", limit))
            else:
                cur.execute("SELECT doc_key, COALESCE(category,'其它'), page_count, doc_summary "
                            "FROM l1_documents ORDER BY doc_no LIMIT %s", (limit,))
            for r in cur.fetchall():
                rows.append({"doc_key": r[0], "category": r[1], "pages": int(r[2] or 0),
                             "summary": (r[3] or "").strip()})
    except Exception as exc:
        return {"rows": [], "error": f"{type(exc).__name__}: {exc}"}
    return {"rows": rows}


# =========================================================
# 规则路由（零 token 快路）
# =========================================================
def route_by_rules(question: str) -> dict | None:
    """明显且**能被完全理解**的问题直接定动作；否则返回 None（交给 AI 判断）。

    两道闸门缺一不可：
      ① 形态闸门：命中计数/清单/总览的形态（否则根本不是这类问题）；
      ② 覆盖闸门：问句里除了词表与本次分类词，**没有别的字**（否则说明带了条件、
         分析或第二个诉求，规则理解不了 → 必须交 AI）。
    """
    q = (question or "").strip()
    if not q or _ANALYTIC_WORDS.search(q):
        return None
    if rules_forced_off():
        return None

    def ok(extra: tuple[str, ...] = ()) -> bool:
        return not _residue(q, extra)

    if re.search(_OVERVIEW_WORDS, q) and ok():
        return {"action": "overview", "params": {}, "needed_evidence": [],
                "source": "rule", "reason": "问的是仓库总体情况（问句完全在词表内）"}
    if re.search(_COUNT_WORDS, q) and re.search(_EDGE_WORDS, q) and ok(("边", "节点", "关系")):
        return {"action": "count_edges", "params": {}, "needed_evidence": [],
                "source": "rule", "reason": "问图/边的规模"}
    if re.search(_COUNT_WORDS, q) and re.search(_FACT_WORDS, q) and ok(("事实", "段落")):
        return {"action": "count_facts", "params": {}, "needed_evidence": [],
                "source": "rule", "reason": "问事实/段落规模"}
    if re.search(_COUNT_WORDS, q) and re.search(_DOC_WORDS, q):
        cat = ""
        for key in ("合同", "发票", "付款", "扣款", "结算", "开票"):
            if key in q:
                cat = key
                break
        if ok((cat,) if cat else ()):
            return {"action": "count_docs", "params": {"category": cat}, "needed_evidence": [],
                    "source": "rule", "reason": "问文档份数（本机 SQL 可答）"}
        return None
    if re.search(_LIST_WORDS, q) and re.search(_DOC_WORDS, q) and ok():
        return {"action": "list_docs", "params": {}, "needed_evidence": [],
                "source": "rule", "reason": "要文档清单（本机 SQL 可答）"}
    return None


# =========================================================
# AI 规划（graph 链路：它是"查询/取证"链路，独立 key，flash 快）
# =========================================================
PLANNER_SYSTEM = """你是财务文档问答的**需求判断**模块。只做一件事：判断这个问题**该用什么动作**去取答案，不回答问题本身。

可用动作（action 只能从下列取值）：
{actions}

规则：
1. 问"有多少份/几份/哪些文件/仓库里有什么"这类**规模与清单**问题 → 用 count_docs / list_docs /
   count_edges / count_facts / overview（本机能直接数，**不要**去图里找证据链）。
2. 问题里出现编号/单号（HT-、FK-、KK-、发票号、PJ/CO 编号）且要追溯关系 → graph_traverse。
3. 需要**具体金额/日期/条款**才能答的问题 → recall_docs，并在 needed_evidence 里写清要哪几类资料
   （从这些里选：合同、开票资料、发票、扣款资料、付款资料、结算资料、物流签收、台账）。
   例如"发票金额是否一定等于合同金额/不等还有哪些可能" → needed_evidence 至少含
   ["开票资料","扣款资料","付款资料"]，并把 also_common_sense 设为 true（先讲一般规则再核对本库）。
4. 问题本身可以不看资料就答（一般性规则、概念解释）→ answer_from_common_sense，
   同时**尽量**在 needed_evidence 里给出"要证实它需要看哪些资料"。
5. 口径不清（哪个仓库/哪段时间/哪种单据）→ clarify，并在 reason 里写要问清什么。

只输出 JSON，不要解释、不要 Markdown 代码块，形如：
{{"action": "recall_docs", "params": {{"field": "开票金额"}},
  "needed_evidence": ["开票资料", "扣款资料"], "also_common_sense": true,
  "reason": "需要发票与扣款资料才能判断金额差异"}}
"""


def _actions_block() -> str:
    return "\n".join(f"- {k}：{v}" for k, v in ACTION_SPECS.items())


def plan_by_ai(question: str, *, overview_text: str = "") -> dict | None:
    """调一次 graph 链路做规划；任何失败都返回 None（调用方回退老路径）。"""
    try:
        from ai_client__ai import complete

        user = (f"【当前仓库规模】\n{overview_text or render_overview()}\n\n"
                f"【用户问题】\n{question}")
        content, reasoning = complete(
            "graph", PLANNER_SYSTEM.format(actions=_actions_block()), user,
            temperature=0.0, max_tokens=600,
        )
        data = _parse_json(content) or _parse_json(reasoning)
        if not isinstance(data, dict):
            return None
        action = str(data.get("action") or "").strip()
        if action not in ACTION_SPECS:
            return None
        ev = data.get("needed_evidence") or []
        if isinstance(ev, str):
            ev = [ev]
        return {
            "action": action,
            "params": data.get("params") if isinstance(data.get("params"), dict) else {},
            "needed_evidence": [str(x) for x in ev if str(x).strip()][:6],
            "also_common_sense": bool(data.get("also_common_sense")),
            "reason": str(data.get("reason") or "")[:200],
            "source": "ai",
        }
    except Exception:
        return None    # 超时/未配 key/JSON 坏 → 交给调用方回退


def _parse_json(text: str) -> dict | None:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"```\s*$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def plan(question: str, *, allow_ai: bool = True) -> dict | None:
    """需求判断总入口：规则优先，其次 AI；总开关关掉时直接返回 None（老行为）。"""
    if not planner_enabled():
        return None
    q = (question or "").strip()
    if not q:
        return None
    by_rule = route_by_rules(q)
    if by_rule:
        return by_rule
    if not allow_ai:
        return None
    return plan_by_ai(q)


# =========================================================
# 执行器（按动作取证；不在这里作答，除了计数类可以直答）
# =========================================================
def _recall_by_classes(classes: list[str], question: str, *, per_class: int = 3) -> dict:
    """按证据类别召回文档（索引卡 + 类别关键词），返回 {docs, text}。"""
    picked: list[dict] = []
    seen: set[str] = set()
    try:
        import doc_index__graph as di

        cards = di.load_cards(with_facts=False)
    except Exception as exc:
        return {"docs": [], "text": f"（按类别召回失败：{type(exc).__name__}: {exc}）"}
    for cls in classes:
        hints = CLASS_HINTS.get(cls) or (cls,)
        matched = [c for c in cards
                   if any(h in (c.get("category") or "") + c["doc_key"]
                          + (c.get("summary") or "") for h in hints)]
        # 同类别里再按与问题的机械相关度排一下（索引卡打分，零 token）
        try:
            sig_cards = [{"doc_no": c["doc_no"], "score": di.score_card(question, c),
                          "card": c} for c in matched]
            sig_cards.sort(key=lambda x: -x["score"])
            matched = [x["card"] for x in sig_cards]
        except Exception:
            pass
        for c in matched[:per_class]:
            if c["doc_key"] in seen:
                continue
            seen.add(c["doc_key"])
            picked.append({"class": cls, "doc_key": c["doc_key"],
                           "category": c.get("category") or "",
                           "summary": (c.get("summary") or "")[:120]})
    if not picked:
        return {"docs": [], "text": "（按类别没有召回到文档：库里可能没有这类资料）"}
    key_facts: dict[str, list[str]] = {}
    try:
        with _db() as conn, conn.cursor() as cur:
            for d in picked[:8]:
                cur.execute("SELECT header, value FROM l1_facts WHERE doc_key = %s "
                            "AND header IS NOT NULL AND header <> '' LIMIT 60", (d["doc_key"],))
                rows = [(h, str(v)) for h, v in cur.fetchall()]
                keep = [f"{h}={v}" for h, v in rows
                        if re.search(r"(金额|合计|总额|价税|税额|扣款|付款|日期|编号|账号|开户)",
                                     h or "")][:8]
                key_facts[d["doc_key"]] = keep or [f"{h}={v}" for h, v in rows[:5]]
    except Exception:
        pass
    lines = ["【候选证据·按类别召回（取数请回原文核对）】"]
    for d in picked:
        lines.append(f"- [{d['class']}] {d['doc_key']}（分类 {d['category']}）：{d['summary']}")
        for kf in key_facts.get(d["doc_key"], [])[:8]:
            lines.append(f"    · {kf}")
    return {"docs": picked, "text": "\n".join(lines)}


def _graph_text(question: str) -> dict:
    try:
        from graph_query__graph_walk import evidence_block, query as graph_query

        g = graph_query(question)
        return {"docs": [], "chains": len(g.get("chains") or []),
                "text": evidence_block(g)}
    except Exception as exc:
        return {"docs": [], "chains": 0,
                "text": f"（图遍历取证失败：{type(exc).__name__}: {exc}）"}


def _lookup_field(field: str, *, limit: int = 30) -> dict:
    if not field:
        return {"docs": [], "text": "（未指定要查的字段）"}
    try:
        with _db() as conn, conn.cursor() as cur:
            cur.execute("SELECT doc_key, header, value FROM l1_facts "
                        "WHERE header LIKE %s LIMIT %s", (f"%{field}%", limit))
            rows = cur.fetchall()
    except Exception as exc:
        return {"docs": [], "text": f"（字段查询失败：{type(exc).__name__}: {exc}）"}
    if not rows:
        return {"docs": [], "text": f"（事实层没有匹配「{field}」的字段）"}
    lines = [f"【候选证据·字段「{field}」（取数请回原文核对）】"]
    docs = []
    for dk, h, v in rows:
        lines.append(f"- {dk}｜{h} = {v}")
        if dk not in docs:
            docs.append(dk)
    return {"docs": docs, "text": "\n".join(lines)}


def execute(plan: dict) -> dict:
    """按动作取证。返回 {direct, text, hits, answer_directly}。"""
    action = plan.get("action") or ""
    params = plan.get("params") or {}
    classes = plan.get("needed_evidence") or []
    question = plan.get("question") or ""
    hits: list[str] = []

    # 计数/清单类动作的守门：问题里带**本机统计不支持的条件**（时间范围、金额阈值、
    # 去重、状态筛选…）时，绝不能把"全量数字"当成答案端出去。
    # 实测踩坑：早先"上月新增了几份合同？"会被当成 count_docs，直接回"共 18 份"——
    # 口径完全不同，比不答更糟。现在改成：明确说清不支持 + 附全量口径。
    if action in DIRECT_ACTIONS and action not in ("clarify", "overview"):
        cond = _unsupported_condition(question)
        if cond:
            ov = overview()
            return {"direct": True, "hits": [],
                    "text": (f"⚠️ 这个问题带了「{cond}」这个条件，**本机统计目前不支持按条件过滤**"
                             f"（时间范围 / 金额阈值 / 去重 / 状态筛选都还没有接）；\n"
                             f"所以我不能把这个数字当成你要的答案。仅供参考的**全量口径**："
                             f"当前仓库共 {ov['docs']} 份文档（{ov.get('workspace') or '未登记'}）。\n"
                             f"要按条件统计，需要把筛选口径做成可执行的动作（可以加），"
                             f"或改用召回资料后由 AI 核对。")}
    if action == "overview":
        return {"direct": True, "text": render_overview(), "hits": []}
    if action == "count_docs":
        ov = overview()
        cat = str(params.get("category") or "").strip()
        if cat:
            n = int((ov.get("by_category") or {}).get(cat, 0))
            # 分类名可能带后缀（如"扣款通知"），退化为包含匹配
            if n == 0:
                n = sum(v for k, v in (ov.get("by_category") or {}).items() if cat in k)
            text = (f"【本机统计】库里共有 {ov['docs']} 份文档，其中「{cat}」类 {n} 份。\n"
                    f"（口径：当前仓库 {ov.get('workspace') or '未登记'} 已入库的脱敏产物；"
                    f"不是源目录文件数）")
        else:
            text = f"【本机统计】库里共有 {ov['docs']} 份文档。\n" + render_overview(ov)
        return {"direct": True, "text": text, "hits": []}
    if action == "count_edges":
        ov = overview()
        return {"direct": True,
                "text": (f"【本机统计】图里有 {ov['nodes']} 个节点、{ov['edges']} 条边"
                         f"（当前仓库；边是 edge AI 判过并本地复核的）"), "hits": []}
    if action == "count_facts":
        ov = overview()
        return {"direct": True,
                "text": (f"【本机统计】事实层 {ov['facts']} 条事实、{ov['segments']} 个段落、"
                         f"{ov['feature_rows']} 条特征哈希（当前仓库）"), "hits": []}
    if action == "list_docs":
        res = list_docs(category=str(params.get("category") or ""))
        rows = res.get("rows") or []
        if not rows:
            return {"direct": True, "text": "【本机统计】库里没有文档。", "hits": []}
        lines = [f"【本机统计】库里共 {len(rows)} 份文档（最多列 60 份）："]
        for r in rows:
            lines.append(f"- {r['doc_key']}｜{r['category']}｜{r['pages']} 页")
        return {"direct": True, "text": "\n".join(lines), "hits": [r["doc_key"] for r in rows]}

    if action == "clarify":
        return {"direct": True,
                "text": ("这个问题我需要先确认口径再答：" + (plan.get("reason") or "") +
                         "\n（例如：只看当前仓库还是全部仓库？哪个时间段？哪种单据？）"),
                "hits": []}

    if action == "graph_traverse":
        res = _graph_text(question)
        # 图遍历回答不了"多少份"这类规模问题：把本机统计一起给模型，别让它答"查不到"
        return {"direct": False, "hits": res.get("docs") or [],
                "text": (short_stats() + "\n\n" + res["text"]).strip()}

    if action == "lookup_field":
        res = _lookup_field(str(params.get("field") or ""))
        return {"direct": False, "hits": res.get("docs") or [],
                "text": (short_stats() + "\n\n" + res["text"]).strip()}

    # answer_from_common_sense / recall_docs：先讲规则（由提示词负责），再按类别召回
    text = ""
    if classes:
        res = _recall_by_classes(classes, question)
        text = res["text"]
        hits = [d["doc_key"] for d in res.get("docs") or []]
    if plan.get("also_common_sense") or action == "answer_from_common_sense":
        text = ("【作答要求·先规则后本库】先按一般财务规则回答（例如是否存在分次开票、"
                "扣款/罚款、部分付款、税额与价税合计之差等情形），再明确区分"
                "「一般来说…」与「在你库里我看到…」；资料未覆盖的部分说清楚，不要编造。\n" + text)
    if not text:
        text = "（需求判断没有给出可执行的取证动作，按无资料作答）"
    # 任何"非直答"路径都带上真实规模，避免模型因拿不到份数而含糊其辞
    return {"direct": False, "hits": hits,
            "text": (short_stats() + "\n\n" + text).strip()}


def meta_of(plan: dict | None) -> dict:
    """写进对话 meta 的规划留痕（审计用）。"""
    if not plan:
        return {}
    return {"planner_action": plan.get("action", ""),
            "planner_source": plan.get("source", ""),
            "planner_reason": plan.get("reason", ""),
            "planner_evidence": plan.get("needed_evidence") or []}


# =========================================================
# 命令行自检
# =========================================================
_RULE_PROBES = [
    "库里有多少份文件？",
    "一共几份合同？",
    "有哪些文件？",
    "图里有多少条边？",
    "库里有多少条事实？",
    "仓库里有什么？",
    "发票金额是否一定等于合同金额？如果不等于，还有哪些其他可能",
    "为什么这个月扣款这么多？",
    "HT-2026-0001 这个合同都关联了哪些单据？",
]


def main(argv: list[str] | None = None) -> int:
    import argparse

    try:      # 命令行直跑时也按 .env 生效（否则开关显示成"未设"）
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=ROOT / ".env")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="问答需求判断：规则路由 / 仓库统计 / 完整规划")
    ap.add_argument("--rules", action="store_true", help="只看规则路由判定")
    ap.add_argument("--overview", action="store_true", help="打印当前仓库统计块")
    ap.add_argument("--plan", default="", help="对这句话走完整规划（会调一次 graph 链路）")
    args = ap.parse_args(argv)

    if args.overview:
        print(render_overview())
        return 0
    if args.plan:
        p = plan(args.plan)
        print("规划结果：", json.dumps(p, ensure_ascii=False, indent=1))
        if p:
            r = execute({**p, "question": args.plan})
            print("\n执行结果（direct=%s）：\n%s" % (r["direct"], r["text"][:1200]))
        return 0
    print("规则路由（零 token 快路）：")
    for q in _RULE_PROBES:
        r = route_by_rules(q)
        print(f"  {q}\n      → {r['action'] if r else '（不定，交给 AI 规划）'}"
              f"{'｜' + r['reason'] if r else ''}")
    print("\n总开关 QUERY_PLANNER =", os.getenv("QUERY_PLANNER", "(未设，默认开)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
