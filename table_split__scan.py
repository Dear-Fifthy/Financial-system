"""L0 表格切分：把"表格式来源"切成统一结构的表块（tables）。

支持两类来源：
  1. `.xlsx`（openpyxl，含样式）：一张 sheet 可能有多个表——
     **主判据：连续空白行**（默认 ≥2 行）；
     **辅判据：表头合并单元格 / 底纹填充 / 列数突变**（用于确认与标注）。
  2. hub 页文本中的表格块：OCR 结果里的 `table:` 标签行，按连续行归为一块
     （没有单元格结构，只有原始文本行，供后续 L1 抽取使用）。

统一表块结构：
    {
      "source": "xlsx" | "hub_text",
      "sheet": str, "table_index": int,
      "start_row": int, "end_row": int, "col_count": int,
      "header": [str, ...],                 # 首行（表头）
      "header_merged": bool,                # 表头是否含合并单元格（辅判据）
      "style_tags": [str, ...],             # 表头样式（底纹/填充）标记
      "rows": [[str, ...], ...],            # 数据行（不含表头）
      "merges": [str, ...],                 # 该表范围内的合并区域（如 "A1:B1"）
    }
本模块只做切分（纯结构），脱敏在 table_desens。
"""
from __future__ import annotations

from pathlib import Path

DEFAULT_BLANK_RUN = 2      # 连续空白行数达到该值 → 视为表之间的分隔（主判据）
MAX_HEADER_SCAN = 3        # 跳过表前的标题行时最多向上扫描几行（辅）


# =========================================================
# xlsx
# =========================================================
_DATE_FMT_HINT = ("yy", "年", "月", "日", "m/d", "d/m", "mm-dd")


def _cell_text(value, cell=None) -> str:
    """单元格 → 文本。**日期序列号要还原成日期**。

    踩坑（实测）：`日期` 列存的是 Excel 序列号（`46266`），`str(value)` 直接把它
    写成 "46266" → 事实层取到的"日期"是一个无意义整数，L4 拿它当日期用就错了。
    openpyxl 只在单元格是日期格式时才返回 datetime；有些表（如本项目的金额表）
    日期列用的是数字格式，只能靠 `number_format` + `from_excel` 还原。
    """
    if value is None:
        return ""
    fmt = str(getattr(cell, "number_format", "") or "").lower()
    is_date_fmt = any(h in fmt for h in ("yy", "年", "月", "日", "m/d", "d/m", "mm-dd"))
    if isinstance(value, (int, float)) and not isinstance(value, bool) and is_date_fmt:
        try:
            from openpyxl.utils.datetime import from_excel

            d = from_excel(value)
            if getattr(d, "hour", 0) or getattr(d, "minute", 0):
                return d.strftime("%Y-%m-%d %H:%M")
            return d.strftime("%Y-%m-%d")
        except Exception:
            pass
    return str(value).strip()


def _row_values(row_cells) -> list[str]:
    return [_cell_text(c.value, c) for c in row_cells]


def split_xlsx(path: str | Path, *, blank_run: int = DEFAULT_BLANK_RUN) -> list[dict]:
    """把一个 xlsx 的所有 sheet 切成表块列表（多表检测见模块说明）。"""
    from openpyxl import load_workbook

    wb = load_workbook(str(path), data_only=True)
    tables: list[dict] = []
    for ws in wb.worksheets:
        merges = [str(rng) for rng in ws.merged_cells.ranges]
        merge_objs = list(ws.merged_cells.ranges)
        blocks: list[list[tuple[int, list[str], list]]] = []
        cur: list[tuple[int, list[str], list]] = []
        blanks = 0
        for row_idx, row_cells in enumerate(ws.iter_rows(), start=1):
            values = _row_values(row_cells)
            if all(v == "" for v in values):
                blanks += 1
                if cur and blanks >= blank_run:
                    blocks.append(cur)
                    cur = []
                    blanks = 0
                continue
            blanks = 0
            cur.append((row_idx, values, list(row_cells)))
        if cur:
            blocks.append(cur)

        for t_idx, blk in enumerate(blocks, start=1):
            start_row = blk[0][0]
            end_row = blk[-1][0]
            header = blk[0][1]
            # 表头可能前有标题行（如"2026年台账"）：辅判据——若首行只有 1 个非空单元格
            # 而下一行有多个，则把下一行当表头（最多看 MAX_HEADER_SCAN 行）。
            hdr_at = 0
            for k in range(0, min(MAX_HEADER_SCAN, len(blk) - 1)):
                nonempty = sum(1 for v in blk[k][1] if v)
                nxt = sum(1 for v in blk[k + 1][1] if v)
                if nonempty <= 1 and nxt >= 2:
                    hdr_at = k + 1
                else:
                    break
            header = blk[hdr_at][1]
            header_row = blk[hdr_at][0]
            data_rows = [v for _i, v, _c in blk[hdr_at + 1:]]
            col_count = max((len([v for v in r if v]) for r in ([header] + data_rows)), default=0)
            # 辅判据：表头合并单元格 + 底纹样式
            in_range_merges = [
                m for m in merge_objs
                if m.min_row >= start_row and m.max_row <= end_row
            ]
            header_merged = any(
                m.min_row <= header_row <= m.max_row and m.max_col > m.min_col
                for m in in_range_merges
            )
            # 本轮新增：把**整块行**（含表头之前的标题行）与"被合并区覆盖的行"一并交出去，
            # 由 `table_desens.layout_of` 统一"先剥标题/签章 → 再判 kv → 再定表头"。
            # 旧实现只交 header/rows，标题行在 split 阶段就被悄悄丢掉（hub 里再也找不到题头）。
            block_rows = [list(v) for _i, v, _c in blk]
            block_merged_rows: set[int] = set()
            for _m in in_range_merges:
                if _m.max_col > _m.min_col:
                    for rr in range(_m.min_row, _m.max_row + 1):
                        rel = rr - start_row
                        if 0 <= rel < len(block_rows):
                            block_merged_rows.add(rel)
            style_tags: list[str] = []
            for c in blk[hdr_at][2]:
                fill = getattr(c, "fill", None)
                ptype = getattr(fill, "patternType", None)
                if ptype not in (None, "none"):
                    rgb = getattr(getattr(fill, "fgColor", None), "rgb", None)
                    style_tags.append(str(rgb or ptype))
            # 单元格元数据（行列号 + Excel 坐标），供单元格级坐标锚点使用
            def _meta(cell, row_i: int) -> dict:
                return {"row": row_i, "col": cell.column, "coord": cell.coordinate,
                        "text": _cell_text(cell.value, cell)}
            cells_meta = {
                "header": [_meta(c, header_row) for c in blk[hdr_at][2]],
                "rows": [[_meta(c, blk[hdr_at + 1 + i][0]) for c in blk[hdr_at + 1 + i][2]]
                         for i in range(len(blk) - hdr_at - 1)],
            }
            tables.append({
                "source": "xlsx",
                "sheet": ws.title,
                "table_index": t_idx,
                "start_row": start_row,
                "end_row": end_row,
                "header_row": header_row,
                "col_count": col_count,
                "header": header,
                "header_merged": bool(header_merged),
                "style_tags": style_tags,
                "rows": data_rows,
                "merges": [str(m) for m in in_range_merges],
                "_cells": cells_meta,
                # 供 layout_of 使用：整块行（含标题行）+ 合并覆盖行（块内相对下标）
                "block_rows": block_rows,
                "block_merged_rows": sorted(block_merged_rows),
                "block_cells": [[_meta(c, blk[i][0]) for c in blk[i][2]]
                                for i in range(len(blk))],
                "grid_cols": col_count,
                "title_rows": [list(v) for _i, v, _c in blk[:hdr_at]],
            })
    return tables


# =========================================================
# hub 页文本中的表格块（无单元格结构，按 table: 标签行归并）
# =========================================================
def split_hub_text_tables(pages: list[str]) -> list[dict]:
    """从 hub 页文本里切出 `table:` 标签组成的表格块（跨页不合并，按块记录页码）。"""
    tables: list[dict] = []
    lines: list[tuple[int, str]] = []
    for page_idx, page in enumerate(pages, start=1):
        for line in (page or "").split("\n"):
            lines.append((page_idx, line.rstrip()))
    buf: list[tuple[int, str]] = []

    def flush() -> None:
        nonlocal buf
        if buf:
            tables.append({
                "source": "hub_text",
                "sheet": "",
                "table_index": len(tables) + 1,
                "page_from": buf[0][0],
                "page_to": buf[-1][0],
                "header": [],
                "header_merged": False,
                "style_tags": [],
                "rows": [[t] for _p, t in buf],
                "raw_lines": [t for _p, t in buf],
            })
        buf = []

    for page_idx, line in lines:
        if line.lstrip().startswith("table:"):
            buf.append((page_idx, line))
        else:
            flush()
    flush()
    return tables


def split_tables(path: str | Path, *, blank_run: int = DEFAULT_BLANK_RUN) -> list[dict]:
    """按扩展名分派：.xlsx → split_xlsx；其它 → 空列表（由调用方决定后续）。"""
    p = Path(path)
    if p.suffix.lower() == ".xlsx":
        return split_xlsx(p, blank_run=blank_run)
    return []
