"""RAG 向量库模块：分块 + 向量化 + pgvector 存储。

文件位置：rag/rag_store__rag.py（按层归入子目录）。
职责：
  - chunk_document()      ：把文档文本切成检索块（标题感知 + 固定窗口兜底）
  - embed_texts()         ：向量化（可插拔：本地 bge / 外部 embedding API）
  - ensure_table()        ：确保 document_chunks 表存在（幂等）
  - upsert_chunks()       ：按 doc_key 先删后插（幂等重建）

关于 embedding 的选择（回答"deepseek 能否做 embedding"）：
  DeepSeek 官方 API 目前**不提供 embedding 接口**（只有 chat/reasoner），
  因此向量化走下面两条路之一（.env 的 RAG_EMBED_BACKEND 切换）：
    local（默认，推荐）：sentence-transformers + bge-small-zh-v1.5
       优点：完全离线、免费、数据不出本机（符合"出本机必脱敏"铁律）
       安装：pip install sentence-transformers（首次会自动下载模型 ~100MB）
       ⚠️ 当前环境未安装，会给出明确报错提示
    api：OpenAI 兼容 embedding 接口（如阿里百炼 text-embedding-v3 等）
       需配置 RAG_EMBED_API_URL / RAG_EMBED_API_KEY / RAG_EMBED_MODEL
       ⚠️ 用 API 时 chunk 文本会出本机，必须确保已脱敏

安全约定：本模块只接收"已脱敏"文本；明文人名/卡号严禁进入本模块。
"""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path

if __package__ in (None, ""):          # 直接 `python <层>/<模块>.py` 跑：把仓库根放回 sys.path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))


import json
import os
import re
from pathlib import Path
from typing import Callable

import psycopg2
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(dotenv_path=BASE_DIR / ".env")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# ---- 配置（.env 可覆盖）----
# 嵌入后端：local（默认，bge-small-zh） | api（OpenAI 兼容接口）
RAG_EMBED_BACKEND = os.getenv("RAG_EMBED_BACKEND", "local").strip().lower()
RAG_EMBED_DIM = _env_int("RAG_EMBED_DIM", 512)      # bge-small-zh = 512；必须与模型一致
RAG_CHUNK_SIZE = _env_int("RAG_CHUNK_SIZE", 700)    # 单块目标字符数（中文约 1 字≈1 token）
RAG_OVERLAP = _env_int("RAG_OVERLAP", 100)           # 相邻块重叠字符数
# 切分点浮动窗口(字符)：±该范围内找干净落点（行首/空白/标点），
# 保护 text:/paragraph_title: 等英文格式标签的完整性
RAG_BOUNDARY_FLOAT_CHARS = _env_int("RAG_BOUNDARY_FLOAT_CHARS", 20)
RAG_EMBED_MODEL = os.getenv("RAG_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
RAG_EMBED_API_URL = os.getenv("RAG_EMBED_API_URL", "")
RAG_EMBED_API_KEY = os.getenv("RAG_EMBED_API_KEY", "")
RAG_EMBED_API_MODEL = os.getenv("RAG_EMBED_API_MODEL", "")
# embedding 子进程超时(秒)：首次运行需下载模型（约100MB），给足余量
RAG_EMBED_TIMEOUT_S = _env_int("RAG_EMBED_TIMEOUT_S", 1800)


# =========================================================
# 1. 分块（chunking）
# =========================================================
# hub 文本里的"英文格式描述前缀"：切分必须保证这些标签行完整，不腰斩
_LABEL_PREFIXES = ("text:", "header:", "doc_title:", "paragraph_title:", "number:", "table:", "seal:")
# 归一化用：标签出现在行中（前面不是行首/字母数字）时，在其前补换行拆成独立行——
# 修复 OCR 把多个块并成一行导致的 "…协助；text:由乙方承担…" 粘连
_LABEL_JOIN_RE = re.compile(
    r"(?<![A-Za-z0-9_\n])(?=(?:text|header|doc_title|paragraph_title|number|table|seal):)"
)


def _is_label_line(line: str) -> bool:
    """该行是否是格式标签/标题行（行首切分时的"干净边界"）。"""
    s = line.lstrip()
    return s.startswith("##") or any(s.startswith(p) for p in _LABEL_PREFIXES)


def _split_long_line(line: str, size: int, float_chars: int) -> list[str]:
    """单行超过块长时做行内切分：切点在 ±float_chars 内浮动到最近的
    空白/中英文标点之后，避免把 text:/paragraph_title: 等 ASCII 标签或
    词语从中间切断；整行没有可用断点时按 size 硬切（保底）。"""
    parts: list[str] = []
    rest = line
    while len(rest) > size:
        lo = max(0, size - float_chars)
        hi = min(len(rest), size + float_chars)
        best = size
        for off in range(lo, hi):
            if rest[off] in " \t，,。；;：:":
                if abs(off - size) < abs(best - size):
                    best = off
        parts.append(rest[:best])
        rest = rest[best:]
    if rest:
        parts.append(rest)
    return parts


def chunk_document(
    full_text: str,
    chunk_size: int = RAG_CHUNK_SIZE,
    overlap: int = RAG_OVERLAP,
    float_chars: int = RAG_BOUNDARY_FLOAT_CHARS,
) -> list[str]:
    """把文档文本切成检索块。

    规则（对应讨论结论）：
      1. 只在【行首】切分——hub 文本每行形如 "text:…"/"paragraph_title:…"，
         行首切分保证英文格式描述（text:/paragraph_title:/doc_title:/header:/
         number:/table:/seal:）永远完整，不会被拦腰切断；
      2. 切分点容许浮动（±float_chars≈10-20 字符）：块超限时不立刻切，
         先看是否属于"超长单行"——是则行内切，切点浮动到最近的空白/标点后；
         普通多行块则在上限边界处自然落在行首（浮动范围即"最后一行"的粒度）；
      3. 重叠：每块开头拼接上一块末尾 overlap（默认 20，10-20 左右）字符，
         供跨块上下文衔接（标点/句意不被割裂）；
      4. 中文按字符数估算（1 字 ≈ 1 token），chunk_size 留安全余量。
    返回按原文档顺序排列的块列表。
    """
    text = (full_text or "").strip()
    if not text:
        return []
    # 归一化：行中粘连的格式标签拆成独立行（修复 "…协助；text:由乙方承担…"）
    text = _LABEL_JOIN_RE.sub("\n", text)
    lines = text.splitlines()
    if not lines:
        return []

    # ---- 第一步：按行累积成"候选块"（只在行首切）----
    raw_chunks: list[str] = []
    buf: list[str] = []
    buf_chars = 0

    def flush() -> None:
        nonlocal buf, buf_chars
        if buf:
            raw_chunks.append("\n".join(buf))
        buf, buf_chars = [], 0

    for line in lines:
        line_cost = len(line) + 1  # +1 近似换行
        if buf and buf_chars + line_cost > chunk_size:
            flush()  # 超限 → 在行首切一刀（天然干净边界）
        if len(line) > chunk_size:
            # 超长单行：行内浮动切分（保护标签/词不被腰斩）
            for seg in _split_long_line(line, chunk_size, float_chars):
                flush()
                buf.append(seg)
                buf_chars = len(seg)
            flush()
        else:
            buf.append(line)
            buf_chars += line_cost
    flush()

    # ---- 第二步：加重叠（每块开头补上一块末尾 overlap 字符）----
    # 接缝处补换行：避免"上一块尾部文字 + 下一块行首标签"在拼接处粘成一行
    chunks: list[str] = []
    prev_tail = ""
    for c in raw_chunks:
        if overlap > 0 and prev_tail and c:
            seam = "\n" if not prev_tail.endswith("\n") else ""
            chunks.append(prev_tail + seam + c)
        else:
            chunks.append(c)
        prev_tail = c[-overlap:] if overlap and c else ""
    return [c for c in chunks if c.strip()]


# =========================================================
# 2. 向量化（embedding）
# =========================================================
def embed_texts(texts: list[str]) -> list[list[float]]:
    """把文本列表向量化，返回 list[list[float]]。

    local 后端：在**独立子进程**（embed_worker__rag.py）里跑 sentence-transformers。
      ⚠️ 为什么用子进程：torch 与 paddlepaddle 同进程先后加载会触发 DLL 冲突
      （实测先 paddle 后 torch -> WinError 127），而主程序必然先加载 paddle，
      所以 embedding 必须隔离到子进程，主进程永不 import torch。
    api 后端：POST {url}，body 兼容 OpenAI embeddings 格式。
    """
    if not texts:
        return []
    if RAG_EMBED_BACKEND == "api":
        return _embed_via_api(texts)
    return _embed_via_subprocess(texts)


def _embed_via_subprocess(texts: list[str]) -> list[list[float]]:
    """本地 embedding：委托 embed_worker__rag.py 子进程执行（DLL 隔离）。

    子进程 stdin 传文本列表，stdout 收 JSON 向量；模型每次调用加载一次。
    超时：RAG_EMBED_TIMEOUT_S（默认 1800s）——首次运行需联网下载 bge 模型
    （约100MB），可能耗时数分钟，超时上限放宽并给出明确提示。
    """
    import subprocess
    import sys

    from infra import proc__infra as _proc    # 静默子进程：避免 pythonw 下弹黑窗口

    worker = Path(__file__).resolve().parent / "embed_worker__rag.py"
    if not worker.exists():
        raise RuntimeError(f"找不到 embedding 工作器：{worker}")
    payload = json.dumps(texts, ensure_ascii=False).encode("utf-8")
    try:
        proc = _proc.run(
            [sys.executable, str(worker)],
            input=payload,
            capture_output=True,
            timeout=RAG_EMBED_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"embedding 子进程超时（{RAG_EMBED_TIMEOUT_S}s）。"
            "首次运行需联网下载 bge 模型（约100MB），请检查网络后重试，"
            "或在 .env 调大 RAG_EMBED_TIMEOUT_S。"
        ) from None
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "ignore")[-500:]
        raise RuntimeError(f"embedding 子进程失败：{err or '未知错误'}")
    try:
        return json.loads(proc.stdout.decode("utf-8"))
    except json.JSONDecodeError:
        raise RuntimeError("embedding 子进程输出不是合法 JSON") from None


def _embed_via_api(texts: list[str]) -> list[list[float]]:
    if not RAG_EMBED_API_URL or not RAG_EMBED_API_KEY:
        raise RuntimeError("RAG_EMBED_BACKEND=api 但未配置 RAG_EMBED_API_URL / RAG_EMBED_API_KEY！")
    import requests

    resp = requests.post(
        RAG_EMBED_API_URL,
        headers={"Authorization": f"Bearer {RAG_EMBED_API_KEY}"},
        json={"model": RAG_EMBED_API_MODEL, "input": texts},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    items = data.get("data", [])
    items.sort(key=lambda it: it.get("index", 0))
    return [it["embedding"] for it in items]


# =========================================================
# 3. pgvector 存储
# =========================================================
def _get_connection():
    """复用 database_serv 的应用连接配置（同一 app 角色）。"""
    from infra.database_serv__infra import APP_DB_CONFIG

    return psycopg2.connect(**APP_DB_CONFIG)


def _vector_literal(vec: list[float]) -> str:
    """把向量列表转成 pgvector 字面量字符串（如 '[0.1,0.2]'），
    配合 SQL 里 %s::vector 使用——不依赖 pgvector 的 Python 适配器。"""
    return "[" + ",".join(str(float(x)) for x in vec) + "]"


def ensure_table() -> None:
    """确保 document_chunks 表存在且 embedding 维度与 RAG_EMBED_DIM 一致。

    DDL 走【管理员连接】：应用角色没有 schema CREATE/ALTER 权限。
    维度自愈：若已有表的 embedding 列维度与当前模型不一致（如旧表建了
    vector(1024)，而 bge-small-zh 输出 512，插入会报
    "expected 1024 dimensions, not 512"），自动 删索引→改列类型→重建索引。
    建表/迁移后补授应用角色 DML 权限（表 + 自增序列）。
    """
    from infra.database_serv__infra import APP_DB_CONFIG, get_admin_connection

    app_user = APP_DB_CONFIG["user"]
    with get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS document_chunks (
                id BIGSERIAL PRIMARY KEY,
                doc_key VARCHAR(200) NOT NULL,
                chunk_index INT NOT NULL,
                chunk_text TEXT NOT NULL,
                meta JSONB NOT NULL DEFAULT '{}',
                embedding vector(%s),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (doc_key, chunk_index)
            )
            """,
            (RAG_EMBED_DIM,),
        )
        # 维度自愈：实际列维度 != 当前模型维度时迁移（先删索引再改类型）
        cur.execute(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'document_chunks'::regclass AND attname = 'embedding'"
        )
        row = cur.fetchone()
        actual_type = (row[0] if row else "") or ""
        if actual_type and actual_type != f"vector({RAG_EMBED_DIM})":
            cur.execute("DROP INDEX IF EXISTS idx_chunks_embedding")
            cur.execute(
                f"ALTER TABLE document_chunks ALTER COLUMN embedding TYPE vector({int(RAG_EMBED_DIM)})"
            )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_embedding "
            "ON document_chunks USING hnsw (embedding vector_cosine_ops)"
        )
        # 补授应用角色：数据操作（DML）+ 自增序列使用权
        cur.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON document_chunks TO "{app_user}"')
        cur.execute(
            f'GRANT USAGE, SELECT ON SEQUENCE document_chunks_id_seq TO "{app_user}"'
        )
        conn.commit()


def upsert_chunks(
    doc_key: str,
    chunks: list[str],
    metas: list[dict] | None = None,
    embeddings: list[list[float]] | None = None,
) -> int:
    """按 doc_key 先删后插（幂等重建），返回插入块数。

    embeddings 可省：省时只存文本与元数据，检索时报错（需要向量）。
    向量以字面量字符串 + %s::vector 写入（无适配器依赖）。
    """
    if not chunks:
        return 0
    ensure_table()
    metas = metas or [{} for _ in chunks]
    with _get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM document_chunks WHERE doc_key = %s", (doc_key,))
        for i, (chunk, meta) in enumerate(zip(chunks, metas)):
            embedding = _vector_literal(embeddings[i]) if embeddings else None
            cur.execute(
                "INSERT INTO document_chunks (doc_key, chunk_index, chunk_text, meta, embedding) "
                "VALUES (%s, %s, %s, %s, %s::vector)",
                (doc_key, i, chunk, json.dumps(meta, ensure_ascii=False), embedding),
            )
        conn.commit()
    return len(chunks)


def retrieve_top_k(
    query_vector: list[float],
    top_k: int = 5,
    meta_filter: dict | None = None,
) -> list[dict]:
    """向量检索 Top-K（可选元数据先过滤，再按余弦距离排序）。"""
    ensure_table()
    where, params = [], []
    if meta_filter:
        for key, val in meta_filter.items():
            where.append(f"meta->>%s = %s")
            params.extend([key, str(val)])
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(_vector_literal(query_vector))  # 字面量字符串 + %s::vector，无适配器依赖
    params.append(top_k)
    with _get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT doc_key, chunk_index, chunk_text, meta,
                   1 - (embedding <=> %s::vector) AS sim
            FROM document_chunks
            {where_sql}
            ORDER BY sim DESC
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()
    return [
        {
            "doc_key": r[0],
            "chunk_index": r[1],
            "chunk_text": r[2],
            "meta": r[3],
            "sim": float(r[4]) if r[4] is not None else 0.0,
        }
        for r in rows
    ]


def index_hub_json(hub_json_path: Path, meta: dict | None = None) -> tuple[int, str]:
    """把一个 hub 脱敏 JSON 分块 + 向量化 + 入库（RAG 索引入口）。

    返回 (块数, 说明)。
    """
    from desens.ai_guard__desens import require_hub_file   # AI 输入守卫：embedding 也是 AI 链路，只允许 hub/

    hub_json_path = require_hub_file(hub_json_path, purpose="RAG 索引（embedding）")
    doc = json.loads(hub_json_path.read_text(encoding="utf-8"))
    full_text = "\n".join(doc.get("pages", []))
    chunks = chunk_document(full_text)
    if not chunks:
        return 0, "无可分块内容"
    m = meta or {}
    m.setdefault("category", doc.get("category", ""))
    m.setdefault("source_file", doc.get("source_file", hub_json_path.name))
    doc_key = m.get("doc_key") or doc.get("doc_key") or hub_json_path.stem
    embeddings = embed_texts(chunks)  # 本地/API 按 RAG_EMBED_BACKEND
    n = upsert_chunks(doc_key, chunks, [m] * len(chunks), embeddings)
    return n, f"已索引 {n} 块（{doc_key}）"


if __name__ == "__main__":
    # 调试入口：python -m rag.rag_store__rag <hub json 路径>
    import sys

    p = sys.argv[1] if len(sys.argv) > 1 else ""
    if not p:
        print("用法：python -m rag.rag_store__rag <hub/xxx.json>")
        sys.exit(1)
    n, msg = index_hub_json(Path(p))
    print(msg)
