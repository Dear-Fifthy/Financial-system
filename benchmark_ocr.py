"""OCR 基准测试：判定"扫描慢是程序问题还是 PaddleOCR-VL 本身"。

文件位置：项目根目录新增 benchmark_ocr.py。
用法：
    python benchmark_ocr.py [PDF路径] [页数]

流程：
  1. 打印环境报告：设备决策 / paddle CUDA / GPU 显存 / CPU 核数 / 推理引擎；
  2. 启动性能监控（3s 采样线程+CPU+GPU -> logs/perf_monitor.log）；
  3. 逐页 渲染->predict（走与生产相同的 predict_safely），每页计时，
     并在每页期间用后台采样线程记录 GPU 利用率（算平均）；
  4. 输出判定结论。

判定口径：
  - OCR 期间 GPU 平均利用率持续高（>70%）→ 计算确在 GPU 上，
    慢是模型本身（VL 是生成式模型，逐 token 解码、kernel 细碎，
    利用率天然偏低但延迟长）；建议换 PP-OCRv5 / RapidOCR（1-3s/页）；
  - GPU 平均利用率 <20% 且 CPU 占用高 → 程序路径问题（误走 CPU/等待），
    需要查设备决策与 .env 配置；
  - 显存接近上限（8G 本机）→ 显存竞争导致退化，清理其它 GPU 程序。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fitz  # noqa: E402

from monitor import gpu_snapshot, start_performance_monitor, stop_performance_monitor  # noqa: E402
from pdf_native_extractor import render_page_to_image  # noqa: E402
from scanner_core import OCR_ENGINE, predict_safely, resolve_ocr_device  # noqa: E402


def _env_report() -> str:
    gpu = gpu_snapshot()
    device, reason = resolve_ocr_device()
    import paddle

    lines = [
        f"设备决策      : {device}（{reason}）",
        f"推理引擎      : {OCR_ENGINE}",
        f"paddle CUDA   : {paddle.is_compiled_with_cuda()} | 默认设备 {paddle.device.get_device()}",
        f"GPU 当前      : 利用率 {gpu.get('gpu_util_pct', 'NA')}% | 显存 {gpu.get('mem_used_mb', 'NA')}/{int(gpu.get('mem_used_mb', 0)) + int(gpu.get('mem_free_mb', 0))}MB | 功耗 {gpu.get('power_w', 'NA')}W",
        f"CPU 逻辑核数  : {os.cpu_count()}",
    ]
    return "\n".join(lines)


def _sample_gpu_during(stop_event: threading.Event, interval: float = 2.0) -> list[float]:
    """在 stop_event 置位前，每 interval 秒采样一次 GPU 利用率。"""
    utils: list[float] = []
    while not stop_event.is_set():
        g = gpu_snapshot()
        try:
            utils.append(float(g.get("gpu_util_pct", "0")))
        except (TypeError, ValueError):
            pass
        time.sleep(interval)
    return utils


def main() -> int:
    parser = argparse.ArgumentParser(description="OCR 基准测试：判定慢是程序还是模型")
    parser.add_argument("pdf", help="PDF 路径")
    parser.add_argument("pages", nargs="?", type=int, default=1, help="测试页数（默认 1，从第 1 页起）")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"文件不存在：{pdf_path}")
        return 1

    print("===== 环境报告 =====")
    print(_env_report())
    print("====================\n")

    perf_log = start_performance_monitor(interval=3)
    print(f"性能监控已启动：{perf_log}\n")

    doc = fitz.open(str(pdf_path))
    total_pages = min(args.pages, len(doc))
    tmp_dir = Path(__file__).resolve().parent / "output" / "_bench_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    per_page: list[dict] = []
    t_start = time.monotonic()
    for i in range(total_pages):
        img = render_page_to_image(doc[i], tmp_dir / f"bench_page_{i + 1}.png")
        stop = threading.Event()
        utils: list[float] = []
        sampler = threading.Thread(target=lambda: utils.extend(_sample_gpu_during(stop)), daemon=True)
        sampler.start()

        t0 = time.monotonic()
        try:
            res = predict_safely(str(img))
            ok = True
        except Exception as exc:
            res, ok = [], False
            print(f"第 {i + 1} 页 predict 失败：{exc}", flush=True)
        dt = time.monotonic() - t0
        stop.set()
        sampler.join(timeout=5)

        avg_util = round(sum(utils) / len(utils), 1) if utils else None
        per_page.append({"page": i + 1, "seconds": round(dt, 1), "avg_gpu_util": avg_util, "ok": ok})
        print(
            f"第 {i + 1} 页：耗时 {dt:.1f}s | OCR期间GPU平均利用率 {avg_util}%"
            f"（采样 {len(utils)} 次）| 结果数 {len(res) if ok else '失败'}",
            flush=True,
        )
        img.unlink(missing_ok=True)
    t_all = time.monotonic() - t_start

    doc.close()
    stop_performance_monitor()

    # ===== 判定结论 =====
    ok_pages = [p for p in per_page if p["ok"]]
    print("\n===== 判定结论 =====")
    if not ok_pages:
        print("所有页面失败——请检查设备/驱动/显存（见上错误）。")
        return 1
    avg_s = sum(p["seconds"] for p in ok_pages) / len(ok_pages)
    utils_ok = [p["avg_gpu_util"] for p in ok_pages if p["avg_gpu_util"] is not None]
    avg_u = round(sum(utils_ok) / len(utils_ok), 1) if utils_ok else None
    print(f"平均每页耗时：{avg_s:.1f}s；OCR 期间 GPU 平均利用率：{avg_u}%")
    if avg_u is not None and avg_u >= 70:
        print("结论：GPU 一直在满负荷计算 → 慢是【模型本身】（VL 生成式推理慢）。")
        print("建议：换 PP-OCRv5 / RapidOCR（经典检测+识别，1-3s/页）。")
    elif avg_u is not None and avg_u < 20:
        print("结论：GPU 基本没在算（CPU 或等待）→ 慢是【程序/配置问题】。")
        print("建议：检查 OCR_DEVICE 决策、.env 配置、是否有其它进程占满显存。")
    else:
        print("结论：GPU 利用率中等——VL 模型生成式推理 kernel 细碎、利用率天然偏低但延迟长（模型特性），")
        print("同时留意显存/功耗是否被其它程序挤占。建议换 PP-OCRv5 立竿见影（1-3s/页）。")
    print(f"完整采样日志：{perf_log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
