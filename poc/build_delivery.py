#!/usr/bin/env python3
"""Package the local workbench without credentials, indexes, or generated runs."""

from __future__ import annotations

import argparse
from pathlib import Path
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_delivery(output: Path, *, include_framework_reference: bool = False) -> Path:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    files = [PROJECT_ROOT / name for name in ("README.md", "DESIGN.md", "CONTEXT.md", ".env.example", ".gitignore")
             if (PROJECT_ROOT / name).is_file()]
    files.extend(path for path in (PROJECT_ROOT / "poc").iterdir()
                 if path.is_file() and not path.is_symlink() and path.suffix in {".py", ".bat", ".md"})
    files.extend(path for directory in ("web", "tests", "fixtures")
                 for path in (PROJECT_ROOT / "poc" / directory).rglob("*")
                 if path.is_file() and not path.is_symlink()
                 and "__pycache__" not in path.parts
                 and path.suffix in {".py", ".bat", ".js", ".cjs", ".css", ".html", ".md", ".json", ".cbl", ".cob", ".cpy", ".sql"})
    files.extend(path for path in (PROJECT_ROOT / "docs").rglob("*.md") if path.is_file() and not path.is_symlink())
    if include_framework_reference:
        reference = PROJECT_ROOT / ".poc-data" / "framework" / "reference.md"
        if not reference.is_file() or reference.is_symlink():
            raise ValueError("The local framework reference is unavailable.")
        files.append(reference)
    if output in files:
        raise ValueError("The delivery path cannot overwrite an input file.")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            archive.write(path, Path("cobol-workbench") / path.relative_to(PROJECT_ROOT))
        instructions = (
            "COBOL 业务分析工作台\r\n\r\n"
            "1. 解压到新目录。电脑需要已安装 Python 3.10 或更新版本。\r\n"
            "2. 把原项目的 .env 复制到新项目根目录，保留现有公司接口配置。\r\n"
            "   没有旧配置时，将 .env.example 复制为 .env，填写接口地址、模型和密钥。\r\n"
            "3. 关闭旧服务，双击 poc\\run_web.bat，保留启动窗口。\r\n"
            "4. 在网页连接源码目录与独立输出目录，选择具体程序。\r\n"
            "5. 填写业务问题，选择阅读深度，点击开始业务分析。\r\n\r\n"
            "默认位置的框架文字稿自动加载；显式 FRAMEWORK_REFERENCE_PATH 配置优先。\r\n"
            "本包不含真实 API 密钥、公司业务源码或历史分析数据库。\r\n"
            + ("本包附带你提供的私有框架资料，请按原资料允许范围保管。\r\n" if include_framework_reference else "")
            + "详细说明：docs/18-business-analysis-workflow.md\r\n"
        )
        archive.writestr("cobol-workbench/START-HERE.txt", instructions.encode("utf-8-sig"))
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-framework-reference", action="store_true")
    args = parser.parse_args()
    print(build_delivery(args.output, include_framework_reference=args.include_framework_reference))


if __name__ == "__main__":
    main()
