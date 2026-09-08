"""Independent abstract-state counterexamples over explicit small source models."""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from error_paths import ErrorContract
from exception_paths import analyze_exception_paths


OUTPUT = "RESULT-AMOUNT"
STATUS = "RESULT-STATUS"
CONTRACT = ErrorContract("FLOWROOT", (STATUS,), (OUTPUT,))
LAYOUTS = {name: {"integer_digits": digits, "scale": 0, "signed": False}
           for name, digits in ((OUTPUT, 2), (STATUS, 2), ("WIDE-STATUS", 3))}


def _node(name, kind, **details):
    return {"node_id": name, "kind": kind, "evidence_refs": [], **details}


def _edge(source, target, outcome="next"):
    return {"source": source, "target": target, "outcome": outcome}


def _fixture():
    return {
        "snapshot_id": "source-snapshot", "program_name": "FLOWROOT", "entry_node_id": "entry",
        "nodes": [
            _node("entry", "ENTRY"),
            _node("initial", "MOVE", source="9", targets=[OUTPUT]),
            _node("compute", "COMPUTE", target=OUTPUT, expression="1 / 0", rounded=False,
                  size_error_receiver_preserved=True),
            _node("failure", "MOVE", source="7", targets=[STATUS]),
            _node("normal", "MOVE", source="ZERO", targets=[OUTPUT]),
            _node("join", "JOIN"),
            _node("exit", "GOBACK"),
        ],
        "edges": [_edge("entry", "initial"), _edge("initial", "compute"),
                  _edge("compute", "failure", "size_error"),
                  _edge("compute", "normal", "normal"),
                  _edge("failure", "join"), _edge("normal", "join"), _edge("join", "exit")],
        "summary": {"truncated": False},
    }


def _get(cfg, name):
    return next(node for node in cfg["nodes"] if node["node_id"] == name)


def _before_exit(cfg, node):
    for edge in cfg["edges"]:
        if edge["target"] == "exit":
            edge["target"] = node["node_id"]
    cfg["nodes"].append(node)
    cfg["edges"].append(_edge(node["node_id"], "exit"))


def _result(cfg, effects=None, **budgets):
    return analyze_exception_paths(cfg, CONTRACT, LAYOUTS, effects or {}, **budgets)


def _event(result):
    return next(event for event in result["events"] if event["event_id"] == "compute:size_error")


def _finding(result):
    return _event(result)["outputs"][OUTPUT]["finding"]


class ExceptionPathAdversarialTests(unittest.TestCase):
    def test_handled_size_error_preserves_previous_nonzero_receiver(self):
        result = _result(_fixture())
        self.assertEqual(_finding(result), "nonzero_exit_possible_in_model")
        self.assertEqual(_event(result)["outputs"][OUTPUT]["exit_values"], {"nonzero": 1})
        witness = next(w for w in result["witnesses"] if w["event_id"] == "compute:size_error")
        self.assertEqual(witness["output_values"][OUTPUT], "9")

    def test_clearing_only_on_normal_compute_branch_does_not_cover_failure(self):
        cfg = _fixture()
        _get(cfg, "failure")["source"] = "ZERO"
        self.assertEqual(_finding(_result(cfg)), "nonzero_exit_possible_in_model")

    def test_later_nonzero_overwrite_defeats_failure_handler_clear(self):
        cfg = _fixture()
        _get(cfg, "failure").update(source="ZERO", targets=[OUTPUT, STATUS])
        _before_exit(cfg, _node("overwrite", "MOVE", source="5", targets=[OUTPUT]))
        self.assertEqual(_finding(_result(cfg)), "nonzero_exit_possible_in_model")

    def test_status_reset_does_not_erase_the_prior_exception_event(self):
        cfg = _fixture()
        _before_exit(cfg, _node("reset", "MOVE", source="ZERO", targets=[STATUS]))
        result = _result(cfg)
        self.assertEqual(_finding(result), "nonzero_exit_possible_in_model")
        self.assertEqual(_event(result)["status_values"][STATUS], ["0"])
        self.assertEqual(_event(result)["terminal_paths"], 1)

    def test_unknown_call_reference_effect_invalidates_prior_zero(self):
        cfg = _fixture()
        _get(cfg, "failure").update(source="ZERO", targets=[OUTPUT, STATUS])
        cfg["edges"] = [edge for edge in cfg["edges"] if edge["source"] != "join"]
        cfg["nodes"].extend([_node("later-call", "CALL", callsite_id="callee-call"),
                             _node("call-failure", "CONTINUE")])
        cfg["edges"].extend([_edge("join", "later-call"),
                             _edge("later-call", "exit", "normal"),
                             _edge("later-call", "call-failure", "exception"),
                             _edge("call-failure", "exit")])
        result = _result(cfg, {"callee-call": {"complete": True, "reference_fields": [OUTPUT]}})
        self.assertEqual(_finding(result), "unproven")
        self.assertIn("unknown", _event(result)["outputs"][OUTPUT]["exit_values"])

    def test_unresolved_normal_call_blocks_proof_instead_of_leaving_old_zero(self):
        cfg = _fixture()
        _get(cfg, "failure").update(source="ZERO", targets=[OUTPUT, STATUS])
        cfg["edges"] = [edge for edge in cfg["edges"] if edge["source"] != "join"]
        cfg["nodes"].extend([_node("later-call", "CALL", callsite_id="callee-call"),
                             _node("call-failure", "CONTINUE")])
        cfg["edges"].extend([_edge("join", "later-call"),
                             _edge("later-call", "exit", "normal"),
                             _edge("later-call", "call-failure", "exception"),
                             _edge("call-failure", "exit")])
        result = _result(cfg)
        self.assertEqual(_finding(result), "unproven")
        self.assertGreater(_event(result)["blocked_paths"], 0)

    def test_unsupported_statement_after_clear_stops_without_proving_exit(self):
        cfg = _fixture()
        _get(cfg, "failure").update(source="ZERO", targets=[OUTPUT, STATUS])
        cfg["edges"] = [edge for edge in cfg["edges"] if edge["source"] != "join"]
        cfg["nodes"].append(_node("unknown-write", "BOUNDARY", reason="unsupported_write"))
        cfg["edges"].append(_edge("join", "unknown-write"))
        result = _result(cfg)
        self.assertEqual(_finding(result), "unproven")
        self.assertEqual(_event(result)["terminal_paths"], 0)

    def test_out_of_range_literal_does_not_prune_potential_zero_guard_branch(self):
        cfg = _fixture()
        _get(cfg, "failure").update(source="100", targets=[STATUS])
        cfg["edges"] = [edge for edge in cfg["edges"] if edge["source"] != "join"]
        cfg["nodes"].extend([_node("guard", "IF", field=STATUS, operator="=", value="ZERO"),
                             _node("true-write", "MOVE", source="7", targets=[OUTPUT]),
                             _node("false-write", "MOVE", source="ZERO", targets=[OUTPUT])])
        cfg["edges"].extend([_edge("join", "guard"), _edge("guard", "true-write", "true"),
                             _edge("guard", "false-write", "false"), _edge("true-write", "exit"),
                             _edge("false-write", "exit")])
        result = _result(cfg)
        self.assertEqual(_finding(result), "nonzero_exit_possible_in_model")
        self.assertEqual(_event(result)["outputs"][OUTPUT]["exit_values"], {"nonzero": 1, "zero": 1})

    def test_narrowing_field_move_loses_exact_value_instead_of_asserting_nonzero(self):
        cfg = _fixture()
        _get(cfg, "failure").update(source="100", targets=["WIDE-STATUS"])
        _before_exit(cfg, _node("narrow", "MOVE", source="WIDE-STATUS", targets=[STATUS]))
        result = _result(cfg)
        self.assertEqual(_event(result)["status_values"][STATUS], [None])

    def test_missing_or_duplicate_exception_edge_is_rejected(self):
        cfg = _fixture()
        cfg["edges"] = [edge for edge in cfg["edges"] if edge["outcome"] != "size_error"]
        with self.assertRaises(ValueError):
            _result(cfg)
        cfg = _fixture()
        cfg["edges"].append(_edge("compute", "failure", "size_error"))
        with self.assertRaises(ValueError):
            _result(cfg)

    def test_budget_cutoff_cannot_upgrade_observed_zero_to_all_paths_proof(self):
        cfg = _fixture()
        _get(cfg, "failure").update(source="ZERO", targets=[OUTPUT, STATUS])
        self.assertEqual(_finding(_result(cfg)), "zero_on_all_modeled_exits")
        bounded = _result(cfg, max_states=6)
        self.assertTrue(bounded["summary"]["truncated"])
        self.assertNotEqual(_finding(bounded), "zero_on_all_modeled_exits")

    def test_model_inputs_remain_unchanged(self):
        cfg = _fixture()
        before = copy.deepcopy((cfg, LAYOUTS))
        _result(cfg)
        self.assertEqual((cfg, LAYOUTS), before)


if __name__ == "__main__":
    unittest.main()
