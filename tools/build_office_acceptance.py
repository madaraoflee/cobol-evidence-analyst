"""Compile the local acceptance assistant, never rebuild the frozen workbench."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from build_app import audit_delivery, collect_licenses, copy_regular, source_identity

MODULES = ("office_acceptance_app.py", "desktop_live_acceptance.py", "desktop_acceptance.py",
           "desktop_acceptance_runtime.py", "desktop_acceptance_cases.py", "desktop_acceptance_scenarios.py",
           "acceptance_diagnostics.py", "office_acceptance_checks.py", "partial_acceptance_audit.py")


def stage_sources(destination, root=ROOT):
    destination = Path(destination)
    for name in MODULES:
        source = root / "tools" / name
        copy_regular(source, destination / "tools" / name)
    for source in sorted((root / "poc").glob("*.py")):
        copy_regular(source, destination / "poc" / source.name)
    fingerprint = hashlib.sha256()
    for source in sorted(destination.rglob("*.py")):
        fingerprint.update(source.relative_to(destination).as_posix().encode() + b"\0" + source.read_bytes() + b"\n")
    return fingerprint.hexdigest()


def compiler_command(stage, output, *, executable=None, jobs=4):
    return [executable or sys.executable, "-m", "nuitka", "--mode=standalone", "--msvc=latest",
        "--output-filename=OfficeAcceptance.exe", f"--output-dir={output}", f"--jobs={jobs}",
        "--windows-console-mode=force", "--enable-plugin=tk-inter", "--include-module=_json",
        "--assume-yes-for-downloads", "--update-check=never", "--python-flag=isolated",
        "--nofollow-import-to=pytest,unittest,webview", f"--report={output / 'helper-compilation-report.xml'}",
        str(stage / "tools" / "office_acceptance_app.py")]


def build(*, output, jobs=4):
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise ValueError("OFFICE_HELPER_WINDOWS_X64_BUILD_REQUIRED")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    identity = source_identity()
    if identity["source_dirty"] or not identity["source_commit"]:
        raise ValueError("OFFICE_HELPER_FROZEN_SOURCE_REQUIRED")
    with tempfile.TemporaryDirectory(prefix="office-acceptance-build-") as temporary:
        stage = Path(temporary) / "stage"
        fingerprint = stage_sources(stage)
        compilation = Path(temporary) / "compile"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(str(item) for item in (stage, stage / "tools", stage / "poc"))
        subprocess.run(compiler_command(stage, compilation, jobs=jobs), check=True, cwd=stage, env=environment)
        distributions = list(compilation.glob("*.dist"))
        if len(distributions) != 1 or not (distributions[0] / "OfficeAcceptance.exe").is_file():
            raise ValueError("OFFICE_HELPER_DISTRIBUTION_MISSING")
        delivery = Path(temporary) / "delivery" / "OfficeAcceptance"
        shutil.copytree(distributions[0], delivery)
        collect_licenses(delivery / "licenses")
        self_test = Path(temporary) / "helper-self-test.json"
        subprocess.run([delivery / "OfficeAcceptance.exe", "--self-test", "--self-test-result", self_test],
                       check=True, timeout=60)
        tested = json.loads(self_test.read_text(encoding="utf-8"))
        if tested.get("ok") is not True or tested.get("gui_started") is not True or tested.get("model_calls") != 0:
            raise ValueError("OFFICE_HELPER_NATIVE_SMOKE_FAILED")
        if source_identity() != identity:
            raise ValueError("OFFICE_HELPER_SOURCE_CHANGED_DURING_BUILD")
        metadata = {"schema_version": "office-helper-build/v1", **identity, "helper_source_sha256": fingerprint,
            "created_at_utc": datetime.now(timezone.utc).isoformat(), "target": "windows-x64",
            "entry_point": "OfficeAcceptance/OfficeAcceptance.exe", "self_test": tested,
            "python": platform.python_version(), "nuitka": importlib.metadata.version("Nuitka"),
            "workbench_rebuilt": False, "model_calls": 0}
        (delivery / "HELPER-BUILD-INFO.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        (delivery / "START-HERE.txt").write_text(
            "解压并保留全部 DLL 与 Tcl/Tk 文件。双击 OfficeAcceptance.exe，选择验收包的 OFFICE-KIT.json。\n"
            "先生成无网络计划，再选择公司电脑本机配置、CA 和报告目录。仅在已授权范围内执行真实问答。\n"
            "报告与配置均留本机，助手不会上传或发布。未评分、未确认设备或不确定远程请求均不得正式发布。\n",
            encoding="utf-8")
        audit_delivery(delivery)
        archive = output / "OfficeAcceptance-windows-x64.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as handle:
            for path in sorted(delivery.rglob("*")):
                if path.is_file():
                    handle.write(path, path.relative_to(delivery.parent).as_posix())
        expected = hashlib.sha256(archive.read_bytes()).hexdigest()
        (output / (archive.name + ".sha256")).write_text(expected + "  " + archive.name + "\n", encoding="utf-8")
        (output / "HELPER-BUILD-INFO.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        return archive


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args(argv)
    print(build(output=args.output, jobs=args.jobs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
