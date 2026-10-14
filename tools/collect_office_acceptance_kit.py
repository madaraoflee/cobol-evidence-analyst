"""Assemble a portable office kit around an already accepted, unchanged EXE."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "poc"))
from release_acceptance import evaluate_release, OFFICE_BASELINE_CHECKS
from desktop_acceptance import digest, inspect_package, require
from office_acceptance_cases import generate_suite
from office_acceptance_app import load_kit


CANDIDATE_FILES = ("COBOLWorkbench-windows-x64.zip", "COBOLWorkbench-windows-x64.zip.sha256",
    "BUILD-INFO.json", "COMPILED-ACCEPTANCE.json", "CANDIDATE-GATE.json",
    "ACCEPTANCE-MANIFEST.json", "CANDIDATE-POLICY.json")


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def assemble(*, candidate, helper_archive, public_source, destination, source_commit,
             helper_commit, max_model_requests=3, max_question_seconds=120, p95_question_seconds=60):
    candidate, helper_archive = Path(candidate).resolve(strict=True), Path(helper_archive).resolve(strict=True)
    destination = Path(destination).resolve()
    require(not destination.exists(), "OFFICE_DESTINATION_EXISTS")
    require(type(max_model_requests) is int and 1 <= max_model_requests <= 10,
            "OFFICE_REQUEST_POLICY_INVALID")
    require(1 <= p95_question_seconds <= max_question_seconds <= 600, "OFFICE_LATENCY_POLICY_INVALID")
    decision = evaluate_release(package=candidate / CANDIDATE_FILES[0],
        compiled_report=candidate / "COMPILED-ACCEPTANCE.json", manifest=candidate / "ACCEPTANCE-MANIFEST.json",
        policy=candidate / "CANDIDATE-POLICY.json")
    require(decision["candidate_allowed"], "OFFICE_UNACCEPTED_CANDIDATE")
    artifact = inspect_package(candidate / CANDIDATE_FILES[0], source_commit)
    with tempfile.TemporaryDirectory(prefix="office-kit-") as temporary:
        stage = Path(temporary) / "OfficeAcceptanceKit"
        stage.mkdir()
        (stage / "candidate").mkdir()
        for name in CANDIDATE_FILES:
            source = candidate / name
            require(source.is_file() and not source.is_symlink(), "OFFICE_CANDIDATE_FILE_INVALID")
            shutil.copyfile(source, stage / "candidate" / name)
        generated = generate_suite(Path(temporary) / "suite", public_source, include_scale=True)
        shutil.copytree(generated["source"], stage / "sources")
        (stage / "cases").mkdir()
        for name in ("LIVE-CASES.json", "SOURCE-LOCK.json", "WINDOWS-MEASUREMENT-PLAN.json"):
            shutil.copyfile(Path(temporary) / "suite" / name, stage / "cases" / name)
        shutil.copyfile(Path(temporary) / "suite/PUBLIC-SOURCE-LICENSE.txt", stage / "PUBLIC-SOURCE-LICENSE.txt")
        (stage / "contracts").mkdir()
        for original, name in (("manifest.json", "public-manifest.json"), ("cases.json", "public-cases.json")):
            shutil.copyfile(ROOT / "docs/eval/public-business-v1" / original, stage / "contracts" / name)
        shutil.copytree(ROOT / "tools/fixtures", stage / "fixtures")
        with zipfile.ZipFile(helper_archive) as archive:
            entries = archive.infolist()
            paths = [entry.filename for entry in entries]
            require(paths and len(paths) <= 4000 and len(paths) == len({path.casefold() for path in paths}),
                    "OFFICE_HELPER_ARCHIVE_DUPLICATES")
            require(sum(entry.file_size for entry in entries) <= 1024 ** 3, "OFFICE_HELPER_EXPANDED_SIZE")
            for entry in entries:
                name, relative = entry.filename, PurePosixPath(entry.filename)
                require(not relative.is_absolute() and ".." not in relative.parts and ":" not in name
                    and "\\" not in name and not stat.S_ISLNK(entry.external_attr >> 16),
                    "OFFICE_HELPER_ARCHIVE_UNSAFE")
            require(archive.testzip() is None, "OFFICE_HELPER_CRC_FAILED")
            archive.extractall(stage / "helper")
        helper = stage / "helper/OfficeAcceptance/OfficeAcceptance.exe"
        metadata_path = stage / "helper/OfficeAcceptance/HELPER-BUILD-INFO.json"
        require(helper.is_file(), "OFFICE_HELPER_EXE_REQUIRED")
        binary = helper.read_bytes()
        pe = struct.unpack_from("<I", binary, 0x3C)[0] if len(binary) > 64 else len(binary)
        require(binary[:2] == b"MZ" and pe + 6 <= len(binary) and binary[pe:pe + 4] == b"PE\0\0"
                and struct.unpack_from("<H", binary, pe + 4)[0] == 0x8664, "OFFICE_HELPER_EXE_NOT_X64")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        require(metadata.get("schema_version") == "office-helper-build/v1"
            and re.fullmatch(r"[0-9a-f]{40}", helper_commit) and metadata.get("source_commit") == helper_commit
            and re.fullmatch(r"[0-9a-f]{64}", metadata.get("helper_source_sha256", ""))
            and metadata.get("target") == "windows-x64" and metadata.get("source_dirty") is False
            and metadata.get("workbench_rebuilt") is False and metadata.get("self_test", {}).get("ok") is True
            and metadata.get("self_test", {}).get("gui_started") is True
            and metadata.get("self_test", {}).get("native_platform") == "Windows"
            and metadata.get("self_test", {}).get("model_calls") == 0, "OFFICE_HELPER_NOT_NATIVE_TESTED")
        policy = {"schema_version": "release-acceptance-policy/v1", "source_commit": source_commit,
            "manifest_sha256": digest(stage / "candidate/ACCEPTANCE-MANIFEST.json"),
            "live_cases_sha256": digest(stage / "cases/LIVE-CASES.json"),
            "office_required_checks": list(OFFICE_BASELINE_CHECKS), "source_format": "auto", "encoding": "utf-8",
            "live_execution": {"max_model_requests": max_model_requests, "max_question_seconds": max_question_seconds},
            "live_limits": {"question_seconds": max_question_seconds, "p95_question_seconds": p95_question_seconds,
                            "min_evidence_recall": 1},
            "threshold_scope": "predeclared_acceptance_targets_not_measured_company_baseline"}
        write_json(stage / "cases/LIVE-POLICY.json", policy)
        start = '@echo off\r\ncd /d "%~dp0"\r\n"helper\\OfficeAcceptance\\OfficeAcceptance.exe" --kit "%~dp0OFFICE-KIT.json"\r\nif errorlevel 1 pause\r\n'
        (stage / "START-ACCEPTANCE.cmd").write_bytes(start.encode("ascii"))
        shutil.copyfile(ROOT / "docs/office-acceptance-steps.md", stage / "START-HERE.md")
        files = {path.relative_to(stage).as_posix(): digest(path) for path in sorted(stage.rglob("*")) if path.is_file()}
        contract = {"schema_version": "office-acceptance-kit/v1", "source_commit": source_commit,
            "artifact": artifact, "helper_source_commit": metadata["source_commit"],
            "helper_entry": "helper/OfficeAcceptance/OfficeAcceptance.exe", "source_root": "sources",
            "archive": "candidate/COBOLWorkbench-windows-x64.zip", "cases": "cases/LIVE-CASES.json",
            "policy": "cases/LIVE-POLICY.json", "compiled_report": "candidate/COMPILED-ACCEPTANCE.json",
            "manifest": "candidate/ACCEPTANCE-MANIFEST.json", "files": files,
            "model_calls_during_assembly": 0, "company_office_acceptance_completed": False,
            "publication_performed": False}
        write_json(stage / "OFFICE-KIT.json", contract)
        checked = load_kit(stage / "OFFICE-KIT.json")
        require(checked["artifact"] == artifact, "OFFICE_ASSEMBLED_ARTIFACT_CHANGED")
        destination.mkdir(parents=True)
        target = destination / "COBOLWorkbench-office-acceptance-windows-x64.zip"
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(stage.parent).as_posix())
        (destination / (target.name + ".sha256")).write_text(digest(target) + "  " + target.name + "\n", encoding="ascii")
        write_json(destination / "KIT-BUILD-RECEIPT.json", {"schema_version": "office-kit-receipt/v1",
            "archive_sha256": digest(target), "office_kit_sha256": digest(stage / "OFFICE-KIT.json"),
            "artifact": artifact, "helper_source_commit": metadata["source_commit"],
            "user_turn_count": checked["turns"], "max_model_requests": checked["request_budget"],
            "source_files": generated["source_files"], "model_calls": 0, "publication_performed": False})
        return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("candidate", "helper-archive", "public-source", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--helper-commit", required=True)
    args = parser.parse_args(argv)
    print(assemble(**vars(args)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
