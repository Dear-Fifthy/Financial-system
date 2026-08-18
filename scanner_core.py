from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import json
from typing import Iterable

import paddle  # 显式引入 paddle 库
from paddleocr import PaddleOCRVL

BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
SUPPORTED_INPUT_EXTENSIONS = {".pdf", *SUPPORTED_IMAGE_EXTENSIONS}


def ensure_workspace_dirs() -> None:
    INPUT_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)


@lru_cache(maxsize=1)
def get_ocr() -> PaddleOCRVL:
    """延迟初始化 PaddleOCR-VL，避免 GUI 启动时就加载大模型。"""
    # 强制禁用静态图模式，防止 PaddleX/Paddle 动转静后多次 predict 触发 int(Tensor) 错误
    paddle.disable_static()
    return PaddleOCRVL(
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_layout_detection=True,
        use_ocr_for_image_block=True,
        format_block_content=True,
        merge_layout_blocks=True,
        use_queues=False,
    )


def _json_default(value):
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _write_result_json(result_entry, out_json: Path) -> None:
    result_json = getattr(result_entry, "json", None)
    if result_json is None:
        result_json = {}
    _write_result_json_dict(result_json, out_json)


def _write_result_json_dict(result_json: dict, out_json: Path) -> None:
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(result_json, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )


def is_supported_input(file_path: Path) -> bool:
    return file_path.is_file() and file_path.suffix.lower() in SUPPORTED_INPUT_EXTENSIONS


def discover_input_files(input_dir: Path = INPUT_DIR) -> list[Path]:
    if not input_dir.exists():
        return []
    return [path for path in sorted(input_dir.iterdir()) if is_supported_input(path)]


def expand_paths(paths: Iterable[Path]) -> list[Path]:
    collected: list[Path] = []
    for path in paths:
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if is_supported_input(child):
                    collected.append(child)
        elif is_supported_input(path):
            collected.append(path)
    return collected


def process_file(file_path: Path, output_root: Path = OUTPUT_DIR) -> list[Path]:
    ensure_workspace_dirs()

    file_output_dir = output_root / file_path.stem
    file_output_dir.mkdir(parents=True, exist_ok=True)

    if file_path.suffix.lower() == ".pdf":
        return _process_pdf(file_path, file_output_dir)
    return _process_image(file_path, file_output_dir)


def _process_image(file_path: Path, file_output_dir: Path) -> list[Path]:
    result_list = get_ocr().predict(str(file_path))
    written_paths: list[Path] = []

    for index, result_entry in enumerate(result_list, start=1):
        page_index = None
        result_json = getattr(result_entry, "json", None)
        if isinstance(result_json, dict):
            page_index = result_json.get("res", {}).get("page_index")

        page_number = (page_index + 1) if isinstance(page_index, int) else index
        page_name = f"page_{page_number:03d}"
        out_json = file_output_dir / f"{page_name}.json"
        _write_result_json(result_entry, out_json)
        written_paths.append(out_json)

    return written_paths


def _process_pdf(pdf_path: Path, file_output_dir: Path) -> list[Path]:
    """PDF 按页判断原生文字 / 扫描件，分别走文字提取或 OCR。"""
    import fitz  # PyMuPDF

    from pdf_native_extractor import (
        extract_native_page_json,
        render_page_to_image,
        split_pdf_pages,
    )

    page_kinds = split_pdf_pages(pdf_path)
    written_paths: list[Path] = []

    doc = fitz.open(str(pdf_path))
    tmp_dir = file_output_dir / "_tmp_pages"

    try:
        for page_index, is_native in page_kinds:
            page_number = page_index + 1
            page_name = f"page_{page_number:03d}"
            out_json = file_output_dir / f"{page_name}.json"

            if is_native:
                result_json = extract_native_page_json(doc[page_index], page_index)
                _write_result_json_dict(result_json, out_json)
                written_paths.append(out_json)
            else:
                page_image_path = render_page_to_image(
                    doc[page_index], tmp_dir / f"{page_name}.png"
                )
                page_results = get_ocr().predict(str(page_image_path))
                
                # 修复空结果逻辑：如果 OCR 识别到了内容，则落盘并记录路径
                has_written = False
                if page_results:
                    for result_entry in page_results:
                        result_json = getattr(result_entry, "json", None) or {}
                        res_block = result_json.setdefault("res", {})
                        res_block["page_index"] = page_index
                        res_block["source"] = "ocr_scan"
                        _write_result_json_dict(result_json, out_json)
                        written_paths.append(out_json)
                        has_written = True
                        break
                
                # 如果 OCR 识别返回空（如模糊空白页），补一个安全的空结构数据，确保不会出现文件缺失
                if not has_written:
                    fallback_json = {"res": {"page_index": page_index, "source": "ocr_scan", "parsing_res_list": []}}
                    _write_result_json_dict(fallback_json, out_json)
                    written_paths.append(out_json)

    finally:
        doc.close()
        if tmp_dir.exists():
            for tmp_file in tmp_dir.glob("*"):
                tmp_file.unlink(missing_ok=True)
            tmp_dir.rmdir()

    return written_paths