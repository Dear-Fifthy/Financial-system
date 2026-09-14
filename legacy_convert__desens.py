"""旧版 Office（.doc / .xls）转换：**只读源文件**，转换产物写入本项目临时目录。

原则（按需求）：
  · 绝不修改 / 移动 / 删除 / 重命名源文件——只以只读方式打开；
  · 转换失败 → 返回 None + 明确**提示文案**（让用户另存为 .docx/.xlsx 再拖入）；
  · 转换路径按优先级尝试：
      1) LibreOffice / soffice 无界面转换（若已安装）
      2) Windows COM（Word.Application / Excel.Application，若装了 Office）
    两者都没有 → 直接给出提示，不猜内容。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONVERT_DIR = BASE_DIR / "logs" / "legacy_convert"
LEGACY_EXTENSIONS = {".doc", ".xls"}
_TARGET_EXT = {".doc": ".docx", ".xls": ".xlsx"}

FAIL_HINT = (
    "无法转换旧版 Office 文件（{name}）：本机未检测到 LibreOffice 或 Word/Excel。\n"
    "请在 Office 中『另存为』.docx / .xlsx 后再拖入；**源文件未被修改**。"
)


def _soffice_path() -> str | None:
    """查找 LibreOffice 可执行文件（soffice / libreoffice）。"""
    for name in ("soffice", "libreoffice", "soffice.exe", "libreoffice.exe"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _convert_via_soffice(src: Path, out_dir: Path, target_ext: str) -> Path | None:
    exe = _soffice_path()
    if not exe:
        return None
    try:
        subprocess.run(
            [exe, "--headless", "--norestore", "--convert-to", target_ext.lstrip("."),
             "--outdir", str(out_dir), str(src)],
            capture_output=True, timeout=180,
        )
    except Exception:
        return None
    out = out_dir / (src.stem + target_ext)
    return out if out.exists() else None


def _convert_via_com(src: Path, out_dir: Path, target_ext: str) -> Path | None:
    """用 PowerShell 驱动 Office COM（无需 pywin32）：只读打开 → 另存为新格式 → 退出。

    `.doc` 用 Word.Application（SaveAs2 格式 16 = docx）；
    `.xls` 用 Excel.Application（SaveAs 格式 51 = xlsx）。
    """
    if target_ext == ".docx":
        script = f"""
$ErrorActionPreference='Stop'
$app = New-Object -ComObject Word.Application
try {{
  $app.Visible = $false; $app.DisplayAlerts = 0
  $doc = $app.Documents.Open('{src}', $false, $true)   # ReadOnly:=true（源文件只读）
  $doc.SaveAs2('{out_dir / (src.stem + target_ext)}', 16)
  $doc.Close($false)
}} finally {{ $app.Quit() }}
"""
    else:
        script = f"""
$ErrorActionPreference='Stop'
$xl = New-Object -ComObject Excel.Application
try {{
  $xl.Visible = $false; $xl.DisplayAlerts = $false
  $wb = $xl.Workbooks.Open('{src}', 0, $true)          # ReadOnly:=true
  $wb.SaveAs('{out_dir / (src.stem + target_ext)}', 51)
  $wb.Close($false)
}} finally {{ $xl.Quit() }}
"""
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, timeout=180)
    except Exception:
        return None
    out = out_dir / (src.stem + target_ext)
    return out if out.exists() else None


def convert_legacy(src: str | Path) -> tuple[Path | None, str]:
    """把 .doc/.xls 转成 .docx/.xlsx（产物在 logs/legacy_convert）。

    返回 (转换后路径 或 None, 说明/提示)。**源文件保持原样。**
    """
    p = Path(src)
    ext = p.suffix.lower()
    if ext not in LEGACY_EXTENSIONS:
        return None, f"不是旧版 Office 文件：{p.name}"
    target_ext = _TARGET_EXT[ext]
    try:
        CONVERT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return None, f"无法创建转换目录（{exc}）。{FAIL_HINT.format(name=p.name)}"

    for fn, label in ((_convert_via_soffice, "LibreOffice"), (_convert_via_com, "Office COM")):
        out = fn(p, CONVERT_DIR, target_ext)
        if out is not None:
            return out, f"已用 {label} 转换为 {out.name}（源文件未改动）"
    return None, FAIL_HINT.format(name=p.name)
