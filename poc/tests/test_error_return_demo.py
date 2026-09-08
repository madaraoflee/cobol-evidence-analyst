from __future__ import annotations

import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))
from error_return_demo import build_error_return_demo, check_case, main, render_markdown


FIXTURE_ROOT = POC_ROOT / "fixtures" / "error-return-v5"


class ReturnOracleTests(unittest.TestCase):
    def setUp(self):
        self.audit = {"root_exits": [{"output_values": {"OUT-VALUE": "0"}, "status_values": {"OUT-STATUS": "21"}}],
                      "boundaries": [], "events": [], "contexts": [], "witnesses": [],
                      "summary": {"modeled_root_exits": 1, "truncated": False, "root_exits_truncated": False}}
        self.case = {"expected_root_values": {"OUT-VALUE": "0", "OUT-STATUS": "21"}}

    def failed(self):
        return any(check["status"] == "FAIL" for check in check_case(self.audit, self.case))

    def test_complete_explicit_oracle_passes(self):
        self.assertFalse(self.failed())

    def test_no_exit_is_not_vacuous_success(self):
        self.audit["root_exits"] = []
        self.audit["summary"]["modeled_root_exits"] = 0
        self.assertTrue(self.failed())

    def test_missing_output_is_not_an_explicit_unknown_value(self):
        self.audit["root_exits"][0]["output_values"] = {}
        self.case["expected_root_values"]["OUT-VALUE"] = None
        self.assertTrue(self.failed())

    def test_exploration_or_exit_record_truncation_blocks_acceptance(self):
        for key in ("truncated", "root_exits_truncated"):
            with self.subTest(key=key):
                self.audit["summary"][key] = True
                self.assertTrue(self.failed())
                self.audit["summary"][key] = False
        self.audit["summary"]["modeled_root_exits"] = 2
        self.assertTrue(self.failed())

    def test_boundary_cannot_be_hidden_by_a_zero_observed_exit(self):
        self.audit["boundaries"] = [{"reason": "unsupported_later_call"}]
        self.assertTrue(self.failed())

    def test_absent_conditional_path_is_not_vacuous_success(self):
        self.case["expected_conditional_exits"] = [{"when": {"OUT-STATUS": "24"}, "then": {"OUT-VALUE": "0"}}]
        self.assertTrue(self.failed())

    def test_call_exception_event_violates_normal_return_only_case(self):
        self.case["no_call_exception_events"] = True
        self.audit["events"] = [{"event_id": "call_failure", "event_kind": "call_exception"}]
        self.assertTrue(self.failed())

    def test_alternative_sets_require_all_expected_values(self):
        self.case["expected_root_value_sets"] = {"OUT-STATUS": ["21", "24"]}
        self.assertTrue(self.failed())

    def test_numeric_equivalence_and_explicit_unknown_are_distinct(self):
        self.case["expected_root_values"] = {"OUT-VALUE": "0.00", "OUT-STATUS": "21.0"}
        self.assertFalse(self.failed())
        self.audit["root_exits"][0]["output_values"]["OUT-VALUE"] = None
        self.assertTrue(self.failed())

    def test_missing_oracle_and_nonfinite_expected_value_are_rejected(self):
        for case in ({}, {"expected_root_values": {"OUT-VALUE": "NaN"}},
                     {"expected_root_values": {"OUT-VALUE": True}}):
            with self.subTest(case=case), self.assertRaises(ValueError):
                check_case(self.audit, case)


class ReturnDemoIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.profile = json.loads((FIXTURE_ROOT / "profile.json").read_text(encoding="utf-8"))
        cls.source = (FIXTURE_ROOT / cls.profile["source_root"]).resolve()
        cls.bundle = build_error_return_demo(cls.source, Path(cls.temporary.name) / "main.sqlite", cls.profile)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_independent_main_expectations_and_actual_return_chains_pass(self):
        self.assertEqual(self.bundle["return_acceptance"]["status"], "PASS")
        chains = [check for case in self.bundle["cases"] for check in case["checks"]
                  if check["check_id"].startswith("RETURN-CHAIN:")]
        self.assertEqual(len(chains), 4)
        self.assertTrue(all(check["status"] == "PASS" and len(check["verified_steps"]) == 6 for check in chains))

    def test_report_and_json_do_not_claim_runtime_or_full_business_completion(self):
        bundle = json.loads(json.dumps(self.bundle))
        for field in ("full_business_analysis_verified", "runtime_execution_tested", "model_called", "network_calls"):
            self.assertIs(bundle[field], False)
        report = render_markdown(bundle)
        self.assertIn("T01", report)
        self.assertIn("正常结果仍为未知", report)
        self.assertIn("LEAF-CODE", report)
        self.assertIn("STATUS-A", report)
        self.assertIn(bundle["snapshot_id"], report)

    def test_wrong_return_context_cannot_pass_on_matching_numeric_root_values(self):
        case = copy.deepcopy(self.profile["cases"][0])
        case["expected_return_chains"][0]["entry_input_field"] = "ABSENT-INPUT"
        checks = check_case(self.bundle["cases"][0]["audit"], case)
        selected = next(check for check in checks if check["check_id"] == "RETURN-CHAIN:A")
        self.assertEqual(selected["status"], "FAIL")
        self.assertEqual(selected["matched_events"], 0)

    def test_missing_return_witness_is_not_replaced_with_expected_narrative(self):
        audit = copy.deepcopy(self.bundle["cases"][0]["audit"])
        audit["witnesses"] = []
        checks = check_case(audit, self.profile["cases"][0])
        chains = [check for check in checks if check["check_id"].startswith("RETURN-CHAIN:")]
        self.assertTrue(all(check["status"] == "FAIL" and not check["verified_steps"] for check in chains))

    def test_return_chain_cannot_join_a_different_finalizer_invocation(self):
        audit = copy.deepcopy(self.bundle["cases"][0]["audit"])
        for witness in audit["witnesses"]:
            for step in witness["trace"]:
                if step["kind"] == "call_return" and step["callee_program"] == "EXFINAL":
                    step["callee_context_id"] = "different_finalizer_invocation"
        chains = [check for check in check_case(audit, self.profile["cases"][0]) if check["check_id"].startswith("RETURN-CHAIN:")]
        self.assertTrue(all(check["status"] == "FAIL" for check in chains))

    def test_report_never_draws_a_copy_parameter_as_return_writeback(self):
        report = render_markdown(self.bundle)
        self.assertNotIn("FINAL-STATUS → STATUS-A", report)
        self.assertNotIn("FINAL-STATUS → STATUS-B", report)
        self.assertIn("FINAL-CODE → CODE-A", report)

    def test_content_and_value_report_verifies_local_error_but_no_outer_writeback(self):
        profile_path = FIXTURE_ROOT / "copy-boundary" / "profile.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            bundle = build_error_return_demo(profile_path.parent / profile["source_root"],
                                              Path(directory) / "copy.sqlite", profile)
        self.assertEqual(bundle["return_acceptance"]["status"], "PASS")
        copies = [check for check in bundle["cases"][0]["checks"] if check["check_id"].startswith("COPY-BARRIER:")]
        self.assertEqual(len(copies), 2)
        self.assertTrue(all(check["status"] == "PASS" for check in copies))
        case = copy.deepcopy(profile["cases"][0])
        case["expected_local_leaf_returns"][0].update(root_field="ROOT-VALUE-STATUS", root_value="8")
        changed = check_case(bundle["cases"][0]["audit"], case)
        self.assertEqual(next(check for check in changed if check["check_id"] == "COPY-BARRIER:CONTENT")["status"], "FAIL")

    def test_cli_mutates_only_a_temporary_copy_and_returns_failure_for_unsafe_output(self):
        original = (self.source / "programs" / "EXENTRY.cbl").read_bytes()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            output = Path(directory) / "result.json"
            code = main(["--caller-overwrite", "--database", str(Path(directory) / "variant.sqlite"),
                         "--json-output", str(output)])
            bundle = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(bundle["return_acceptance"]["status"], "FAIL")
        self.assertIs(bundle["source_mutation"]["original_source_unchanged"], True)
        self.assertNotEqual(bundle["snapshot_id"], self.bundle["snapshot_id"])
        self.assertTrue(all(record["output_values"]["OUTPUT-A"] == "7"
                            for record in bundle["cases"][0]["audit"]["root_exits"]))
        self.assertEqual(original, (self.source / "programs" / "EXENTRY.cbl").read_bytes())

    def test_cli_rejects_output_collision_with_source_profile_or_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "safe.sqlite")
            for output in (database, str(self.source / "programs" / "EXENTRY.cbl"), str(FIXTURE_ROOT / "profile.json")):
                with self.subTest(output=output), self.assertRaises(ValueError):
                    main(["--database", database, "--json-output", output])


if __name__ == "__main__":
    unittest.main()
