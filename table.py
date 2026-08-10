from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys
import traceback

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
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
    QVBoxLayout,
    QWidget,
)

# 引入数据库与 API 服务层
from database_serv import (
    api_ai_analyze_user_habits,
    api_alter_table_field,
    api_save_contract_from_ai,
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


@dataclass
class TaskRecord:
    file_path: Path
    output_paths: list[Path]
    status: str = "等待中"


class ScanWorker(QObject):
    log_message = Signal(str)
    task_started = Signal(str)
    task_finished = Signal(str, object)
    task_failed = Signal(str, str)
    finished = Signal()

    def __init__(self, file_path: Path) -> None:
        super().__init__()
        self._file_path = file_path

    @Slot()
    def run(self) -> None:
        try:
            from scanner_core import OUTPUT_DIR, process_file

            self.task_started.emit(str(self._file_path))
            self.log_message.emit(f"开始处理：{self._file_path}")

            output_paths = process_file(self._file_path, OUTPUT_DIR)

            self.task_finished.emit(str(self._file_path), output_paths)
            self.log_message.emit(f"完成处理：{self._file_path}")
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

        # 数据库扩展与 AI 控制区
        db_control_frame = QFrame()
        db_control_frame.setFrameShape(QFrame.Shape.StyledPanel)
        db_control_layout = QHBoxLayout(db_control_frame)

        self.btn_add_column = QPushButton("手动添加表字段")
        self.btn_add_column.clicked.connect(self._manual_add_column)
        self.btn_ai_habit = QPushButton("AI 分析人员习惯并建议字段")
        self.btn_ai_habit.clicked.connect(self._ai_habit_suggest)

        db_control_layout.addWidget(self.btn_add_column)
        db_control_layout.addWidget(self.btn_ai_habit)
        right_layout.addWidget(db_control_frame)

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
    def _manual_add_column(self) -> None:
        col_name, ok = QInputDialog.getText(self, "新增数据库字段", "请输入要扩充的英文字段名:")
        if ok and col_name.strip():
            success, msg = api_alter_table_field("ADD", col_name.strip())
            if success:
                QMessageBox.information(self, "成功", msg)
                self._append_log(f"数据库调整：{msg}")
            else:
                QMessageBox.critical(self, "失败", msg)

    def _ai_habit_suggest(self) -> None:
        suggestion = api_ai_analyze_user_habits()
        msg = f"【AI 建议理由】: {suggestion['reason']}\n拟新增字段: {suggestion['col_name']} ({suggestion['col_type']})\n\n是否同意修改数据库结构？"
        reply = QMessageBox.question(self, "AI 申请修改数据库结构", msg)
        if reply == QMessageBox.StandardButton.Yes:
            success, res_msg = api_alter_table_field(suggestion["action"], suggestion["col_name"], suggestion["col_type"])
            if success:
                QMessageBox.information(self, "应用成功", res_msg)
                self._append_log(f"AI 协作调整表结构成功：{res_msg}")
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

            record = TaskRecord(file_path=normalized, output_paths=[])
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
        from scanner_core import OUTPUT_DIR

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(OUTPUT_DIR)))

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
        worker = ScanWorker(file_path)
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

    def _on_task_finished(self, file_path_text: str, output_paths: object) -> None:
        file_path = Path(file_path_text)
        record = self._records.get(file_path)
        if record is None:
            return

        if isinstance(output_paths, list):
            record.output_paths = [Path(path) for path in output_paths]
        record.status = "已完成"
        self._update_item(file_path)
        self._append_log(f"输出：{', '.join(str(path) for path in record.output_paths)}")

        # 核心逻辑联动：AI 识别完成后自动解析并存入 PostgreSQL 数据库
        if record.output_paths:
            try:
                payload = json.loads(record.output_paths[0].read_text(encoding="utf-8"))
                ok, msg = api_save_contract_from_ai(payload)
                self._append_log(f"数据库同步状态: {msg}")
            except Exception as exc:
                self._append_log(f"数据自动解析入库失败: {exc}")

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

        if record.output_paths:
            try:
                payload = json.loads(record.output_paths[0].read_text(encoding="utf-8"))
                self.preview.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))
            except Exception as exc:
                self.preview.setPlainText(f"无法读取预览：{exc}")
        else:
            self.preview.setPlainText(f"{file_path}\n\n状态：{record.status}")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Table")
    
    from database_serv import init_db
    db_ok, db_msg = init_db()
    if not db_ok:
        QMessageBox.critical(None, "数据库初始化失败", f"错误详情:\n{db_msg}")
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