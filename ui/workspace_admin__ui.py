# -*- coding: utf-8 -*-
"""仓库（工作区）管理窗口：新建 / 切换 / 体检 / 重置 / 删除登记。

「仓库」= 一套完全隔离的环境（独立数据库 + 独立 hub/input/output + 独立密钥）；
AI 接口与检索方式（图遍历/RAG）是**全局**的，不随仓库切换。

界面契约（重要）：
  · 切换仓库会让**当前登录用户**失效（用户表也在各自仓库里），所以本窗口返回
    `Accepted` 时调用方必须退出登录、回到登录框；
  · 重置/删除都是破坏性操作，但**可回滚**：旧库改名加 `__bak_时间戳` 保留、
    旧数据目录移到 `workspaces/_bak/`，窗口里会明确显示回滚位置；
  · 新建仓库是"完全新建"：空库 + 空目录 + 独立密钥，**零数据、零编号**
    （所以第一个用户会是最高管理员）。
"""
from __future__ import annotations
from ui.ui_kit__ui import fit_to_screen


from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
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

import infra.workspace__infra as ws

COLUMNS = ["仓库", "短名", "数据库", "hub 目录", "文档", "事实", "边", "登记", "用户", "状态"]


class WorkspaceDialog(QDialog):
    def __init__(self, parent=None, current_user: dict | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user or {}
        self.switched = False           # 是否发生过切换（调用方据此退出登录）
        self.setWindowTitle("仓库管理（环境隔离）")
        fit_to_screen(self, 1120, 620)
        self._rows: list[dict] = []
        self._build_ui()
        self.reload()

    # ---------------- 界面 ----------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        head = QLabel(
            "一个「仓库」= 一套完全隔离的环境：**独立数据库 + 独立 hub/input/output + 独立加密密钥**。\n"
            "切来切去之后，上一个仓库的文档、事实、图边、脱敏编号、台账都**不会再被读到**；"
            "AI 接口与检索方式（图遍历 / RAG）是全局的，不随仓库变化。\n"
            "切换仓库会因为「用户表也在各仓库里」而需要**重新登录**。"
        )
        head.setWordWrap(True)
        root.addWidget(head)

        row = QHBoxLayout()
        self._btn_switch = QPushButton("切换到选中仓库")
        self._btn_switch.clicked.connect(self._switch_selected)
        self._btn_new = QPushButton("新建仓库（完全空环境）…")
        self._btn_new.clicked.connect(self._create_new)
        self._btn_check = QPushButton("体检（重新数一遍）")
        self._btn_check.clicked.connect(self.reload)
        self._btn_open = QPushButton("打开该仓库目录")
        self._btn_open.clicked.connect(self._open_dir)
        row.addWidget(self._btn_switch)
        row.addWidget(self._btn_new)
        row.addWidget(self._btn_check)
        row.addWidget(self._btn_open)
        row.addStretch(1)
        root.addLayout(row)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.doubleClicked.connect(lambda *_: self._switch_selected())
        root.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self._btn_reset = QPushButton("清空重来（可回滚）…")
        self._btn_reset.clicked.connect(self._reset_selected)
        self._btn_delete = QPushButton("删除登记（可回滚）…")
        self._btn_delete.clicked.connect(self._delete_selected)
        self._btn_close = QPushButton("关闭")
        self._btn_close.clicked.connect(self.reject)
        bottom.addWidget(self._btn_reset)
        bottom.addWidget(self._btn_delete)
        bottom.addStretch(1)
        bottom.addWidget(self._btn_close)
        root.addLayout(bottom)

    # ---------------- 数据 ----------------
    def reload(self) -> None:
        cur = (ws.active() or {}).get("slug", "")
        items = ws.list_workspaces()
        self._rows = []
        self.table.setRowCount(0)
        for w in items:
            slug = w.get("slug", "")
            try:
                c = ws.counts(slug)
            except Exception as exc:
                c = {"error": f"{type(exc).__name__}: {exc}"}
            self._rows.append({"entry": w, "counts": c})
            r = self.table.rowCount()
            self.table.insertRow(r)
            if c.get("error"):
                cells = [w.get("name", ""), slug, w.get("db", ""), w.get("hub", ""),
                         "❌", "❌", "❌", "❌", "❌", str(c["error"])[:60]]
            else:
                marks = []
                if slug == cur:
                    marks.append("★ 当前")
                if w.get("legacy"):
                    marks.append("既有环境")
                cells = [
                    w.get("name", ""), slug, w.get("db", ""), w.get("hub", ""),
                    str(c.get("docs", 0)), str(c.get("facts", 0)), str(c.get("graph_edges", 0)),
                    str(c.get("entity_total", 0)), str(c.get("users", 0)),
                    " ".join(marks),
                ]
            for i, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if slug == cur:
                    item.setForeground(Qt.GlobalColor.darkGreen)
                self.table.setItem(r, i, item)
        self._btn_switch.setEnabled(bool(items))

    def _selected(self) -> dict | None:
        r = self.table.currentRow()
        if r < 0 or r >= len(self._rows):
            QMessageBox.information(self, "未选择", "请先在表格里选一个仓库。")
            return None
        return self._rows[r]

    # ---------------- 动作 ----------------
    def _switch_selected(self) -> None:
        row = self._selected()
        if not row:
            return
        w = row["entry"]
        if w.get("slug") == (ws.active() or {}).get("slug"):
            QMessageBox.information(self, "已是当前仓库", f"当前就在「{w.get('name')}」里。")
            return
        c = row["counts"]
        reply = QMessageBox.question(
            self,
            "切换仓库",
            f"切换到「{w.get('name')}」（{w.get('slug')}）？\n\n"
            f"该仓库：文档 {c.get('docs', 0)}｜事实 {c.get('facts', 0)}｜"
            f"边 {c.get('graph_edges', 0)}｜登记 {c.get('entity_total', 0)}｜"
            f"用户 {c.get('users', 0)}\n"
            f"库：{w.get('db')}\nhub：{w.get('hub')}\n\n"
            "切换后本窗口会退出登录（用户表也在各仓库里），请用该仓库的账号重新登录。",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if (c.get("users") or 0) == 0:
            QMessageBox.warning(
                self, "该仓库还没有账号",
                "这个仓库里一个用户都没有，切过去会登录不上。\n"
                "请先在本窗口用「新建仓库」带上首个管理员，或切过去后按首次注册流程注册。",
            )
            return
        try:
            info = ws.switch(w.get("slug"))
        except Exception as exc:
            QMessageBox.critical(self, "切换失败", f"{type(exc).__name__}: {exc}")
            return
        self.switched = True
        QMessageBox.information(
            self, "已切换",
            f"已切换到「{w.get('name')}」\n库：{info['db']}\nhub：{info['hub']}\n"
            f"密钥：{info['key_file']}\n\n（AI 接口与检索方式不随仓库变化）",
        )
        self.accept()

    def _create_new(self) -> None:
        name, ok = QInputDialog.getText(self, "新建仓库", "仓库名称（界面上显示的名字）：")
        if not (ok and name.strip()):
            return
        name = name.strip()
        slug, _ok2 = QInputDialog.getText(
            self, "新建仓库", "仓库短名（英文/数字，用于数据库名，可留空自动生成）："
        )
        admin = None
        reply = QMessageBox.question(
            self, "首个管理员",
            "是否为该仓库创建一个最高管理员账号？\n（选否就等首次打开时按注册流程注册）",
        )
        if reply == QMessageBox.StandardButton.Yes:
            user, ok_u = QInputDialog.getText(self, "首个管理员", "用户名：")
            if not (ok_u and user.strip()):
                return
            pwd, ok_p = QInputDialog.getText(
                self, "首个管理员", "密码：", QLineEdit.EchoMode.Password
            )
            if not (ok_p and pwd):
                return
            email, _e = QInputDialog.getText(self, "首个管理员", "邮箱：")
            phone, _ph = QInputDialog.getText(self, "首个管理员", "手机号：")
            admin = (user.strip(), pwd,
                     (email or "").strip() or f"{user.strip()}@example.com",
                     (phone or "").strip() or "13900000000")
        try:
            entry = ws.create(name, slug=(slug or "").strip(), make_active=False, admin=admin)
        except Exception as exc:
            QMessageBox.critical(self, "新建失败", f"{type(exc).__name__}: {exc}")
            return
        QMessageBox.information(
            self, "已新建",
            f"仓库「{entry['name']}」已建好（完全空环境：零数据、零编号）。\n"
            f"库：{entry['db']}\nhub：{entry['hub']}\n密钥：{entry['key_file']}\n"
            f"{('首个用户：' + entry['admin_result']) if entry.get('admin_result') else ''}\n\n"
            "密钥请自行备份：**丢失则该仓库的密文永久不可解**。\n"
            "（没有自动切过去，需要时在列表里选中它点「切换」。）",
        )
        self.reload()

    def _open_dir(self) -> None:
        row = self._selected()
        if not row:
            return
        import os
        from pathlib import Path

        from infra import proc__infra as _proc

        path = Path(ws.ROOT) / str(row["entry"].get("hub") or "hub")
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(path))          # noqa: S606  （Windows 打开资源管理器）
        except Exception:
            _proc.popen(["explorer", str(path)])

    def _reset_selected(self) -> None:
        row = self._selected()
        if not row:
            return
        w = row["entry"]
        if w.get("legacy"):
            QMessageBox.warning(self, "不允许", "既有仓库（legacy）指向当前的老库与老目录，"
                                                "不允许原地重置。请先新建一个仓库再切过去。")
            return
        reply = QMessageBox.question(
            self, "清空重来",
            f"清空「{w.get('name')}」？\n\n旧库会改名保留为 {w.get('db')}__bak_时间戳，\n"
            "旧数据目录会移到 workspaces/_bak/ 下，**都可回滚**；\n"
            "然后建一个全新的空库 + 空目录。",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            r = ws.reset(w.get("slug"), confirm=True)
        except Exception as exc:
            QMessageBox.critical(self, "重置失败", f"{type(exc).__name__}: {exc}")
            return
        QMessageBox.information(self, "已重置",
                                f"新库：{r['db']}（空）\n可回滚：{r['archived_db']}\n"
                                f"旧目录：{r['archived_dir']}")
        self.reload()

    def _delete_selected(self) -> None:
        row = self._selected()
        if not row:
            return
        w = row["entry"]
        if (ws.active() or {}).get("slug") == w.get("slug"):
            QMessageBox.warning(self, "不允许", "不能删除正在使用的仓库：先切到别的仓库。")
            return
        if w.get("legacy"):
            QMessageBox.warning(self, "不允许", "既有仓库（legacy）不允许删除登记。")
            return
        reply = QMessageBox.question(
            self, "删除仓库登记",
            f"删除「{w.get('name')}」的登记？\n\n"
            f"库 {w.get('db')} 会改名保留为 ...__bak_时间戳，数据目录移到 workspaces/_bak/，\n"
            "都可回滚；只是从列表里去掉。",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            r = ws.delete(w.get("slug"), confirm=True)
        except Exception as exc:
            QMessageBox.critical(self, "删除失败", f"{type(exc).__name__}: {exc}")
            return
        QMessageBox.information(self, "已删除登记",
                                f"可回滚：{r['archived_db']}\n{r['archived_dir']}")
        self.reload()
