from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import sys
import traceback

from PySide6.QtCore import QEventLoop, QObject, QThread, Qt, Signal, Slot, QUrl
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
from database_serv import (
    ALLOWED_COLUMN_TYPES,
    ALLOWED_TABLES,
    api_ai_analyze_user_habits,
    api_alter_table_field,
    api_get_table_fields,
    api_list_contracts,
    api_rename_table_field,
    api_save_contract_from_ai,
    api_set_contract_status,
    authenticate_user,
    register_user,
)


# ===== 新增：登录/注册弹窗 =====
class LoginRegisterDialog(QDialog):
    """登录与注册整合窗口。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("财务系统 - 登录/注册")
        self.resize(320, 280)
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

    展示指定业务表的全部字段（名称+类型），支持：
      - 新增字段（名称 + 白名单类型）
      - 重命名选中字段
      - 删除选中字段
    所有变更先收集到 self._ops，点「确定」后按顺序提交给 database_serv
    （权限：field:write，仅 financial_role / admin）。

    业务扩展说明：以后新增其它业务表（发票/物流等）时，只需把表名登记进
    database_serv.ALLOWED_TABLES，本对话框即可复用，互不影响。
    ⚠️ 建议只调整用户扩展的自定义字段；内置字段（合同编号/甲方等）重命名
    会导致入库 SQL 失效（见 api_rename_table_field 注释）。
    """

    def __init__(self, parent, current_user: dict, table_name: str) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self.table_name = table_name
        self._fields: list[dict] = []   # 当前字段工作副本（随操作实时变化）
        self._ops: list[tuple] = []     # 待提交操作列表：(ACTION, *args)
        self.setWindowTitle(f"调整字段 - {table_name}")
        self.resize(620, 460)
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
        self.btn_delete = QPushButton("删除选中")
        self.btn_delete.clicked.connect(self._delete_selected)
        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_rename)
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
        """按工作副本重建字段列表，并刷新变更预览。"""
        self.field_list.clear()
        for f in self._fields:
            self.field_list.addItem(f"{f['column_name']}  ({f['data_type']})")
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
            elif op[0] == "DROP":
                ok, msg = api_alter_table_field(self.current_user, "DROP", self.table_name, op[1])
            else:
                continue
            results.append(("✅ " if ok else "❌ ") + msg)
        QMessageBox.information(self, "调整结果", "\n".join(results))
        self.accept()


# ===== 台账状态管理对话框（已收款 / 已开票 手动切换，初始默认未）=====
class LedgerStatusDialog(QDialog):
    """台账状态管理：列出全部合同，勾选/取消"已收款、已开票"，保存后落库。

    权限：ledger:status（financial_role / admin，在 database_serv 侧校验）。
    状态初始默认"未"（数据库 DEFAULT FALSE），人工在此手动切换。
    """

    def __init__(self, parent, current_user: dict, table_name: str = "contract_projects") -> None:
        super().__init__(parent)
        self.current_user = current_user
        self.table_name = table_name
        self._rows: list[dict] = []
        self.setWindowTitle(f"台账状态管理 - {table_name}")
        self.resize(720, 480)
        self._build_ui()
        self._load()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("勾选 = 已；取消 = 未（默认未）。修改后点「保存状态」。"))

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["合同编号", "项目", "合同金额", "已收款", "已开票"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_save = QPushButton("保存状态")
        self.btn_save.clicked.connect(self._save)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_close)
        layout.addLayout(btn_row)

    def _load(self) -> None:
        ok, res = api_list_contracts(self.current_user, self.table_name)
        if not ok:
            QMessageBox.critical(self, "读取台账失败", str(res))
            return
        self._rows = list(res)
        self.table.setRowCount(len(self._rows))
        for i, row in enumerate(self._rows):
            self.table.setItem(i, 0, QTableWidgetItem(str(row.get("合同编号", ""))))
            self.table.setItem(i, 1, QTableWidgetItem(str(row.get("项目", "") or "")))
            self.table.setItem(i, 2, QTableWidgetItem(str(row.get("合同金额", ""))))

            paid = QCheckBox()
            paid.setChecked(bool(row.get("是否已收款", False)))
            paid.setText("已收款")
            self.table.setCellWidget(i, 3, paid)

            invoiced = QCheckBox()
            invoiced.setChecked(bool(row.get("是否已开票", False)))
            invoiced.setText("已开票")
            self.table.setCellWidget(i, 4, invoiced)

    def _save(self) -> None:
        """对勾选状态与数据库不一致的行逐个提交 api_set_contract_status。"""
        results: list[str] = []
        for i, row in enumerate(self._rows):
            code = row.get("合同编号")
            if not code:
                continue
            paid_box: QCheckBox = self.table.cellWidget(i, 3)
            invoiced_box: QCheckBox = self.table.cellWidget(i, 4)
            new_paid = paid_box.isChecked()
            new_invoiced = invoiced_box.isChecked()
            if new_paid == bool(row.get("是否已收款", False)) and new_invoiced == bool(row.get("是否已开票", False)):
                continue  # 未变更
            ok, msg = api_set_contract_status(
                self.current_user,
                code,
                paid=new_paid if new_paid != bool(row.get("是否已收款", False)) else None,
                invoiced=new_invoiced if new_invoiced != bool(row.get("是否已开票", False)) else None,
                table_name=self.table_name,
            )
            results.append(("✅ " if ok else "❌ ") + f"{code}: {msg}")
        if not results:
            QMessageBox.information(self, "提示", "没有需要保存的变更。")
            return
        QMessageBox.information(self, "保存结果", "\n".join(results) if results else "全部成功")
        self._load()


@dataclass
class TaskRecord:
    file_path: Path
    category: str = ""
    hub_json_path: Path | None = None
    cache_json_paths: list[Path] = field(default_factory=list)
    status: str = "等待中"


class ScanWorker(QObject):
    log_message = Signal(str)
    task_started = Signal(str)
    task_finished = Signal(str, object)
    task_failed = Signal(str, str)
    finished = Signal()

    def __init__(self, file_path: Path, current_user: dict) -> None:
        super().__init__()
        self._file_path = file_path
        self._current_user = current_user

    @Slot()
    def run(self) -> None:
        try:
            from hub_pipeline import process_file_to_hub

            self.task_started.emit(str(self._file_path))
            self.log_message.emit(f"开始处理：{self._file_path}")

            result = process_file_to_hub(self._file_path, current_user=self._current_user)

            self.task_finished.emit(str(self._file_path), result)
            self.log_message.emit(
                f"完成处理：{self._file_path} | 分类：{result['category']}"
            )
        except Exception as exc:
            self.task_failed.emit(str(self._file_path), f"{type(exc).__name__}: {exc}")
            self.log_message.emit(f"处理失败：{self._file_path} | {type(exc).__name__}: {exc}")
        finally:
            self.finished.emit()


class MainWindow(QMainWindow):

    def __init__(self, current_user: dict) -> None:
        super().__init__()
        self.current_user = current_user
        self.setWindowTitle(f"Table 扫描工作台 - [当前用户: {current_user['username']}]")
        self.resize(1380, 850)
        self.setAcceptDrops(True)

        self._pending_paths: list[Path] = []
        self._records: dict[Path, TaskRecord] = {}
        self._current_thread: QThread | None = None
        self._current_worker: ScanWorker | None = None

        self._build_ui()

    def _build_ui(self) -> None:
        central = QWidget(self)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(16, 16, 16, 16)
        root_layout.setSpacing(12)

        self.drop_hint = QLabel("把 PDF、图片或文件夹直接拖到窗口里，系统会自动进入输入队列。")
        self.drop_hint.setFrameShape(QFrame.Shape.StyledPanel)
        self.drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_hint.setMinimumHeight(48)
        root_layout.addWidget(self.drop_hint)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左侧面板
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("任务列表"))

        self.task_list = QListWidget()
        self.task_list.currentItemChanged.connect(self._show_selected_preview)
        left_layout.addWidget(self.task_list, 1)

        button_row = QHBoxLayout()
        self.add_files_button = QPushButton("手动添加文件")
        self.add_files_button.clicked.connect(self._pick_files)
        self.open_output_button = QPushButton("打开输出目录")
        self.open_output_button.clicked.connect(self._open_output_directory)

        button_row.addWidget(self.add_files_button)
        button_row.addWidget(self.open_output_button)
        left_layout.addLayout(button_row)

        # 右侧面板
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)

        right_layout.addWidget(QLabel("JSON 预览"))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("选择左侧任务后，这里显示输出 JSON。")
        right_layout.addWidget(self.preview, 2)

        # 数据库字段管理区：读取与调整权限分离（financial_role 拥有最高权限）
        db_control_frame = QFrame()
        db_control_frame.setFrameShape(QFrame.Shape.StyledPanel)
        db_control_layout = QHBoxLayout(db_control_frame)

        # 业务表选择：目前只有合同台账；以后新增业务时在
        # database_serv.ALLOWED_TABLES 登记即可自动出现在这里
        db_control_layout.addWidget(QLabel("业务表:"))
        self.table_combo = QComboBox()
        for table_name, display_name in ALLOWED_TABLES.items():
            self.table_combo.addItem(display_name, table_name)
        db_control_layout.addWidget(self.table_combo)

        self.btn_read_fields = QPushButton("读取现有字段")
        self.btn_read_fields.clicked.connect(self._read_fields)
        self.btn_adjust_fields = QPushButton("调整字段")
        self.btn_adjust_fields.clicked.connect(self._adjust_fields)
        self.btn_ledger_status = QPushButton("台账状态")
        self.btn_ledger_status.clicked.connect(self._ledger_status)
        self.btn_ai_habit = QPushButton("AI 分析人员习惯并建议字段")
        self.btn_ai_habit.clicked.connect(self._ai_habit_suggest)

        db_control_layout.addWidget(self.btn_read_fields)
        db_control_layout.addWidget(self.btn_adjust_fields)
        db_control_layout.addWidget(self.btn_ledger_status)
        db_control_layout.addWidget(self.btn_ai_habit)
        right_layout.addWidget(db_control_frame)

        # 字段列表显示区：只读，仅显示字段名（+类型），不显示任何数据内容
        right_layout.addWidget(QLabel("现有字段（仅字段名，不显示内容）"))
        self.fields_view = QPlainTextEdit()
        self.fields_view.setReadOnly(True)
        self.fields_view.setMaximumHeight(120)
        self.fields_view.setPlaceholderText("点击「读取现有字段」查看当前台账字段。")
        right_layout.addWidget(self.fields_view)

        right_layout.addWidget(QLabel("运行日志"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        right_layout.addWidget(self.log_view, 1)

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        root_layout.addWidget(splitter, 1)

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
            f"拟在表 {self._current_business_table()} 新增字段: "
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
        try:
            from scanner_core import expand_paths

            paths = expand_paths(raw_paths)
        except Exception as exc:
            QMessageBox.critical(self, "错误", f"展开文件路径失败: {exc}")
            return

        if not paths:
            QMessageBox.information(self, "没有可处理的文件", "只支持 PDF、图片，或者包含这些文件的文件夹。")
            return

        new_items = 0
        for file_path in paths:
            normalized = file_path.resolve()
            if normalized in self._records:
                continue

            record = TaskRecord(file_path=normalized)
            self._records[normalized] = record

            item = QListWidgetItem(f"{normalized.name}  [{record.status}]")
            item.setData(Qt.ItemDataRole.UserRole, str(normalized))

            self.task_list.addItem(item)
            self._pending_paths.append(normalized)
            self._append_log(f"加入队列：{normalized}")
            new_items += 1

        if new_items:
            self._start_next_task()

    def _pick_files(self) -> None:
        file_names, _ = QFileDialog.getOpenFileNames(
            self,
            "选择 PDF 或图片",
            str(Path.cwd()),
            "Documents (*.pdf *.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp)",
        )
        if file_names:
            self._add_paths([Path(name) for name in file_names])

    def _open_output_directory(self) -> None:
        from hub_pipeline import HUB_DIR

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
        if self._current_thread is not None or not self._pending_paths:
            return

        file_path = self._pending_paths.pop(0)
        record = self._records[file_path]
        record.status = "处理中"
        self._update_item(file_path)

        thread = QThread(self)
        worker = ScanWorker(file_path, current_user=self.current_user)
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
        thread.start()

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

        record.status = f"已完成 [{record.category}]" if record.category else "已完成"
        self._update_item(file_path)
        self._append_log(f"分类：{record.category} | Hub 输出：{record.hub_json_path}")

        # 核心逻辑联动：只有分类为"合同"、且已经提取出台账字段时，才自动写入
        # contract_projects 台账。合同编号/合同金额目前还是占位值，等第5步
        # 接上外部 AI API 解析之后，应该用解析结果覆盖这行（同一个合同编号
        # ON CONFLICT 会自动更新，不会产生重复行）。
        contract_fields = result.get("contract_fields") if isinstance(result, dict) else None
        if contract_fields:
            try:
                ok, msg = api_save_contract_from_ai(contract_fields)
                self._append_log(f"合同台账同步状态: {msg}")
            except Exception as exc:
                self._append_log(f"合同台账写入失败: {exc}")

        # AI 台账填写：若文件标题识别不到合同编号（need_code），弹窗请用户输入后补存
        ai_report = result.get("ai_report") if isinstance(result, dict) else None
        if isinstance(ai_report, dict):
            ledger = ai_report.get("ledger") or {}
            if ledger.get("need_code") and ledger.get("fields"):
                code, ok_input = QInputDialog.getText(
                    self, "合同编号", "文件标题中未识别到合同编号，请人工输入："
                )
                if ok_input and code.strip():
                    fields = dict(ledger["fields"])
                    fields["contract_code"] = code.strip()
                    try:
                        ok_save, msg_save = api_save_contract_from_ai(fields)
                        self._append_log(f"人工补录合同编号（{code.strip()}）: {msg_save}")
                    except Exception as exc:
                        self._append_log(f"人工补录合同编号失败: {exc}")
                else:
                    self._append_log("未输入合同编号，本份合同未写入台账（可稍后处理）")

        self._load_preview_for_path(file_path)

    def _on_task_failed(self, file_path_text: str, error_text: str) -> None:
        file_path = Path(file_path_text)
        record = self._records.get(file_path)
        if record is None:
            return

        record.status = f"失败：{error_text}"
        self._update_item(file_path)

    def _on_worker_thread_finished(self) -> None:
        self._current_thread = None
        self._current_worker = None
        self._start_next_task()

    def _update_item(self, file_path: Path) -> None:
        for index in range(self.task_list.count()):
            item = self.task_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == str(file_path):
                record = self._records[file_path]
                item.setText(f"{file_path.name}  [{record.status}]")
                break

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


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Table")

    # 启动性能监控（线程/CPU/GPU 采样 -> logs/perf_monitor.log），
    # 用于诊断"扫描慢是程序问题还是模型问题"；失败不影响主流程。
    try:
        from monitor import start_performance_monitor

        start_performance_monitor()
    except Exception:
        pass

    # ===== 启动画面：数据库初始化 + OCR 模型预加载 =====
    # 在登录前先建好数据库表并提前加载 OCR 模型（首次加载较慢），
    # 用 splash 画面遮盖这段时间；拖入文件时模型已就绪、不再卡顿。
    # （模块：splash.py —— SplashWindow 画面 + StartupThread 后台线程）
    from splash import SplashWindow, StartupThread

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

    # 弹出登录拦截
    login_dialog = LoginRegisterDialog()
    if login_dialog.exec() != QDialog.DialogCode.Accepted:
        return 0

    window = MainWindow(current_user=login_dialog.user_info)
    window.show()

    window.raise_()
    window.activateWindow()

    print("Qt event loop starting...")
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()