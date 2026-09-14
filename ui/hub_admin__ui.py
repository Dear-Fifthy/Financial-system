"""Hub 状态 / 文件格式化窗口（最高管理员）。

⚠️ 命名：本窗口管的是 **Hub**（脱敏产物目录）；本程序里「仓库」指工作区/隔离环境，
   两者不是一回事（见 `workspace__infra`）。

需求（本轮三条）：
  · **Hub 状态**：只看**用户拖进来的源文件**，不列生成的 JSON / 概括 / 向量；
    每行显示：源文件是否还在原位、hub 产物在不在、事实/段落/节点/边/向量计数、分类；
    选中行下方显示"概括"——**概括只作定位索引，真正取数要回原文核对**。
  · **删除指定文件**：连同它的全部数据（L1 文档/段落/事实、hub 产物、向量分块、
    内容指纹）与**所有边**（两端任一涉及它的边，含其它文档连过来的）一起删除；
    编号记入退役墓碑、永久留空，源文件默认不动。
  · **文件格式化**：清空 hub 与 output 的全部文件、向量、以及表里的派生数据
    （台账 contract_projects、project_archive、提案、特征哈希、图节点/边…）；
    **默认保留**脱敏映射、本公司主体、项目登记、权限与列/特征字典（可勾选一并清）。

权限：入口只在 `role_type == 'admin'` 的"设置"菜单里出现；真正的删改仍由
`doc_lifecycle__desens` 校验并写审计日志（`logs/format/format_<date>.jsonl`）。
"""
from __future__ import annotations
from ui.ui_kit__ui import fit_to_screen


from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

COLUMNS = ["编号", "状态", "源文件", "分类", "源文件在", "hub产物", "页", "表",
           "事实", "段落", "节点", "边", "向量", "更新时间"]


class HubAdminDialog(QDialog):
    """Hub 状态窗口：查看 / 删除指定文件 / 格式化。"""

    def __init__(self, parent=None, current_user: dict | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user or {}
        self.setWindowTitle("Hub 状态 - 文件 / 数据管理（最高管理员）")
        fit_to_screen(self, 1180, 640)
        self._items: list[dict] = []
        self._shown: list[dict] = []
        self._build_ui()
        self.reload()

    # ---------------- 界面 ----------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        head = QLabel(
            "这里**只列你拖进来的源文件**（生成的 JSON / 概括 / 向量不单独成行）。"
            "「状态」=已入库 表示库里有记录；=仅落盘产物 表示 hub 里有残留 JSON 但库里没记录。"
            "选中一行可在下方看它的概括（**只作定位索引，取数请回原文核对**）。"
        )
        head.setWordWrap(True)
        layout.addWidget(head)

        self.lbl_stats = QLabel("")
        self.lbl_stats.setStyleSheet("color: #444; font-weight: bold;")
        self.lbl_stats.setWordWrap(True)
        layout.addWidget(self.lbl_stats)

        self.chk_orphan = QCheckBox("显示落盘残留产物（库里没有记录的生成 JSON；默认隐藏）")
        self.chk_orphan.setChecked(False)
        self.chk_orphan.toggled.connect(lambda _on: self._populate())
        layout.addWidget(self.chk_orphan)

        self.tbl = QTableWidget(0, len(COLUMNS))
        self.tbl.setHorizontalHeaderLabels(COLUMNS)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.verticalHeader().setVisible(False)
        hdr = self.tbl.horizontalHeader()
        for col in range(len(COLUMNS)):
            mode = (QHeaderView.ResizeMode.Stretch if col == 2
                    else QHeaderView.ResizeMode.ResizeToContents)
            hdr.setSectionResizeMode(col, mode)
        self.tbl.itemSelectionChanged.connect(self._on_pick)
        layout.addWidget(self.tbl, 3)

        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("选中一行查看：源文件路径 / hub 产物 / 概括（定位索引）/ 数据计数")
        self.detail.setMinimumHeight(120)
        layout.addWidget(self.detail, 1)

        # 按钮文案压短、完整含义进 tooltip，并按网格排列：
        # 原来 4 个长按钮挤一行，窗口一窄"文件格式化…（清空 hub / output / 向量 / 表格数据）"
        # 就被裁成半个字（实测 434px 的文字塞进 139px）。
        row = QHBoxLayout()
        b_ref = QPushButton("刷新")
        b_ref.clicked.connect(self.reload)
        b_del = QPushButton("删除选中")
        b_del.setToolTip("删除选中文件，连同它的全部数据（事实/段落/向量）与所有边")
        b_del.clicked.connect(self._delete_selected)
        b_fmt = QPushButton("格式化…")
        b_fmt.setToolTip("文件格式化：清空 hub / output / 向量 / 表格派生数据")
        b_fmt.clicked.connect(self._format_all)
        b_open = QPushButton("打开目录")
        b_open.setToolTip("在资源管理器里打开当前仓库的 hub 目录")
        b_open.clicked.connect(self._open_hub_dir)
        row.addWidget(b_ref)
        row.addWidget(b_del)
        row.addStretch(1)
        row.addWidget(b_open)
        row.addWidget(b_fmt)
        layout.addLayout(row)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        close.accepted.connect(self.accept)
        layout.addWidget(close)

    # ---------------- 数据 ----------------
    def reload(self) -> None:
        import desens.doc_lifecycle__desens as life

        try:
            data = life.list_repository()
        except Exception as exc:
            QMessageBox.critical(self, "读取 Hub 状态失败", f"{type(exc).__name__}: {exc}")
            return
        self._items = data.get("items", [])
        s = data.get("summary", {})
        self.lbl_stats.setText(
            f"源文件 {s.get('docs', 0)} 份（另有 {s.get('orphan', 0)} 份仅落盘残留）｜"
            f"事实 {s.get('facts', 0)}｜向量 {s.get('chunks', 0)}｜"
            f"图节点 {s.get('nodes', 0)} / 边 {s.get('edges', 0)}｜"
            f"hub JSON {s.get('hub_files', 0)} 个｜逐页缓存目录 {s.get('cache_dirs', 0)} 个｜"
            f"退役编号 {s.get('deleted', 0)} 个"
            + (f"｜源文件已不在原位 {s['source_missing']} 份" if s.get("source_missing") else "")
            + (f"｜库里有记录但 hub 产物已丢 {s['hub_missing']} 份" if s.get("hub_missing") else "")
        )
        self._populate()

    def _populate(self) -> None:
        """只列**用户拖进来的源文件**；落盘残留产物按勾选决定是否显示。"""
        show_orphan = self.chk_orphan.isChecked()
        self._shown = [it for it in self._items
                       if show_orphan or it.get("state") == "已入库"]
        self.tbl.setRowCount(len(self._shown))
        for r, it in enumerate(self._shown):
            cells = [
                "" if it.get("doc_no") is None else str(it["doc_no"]),
                it.get("state", ""),
                it.get("name", ""),
                it.get("category", "") or "—",
                "是" if it.get("source_exists") else "否",
                "是" if it.get("hub_exists") else "否",
                str(it.get("page_count", 0)),
                str(it.get("table_count", 0)),
                str(it.get("fact_count", 0)),
                str(it.get("segment_count", 0)),
                str(it.get("node_count", 0)),
                str(it.get("edge_count", 0)),
                str(it.get("chunk_count", 0)),
                it.get("updated_at", ""),
            ]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if c in (0, 6, 7, 8, 9, 10, 11, 12):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.tbl.setItem(r, c, item)
        self.detail.clear()

    def _selected(self) -> dict | None:
        row = self.tbl.currentRow()
        if row < 0 or row >= len(self._shown):
            return None
        return self._shown[row]

    def _on_pick(self) -> None:
        it = self._selected()
        if not it:
            return
        lines = [
            f"源文件：{it.get('source_path') or '（无记录）'}"
            f"{'' if it.get('source_exists') else '   ← 源文件已不在原位（记录仍在）'}",
            f"hub 产物：{(it.get('hub_file') or '(无)')}"
            f"{'' if it.get('hub_exists') else '   ← 库里没找到对应 JSON'}",
            f"doc_key：{it.get('doc_key', '')}",
            f"分类：{it.get('category') or '未分类'}｜页 {it.get('page_count', 0)}｜"
            f"表 {it.get('table_count', 0)}｜事实 {it.get('fact_count', 0)}｜"
            f"段落 {it.get('segment_count', 0)}｜节点 {it.get('node_count', 0)}｜"
            f"边 {it.get('edge_count', 0)}｜向量 {it.get('chunk_count', 0)}",
            f"文件哈希：{(it.get('file_hash') or '')[:16]}｜hub 哈希：{(it.get('hub_hash') or '')[:16]}",
            "",
            "概括（**只作定位索引，取数请回原文核对**）：",
            (it.get("summary") or "（无概括）")[:1200],
        ]
        self.detail.setPlainText("\n".join(lines))

    # ---------------- 删除 ----------------
    def _delete_selected(self) -> None:
        import desens.doc_lifecycle__desens as life

        it = self._selected()
        if not it:
            QMessageBox.information(self, "请选择", "请先在列表里选择一份文件。")
            return
        if it.get("state") != "已入库":
            name = it.get("name", "")
            if QMessageBox.question(
                self, "清理残留产物",
                f"「{name}」在库里没有记录，只有落盘残留产物。\n"
                f"是否直接删掉它的 hub JSON 与伴生文件？",
            ) != QMessageBox.StandardButton.Yes:
                return
            report = life.delete_document(it.get("doc_key", ""), user=self.current_user,
                                          reason="管理员在 Hub 窗口清理残留产物")
            QMessageBox.information(self, "已清理", report.summary())
            self.reload()
            return

        msg = (
            f"删除「{it.get('name')}」（doc_no={it.get('doc_no')}）？\n\n"
            f"将一并删除：\n"
            f"· 该文档的全部数据：L1 文档 / 段落 / 事实清单（{it.get('fact_count', 0)} 条）\n"
            f"· hub 产物与其伴生文件（.l1.json / .features.json / .ai.log）+ 逐页缓存\n"
            f"· 图中它的节点（{it.get('node_count', 0)}）与**所有边**（{it.get('edge_count', 0)}，"
            f"含其它文档连过来的边）\n"
            f"· RAG 向量分块（{it.get('chunk_count', 0)}）、内容指纹索引\n\n"
            f"其编号会记入退役墓碑并**永久留空**；源文件**不会**被删除（不破坏源文件夹）。"
        )
        box = QMessageBox(self)
        box.setWindowTitle("确认删除")
        box.setText(msg)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        try:
            report = life.delete_document(it.get("doc_key", ""), user=self.current_user,
                                          reason="管理员在 Hub 状态窗口删除")
        except Exception as exc:
            QMessageBox.critical(self, "删除失败", f"{type(exc).__name__}: {exc}")
            return
        QMessageBox.information(self, "删除完成", report.summary())
        self.reload()

    # ---------------- 格式化 ----------------
    def _format_all(self) -> None:
        dlg = FormatConfirmDialog(self, current_user=self.current_user)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.reload()
            QMessageBox.information(self, "文件格式化完成", dlg.result_text or "已清空。")

    def _open_hub_dir(self) -> None:
        import os
        from pathlib import Path

        from infra import proc__infra as _proc

        d = Path(__file__).resolve().parents[1] / "hub"
        d.mkdir(exist_ok=True)
        try:
            os.startfile(str(d))          # noqa: S606 - Windows only
        except Exception:
            _proc.popen(["explorer", str(d)])


class FormatConfirmDialog(QDialog):
    """格式化确认：先**预演**（只统计不动数据），再勾选、再确认。"""

    def __init__(self, parent=None, current_user: dict | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user or {}
        self.setWindowTitle("文件格式化 - 二次确认")
        fit_to_screen(self, 760, 560)
        self.result_text: str | None = None
        self._build_ui()
        self._preview()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        warn = QLabel(
            "格式化会**清空**：hub 下所有文档 JSON 与伴生文件、output 下所有逐页缓存、"
            "向量分块（document_chunks），以及表格里的派生数据——"
            "L1 文档/段落/事实、图节点与边、内容指纹、特征哈希、"
            "台账 contract_projects（合同台账）、invoice_ledger（发票台账）、"
            "待入账队列 ledger_inbox、项目归档 project_archive、AI 项目提案 project_proposals、"
            "退役墓碑 deleted_documents。\n\n"
            "**保留**：源文件不动（不破坏源文件夹）、脱敏映射、本公司主体、项目登记、"
            "用户/权限、列字典与特征字典。\n\n"
            "格式化不可撤销（源文件还在，重新拖入即可重建）。"
        )
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #a33;")
        layout.addWidget(warn)

        self.txt = QPlainTextEdit()
        self.txt.setReadOnly(True)
        layout.addWidget(self.txt, 1)

        # 复选框文字不能换行，长文案在窄窗口必被裁 → 压短 + tooltip 说全
        self.chk_map = QCheckBox("连脱敏映射一起清空")
        self.chk_map.setToolTip("连脱敏映射一起清空（entity_mapping_* 及 hub/_mapping；"
                                "编号将从 1 重新开始）")
        self.chk_map.setChecked(False)
        self.chk_map.toggled.connect(lambda _on: self._preview())
        layout.addWidget(self.chk_map)

        row = QHBoxLayout()
        b_prev = QPushButton("重新预演")
        b_prev.clicked.connect(self._preview)
        row.addWidget(b_prev)
        row.addStretch(1)
        layout.addLayout(row)

        buttons = QDialogButtonBox()
        b_ok = buttons.addButton("确认格式化（不可撤销）", QDialogButtonBox.ButtonRole.AcceptRole)
        b_no = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        b_ok.clicked.connect(self._do_format)
        b_no.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def _preview(self) -> None:
        import desens.doc_lifecycle__desens as life

        purge_map = self.chk_map.isChecked()
        try:
            rep = life.format_preview(include_mappings=purge_map)
        except Exception as exc:
            self.txt.setPlainText(f"预演失败：{type(exc).__name__}: {exc}")
            return
        lines = [
            "【预演】当前将清空的内容：",
            f"· hub 文档 JSON {rep.hub_files} 个 / 伴生文件 {rep.hub_sidecars} 个 / "
            f"其它日志 {rep.hub_logs} 个",
            f"· output 逐页缓存目录 {rep.cache_dirs} 个",
            f"· 合计约 {rep.bytes_freed / 1024 / 1024:.1f} MB",
        ]
        if purge_map:
            lines.append(f"· 脱敏映射文件 hub/_mapping 下 {rep.hub_mapping} 个（已勾选：一并清空）")
        else:
            lines.append(f"· hub/_mapping 下 {rep.hub_mapping} 个脱敏映射文件（保留）")
        lines += ["", "库内将清空的表（行数）："]
        for k, v in rep.tables.items():
            lines.append(f"  - {k}: {v}")
        lines.append("")
        lines.append("将保留的表（行数）：")
        for k, v in rep.kept.items():
            lines.append(f"  - {k}: {v}")
        self.txt.setPlainText("\n".join(lines))

    def _do_format(self) -> None:
        import desens.doc_lifecycle__desens as life

        if QMessageBox.warning(
            self, "再次确认",
            "真的要格式化吗？\n\n"
            "hub / output 的全部文件、向量、以及表里的派生数据都会被清空，此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            rep = life.format_repository(user=self.current_user,
                                         include_mappings=self.chk_map.isChecked())
        except Exception as exc:
            QMessageBox.critical(self, "格式化失败", f"{type(exc).__name__}: {exc}")
            return
        self.result_text = rep.summary()
        self.accept()
