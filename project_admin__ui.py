"""项目名称加密管理窗口（最高管理员）：自定义加密 + 分类 + AI 提案审批。

需求（4）界面口径：
  · **自定义加密**：管理员登记项目名称（名称必填、简称选填）→ 系统给出编号 PJ####，
    名称/简称以 Fernet 密文入库（明文不落盘），此后文档里该名称一律替换为编号；
  · **分类**：可给加密内容分类——"已有即选"（下拉框），"未有可加"（下拉框里
    「＋ 新增分类…」即时创建）；
  · **与 AI 判断交叉验证**：AI 提案（未命中登记表的项目名）在下方列表待审，
    管理员**一键登记**（批准并加密）或**驳回**（AI 提案、我审批）。

权限：入口只对 `role_type == 'admin'` 显示；本窗口内的读写仍各自走
project_registry 的校验（名称明文仅在 admin 有 entity:decrypt:project 时展示）。
"""
from __future__ import annotations
from ui_kit__ui import fit_to_screen


from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
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
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import project_registry__desens as pr
import self_entity__desens

NEW_CATEGORY_LABEL = "＋ 新增分类…"


class ProjectEditDialog(QDialog):
    """新增/编辑一条项目加密登记（名称必填、简称选填、分类已有即选未有可加）。"""

    def __init__(self, parent=None, *, current_user: dict | None = None, entry: dict | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self.entry = entry or {}
        self.setWindowTitle("项目加密登记 - 编辑" if entry else "项目加密登记 - 新增")
        fit_to_screen(self, 520, 300)
        self._result: dict | None = None

        layout = QVBoxLayout(self)
        tip = QLabel(
            "登记后该项目名称将被加密（库内只存密文 + 编号），文档中出现时一律显示为编号；"
            "简称（选填）用于同一项目的别名匹配。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #666;")
        layout.addWidget(tip)

        form = QFormLayout()
        self.ed_name = QLineEdit(self.entry.get("name") or "")
        self.ed_name.setPlaceholderText("必填，如：南苑新村消防维保")
        form.addRow("项目名称 *", self.ed_name)

        self.ed_short = QLineEdit(self.entry.get("short_name") or "")
        self.ed_short.setPlaceholderText("选填，如：南苑维保")
        form.addRow("项目简称", self.ed_short)

        self.cb_category = QComboBox()
        self.cb_category.setEditable(False)
        self._reload_categories(self.entry.get("category"))
        self.cb_category.currentIndexChanged.connect(self._on_category_changed)
        form.addRow("分类", self.cb_category)

        self.ed_note = QLineEdit(self.entry.get("note") or "")
        form.addRow("备注", self.ed_note)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存并加密")
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _reload_categories(self, selected: str | None = None) -> None:
        self.cb_category.blockSignals(True)
        self.cb_category.clear()
        self.cb_category.addItem("（不分类）", None)
        for name in pr.list_categories():
            self.cb_category.addItem(name, name)
        self.cb_category.addItem(NEW_CATEGORY_LABEL, "__new__")
        if selected:
            idx = self.cb_category.findData(selected)
            if idx < 0:
                self.cb_category.addItem(selected, selected)
                idx = self.cb_category.findData(selected)
            self.cb_category.setCurrentIndex(idx)
        self.cb_category.blockSignals(False)

    def _on_category_changed(self, _idx: int) -> None:
        """选中「＋ 新增分类…」→ 立即弹输入框（未有可加）。"""
        if self.cb_category.currentData() != "__new__":
            return
        name, ok = QInputDialog.getText(self, "新增分类", "分类名称：")
        if ok and name.strip():
            try:
                pr.add_category(name.strip(), user=self.current_user)
            except Exception as exc:
                QMessageBox.warning(self, "新增分类失败", str(exc))
        self._reload_categories(name.strip() if ok and name.strip() else None)

    def _on_ok(self) -> None:
        name = self.ed_name.text().strip()
        if not name:
            QMessageBox.warning(self, "名称必填", "项目名称不能为空。")
            return
        cat = self.cb_category.currentData()
        self._result = {
            "name": name,
            "short_name": self.ed_short.text().strip() or None,
            "category": None if cat in (None, "__new__") else cat,
            "note": self.ed_note.text().strip() or None,
        }
        self.accept()

    def payload(self) -> dict | None:
        return self._result


class ProjectRegistryDialog(QDialog):
    """最高管理员：项目名称加密登记表 + AI 提案审批。"""

    def __init__(self, parent=None, current_user: dict | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user or {}
        self.setWindowTitle("项目名称加密 - 最高管理员")
        fit_to_screen(self, 900, 560)
        self._entries: list[dict] = []
        self._proposals: list[dict] = []
        self._build_ui()
        self.reload()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        head = QLabel(
            "自定义加密：登记项目名称（必填）+ 简称（选填）+ 分类（已有即选、未有可加）。"
            "登记即加密——库内只存密文与编号，文档/AI 链路中出现该名称一律替换为编号。"
        )
        head.setWordWrap(True)
        layout.addWidget(head)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_registry_tab(), "加密登记表")
        self.tabs.addTab(self._build_proposal_tab(), "AI 提案待审批")
        layout.addWidget(self.tabs, 1)

        bottom = QHBoxLayout()
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #666;")
        bottom.addWidget(self.lbl_status, 1)
        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(self.reload)
        bottom.addWidget(btn_refresh)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        bottom.addWidget(btn_close)
        layout.addLayout(bottom)

    def _build_registry_tab(self) -> QWidget:
        w = QWidget()
        box = QVBoxLayout(w)
        self.tbl = QTableWidget(0, 7)
        self.tbl.setHorizontalHeaderLabels(
            ["编号", "项目名称", "简称", "分类", "状态", "创建人", "创建时间"]
        )
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hdr = self.tbl.horizontalHeader()
        for col in (0, 3, 4, 5, 6):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        box.addWidget(self.tbl, 1)

        row = QHBoxLayout()
        for text, slot in (
            ("新增（名称必填）", self._add),
            ("编辑", self._edit),
            ("停用 / 启用", self._toggle_active),
            ("删除", self._delete),
        ):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        box.addLayout(row)

        self.txt_audit = QTextEdit()
        self.txt_audit.setReadOnly(True)
        self.txt_audit.setMaximumHeight(110)
        box.addWidget(QLabel("最近审计（仅编号/指纹，不含明文名称）"))
        box.addWidget(self.txt_audit)
        return w

    def _build_proposal_tab(self) -> QWidget:
        w = QWidget()
        box = QVBoxLayout(w)
        box.addWidget(QLabel(
            "AI 判断出的项目名称与登记表交叉验证后，未命中/近似的进入此列表（AI 提案、我审批）："
            "「登记并加密」= 批准；「驳回」= 不采用。"
        ))
        self.tbl_prop = QTableWidget(0, 5)
        self.tbl_prop.setHorizontalHeaderLabels(["#", "AI 提案名称", "来源文档", "建议分类", "时间"])
        self.tbl_prop.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_prop.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tbl_prop.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hp = self.tbl_prop.horizontalHeader()
        hp.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hp.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hp.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hp.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hp.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        box.addWidget(self.tbl_prop, 1)

        row = QHBoxLayout()
        b_ok = QPushButton("登记并加密（批准）")
        b_ok.clicked.connect(self._approve)
        b_no = QPushButton("驳回")
        b_no.clicked.connect(self._reject)
        b_all = QPushButton("显示全部（含已处理）")
        b_all.clicked.connect(lambda: self._load_proposals(status=None))
        row.addWidget(b_ok)
        row.addWidget(b_no)
        row.addStretch(1)
        row.addWidget(b_all)
        box.addLayout(row)
        return w

    # ---------- 数据 ----------
    def reload(self) -> None:
        self._load_entries()
        self._load_proposals()
        self._load_audit()

    def _load_entries(self) -> None:
        try:
            self._entries = pr.list_entries(self.current_user, include_inactive=True)
        except Exception as exc:
            self._entries = []
            QMessageBox.critical(self, "读取登记表失败", str(exc))
        self.tbl.setRowCount(len(self._entries))
        for r, e in enumerate(self._entries):
            name = e.get("name") or e.get("name_masked") or "（无解密权限）"
            cells = [
                e["code"], name, e.get("short_name") or "", e.get("category") or "",
                "启用" if e.get("is_active") else "已停用",
                e.get("created_by") or "", e.get("created_at") or "",
            ]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, e)
                self.tbl.setItem(r, c, item)
        self.lbl_status.setText(
            f"登记项 {len(self._entries)} 条；待审批提案 {len(self._proposals)} 条"
        )

    def _load_proposals(self, status: str | None = "pending") -> None:
        try:
            self._proposals = pr.list_proposals(status, user=self.current_user)
        except Exception as exc:
            self._proposals = []
            QMessageBox.critical(self, "读取提案失败", str(exc))
        self.tbl_prop.setRowCount(len(self._proposals))
        for r, p in enumerate(self._proposals):
            name = p.get("name") or p.get("name_masked") or "（无解密权限）"
            if p.get("status") and p["status"] != "pending":
                name = f"{name}（{p['status']}）"
            cells = [str(p["id"]), name, p.get("doc_key") or "",
                     p.get("suggested_category") or "", p.get("created_at") or ""]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, p)
                self.tbl_prop.setItem(r, c, item)

    def _load_audit(self) -> None:
        rows = pr.audit_tail(15)
        self.txt_audit.setPlainText(
            "\n".join(
                f"{r.get('ts','')} {r.get('action','')} {r.get('code') or r.get('proposal_id') or ''} "
                f"{r.get('name_fp','')}"
                for r in rows
            ) or "（暂无审计记录）"
        )

    def _selected_entry(self) -> dict | None:
        row = self.tbl.currentRow()
        if row < 0 or row >= len(self._entries):
            return None
        return self._entries[row]

    # ---------- 操作 ----------
    def _add(self) -> None:
        dlg = ProjectEditDialog(self, current_user=self.current_user)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        data = dlg.payload() or {}
        try:
            res = pr.add_entry(
                data["name"], short_name=data.get("short_name"),
                category=data.get("category"), note=data.get("note"), user=self.current_user,
            )
        except Exception as exc:
            QMessageBox.critical(self, "登记失败", str(exc))
            return
        QMessageBox.information(
            self, "登记成功",
            f"项目已加密：{res['code']}\n（{'新增' if res.get('created') else '更新已有登记'}；"
            f"分类：{res.get('category') or '未分类'}）",
        )
        self.reload()

    def _edit(self) -> None:
        e = self._selected_entry()
        if not e:
            QMessageBox.information(self, "请选择", "请先在列表里选择一条登记项。")
            return
        dlg = ProjectEditDialog(self, current_user=self.current_user, entry=e)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        data = dlg.payload() or {}
        try:
            res = pr.update_entry(
                e["code"], short_name=data.get("short_name") or "",
                category=data.get("category") or "", note=data.get("note") or "",
                user=self.current_user,
            )
        except Exception as exc:
            QMessageBox.critical(self, "修改失败", str(exc))
            return
        if not res.get("ok"):
            QMessageBox.warning(self, "修改失败", res.get("msg", ""))
        self.reload()

    def _toggle_active(self) -> None:
        e = self._selected_entry()
        if not e:
            return
        try:
            pr.update_entry(e["code"], is_active=not e["is_active"], user=self.current_user)
        except Exception as exc:
            QMessageBox.critical(self, "操作失败", str(exc))
        self.reload()

    def _delete(self) -> None:
        e = self._selected_entry()
        if not e:
            return
        if QMessageBox.question(
            self, "确认删除",
            f"删除登记项 {e['code']}？\n（历史文档里的编号仍可解密，但新文档不再替换该名称）",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            pr.delete_entry(e["code"], user=self.current_user)
        except Exception as exc:
            QMessageBox.critical(self, "删除失败", str(exc))
        self.reload()

    def _selected_proposal(self) -> dict | None:
        row = self.tbl_prop.currentRow()
        if row < 0 or row >= len(self._proposals):
            return None
        return self._proposals[row]

    def _approve(self) -> None:
        p = self._selected_proposal()
        if not p:
            QMessageBox.information(self, "请选择", "请先选择一条 AI 提案。")
            return
        if p.get("status") != "pending":
            QMessageBox.information(self, "已处理", "该提案已处理过。")
            return
        dlg = ProjectEditDialog(
            self, current_user=self.current_user,
            entry={"name": p.get("name") or "", "category": p.get("suggested_category")},
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        data = dlg.payload() or {}
        try:
            res = pr.approve_proposal(
                p["id"], name=data.get("name"), short_name=data.get("short_name"),
                category=data.get("category"), note=data.get("note"), user=self.current_user,
            )
        except Exception as exc:
            QMessageBox.critical(self, "批准失败", str(exc))
            return
        QMessageBox.information(self, "已批准", res.get("msg", ""))
        self.reload()

    def _reject(self) -> None:
        p = self._selected_proposal()
        if not p:
            return
        reason, ok = QInputDialog.getText(self, "驳回提案", "驳回原因（可空）：")
        if not ok:
            return
        try:
            pr.reject_proposal(p["id"], reason=reason or None, user=self.current_user)
        except Exception as exc:
            QMessageBox.critical(self, "驳回失败", str(exc))
        self.reload()


# =========================================================
# 本公司（我方主体）：最高管理员登录时确认 → 全局脱敏 + 编号特殊提示
# =========================================================
class SelfCompanyDialog(QDialog):
    """确认/维护"本公司完整名称"（全局脱敏用；编号带 [本公司·CO####] 特殊提示）。

    入口：最高管理员登录时自动弹出确认；也可从 设置 → 本公司名称确认 再次打开。
    """

    def __init__(self, parent=None, current_user: dict | None = None, *, at_login: bool = False) -> None:
        super().__init__(parent)
        self.current_user = current_user or {}
        self.at_login = at_login
        self.setWindowTitle("本公司名称确认 - 最高管理员")
        fit_to_screen(self, 720, 420)
        self._entries: list[dict] = []
        self._build_ui()
        self.reload()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        head = QLabel(
            "请确认**本公司（我方主体）的完整名称**。确认后全局生效："
            "文档里出现该名称（无需\"甲方/乙方\"等上下文）都会替换为带标记的编号，"
            "例如 [本公司·CO0001]。名称以密文入库、明文不落盘；编号与公司类别共用，"
            "保证同一家公司在全库是同一个编号。"
        )
        head.setWordWrap(True)
        layout.addWidget(head)

        self.tbl = QTableWidget(0, 5)
        self.tbl.setHorizontalHeaderLabels(["编号（脱敏展示）", "公司完整名称", "主公司", "确认人", "确认时间"])
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hdr = self.tbl.horizontalHeader()
        for col in (0, 2, 3, 4):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.tbl, 1)

        row = QHBoxLayout()
        b_add = QPushButton("新增 / 更新本公司名称…")
        b_add.clicked.connect(self._add)
        b_primary = QPushButton("设为主公司")
        b_primary.clicked.connect(self._set_primary)
        b_del = QPushButton("移除")
        b_del.clicked.connect(self._remove)
        row.addWidget(b_add)
        row.addWidget(b_primary)
        row.addWidget(b_del)
        row.addStretch(1)
        layout.addLayout(row)

        self.lbl_state = QLabel("")
        self.lbl_state.setStyleSheet("color: #666;")
        layout.addWidget(self.lbl_state)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "确认（记住本次确认）" if self.at_login else "完成"
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def reload(self) -> None:
        try:
            self._entries = self_entity__desens.list_entries(self.current_user)
        except Exception as exc:
            self._entries = []
            QMessageBox.critical(self, "读取本公司登记失败", str(exc))
        self.tbl.setRowCount(len(self._entries))
        for r, e in enumerate(self._entries):
            cells = [
                e.get("display") or "",
                e.get("name") or e.get("name_masked") or "（无解密权限）",
                "是" if e.get("is_primary") else "",
                e.get("confirmed_by") or "",
                e.get("confirmed_at") or "",
            ]
            for c, text in enumerate(cells):
                self.tbl.setItem(r, c, QTableWidgetItem(str(text)))
        if not self._entries:
            self.lbl_state.setText("尚未登记本公司名称——请点「新增 / 更新本公司名称…」填写完整名称（必填）。")
        else:
            primary = [e for e in self._entries if e.get("is_primary")]
            self.lbl_state.setText(
                f"已登记 {len(self._entries)} 个本公司名称；"
                f"主公司：{primary[0].get('display') if primary else '（未指定）'}"
            )

    def _add(self) -> None:
        name, ok = QInputDialog.getText(
            self, "本公司完整名称",
            "请输入本公司（我方主体）的**完整名称**（与合同/发票抬头一致）：\n"
            "（必填；会立即加密入库并全局用于脱敏）",
        )
        if not ok or not name.strip():
            return
        try:
            res = self_entity__desens.add_entry(name.strip(), is_primary=True, user=self.current_user)
        except Exception as exc:
            QMessageBox.critical(self, "登记失败", str(exc))
            return
        QMessageBox.information(self, "已登记",
                                f"本公司名称已记入脱敏：{res['display'] if 'display' in res else res['code']}")
        self.reload()

    def _selected(self) -> dict | None:
        row = self.tbl.currentRow()
        if row < 0 or row >= len(self._entries):
            return None
        return self._entries[row]

    def _set_primary(self) -> None:
        e = self._selected()
        if not e:
            QMessageBox.information(self, "请选择", "请先选择一条本公司记录。")
            return
        try:
            self_entity__desens.set_primary(e["code"], user=self.current_user)
        except Exception as exc:
            QMessageBox.critical(self, "操作失败", str(exc))
        self.reload()

    def _remove(self) -> None:
        e = self._selected()
        if not e:
            return
        if QMessageBox.question(self, "确认移除",
                                f"移除本公司登记 {e['display']}？移除后新文档不再按本公司全局替换。") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            self_entity__desens.remove_entry(e["code"], user=self.current_user)
        except Exception as exc:
            QMessageBox.critical(self, "移除失败", str(exc))
        self.reload()

    def _on_accept(self) -> None:
        """登录确认：有登记 → 记一次"确认无改动"审计；无登记则提示但不阻断登录。"""
        if not self._entries:
            QMessageBox.warning(self, "尚未登记",
                                "未登记本公司名称，将无法全局脱敏本公司名称。可稍后在设置里补充。")
            self.accept()
            return
        primary = next((e for e in self._entries if e.get("is_primary")), self._entries[0])
        try:
            self_entity__desens.mark_confirmed(primary["code"], self.current_user)
        except Exception:
            pass
        self.accept()

    def confirm_required(self) -> bool:
        return not self._entries


def confirm_self_company(parent, current_user: dict | None, *, at_login: bool = False) -> bool:
    """弹窗确认本公司名称；返回是否已登记（登录流程用，失败不阻断登录）。"""
    dlg = SelfCompanyDialog(parent, current_user, at_login=at_login)
    dlg.exec()
    return not dlg.confirm_required()


# =========================================================
# 假设边确认（L3 图遍历：假设 → 证实/驳回）
# =========================================================
class HypothesisEdgeDialog(QDialog):
    """AI/规则给出的**假设边**人工确认：确认后转 validated（L3 才直接取用），驳回即 rejected。"""

    def __init__(self, parent=None, current_user: dict | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user or {}
        self.setWindowTitle("假设边确认 - 图遍历（L3）")
        fit_to_screen(self, 900, 520)
        self._edges: list[dict] = []
        self._build_ui()
        self.reload()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        head = QLabel(
            "下面是**证据不足但可能成立**的关系边（AI 判定 + 本地重算后降级）。"
            "确认 = 转为已验证边（L3 查询会直接走它）；驳回 = 标记为 rejected（不再取用）。"
            "依据里给出共享值；拿不准时可以先看" + "「来源文档/置信度/缺失证据」，再回原文核对。" 
        )
        head.setWordWrap(True)
        layout.addWidget(head)

        self.tbl = QTableWidget(0, 6)
        self.tbl.setHorizontalHeaderLabels(["关系", "置信度", "来源 → 目标", "已有依据", "缺失证据", "原因"])
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hdr = self.tbl.horizontalHeader()
        for col in (0, 1):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        for col in (2, 3, 4, 5):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.tbl, 1)

        row = QHBoxLayout()
        b_ok = QPushButton("确认（转已验证）")
        b_ok.clicked.connect(self._confirm)
        b_no = QPushButton("驳回")
        b_no.clicked.connect(self._reject)
        b_ref = QPushButton("刷新")
        b_ref.clicked.connect(self.reload)
        row.addWidget(b_ok)
        row.addWidget(b_no)
        row.addStretch(1)
        row.addWidget(b_ref)
        layout.addLayout(row)

        self.lbl = QLabel("")
        self.lbl.setStyleSheet("color: #666;")
        layout.addWidget(self.lbl)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        close.accepted.connect(self.accept)
        layout.addWidget(close)

    def reload(self) -> None:
        import graph_query__graph_walk as gq

        try:
            self._edges = gq.list_hypotheses()
        except Exception as exc:
            self._edges = []
            QMessageBox.critical(self, "读取假设边失败", str(exc))
        self.tbl.setRowCount(len(self._edges))
        for r, e in enumerate(self._edges):
            ev = sorted(((e.get("evidence") or {}).get("kinds") or {}).keys())
            cells = [e["relation"], f"{e['confidence']:.2f}", f"{e['src']} → {e['dst']}",
                     ", ".join(ev), ", ".join(e.get("missing") or []), e.get("reason") or ""]
            for c, text in enumerate(cells):
                self.tbl.setItem(r, c, QTableWidgetItem(str(text)))
        self.lbl.setText(f"待确认假设边 {len(self._edges)} 条")

    def _selected(self) -> dict | None:
        row = self.tbl.currentRow()
        if row < 0 or row >= len(self._edges):
            return None
        return self._edges[row]

    def _confirm(self) -> None:
        import graph_query__graph_walk as gq

        e = self._selected()
        if not e:
            QMessageBox.information(self, "请选择", "请先选择一条假设边。")
            return
        res = gq.confirm_edge(e["edge_id"], user=self.current_user, note="管理员确认（假设边窗口）")
        if not res.get("ok"):
            QMessageBox.warning(self, "确认失败", res.get("msg", ""))
        self.reload()

    def _reject(self) -> None:
        import graph_query__graph_walk as gq

        e = self._selected()
        if not e:
            return
        reason, ok = QInputDialog.getText(self, "驳回假设边", "驳回原因（可空）：")
        if not ok:
            return
        gq.reject_edge(e["edge_id"], user=self.current_user, note=reason or None)
        self.reload()
