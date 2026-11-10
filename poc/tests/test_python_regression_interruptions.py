"""Interrupted cases cannot appear as successful source regressions."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.check_python_regression_reports import check_reports


class PythonRegressionInterruptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tests = self.root / "tests"
        self.tests.mkdir()

    def invoke(self, body):
        (self.tests / "test_controlled.py").write_text(body, encoding="utf-8")
        report = self.root / "report.json"
        process = subprocess.run([sys.executable, str(ROOT / "tools/run_python_regressions.py"),
            "--tests-dir", str(self.tests), "--stack-dump-interval", "0", "--output", str(report)],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertTrue(report.is_file(), process.stderr)
        return process, json.loads(report.read_text(encoding="utf-8"))

    def test_keyboard_interrupt_records_incomplete_and_blocks_the_full_collector(self):
        process, report = self.invoke("""import unittest
class Cases(unittest.TestCase):
    def test_a_interrupted(self): raise KeyboardInterrupt('controlled interruption')
    def test_b_unvisited(self): raise AssertionError('must not run')
""")
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(report["status"], "runner_error")
        self.assertEqual(report["tests_run"], 1)
        self.assertEqual(len(report["test_results"]), 1)
        interrupted = report["test_results"][0]
        self.assertEqual(interrupted["outcome"], "incomplete")
        self.assertEqual(interrupted["id"], "test_controlled.Cases.test_a_interrupted")
        self.assertIn("KeyboardInterrupt", "\n".join(report["runner_errors"]))
        self.assertIn("END " + interrupted["id"] + " incomplete", process.stderr)
        self.assertNotIn("END " + interrupted["id"] + " passed", process.stderr)
        # Isolate execution completeness from the host's platform/dirty-tree
        # boundaries, as the existing collector tests do.
        checked = deepcopy(report)
        checked.update(runtime_platform="Windows", source_dirty=False)
        merged = check_reports([("interrupted.json", checked)], source_commit=checked["source_commit"],
            expected_shards=1, required_test_ids=())
        self.assertEqual(merged["status"], "failed")
        self.assertTrue(any("runner status must be passed" in message for message in merged["validation_errors"]))
        self.assertTrue(any("unsuccessful outcome" in message for message in merged["validation_errors"]))

    def test_only_completed_successful_cases_have_a_passed_outcome(self):
        process, report = self.invoke("""import unittest
class Cases(unittest.TestCase):
    def test_normal(self): self.assertEqual(2 + 2, 4)
    def test_subtests(self):
        for value in (1, 2):
            with self.subTest(value=value): self.assertGreater(value, 0)
""")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["tests_run"], 2)
        self.assertEqual(report["started_ids"], report["selected_ids"])
        self.assertEqual([row["outcome"] for row in report["test_results"]], ["passed", "passed"])
        self.assertEqual(report["failures"], [])
        self.assertEqual(report["errors"], [])

    def test_failure_error_skip_expected_and_subtest_results_keep_their_precise_outcomes(self):
        process, report = self.invoke("""import unittest
class Cases(unittest.TestCase):
    def test_failure(self): self.fail('controlled failure')
    def test_error(self): raise RuntimeError('controlled error')
    @unittest.skip('controlled platform boundary')
    def test_skip(self): pass
    @unittest.expectedFailure
    def test_expected(self): self.fail('controlled known failure')
    @unittest.expectedFailure
    def test_unexpected(self): pass
    def test_subfailure(self):
        with self.subTest(case=1): self.fail('controlled subtest failure')
    def test_suberror(self):
        with self.subTest(case=1): raise RuntimeError('controlled subtest error')
""")
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(report["status"], "failed")
        outcomes = {row["id"].rsplit(".", 1)[-1]: row["outcome"] for row in report["test_results"]}
        self.assertEqual(outcomes, {"test_failure": "failed", "test_error": "error", "test_skip": "skipped",
            "test_expected": "expected_failure", "test_unexpected": "unexpected_success",
            "test_subfailure": "failed", "test_suberror": "error"})
        self.assertEqual(report["tests_run"], 7)
        self.assertEqual(len(report["failures"]), 2)
        self.assertEqual(len(report["errors"]), 2)
        self.assertEqual(len(report["skipped"]), 1)
        self.assertEqual(len(report["expected_failures"]), 1)
        self.assertEqual(len(report["unexpected_successes"]), 1)


if __name__ == "__main__":
    unittest.main()
