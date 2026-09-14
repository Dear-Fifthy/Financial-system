"""AI 对话会话存储：一个会话 = 一个文件夹。

目录结构（logs/ 已被 .gitignore 排除，天然不提交、随项目备份）：
    logs/chat/
    ├── <用户名>/                      每个用户的对话各归其位
    │   └── <会话id>/                 一个会话一个文件夹
    │       ├── conv.json            会话元数据：id/标题/创建与更新时间/提示词版本/
    │       │                         记忆方式/范围/引擎信息
    │       ├── messages.jsonl       追加式消息日志（双方对话）：
    │       │                         {"ts": iso, "role": "user"|"ai",
    │       │                          "content": str, "meta": {...}}
    │       └── thinking.jsonl       **思考过程**审计日志（每条 AI 回复一条）：
    │                                 {"ts", "idx", "question", "reasoning",
    │                                  "reasoning_len", "reasoning_tokens", "model",
    │                                  "prompt_key", "memory_key", "usage"}
    └── _recycle/<用户名>/…           删除 = 移动到回收区（可恢复 / 可彻底删除）

为什么思考过程单独一个文件（而不是塞进 messages.jsonl 的 meta）：
    · messages.jsonl 是"人读的对话流"，思考动辄几千字会把正文淹没；
    · 单列 thinking.jsonl 便于审计/统计（思考 token 消耗、被折叠过的历史），
      也便于以后整体清理而不动对话正文；
    · AI 消息的 meta 里只留 `thinking_idx`（指向本文件第 N 条）+ 长度/token 数，
      窗口展开时按 idx 取正文。

设计取舍（为什么放项目内而非 Windows 用户目录）：
    · 与 input/hub/output/logs 全项目"一处可查、随项目备份"一致；logs/ 已在
      .gitignore，不会被误提交；本工具是单机本地财务工具，%APPDATA% 的优势
      （分发/多机漫游）用不上，反而难找难排查。
    · 不用系统回收站：避免依赖 send2trash / 平台差异；项目内回收区零依赖且可逆。

纯 Python、无 Qt 依赖，可独立单测。
"""
from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
# 对话记录随「仓库」（工作区）隔离：历史问答里带着该仓库的证据（金额/编号/原文片段），
# 属于业务数据，不能让另一个仓库的会话看到。默认仍是 logs/chat，老环境行为不变。
from infra.workspace__infra import chat_root as _chat_root

CHAT_ROOT = _chat_root()
RECYCLE_ROOT = CHAT_ROOT / "_recycle"

CN_TZ = timezone(timedelta(hours=8))  # 日志时间统一用东八区便于人工查阅


# ---------- 工具 ----------
def _now_iso() -> str:
    """当前时间 ISO 字符串（东八区）。"""
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def safe_component(name: str, fallback: str = "未命名") -> str:
    """把用户名/标题清洗成可安全用于文件夹名的片段（去路径非法字符与空白）。"""
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "")).strip("._")
    return cleaned or fallback


def new_conv_id() -> str:
    """会话 id：时间戳 + 短随机后缀（同秒多会话不冲突）。"""
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


# ---------- 路径 ----------
def user_dir(username: str) -> Path:
    """某用户的会话根目录（不存在则创建）。"""
    d = CHAT_ROOT / safe_component(username)
    d.mkdir(parents=True, exist_ok=True)
    return d


def conversation_dir(username: str, conv_id: str) -> Path:
    return user_dir(username) / conv_id


def _meta_path(conv_dir: Path) -> Path:
    return conv_dir / "conv.json"


def _messages_path(conv_dir: Path) -> Path:
    return conv_dir / "messages.jsonl"


def _thinking_path(conv_dir: Path) -> Path:
    return conv_dir / "thinking.jsonl"


# ---------- 思考过程（对话 AI 的 reasoning_content）----------
def append_thinking(
    conv_dir: Path,
    *,
    question: str = "",
    reasoning: str = "",
    reasoning_tokens: int = 0,
    model: str = "",
    meta: dict | None = None,
) -> dict:
    """记录一条思考过程（追加写 thinking.jsonl），返回该条记录（含 idx）。

    · idx = 本会话第几条思考（0 基），AI 消息的 meta 里存 `thinking_idx` 指向它；
    · 思考文本是模型基于**已脱敏** payload 生成的，可与对话正文同等对待（logs/ 已 gitignore）；
    · 空思考（模型没给 / 已关闭记录）不写文件，返回 idx=-1 供调用方跳过。
    """
    text = str(reasoning or "")
    if not text.strip():
        return {"idx": -1, "reasoning_len": 0, "reasoning_tokens": int(reasoning_tokens or 0)}
    conv_dir.mkdir(parents=True, exist_ok=True)
    path = _thinking_path(conv_dir)
    idx = 0
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            idx = sum(1 for line in f if line.strip())
    rec = {
        "ts": _now_iso(),
        "idx": idx,
        "question": str(question or "")[:2000],
        "reasoning": text,
        "reasoning_len": len(text),
        "reasoning_tokens": int(reasoning_tokens or 0),
        "model": model or "",
        "prompt_key": (meta or {}).get("prompt_key", ""),
        "memory_key": (meta or {}).get("memory_key", ""),
        "usage": (meta or {}).get("usage", {}),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def load_thinking(conv_dir: Path | None) -> list[dict]:
    """读取会话的全部思考记录（按 idx 顺序；损坏行跳过）。"""
    if conv_dir is None:
        return []
    path = _thinking_path(conv_dir)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    out.sort(key=lambda r: r.get("idx", 0))
    return out


def thinking_by_idx(conv_dir: Path | None) -> dict[int, dict]:
    """{idx: 思考记录}，供窗口按消息里的 thinking_idx 取正文。"""
    return {int(r.get("idx", 0)): r for r in load_thinking(conv_dir)}


def thinking_stats(conv_dir: Path | None) -> dict:
    """会话思考统计（条数/总字数/总思考 token）——UI 标题与审计用。"""
    recs = load_thinking(conv_dir)
    return {
        "count": len(recs),
        "chars": sum(int(r.get("reasoning_len") or 0) for r in recs),
        "tokens": sum(int(r.get("reasoning_tokens") or 0) for r in recs),
    }


# ---------- 会话 ----------
def new_conversation(username: str, title: str, meta: dict | None = None) -> dict:
    """新建一个会话文件夹，返回会话信息 dict（与 list_conversations 同构）。"""
    conv_id = new_conv_id()
    d = conversation_dir(username, conv_id)
    d.mkdir(parents=True, exist_ok=True)
    now = _now_iso()
    record = {
        "conv_id": conv_id,
        "username": username,
        "title": safe_component(title) if title else "新会话",
        "created_at": now,
        "updated_at": now,
        "message_count": 0,
        # 以下为"提示词/记忆/范围"多版本对比的关键记录：每条消息还会再记一遍 meta
        "prompt_version": (meta or {}).get("prompt_version", ""),
        "memory_key": (meta or {}).get("memory_key", ""),
        "scope_label": (meta or {}).get("scope_label", ""),
        "engine": (meta or {}).get("engine", ""),
    }
    _meta_path(d).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def load_conv_meta(conv_dir: Path) -> dict:
    """读取会话元数据；损坏/缺失时返回带空字段的最小 dict（不抛错）。"""
    try:
        return json.loads(_meta_path(conv_dir).read_text(encoding="utf-8"))
    except Exception:
        return {
            "conv_id": conv_dir.name,
            "title": conv_dir.name,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "message_count": 0,
        }


def append_message(conv_dir: Path, role: str, content: str, meta: dict | None = None) -> dict:
    """追加一条对话记录（jsonl 追加写；返回该条消息 dict）。

    role: user | ai。content 为最终展示文本（已 decode）；meta 记录
    提示词版本/记忆方式/范围等，供多版本对比审计。
    """
    conv_dir.mkdir(parents=True, exist_ok=True)
    msg = {
        "ts": _now_iso(),
        "role": role,
        "content": content or "",
        "meta": meta or {},
    }
    with open(_messages_path(conv_dir), "a", encoding="utf-8") as f:
        f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    # 刷新元数据里的更新时间/条数
    rec = load_conv_meta(conv_dir)
    rec["updated_at"] = _now_iso()
    rec["message_count"] = (rec.get("message_count") or 0) + 1
    _meta_path(conv_dir).write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return msg


def load_messages(conv_dir: Path) -> list[dict]:
    """读取会话全部消息（按写入顺序）。损坏行跳过。"""
    msgs: list[dict] = []
    p = _messages_path(conv_dir)
    if not p.exists():
        return msgs
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msgs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return msgs


def list_conversations(username: str) -> list[dict]:
    """列出某用户全部会话（按最后更新倒序，跳过回收区）。

    排序：以 conv.json 的 updated_at 为主、目录 mtime（微秒级）为次——
    同一秒内多次更新的会话也能稳定排在前面。
    """
    root = user_dir(username)
    out: list[tuple[dict, float]] = []
    if not root.is_dir():
        return []
    for child in root.iterdir():
        if not child.is_dir() or child.name.startswith("_"):
            continue
        if not _meta_path(child).exists():
            continue
        rec = load_conv_meta(child)
        out.append((rec, child.stat().st_mtime))
    out.sort(key=lambda t: (t[0].get("updated_at", ""), t[1]), reverse=True)
    return [rec for rec, _m in out]


# ---------- 回收 ----------
def _recycle_dir(username: str) -> Path:
    d = RECYCLE_ROOT / safe_component(username)
    d.mkdir(parents=True, exist_ok=True)
    return d


def delete_conversation(conv_dir: Path) -> Path:
    """删除会话 = 移动到回收区（可恢复）；返回回收后的路径。

    重名（同用户重复删除/恢复）时追加时间戳后缀，保证不覆盖。
    """
    src = Path(conv_dir)
    target = _recycle_dir(src.parent.name if src.parent.name != "_recycle" else src.parent.parent.name) / (
        src.name + "_" + time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:4]
    )
    shutil.move(str(src), str(target))
    return target


def list_recycle() -> list[dict]:
    """列出回收区全部会话（含原属用户信息），供"回收站"管理界面使用。"""
    out: list[dict] = []
    if not RECYCLE_ROOT.is_dir():
        return out
    for user_sub in sorted(RECYCLE_ROOT.iterdir()):
        if not user_sub.is_dir():
            continue
        for child in sorted(user_sub.iterdir()):
            if child.is_dir() and _meta_path(child).exists():
                rec = load_conv_meta(child)
                rec["_recycle_path"] = str(child)
                rec["_owner"] = user_sub.name
                out.append(rec)
    return out


def restore_conversation(recycle_path: Path) -> Path:
    """从回收区恢复到原用户目录；名字冲突时加时间戳后缀。"""
    src = Path(recycle_path)
    owner = src.parent.name
    target = user_dir(owner) / src.name
    if target.exists():
        target = user_dir(owner) / (src.name + "_" + time.strftime("%Y%m%d%H%M%S"))
    shutil.move(str(src), str(target))
    return target


def purge_conversation(recycle_path: Path) -> None:
    """彻底删除回收区中的会话（不可恢复）。"""
    shutil.rmtree(Path(recycle_path), ignore_errors=True)
