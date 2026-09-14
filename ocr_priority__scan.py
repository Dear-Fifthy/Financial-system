"""扫描优先级：**需要 OCR 转文字的 PDF/图片排到最后**，并给出"为什么要 OCR"的原因。

为什么这样排（需求）：图片/扫描件 OCR 又慢又吃 GPU，先跑"能直接提文字"的文件
（docx/xlsx、有文字层的 PDF、.doc/.xls 转换后直读）能最快出结果；等这些全部处理完，
再**询问用户是否要 OCR**，同意后才释放内存/显存、集中做 OCR。

⚠️ 只改**处理顺序**，不改文件位置：不移动、不改名、不改源文件夹结构；
hub 产物路径仍由"源文件夹相对路径 + 文件名"决定（与队列顺序无关）。

判定方式（不需要真的 OCR，纯本地探测，毫秒级）：
  · `.png/.jpg/.jpeg/.bmp/.tif/.tiff/.webp` → 必须 OCR（图片）
  · `.pdf` → 用 PyMuPDF 检查每页是否有可提取文字（`split_pdf_pages`）：
        有文字层的页 → 直读；全是"无文字 + 有图"的页 → 扫描件，需要 OCR
  · `.docx/.xlsx` → 直读，不 OCR
  · `.doc/.xls` → 先转换再直读（转换失败时会报错，不算 OCR 类）
"""
from __future__ import annotations

from pathlib import Path

__all__ = [
    "IMAGE_EXTENSIONS",
    "OCR_REASON_IMAGE",
    "OCR_REASON_SCANNED_PDF",
    "needs_ocr",
    "partition_paths",
    "describe",
    "order_for_processing",
]

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
OCR_REASON_IMAGE = "图片文件：没有文字层，必须 OCR 转文字"
OCR_REASON_SCANNED_PDF = "PDF 扫描件：页面无可提取文字，需要 OCR 转文字"
NO_OCR_PDF = "PDF 有文字层：直接提取文字，不需要 OCR"
NO_OCR_OFFICE = "Office 直读（xlsx/docx）：不经过 OCR"
NO_OCR_LEGACY = "旧版 Office（.doc/.xls）：转换后直读，不经过 OCR"


def needs_ocr(path: str | Path) -> tuple[bool, str]:
    """(是否需要 OCR, 原因)。探测失败时**按需要 OCR 处理**（排到最后交用户决定）。"""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return True, OCR_REASON_IMAGE
    if suffix == ".pdf":
        try:
            from pdf_native_extractor__scan import split_pdf_pages

            kinds = split_pdf_pages(p)
            if not kinds:
                return True, "PDF 无有效页：无法提取文字，按需要 OCR 处理"
            native = sum(1 for _i, is_native in kinds if is_native)
            if native == 0:
                return True, f"{OCR_REASON_SCANNED_PDF}（{len(kinds)} 页全为图像）"
            if native < len(kinds):
                return True, (f"PDF 混合件：{len(kinds) - native}/{len(kinds)} 页无文字层，"
                              f"需要 OCR 补齐")
            return False, f"{NO_OCR_PDF}（{len(kinds)} 页）"
        except Exception as exc:
            return True, f"PDF 解析失败（{type(exc).__name__}）：按需要 OCR 处理"
    if suffix in (".docx", ".xlsx"):
        return False, NO_OCR_OFFICE
    if suffix in (".doc", ".xls"):
        return False, NO_OCR_LEGACY
    return True, f"未知类型 {suffix or '(无扩展名)'}：按需要 OCR 处理"


def describe(paths: list[str | Path]) -> list[dict]:
    """给每个文件标注 (是否需 OCR, 原因)，保持输入顺序。"""
    out: list[dict] = []
    for raw in paths:
        p = Path(raw)
        ocr, reason = needs_ocr(p)
        out.append({"path": p, "name": p.name, "needs_ocr": ocr, "reason": reason})
    return out


def partition_paths(paths: list[str | Path]) -> tuple[list[Path], list[Path]]:
    """拆成 (可直接处理, 需要 OCR)；两组内部都保持原顺序。"""
    direct: list[Path] = []
    ocr: list[Path] = []
    for item in describe(paths):
        (ocr if item["needs_ocr"] else direct).append(Path(item["path"]))
    return direct, ocr


def order_for_processing(paths: list[str | Path]) -> list[Path]:
    """排好序的处理队列：先"直接提文字"，再"需要 OCR"（各组内保持原顺序）。"""
    direct, ocr = partition_paths(paths)
    return direct + ocr
