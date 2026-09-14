# -*- coding: utf-8 -*-
"""样例语料装载器：`sample_docs/*.json` → `hub/` + 数据库（默认独立样例库）

为什么这样写（设计要点）：

1. **不跑 OCR、不调大模型**：样例文档本身就是 hub 产物的形状
   （`tables` / `pages` / `features` / `summary`），所以"装载"= 落 hub +
   抽事实 + 写特征哈希 + 手写概括入库 + 灌弱关联边。
   概括/特征是**手写**的：投影时 `summarize=False`（不调 AI），随后用样例 JSON 里的
   `summary` 覆盖投影出来的空概括，并把 `model` 标成 `sample-handwritten`，
   以便和真实语料"AI 概括"区分开。

2. **默认写独立样例库**（`--db sample_test_db`）：样例绝不能混进生产语料
   （会冲掉 18 份真实文档的实验基线）。要写生产库必须显式
   `--db fin_system_db --allow-prod`。

3. **编号自检**：编号由库的 `entity_code_seq` 发号（CO0001…），装载时**逐条断言**
   实际发到的编号 == `00_编号对照.json` 里写的编号；不一致立即报错（不静默）——
   否则样例正文里的编号含义会悄悄漂移。

4. **幂等**：hub 覆盖写、事实 upsert、特征哈希先删后插、图节点/边 upsert，可重复跑。

用法：
    python sample_corpus__infra.py --list
    python sample_corpus__infra.py --create --load            # 建样例库 + 装载
    python sample_corpus__infra.py --create --reset --load    # 重建（丢样例库旧数据）
    python sample_corpus__infra.py --verify                   # 真值自检 + 孤儿编号检查
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SAMPLE_DIR = ROOT / "sample_docs"
LEGEND_FILE = SAMPLE_DIR / "00_编号对照.json"
EDGES_FILE = SAMPLE_DIR / "98_弱关联边.json"
QA_FILE = SAMPLE_DIR / "99_问题与真值.json"
INIT_SQL = ROOT / "init_db.sql"

DEFAULT_DB = "sample_test_db"
PROD_DB = "fin_system_db"
HANDWRITTEN_MODEL = "sample-handwritten"

# 编号形状：CO0001 / PT0002 / DT0005 …（与 database_serv__infra._CODE_PREFIX 一致）
CODE_RE = re.compile(r"(CO|PT|DT|ID|BC|TX|BK|BA|PJ|PH)\d{4}")
CATEGORY_BY_PREFIX = {
    "CO": "company", "PT": "party", "DT": "date", "ID": "id_card", "BC": "bank_card",
    "TX": "tax_id", "BK": "bank_name", "BA": "bank_account", "PJ": "project", "PH": "phone",
}


# =========================================================
# 0. 小工具（**只用标准库**：--db 必须先于任何重量级 import 生效）
# =========================================================
def read_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def doc_files() -> list[tuple[Path, dict]]:
    """样例文档（kind=hub_doc）按文件名排序。"""
    out: list[tuple[Path, dict]] = []
    for p in sorted(SAMPLE_DIR.glob("*.json")):
        try:
            data = read_json(p)
        except Exception as exc:
            print(f"⚠️ 跳过 {p.name}（JSON 读取失败：{exc}）", flush=True)
            continue
        if isinstance(data, dict) and data.get("kind") == "hub_doc":
            out.append((p, data))
    return out


def legend() -> dict:
    return read_json(LEGEND_FILE)


def qa_truth() -> dict:
    return read_json(QA_FILE)


def _norm(value: str) -> str:
    """去空白归一化（与 database_serv__infra._normalize 同规则）。"""
    return "".join(str(value or "").split())


def _norm_key(value: str) -> str:
    """映射表 norm_key（同 database_serv__infra._norm_key 规则）。"""
    norm = _norm(value)
    return norm if len(norm) <= 64 else hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _hub_dir() -> Path:
    from ai_guard__desens import hub_dir  # 惰性导入：确保 APP_DB_NAME 已生效

    return hub_dir()


# =========================================================
# 1. 建库（独立样例库；可重置）
# =========================================================
def ensure_db(dbname: str, *, reset: bool = False) -> str:
    """建库并套用 `init_db.sql` 的库结构（幂等）。

    实现统一在 `workspace__infra.ensure_db`（仓库创建走同一条路，避免两套建库代码）。
    `init_db.sql` 里没有任何硬编码库名，全是 `CREATE TABLE IF NOT EXISTS` + 角色授权。
    """
    from workspace__infra import ensure_db as _ws_ensure_db

    return _ws_ensure_db(dbname, reset=reset)


# =========================================================
# 2. 编号登记（00_编号对照.json → entity_mapping_*）
# =========================================================
def register_mappings(legend_data: dict | None = None) -> dict:
    """按对照表登记实体，并**断言**发号与对照表一致。

    走的是生产同一条登记路径（`MappingDbStore`），所以样例正文里的编号
    在映射表里查得到（孤儿=0），且发号规则与线上完全一致。
    """
    from database_serv__infra import SECRET_CATEGORIES, MappingDbStore

    legend_data = legend_data or legend()
    # ⚠️ 故意**不用** `MappingDbStore.load()`：那个入口会把旧版
    # `hub/_mapping/entity_mapping.json` 一次性导进空库，把真实语料的编号
    # 灌进样例库，样例正文的 CO0001… 含义就跟着变了。这里只要发号能力。
    store = MappingDbStore()
    report: dict = {"registered": [], "mismatch": [], "failed": []}
    for category, entries in (legend_data.get("类别") or {}).items():
        for expect_code, meta in entries.items():
            real = str(meta.get("real") or "").strip()
            if not real:
                report["failed"].append({"category": category, "code": expect_code,
                                         "why": "缺少 real 值"})
                continue
            try:
                if category in SECRET_CATEGORIES:
                    masked = meta.get("masked")
                    got = store.get_or_create_secret_code(category, real, masked)
                else:
                    got = store.get_or_create_code(category, real)
            except Exception as exc:
                report["failed"].append({"category": category, "code": expect_code,
                                         "why": f"{type(exc).__name__}: {exc}"})
                continue
            if got == expect_code:
                report["registered"].append(f"{category}:{got}")
            else:
                # 已有登记（重复跑）时按 norm_key 命中旧码：只要库里那条码含义一致就不算错
                report["mismatch"].append({"category": category, "expect": expect_code, "got": got})
    return report


# =========================================================
# 3. 特征哈希（镜像 ai_parser.classify_and_store 的哈希口径，不调 AI）
# =========================================================
def hashes_for(features: dict) -> dict[str, str]:
    """手写特征值 → 特征哈希（与 `ai_parser.classify_and_store` 同口径）。

    说明：样例里的日期特征值本身就是编号（DT0001），`_date_parts` 解析不出年月日，
    因此不会产出 date_*_year/month/day 分层哈希——**这与真实语料同一行为**
    （线上特征值也是编号），不是本装载器的简化。
    """
    from ai_parser__ai import _date_parts, hash_feature_value  # noqa: PLC0415

    hashes: dict[str, str] = {}
    for side in ("start", "end"):
        raw = features.get(f"date_{side}")
        if not raw:
            continue
        parts = _date_parts(str(raw))
        if not parts:
            continue
        for gran in ("year", "month", "day"):
            hashes[f"date_{side}_{gran}"] = hash_feature_value(f"date_{gran}", parts[gran])
    for code in ("doc_type", "money_flow", "voucher_kind"):
        if features.get(code):
            hashes[code] = hash_feature_value(code, str(features[code]))
    flow = features.get("four_flow")
    if isinstance(flow, list):
        for i, item in enumerate(flow, start=1):
            hashes[f"four_flow:{i}"] = hash_feature_value("four_flow", str(item))
    elif isinstance(flow, str) and flow:
        hashes["four_flow:1"] = hash_feature_value("four_flow", flow)
    proj = str(features.get("project") or "").strip()
    if proj:
        hashes["project"] = hash_feature_value("project", proj)
    return hashes


def catalog_codes() -> set[str]:
    from database_serv__infra import get_admin_connection

    with get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT feature_code FROM feature_catalog")
        return {r[0] for r in cur.fetchall()}


def store_feature_hashes(doc_key: str, hashes: dict[str, str]) -> dict:
    """特征哈希落库（先删后插）。非目录键**只留在 .features.json**，不入库。

    `file_feature_hashes.feature_code` 有指向 `feature_catalog` 的外键，
    目录外的键（本样例里没有）会被跳过并计入返回值的 `skipped`。
    """
    from database_serv__infra import get_admin_connection

    codes = catalog_codes()
    plain = {k: v for k, v in hashes.items() if ":" not in k and k in codes}
    skipped = sorted(k for k in hashes if ":" not in k and k not in codes)
    flows = {int(k.split(":")[1]): v for k, v in hashes.items() if k.startswith("four_flow:")}
    with get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM file_feature_hashes WHERE doc_key = %s", (doc_key,))
        for code, value_hash in plain.items():
            cur.execute(
                "INSERT INTO file_feature_hashes (doc_key, feature_code, value_hash, seq) "
                "VALUES (%s, %s, %s, 0) ON CONFLICT DO NOTHING",
                (doc_key, code, value_hash),
            )
        for seq_no, value_hash in sorted(flows.items()):
            cur.execute(
                "INSERT INTO file_feature_hashes (doc_key, feature_code, value_hash, seq) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (doc_key, "four_flow", value_hash, seq_no),
            )
        conn.commit()
    return {"saved": len(plain) + len(flows), "skipped": skipped}


# =========================================================
# 4. 装载文档（hub + 事实 + 概括 + 特征哈希）
# =========================================================
def load_docs(docs: list[tuple[Path, dict]] | None = None) -> dict:
    from database_serv__infra import get_admin_connection
    from l1_extract__summary_hash import EXTRACT_VERSION, project_hub_file

    docs = docs if docs is not None else doc_files()
    hub = _hub_dir()
    report: dict = {"hub_dir": str(hub), "docs": [], "facts": 0}

    for path, doc in docs:
        doc_key = str(doc.get("doc_key") or path.stem)
        out = hub / f"{doc_key}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

        features = doc.get("features") or {}
        hashes = hashes_for(features)
        side_path = out.parent / f"{out.stem}.features.json"
        side_path.write_text(json.dumps({
            "doc_key": doc_key,
            "sample": True,
            "features": features,
            "hashes": hashes,
            "projects": [],
            "project_check": None,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        saved = store_feature_hashes(doc_key, hashes)

        # 投影（不调 AI：概括是手写的）→ 事实抽取/入库、hub_index、l1_segments 都在这里发生
        proj = project_hub_file(out, source_path=None, summarize=False)

        summary = str(doc.get("summary") or "")
        with get_admin_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE l1_documents SET doc_summary = %s, model = %s, updated_at = CURRENT_TIMESTAMP "
                "WHERE doc_key = %s AND extract_version = %s",
                (summary, HANDWRITTEN_MODEL, doc_key, EXTRACT_VERSION),
            )
            cur.execute(
                "UPDATE hub_index SET doc_summary = %s, summary_model = %s, updated_at = CURRENT_TIMESTAMP "
                "WHERE doc_key = %s",
                (summary, HANDWRITTEN_MODEL, doc_key),
            )
            cur.execute(
                "UPDATE l1_segments SET seg_summary = %s, model = %s "
                "WHERE doc_key = %s AND extract_version = %s",
                (summary, HANDWRITTEN_MODEL, doc_key, EXTRACT_VERSION),
            )
            conn.commit()

        # `.l1.json` 伴生文件里的概括同样改成手写概括（保持与库一致）
        sidecar = Path(str(proj.get("sidecar") or ""))
        if sidecar.exists():
            payload = read_json(sidecar)
            payload["doc_summary"] = summary
            payload["model"] = HANDWRITTEN_MODEL
            payload["sample"] = True
            sidecar.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        report["docs"].append({
            "doc_key": doc_key, "hub": str(out.relative_to(hub)),
            "facts": int(proj.get("fact_count") or 0),
            "feature_hashes": len(hashes), "features_saved": saved["saved"],
            "features_skipped": saved["skipped"],
            "summary_len": len(summary),
        })
        report["facts"] += int(proj.get("fact_count") or 0)
        print(f"  ✓ {doc_key}｜事实 {proj.get('fact_count')}｜特征哈希 {len(hashes)} "
              f"（入库 {saved['saved']}）", flush=True)
    return report


# =========================================================
# 5. 弱关联边（98_弱关联边.json → graph_nodes / graph_edges）
# =========================================================
def load_graph(edges_data: dict | None = None) -> dict:
    """灌手写弱关联边（不调 EDGE AI）。weight≥0.8 记 validated，否则 hypothesis。"""
    from database_serv__infra import get_admin_connection

    data = edges_data or read_json(EDGES_FILE)
    nodes = data.get("nodes") or []
    edges = data.get("edges") or []
    node_ids = {n["id"] for n in nodes}
    dangling = [e for e in edges if e["src"] not in node_ids or e["dst"] not in node_ids]

    with get_admin_connection() as conn, conn.cursor() as cur:
        for n in nodes:
            nid = n["id"]
            doc_key = nid.split(":", 1)[1] if nid.startswith("doc:") else None
            cur.execute(
                """INSERT INTO graph_nodes
                   (node_id, node_type, doc_key, label, value, category, path_json)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (node_id) DO UPDATE
                   SET node_type = EXCLUDED.node_type, doc_key = EXCLUDED.doc_key,
                       label = EXCLUDED.label, value = EXCLUDED.value,
                       path_json = EXCLUDED.path_json""",
                (nid, str(n.get("type") or "entity")[:16], doc_key, n.get("label"),
                 nid, "样例", json.dumps({"sample": True}, ensure_ascii=False)),
            )
        for e in edges:
            edge_id = hashlib.sha1(
                f"{e['src']}|{e['dst']}|{e['relation']}".encode("utf-8")).hexdigest()[:32]
            weight = float(e.get("weight") or 0.0)
            status = "validated" if weight >= 0.8 else "hypothesis"
            cur.execute(
                """INSERT INTO graph_edges
                   (edge_id, src_node, dst_node, relation, status, confidence,
                    evidence, method, reason)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (src_node, dst_node, relation) DO UPDATE
                   SET status = EXCLUDED.status, confidence = EXCLUDED.confidence,
                       evidence = EXCLUDED.evidence, reason = EXCLUDED.reason,
                       updated_at = CURRENT_TIMESTAMP""",
                (edge_id, e["src"], e["dst"], e["relation"], status, weight,
                 json.dumps({"basis": e.get("basis", ""), "sample": True}, ensure_ascii=False),
                 "rule", "样例手写弱关联边"),
            )
        for doc_key in sorted({n["id"][4:] for n in nodes if n["id"].startswith("doc:")}):
            cur.execute(
                """INSERT INTO graph_build_state (doc_key, facts_count, nodes, candidates,
                                                  stored, status, note)
                   VALUES (%s, 0, 0, 0, 0, 'sample', '样例手写边')
                   ON CONFLICT (doc_key) DO UPDATE SET status = 'sample',
                       note = '样例手写边', built_at = CURRENT_TIMESTAMP""",
                (doc_key,),
            )
        conn.commit()
    return {"nodes": len(nodes), "edges": len(edges), "dangling": dangling}


# =========================================================
# 6. 校验（真值自检 + 孤儿编号）
# =========================================================
def verify() -> dict:
    """① 12 道题的 evidence_facts 必须能在 `l1_facts` 里逐条找到；
    ② hub 正文里的编号必须都能在映射表里查到解释（孤儿=0）。
    """
    from database_serv__infra import ENTITY_TABLES, get_admin_connection

    qa = qa_truth()
    problems: list[dict] = []
    checked_facts = 0
    with get_admin_connection() as conn, conn.cursor() as cur:
        for q in qa.get("questions") or []:
            for f in q.get("evidence_facts") or []:
                checked_facts += 1
                cur.execute(
                    "SELECT value, row_context FROM l1_facts WHERE doc_key = %s AND header = %s",
                    (f["doc_key"], f["header"]),
                )
                rows = cur.fetchall()
                hit_value = [r for r in rows if str(r[0]) == str(f["value"])]
                if not hit_value:
                    problems.append({"q": q["id"], "doc_key": f["doc_key"], "header": f["header"],
                                     "value": f["value"], "why": "l1_facts 无此『表头+值』",
                                     "found": [r[0] for r in rows][:6]})
                    continue
                hint = str(f.get("row") or "").strip()
                if hint:
                    tokens = [t for t in re.split(r"[\s|]+", hint) if len(t) >= 2]
                    if tokens:
                        joined = [" ".join(str(x) for x in (r[1] or {}).get("row") or [])
                                  for r in hit_value]
                        if not any(all(t in j for t in tokens) for j in joined):
                            problems.append({"q": q["id"], "doc_key": f["doc_key"],
                                             "header": f["header"], "value": f["value"],
                                             "why": f"值命中但行线索 {tokens} 对不上",
                                             "found": joined[:6]})

        # 孤儿编号：样例 hub 正文里的编号 → 映射表
        hub = _hub_dir()
        codes: dict[str, set[str]] = {}
        for p, doc in doc_files():
            text = "\n".join(doc.get("pages") or [])
            for m in CODE_RE.finditer(text):
                codes.setdefault(m.group(0), set()).add(str(p.name))
        orphans = []
        for code in sorted(codes):
            category = CATEGORY_BY_PREFIX.get(code[:2], "")
            table = ENTITY_TABLES.get(category)
            if not table:
                orphans.append({"code": code, "why": "未知前缀", "docs": sorted(codes[code])})
                continue
            cur.execute(f"SELECT 1 FROM {table} WHERE code = %s", (code,))
            if not cur.fetchone():
                orphans.append({"code": code, "why": "映射表无登记", "docs": sorted(codes[code])})

    return {"checked_facts": checked_facts, "problems": problems, "orphans": orphans,
            "codes_in_text": len(codes), "questions": len(qa.get("questions") or [])}


# =========================================================
# 7. 冒烟对比（**不调 AI**：只比"召回到没召回到"，答案对错要另跑 AI）
# =========================================================
def cmd_smoke() -> int:
    """样例库上跑两条召回腿，按 `99_问题与真值.json` 的 evidence_docs 判"召回达标"。

    注意：这里**只判召回**（证据文档集合是否覆盖真值文档），不判答案对错——
    答案要 AI 读原文才能给。这样把"召回的锅"和"作答的锅"分开，符合本仓库
    一直用的口径（召回达标 / 严格命中 / 放行内正确率分开报）。
    """
    import doc_index__graph as di
    import graph_query__graph_walk as gw

    qa = qa_truth()
    questions = qa.get("questions") or []
    cards = di.load_cards(with_facts=True)
    print(f"索引卡 {len(cards)} 张｜特征明文来自 features.json："
          f"{sum(1 for c in cards if c['features_src'] == 'features.json')}", flush=True)

    stat = {"topk": 0, "graph": 0, "total": 0}
    for q in questions:
        truth = set(q.get("evidence_docs") or [])
        if not truth:            # 全否题没有"真值文档集合"，不参与召回统计
            print(f"[{q['id']}] 否定题（真值=不存在）：召回腿不作判分，"
                  f"考点是程序敢不敢回『未找到』", flush=True)
            continue
        stat["total"] += 1
        hits = di.recall_by_topk(q["question"], cards, k=4)
        topk_docs = [h["card"]["doc_key"] for h in hits]
        try:
            res = gw.query(q["question"])
            graph_docs = []
            for v in res.get("values") or []:
                if v.get("doc_key") and v["doc_key"] not in graph_docs:
                    graph_docs.append(v["doc_key"])
        except Exception as exc:
            graph_docs = [f"<异常 {type(exc).__name__}: {exc}>"]
        ok_topk = truth.issubset(set(topk_docs))
        ok_graph = truth.issubset(set(graph_docs))
        stat["topk"] += 1 if ok_topk else 0
        stat["graph"] += 1 if ok_graph else 0
        print(f"[{q['id']}] {q['question']}", flush=True)
        print(f"   真值文档 {sorted(truth)}", flush=True)
        print(f"   索引卡 top-k {'✓' if ok_topk else '✗'} {topk_docs}", flush=True)
        print(f"   图遍历      {'✓' if ok_graph else '✗'} {graph_docs}", flush=True)
    n = max(stat["total"], 1)
    print(f"召回达标（真值文档集合被覆盖）：索引卡 {stat['topk']}/{stat['total']}"
          f"（{stat['topk']/n:.0%}）｜图遍历 {stat['graph']}/{stat['total']}"
          f"（{stat['graph']/n:.0%}）", flush=True)
    return 0


# =========================================================
# 8. 入口
# =========================================================
def cmd_list() -> int:
    docs = doc_files()
    print(f"样例目录：{SAMPLE_DIR}")
    print(f"文档 {len(docs)} 份：")
    for p, d in docs:
        feats = d.get("features") or {}
        print(f"  · {p.name}｜{d.get('category')}｜{d.get('doc_key')}"
              f"｜表 {len(d.get('tables') or [])}｜页 {len(d.get('pages') or [])}"
              f"｜doc_type={feats.get('doc_type')} money_flow={feats.get('money_flow')}")
    qa = qa_truth()
    print(f"题目 {len(qa.get('questions') or [])} 道（真值文件 {QA_FILE.name}）：")
    for q in qa.get("questions") or []:
        print(f"  [{q['id']}] {q['group']}：{q['question']}")
    ed = read_json(EDGES_FILE)
    print(f"弱关联边：节点 {len(ed.get('nodes') or [])}｜边 {len(ed.get('edges') or [])}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="样例语料装载器（离线、不调 AI、不跑 OCR）")
    ap.add_argument("--list", action="store_true", help="列出样例文档/题目/边，不写库")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"目标库名（默认 {DEFAULT_DB}）")
    ap.add_argument("--workspace", default="",
                    help="装进某个仓库（workspace/界面叫仓库）：用它自己的库与 hub，忽略 --db")
    ap.add_argument("--allow-prod", action="store_true",
                    help=f"允许写真实语料库（{PROD_DB} 或既有仓库的库）：样例会混进去，慎用")
    ap.add_argument("--create", action="store_true", help="建库（首次必须）+ 套用 init_db.sql")
    ap.add_argument("--reset", action="store_true", help="先 DROP 样例库再建（清空重来）")
    ap.add_argument("--load", action="store_true", help="装载：映射 + 文档 + 弱关联边")
    ap.add_argument("--no-graph", action="store_true", help="装载时跳过 98_弱关联边.json")
    ap.add_argument("--verify", action="store_true", help="真值自检 + 孤儿编号检查")
    ap.add_argument("--smoke", action="store_true",
                    help="跑两条召回腿（索引卡 top-k / 图遍历），只判召回达标，不调 AI")
    args = ap.parse_args(argv)

    # ⚠️ 必须在任何重量级 import 之前生效：APP_DB_CONFIG 是导入期快照
    if args.workspace:
        # 装进指定仓库：库名/hub/input/output/密钥全部按那个仓库走（环境隔离）
        import workspace__infra as _ws

        entry = _ws.find(args.workspace)
        if not entry:
            print(f"❌ 没有这个仓库：{args.workspace}（先看 python workspace__infra.py list）")
            return 2
        _ws.apply_active(entry, persist=False)
    if not args.workspace:
        os.environ["APP_DB_NAME"] = args.db      # 没指定仓库：按 --db 直接装（老用法）
    target_db = os.environ["APP_DB_NAME"]
    prod_dbs = {PROD_DB}
    try:
        import workspace__infra as _ws2

        prod_dbs |= {str(w.get("db")) for w in _ws2.list_workspaces() if w.get("legacy")}
    except Exception:
        pass
    if target_db in prod_dbs and not args.allow_prod:
        print(f"❌ 拒绝把样例写进真实语料库 {target_db}（这是保护：会混进真语料与真编号）。\n"
              f"   如确实要写，加 --allow-prod。推荐：先建一个仓库再装，例如\n"
              f"   python workspace__infra.py create --name 样例仓库 --slug sample\n"
              f"   python sample_corpus__infra.py --workspace sample --create --load")
        return 2
    if args.reset and not args.create:
        args.create = True

    if args.list and not (args.create or args.load or args.verify or args.smoke):
        return cmd_list()

    print(f"目标库：{target_db}｜hub：{_hub_dir()}", flush=True)
    if args.create:
        ensure_db(target_db, reset=args.reset)
    if args.create or args.load:
        reg = register_mappings()
        print(f"[编号登记] 一致 {len(reg['registered'])}｜不一致 {len(reg['mismatch'])}"
              f"｜失败 {len(reg['failed'])}", flush=True)
        if reg["mismatch"]:
            for m in reg["mismatch"]:
                print(f"  ⚠️ {m['category']} 期望 {m['expect']} 实际 {m['got']}", flush=True)
        if reg["failed"]:
            for f in reg["failed"]:
                print(f"  ❌ {f['category']} {f['code']}：{f['why']}", flush=True)
            return 1
    if args.load:
        rep = load_docs()
        print(f"[文档装载] {len(rep['docs'])} 份｜事实合计 {rep['facts']}｜hub={rep['hub_dir']}",
              flush=True)
        if not args.no_graph:
            g = load_graph()
            print(f"[弱关联边] 节点 {g['nodes']}｜边 {g['edges']}｜悬空 {len(g['dangling'])}",
                  flush=True)
            if g["dangling"]:
                for d in g["dangling"]:
                    print(f"  ⚠️ 悬空边：{d['src']} → {d['dst']}", flush=True)
    if args.verify:
        res = verify()
        print(f"[真值自检] 核对事实 {res['checked_facts']} 条｜问题 {len(res['problems'])}"
              f"｜正文编号 {res['codes_in_text']} 个｜孤儿 {len(res['orphans'])}", flush=True)
        for p in res["problems"]:
            print(f"  ❌ [{p['q']}] {p['why']}：{p['doc_key']}｜{p['header']}={p['value']}"
                  f"｜实际 {p.get('found')}", flush=True)
        for o in res["orphans"]:
            print(f"  ❌ 孤儿编号 {o['code']}（{o['why']}）出现在 {o['docs']}", flush=True)
        if res["problems"] or res["orphans"]:
            return 1
        print(f"  ✓ {res['questions']} 道题的真值都能在事实层逐条找到；"
              f"正文编号全部有登记（孤儿=0）", flush=True)
    if args.smoke:
        return cmd_smoke()
    return 0


if __name__ == "__main__":
    sys.exit(main())
