"""PDF 逐页判断：原生文字页 vs 扫描(图片)页。

依赖 PyMuPDF：pip install pymupdf --break-system-packages

⚠️ 字段命名说明：
下面 extract_native_page_json() 生成的 JSON 结构（res / page_index / rec_texts ...）
是参照 PaddleOCR-VL 常见输出习惯拼出来的，我没有你实际环境里 PaddleOCR-VL 真实
json 结构做过对照。建议你随便跑一份现有的 OCR page_xxx.json，和这里生成的
"原生页" json 对比一下字段名是否一致；如果不一致，只需要改这个文件里的字段名，
不影响其它逻辑。
"""

from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF

# 每页判定为"原生文字页"所需的最小非空白字符数。
# 低于这个阈值（比如只有页码、印章文字被误识别成几个字符）就当作扫描页处理。
NATIVE_TEXT_MIN_CHARS = 20


def is_native_text_page(page: fitz.Page) -> bool:
    """判断单页是否为可直接提取文字的原生页。"""
    text = page.get_text("text")
    stripped = "".join(text.split())
    return len(stripped) >= NATIVE_TEXT_MIN_CHARS


def split_pdf_pages(pdf_path: Path) -> list[tuple[int, bool]]:
    """返回每一页的 (0-based 页码, 是否原生文字页)。"""
    doc = fitz.open(str(pdf_path))
    try:
        return [(i, is_native_text_page(doc[i])) for i in range(len(doc))]
    finally:
        doc.close()


def extract_native_page_json(page: fitz.Page, page_index: int) -> dict:
    """把原生 PDF 页面的文字提取为与 OCR 输出对齐的 JSON 结构。"""
    full_text = page.get_text("text")
    blocks = page.get_text("blocks")  # [(x0, y0, x1, y1, text, block_no, block_type), ...]
    rec_texts = [b[4].strip() for b in blocks if b[4].strip()]

    return {
        "res": {
            "page_index": page_index,
            "source": "native_pdf_text",  # 标记来源，方便后续区分原生/OCR
            "rec_texts": rec_texts,
            "full_text": full_text,
        }
    }


def render_page_to_image(page: fitz.Page, out_path: Path, zoom: float = 2.0) -> Path:
    """把需要 OCR 的页面渲染成图片，供 PaddleOCR-VL 识别。

    zoom=2.0 约等于 144 DPI，文字较小/密集的凭证可以调到 3.0（约 216 DPI）。
    """
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(out_path))
    return out_path