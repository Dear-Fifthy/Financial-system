from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import paddle  # 显式引入 paddle 库
from paddleocr import PaddleOCRVL
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
SUPPORTED_INPUT_EXTENSIONS = {".pdf", *SUPPORTED_IMAGE_EXTENSIONS}

# =========================================================
# OCR 运行环境配置（全部可通过项目根目录 .env 覆盖）
# =========================================================
load_dotenv(dotenv_path=BASE_DIR / ".env")


def _env_int(name: str, default: int) -> int:
    """从环境变量读取整数，非法值时回退默认值，避免配置写错导致程序崩溃。"""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# 设备选择策略：auto（自动探测，推荐）| gpu | cpu
OCR_DEVICE = os.getenv("OCR_DEVICE", "auto").strip().lower()
# ---- 显存检查只做"提示"，不做"硬切换" ----
# 设计说明（回应"一刀切砍显存"的反馈）：
#   · GPU 占用率高（利用率 100%）是正常现象，与"能否再放一个模型"无关；
#     真正决定能否运行的是"空闲显存是否够模型本体"（PaddleOCR-VL 约需
#     OCR_REQUIRED_VRAM_MB，本机实测 6~7G）。
#   · 空闲显存低于阈值只打印警告、仍按 GPU 尝试——避免"处理完一个文件后
#     显存占用高，导致下一个文件被误判降级 CPU"这类误伤。
#   · CPU 不作为自动兜底：VL 模型 CPU 单页实测 12 分钟+，基本不可用；
#     只有显式 OCR_DEVICE=cpu 或 GPU 探测真正失败时才使用 CPU。
OCR_REQUIRED_VRAM_MB = _env_int("OCR_REQUIRED_VRAM_MB", 6144)  # 模型本体需求（提示用）
OCR_MIN_FREE_VRAM_MB = _env_int("OCR_MIN_FREE_VRAM_MB", 1024)  # 警告阈值（不再切换设备）
# OCR 连续失败多少次后熔断（不再调用模型，避免反复崩溃卡死程序）
OCR_FAIL_THRESHOLD = _env_int("OCR_FAIL_THRESHOLD", 3)
# ---- 单页超时看门狗（按页计时，不按整个文件）----
# 单次 predict（一页）耗时超过 OCR_PAGE_TIMEOUT_S 记为"慢页"；连续
# OCR_SLOW_REBUILD_LIMIT 个慢页后重建模型实例一次（排除实例状态被拖慢）。
# 注意：进程内无法强杀卡死的 C++ 调用，硬超时终止需要进程级隔离（后续方案）。
OCR_PAGE_TIMEOUT_S = _env_int("OCR_PAGE_TIMEOUT_S", 180)
OCR_SLOW_REBUILD_LIMIT = _env_int("OCR_SLOW_REBUILD_LIMIT", 2)
# OCR 推理引擎：paddleocr 的 engine 参数默认 None 时会在
# _common_args.prepare_common_init_args() 里强制构建 paddle_static(静态图)
# 引擎配置，触发 "int(Tensor) is not supported in static graph mode" 报错。
# 这里显式使用 paddle_dynamic(动态图) 规避；如确需静态图/TRT 加速可改
# 为 paddle_static 并通过 OCR_ENGINE 覆盖。
# 可选值：paddle | paddle_static | paddle_dynamic | transformers | onnxruntime
OCR_ENGINE = os.getenv("OCR_ENGINE", "paddle_dynamic").strip().lower()


def ensure_workspace_dirs() -> None:
    INPUT_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)


# =========================================================
# GPU/显存探测与设备选择（自动降级）
# =========================================================
def query_free_vram_mb() -> int | None:
    """查询当前空闲显存(MB)。

    通过 nvidia-smi 读取；任何异常（无驱动/无 NVIDIA GPU/超时）都返回
    None 而不是抛错，保证降级逻辑不会反过来把程序搞崩。
    """
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return None
        first_line = proc.stdout.strip().splitlines()[0].strip()
        return int(first_line)
    except Exception:
        return None


def _probe_gpu() -> tuple[bool, str]:
    """用一次小规模显存分配实际探测 GPU 是否可用。

    驱动异常 / 显存不足 / 无 CUDA 上下文都会在此抛错，从而被判定为不可用。
    返回 (是否可用, 说明)。
    """
    try:
        import paddle as _paddle

        tensor = _paddle.ones([256, 256], dtype="float32")
        _ = int(tensor.sum())
        del tensor
        return True, "GPU 实际分配测试通过"
    except Exception as exc:
        return False, f"GPU 探测失败：{exc}"


def resolve_ocr_device() -> tuple[str, str]:
    """决定 OCR 运行设备，返回 (设备名, 原因说明)。

    规则（OCR_DEVICE=auto 时）：
      1. paddle 未编译 CUDA           -> cpu（真没有 GPU）
      2. 空闲显存低于阈值             -> 仅打印警告，仍走 GPU（不切换！）
      3. GPU 实际分配测试失败         -> cpu
      4. 全部通过                     -> gpu
    显式配置 gpu/cpu 时直接采用，不再探测。

    说明：GPU 利用率高 ≠ 显存不够，见模块顶部 OCR_MIN_FREE_VRAM_MB 的设计注释。
    """
    if OCR_DEVICE in ("gpu", "cpu"):
        return OCR_DEVICE, f"配置 OCR_DEVICE 强制指定 {OCR_DEVICE}"

    if not paddle.is_compiled_with_cuda():
        return "cpu", "paddle 未编译 CUDA，只能使用 CPU"

    free_mb = query_free_vram_mb()
    if free_mb is not None and free_mb < OCR_MIN_FREE_VRAM_MB:
        # 仅提示：显存紧张时仍尝试 GPU（模型本体需要 OCR_REQUIRED_VRAM_MB），
        # 避免"处理完一个文件后显存占用高导致下一个文件被误判降级 CPU"。
        print(
            f"[OCR] ⚠️ 空闲显存 {free_mb}MB 低于阈值 {OCR_MIN_FREE_VRAM_MB}MB"
            f"（模型本体约需 {OCR_REQUIRED_VRAM_MB}MB）。仍按 GPU 尝试；"
            "若加载/推理失败，请关闭其它占用显存的程序后重试。"
        )

    ok, reason = _probe_gpu()
    if not ok:
        return "cpu", f"{reason}，只能使用 CPU"

    return "gpu", "GPU 探测通过"


@lru_cache(maxsize=1)
def get_ocr() -> PaddleOCRVL:
    """延迟初始化 PaddleOCR-VL，避免 GUI 启动时就加载大模型。

    设备选择：按 resolve_ocr_device() 自动选择 GPU/CPU（可用 .env 的
    OCR_DEVICE 强制指定），避免小显存机器 OOM 崩溃。
    引擎选择：显式传 engine=OCR_ENGINE（默认 paddle_dynamic 动态图），
    规避 paddleocr 默认 paddle_static 静态图引擎的 int(Tensor) 报错。
    """
    # 保险起见仍禁用全局静态图模式（动态图引擎下无副作用）
    paddle.disable_static()

    device, reason = resolve_ocr_device()
    print(f"[OCR] 设备选择：{device}（{reason}） | 引擎：{OCR_ENGINE}")

    kwargs: dict = dict(
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_layout_detection=True,
        use_ocr_for_image_block=True,
        format_block_content=True,
        merge_layout_blocks=True,
        use_queues=False,
    )
    kwargs["device"] = device
    kwargs["engine"] = OCR_ENGINE
    try:
        return PaddleOCRVL(**kwargs)
    except TypeError:
        # 兼容不支持 device 参数的旧版本：去掉该参数后按默认设备初始化
        kwargs.pop("device", None)
        return PaddleOCRVL(**kwargs)


# =========================================================
# OCR 熔断器（防止小显存/坏驱动环境下反复 OOM 崩溃）
# =========================================================
class OcrCircuitBreaker:
    """OCR 熔断器。

    连续失败 OCR_FAIL_THRESHOLD 次后熔断：后续调用直接抛错，不再尝试
    加载/运行模型，避免在显存不足或驱动异常时反复崩溃。成功一次即
    重置计数；熔断后可通过 reset() 手动恢复（例如用户修复环境后）。
    """

    def __init__(self, fail_threshold: int = 3) -> None:
        self.fail_threshold = max(1, fail_threshold)
        self._failures = 0
        self._tripped = False

    @property
    def tripped(self) -> bool:
        """是否已熔断。"""
        return self._tripped

    @property
    def remaining(self) -> int:
        """距离熔断还剩余的可失败次数（熔断后为 0）。"""
        return max(0, self.fail_threshold - self._failures)

    def record_success(self) -> None:
        """记录一次成功：重置失败计数。"""
        self._failures = 0

    def record_failure(self) -> bool:
        """记录一次失败；返回本次是否刚好触发熔断。"""
        self._failures += 1
        if self._failures >= self.fail_threshold:
            self._tripped = True
        return self._tripped

    def reset(self) -> None:
        """手动解除熔断并清零计数。"""
        self._failures = 0
        self._tripped = False


_OCR_BREAKER = OcrCircuitBreaker(OCR_FAIL_THRESHOLD)

# ---- 单页超时看门狗状态 ----
# 按"每一页 predict 的时长"判断，而不是"整份文件有没有输出"：
# 进程内无法强杀卡死的 C++ 调用，所以看门狗线程只负责"超时后打印提示"；
# predict 返回后由 _watch_slow_page 统计慢页并触发重建模型。
_OCR_MONITOR_STATE = {"running": False, "started_at": 0.0, "hung_logged": False}
_OCR_MONITOR_STARTED = {"flag": False}
_OCR_SLOW_PAGES = {"count": 0}


def _start_page_monitor() -> None:
    """启动后台看门狗线程（只启动一次，daemon）。

    每 5s 检查一次：若某页 predict 已运行超过 OCR_PAGE_TIMEOUT_S 仍未返回，
    打印一次提示（无法强杀，仅提示；硬超时终止需进程级隔离，见模块注释）。
    """
    if _OCR_MONITOR_STARTED["flag"]:
        return
    _OCR_MONITOR_STARTED["flag"] = True

    def _monitor() -> None:
        while True:
            time.sleep(5)
            if _OCR_MONITOR_STATE["running"] and not _OCR_MONITOR_STATE["hung_logged"]:
                elapsed = time.monotonic() - _OCR_MONITOR_STATE["started_at"]
                if elapsed > OCR_PAGE_TIMEOUT_S:
                    _OCR_MONITOR_STATE["hung_logged"] = True
                    print(
                        f"[OCR] ⚠️ 当前页 predict 已运行 {elapsed:.0f}s 超过阈值 "
                        f"{OCR_PAGE_TIMEOUT_S}s，可能卡死/极慢；请检查 GPU 与驱动。",
                        flush=True,
                    )

    threading.Thread(target=_monitor, daemon=True).start()


def _watch_slow_page(started_at: float, outcome: str) -> None:
    """单页超时统计：predict 返回后调用。

    耗时超过 OCR_PAGE_TIMEOUT_S 记为"慢页"；连续 OCR_SLOW_REBUILD_LIMIT 个
    慢页后重建模型实例一次（排除实例状态被拖慢的情况，而不是一刀切换设备）。
    """
    elapsed = time.monotonic() - started_at
    if elapsed <= OCR_PAGE_TIMEOUT_S:
        _OCR_SLOW_PAGES["count"] = 0
        return
    _OCR_SLOW_PAGES["count"] += 1
    print(
        f"[OCR] ⚠️ 单页耗时 {elapsed:.1f}s 超过阈值 {OCR_PAGE_TIMEOUT_S}s（{outcome}）。"
        f"慢页计数 {_OCR_SLOW_PAGES['count']}/{OCR_SLOW_REBUILD_LIMIT}，"
        "请留意 GPU 是否被其它程序占用。",
        flush=True,
    )
    if _OCR_SLOW_PAGES["count"] >= OCR_SLOW_REBUILD_LIMIT:
        _OCR_SLOW_PAGES["count"] = 0
        print("[OCR] 连续慢页，重建模型实例一次…", flush=True)
        try:
            get_ocr.cache_clear()
            get_ocr()  # 预热重建，供后续页使用
            print("[OCR] 模型重建完成。", flush=True)
        except Exception:
            pass


def _raise_ocr_failure(exc: Exception) -> None:
    """记录一次失败；触发熔断时给出明确提示。供 predict_safely 收尾使用。"""
    tripped = _OCR_BREAKER.record_failure()
    hint = "已触发熔断，后续 OCR 任务将被跳过。" if tripped else f"剩余可用重试次数：{_OCR_BREAKER.remaining}"
    raise RuntimeError(f"OCR 预测失败：{exc}；{hint}") from exc


def predict_safely(image_path, *, _retried: bool = False) -> list:
    """带熔断 + 自动重建重试 + 单页超时看门狗的 OCR 预测入口。

    防御策略：
      1. 熔断器：连续失败 OCR_FAIL_THRESHOLD 次后不再调用模型，防止反复崩溃；
      2. 单页超时看门狗：按"每一页 predict 的时长"判断（不是整份文件有没有
         输出）；超时页统计慢页，连续 OCR_SLOW_REBUILD_LIMIT 个慢页后重建
         模型一次；看门狗线程在调用卡死超时后打印提示；
      3. 自动重建重试：框架级异常（int(Tensor) / InvalidType / EagerParamBase 等）
         先丢弃当前模型实例、重新加载后重试一次（约 30~60s）；
      4. 仍失败才计入熔断，上层（ScanWorker）转成任务失败状态展示。
    """
    if _OCR_BREAKER.tripped:
        raise RuntimeError(
            f"OCR 已熔断（连续失败 {_OCR_BREAKER.fail_threshold} 次）。"
            "请检查 GPU 显存/驱动后重启程序，或在 .env 中设置 OCR_DEVICE=gpu 并关闭其它占用显存的程序。"
        )

    _start_page_monitor()
    _OCR_MONITOR_STATE["running"] = True
    _OCR_MONITOR_STATE["started_at"] = time.monotonic()
    _OCR_MONITOR_STATE["hung_logged"] = False
    t0 = time.monotonic()
    try:
        results = get_ocr().predict(str(image_path))
        _OCR_BREAKER.record_success()
        _watch_slow_page(t0, "完成")
        return results
    except Exception as exc:
        _watch_slow_page(t0, "失败")
        if not _retried:
            # 丢弃被污染的模型实例（同时释放显存），重建后重试一次
            print(f"[OCR] 预测失败（{type(exc).__name__}），正在重建模型后重试一次…", flush=True)
            try:
                get_ocr.cache_clear()
            except Exception:
                pass
            try:
                results = get_ocr().predict(str(image_path))
                _OCR_BREAKER.record_success()
                print("[OCR] 重建模型后重试成功。", flush=True)
                return results
            except Exception as exc2:
                _raise_ocr_failure(exc2)
        _raise_ocr_failure(exc)
    finally:
        _OCR_MONITOR_STATE["running"] = False


def warmup_ocr() -> dict:
    """程序启动时预加载 OCR 模型（可选调用）。

    在登录/主窗口显示之前调用一次，把模型加载耗时从「拖入文件那一刻」
    转移到启动阶段；配合启动画面（splash）即可消除拖拽瞬间的卡顿。
    返回环境探测结果，便于 UI 提示用户当前运行模式。
    """
    device, reason = resolve_ocr_device()
    get_ocr()  # 触发 lru_cache 完成模型初始化
    return {
        "device": device,
        "reason": reason,
        "tripped": _OCR_BREAKER.tripped,
    }


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


def process_file(
    file_path: Path,
    output_root: Path = OUTPUT_DIR,
    on_page_json=None,
) -> list[Path]:
    """处理单个文件，产出逐页 json 缓存。

    on_page_json：可选回调（接收 Path），在**每一页 json 落盘后立即调用**
    （下一页开始 OCR 之前）。配合流水线重叠：调用方可以在扫描的同时
    立刻处理本页结果，不必等整份文件扫完。
    """
    ensure_workspace_dirs()

    file_output_dir = output_root / file_path.stem
    file_output_dir.mkdir(parents=True, exist_ok=True)

    if file_path.suffix.lower() == ".pdf":
        return _process_pdf(file_path, file_output_dir, on_page_json)
    return _process_image(file_path, file_output_dir, on_page_json)


def _process_image(file_path: Path, file_output_dir: Path, on_page_json=None) -> list[Path]:
    # 统一走带熔断的预测入口，避免 GPU 异常时整个程序崩溃
    result_list = predict_safely(str(file_path))
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
        if on_page_json is not None:
            on_page_json(out_json)  # 流式：本页结果就绪即通知调用方

    return written_paths


def _process_pdf(pdf_path: Path, file_output_dir: Path, on_page_json=None) -> list[Path]:
    """PDF 按页判断原生文字 / 扫描件，分别走文字提取或 OCR。

    每页处理完立即调用 on_page_json（若有），实现逐页流式输出。
    """
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
                page_results = predict_safely(str(page_image_path))
                
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

            # 流式：本页（无论原生文字还是 OCR）处理完立即通知调用方，
            # 下一页的渲染/OCR 尚未开始——这正是流水线重叠的时机
            if on_page_json is not None:
                on_page_json(out_json)

    finally:
        doc.close()
        if tmp_dir.exists():
            for tmp_file in tmp_dir.glob("*"):
                tmp_file.unlink(missing_ok=True)
            tmp_dir.rmdir()

    return written_paths