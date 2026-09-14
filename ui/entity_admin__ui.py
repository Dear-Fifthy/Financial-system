"""脱敏登记表管理窗口（最高管理员）：查看 / 扫描脏登记 / 删除 / 人工登记 / 项目分支登记。

为什么需要人工入口：
  · 自动脱敏难免**漏登记**（新公司名、新项目名）→ 管理员应能直接补登，下一次扫描即生效；
  · 自动脱敏也可能**错登记**（实测：`CO0113=株`、`CO0112=20 年 月 日`、`CO0128=张佳平`），
    错登记的代价是"全库编号语义被污染"（AI 会把编号当公司名，据此编造结论），
    所以必须能**逐条查看 + 删除 + 写审计**；
  · 项目/公司的**分支**（标段/片区/分公司）按"主编号 + 后缀"约定登记：`PJ0007-01`。

界面分工：
  · 左侧类别下拉（公司/人员/日期/项目/银行名称/银行账号/税号/身份证/银行卡/电话）；
    明文类别显示真值，敏感类别只显示编号（明文不落盘，本窗口也拿不到）。
  · 「扫描脏登记」把不合规的条目挑出来并给出原因；可一键勾选建议删除项（仍要人工点删除）。
  · 「删除选中」会提示该编号在 hub 里还被多少篇文档引用（删除登记≠改正文，需要重扫才会刷新）。
  · 「新增登记」走 `entity_repair.register` 的结构校验；项目/公司可选"作为某主编号的分支"。
"""
from __future__ import annotations
from ui.ui_kit__ui import fit_to_screen


from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
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

import desens.entity_repair__desens as repair

CATEGORIES = [
    ("company", "公司/单位"),
    ("party", "人员姓名"),
    ("project", "项目名称"),
    ("date", "日期"),
    ("bank_name", "开户银行"),
    ("bank_account", "银行账号"),
    ("tax_id", "税号/统一社会信用代码"),
    ("id_card", "身份证号"),
    ("bank_card", "银行卡号"),
    ("phone", "电话/手机号"),
]
VISIBLE = {"company", "party", "date"}
COLUMNS = ["编号", "值（敏感类别不外显）", "主编号", "分支", "可疑原因"]


class EntityAdminDialog(QDialog):
    def __init__(self, parent=None, current_user: dict | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user or {}
        self.setWindowTitle("脱敏登记表管理（最高管理员）")
        fit_to_screen(self, 1000, 620)
        self._rows: list[dict] = []
        self._dirty: dict[str, str] = {}
        self._build_ui()
        self.reload()

    # ---------------- 界面 ----------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        head = QLabel(
            "这里能看到脱敏时建立的**编号 ↔ 实体**对照表（敏感类别只存指纹与密文，本窗口也看不到明文）。\n"
            "自动脱敏会漏登也会错登：漏登的在这里补，错登的（把标签/单位/金额当成公司或人名）删掉。\n"
            "删除只删登记，**不会改 hub 正文里已写入的编号**——要让正文刷新，删除后重新扫描相关文档。"
        )
        head.setWordWrap(True)
        root.addWidget(head)

        row = QHBoxLayout()
        row.addWidget(QLabel("类别："))
        self.cb_cat = QComboBox()
        for key, label in CATEGORIES:
            self.cb_cat.addItem(label, key)
        self.cb_cat.currentIndexChanged.connect(lambda _i: self.reload())
        row.addWidget(self.cb_cat)
        self.chk_only_dirty = QCheckBox("只显示可疑条目")
        self.chk_only_dirty.toggled.connect(lambda _b: self.reload())
        row.addWidget(self.chk_only_dirty)
        self.lbl_stats = QLabel("")
        self.lbl_stats.setStyleSheet("color:#444;font-weight:bold;")
        self.lbl_stats.setWordWrap(True)     # 计数行随内容变长，窄窗口下要能换行
        row.addWidget(self.lbl_stats)
        row.addStretch(1)
        root.addLayout(row)

        self.tbl = QTableWidget(0, len(COLUMNS))
        self.tbl.setHorizontalHeaderLabels(COLUMNS)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.verticalHeader().setVisible(False)
        hdr = self.tbl.horizontalHeader()
        for c in range(len(COLUMNS)):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch if c == 1
                                     else QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self.tbl, 3)

        self.detail = QLabel("")
        self.detail.setStyleSheet("color:#666;")
        self.detail.setWordWrap(True)
        root.addWidget(self.detail)

        # 6 个按钮按网格排（3 列）：窗口窄时换行，文字不会被裁。
        # 文案压短、完整含义进 tooltip（原来"登记分支（主编号+后缀）…"这类长文案在窄窗口被切掉）。
        b_ref = QPushButton("刷新")
        b_ref.clicked.connect(self.reload)
        b_scan = QPushButton("扫描可疑")
        b_scan.setToolTip("把不合规的登记挑出来并给出原因")
        b_scan.clicked.connect(self._scan)
        b_del = QPushButton("删除选中")
        b_del.setToolTip("删除选中的登记（会写审计；正文里的编号需重扫才刷新）")
        b_del.clicked.connect(self._delete_selected)
        b_add = QPushButton("新增登记…")
        b_add.clicked.connect(self._register)
        b_branch = QPushButton("登记分支…")
        b_branch.setToolTip("把某条登记登记成主编号的分支（主编号 + 后缀，如 PJ0007-01）")
        b_branch.clicked.connect(self._register_branch)
        b_dup = QPushButton("重复检查")
        b_dup.setToolTip("跨类别重复检查（同一实体被登记到多个类别的情况）")
        b_dup.clicked.connect(self._check_duplicates)
        import ui.ui_kit__ui as kit

        root.addLayout(kit.grid_row([b_ref, b_scan, b_del, b_add, b_branch, b_dup],
                                    columns=3))

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        close.accepted.connect(self.accept)
        root.addWidget(close)

    # ---------------- 数据 ----------------
    def _category(self) -> str:
        return str(self.cb_cat.currentData())

    def reload(self) -> None:
        cat = self._category()
        try:
            entries = repair.list_entries(cat)
        except Exception as exc:
            QMessageBox.critical(self, "读取失败", f"{type(exc).__name__}: {exc}")
            return
        self._dirty = {d["code"]: d["reason"] for d in repair.scan_dirty((cat,))}
        rows = entries
        if self.chk_only_dirty.isChecked():
            rows = [e for e in entries if e["code"] in self._dirty]
        self._rows = rows
        self.tbl.setRowCount(len(rows))
        for r, e in enumerate(rows):
            reason = self._dirty.get(e["code"], "")
            cells = [e["code"], e.get("value") or e.get("masked") or "",
                     e.get("parent_code") or "", str(e.get("branch_seq") or ""), reason]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if reason:
                    item.setForeground(Qt.GlobalColor.darkRed)
                self.tbl.setItem(r, c, item)
        self.lbl_stats.setText(f"共 {len(entries)} 条｜可疑 {len(self._dirty)} 条｜当前显示 {len(rows)} 条")
        self.detail.setText("")

    def _selected_codes(self) -> list[str]:
        rows = sorted({i.row() for i in self.tbl.selectedIndexes()})
        return [self._rows[r]["code"] for r in rows if 0 <= r < len(self._rows)]

    # ---------------- 操作 ----------------
    def _scan(self) -> None:
        cat = self._category()
        dirty = repair.scan_dirty((cat,))
        self.chk_only_dirty.setChecked(True)
        self.reload()
        if not dirty:
            QMessageBox.information(self, "扫描完成", "本类别没有发现可疑登记。")
            return
        lines = [f"· {d['code']} = {d['value'][:40]} ← {d['reason']}" for d in dirty[:20]]
        QMessageBox.information(
            self, f"发现 {len(dirty)} 条可疑登记",
            "\n".join(lines) + ("\n…" if len(dirty) > 20 else "")
            + "\n\n（已切到「只看可疑」视图；请逐条确认后删除——真名可能只是写法不规范。）")

    def _delete_selected(self) -> None:
        codes = self._selected_codes()
        if not codes:
            QMessageBox.information(self, "请选择", "请先选中要删除的登记（可多选）。")
            return
        cat = self._category()
        preview = repair.cleanup(codes, category=cat, dry_run=True)
        refs = {c: len(v) for c, v in (preview.get("hub_refs") or {}).items()}
        ref_txt = "\n".join(f"  · {c}：{n} 篇 hub 文档仍引用该编号" for c, n in refs.items() if n)
        msg = (f"删除 {len(codes)} 条【{dict(CATEGORIES).get(cat, cat)}】登记？\n\n"
               f"{ref_txt}\n" if ref_txt else "")
        msg += ("\n⚠️ 删除只删登记，hub 正文里已写入的编号不会自动改；"
                "需要正文一致请删除后**重新扫描**相关文档。\n\n确定删除？")
        if QMessageBox.question(self, "确认删除", msg) != QMessageBox.StandardButton.Yes:
            return
        reason, ok = QInputDialog.getText(self, "删除原因", "原因（写入审计日志）：")
        if not ok:
            return
        rep = repair.cleanup(codes, category=cat, user=self.current_user,
                             reason=reason or "管理员清理脏登记")
        QMessageBox.information(self, "已删除",
                               f"删除 {len(rep['deleted'])} 条"
                               + (f"；未找到 {len(rep['missing'])} 条" if rep["missing"] else "")
                               + f"\n审计：logs/desens_repair/repair_*.jsonl")
        self.reload()

    def _register(self) -> None:
        cat = self._category()
        value, ok = QInputDialog.getText(self, "新增登记", "要登记的实体名称/值：")
        if not ok or not value.strip():
            return
        res = repair.register(cat, value.strip(), user=self.current_user)
        if not res.get("ok"):
            QMessageBox.warning(self, "登记被拒绝", res.get("msg", ""))
            return
        QMessageBox.information(self, "已登记", f"{res['msg']}\n（重新扫描相关文档即生效）")
        self.reload()

    def _register_branch(self) -> None:
        """登记"某主编号的分支"：主编号下拉 + 分支名称 → 生成 主编号-NN。"""
        cat = self._category()
        if cat not in ("project", "company"):
            QMessageBox.information(self, "仅项目/公司支持分支",
                                    "分支编号用于「同一主体的不同标段/片区/分公司」，"
                                    "当前类别不支持。")
            return
        entries = [e for e in repair.list_entries(cat) if not e.get("parent_code")]
        if not entries:
            QMessageBox.information(self, "没有主编号", "请先登记主实体（不带分支）。")
            return
        labels = [f"{e['code']} {e.get('value') or ''}" for e in entries]
        pick, ok = QInputDialog.getItem(self, "选择主编号", "分支属于哪个主实体：", labels, 0, False)
        if not ok:
            return
        parent = pick.split(" ", 1)[0]
        label, ok2 = QInputDialog.getText(self, "分支名称", "分支说明（如 一标段/原5片区/北京分公司）：")
        if not ok2:
            return
        res = repair.register(cat, label.strip() or parent, user=self.current_user,
                              parent_code=parent, branch_label=label.strip())
        if not res.get("ok"):
            QMessageBox.warning(self, "登记被拒绝", res.get("msg", ""))
            return
        QMessageBox.information(self, "已登记分支",
                               f"{res['msg']}\n提示词里已声明：主编号相同=同一主体的不同分支。")
        self.reload()

    def _check_duplicates(self) -> None:
        dups = repair.cross_category_duplicates()
        if not dups:
            QMessageBox.information(self, "检查完成", "没有发现跨类别重复登记。")
            return
        lines = [f"· {d['value'][:30]} → {[f'{c}:{code}' for c, code in d['entries']]}"
                 for d in dups[:20]]
        QMessageBox.information(
            self, f"发现 {len(dups)} 组跨类别重复",
            "\n".join(lines) + "\n\n建议：人名只留「人员」表、公司只留「公司」表，"
            "把错的那条删掉（否则同一实体两个编号，AI 会当成两个主体）。")
