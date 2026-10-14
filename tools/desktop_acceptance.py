"""Run frozen joint regressions through the HTTP API of the final delivery.

The only model in this runner is a synthetic loopback provider. Passing this
suite permits a candidate artifact, never a claim of real-model acceptance.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import platform
import re
import secrets
import stat
import struct
import subprocess
import sys
import time
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DEFAULT_MANIFEST = ROOT / "docs/eval/desktop-joint-v1/manifest.json"
BINARY_MEMBER = "COBOLWorkbench/COBOLWorkbench/COBOLWorkbench.exe"
BUILD_MEMBER = "COBOLWorkbench/BUILD-INFO.json"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(condition, code):
    if not condition:
        raise ValueError(code)


def inspect_package(archive, source_commit):
    """Inspect before extraction; bind the measured executable to its ZIP."""
    archive = Path(archive).resolve(strict=True)
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        names = [item.filename for item in entries]
        require(len(names) <= 4000 and len({name.casefold() for name in names}) == len(names),
                "DELIVERY_DUPLICATE_OR_EXCESSIVE_MEMBERS")
        require(sum(item.file_size for item in entries) <= 1024 ** 3, "DELIVERY_EXPANDED_SIZE")
        for item in entries:
            path = PurePosixPath(item.filename)
            require(not path.is_absolute() and ".." not in path.parts and "\\" not in item.filename
                    and ":" not in item.filename and not stat.S_ISLNK(item.external_attr >> 16),
                    "DELIVERY_UNSAFE_MEMBER")
        require(BINARY_MEMBER in names and BUILD_MEMBER in names, "DELIVERY_MEMBERS_MISSING")
        require(package.testzip() is None, "DELIVERY_CRC_FAILED")
        metadata = json.loads(package.read(BUILD_MEMBER))
        require(metadata.get("target") == "windows-x64", "DELIVERY_TARGET_MISMATCH")
        require(metadata.get("source_commit") == source_commit and metadata.get("source_dirty") is False,
                "DELIVERY_SOURCE_IDENTITY_MISMATCH")
        require(re.fullmatch(r"[0-9a-f]{64}", metadata.get("source_fingerprint", "")),
                "DELIVERY_SOURCE_FINGERPRINT_MISSING")
        checks = metadata.get("self_test", {})
        require(checks.get("ok") is True and checks.get("json_accelerated") is True
                and checks.get("model_calls") == 0, "DELIVERY_SELF_TEST_FAILED")
        binary = package.read(BINARY_MEMBER)
        require(len(binary) > 64 and binary[:2] == b"MZ", "DELIVERY_NOT_WINDOWS_EXECUTABLE")
        offset = struct.unpack_from("<I", binary, 0x3C)[0]
        require(offset + 6 <= len(binary) and binary[offset:offset + 4] == b"PE\0\0"
                and struct.unpack_from("<H", binary, offset + 4)[0] == 0x8664,
                "DELIVERY_NOT_X64_EXECUTABLE")
    return {"archive_sha256": digest(archive), "binary_path": BINARY_MEMBER,
            "binary_sha256": hashlib.sha256(binary).hexdigest(),
            "application_sha256": metadata["source_fingerprint"],
            "source_commit": source_commit, "source_dirty": False,
            "execution_surface": "native_windows_http", "runtime_platform": "Windows"}


def extract_package(archive, destination):
    # inspect_package has already validated every member, including duplicates.
    with zipfile.ZipFile(archive) as package:
        package.extractall(destination)
    return Path(destination) / BINARY_MEMBER


def source_provenance(source_root, source_commit):
    paths = sorted(path for path in (source_root / "poc").rglob("*.py")
                   if "tests" not in path.relative_to(source_root / "poc").parts)
    identity = hashlib.sha256()
    for path in paths:
        identity.update(path.relative_to(source_root / "poc").as_posix().encode() + b"\0"
                        + path.read_bytes() + b"\n")
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=source_root, text=True))
    return {"source_commit": source_commit, "source_dirty": dirty,
            "execution_surface": "source_python_http", "runtime_platform": platform.system(),
            "archive_sha256": None, "binary_path": None, "binary_sha256": None,
            "application_sha256": identity.hexdigest()}


def sample_failures(manifest, rows):
    """Missing observations and failed dimensions cannot become a green total."""
    errors = []
    specs = {case["id"]: case for case in manifest["cases"]}
    require(specs and len(specs) == len(manifest["cases"]), "MANIFEST_CASE_IDS_INVALID")
    run_ids = [row.get("run_id") for row in rows]
    if any(not isinstance(value, str) or not value.strip() for value in run_ids) or len(set(run_ids)) != len(run_ids):
        errors.append("RUN_IDS_MISSING_OR_DUPLICATED")
    for row in rows:
        if row.get("id") not in specs:
            errors.append("UNEXPECTED_CASE:" + str(row.get("id")))
    for case_id, spec in specs.items():
        samples = [row for row in rows if row.get("id") == case_id]
        if sorted(row.get("sample_index", 0) for row in samples) != list(range(1, spec["repeats"] + 1)):
            errors.append("SAMPLES_MISSING_OR_DUPLICATED:" + case_id)
        require(spec.get("assertions"), "MANIFEST_ASSERTIONS_EMPTY")
        for row in samples:
            label = case_id + "/" + str(row.get("sample_index"))
            if (row.get("turn_id") != str(spec.get("turn_id", "1"))
                    or not isinstance(row.get("run_id"), str) or not row["run_id"].strip()):
                errors.append("SAMPLE_IDENTITY_FAILED:" + label)
            if set(row.get("checks", {})) != set(spec["assertions"]):
                errors.append("CHECK_SET_MISMATCH:" + label)
            for check in spec["assertions"]:
                if row.get("checks", {}).get(check) is not True:
                    errors.append("CHECK_FAILED:" + label + ":" + check)
            timings = row.get("timings", {})
            reused = row.get("index_reused")
            if (type(reused) is not bool or (row.get("sample_index") == 1 and reused is not False)
                    or row.get("question_index_reused") is not True):
                errors.append("INDEX_REUSE_FAILED:" + label)
            for name, maximum in spec.get("limits", {}).items():
                value = timings.get(name)
                if name == "cold_index_seconds" and reused is True and value is None:
                    continue
                if (type(value) not in {int, float} or not math.isfinite(value)
                        or value < 0 or value > maximum):
                    errors.append("TIMING_FAILED:" + label + ":" + name)
            elapsed, question_time = row.get("elapsed_seconds"), timings.get("question_seconds")
            if (type(elapsed) not in {int, float} or not math.isfinite(elapsed)
                    or type(question_time) not in {int, float} or not math.isfinite(question_time)
                    or abs(elapsed - question_time) > .05):
                errors.append("QUESTION_TIMING_MISMATCH:" + label)
            if spec.get("kind", "answer") == "answer":
                if not re.fullmatch(r"[0-9a-f]{64}", row.get("answer_sha256") or ""):
                    errors.append("ANSWER_MISSING:" + label)
            if row.get("error"):
                errors.append("CASE_ERROR:" + label)
            if row.get("status") not in {None, "passed"}:
                errors.append("CASE_STATUS_FAILED:" + label)
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--archive", type=Path)
    target.add_argument("--source-root", type=Path, help="Development preview; never native acceptance.")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--public-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", help="Debug subset; cannot pass the release gate.")
    args = parser.parse_args(argv)
    require(re.fullmatch(r"[0-9a-f]{40}", args.source_commit), "SOURCE_COMMIT_INVALID")
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": "compiled-workbench-acceptance/v1",
              "report_execution_id": secrets.token_hex(16),
              "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "transport_kind": "synthetic", "external_model_calls": 0, "paid_model_calls": 0,
              "real_model_accuracy": None, "business_answer_correctness": None,
              "selection_complete": not bool(args.case), "cases": [], "errors": [],
              "manifest_sha256": digest(args.manifest), "status": "failed"}
    started = time.monotonic()
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if args.case:
            requested = set(args.case)
            require(requested <= {row["id"] for row in manifest["cases"]}, "UNKNOWN_DEBUG_CASE")
            manifest = {**manifest, "cases": [row for row in manifest["cases"] if row["id"] in requested]}
        if args.archive:
            require(sys.platform == "win32", "NATIVE_WINDOWS_EXECUTION_REQUIRED")
            report.update(inspect_package(args.archive, args.source_commit))
            binary = extract_package(args.archive, root / "extracted")
            require(digest(binary) == report["binary_sha256"], "EXTRACTED_BINARY_MISMATCH")
            command = [str(binary)]
        else:
            source_root = args.source_root.resolve(strict=True)
            report.update(source_provenance(source_root, args.source_commit))
            command = [sys.executable, str(source_root / "poc/desktop_app.py")]
        from desktop_acceptance_scenarios import run_scenarios
        from desktop_acceptance_cases import run_cases
        for owner, runner in (("runtime", run_scenarios), ("cases", run_cases)):
            selected = {**manifest, "cases": [row for row in manifest["cases"] if row["owner"] == owner]}
            if not selected["cases"]:
                continue
            print(json.dumps({"stage": owner, "cases": [row["id"] for row in selected["cases"]]}), flush=True)
            try:
                options = {"public_source": args.public_source.resolve(strict=True)} if owner == "cases" else {}
                report["cases"].extend(runner(command, root / owner, selected, **options))
            except Exception as error:
                report["errors"].append({"owner": owner, "type": type(error).__name__, "message": str(error)})
        report["failures"] = sample_failures(manifest, report["cases"])
        require(digest(args.manifest) == report["manifest_sha256"], "MANIFEST_CHANGED_DURING_ACCEPTANCE")
        if args.archive:
            require(inspect_package(args.archive, args.source_commit) == {
                key: report[key] for key in ("archive_sha256", "binary_path", "binary_sha256",
                    "application_sha256", "source_commit", "source_dirty", "execution_surface", "runtime_platform")},
                "DELIVERY_CHANGED_DURING_ACCEPTANCE")
            require(digest(binary) == report["binary_sha256"], "BINARY_CHANGED_DURING_ACCEPTANCE")
        else:
            require(source_provenance(source_root, args.source_commit)["application_sha256"]
                    == report["application_sha256"], "SOURCE_CHANGED_DURING_PREVIEW")
        report["status"] = "passed" if not report["failures"] and not report["errors"] else "failed"
    except Exception as error:
        report["errors"].append({"type": type(error).__name__, "message": str(error)})
    report["elapsed_seconds"] = round(time.monotonic() - started, 6)
    output = root / "COMPILED-ACCEPTANCE.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(output),
                      "selection_complete": report["selection_complete"],
                      "failures": report.get("failures", []), "errors": report["errors"]}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
