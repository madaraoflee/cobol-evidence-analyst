from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exception_cfg import build_exception_cfg
from structural_index import build_structural_index


def source(body: str, *, declarations: str = "") -> str:
    return ("IDENTIFICATION DIVISION.\nPROGRAM-ID. ROOTPG.\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 A PIC 9(4) COMP-5.\n01 B PIC 9(4) COMP-5.\n01 OUT-VALUE PIC 9(4) COMP-5.\n"
            "01 STATUS-CODE PIC 9(4) COMP-5.\n01 TARGET-NAME PIC X(16).\n"
            + declarations + "\nPROCEDURE DIVISION.\nMAIN-ENTRY.\n" + body + "\n")


def paths(graph: dict) -> list[tuple[list[dict], list[str]]]:
    nodes = {node["node_id"]: node for node in graph["nodes"]}
    edges = {}
    for edge in graph["edges"]:
        edges.setdefault(edge["source"], []).append(edge)
    result = []
    pending = [(graph["entry_node_id"], [], [])]
    while pending:
        node_id, visited, outcomes = pending.pop()
        if node_id in [item["node_id"] for item in visited] or len(result) > 256:
            raise AssertionError("Unexpected graph cycle or path explosion.")
        visited = [*visited, nodes[node_id]]
        if node_id not in edges:
            result.append((visited, outcomes))
        for edge in edges.get(node_id, []):
            pending.append((edge["target"], visited, [*outcomes, edge["outcome"]]))
    return result


class ExceptionCFGTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        self.database = Path(self.temporary.name) / "facts.sqlite"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self, body: str, *, declarations: str = "", **budgets: int) -> dict:
        (self.root / "root.cbl").write_text(source(body, declarations=declarations), encoding="utf-8")
        build_structural_index(self.root, self.database, quiet=True)
        return build_exception_cfg(self.database, "ROOTPG", **budgets)

    def test_call_error_and_normal_handlers_are_exclusive(self) -> None:
        graph = self.build("CALL 'WORKER' USING A OUT-VALUE STATUS-CODE\n"
            "ON EXCEPTION\nMOVE 91 TO STATUS-CODE\nMOVE ZERO TO OUT-VALUE\n"
            "NOT ON EXCEPTION\nMOVE 2 TO OUT-VALUE\nEND-CALL.\nGOBACK.")
        self.assertTrue(graph["summary"]["supported_graph_closed"])
        alternatives = paths(graph)
        self.assertEqual(len(alternatives), 2)
        for nodes, outcomes in alternatives:
            writes = [node["source"] for node in nodes if node["kind"] == "MOVE"]
            self.assertEqual(writes, ["91", "ZERO"] if "exception" in outcomes else ["2"])
        call = next(node for node in graph["nodes"] if node["kind"] == "CALL")
        self.assertEqual(call["passing_parameters"], [
            {"name": "A", "mode": "REFERENCE"}, {"name": "OUT-VALUE", "mode": "REFERENCE"},
            {"name": "STATUS-CODE", "mode": "REFERENCE"}])

    def test_call_exception_is_not_nonzero_business_status(self) -> None:
        graph = self.build("CALL 'WORKER' USING STATUS-CODE\nON EXCEPTION\nMOVE 91 TO STATUS-CODE\n"
            "NOT ON EXCEPTION\nIF STATUS-CODE NOT = ZERO\nMOVE ZERO TO OUT-VALUE\nEND-IF\nEND-CALL.\nGOBACK.")
        call = next(node for node in graph["nodes"] if node["kind"] == "CALL")
        direct = [edge for edge in graph["edges"] if edge["source"] == call["node_id"]]
        node_map = {node["node_id"]: node for node in graph["nodes"]}
        self.assertEqual(node_map[next(edge["target"] for edge in direct if edge["outcome"] == "normal")]["kind"], "IF")
        self.assertEqual(len(paths(graph)), 3)

    def test_size_error_preserves_single_receiver_and_routes_handlers(self) -> None:
        graph = self.build("MOVE 7 TO OUT-VALUE.\nCOMPUTE OUT-VALUE ROUNDED = A * B\n"
            "ON SIZE ERROR\nMOVE 24 TO STATUS-CODE\nNOT ON SIZE ERROR\nMOVE ZERO TO STATUS-CODE\nEND-COMPUTE.\nGOBACK.")
        compute = next(node for node in graph["nodes"] if node["kind"] == "COMPUTE")
        self.assertTrue(compute["size_error_receiver_preserved"])
        self.assertTrue(compute["rounded"])
        self.assertEqual(compute["expression"], "A * B")
        for nodes, outcomes in paths(graph):
            status_writes = [node["source"] for node in nodes if node["kind"] == "MOVE" and node["targets"] == ["STATUS-CODE"]]
            self.assertEqual(status_writes, ["24"] if "size_error" in outcomes else ["ZERO"])

    def test_unhandled_call_and_compute_failures_stop_at_boundaries(self) -> None:
        for statement, reason, outcome in [("CALL 'WORKER' USING A.", "unhandled_call_exception", "exception"),
                                            ("COMPUTE OUT-VALUE = A + B.", "unhandled_size_error", "size_error")]:
            with self.subTest(statement=statement):
                graph = self.build(statement + "\nMOVE 8 TO OUT-VALUE.\nGOBACK.")
                self.assertIn(reason, graph["summary"]["boundary_counts"])
                for nodes, outcomes in paths(graph):
                    if outcome in outcomes:
                        self.assertEqual(nodes[-1]["kind"], "BOUNDARY")
                        self.assertFalse(any(node["kind"] == "MOVE" for node in nodes))
                if outcome == "size_error":
                    self.assertFalse(next(node for node in graph["nodes"] if node["kind"] == "COMPUTE")["size_error_receiver_preserved"])

    def test_not_only_handler_keeps_unhandled_failure_edge(self) -> None:
        graph = self.build("CALL 'WORKER' USING A\nNOT ON EXCEPTION\nMOVE 3 TO OUT-VALUE\nEND-CALL.\nGOBACK.")
        self.assertEqual(len(paths(graph)), 2)
        self.assertIn("unhandled_call_exception", graph["summary"]["boundary_counts"])

    def test_nested_calls_do_not_attach_inner_handlers_to_outer_call(self) -> None:
        graph = self.build("CALL 'OUTERPG' USING A\nON EXCEPTION\n"
            "CALL 'INNERPG' USING B\nON EXCEPTION\nMOVE 92 TO STATUS-CODE\n"
            "NOT ON EXCEPTION\nMOVE 3 TO OUT-VALUE\nEND-CALL\n"
            "NOT ON EXCEPTION\nMOVE 4 TO OUT-VALUE\nEND-CALL.\nGOBACK.")
        self.assertTrue(graph["summary"]["supported_graph_closed"])
        self.assertEqual(len(paths(graph)), 3)
        seen = set()
        for nodes, _ in paths(graph):
            seen.add(tuple(node["source"] for node in nodes if node["kind"] == "MOVE"))
        self.assertEqual(seen, {("92",), ("3",), ("4",)})

    def test_if_else_conditions_are_normalized(self) -> None:
        graph = self.build("IF A NOT = ZERO\nMOVE 1 TO OUT-VALUE\nELSE\nMOVE 2 TO OUT-VALUE\nEND-IF.\nGOBACK.")
        condition = next(node for node in graph["nodes"] if node["kind"] == "IF")
        self.assertEqual((condition["field"], condition["operator"], condition["value"]), ("A", "<>", "ZERO"))
        self.assertEqual(len(paths(graph)), 2)

    def test_handler_goback_does_not_join_the_normal_successor(self) -> None:
        graph = self.build("CALL 'WORKER' USING A\nON EXCEPTION\nGOBACK\nEND-CALL.\nMOVE 5 TO OUT-VALUE.\nGOBACK.")
        for nodes, outcomes in paths(graph):
            if "exception" in outcomes:
                self.assertEqual(nodes[-1]["kind"], "GOBACK")
                self.assertFalse(any(node["kind"] == "MOVE" for node in nodes))

    def test_bare_exit_does_not_end_program_or_paragraph_early(self) -> None:
        graph = self.build("EXIT.\nMOVE 5 TO OUT-VALUE.\nGOBACK.")
        nodes, _ = paths(graph)[0]
        self.assertEqual([node["kind"] for node in nodes], ["ENTRY", "EXIT", "MOVE", "GOBACK"])
        self.assertEqual(nodes[1]["exit_kind"], "paragraph_noop")

    def test_repeated_perform_instances_return_to_their_own_successor(self) -> None:
        graph = self.build("PERFORM RUN-STEP.\nMOVE 1 TO STATUS-CODE.\nPERFORM RUN-STEP.\n"
            "MOVE 2 TO STATUS-CODE.\nGOBACK.\nRUN-STEP.\nMOVE 7 TO OUT-VALUE.\nEXIT.")
        self.assertTrue(graph["summary"]["supported_graph_closed"])
        nodes, _ = paths(graph)[0]
        writes = [node for node in nodes if node["kind"] == "MOVE"]
        self.assertEqual([node["source"] for node in writes], ["7", "1", "7", "2"])
        self.assertEqual(writes[0]["statement_id"], writes[2]["statement_id"])
        self.assertNotEqual(writes[0]["instance_chain"], writes[2]["instance_chain"])

    def test_perform_thru_includes_entire_last_paragraph_before_return(self) -> None:
        graph = self.build("PERFORM START-STEP THRU END-STEP.\nMOVE 9 TO STATUS-CODE.\nGOBACK.\n"
            "START-STEP.\nMOVE 1 TO OUT-VALUE.\nMIDDLE-STEP.\nMOVE 2 TO OUT-VALUE.\n"
            "END-STEP.\nEXIT.\nMOVE 3 TO OUT-VALUE.\nAFTER-RANGE.\nMOVE 4 TO OUT-VALUE.")
        nodes, _ = paths(graph)[0]
        self.assertEqual([node["source"] for node in nodes if node["kind"] == "MOVE"], ["1", "2", "3", "9"])

    def test_goback_inside_perform_exits_the_whole_program(self) -> None:
        graph = self.build("PERFORM RUN-STEP.\nMOVE 9 TO OUT-VALUE.\nGOBACK.\nRUN-STEP.\nGOBACK.")
        nodes, _ = paths(graph)[0]
        self.assertEqual(nodes[-1]["kind"], "GOBACK")
        self.assertFalse(any(node["kind"] == "MOVE" for node in nodes))

    def test_nested_performs_preserve_full_local_instance_chain(self) -> None:
        graph = self.build("PERFORM WRAP-STEP.\nPERFORM WRAP-STEP.\nGOBACK.\nWRAP-STEP.\nPERFORM LEAF-STEP.\n"
            "EXIT.\nLEAF-STEP.\nMOVE 7 TO OUT-VALUE.\nEXIT.")
        writes = [node for node in graph["nodes"] if node["kind"] == "MOVE"]
        self.assertEqual(len(writes), 2)
        self.assertEqual([len(node["instance_chain"]) for node in writes], [2, 2])
        self.assertEqual(writes[0]["instance_chain"][-1], writes[1]["instance_chain"][-1])
        self.assertNotEqual(writes[0]["instance_chain"][0], writes[1]["instance_chain"][0])

    def test_recursive_and_unresolved_perform_terminate_at_boundaries(self) -> None:
        for body, reason in [
            ("PERFORM RUN-STEP.\nGOBACK.\nRUN-STEP.\nPERFORM RUN-STEP.", "recursive_or_overlapping_perform_not_expanded"),
            ("PERFORM MISSING-STEP.\nGOBACK.", "perform_range_not_uniquely_resolved"),
        ]:
            with self.subTest(reason=reason):
                graph = self.build(body)
                self.assertIn(reason, graph["summary"]["boundary_counts"])
                self.assertEqual(paths(graph)[0][0][-1]["kind"], "BOUNDARY")

    def test_unsupported_operations_and_implicit_scopes_never_disappear(self) -> None:
        cases = ["EVALUATE A\nWHEN ZERO\nMOVE ZERO TO OUT-VALUE\nEND-EVALUATE.",
                 "EXEC SQL SELECT VALUE_COL INTO :A FROM VALUE_TABLE END-EXEC.",
                 "PERFORM RUN-STEP 3 TIMES.", "GO TO END-STEP.", "EXIT PARAGRAPH.",
                 "IF A = ZERO\nMOVE ZERO TO OUT-VALUE.",
                 "CALL 'WORKER' USING A\nON EXCEPTION\nMOVE ZERO TO OUT-VALUE.",
                 "MOVE 'TEXT' TO OUT-VALUE.", "COMPUTE OUT-VALUE = FUNCTION LENGTH(A)."]
        for body in cases:
            with self.subTest(body=body):
                graph = self.build(body + "\nMOVE 8 TO STATUS-CODE.\nGOBACK.")
                self.assertTrue(graph["boundaries"])
                self.assertFalse(any(node["kind"] == "MOVE" and node["source"] == "8" for node in graph["nodes"]))

    def test_ambiguous_unqualified_fields_stop_at_boundary(self) -> None:
        graph = self.build("MOVE ZERO TO DUP-FIELD.\nGOBACK.",
            declarations="01 GROUP-A.\n05 DUP-FIELD PIC 9.\n01 GROUP-B.\n05 DUP-FIELD PIC 9.")
        self.assertTrue(graph["boundaries"])
        self.assertFalse(any(node["kind"] == "MOVE" for node in graph["nodes"]))

    def test_stop_run_and_post_exit_code_are_terminal(self) -> None:
        graph = self.build("STOP RUN.\nMOVE 9 TO OUT-VALUE.")
        self.assertEqual([node["kind"] for node in graph["nodes"]], ["ENTRY", "GOBACK"])
        self.assertEqual(graph["nodes"][-1]["exit_kind"], "stop_run")

    def test_graph_budget_never_returns_a_silently_truncated_branch(self) -> None:
        for budgets in ({"max_nodes": 1}, {"max_nodes": 3}, {"max_edges": 0}):
            with self.subTest(budgets=budgets):
                graph = self.build("IF A = ZERO\nMOVE 1 TO OUT-VALUE\nELSE\nMOVE 2 TO OUT-VALUE\nEND-IF.\nGOBACK.", **budgets)
                self.assertEqual(len(graph["nodes"]), 1)
                self.assertEqual(graph["nodes"][0]["kind"], "BOUNDARY")
                self.assertEqual(graph["edges"], [])
                self.assertTrue(graph["summary"]["truncated"])
        graph = self.build("PERFORM RUN-STEP.\nGOBACK.\nRUN-STEP.\nEXIT.", max_perform_depth=0)
        self.assertIn("perform_depth_budget_exhausted", graph["summary"]["boundary_counts"])

    def test_boolean_budgets_are_rejected(self) -> None:
        for budgets in ({"max_nodes": True}, {"max_edges": False}, {"max_perform_depth": True}):
            with self.subTest(budgets=budgets), self.assertRaises(ValueError):
                self.build("GOBACK.", **budgets)

    def test_combined_expansion_depth_is_bounded(self) -> None:
        with patch("exception_cfg.MAX_BUILD_DEPTH", 2):
            graph = self.build("IF A = ZERO\nIF B = ZERO\nMOVE 1 TO OUT-VALUE\nEND-IF\nEND-IF.\nGOBACK.")
        self.assertEqual(graph["nodes"][0]["kind"], "BOUNDARY")
        self.assertIn("combined_control_depth_budget_exhausted", graph["summary"]["boundary_counts"])

    def test_two_performs_on_one_unseparated_statement_do_not_share_an_instance_identity(self) -> None:
        graph = self.build("PERFORM RUN-STEP PERFORM RUN-STEP.\nGOBACK.\nRUN-STEP.\nMOVE 7 TO OUT-VALUE.")
        self.assertTrue(graph["boundaries"])
        writes = [node for node in graph["nodes"] if node["kind"] == "MOVE"]
        self.assertLessEqual(len(writes), 1)

    def test_stored_evidence_and_snapshot_consistency_are_checked(self) -> None:
        self.build("MOVE ZERO TO OUT-VALUE.\nGOBACK.")
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE metadata SET value = ? WHERE key = 'snapshot_id'", ("sha256:" + "b" * 64,))
        with self.assertRaises(ValueError):
            build_exception_cfg(self.database, "ROOTPG")

    def test_graph_is_deterministic_and_read_only(self) -> None:
        first = self.build("MOVE ZERO TO OUT-VALUE.\nGOBACK.")
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        second = build_exception_cfg(self.database, "ROOTPG")
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())
        self.assertFalse(first["complete"])
        self.assertFalse(first["runtime_execution_tested"])


if __name__ == "__main__":
    unittest.main()
