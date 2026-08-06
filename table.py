from __future__ import annotations

"""桌面端第一版工作台（优化版）。"""

from dataclasses import dataclass
from pathlib import Path
import json
import sys
import traceback

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSplitter,
    QVBoxLayout,
    QWidget,
)


@dataclass
class TaskRecord:
    """单个输入文件对应的一条任务记录。"""
    file_path: Path
    output_paths: list[Path]
    status: str = "等待中"


class ScanWorker(QObject):
    """后台扫描工作对象。"""

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
            # 延迟导入重型核心库：避免启动 GUI 时因为 scanner_core 报错或加载太慢导致窗口打不开
            from scanner_core import OUTPUT_DIR, process_file

            self.task_started.emit(str(self._file_path))
            self.log_message.emit(f"开始处理：{self._file_path}")
            
            output_paths = process_file(self._file_path, OUTPUT_DIR)
            
            self.task_finished.emit(str(self._file_path), output_paths)
            self.log_message.emit(f"完成处理：{self._file_path}")
        except Exception as exc:  # noqa: BLE001
            self.task_failed.emit(str(self._file_path), f"{type(exc).__name__}: {exc}")
            self.log_message.emit(f"处理失败：{self._file_path} | {type(exc).__name__}: {exc}")
        finally:
            self.finished.emit()


class MainWindow(QMainWindow):
    """应用主窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Table 扫描工作台")
        self.resize(1280, 820)
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
        self.drop_hint.setMinimumHeight(56)
        root_layout.addWidget(self.drop_hint)

        splitter = QSplitter(Qt.Orientation.Horizontal)

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

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)

        right_layout.addWidget(QLabel("JSON 预览"))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("选择左侧任务后，这里显示输出 JSON。")
        right_layout.addWidget(self.preview, 2)

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

    def _append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    def _add_paths(self, raw_paths: list[Path]) -> None:
        # 在触发添加逻辑时才导入 expand_paths
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
        
        # 正确销毁线程与 Worker
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
            except Exception as exc:  # noqa: BLE001
                self.preview.setPlainText(f"无法读取预览：{exc}")
        else:
            self.preview.setPlainText(f"{file_path}\n\n状态：{record.status}")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Table")

    window = MainWindow()
    window.show()

    # 保持窗口最前的常规激活方式
    window.raise_()
    window.activateWindow()

    print("Qt event loop starting...")
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()