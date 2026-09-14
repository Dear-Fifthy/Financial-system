"""AI 输入守卫（隐私边界）：**AI 链路只允许在 hub/ 的文件上进行**。

规则（按需求）：
    · 允许：`hub/` 及其子目录里的文件——这是唯一"已脱敏 + 已结构化"的落盘层；
    · 禁止：`input/`（原始件）、`output/`（逐页 OCR 原文缓存）、`logs/`、
      任何源目录（用户桌面/共享盘）以及 hub 之外的临时文件；
    · 越界调用**直接抛 AIInputForbidden**（不降级、不静默、不用"尽力而为"），
      同时写审计日志 `logs/ai/guard_<日期>.log`。

为什么必须硬拦：AI 一旦看到未脱敏明文，隐私就已经离开本机边界；
"AI 只看 hub" 这条约束放在**唯一实现点**上，不能依赖每个调用方自觉。

测试/备用根目录：可用环境变量 `DSH_HUB_DIR` 覆盖 hub 位置（仅用于自测与隔离环境）。
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

__all__ = [
    "AIInputForbidden",
    "hub_dir",
    "is_hub_path",
    "require_hub_file",
    "require_hub_dir",
    "audit_rejection",
]


class AIInputForbidden(RuntimeError):
    """AI 输入越界：目标文件不在 hub/ 内（隐私边界拒绝）。"""


def hub_dir() -> Path:
    """当前生效的 hub 目录（DSH_HUB_DIR 覆盖 > hub_pipeline__desens.HUB_DIR > 默认 ./hub）。"""
    override = os.getenv("DSH_HUB_DIR", "").strip()
    if override:
        return Path(override).resolve()
    try:
        import desens.hub_pipeline__desens as hub_pipeline__desens  # 惰性导入：避免与 hub_pipeline 形成导入环

        return Path(hub_pipeline__desens.HUB_DIR).resolve()
    except Exception:
        return (Path(__file__).resolve().parents[1] / "hub").resolve()


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def is_hub_path(path: str | Path | None) -> bool:
    """路径是否位于 hub/ 内（含子目录）。不存在的路径也按位置判断。"""
    if path is None:
        return False
    try:
        p = Path(path).resolve()
    except Exception:
        return False
    root = hub_dir()
    return p == root or _is_inside(p, root)


def audit_rejection(path: str | Path | None, purpose: str, reason: str) -> None:
    """越界拒绝审计日志：只记路径与用途，不读文件内容（避免二次泄露）。"""
    try:
        log_dir = Path(__file__).resolve().parents[1] / "logs" / "ai"
        log_dir.mkdir(parents=True, exist_ok=True)
        line = (
            f"{datetime.now().isoformat(timespec='seconds')}\t"
            f"REJECT\t{purpose}\t{reason}\t{path}\n"
        )
        with (log_dir / f"guard_{datetime.now():%Y%m%d}.log").open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # 审计失败不能反过来打断主流程


def _reject(path: str | Path | None, purpose: str, kind: str) -> "AIInputForbidden":
    root = hub_dir()
    reason = f"{kind}不在 AI 允许范围（仅 {root}）"
    audit_rejection(path, purpose, reason)
    return AIInputForbidden(
        f"{purpose}被拒绝：{path}\n"
        f"AI 只能在 hub/ 内的文件上运行（当前允许根目录：{root}）。\n"
        f"原始件/OCR 缓存/源目录一律禁止作为 AI 输入——请先经脱敏流水线落到 hub/。"
    )


def require_hub_file(path: str | Path, *, purpose: str = "AI 输入") -> Path:
    """要求 `path` 是 hub/ 内的**文件**；否则抛 AIInputForbidden。"""
    p = Path(path)
    if not is_hub_path(p):
        raise _reject(p, purpose, "文件")
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"{purpose}文件不存在：{p}")
    return p


def require_hub_dir(folder: str | Path, *, purpose: str = "AI 输入目录") -> Path:
    """要求 `folder` 是 hub/ 内的**目录**；否则抛 AIInputForbidden。"""
    p = Path(folder)
    if not is_hub_path(p):
        raise _reject(p, purpose, "目录")
    if not p.exists() or not p.is_dir():
        raise FileNotFoundError(f"{purpose}不存在：{p}")
    return p
