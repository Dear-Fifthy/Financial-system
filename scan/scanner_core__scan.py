from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from infra import proc__infra as _proc       # 静默子进程：nvidia-smi 别弹黑窗口
                                             # （别名用 _proc：本文件里有局部变量叫 proc，别遮蔽）

# ⚠️ DLL 顺序修复（torch 必须先于 paddle 加载）：
# paddleocr -> paddlex -> modelscope 这条导入链会在导入期 `import torch`；
# 若 torch 在 paddle 之后加载，torch 的 shm.dll 会因 DLL 冲突报
# WinError 127（实测：先 paddle 后 torch 必崩，先 torch 后 paddle 共存）。
# 因此在 paddle 相关导入之前先完成 torch 加载；未安装 torch 时静默跳过。
try:
    import torch  # noqa: F401  # 先加载 torch，规避与 paddle 的 DLL 顺序冲突
except Exception:  # torch 缺失/损坏时不影响扫描主流程
    pass

import paddle  # 显式引入 paddle 库
from paddleocr import PaddleOCRVL
from dotenv import load_dotenv

# 停止扫描（协作式取消）检查点：本模块在**逐页循环**里调 check()。
# 注意：paddle/torch 很重，scan_control 只依赖标准库，放在这里不会引入额外开销。
import scan.scan_control__scan as scan_ctl

BASE_DIR = Path(__file__).resolve().parents[1]
# input / output 随工作区（界面叫「仓库」）切换：见 workspace__infra。
# output 里是**逐页原文级缓存**，属于业务材料，所以必须按仓库隔离，不能跨仓库复用。
from infra.workspace__infra import input_root as _input_root
from infra.workspace__infra import output_root as _output_root

INPUT_DIR = _input_root()
OUTPUT_DIR = _output_root()
SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
# Office 文件：**能直接提取文字就绝不进 OCR**（xlsx=openpyxl 直读；docx=stdlib 直读）
SUPPORTED_OFFICE_EXTENSIONS = {".xlsx", ".docx"}
# 旧版 Office：需先转换（LibreOffice 或 Office COM），失败则提示另存为；源文件不动
LEGACY_OFFICE_EXTENSIONS = {".doc", ".xls"}
SUPPORTED_INPUT_EXTENSIONS = {
    ".pdf", *SUPPORTED_IMAGE_EXTENSIONS, *SUPPORTED_OFFICE_EXTENSIONS, *LEGACY_OFFICE_EXTENSIONS
}

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
# 单页 OCR **硬上限**（用户约定：单页最长 120s，超时直接放弃这份扫描，不傻等）
OCR_PAGE_TIMEOUT_S = _env_int("OCR_PAGE_TIMEOUT_S", 120)
# 放弃后进入冷却：被放弃的推理线程是 daemon、进程内杀不掉，冷却期内不再发起新 OCR
OCR_ABANDON_COOLDOWN_S = _env_int("OCR_ABANDON_COOLDOWN_S", 300)
OCR_SLOW_REBUILD_LIMIT = _env_int("OCR_SLOW_REBUILD_LIMIT", 2)
# ---- 模型重建守卫（防"重复初始化"刷屏/无限循环）----
# 失败重试与慢页重建共用一个重建入口（_rebuild_ocr）：
#   冷却期内不重复重建；进程内累计重建达到上限后只告警不再重建，
#   避免慢环境（如 CPU）下"加载模型 60s -> 2 个慢页 -> 再加载"无限循环。
OCR_REBUILD_COOLDOWN_S = _env_int("OCR_REBUILD_COOLDOWN_S", 120)
OCR_REBUILD_MAX = _env_int("OCR_REBUILD_MAX", 3)
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
        proc = _proc.run(
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


# 记录最近一次设备探测结果（warmup_ocr 读取，避免启动时重复 resolve_ocr_device）
_OCR_INFO = {"device": "", "reason": ""}


@lru_cache(maxsize=1)
def get_ocr() -> PaddleOCRVL:
    """延迟初始化 PaddleOCR-VL，避免 GUI 启动时就加载大模型。

    设备选择：按 resolve_ocr_device() 自动选择 GPU/CPU（可用 .env 的
    OCR_DEVICE 强制指定），避免小显存机器 OOM 崩溃。
    引擎选择：显式传 engine=OCR_ENGINE（默认 paddle_dynamic 动态图），
    规避 paddleocr 默认 paddle_static 静态图引擎的 int(Tensor) 报错。
    模型实例由 lru_cache 保证进程内最多一份（除非主动 _rebuild_ocr 重建）。
    """
    # 保险起见仍禁用全局静态图模式（动态图引擎下无副作用）
    paddle.disable_static()

    device, reason = resolve_ocr_device()
    _OCR_INFO["device"], _OCR_INFO["reason"] = device, reason
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
# 单页超时放弃后的冷却截止时间（monotonic）
_OCR_ABANDON_STATE = {"until": 0.0}


class OcrAbandoned(RuntimeError):
    """单页 OCR 超过 OCR_PAGE_TIMEOUT_S → 按约定**放弃本次扫描**（不是模型故障）。"""

# 重建守卫状态：冷却时间戳 + 进程内累计重建次数（防"重复初始化"）
_REBUILD_STATE = {"last_time": 0.0, "count": 0, "lock": threading.Lock()}


def _rebuild_ocr(reason: str) -> bool:
    """统一的重建模型实例入口（带冷却 + 上限，防止重复初始化）。

    这是唯一允许丢弃/重载模型的地方：失败重试与慢页重建都走这里。
      - 冷却：距上次重建 < OCR_REBUILD_COOLDOWN_S 时跳过（避免连续触发）；
      - 上限：进程内累计重建 >= OCR_REBUILD_MAX 后只告警不再重建
        （防止慢环境/坏环境下"加载 60s -> 慢页 -> 再加载"无限循环）。
    返回是否真的执行了重建。
    """
    with _REBUILD_STATE["lock"]:
        now = time.monotonic()
        if _REBUILD_STATE["count"] >= OCR_REBUILD_MAX:
            print(
                f"[OCR] 已重建 {OCR_REBUILD_MAX} 次达到上限，不再重建（{reason}）。"
                "请检查 GPU 显存/驱动或改用 GPU 环境。",
                flush=True,
            )
            return False
        if now - _REBUILD_STATE["last_time"] < OCR_REBUILD_COOLDOWN_S:
            print(f"[OCR] 距上次重建不足 {OCR_REBUILD_COOLDOWN_S}s，跳过重建（{reason}）。", flush=True)
            return False
        _REBUILD_STATE["last_time"] = now
        _REBUILD_STATE["count"] += 1
        rebuild_no = _REBUILD_STATE["count"]

    print(f"[OCR] 重建模型实例（第 {rebuild_no} 次，原因：{reason}）…", flush=True)
    try:
        get_ocr.cache_clear()  # 丢弃被污染的实例（同时释放显存）
        get_ocr()              # 重新加载（lru_cache 重建）
        print("[OCR] 模型重建完成。", flush=True)
        return True
    except Exception as exc:
        print(f"[OCR] 模型重建失败：{exc}", flush=True)
        return False


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

    threading.Thread(target=_monitor, daemon=True, name="ocr-watchdog").start()


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
        # 统一走 _rebuild_ocr：带冷却+上限，防止慢环境无限循环重建
        _rebuild_ocr(f"连续 {OCR_SLOW_REBUILD_LIMIT} 个慢页")


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
    if time.monotonic() < _OCR_ABANDON_STATE["until"]:
        left = _OCR_ABANDON_STATE["until"] - time.monotonic()
        raise OcrAbandoned(
            f"上一次单页 OCR 超时被放弃，仍在冷却中（剩余 {left:.0f}s）："
            "被放弃的推理线程还没归还 GPU，先不要继续扫描。"
        )

    _start_page_monitor()
    _OCR_MONITOR_STATE["running"] = True
    _OCR_MONITOR_STATE["started_at"] = time.monotonic()
    _OCR_MONITOR_STATE["hung_logged"] = False
    t0 = time.monotonic()
    # 每页 GPU 打点：开始/结束快照 + 耗时，写入 perf_monitor 日志（monitor__infra.py）
    # 目的：一眼看出"这一页是 GPU 在算还是 CPU 在算、显存变化多少"。
    try:
        from infra.monitor__infra import log_snapshot_now

        gpu_before = log_snapshot_now(f"predict 开始 {Path(image_path).name}")
    except Exception:
        gpu_before = {}
    try:
        results = _predict_with_deadline(image_path, t0)
        _OCR_BREAKER.record_success()
        try:
            from infra.monitor__infra import log_snapshot_now

            gpu_after = log_snapshot_now(f"predict 结束 {Path(image_path).name}")
        except Exception:
            gpu_after = {}
        _watch_slow_page(t0, "完成")
        print(
            f"[OCR] 单页完成 {Path(image_path).name} 耗时 {time.monotonic() - t0:.1f}s | "
            f"GPU利用(始/终) {gpu_before.get('gpu_util_pct', 'NA')}%/{gpu_after.get('gpu_util_pct', 'NA')}% | "
            f"显存 {gpu_before.get('mem_used_mb', 'NA')}->{gpu_after.get('mem_used_mb', 'NA')}MB",
            flush=True,
        )
        return results
    except OcrAbandoned:
        _watch_slow_page(t0, "超时放弃")
        raise
    except Exception as exc:
        _watch_slow_page(t0, "失败")
        if not _retried:
            # 统一走 _rebuild_ocr（带冷却+上限）丢弃被污染实例后再重试一次
            _rebuild_ocr(f"预测失败（{type(exc).__name__}）")
            try:
                results = _predict_with_deadline(image_path, time.monotonic())
                _OCR_BREAKER.record_success()
                print("[OCR] 重建后重试成功。", flush=True)
                return results
            except OcrAbandoned:
                raise
            except Exception as exc2:
                _raise_ocr_failure(exc2)
        _raise_ocr_failure(exc)
    finally:
        _OCR_MONITOR_STATE["running"] = False


def _predict_with_deadline(image_path, t0: float) -> list:
    """在子线程里跑 predict，最多等 OCR_PAGE_TIMEOUT_S；超时抛 `OcrAbandoned`。

    为什么用线程：Paddle 推理是 C++ 调用、Python 层没有可中断点，进程内杀不掉；
    线程至少让**主流程**按约定在 120s 内退出（不再阻塞扫描）。
    超时后写一条 `logs/perf/ocr_abandon_<date>.jsonl`，便于事后定位是哪一页。
    """
    box: dict = {}

    def _run() -> None:
        try:
            box["res"] = get_ocr().predict(str(image_path))
        except BaseException as exc:          # 线程内异常带回主线程再抛
            box["exc"] = exc

    th = threading.Thread(target=_run, daemon=True, name="ocr-predict")
    th.start()
    th.join(OCR_PAGE_TIMEOUT_S)
    if th.is_alive():
        _OCR_ABANDON_STATE["until"] = time.monotonic() + OCR_ABANDON_COOLDOWN_S
        elapsed = time.monotonic() - t0
        msg = (
            f"单页 OCR 已运行 {elapsed:.0f}s 仍未返回（上限 {OCR_PAGE_TIMEOUT_S}s，"
            f"文件 {Path(image_path).name}）→ 按约定**放弃本次扫描**。"
            f"该页推理线程无法在进程内强杀，已进入 {OCR_ABANDON_COOLDOWN_S}s 冷却；"
            "建议检查 GPU/驱动，或把该页单独拆出来再试。"
        )
        print(f"[OCR] ⛔ {msg}", flush=True)
        try:
            d = Path("logs/perf")
            d.mkdir(parents=True, exist_ok=True)
            with (d / f"ocr_abandon_{time.strftime('%Y%m%d')}.jsonl").open(
                    "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "file": Path(image_path).name,
                                    "elapsed_s": round(elapsed, 1),
                                    "limit_s": OCR_PAGE_TIMEOUT_S},
                                   ensure_ascii=False) + "\n")
        except Exception:
            pass
        raise OcrAbandoned(msg)
    if "exc" in box:
        raise box["exc"]
    return box.get("res") or []


def warmup_ocr() -> dict:
    """程序启动时预加载 OCR 模型（可选调用）。

    在登录/主窗口显示之前调用一次，把模型加载耗时从「拖入文件那一刻」
    转移到启动阶段；配合启动画面（splash）即可消除拖拽瞬间的卡顿。
    注意：这里不再单独调 resolve_ocr_device()——get_ocr() 内部会解析一次
    并写入 _OCR_INFO，避免启动路径重复探测/重复打印。
    返回环境探测结果，便于 UI 提示用户当前运行模式。
    """
    get_ocr()  # 触发 lru_cache 完成模型初始化
    return {
        "device": _OCR_INFO.get("device", ""),
        "reason": _OCR_INFO.get("reason", ""),
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
    name = file_path.name
    # 跳过 Office 打开文件时生成的临时/锁文件（~$xxx.xlsx），它们是垃圾内容
    if name.startswith("~$") or name.startswith(".~"):
        return False
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


def expand_inputs(paths: Iterable[Path]) -> list[tuple[Path | None, Path]]:
    """展开输入 → [(源文件夹根, 文件路径), …]（**保留文件夹归属**）。

    与 expand_paths 的区别：文件夹输入时记下"根目录"，让 hub 能按源文件夹
    结构落盘（hub/<源文件夹子树>/<文件名>.json），不再把一个文件夹里的文件
    在 hub 根目录里拆成一堆散文件；直接拖入的单个文件根为 None（仍落 hub 根）。
    """
    out: list[tuple[Path | None, Path]] = []
    for path in paths:
        if path.is_dir():
            root = path.resolve()
            for child in sorted(path.rglob("*")):
                if is_supported_input(child):
                    out.append((root, child))
        elif is_supported_input(path):
            out.append((None, path))
    return out


def _cached_pages(file_path: Path, file_output_dir: Path) -> list[Path] | None:
    """页缓存可复用时返回按页序排列的 page_XXX.json 路径，否则 None。

    安全性依据：缓存目录名含**源文件内容指纹**（`<stem>__<hash10>`），同目录 ⇒ 同内容
    ⇒ 上次的 OCR/文字提取结果依然成立。这里再做一层完整性校验：页数对得上、每页
    JSON 都能解析且 `res` 非空；任何一项不满足就退回真实扫描（不猜、不半用）。
    """
    import json

    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        try:
            from scan.pdf_native_extractor__scan import split_pdf_pages

            page_count = len(split_pdf_pages(file_path))
        except Exception:
            return None
    else:
        page_count = 1
    if page_count <= 0:
        return None
    paths: list[Path] = []
    for i in range(1, page_count + 1):
        p = file_output_dir / f"page_{i:03d}.json"
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict) or not (data.get("res") or {}):
            return None
        paths.append(p)
    return paths


def process_file(
    file_path: Path,
    output_root: Path = OUTPUT_DIR,
    on_page_json=None,
    content_hash: str | None = None,
) -> list[Path]:
    """处理单个文件，产出逐页 json 缓存。

    on_page_json：可选回调（接收 Path），在**每一页 json 落盘后立即调用**
    （下一页开始 OCR 之前）。配合流水线重叠：调用方可以在扫描的同时
    立刻处理本页结果，不必等整份文件扫完。

    content_hash：源文件内容指纹（调用方通常已经算过，避免重复哈希）。缓存目录名带上
    指纹（`<stem>__<指纹前10位>`），防止**不同文件夹里的同名文件**共用同一缓存目录、
    互相覆盖 `page_*.json`；同内容 → 同目录，缓存天然可复用。
    """
    ensure_workspace_dirs()

    import desens.dedup__desens as dedup__desens

    tag = content_hash or dedup__desens.content_key(file_path)
    file_output_dir = output_root / dedup__desens.cache_dir_name(file_path, tag)
    file_output_dir.mkdir(parents=True, exist_ok=True)

    # OCR 前释放内存/显存（需求）：真要进 OCR 分支时兜底调用一次
    # （UI 侧在"开始 OCR 组"时会先主动调一次 force=True；这里保证 CLI/批处理也有）。
    suffix = file_path.suffix.lower()
    is_ocr_source = (suffix == ".pdf"
                     or suffix in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"})
    if is_ocr_source:
        # **页缓存复用**：缓存目录名里已经带了**源文件内容指纹**（同内容→同目录），
        # 因此目录里页齐且都能解析时，可直接复用上次的 OCR/文字提取结果，
        # 不必再跑一遍 GPU（重扫 7 份文档里那 1 份 PDF 实测单页 OCR >3 分钟）。
        # 只对 PDF/图片生效：Office 直读产物是**已脱敏**的页 JSON，复用会把旧脱敏结果带回来。
        cached = _cached_pages(file_path, file_output_dir)
        if cached:
            print(f"[OCR] 复用已有页缓存（{len(cached)} 页，源文件内容未变）", flush=True)
            for p in cached:
                if on_page_json is not None:
                    on_page_json(p)
            return cached
        try:
            import scan.resource_release__scan as resource_release

            # 注意用**别名**调用：写成 `resource_release__scan.release_before_ocr(...)`
            # 会 NameError（模块名不绑定到局部作用域）→ 释放被静默跳过（实测）。
            resource_release.release_before_ocr(reason=f"扫描 {file_path.name} 前释放资源")
        except Exception as exc:
            print(f"[资源释放] 跳过（{type(exc).__name__}: {exc}）", flush=True)

    # 重活闸门：OCR/直读是整条链路里最吃 GPU/CPU 的一步，与后台 embedding 共用一把锁，
    # 保证"同一时刻只有一个重活"（串行排队，而不是抢资源）。
    # 传 cancel_check：在**排队等锁**时也能响应"停止扫描"（否则要等前一个重活跑完）。
    from scan.runtime_lane__scan import heavy_lane

    scan_ctl.check("扫描前")
    with heavy_lane("ocr_scan", cancel_check=lambda: scan_ctl.check("等待重活闸门")):
        if file_path.suffix.lower() == ".pdf":
            return _process_pdf(file_path, file_output_dir, on_page_json)
        if file_path.suffix.lower() in SUPPORTED_OFFICE_EXTENSIONS:
            # Office：直接提取文字/表格（xlsx、docx），**不经过 OCR**
            return _process_office(file_path, file_output_dir, on_page_json)
        if file_path.suffix.lower() in LEGACY_OFFICE_EXTENSIONS:
            # 旧版 Office（.doc/.xls）：先转换（只读源文件）→ 再直读；失败给出提示
            from desens.legacy_convert__desens import convert_legacy

            converted, message = convert_legacy(file_path)
            if converted is None:
                raise RuntimeError(message)
            print(f"[Office] {message}", flush=True)
            return _process_office(converted, file_output_dir, on_page_json,
                                   origin=file_path)
        return _process_image(file_path, file_output_dir, on_page_json)
    return _process_image(file_path, file_output_dir, on_page_json)


def _process_office(file_path: Path, file_output_dir: Path, on_page_json=None,
                    origin: Path | None = None) -> list[Path]:
    """Office 直读分支（xlsx/docx）：完全不加载 OCR 模型。

    产出与原生文本页同构的页 JSON（含 `desensitized: true` 与 tables），
    hub 流水线据此跳过二次脱敏。origin 非空时表示来自 .doc/.xls 转换，
    会在页 JSON 里记录来源（可追溯，且源文件始终未改动）。
    """
    from desens.office_reader__desens import read_office

    written_paths: list[Path] = []
    for index, page_json in enumerate(read_office(file_path), start=1):
        scan_ctl.check(f"Office 直读第 {index} 页前")     # 停止扫描检查点
        if origin is not None:
            res = page_json.setdefault("res", {})
            res["converted_from"] = origin.suffix.lower()
            res["source_file_original"] = origin.name
        out_json = file_output_dir / f"page_{index:03d}.json"
        _write_result_json_dict(page_json, out_json)
        written_paths.append(out_json)
        if on_page_json is not None:
            on_page_json(out_json)
    return written_paths


def _process_image(file_path: Path, file_output_dir: Path, on_page_json=None) -> list[Path]:
    # 统一走带熔断的预测入口，避免 GPU 异常时整个程序崩溃
    result_list = predict_safely(str(file_path))
    written_paths: list[Path] = []

    for index, result_entry in enumerate(result_list, start=1):
        scan_ctl.check(f"图片 OCR 第 {index} 页前")       # 停止扫描检查点
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

    from scan.pdf_native_extractor__scan import (
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
            # 停止扫描检查点：上一页已完整落盘，这里中断不产生半截产物
            scan_ctl.check(f"PDF 第 {page_index + 1} 页前")
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