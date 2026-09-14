"""命令行扫描入口（把 input/ 里的文件扫成 hub/ 产物）。

⚠️ 先让「当前仓库」（工作区/环境隔离）生效，再导入扫描模块：
`scanner_core__scan` 在**导入期**就把 INPUT_DIR / OUTPUT_DIR 定成了常量，
所以必须"先切仓库、后导入"，否则会把文件读/写到别的仓库目录里。
"""
import sys as _sys
from pathlib import Path as _Path

if __package__ in (None, ""):          # 直接 `python <层>/<模块>.py` 跑：把仓库根放回 sys.path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from argparse import ArgumentParser


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("--workers", type=int, default=1, help="保留参数，当前由桌面端/模型内部自行调度")
    parser.add_argument("--workspace", default="",
                        help="在指定仓库里跑（默认用登记表里当前生效的仓库）")
    args = parser.parse_args()

    try:
        import infra.workspace__infra as ws

        if args.workspace:
            entry = ws.find(args.workspace)
            if not entry:
                print(f"没有这个仓库：{args.workspace}（python -m infra.workspace__infra list）")
                return
            ws.apply_active(entry, persist=False)
        elif ws.active() is not None:
            ws.apply_active(persist=True)
    except Exception as exc:
        print(f"[仓库] 生效失败（按默认路径继续）：{type(exc).__name__}: {exc}")

    from scan.scanner_core__scan import INPUT_DIR, OUTPUT_DIR, discover_input_files, process_file

    files = discover_input_files(INPUT_DIR)
    if not files:
        print(f"输入目录为空：{INPUT_DIR}")
        print("请把 PDF 或图片文件放进 input 目录后再运行。")
        return

    for file_path in files:
        print(f"正在处理：{file_path.name}")
        process_file(file_path, OUTPUT_DIR)

    print(f"处理完成，结果输出目录：{OUTPUT_DIR}")


if __name__ == "__main__":
    main()
