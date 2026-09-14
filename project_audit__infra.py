# -*- coding: utf-8 -*-
"""项目体检（可反复跑）：临时文件 / 空文件 / 孤儿模块 / gitignore 覆盖 / 凭据泄露 / 数据体积。

为什么需要它：仓库里长过不少"一次性的东西"——
  · 我自己排障留下的 `_tmp_*.py`、空的 `logs/*.log`、Word 锁文件 `~$*.docx`；
  · 工作区（仓库）切换带出来的 `workspaces/_bak/` 归档；
  · 新增目录（`workspaces/`，里面有**每个仓库独立的加密密钥**）忘了进 `.gitignore`。
这些要么该清、要么绝不能入库，靠人记容易漏，所以做成一条命令：

    python project_audit__infra.py            # 人看的报告
    python project_audit__infra.py --json     # 机器读（CI/脚本用）

退出码：0 = 没有需要处理的东西；1 = 有（临时文件/空文件/漏掉的 gitignore 项）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKIP_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
             ".ruff_cache", ".idea", ".vs"}

# 名字里出现这些就是"临时/垃圾"的候选（hub/*.ai.log 是设计产物；KEEP_BACKUPS 里的
# 是刻意留的回滚备份，都不算）
TEMP_NAME_RE = re.compile(r"(^_tmp|_tmp_|^tmp_|\.bak$|\.orig$|~$|\.tmp$|^~\$|"
                          r"_bak_|_bak$|copy \d|副本|\.swp$|\.pyc$)", re.IGNORECASE)

# 名字像备份、但**故意保留**的（AGENTS §2：破坏性操作要先备份、可回滚）
KEEP_BACKUPS = ("logs/workspace/", "logs/analysis/")

# 必须被 .gitignore 挡住的东西（存在才算问题）
MUST_IGNORE = [
    ".venv/", "__pycache__/", ".env", "hub_encryption.key", "hub/", "input/", "output/",
    "logs/", "workspaces/",
    # AGENTS.md：本地协作笔记，2026-09 用户决定不入库（内容只给 AI 看）
    "AGENTS.md",
]


def walk(root: Path = ROOT) -> tuple[list[Path], list[Path]]:
    files: list[Path] = []
    dirs: list[Path] = []
    # logs/cleanup/ 是"清理归档"本身（按设计就放着那些旧文件），不再体检它
    skip_prefixes = [(root / "logs" / "cleanup").resolve()]
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        d = Path(dirpath)
        if any(str(d.resolve()).startswith(str(sp)) for sp in skip_prefixes):
            dirnames[:] = []
            continue
        dirs.append(d)
        files.extend(d / f for f in filenames)
    return files, dirs


def gitignore_lines() -> list[str]:
    p = ROOT / ".gitignore"
    if not p.exists():
        return []
    return [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def ignored_by_gitignore(rel: str, patterns: list[str]) -> bool:
    """极简匹配（够用）：目录前缀、`*` 通配、精确名。"""
    rel = rel.replace("\\", "/")
    for pat in patterns:
        p = pat.rstrip("/")
        if rel == p or rel.startswith(p + "/") or rel.split("/")[-1] == p:
            return True
        if "*" in p:
            rx = "^" + re.escape(p).replace(r"\*", "[^/]*") + "$"
            if re.match(rx, rel) or re.match(rx, rel.split("/")[-1]):
                return True
    return False


# =========================================================
# 凭据反查：`.env` 里的**真口令/真密钥**绝不能出现在任何入库文件里
# ---------------------------------------------------------
# 教训：`init_db.sql`、`database_serv__infra.py` 的兜底默认值、`.env.example`
# 曾同时写着**本机真口令**（三份拷贝都会随提交进版本库）。
# 现在 `init_db.sql` 只留占位符、代码不设兜底口令，这里再上一道自动闸：
#   · 从 .env 取真值（.env 本身被 .gitignore 挡住），在**已跟踪文件**里搜；
#   · 只报"哪个文件第几行命中"，**绝不回显命中内容**——否则体检报告自己成了泄露源。
# =========================================================
SECRET_KEYS = ("APP_DB_PASS", "POSTGRES_ADMIN_PASSWORD", "AI_API_KEY",
               "CHAT_AI_API_KEY", "EDGE_AI_API_KEY", "GRAPH_AI_API_KEY")
INIT_SQL_PASSWORD_TOKEN = "__APP_DB_PASS__"
# 只有这些后缀当文本读（其余如 .png/.pdf 直接跳过）
TEXT_SUFFIXES = {".py", ".sql", ".md", ".bat", ".txt", ".json", ".example", ".ini", ".cfg", ""}
# 像占位符/弱口令的**不当真值**用：否则 "postgres" 这种词会在文档里到处误报
_FALSE_POSITIVE_VALUES = {"postgres", "password", "passwd", "changeme", "placeholder",
                          "localhost", "admin"}


def _looks_like_secret(v: str) -> bool:
    v = v.strip()
    if len(v) < 8 or v.lower() in _FALSE_POSITIVE_VALUES:
        return False
    if v.lower().startswith(("your_", "your-", "<", "xxx", "todo")):
        return False
    # 纯字母且短（8~11 个字符）的多半是普通单词/用户名，不当密钥
    return not (v.isalpha() and len(v) < 12)


def tracked_files() -> list[Path]:
    """git 跟踪的文件（含已提交、但工作区里已被删的；调用方自行判存在）。"""
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True, encoding="utf-8", errors="ignore", timeout=60)
        if out.returncode == 0:
            return [ROOT / ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    except Exception:
        pass
    return [p for p in ROOT.glob("*") if p.is_file()]


def real_secret_values() -> dict[str, str]:
    """从 .env 取真值（只认 SECRET_KEYS 里的项，且"像密钥"的才算）。"""
    env = ROOT / ".env"
    vals: dict[str, str] = {}
    if not env.exists():
        return vals
    for ln in env.read_text(encoding="utf-8", errors="ignore").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k in SECRET_KEYS and _looks_like_secret(v):
            vals[k] = v
    return vals


def secret_leaks() -> list[dict]:
    """入库文件里的真口令 / 硬编码 SQL 口令。返回 [{path, line, kind}]，不含内容。"""
    hits: list[dict] = []
    secrets = real_secret_values()
    for p in tracked_files():
        if not p.exists() or p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "\x00" in text:
            continue
        rel = str(p.relative_to(ROOT))
        for i, line in enumerate(text.splitlines(), 1):
            if any(v in line for v in secrets.values()):
                hits.append({"path": rel, "line": i,
                             "kind": "出现 .env 里的真口令/真密钥"})
            m = re.search(r"PASSWORD\s+'([^']*)'", line)
            if m and m.group(1) != INIT_SQL_PASSWORD_TOKEN:
                hits.append({"path": rel, "line": i,
                             "kind": "硬编码 SQL 口令（应改用占位符注入）"})
    return hits


def module_references(exclude_prefixes: tuple[str, ...] = ()) -> dict[str, list[str]]:
    """顶层模块 -> 提到过它的文件（用来找"谁都不用的模块"）。"""
    mods = [p for p in ROOT.glob("*.py")]
    scan: list[Path] = []
    for pat in ("*.py", "*.md", "*.bat", "*.sql"):
        scan.extend(ROOT.glob(pat))
    for sub in ("sample_docs",):
        d = ROOT / sub
        if d.exists():
            scan.extend(d.rglob("*"))
    texts = {}
    for p in scan:
        if p.is_file() and p.suffix in (".py", ".md", ".bat", ".sql"):
            try:
                texts[p] = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                pass
    out: dict[str, list[str]] = {}
    for m in mods:
        if m.name.startswith(exclude_prefixes):
            continue
        hits = [p.name for p, t in texts.items() if p != m and m.stem in t]
        out[m.name] = sorted(set(hits))
    return out


def report() -> dict:
    files, _dirs = walk()
    patterns = gitignore_lines()

    empty = [str(p.relative_to(ROOT)) for p in files if p.stat().st_size == 0]
    temp: list[dict] = []
    kept_backups: list[str] = []
    for p in files:
        rel = str(p.relative_to(ROOT))
        rel_posix = rel.replace("\\", "/")
        # 刻意保留的回滚备份（AGENTS §2）单独统计，不算"待清理"
        if any(rel_posix.startswith(k) for k in KEEP_BACKUPS) and (
                "backup" in p.name.lower() or "备份" in p.name):
            kept_backups.append(rel)
            continue
        if p.name.endswith(".ai.log"):          # AI 留痕是设计产物，不是垃圾
            continue
        if not TEMP_NAME_RE.search(p.name):
            continue
        temp.append({"path": rel, "size": p.stat().st_size,
                     "why": "名字像临时/备份/锁文件"})
    # 归档目录（workspaces/_bak 这类）
    archives = [str(p.relative_to(ROOT)) for p in (ROOT / "workspaces").glob("_bak*")
                if p.exists()] if (ROOT / "workspaces").exists() else []

    refs = module_references(exclude_prefixes=("_tmp",))
    # 入口脚本（带 `if __name__ == "__main__"`）本来就没人 import，不算孤儿
    orphans = []
    for m, hits in refs.items():
        if hits:
            continue
        text = (ROOT / m).read_text(encoding="utf-8", errors="ignore")
        if '__name__ == "__main__"' in text:
            continue
        orphans.append(m)
    orphans.sort()

    missing_ignores = [x for x in MUST_IGNORE if (ROOT / x.rstrip("/")).exists()
                       and not ignored_by_gitignore(x, patterns)]
    # 不该被忽略的（代码/模板）：误伤检查。
    # 注：`sample_docs/`（合成样例语料）按用户决定**当本地测试脚手架、故意忽略**；
    #     `AGENTS.md`（协作笔记）2026-09 也按用户决定**故意忽略**，故两者都不在此列。
    wrongly_ignored = [x for x in ("init_db.sql", "ui_kit__ui.py", "sample_corpus__infra.py")
                       if ignored_by_gitignore(x, patterns)]

    leaks = secret_leaks()

    dirs_info = {}
    for d in sorted(p for p in ROOT.iterdir() if p.is_dir() and p.name not in SKIP_DIRS):
        n = sum(1 for f in d.rglob("*") if f.is_file())
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        dirs_info[d.name] = {"files": n, "mb": round(size / 1048576, 2)}

    return {
        "files": len(files),
        "empty_files": empty,
        "temp_candidates": temp,
        "kept_backups": kept_backups,
        "archive_dirs": archives,
        "orphan_modules": orphans,
        "module_refs": {k: v for k, v in refs.items() if not v},
        "secret_leaks": leaks,
        "gitignore": {"patterns": patterns, "missing": missing_ignores,
                      "wrongly_ignored": wrongly_ignored},
        "dirs": dirs_info,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="项目体检：临时文件 / 空文件 / 孤儿模块 / gitignore")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--full", action="store_true", help="列出所有候选（默认只列前 15 条）")
    args = ap.parse_args(argv)
    res = report()

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 1 if (res["empty_files"] or res["temp_candidates"]
                     or res["gitignore"]["missing"] or res["secret_leaks"]) else 0

    lim = 10 ** 9 if args.full else 15
    print(f"扫描 {res['files']} 个文件（已排除 .venv/.git/__pycache__）")
    print(f"\n① 空文件：{len(res['empty_files'])} 个")
    for p in res["empty_files"][:lim]:
        print("   ", p)
    print(f"\n② 临时/备份候选：{len(res['temp_candidates'])} 个（不含 hub/*.ai.log 这种设计产物）")
    for t in res["temp_candidates"][:lim]:
        print(f"    {t['path']}  ({t['size']}B)")
    print(f"\n③ 归档目录：{res['archive_dirs'] or '（无）'}")
    print(f"   刻意保留的回滚备份：{len(res['kept_backups'])} 个"
          + (f"（{res['kept_backups'][0]} …）" if res["kept_backups"] else ""))
    print(f"\n④ 谁都不引用的模块：{res['orphan_modules'] or '（无）'}")
    print("   （注：入口脚本如 scanner_entrance__scan.py 本来就没人 import，属正常）")
    print(f"\n⑤ .gitignore 漏项：{res['gitignore']['missing'] or '（无）'}")
    print(f"   误伤（把交付物忽略了）：{res['gitignore']['wrongly_ignored'] or '（无）'}")
    leaks = res["secret_leaks"]
    print(f"\n⑥ 凭据泄露（入库文件里出现 .env 真值 / 硬编码口令）：{len(leaks)} 处"
          + ("（只报位置，不回显内容）" if leaks else ""))
    for t in leaks[:lim]:
        print(f"    {t['path']}:{t['line']}  {t['kind']}")
    print("\n⑦ 顶层目录：")
    for k, v in res["dirs"].items():
        print(f"    {k:14s} {v['files']:5d} 个文件  {v['mb']:7.2f} MB")

    bad = bool(res["empty_files"] or res["temp_candidates"] or res["gitignore"]["missing"]
               or res["secret_leaks"])
    print("\n结论：", "需要处理（见上）" if bad else "干净")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
