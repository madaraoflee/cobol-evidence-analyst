"""Independent tests of exceptional branch ownership and local return scopes."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import sys
import tempfile
import unittest

POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))
from exception_cfg import build_exception_cfg
from structural_index import build_structural_index


HEADER = """IDENTIFICATION DIVISION.
PROGRAM-ID. FLOWROOT.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 RESULT-AMOUNT PIC 99.
01 RESULT-STATUS PIC 99.
01 INPUT-AMOUNT PIC 99.
PROCEDURE DIVISION.
MAIN-ENTRY.
"""


def _paths(cfg):
    nodes = {node["node_id"]: node for node in cfg["nodes"]}
    outgoing = defaultdict(list)
    for edge in cfg["edges"]:
        outgoing[edge["source"]].append(edge)
    pending = [(cfg["entry_node_id"], [], [])]
    result = []
    while pending:
        current, sequence, edges = pending.pop()
        if len(sequence) > 128:
            raise AssertionError("A bounded acyclic test graph unexpectedly loops.")
        sequence = [*sequence, nodes[current]]
        if not outgoing[current]:
            result.append((sequence, edges))
        else:
            pending.extend((edge["target"], sequence, [*edges, edge]) for edge in outgoing[current])
    return result


class ExceptionCfgAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = self.root / "sources"
        self.sources.mkdir()
        self.database = self.root / "index.sqlite"

    def cfg(self, body, **budgets):
        (self.sources / "flow.cbl").write_text(HEADER + body, encoding="utf-8")
        build_structural_index(self.sources, self.database, quiet=True)
        return build_exception_cfg(self.database, "FLOWROOT", **budgets)

    def test_call_failure_and_normal_handlers_are_mutually_exclusive(self):
        cfg = self.cfg("""    CALL 'WORKERONE'
        ON EXCEPTION
            MOVE 2 TO RESULT-STATUS
        NOT ON EXCEPTION
            MOVE 3 TO RESULT-STATUS
    END-CALL.
    MOVE 4 TO RESULT-AMOUNT.
    GOBACK.
""")
        self.assertFalse(cfg["boundaries"])
        paths = _paths(cfg)
        self.assertEqual(len(paths), 2)
        for nodes, edges in paths:
            writes = [node["source"] for node in nodes if node["kind"] == "MOVE"]
            if any(edge["outcome"] == "exception" for edge in edges):
                self.assertEqual(writes, ["2", "4"])
            else:
                self.assertEqual(writes, ["3", "4"])

    def test_nested_call_handler_cannot_leak_into_outer_normal_path(self):
        cfg = self.cfg("""    CALL 'WORKERONE'
        ON EXCEPTION
            CALL 'WORKERTWO'
                ON EXCEPTION
                    MOVE 5 TO RESULT-STATUS
                NOT ON EXCEPTION
                    MOVE 6 TO RESULT-STATUS
            END-CALL
        NOT ON EXCEPTION
            MOVE 7 TO RESULT-STATUS
    END-CALL.
    GOBACK.
""")
        self.assertFalse(cfg["boundaries"])
        paths = _paths(cfg)
        self.assertEqual(len(paths), 3)
        for nodes, _ in paths:
            calls = [node["target"] for node in nodes if node["kind"] == "CALL"]
            writes = [node["source"] for node in nodes if node["kind"] == "MOVE"]
            if writes == ["7"]:
                self.assertEqual(calls, ["WORKERONE"])
            else:
                self.assertEqual(calls, ["WORKERONE", "WORKERTWO"])
                self.assertIn(writes, [["5"], ["6"]])

    def test_goback_in_failure_handler_does_not_join_normal_continuation(self):
        cfg = self.cfg("""    CALL 'WORKERONE'
        ON EXCEPTION
            GOBACK
        NOT ON EXCEPTION
            MOVE 3 TO RESULT-STATUS
    END-CALL.
    MOVE 7 TO RESULT-AMOUNT.
    GOBACK.
""")
        for nodes, edges in _paths(cfg):
            writes = [node["source"] for node in nodes if node["kind"] == "MOVE"]
            self.assertEqual(writes, [] if any(edge["outcome"] == "exception" for edge in edges)
                             else ["3", "7"])

    def test_period_inside_unfinished_scope_fails_closed(self):
        cfg = self.cfg("""    IF RESULT-STATUS = ZERO
        CALL 'WORKERONE'
            ON EXCEPTION
                MOVE ZERO TO RESULT-AMOUNT.
    MOVE 7 TO RESULT-AMOUNT.
    GOBACK.
""")
        self.assertIn("implicit_scope_termination_not_supported", cfg["summary"]["boundary_counts"])
        self.assertFalse(any(node["kind"] == "MOVE" for node in cfg["nodes"]))
        self.assertFalse(cfg["summary"]["supported_graph_closed"])

    def test_plain_exit_does_not_skip_remaining_paragraph_statements(self):
        cfg = self.cfg("""    PERFORM STEP-PARA.
    MOVE 5 TO RESULT-AMOUNT.
    GOBACK.
STEP-PARA.
    MOVE 1 TO RESULT-AMOUNT.
    EXIT.
    MOVE 2 TO RESULT-AMOUNT.
""")
        self.assertFalse(cfg["boundaries"])
        nodes, _ = _paths(cfg)[0]
        self.assertEqual([node["source"] for node in nodes if node["kind"] == "MOVE"], ["1", "2", "5"])

    def test_goback_inside_perform_exits_entire_program(self):
        cfg = self.cfg("""    PERFORM STEP-PARA.
    MOVE 5 TO RESULT-AMOUNT.
    GOBACK.
STEP-PARA.
    MOVE 1 TO RESULT-AMOUNT.
    GOBACK.
""")
        self.assertFalse(cfg["boundaries"])
        nodes, _ = _paths(cfg)[0]
        self.assertEqual([node["source"] for node in nodes if node["kind"] == "MOVE"], ["1"])
        self.assertEqual(nodes[-1]["kind"], "GOBACK")

    def test_not_only_handler_does_not_handle_call_failure(self):
        cfg = self.cfg("""    CALL 'WORKERONE'
        NOT ON EXCEPTION
            MOVE ZERO TO RESULT-AMOUNT
    END-CALL.
    GOBACK.
""")
        failure = [(nodes, edges) for nodes, edges in _paths(cfg)
                   if any(edge["outcome"] == "exception" for edge in edges)]
        self.assertEqual(len(failure), 1)
        self.assertEqual(failure[0][0][-1]["kind"], "BOUNDARY")
        self.assertFalse(any(node["kind"] == "MOVE" for node in failure[0][0]))
        self.assertEqual(failure[0][0][-1]["reason"], "unhandled_call_exception")

    def test_two_performs_keep_distinct_nested_call_occurrences(self):
        cfg = self.cfg("""    PERFORM STEP-PARA.
    PERFORM STEP-PARA.
    GOBACK.
STEP-PARA.
    CALL 'WORKERONE'
        ON EXCEPTION
            MOVE 7 TO RESULT-STATUS
    END-CALL.
""")
        calls = [node for node in cfg["nodes"] if node["kind"] == "CALL"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["statement_id"], calls[1]["statement_id"])
        self.assertNotEqual(calls[0]["instance_chain"], calls[1]["instance_chain"])
        self.assertNotEqual(calls[0]["node_id"], calls[1]["node_id"])

    def test_budget_exhaustion_never_returns_a_partial_success_branch(self):
        cfg = self.cfg("""    CALL 'WORKERONE'
        ON EXCEPTION
            MOVE ZERO TO RESULT-AMOUNT
        NOT ON EXCEPTION
            MOVE 3 TO RESULT-AMOUNT
    END-CALL.
    GOBACK.
""", max_nodes=3)
        self.assertTrue(cfg["summary"]["truncated"])
        self.assertEqual(len(cfg["nodes"]), 1)
        self.assertEqual(cfg["nodes"][0]["kind"], "BOUNDARY")
        self.assertEqual(cfg["edges"], [])


if __name__ == "__main__":
    unittest.main()
