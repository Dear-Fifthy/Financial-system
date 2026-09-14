"""建图 / 补齐关系窗口：动态建图的状态查看与**存量补建**入口。

背景（为什么需要这个窗口）：
  · 动态建图是"入一份文档就建一次"（`hub_pipeline` 每份处理完后台调
    `edge_build.build_for_doc`），所以新文件不需要来这里点任何东西；
  · 但**之前已经扫过、当时还没接上建图的存量文件**必须补一次——本窗口就是那个入口
    （`build_pending`：按 doc_no 从旧到新补，`graph_build_state` 记录进度，可中断可续跑）；
  · 补建/重建走的都是 `EDGE_AI_*` 独立链路的 api 模式（未配 key 时只写请求文件，
    等人工/外部 agent 回填 `logs/graph/graph_edge_requests_incremental.jsonl`）。

界面：
  · 顶部：节点/边计数、已建图文档数、待建队列长度、pair 缓存条数、上次构建时间；
  · 中部：待建清单（doc_no / 分类 / 原因：从未建图 or 事实已变）；
  · 按钮：补齐存量（后台线程，可"停止"）｜重建选中文档｜刷新｜打开图日志目录。
"""
from __future__ import annotations
from ui.ui_kit__ui import fit_to_screen


import os
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
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

import graph.edge_build__graph_edges as eb

COLUMNS = ["编号", "文档", "分类", "为什么要建", "上次状态", "上次构建时间"]


class _BuildWorker(QObject):
    """后台补齐存量（网络 + 写库，不能卡 UI）。"""

    progress = Signal(str)
    done = Signal(object)

    def __init__(self, max_docs: int | None = None, force: bool = False) -> None:
        super().__init__()
        self._max_docs = max_docs
        self._force = force

    @Slot()
    def run(self) -> None:
        try:
            def _progress(i: int, total: int, doc_key: str, why: str) -> None:
                self.progress.emit(f"[{i}/{total}] {doc_key}（{why}）")

            res = eb.build_pending(max_docs=self._max_docs, force=self._force,
                                   progress=_progress)
            self.done.emit(res)
        except Exception as exc:                       # noqa: BLE001
            self.done.emit({"error": f"{type(exc).__name__}: {exc}"})


class GraphAdminDialog(QDialog):
    def __init__(self, parent=None, current_user: dict | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user or {}
        self.setWindowTitle("建图 / 补齐关系（图遍历 L3 的前置）")
        fit_to_screen(self, 980, 620)
        self._pending: list[dict] = []
        self._thread: QThread | None = None
        self._worker: _BuildWorker | None = None
        self._build_ui()
        self.reload()

    # ---------------- 界面 ----------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        head = QLabel(
            "**动态建图已启用**：每份文档扫描完成即后台建它的节点与关系（增量、幂等）。\n"
            "这个窗口用于**补齐存量**——之前扫过、当时还没建关系的文件，按旧→新顺序补建；"
            "也可对单个文档重建。\n"
            "重复内容不会重复建（指纹没变直接跳过）；文档变了会重跑并**更新**原有节点/边，"
            "不会产生第二套。"
        )
        head.setWordWrap(True)
        layout.addWidget(head)

        self.lbl_stats = QLabel("")
        self.lbl_stats.setStyleSheet("color: #444; font-weight: bold;")
        self.lbl_stats.setWordWrap(True)
        layout.addWidget(self.lbl_stats)

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
        layout.addWidget(self.tbl, 2)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("建图日志（补齐进度/结果）")
        self.log.setMaximumHeight(150)
        layout.addWidget(self.log, 1)

        opt = QHBoxLayout()
        self.chk_force = QCheckBox("强制重建（忽略指纹，重新判定并更新）")
        opt.addWidget(self.chk_force)
        self.chk_all = QCheckBox("不限数量（默认一次最多补 20 份）")
        opt.addWidget(self.chk_all)
        opt.addStretch(1)
        layout.addLayout(opt)

        row = QHBoxLayout()
        self.btn_pending = QPushButton("补齐存量（后台）")
        self.btn_pending.clicked.connect(self._build_pending)
        self.btn_selected = QPushButton("重建选中文档")
        self.btn_selected.clicked.connect(self._rebuild_selected)
        self.btn_stop = QPushButton("停止补齐")
        self.btn_stop.clicked.connect(self._stop_build)
        self.btn_stop.setEnabled(False)
        self.btn_ref = QPushButton("刷新")
        self.btn_ref.clicked.connect(self.reload)
        self.btn_open = QPushButton("打开图日志目录")
        self.btn_open.clicked.connect(self._open_logs)
        row.addWidget(self.btn_pending)
        row.addWidget(self.btn_selected)
        row.addWidget(self.btn_stop)
        row.addStretch(1)
        row.addWidget(self.btn_open)
        row.addWidget(self.btn_ref)
        layout.addLayout(row)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        close.accepted.connect(self.accept)
        layout.addWidget(close)

    # ---------------- 数据 ----------------
    def reload(self) -> None:
        try:
            state = eb.graph_state()
            self._pending = eb.pending_docs()
        except Exception as exc:
            QMessageBox.critical(self, "读取图状态失败", f"{type(exc).__name__}: {exc}")
            return
        e = state.get("edges", {})
        self.lbl_stats.setText(
            f"节点 {state.get('node_total', 0)}"
            f"（文档 {state['nodes'].get('doc', 0)} / 表块 {state['nodes'].get('table', 0)}"
            f" / 行 {state['nodes'].get('row', 0)} / 实体 {state['nodes'].get('entity', 0)}）｜"
            f"边 {e.get('total', 0)}"
            f"（已验证 {e.get('by_status', {}).get('validated', 0)}"
            f" / 假设 {e.get('by_status', {}).get('hypothesis', 0)}）｜"
            f"已建图文档 {state.get('built_docs', 0)}｜**待建 {state.get('pending', 0)}**｜"
            f"判定缓存 {state.get('pair_cache', 0)} 条｜上次构建 {state.get('last_built_at') or '—'}"
        )
        self.tbl.setRowCount(len(self._pending))
        for r, p in enumerate(self._pending):
            cells = [str(p.get("doc_no") or ""), p.get("doc_key", ""),
                     p.get("category") or "—", p.get("reason", ""),
                     p.get("status") or "（未建）", p.get("built_at") or "—"]
            for c, text in enumerate(cells):
                self.tbl.setItem(r, c, QTableWidgetItem(str(text)))

    def _selected_docs(self) -> list[str]:
        rows = sorted({i.row() for i in self.tbl.selectedIndexes()})
        return [self._pending[r]["doc_key"] for r in rows if 0 <= r < len(self._pending)]

    # ---------------- 操作 ----------------
    def _build_pending(self) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "正在建图", "已有补齐任务在跑，请等它结束或点停止。")
            return
        try:
            mode = eb.llm_mode()
        except Exception:
            mode = "?"
        if mode != "api":
            QMessageBox.information(
                self, "agent 模式",
                "当前未配置 EDGE_AI_API_KEY（edge 链路），补齐只会把请求写到\n"
                "logs/graph/graph_edge_requests_incremental.jsonl，等人工/外部 agent 回填。\n\n"
                "要自动判定请在 .env 配置 EDGE_AI_API_KEY。",
            )
        max_docs = None if self.chk_all.isChecked() else 20
        self.btn_pending.setEnabled(False)
        self.btn_stop.setEnabled(True)
        eb.clear_build_stop()
        self.log.appendPlainText(
            f"开始补齐（模式={mode}，最多 {max_docs or '不限'} 份，"
            f"强制={self.chk_force.isChecked()}）")
        worker = _BuildWorker(max_docs=max_docs, force=self.chk_force.isChecked())
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.log.appendPlainText)
        worker.done.connect(self._on_build_done)
        worker.done.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_thread_finished)
        self._thread, self._worker = thread, worker
        thread.start()

    def _stop_build(self) -> None:
        eb.request_stop_build("用户在窗口点击停止")
        self.log.appendPlainText("已请求停止：当前这份建完就停，剩下的下次继续")

    def _on_build_done(self, res: object) -> None:
        d = res if isinstance(res, dict) else {}
        if d.get("error"):
            self.log.appendPlainText(f"补齐失败：{d['error']}")
        else:
            self.log.appendPlainText(
                f"补齐结束：待建 {d.get('total', 0)}｜处理 {d.get('processed', 0)}"
                f"｜成功 {d.get('ok', 0)}｜失败 {d.get('failed', 0)}"
                f"｜{'已被用户停止' if d.get('stopped') else '已跑完'}")
            for item in (d.get("details") or [])[:30]:
                mark = "OK " if item.get("ok") else "ERR"
                self.log.appendPlainText(f"  {mark} {item.get('doc_key')}｜{item.get('msg')}")
        self.reload()

    def _on_thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self.btn_pending.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def _rebuild_selected(self) -> None:
        docs = self._selected_docs()
        if not docs:
            QMessageBox.information(self, "请选择", "请先在列表里选择要重建的文档（可多选）。")
            return
        if QMessageBox.question(
            self, "确认重建",
            f"重新建图中选 {len(docs)} 份文档的节点与关系？\n"
            f"（节点/边为 UPSERT 更新，不会产生重复行）",
        ) != QMessageBox.StandardButton.Yes:
            return
        ok = fail = 0
        for doc_key in docs:
            try:
                res = eb.build_for_doc(doc_key, force=True)
                self.log.appendPlainText(f"重建 {doc_key}：{res.get('msg')}")
                ok += 1
            except Exception as exc:                   # noqa: BLE001
                self.log.appendPlainText(f"重建失败 {doc_key}：{type(exc).__name__}: {exc}")
                fail += 1
        self.log.appendPlainText(f"重建完成：成功 {ok}｜失败 {fail}")
        self.reload()

    def _open_logs(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        d = Path(eb.__file__).resolve().parents[1] / "logs" / "graph"
        d.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))
        _ = os

    def closeEvent(self, event) -> None:              # noqa: N802
        if self._thread is not None:
            eb.request_stop_build("窗口关闭")
            self._thread.quit()
            self._thread.wait(3000)
            self._thread = None
        super().closeEvent(event)
