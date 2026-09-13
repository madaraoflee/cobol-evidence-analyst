from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_framework_paths import build_path_bundle, check_case, load_case_set, main, render_path_report


class FrameworkPathRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "output"
        self.contract = {"contract_id": "fixture", "contract_version": "1", "provenance": {"kind": "synthetic"},
                         "access_paths": [], "locking": {}, "transaction": {}, "restart": {}}
        self.audit = {"entry_program": "ENTRYPG", "source_snapshot_id": "source:1", "scenario_hash": "scenario:1",
                      "scope": {"model_path_complete": True, "runtime_verified": False},
                      "root_exits": [{"values": {"STATUS": "OK"}, "io_state": None}], "boundaries": [],
                      "trace": [{"event": "io_call", "program_name": "ENTRYPG", "io_event": {"action": "save", "function_value": "SAVE"},
                                 "evidence_refs": [{"source_spans": [{"relative_path": "control.cpy", "start_line": 3,
                                  "end_line": 3, "include_chain": [{"relative_path": "main.cbl", "line": 8}]}]}]}]}
        self.case = {"id": "selected", "entry": "ENTRYPG", "scenario": {"records": []},
                     "expected": {"model_path_complete": True, "values": {"STATUS": "OK"}}}

    def tearDown(self):
        self.temporary.cleanup()

    def build(self, cases=None, audit=None):
        with patch("run_framework_paths.validate_runtime_contract", side_effect=lambda c: c), \
             patch("run_framework_paths.audit_framework_paths", return_value=audit or self.audit):
            return build_path_bundle(self.source, self.output, self.contract, cases or [self.case])

    def test_bundle_keeps_conditional_labels_and_original_provenance(self):
        result = self.build()
        self.assertFalse(result["runtime_verified"])
        self.assertFalse(result["question_answered"])
        self.assertEqual(result["summary"]["failed_checks"], 0)
        report = (self.output / "framework-paths.md").read_text()
        self.assertIn("不是实际业务程序执行", report)
        self.assertIn("control.cpy:3（包含：main.cbl:8）", report)
        self.assertEqual(json.loads((self.output / "framework-paths.json").read_text())["summary"]["checks"], 2)

    def test_refuses_source_output_overlap_and_preserves_existing_output(self):
        for target in (self.source, self.source / "nested", self.root):
            with self.subTest(target=target), self.assertRaises(ValueError):
                build_path_bundle(self.source, target, self.contract, [self.case])
        self.output.mkdir()
        marker = self.output / "existing.txt"
        marker.write_text("preserve")
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual(marker.read_text(), "preserve")

    def test_no_output_overwrite_on_late_collision(self):
        def collision(*args, **kwargs):
            (self.output / "framework-paths.json").write_text("existing result")
            return self.audit
        with patch("run_framework_paths.validate_runtime_contract", side_effect=lambda c: c), \
             patch("run_framework_paths.audit_framework_paths", side_effect=collision), self.assertRaises(FileExistsError):
            build_path_bundle(self.source, self.output, self.contract, [self.case])
        self.assertEqual((self.output / "framework-paths.json").read_text(), "existing result")

    def test_cases_cannot_mix_source_snapshots(self):
        other_case = {**self.case, "id": "other"}
        other = {**self.audit, "source_snapshot_id": "source:2"}
        with patch("run_framework_paths.validate_runtime_contract", side_effect=lambda c: c), \
             patch("run_framework_paths.audit_framework_paths", side_effect=[self.audit, other]), self.assertRaises(ValueError):
            build_path_bundle(self.source, self.output, self.contract, [self.case, other_case])
        self.assertFalse((self.output / "framework-paths.json").exists())

    def test_expectation_failure_is_not_relabelled_as_pass(self):
        case = deepcopy(self.case)
        case["expected"]["values"]["STATUS"] = "ERROR"
        result = self.build([case])
        self.assertEqual(result["summary"]["failed_checks"], 1)
        self.assertIn("预期不符", render_path_report(result))

    def test_no_exit_cannot_satisfy_expected_missing_field(self):
        audit = {**self.audit, "root_exits": []}
        checks = check_case(audit, {"model_path_complete": True, "values": {"MISSING": None}})
        self.assertFalse(checks[1]["passed"])

    def test_record_and_action_expectations_checked_exactly(self):
        audit = deepcopy(self.audit)
        audit["root_exits"][0]["io_state"] = {"committed_records": [{"ID": "A"}], "pending_writes": []}
        checks = check_case(audit, {"model_path_complete": True, "committed_records": [{"ID": "B"}],
                                   "pending_writes": [], "io_actions": ["save"]})
        self.assertEqual([item["passed"] for item in checks], [True, False, True, True])

    def test_record_checks_do_not_equate_booleans_and_integers(self):
        audit = deepcopy(self.audit)
        audit["root_exits"][0]["io_state"] = {"committed_records": [{"COUNT": True}], "pending_writes": []}
        checks = check_case(audit, {"model_path_complete": True, "committed_records": [{"COUNT": 1}]})
        self.assertFalse(checks[-1]["passed"])
        with self.assertRaises(ValueError):
            check_case(audit, {"model_path_complete": True, "committed_records": [{"COUNT": True}]})

    def test_invalid_expectation_keys_and_shapes_are_rejected(self):
        for extra in ({"values": {1: "OK"}}, {"values": {"STATUS": []}},
                      {"committed_records": ["record"]}, {"pending_writes": [{1: "OK"}]},
                      {"io_actions": [None]}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                check_case(self.audit, {"model_path_complete": True, **extra})

    def test_boundary_reports_and_checks_preserve_pending_model_state(self):
        audit = deepcopy(self.audit)
        audit["scope"]["model_path_complete"] = False
        audit["root_exits"] = []
        audit["final_io_state"] = {"committed_records": [{"ID": "A", "STATE": "PENDING"}],
                                   "pending_writes": [{"ID": "A", "STATE": "DONE"}], "restart_checkpoint": None}
        case = deepcopy(self.case)
        case["expected"] = {"model_path_complete": False, "pending_writes": [{"ID": "A", "STATE": "DONE"}]}
        result = self.build([case], audit)
        self.assertEqual(result["summary"]["failed_checks"], 0)
        report = render_path_report(result)
        self.assertIn("未证明后续结果", report)
        self.assertIn("尚未提交的写入", report)
        self.assertIn("DONE", report)

    def test_case_set_rejects_escape_and_empty_checks(self):
        catalog = self.root / "cases.json"
        for scenario in ("../outside.json", "/outside.json", "C:/outside.json"):
            catalog.write_text(json.dumps({"schema_version": "1.0", "cases": [{**self.case, "scenario": scenario}]}))
            with self.subTest(scenario=scenario), self.assertRaises(ValueError):
                load_case_set(catalog)
        with self.assertRaises(ValueError):
            check_case(self.audit, {})
        with self.assertRaises(ValueError):
            check_case(self.audit, {"model_path_complete": True, "runtime_verified": True})

    def test_cli_failure_is_visible_without_running_model(self):
        invalid = self.root / "bad.json"
        invalid.write_text("[]")
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = main(["--source", str(self.source), "--output", str(self.output), "--contract", str(invalid),
                         "--scenario", str(invalid), "--entry", "ENTRYPG"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout.getvalue())["runner_status"], "FAILED")
        self.assertFalse(self.output.exists())

    def test_restart_case_uses_prior_committed_state_and_checkpoint(self):
        first = deepcopy(self.audit)
        first["root_exits"][0]["io_state"] = {"committed_records": [{"ID": "A", "STATE": "DONE"}],
                                               "pending_writes": [], "restart_checkpoint": {"key": ["A"]}}
        second_case = {**self.case, "id": "resumed", "resume_from": "selected"}
        with patch("run_framework_paths.validate_runtime_contract", side_effect=lambda c: c), \
             patch("run_framework_paths.audit_framework_paths", side_effect=[first, self.audit]) as execute:
            result = build_path_bundle(self.source, self.output, self.contract, [self.case, second_case])
        second_scenario = execute.call_args_list[1].args[3]
        self.assertEqual(second_scenario["records"], first["root_exits"][0]["io_state"]["committed_records"])
        self.assertEqual(second_scenario["restart_checkpoint"], {"key": ["A"]})
        self.assertEqual(result["cases"][1]["resume_from"], "selected")
        self.assertEqual(self.case["scenario"], {"records": []})

    def test_restart_case_without_valid_prior_checkpoint_is_not_fabricated(self):
        second = {**self.case, "id": "resumed", "resume_from": "selected"}
        with self.assertRaises(ValueError):
            self.build([self.case, second])
        self.assertFalse((self.output / "framework-paths.json").exists())


if __name__ == "__main__":
    unittest.main()
