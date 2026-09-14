"""文件名乱码修复的 UI 组件：后台扫描/改名 Worker + 预览确认对话框。

流程（由 MainWindow 编排）：
  拖入文件夹(修复模式)
    → 询问"报告保存位置"
    → FixScanWorker（后台递归扫描，只读文件名）→ 预览列表
    → FixPreviewDialog（勾选哪些要改，默认全选；只显示 原名→新名）
    → FixApplyWorker（后台原地改名 + 写报告）→ 汇总提示
"""
from __future__ import annotations
from ui_kit__ui import fit_to_screen
from ui_kit__ui import hint


from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import filename_fix__ui as fixmod


class FixScanWorker(QObject):
    """后台扫描：遍历文件夹（含子目录）收集待改名清单（不读文件内容）。"""

    done = Signal(object)      # list[dict]
    progress = Signal(int)     # 累计已检查文件名数（UI 显示进度用）
    failed = Signal(str)

    def __init__(self, root: str) -> None:
        super().__init__()
        self._root = root
        self._cancel_requested = False

    def request_cancel(self) -> None:
        """请求取消（扫描线程会在下一个目录处停下）。"""
        self._cancel_requested = True

    @Slot()
    def run(self) -> None:
        try:
            items, _notes = fixmod.scan_folder(
                self._root,
                progress=self.progress.emit,
                cancel=lambda: self._cancel_requested,
            )
            self.done.emit(items)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class FixApplyWorker(QObject):
    """后台执行：原地改名 + 写处理报告。"""

    done = Signal(object, object)  # (summary dict, errors list)
    failed = Signal(str)

    def __init__(self, items: list[dict], report_dir: str) -> None:
        super().__init__()
        self._items = items
        self._report_dir = report_dir

    @Slot()
    def run(self) -> None:
        try:
            report_lines: list[str] = []
            ok_count, errors = fixmod.perform_renames(self._items, report_lines)
            report_path = ""
            try:
                import os
                import time

                os.makedirs(self._report_dir, exist_ok=True)
                report_path = os.path.join(
                    self._report_dir,
                    f"乱码修复报告_{time.strftime('%Y%m%d_%H%M%S')}.txt",
                )
                header = f"文件名乱码修复报告（{time.strftime('%Y-%m-%d %H:%M:%S')}）\n" \
                         f"成功 {ok_count} 项，失败 {len(errors)} 项\n\n"
                with open(report_path, "w", encoding="utf-8") as f:
                    f.write(header)
                    f.write("\n".join(report_lines) if report_lines else "（无）")
                    if errors:
                        f.write("\n\n失败明细：\n" + "\n".join(errors))
            except Exception as exc:
                report_path = f"报告写入失败：{exc}"
            summary = {"ok": ok_count, "errors": errors, "report": report_path}
            self.done.emit(summary, errors)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class FixPreviewDialog(QDialog):
    """预览确认：列出 原名 → 新名，可勾选；确定后才执行改名。

    只展示文件名（+所在目录），不改任何内容；取消 = 什么都不做。
    """

    def __init__(self, items: list[dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items = list(items)
        self._checks: list[QCheckBox] = []
        self.setWindowTitle("文件名乱码修复 - 预览确认")
        fit_to_screen(self, 860, 480)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(hint(f"共 {len(self._items)} 个文件将被改名"
                              "（仅文件名；后缀/位置/内容均不变）。取消勾选 = 该文件不处理。"))
        self._table = QTableWidget(len(self._items), 3)
        self._table.setHorizontalHeaderLabels(["处理", "原名", "修复后名"])
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        import os

        for i, item in enumerate(self._items):
            check = QCheckBox()
            check.setChecked(True)
            self._checks.append(check)
            self._table.setCellWidget(i, 0, check)
            self._table.setItem(i, 1, QTableWidgetItem(os.path.basename(item["old"])))
            self._table.setItem(i, 2, QTableWidgetItem(item["new"]))
            # 用 tooltip 放完整路径便于核对
            tool = f"{item['path']}\n原因：{item.get('reason', '')}"
            self._table.item(i, 1).setToolTip(tool)
            self._table.item(i, 2).setToolTip(tool)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        layout.addWidget(self._table, 1)

        row = QHBoxLayout()
        self._btn_all = QPushButton("全选")
        self._btn_all.clicked.connect(lambda: self._set_all(True))
        self._btn_none = QPushButton("全不选")
        self._btn_none.clicked.connect(lambda: self._set_all(False))
        self._btn_invert = QPushButton("反选")
        self._btn_invert.clicked.connect(self._invert)
        row.addWidget(self._btn_all)
        row.addWidget(self._btn_none)
        row.addWidget(self._btn_invert)
        row.addStretch(1)
        self._btn_ok = QPushButton("确定改名")
        self._btn_ok.clicked.connect(self.accept)
        self._btn_cancel = QPushButton("取消")
        self._btn_cancel.clicked.connect(self.reject)
        row.addWidget(self._btn_ok)
        row.addWidget(self._btn_cancel)
        layout.addLayout(row)

    def _set_all(self, value: bool) -> None:
        for c in self._checks:
            c.setChecked(value)

    def _invert(self) -> None:
        for c in self._checks:
            c.setChecked(not c.isChecked())

    def selected_items(self) -> list[dict]:
        """返回勾选中的待改名项。"""
        return [it for it, c in zip(self._items, self._checks) if c.isChecked()]
