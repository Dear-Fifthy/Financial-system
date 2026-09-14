"""全局检索方式配置（作用于整个检索/核对链路，不再按对话会话选择）。

两种方法（对应 chat_engine__ai.MEMORY_VARIANTS 的 key）：
    · graph —— 图遍历：沿证据链/因果边取证（默认；用 GRAPH_AI_* 独立 key，非 embedding）
    · rag   —— 向量检索 RAG：分块 + embedding + document_chunks 近邻检索（保留 embedding）

存放：`.env` 的 `RETRIEVAL_METHOD`（默认 graph）。本模块是**唯一读取入口**，
L1/边构建/L3 查询/对话都从这里取，避免各链路各写一份。
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
ENV_KEY = "RETRIEVAL_METHOD"
DEFAULT = "graph"
VALID = ("graph", "rag")

_LABELS = {"graph": "图遍历（证据链/因果边）", "rag": "向量检索 RAG（含 embedding）"}


def global_method() -> str:
    """当前全局检索方式（非法值回退默认 graph）。"""
    value = (os.getenv(ENV_KEY, "") or "").strip().lower()
    return value if value in VALID else DEFAULT


def label(method: str | None = None) -> str:
    return _LABELS.get(method or global_method(), _LABELS[DEFAULT])


def uses_embedding(method: str | None = None) -> bool:
    """是否需要 embedding（只有 RAG 方法需要；图遍历不需要）。"""
    return (method or global_method()) == "rag"


def set_method(method: str, *, persist: bool = True) -> str:
    """切换全局方式：更新进程环境变量，并（默认）写回 .env 以便重启后保持。"""
    m = (method or "").strip().lower()
    if m not in VALID:
        raise ValueError(f"不支持的检索方式：{method}（可选：{', '.join(VALID)}）")
    os.environ[ENV_KEY] = m
    if persist:
        _persist_to_env(m)
    return m


def _persist_to_env(method: str) -> None:
    """把 RETRIEVAL_METHOD 写回 .env（存在则替换该行，不存在则追加）。"""
    try:
        lines: list[str] = []
        if ENV_PATH.exists():
            lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
        found = False
        for i, line in enumerate(lines):
            if line.strip().startswith(f"{ENV_KEY}="):
                lines[i] = f"{ENV_KEY}={method}"
                found = True
                break
        if not found:
            lines.append(f"{ENV_KEY}={method}")
        ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass  # 持久化失败不影响本次运行（内存中的值已生效）
