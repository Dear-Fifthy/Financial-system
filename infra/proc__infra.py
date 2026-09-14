# -*- coding: utf-8 -*-
"""子进程静默化（Windows）：别让窗口程序 fork 出黑色控制台窗口。

**为什么要有这个模块（2026-09-15 用户实测）**：
用 `launch_table.bat` 启动（走 `pythonw.exe`，进程自己没有控制台）后，屏幕上每隔几秒
就闪一个黑色控制台窗口，抢焦点，人在 DSH 网页那边就打不了字。

原因是 Windows 的规则：**GUI 子系统程序 fork 控制台子系统程序时，系统会为子进程
新建一个控制台窗口**（父进程没有控制台可继承）。本项目里 `nvidia-smi`（性能监控每 3 秒
一次）、`wmic`、`taskkill`、`powershell`、soffice 全是控制台程序，所以闪个不停。
修法就是给这些调用加 `CREATE_NO_WINDOW`（0x08000000）。

**怎么用**：凡是"启动外部程序"的地方都别直接 import subprocess，
改成本模块的 `run` / `popen`（参数与 `subprocess` 完全一致）：

    from infra import proc__infra as proc
    proc.run(["nvidia-smi", ...], capture_output=True, timeout=5)

需要 `subprocess.PIPE` / `subprocess.TimeoutExpired` 这类常量与异常时，
仍然可以 `import subprocess`，只是**不要**拿它去 `run`/`Popen`。
"""
from __future__ import annotations

import subprocess
import sys

# 非 Windows 平台没有这个常量，取 0（等价于不传）；Windows 上是 0x08000000
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _with_no_window(kwargs: dict) -> dict:
    """给 kwargs 补上 CREATE_NO_WINDOW；调用方显式传了 creationflags 就尊重调用方。"""
    if NO_WINDOW and "creationflags" not in kwargs:
        kwargs["creationflags"] = NO_WINDOW
    return kwargs


def run(*args, **kwargs):
    """`subprocess.run` 的静默版（自动带 CREATE_NO_WINDOW）。"""
    return subprocess.run(*args, **_with_no_window(kwargs))


def popen(*args, **kwargs):
    """`subprocess.Popen` 的静默版。"""
    return subprocess.Popen(*args, **_with_no_window(kwargs))


def check_output(*args, **kwargs):
    """`subprocess.check_output` 的静默版。"""
    return subprocess.check_output(*args, **_with_no_window(kwargs))


def call(*args, **kwargs):
    """`subprocess.call` 的静默版。"""
    return subprocess.call(*args, **_with_no_window(kwargs))


if __name__ == "__main__":
    # 自检：确认常量与包装都按预期（真跑一次 cmd，看有没有被加上 flags）
    print(f"平台={sys.platform}  CREATE_NO_WINDOW={NO_WINDOW:#x}")
    r = run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True)
    print("包装后的 run 返回码:", r.returncode, "输出:", (r.stdout or "").strip())
