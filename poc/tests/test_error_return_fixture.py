from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from error_paths import ErrorContract
from interprogram_paths import audit_interprogram_paths
from structural_index import build_structural_index


FIXTURE_ROOT = POC_ROOT / "fixtures" / "error-return-v5"


def contracts(profile: dict) -> tuple[ErrorContract, ...]:
    return tuple(ErrorContract(item["program_name"], tuple(item["status_fields"]),
                               tuple(item["output_fields"])) for item in profile["contracts"])


class ErrorReturnFixtureTests(unittest.TestCase):
    """Independent interprogram model expectations, never runtime acceptance."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.profile = json.loads((FIXTURE_ROOT / "profile.json").read_text(encoding="utf-8"))
        cls.source = (FIXTURE_ROOT / cls.profile["source_root"]).resolve()
        cls.database = Path(cls.temporary.name) / "main.sqlite"
        cls.build = build_structural_index(cls.source, cls.database, quiet=True)
        cls.results = {case["case_id"]: audit_interprogram_paths(
            cls.database, cls.profile["root_program"], contracts(cls.profile),
            initial_values=case["initial_values"], call_policy=case["call_policy"])
            for case in cls.profile["cases"]}
        cls.copy_profile = json.loads(
            (FIXTURE_ROOT / "copy-boundary" / "profile.json").read_text(encoding="utf-8"))
        cls.copy_source = (FIXTURE_ROOT / "copy-boundary" / cls.copy_profile["source_root"]).resolve()
        cls.copy_database = Path(cls.temporary.name) / "copy.sqlite"
        cls.copy_build = build_structural_index(cls.copy_source, cls.copy_database, quiet=True)
        cls.copy_case = cls.copy_profile["cases"][0]
        cls.copy_result = audit_interprogram_paths(
            cls.copy_database, cls.copy_profile["root_program"], contracts(cls.copy_profile),
            initial_values=cls.copy_case["initial_values"], call_policy=cls.copy_case["call_policy"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def exit_values(self, result: dict) -> list[dict]:
        values = []
        for exit_item in result["root_exits"]:
            outputs, statuses = exit_item["output_values"], exit_item["status_values"]
            for key in outputs.keys() & statuses.keys():
                self.assertEqual(outputs[key], statuses[key])
            values.append({**outputs, **statuses})
        return values

    def assert_case(self, result: dict, case: dict) -> None:
        values = self.exit_values(result)
        self.assertTrue(values)
        self.assertFalse(result["summary"]["truncated"])
        self.assertFalse(result["summary"]["root_exits_truncated"])
        self.assertEqual(result["boundaries"], [])
        for name, expected in case["expected_root_values"].items():
            self.assertEqual({exit_value[name] for exit_value in values}, {expected}, name)
        for name, expected in case["expected_root_value_sets"].items():
            self.assertEqual({exit_value[name] for exit_value in values}, set(expected), name)
        for condition in case["expected_conditional_exits"]:
            selected = [value for value in values
                        if all(value[field] == expected for field, expected in condition["when"].items())]
            self.assertTrue(selected, condition)
            for field, expected in condition["then"].items():
                self.assertEqual({value[field] for value in selected}, {expected}, condition)
        if case["no_call_exception_events"]:
            self.assertFalse(any(event["event_kind"] in {"exception", "call_exception"}
                                 for event in result["events"]))

    def leaf_context_ids(self, branch: str, context_audit: dict | None = None) -> set[str]:
        contexts = (context_audit or self.results["T01-BOTH-ZERO"])["contexts"]
        by_id = {context["context_id"]: context for context in contexts}
        selected = set()
        for context in contexts:
            if context["program_chain"] != ["EXENTRY", "EXWRAP", "EXLEAF"]:
                continue
            wrapper = by_id[context["parent_context_id"]]
            if any(mapping["caller_field"]["name"] == f"INPUT-{branch}"
                   and mapping["callee_field"]["name"] == "WRAP-INPUT"
                   for mapping in wrapper["parameter_mappings"]):
                selected.add(context["context_id"])
        self.assertEqual(len(selected), 1)
        return selected

    def leaf_events(self, result: dict, branch: str,
                    context_audit: dict | None = None, status: str = "21") -> list[dict]:
        context_ids = self.leaf_context_ids(branch, context_audit or result)
        return [event for event in result["events"]
                if event["event_kind"] == "business_error_return"
                and event["program_name"] == "EXLEAF" and event["context_id"] in context_ids
                and event["status_field"] == "LEAF-CODE" and event["status_value"] == status]

    def test_profile_resolves_unchanged_v4_source_and_has_independent_model_only_cases(self) -> None:
        self.assertEqual(self.source, (POC_ROOT / "fixtures" / "exception-flow-v4" / "main").resolve())
        self.assertEqual(self.profile["source_root_base"], "profile_directory")
        self.assertEqual(self.profile["task_id"], "T01")
        self.assertEqual(self.build["files"]["candidate"], 5)
        self.assertEqual(self.build["call_bindings"]["confirmed_bindings"], 23)
        self.assertEqual(len(self.profile["cases"]), 3)
        for case in [*self.profile["cases"], self.copy_case]:
            self.assertEqual(case["execution_scope"], "static_interprogram_model_not_cobol_execution")
            self.assertEqual(case["runtime_execution_status"], "NOT_EXECUTED")
            self.assertEqual(case["call_policy"], "normal_return_only")
        for case in self.profile["cases"]:
            for chain in case["expected_return_chains"]:
                self.assertEqual(chain["origin_program"], "EXLEAF")
                self.assertEqual(chain["origin_status_field"], "LEAF-CODE")
                self.assertEqual(chain["return_to_status_field"], "LEAF-STATUS")
                self.assertEqual(chain["status_value"], "21")

    def test_both_zero_inputs_return_leaf_error_to_each_root_branch_and_total(self) -> None:
        case = self.profile["cases"][0]
        result = self.results[case["case_id"]]
        self.assert_case(result, case)
        self.assertEqual({values["CODE-A"] for values in self.exit_values(result)}, {"21"})
        self.assertEqual({values["CODE-B"] for values in self.exit_values(result)}, {"21"})
        for branch in ("A", "B"):
            events = self.leaf_events(result, branch)
            self.assertTrue(events)
            event_ids = {event["event_id"] for event in events}
            for exit_item in result["root_exits"]:
                self.assertTrue(event_ids & set(exit_item["event_ids"]))
            for event in events:
                self.assertEqual(event["outputs"][f"OUTPUT-{branch}"]["finding"], "zero_on_all_modeled_exits")
                self.assertEqual(event["outputs"]["TOTAL-AMOUNT"]["finding"], "zero_on_all_modeled_exits")

    def test_a_error_does_not_contaminate_b_context_or_status(self) -> None:
        case = next(case for case in self.profile["cases"] if case["case_id"] == "T01-A-ERROR")
        result = self.results[case["case_id"]]
        self.assert_case(result, case)
        self.assertTrue(self.leaf_events(result, "A"))
        self.assertEqual(self.leaf_events(result, "B"), [])
        self.assertNotIn("21", {value["CODE-B"] for value in self.exit_values(result)})
        self.assertTrue(self.leaf_context_ids("A").isdisjoint(self.leaf_context_ids("B")))

    def test_error_return_ledger_closes_leaf_wrapper_finalizer_and_join_in_order(self) -> None:
        result = self.results["T01-BOTH-ZERO"]
        contexts = {context["context_id"]: context for context in result["contexts"]}

        def has_parameter(step, caller, callee, value, mode, written_back):
            return any(parameter["caller_field"] == caller and parameter["callee_field"] == callee
                       and parameter["value"] == value and parameter["passing_mode"] == mode
                       and parameter["written_back"] is written_back for parameter in step.get("parameters", []))

        for branch, join_field in (("A", "LEFT-STATUS"), ("B", "RIGHT-STATUS")):
            event = self.leaf_events(result, branch)[0]
            witnesses = [witness for witness in result["witnesses"] if event["event_id"] in witness["event_ids"]]
            self.assertTrue(witnesses)
            trace = witnesses[0]["trace"]
            pending_frames = []
            for step in trace:
                if step["kind"] == "call_enter":
                    pending_frames.append(step["callee_context_id"])
                elif step["kind"] == "call_return":
                    self.assertTrue(pending_frames)
                    self.assertEqual(pending_frames.pop(), step["callee_context_id"])
            self.assertEqual(pending_frames, [])
            wrapper_id = contexts[event["context_id"]]["parent_context_id"]
            requirements = [
                ("call_return", "EXLEAF", event["context_id"], "LEAF-STATUS", "LEAF-CODE", "REFERENCE", True),
                ("call_return", "EXWRAP", wrapper_id, f"STATUS-{branch}", "WRAP-STATUS", "REFERENCE", True),
                ("call_enter", "EXFINAL", None, f"STATUS-{branch}", "FINAL-STATUS", "CONTENT", False),
                ("call_return", "EXFINAL", None, f"CODE-{branch}", "FINAL-CODE", "REFERENCE", True),
                ("call_enter", "EXJOIN", None, f"CODE-{branch}", join_field, "CONTENT", False),
                ("call_return", "EXJOIN", None, "SUMMARY-STATUS", "JOIN-STATUS", "REFERENCE", True),
            ]
            start = 0
            selected = []
            for kind, program, context_id, caller, callee, mode, written_back in requirements:
                candidates = [index for index in range(start, len(trace))
                              if trace[index]["kind"] == kind and trace[index].get("callee_program") == program
                              and (context_id is None or trace[index].get("callee_context_id") == context_id)
                              and has_parameter(trace[index], caller, callee, "21", mode, written_back)]
                self.assertTrue(candidates, (branch, program, caller, callee))
                selected.append(trace[candidates[0]])
                start = candidates[0] + 1
            self.assertEqual(selected[2]["callee_context_id"], selected[3]["callee_context_id"])
            self.assertEqual(selected[4]["callee_context_id"], selected[5]["callee_context_id"])

    def test_mirrored_b_error_keeps_left_error_priority_without_status_crossing(self) -> None:
        case = next(case for case in self.profile["cases"] if case["case_id"] == "T01-B-ERROR")
        result = self.results[case["case_id"]]
        self.assert_case(result, case)
        self.assertTrue(self.leaf_events(result, "B"))
        self.assertEqual(self.leaf_events(result, "A"), [])
        self.assertNotIn("21", {value["CODE-A"] for value in self.exit_values(result)})

    def test_content_and_value_middle_calls_do_not_write_leaf_status_back_to_root(self) -> None:
        self.assert_case(self.copy_result, self.copy_case)
        self.assertEqual(self.copy_build["call_bindings"]["boundary_counts"], {})
        with closing(sqlite3.connect(self.copy_database)) as connection:
            modes = set(connection.execute(
                "SELECT caller_program, passing_mode FROM call_bindings "
                "WHERE caller_program IN ('MIDCONTENT','MIDVALUE')"))
        self.assertEqual(modes, {("MIDCONTENT", "CONTENT"), ("MIDVALUE", "VALUE")})
        for expected in self.copy_case["expected_local_leaf_returns"]:
            events = [event for event in self.copy_result["events"]
                      if event["event_kind"] == "business_error_return"
                      and event["program_name"] == expected["program_name"]
                      and event["status_field"] == expected["status_field"]
                      and event["status_value"] == expected["value"]]
            self.assertTrue(events)
            self.assertEqual({value[expected["root_field"]] for value in self.exit_values(self.copy_result)},
                             {expected["root_value"]})
            event_ids = {event["event_id"] for event in events}
            event_contexts = {event["context_id"] for event in events}
            returns = [step for witness in self.copy_result["witnesses"]
                       if event_ids & set(witness["event_ids"]) for step in witness["trace"]
                       if step["kind"] == "call_return" and step.get("callee_context_id") in event_contexts]
            self.assertTrue(returns)
            self.assertTrue(all(any(parameter["callee_field"] == expected["status_field"]
                                    and parameter["value"] == "21"
                                    and parameter["passing_mode"] == expected["passing_mode"]
                                    and parameter["written_back"] is False
                                    for parameter in step["parameters"]) for step in returns))

    def test_caller_overwrite_after_finalizer_return_is_visible_after_leaf_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            shutil.copytree(self.source, source)
            entry = source / "programs" / "EXENTRY.cbl"
            original = entry.read_text(encoding="utf-8")
            start = original.index("    CALL 'EXFINAL' USING BY CONTENT WORK-A STATUS-A")
            end = original.index("    END-CALL.\n", start) + len("    END-CALL.\n")
            entry.write_text(original[:end] + "    MOVE 7 TO OUTPUT-A.\n" + original[end:], encoding="utf-8")
            database = Path(directory) / "changed.sqlite"
            build_structural_index(source, database, quiet=True)
            result = audit_interprogram_paths(database, self.profile["root_program"], contracts(self.profile),
                initial_values={"INPUT-A": "0", "INPUT-B": "0"}, call_policy="normal_return_only")
        expected = next(item for item in self.profile["mutation_expectations"]
                        if item["mutation_id"] == "T01-CALLER-OVERWRITE")["expected_root_values"]
        self.assertFalse(result["summary"]["truncated"])
        self.assertTrue(result["root_exits"])
        for field, value in expected.items():
            self.assertEqual({actual[field] for actual in self.exit_values(result)}, {value})
        events = self.leaf_events(result, "A")
        self.assertTrue(events)
        self.assertTrue(all(event["outputs"]["OUTPUT-A"]["finding"] == "nonzero_exit_possible_in_model"
                            for event in events))
        self.assertNotEqual(result["snapshot_id"], self.results["T01-BOTH-ZERO"]["snapshot_id"])

    def test_reindex_leaf_status_change_changes_snapshot_and_real_model_return_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            shutil.copytree(self.source, source)
            database = Path(directory) / "reindexed.sqlite"
            build_structural_index(source, database, quiet=True)
            before = audit_interprogram_paths(database, self.profile["root_program"], contracts(self.profile),
                initial_values={"INPUT-A": "0", "INPUT-B": "0"}, call_policy="normal_return_only")
            leaf = source / "programs" / "EXLEAF.cbl"
            original = leaf.read_text(encoding="utf-8")
            self.assertEqual(original.count("MOVE 21 TO LEAF-CODE"), 1)
            leaf.write_text(original.replace("MOVE 21 TO LEAF-CODE", "MOVE 22 TO LEAF-CODE"), encoding="utf-8")
            rebuilt = build_structural_index(source, database, quiet=True)
            after = audit_interprogram_paths(database, self.profile["root_program"], contracts(self.profile),
                initial_values={"INPUT-A": "0", "INPUT-B": "0"}, call_policy="normal_return_only")
        self.assertNotEqual(before["snapshot_id"], after["snapshot_id"])
        self.assertEqual(after["snapshot_id"], rebuilt["snapshot_id"])
        self.assertEqual({value["CODE-A"] for value in self.exit_values(before)}, {"21"})
        expected = next(item for item in self.profile["mutation_expectations"]
                        if item["mutation_id"] == "T01-LEAF-STATUS-CHANGED")["expected_root_values"]
        self.assertTrue(after["root_exits"])
        for field, value in expected.items():
            self.assertEqual({actual[field] for actual in self.exit_values(after)}, {value})
        leaf_returns = [event for event in after["events"]
                        if event["event_kind"] == "business_error_return" and event["program_name"] == "EXLEAF"]
        self.assertTrue(leaf_returns)
        self.assertEqual({event["status_value"] for event in leaf_returns}, {"22"})

    def test_normal_return_policy_is_explicit_and_does_not_claim_runtime_execution(self) -> None:
        for result in [*self.results.values(), self.copy_result]:
            self.assertFalse(result["runtime_execution_tested"])
            self.assertFalse(result["complete"])
            self.assertTrue(result["root_exits"])
            self.assertEqual(result["summary"]["modeled_root_exits"], len(result["root_exits"]))
            self.assertFalse(any(event["event_kind"] in {"exception", "call_exception"}
                                 for event in result["events"]))


if __name__ == "__main__":
    unittest.main()
