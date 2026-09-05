"""启动画面模块：OCR 模型预加载期间的加载窗口（蓝色横线进度条）。

文件位置：项目根目录新增 splash.py（单个文件即可，暂不需要独立文件夹）。
职责单一：只负责"程序启动期"的 UI 与后台任务——
  - SplashWindow    ：无边框置顶窗口，蓝色不确定进度条 + 阶段文字
  - StartupThread   ：后台线程，按阶段执行【数据库初始化 -> GPU/显存检测 -> OCR 模型预加载】
table.py 的 main() 在登录弹窗前使用二者，把"首次加载模型"的耗时
从「拖入文件那一刻」转移到启动阶段，拖拽不再卡顿。
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import QLabel, QProgressBar, QVBoxLayout, QWidget


class SplashWindow(QWidget):
    """启动画面：无边框、置顶、深色底，蓝色横线进度条 + 阶段文字。

    用法：
        splash = SplashWindow()
        splash.show()
        splash.set_stage("正在加载 OCR 模型…")
        ...（工作完成后）
        splash.close()
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setFixedSize(420, 160)
        self.setStyleSheet(
            "SplashWindow { background: #1e2a3a; border-radius: 8px; }"
            "QLabel { color: #d0d8e8; font-size: 14px; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel("正在启动扫描工作台…")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        # 蓝色横线进度条：不确定模式（蓝色碎片来回滚动）代替真实进度，
        # 模型加载没有可量化的进度事件，这样最简单可靠
        self._bar = QProgressBar()
        self._bar.setRange(0, 0)  # 0-0 = 不确定进度
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(10)
        self._bar.setStyleSheet(
            "QProgressBar { background: #2c3e50; border: none; border-radius: 5px; }"
            "QProgressBar::chunk { background: #3b82f6; border-radius: 5px; }"
        )
        layout.addWidget(self._bar)

        self._stage_label = QLabel("准备中…")
        self._stage_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._stage_label)

    def set_stage(self, text: str) -> None:
        """更新阶段文字（跨线程安全：信号槽调用，在 GUI 线程执行）。"""
        self._stage_label.setText(text)


class StartupThread(QThread):
    """启动线程：数据库初始化 + OCR 模型预加载（分阶段报告进度）。

    阶段顺序：
      1. 初始化数据库（init_db：建库/建表/权限种子，必要时清理旧列）
      2. 检测 GPU / 显存（resolve_ocr_device，决定 gpu/cpu）
      3. 加载 OCR 模型（get_ocr 首次初始化，通常最耗时——正是启动画面要遮盖的时间）

    信号：
      stage_changed(str)  —— 阶段文字
      init_db_result(bool, str) —— 数据库初始化结果
      warmup_result(dict) —— OCR 环境探测结果（含 device/reason）
    """
    stage_changed = Signal(str)
    init_db_result = Signal(bool, str)
    warmup_result = Signal(dict)

    def run(self) -> None:
        # 1. 数据库初始化（结果必发；失败由 main() 决定是否中止）
        self.stage_changed.emit("初始化数据库…")
        from database_serv import init_db

        db_ok, db_msg = init_db()
        self.init_db_result.emit(db_ok, db_msg)

        # 2-3. OCR 预加载：失败不阻断启动，模型会按需惰性加载 + 熔断保护
        # 注意：warmup_ocr 内部已做设备探测+模型加载（get_ocr 解析一次并记录），
        # 这里不再单独调用 resolve_ocr_device()，避免启动路径重复探测/重复打印。
        try:
            from scanner_core import warmup_ocr

            self.stage_changed.emit("检测 GPU / 显存并加载 OCR 模型…")
            result = warmup_ocr()
            self.warmup_result.emit(result)
        except Exception as exc:
            self.stage_changed.emit(f"OCR 预加载失败：{exc}（稍后按需加载）")
