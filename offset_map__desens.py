"""脱敏 ↔ 原文 坐标映射。

问题：脱敏会改变文本长度（"深圳市腾讯计算机系统有限公司" → "CO0003"），
因此"脱敏后文本里的位置"不能直接当作"原文位置"——证据链要回原文核对就会错位。

做法（轻量、无需改动脱敏管线）：
    用 difflib 对 (原文, 脱敏后文本) 做对齐，取出所有**相等片段**：
        [[final_start, raw_start, length], ...]
    · `map_span`：脱敏后区间 → 原文区间（必须完整落在某个相等片段内，否则返回 None）；
    · `map_pos` ：单点映射。
    被替换/插入的区间（掩码、编号）本身在原文里对应的是"被脱敏的真实值"，
    映射时返回 None 或由调用方按"锚点边界"处理；本模块只报告确定可映射的部分。
"""
from __future__ import annotations

import difflib


class OffsetMap(list):
    """相等片段列表 [[final_start, raw_start, length], …]，附带两侧文本长度。

    是 list 的子类：与普通列表可比较、可直接 json 序列化；
    额外携带 raw_len / final_len，用于"结尾被整体替换"时推断原文右边界
    （单看片段列表无法知道原文尾部还剩多少字符）。
    """

    raw_len: int = 0
    final_len: int = 0


def build_map(raw: str, final: str) -> OffsetMap:
    """构造映射：返回 OffsetMap([[final_start, raw_start, length], …])（相等片段）。"""
    raw = raw or ""
    final = final or ""
    sm = difflib.SequenceMatcher(None, raw, final, autojunk=False)
    segs = OffsetMap()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal" and i2 > i1:
            segs.append([j1, i1, i2 - i1])
    segs.raw_len = len(raw)
    segs.final_len = len(final)
    return segs


def map_span(segs: list[list[int]], start: int, end: int) -> tuple[int, int] | None:
    """脱敏后区间 [start,end) → 原文区间；不能完整映射时返回 None。"""
    if start is None or end is None or end < start:
        return None
    for f_start, r_start, length in segs:
        if start >= f_start and end <= f_start + length:
            off = start - f_start
            return r_start + off, r_start + off + (end - start)
    return None


def map_pos(segs: list[list[int]], pos: int) -> int | None:
    """脱敏后单点 → 原文位置；不在相等片段内返回 None。"""
    for f_start, r_start, length in segs:
        if f_start <= pos < f_start + length:
            return r_start + (pos - f_start)
    return None


def covered(segs: list[list[int]]) -> int:
    """被"逐字相同"片段覆盖的原文/脱敏文本长度（两者一致）。"""
    return sum(length for _f, _r, length in segs)


def map_span_loose(
    segs: list[list[int]], start: int, end: int, raw_len: int | None = None
) -> tuple[int, int, bool] | None:
    """宽松映射：返回 (raw_start, raw_end, exact)。

    · 区间完整落在某个相等片段内 → exact=True，坐标逐字对应；
    · 区间跨过（或被整个替换成）掩码/编号区段 → 用相邻相等片段在**原文**里的
      边界包夹出一个"包围盒"，exact=False：表示"证据必定落在这段原文里"。
      这对"单元格值被整体替换成编号"的场景是必需的——严格映射会返回 None，
      证据链就断了；包围盒能保证仍可回原文核对。
    · raw_len：原文长度（缺省取 segs.raw_len），用于"结尾被整体替换"时定位右边界。
    """
    if start is None or end is None or end < start:
        return None
    strict = map_span(segs, start, end)
    if strict is not None:
        return strict[0], strict[1], True
    if not segs:
        return None
    total_raw = raw_len if raw_len is not None else getattr(segs, "raw_len", None)
    raw_start: int | None = None
    raw_end: int | None = None
    for f_start, r_start, length in segs:
        if f_start + length <= start:
            raw_start = r_start + length
        if f_start >= end and raw_end is None:
            raw_end = r_start
    if raw_start is None:                     # 起点在被替换区段之前
        f0, r0, _l0 = segs[0]
        raw_start = max(0, r0 - (f0 - start))
    if raw_end is None:                       # 终点在被替换区段之后
        f_last, r_last, l_last = segs[-1]
        if total_raw is not None:
            # 末尾没有相等片段，说明"剩余脱敏文本"对应"剩余原文"（含被替换区段）
            # → 右边界直接取原文末尾，避免把原文真值切短（切短会导致核对取到残缺值）
            raw_end = max(r_last + l_last, int(total_raw))
        else:
            raw_end = r_last + l_last + max(0, end - (f_last + l_last))
    if raw_end < raw_start:
        raw_end = raw_start
    return raw_start, raw_end, False
