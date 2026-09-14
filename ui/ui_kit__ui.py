# -*- coding: utf-8 -*-
"""界面适配小工具（屏幕自适应 + 区块可自由拖动 + 文字不被裁掉）。

存在的理由（实测问题）：
  · 窗口/对话框原来写死初始尺寸（主窗 1380×850、部分对话框 1180×640、1280×720）。
    在本机 1440×900（缩放 200%）上刚好勉强放得下，但在 1366×768 或 125%/150% 缩放的
    笔记本上，**窗口会超出屏幕** → 底部按钮/说明文字看不到、也没法拖出来。
  · 中央区原来是 QVBoxLayout：预览/字段/日志 三块的高度由"拉伸因子"决定，
    **用户拖不动**边界；左右两栏虽然用了 QSplitter，但按钮一整排挤在窄栏里，
    文字被硬裁（"删除选中（含节点/边/向量）"这种长文案首当其冲）。
  · 对话框里的长说明文字没开自动换行，一窄就被切掉。

用法：
    fit_to_screen(self, 1180, 640)          # 初始尺寸自动裁到屏幕内并居中
    split = vbox_splitter([预览, 字段, 日志], sizes=[420, 120, 220])
    save_geometry(self, "main") / restore_geometry(self, "main")
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QRect, QSettings, Qt
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QLabel,
    QScrollArea,
    QSplitter,
    QWidget,
)

# 窗口尺寸/分栏位置的落盘位置。
# ⚠️ 用 **INI 文件**而不是 QSettings 默认的注册表：实测本机（受限运行环境）写注册表
#    会返回 `Status.AccessError` 且**静默失败**（用户拖好的布局下次打开全丢）。
#    放 INI 到 logs/ 下既能写、又跟着项目走、还能一眼看到/手工删掉。
ROOT = Path(__file__).resolve().parents[1]
ORG = os.getenv("DSH_SETTINGS_ORG", "fin_system")
APP = os.getenv("DSH_SETTINGS_APP", "Table")
DEFAULT_GEOMETRY = QRect(0, 0, 1440, 900)


def settings_file() -> Path:
    """UI 状态文件（自测用 DSH_SETTINGS_APP 换成另一个文件名，互不干扰）。"""
    suffix = "" if APP == "Table" else f"_{APP}"
    return ROOT / "logs" / f"ui_state{suffix}.ini"


def _settings() -> QSettings:
    path = settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    return QSettings(str(path), QSettings.Format.IniFormat)


def clear_settings() -> None:
    """清掉记住的窗口布局（排障/自测用）。"""
    s = _settings()
    s.clear()
    s.sync()


# =========================================================
# 1. 屏幕尺寸
# =========================================================
def available_geometry() -> QRect:
    """当前屏幕**可视区域**（逻辑像素，已排除任务栏）。取不到时给个保守默认。"""
    app = QApplication.instance()
    scr = app.primaryScreen() if app is not None else None
    return scr.availableGeometry() if scr is not None else QRect(DEFAULT_GEOMETRY)


def screen_size() -> tuple[int, int]:
    av = available_geometry()
    return av.width(), av.height()


def fit_to_screen(win: QWidget, width: int, height: int, *, min_w: int = 520,
                  min_h: int = 320, margin: int = 40, center: bool = True) -> tuple[int, int]:
    """把窗口初始尺寸**裁到屏幕可视区域内**并居中（放得下就保持原设计尺寸）。

    · 屏幕比设计尺寸小 → 取屏幕能给的（小屏/高缩放不再顶出屏幕）；
    · 屏幕放得下 → 不小于 `min_w/min_h`（这些是"低于就没法用"的底线，
      比如主窗 900×560：再窄按钮文字就要开始被裁）；
    · 同时把 QWidget 的最小尺寸压到屏幕之内，保证用户**拖得小**、底部按钮**够得着**。
    """
    av = available_geometry()
    max_w = max(320, av.width() - margin)
    max_h = max(260, av.height() - margin)
    floor_w, floor_h = min(min_w, max_w), min(min_h, max_h)
    w = max(floor_w, min(int(width), max_w))
    h = max(floor_h, min(int(height), max_h))
    win.resize(w, h)
    if center:
        win.move(av.x() + max(0, (av.width() - w) // 2),
                 av.y() + max(0, (av.height() - h) // 2))
    hint = win.minimumSizeHint()
    win.setMinimumSize(max(floor_w, min(hint.width(), max_w)),
                       max(floor_h, min(hint.height(), max_h)))
    return w, h


# =========================================================
# 2. 可自由拖动的分栏
# =========================================================
def vbox_splitter(widgets: list[QWidget], *, sizes: list[int] | None = None,
                  stretch: list[int] | None = None, name: str = "") -> QSplitter:
    """纵向分栏：每块之间都能拖动（分隔条不隐藏、不允许塌成 0）。"""
    sp = QSplitter(Qt.Orientation.Vertical)
    if name:
        sp.setObjectName(name)
    for w in widgets:
        sp.addWidget(w)
    sp.setChildrenCollapsible(False)
    sp.setHandleWidth(6)
    if sizes:
        sp.setSizes(sizes)
    for i, s in enumerate(stretch or []):
        sp.setStretchFactor(i, s)
    return sp


def hbox_splitter(widgets: list[QWidget], *, sizes: list[int] | None = None,
                  stretch: list[int] | None = None, name: str = "") -> QSplitter:
    sp = QSplitter(Qt.Orientation.Horizontal)
    if name:
        sp.setObjectName(name)
    for w in widgets:
        sp.addWidget(w)
    sp.setChildrenCollapsible(False)
    sp.setHandleWidth(6)
    if sizes:
        sp.setSizes(sizes)
    for i, s in enumerate(stretch or []):
        sp.setStretchFactor(i, s)
    return sp


def grid_row(widgets: list[QWidget], *, columns: int = 3, spacing: int = 6) -> QGridLayout:
    """一排按钮改成"多行网格"：窄窗口里自动换行，文字不再被硬裁。

    Qt 没有 flex-wrap，所以按固定列数排；列数取 3 时，7 个按钮排成 3 行，
    所需宽度 ≈ 最宽那个按钮的 3 倍，而不是 7 个按钮之和。
    """
    grid = QGridLayout()
    grid.setSpacing(spacing)
    grid.setContentsMargins(0, 0, 0, 0)
    for i, w in enumerate(widgets):
        grid.addWidget(w, i // max(columns, 1), i % max(columns, 1))
    for c in range(max(columns, 1)):
        grid.setColumnStretch(c, 0)
    return grid


# =========================================================
# 3. 文字不被裁
# =========================================================
def wrap(*labels: QLabel, min_width: int = 240) -> None:
    """给说明性 QLabel 开自动换行（并把最小宽度压小，允许窗口缩窄）。"""
    for label in labels:
        if label is None:
            continue
        label.setWordWrap(True)
        label.setMinimumWidth(min(min_width, label.minimumWidth() or min_width))
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)


def hint(text: str, *, min_width: int = 240) -> QLabel:
    """一行说明文字（自动换行 + 可选中复制）。"""
    label = QLabel(text)
    wrap(label, min_width=min_width)
    return label


def elide_to_tooltip(widget: QWidget, text: str, *, max_chars: int = 28) -> None:
    """把长文案塞进 tooltip，界面上留短文案：窄窗口下不会被切一半。"""
    widget.setToolTip(text)
    if len(text) > max_chars:
        widget.setText(text[:max_chars] + "…")


def scrollable(content: QWidget, *, min_height: int = 200) -> QScrollArea:
    """把内容包进滚动区：屏幕小的时候内容可滚动，**底部按钮永远够得着**。"""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(content)
    area.setMinimumHeight(min_height)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    return area


# =========================================================
# 4. 记住用户自己拖出来的窗口大小
# =========================================================
def restore_geometry(win: QWidget, key: str, *, default: tuple[int, int] | None = None,
                     min_w: int = 900, min_h: int = 560) -> None:
    """恢复上次的窗口大小/分栏位置/停靠状态；没有记录或记录已超出屏幕时按默认尺寸。"""
    settings = _settings()
    geo = settings.value(f"geometry/{key}")
    if geo is not None:
        win.restoreGeometry(geo)
        clamp_to_screen(win)
    elif default:
        fit_to_screen(win, default[0], default[1], min_w=min_w, min_h=min_h)
    state = settings.value(f"state/{key}")
    if state is not None and hasattr(win, "restoreState"):
        try:
            win.restoreState(state)
        except Exception:
            pass
    # 所有**有名字**的分隔条都恢复用户拖出来的位置（窗口内任意层级）
    for sp in win.findChildren(QSplitter):
        name = sp.objectName()
        if not name:
            continue
        saved = settings.value(f"splitter/{key}/{name}")
        if not saved:
            continue
        try:
            sp.setSizes([int(x) for x in saved])
        except Exception:
            pass


def save_geometry(win: QWidget, key: str) -> None:
    settings = _settings()
    settings.setValue(f"geometry/{key}", win.saveGeometry())
    if hasattr(win, "saveState"):
        try:
            settings.setValue(f"state/{key}", win.saveState())
        except Exception:
            pass
    for sp in win.findChildren(QSplitter):
        name = sp.objectName()
        if name:
            settings.setValue(f"splitter/{key}/{name}", [int(x) for x in sp.sizes()])
    settings.sync()          # 立刻落盘（不依赖析构时的缓冲刷新）


def clamp_to_screen(win: QWidget) -> None:
    """把窗口拉回屏幕内（换了小屏/拔了外接屏之后，别让窗口跑到看不见的地方）。"""
    av = available_geometry()
    w = min(win.width(), av.width())
    h = min(win.height(), av.height())
    x = min(max(win.x(), av.x()), max(av.x(), av.right() - w + 1))
    y = min(max(win.y(), av.y()), max(av.y(), av.bottom() - h + 1))
    win.resize(w, h)
    win.move(x, y)
