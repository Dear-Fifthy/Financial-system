"""入账审核窗口：查看发票/合同等候选文档，由**用户**决定是否入账。

背景（本轮需求）：
  · 以前是"扫到合同字段就自动写台账"——分类一错（发票/汇总表/付款申请被关键词误判成
    合同），错行就自动进台账；
  · 现在识别只负责**提名**：候选（合同/发票）连同已脱敏的预填字段进 `ledger_inbox`，
    本窗口负责**决定**——「入账」写进对应台账表，「不入账」只留痕。

界面分工：
  · 左/上：待入账列表（类型 / 文档 / 标题 / 关键字段 / 来源 / 时间）；
  · 右/下：选中条目的详情 —— 预填字段（可编辑）、脱敏正文预览、源文件路径（回原文核对）；
  · 按钮：入账（写台账）｜不入账（驳回）｜保存字段修改｜打开源文件｜查看已处理记录。

权限：任何已登录用户可以查看；**入账/不入账只有最高管理员与财务主管可点**
（界面按钮禁用 + 文案说明；写库本身仍走 database_serv 的列校验）。
"""
from __future__ import annotations
from ui_kit__ui import fit_to_screen


from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
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

import ledger_inbox__desens as inbox

KIND_LABELS = {"contract": "合同", "invoice": "发票", "other": "其它"}


class _AiFillWorker(QThread):
    """后台跑 `inbox.ai_fill`（会调 AI，10~60 秒），别冻住界面。"""

    done = Signal(object)

    def __init__(self, item_id: int, user: dict | None, parent=None) -> None:
        super().__init__(parent)
        self._item_id = item_id
        self._user = user
        self._parent = parent

    def run(self) -> None:      # noqa: D102（Qt 线程入口）
        if self._parent is not None:
            self._parent._ai_last_id = self._item_id
        try:
            res = inbox.ai_fill(self._item_id, self._user)
        except Exception as exc:
            res = {"ok": False, "msg": f"{type(exc).__name__}: {exc}"}
        self.done.emit(res)
COLUMNS = ["Id", "类型", "状态", "文档", "标题", "目标台账", "预填字段", "字段来源", "登记人", "时间"]
EDITABLE_KINDS = {"contract", "invoice"}


class LedgerInboxDialog(QDialog):
    """待入账审核：查看 + 决定是否入账。"""

    def __init__(self, parent=None, current_user: dict | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user or {}
        self.setWindowTitle("入账审核 - 合同 / 发票是否入账（人工决定）")
        fit_to_screen(self, 1280, 720)
        self._items: list[dict] = []
        role = self.current_user.get("role_type")
        self._can_decide = role in ("admin", "financial_role")
        self._ai_busy = False          # AI 填写进行中（按钮禁用，防重复点）
        self._ai_worker: _AiFillWorker | None = None
        self._ai_last_id: int | None = None
        self._build_ui()
        self.reload()

    # ---------------- 界面 ----------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        head = QLabel(
            "扫描**不会自动入账**了：判定为合同/发票的文档会先到这里，由你决定。\n"
            "「从 hub 刷新」= 直接读 hub 里已有的 JSON 产物重新提名（不重扫、不 OCR、不调 AI）；"
            "「AI 读取并填表」= 让 AI 读这份脱敏正文把台账字段填好（仍**不写库**）。\n"
            "「入账」= 写入对应台账（合同 → 合同台账；发票 → 发票台账）；"
            "「不入账」= 只留痕、不写任何表。\n"
            "字段是**已脱敏**的预填值（可修改）：编号/甲方等显示为 [CO0001] 之类的代号，"
            "这意味着台账里保存的也是代号，不会落明文。"
            + ("" if self._can_decide else
               "\n当前角色为查看权限：入账/不入账按钮已禁用（需最高管理员或财务主管）。")
        )
        head.setWordWrap(True)
        layout.addWidget(head)

        filt = QHBoxLayout()
        filt.addWidget(QLabel("显示："))
        self.cb_status = QComboBox()
        self.cb_status.addItem("待入账", "pending")
        self.cb_status.addItem("已入账", "posted")
        self.cb_status.addItem("不入账", "rejected")
        self.cb_status.addItem("全部", None)
        self.cb_status.currentIndexChanged.connect(lambda _i: self.reload())
        filt.addWidget(self.cb_status)
        filt.addWidget(QLabel("类型："))
        self.cb_kind = QComboBox()
        self.cb_kind.addItem("全部", None)
        for k, label in KIND_LABELS.items():
            self.cb_kind.addItem(label, k)
        self.cb_kind.currentIndexChanged.connect(lambda _i: self.reload())
        filt.addWidget(self.cb_kind)
        self.lbl_stats = QLabel("")
        self.lbl_stats.setStyleSheet("color: #444; font-weight: bold;")
        self.lbl_stats.setWordWrap(True)     # 计数行随内容变长，窄窗口下要能换行
        filt.addWidget(self.lbl_stats)
        filt.addStretch(1)
        layout.addLayout(filt)

        self.tbl = QTableWidget(0, len(COLUMNS))
        self.tbl.setHorizontalHeaderLabels(COLUMNS)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.verticalHeader().setVisible(False)
        hdr = self.tbl.horizontalHeader()
        for col in range(len(COLUMNS)):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents
                                     if col != 4 else QHeaderView.ResizeMode.Stretch)
        self.tbl.itemSelectionChanged.connect(self._on_pick)
        layout.addWidget(self.tbl, 2)

        detail_row = QHBoxLayout()
        left = QVBoxLayout()
        kind_row = QHBoxLayout()
        kind_row.addWidget(QLabel("类型（可改判）："))
        self.cb_kind_override = QComboBox()
        self.cb_kind_override.addItem("合同（→合同台账）", "contract")
        self.cb_kind_override.addItem("发票（→发票台账）", "invoice")
        self.cb_kind_override.addItem("其它（不入账）", "other")
        self.cb_kind_override.setEnabled(self._can_decide)
        kind_row.addWidget(self.cb_kind_override)
        self.btn_set_kind = QPushButton("应用类型")
        self.btn_set_kind.clicked.connect(self._apply_kind)
        self.btn_set_kind.setEnabled(self._can_decide)
        kind_row.addWidget(self.btn_set_kind)
        kind_row.addStretch(1)
        left.addLayout(kind_row)
        left.addWidget(QLabel("预填字段（可修改；键=台账列名/字段名）"))
        self.ed_fields = QPlainTextEdit()
        self.ed_fields.setPlaceholderText("{}")
        left.addWidget(self.ed_fields)
        detail_row.addLayout(left, 1)
        right = QVBoxLayout()
        right.addWidget(QLabel("脱敏正文预览（取数请回原文核对）"))
        self.txt_doc = QPlainTextEdit()
        self.txt_doc.setReadOnly(True)
        right.addWidget(self.txt_doc)
        detail_row.addLayout(right, 1)
        layout.addLayout(detail_row, 1)

        self.btn_save = QPushButton("保存修改")
        self.btn_save.clicked.connect(self._save_fields)
        self.btn_post = QPushButton("入账")
        self.btn_post.setToolTip("入账：把这份合同/发票写进对应台账")
        self.btn_post.clicked.connect(self._approve)
        self.btn_reject = QPushButton("驳回")
        self.btn_reject.setToolTip("不入账（驳回）：从待入账队列里去掉，不写台账")
        self.btn_reject.clicked.connect(self._reject)
        self.btn_src = QPushButton("打开源文件")
        self.btn_src.clicked.connect(self._open_source)
        self.btn_ref = QPushButton("刷新")
        self.btn_ref.clicked.connect(self.reload)
        # 直接读 hub 产物重新提名（不依赖库里有行、不重扫、不调 AI）
        self.btn_hub_refresh = QPushButton("从 hub 刷新…")
        self.btn_hub_refresh.setToolTip(
            "从 hub 目录里**已有的 JSON 产物**重新读取，把「可能需要入账」的合同/发票再捞一遍：\n"
            "· 走的是和流水线同一套合同闸门 + 字段预填（正则），不重新 OCR、不动源文件、不调 AI；\n"
            "· 库被清过、hub 从别处拷进来、上次扫描中断只落了 hub —— 这些情况都能补上；\n"
            "· 已人工入账/驳回的条目不会被覆盖（同一文档只保留一条待入账）。"
        )
        self.btn_hub_refresh.clicked.connect(self._refresh_from_hub)
        self.btn_hub_refresh.setEnabled(self._can_decide)
        # 让 AI 读这份 hub 产物并把台账字段填好（只填不写库）
        self.btn_ai_fill = QPushButton("AI 读取并填表")
        self.btn_ai_fill.setToolTip(
            "让 AI 读这份**已脱敏**的 hub 正文 + 台账历史项目/备注习惯，把台账字段填好：\n"
            "· 合同 → 合同台账字段；发票 → 发票台账字段（同样由 AI 读正文抽取，不用正则）；\n"
            "· 两条走**同一条 AI 链路与同一个模型**（.env 的 AI_* 配置）；\n"
            "· 只填字段、**不写台账**——核对满意后再点「入账」。密钥只在 .env，不会进日志。"
        )
        self.btn_ai_fill.clicked.connect(self._ai_fill)
        self.btn_ai_fill.setEnabled(self._can_decide)
        # 文案压短（原来"按新合同口径重新判定（批量）"需要 182px，窄窗口里被裁成半个字）
        self.btn_rejudge = QPushButton("批量重判")
        self.btn_rejudge.setToolTip(
            "按新合同口径重新判定（批量）：对**库里已有文档行**重新跑一遍合同判定"
            "（PDF/Word + 命名/标题含合同/协议），\n"
            "校正「付款申请/xlsx 曾被判成合同」这类旧分类，并把候选重新送进本队列。\n"
            "不重新 OCR、不动源文件。仅最高管理员/财务主管可执行。"
        )
        self.btn_rejudge.clicked.connect(self._rejudge)
        self.btn_rejudge.setEnabled(self._can_decide)
        for b in (self.btn_save, self.btn_post, self.btn_reject):
            b.setEnabled(self._can_decide)
        # 8 个按钮排成网格（4 列）：窄窗口自动换行，文字不会被裁（与其它窗口同一约定）
        import ui_kit__ui as kit

        layout.addLayout(kit.grid_row(
            [self.btn_save, self.btn_post, self.btn_reject, self.btn_ai_fill,
             self.btn_hub_refresh, self.btn_rejudge, self.btn_src, self.btn_ref],
            columns=4))

        self.lbl_note = QLabel("")
        self.lbl_note.setStyleSheet("color: #666;")
        self.lbl_note.setWordWrap(True)
        layout.addWidget(self.lbl_note)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        close.accepted.connect(self.accept)
        layout.addWidget(close)

    # ---------------- 数据 ----------------
    def reload(self) -> None:
        status = self.cb_status.currentData()
        kind = self.cb_kind.currentData()
        try:
            self._items = inbox.list_items(status=status, kind=kind)
            c = inbox.counts()
        except Exception as exc:
            QMessageBox.critical(self, "读取待入账队列失败", f"{type(exc).__name__}: {exc}")
            self._items, c = [], {}
        self.lbl_stats.setText(
            f"待入账 {c.get('pending', 0)}（合同 {c.get('pending_contract', 0)} / "
            f"发票 {c.get('pending_invoice', 0)} / 其它 {c.get('pending_other', 0)}）｜"
            f"已入账 {c.get('posted', 0)}｜不入账 {c.get('rejected', 0)}"
        )
        self.tbl.setRowCount(len(self._items))
        for r, it in enumerate(self._items):
            fk = "、".join(sorted((it.get("fields") or {}).keys()))
            cells = [str(it["id"]), KIND_LABELS.get(it["kind"], it["kind"]),
                     it["status_label"], it.get("source_file") or it.get("doc_key", ""),
                     it.get("title") or "", it.get("target_table") or "—",
                     fk or "（空）", it.get("fields_source") or "", it.get("created_by") or "",
                     it.get("created_at") or ""]
            for c_idx, text in enumerate(cells):
                self.tbl.setItem(r, c_idx, QTableWidgetItem(str(text)))
        self.ed_fields.clear()
        self.txt_doc.clear()

    def _selected(self) -> dict | None:
        row = self.tbl.currentRow()
        if row < 0 or row >= len(self._items):
            return None
        return self._items[row]

    def _on_pick(self) -> None:
        it = self._selected()
        if not it:
            return
        import json

        self.ed_fields.setPlainText(json.dumps(it.get("fields") or {}, ensure_ascii=False,
                                               indent=2))
        self.lbl_note.setText(
            f"[{it['id']}] {KIND_LABELS.get(it['kind'], it['kind'])}｜状态 {it['status_label']}"
            f"｜目标台账 {it.get('target_table') or '无'}"
            + (f"｜已入账键 {it.get('posted_key')}" if it.get("posted_key") else "")
            + (f"\n源文件：{it.get('source_path')}" if it.get("source_path") else "")
            + (f"\n提示：{it.get('note')}" if it.get("note") else "")
            + (f"\n处理：{it.get('decided_by')} @ {it.get('decided_at')}"
               if it.get("decided_at") else "")
        )
        pages = []
        hub = it.get("hub_file")
        if hub:
            try:
                from pathlib import Path
                import json as _json

                doc = _json.loads(Path(hub).read_text(encoding="utf-8"))
                pages = [str(p or "") for p in (doc.get("pages") or [])]
            except Exception as exc:
                pages = [f"（读取 hub 预览失败：{type(exc).__name__}: {exc}）"]
        self.txt_doc.setPlainText("\n\n".join(pages)[:20000] or "（无正文预览）")
        # 类型下拉框对齐当前条目（用户可改判）
        idx = self.cb_kind_override.findData(it["kind"])
        if idx >= 0:
            self.cb_kind_override.blockSignals(True)
            self.cb_kind_override.setCurrentIndex(idx)
            self.cb_kind_override.blockSignals(False)
        if it["status"] != "pending" or not self._can_decide:
            self.btn_post.setEnabled(False)
            self.btn_reject.setEnabled(False)
            self.btn_save.setEnabled(False)
        else:
            self.btn_post.setEnabled(it["kind"] in EDITABLE_KINDS)
            self.btn_reject.setEnabled(True)
            self.btn_save.setEnabled(True)

    # ---------------- 操作 ----------------
    def _parse_fields(self) -> dict | None:
        import json

        try:
            data = json.loads(self.ed_fields.toPlainText() or "{}")
        except Exception as exc:
            QMessageBox.warning(self, "字段格式错误", f"字段必须是 JSON 对象：{exc}")
            return None
        if not isinstance(data, dict):
            QMessageBox.warning(self, "字段格式错误", "字段必须是 JSON 对象（键值对）")
            return None
        return data

    def _apply_kind(self) -> None:
        """把条目改判为所选类型（识别漏判时人工纠正）。"""
        it = self._selected()
        if not it:
            QMessageBox.information(self, "请选择", "请先在列表里选择一条。")
            return
        kind = self.cb_kind_override.currentData()
        if kind == it["kind"]:
            QMessageBox.information(self, "无需修改", "类型与当前一致。")
            return
        res = inbox.set_kind(it["id"], kind, user=self.current_user)
        if not res.get("ok"):
            QMessageBox.warning(self, "改判失败", res.get("msg", ""))
        else:
            QMessageBox.information(self, "已改判", res.get("msg", ""))
        self.reload()

    def _refresh_from_hub(self) -> None:
        """**从 hub 已有 JSON 重新读取**可能需要入账的合同/发票（不重扫、不 OCR、不调 AI）。"""
        try:
            files = inbox.hub_json_files()
        except Exception as exc:
            QMessageBox.critical(self, "读取 hub 失败", f"{type(exc).__name__}: {exc}")
            return
        if not files:
            QMessageBox.information(
                self, "hub 里没有产物",
                "当前仓库的 hub 目录里没有文档 JSON。\n"
                "（hub 根 = 当前仓库的 hub/；先扫描文件，或确认仓库切换是否正确。）")
            return
        if QMessageBox.question(
            self, "从 hub 刷新待入账",
            f"从 hub 里已有的 JSON 产物重新提名待入账？\n\n"
            f"· 将扫描 **{len(files)}** 份 hub 产物（只读文件 + 本机正则判定）；\n"
            f"· **不重新 OCR、不动源文件、不调用 AI**；\n"
            f"· 已人工入账/驳回的条目不会被覆盖。",
        ) != QMessageBox.StandardButton.Yes:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            res = inbox.refresh_from_hub(user=self.current_user)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "刷新失败", f"{type(exc).__name__}: {exc}")
            return
        QApplication.restoreOverrideCursor()
        lines = [
            f"扫描 hub 产物 {res['scanned']} 份：",
            f"· 判定合同 {res['contract']}｜发票 {res['invoice']}｜其它 {res['other']}"
            f"（其它默认不入队，已跳过 {res['skipped_other']}）",
            f"· 新增待入账 {res['created']}｜更新 {res['updated']}"
            f"｜已在【已入账/不入账】跳过 {res['skipped_decided']}",
        ]
        if res.get("no_pages"):
            lines.append(f"· 无正文（跳过）{res['no_pages']}")
        if res.get("errors"):
            lines.append(f"· 错误 {len(res['errors'])} 条："
                         + "；".join(str(e)[:80] for e in res["errors"][:5]))
        QMessageBox.information(self, "刷新完成", "\n".join(lines))
        self.reload()

    def _ai_fill(self) -> None:
        """让 AI 读选中条目的 hub 产物并把台账字段填进编辑框（**不写库**）。"""
        it = self._selected()
        if not it:
            QMessageBox.information(self, "请选择", "请先在列表里选择一条。")
            return
        if it["status"] != "pending":
            QMessageBox.information(self, "不能修改", f"该条目已是「{it['status_label']}」。")
            return
        if it["kind"] == "other":
            QMessageBox.warning(
                self, "类型未确认",
                "这条没被确认为合同/发票。\n"
                "如果它其实是合同，请先在左上「类型（可改判）」里改成「合同」再让 AI 填表。")
            return
        if self._ai_busy:
            QMessageBox.information(self, "正在处理", "上一次 AI 填写还在进行，请稍候。")
            return
        self._ai_busy = True
        self._ai_last_id = it["id"]
        self._set_busy(True, f"AI 正在读取并填写 #{it['id']}…（合同可能要 10~60 秒）")
        self._ai_worker = _AiFillWorker(it["id"], self.current_user, self)
        self._ai_worker.done.connect(self._on_ai_fill_done)
        self._ai_worker.start()

    def _set_busy(self, busy: bool, note: str = "") -> None:
        for b in (self.btn_ai_fill, self.btn_hub_refresh, self.btn_rejudge, self.btn_post,
                  self.btn_reject, self.btn_save, self.btn_ref):
            b.setEnabled((not busy) and (self._can_decide or b is self.btn_ref))
        if busy:
            self.lbl_note.setText(note)
        else:
            self._on_pick()

    def _on_ai_fill_done(self, payload: object) -> None:
        """AI 填写线程结束：刷新列表并把字段框对准这条（写库仍需用户点「入账」）。"""
        self._ai_busy = False
        self._set_busy(False)
        res = payload if isinstance(payload, dict) else {"ok": False, "msg": "AI 填写异常结束"}
        item_id = getattr(self, "_ai_last_id", None)
        self.reload()
        if item_id:
            for r, it in enumerate(self._items):
                if it["id"] == item_id:
                    self.tbl.selectRow(r)
                    break
        if res.get("ok"):
            QMessageBox.information(self, "AI 已填好字段", res.get("msg", ""))
        else:
            QMessageBox.warning(self, "AI 填写失败", res.get("msg", ""))

    def _rejudge(self) -> None:
        """按新合同口径批量重新判定库里已有文档，并刷新待入账队列。"""
        if QMessageBox.question(
            self, "重新判定",
            "对库里已有文档重新跑一遍合同判定？\n\n"
            "· 只改分类与待入账队列，**不重新 OCR、不动源文件**；\n"
            "· 已人工入账/驳回的条目不会被覆盖。",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            import hub_pipeline__desens as hp

            res = hp.rescan_hub_classifications(dry_run=False, stage_inbox=True,
                                                current_user=self.current_user)
        except Exception as exc:
            QMessageBox.critical(self, "重新判定失败", f"{type(exc).__name__}: {exc}")
            return
        changed = [i for i in res.get("items", []) if i["changed"]]
        lines = [f"扫描 {len(res.get('items', []))} 份，修正分类 {len(changed)} 份，"
                 f"送待入账 {res.get('staged', 0)} 条"]
        for i in changed[:15]:
            lines.append(f"· {i['source_file'][:40]}：{i['old'] or '—'} → {i['new']}")
        if res.get("errors"):
            lines.append(f"错误 {len(res['errors'])} 条：" + "；".join(res["errors"][:3]))
        QMessageBox.information(self, "重新判定完成", "\n".join(lines))
        self.reload()

    def _save_fields(self) -> None:
        it = self._selected()
        if not it:
            QMessageBox.information(self, "请选择", "请先在列表里选择一条。")
            return
        fields = self._parse_fields()
        if fields is None:
            return
        res = inbox.update_fields(it["id"], fields, user=self.current_user)
        if not res.get("ok"):
            QMessageBox.warning(self, "保存失败", res.get("msg", ""))
        else:
            QMessageBox.information(self, "已保存", "字段已更新（仍为待入账）。")
        self.reload()

    def _approve(self) -> None:
        it = self._selected()
        if not it:
            QMessageBox.information(self, "请选择", "请先在列表里选择一条。")
            return
        fields = self._parse_fields()
        if fields is None:
            return
        # 用户在下拉框里改了类型 → 先改判，再入账（"识别提名、人工定性"）
        chosen = self.cb_kind_override.currentData()
        if chosen and chosen != it["kind"]:
            res_kind = inbox.set_kind(it["id"], chosen, user=self.current_user)
            if not res_kind.get("ok"):
                QMessageBox.warning(self, "改判失败", res_kind.get("msg", ""))
                return
            it = inbox.get_item(it["id"]) or it
        kind_label = KIND_LABELS.get(it["kind"], it["kind"])
        target = it.get("target_table")
        if QMessageBox.question(
            self, "确认入账",
            f"把这条{kind_label}写入「{target}」？\n\n"
            f"文档：{it.get('source_file') or it.get('doc_key')}\n"
            f"字段：{'、'.join(sorted(fields.keys())) or '（空）'}\n\n"
            f"同编号重复入账会按最新版覆盖；写入的是**脱敏后**的值。",
        ) != QMessageBox.StandardButton.Yes:
            return
        res = inbox.approve(it["id"], user=self.current_user, fields=fields)
        if res.get("ok"):
            QMessageBox.information(self, "入账完成", res.get("msg", ""))
        else:
            QMessageBox.warning(self, "入账失败", res.get("msg", ""))
        self.reload()

    def _reject(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        it = self._selected()
        if not it:
            QMessageBox.information(self, "请选择", "请先在列表里选择一条。")
            return
        reason, ok = QInputDialog.getText(self, "不入账", "原因（可空）：")
        if not ok:
            return
        res = inbox.reject(it["id"], user=self.current_user, reason=reason or "")
        if not res.get("ok"):
            QMessageBox.warning(self, "操作失败", res.get("msg", ""))
        self.reload()

    def _open_source(self) -> None:
        it = self._selected()
        if not it:
            return
        path = it.get("source_path")
        if not path:
            QMessageBox.information(self, "无源文件路径", "该条目没有记录源文件路径。")
            return
        from pathlib import Path

        p = Path(path)
        if not p.exists():
            QMessageBox.warning(self, "源文件不在原位", f"找不到：{path}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
