"""Same-runner measurement helpers never download or run a model in tests."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import compare_native_release_performance as comparison


class NativeReleasePerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        manifest = json.loads((ROOT / "docs/eval/desktop-joint-v1/manifest.json").read_text(encoding="utf-8"))
        self.case = next(row for row in manifest["cases"] if row["id"] == "large_repository")
        self.manifest = {**manifest, "cases": [self.case]}

    def report(self):
        return {"status": "passed", "errors": [], "failures": [],
            "execution_surface": "native_windows_http", "runtime_platform": "Windows",
            "source_commit": "a" * 40, "source_dirty": False, "archive_sha256": "b" * 64,
            "transport_kind": "synthetic", "external_model_calls": 0, "paid_model_calls": 0,
            "manifest_sha256": comparison.FROZEN_MANIFEST_SHA256, "selection_complete": False,
            "report_execution_id": "unique-execution", "cases": [{
                "id": "large_repository", "sample_index": number, "turn_id": "1", "run_id": f"run-{number}",
                "status": "passed", "answer_sha256": "c" * 64, "elapsed_seconds": 10 + number,
                "index_reused": number > 1, "question_index_reused": True,
                "checks": {name: True for name in self.case["assertions"]},
                "timings": {"cold_index_seconds": 100 if number == 1 else None,
                    "question_seconds": 10 + number, "first_provider_seconds": 2 + number,
                    "primary_question_seconds": 7 + number, "scope_probe_seconds": 3},
                "corpus": {"source_fingerprint": "d" * 64, "files": 12024, "lines": 2712992},
            } for number in range(1, 6)]}

    def verify(self, report):
        path = self.root / "report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return comparison.verified_report(path, self.manifest, source_commit="a" * 40,
                                           archive_sha256="b" * 64, complete=False)

    def test_complete_native_offline_samples_keep_measured_timings(self):
        report = self.verify(self.report())
        summary = comparison.summarize_report(report)
        self.assertEqual(summary["sample_count"], 5)
        self.assertEqual(summary["cold_import_seconds"], 100)
        self.assertEqual(summary["median_question_seconds"], 13)
        self.assertEqual(summary["median_warm_question_seconds"], 13.5)
        self.assertEqual(summary["median_primary_question_seconds"], 10)
        self.assertEqual(summary["median_warm_primary_question_seconds"], 10.5)
        self.assertEqual(summary["median_warm_scope_probe_seconds"], 3)
        self.assertEqual(len(summary["samples"]), 5)

    def test_source_preview_wrong_package_and_paid_calls_cannot_enter_comparison(self):
        for change in ({"execution_surface": "source_python_http"}, {"runtime_platform": "Darwin"},
                       {"source_commit": "f" * 40}, {"archive_sha256": "0" * 64},
                       {"external_model_calls": 1}, {"paid_model_calls": 1},
                       {"manifest_sha256": "0" * 64}, {"selection_complete": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify({**self.report(), **change})

    def test_missing_or_failed_sample_is_not_a_valid_timing_result(self):
        report = self.report()
        report["cases"].pop()
        with self.assertRaisesRegex(ValueError, "SAMPLE_CHECK"):
            self.verify(report)
        report = self.report()
        report["cases"][1]["checks"][self.case["assertions"][0]] = False
        with self.assertRaisesRegex(ValueError, "SAMPLE_CHECK"):
            self.verify(report)

    def summaries(self):
        values = {}
        for label, multiplier in (("previous", 1), ("current", 1.5), ("optimized", .75)):
            report = self.report()
            report["report_execution_id"] = label
            for row in report["cases"]:
                for name, value in row["timings"].items():
                    if value is not None:
                        row["timings"][name] = value * multiplier
            values[label] = comparison.summarize_report(report)
        return values

    def test_ratios_report_slower_baseline_without_inventing_an_improvement(self):
        ratios = comparison.compare_summaries(self.summaries())
        self.assertEqual(ratios["current_over_previous"]["median_question_seconds"], 1.5)
        self.assertEqual(ratios["optimized_over_current"]["cold_import_seconds"], .5)
        self.assertEqual(ratios["optimized_over_current"]["median_warm_primary_question_seconds"], .5)

    def test_reused_execution_and_different_corpus_are_rejected(self):
        values = self.summaries()
        values["current"]["report_execution_id"] = "previous"
        with self.assertRaisesRegex(ValueError, "REUSED_EXECUTION"):
            comparison.compare_summaries(values)
        values = self.summaries()
        values["current"]["corpus"] = {"source_fingerprint": "other"}
        with self.assertRaisesRegex(ValueError, "CORPUS_MISMATCH"):
            comparison.compare_summaries(values)

    def test_download_checks_public_candidate_and_exact_pinned_bytes_before_execution(self):
        baseline = comparison.BASELINES[0]
        metadata = {"tagName": baseline["tag"], "isDraft": False, "isPrerelease": True,
                    "assets": [{"name": comparison.ARCHIVE_NAME}]}
        with patch.object(comparison.subprocess, "check_output", return_value=json.dumps(metadata)), \
             patch.object(comparison.subprocess, "run") as run, \
             patch.object(comparison, "digest", return_value="wrong"), \
             patch.object(comparison, "inspect_package") as inspect:
            with self.assertRaisesRegex(ValueError, "ARCHIVE_CHANGED"):
                comparison.download_baseline("owner/project", baseline, self.root / "download")
        self.assertEqual(run.call_count, 1)
        self.assertIn("owner/project", run.call_args.args[0])
        inspect.assert_not_called()

    def test_draft_or_wrong_repository_never_downloads(self):
        baseline = comparison.BASELINES[0]
        metadata = {"tagName": baseline["tag"], "isDraft": True, "isPrerelease": True,
                    "assets": [{"name": comparison.ARCHIVE_NAME}]}
        with patch.object(comparison.subprocess, "check_output", return_value=json.dumps(metadata)), \
             patch.object(comparison.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "PUBLIC_CANDIDATE"):
                comparison.download_baseline("owner/project", baseline, self.root / "draft")
            with self.assertRaisesRegex(ValueError, "REPOSITORY_INVALID"):
                comparison.download_baseline("owner/project/other", baseline, self.root / "wrong")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
