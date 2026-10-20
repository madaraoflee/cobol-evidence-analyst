"""Fail closed unless Windows regression shards cover one clean source exactly once.

This gate checks source-test reports only. It does not certify a compiled
application, model answers, target devices, or human release acceptance.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import sys


SCHEMA_VERSION = 1
REQUIRED_TEST_IDS = (
    "test_public_business_acceptance.PublicBusinessAcceptanceTests.test_default_pipeline_uses_real_versioned_source_and_repairs_wrong_claims",
    "test_file_question_obligations.FileQuestionObligationTests.test_actual_frozen_public_file_mapping_default_source_evidence_correct",
)
SUCCESS_OUTCOMES = {"passed", "skipped", "expected_failure"}


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _identifier_list(value):
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def _nonnegative_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def check_reports(reports, *, source_commit, expected_shards=4, required_test_ids=REQUIRED_TEST_IDS):
    """Validate (report name, decoded JSON) pairs without trusting totals alone."""
    problems = []
    merged = {"schema_version": SCHEMA_VERSION, "kind": "python_regression_aggregate",
              "status": "failed", "source_commit": source_commit, "runtime_platform": "Windows",
              "source_dirty": False, "expected_shard_count": expected_shards,
              "report_count": len(reports), "discovery": None, "tests_run": 0,
              "selected_ids": [], "test_results": [], "skipped": [], "expected_failures": [],
              "skipped_count": 0, "expected_failure_count": 0,
              "shards": [], "validation_errors": problems,
              "scope": "source_regressions_only_not_executable_or_human_acceptance"}
    if type(expected_shards) is not int or expected_shards <= 0:
        problems.append("expected_shards must be a positive integer")
        return merged
    if not isinstance(source_commit, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source_commit):
        problems.append("expected source_commit must be a full Git object ID")
    if len(reports) != expected_shards:
        problems.append(f"expected {expected_shards} reports, found {len(reports)}")
    indices, all_selected, all_executed = [], [], []
    reference = None
    for name, report in reports:
        def require(condition, message):
            if not condition:
                problems.append(f"{name}: {message}")
            return condition

        if not require(isinstance(report, dict), "report must be a JSON object"):
            continue
        require(type(report.get("schema_version")) is int and report["schema_version"] == SCHEMA_VERSION,
                "unsupported schema_version")
        require(report.get("source_commit") == source_commit, "source_commit does not match expected checkout")
        require(report.get("source_dirty") is False, "source_dirty must be false")
        require(report.get("runtime_platform") == "Windows", "runtime_platform must be Windows")
        require(report.get("status") == "passed", "runner status must be passed")
        require(type(report.get("shard_count")) is int and report["shard_count"] == expected_shards,
                "shard_count does not match expected shard count")
        index = report.get("shard_index")
        if require(type(index) is int and 0 <= index < expected_shards, "invalid shard_index"):
            indices.append(index)
        for key in ("failures", "errors", "unexpected_successes", "discovery_errors", "runner_errors"):
            require(isinstance(report.get(key), list) and not report[key], f"{key} must be an empty list")
        require(_nonnegative_number(report.get("elapsed_seconds")), "invalid elapsed_seconds")

        discovery = report.get("discovery")
        discovered = None
        if require(isinstance(discovery, dict), "missing discovery object"):
            ids = discovery.get("ids")
            if require(_identifier_list(ids) and bool(ids), "discovery.ids must be a nonempty identifier list"):
                discovered = ids
                require(len(set(ids)) == len(ids), "duplicate discovered test IDs")
                require(type(discovery.get("count")) is int and discovery["count"] == len(ids),
                        "discovery.count differs from discovered IDs")
                require(discovery.get("ids_sha256") == canonical_digest(sorted(ids)),
                        "discovery.ids_sha256 does not match full test list")
            sources = discovery.get("test_sources")
            valid_sources = isinstance(sources, list) and bool(sources)
            if valid_sources:
                for item in sources:
                    if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                        valid_sources = False
                        break
                    path = item["path"]
                    if (not isinstance(path, str) or not path.endswith(".py") or "\\" in path
                            or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts
                            or not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
                        valid_sources = False
                        break
            if require(valid_sources, "discovery.test_sources must contain relative Python paths and SHA256 hashes"):
                paths = [item["path"] for item in sources]
                require(paths == sorted(set(paths)), "test source paths must be unique and sorted")
                require(discovery.get("test_sources_sha256") == canonical_digest(sources),
                        "discovery.test_sources_sha256 does not match file manifest")
            if reference is None:
                reference = discovery
                merged["discovery"] = discovery
            else:
                require(discovery == reference, "discovery list, order, hashes or source files differ across shards")

        selected = report.get("selected_ids")
        if not require(_identifier_list(selected) and bool(selected), "selected_ids must be nonempty"):
            selected = []
        require(len(set(selected)) == len(selected), "duplicate selected test IDs within shard")
        require(type(report.get("selected_count")) is int and report["selected_count"] == len(selected),
                "selected_count differs from selected IDs")
        require(type(report.get("tests_run")) is int and report["tests_run"] == len(selected),
                "tests_run differs from selected test count")
        require(report.get("started_ids") == selected, "started test IDs differ from selected IDs or order")
        if discovered is not None:
            require(set(selected).issubset(discovered), "selected test IDs were not discovered")
        all_selected.extend(selected)

        results = report.get("test_results")
        executed, outcomes = [], {}
        if require(isinstance(results, list), "test_results must be a list"):
            for result in results:
                if not require(isinstance(result, dict) and isinstance(result.get("id"), str),
                               "test_results entry needs a test ID"):
                    continue
                executed.append(result["id"])
                outcomes[result["id"]] = result.get("outcome")
                require(isinstance(result.get("outcome"), str) and result["outcome"] in SUCCESS_OUTCOMES,
                        f"unsuccessful outcome for {result['id']}")
                require(_nonnegative_number(result.get("seconds")), f"invalid seconds for {result['id']}")
                merged["test_results"].append({**result, "shard_index": index})
        require(executed == selected, "executed test IDs differ from selected IDs or order")
        all_executed.extend(executed)
        for key, detail in (("skipped", "reason"), ("expected_failures", "traceback")):
            rows = report.get(key)
            if not require(isinstance(rows, list), f"{key} must be a list"):
                continue
            recorded_ids = []
            for row in rows:
                if not require(isinstance(row, dict) and isinstance(row.get("id"), str)
                               and isinstance(row.get(detail), str), f"invalid {key} entry"):
                    continue
                recorded_ids.append(row["id"])
                require(any(row["id"] == test_id or row["id"].startswith(test_id + " ") for test_id in selected),
                        f"{key} contains an unselected test ID")
                merged[key].append({**row, "shard_index": index})
            required_outcome = "skipped" if key == "skipped" else "expected_failure"
            for test_id, outcome in outcomes.items():
                if outcome == required_outcome:
                    require(any(value == test_id or value.startswith(test_id + " ") for value in recorded_ids),
                            f"{required_outcome} test has no explicit detail: {test_id}")
        for test_id in required_test_ids:
            if test_id in selected:
                require(outcomes.get(test_id) == "passed", f"mandatory corpus test did not pass: {test_id}")
                skipped_rows = report.get("skipped") if isinstance(report.get("skipped"), list) else []
                require(not any(isinstance(row, dict) and (row.get("id") == test_id or
                        str(row.get("id", "")).startswith(test_id + " "))
                        for row in skipped_rows),
                        f"mandatory corpus test was skipped: {test_id}")
        merged["shards"].append({"report": name, "shard_index": index, "selected_count": len(selected),
                                 "tests_run": report.get("tests_run"), "elapsed_seconds": report.get("elapsed_seconds")})
        if type(report.get("tests_run")) is int:
            merged["tests_run"] += report["tests_run"]

    if sorted(indices) != list(range(expected_shards)):
        problems.append("shard indices must cover the expected range exactly once")
    counts = Counter(all_selected)
    if any(count != 1 for count in counts.values()):
        problems.append("test IDs are selected in more than one shard")
    if reference and _identifier_list(reference.get("ids")):
        expected = Counter(reference["ids"])
        if Counter(all_selected) != expected:
            problems.append("selected union does not equal the complete discovered test list exactly once")
        if Counter(all_executed) != expected:
            problems.append("executed union does not equal the complete discovered test list exactly once")
        for test_id in required_test_ids:
            if test_id not in expected:
                problems.append(f"mandatory corpus test missing from discovery: {test_id}")
    else:
        problems.append("no usable full discovery list")
    merged["selected_ids"] = sorted(all_selected)
    merged["skipped_count"] = len(merged["skipped"])
    merged["expected_failure_count"] = len(merged["expected_failures"])
    merged["status"] = "failed" if problems else "passed"
    return merged


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-shards", type=int, default=4)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args(argv)
    reports, read_errors = [], []
    if not args.reports_dir.is_dir():
        read_errors.append(f"report directory does not exist: {args.reports_dir}")
    for path in sorted(args.reports_dir.rglob("*.json")):
        if path.resolve() == args.output.resolve():
            continue
        name = path.relative_to(args.reports_dir).as_posix()
        try:
            def reject_constant(value):
                raise ValueError(f"non-finite JSON number: {value}")
            reports.append((name, json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)))
        except (OSError, UnicodeError, ValueError) as error:
            read_errors.append(f"{name}: cannot read complete JSON report: {error}")
    report = check_reports(reports, source_commit=args.source_commit, expected_shards=args.expected_shards)
    if read_errors:
        report["validation_errors"].extend(read_errors)
        report["status"] = "failed"
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except (OSError, ValueError) as error:
        print(f"Could not write aggregate report: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"status": report["status"], "tests_run": report["tests_run"],
                      "skipped": report["skipped_count"], "output": str(args.output),
                      "validation_errors": report["validation_errors"]}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
