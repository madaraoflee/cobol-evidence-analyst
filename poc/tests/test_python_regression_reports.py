"""Synthetic Windows report fixtures test the merger, not native acceptance."""
from __future__ import annotations

import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.check_python_regression_reports import REQUIRED_TEST_IDS, canonical_digest, check_reports, main


COMMIT = "a" * 40


def fixture_reports():
    ids = [*REQUIRED_TEST_IDS, *(f"test_sample.SampleTests.test_case_{index}" for index in range(6))]
    sources = [{"path": "test_sample.py", "sha256": "b" * 64}]
    discovery = {"count": len(ids), "ids": ids, "ids_sha256": canonical_digest(sorted(ids)),
                 "test_sources": sources, "test_sources_sha256": canonical_digest(sources)}
    reports = []
    for index in range(4):
        selected = ids[index::4]
        reports.append((f"shard-{index}.json", {"schema_version": 1, "source_commit": COMMIT,
            "source_dirty": False, "runtime_platform": "Windows", "status": "passed",
            "shard_count": 4, "shard_index": index, "discovery": copy.deepcopy(discovery),
            "selected_ids": selected, "selected_count": len(selected), "tests_run": len(selected),
            "started_ids": list(selected), "runner_errors": [],
            "failures": [], "errors": [], "unexpected_successes": [], "discovery_errors": [],
            "skipped": [], "expected_failures": [], "elapsed_seconds": 2.0,
            "test_results": [{"id": test_id, "seconds": .1, "outcome": "passed"} for test_id in selected]}))
    return reports


class PythonRegressionReportsTests(unittest.TestCase):
    def verify(self, reports, passed=False, contains=None):
        result = check_reports(reports, source_commit=COMMIT)
        self.assertEqual(result["status"], "passed" if passed else "failed", result["validation_errors"])
        if contains:
            self.assertTrue(any(contains in message for message in result["validation_errors"]), result)
        return result

    def test_complete_four_shards_pass_and_preserve_full_execution_list(self):
        result = self.verify(fixture_reports(), passed=True)
        self.assertEqual(result["tests_run"], 8)
        self.assertEqual(len(result["test_results"]), 8)
        self.assertEqual(len(set(result["selected_ids"])), 8)
        self.assertEqual(result["scope"], "source_regressions_only_not_executable_or_human_acceptance")

    def test_missing_extra_duplicate_or_out_of_range_shards_fail(self):
        for mutation in (lambda rows: rows.pop(), lambda rows: rows.append(copy.deepcopy(rows[0])),
                         lambda rows: rows[1][1].update(shard_index=0),
                         lambda rows: rows[1][1].update(shard_index=4),
                         lambda rows: rows[1][1].update(shard_count=3),
                         lambda rows: rows[1][1].update(shard_index=True)):
            rows = fixture_reports()
            mutation(rows)
            with self.subTest(mutation=mutation):
                self.verify(rows)

    def test_wrong_schema_commit_dirty_source_platform_or_status_fails(self):
        for key, value in [("schema_version", 2), ("schema_version", True), ("source_commit", "c" * 40),
                           ("source_dirty", True), ("source_dirty", None), ("runtime_platform", "Darwin"),
                           ("status", "runner_error"), ("status", "discovery_error")]:
            rows = fixture_reports()
            rows[0][1][key] = value
            with self.subTest(key=key, value=value):
                self.verify(rows)

    def test_discovery_count_hash_order_and_complete_ids_must_agree(self):
        for change in (lambda value: value.update(count=7), lambda value: value.update(ids_sha256="0" * 64),
                       lambda value: value["ids"].reverse(), lambda value: value["ids"].append(value["ids"][0])):
            rows = fixture_reports()
            change(rows[0][1]["discovery"])
            with self.subTest(change=change):
                self.verify(rows)
        rows = fixture_reports()
        rows[0][1]["discovery"]["ids"][0] = "test_other.Other.test_replacement"
        rows[0][1]["discovery"]["ids_sha256"] = canonical_digest(sorted(rows[0][1]["discovery"]["ids"]))
        self.verify(rows, contains="differ across shards")

    def test_test_source_manifest_cannot_drift_even_with_same_test_ids(self):
        rows = fixture_reports()
        discovery = rows[0][1]["discovery"]
        discovery["test_sources"][0]["sha256"] = "d" * 64
        discovery["test_sources_sha256"] = canonical_digest(discovery["test_sources"])
        self.verify(rows, contains="differ across shards")
        for change in (lambda value: value.update(test_sources_sha256="0" * 64),
                       lambda value: value["test_sources"][0].update(path="../test_sample.py"),
                       lambda value: value["test_sources"].append(copy.deepcopy(value["test_sources"][0]))):
            rows = fixture_reports()
            change(rows[0][1]["discovery"])
            self.verify(rows)

    def test_selected_union_missing_duplicate_or_unknown_ids_fails_even_when_counts_match(self):
        for replacement in ("test_sample.SampleTests.test_unknown", fixture_reports()[1][1]["selected_ids"][0]):
            rows = fixture_reports()
            rows[0][1]["selected_ids"][0] = replacement
            rows[0][1]["test_results"][0]["id"] = replacement
            with self.subTest(replacement=replacement):
                self.verify(rows, contains="selected union")
        rows = fixture_reports()
        report = rows[0][1]
        report["selected_ids"].pop()
        report["test_results"].pop()
        report.update(selected_count=1, tests_run=1)
        self.verify(rows, contains="selected union")

    def test_totals_do_not_replace_matching_actual_test_execution_ids(self):
        for change in (lambda report: report["test_results"].pop(),
                       lambda report: report["test_results"].append(copy.deepcopy(report["test_results"][0])),
                       lambda report: report["test_results"][0].update(id="test_other.Other.test_not_selected"),
                       lambda report: report.update(tests_run=1), lambda report: report.update(selected_count=True)):
            rows = fixture_reports()
            change(rows[0][1])
            self.verify(rows)

    def test_empty_shard_and_partial_runner_results_cannot_pass(self):
        rows = fixture_reports()
        rows[0][1].update(selected_ids=[], selected_count=0, tests_run=0, test_results=[])
        self.verify(rows, contains="selected_ids must be nonempty")
        for key in ("failures", "errors", "unexpected_successes", "discovery_errors", "runner_errors"):
            rows = fixture_reports()
            rows[0][1][key] = ["failure detail"]
            self.verify(rows, contains=key)

    def test_started_and_completed_test_ids_must_match_selected_order(self):
        for key in ("started_ids", "test_results"):
            rows = fixture_reports()
            rows[0][1][key].reverse()
            with self.subTest(key=key):
                self.verify(rows, contains="order")
        rows = fixture_reports()
        rows[0][1].pop("started_ids")
        self.verify(rows, contains="started test IDs")

    def test_failed_or_incomplete_outcome_cannot_hide_behind_empty_failure_lists(self):
        for outcome in ("failed", "error", "unexpected_success", "started", None, []):
            rows = fixture_reports()
            rows[0][1]["test_results"][0]["outcome"] = outcome
            with self.subTest(outcome=outcome):
                self.verify(rows, contains="unsuccessful outcome")

    def test_timing_values_cannot_be_negative_nonfinite_or_boolean(self):
        for seconds in (-1, float("nan"), float("inf"), True, None):
            rows = fixture_reports()
            rows[0][1]["test_results"][0]["seconds"] = seconds
            with self.subTest(seconds=seconds):
                self.verify(rows, contains="invalid seconds")

    def test_platform_skip_and_expected_failure_are_explicitly_aggregated(self):
        rows = fixture_reports()
        skip = rows[2][1]["test_results"][0]
        skip["outcome"] = "skipped"
        rows[2][1]["skipped"] = [{"id": skip["id"], "reason": "Only applies on another platform"}]
        expected = rows[3][1]["test_results"][0]
        expected["outcome"] = "expected_failure"
        rows[3][1]["expected_failures"] = [{"id": expected["id"], "traceback": "Expected failure detail"}]
        result = self.verify(rows, passed=True)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["skipped"][0]["shard_index"], 2)
        self.assertEqual(result["expected_failure_count"], 1)
        rows[2][1]["skipped"] = []
        self.verify(rows, contains="no explicit detail")

    def test_mandatory_public_corpus_test_cannot_skip_or_disappear(self):
        rows = fixture_reports()
        test_id = REQUIRED_TEST_IDS[0]
        rows[0][1]["test_results"][0]["outcome"] = "skipped"
        rows[0][1]["skipped"] = [{"id": test_id, "reason": "Corpus not configured"}]
        self.verify(rows, contains="mandatory corpus test")
        rows[0][1]["test_results"][0]["outcome"] = "passed"
        rows[0][1]["skipped"][0]["id"] += " (sample=1)"
        self.verify(rows, contains="mandatory corpus test was skipped")
        rows = fixture_reports()
        replacement = "test_other.Other.test_not_corpus"
        for _, report in rows:
            report["discovery"]["ids"] = [replacement if item == test_id else item for item in report["discovery"]["ids"]]
            report["discovery"]["ids_sha256"] = canonical_digest(sorted(report["discovery"]["ids"]))
            report["selected_ids"] = [replacement if item == test_id else item for item in report["selected_ids"]]
            for result in report["test_results"]:
                if result["id"] == test_id:
                    result["id"] = replacement
        self.verify(rows, contains="mandatory corpus test missing")

    def test_malformed_required_fields_fail_without_crashing(self):
        for key in ("discovery", "selected_ids", "test_results", "skipped", "expected_failures"):
            rows = fixture_reports()
            rows[0][1][key] = None
            with self.subTest(key=key):
                self.verify(rows)
        self.verify([("invalid.json", [])])

    def test_cli_writes_machine_readable_failure_and_success_and_ignores_own_output(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            output = root / "merged.json"
            arguments = ["--reports-dir", str(root), "--output", str(output), "--source-commit", COMMIT]
            for name, report in fixture_reports():
                nested = root / "downloaded" / name
                nested.parent.mkdir(exist_ok=True)
                nested.write_text(json.dumps(report), encoding="utf-8")
            self.assertEqual(main(arguments), 0)
            self.assertEqual(main(arguments), 0, "rerun must not count its own aggregate as a fifth shard")
            (root / "downloaded/shard-0.json").write_text('{"unfinished":', encoding="utf-8")
            self.assertEqual(main(arguments), 1)
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("cannot read complete JSON" in item for item in result["validation_errors"]))

    def test_cli_missing_directory_and_nonfinite_json_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            output = root / "merged.json"
            arguments = ["--reports-dir", str(root / "absent"), "--output", str(output), "--source-commit", COMMIT]
            self.assertEqual(main(arguments), 1)
            self.assertEqual(json.loads(output.read_text())["status"], "failed")
            self.assertEqual(main(arguments + ["--expected-shards", "0"]), 1)
            self.assertIn("positive integer", " ".join(json.loads(output.read_text())["validation_errors"]))
            reports = root / "absent"
            reports.mkdir()
            (reports / "bad.json").write_text('{"value": NaN}')
            self.assertEqual(main(arguments), 1)
            self.assertIn("non-finite JSON number", " ".join(json.loads(output.read_text())["validation_errors"]))

    def test_runner_report_schema_roundtrips_through_merger_with_explicit_synthetic_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            test_dir = root / "tests"
            test_dir.mkdir()
            ids = [*REQUIRED_TEST_IDS, "test_alpha.AlphaTests.test_pass", "test_beta.BetaTests.test_pass"]
            for test_id in ids:
                module, cls, method = test_id.split(".")
                (test_dir / (module + ".py")).write_text(
                    f"import unittest\nclass {cls}(unittest.TestCase):\n    def {method}(self):\n        self.assertTrue(True)\n",
                    encoding="utf-8")
            reports = []
            for index in range(4):
                output = root / f"shard-{index}.json"
                process = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2] /
                    "tools/run_python_regressions.py"), "--tests-dir", str(test_dir), "--shard-index", str(index),
                    "--shard-count", "4", "--output", str(output), "--stack-dump-interval", "0"],
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(process.returncode, 0, process.stderr)
                report = json.loads(output.read_text(encoding="utf-8"))
                # This projection is explicitly synthetic: it tests only
                # compatibility, never claims native Windows acceptance.
                report.update(runtime_platform="Windows", source_dirty=False)
                reports.append((output.name, report))
            actual_commit = reports[0][1]["source_commit"]
            result = check_reports(reports, source_commit=actual_commit)
            self.assertEqual(result["status"], "passed", result["validation_errors"])
            self.assertEqual(result["tests_run"], 4)


if __name__ == "__main__":
    unittest.main()
