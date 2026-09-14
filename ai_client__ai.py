"""按链路隔离的 AI 客户端（**无进程级全局状态**）。

设计目的（对应"从 DeepSeek V4.1 概括这一步开始隔离"）：
    · 公共入口只保留：PaddleOCR、脱敏、（台账）入库判别；
    · 从概括/抽取开始，各 AI 链路各自独立，不共用任何模块级可变状态：
        - l1    ：分段/概括/事实抽取     ← AI_*        （deepseek-flash）
        - edge  ：证据链/因果边构建      ← EDGE_AI_*   （qwen3.8-flash）
        - graph ：图遍历查询/核对        ← GRAPH_AI_*  （qwen3.8-flash，独立 key）
        - chat  ：AI 对话                ← CHAT_AI_*   （qwen3.8-max）
    · 调用返回 (content, reasoning)，不写任何模块级全局；因此不会出现
      "A 链路的调用覆盖 B 链路的推理内容，导致 B 的 JSON 兜底解析串味" 的问题。
    · 日志独立落盘：logs/ai/<chain>_<YYYYMMDD>.log（与旧链路的 <doc>.ai.log 分开）。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
AI_LOG_DIR = BASE_DIR / "logs" / "ai"

# 链路 -> (key, base_url, model, timeout) 的环境变量名；graph 为独立 key（不回退）。
_CHAIN_KEYS = {
    "l1": ("AI_API_KEY", "AI_BASE_URL", "AI_MODEL", "AI_TIMEOUT"),
    "edge": ("EDGE_AI_API_KEY", "EDGE_AI_BASE_URL", "EDGE_AI_MODEL", "EDGE_AI_TIMEOUT"),
    "graph": ("GRAPH_AI_API_KEY", "GRAPH_AI_BASE_URL", "GRAPH_AI_MODEL", "GRAPH_AI_TIMEOUT"),
    "chat": ("CHAT_AI_API_KEY", "CHAT_AI_BASE_URL", "CHAT_AI_MODEL", "CHAT_AI_TIMEOUT"),
}
_DEFAULT_BASE = "https://api.deepseek.com"
_DEFAULT_TIMEOUT = 90


def load_chain_config(chain: str) -> dict:
    """读取某链路的配置（每次现读 env，避免缓存带来的串味）。"""
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=BASE_DIR / ".env")
    keys = _CHAIN_KEYS.get(chain)
    if keys is None:
        raise KeyError(f"未知 AI 链路：{chain}")
    key_env, base_env, model_env, timeout_env = keys
    api_key = os.getenv(key_env, "").strip()
    # edge 允许复用 chat 的 key（历史约定）；graph 必须独立 key，不回退
    if not api_key and chain == "edge":
        api_key = os.getenv("CHAT_AI_API_KEY", "").strip()
        base_env_alt = os.getenv("CHAT_AI_BASE_URL", "")
    else:
        base_env_alt = ""
    base_url = (os.getenv(base_env, "") or base_env_alt or _DEFAULT_BASE).rstrip("/")
    try:
        timeout = int(os.getenv(timeout_env, str(_DEFAULT_TIMEOUT)))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT
    return {
        "chain": chain,
        "api_key": api_key,
        "base_url": base_url,
        "model": os.getenv(model_env, "").strip(),
        "timeout": timeout,
    }


def _log(chain: str, message: str) -> None:
    """链路独立日志：logs/ai/<chain>_<date>.log（追加式，不打印敏感内容）。"""
    try:
        AI_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = AI_LOG_DIR / f"{chain}_{time.strftime('%Y%m%d')}.log"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except Exception:
        pass


def complete(
    chain: str,
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    max_tokens: int = 2000,
) -> tuple[str, str]:
    """调用指定链路，返回 (content, reasoning_content)。

    与旧客户端的关键区别：
      · 不再设置任何模块级 `_LAST_REASONING` / `_AI_LOG_PATH`；
      · reasoning 直接随返回值交给调用方，调用方自己决定是否使用兜底解析；
      · 每条链路独立 key / base_url / model / 日志文件。
    """
    import requests

    cfg = load_chain_config(chain)
    if not cfg["api_key"]:
        raise RuntimeError(
            f"链路 {chain} 未配置 API Key（{_CHAIN_KEYS[chain][0]}）；"
            "graph 链路必须使用独立 key，不复用其它链路。"
        )
    if not cfg["model"]:
        raise RuntimeError(f"链路 {chain} 未配置模型（{_CHAIN_KEYS[chain][2]}）。")
    _log(chain, f"request model={cfg['model']} system={len(system)}字 user={len(user)}字")
    resp = requests.post(
        f"{cfg['base_url']}/chat/completions",
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        json={
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        },
        timeout=cfg["timeout"],
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        message = data["choices"][0]["message"]
        content = (message.get("content") or "").strip()
        reasoning = message.get("reasoning_content") or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"链路 {chain} 返回格式异常: {exc}") from exc
    _log(chain, f"response content={len(content)}字 reasoning={len(reasoning)}字")
    # 思考过程**正文**也记进链路日志（读取链路 AI_* 本来就在 ai_parser 里单独记；
    # 这里统一覆盖 edge / l1 / graph 三条链路，便于排查"模型为什么这么判/这么概括"）。
    # 截断保护避免深思考把日志撑爆；AI_LOG_REASONING=0 可关闭。
    if reasoning and os.getenv("AI_LOG_REASONING", "1").strip() not in ("0", "false", "no"):
        try:
            limit = int(os.getenv("AI_LOG_REASONING_MAX", "4000") or 4000)
        except ValueError:
            limit = 4000
        _log(chain, f"reasoning 正文（最多 {limit} 字）：\n{reasoning[:limit]}")
    return content, reasoning
