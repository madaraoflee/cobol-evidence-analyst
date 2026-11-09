"""Maintenance acceptance must not hide a historical regression behind totals."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("defect_gate", ROOT / "tools/verify_bug_regressions.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class BugRegressionGateTests(unittest.TestCase):
    def setUp(self):
        self.identifier = "test_sample.SampleTests.test_original_failure"
        self.browser_path = "poc/tests/sample.test.cjs"
        self.browser_id = self.browser_path + "::retains prior answer"
        self.baseline = {"schema_version": 1, "python_ids": [self.identifier], "browser_ids": [self.browser_id]}
        self.bug = {"id": "BUG-001", "title": "History lost", "symptom": "Prior answer disappears",
            "root_cause": "State replaced", "status": "fixed", "evidence": ["commit:example"],
            "verification_scope": "source_offline", "limitations": "Does not certify a model answer",
            "regressions": [{"kind": "python", "test_id": self.identifier},
                            {"kind": "browser", "path": self.browser_path}]}
        self.registry = {"schema_version": 1, "bugs": [self.bug]}
        self.python = {"status": "passed", "shard_index": 0, "shard_count": 1,
            "discovery": {"ids": [self.identifier]}, "selected_ids": [self.identifier],
            "started_ids": [self.identifier], "selected_count": 1, "tests_run": 1,
            "test_results": [{"id": self.identifier, "outcome": "passed"}]}
        for key in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes",
                    "discovery_errors", "runner_errors", "diagnostic_errors"):
            self.python[key] = []
        self.browser = [{"id": self.browser_id, "path": self.browser_path, "outcome": "passed"}]

    def evaluate(self, reports=None):
        return gate.evaluate(self.registry, self.baseline, reports or [self.python], self.browser,
                             [self.identifier], [self.browser_path])

    def contract(self, **kwargs):
        return gate.validate_contract(self.registry, self.baseline, [self.identifier], [self.browser_path], **kwargs)

    def test_complete_run_verifies_each_bug_with_named_checks(self):
        self.assertEqual(self.contract(), [])
        result = self.evaluate()
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["all_recorded_bugs_verified"])
        self.assertEqual(len(result["bugs"][0]["checks"]), 2)
        self.assertFalse(result["native_or_real_model_acceptance"])

    def test_deleted_old_test_is_rejected_even_if_current_discovery_is_consistent(self):
        errors = gate.validate_contract(self.registry, self.baseline, [], [self.browser_path])
        self.assertTrue(any("Historical Python test missing" in e for e in errors))

    def test_deleted_bug_record_cannot_shrink_acceptance(self):
        previous = deepcopy(self.registry)
        self.registry["bugs"] = []
        self.assertTrue(any("Historical defect removed" in e for e in self.contract(previous_registry=previous)))

    def test_removed_bug_test_mapping_is_rejected(self):
        previous = deepcopy(self.registry)
        self.bug["regressions"].pop()
        self.assertTrue(any("mapping removed" in e for e in self.contract(previous_registry=previous)))

    def test_baseline_cannot_drop_a_test_by_rewriting_the_manifest(self):
        previous = deepcopy(self.baseline)
        previous["python_ids"].append("test_other.Tests.test_old_failure")
        self.assertTrue(any("baseline reduced" in e for e in self.contract(previous_baseline=previous)))

    def test_skip_cannot_count_as_bug_fixed_even_when_runner_says_passed(self):
        self.python["test_results"][0]["outcome"] = "skipped"
        self.python["skipped"] = [{"id": self.identifier, "reason": "fixture unavailable"}]
        result = self.evaluate()
        self.assertFalse(result["all_recorded_bugs_verified"])
        self.assertEqual(result["bugs"][0]["status"], "unverified")

    def test_expected_failure_cannot_count_as_a_passing_fix(self):
        self.python["test_results"][0]["outcome"] = "expected_failure"
        self.python["expected_failures"] = [{"id": self.identifier, "traceback": "expected"}]
        self.assertEqual(self.evaluate()["status"], "blocked")

    def test_duplicate_shards_are_not_extra_coverage(self):
        self.assertEqual(self.evaluate([self.python, deepcopy(self.python)])["status"], "blocked")

    def test_missing_completion_is_rejected(self):
        self.python["test_results"] = []
        self.assertEqual(self.evaluate()["status"], "blocked")

    def test_newly_added_test_also_must_run(self):
        result = gate.evaluate(self.registry, self.baseline, [self.python], self.browser,
            [self.identifier, "test_new.Tests.test_new_failure"], [self.browser_path])
        self.assertEqual(result["status"], "blocked")

    def test_independent_maintenance_suite_must_also_complete(self):
        other = deepcopy(self.python)
        identifier = "test_packaging.Tests.test_staged_dependencies"
        other.update(discovery={"ids": [identifier]}, selected_ids=[identifier], started_ids=[identifier],
                     test_results=[{"id": identifier, "outcome": "passed"}])
        result = gate.evaluate(self.registry, self.baseline, [self.python, other], self.browser,
            [self.identifier, identifier], [self.browser_path])
        self.assertEqual(result["status"], "passed")
        other["test_results"] = []
        self.assertEqual(gate.evaluate(self.registry, self.baseline, [self.python, other], self.browser,
            [self.identifier, identifier], [self.browser_path])["status"], "blocked")

    def test_missing_old_browser_test_is_rejected_even_if_file_remains(self):
        self.browser[0]["id"] = self.browser_path + "::different test"
        self.assertIn("Historical browser tests missing", self.evaluate()["errors"])

    def test_failed_browser_test_blocks_all_bug_acceptance(self):
        self.browser[0]["outcome"] = "failed"
        self.assertEqual(self.evaluate()["status"], "blocked")

    def test_fixed_bug_requires_executable_regression_mapping(self):
        self.bug["regressions"] = []
        self.assertTrue(any("needs a regression" in e for e in self.contract()))

    def test_source_tests_cannot_certify_device_or_model_scope(self):
        self.bug["verification_scope"] = "target_device"
        self.assertTrue(any("only verify source_offline" in e for e in self.contract()))

    def test_committed_registry_deletion_does_not_erase_older_contract(self):
        latest = {"bugs": []}
        older = deepcopy(self.registry)
        responses = [SimpleNamespace(stdout="new\nold\n"), SimpleNamespace(stdout=json.dumps(latest)),
                     SimpleNamespace(stdout=json.dumps(older))]
        with patch.object(gate.subprocess, "run", side_effect=responses):
            previous = gate.previous_json(ROOT, gate.REGISTRY)
        self.registry["bugs"] = []
        self.assertTrue(any("Historical defect removed" in e for e in self.contract(previous_registry=previous)))

    def test_committed_baseline_deletion_does_not_erase_older_tests(self):
        older = deepcopy(self.baseline)
        older["python_ids"].append("test_old.Tests.test_removed")
        responses = [SimpleNamespace(stdout="new\nold\n"), SimpleNamespace(stdout=json.dumps(self.baseline)),
                     SimpleNamespace(stdout=json.dumps(older))]
        with patch.object(gate.subprocess, "run", side_effect=responses):
            previous = gate.previous_json(ROOT, gate.BASELINE)
        self.assertTrue(any("baseline reduced" in e for e in self.contract(previous_baseline=previous)))

    def test_shallow_history_cannot_hide_removed_historical_bugs(self):
        with patch.object(gate.subprocess, "check_output", return_value="true\n"):
            with self.assertRaisesRegex(ValueError, "Full Git history is required"):
                gate.require_full_history(ROOT)

    def test_complete_history_can_be_audited(self):
        with patch.object(gate.subprocess, "check_output", return_value="false\n"):
            gate.require_full_history(ROOT)

    def test_open_issue_remains_unverified_when_all_available_tests_pass(self):
        self.bug.update(status="open", regressions=[])
        result = self.evaluate()
        self.assertTrue(result["source_regressions_passed"])
        self.assertFalse(result["all_recorded_bugs_verified"])
        self.assertEqual(result["status"], "blocked")

    def test_diagnostic_runner_error_is_not_ignored(self):
        self.python["diagnostic_errors"].append("worker did not stop")
        self.assertFalse(self.evaluate()["source_regressions_passed"])

    def test_browser_missing_summary_is_incomplete(self):
        rows, errors = gate.browser_results([], ROOT, [self.browser_path])
        self.assertFalse(rows)
        self.assertTrue(errors)

    def test_real_browser_skip_is_not_reported_as_passing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            test = root / "sample.test.cjs"
            test.write_text("const test=require('node:test'); test.skip('old regression',()=>{});", encoding="utf-8")
            result = subprocess.run(["node", "--test", "--test-reporter=" + (ROOT / "tools/bug_browser_reporter.cjs").as_uri(),
                str(test)], capture_output=True, text=True, check=True)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            rows, errors = gate.browser_results(events, root, [test.name])
            self.assertEqual(rows[0]["outcome"], "skipped")
            self.assertTrue(errors)

    def test_inventory_check_never_claims_tests_were_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            root, output = Path(directory) / "repo", Path(directory) / "result"
            (root / "docs/bugs").mkdir(parents=True)
            (root / "poc/tests").mkdir(parents=True)
            (root / self.browser_path).write_text("", encoding="utf-8")
            gate.write_json(root / gate.REGISTRY, self.registry)
            gate.write_json(root / gate.BASELINE, self.baseline)
            with patch.object(gate, "ROOT", root), patch.object(gate, "discover", side_effect=[[self.identifier], []]), \
                    patch.object(gate, "require_full_history"), \
                    patch.object(gate, "previous_json", return_value=None), \
                    patch.object(gate.subprocess, "check_output", return_value="a" * 40):
                self.assertEqual(gate.main(["--output", str(output), "--check-only"]), 0)
            report = gate.read_json(output / "BUG-REGRESSION-REPORT.json")
            self.assertEqual(report["status"], "inventory_only")
            self.assertFalse(report["all_recorded_bugs_verified"])

    def test_source_change_during_verification_invalidates_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root, output = Path(directory) / "repo", Path(directory) / "result"
            (root / "docs/bugs").mkdir(parents=True)
            (root / "poc/tests").mkdir(parents=True)
            (root / self.browser_path).write_text("", encoding="utf-8")
            gate.write_json(root / gate.REGISTRY, self.registry)
            gate.write_json(root / gate.BASELINE, self.baseline)
            with patch.object(gate, "ROOT", root), patch.object(gate, "discover", side_effect=[[self.identifier], []]), \
                    patch.object(gate, "require_full_history"), \
                    patch.object(gate, "previous_json", return_value=None), \
                    patch.object(gate, "snapshot", side_effect=[{"sha256": "before"}, {"sha256": "after"}]), \
                    patch.object(gate.subprocess, "check_output", return_value="a" * 40):
                self.assertEqual(gate.main(["--output", str(output), "--check-only"]), 1)
            self.assertIn("Source or regression contract changed", gate.read_json(output / "BUG-REGRESSION-REPORT.json")["errors"][0])


if __name__ == "__main__":
    unittest.main()
