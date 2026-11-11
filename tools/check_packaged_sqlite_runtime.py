"""Independently verify the SQLite runtime used by one packaged Windows EXE.

This writes a separate component acceptance report. It does not change the
frozen joint-case report, build an application, or request a model response.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "poc")]
from tools.desktop_acceptance import BUILD_MEMBER, digest, extract_package, inspect_package, require
from sqlite_runtime_probe import APPROVED_PIN, validate_report


REPORT_NAME = "SQLITE-RUNTIME-ACCEPTANCE.json"
IDENTITY_FIELDS = ("version", "source_id", "loaded_dll_sha256")


def _inside(path, root):
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _error_code(error):
    text = str(error)
    return text if re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", text) else "PACKAGED_SQLITE_RUNTIME_CHECK_FAILED"


def check_package(*, archive, source_commit, pin, output):
    """Run only the extracted EXE; there is no interpreter/SDK fallback."""
    output = Path(output)
    require(not output.exists() and not output.is_symlink(), "PACKAGED_SQLITE_OUTPUT_MUST_BE_NEW")
    output.mkdir(parents=True, exist_ok=False)
    output = output.resolve(strict=True)
    report = {"schema_version": "packaged-sqlite-runtime-acceptance/v1", "status": "failed",
              "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "execution_surface": "native_windows_exe_self_test", "source_commit": source_commit,
              "model_calls": None, "external_model_calls": None, "paid_model_calls": None,
              "errors": []}
    stage = "preflight"
    try:
        require(sys.platform == "win32", "PACKAGED_SQLITE_NATIVE_WINDOWS_REQUIRED")
        require(isinstance(source_commit, str) and re.fullmatch(r"[0-9a-f]{40}", source_commit),
                "PACKAGED_SQLITE_SOURCE_COMMIT_INVALID")
        archive, pin = Path(archive).resolve(strict=True), Path(pin)
        require(pin.is_file() and not pin.is_symlink() and pin.stat().st_size <= 65536,
                "PACKAGED_SQLITE_PIN_INVALID")
        pin_bytes = pin.read_bytes()
        approved = json.loads(pin_bytes)
        require(approved == APPROVED_PIN, "PACKAGED_SQLITE_PIN_NOT_APPROVED")
        pin_hash = hashlib.sha256(pin_bytes).hexdigest()
        pin_snapshot = output / "runtime-pin.json"
        with pin_snapshot.open("xb") as handle:
            handle.write(pin_bytes)
        # Validate and execute a private immutable copy, rather than extracting
        # from an input ZIP that another process can replace between checks.
        package = output / "input-package.zip"
        shutil.copyfile(archive, package)
        stage = "package_inspection"
        artifact = inspect_package(package, source_commit)
        require(digest(archive) == artifact["archive_sha256"], "PACKAGED_SQLITE_ARCHIVE_CHANGED")
        report.update({key: artifact[key] for key in
                       ("archive_sha256", "binary_path", "binary_sha256", "application_sha256", "source_dirty")})
        report["runtime_platform"] = artifact["runtime_platform"]
        report["pin_sha256"] = pin_hash
        report["pin"] = approved
        with zipfile.ZipFile(package) as handle:
            metadata = json.loads(handle.read(BUILD_MEMBER))
        binary = extract_package(package, output / "extracted")
        product = binary.parent.resolve(strict=True)
        require(digest(binary) == artifact["binary_sha256"], "PACKAGED_SQLITE_EXTRACTED_EXE_MISMATCH")
        dlls = [path for path in (output / "extracted").rglob("*") if path.name.casefold() == "sqlite3.dll"]
        require(len(dlls) == 1 and dlls[0].is_file() and not dlls[0].is_symlink()
                and _inside(dlls[0], product) and digest(dlls[0]) == approved["dll_sha256"],
                "PACKAGED_SQLITE_DLL_MISMATCH")
        dll = dlls[0]
        report["packaged_dll"] = {"path": dll.relative_to(output / "extracted").as_posix(),
                                  "sha256": digest(dll)}
        stage = "native_self_test"
        native_path = output / "native-self-test.json"
        profile = output / "disposable-profile"
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("COMPANY_", "PYTHON", "WORKBENCH_"))
                       and key not in {"FRAMEWORK_REFERENCE_PATH", "AGENT_SETTINGS_PATH"}}
        subprocess.run([str(binary), "--self-test", "--sqlite-runtime-pin", str(pin_snapshot),
                        "--self-test-result", str(native_path), "--data-dir", str(profile)],
                       cwd=output, env=environment, check=True, timeout=120,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        require(native_path.is_file() and not native_path.is_symlink() and native_path.stat().st_size <= 1024 * 1024,
                "PACKAGED_SQLITE_SELF_TEST_RESULT_MISSING")
        native = json.loads(native_path.read_text(encoding="utf-8"))
        require(isinstance(native, dict) and native.get("ok") is True
                and type(native.get("model_calls")) is int and native["model_calls"] == 0,
                "PACKAGED_SQLITE_NATIVE_SELF_TEST_FAILED")
        stage = "runtime_report"
        probe = native.get("sqlite_runtime")
        validate_report(probe, approved, product, identity_kind="compiled")
        require(Path(probe["loaded_dll_path"]).resolve(strict=True) == dll.resolve(strict=True),
                "PACKAGED_SQLITE_LOADED_DLL_MISMATCH")
        report["sqlite_runtime"] = probe
        report["self_test"] = native
        stage = "build_identity"
        recorded = metadata.get("self_test", {}).get("sqlite_runtime")
        require(isinstance(recorded, dict)
                and all(recorded.get(key) == probe.get(key) for key in IDENTITY_FIELDS),
                "PACKAGED_SQLITE_BUILD_IDENTITY_MISMATCH")
        report["build_self_test_sqlite_runtime"] = recorded
        stage = "final_identity"
        require(inspect_package(package, source_commit) == artifact
                and digest(archive) == artifact["archive_sha256"]
                and digest(binary) == artifact["binary_sha256"]
                and digest(dll) == approved["dll_sha256"]
                and digest(pin_snapshot) == pin_hash
                and hashlib.sha256(pin.read_bytes()).hexdigest() == pin_hash,
                "PACKAGED_SQLITE_INPUT_CHANGED_DURING_CHECK")
        report.update(status="passed", model_calls=0, external_model_calls=0, paid_model_calls=0)
    except Exception as error:
        report["errors"].append({"stage": stage, "type": type(error).__name__, "code": _error_code(error)})
    with (output / REPORT_NAME).open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--pin", type=Path, default=ROOT / "tools/sqlite-runtime-pin.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = check_package(**vars(args))
    print(json.dumps({"status": report["status"], "report": str(args.output / REPORT_NAME), "errors": report["errors"]}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
