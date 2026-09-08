from __future__ import annotations

from collections import defaultdict
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from exception_demo import build_exception_demo, render_markdown


FIXTURE_ROOT = POC_ROOT / "fixtures" / "exception-flow-v4"


class ExceptionDemoAcceptanceTests(unittest.TestCase):
    """Independent fixture-oracle and mutation checks, not COBOL execution."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.profile = json.loads((FIXTURE_ROOT / "profile.json").read_text(encoding="utf-8"))
        cls.regression_profile = json.loads(
            (FIXTURE_ROOT / "regressions" / "profile.json").read_text(encoding="utf-8"))
        cls.bundle = build_exception_demo(
            FIXTURE_ROOT / "main", Path(cls.temporary.name) / "main.sqlite", cls.profile)
        cls.regression = build_exception_demo(
            FIXTURE_ROOT / "regressions" / cls.regression_profile["source_root"],
            Path(cls.temporary.name) / "regression.sqlite", cls.regression_profile)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def audit(self, program: str, bundle: dict | None = None) -> dict:
        matches = [audit for audit in (bundle or self.bundle)["program_audits"]
                   if audit["program_name"] == program]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def event(self, program: str, kind: str, bundle: dict | None = None) -> dict:
        matches = [event for event in self.audit(program, bundle)["events"] if event["event_kind"] == kind]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def mutate_wrapper(self, transform) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            shutil.copytree(FIXTURE_ROOT / "main", source)
            wrapper = source / "programs" / "EXWRAP.cbl"
            original = wrapper.read_text(encoding="utf-8")
            changed = transform(original)
            self.assertNotEqual(changed, original)
            wrapper.write_text(changed, encoding="utf-8")
            return build_exception_demo(source, Path(directory) / "mutated.sqlite", self.profile)

    def test_positive_source_oracle_accepts_all_three_selected_failure_events(self) -> None:
        result = self.bundle["local_exception_acceptance"]
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["failed"], 0)
        self.assertGreater(result["passed"], 0)
        self.assertTrue(all(check["status"] == "PASS" for check in result["checks"]))
        self.assertEqual(len(self.profile["local_exceptional_expectations"]), 3)
        for expectation in self.profile["local_exceptional_expectations"]:
            event = self.event(expectation["program_name"], expectation["event_kind"])
            self.assertEqual(event["outputs"][expectation["output_field"]]["finding"],
                             "zero_on_all_modeled_exits")
            self.assertEqual(event["status_values"][expectation["status_field"]],
                             [str(expectation["status_value"])])
            self.assertFalse(event["runtime_path_verified"])

    def test_regression_pass_means_detected_unsafe_model_exit_not_business_success(self) -> None:
        result = self.regression["local_exception_acceptance"]
        self.assertEqual(result["status"], "PASS")
        selected_root = FIXTURE_ROOT / "regressions" / self.regression_profile["source_root"]
        anchor = self.regression_profile["local_exceptional_expectations"][0]["anchor"]
        self.assertTrue((selected_root / anchor["relative_path"]).is_file())
        event = self.event("EXBAD", "size_error", self.regression)
        self.assertTrue(any(ref["relative_path"] == anchor["relative_path"] for ref in event["evidence_refs"]))
        self.assertEqual(event["outputs"]["RESULT-AMOUNT"]["finding"], "nonzero_exit_possible_in_model")
        self.assertEqual(event["status_values"]["RESULT-STATUS"], ["24"])
        witnesses = self.audit("EXBAD", self.regression)["witnesses"]
        self.assertTrue(any(witness["event_id"] == event["event_id"]
                            and witness["output_values"]["RESULT-AMOUNT"] == "7" for witness in witnesses))
        self.assertFalse(self.regression["full_business_analysis_verified"])
        self.assertFalse(self.regression["runtime_execution_tested"])

    def test_clear_then_nonzero_overwrite_in_call_handler_fails_safe_exit_expectation(self) -> None:
        bundle = self.mutate_wrapper(lambda source: source.replace(
            "            MOVE ZERO TO WRAP-OUTPUT\n        NOT ON EXCEPTION",
            "            MOVE ZERO TO WRAP-OUTPUT\n            MOVE 7 TO WRAP-OUTPUT\n        NOT ON EXCEPTION", 1))
        self.assertEqual(bundle["local_exception_acceptance"]["status"], "FAIL")
        event = self.event("EXWRAP", "exception", bundle)
        self.assertEqual(event["outputs"]["WRAP-OUTPUT"]["finding"], "nonzero_exit_possible_in_model")
        self.assertEqual(event["status_values"]["WRAP-STATUS"], ["91"])
        self.assertTrue(any(witness["output_values"]["WRAP-OUTPUT"] == "7"
                            for witness in self.audit("EXWRAP", bundle)["witnesses"]))

    def test_error_status_reset_to_zero_is_rejected_even_when_output_stays_zero(self) -> None:
        bundle = self.mutate_wrapper(lambda source: source.replace(
            "MOVE 91 TO WRAP-STATUS", "MOVE ZERO TO WRAP-STATUS", 1))
        self.assertEqual(bundle["local_exception_acceptance"]["status"], "FAIL")
        event = self.event("EXWRAP", "exception", bundle)
        self.assertEqual(event["outputs"]["WRAP-OUTPUT"]["finding"], "zero_on_all_modeled_exits")
        self.assertEqual(event["status_values"]["WRAP-STATUS"], ["0"])

    def test_removed_size_error_clear_does_not_hide_nonzero_preserved_receiver(self) -> None:
        def retain_prior_output(source: str) -> str:
            source = source.replace("    MOVE ZERO TO WRAP-OUTPUT WRAP-STATUS.",
                                    "    MOVE 7 TO WRAP-OUTPUT.\n    MOVE ZERO TO WRAP-STATUS.", 1)
            return source.replace("                        MOVE ZERO TO WRAP-OUTPUT\n                    NOT ON SIZE ERROR",
                                  "                    NOT ON SIZE ERROR", 1)

        bundle = self.mutate_wrapper(retain_prior_output)
        self.assertEqual(bundle["local_exception_acceptance"]["status"], "FAIL")
        event = self.event("EXWRAP", "size_error", bundle)
        self.assertEqual(event["outputs"]["WRAP-OUTPUT"]["finding"], "nonzero_exit_possible_in_model")
        self.assertEqual(event["status_values"]["WRAP-STATUS"], ["24"])
        self.assertTrue(any(witness["event_id"] == event["event_id"]
                            and witness["output_values"]["WRAP-OUTPUT"] == "7"
                            for witness in self.audit("EXWRAP", bundle)["witnesses"]))

    def test_overwrite_after_perform_return_invalidates_both_failure_handlers(self) -> None:
        bundle = self.mutate_wrapper(lambda source: source.replace(
            "    PERFORM RUN-STEP THRU STEP-END.\n    GOBACK.",
            "    PERFORM RUN-STEP THRU STEP-END.\n    MOVE 7 TO WRAP-OUTPUT.\n    GOBACK.", 1))
        self.assertEqual(bundle["local_exception_acceptance"]["status"], "FAIL")
        for kind in ("exception", "size_error"):
            self.assertEqual(self.event("EXWRAP", kind, bundle)["outputs"]["WRAP-OUTPUT"]["finding"],
                             "nonzero_exit_possible_in_model")

    def test_local_event_results_are_referenced_by_two_static_wrapper_contexts(self) -> None:
        contexts = [context for context in self.bundle["context_audit"]["contexts"]
                    if context["program_name"] == "EXWRAP"]
        self.assertEqual(len(contexts), 2)
        context_ids = {context["context_id"] for context in contexts}
        refs = [item for item in self.bundle["context_event_refs"] if item["program_name"] == "EXWRAP"]
        by_event = defaultdict(list)
        for item in refs:
            by_event[item["event_id"]].append(item)
            self.assertEqual(item["local_model_scope"], "independent_local_analysis_not_interprogram_execution")
            audit = self.bundle["program_audits"][item["program_audit_index"]]
            self.assertEqual(audit["program_name"], "EXWRAP")
            self.assertIn(item["event_id"], {event["event_id"] for event in audit["events"]})
        self.assertEqual(len(by_event), 2)
        for references in by_event.values():
            self.assertEqual(len(references), 2)
            self.assertEqual({item["context_id"] for item in references}, context_ids)
            self.assertEqual(len({item["program_audit_index"] for item in references}), 1)

    def test_missing_independent_source_anchor_is_not_an_implicit_pass(self) -> None:
        profile = copy.deepcopy(self.profile)
        profile["local_exceptional_expectations"][0]["anchor"]["source_text_contains"] = "CALL 'ABSENTLEAF'"
        with tempfile.TemporaryDirectory() as directory:
            bundle = build_exception_demo(FIXTURE_ROOT / "main", Path(directory) / "anchor.sqlite", profile)
        self.assertEqual(bundle["local_exception_acceptance"]["status"], "FAIL")
        self.assertGreater(bundle["local_exception_acceptance"]["failed"], 0)

    def test_every_witness_is_an_abstract_graph_path_with_existing_edges(self) -> None:
        for audit in self.bundle["program_audits"]:
            cfg_edges = {(edge["source"], edge["target"], edge["outcome"]) for edge in audit["cfg"]["edges"]}
            for witness in audit["witnesses"]:
                self.assertEqual(witness["scope"], "abstract_model_path_not_runtime_trace")
                self.assertTrue(all((edge["source"], edge["target"], edge["outcome"]) in cfg_edges
                                    for edge in witness["edges"]))
                for first, second in zip(witness["edges"], witness["edges"][1:]):
                    self.assertEqual(first["target"], second["source"])
                if witness["edges"]:
                    self.assertEqual(witness["edges"][-1]["target"], witness["exit_node_id"])

    def test_report_serializes_snapshot_and_keeps_all_runtime_and_business_flags_false(self) -> None:
        for original in (self.bundle, self.regression):
            bundle = json.loads(json.dumps(original, ensure_ascii=False))
            for flag in ("full_business_analysis_verified", "runtime_execution_tested", "model_called", "network_calls"):
                self.assertIs(bundle[flag], False)
            self.assertEqual(bundle["snapshot_id"], bundle["context_audit"]["snapshot_id"])
            for audit in bundle["program_audits"]:
                self.assertEqual(bundle["snapshot_id"], audit["snapshot_id"])
                self.assertEqual(bundle["snapshot_id"], audit["cfg"]["snapshot_id"])
                self.assertIs(audit["complete"], False)
                self.assertIs(audit["full_control_flow_proven"], False)
                self.assertIs(audit["runtime_execution_tested"], False)
            markdown = render_markdown(bundle)
            self.assertIn("COBOL", markdown)
            self.assertIn(bundle["local_exception_acceptance"]["status"], markdown)


if __name__ == "__main__":
    unittest.main()
