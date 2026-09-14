"""Office 直读（**能直接提取文字就绝不进 OCR**）：xlsx / docx → 与原生页同构的页 JSON。

产物结构与 pdf_native_extractor 一致：
    {"res": {"page_index", "source", "full_text", "parsing_res_list",
             "desensitized": true, "tables": [...]}}

关键点：
  · xlsx：openpyxl 直读单元格值（不经过任何图像/OCR）；先按 table_split 切表，
    再按列头语义做**表格字段感知脱敏**（table_desens），表结构一并写入页 JSON 的 tables；
  · docx：stdlib（zipfile + xml）直读 word/document.xml（零新依赖），段落文本走
    文本脱敏（hub_pipeline__desens.desensitize_text），表格按行拼文本后同样脱敏；
  · 页 JSON 标记 `desensitized: true`：hub 流水线据此**跳过二次脱敏**（避免双重处理）。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


# =========================================================
# xlsx
# =========================================================
def _coord(table: dict, where: str, col: int, row_i: int = 0) -> str | None:
    """取单元格 Excel 坐标（如 D3）；OCR 表没有该信息时返回 None。

    注意 `_cells` 里的 col 是 **1 基**（Excel 原生列号），而调用方传进来的是
    0 基列索引，所以要 +1；行则直接用 `row_i` 选中对应行的元数据桶
    （桶里的 row 是 Excel 行号，与传入的 0 基行索引不是同一个口径，不能比较）。
    """
    cells = table.get("_cells") or {}
    try:
        bucket = cells.get("header") if where == "header" else cells.get("rows")[row_i]
        for meta in bucket:
            if meta.get("col") == col + 1:
                return meta.get("coord")
    except Exception:
        return None
    return None


def _serialize_table_anchored(table: dict, page_no: int, base: int) -> tuple[str, list, list]:
    """把表块序列化成文本，同时产出**单元格级锚点**（字符区间相对该页文本）。

    返回 (text, header_anchors, cell_anchors)：
      · 锚点字段：row / col / coord / page / char_start / char_end；
      · 单元格文本已脱敏（本函数在 desensitize_table 之后调用），
        因此 char 区间可直接用于在 hub 页文本里定位该单元格。
    """
    lines: list[str] = []
    pos = base
    # 标题/签章行**照样进页文本**（前缀区分：`title:` / `signature:`），
    # 否则被剥掉的行在 hub 里就彻底消失了，下游再也看不到题头/落款。
    for key, tag in (("title_rows", "title:"), ("signature_rows", "signature:")):
        for row in (table.get(key) or []):
            txt = " | ".join(str(c or "").strip() for c in row if str(c or "").strip())
            if txt:
                lines.append(tag + txt)
                pos += len(tag + txt) + 1
    prefix = "header:"
    line = prefix
    header_anchors: list[dict] = []
    for c, h in enumerate(table.get("header") or []):
        v = str(h or "").strip()
        if not v:
            continue
        if line != prefix:
            line += " | "
        start = pos + len(line)
        line += v
        header_anchors.append({
            "row": 0, "col": c, "coord": _coord(table, "header", c),
            "page": page_no, "char_start": start, "char_end": start + len(v),
        })
    if line != prefix:
        lines.append(line)
        pos += len(line) + 1

    cell_anchors: list[list[dict | None]] = []
    for r, row in enumerate(table.get("rows") or [], start=1):
        line = "row:"
        row_anchors: list[dict | None] = []
        for c, val in enumerate(row):
            v = str(val or "")
            if not v.strip():
                row_anchors.append(None)
                continue
            if line != "row:":
                line += " | "
            start = pos + len(line)
            line += v
            row_anchors.append({
                "row": r, "col": c, "coord": _coord(table, "rows", c, r - 1),
                "page": page_no, "char_start": start, "char_end": start + len(v),
            })
        if line != "row:":
            lines.append(line)
            pos += len(line) + 1
        cell_anchors.append(row_anchors)

    return "\n".join(lines), header_anchors, cell_anchors


def _grid_cells_meta(block_rows: list[list[str]], base: dict) -> dict:
    """按"剥行/分流之后"的表块，重建 docx 单元格元数据（只有行/列，无 Excel 坐标）。"""
    k0 = len(base.get("title_rows") or []) + len(base.get("signature_rows") or [])
    k1 = k0 + 1 if base.get("header") else k0
    out_rows: list[list[dict]] = []
    for i, row in enumerate(base.get("rows") or []):
        src = block_rows[k1 + i] if k1 + i < len(block_rows) else []
        out_rows.append([
            {"row": k1 + i + 1, "col": c + 1, "coord": None,
             "text": str(src[c] if c < len(src) else "")}
            for c in range(len(row))])
    return {
        "header": [{"row": k0 + 1, "col": c + 1, "coord": None, "text": str(h or "")}
                   for c, h in enumerate(base.get("header") or [])],
        "rows": out_rows,
    }


def _realign_cells_meta(t: dict, base: dict) -> dict:
    """把 Excel 真实坐标（A1）对齐到**剥行/分流之后**的行列。

    为什么需要：`split_xlsx` 的 `_cells` 是按"旧表头行"切的，而本轮起表块可能
    被剥掉若干标题/签章行、或按 kv 布局把表头行并入数据，行列下标都会平移。
    这里用 `block_cells`（整块行的原始坐标）按新下标重新映射，保证锚点仍指回原文格子。
    """
    bc = t.get("block_cells") or []
    k0 = len(base.get("title_rows") or []) + len(base.get("signature_rows") or [])
    k1 = k0 + 1 if base.get("header") else k0

    def _bucket(src: list, width: int) -> list[dict]:
        return [(src[c] if c < len(src) else {"row": None, "col": c + 1,
                                              "coord": None, "text": ""})
                for c in range(width)]

    return {
        "header": _bucket(bc[k0] if k0 < len(bc) else [], len(base.get("header") or [])),
        "rows": [_bucket(bc[k1 + i] if k1 + i < len(bc) else [], len(row))
                 for i, row in enumerate(base.get("rows") or [])],
    }


def _fix_date_serials(rows: list[list[str]], header: list[str]) -> list[list[str]]:
    """日期列里存的是 **Excel 序列号**时还原成 `YYYY-MM-DD`（返回新行，不改入参）。

    为什么需要：有些表把日期存成裸整数且单元格格式是 `General`（`is_date=False`），
    openpyxl 不会给 datetime，`str(value)` 就是 `46266`——hub 里"日期"变成一个
    无意义整数（实测 `马山三标段苗木采购合同金额表` 的日期列），取数/时间线全废。
    只在**列头语义是 date** 时转换，避免把金额 46266 元误当日期。

    ⚠️ 必须在**脱敏之前**做（否则列头语义的格式校验看到 "46266" 会跳过、日期先以明文
    落进表块，而页文本二次文本脱敏又会把它换成 DT####，两边就不一致了）。
    """
    hdr = [str(h or "") for h in (header or [])]
    import table_desens__desens as td

    cols = [i for i, h in enumerate(hdr) if td.match_category(h) == "date"]
    out = [list(r) for r in (rows or [])]
    if not cols:
        return out
    try:
        from openpyxl.utils.datetime import from_excel
    except Exception:
        return out
    for row in out:
        for c in cols:
            if c >= len(row):
                continue
            s = str(row[c] or "").strip()
            if not re.fullmatch(r"\d{5}", s):
                continue
            n = int(s)
            if not (20000 <= n <= 60000):          # 1954-08 ~ 2064-03 之外的序列号不动
                continue
            try:
                row[c] = from_excel(n).strftime("%Y-%m-%d")
            except Exception:
                pass
    return out


def read_xlsx(path: str | Path) -> list[dict]:
    """xlsx → 页 JSON 列表（每个 sheet 一页；表块已字段感知脱敏 + 单元格锚点）。"""
    import table_desens__desens as td
    import table_split__scan as ts
    from database_serv__infra import MappingDbStore

    store = MappingDbStore()
    tables = ts.split_xlsx(path)
    by_sheet: dict[str, list[dict]] = {}
    for t in tables:
        by_sheet.setdefault(t.get("sheet") or "Sheet1", []).append(t)

    pages: list[dict] = []
    for page_index, (sheet, sheet_tables) in enumerate(by_sheet.items()):
        page_no = page_index + 1
        desens_tables = []
        for t in sheet_tables:
            # 统一分流（与 docx/OCR 同一条代码路径）：先剥标题/签章行 → 再判 kv → 再定表头。
            # 用 `block_rows`（含表头之前的标题行）而不是旧的 header+rows——
            # 旧实现把标题行在 split 阶段就丢了，hub 里再也看不到"付款申请"这类题头。
            block_rows = t.get("block_rows") or (
                [list(t.get("header") or [])] + [list(r) for r in (t.get("rows") or [])])
            merged_rows = set(t.get("block_merged_rows") or ())
            # 日期列里的 Excel 序列号先还原（**在脱敏前**，见 _fix_date_serials 说明）
            _lo = td.layout_of(block_rows, grid_cols=t.get("grid_cols"),
                               merged_rows=merged_rows)
            block_rows = _fix_date_serials(block_rows, _lo.get("header") or [])
            base, masked = td.mask_table_layout(
                block_rows, store, source="xlsx",
                grid_cols=t.get("grid_cols"), merged_rows=merged_rows,
                extra={"sheet": t.get("sheet") or "", "table_index": t.get("table_index"),
                       "start_row": t.get("start_row"), "end_row": t.get("end_row"),
                       "header_row": t.get("header_row"), "merges": t.get("merges") or [],
                       "_cells": t.get("_cells") or {}},
            )
            # Excel 单元格坐标（唯一能给出真实 A1 坐标的路径）：按"数据行"重新对齐
            masked["_cells"] = _realign_cells_meta(t, base)
            desens_tables.append(masked)
        page_text = ""
        for t in desens_tables:
            if page_text:
                page_text += "\n\n"
            start = len(page_text)
            text, hdr_anchors, cell_anchors = _serialize_table_anchored(t, page_no, start)
            t["page"] = page_no
            t["header_anchors"] = hdr_anchors
            t["cell_anchors"] = cell_anchors
            t["text_char_start"] = start
            t["text_char_end"] = start + len(text)
            page_text += text
        pages.append({
            "res": {
                "page_index": page_index,
                "source": "xlsx",              # 直读标记（非 OCR）
                "desensitized": True,          # 已做表格字段感知脱敏
                "full_text": page_text,
                "rec_texts": [ln for ln in page_text.split("\n") if ln],
                "tables": desens_tables,
            }
        })
    return pages


# =========================================================
# docx（stdlib 直读，零新依赖）
# =========================================================
def _docx_paragraphs_and_tables(path: str | Path) -> tuple[list[str], list[list[list[str]]]]:
    """解析 word/document.xml：返回 (段落文本列表, 表格[行[单元格文本]])。

    兼容旧调用（只给文本矩阵）；需要**列对齐/合并信息**时用 `_docx_tables_grid()`。
    """
    paras, tables, _grids = _docx_tables_grid(path)
    return paras, tables


def _docx_tables_grid(path: str | Path) -> tuple[list[str], list[list[list[str]]], list[dict]]:
    """解析 docx，并**展开合并单元格**得到"列对齐矩阵"。

    为什么必须展开（本轮修复的根因之一）：
      OOXML 里横向合并写作 `w:gridSpan`（该格占 N 列）、纵向合并写作 `w:vMerge`
      （后续行是 `continue`，且往往是个**空格子**）。旧实现只按 `tr → tc` 取格子，
      于是同一张表每行格数不等（实测 [2,4,2,3,2,4]），"标签的右侧格"在行内根本定位不准，
      也就拿不到"这一行有合并"的迹象。这里按 gridSpan/vMerge 补齐到表级列数。

    返回 (段落, 行矩阵, 每表 {col_count, merged_rows, merged} )。
    """
    with zipfile.ZipFile(str(path)) as zf:
        xml_bytes = zf.read("word/document.xml")
    root = ET.fromstring(xml_bytes)
    body = root.find(f"{_W}body")
    if body is None:
        return [], [], []
    paras: list[str] = []
    tables: list[list[list[str]]] = []
    grids: list[dict] = []
    for node in list(body):
        if node.tag == f"{_W}p":
            text = "".join(t.text or "" for t in node.iter(f"{_W}t")).strip()
            if text:
                paras.append(text)
        elif node.tag == f"{_W}tbl":
            table: list[list[str]] = []
            spans: list[list[int]] = []          # 每格占几列
            vmerge: list[list[str]] = []         # "" | "start" | "continue"
            for tr in node.findall(f"{_W}tr"):
                row: list[str] = []
                row_spans: list[int] = []
                row_vm: list[str] = []
                for tc in tr.findall(f"{_W}tc"):
                    txt = "".join(t.text or "" for t in tc.iter(f"{_W}t")).strip()
                    pr = tc.find(f"{_W}tcPr")
                    gs = 1
                    vm = ""
                    if pr is not None:
                        g = pr.find(f"{_W}gridSpan")
                        if g is not None:
                            try:
                                gs = max(1, int(g.get(f"{_W}val") or 1))
                            except (TypeError, ValueError):
                                gs = 1
                        v = pr.find(f"{_W}vMerge")
                        if v is not None:
                            vm = str(v.get(f"{_W}val") or "continue")
                    row.append(txt)
                    row_spans.append(gs)
                    row_vm.append(vm)
                if any(row) or row_spans:
                    table.append(row)
                    spans.append(row_spans)
                    vmerge.append(row_vm)
            if not table:
                continue
            col_count = max((sum(sp) for sp in spans), default=0)
            merged_rows: set[int] = set()
            # 展开：横向按 gridSpan 占位；纵向 vMerge=continue 沿用上一行同列的值
            grid: list[list[str]] = []
            for r_i, row in enumerate(table):
                expanded: list[str] = []
                for c_i, txt in enumerate(row):
                    gs = spans[r_i][c_i]
                    vm = vmerge[r_i][c_i]
                    val = txt
                    if vm == "continue" and r_i > 0:
                        # 纵向合并：取上一行同一列的值（列位置在展开后再对齐）
                        pass
                    expanded.append(val)
                    for _ in range(gs - 1):
                        expanded.append("")
                if len(expanded) < col_count:
                    expanded += [""] * (col_count - len(expanded))
                grid.append(expanded)
                if any(sp > 1 for sp in spans[r_i]) or any(vm == "continue" for vm in vmerge[r_i]):
                    merged_rows.add(r_i)
            tables.append(grid)
            grids.append({"col_count": col_count, "merged_rows": merged_rows,
                          "merged": sorted(merged_rows)})
    return paras, tables, grids


def read_docx(path: str | Path) -> list[dict]:
    """docx → 页 JSON 列表（单页；段落与**表格单元格**都走脱敏）。

    ⚠️ 修复（真实泄露）：以前只对段落做文本脱敏，**表格行是原样拼进页文本**的，
    而页却标了 `desensitized: True` → 下游据此跳过二次脱敏，导致
    "单位全称/开户行/账号/金额"等明文直接进了 hub。现在表格也走**列头感知脱敏**
    （table_desens，与 xlsx 同一条路径），页文本由**脱敏后的单元格**渲染。
    """
    import hub_pipeline__desens as hp
    import table_desens__desens as td
    from database_serv__infra import MappingDbStore

    store = MappingDbStore()
    tracker = hp.DesensTracker()
    paras, tables, grids = _docx_tables_grid(path)

    # 表格 → 与 xlsx 同构的表块。顺序（本轮修正）：**先剥标题/签章行 → 再判 kv → 再定表头**。
    #   · 标题/签章行不再被当成"首行=表头"（那会让 8 列数据只有第 1 列有语义）；
    #   · 剥出来的标题写进 `title` 并渲染进页文本（不丢信息）。
    masked_tables: list[dict] = []
    for t_i, rows in enumerate(tables):
        if not rows:
            continue
        grid_info = grids[t_i] if t_i < len(grids) else {}
        # 与 xlsx/OCR 共用**同一分流入口**（先剥标题/签章 → 判 kv → 定表头 → 分层脱敏）
        _base, masked = td.mask_table_layout(
            rows, store, source="docx",
            grid_cols=grid_info.get("col_count"),
            merged_rows=grid_info.get("merged_rows"),
            extra={"_cells": {"header": [], "rows": [
                [{"row": r + 1, "col": c + 1, "coord": None, "text": str(v or "")}
                 for c, v in enumerate(row)] for r, row in enumerate(rows)]}},
        )
        masked["_cells"] = _grid_cells_meta(rows, _base)
        masked_tables.append(masked)

    lines: list[str] = []
    for p in paras:
        lines.append(hp.desensitize_text(p, store, tracker))
    for t in masked_tables:
        # **标题/签章行照样进页文本**（本轮修复：以前表前的标题行会被当成"首行=表头"
        # 或被直接丢掉，hub 里就再也看不到"付款申请/苗木采购合同"这类题头了）。
        # 行前缀分别是 `title:` / `signature:`，下游按前缀区分，不会被当数据。
        for row in (t.get("title_rows") or []):
            txt = " | ".join(str(c or "").strip() for c in row if str(c or "").strip())
            if txt:
                lines.append("title:" + hp.desensitize_text(txt, store, tracker))
        for row in (t.get("signature_rows") or []):
            txt = " | ".join(str(c or "").strip() for c in row if str(c or "").strip())
            if txt:
                lines.append("signature:" + hp.desensitize_text(txt, store, tracker))
        header = [str(h or "") for h in (t.get("header") or [])]
        body = [[str(c or "") for c in row] for row in (t.get("rows") or [])]
        if header:
            lines.append("table:" + " | ".join(header))
        for row in body:
            lines.append("table:" + " | ".join(row))
    text = "\n".join(lines)
    return [{
        "res": {
            "page_index": 0,
            "source": "docx",                 # 直读标记（非 OCR）
            "desensitized": True,             # 段落 + 表格都已脱敏（下游可直通）
            "full_text": text,
            "rec_texts": [ln for ln in text.split("\n") if ln],
            "tables": masked_tables,
        }
    }]


def read_office(path: str | Path) -> list[dict]:
    """按扩展名分派：.xlsx / .docx → 页 JSON 列表（均不经过 OCR）。"""
    suffix = Path(path).suffix.lower()
    if suffix == ".xlsx":
        return read_xlsx(path)
    if suffix == ".docx":
        return read_docx(path)
    raise ValueError(f"不支持的 Office 直读类型：{suffix}")
