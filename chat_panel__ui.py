"""AI 对话面板：常驻 Dock 窗口（气泡式双方对话 + 会话管理 + 多版本对比入口）。

依赖：chat_store（存储/回收）、chat_engine（版本/范围/记忆注册表 + decode 边界）。
架构约定：
    UI（本文件）→ chat_engine__ai.generate →（后续接入）RAG / 图节点检索
    ——引擎只返回"原始文本"，展示前在 chat_engine__ai.decode_answer 净化（需求点 1）；
    ——每条消息经 chat_store__ui.append_message 落 logs/chat/<用户>/<会话id>/messages.jsonl
      （含 prompt 版本/记忆方式/范围 meta，支撑"多版本并比较"）；
    ——一个会话一个文件夹；删除=移动到回收区（可恢复/可彻底删除）。

窗口只依赖 chat_engine 的公开接口，检索层换实现不影响本文件。
"""
from __future__ import annotations
from ui_kit__ui import fit_to_screen


import html as _html
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import chat_engine__ai as engine
import chat_store__ui as store
import retrieval_config__ai

# 气泡配色（浅色）
_ROLE_STYLE = {
    "user": ("#e8f1fb", "#1a1a1a", "right"),
    "ai": ("#f0f4f1", "#1a1a1a", "left"),
    "error": ("#fdecea", "#8a1f11", "left"),
}


class _ChatInputEdit(QTextEdit):
    """输入框：Enter 发送、Shift+Enter 换行。"""

    send_requested = Signal()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.send_requested.emit()
            return
        super().keyPressEvent(event)


class _ChatWorker(QObject):
    """后台生成线程（避免网络调用卡 UI）。engine.generate 在本线程执行。"""

    done = Signal(object, object, object)  # (decoded_text, meta, error)

    def __init__(self, user_text: str, username: str, scope_label: str,
                 prompt_key: str, memory_key: str, history: list[dict]) -> None:
        super().__init__()
        self._args = (user_text, username, scope_label, prompt_key, memory_key, history)

    @Slot()
    def run(self) -> None:
        user_text, username, scope_label, prompt_key, memory_key, history = self._args
        text, meta, err = engine.generate(
            user_text,
            username=username,
            scope_label=scope_label,
            prompt_key=prompt_key,
            memory_key=memory_key,
            history=history,
        )
        self.done.emit(text, meta, err)


class _OutboundGuardDialog(QDialog):
    """外发守卫弹窗：用户输入中的实体有多个匹配/子类时，本地选择真实所指。

    只展示**完整名称**（非脱敏码），每处一个下拉：默认选中最长（最完整）名称，
    也可选择"不脱敏，原样发送"（用户明确放行时保留原文）。
    返回 selections()：{组索引: 选定 code 或 None}；取消返回 None。
    """

    def __init__(self, groups: list[dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._groups = groups
        self._combos: list = []
        self.setWindowTitle("发送前确认 - 实体多义")
        self.resize(560, 0)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("检测到以下表述对应多个本地实体（公司/人员或子类），"
                                "请选择实际所指（仅本机显示，完整名称不会发给 AI）："))
        for i, group in enumerate(self._groups, start=1):
            row = QHBoxLayout()
            row.addWidget(QLabel(f"第 {i} 处："))
            combo = QComboBox()
            combo.addItem("（不脱敏，原样发送）", None)
            for cand in group.get("candidates", []):
                combo.addItem(cand["real"], cand["code"])
            if combo.count() > 1:
                combo.setCurrentIndex(1)  # 默认最长/最完整名称
            row.addWidget(combo, 1)
            layout.addLayout(row)
            self._combos.append(combo)
        btns = QHBoxLayout()
        self._btn_ok = QPushButton("发送（所选实体将替换为脱敏码）")
        self._btn_ok.clicked.connect(self.accept)
        self._btn_cancel = QPushButton("取消（不发送，可修改输入）")
        self._btn_cancel.clicked.connect(self.reject)
        btns.addWidget(self._btn_ok)
        btns.addWidget(self._btn_cancel)
        layout.addLayout(btns)

    def selections(self) -> dict[int, str | None]:
        return {i: combo.currentData() for i, combo in enumerate(self._combos)}


class _ThinkingBlock(QWidget):
    """可折叠的"思考过程"块：默认收起，点标题展开/收起。

    为什么折叠：思考过程动辄上千字，直接铺开会把正文淹没；但它是"模型为什么这么答"
    的第一手依据，必须**可查**且**已落盘**（logs/chat/<用户>/<会话>/thinking.jsonl）。
    """

    def __init__(self, reasoning: str, *, tokens: int = 0, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._reasoning = reasoning or ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        tok = f" · 思考 {tokens} tokens" if tokens else ""
        self._btn = QPushButton(f"▸ 思考过程（{len(self._reasoning)} 字{tok}）")
        self._btn.setFlat(True)
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.setStyleSheet(
            "text-align:left;color:#5a6b7a;font-size:11px;padding:2px 4px;border:none;")
        self._btn.clicked.connect(self._toggle)
        layout.addWidget(self._btn)
        self._body = QLabel(_html.escape(self._reasoning).replace("\n", "<br>"))
        self._body.setWordWrap(True)
        self._body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._body.setStyleSheet(
            "background:#f7f7f4;color:#4a4a4a;border-left:3px solid #cfd6cc;"
            "padding:6px 8px;font-size:12px;")
        self._body.setVisible(False)
        layout.addWidget(self._body)
        self._expanded = False

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        tok = ""
        if "tokens" in self._btn.text():
            tok = " · " + self._btn.text().split(" · ", 1)[1].rstrip("）")
        self._btn.setText(
            f"{'▾' if self._expanded else '▸'} 思考过程（{len(self._reasoning)} 字{tok}）")

    @property
    def reasoning(self) -> str:
        return self._reasoning


class ChatPanel(QWidget):
    """对话面板主体（放 QDockWidget 内即为常驻窗口）。"""

    def __init__(self, username: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._username = username
        self._active_dir: Path | None = None
        self._busy = False
        self._thread_worker = None  # (QThread, _ChatWorker) 运行中才非空

        self._build_ui()
        self._refresh_conversations()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # 第一行：会话选择与新建/删除
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("会话："))
        self._conv_combo = QComboBox()
        self._conv_combo.setMinimumWidth(120)   # 原来 180：停靠窗拖窄时会顶破布局
        self._conv_combo.currentIndexChanged.connect(self._on_conv_changed)
        row1.addWidget(self._conv_combo, 1)
        self._btn_new_conv = QPushButton("新建")
        self._btn_new_conv.clicked.connect(self._new_conversation)
        self._btn_del_conv = QPushButton("删除")
        self._btn_del_conv.clicked.connect(self._delete_conversation)
        row1.addWidget(self._btn_new_conv)
        row1.addWidget(self._btn_del_conv)
        root.addLayout(row1)

        # 第二行：记忆方式 / 提示词版本 / 范围（多版本对比入口）
        # 用表单布局（每项一行）：停靠窗被拖窄时不会像原来那样把三个下拉挤成一条
        # 缝、文字全被裁掉。原来的「检索方式(全局)」括号说明移到 tooltip。
        from PySide6.QtWidgets import QFormLayout

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(4)
        # 全局检索方式：不再按会话选择；切换即写入 .env（RETRIEVAL_METHOD），
        # 对整条检索/核对链路（L1/边构建/L3 查询/对话）统一生效。
        self._method_combo = QComboBox()
        self._method_combo.setToolTip("全局检索方式（写入 .env 的 RETRIEVAL_METHOD）："
                                      "图遍历不依赖 embedding；RAG 方法保留分块+embedding。")
        for key in ("graph", "rag"):
            v = engine.MEMORY_VARIANTS.get(key)
            if v is not None:
                self._method_combo.addItem(v.label, key)
        cur_idx = self._method_combo.findData(retrieval_config__ai.global_method())
        if cur_idx >= 0:
            self._method_combo.setCurrentIndex(cur_idx)
        self._method_combo.currentIndexChanged.connect(self._on_method_changed)
        form.addRow("检索方式：", self._method_combo)

        self._version_combo = QComboBox()
        for key, v in engine.PROMPT_VERSIONS.items():
            self._version_combo.addItem(v.label, key)
        form.addRow("提示词：", self._version_combo)

        self._scope_combo = QComboBox()
        for label, _desc in engine.SCOPE_OPTIONS:
            self._scope_combo.addItem(label)
        form.addRow("范围：", self._scope_combo)
        root.addLayout(form)

        # 消息区（滚动气泡）
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.StyledPanel)
        self._messages_host = QWidget()
        self._messages_layout = QVBoxLayout(self._messages_host)
        self._messages_layout.setContentsMargins(4, 4, 4, 4)
        self._messages_layout.setSpacing(6)
        self._messages_layout.addStretch(1)
        self._scroll.setWidget(self._messages_host)
        self._scroll.setMinimumHeight(120)

        # 输入区（放进纵向分栏：**消息区 / 输入区 的分界线可以拖**，
        # 想多写几行就把输入框拉高；原来用 setMaximumHeight(90) 卡死了）
        self._input = _ChatInputEdit()
        self._input.setPlaceholderText("输入问题…（Enter 发送 / Shift+Enter 换行）")
        self._input.setMinimumHeight(46)
        self._input.send_requested.connect(self._send)
        self._btn_send = QPushButton("发送")
        self._btn_send.clicked.connect(self._send)
        input_box = QWidget()
        row3 = QHBoxLayout(input_box)
        row3.setContentsMargins(0, 0, 0, 0)
        row3.addWidget(self._input, 1)
        row3.addWidget(self._btn_send)

        import ui_kit__ui as kit

        self._chat_split = kit.vbox_splitter([self._scroll, input_box],
                                             sizes=[430, 110], name="chat_col")
        root.addWidget(self._chat_split, 1)

        hint = QLabel("提示：生成内容展示前会经 decode 净化；每条消息与所选 记忆/提示词/范围 一并写入会话日志。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 11px;")
        root.addWidget(hint)
        # 思考过程状态行：当前思考开关 + 本会话已记录多少（点消息上方 ▸ 展开查看）
        # 注：配置状态只在这里算一次（self._thinking_status_text）；会话加载时**追加**
        # 本会话统计，不能整行覆盖——否则一进会话就把"思考开关"提示抹掉了。
        self._thinking_status_text = ""
        try:
            self._thinking_status_text = (
                f"思考过程：{engine.thinking_status()}"
                f"｜落盘 logs/chat/<用户>/<会话>/thinking.jsonl")
        except Exception:
            self._thinking_status_text = "思考过程：模型默认（如返回 reasoning_content 即记录）"
        self._thinking_hint = QLabel(self._thinking_status_text)
        self._thinking_hint.setStyleSheet("color: #5a6b7a; font-size: 11px;")
        self._thinking_hint.setWordWrap(True)
        root.addWidget(self._thinking_hint)

    def _on_method_changed(self) -> None:
        """切换**全局**检索方式：写入 .env 的 RETRIEVAL_METHOD，整条链路生效。"""
        key = str(self._method_combo.currentData() or "")
        if not key:
            return
        try:
            retrieval_config__ai.set_method(key)
        except Exception as exc:
            QMessageBox.warning(self, "切换失败", str(exc))
            return
        self._method_combo.setToolTip(
            f"已保存为全局检索方式：{retrieval_config__ai.label(key)}（.env → RETRIEVAL_METHOD）"
        )

    # ---------- 会话管理 ----------
    def _refresh_conversations(self, select_conv_id: str | None = None) -> None:
        """重载会话下拉（保留/指定选中项；无会话时自动新建一个）。"""
        convs = store.list_conversations(self._username)
        self._conv_combo.blockSignals(True)
        self._conv_combo.clear()
        self._conv_data: list[dict] = []
        for rec in convs:
            self._conv_data.append(rec)
            self._conv_combo.addItem(
                f"{rec.get('title', '新会话')}  ({rec.get('message_count', 0)}条)",
                rec.get("conv_id"),
            )
        self._conv_combo.blockSignals(False)
        if not convs:
            # 首次打开/全部删除后自动建一个"新会话"
            self._create_conversation("新会话", select_after=True)
            return
        idx = 0
        if select_conv_id:
            for i, rec in enumerate(self._conv_data):
                if rec.get("conv_id") == select_conv_id:
                    idx = i
                    break
        self._conv_combo.setCurrentIndex(idx)
        self._on_conv_changed()

    def _conv_dir(self) -> Path | None:
        conv_id = self._conv_combo.currentData()
        if not conv_id:
            return None
        return store.conversation_dir(self._username, str(conv_id))

    def _create_conversation(self, title: str, select_after: bool = False) -> str | None:
        meta = {
            "prompt_version": self._version_combo.currentData() or "",
            "memory_key": retrieval_config__ai.global_method(),
            "scope_label": self._scope_combo.currentText(),
            "engine": "chat_engine__ai.v1",
        }
        rec = store.new_conversation(self._username, title, meta)
        if select_after:
            self._refresh_conversations(select_conv_id=rec["conv_id"])
        return rec["conv_id"]

    def _new_conversation(self) -> None:
        if self._busy:
            QMessageBox.information(self, "提示", "正在生成回答，请稍候。")
            return
        self._create_conversation("新会话", select_after=True)

    def _delete_conversation(self) -> None:
        conv_dir = self._conv_dir()
        if conv_dir is None:
            return
        reply = QMessageBox.question(
            self, "确认删除", "删除该会话？\n（将移入回收区，可恢复；彻底删除可在回收站中操作）"
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        store.delete_conversation(conv_dir)
        self._refresh_conversations()

    def _open_recycle(self) -> None:
        dialog = _RecycleDialog(self)
        dialog.exec()

    # ---------- 消息展示 ----------
    def _set_thinking_hint(self, extra: str) -> None:
        """思考状态行 = 配置状态（一直在）+ 本会话统计（追加，不覆盖配置）。"""
        base = getattr(self, "_thinking_status_text", "")
        self._thinking_hint.setText(f"{base}\n{extra}" if extra else base)

    def _clear_bubbles(self) -> None:
        while self._messages_layout.count() > 1:  # 保留末尾 stretch
            item = self._messages_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _append_bubble(self, role: str, text: str, tag: str = "",
                       *, thinking: str = "", thinking_tokens: int = 0) -> None:
        bg, fg, align = _ROLE_STYLE.get(role, _ROLE_STYLE["ai"])
        safe = _html.escape(text or "").replace("\n", "<br>")
        title = tag if tag else ("我" if role == "user" else "助手")
        html = (
            f'<div style="background:{bg};color:{fg};border-radius:8px;'
            f'padding:6px 10px;font-size:13px;word-wrap:break-word;">'
            f'<b>{_html.escape(title)}</b><br>{safe}</div>'
        )
        label = QLabel(html)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        halign = Qt.AlignmentFlag.AlignRight if align == "right" else Qt.AlignmentFlag.AlignLeft
        insert_at = self._messages_layout.count() - 1
        # 思考过程放在回答**上面**：阅读顺序 = 怎么想的 → 答了什么（默认收起）
        if thinking:
            self._messages_layout.insertWidget(
                insert_at, _ThinkingBlock(thinking, tokens=thinking_tokens, parent=self), 0, halign)
            insert_at += 1
        self._messages_layout.insertWidget(insert_at, label, 0, halign)
        # 滚到底部
        sb = self._scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_conv_changed(self) -> None:
        if self._busy:
            return
        self._clear_bubbles()
        conv_dir = self._conv_dir()
        self._active_dir = conv_dir
        if conv_dir is None:
            return
        # 思考过程单独存 thinking.jsonl：按消息 meta 里的 thinking_idx 取正文回填
        thinking_map = store.thinking_by_idx(conv_dir)
        stats = store.thinking_stats(conv_dir)
        if stats.get("count"):
            self._set_thinking_hint(
                f"本会话已记录思考 {stats['count']} 段（{stats['chars']} 字 / "
                f"思考 {stats['tokens']} tokens）｜点消息上方「▸ 思考过程」展开")
        else:
            self._set_thinking_hint("")
        for m in store.load_messages(conv_dir):
            role = m.get("role", "ai")
            if role == "error":
                role = "error"
            elif role not in _ROLE_STYLE:
                role = "ai"
            # 展示层：用户消息优先显示本地原文（日志正文是掩码 payload）
            content = m.get("content", "")
            if role == "user" and m.get("meta", {}).get("original"):
                content = m["meta"]["original"]
            meta = m.get("meta", {}) or {}
            tag = meta.get("prompt_label", "") if role == "ai" else ""
            rec = thinking_map.get(int(meta.get("thinking_idx", -1)))
            self._append_bubble(
                role, content, tag,
                thinking=str((rec or {}).get("reasoning") or ""),
                thinking_tokens=int((rec or {}).get("reasoning_tokens") or 0),
            )

    # ---------- 发送 / 生成 ----------
    def _send(self) -> None:
        text = self._input.toPlainText().strip()
        if not text:
            return
        if self._busy:
            QMessageBox.information(self, "提示", "正在生成回答，请稍候。")
            return
        conv_dir = self._conv_dir()
        if conv_dir is None:
            self._refresh_conversations()
            conv_dir = self._conv_dir()
            if conv_dir is None:
                return

        # ===== 出站守卫：本地检查需脱敏信息（不联网）=====
        # 唯一命中 → 自动换脱敏码；多义/子类 → 弹本地选择后发送；
        # 数字（证件/卡号）一律自动掩码。日志保留原文（meta.original），
        # 真正发给模型的是掩码后的 payload（含后续历史，天然安全）。
        plan = None
        try:
            plan = engine.analyze_outbound(text)
        except Exception as exc:
            plan = None
            print(f"⚠️ [外发守卫] 分析失败，按原文发送：{exc}", flush=True)
        resolutions: dict[int, str | None] | None = None
        if plan and plan.get("ambiguous"):
            dialog = _OutboundGuardDialog(plan["ambiguous"], self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return  # 取消：保留输入框内容，用户可修改后再发
            resolutions = dialog.selections()
        try:
            payload = engine.compose_payload(text, plan or {}, resolutions)
        except Exception:
            payload = text

        self._append_bubble("user", text)
        # 日志内容 = 掩码后的 payload（外发安全）；原文放 meta 供本地审计/展示
        changed = payload != text
        outbound_meta = {
            "original": text,
            "outbound": {
                "auto_codes": [a["code"] for a in (plan or {}).get("auto", [])],
                "chosen": [c for c in (resolutions or {}).values() if c],
                "skipped": sum(
                    1 for c in (resolutions or {}).values() if c is None
                ) if resolutions else 0,
                "fuzzy_auto": bool((plan or {}).get("fuzzy_auto")),
                "changed": changed,
            },
        }
        store.append_message(conv_dir, "user", payload, outbound_meta)

        self._input.clear()
        self._busy = True
        self._btn_send.setEnabled(False)
        self._input.setEnabled(False)

        history = store.load_messages(conv_dir)[:-1]  # 上文不含刚发的这条（已是掩码内容）
        worker = _ChatWorker(
            payload,
            self._username,
            self._scope_combo.currentText(),
            str(self._version_combo.currentData()),
            None,  # 检索方式由**全局配置**决定（retrieval_config），不按会话传递
            history,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_generate_done)
        worker.done.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        self._thread_worker = (thread, worker)
        thread.start()

    def _on_generate_done(self, text: object, meta: object, err: object) -> None:
        self._busy = False
        self._btn_send.setEnabled(True)
        self._input.setEnabled(True)
        conv_dir = self._conv_dir()
        text_s = str(text) if text else ""
        meta_d = dict(meta) if isinstance(meta, dict) else {}
        err_s = str(err) if err else ""

        if err_s:
            self._append_bubble("error", err_s, "提示")
            if conv_dir is not None:
                store.append_message(conv_dir, "error", err_s, meta_d)
            return
        # ---- 思考过程：先落 thinking.jsonl，再把 idx 记进消息 meta ----
        thinking = str(meta_d.get("reasoning") or "")
        thinking_idx = -1
        if conv_dir is not None and thinking:
            rec = store.append_thinking(
                conv_dir,
                question=str(meta_d.get("question_masked") or ""),
                reasoning=thinking,
                reasoning_tokens=int(meta_d.get("reasoning_tokens") or 0),
                model=str(meta_d.get("model") or ""),
                meta=meta_d,
            )
            thinking_idx = int(rec.get("idx", -1))
        meta_d["thinking_idx"] = thinking_idx
        meta_d.pop("reasoning", None)          # 正文只在 thinking.jsonl，避免消息流被淹没
        meta_d.pop("question_masked", None)
        # 正常回答：展示（已 decode，思考折叠在上方）+ 记录
        self._append_bubble("ai", text_s, meta_d.get("prompt_label", ""),
                            thinking=thinking,
                            thinking_tokens=int(meta_d.get("reasoning_tokens") or 0))
        if conv_dir is not None:
            store.append_message(conv_dir, "ai", text_s, meta_d)
            if thinking_idx >= 0:
                stats = store.thinking_stats(conv_dir)
                self._set_thinking_hint(
                    f"本会话已记录思考 {stats['count']} 段（{stats['chars']} 字 / "
                    f"思考 {stats['tokens']} tokens）｜已写入 thinking.jsonl")
        # 会话标题：首条消息后以问题开头命名（便于列表辨识）
        if conv_dir is not None and not store.load_conv_meta(conv_dir).get("_titled"):
            msgs = store.load_messages(conv_dir)
            if msgs:
                first = str(msgs[0].get("content", "")).strip()
                title = (first[:20] + "…") if len(first) > 20 else (first or "新会话")
                rec = store.load_conv_meta(conv_dir)
                rec["title"] = title
                rec["_titled"] = True
                import json as _json
                conv_p = store._meta_path(conv_dir)
                conv_p.write_text(_json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
                # 刷新下拉显示（不重载，避免打断滚动）
                idx = self._conv_combo.currentIndex()
                if idx >= 0:
                    self._conv_combo.setItemText(
                        idx, f"{title}  ({rec.get('message_count', 0)}条)"
                    )
        self._thread_worker = None

    def closeEvent(self, event) -> None:
        # 兜底：若后台线程仍在跑，请求退出（网络超时由引擎侧 timeout 兜底）
        if self._thread_worker is not None:
            thread, _w = self._thread_worker
            thread.quit()
            thread.wait(1500)
            self._thread_worker = None
        super().closeEvent(event)


class _RecycleDialog(QDialog):
    """回收站：列出回收区会话，支持 恢复 / 彻底删除。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("回收站 - AI 对话")
        fit_to_screen(self, 560, 300)
        self._recycled: list[dict] = []
        self._build_ui()
        self._reload()

    def _build_ui(self) -> None:
        from PySide6.QtWidgets import QListWidget, QListWidgetItem
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("已删除（回收）的会话："))
        self._list = QListWidget()
        layout.addWidget(self._list, 1)
        row = QHBoxLayout()
        self._btn_restore = QPushButton("恢复选中")
        self._btn_restore.clicked.connect(self._restore)
        self._btn_purge = QPushButton("彻底删除选中")
        self._btn_purge.clicked.connect(self._purge)
        self._btn_refresh = QPushButton("刷新")
        self._btn_refresh.clicked.connect(self._reload)
        row.addWidget(self._btn_restore)
        row.addWidget(self._btn_purge)
        row.addStretch(1)
        row.addWidget(self._btn_refresh)
        layout.addLayout(row)

    def _reload(self) -> None:
        self._recycled = store.list_recycle()
        self._list.clear()
        for rec in self._recycled:
            owner = rec.get("_owner", "")
            title = rec.get("title", rec.get("conv_id", ""))
            ts = rec.get("updated_at", "")
            self._list.addItem(f"[{owner}] {title}（更新 {ts}）")

    def _selected_path(self) -> str | None:
        idx = self._list.currentRow()
        if idx < 0 or idx >= len(self._recycled):
            return None
        return self._recycled[idx].get("_recycle_path")

    def _restore(self) -> None:
        p = self._selected_path()
        if not p:
            QMessageBox.information(self, "提示", "请先选择一条回收会话。")
            return
        target = store.restore_conversation(p)
        QMessageBox.information(self, "已恢复", f"已恢复到：{target}")
        self._reload()

    def _purge(self) -> None:
        p = self._selected_path()
        if not p:
            QMessageBox.information(self, "提示", "请先选择一条回收会话。")
            return
        reply = QMessageBox.question(self, "确认", "彻底删除该会话？此操作不可恢复！")
        if reply != QMessageBox.StandardButton.Yes:
            return
        store.purge_conversation(p)
        self._reload()


class ChatDock(QDockWidget):
    """常驻"AI 对话"停靠窗口（主窗口右侧；可拖动/悬浮/关闭，可随时再打开）。"""

    def __init__(self, username: str, parent: QWidget | None = None) -> None:
        super().__init__("AI 对话（常驻）", parent)
        self.setObjectName("ai_chat_dock")
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.setWidget(ChatPanel(username, self))
