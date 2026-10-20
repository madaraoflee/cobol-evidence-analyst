"""The sharded runner must preserve the full suite and expose every failure."""

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tools" / "run_python_regressions.py"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


class PythonRegressionRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tests = self.root / "tests"
        self.tests.mkdir()
        self.invocations = 0

    def module(self, name, body):
        (self.tests / name).write_text(body, encoding="utf-8")

    def invoke(self, index=0, count=1, extra=()):
        self.invocations += 1
        output = self.root / f"report-{self.invocations}.json"
        process = subprocess.run([sys.executable, str(RUNNER), "--tests-dir", str(self.tests),
                                  "--shard-index", str(index), "--shard-count", str(count),
                                  "--stack-dump-interval", "0", "--output", str(output), *extra],
                                 cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertTrue(output.is_file(), process.stdout + process.stderr)
        return process, json.loads(output.read_text(encoding="utf-8"))

    def test_every_module_is_atomic_and_full_discovery_runs_exactly_once(self):
        counts = {"alpha": 5, "bravo": 4, "charlie": 3, "delta": 2, "echo": 2, "foxtrot": 1}
        for name, count in counts.items():
            self.module(f"test_{name}.py", "import unittest\nclass Cases(unittest.TestCase):\n"
                        + "    @classmethod\n    def setUpClass(cls):\n        cls.order = []\n"
                        + "".join(f"    def test_{number:02d}(self):\n"
                                  f"        self.assertEqual(self.order, list(range({number})))\n"
                                  f"        self.order.append({number})\n" for number in range(count)))
        reports = []
        for index in range(4):
            process, report = self.invoke(index, 4)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["tests_run"], report["selected_count"])
            self.assertEqual(report["started_ids"], report["selected_ids"])
            self.assertEqual([row["id"] for row in report["test_results"]], report["selected_ids"])
            self.assertTrue(all(row["outcome"] == "passed" and row["seconds"] >= 0
                                for row in report["test_results"]))
            self.assertIn("START " + report["selected_ids"][0], process.stderr)
            self.assertIn("END " + report["selected_ids"][0] + " passed", process.stderr)
            reports.append(report)
        discovery = reports[0]["discovery"]
        self.assertEqual(discovery["count"], sum(counts.values()))
        self.assertEqual(discovery["ids_sha256"], digest(sorted(discovery["ids"])))
        self.assertEqual(discovery["test_sources_sha256"], digest(discovery["test_sources"]))
        self.assertEqual(len(discovery["test_sources"]), len(counts))
        all_selected = [identifier for report in reports for identifier in report["selected_ids"]]
        self.assertEqual(Counter(all_selected), Counter(discovery["ids"]))
        modules = defaultdict(set)
        for report in reports:
            self.assertEqual(report["discovery"], discovery)
            for identifier in report["selected_ids"]:
                modules[identifier.split(".")[0]].add(report["shard_index"])
            self.assertEqual(report["selected_ids"], [identifier for identifier in discovery["ids"]
                                                       if identifier in set(report["selected_ids"])])
        self.assertTrue(all(len(shards) == 1 for shards in modules.values()))
        self.assertEqual(sorted(report["selected_count"] for report in reports), [4, 4, 4, 5])
        again_process, again = self.invoke(0, 4)
        self.assertEqual(again_process.returncode, 0, again_process.stderr)
        self.assertEqual(again["selected_ids"], reports[0]["selected_ids"])
        self.assertEqual(again["discovery"], discovery)

    def test_failures_errors_unexpected_success_and_existing_skips_are_reported(self):
        self.module("test_outcomes.py", """import unittest
class Cases(unittest.TestCase):
    def test_pass(self): pass
    def test_failure(self): self.fail('intentional failure')
    def test_error(self): raise RuntimeError('intentional error')
    @unittest.skip('existing platform boundary')
    def test_skip(self): pass
    @unittest.expectedFailure
    def test_expected(self): self.fail('known failure')
    @unittest.expectedFailure
    def test_unexpected(self): pass
    def test_subtest(self):
        with self.subTest(case='bad'): self.fail('subtest failure')
""")
        process, report = self.invoke()
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["tests_run"], 7)
        self.assertEqual(len(report["test_results"]), 7)
        self.assertEqual(len(report["failures"]), 2)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(len(report["unexpected_successes"]), 1)
        self.assertEqual(len(report["expected_failures"]), 1)
        self.assertEqual(report["skipped"], [{"id": "test_outcomes.Cases.test_skip",
                                             "reason": "existing platform boundary"}])

    def test_import_failure_stops_every_shard_before_any_selected_test_runs(self):
        self.module("test_healthy.py", "import unittest\nclass Cases(unittest.TestCase):\n"
                    "    def test_must_not_run(self): raise AssertionError('should not execute')\n")
        self.module("test_unimportable.py", "raise ImportError('intentional import failure')\n")
        for index in range(2):
            process, report = self.invoke(index, 2)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(report["status"], "discovery_error")
            self.assertTrue(report["discovery_errors"])
            self.assertEqual(report["tests_run"], 0)
            self.assertEqual(report["started_ids"], [])

    def test_class_setup_failure_cannot_pass_with_missing_executed_tests(self):
        self.module("test_setup.py", """import unittest
class Cases(unittest.TestCase):
    @classmethod
    def setUpClass(cls): raise RuntimeError('class setup failed')
    def test_one(self): pass
    def test_two(self): pass
""")
        process, report = self.invoke()
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(report["selected_count"], 2)
        self.assertEqual(report["tests_run"], 0)
        self.assertEqual(len(report["errors"]), 1)
        self.assertTrue(report["runner_errors"])

    def test_git_commit_is_measured_and_expected_commit_cannot_override_it(self):
        self.module("test_one.py", "import unittest\nclass Cases(unittest.TestCase):\n"
                    "    def test_one(self): pass\n")
        process, report = self.invoke(extra=("--source-commit", "0" * 40))
        actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(report["source_commit"], actual)
        self.assertIsInstance(report["source_dirty"], bool)
        self.assertTrue(report["runtime_platform"])
        self.assertEqual(report["tests_run"], 0)

    def test_empty_discovery_is_a_failure(self):
        process, report = self.invoke()
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(report["status"], "discovery_error")
        self.assertIn("Discovery found no tests", report["discovery_errors"])

    def test_direct_script_entry_preserves_repository_package_imports(self):
        self.module("test_repository_import.py", """import unittest
from tools import desktop_acceptance_runtime
class Cases(unittest.TestCase):
    def test_import(self):
        self.assertTrue(desktop_acceptance_runtime.__file__.endswith('desktop_acceptance_runtime.py'))
""")
        process, report = self.invoke()
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["tests_run"], 1)
        self.assertEqual(report["discovery_errors"], [])

    def test_custom_discovery_cannot_run_the_same_test_id_twice(self):
        self.module("test_duplicate.py", """import unittest
class Cases(unittest.TestCase):
    def test_one(self): pass
def load_tests(loader, tests, pattern):
    return unittest.TestSuite([Cases('test_one'), Cases('test_one')])
""")
        process, report = self.invoke()
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(report["status"], "discovery_error")
        self.assertEqual(report["tests_run"], 0)
        self.assertIn("Duplicate discovered test IDs", report["discovery_errors"][0])

    def test_periodic_python_stacks_do_not_interrupt_running_tests(self):
        self.module("test_sampling.py", """import time
import unittest
class Cases(unittest.TestCase):
    def test_dynamic_code(self):
        until = time.monotonic() + 2.2
        while time.monotonic() < until:
            exec(compile('value = sum(range(100))', '<temporary-test-code>', 'exec'), {})
            time.sleep(.001)
""")
        process, report = self.invoke(extra=("--stack-dump-interval", "1"))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["diagnostic_errors"], [])
        self.assertIn("Python thread stack snapshot", process.stderr)
        self.assertIn("test_dynamic_code", process.stderr)
        self.assertIn("END test_sampling.Cases.test_dynamic_code passed", process.stderr)
        self.assertNotIn("Timeout (", process.stderr)

    def test_native_exit_retains_running_discovery_and_never_marks_active_test_passed(self):
        self.module("test_abrupt.py", """import os
import unittest
class Cases(unittest.TestCase):
    def test_a_exit(self): os._exit(7)
    def test_b_not_run(self): pass
""")
        process, report = self.invoke()
        self.assertEqual(process.returncode, 7)
        self.assertEqual(report["status"], "running")
        self.assertEqual(report["selected_count"], 2)
        self.assertEqual(report["discovery"]["count"], 2)
        self.assertEqual(report["tests_run"], 1)
        self.assertEqual(report["started_ids"], ["test_abrupt.Cases.test_a_exit"])
        self.assertEqual(report["test_results"], [])
        self.assertIn("START test_abrupt.Cases.test_a_exit", process.stderr)
        self.assertNotIn("END test_abrupt.Cases.test_a_exit", process.stderr)

        from tools.check_python_regression_reports import check_reports
        # Remove unrelated platform/working-tree gate failures so the partial
        # execution itself must still be rejected.
        report["runtime_platform"] = "Windows"
        report["source_dirty"] = False
        merged = check_reports([("partial.json", report)], source_commit=report["source_commit"],
                               expected_shards=1, required_test_ids=())
        self.assertEqual(merged["status"], "failed")
        self.assertTrue(any("runner status must be passed" in item
                            for item in merged["validation_errors"]))
        self.assertTrue(any("executed test IDs differ" in item
                            for item in merged["validation_errors"]))

    def test_failure_trace_is_checkpointed_before_later_native_exit(self):
        self.module("test_abrupt.py", """import os
import unittest
class Cases(unittest.TestCase):
    def test_a_failure(self): self.fail('failure before native exit')
    def test_b_exit(self): os._exit(9)
""")
        process, report = self.invoke()
        self.assertEqual(process.returncode, 9)
        self.assertEqual(report["status"], "running")
        self.assertEqual(report["selected_count"], 2)
        self.assertEqual(report["test_results"][0]["outcome"], "failed")
        self.assertIn("failure before native exit", report["failures"][0]["traceback"])
        self.assertNotIn("test_abrupt.Cases.test_b_exit",
                         [row["id"] for row in report["test_results"]])

    def test_external_watchdog_can_stop_hang_with_diagnostic_and_partial_report(self):
        self.module("test_hang.py", """import threading
import unittest
class Cases(unittest.TestCase):
    def test_wait_forever(self): threading.Event().wait()
""")
        output = self.root / "hanging-report.json"
        log_path = self.root / "hanging-stderr.log"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, str(RUNNER), "--tests-dir", str(self.tests),
                 "--stack-dump-interval", "1", "--output", str(output)],
                cwd=ROOT, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if "Python thread stack snapshot" in log_path.read_text(encoding="utf-8"):
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(.05)
                self.assertIsNone(process.poll(), log_path.read_text(encoding="utf-8"))
                self.assertIn("Python thread stack snapshot", log_path.read_text(encoding="utf-8"))
                process.terminate()
                process.wait(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(report["status"], "running")
        self.assertEqual(report["started_ids"], ["test_hang.Cases.test_wait_forever"])
        self.assertEqual(report["test_results"], [])
        self.assertIn("test_wait_forever", log_path.read_text(encoding="utf-8"))

    def test_snapshot_releases_frames_even_if_formatting_fails(self):
        from tools import run_python_regressions as runner
        from unittest.mock import patch

        frames = {1: sys._getframe()}
        with patch.object(runner.sys, "_current_frames", return_value=frames):
            with patch.object(runner.traceback, "format_stack", side_effect=ValueError("format failure")):
                with self.assertRaisesRegex(ValueError, "format failure"):
                    runner.format_thread_stacks()
        self.assertEqual(frames, {})

    def test_diagnostic_stream_error_is_recorded_and_watchdog_stops(self):
        from tools import run_python_regressions as runner
        import threading

        attempted = threading.Event()

        class ClosedStream:
            def write(self, text):
                attempted.set()
                raise ValueError("stream closed")

        watchdog = runner.StackWatchdog(.01, ClosedStream())
        watchdog.start()
        try:
            self.assertTrue(attempted.wait(timeout=10))
        finally:
            watchdog.stop()
        self.assertFalse(watchdog.thread.is_alive())
        self.assertEqual(watchdog.errors, ["Thread stack diagnostic failed: ValueError"])


if __name__ == "__main__":
    unittest.main()
