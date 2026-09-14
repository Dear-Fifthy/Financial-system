"""Embedding 子进程工作器：供 rag_store 以子进程方式调用。

背景（为什么需要子进程）：
  torch 与 paddlepaddle 在**同一进程**内先后加载会触发 DLL 冲突
  （实测：先 import paddle 再 import torch -> WinError 127 shm.dll 加载失败）。
  主程序（table/hub_pipeline）必然先加载 paddle，所以 embedding 不能在
  主进程内 import sentence-transformers/torch；本工作器在独立子进程中执行，
  通过 stdin/stdout 交换 JSON，主进程永不 import torch。

协议：
  stdin  : JSON 字符串数组（已脱敏文本列表）
  stdout : JSON 二维数组（每文本的 embedding 向量列表）
  stderr : 错误信息（非零退出码时由调用方读取）
"""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path

if __package__ in (None, ""):          # 直接 `python <层>/<模块>.py` 跑：把仓库根放回 sys.path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))


import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")


def _model_in_local_cache(model_name: str) -> bool:
    """检查 bge 模型是否已缓存在本地（命中则离线加载，不联网）。

    缓存路径：~/.cache/huggingface/hub/models--{org}--{name}/snapshots/
    （可用 HF_HOME 覆盖）。模型下载成功过一次后即可离线使用。
    """
    repo_dir = "models--" + model_name.replace("/", "--")
    base = os.getenv("HF_HOME") or str(Path.home() / ".cache" / "huggingface")
    snapshots = Path(base) / "hub" / repo_dir / "snapshots"
    return snapshots.is_dir() and any(snapshots.iterdir())


def main() -> int:
    # utf-8-sig：兼容 PowerShell 管道等会附加 BOM 的调用方（防御性）
    raw = sys.stdin.buffer.read()
    texts = json.loads(raw.decode("utf-8-sig"))
    if not texts:
        print("[]")
        return 0

    model_name = os.getenv("RAG_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")

    # 网络策略（回答"不是线程问题，是模型下载问题"）：
    # 1. 模型已在本地缓存 -> 强制离线加载（HF_HUB_OFFLINE=1），绝不联网；
    # 2. 无缓存且未配置镜像 -> 自动使用 hf-mirror.com（国内可达），并提示；
    # 3. 用户显式配置的 HF_ENDPOINT 优先。
    if _model_in_local_cache(model_name):
        os.environ["HF_HUB_OFFLINE"] = "1"
        print("[embed_worker] 命中本地模型缓存，离线加载。", file=sys.stderr)
    elif os.getenv("HF_ENDPOINT", ""):
        os.environ["HF_ENDPOINT"] = os.getenv("HF_ENDPOINT", "")
    else:
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
        print(
            "[embed_worker] 模型未在本地缓存且未配置 HF_ENDPOINT，"
            "自动改用镜像 hf-mirror.com 下载（可写入 .env 固化）。",
            file=sys.stderr,
        )

    from sentence_transformers import SentenceTransformer  # 本进程内 import torch 无冲突

    try:
        model = SentenceTransformer(model_name, device="cpu")
    except Exception as exc:
        raise RuntimeError(
            f"模型加载失败：{exc}\n"
            "解决办法：① 在 .env 写入 HF_ENDPOINT=https://hf-mirror.com 后重试一次；\n"
            "② 或手动下载 BAAI/bge-small-zh-v1.5 到本地目录，并把 RAG_EMBED_MODEL 指向本地路径（完全离线）。"
        ) from exc
    vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    print(json.dumps([v.tolist() for v in vectors], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"EMBED_WORKER_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
