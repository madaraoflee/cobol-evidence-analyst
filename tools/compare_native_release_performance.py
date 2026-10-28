"""Compare pinned candidate executables serially on the current Windows runner.

This is a manual CI measurement, not office or real-model acceptance. The new
package has already passed the complete native suite in the same build job;
the two pinned baselines run only the unchanged large-repository scenario.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.desktop_acceptance import digest, inspect_package, sample_failures


BASELINES = (
    {"label": "previous", "tag": "v2026.10.27.1-rc.1",
     "source_commit": "3628a055d3578a98a7455c655aa62177eb38cc10",
     "archive_sha256": "da2185629ae5a003f860f5690ac14689f758faade375ca3bef90cdcc71387d24"},
    {"label": "current", "tag": "v2026.10.27.2-rc.1",
     "source_commit": "b636fbdd96ac4483b342c2a9dbce2e00a3e17caf",
     "archive_sha256": "bd0000862f1f5c5ffb9b958e9b563c4a242125bf0ec30af280c0ecb293fc6845"},
)
FROZEN_MANIFEST_SHA256 = "ffbb287833621ad1326a17f76d7c008065c703323f4114878680b10f2037fe06"
ARCHIVE_NAME = "COBOLWorkbench-windows-x64.zip"
CASE_ID = "large_repository"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def download_baseline(repository, baseline, directory):
    """Use only the selected repository and exact reviewed release bytes."""
    require(bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)), "REPOSITORY_INVALID")
    directory.mkdir(parents=True, exist_ok=False)
    metadata = json.loads(subprocess.check_output([
        "gh", "release", "view", baseline["tag"], "--repo", repository,
        "--json", "tagName,isDraft,isPrerelease,assets"], text=True))
    require(metadata.get("tagName") == baseline["tag"] and metadata.get("isDraft") is False
            and metadata.get("isPrerelease") is True, "BASELINE_NOT_PUBLIC_CANDIDATE")
    require(sum(asset.get("name") == ARCHIVE_NAME for asset in metadata.get("assets", [])) == 1,
            "BASELINE_ASSET_MISSING_OR_DUPLICATED")
    subprocess.run(["gh", "release", "download", baseline["tag"], "--repo", repository,
                    "--pattern", ARCHIVE_NAME, "--dir", str(directory)], check=True)
    archive = directory / ARCHIVE_NAME
    require(digest(archive) == baseline["archive_sha256"], "BASELINE_ARCHIVE_CHANGED")
    inspect_package(archive, baseline["source_commit"])
    return archive


def verified_report(path, manifest, *, source_commit, archive_sha256, complete):
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    require(report.get("status") == "passed" and not report.get("errors") and not report.get("failures"),
            "NATIVE_ACCEPTANCE_FAILED")
    require(report.get("execution_surface") == "native_windows_http"
            and report.get("runtime_platform") == "Windows", "NATIVE_WINDOWS_REQUIRED")
    require(report.get("source_commit") == source_commit and report.get("source_dirty") is False
            and report.get("archive_sha256") == archive_sha256, "NATIVE_PACKAGE_MISMATCH")
    require(report.get("transport_kind") == "synthetic" and report.get("external_model_calls") == 0
            and report.get("paid_model_calls") == 0, "OFFLINE_TRANSPORT_REQUIRED")
    require(report.get("manifest_sha256") == FROZEN_MANIFEST_SHA256
            and report.get("selection_complete") is complete, "NATIVE_CONTRACT_MISMATCH")
    selected = manifest if complete else {**manifest, "cases": [row for row in manifest["cases"] if row["id"] == CASE_ID]}
    require(not sample_failures(selected, report.get("cases", [])), "NATIVE_SAMPLE_CHECK_FAILED")
    require(isinstance(report.get("report_execution_id"), str) and report["report_execution_id"],
            "NATIVE_EXECUTION_ID_REQUIRED")
    return report


def summarize_report(report):
    rows = sorted((row for row in report["cases"] if row["id"] == CASE_ID), key=lambda row: row["sample_index"])
    require(len(rows) == 5 and [row["sample_index"] for row in rows] == list(range(1, 6)),
            "FIVE_LARGE_SAMPLES_REQUIRED")
    corpus = rows[0]["corpus"]
    require(all(row["corpus"] == corpus for row in rows), "CORPUS_CHANGED_WITHIN_RUN")
    return {
        "source_commit": report["source_commit"], "archive_sha256": report["archive_sha256"],
        "report_execution_id": report["report_execution_id"], "corpus": corpus,
        "sample_count": len(rows), "cold_import_seconds": rows[0]["timings"]["cold_index_seconds"],
        "first_question_seconds": rows[0]["timings"]["question_seconds"],
        "median_question_seconds": statistics.median(row["timings"]["question_seconds"] for row in rows),
        "median_warm_question_seconds": statistics.median(row["timings"]["question_seconds"] for row in rows[1:]),
        "median_primary_question_seconds": statistics.median(row["timings"]["primary_question_seconds"] for row in rows),
        "median_warm_primary_question_seconds": statistics.median(row["timings"]["primary_question_seconds"] for row in rows[1:]),
        "median_warm_scope_probe_seconds": statistics.median(row["timings"]["scope_probe_seconds"] for row in rows[1:]),
        "median_first_provider_seconds": statistics.median(row["timings"]["first_provider_seconds"] for row in rows),
        "samples": [{"sample_index": row["sample_index"], "run_id": row["run_id"],
            "timings": row["timings"], "checks": row["checks"],
            "source_evidence_sha256": row.get("source_evidence_sha256"),
            "investigation_rounds": row.get("acceptance_diagnostics", {}).get("question_investigation_rounds")}
            for row in rows],
    }


def compare_summaries(variants):
    require(len({row["report_execution_id"] for row in variants.values()}) == len(variants),
            "REUSED_EXECUTION_REPORT")
    corpora = [row["corpus"] for row in variants.values()]
    require(all(corpus == corpora[0] for corpus in corpora), "COMPARISON_CORPUS_MISMATCH")
    result = {}
    fields = ("cold_import_seconds", "median_question_seconds", "median_warm_question_seconds",
              "median_primary_question_seconds", "median_warm_primary_question_seconds",
              "median_warm_scope_probe_seconds", "median_first_provider_seconds")
    for before, after in (("previous", "current"), ("current", "optimized"), ("previous", "optimized")):
        result[f"{after}_over_{before}"] = {name: variants[after][name] / variants[before][name]
            if variants[before][name] > 0 else None for name in fields}
    return result


def run(args):
    require(sys.platform == "win32", "WINDOWS_RUNNER_REQUIRED")
    require(digest(args.manifest) == FROZEN_MANIFEST_SHA256, "PINNED_MANIFEST_CHANGED")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": "native-release-performance/v1", "status": "failed",
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "same_runner": True,
        "same_runner_basis": "sequential steps in one manual build job; optimized complete report generated immediately before comparison",
        "environment": {"platform": platform.platform(), "machine": platform.machine(), "processor": platform.processor()},
        "measurement_order": ["optimized", "previous", "current"], "repeated_order_rotation": False,
        "manifest_sha256": FROZEN_MANIFEST_SHA256, "external_model_calls": 0, "paid_model_calls": 0,
        "real_model_accuracy": None, "office_acceptance": False, "variants": {}, "errors": []}
    try:
        current_package = inspect_package(args.current_archive, args.source_commit)
        current = verified_report(args.current_report, manifest, source_commit=args.source_commit,
            archive_sha256=current_package["archive_sha256"], complete=True)
        (root / "OPTIMIZED-ACCEPTANCE.json").write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["variants"]["optimized"] = summarize_report(current)
        for baseline in BASELINES:
            print(json.dumps({"stage": "native_baseline", "label": baseline["label"], "tag": baseline["tag"]}), flush=True)
            directory = root / baseline["label"]
            archive = download_baseline(args.repository, baseline, directory / "download")
            output = directory / "acceptance"
            subprocess.run([sys.executable, str(ROOT / "tools/desktop_acceptance.py"), "--archive", str(archive),
                "--source-commit", baseline["source_commit"], "--manifest", str(args.manifest),
                "--public-source", str(args.public_source), "--case", CASE_ID, "--output", str(output)], check=True)
            measured = verified_report(output / "COMPILED-ACCEPTANCE.json", manifest,
                source_commit=baseline["source_commit"], archive_sha256=baseline["archive_sha256"], complete=False)
            report["variants"][baseline["label"]] = summarize_report(measured)
        report["ratios"] = compare_summaries(report["variants"])
        report["status"] = "passed"
    except Exception as error:
        report["errors"].append({"type": type(error).__name__, "message": str(error)})
    (root / "NATIVE-PERFORMANCE.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "ratios": report.get("ratios"), "errors": report["errors"]}), flush=True)
    return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--current-archive", type=Path, required=True)
    parser.add_argument("--current-report", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--public-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
