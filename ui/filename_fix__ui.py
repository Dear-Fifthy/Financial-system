"""文件名乱码修复（纯逻辑，无 Qt）—— 配合"扫描/修复"切换模式使用。

约束（对应需求）：
  1. 只处理**文件**的名字；文件夹只用于遍历（进入），**目录名绝不改**；
  2. 递归遍历输入文件夹的所有子目录；
  3. **后缀（最后一个 . 之后）原样保留，绝不修改**；只可能改"主名"部分；
  4. **完全不打开/读取文件内容**，只对文件名做字符串级处理；
  5. 其余位置与内容一概不动（原地改名，不复制、不移动、不改结构）；
  6. 修复前必须判定为"乱码"（suspicion 门槛），并做 GBK/CP936 ↔ UTF-8
     双向解码验证 + 合理性校验，避免误伤正常中文名；
  7. 返回"待改列表"供 UI **预览确认**后才执行（perform_renames）。

典型场景：文件名本是 UTF-8（或 GBK）中文，被错误按 GBK/UTF-8 解码存储成乱码
（如 "你好合同" → "浣犲ソ鍚堝悓"）。修复方向尝试两种并取合理者：
  方向1：乱码.encode("cp936") → decode("utf-8")   （UTF-8 内容被 GBK 误读）
  方向2：乱码.encode("utf-8") → decode("gbk")      （GBK 内容被 UTF-8 误读）
"""
from __future__ import annotations

import os

_REPL = "\ufffd"

# 民间典型乱码样例（utf-8 ↔ gbk 互误读产物），用于生成"可疑字符集"兜底
_FOLKLORE_MOJI = (
    "锟斤拷烫烫屯屯锘匡拷锘库�" "涓浗浣犲ソ鎴戠殑鏄痑閿欒鐨勮瘽" "鍟婂憖鍛€鍏卞拰鍝佺墝"
)


def _gen_moji_samples() -> list[str]:
    """动态生成测试短语在两种误读下的乱码形态（保证怀疑集覆盖常见字）。"""
    out: list[str] = []
    for phrase in ("你好合同", "测试文件", "项目管理", "付款凭证", "深圳平安", "银行回单", "财务系统"):
        try:
            out.append(phrase.encode("utf-8").decode("gbk"))  # UTF-8 被当 GBK 读
        except Exception:
            pass
        try:
            out.append(phrase.encode("gbk").decode("utf-8"))  # GBK 被当 UTF-8 读
        except Exception:
            pass
    return out


_MOJI_CHARS: frozenset[str] = frozenset(
    _FOLKLORE_MOJI + "".join(_gen_moji_samples())
)


def looks_garbled(name: str) -> bool:
    """乱码怀疑门槛：含替换符 � 或含"乱码特征字符"。保守设计——不确定就不动。"""
    return (_REPL in name) or any(c in _MOJI_CHARS for c in name)


def split_stem_ext(name: str) -> tuple[str, str]:
    """拆主名与后缀（含点）。后缀 = 最后一个 '.' 及其后，绝不参与修复。"""
    idx = name.rfind(".")
    if idx > 0:
        return name[:idx], name[idx:]
    return name, ""


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf" for ch in s)


def _decode_candidates(stem: str) -> list[tuple[str, str]]:
    """尝试两种方向的解码恢复，返回 [(候选主名, 方向说明)]（去重、合法性过滤）。

    合法性：候选非空、不同于原名、无 �/乱码特征、含 CJK 字（修复目标是中文名）。
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    attempts = (
        ("cp936→utf8", lambda s: s.encode("cp936").decode("utf-8")),
        ("utf8→gbk", lambda s: s.encode("utf-8").decode("gbk")),
    )
    for label, fn in attempts:
        try:
            cand = fn(stem)
        except Exception:
            continue
        cand = cand.strip()
        if not cand or cand == stem:
            continue
        if _REPL in cand or any(c in _MOJI_CHARS for c in cand):
            continue
        if not _has_cjk(cand):
            continue
        if cand not in seen:
            seen.add(cand)
            out.append((cand, label))
    return out


def propose_fix(filename: str) -> dict | None:
    """对单个文件名给出修复建议；无法/无需修复返回 None。

    返回 {"old": 原名, "new": 修复名(后缀原样), "reason": 说明}
    """
    stem, ext = split_stem_ext(filename)
    if not stem:
        return None
    if not looks_garbled(stem):
        return None  # 门槛：不像是乱码，绝不动（防误伤正常中文名）
    candidates = _decode_candidates(stem)
    if not candidates:
        return None  # 解不出合法中文（可能字节已丢失，如 �），不动
    new_stem = candidates[0][0]
    return {
        "old": filename,
        "new": new_stem + ext,  # 后缀原样保留
        "reason": f"乱码修复（{candidates[0][1]}）",
    }


# 默认跳过的目录：隐藏目录与巨型依赖/系统目录（防止误拖整个磁盘/项目根时
# 遍历 node_modules/.venv/.git 等数万文件；如需扫描隐藏目录后续可加开关）
SKIP_DIR_NAMES: frozenset[str] = frozenset({
    ".git", ".svn", ".hg", "__pycache__", ".venv", "venv", "env",
    "node_modules", "site-packages", "dist", "build", "target",
    "$RECYCLE.BIN", "System Volume Information", ".Trash",
    ".idea", ".vscode", ".vs",
})


def _prune_dirnames(dirnames: list[str]) -> list[str]:
    """原地过滤子目录名（隐藏目录或常见巨型目录不进）；返回被跳过的名单。"""
    skipped: list[str] = []
    keep: list[str] = []
    for d in dirnames:
        if d.startswith(".") or d in SKIP_DIR_NAMES:
            skipped.append(d)
        else:
            keep.append(d)
    dirnames[:] = keep
    return skipped


def scan_folder(
    root: str,
    progress: "callable[[int], None] | None" = None,
    cancel: "callable[[], bool] | None" = None,
) -> tuple[list[dict], list[str]]:
    """递归扫描（只检查文件名，不读内容、不改目录名）。

    返回 (items, notes)：
      items: [{"path": 完整路径, "old":…, "new":…, "reason":…}, …]
      notes: 过程说明/跳过原因（含被跳过的目录名、用户取消）
    参数：
      progress: 每处理完一个目录回调 progress(累计已检查文件数)；
      cancel:   每处理一个目录回调，返回 True 则中止扫描（返回已收集部分）。
    本函数不做任何文件内容读取。
    """
    items: list[dict] = []
    notes: list[str] = []
    checked = 0
    for dirpath, dirnames, filenames in os.walk(root):
        if cancel is not None and cancel():
            notes.append("扫描已由用户取消（返回已发现部分）。")
            break
        skipped = _prune_dirnames(dirnames)
        for s in skipped:
            notes.append(f"跳过目录（隐藏/依赖/系统）：{os.path.join(dirpath, s)}")
        per_dir: list[dict] = []
        existing: set[str] = set()
        for fn in filenames:
            existing.add(fn)
            checked += 1
            fix = propose_fix(fn)
            if fix is not None:
                per_dir.append({
                    "path": os.path.join(dirpath, fn),
                    "old": fix["old"],
                    "new": fix["new"],
                    "reason": fix["reason"],
                })
        if per_dir:
            # 同一目录内的新名做唯一化（冲突自动加 (n) 后缀）
            _resolve_dir_conflicts(per_dir, existing)
            items.extend(per_dir)
        if progress is not None:
            progress(checked)
    return items, notes


def _resolve_dir_conflicts(items_in_dir: list[dict], existing_names: set[str]) -> None:
    """让本目录内所有新名唯一：相对 existing(原名集合) 与彼此不冲突。"""
    names = {os.path.normcase(n) for n in existing_names}
    for item in items_in_dir:
        base_stem, ext = split_stem_ext(item["new"])
        cand = item["new"]
        n = 1
        # 与自己旧名相同也视为占位（防止改名后自己仍冲突，虽然 propose 已排除）
        while os.path.normcase(cand) in names:
            cand = f"{base_stem}({n}){ext}"
            n += 1
        item["new"] = cand
        names.add(os.path.normcase(cand))


def perform_renames(items: list[dict], report_lines: list[str] | None = None) -> tuple[int, list[str]]:
    """执行原地改名（只改文件名本身，不移动位置、不读内容）。

    返回 (成功数, 错误列表)。report_lines 若提供则追加"原名 -> 新名"行。
    """
    ok_count = 0
    errors: list[str] = []
    for item in items:
        old_path = item["path"]
        new_path = os.path.join(os.path.dirname(old_path), item["new"])
        try:
            os.rename(old_path, new_path)
            ok_count += 1
            if report_lines is not None:
                report_lines.append(f"{old_path} -> {new_path}  ({item['reason']})")
        except Exception as exc:
            errors.append(f"{old_path}: {exc}")
    return ok_count, errors
