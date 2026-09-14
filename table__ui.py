from __future__ import annotations
from ui_kit__ui import fit_to_screen
from ui_kit__ui import hint as kit_hint


from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sys
import traceback

from PySide6.QtCore import QEventLoop, QObject, QThread, QTimer, Qt, Signal, Slot, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)



# 引入数据库与 API 服务层
from database_serv__infra import (
    ALLOWED_COLUMN_TYPES,
    ALLOWED_TABLES,
    api_ai_analyze_user_habits,
    api_alter_table_field,
    api_deactivate_self,
    api_get_table_fields,
    api_ledger_view,
    api_list_contracts,
    api_list_users,
    api_rename_table_field,
    api_set_contract_status,
    api_set_key_column,
    api_set_ledger_status,
    api_toggle_user_active,
    api_transfer_admin,
    authenticate_user,
    register_user,
)
# 停止扫描（协作式取消）：UI 用 ScanCancelled 区分"用户停止"与"真失败"
from scan_control__scan import ScanCancelled


# 角色英文标识 -> 中文显示名（界面统一展示用）
ROLE_LABELS = {
    "admin": "最高管理员",
    "financial_role": "财务主管",
    "finance_staff": "财务专员",
}


def _role_label(role: str) -> str:
    """角色英文标识转中文；未知值原样返回（便于发现未登记的新角色）。"""
    return ROLE_LABELS.get(role, role or "未知")


# ===== 新增：登录/注册弹窗 =====
class LoginRegisterDialog(QDialog):
    """登录与注册整合窗口。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("财务系统 - 登录/注册")
        fit_to_screen(self, 320, 280)
        self.user_info = None

        layout = QVBoxLayout(self)

        self.title_label = QLabel("用户登录")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title_label)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("用户名")
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("密码")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.email_input = QLineEdit()
        self.email_input.setPlaceholderText("邮箱 (注册用)")
        self.phone_input = QLineEdit()
        self.phone_input.setPlaceholderText("手机号 (注册用)")

        layout.addWidget(self.username_input)
        layout.addWidget(self.password_input)
        layout.addWidget(self.email_input)
        layout.addWidget(self.phone_input)

        self.email_input.hide()
        self.phone_input.hide()

        self.btn_action = QPushButton("登录")
        self.btn_action.clicked.connect(self._handle_action)
        layout.addWidget(self.btn_action)

        self.btn_switch = QPushButton("没有账号？点击注册")
        self.btn_switch.clicked.connect(self._toggle_mode)
        layout.addWidget(self.btn_switch)

        self._is_register_mode = False

    def _toggle_mode(self) -> None:
        self._is_register_mode = not self._is_register_mode
        if self._is_register_mode:
            self.title_label.setText("财务人员注册")
            self.email_input.show()
            self.phone_input.show()
            self.btn_action.setText("注册并保存")
            self.btn_switch.setText("已有账号？返回登录")
        else:
            self.title_label.setText("用户登录")
            self.email_input.hide()
            self.phone_input.hide()
            self.btn_action.setText("登录")
            self.btn_switch.setText("没有账号？点击注册")

    def _handle_action(self) -> None:
        user = self.username_input.text().strip()
        pwd = self.password_input.text().strip()

        if self._is_register_mode:
            email = self.email_input.text().strip()
            phone = self.phone_input.text().strip()
            ok, msg = register_user(user, pwd, email, phone)
            if ok:
                QMessageBox.information(self, "成功", msg)
                self._toggle_mode()
            else:
                QMessageBox.warning(self, "校验失败", msg)
        else:
            ok, res = authenticate_user(user, pwd)
            if ok:
                self.user_info = res
                self.accept()
            else:
                QMessageBox.critical(self, "登录失败", str(res))


# ===== 字段调整对话框（读取/调整权限分离）=====
class FieldAdjustDialog(QDialog):
    """字段调整对话框。

    展示指定业务表的全部字段（名称+类型+语义标记），支持：
      - 新增字段（名称 + 白名单类型）           -> ADD
      - 重命名选中字段（内置列同样允许）        -> RENAME
      - 设为唯一键（删除唯一键列前先移交键）    -> SETKEY
      - 删除选中字段（系统列/唯一键列受保护）   -> DROP
    所有变更先收集到 self._ops，点「确定」后按顺序提交给 database_serv
    （权限：field:write，仅 financial_role / admin）。
    语义标记「系统/唯一键/人工状态」来自列目录 column_catalog（database_serv
    自动对账维护），字段列表本身永远以数据库实际存在的物理列（information_schema）
    为准——删除列后目录行由服务端下次读取时自动清理，列表不会残留。

    业务扩展说明：以后新增其它业务表（发票/物流等）时，只需把表名登记进
    database_serv.ALLOWED_TABLES，本对话框即可复用，互不影响。
    ⚠️ 说明：内置列（合同编号/甲方等）现可删除/改名，其语义标志（唯一键/
    人工状态等）由列目录保留；但台账写入/AI 等旧版代码仍按写死的中文列名运行，
    对它们引用的列做改名/删除会导致相关功能报错（见 database_serv 中
    api_rename_table_field / api_alter_table_field 的文档化局限）。
    """

    def __init__(self, parent, current_user: dict, table_name: str) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self.table_name = table_name
        self._fields: list[dict] = []   # 当前字段工作副本（随操作实时变化）
        self._ops: list[tuple] = []     # 待提交操作列表：(ACTION, *args)
        self._display = ALLOWED_TABLES.get(table_name, table_name)
        self.setWindowTitle(f"调整字段 - {self._display}")
        fit_to_screen(self, 620, 460)
        self._build_ui()
        self._load_fields()

    def _build_ui(self) -> None:
        """构建对话框界面：字段列表 + 操作按钮 + 变更预览 + 确定/取消。"""
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("当前字段列表（可选中后重命名/删除）："))

        self.field_list = QListWidget()
        self.field_list.itemDoubleClicked.connect(self._rename_selected)
        layout.addWidget(self.field_list, 1)

        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("新增字段")
        self.btn_add.clicked.connect(self._add_field)
        self.btn_rename = QPushButton("重命名选中")
        self.btn_rename.clicked.connect(self._rename_selected)
        self.btn_set_key = QPushButton("设为唯一键")
        self.btn_set_key.clicked.connect(self._set_key_selected)
        self.btn_delete = QPushButton("删除选中")
        self.btn_delete.clicked.connect(self._delete_selected)
        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_rename)
        btn_row.addWidget(self.btn_set_key)
        btn_row.addWidget(self.btn_delete)
        layout.addLayout(btn_row)

        layout.addWidget(QLabel("变更预览："))
        self.op_view = QPlainTextEdit()
        self.op_view.setReadOnly(True)
        self.op_view.setMaximumHeight(100)
        layout.addWidget(self.op_view)

        btn_confirm_row = QHBoxLayout()
        self.btn_ok = QPushButton("确定")
        self.btn_ok.clicked.connect(self._apply_changes)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        btn_confirm_row.addStretch(1)
        btn_confirm_row.addWidget(self.btn_ok)
        btn_confirm_row.addWidget(self.btn_cancel)
        layout.addLayout(btn_confirm_row)

    def _load_fields(self) -> None:
        """读取当前业务表的全部字段（仅元数据，不含数据内容）。"""
        ok, res = api_get_table_fields(self.current_user, self.table_name)
        if not ok:
            QMessageBox.critical(self, "读取字段失败", str(res))
            self._fields = []
        else:
            self._fields = list(res)
        self._refresh_list()

    def _refresh_list(self) -> None:
        """按工作副本重建字段列表，并刷新变更预览。

        列表项追加列目录语义标记：系统 / 唯一键 / 人工状态（来自
        api_get_table_fields 返回的 is_* 标注，帮助用户识别哪些列受保护）。
        """
        self.field_list.clear()
        for f in self._fields:
            tags = []
            if f.get("is_system_col"):
                tags.append("系统")
            if f.get("is_key_col"):
                tags.append("唯一键")
            if f.get("is_status_col"):
                tags.append("人工状态")
            suffix = f"  [{' / '.join(tags)}]" if tags else ""
            self.field_list.addItem(f"{f['column_name']}  ({f['data_type']}){suffix}")
        self._refresh_ops()

    def _refresh_ops(self) -> None:
        """把待提交操作渲染成可读的变更预览文本。"""
        lines = [self._fmt_op(op) for op in self._ops]
        self.op_view.setPlainText("\n".join(lines) if lines else "（暂无变更）")

    @staticmethod
    def _fmt_op(op: tuple) -> str:
        """把一条操作元组格式化为预览文本。"""
        if op[0] == "ADD":
            return f"＋ 新增字段：{op[1]}  ({op[2]})"
        if op[0] == "RENAME":
            return f"⇄ 重命名：{op[1]}  →  {op[2]}"
        if op[0] == "SETKEY":
            return f"⇧ 设为唯一键：{op[1]}（原唯一键列自动取消该标志）"
        if op[0] == "DROP":
            return f"－ 删除字段：{op[1]}"
        return str(op)

    def _selected_field_name(self) -> str | None:
        """返回列表当前选中项对应的字段名；未选中返回 None。"""
        item = self.field_list.currentItem()
        if item is None:
            return None
        return item.text().split("  (")[0]

    def _add_field(self) -> None:
        """新增字段：输入名称并从类型白名单中选择类型，加入待提交操作。"""
        name, ok1 = QInputDialog.getText(self, "新增字段", "字段名（中英文、数字、下划线）：")
        if not (ok1 and name.strip()):
            return
        name = name.strip()
        if any(f["column_name"] == name for f in self._fields):
            QMessageBox.warning(self, "重复", f"字段「{name}」已存在！")
            return
        col_type, ok2 = QInputDialog.getItem(
            self, "新增字段", "字段类型：", sorted(ALLOWED_COLUMN_TYPES), 0, False
        )
        if not ok2:
            return
        self._fields.append({"column_name": name, "data_type": col_type})
        self._ops.append(("ADD", name, col_type))
        self._refresh_list()

    def _rename_selected(self) -> None:
        """重命名选中的字段（记录 RENAME 操作）。"""
        old = self._selected_field_name()
        if old is None:
            QMessageBox.information(self, "提示", "请先选择一个字段。")
            return
        new, ok = QInputDialog.getText(self, "重命名字段", f"「{old}」的新名称：", text=old)
        if not (ok and new.strip()):
            return
        new = new.strip()
        if new == old:
            return
        if any(f["column_name"] == new for f in self._fields):
            QMessageBox.warning(self, "重复", f"字段「{new}」已存在！")
            return
        for f in self._fields:
            if f["column_name"] == old:
                f["column_name"] = new
                break
        self._ops.append(("RENAME", old, new))
        self._refresh_list()

    def _set_key_selected(self) -> None:
        """把选中的字段设为唯一键（记录 SETKEY 操作，点「确定」时统一提交）。

        用途：删除内置唯一键列（合同编号）前，先对另一列执行本操作移交唯一键。
        语义由 database_serv.api_set_key_column 校验并落库（NOT NULL + 唯一索引 +
        目录标志移交），对话框只负责记录待提交操作。
        """
        name = self._selected_field_name()
        if name is None:
            QMessageBox.information(self, "提示", "请先选择一个字段。")
            return
        for op in self._ops:
            if op[0] == "SETKEY" and op[1] == name:
                return  # 已排入队列，避免重复
        self._ops.append(("SETKEY", name))
        self._refresh_list()

    def _delete_selected(self) -> None:
        """删除选中的字段（记录 DROP 操作）。"""
        old = self._selected_field_name()
        if old is None:
            QMessageBox.information(self, "提示", "请先选择一个字段。")
            return
        reply = QMessageBox.question(
            self, "确认删除", f"确定删除字段「{old}」？该操作不可撤销。"
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._fields = [f for f in self._fields if f["column_name"] != old]
        self._ops.append(("DROP", old))
        self._refresh_list()

    def _apply_changes(self) -> None:
        """点「确定」：按顺序提交所有待执行操作，汇总结果显示后关闭对话框。"""
        if not self._ops:
            self.accept()
            return
        results: list[str] = []
        for op in self._ops:
            if op[0] == "ADD":
                ok, msg = api_alter_table_field(self.current_user, "ADD", self.table_name, op[1], op[2])
            elif op[0] == "RENAME":
                ok, msg = api_rename_table_field(self.current_user, self.table_name, op[1], op[2])
            elif op[0] == "SETKEY":
                ok, msg = api_set_key_column(self.current_user, self.table_name, op[1])
            elif op[0] == "DROP":
                ok, msg = api_alter_table_field(self.current_user, "DROP", self.table_name, op[1])
            else:
                continue
            results.append(("✅ " if ok else "❌ ") + msg)
        QMessageBox.information(self, "调整结果", "\n".join(results))
        self.accept()


# ===== 台账状态管理对话框（随"业务表"切换：合同台账 / 发票台账 各自的字段）=====
class LedgerStatusDialog(QDialog):
    """台账状态管理：按**当前选中的业务表**列出它的字段，状态列可勾选后保存。

    · 合同台账：合同编号 / 项目 / 合同金额 + 「已收款」「已开票」两个勾选框；
    · 发票台账：发票号码 / 开票日期 / 销售方 / 购买方 / 金额 / 税额 / 价税合计 / 合同编号 / 项目
      —— 发票台账**没有**"已收款/已开票"两列（这两个状态记在合同台账上），
      所以这里只读展示、不显示勾选框，并提示去合同台账改状态 / 用「完整字段」窗口改其它字段。
    列清单由 `database_serv.LEDGER_VIEW` 配置 + 实际物理列求交集得到，用户删过列也不会报错。

    权限：ledger:status（financial_role / admin，在 database_serv 侧校验）。
    """

    def __init__(self, parent, current_user: dict, table_name: str = "contract_projects") -> None:
        super().__init__(parent)
        self.current_user = current_user
        self.table_name = table_name
        self._rows: list[dict] = []
        self._display = ALLOWED_TABLES.get(table_name, table_name)
        self._columns: list[str] = []
        self._status_cols: list[dict] = []
        self._key_col = ""
        self._checks: dict[tuple[int, str], QCheckBox] = {}
        self.setWindowTitle(f"台账状态管理 - {self._display}")
        fit_to_screen(self, 760, 500)
        self._build_ui()
        self._load()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        self.lbl_head = QLabel("")
        self.lbl_head.setWordWrap(True)
        layout.addWidget(self.lbl_head)

        self.table = QTableWidget(0, 0)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 1)

        btn_row = QHBoxLayout()
        self.btn_full = QPushButton("完整字段 / 导出…")
        self.btn_full.setToolTip(
            "打开「台账全字段管理」：能看到这张台账的全部字段（含 id/创建时间）、\n"
            "任意列都能人工修改，并可导出成 xlsx / csv 存到指定位置。仅最高管理员。"
        )
        self.btn_full.clicked.connect(self._open_full)
        self.btn_save = QPushButton("保存状态")
        self.btn_save.clicked.connect(self._save)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.accept)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_full)
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_close)
        layout.addLayout(btn_row)

    def _open_full(self) -> None:
        """跳到「台账全字段管理」（同一张台账）。"""
        from ledger_admin__ui import LedgerAdminDialog

        LedgerAdminDialog(self, self.current_user, self.table_name).exec()

    def _load(self) -> None:
        ok, data = api_ledger_view(self.current_user, self.table_name, limit=500)
        if not ok:
            QMessageBox.critical(self, "读取台账失败", str(data))
            return
        self._columns = list(data["columns"])
        self._status_cols = list(data.get("status_columns") or [])
        # 状态列由勾选框呈现 → 不再重复显示一列纯文本（否则"是否已收款"会同时以文本+勾选框出现）
        status_names = {c["name"] for c in self._status_cols}
        self._columns = [c for c in self._columns if c not in status_names]
        self._key_col = str(data.get("key_column") or "")
        self._rows = list(data["rows"])
        self._display = str(data.get("display") or self.table_name)

        has_status = bool(self._status_cols)
        self.lbl_head.setText(
            f"台账：**{self._display}**（共 {data['total']} 行，显示最近 {len(self._rows)} 行）｜"
            f"定位列：{self._key_col}｜列：{'、'.join(self._columns + [c['label'] for c in self._status_cols])}\n"
            + ("勾选 = 已；取消 = 未（默认未）。修改后点「保存状态」。"
               if has_status else
               "该台账**没有「已收款 / 已开票」状态列**（这两个状态记在合同台账上），"
               "此处只读展示；要改发票号/日期/金额等字段，请点右下「完整字段 / 导出…」。")
        )
        self.btn_save.setEnabled(has_status)

        self.table.clear()
        self.table.setColumnCount(len(self._columns) + len(self._status_cols))
        self.table.setHorizontalHeaderLabels(
            self._columns + [c["label"] for c in self._status_cols])
        self.table.setRowCount(len(self._rows))
        self._checks.clear()
        for r, row in enumerate(self._rows):
            for c, col in enumerate(self._columns):
                self.table.setItem(r, c, QTableWidgetItem(str(row.get(col) or "")))
            for k, sc in enumerate(self._status_cols):
                box = QCheckBox()
                box.setChecked(bool(row.get(sc["name"], False)))
                box.setText(sc["label"])
                if not has_status:
                    box.setEnabled(False)
                self.table.setCellWidget(r, len(self._columns) + k, box)
                self._checks[(r, sc["name"])] = box

    def _save(self) -> None:
        """对勾选状态与数据库不一致的行逐个提交（按当前表的定位列）。"""
        results: list[str] = []
        for i, row in enumerate(self._rows):
            key_value = row.get(self._key_col)
            if key_value in (None, ""):
                continue
            changes: dict = {}
            for sc in self._status_cols:
                col = sc["name"]
                box = self._checks.get((i, col))
                if box is None:
                    continue
                new_val = box.isChecked()
                if new_val != bool(row.get(col, False)):
                    changes[col] = new_val
            if not changes:
                continue
            ok, msg = api_set_ledger_status(
                self.current_user, self.table_name, str(key_value),
                paid=changes.get("是否已收款"), invoiced=changes.get("是否已开票"))
            results.append(("✅ " if ok else "❌ ") + f"{key_value}: {msg}")
        if not results:
            QMessageBox.information(self, "提示", "没有需要保存的变更。")
            return
        QMessageBox.information(self, "保存结果", "\n".join(results))
        self._load()


# ===== 用户管理窗口（仅最高管理员 admin 可打开，入口：设置 → 用户管理）=====
class AdminUsersDialog(QDialog):
    """最高管理员：查看全部用户状态 + 停用/启用 + 转让最高管理员。

    · 权限：调用方（MainWindow）已保证本窗口只对 admin 开放；服务端
      database_serv.api_* 内部仍会再次校验 role_type（纵深防御）；
    · 展示字段：用户名 / 角色 / 账号状态（正常·停用）/ 最近登录 / 注册时间 /
      是否本机当前登录；
    · 在线状态说明：桌面单机阶段尚无服务端会话（FastAPI 阶段才落地
      user_sessions），此处"状态"= 账号状态 + 最近登录时间；
    · 转让成功后返回 QDialog.Accepted，主窗口据此退出登录（本人已降级）。
    """

    def __init__(self, parent, current_user: dict) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self.setWindowTitle("用户管理 - 最高管理员")
        fit_to_screen(self, 780, 430)
        self._rows: list[dict] = []
        self._build_ui()
        self._load()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(kit_hint("全部用户状态（账号状态 + 最近登录；"
                                  "真正的在线会话随 FastAPI 会话表落地）"))

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["用户名", "角色", "账号状态", "最近登录", "注册时间", "当前登录"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)

        btn_row = QHBoxLayout()
        self.btn_toggle = QPushButton("停用 / 启用选中")
        self.btn_toggle.clicked.connect(self._toggle_selected)
        self.btn_transfer = QPushButton("转让最高管理员…")
        self.btn_transfer.clicked.connect(self._transfer_admin)
        btn_row.addWidget(self.btn_toggle)
        btn_row.addWidget(self.btn_transfer)
        btn_row.addStretch(1)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self._load)
        btn_row.addWidget(self.btn_refresh)
        layout.addLayout(btn_row)

        hint = QLabel("提示：停用后该账号无法登录；转让成功后本机将退出登录（本人降为财务主管）。")
        hint.setStyleSheet("color: #777;")
        layout.addWidget(hint)

    def _load(self) -> None:
        ok, res = api_list_users(self.current_user)
        if not ok:
            QMessageBox.critical(self, "读取用户失败", str(res))
            self._rows = []
        else:
            self._rows = list(res)
        self.table.setRowCount(len(self._rows))
        for i, row in enumerate(self._rows):
            self.table.setItem(i, 0, QTableWidgetItem(str(row.get("username", ""))))
            self.table.setItem(i, 1, QTableWidgetItem(_role_label(row.get("role_type", ""))))
            self.table.setItem(i, 2, QTableWidgetItem("正常" if row.get("is_active", True) else "已停用"))
            self.table.setItem(i, 3, QTableWidgetItem(str(row.get("last_login_at") or "从未登录")))
            self.table.setItem(i, 4, QTableWidgetItem(str(row.get("created_at") or "")))
            is_me = row.get("username") == self.current_user.get("username")
            self.table.setItem(i, 5, QTableWidgetItem("● 本机" if is_me else ""))

    def _selected_username(self) -> str | None:
        idx = self.table.currentRow()
        if idx < 0 or idx >= len(self._rows):
            return None
        return str(self._rows[idx].get("username", ""))

    def _toggle_selected(self) -> None:
        name = self._selected_username()
        if not name:
            QMessageBox.information(self, "提示", "请先选择一名用户。")
            return
        ok, msg = api_toggle_user_active(self.current_user, name)
        if ok:
            QMessageBox.information(self, "操作结果", msg)
        else:
            QMessageBox.warning(self, "操作失败", msg)
        self._load()

    def _transfer_admin(self) -> None:
        name = self._selected_username()
        if not name:
            QMessageBox.information(self, "提示", "请先选择接收最高管理员的用户。")
            return
        reply = QMessageBox.question(
            self,
            "确认转让",
            f"确定把最高管理员转让给「{name}」？\n"
            "转让后您将降为财务主管并退出登录，需使用新账号重新登录。",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        pwd, ok_in = QInputDialog.getText(
            self, "密码确认", "请输入您的登录密码以确认转让：", QLineEdit.EchoMode.Password
        )
        if not (ok_in and pwd):
            return
        ok, msg = api_transfer_admin(self.current_user, name, pwd)
        if ok:
            QMessageBox.information(self, "转让成功", msg)
            self.accept()  # 通知主窗口：退出登录
        else:
            QMessageBox.warning(self, "转让失败", msg)


@dataclass
class TaskRecord:
    file_path: Path
    category: str = ""
    hub_json_path: Path | None = None
    cache_json_paths: list[Path] = field(default_factory=list)
    status: str = "等待中"
    source_root: Path | None = None      # 源文件夹根（文件夹拖入时；决定 hub 落盘子目录）
    doc_key: str = ""                    # 入库后的文档主键（删除时按它清痕迹）
    needs_ocr: bool = False              # 是否需要 OCR 转文字（排到队列最后）
    ocr_reason: str = ""                 # 判定原因（展示/日志用）


class ScanWorker(QObject):
    log_message = Signal(str)
    task_started = Signal(str)
    task_finished = Signal(str, object)
    task_failed = Signal(str, str)
    finished = Signal()

    def __init__(self, file_path: Path, current_user: dict,
                 source_root: Path | None = None) -> None:
        super().__init__()
        self._file_path = file_path
        self._current_user = current_user
        self._source_root = source_root

    @Slot()
    def run(self) -> None:
        try:
            from hub_pipeline__desens import process_file_to_hub

            self.task_started.emit(str(self._file_path))
            self.log_message.emit(f"开始处理：{self._file_path}")

            result = process_file_to_hub(
                self._file_path, current_user=self._current_user,
                source_root=self._source_root,
            )

            self.task_finished.emit(str(self._file_path), result)
            self.log_message.emit(
                f"完成处理：{self._file_path} | 分类：{result.get('category') or '-'}"
                + (f" | doc_key：{result.get('doc_key')}" if result.get("doc_key") else "")
            )
        except ScanCancelled as exc:
            # 用户点了"停止扫描"：按"已停止"上报（不是失败），不再进入后续步骤
            self.task_failed.emit(str(self._file_path), f"已停止：{exc.where or '检查点'}")
            self.log_message.emit(f"已停止：{self._file_path} | {exc}")
        except Exception as exc:
            self.task_failed.emit(str(self._file_path), f"{type(exc).__name__}: {exc}")
            self.log_message.emit(f"处理失败：{self._file_path} | {type(exc).__name__}: {exc}")
        finally:
            self.finished.emit()


class MainWindow(QMainWindow):
    # 请求退出登录（回到登录对话框）。注销账户/转让最高管理员/主动退出时发出，
    # 由 main() 的"登录-主窗口"循环接管回到登录框。
    logout_requested = Signal()

    def __init__(self, current_user: dict) -> None:
        super().__init__()
        self.current_user = current_user
        # 标题里带上当前「仓库」（工作区）：一屏就能看出自己在哪套隔离环境里干活
        _ws_name = ""
        try:
            import workspace__infra as _ws

            _active = _ws.active()
            _ws_name = f" - 仓库: {(_active or {}).get('name') or (_active or {}).get('slug') or '未登记'}"
        except Exception:
            _ws_name = ""
        self.setWindowTitle(f"Table 扫描工作台 - [当前用户: {current_user['username']}]{_ws_name}")
        # 初始尺寸：按屏幕可视区域自适应（小屏/高缩放不再超出屏幕），
        # 之后记住用户自己拖出来的窗口大小与各分栏位置（见 closeEvent）。
        import ui_kit__ui as kit

        self.setMinimumSize(760, 480)
        self.setAcceptDrops(True)

        self._pending_paths: list[Path] = []
        self._pending_ocr: list[Path] = []      # 需要 OCR 的：排到最后，处理前询问
        self._ocr_consent: str = ""             # "" 未问 | "all" 同意全部 | "skip" 全部跳过
        self._records: dict[Path, TaskRecord] = {}
        self._current_thread: QThread | None = None
        self._current_worker: ScanWorker | None = None
        self._stop_requested = False            # 用户点了"停止扫描"（挡住后续排队任务）
        self._stopped_paths: list[Path] = []    # 被停止的（可"继续扫描"重新排队）
        # 文件名乱码修复模式的状态
        self._fix_busy = False
        self._fix_thread: QThread | None = None
        self._fix_report_dir: str = ""

        self._build_ui()
        self._build_top_right_menu()
        self._build_ai_chat_dock()
        # 启动时把"待入账 N 条"刷到按钮上（扫描不再自动入账，需要人来看一眼）
        self._refresh_inbox_hint()
        # 启动时把"待建 N 份"刷到建图按钮上（存量补齐入口；不自动跑，避免意外花 token）
        self._refresh_graph_hint()
        # 最高管理员登录后：确认本公司完整名称（全局脱敏用；不阻断登录）
        self._schedule_self_company_confirm()
        # 窗口大小/分栏：优先恢复用户上次的调整，其次按屏幕自适应（放得下就用设计尺寸）
        kit.restore_geometry(self, "main", default=(1380, 850), min_w=900, min_h=560)

    def closeEvent(self, event) -> None:  # noqa: N802 （Qt 命名）
        """退出时记住窗口大小、各分栏位置与 AI 停靠窗状态。"""
        try:
            import ui_kit__ui as kit

            kit.save_geometry(self, "main")
        except Exception:
            pass
        super().closeEvent(event)

    def _schedule_self_company_confirm(self) -> None:
        """登录后延迟弹一次"本公司名称确认"（仅最高管理员；关窗/无登记都不阻断登录）。"""
        if (self.current_user or {}).get("role_type") != "admin":
            return
        from PySide6.QtCore import QTimer

        QTimer.singleShot(800, self._confirm_self_company)

    def _confirm_self_company(self) -> None:
        try:
            import self_entity__desens as self_entity

            if not self_entity.needs_confirmation():
                # 已登记：仍做一次"确认无改动"审计（记谁在何时确认过），不弹窗打断
                for e in self_entity.list_entries(self.current_user):
                    if e.get("is_primary"):
                        self_entity.mark_confirmed(e["code"], self.current_user)
                        break
                return
            from project_admin__ui import confirm_self_company

            confirm_self_company(self, self.current_user, at_login=True)
        except Exception as exc:
            print(f"[本公司确认] 跳过：{type(exc).__name__}: {exc}")

    def _open_self_company(self) -> None:
        """设置 → 本公司名称确认（仅 admin）：复核/新增本公司完整名称。"""
        from project_admin__ui import SelfCompanyDialog

        SelfCompanyDialog(self, self.current_user).exec()

    def _open_hypothesis_edges(self) -> None:
        """设置 → 假设边确认：L3 图遍历的假设边人工证实/驳回。"""
        from project_admin__ui import HypothesisEdgeDialog

        HypothesisEdgeDialog(self, self.current_user).exec()

    def _build_ui(self) -> None:
        import ui_kit__ui as kit

        def _titled(title: str, widget) -> QWidget:
            """给一块内容加小标题（作为分栏的一页，方便单独拖动高度）。"""
            page = QWidget()
            box = QVBoxLayout(page)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(2)
            box.addWidget(QLabel(title))
            box.addWidget(widget, 1)
            return page

        central = QWidget(self)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(12, 10, 12, 10)
        root_layout.setSpacing(8)

        # 模式切换：扫描模式 / 文件名乱码修复模式（与扫描互斥，作用于拖动区）
        mode_row = QHBoxLayout()
        self._fix_mode_check = QCheckBox("文件名乱码修复模式")
        # 长说明移到 tooltip：窄窗口下复选框文字不会被硬裁（仍能悬停看全文）
        self._fix_mode_check.setToolTip(
            "文件名乱码修复模式：拖入文件夹（含所有子目录），先选报告保存位置 → 只看名称不读内容 → "
            "预览确认 → 原地改名；后缀、位置、内容都不变。"
        )
        self._fix_mode_check.toggled.connect(self._on_fix_mode_toggled)
        mode_row.addWidget(self._fix_mode_check)
        mode_row.addStretch(1)
        root_layout.addLayout(mode_row)

        self.drop_hint = kit.hint("把 PDF、图片或文件夹直接拖到窗口里，系统会自动进入输入队列。",
                                  min_width=200)
        self.drop_hint.setFrameShape(QFrame.Shape.StyledPanel)
        self.drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_hint.setMinimumHeight(44)
        root_layout.addWidget(self.drop_hint)

        # 左侧面板
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("任务列表"))

        self.task_list = QListWidget()
        self.task_list.currentItemChanged.connect(self._show_selected_preview)
        # 右键菜单：删除文件（连同它的节点/边/向量记录一起清）
        self.task_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.task_list.customContextMenuRequested.connect(self._task_list_menu)
        left_layout.addWidget(self.task_list, 1)

        self.add_files_button = QPushButton("添加文件")
        self.add_files_button.setToolTip("手动添加文件进扫描队列（也可以直接把文件/文件夹拖进窗口）")
        self.add_files_button.clicked.connect(self._pick_files)
        self.open_output_button = QPushButton("输出目录")
        self.open_output_button.setToolTip("打开该文件的输出目录（逐页 OCR 缓存 / 中间产物）")
        self.open_output_button.clicked.connect(self._open_output_directory)
        self.delete_button = QPushButton("删除选中")
        self.delete_button.setToolTip("删除选中文件，连同它的节点/边/向量记录一起清")
        self.delete_button.clicked.connect(self._delete_selected_document)

        # 停止扫描 / 继续扫描：协作式取消（页边界停下；当前页会跑完，源文件不动）
        self.stop_button = QPushButton("■ 停止")
        self.stop_button.setToolTip(
            "立即停止扫描：当前正在识别的**这一页会跑完**，之后不再开始下一页，\n"
            "也不会进入 AI/概括/事实等后续步骤；已落盘的页缓存保留，源文件不动。"
        )
        self.stop_button.clicked.connect(self._stop_scan)
        self.stop_button.setEnabled(False)
        self.resume_button = QPushButton("继续扫描")
        self.resume_button.setToolTip("继续扫描被停止的文件（重新排队）")
        self.resume_button.clicked.connect(self._resume_scan)
        self.resume_button.setEnabled(False)
        self.inbox_button = QPushButton("待入账…")
        self.inbox_button.setToolTip("待入账审核：查看合同/发票候选，由你决定是否入账（扫描不再自动入账）")
        self.inbox_button.clicked.connect(self._open_ledger_inbox)

        # 建图/补齐：动态建图是"入一份建一份"，这个按钮专门用于**补齐存量**
        # （之前扫过但没建关系的文件，按旧→新顺序补），也可用于重建。
        # 文案保持短：动态计数走下面的状态行（原来塞进按钮，窄窗口必被裁）。
        self.graph_button = QPushButton("建图/补齐…")
        self.graph_button.setToolTip(
            "动态建图：每份文档扫描完成即后台建它的节点与关系。\n"
            "此按钮用于补齐存量（之前扫过、还没建图的文件，按旧→新补）与查看图状态。"
        )
        self.graph_button.clicked.connect(self._open_graph_tools)

        # 按钮排成网格（3 列）：窄窗口里自动换行，不会把每个按钮挤到只剩半个字
        left_layout.addLayout(kit.grid_row(
            [self.add_files_button, self.open_output_button, self.delete_button,
             self.stop_button, self.resume_button, self.inbox_button, self.graph_button],
            columns=3))
        # 动态计数（待入账 N 条 / 待建 N 份 …）放在**会自动换行的状态行**里：
        # 原来塞进按钮文字，窄窗口下必然被裁（实测"建图/补齐（待建 7｜节点 27｜边 44）…"需要 284px）。
        self.status_hint = kit.hint("", min_width=120)
        self.status_hint.setStyleSheet("color:#555;")
        left_layout.addWidget(self.status_hint)
        self._inbox_pending = 0
        self._graph_counts: dict = {}

        # 右侧面板
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("选择左侧任务后，这里显示输出 JSON。")
        self.preview.setMinimumHeight(120)

        # 数据库字段管理区：读取与调整权限分离（financial_role 拥有最高权限）
        db_control_frame = QFrame()
        db_control_frame.setFrameShape(QFrame.Shape.StyledPanel)
        db_control_layout = QVBoxLayout(db_control_frame)
        db_control_layout.setSpacing(6)

        # 业务表选择：目前只有合同台账；以后新增业务时在
        # database_serv.ALLOWED_TABLES 登记即可自动出现在这里
        table_row = QHBoxLayout()
        table_row.addWidget(QLabel("业务表:"))
        self.table_combo = QComboBox()
        for table_name, display_name in ALLOWED_TABLES.items():
            self.table_combo.addItem(display_name, table_name)
        table_row.addWidget(self.table_combo, 1)
        db_control_layout.addLayout(table_row)

        self.btn_read_fields = QPushButton("读取字段")
        self.btn_read_fields.setToolTip("读取现有字段（只显示字段名/类型，不含数据内容）")
        self.btn_read_fields.clicked.connect(self._read_fields)
        self.btn_adjust_fields = QPushButton("调整字段")
        self.btn_adjust_fields.clicked.connect(self._adjust_fields)
        self.btn_ledger_status = QPushButton("台账状态")
        self.btn_ledger_status.clicked.connect(self._ledger_status)
        # 按钮文案压短（完整含义进 tooltip）：4 个按钮自己占一行并排成 2×2，
        # 原来和"业务表"下拉挤在同一行，窄窗口里"AI 分析人员习惯并建议字段"被裁成半个字。
        self.btn_ai_habit = QPushButton("AI 建议")
        self.btn_ai_habit.setToolTip("让 AI 读人员填写习惯，建议新增/调整字段（仅建议，改动仍需你确认）")
        self.btn_ai_habit.clicked.connect(self._ai_habit_suggest)
        db_control_layout.addLayout(kit.grid_row(
            [self.btn_read_fields, self.btn_adjust_fields, self.btn_ledger_status,
             self.btn_ai_habit], columns=2))

        # 字段列表显示区：只读，仅显示字段名（+类型），不显示任何数据内容
        self.fields_view = QPlainTextEdit()
        self.fields_view.setReadOnly(True)
        self.fields_view.setMinimumHeight(60)
        self.fields_view.setPlaceholderText("点击「读取现有字段」查看当前台账字段。")

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setMinimumHeight(100)

        # 右侧三块（JSON 预览 / 库字段 / 运行日志）放进**纵向分栏**：
        # 每条分界线都能拖，用户可以把任意一块拉大或缩小。
        self._right_split = kit.vbox_splitter(
            [_titled("JSON 预览", self.preview),
             _titled("现有字段（仅字段名）", self.fields_view),
             db_control_frame,
             _titled("运行日志", self.log_view)],
            sizes=[300, 110, 70, 180], name="right_col")
        right_layout.addWidget(self._right_split, 1)

        # 左右两栏也放进可拖动分栏（第一版就有，这里统一用 kit 的封装：加粗分隔条、
        # 不允许塌成 0、并登记 objectName 以便下次启动恢复用户拖出来的位置）
        self._main_split = kit.hbox_splitter([left_panel, right_panel],
                                             sizes=[420, 900], stretch=[1, 2],
                                             name="main_col")
        root_layout.addWidget(self._main_split, 1)

        self.setCentralWidget(central)

    # ===== 数据库与 AI 操作交互实现 =====
    def _current_business_table(self) -> str:
        """返回当前选中的业务表名（默认合同台账）。

        后续新增业务表时，下拉框会自动带出，无需改这里的逻辑。
        """
        return self.table_combo.currentData() or "contract_projects"

    def _read_fields(self) -> None:
        """读取现有字段：在下方字段列表区展示（仅字段名+类型，不含任何数据内容）。

        权限：field:read —— 所有已登录用户（含 finance_staff）。
        """
        table_name = self._current_business_table()
        ok, res = api_get_table_fields(self.current_user, table_name)
        if not ok:
            QMessageBox.critical(self, "读取字段失败", str(res))
            self.fields_view.setPlainText("")
            return
        lines = [f"{i}. {f['column_name']}  ({f['data_type']})" for i, f in enumerate(res, 1)]
        self.fields_view.setPlainText("\n".join(lines) if lines else "（该表暂无字段）")
        self._append_log(f"已读取 {table_name} 的 {len(res)} 个字段")

    def _adjust_fields(self) -> None:
        """打开字段调整对话框：查看全部字段，可新增/重命名/删除后统一提交。

        权限：field:write —— 仅 financial_role / admin（在 database_serv 侧校验）。
        调整完成后自动刷新下方字段列表。
        """
        dialog = FieldAdjustDialog(self, self.current_user, self._current_business_table())
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._read_fields()

    def _ledger_status(self) -> None:
        """打开台账状态管理：手动切换 已收款 / 已开票（默认未）。

        权限：ledger:status —— financial_role / admin。
        """
        dialog = LedgerStatusDialog(self, self.current_user, self._current_business_table())
        dialog.exec()

    def _ai_habit_suggest(self) -> None:
        suggestion = api_ai_analyze_user_habits()
        msg = (
            f"【AI 建议理由】: {suggestion['reason']}\n"
            f"拟在表 {ALLOWED_TABLES.get(self._current_business_table(), self._current_business_table())} 新增字段: "
            f"{suggestion['col_name']} ({suggestion['col_type']})\n\n"
            "是否同意修改数据库结构？"
        )
        reply = QMessageBox.question(self, "AI 申请修改数据库结构", msg)
        if reply == QMessageBox.StandardButton.Yes:
            success, res_msg = api_alter_table_field(
                self.current_user,
                suggestion["action"],
                self._current_business_table(),
                suggestion["col_name"],
                suggestion["col_type"],
            )
            if success:
                QMessageBox.information(self, "应用成功", res_msg)
                self._append_log(f"AI 协作调整表结构成功：{res_msg}")
                self._read_fields()
            else:
                QMessageBox.critical(self, "调整失败", res_msg)

    def _append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    def _add_paths(self, raw_paths: list[Path]) -> None:
        # ---- 文件名乱码修复模式：路径交给修复流程（只接受文件夹）----
        if self._fix_mode_check.isChecked():
            for path in raw_paths:
                if path.is_dir():
                    self._start_fix_folder(path)
                else:
                    QMessageBox.information(
                        self, "修复模式", "修复模式只接受文件夹（含其所有子目录的文件）。"
                    )
            return
        try:
            from scanner_core__scan import expand_inputs

            inputs = expand_inputs(raw_paths)     # [(源文件夹根, 文件), …]：保留文件夹归属
        except Exception as exc:
            QMessageBox.critical(self, "错误", f"展开文件路径失败: {exc}")
            return

        if not inputs:
            QMessageBox.information(self, "没有可处理的文件", "只支持 PDF、图片，或者包含这些文件的文件夹。")
            return

        # 新一批文件 = 新的扫描会话：清掉上一次的"已停止"标记（否则新文件不会开跑）
        if self._stop_requested:
            import scan_control__scan as scan_ctl

            self._stop_requested = False
            scan_ctl.begin_scan("ui-new-batch")
            self._append_log("已开始新的扫描会话（清掉此前的停止标记）")

        new_items = 0
        ocr_new = 0
        for source_root, file_path in inputs:
            normalized = file_path.resolve()
            if normalized in self._records:
                continue

            # 需要 OCR 转文字的 PDF/图片 → 排到**处理队列最后**（只改处理顺序，
            # 不改文件位置：不移动/不改名/不动源文件夹；hub 路径仍按源文件夹结构生成）
            try:
                from ocr_priority__scan import needs_ocr

                need_ocr, why = needs_ocr(normalized)
            except Exception as exc:
                need_ocr, why = True, f"判定失败（{type(exc).__name__}: {exc}）→ 按需 OCR 排最后"

            record = TaskRecord(file_path=normalized, source_root=source_root,
                                needs_ocr=need_ocr, ocr_reason=why)
            self._records[normalized] = record

            tag = "  [待OCR]" if need_ocr else ""
            item = QListWidgetItem(f"{normalized.name}{tag}  [{record.status}]")
            item.setData(Qt.ItemDataRole.UserRole, str(normalized))

            self.task_list.addItem(item)
            (self._pending_ocr if need_ocr else self._pending_paths).append(normalized)
            ocr_new += 1 if need_ocr else 0
            self._append_log(
                f"加入队列：{normalized}"
                + (f"（源文件夹：{source_root.name}）" if source_root else "")
                + f"｜{why}" + ("｜已排到 OCR 组（最后处理）" if need_ocr else "")
            )
            new_items += 1

        if new_items:
            if ocr_new:
                self._append_log(
                    f"本批 {new_items} 个文件：{new_items - ocr_new} 个可直接提文字（先处理），"
                    f"{ocr_new} 个需要 OCR（排最后，处理前会询问是否扫描成文字）"
                )
            self._sync_scan_buttons()
            self._start_next_task()

    def _pick_files(self) -> None:
        if self._fix_mode_check.isChecked():
            # 修复模式：只选文件夹（含子目录）
            folder = QFileDialog.getExistingDirectory(self, "选择要修复乱码文件名的文件夹", str(Path.cwd()))
            if folder:
                self._add_paths([Path(folder)])
            return
        file_names, _ = QFileDialog.getOpenFileNames(
            self,
            "选择 PDF 或图片",
            str(Path.cwd()),
            "Documents (*.pdf *.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp)",
        )
        if file_names:
            self._add_paths([Path(name) for name in file_names])

    def _open_output_directory(self) -> None:
        from hub_pipeline__desens import HUB_DIR

        HUB_DIR.mkdir(exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(HUB_DIR)))

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        dropped_paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        self._add_paths(dropped_paths)
        event.acceptProposedAction()

    def _start_next_task(self) -> None:
        """启动下一个任务：**先跑"可直接提文字"的文件，OCR 组留到最后**。

        OCR 组启动前会：① 询问用户是否要扫描成文字；② 释放内存/显存，让 GPU 尽量空出来。
        若用户已点"停止扫描"，这里直接不再启动任何任务（排队中的标为"已停止"）。
        """
        if self._stop_requested:
            self._mark_pending_stopped()
            return
        if self._current_thread is not None:
            return
        if not self._pending_paths:
            # 只剩需要 OCR 的：先问用户
            if self._pending_ocr and not self._ask_ocr_consent():
                return
            if not self._pending_ocr:
                return
            self._pending_paths, self._pending_ocr = self._pending_ocr, []

        file_path = self._pending_paths.pop(0)
        record = self._records[file_path]
        record.status = "处理中"
        self._update_item(file_path)
        self._start_lane_watchdog()

        thread = QThread(self)
        worker = ScanWorker(file_path, current_user=self.current_user,
                            source_root=record.source_root)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.log_message.connect(self._append_log)
        worker.task_started.connect(self._on_task_started)
        worker.task_finished.connect(self._on_task_finished)
        worker.task_failed.connect(self._on_task_failed)

        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_worker_thread_finished)
        self._current_thread = thread
        self._current_worker = worker
        self._sync_scan_buttons()
        thread.start()

    # ===== 停止 / 继续 扫描（协作式取消）=====
    def _sync_scan_buttons(self) -> None:
        """按钮可用性：有活（在处理或排队）→ 可"停止"；有被停止的 → 可"继续"。"""
        busy = bool(self._current_thread is not None or self._pending_paths
                    or self._pending_ocr)
        try:
            self.stop_button.setEnabled(busy and not self._stop_requested)
            self.resume_button.setEnabled(bool(self._stopped_paths))
        except Exception:
            pass

    def _stop_scan(self) -> None:
        """停止扫描：① 请求取消（当前页跑完即停）② 清空排队 ③ 标记状态。"""
        import scan_control__scan as scan_ctl

        if self._current_thread is None and not self._pending_paths and not self._pending_ocr:
            QMessageBox.information(self, "没有在跑的扫描", "当前没有待处理或正在处理的文件。")
            return
        snap = scan_ctl.request_stop(
            reason="用户点击停止扫描",
            by=(self.current_user or {}).get("username", ""),
        )
        self._stop_requested = True
        self._ocr_consent = ""                     # 下次开始重新询问
        queued = list(self._pending_paths) + list(self._pending_ocr)
        self._pending_paths.clear()
        self._pending_ocr.clear()
        for p in queued:
            rec = self._records.get(p)
            if rec and rec.status in ("等待中", "处理中"):
                rec.status = "已停止（未开始）"
                self._update_item(p)
                if p not in self._stopped_paths:
                    self._stopped_paths.append(p)
        if self._current_thread is not None:
            rec = self._records.get(self._current_record_path())
            if rec:
                rec.status = "停止中（当前页跑完即停）"
                self._update_item(rec.file_path)
            self._append_log(
                "已请求停止：当前文件会在**本页识别完成后**停下（不进入后续 AI/概括步骤）；"
                f"排队中的 {len(queued)} 个文件已取消"
            )
        else:
            self._append_log(f"已停止扫描（排队 {len(queued)} 个文件取消）")
        try:
            import runtime_lane__scan as runtime_lane

            runtime_lane.stop_watchdog()
        except Exception:
            pass
        self._append_log(f"停止记录：{scan_ctl.cancel_log_path()}")
        # 停止扫描 = 也停掉后台建图队列（剩下的留给"建图/补齐"按钮，按旧→新继续）
        try:
            import edge_build__graph_edges as eb

            if eb.build_queue_status().get("in_flight"):
                eb.request_stop_build("用户停止扫描")
                self._append_log("后台建图队列已请求停止（剩余的在「建图/补齐」里继续）")
        except Exception:
            pass
        self._sync_scan_buttons()

    def _current_record_path(self) -> Path | None:
        """当前正在处理的文件路径（用于状态展示）。"""
        for p, rec in self._records.items():
            if rec.status.startswith(("处理中", "停止中")):
                return p
        return None

    def _mark_pending_stopped(self) -> None:
        """停止态下不启动新任务：把仍在排队的标成"已停止"。"""
        queued = list(self._pending_paths) + list(self._pending_ocr)
        self._pending_paths.clear()
        self._pending_ocr.clear()
        for p in queued:
            rec = self._records.get(p)
            if rec:
                rec.status = "已停止（未开始）"
                self._update_item(p)
            if p not in self._stopped_paths:
                self._stopped_paths.append(p)
        self._sync_scan_buttons()

    def _resume_scan(self) -> None:
        """继续扫描：把"已停止"的文件重新排队（可继续处理剩余文件）。"""
        import scan_control__scan as scan_ctl

        paths = list(self._stopped_paths)
        if not paths:
            QMessageBox.information(self, "没有可继续的", "没有处于「已停止」状态的文件。")
            return
        self._stopped_paths.clear()
        self._stop_requested = False
        scan_ctl.begin_scan("ui-resume")            # 新会话：清掉"已停止"标记
        self._append_log(f"继续扫描：重新排队 {len(paths)} 个文件")
        self._enqueue_existing(paths)

    def _enqueue_existing(self, paths: list[Path]) -> None:
        """把已有记录重新排队（按"是否需 OCR"分回两个队列，源文件不动）。"""
        for p in paths:
            rec = self._records.get(p)
            if rec is None:
                continue
            rec.status = "等待中"
            self._update_item(p)
            (self._pending_ocr if rec.needs_ocr else self._pending_paths).append(p)
        self._sync_scan_buttons()
        self._start_next_task()

    # ===== OCR 组：询问 + 释放资源 + 看门狗（线程挤压/卡死预警）=====
    def _ask_ocr_consent(self) -> bool:
        """到 OCR 组时询问用户是否扫描成文字；同意则先释放内存/显存。"""
        if self._stop_requested:
            return False
        if self._ocr_consent == "skip":
            self._skip_all_ocr("此前已选择跳过")
            return False
        if self._ocr_consent == "all":
            self._prepare_ocr_group()
            return True
        names = "、".join(p.name for p in self._pending_ocr[:5])
        more = f" 等 {len(self._pending_ocr)} 个" if len(self._pending_ocr) > 5 else ""
        box = QMessageBox(self)
        box.setWindowTitle("是否扫描成文字（OCR）")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            f"可直接提取文字的文件已处理完。\n\n"
            f"剩余 {len(self._pending_ocr)} 个文件需要 OCR 转文字：\n{names}{more}\n\n"
            f"现在开始扫描吗？（开始前会先释放前面占用的内存与显存）\n"
            f"注意：只影响处理顺序，**不会移动/改名/改动源文件位置**。"
        )
        b_all = box.addButton("开始扫描全部", QMessageBox.ButtonRole.AcceptRole)
        b_skip = box.addButton("跳过全部（稍后手动）", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(b_all)
        box.exec()
        if box.clickedButton() is b_skip:
            self._ocr_consent = "skip"
            self._skip_all_ocr("用户选择跳过")
            return False
        self._ocr_consent = "all"
        self._prepare_ocr_group()
        return True

    def _prepare_ocr_group(self) -> None:
        """OCR 前：释放内存/显存 + 启动看门狗（并把释放结果写日志）。"""
        try:
            import resource_release__scan as resource_release

            rep = resource_release.release_before_ocr(force=True, reason="开始 OCR 组前释放资源")
            self._append_log(
                f"OCR 前资源释放：RSS {rep.get('rss_before_mb')}→{rep.get('rss_after_mb')} MB｜"
                f"GPU reserved {rep.get('gpu_before', {}).get('reserved_mb')}→"
                f"{rep.get('gpu_after', {}).get('reserved_mb')} MB"
            )
        except Exception as exc:
            self._append_log(f"OCR 前资源释放失败（不影响扫描）：{type(exc).__name__}: {exc}")
        self._start_lane_watchdog()

    def _skip_all_ocr(self, why: str) -> None:
        skipped = list(self._pending_ocr)
        self._pending_ocr.clear()
        for p in skipped:
            rec = self._records.get(p)
            if rec:
                rec.status = "已跳过（未 OCR）"
                self._update_item(p)
        if skipped:
            self._append_log(f"{why}：{len(skipped)} 个需要 OCR 的文件未扫描（源文件未改动，可稍后重拖）")

    def _start_lane_watchdog(self) -> None:
        """启动重活看门狗：线程挤压/疑似卡死会写日志并在这里提示。"""
        try:
            import runtime_lane__scan as runtime_lane

            runtime_lane.start_watchdog()
            if not hasattr(self, "_lane_timer"):
                self._lane_timer = QTimer(self)
                self._lane_timer.setInterval(4000)
                self._lane_timer.timeout.connect(self._poll_lane_incidents)
                self._seen_incidents = 0
            self._lane_timer.start()
        except Exception:
            pass

    def _poll_lane_incidents(self) -> None:
        """轮询闸门事件：新增的告警/卡死立即写进运行日志；CRITICAL 弹一次提示。"""
        try:
            import runtime_lane__scan as runtime_lane

            items = runtime_lane.incidents()
            new = items[: max(0, len(items) - getattr(self, "_seen_incidents", 0))]
            self._seen_incidents = len(items)
            for rec in reversed(new):
                self._append_log(f"[重活闸门·{rec.get('level')}] {rec.get('message')}")
                if rec.get("level") == "CRITICAL":
                    QMessageBox.warning(
                        self, "疑似卡死",
                        rec.get("message") + "\n\n已写入日志：logs/perf/lane_watchdog_*.jsonl\n"
                        "可继续等待，或在该文件处理完后重试；若反复出现请把日志发我。"
                    )
        except Exception:
            pass

    def _on_task_started(self, file_path_text: str) -> None:
        self._append_log(f"开始：{file_path_text}")

    def _on_task_finished(self, file_path_text: str, result: object) -> None:
        file_path = Path(file_path_text)
        record = self._records.get(file_path)
        if record is None:
            return

        if isinstance(result, dict):
            record.category = result.get("category", "")
            record.hub_json_path = result.get("hub_json_path")
            record.cache_json_paths = result.get("cache_json_paths", [])
            record.doc_key = result.get("doc_key", "") or record.doc_key
            if result.get("replaced_document"):
                rep = result["replaced_document"]
                self._append_log(
                    f"副本替换：新文件 {file_path.name} 替代 {rep.get('doc_key')}"
                    f"（旧 doc_no={rep.get('doc_no')} 已退役留空），所有工作已重新跑一遍"
                )

        # 内容级去重命中：同一内容（复制件/改名/已扫过的同一文件）不再重复扫描 ——
        # 直接标状态并返回，避免生成第二套 hub/事实/节点（防节点污染）。
        if isinstance(result, dict) and result.get("skipped"):
            action = result.get("skip_action") or "skipped"
            record.status = f"已跳过 [{action}]"
            record.category = record.category or ""
            self._update_item(file_path)
            self._append_log(
                f"跳过（{action}）：{file_path.name} | {result.get('skip_reason')}"
                + (f" | 同内容首见：{result['duplicate_of']}" if result.get("duplicate_of") else "")
            )
            self._load_preview_for_path(file_path)
            return

        record.status = f"已完成 [{record.category}]" if record.category else "已完成"
        self._update_item(file_path)
        self._append_log(f"分类：{record.category} | Hub 输出：{record.hub_json_path}")

        # ★入账口径（本轮变更）★：**扫描不再自动写台账**。
        # 识别只负责"提名"：合同（PDF/Word + 命名/标题含合同/协议）与发票连同
        # 已脱敏的预填字段进入 `ledger_inbox`（待入账队列），由用户在
        # 「待入账审核」窗口逐条决定入账/不入账。
        staged = result.get("ledger_inbox") if isinstance(result, dict) else None
        if isinstance(staged, dict) and staged.get("ok"):
            kind_label = {"contract": "合同", "invoice": "发票", "other": "其它"}.get(
                staged.get("kind", ""), staged.get("kind", ""))
            self._append_log(
                f"已送待入账队列（{kind_label}）：{file_path.name}"
                + (f" | 目标台账={staged.get('target_table')}" if staged.get("target_table")
                   else " | 无对应台账，仅登记")
                + f" | 待入账条目 id={staged.get('id')}"
            )
            self._refresh_inbox_hint()
        elif isinstance(result, dict) and result.get("ledger_inbox_error"):
            self._append_log(f"待入账登记失败（不影响扫描）：{result['ledger_inbox_error']}")
        if isinstance(result, dict) and result.get("ledger_skipped"):
            self._append_log(f"合同判定：不是合同（不入待入账队列）——{result['ledger_skipped']}")

        # AI 台账字段里若识别不到合同编号：**不再直接写库**，只提示用户到
        # 「待入账审核」窗口补编号后入账（人工补录仍在同一个人工确认环节）。
        ai_report = result.get("ai_report") if isinstance(result, dict) else None
        if isinstance(ai_report, dict):
            ledger = ai_report.get("ledger") or {}
            if ledger.get("need_code"):
                self._append_log(
                    f"合同编号未从标题识别到（{file_path.name}）：已进待入账队列，"
                    f"请在「待入账审核」窗口补编号后再入账"
                )

        self._load_preview_for_path(file_path)

    def _on_task_failed(self, file_path_text: str, error_text: str) -> None:
        file_path = Path(file_path_text)
        record = self._records.get(file_path)
        if record is None:
            return

        if str(error_text).startswith("已停止"):
            # 用户点了停止：标"已停止"，并记入可"继续扫描"的清单
            record.status = error_text
            self._update_item(file_path)
            if file_path not in self._stopped_paths:
                self._stopped_paths.append(file_path)
            self._append_log(f"{file_path.name} → {error_text}（已落盘的页缓存保留，可稍后继续）")
        else:
            record.status = f"失败：{error_text}"
            self._update_item(file_path)
        self._sync_scan_buttons()

    def _on_worker_thread_finished(self) -> None:
        self._current_thread = None
        self._current_worker = None
        # 队列都空了 → 停掉看门狗轮询（避免空转）；还有活就继续
        if not self._pending_paths and not self._pending_ocr:
            try:
                if hasattr(self, "_lane_timer"):
                    self._lane_timer.stop()
                import runtime_lane__scan as runtime_lane

                runtime_lane.stop_watchdog()
            except Exception:
                pass
            # 整批扫完：刷新"待建"计数；并按需在后台补齐存量（旧文件优先）。
            self._refresh_graph_hint()
            if os.getenv("EDGE_AUTO_BACKLOG", "1").strip() not in ("0", "false", "no"):
                self._auto_build_backlog()
        self._sync_scan_buttons()
        self._start_next_task()

    def _auto_build_backlog(self) -> None:
        """整批扫完后，后台补齐"库里有、还没建图"的文档（旧→新）。

        · 为什么在这里：这就是"存量优先"的落点——新文件在扫描时已经各自增量建过图，
          这里只处理历史遗留（以及上次被停止/失败的那几份）；
        · 后台线程 + 幂等：`build_pending` 内部按指纹跳过已建的，不会重复花 token；
        · 可用 .env 的 `EDGE_AUTO_BACKLOG=0` 关掉自动补齐（改为手动点按钮）。
        """
        try:
            import edge_build__graph_edges as eb

            pend = eb.pending_docs()
            if not pend:
                return
            if eb.build_stop_requested():
                eb.clear_build_stop()
            self._append_log(
                f"自动建图：还有 {len(pend)} 份未建关系，正在后台补齐（旧→新，"
                f"可用 EDGE_AUTO_BACKLOG=0 关闭）"
            )

            def _worker() -> None:
                try:
                    res = eb.build_pending()
                    print(f"[自动建图] 补齐结束：{res}", flush=True)
                except Exception as exc:
                    print(f"[自动建图] 补齐失败：{type(exc).__name__}: {exc}", flush=True)

            import threading

            threading.Thread(target=_worker, daemon=True, name="edge-backlog").start()
        except Exception as exc:
            self._append_log(f"自动建图调度失败（不影响扫描）：{type(exc).__name__}: {exc}")

    def _update_item(self, file_path: Path) -> None:
        for index in range(self.task_list.count()):
            item = self.task_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == str(file_path):
                record = self._records[file_path]
                item.setText(f"{file_path.name}  [{record.status}]")
                break

    # ===== 删除文件（连同它的节点/边/向量记录一起清；源文件默认不动）=====
    def _task_list_menu(self, pos) -> None:
        item = self.task_list.itemAt(pos)
        if item is None:
            return
        self.task_list.setCurrentItem(item)
        menu = QMenu(self)
        act_del = menu.addAction("删除（含节点/边/向量记录）")
        act_clear = menu.addAction("仅从列表移除（不动库里记录）")
        chosen = menu.exec(self.task_list.mapToGlobal(pos))
        if chosen is act_del:
            self._delete_selected_document()
        elif chosen is act_clear:
            self._remove_from_list_only()

    def _selected_record(self) -> tuple[Path, TaskRecord] | None:
        item = self.task_list.currentItem()
        if item is None:
            return None
        stored = item.data(Qt.ItemDataRole.UserRole)
        if not stored:
            return None
        path = Path(stored)
        record = self._records.get(path)
        return (path, record) if record else None

    def _remove_from_list_only(self) -> None:
        picked = self._selected_record()
        if not picked:
            return
        path, _record = picked
        for index in range(self.task_list.count()):
            if self.task_list.item(index).data(Qt.ItemDataRole.UserRole) == str(path):
                self.task_list.takeItem(index)
                break
        self._records.pop(path, None)
        self._pending_paths = [p for p in self._pending_paths if p != path]
        self._append_log(f"已从列表移除（库里记录保留）：{path}")

    def _delete_selected_document(self) -> None:
        """删除选中文件：级联清掉它的 hub 产物、L1/事实、图节点与**指向它的边**、向量分块。

        编号退役：其 doc_no 记入墓碑后**永久留空**，后面新文件不会填充该编号。
        源文件默认**不动**（不破坏源文件夹），需要时可在弹窗里勾选一并删除。
        """
        picked = self._selected_record()
        if not picked:
            QMessageBox.information(self, "请选择", "请先在任务列表里选择一份文件。")
            return
        path, record = picked
        if self._current_thread is not None and record.status == "处理中":
            QMessageBox.warning(self, "正在处理", "该文件正在处理中，请等它结束后再删除。")
            return
        doc_key = record.doc_key or (Path(record.hub_json_path).stem if record.hub_json_path else "")
        try:
            import doc_lifecycle__desens

            preview = doc_lifecycle__desens.plan_ingest(path)   # 只为展示判定信息
        except Exception:
            preview = None
        msg = (f"删除「{path.name}」？\n\n"
               f"将一并删除：\n"
               f"· hub 产物（hub/<doc_key>.json 与伴生 .l1.json/.features.json/.ai.log）\n"
               f"· L1 文档/段落/事实清单、hub 资产索引\n"
               f"· 图中它的节点，以及**所有指向它的边**（含其它文档连过来的边）\n"
               f"· RAG 向量分块、内容指纹索引\n\n"
               f"其编号（doc_no）会记入退役墓碑并**永久留空**，后面新文件不会填充该编号。\n"
               f"源文件**不会**被删除（不破坏源文件夹）。")
        if doc_key:
            msg += f"\n\ndoc_key：{doc_key}"
        if preview is not None and getattr(preview, "reason", ""):
            msg += f"\n副本判定：{preview.reason}"
        box = QMessageBox(self)
        box.setWindowTitle("确认删除")
        box.setText(msg)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        try:
            import doc_lifecycle__desens

            report = doc_lifecycle__desens.delete_document(
                doc_key, user=self.current_user, reason="用户在任务列表删除",
            )
            self._append_log("删除完成：" + report.summary())
        except Exception as exc:
            QMessageBox.critical(self, "删除失败", f"{type(exc).__name__}: {exc}")
            return
        self._remove_from_list_only()
        self.preview.setPlainText(f"已删除：{path.name}\n\n{report.summary()}")

    def _show_selected_preview(self, current: QListWidgetItem | None, previous: QListWidgetItem | None) -> None:
        if current is None:
            return
        stored = current.data(Qt.ItemDataRole.UserRole)
        if stored:
            self._load_preview_for_path(Path(stored))

    def _load_preview_for_path(self, file_path: Path) -> None:
        record = self._records.get(file_path)
        if record is None:
            return

        if record.hub_json_path is not None:
            try:
                payload = json.loads(Path(record.hub_json_path).read_text(encoding="utf-8"))
                self.preview.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))
            except Exception as exc:
                self.preview.setPlainText(f"无法读取预览：{exc}")
        else:
            self.preview.setPlainText(f"{file_path}\n\n状态：{record.status}")

    # ===== 右上角设置菜单：登录信息 / 退出登录 / 注销账户 / 用户管理 =====
    def _build_top_right_menu(self) -> None:
        """主窗口右上角"设置"菜单（QMenuBar 右上角挂件）。

        · 「用户管理」仅最高管理员可见；
        · 服务端（database_serv）负责全部权限与约束校验，UI 只做调用与提示。
        """
        menu = QMenu(self)
        act_info = menu.addAction("查看登录信息")
        act_info.triggered.connect(self._show_user_info)
        # 当前仓库（工作区）一眼可见 + 切换入口：切仓库会因为用户表也在各仓库里而需要重新登录
        try:
            import workspace__infra as _ws

            _a = _ws.active() or {}
            cur_ws = f"{_a.get('name') or '(未登记)'}｜库 {_a.get('db') or '-'}"
        except Exception:
            cur_ws = "（读取失败）"
        act_ws = menu.addAction(f"当前仓库：{cur_ws}")
        act_ws.triggered.connect(self._open_workspaces)
        menu.addSeparator()
        if (self.current_user or {}).get("role_type") == "admin":
            act_ws_admin = menu.addAction("仓库管理（新建 / 切换 / 重置）…")
            act_ws_admin.triggered.connect(self._open_workspaces)
            menu.addSeparator()
            act_manage = menu.addAction("用户管理（最高管理员）")
            act_manage.triggered.connect(self._open_admin_users)
            # 项目名称自定义加密（需求 4）：登记 + 分类 + AI 提案审批
            act_project = menu.addAction("项目名称加密（最高管理员）")
            act_project.triggered.connect(self._open_project_registry)
            # 本公司名称确认（登录时自动确认一次；此处可复核/补充）
            act_self = menu.addAction("本公司名称确认（最高管理员）")
            act_self.triggered.connect(self._open_self_company)
            # 假设边确认（L3 图遍历：假设 → 证实/驳回）
            act_hypo = menu.addAction("假设边确认（图遍历 L3）")
            act_hypo.triggered.connect(self._open_hypothesis_edges)
            # Hub 状态 / 删除指定文件 / 文件格式化
            # （注意：这里的「Hub」就是脱敏产物目录；「仓库」在本程序里指工作区/隔离环境）
            act_repo = menu.addAction("Hub 状态与文件格式化（最高管理员）")
            act_repo.triggered.connect(self._open_hub_admin)
            # 脱敏登记表管理：查看/删除错登、人工补登、项目分支登记
            act_entity = menu.addAction("脱敏登记表管理（最高管理员）")
            act_entity.triggered.connect(self._open_entity_admin)
            # 台账**全字段**管理：看全部字段 / 人工改任意列 / 导出 xlsx-csv（仅最高管理员）
            act_ledger = menu.addAction("台账全字段管理（最高管理员）")
            act_ledger.triggered.connect(self._open_ledger_admin)
            menu.addSeparator()
        act_logout = menu.addAction("退出登录")
        act_logout.triggered.connect(self._logout)
        act_deactivate = menu.addAction("注销账户…")
        act_deactivate.triggered.connect(self._deactivate_account)

        self._settings_button = QPushButton("⚙ 设置")
        self._settings_button.setMenu(menu)
        self._settings_button.setFlat(True)
        self._settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.menuBar().setCornerWidget(self._settings_button, Qt.Corner.TopRightCorner)

    def _show_user_info(self) -> None:
        """设置 → 查看登录信息：展示当前登录用户资料。"""
        u = self.current_user or {}
        role = _role_label(u.get("role_type", ""))
        lines = [
            f"用户名：{u.get('username', '')}",
            f"角色：{role}",
            f"账号状态：{'正常' if u.get('is_active', True) else '已停用'}",
            f"最近登录：{u.get('last_login_at') or '本次（首次记录）'}",
        ]
        if u.get("role_type") == "admin":
            lines.append("")
            lines.append("您是最高管理员，可在「设置 → 用户管理」中查看/停用/转让。")
        QMessageBox.information(self, "登录信息", "\n".join(lines))

    def _logout(self) -> None:
        """退出登录：回到登录对话框（由 main() 的登录循环接管）。"""
        self.logout_requested.emit()
        self.close()

    def _deactivate_account(self) -> None:
        """设置 → 注销账户：软停用当前账号（最高管理员须先转让，服务端强制）。"""
        u = self.current_user or {}
        if u.get("role_type") == "admin":
            QMessageBox.warning(
                self,
                "无法注销",
                "最高管理员不能直接注销账号。\n"
                "请先通过「设置 → 用户管理 → 转让最高管理员」把管理权转交他人，"
                "再用新账号登录后注销本账号。",
            )
            return
        reply = QMessageBox.question(
            self,
            "确认注销",
            "确定注销当前账号吗？\n注销后该账号将无法登录（如误操作，可由最高管理员在"
            "「用户管理」中重新启用）。",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        pwd, ok_in = QInputDialog.getText(
            self, "密码确认", "请输入您的登录密码以确认注销：", QLineEdit.EchoMode.Password
        )
        if not (ok_in and pwd):
            return
        ok, msg = api_deactivate_self(self.current_user, pwd)
        if ok:
            QMessageBox.information(self, "注销成功", msg)
            self._logout()
        else:
            QMessageBox.warning(self, "注销失败", msg)

    def _open_admin_users(self) -> None:
        """设置 → 用户管理（仅 admin）：转让成功后本机退出登录。"""
        dialog = AdminUsersDialog(self, self.current_user)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # 转让成功且本机用户已降级 → 退出登录（转让结果提示已在窗口内展示）
            self._logout()

    def _open_project_registry(self) -> None:
        """设置 → 项目名称加密（仅 admin）：自定义加密登记 + 分类 + AI 提案审批。"""
        from project_admin__ui import ProjectRegistryDialog  # 惰性导入，避免拖慢启动

        ProjectRegistryDialog(self, self.current_user).exec()

    def _open_hub_admin(self) -> None:
        """设置 → Hub 状态与文件格式化（仅 admin）：查看源文件、删指定文件、格式化。"""
        from hub_admin__ui import HubAdminDialog  # 惰性导入，避免拖慢启动

        HubAdminDialog(self, self.current_user).exec()

    def _open_workspaces(self) -> None:
        """设置 → 仓库管理：新建/切换/体检/重置/删除登记（环境隔离）。

        切换仓库会让当前登录失效（用户表也在各仓库里）→ 直接退出登录回到登录框。
        """
        from workspace_admin__ui import WorkspaceDialog  # 惰性导入，避免拖慢启动

        dialog = WorkspaceDialog(self, self.current_user)
        dialog.exec()
        if dialog.switched:
            QMessageBox.information(
                self, "已切换仓库",
                "仓库已切换，需要重新登录该仓库的账号（用户表也在各自仓库里）。",
            )
            self._logout()

    def _open_entity_admin(self) -> None:
        """设置 → 脱敏登记表管理（仅 admin）：查看/删除错登、补登、项目分支登记。"""
        from entity_admin__ui import EntityAdminDialog  # 惰性导入，避免拖慢启动

        EntityAdminDialog(self, self.current_user).exec()

    def _open_ledger_admin(self, table_name: str = "contract_projects") -> None:
        """设置 → 台账全字段管理（仅 admin）：完整字段 + 人工改 + 导出表格。"""
        from ledger_admin__ui import LedgerAdminDialog  # 惰性导入，避免拖慢启动

        LedgerAdminDialog(self, self.current_user, table_name).exec()

    # ===== 待入账审核（合同/发票是否入账由用户决定）=====
    def _open_ledger_inbox(self) -> None:
        """打开"入账审核"窗口：查看合同/发票候选并决定是否入账。"""
        from ledger_inbox__ui import LedgerInboxDialog  # 惰性导入，避免拖慢启动

        LedgerInboxDialog(self, self.current_user).exec()
        self._refresh_inbox_hint()

    def _refresh_inbox_hint(self) -> None:
        """把"待入账 N 条"记下来（按钮文字保持短，计数进状态行 + tooltip，避免被裁）。"""
        try:
            import ledger_inbox__desens as inbox

            c = inbox.counts()
            self._inbox_pending = int(c.get("pending", 0))
        except Exception:
            self._inbox_pending = getattr(self, "_inbox_pending", 0)
        n = self._inbox_pending
        self.inbox_button.setToolTip(
            f"待入账审核：{n} 条待你决定是否入账" if n else "暂无待入账项")
        self._refresh_status_hint()

    def _refresh_status_hint(self) -> None:
        """状态行：把动态计数集中显示（自动换行，窄窗口也看得全）。"""
        if not hasattr(self, "status_hint"):
            return
        parts = [f"待入账 {self._inbox_pending} 条"]
        g = getattr(self, "_graph_counts", {}) or {}
        if g:
            tail = f"｜正在建 {g['in_flight']}" if g.get("in_flight") else ""
            parts.append(f"建图：待建 {g.get('pending', 0)}｜节点 {g.get('nodes', 0)}"
                         f"｜边 {g.get('edges', 0)}{tail}")
        self.status_hint.setText("｜".join(parts))

    # ===== 建图 / 补齐关系（动态建图的存量补建入口）=====
    def _refresh_graph_hint(self) -> None:
        """把"待建 N 份 / 节点 / 边"记下来（同样走状态行，按钮文字保持短）。"""
        try:
            import edge_build__graph_edges as eb

            st = eb.graph_state()
            q = eb.build_queue_status()
            self._graph_counts = {
                "pending": int(st.get("pending", 0)),
                "nodes": int(st.get("node_total", 0)),
                "edges": int(st.get("edges", {}).get("total", 0)),
                "in_flight": int(q.get("in_flight", 0) or 0),
            }
            self.graph_button.setToolTip(
                f"建图/补齐关系：待建 {self._graph_counts['pending']} 份｜"
                f"节点 {self._graph_counts['nodes']}｜边 {self._graph_counts['edges']}")
        except Exception:
            pass
        self._refresh_status_hint()

    def _open_graph_tools(self) -> None:
        """建图/补齐窗口：看状态 + 一键补齐存量（后台跑，不卡界面）。"""
        from graph_admin__ui import GraphAdminDialog

        GraphAdminDialog(self, self.current_user).exec()
        self._refresh_graph_hint()

    # ===== 常驻 AI 对话窗口（菜单栏"AI 对话"开关 + 右侧停靠）=====
    def _build_ai_chat_dock(self) -> None:
        """创建"AI 对话"停靠窗（右侧，可拖动/悬浮/关闭后经菜单再打开）。

        对话面板（chat_panel.ChatPanel）只依赖 chat_engine 公开接口——
        RAG / 图节点检索后续接入时无需改动主窗口。
        """
        from chat_panel__ui import ChatDock  # 惰性导入，避免拖慢启动

        username = (self.current_user or {}).get("username", "user")
        self._ai_dock = ChatDock(username, self)
        # 最小宽度压小（原来 380）：小屏上右栏 + 中央区一起会超出窗口宽度，
        # 中央区的按钮就被挤到换行/裁字。用户仍可拖宽（停靠窗自带拖动分界）。
        self._ai_dock.setMinimumWidth(300)
        self._ai_dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea
                                      | Qt.DockWidgetArea.RightDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._ai_dock)

        ai_menu = self.menuBar().addMenu("AI 对话")
        self._ai_toggle_action = ai_menu.addAction("显示 / 隐藏对话窗口")
        self._ai_toggle_action.setCheckable(True)
        self._ai_toggle_action.setChecked(True)
        self._ai_toggle_action.toggled.connect(self._ai_dock.setVisible)
        self._ai_dock.visibilityChanged.connect(self._ai_toggle_action.setChecked)

    # ===== 文件名乱码修复模式（与扫描互斥，拖动区切换）=====
    def _on_fix_mode_toggled(self, on: bool) -> None:
        """切换提示文案：扫描模式 vs 修复模式（不影响其它功能）。"""
        if on:
            self.drop_hint.setText(
                "【乱码修复模式】拖入：文件夹（含所有子目录）。\n"
                "流程：先选择报告保存位置 → 只扫描名称（不读内容）→ 预览确认 → "
                "原地改名（后缀/位置/内容均不变）。"
            )
        else:
            self.drop_hint.setText("把 PDF、图片或文件夹直接拖到窗口里，系统会自动进入输入队列。")

    def _start_fix_folder(self, folder: Path) -> None:
        """修复模式主流程（主线程同步执行，避免后台线程信号投递问题）。

        问报告位置 → 扫描（进度窗 + 可取消）→ 预览确认 → 原地改名 → 写报告。
        实测本地 6 万文件扫描约 1s，故不引入 QThread；用 QProgressDialog +
        processEvents 保持界面可刷新、可取消，杜绝“一直扫描、不出选择界面”。
        """
        from filename_fix__ui import perform_renames, scan_folder
        from fix_rename_ui__ui import FixPreviewDialog
        from PySide6.QtWidgets import QProgressDialog

        if self._fix_busy:
            QMessageBox.information(self, "提示", "修复任务进行中，请稍候。")
            return
        if self._current_thread is not None:
            QMessageBox.information(self, "提示", "扫描任务进行中，请先等待其完成。")
            return
        # 需求点：输入文件夹后，首先询问保存（报告）路径
        report_dir = QFileDialog.getExistingDirectory(
            self, "请选择处理报告保存位置（修复本身在原地进行）", str(Path.cwd())
        )
        if not report_dir:
            QMessageBox.information(self, "已取消", "未选择报告保存位置，操作已取消。")
            return

        self._fix_busy = True
        self._append_log(f"【乱码修复】开始扫描：{folder}")

        import time as _time

        progress = QProgressDialog("正在检查文件名…", "取消", 0, 0, self)
        progress.setWindowTitle("乱码修复 - 扫描")
        progress.setMinimumDuration(300)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        cancelled = {"flag": False}
        last = {"t": _time.monotonic()}

        def on_progress(n: int) -> None:
            now = _time.monotonic()
            if now - last["t"] >= 0.2:
                progress.setLabelText(f"已检查 {n} 个文件名…")
                last["t"] = now
            if progress.wasCanceled():
                cancelled["flag"] = True
            QApplication.processEvents()  # 刷新窗口/响应取消按钮

        try:
            items, notes = scan_folder(
                str(folder),
                progress=on_progress,
                cancel=lambda: cancelled["flag"] or progress.wasCanceled(),
            )
        finally:
            progress.close()
        for n in notes[:8]:
            self._append_log(f"【乱码修复】{n}")
        if cancelled["flag"]:
            QMessageBox.information(self, "已取消", "扫描已取消（未执行任何改名）。")
            self._finish_fix()
            return
        if not items:
            QMessageBox.information(self, "未发现", "该文件夹（含子目录）未发现可修复的乱码文件名。")
            self._finish_fix()
            return

        dialog = FixPreviewDialog(items, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            QMessageBox.information(self, "已取消", "未执行任何改名。")
            self._finish_fix()
            return
        selected = dialog.selected_items()
        if not selected:
            QMessageBox.information(self, "未选择", "未勾选任何文件，未执行改名。")
            self._finish_fix()
            return

        self._append_log(f"【乱码修复】预览确认 {len(selected)} 项，原地改名…")
        report_lines: list[str] = []
        ok_count, err_list = perform_renames(selected, report_lines)
        report = self._write_fix_report(report_dir, "乱码修复报告", report_lines, err_list)
        self._append_log(f"【乱码修复】完成：成功 {ok_count}，失败 {len(err_list)}；报告：{report}")
        QMessageBox.information(
            self,
            "乱码修复完成",
            f"成功改名：{ok_count} 项\n失败：{len(err_list)} 项\n\n处理报告：\n{report}",
        )
        self._finish_fix()

    def _write_fix_report(self, report_dir: str, prefix: str,
                          lines: list[str], errors: list[str]) -> str:
        """把处理报告写入 report_dir，返回报告路径（失败时返回错误说明）。"""
        import os
        import time as _time

        try:

            os.makedirs(report_dir, exist_ok=True)
            path = os.path.join(report_dir, f"{prefix}_{_time.strftime('%Y%m%d_%H%M%S')}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"{prefix}（{_time.strftime('%Y-%m-%d %H:%M:%S')}）\n")
                f.write(f"成功 {len(lines)} 项，失败 {len(errors)} 项\n\n")
                f.write("\n".join(lines) if lines else "（无）")
                if errors:
                    f.write("\n\n失败明细：\n" + "\n".join(errors))
            return path
        except Exception as exc:
            return f"报告写入失败：{exc}"

    def _start_fix_watchdog(self) -> None:
        """扫描看门狗：扫描 >15 秒仍未结束 → 弹窗询问“继续等待/取消”。

        防止“一直扫描、连选择界面都不出现”时用户只能干等。
        """
        if not hasattr(self, "_fix_watchdog") or self._fix_watchdog is None:
            self._fix_watchdog = QTimer(self)
            self._fix_watchdog.setInterval(3000)
            self._fix_watchdog.timeout.connect(self._on_fix_watchdog)
        self._fix_watchdog.start()

    def _on_fix_watchdog(self) -> None:
        import time as _time

        if not self._fix_busy or self._fix_phase != "scan":
            self._stop_fix_watchdog()
            return
        if self._fix_asked_cancel:
            return  # 已经问过，等线程响应取消
        elapsed = _time.monotonic() - getattr(self, "_fix_started", _time.monotonic())
        if elapsed < 15:
            return
        self._fix_asked_cancel = True
        reply = QMessageBox.question(
            self,
            "扫描似乎较慢",
            f"已扫描 {elapsed:.0f} 秒仍未完成（已检查 "
            f"{getattr(self, '_fix_last_progress', 0)} 个文件名）。\n"
            "可能该目录包含大量文件或位于网络盘。\n继续等待，还是取消？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            worker = getattr(self, "_fix_scan_worker", None)
            if worker is not None:
                worker.request_cancel()  # 线程在下一个目录处停下并返回已发现部分
            self._append_log("【乱码修复】用户请求取消扫描…")

    def _stop_fix_watchdog(self) -> None:
        if hasattr(self, "_fix_watchdog") and self._fix_watchdog is not None:
            self._fix_watchdog.stop()

    def _on_fix_scan_progress(self, checked: object) -> None:
        """扫描进度反馈：节流写日志 + 更新拖放区提示，让用户看到任务在动。"""
        n = int(checked) if checked is not None else 0
        if not hasattr(self, "_fix_last_progress"):
            self._fix_last_progress = 0
        if n - self._fix_last_progress >= 1000 or (n == 0 and self._fix_last_progress == 0):
            self._fix_last_progress = n
            self._append_log(f"【乱码修复】已检查 {n} 个文件名…")
        # 每 500 个刷新一次提示文字（含用时，能直观看到“在动”）
        if n % 500 < 50:
            import time as _time

            el = _time.monotonic() - getattr(self, "_fix_started", _time.monotonic())
            self.drop_hint.setText(f"扫描中…已检查 {n} 个文件名（{el:.0f}s）")

    def _on_fix_scan_done(self, items: object) -> None:
        """扫描完成：无可修复→提示；否则预览确认后执行改名。"""
        from fix_rename_ui__ui import FixApplyWorker, FixPreviewDialog

        if getattr(self, "_fix_asked_cancel", False):
            QMessageBox.information(self, "已取消", "扫描已取消（未执行任何改名）。")
            self._finish_fix()
            return
        items = list(items) if isinstance(items, list) else []
        if not items:
            QMessageBox.information(
                self, "未发现", "该文件夹（含子目录）未发现可修复的乱码文件名，未做任何改动。"
            )
            self._finish_fix()
            return
        dialog = FixPreviewDialog(items, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            QMessageBox.information(self, "已取消", "未执行任何改名。")
            self._finish_fix()
            return
        selected = dialog.selected_items()
        if not selected:
            QMessageBox.information(self, "未选择", "未勾选任何文件，未执行改名。")
            self._finish_fix()
            return
        self._append_log(f"【乱码修复】预览确认 {len(selected)} 项，开始原地改名…")
        worker = FixApplyWorker(selected, self._fix_report_dir)
        thread = QThread(self)
        worker.moveToThread(thread)
        CT = Qt.ConnectionType.QueuedConnection
        thread.started.connect(worker.run)
        worker.done.connect(self._on_fix_apply_done, CT)
        worker.failed.connect(self._on_fix_flow_error, CT)
        worker.done.connect(thread.quit, CT)
        worker.failed.connect(thread.quit, CT)
        thread.finished.connect(worker.deleteLater, CT)
        thread.finished.connect(self._on_fix_thread_finished, CT)
        self._fix_thread = thread
        thread.start()

    def _on_fix_apply_done(self, summary: object, errors: object) -> None:
        summary = dict(summary) if isinstance(summary, dict) else {}
        ok_n = summary.get("ok", 0)
        err_list = list(errors) if isinstance(errors, list) else []
        report = summary.get("report", "")
        self._append_log(
            f"【乱码修复】完成：成功 {ok_n} 项，失败 {len(err_list)} 项；报告：{report}"
        )
        QMessageBox.information(
            self,
            "乱码修复完成",
            f"成功改名：{ok_n} 项\n失败：{len(err_list)} 项\n\n处理报告：\n{report}",
        )
        self._finish_fix()

    def _on_fix_flow_error(self, err: object) -> None:
        QMessageBox.critical(self, "乱码修复失败", str(err))
        self._finish_fix()

    def _on_fix_thread_finished(self) -> None:
        self._fix_thread = None

    def _finish_fix(self) -> None:
        self._stop_fix_watchdog()
        self._fix_busy = False
        self._fix_report_dir = ""
        self._fix_thread = None
        self._fix_scan_worker = None
        self._fix_asked_cancel = False
        # 恢复拖放区提示文案（按当前模式）
        self._on_fix_mode_toggled(self._fix_mode_check.isChecked())


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Table")

    # ===== 先让「当前仓库」（工作区）生效 =====
    # 一个仓库 = 独立数据库 + 独立 hub/input/output + 独立密钥；AI 接口与检索方式是全局的。
    # 这里把登记表里的当前仓库写进环境变量，并改写已导入模块的路径/库名常量，
    # 保证后面的 init_db / 登录 / 扫描 / 图 / 对话都落在同一个仓库上。
    try:
        import workspace__infra as _ws

        if _ws.active() is None and not _ws.list_workspaces():
            print("[仓库] 还没有登记任何仓库：本次按老的默认路径运行（hub/ + .env 的库名）。\n"
                  "        要启用隔离，先跑：python workspace__infra.py register-legacy")
        else:
            _info = _ws.apply_active(persist=True)
            print(f"[仓库] 当前仓库：{_info['slug']}｜库 {_info['db']}｜hub {_info['hub']}")
    except Exception as _exc:
        print(f"[仓库] 生效失败（按默认路径继续）：{type(_exc).__name__}: {_exc}")

    # 启动性能监控（线程/CPU/GPU 采样 -> logs/perf_monitor.log），
    # 用于诊断"扫描慢是程序问题还是模型问题"；失败不影响主流程。
    try:
        from monitor__infra import start_performance_monitor

        start_performance_monitor()
    except Exception:
        pass

    # ===== 启动画面：数据库初始化 + OCR 模型预加载 =====
    # 在登录前先建好数据库表并提前加载 OCR 模型（首次加载较慢），
    # 用 splash 画面遮盖这段时间；拖入文件时模型已就绪、不再卡顿。
    # （模块：splash__ui.py —— SplashWindow 画面 + StartupThread 后台线程）
    from splash__ui import SplashWindow, StartupThread

    splash = SplashWindow()
    splash.show()

    db_result: list[tuple[bool, str]] = []  # 捕获数据库初始化结果
    startup = StartupThread()
    startup.stage_changed.connect(splash.set_stage)
    startup.init_db_result.connect(lambda ok, msg: db_result.append((ok, msg)))
    startup.finished.connect(splash.close)
    startup.start()

    # 本地事件循环：驱动 splash 正常重绘，直到启动线程结束
    loop = QEventLoop()
    startup.finished.connect(loop.quit)
    loop.exec()

    if db_result and not db_result[0][0]:
        QMessageBox.critical(None, "数据库初始化失败", f"错误详情:\n{db_result[0][1]}")
        return 1

    # ===== 登录 / 主窗口循环 =====
    # 支持「退出登录/注销账户/转让最高管理员」后回到登录对话框重新登录；
    # 关闭登录框或直接关闭主窗口（未触发退出登录）则结束程序。
    print("Qt event loop starting...")
    exit_to_login = {"flag": False}  # 主窗口是否以"退出登录"方式关闭

    while True:
        login_dialog = LoginRegisterDialog()
        if login_dialog.exec() != QDialog.DialogCode.Accepted:
            return 0  # 关闭登录框 = 退出程序

        window = MainWindow(current_user=login_dialog.user_info)
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        def _on_logout_requested() -> None:
            exit_to_login["flag"] = True

        window.logout_requested.connect(_on_logout_requested)
        window.show()
        window.raise_()
        window.activateWindow()

        win_loop = QEventLoop()
        window.destroyed.connect(win_loop.quit)
        win_loop.exec()  # 主窗口关闭（点 X / 退出登录）即结束本轮

        if exit_to_login["flag"]:
            exit_to_login["flag"] = False
            continue  # 回到登录对话框
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
