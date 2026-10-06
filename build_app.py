#!/usr/bin/env python3
"""Build a native desktop delivery from the current working tree."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile


ROOT = Path(__file__).resolve().parent
BUILD_ROOT = ROOT / ".build" / "desktop"
APP_NAME = "COBOLWorkbench"
WEB_FILES = ("index.html", "app.js", "i18n.js", "styles.css", "appearance.css",
             "theme.js", "layout.js", "markdown.js", "marked.umd.js", "marked.LICENSE.md")
FORBIDDEN_SUFFIXES = {".py", ".pyc", ".pyo", ".pyw", ".sqlite", ".db", ".pem", ".key", ".p12", ".pfx"}
FORBIDDEN_PARTS = {".git", ".poc-data", "__pycache__", "tests", ".env"}


def target_label(system=None, machine=None):
    system = sys.platform if system is None else system
    machine = platform.machine().lower() if machine is None else machine.lower()
    if system not in {"win32", "darwin"}:
        raise ValueError("请在 Windows 或 macOS 上运行；每种系统需要分别构建。")
    architecture = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if architecture is None:
        raise ValueError("桌面版只支持 64 位 Python；请安装与目标电脑架构一致的版本。")
    return f"{'windows' if system == 'win32' else 'macos'}-{architecture}"


def copy_regular(source, destination):
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"构建输入必须为普通文件：{source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def stage_sources(destination, root=ROOT):
    """Stage only application code and explicitly selected public resources."""
    source = root / "poc"
    for directory in (source, source / "web", source / "fixtures",
                      source / "fixtures" / "framework-workbench",
                      source / "fixtures" / "framework-workbench" / "source"):
        if directory.is_symlink():
            raise ValueError("应用源码及资源目录不能是符号链接。")
    for path in sorted(source.glob("*.py")):
        copy_regular(path, destination / path.name)
    for name in WEB_FILES:
        copy_regular(source / "web" / name, destination / "web" / name)
    fixture = source / "fixtures" / "framework-workbench"
    copy_regular(fixture / "manifest.json", destination / "fixtures" / "framework-workbench" / "manifest.json")
    for path in sorted((fixture / "source").rglob("*")):
        if path.is_symlink():
            raise ValueError("演示资源不能是符号链接。")
        if path.is_file() and path.suffix.lower() in {".cbl", ".cpy"}:
            copy_regular(path, destination / "fixtures" / "framework-workbench" / path.relative_to(fixture))
    return destination


def collect_licenses(destination):
    """Preserve dependency notices without copying package source files."""
    destination.mkdir(parents=True)
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name", "dependency")
        safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
        for record in distribution.files or ():
            if not any(part.lower().startswith(("license", "copying", "notice")) for part in record.parts):
                continue
            path = Path(distribution.locate_file(record))
            if path.is_file() and path.suffix.lower() not in FORBIDDEN_SUFFIXES:
                # A digest avoids collisions between differently nested notices.
                tag = hashlib.sha256(str(record).encode()).hexdigest()[:10]
                copy_regular(path, destination / safe_name / f"{tag}-{path.name}")
    python_license = __import__("sysconfig").get_path("stdlib")
    for candidate in (Path(python_license) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE.txt"):
        if candidate.is_file():
            copy_regular(candidate, destination / "Python-LICENSE.txt")
            break
    else:
        # CPython ships the complete license text with its site module.
        import builtins
        builtins.license._Printer__setup()
        (destination / "Python-LICENSE.txt").write_text("\n".join(builtins.license._Printer__lines), encoding="utf-8")
    (destination / "NOTICE.txt").write_text(
        "Dependency notices accompany this application. The bundled synthetic COBOL files are demonstration data.\n"
        "Python dependencies and their versions are recorded in BUILD-INFO.json.\n", encoding="utf-8")


def compiler_command(source, output, *, system=None, jobs=4):
    system = sys.platform if system is None else system
    command = [sys.executable, "-m", "nuitka", "--mode=app-dist",
               f"--output-dir={output}", f"--output-filename={APP_NAME}",
               f"--jobs={jobs}", "--assume-yes-for-downloads", "--update-check=never", "--python-flag=isolated",
               "--include-package-data=certifi",
               "--nofollow-import-to=tkinter,pytest,unittest",
               f"--include-data-dir={source / 'web'}=web",
               f"--include-data-dir={source / 'fixtures'}=fixtures",
               f"--include-data-dir={source / 'licenses'}=licenses",
               f"--report={output / 'compilation-report.xml'}"]
    if system == "darwin":
        command += ["--macos-app-name=" + APP_NAME,
                    "--macos-app-version=1.0", "--macos-signed-app-name=org.localworkbench.desktop",
                    "--include-module=webview.platforms.cocoa"]
    elif system == "win32":
        command += ["--windows-console-mode=disable", "--msvc=latest",
                    "--include-module=webview.platforms.winforms", "--include-module=webview.platforms.edgechromium"]
    else:
        raise ValueError("Unsupported build platform")
    icon = ROOT / "assets" / ("app.icns" if system == "darwin" else "app.ico")
    if icon.is_symlink():
        raise ValueError("应用图标必须为项目中的普通文件。")
    if icon.is_file():
        option = "--macos-app-icon" if system == "darwin" else "--windows-icon-from-ico"
        command.append(f"{option}={icon}")
    command.append(str(source / "desktop_app.py"))
    return command


def _forbidden_delivery_path(path):
    return (bool(set(path.parts) & FORBIDDEN_PARTS) or path.name.startswith(".env")
            or path.suffix.lower() in FORBIDDEN_SUFFIXES)


def _audit_archive(handle, depth=0):
    if depth > 3:
        raise ValueError("交付包包含过深的嵌套压缩文件。")
    with zipfile.ZipFile(handle) as archive:
        for name in archive.namelist():
            if _forbidden_delivery_path(Path(name.replace("\\", "/"))):
                raise ValueError(f"交付包内部压缩文件含源码、凭据或本地数据：{name}")
            if name.lower().endswith(".zip"):
                _audit_archive(io.BytesIO(archive.read(name)), depth + 1)


def audit_delivery(directory, *, public_ca_sha256=None):
    """Fail a delivery if Python source, credentials, or local data slipped in."""
    for path in directory.rglob("*"):
        relative = path.relative_to(directory)
        if path.is_symlink():
            if not path.resolve().is_relative_to(directory.resolve()):
                raise ValueError(f"交付包包含外部链接：{relative}")
        if (public_ca_sha256 and path.name == "cacert.pem" and path.parent.name == "certifi"
                and hashlib.sha256(path.read_bytes()).hexdigest() == public_ca_sha256):
            continue
        if _forbidden_delivery_path(relative):
            raise ValueError(f"交付包包含不允许分发的文件：{relative}")
        if path.is_file() and zipfile.is_zipfile(path):
            _audit_archive(path)


def run_checked(command, **kwargs):
    subprocess.run([str(item) for item in command], check=True, **kwargs)


def prepare_environment(args):
    environment = BUILD_ROOT / "venv"
    interpreter = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not interpreter.exists():
        print("首次构建：创建独立打包环境。", flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    if not args.skip_install:
        print("检查并安装固定版本的打包依赖……", flush=True)
        run_checked([interpreter, "-m", "pip", "--disable-pip-version-check", "install",
                     "-r", ROOT / "desktop-requirements.txt"])
    return interpreter


def minimum_macos_version(binary):
    if sys.platform != "darwin":
        return None
    details = subprocess.check_output(["/usr/bin/vtool", "-show-build", str(binary)], text=True)
    versions = re.findall(r"\bminos\s+([0-9.]+)", details)
    if not versions:
        raise ValueError("无法确认 macOS 最低系统要求，未生成交付包。")
    return max(versions, key=lambda value: tuple(map(int, value.split("."))))


def write_start_here(path, target, minimum_macos=None):
    launch = "完整解压，进入 COBOLWorkbench 文件夹，双击其中的 COBOLWorkbench.exe。请勿单独复制 EXE。" if target.startswith("windows") else "解压后将 COBOLWorkbench.app 拖到“应用程序”，双击打开。"
    path.write_text(
        "COBOL 业务分析工作台 · 桌面版\n\n" + launch + "\n"
        + (f"此包要求 macOS {minimum_macos} 或更新版本，架构：{target}。\n" if minimum_macos else "") +
        "使用者无需安装 Python、Git 或编译器；不会修改电脑已有的 Python。\n"
        "Windows 桌面组件不可用时自动改用现有默认浏览器，不要求安装组件。\n"
        "Windows 也可双击外层 OPEN-IN-BROWSER.bat 主动使用浏览器模式。\n"
        "浏览器模式控制窗口：是=重新打开页面，否=编辑接口配置，取消=退出工作台。\n"
        "关闭浏览器标签不会停止工作台；在控制窗口选择取消才会停止。\n"
        "首次启动后，在“工作台 → 编辑接口配置”填写自己的接口地址、模型与密钥，保存后重新载入页面。\n"
        "在工作台选择本机源码目录和独立结果目录，即可继续使用原有业务分析流程。\n"
        "应用更新：关闭旧应用，替换应用文件。用户配置和分析结果在应用之外保存。\n"
        "Windows 配置：%LOCALAPPDATA%\\COBOLWorkbench\\.env\n"
        "macOS 配置：~/Library/Application Support/COBOLWorkbench/.env\n"
        "本包不附带开发者密钥、真实业务源码、私有框架资料或历史数据库。\n"
        "本版本未完成商业代码签名与公证；企业受管设备可能要求管理员批准。\n"
        "第三方许可见应用内 licenses 目录；版本信息见 BUILD-INFO.json。\n",
        encoding="utf-8-sig")


def build(args):
    target = target_label()
    if not (3, 10) <= sys.version_info[:2] <= (3, 14):
        raise ValueError("请使用 Python 3.10–3.14，推荐 Python 3.12。")
    if sys.platform == "win32" and sys.version_info[:2] > (3, 13):
        raise ValueError("Windows 桌面依赖请使用 Python 3.10–3.13，推荐 Python 3.12。")
    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="run-", dir=BUILD_ROOT))
    source = stage_sources(run / "source")
    collect_licenses(source / "licenses")
    output = run / "compiled"
    environment = os.environ.copy()
    environment["NUITKA_CACHE_DIR"] = str(BUILD_ROOT / "cache")
    print(f"编译 {target} 桌面应用，首次可能需要较长时间……", flush=True)
    run_checked(compiler_command(source, output, jobs=args.jobs), env=environment, cwd=source)
    return finish_delivery(args, source, output, run, target)


def finish_delivery(args, source, output, run, target):
    """Verify an already compiled product before assembling its delivery."""
    import certifi

    candidates = list(output.glob("*.app" if sys.platform == "darwin" else "*.dist"))
    if len(candidates) != 1:
        raise ValueError("编译输出不完整，未生成交付包。")
    product = candidates[0]
    binary = product / (f"Contents/MacOS/{APP_NAME}" if sys.platform == "darwin" else f"{APP_NAME}.exe")
    if not binary.is_file():
        raise ValueError(f"找不到应用启动文件：{binary}")
    minimum_macos = minimum_macos_version(binary)
    public_ca_sha256 = hashlib.sha256(Path(certifi.where()).read_bytes()).hexdigest()
    audit_delivery(product, public_ca_sha256=public_ca_sha256)
    # A separate, clean profile must pass with no developer credentials or Python path.
    check_env = {key: value for key, value in os.environ.items()
                 if not key.startswith(("COMPANY_", "PYTHON", "WORKBENCH_"))
                 and key not in {"FRAMEWORK_REFERENCE_PATH", "AGENT_SETTINGS_PATH"}}
    report = run / "self-test.json"
    run_checked([binary, "--self-test", "--self-test-result", report, "--data-dir", run / "test-profile"],
                cwd=run, env=check_env, timeout=120)
    result = json.loads(report.read_text(encoding="utf-8"))
    if result.get("ok") is not True:
        raise ValueError("应用自检未通过，未生成交付包。")
    label = datetime.now().strftime("%Y%m%d-%H%M%S")
    parent = args.output_dir.expanduser().resolve() / target
    parent.mkdir(parents=True, exist_ok=True)
    delivery = Path(tempfile.mkdtemp(prefix=label + "-", dir=parent))
    package = delivery / APP_NAME
    package.mkdir()
    destination = package / (APP_NAME + ".app" if sys.platform == "darwin" else APP_NAME)
    if sys.platform == "darwin":
        # Bundle data can carry code signatures in extended attributes.
        # Preserve them along with the native binaries' embedded signatures.
        run_checked(["/usr/bin/ditto", product, destination])
        run_checked(["/usr/bin/codesign", "--verify", "--deep", "--strict", destination])
    else:
        shutil.copytree(product, destination, symlinks=True)
    write_start_here(package / "START-HERE.txt", target, minimum_macos)
    if sys.platform == "win32":
        (package / "OPEN-IN-BROWSER.bat").write_bytes(
            b'@echo off\r\nsetlocal\r\nstart "" "%~dp0COBOLWorkbench\\COBOLWorkbench.exe" --browser\r\n')
    metadata = {"application": APP_NAME, "target": target, "built_at": datetime.now(timezone.utc).isoformat(),
                "python": platform.python_version(), "self_test": result, "minimum_macos": minimum_macos,
                "icons": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in (ROOT / "assets" / "app.ico", ROOT / "assets" / "app.icns") if path.is_file()},
                "dependencies": sorted(f"{d.metadata['Name']}=={d.version}" for d in importlib.metadata.distributions()),
                "source_fingerprint": hashlib.sha256(b"".join(
                    str(p.relative_to(source)).encode() + p.read_bytes()
                    for p in sorted(source.rglob("*")) if p.is_file())).hexdigest()}
    (package / "BUILD-INFO.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    audit_delivery(package, public_ca_sha256=public_ca_sha256)
    archive = delivery / f"{APP_NAME}-{target}.zip"
    if sys.platform == "darwin":
        run_checked(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", package, archive])
    else:
        shutil.make_archive(str(archive.with_suffix("")), "zip", root_dir=delivery, base_dir=APP_NAME)
    digest = hashlib.sha256()
    with archive.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    checksum = digest.hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{checksum}  {archive.name}\n", encoding="ascii")
    print(f"\n完成。只需发送这个 ZIP：\n{archive}\n", flush=True)
    return archive


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release")
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--skip-install", action="store_true", help="Use the existing isolated build dependencies offline")
    parser.add_argument("--check", action="store_true", help="Print build prerequisites without installing or compiling")
    parser.add_argument("--prepared", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        target = target_label()
        if args.jobs < 1:
            parser.error("--jobs must be positive")
        if args.check:
            print(f"目标：{target}\nPython：{platform.python_version()}\n输出：{args.output_dir}\n"
                  "准备：Python 3.12；Windows 需要 C++ Build Tools，macOS 需要 Command Line Tools。")
            return 0
        if not args.prepared:
            interpreter = prepare_environment(args)
            forwarded = sys.argv[1:] if argv is None else argv
            return subprocess.call([str(interpreter), str(Path(__file__).resolve()), *forwarded, "--prepared"])
        build(args)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"\n打包未完成：{error}\n请查看 docs/desktop-app.md；现有交付版本未被覆盖。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
