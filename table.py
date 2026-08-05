from __future__ import annotations

"""桌面端第一版工作台。

这一层只负责交互，不直接承载 OCR 细节：
- 左侧接收文件/文件夹拖入，并形成任务队列。
- 中间/右侧显示识别后的 JSON 预览。
- 运行扫描任务时通过后台线程避免界面卡死。

真正的扫描逻辑放在 scanner_core.py，table.py 只调用公共接口。
"""

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

from scanner_core import OUTPUT_DIR, expand_paths, process_file


# 让窗口对象在函数返回后仍然被引用，避免在某些启动路径里过早释放。
_APP_INSTANCE: QApplication | None = None
_MAIN_WINDOW: "MainWindow" | None = None


@dataclass
class TaskRecord:
    """单个输入文件对应的一条任务记录。"""

    file_path: Path
    output_paths: list[Path]
    status: str = "等待中"


class ScanWorker(QObject):
    """后台扫描工作对象。

    这个类只做一件事：在线程里调用 scanner_core.process_file。
    这样 GUI 线程可以继续响应拖拽、点击和预览刷新。
    """

    log_message = Signal(str)
    task_started = Signal(str)
    task_finished = Signal(str, object)
    task_failed = Signal(str, str)
    finished = Signal()

    def __init__(self, file_path: Path, output_root: Path) -> None:
        super().__init__()
        self._file_path = file_path
        self._output_root = output_root

    @Slot()
    def run(self) -> None:
        # 这里不要做 UI 操作，只负责执行耗时扫描并通过信号把结果发回主线程。
        try:
            self.task_started.emit(str(self._file_path))
            self.log_message.emit(f"开始处理：{self._file_path}")
            output_paths = process_file(self._file_path, self._output_root)
            self.task_finished.emit(str(self._file_path), output_paths)
            self.log_message.emit(f"完成处理：{self._file_path}")
        except Exception as exc:  # noqa: BLE001
            self.task_failed.emit(str(self._file_path), f"{type(exc).__name__}: {exc}")
            self.log_message.emit(f"处理失败：{self._file_path} | {type(exc).__name__}: {exc}")
        finally:
            self.finished.emit()


class MainWindow(QMainWindow):
    """应用主窗口。

    结构非常简单：
    - 顶部提示区：告诉用户可以直接拖文件进来。
    - 左侧任务区：显示待处理、处理中、完成和失败状态。
    - 右侧预览区：查看 JSON 内容和运行日志。
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Table 扫描工作台")
        self.resize(1280, 820)
        self.setAcceptDrops(True)

        # _pending_paths 保存等待执行的文件；_records 保存 UI 与输出路径的状态。
        self._pending_paths: list[Path] = []
        self._records: dict[Path, TaskRecord] = {}

        # 当前一次只跑一个后台线程，先保证流程稳定和易调试，后面再考虑并发。
        self._current_thread: QThread | None = None
        self._current_worker: ScanWorker | None = None

        self._build_ui()
        self._append_log(f"输出目录：{OUTPUT_DIR}")

    def _build_ui(self) -> None:
        # 中央布局：上方提示，中间左右分栏，下方日志。
        central = QWidget(self)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(16, 16, 16, 16)
        root_layout.setSpacing(12)

        # 提示区：这是最直接的“拖拽入口”。
        self.drop_hint = QLabel("把 PDF、图片或文件夹直接拖到窗口里，系统会自动进入输入队列。")
        self.drop_hint.setFrameShape(QFrame.Shape.StyledPanel)
        self.drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_hint.setMinimumHeight(56)
        root_layout.addWidget(self.drop_hint)

        # 左右分栏：左边看任务，右边看 JSON 和日志。
        splitter = QSplitter(Qt.Orientation.Horizontal)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("任务列表"))

        # 任务列表既能反映状态，也能作为点击预览的入口。
        self.task_list = QListWidget()
        self.task_list.currentItemChanged.connect(self._show_selected_preview)
        left_layout.addWidget(self.task_list, 1)

        # 常用操作按钮：手动选文件、打开输出目录。
        button_row = QHBoxLayout()
        self.open_output_button = QPushButton("打开输出目录")
        self.open_output_button.clicked.connect(self._open_output_directory)
        self.add_files_button = QPushButton("手动添加文件")
        self.add_files_button.clicked.connect(self._pick_files)
        button_row.addWidget(self.add_files_button)
        button_row.addWidget(self.open_output_button)
        left_layout.addLayout(button_row)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # 预览区直接展示页面 JSON，方便后面做人为校对和 AI 处理。
        right_layout.addWidget(QLabel("JSON 预览"))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("选择左侧任务后，这里显示输出 JSON。")
        right_layout.addWidget(self.preview, 2)

        # 日志区显示队列、开始、完成、失败等过程信息。
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
        # 统一日志入口，后面如果要接文件日志/远端日志，只改这里即可。
        self.log_view.appendPlainText(message)

    def _add_paths(self, raw_paths: list[Path]) -> None:
        # 把拖进来的路径展开成“可识别输入”，文件夹会递归扫描。
        paths = expand_paths(raw_paths)
        if not paths:
            QMessageBox.information(self, "没有可处理的文件", "只支持 PDF、图片，或者包含这些文件的文件夹。")
            return

        new_items = 0
        for file_path in paths:
            normalized = file_path.resolve()
            if normalized in self._records:
                continue

            # 每个新文件都生成一条任务记录，并同步放入列表。
            record = TaskRecord(file_path=normalized, output_paths=[])
            self._records[normalized] = record
            item = QListWidgetItem(f"{normalized.name}  [{record.status}]")
            item.setData(Qt.ItemDataRole.UserRole, str(normalized))
            self.task_list.addItem(item)
            new_items += 1
            self._pending_paths.append(normalized)
            self._append_log(f"加入队列：{normalized}")

        if new_items:
            self._start_next_task()

    def _pick_files(self) -> None:
        # 手动选择时直接允许多选 PDF/图片。
        file_names, _ = QFileDialog.getOpenFileNames(
            self,
            "选择 PDF 或图片",
            str(Path.cwd()),
            "Documents (*.pdf *.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp)",
        )
        if file_names:
            self._add_paths([Path(name) for name in file_names])

    def _open_output_directory(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(OUTPUT_DIR)))

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        # 只要是文件 URL，就允许拖入。
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        # 把拖入的内容转换成本地路径，再交给统一的添加逻辑。
        dropped_paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        self._add_paths(dropped_paths)
        event.acceptProposedAction()

    def _start_next_task(self) -> None:
        # 当前只有一个后台任务在跑，任务完成后再启动下一个，先保证稳定。
        if self._current_thread is not None or not self._pending_paths:
            return

        file_path = self._pending_paths.pop(0)
        record = self._records[file_path]
        record.status = "处理中"
        self._update_item(file_path)

        thread = QThread(self)
        worker = ScanWorker(file_path, OUTPUT_DIR)
        worker.moveToThread(thread)

        # 所有耗时动作都在 worker 线程里，UI 线程只接收信号刷新状态。
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
        # 任务真正开始时，追加一条日志，方便定位卡在哪一步。
        self._append_log(f"开始：{file_path_text}")

    def _on_task_finished(self, file_path_text: str, output_paths: object) -> None:
        # 成功后更新状态、记录输出路径，并自动刷新第一页预览。
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
        # 失败时把错误直接写进列表状态，方便人工回看。
        file_path = Path(file_path_text)
        record = self._records.get(file_path)
        if record is None:
            return

        record.status = f"失败：{error_text}"
        self._update_item(file_path)

    def _on_worker_thread_finished(self) -> None:
        # 当前任务线程退出后，清理引用并启动队列中的下一个任务。
        self._current_thread = None
        self._current_worker = None
        self._start_next_task()

    def _update_item(self, file_path: Path) -> None:
        # 这里通过 UserRole 找到对应列表项，确保状态文本能同步刷新。
        for index in range(self.task_list.count()):
            item = self.task_list.item(index)
            stored = item.data(Qt.ItemDataRole.UserRole)
            if stored == str(file_path):
                record = self._records[file_path]
                item.setText(f"{file_path.name}  [{record.status}]")
                break

    def _show_selected_preview(self, current: QListWidgetItem | None, previous: QListWidgetItem | None) -> None:
        # 点击左侧任务后，右侧预览区切换到对应 JSON。
        if current is None:
            return
        stored = current.data(Qt.ItemDataRole.UserRole)
        if not stored:
            return
        self._load_preview_for_path(Path(stored))

    def _load_preview_for_path(self, file_path: Path) -> None:
        # 预览逻辑只负责读 JSON，不参与扫描和写盘。
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
    # 入口只做 QApplication 创建和窗口展示，方便被 pythonw 或 bat 启动。
    global _APP_INSTANCE, _MAIN_WINDOW

    try:
        app = QApplication(sys.argv)
        app.setApplicationName("Table")
        app.setQuitOnLastWindowClosed(False)

        window = MainWindow()
        window.show()
        window.showNormal()
        window.raise_()
        window.activateWindow()

        _APP_INSTANCE = app
        _MAIN_WINDOW = window

        print("Qt event loop starting...")
        return app.exec()
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        raise RuntimeError(f"table.py 启动失败: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
