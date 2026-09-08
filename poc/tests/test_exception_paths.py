from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from error_paths import ErrorContract
from exception_paths import _fits, _layout, analyze_exception_paths, audit_exception_paths
from structural_index import build_structural_index

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "exception-flow-v4"


class ExceptionPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temporary.name) / "main.sqlite"
        build_structural_index(FIXTURE / "main", cls.database, quiet=True)
        cls.wrapper = audit_exception_paths(cls.database, "EXWRAP", ErrorContract(
            "EXWRAP", ("WRAP-STATUS",), ("WRAP-OUTPUT",)))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_wrapper_failure_branches_have_separate_statuses_and_zero_exits(self):
        events = {e["event_kind"]: e for e in self.wrapper["events"]}
        self.assertEqual(set(events), {"exception", "size_error"})
        for kind, status in (("exception", "91"), ("size_error", "24")):
            self.assertEqual(events[kind]["outputs"]["WRAP-OUTPUT"]["finding"], "zero_on_all_modeled_exits")
            self.assertEqual(events[kind]["status_values"]["WRAP-STATUS"], [status])
            self.assertEqual(events[kind]["blocked_paths"], 0)
        self.assertFalse(self.wrapper["summary"]["truncated"])

    def test_join_size_error_clears_output_after_nested_status_gates(self):
        result = audit_exception_paths(self.database, "EXJOIN", ErrorContract(
            "EXJOIN", ("LEFT-STATUS", "RIGHT-STATUS", "JOIN-STATUS"), ("JOIN-OUTPUT",)))
        event = result["events"][0]
        self.assertEqual(event["outputs"]["JOIN-OUTPUT"]["finding"], "zero_on_all_modeled_exits")
        self.assertEqual(event["status_values"]["JOIN-STATUS"], ["25"])

    def test_overwrite_after_clear_has_a_nonzero_model_witness(self):
        database = Path(self.temporary.name) / "bad.sqlite"
        build_structural_index(FIXTURE / "regressions" / "programs", database, quiet=True)
        result = audit_exception_paths(database, "EXBAD", ErrorContract(
            "EXBAD", ("RESULT-STATUS",), ("RESULT-AMOUNT",)))
        self.assertEqual(result["events"][0]["outputs"]["RESULT-AMOUNT"]["finding"], "nonzero_exit_possible_in_model")
        self.assertTrue(any(w["output_values"]["RESULT-AMOUNT"] == "7" for w in result["witnesses"]))

    def test_witness_keeps_source_edges_and_distinguishes_runtime_scope(self):
        nodes = {n["node_id"] for n in self.wrapper["cfg"]["nodes"]}
        for witness in self.wrapper["witnesses"]:
            self.assertEqual(witness["scope"], "abstract_model_path_not_runtime_trace")
            self.assertTrue(witness["edges"])
            self.assertTrue(all(e["source"] in nodes and e["target"] in nodes for e in witness["edges"]))
        for flag in ("complete", "full_control_flow_proven", "runtime_execution_tested"):
            self.assertFalse(self.wrapper[flag])

    def test_state_and_step_budgets_cannot_keep_all_exits_claim(self):
        for budget in ({"max_states": 3}, {"max_steps": 3}):
            with self.subTest(budget=budget):
                result = audit_exception_paths(self.database, "EXWRAP", ErrorContract(
                    "EXWRAP", ("WRAP-STATUS",), ("WRAP-OUTPUT",)), **budget)
                self.assertTrue(result["summary"]["truncated"])
                self.assertTrue(all(e["outputs"]["WRAP-OUTPUT"]["finding"] != "zero_on_all_modeled_exits" for e in result["events"]))

    def test_unrepresentable_moves_are_unknown_not_false_nonzero_constants(self):
        small = _layout("99")
        self.assertEqual(_fits("91", small), "91")
        for value in ("100", "0.01", "-1"):
            self.assertIsNone(_fits(value, small))
        self.assertEqual(_fits("0", small), "0")
        self.assertEqual(_fits("-1", _layout("S99")), "-1")

    def test_witness_display_budget_does_not_hide_its_truncation(self):
        result = audit_exception_paths(self.database, "EXWRAP", ErrorContract(
            "EXWRAP", ("WRAP-STATUS",), ("WRAP-OUTPUT",)), max_witnesses=1)
        self.assertTrue(result["summary"]["witnesses_truncated"])
        self.assertFalse(result["summary"]["truncated"])
        self.assertEqual(len(result["witnesses"]), 1)
        self.assertTrue(all(e["outputs"]["WRAP-OUTPUT"]["finding"] == "zero_on_all_modeled_exits" for e in result["events"]))

    def test_truthy_nonboolean_cannot_declare_call_effects_complete(self):
        for effect in ({"complete": "false", "reference_fields": []},
                       {"complete": True, "reference_fields": "WRAP-OUTPUT"},
                       {"complete": True, "reference_fields": [False]}):
            with self.subTest(effect=effect), self.assertRaises(ValueError):
                analyze_exception_paths(self.wrapper["cfg"], ErrorContract(
                    "EXWRAP", ("WRAP-STATUS",), ("WRAP-OUTPUT",)), {}, {"call": effect})


if __name__ == "__main__":
    unittest.main()
