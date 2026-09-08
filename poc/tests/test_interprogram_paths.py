from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from error_paths import ErrorContract
from interprogram_paths import audit_interprogram_paths
from structural_index import build_structural_index


def program(name: str, fields: str, body: str, *, using: str = "", local: str = "") -> str:
    return (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
            + ("WORKING-STORAGE SECTION.\n" + local + "\n" if local else "")
            + ("LINKAGE SECTION.\n" if using else "WORKING-STORAGE SECTION.\n") + fields
            + "\nPROCEDURE DIVISION" + (" USING " + using if using else "")
            + ".\nMAIN-ENTRY.\n" + body + "\n")


ROOT_FIELDS = "01 INPUT-VAL PIC 9(4) COMP-5.\n01 RESULT-VAL PIC 9(4) COMP-5.\n01 STATUS-VAL PIC 9(4) COMP-5."
LEAF_FIELDS = "01 ARG-VAL PIC 9(4) COMP-5.\n01 OUT-VAL PIC 9(4) COMP-5.\n01 CODE-VAL PIC 9(4) COMP-5."
CONTRACTS = (ErrorContract("ROOTPG", ("STATUS-VAL",), ("RESULT-VAL",)),
             ErrorContract("LEAFPG", ("CODE-VAL",), ("OUT-VAL",)))
CALL = "CALL 'LEAFPG' USING BY CONTENT INPUT-VAL BY REFERENCE RESULT-VAL STATUS-VAL END-CALL."


class InterprogramPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        self.database = Path(self.temporary.name) / "facts.sqlite"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self, *, root_body: str | None = None, leaf_body: str | None = None,
              root_fields: str = ROOT_FIELDS, leaf_fields: str = LEAF_FIELDS) -> dict:
        if root_body is None:
            root_body = "MOVE ZERO TO STATUS-VAL.\n" + CALL + "\nGOBACK."
        if leaf_body is None:
            leaf_body = "MOVE 21 TO CODE-VAL.\nMOVE ZERO TO OUT-VAL.\nGOBACK."
        (self.root / "root.cbl").write_text(program("ROOTPG", root_fields, root_body), encoding="utf-8")
        (self.root / "leaf.cbl").write_text(program("LEAFPG", leaf_fields, leaf_body,
            using="ARG-VAL OUT-VAL CODE-VAL"), encoding="utf-8")
        return build_structural_index(self.root, self.database, quiet=True)

    def audit(self, **options: object) -> dict:
        return audit_interprogram_paths(self.database, "ROOTPG", CONTRACTS,
            call_policy=options.pop("call_policy", "normal_return_only"), **options)

    def test_return_witness_is_continuous_and_keeps_actual_parameter_values(self) -> None:
        self.build()
        report = self.audit(initial_values={"INPUT-VAL": "5"})
        witness = report["witnesses"][0]
        contexts = {context["context_id"]: context for context in report["contexts"]}
        active = [report["contexts"][0]["context_id"]]
        for item in witness["trace"]:
            self.assertIn(item["context_id"], contexts)
            self.assertEqual(item["context_id"], active[-1])
            if item["kind"] == "call_enter":
                active.append(item["callee_context_id"])
                incoming = next(value for value in item["parameters"] if value["callee_field"] == "ARG-VAL")
                self.assertEqual((incoming["value"], incoming["passing_mode"], incoming["written_back"]),
                                 ("5", "CONTENT", False))
            elif item["kind"] == "call_return":
                outgoing = next(value for value in item["parameters"] if value["callee_field"] == "CODE-VAL")
                self.assertEqual((outgoing["value"], outgoing["passing_mode"], outgoing["written_back"]),
                                 ("21", "REFERENCE", True))
                active.pop()
            elif item["kind"] == "MOVE":
                self.assertTrue(item["writes"])
                self.assertTrue(item["evidence_refs"])
        self.assertEqual(len(active), 1)
        self.assertEqual(witness["status_values"], {"STATUS-VAL": "21"})
        self.assertEqual(witness["output_values"], {"RESULT-VAL": "0"})

    def test_business_return_event_cites_status_assignment_and_exit(self) -> None:
        self.build()
        report = self.audit()
        event = next(item for item in report["events"] if item["event_kind"] == "business_error_return")
        self.assertEqual((event["program_name"], event["status_field"], event["status_value"]),
                         ("LEAFPG", "CODE-VAL", "21"))
        self.assertGreaterEqual(len(event["evidence_refs"]), 2)
        self.assertTrue(event["witness_ids"])
        self.assertIn(event["event_id"], report["root_exits"][0]["event_ids"])
        self.assertEqual(event["output_association"], "root_observations_not_causal_or_clearing_obligation")

    def test_unknown_business_status_prevents_an_all_exits_success_claim(self) -> None:
        self.build(root_body=CALL + "\nGOBACK.", leaf_body="MOVE ZERO TO OUT-VAL.\nGOBACK.")
        report = self.audit()
        self.assertTrue(report["root_exits"])
        self.assertEqual(report["root_exits"][0]["status_values"], {"STATUS-VAL": None})
        self.assertIn("callee_business_status_unknown", report["summary"]["boundary_counts"])
        self.assertFalse(report["summary"]["modeled_exploration_closed"])

    def test_default_explore_does_not_assume_call_availability(self) -> None:
        self.build()
        explored = audit_interprogram_paths(self.database, "ROOTPG", CONTRACTS)
        normal_only = self.audit()
        self.assertTrue(any(item["event_kind"] == "call_exception" for item in explored["events"]))
        self.assertIn("unhandled_call_exception", explored["summary"]["boundary_counts"])
        self.assertFalse(explored["summary"]["modeled_exploration_closed"])
        self.assertTrue(normal_only["summary"]["modeled_exploration_closed"])
        self.assertFalse(any(item["event_kind"] == "call_exception" for item in normal_only["events"]))

    def test_known_compute_inputs_do_not_expand_into_arithmetic_evaluation(self) -> None:
        self.build(leaf_body="MOVE ZERO TO CODE-VAL.\nMOVE 7 TO OUT-VAL.\n"
            "COMPUTE OUT-VAL = ARG-VAL * 2\nON SIZE ERROR\nMOVE 24 TO CODE-VAL\nEND-COMPUTE.\nGOBACK.")
        report = self.audit(initial_values={"INPUT-VAL": "5"})
        exits = {(item["output_values"]["RESULT-VAL"], item["status_values"]["STATUS-VAL"])
                 for item in report["root_exits"]}
        self.assertEqual(exits, {(None, "0"), ("7", "24")})
        self.assertTrue(any(item["event_kind"] == "size_error" for item in report["events"]))
        self.assertFalse(any(item["event_kind"] == "call_exception" for item in report["events"]))

    def test_display_limits_are_distinct_from_exploration_limits(self) -> None:
        self.build()
        with patch("interprogram_paths.MAX_ROOT_EXITS", 0):
            report = self.audit(max_witnesses=0)
        self.assertEqual(report["root_exits"], [])
        self.assertEqual(report["witnesses"], [])
        self.assertTrue(report["summary"]["root_exits_truncated"])
        self.assertTrue(report["summary"]["witnesses_truncated"])
        self.assertFalse(report["summary"]["truncated"])
        self.assertEqual(report["summary"]["modeled_root_exits"], 1)
        self.assertEqual(report["events"][0]["outputs"]["RESULT-VAL"]["finding"], "zero_on_all_modeled_exits")

    def test_event_and_context_parameter_budgets_fail_closed(self) -> None:
        self.build()
        with patch("interprogram_paths.MAX_EVENTS", 0):
            event_limited = self.audit()
        self.assertTrue(event_limited["summary"]["truncated"])
        self.assertEqual(event_limited["root_exits"], [])
        with patch("interprogram_paths.MAX_CONTEXT_BINDINGS", 1):
            binding_limited = self.audit()
        self.assertTrue(binding_limited["summary"]["truncated"])
        self.assertEqual(binding_limited["root_exits"], [])

    def test_unbound_linkage_cannot_be_used_as_a_call_actual(self) -> None:
        self.build(leaf_fields=LEAF_FIELDS + "\n01 UNBOUND-VAL PIC 9(4) COMP-5.",
            leaf_body="CALL 'OTHERPG' USING UNBOUND-VAL END-CALL.\nGOBACK.")
        other_fields = "01 OTHER-STATUS PIC 9(4) COMP-5."
        (self.root / "other.cbl").write_text(program("OTHERPG", other_fields,
            "MOVE 21 TO OTHER-STATUS.\nGOBACK.", using="OTHER-STATUS"), encoding="utf-8")
        build_structural_index(self.root, self.database, quiet=True)
        report = audit_interprogram_paths(self.database, "ROOTPG", (*CONTRACTS,
            ErrorContract("OTHERPG", ("OTHER-STATUS",), ("OTHER-STATUS",))), call_policy="normal_return_only")
        self.assertEqual(report["root_exits"], [])
        self.assertIn("unbound_linkage_argument_not_supported", report["summary"]["boundary_counts"])

    def test_root_unbound_linkage_is_neither_local_storage_nor_a_valid_call_actual(self) -> None:
        fields = ROOT_FIELDS + "\nLINKAGE SECTION.\n01 UNBOUND-VAL PIC 9(4) COMP-5."
        for body in ("MOVE ZERO TO UNBOUND-VAL.\nMOVE UNBOUND-VAL TO RESULT-VAL.\nGOBACK.",
                     CALL.replace("BY CONTENT INPUT-VAL", "BY CONTENT UNBOUND-VAL") + "\nGOBACK."):
            with self.subTest(body=body):
                self.build(root_fields=fields, root_body=body)
                report = self.audit()
                self.assertTrue(report["boundaries"])
                self.assertEqual(report["root_exits"], [])

    def test_zero_call_depth_is_an_explicit_boundary(self) -> None:
        self.build()
        report = self.audit(max_depth=0)
        self.assertEqual(report["root_exits"], [])
        self.assertTrue(report["summary"]["truncated"])
        self.assertIn("call_depth_budget_exhausted", report["summary"]["boundary_counts"])

    def test_inputs_and_budgets_are_strict_and_numeric(self) -> None:
        self.build()
        for options in ({"initial_values": {"INPUT-VAL": True}}, {"initial_values": {"INPUT-VAL": 0.1}},
                        {"initial_values": {"INPUT-VAL": "10000"}}, {"initial_values": {"NOT-A-FIELD": "0"}},
                        {"max_states": True}, {"max_contexts": False}, {"max_witnesses": -1},
                        {"call_policy": "assume_success"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.audit(**options)

    def test_read_only_deterministic_and_no_runtime_proof_claim(self) -> None:
        self.build()
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        first, second = self.audit(), self.audit()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())
        for flag in ("complete", "full_control_flow_proven", "runtime_execution_tested"):
            self.assertFalse(first[flag])


if __name__ == "__main__":
    unittest.main()
