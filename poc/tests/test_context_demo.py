from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from call_contexts import audit_call_contexts
from context_demo import build_context_demo, check_expectations, render_markdown
from context_paths import contextualize_errors
from structural_index import build_structural_index


FIXTURE_ROOT = POC_ROOT / "fixtures" / "call-context-v3"


class ContextDemoAcceptanceTests(unittest.TestCase):
    """Real indexed-source acceptance and source mutations; no COBOL execution."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.profile = json.loads((FIXTURE_ROOT / "profile.json").read_text(encoding="utf-8"))
        cls.database = Path(cls.temporary.name) / "baseline.sqlite"
        cls.bundle = build_context_demo(FIXTURE_ROOT / "main", cls.database, cls.profile)
        cls.boundary_database = Path(cls.temporary.name) / "boundaries.sqlite"
        build_structural_index(FIXTURE_ROOT / "boundaries", cls.boundary_database, quiet=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def contexts(self, program: str, bundle: dict | None = None) -> list[dict]:
        return [context for context in (bundle or self.bundle)["context_audit"]["contexts"]
                if context["program_name"] == program]

    def worker_routes(self, bundle: dict | None = None) -> list[dict]:
        return [route for route in (bundle or self.bundle)["context_error_view"]["parameter_routes"]
                if route["program_name"] == "SHAREDWK" and route["field_name"] == "WORK-STATUS"]

    def mutate_entry(self, transform) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            shutil.copytree(FIXTURE_ROOT / "main", source)
            entry = source / "programs" / "PAIRMAIN.cbl"
            original = entry.read_text(encoding="utf-8")
            changed = transform(original)
            self.assertNotEqual(changed, original)
            entry.write_text(changed, encoding="utf-8")
            return build_context_demo(source, Path(directory) / "changed.sqlite", self.profile)

    def test_baseline_passes_only_explicit_static_context_acceptance(self) -> None:
        acceptance = self.bundle["source_context_acceptance"]
        self.assertEqual(acceptance["status"], "PASS")
        self.assertGreater(acceptance["passed"], 0)
        self.assertEqual(acceptance["failed"], 0)
        self.assertTrue(all(check["status"] == "PASS" for check in acceptance["checks"]))
        actual = Counter(context["program_name"] for context in self.bundle["context_audit"]["contexts"])
        self.assertEqual(dict(actual), self.profile["expected_context_counts"])
        self.assertEqual(sum(actual.values()), 12)
        for flag in ("full_business_analysis_verified", "runtime_execution_tested", "model_called", "network_calls"):
            self.assertIs(self.bundle[flag], False)
        self.assertIs(acceptance["business_acceptance_passed"], False)
        self.assertIs(acceptance["runtime_execution_tested"], False)
        self.assertIs(self.bundle["context_audit"]["complete"], False)
        self.assertIs(self.bundle["error_audit"]["full_control_flow_proven"], False)
        self.assertIs(self.bundle["context_error_view"]["runtime_error_propagation_proven"], False)

    def test_shared_worker_contexts_reuse_source_identity_but_not_context_field_identity(self) -> None:
        workers = self.contexts("SHAREDWK")
        self.assertEqual(len(workers), 2)
        self.assertEqual(len({context["program_symbol_id"] for context in workers}), 1)
        self.assertEqual(len({context["via_callsite_id"] for context in workers}), 1)
        self.assertEqual(len({context["context_id"] for context in workers}), 2)
        self.assertEqual(len({tuple(context["callsite_chain"]) for context in workers}), 2)
        fields = [mapping["callee_field"] for context in workers for mapping in context["parameter_mappings"]
                  if mapping["callee_field"]["name"] == "WORK-STATUS"]
        self.assertEqual(len(fields), 2)
        self.assertEqual(len({field["symbol_id"] for field in fields}), 1)
        self.assertEqual(len({field["field_instance_id"] for field in fields}), 2)

    def test_nested_status_routes_end_at_separate_root_fields(self) -> None:
        routes = self.worker_routes()
        self.assertEqual(len(routes), 2)
        self.assertEqual({route["fields_inner_to_outer"][-1]["name"] for route in routes},
                         {"STATUS-A", "STATUS-B"})
        for route in routes:
            self.assertEqual([field["name"] for field in route["fields_inner_to_outer"][:-1]],
                             ["WORK-STATUS", "WRAP-STATUS"])
            self.assertEqual(route["terminal_reason"], "root_context_field")
            self.assertTrue(route["reference_return_candidate"])
            self.assertFalse(route["runtime_error_propagation_proven"])
            self.assertEqual([step["passing_mode"] for step in route["steps_inner_to_outer"]],
                             ["REFERENCE", "REFERENCE"])

    def test_local_error_observations_are_references_not_duplicated_execution_claims(self) -> None:
        overlays = [overlay for overlay in self.bundle["context_error_view"]["contexts"]
                    if overlay["program_name"] == "SHAREDWK"]
        self.assertEqual(len(overlays), 2)
        self.assertNotEqual(overlays[0]["context_id"], overlays[1]["context_id"])
        indexes = overlays[0]["local_observation_indexes"]
        self.assertTrue(indexes)
        self.assertEqual(indexes, overlays[1]["local_observation_indexes"])
        observations = self.bundle["error_audit"]["observations"]
        self.assertTrue(all(observations[index]["program_name"] == "SHAREDWK" for index in indexes))
        self.assertEqual(self.bundle["context_error_view"]["observation_reference_scope"],
                         "same_local_source_observation_reused_per_static_context")

    def test_rewired_second_status_fails_independent_oracle_despite_same_context_count(self) -> None:
        bundle = self.mutate_entry(lambda source: source.replace(
            "BY REFERENCE WORK-B STATUS-B", "BY REFERENCE WORK-B STATUS-A", 1))
        self.assertEqual(bundle["source_context_acceptance"]["status"], "FAIL")
        self.assertEqual(len(bundle["context_audit"]["contexts"]), 12)
        self.assertEqual({route["fields_inner_to_outer"][-1]["name"] for route in self.worker_routes(bundle)},
                         {"STATUS-A"})
        failures = [check for check in bundle["source_context_acceptance"]["checks"] if check["status"] == "FAIL"]
        self.assertTrue(any(check["name"].startswith("entry_mapping_") for check in failures))
        self.assertTrue(any(check["name"].startswith("nested_branch_") for check in failures))

    def test_content_status_blocks_outer_return_even_when_nested_call_is_reference(self) -> None:
        bundle = self.mutate_entry(lambda source: source.replace(
            "BY REFERENCE WORK-A STATUS-A", "BY REFERENCE WORK-A BY CONTENT STATUS-A", 1))
        self.assertEqual(bundle["source_context_acceptance"]["status"], "FAIL")
        routes = {route["fields_inner_to_outer"][-1]["name"]: route for route in self.worker_routes(bundle)}
        left, right = routes["STATUS-A"], routes["STATUS-B"]
        self.assertEqual(left["terminal_reason"], "root_context_field")
        self.assertEqual([step["passing_mode"] for step in left["steps_inner_to_outer"]],
                         ["REFERENCE", "CONTENT"])
        self.assertFalse(left["reference_return_candidate"])
        self.assertTrue(left["copy_boundary_blocks_outer_reference_return"])
        self.assertTrue(right["reference_return_candidate"])
        self.assertFalse(right["copy_boundary_blocks_outer_reference_return"])

    def test_deleted_second_wrapper_call_fails_expected_branch_counts(self) -> None:
        def remove_second(source: str) -> str:
            start = source.index("    CALL 'CALCWRAP' USING BY CONTENT REQUEST-B")
            end = source.index("    END-CALL.\n", start) + len("    END-CALL.\n")
            return source[:start] + source[end:]

        bundle = self.mutate_entry(remove_second)
        self.assertEqual(bundle["source_context_acceptance"]["status"], "FAIL")
        self.assertEqual(len(self.contexts("CALCWRAP", bundle)), 1)
        self.assertEqual(len(self.contexts("SHAREDWK", bundle)), 1)
        count_check = next(check for check in bundle["source_context_acceptance"]["checks"]
                           if check["name"] == "exact_context_counts")
        self.assertEqual(count_check["status"], "FAIL")

    def test_truncated_context_expansion_cannot_pass_full_fixture_oracle(self) -> None:
        audit = audit_call_contexts(self.database, "PAIRMAIN", max_contexts=2)
        self.assertEqual(len(audit["contexts"]), 2)
        self.assertTrue(audit["summary"]["truncated"])
        self.assertFalse(audit["complete"])
        self.assertIn("context_budget_exhausted", audit["summary"]["boundary_counts"])
        view = contextualize_errors(audit, self.bundle["error_audit"])
        acceptance = check_expectations(audit, view, self.profile)
        self.assertEqual(acceptance["status"], "FAIL")
        self.assertFalse(acceptance["business_acceptance_passed"])

    def test_context_error_projection_budget_cannot_silently_pass(self) -> None:
        view = contextualize_errors(self.bundle["context_audit"], self.bundle["error_audit"],
                                    max_observation_refs=1)
        self.assertTrue(view["summary"]["truncated"])
        acceptance = check_expectations(self.bundle["context_audit"], view, self.profile)
        self.assertEqual(acceptance["status"], "FAIL")
        check = next(item for item in acceptance["checks"] if item["name"] == "projection_not_truncated")
        self.assertEqual(check["status"], "FAIL")

    def test_negative_contexts_are_bounded_and_do_not_claim_complete_runtime_paths(self) -> None:
        for program, reason in (("CONTEXTREC", "recursive_call_chain_not_expanded"),
                                ("CONTEXTDYN", "dynamic_target_not_resolved"),
                                ("ALIASCALL", "duplicate_reference_actual_alias_possible")):
            with self.subTest(program=program):
                audit = audit_call_contexts(self.boundary_database, program)
                self.assertFalse(audit["complete"])
                self.assertFalse(audit["runtime_execution_tested"])
                self.assertIn(reason, audit["summary"]["boundary_counts"])
                self.assertLessEqual(len(audit["contexts"]), 2)
        loop = audit_call_contexts(self.boundary_database, "LOOPCALL")
        self.assertEqual(len(loop["contexts"]), 2)
        self.assertEqual(loop["summary"]["callsites_considered"], 1)
        self.assertFalse(loop["complete"])
        self.assertTrue(any("loop executions" in item for item in loop["limitations"]))

    def test_report_serializes_source_examples_and_honest_limitations(self) -> None:
        roundtrip = json.loads(json.dumps(self.bundle, ensure_ascii=False))
        self.assertEqual(roundtrip["snapshot_id"], self.bundle["snapshot_id"])
        markdown = render_markdown(roundtrip)
        self.assertIn("SHAREDWK::WORK-STATUS", markdown)
        self.assertIn("STATUS-A", markdown)
        self.assertIn("STATUS-B", markdown)
        self.assertIn("BY CONTENT/BY VALUE", markdown)
        self.assertIn("没有执行 COBOL", markdown)
        self.assertIn("源码验收 PASS 不能替代业务验收", markdown)
        self.assertEqual(self.bundle["snapshot_id"], self.bundle["context_error_view"]["snapshot_id"])


if __name__ == "__main__":
    unittest.main()
