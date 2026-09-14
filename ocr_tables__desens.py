"""OCR/PDF 表格结构化：把 OCR 的 table 块解析成与 xlsx 表块同构的结构，并带坐标锚点。

来源（PaddleOCR-VL 逐页 JSON 的 res.parsing_res_list）里 label 含 "table" 的块，
其 block_content 常见三种形态，按优先级解析：
    1. HTML：`<table><tr><td>…`  → 标准行列（最可靠）
    2. Markdown/管道：`| a | b |` + 分隔行 `|---|---|`
    3. 纯文本：多行，无列分隔 → 每行作为单列表格（保底，不做猜测）
锚点：记录页码、block 的 bbox（若有）、块内容内的字符区间，供证据链回原文定位。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser


class _HTMLTableParser(HTMLParser):
    """HTML 表格 → **列对齐矩阵**（展开 colspan/rowspan）。

    为什么必须展开（与 docx 的 `w:gridSpan/w:vMerge` 同一个坑）：
      OCR 出的 HTML 里 `colspan=4` 的标题格会让同一表每行格数不等，
      于是"标签右侧那一格"在行内根本定位不准，判 kv 时也就看不到"跨列合并"的迹象。
      这里把每个格按 colspan 铺成 N 列、rowspan 在后续行同列补占位，得到等宽矩阵。
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self.col_count = 0
        self.merged_rows: set[int] = set()   # 行下标（0 基）：该行含跨列合并格
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._span = (1, 1)                  # (colspan, rowspan)
        self._pending: dict[int, int] = {}   # 列下标 → 还剩几行需要补占位

    @staticmethod
    def _int_attr(attrs, name: str) -> int:
        for k, v in attrs:
            if str(k).lower() == name:
                try:
                    return max(1, int(str(v).strip()))
                except Exception:
                    return 1
        return 1

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
            self._span = (self._int_attr(attrs, "colspan"), self._int_attr(attrs, "rowspan"))

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            text = "".join(self._cell).strip()
            cs, rs = self._span
            col = len(self._row)
            self._row.append(text)
            for _ in range(cs - 1):
                self._row.append("")
            if cs > 1:
                self.merged_rows.add(len(self.rows))
            if rs > 1:
                for extra in range(1, rs):
                    self._pending[col] = max(self._pending.get(col, 0), extra)
            self._cell = None
            self._span = (1, 1)
        elif tag == "tr" and self._row is not None:
            for col, left in list(self._pending.items()):
                while len(self._row) <= col:
                    self._row.append("")
                if left - 1 > 0:
                    self._pending[col] = left - 1
                else:
                    self._pending.pop(col, None)
            if any(c for c in self._row):
                self.col_count = max(self.col_count, len(self._row))
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def finish(self) -> None:
        """统一补齐到表级列数（不同行格数不等会让"下一行同列"取错格）。"""
        for row in self.rows:
            while len(row) < self.col_count:
                row.append("")


def _pad_rows(rows: list[list[str]]) -> int:
    width = max((len(r) for r in rows), default=0)
    for r in rows:
        while len(r) < width:
            r.append("")
    return width


def parse_table_content(content: str) -> tuple[list[str], list[list[str]], str]:
    """兼容旧接口：返回 (首行, 其余行, 格式标记)。新代码请用 `parse_table_block`。"""
    blk = parse_table_block(content)
    rows = blk["rows"]
    if not rows:
        return [], [], blk["format"]
    return rows[0], rows[1:], blk["format"]


def parse_table_block(content: str) -> dict:
    """解析表块内容 → {rows, format, grid_cols, merged_rows}（**不在这里定表头**）。

    表头/kv/标题的判定统一交给 `table_desens__desens.layout_of`
    （"先剥标题/签章 → 再判 kv → 再定表头"），避免 OCR 路径自己猜一套。
    """
    s = (content or "").strip()
    if not s:
        return {"rows": [], "format": "empty", "grid_cols": 0, "merged_rows": set()}
    if "<table" in s.lower():
        p = _HTMLTableParser()
        try:
            p.feed(s)
            p.close()
            p.finish()
        except Exception:
            pass
        if p.rows:
            return {"rows": p.rows, "format": "html",
                    "grid_cols": max(p.col_count, _pad_rows(p.rows)),
                    "merged_rows": set(p.merged_rows)}
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    pipe_lines = [ln for ln in lines if ln.count("|") >= 2]
    if pipe_lines:
        def cells(ln: str) -> list[str]:
            return [c.strip() for c in ln.strip("|").split("|")]
        data = [cells(ln) for ln in pipe_lines if not re.fullmatch(r"\|[\s:\-|]+\|", ln)]
        if data:
            return {"rows": data, "format": "markdown", "grid_cols": _pad_rows(data),
                    "merged_rows": set()}
    return {"rows": [[ln] for ln in lines], "format": "text", "grid_cols": 1,
            "merged_rows": set()}


def tables_from_page_json(page_json: dict) -> list[dict]:
    """从一页 OCR JSON 里抽出所有 table 块（带页码/bbox/字符区间锚点）。

    布局判定与 xlsx/docx **同一条路径**（`table_desens.layout_of`）：
    先剥标题/签章行 → 再判 kv → 再定表头。旧实现直接拿 `rows[0]` 当表头，
    于是"付款申请"这类题头行会变成整列的语义来源（表格里 8 列只有 1 列有语义）。
    """
    import table_desens__desens as td

    res = (page_json or {}).get("res") or {}
    page_index = res.get("page_index")
    page_no = (page_index + 1) if isinstance(page_index, int) else 1
    blocks = res.get("parsing_res_list") or []
    tables: list[dict] = []
    for order, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        label = str(block.get("block_label") or "")
        if "table" not in label.lower():
            continue
        content = str(block.get("block_content") or "")
        blk = parse_table_block(content)
        rows = blk["rows"]
        if not rows:
            continue
        lo = td.layout_of(rows, grid_cols=blk.get("grid_cols"),
                          merged_rows=blk.get("merged_rows"))
        body = lo["body"] if lo["body"] else [list(r) for r in rows]
        tables.append({
            "source": "ocr_table",
            "sheet": "",
            "table_index": len(tables) + 1,
            "page_from": page_no,
            "page_to": page_no,
            "header": [str(h or "") for h in (lo["header"] or [])],
            "header_merged": bool(blk.get("merged_rows")),
            "style_tags": [],
            "rows": [list(r) for r in body],
            "layout": lo["layout"],
            "title_rows": [list(r) for r in lo["title"]],
            "signature_rows": [list(r) for r in lo["signature"]],
            "layout_reasons": lo["reasons"],
            "kv_score": lo["kv_score"],
            # 注意：这里**不保存**原始 block_content（含未脱敏真值）——
            # 表格一律由字段感知脱敏后的单元格渲染，hub 里不得出现明文。
            "anchor": {
                "page": page_no,
                "block_order": order,
                "block_label": label,
                "bbox": block.get("bbox") or block.get("block_bbox"),
                "char_start": 0,
                "char_end": len(content),
                "format": blk["format"],
            },
        })
    return tables


def attach_page_anchors(tables: list[dict], page_text: str, page_no: int) -> None:
    """在**脱敏后**的页文本里为表块补单元格级锚点（原地修改 tables）。

    做法：按（表头 → 逐行逐列）顺序，在页文本中从游标处查找该单元格的（已脱敏）文本，
    命中即记录 char_start/char_end（相对页文本）。找不到的单元格留 None，不猜。
    已带 cell_anchors 的表（如 xlsx 直读产物）不覆盖，保留其 Excel 坐标。
    """
    text = page_text or ""
    for t in tables:
        if t.get("cell_anchors"):
            continue
        cursor = 0
        first = (t.get("raw_lines") or [""])[0]
        if first:
            base = text.find(first[:40])
            if base >= 0:
                cursor = base
        header_anchors: list[dict] = []
        for c, val in enumerate(t.get("header") or []):
            v = str(val or "")
            if not v.strip():
                continue
            idx = text.find(v, cursor)
            if idx < 0:
                continue
            header_anchors.append({"row": 0, "col": c, "coord": None, "page": page_no,
                                   "char_start": idx, "char_end": idx + len(v)})
            cursor = idx + len(v)
        cell_anchors: list[list[dict | None]] = []
        for r, row in enumerate(t.get("rows") or [], start=1):
            row_anchors: list[dict | None] = []
            for c, val in enumerate(row):
                v = str(val or "")
                if not v.strip():
                    row_anchors.append(None)
                    continue
                idx = text.find(v, cursor)
                if idx < 0:
                    row_anchors.append(None)
                    continue
                row_anchors.append({"row": r, "col": c, "coord": None, "page": page_no,
                                    "char_start": idx, "char_end": idx + len(v)})
                cursor = idx + len(v)
            cell_anchors.append(row_anchors)
        if header_anchors:
            t["header_anchors"] = header_anchors
        if cell_anchors:
            t["cell_anchors"] = cell_anchors


def add_raw_spans(tables: list[dict], segs: list[list[int]], raw_len: int | None = None) -> None:
    """用脱敏↔原文映射给每个单元格锚点补原文坐标（原地修改）。

    写入：
      · raw_char_start / raw_char_end —— 原文页文本里的区间（宽松包围盒）；
      · raw_exact —— True 表示该区间逐字对应；False 表示区间内含被替换/插入的
        脱敏区段（如单元格值被整体换成 [CO0001]），此时区间是"原文包围盒"，
        仍可据此回原文核对，但不能逐字比对。
    raw_len：原文页文本长度（用于定位结尾被整体替换的右边界）。
    """
    import offset_map__desens

    def _fill(anchor: dict | None) -> None:
        if not anchor or anchor.get("raw_char_start") is not None:
            return   # 已有精确原文坐标（装配时算出）→ 不覆盖
        mapped = offset_map__desens.map_span_loose(
            segs, anchor.get("char_start"), anchor.get("char_end"), raw_len
        )
        if mapped:
            anchor["raw_char_start"], anchor["raw_char_end"], anchor["raw_exact"] = mapped

    for t in tables:
        for a in (t.get("header_anchors") or []):
            _fill(a)
        for row in (t.get("cell_anchors") or []):
            for a in row:
                _fill(a)


def collect_tables_from_page_json(page_json: dict) -> list[dict]:
    """统一入口：已有结构化 tables 就直接用（如 xlsx 直读产物），否则解析 OCR table 块。"""
    res = (page_json or {}).get("res") or {}
    existing = res.get("tables")
    if isinstance(existing, list) and existing:
        return list(existing)
    return tables_from_page_json(page_json)
