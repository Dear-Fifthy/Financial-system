from argparse import ArgumentParser

from scanner_core import INPUT_DIR, OUTPUT_DIR, discover_input_files, process_file


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("--workers", type=int, default=1, help="保留参数，当前由桌面端/模型内部自行调度")
    args = parser.parse_args()

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