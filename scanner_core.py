from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import json
from typing import Iterable

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
    return PaddleOCRVL(
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_layout_detection=True,
        use_ocr_for_image_block=True,
        format_block_content=True,
        merge_layout_blocks=True,
        use_queues=True,
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
    """处理单个 PDF 或图片文件，返回写出的 JSON 路径列表。"""
    ensure_workspace_dirs()

    file_output_dir = output_root / file_path.stem
    file_output_dir.mkdir(parents=True, exist_ok=True)

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
