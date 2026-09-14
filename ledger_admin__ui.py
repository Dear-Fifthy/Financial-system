# -*- coding: utf-8 -*-
"""台账「全字段」管理窗口（**仅最高管理员**）：看全部字段 + 人工改 + 导出成表格。

与既有两个入口的分工：
  · `table__ui.LedgerStatusDialog`（台账状态）：只切"已收款/已开票"，financial_role 也能用；
  · 本窗口：**完整字段**（含 id/创建时间）、**任意列可改**、**可导出 xlsx/csv**，
    门槛收到 `role_type == 'admin'`（服务端 `_require_admin` 强制，UI 禁用只是明面上的）。

安全与可追溯：
  · 改哪一格就写哪一格：按主键 id 定位（不用"合同编号"这种**本身可编辑**的列当定位键）；
  · 列名必须命中表里的实际列；`id` / `创建时间` 只读（防把主键改坏）；
  · 每次改动写 `logs/ledger_admin/audit_<日期>.jsonl`（旧值→新值、操作人、时间）；
  · 导出用台账自己的中文列名做表头（xlsx 首行冻结、列宽自适应；csv 用 utf-8-sig 防乱码）。

用法：设置 → 「台账全字段管理（最高管理员）」；也可从「台账状态」窗口里的按钮进来。
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from database_serv__infra import (
    ALLOWED_TABLES,
    api_ledger_export,
    api_ledger_rows,
    api_ledger_schema,
    api_ledger_update_cell,
)
from ui_kit__ui import fit_to_screen, grid_row, hint

READONLY_COLS = ("id", "创建时间")
PAGE_SIZE = 200


class LedgerAdminDialog(QDialog):
    """台账全字段浏览 / 人工修改 / 导出（仅最高管理员）。"""

    def __init__(self, parent=None, current_user: dict | None = None,
                 table_name: str = "contract_projects") -> None:
        super().__init__(parent)
        self.current_user = current_user or {}
        self.table_name = table_name if table_name in ALLOWED_TABLES else "contract_projects"
        self._columns: list[str] = []
        self._column_meta: dict[str, dict] = {}
        self._rows: list[dict] = []
        self._dirty: dict[tuple[int, str], object] = {}   # (row_id, col) -> 新值
        self._loading = False
        self.setWindowTitle("台账全字段管理（最高管理员）")
        fit_to_screen(self, 1180, 660)
        self._build_ui()
        self.reload()

    # ---------------- 界面 ----------------
    def _build_ui(self) -> None:
        is_admin = (self.current_user or {}).get("role_type") == "admin"
        layout = QVBoxLayout(self)
        layout.addWidget(hint(
            "这里是台账的**完整字段**（含 id / 创建时间）：双击单元格即可修改，改完点「保存修改」。\n"
            "· id / 创建时间 只读；其余列按类型校验（数字列填数字、是否类列填 是/否）。\n"
            "· 每次改动都会写审计（logs/ledger_admin/audit_<日期>.jsonl），可追溯。\n"
            "· 「导出为表格」把整张台账写成 xlsx/csv 存到你选的位置（表头就是这些列名）。"
            + ("" if is_admin else "\n⚠️ 当前角色不是最高管理员：只能查看，保存与导出会被服务端拒绝。")
        ))

        top = QHBoxLayout()
        top.addWidget(QLabel("台账："))
        self.cb_table = QComboBox()
        for t, disp in ALLOWED_TABLES.items():
            self.cb_table.addItem(disp, t)
        self.cb_table.setCurrentIndex(max(0, self.cb_table.findData(self.table_name)))
        self.cb_table.currentIndexChanged.connect(self._on_table_changed)
        top.addWidget(self.cb_table)
        top.addWidget(QLabel("搜索："))
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("按任意列模糊查找（合同号 / 公司 / 金额 / 日期…）")
        self.ed_search.returnPressed.connect(self.reload)
        top.addWidget(self.ed_search, 1)
        self.btn_load = QPushButton("读取")
        self.btn_load.clicked.connect(self.reload)
        top.addWidget(self.btn_load)
        layout.addLayout(top)

        self.tbl = QTableWidget(0, 0)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                 | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.tbl, 1)

        self.lbl_note = QLabel("")
        self.lbl_note.setWordWrap(True)
        self.lbl_note.setStyleSheet("color:#555;")
        layout.addWidget(self.lbl_note)

        self.btn_save = QPushButton("保存修改")
        self.btn_save.setToolTip("把改动过的单元格逐格写回台账（按 id 定位，写审计）")
        self.btn_save.clicked.connect(self._save)
        self.btn_revert = QPushButton("放弃修改")
        self.btn_revert.clicked.connect(self.reload)
        self.btn_xlsx = QPushButton("导出为 Excel…")
        self.btn_xlsx.clicked.connect(lambda: self._export("xlsx"))
        self.btn_csv = QPushButton("导出为 CSV…")
        self.btn_csv.clicked.connect(lambda: self._export("csv"))
        self.btn_open = QPushButton("打开导出目录")
        self.btn_open.clicked.connect(self._open_last_dir)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.accept)
        layout.addLayout(grid_row(
            [self.btn_save, self.btn_revert, self.btn_xlsx, self.btn_csv, self.btn_open,
             self.btn_close], columns=3))

        bottom = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bottom.rejected.connect(self.reject)
        bottom.accepted.connect(self.accept)
        layout.addWidget(bottom)
        self._last_export_dir = ""

    # ---------------- 数据 ----------------
    def _on_table_changed(self) -> None:
        self.table_name = self.cb_table.currentData() or "contract_projects"
        self.reload()

    def reload(self) -> None:
        self._dirty.clear()
        ok, schema = api_ledger_schema(self.current_user, self.table_name)
        if not ok:
            QMessageBox.warning(self, "读取台账结构失败", str(schema))
            self.lbl_note.setText(f"读取台账结构失败：{schema}")
            return
        self._column_meta = {c["column_name"]: c for c in schema["columns"]}
        ok, data = api_ledger_rows(self.current_user, self.table_name,
                                   limit=PAGE_SIZE, search=self.ed_search.text())
        if not ok:
            QMessageBox.critical(self, "读取台账失败", str(data))
            # 非最高管理员会走到这里：把原因留在窗口上，别让界面看起来是"空的"
            self.lbl_note.setText(f"读取台账失败：{data}")
            return
        self._columns = list(data["columns"])
        self._rows = list(data["rows"])

        self._loading = True
        self.tbl.clear()
        self.tbl.setColumnCount(len(self._columns))
        self.tbl.setHorizontalHeaderLabels(self._columns)
        self.tbl.setRowCount(len(self._rows))
        for r, row in enumerate(self._rows):
            for c, col in enumerate(self._columns):
                val = row.get(col)
                text = "" if val is None else str(val)
                item = QTableWidgetItem(text)
                if col in READONLY_COLS or not self._can_edit:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    if col in READONLY_COLS:
                        item.setForeground(Qt.GlobalColor.gray)
                self.tbl.setItem(r, c, item)
        hdr = self.tbl.horizontalHeader()
        for c in range(len(self._columns)):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        if self._columns:
            hdr.setSectionResizeMode(len(self._columns) - 1, QHeaderView.ResizeMode.Stretch)
        self._loading = False
        self.lbl_note.setText(
            f"{ALLOWED_TABLES.get(self.table_name, self.table_name)}：共 {data['total']} 行，"
            f"本次显示 {len(self._rows)} 行（最多 {PAGE_SIZE}）｜列 {len(self._columns)} 个"
            + ("｜有未保存修改" if self._dirty else "")
            + ("" if self._can_edit else "｜当前角色只读"))

    @property
    def _can_edit(self) -> bool:
        return (self.current_user or {}).get("role_type") == "admin"

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        r, c = item.row(), item.column()
        if r >= len(self._rows) or c >= len(self._columns):
            return
        col = self._columns[c]
        if col in READONLY_COLS:
            return
        row_id = self._rows[r].get("id")
        old = self._rows[r].get(col)
        new = item.text()
        if ("" if old is None else str(old)) == new:
            return
        self._dirty[(int(row_id), col)] = new
        item.setBackground(Qt.GlobalColor.yellow)
        self.lbl_note.setText(f"待保存 {len(self._dirty)} 处改动（黄色底）——点「保存修改」写库")

    def _save(self) -> None:
        if not self._dirty:
            QMessageBox.information(self, "没有改动", "没有需要保存的修改。")
            return
        if QMessageBox.question(
            self, "保存修改",
            f"把 {len(self._dirty)} 处改动写回「{ALLOWED_TABLES.get(self.table_name)}」？\n\n"
            "· 逐格按 id 写回，按列类型校验；\n"
            "· 每次改动都会写审计（logs/ledger_admin/audit_<日期>.jsonl）。",
        ) != QMessageBox.StandardButton.Yes:
            return
        ok_n, fails = 0, []
        for (row_id, col), val in list(self._dirty.items()):
            ok, msg = api_ledger_update_cell(self.current_user, self.table_name, row_id,
                                             col, val)
            if ok:
                ok_n += 1
                self._dirty.pop((row_id, col), None)
            else:
                fails.append(f"id={row_id} 的「{col}」：{msg}")
        if fails:
            QMessageBox.warning(self, "部分未保存",
                                f"成功 {ok_n} 处，失败 {len(fails)} 处：\n" + "\n".join(fails[:10]))
        else:
            QMessageBox.information(self, "已保存", f"{ok_n} 处改动已写回台账。")
        self.reload()

    # ---------------- 导出 ----------------
    def _export(self, fmt: str) -> None:
        disp = ALLOWED_TABLES.get(self.table_name, self.table_name)
        default_dir = self._last_export_dir or str(Path.home() / "Desktop")
        default_name = f"{disp}_{datetime.now():%Y%m%d_%H%M%S}.{fmt}"
        filt = "Excel 工作簿 (*.xlsx)" if fmt == "xlsx" else "CSV 文件 (*.csv)"
        path, _sel = QFileDialog.getSaveFileName(
            self, f"导出「{disp}」到…", os.path.join(default_dir, default_name), filt)
        if not path:
            return
        ok, msg = api_ledger_export(self.current_user, self.table_name, path, fmt=fmt)
        if ok:
            self._last_export_dir = str(Path(path).parent)
            QMessageBox.information(self, "导出完成", msg)
        else:
            QMessageBox.warning(self, "导出失败", msg)

    def _open_last_dir(self) -> None:
        d = self._last_export_dir
        if not d:
            QMessageBox.information(self, "还没有导出过",
                                    "先点「导出为 Excel…」或「导出为 CSV…」选好保存位置。")
            return
        import subprocess

        try:
            os.startfile(d)          # noqa: S606 （Windows 打开资源管理器）
        except Exception:
            subprocess.Popen(["explorer", d])
