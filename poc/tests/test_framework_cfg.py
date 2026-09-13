from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exception_cfg import CFG_VERSION, FRAMEWORK_CFG_VERSION, build_exception_cfg
from structural_index import build_structural_index


def source(body: str) -> str:
    return ("IDENTIFICATION DIVISION.\nPROGRAM-ID. FLOWROOT.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 ACCESS-STATUS PIC X(4).\n01 RECORD-DECISION PIC X(4).\n"
            "01 END-FLAG PIC X.\n01 ITEM-COUNT PIC 9(4) COMP-5.\n"
            "01 RESULT-AREA.\n05 RESULT-FLAG PIC X.\n"
            "PROCEDURE DIVISION.\n" + body + "\n")


def acyclic_paths(graph: dict) -> list[list[dict]]:
    nodes = {node["node_id"]: node for node in graph["nodes"]}
    outgoing: dict[str, list[str]] = {}
    for edge in graph["edges"]:
        outgoing.setdefault(edge["source"], []).append(edge["target"])
    pending = [(graph["entry_node_id"], [])]
    result = []
    while pending:
        node_id, path = pending.pop()
        if node_id in [item["node_id"] for item in path] or len(result) > 128:
            raise AssertionError("Unexpected cycle or excessive test paths.")
        path = [*path, nodes[node_id]]
        if node_id not in outgoing:
            result.append(path)
        for target in outgoing.get(node_id, []):
            pending.append((target, path))
    return result


class FrameworkCFGTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        self.database = Path(self.temporary.name) / "facts.sqlite"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self, body: str, **options: object) -> dict:
        (self.root / "root.cbl").write_text(source(body), encoding="utf-8")
        build_structural_index(self.root, self.database, quiet=True)
        return build_exception_cfg(self.database, "FLOWROOT", framework_mode=True, **options)

    def assert_closed(self, graph: dict) -> None:
        self.assertTrue(graph["summary"]["supported_graph_closed"], graph["boundaries"])

    def test_numbered_section_executes_its_paragraphs_but_not_next_section(self) -> None:
        graph = self.build("MAIN-ENTRY.\nPERFORM 1000-INITIALISE.\nGOBACK.\n"
            "1000-INITIALISE SECTION.\nMOVE 1 TO ITEM-COUNT.\n"
            "1010-FIRST.\nMOVE 2 TO ITEM-COUNT.\n1020-LAST.\nMOVE 3 TO ITEM-COUNT.\n"
            "2000-READ SECTION.\nMOVE 4 TO ITEM-COUNT.")
        self.assert_closed(graph)
        moves = [node["source"] for node in acyclic_paths(graph)[0] if node["kind"] == "MOVE"]
        self.assertEqual(moves, ["1", "2", "3"])
        entry = next(node for node in graph["nodes"] if node.get("role") == "perform_entry")
        self.assertEqual((entry["target_kind"], entry["end_kind"]), ("Section", "Section"))

    def test_plain_paragraph_perform_does_not_consume_sibling_paragraphs(self) -> None:
        graph = self.build("MAIN-ENTRY.\nPERFORM 1010-FIRST.\nGOBACK.\n"
            "1000-WORK SECTION.\nMOVE 1 TO ITEM-COUNT.\n"
            "1010-FIRST.\nMOVE 2 TO ITEM-COUNT.\n1020-LAST.\nMOVE 3 TO ITEM-COUNT.")
        self.assert_closed(graph)
        self.assertEqual([node["source"] for node in acyclic_paths(graph)[0] if node["kind"] == "MOVE"], ["2"])

    def test_section_through_section_includes_last_section_paragraphs(self) -> None:
        graph = self.build("MAIN-ENTRY.\nPERFORM 1000-FIRST THRU 2000-LAST.\nGOBACK.\n"
            "1000-FIRST SECTION.\nMOVE 1 TO ITEM-COUNT.\n1010-PART.\nMOVE 2 TO ITEM-COUNT.\n"
            "2000-LAST SECTION.\nMOVE 3 TO ITEM-COUNT.\n2010-PART.\nMOVE 4 TO ITEM-COUNT.\n"
            "3000-AFTER SECTION.\nMOVE 5 TO ITEM-COUNT.")
        self.assert_closed(graph)
        self.assertEqual([node["source"] for node in acyclic_paths(graph)[0] if node["kind"] == "MOVE"], ["1", "2", "3", "4"])

    def test_repeated_sections_keep_distinct_perform_instances(self) -> None:
        graph = self.build("MAIN-ENTRY.\nPERFORM 1000-WORK.\nPERFORM 1000-WORK.\nGOBACK.\n"
            "1000-WORK SECTION.\n1010-PART.\nMOVE 1 TO ITEM-COUNT.")
        self.assert_closed(graph)
        writes = [node for node in graph["nodes"] if node["kind"] == "MOVE"]
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0]["statement_id"], writes[1]["statement_id"])
        self.assertNotEqual(writes[0]["instance_chain"], writes[1]["instance_chain"])
        self.assertEqual(writes[0]["evidence_refs"], writes[1]["evidence_refs"])

    def test_entry_can_fall_through_section_without_calling_it(self) -> None:
        graph = self.build("1000-FIRST SECTION.\n1010-PART.\nMOVE 1 TO ITEM-COUNT.\n"
            "2000-LAST SECTION.\nMOVE 2 TO ITEM-COUNT.\nGOBACK.")
        self.assert_closed(graph)
        self.assertEqual([node["source"] for node in acyclic_paths(graph)[0] if node["kind"] == "MOVE"], ["1", "2"])

    def test_nested_text_conditions_keep_io_and_record_decision_separate(self) -> None:
        graph = self.build("MAIN-ENTRY.\nMOVE SPACES TO RECORD-DECISION.\n"
            "IF ACCESS-STATUS = 'GOOD'\nIF RECORD-DECISION IS NOT EQUAL TO SPACES\n"
            "MOVE 'Y' TO RESULT-FLAG\nELSE\nMOVE 'N' TO RESULT-FLAG\nEND-IF\n"
            "ELSE\nMOVE 'E' TO RESULT-FLAG\nEND-IF.\nGOBACK.")
        self.assert_closed(graph)
        conditions = [node for node in graph["nodes"] if node["kind"] == "IF"]
        self.assertEqual([(node["field"], node["operator"], node["value"]) for node in conditions],
                         [("ACCESS-STATUS", "=", "'GOOD'"), ("RECORD-DECISION", "<>", "SPACES")])
        self.assertEqual(len(acyclic_paths(graph)), 3)

    def test_quoted_scalar_escaping_and_spaces_are_preserved_lexically(self) -> None:
        graph = self.build('MAIN-ENTRY.\nMOVE "A""B" TO ACCESS-STATUS.\n'
                           "MOVE 'C''D' TO RECORD-DECISION.\nMOVE SPACE TO RESULT-AREA.\nGOBACK.")
        self.assert_closed(graph)
        self.assertEqual([node["source"] for node in graph["nodes"] if node["kind"] == "MOVE"],
                         ['"A""B"', "'C''D'", "SPACE"])

    def test_inline_until_is_a_test_before_cycle_with_true_exit(self) -> None:
        graph = self.build("MAIN-ENTRY.\nPERFORM UNTIL END-FLAG = 'Y'\n"
                           "MOVE 'Y' TO END-FLAG\nEND-PERFORM.\nGOBACK.")
        self.assert_closed(graph)
        loop = next(node for node in graph["nodes"] if node["kind"] == "LOOP_TEST")
        self.assertEqual((loop["field"], loop["operator"], loop["value"], loop["test_position"]),
                         ("END-FLAG", "=", "'Y'", "before"))
        self.assertEqual(loop["loop_id"], loop["node_id"])
        nodes = {node["node_id"]: node for node in graph["nodes"]}
        outgoing = {edge["outcome"]: nodes[edge["target"]] for edge in graph["edges"] if edge["source"] == loop["node_id"]}
        self.assertEqual(outgoing["false"]["kind"], "MOVE")
        self.assertEqual(outgoing["true"]["role"], "loop_exit")
        backedges = [edge for edge in graph["edges"] if edge["outcome"] == "loop_back"]
        self.assertEqual(len(backedges), 1)
        self.assertEqual(backedges[0]["target"], loop["node_id"])

    def test_explicit_test_before_and_nested_conditions_are_supported(self) -> None:
        graph = self.build("MAIN-ENTRY.\nPERFORM WITH TEST BEFORE UNTIL END-FLAG = 'Y'\n"
                           "IF ACCESS-STATUS = 'DONE'\nMOVE 'Y' TO END-FLAG\n"
                           "ELSE\nMOVE SPACE TO RECORD-DECISION\nEND-IF\nEND-PERFORM.\nGOBACK.")
        self.assert_closed(graph)
        self.assertEqual(graph["summary"]["nodes_by_kind"]["LOOP_TEST"], 1)
        self.assertEqual(graph["summary"]["nodes_by_kind"]["IF"], 1)

    def test_loop_perform_instances_are_not_unrolled_or_merged(self) -> None:
        graph = self.build("MAIN-ENTRY.\nPERFORM 1000-WORK.\nPERFORM 1000-WORK.\nGOBACK.\n"
            "1000-WORK SECTION.\nPERFORM UNTIL END-FLAG = 'Y'\n"
            "PERFORM 2000-READ\nEND-PERFORM.\n2000-READ SECTION.\nMOVE 'Y' TO END-FLAG.")
        self.assert_closed(graph)
        loops = [node for node in graph["nodes"] if node["kind"] == "LOOP_TEST"]
        writes = [node for node in graph["nodes"] if node["kind"] == "MOVE"]
        self.assertEqual(len(loops), 2)
        self.assertEqual(len(writes), 2)
        self.assertNotEqual(loops[0]["loop_id"], loops[1]["loop_id"])
        self.assertTrue(all(len(node["instance_chain"]) == 2 for node in writes))

    def test_goback_in_loop_body_does_not_get_a_backedge(self) -> None:
        graph = self.build("MAIN-ENTRY.\nPERFORM UNTIL END-FLAG = 'Y'\nGOBACK\nEND-PERFORM.\nGOBACK.")
        self.assert_closed(graph)
        self.assertFalse(any(edge["outcome"] == "loop_back" for edge in graph["edges"]))
        exits = {node["node_id"] for node in graph["nodes"] if node["kind"] == "GOBACK"}
        self.assertFalse(any(edge["source"] in exits for edge in graph["edges"]))

    def test_unsupported_loop_and_scalar_forms_stop_before_later_effects(self) -> None:
        cases = [
            "PERFORM WITH TEST AFTER UNTIL END-FLAG = 'Y'\nMOVE 'Y' TO END-FLAG\nEND-PERFORM.",
            "PERFORM VARYING ITEM-COUNT FROM 1 BY 1 UNTIL ITEM-COUNT > 3\nCONTINUE\nEND-PERFORM.",
            "PERFORM 1000-WORK UNTIL END-FLAG = 'Y'.",
            "PERFORM UNTIL END-FLAG = 'Y'\nMOVE 'Y' TO END-FLAG.",
            "PERFORM UNTIL END-FLAG = 'Y' OR ACCESS-STATUS = 'DONE'\nCONTINUE\nEND-PERFORM.",
            "MOVE + 'Y' TO END-FLAG.",
            "MOVE HIGH-VALUES TO ACCESS-STATUS.",
            "IF END-FLAG = 'Y'\nMOVE SPACE TO RECORD-DECISION.",
        ]
        for body in cases:
            with self.subTest(body=body):
                graph = self.build("MAIN-ENTRY.\n" + body + "\nMOVE 99 TO ITEM-COUNT.\nGOBACK.")
                self.assertTrue(graph["boundaries"])
                self.assertFalse(any(node["kind"] == "MOVE" and node["source"] == "99" for node in graph["nodes"]))

    def test_ambiguous_and_recursive_sections_remain_boundaries(self) -> None:
        cases = [
            ("PERFORM 1000-WORK.\nGOBACK.\n1000-WORK SECTION.\nCONTINUE.\n1000-WORK SECTION.\nCONTINUE.",
             "perform_range_not_uniquely_resolved"),
            ("PERFORM 1000-WORK.\nGOBACK.\n1000-WORK SECTION.\nPERFORM 1000-WORK.",
             "recursive_or_overlapping_perform_not_expanded"),
        ]
        for body, reason in cases:
            with self.subTest(reason=reason):
                graph = self.build("MAIN-ENTRY.\n" + body)
                self.assertIn(reason, graph["summary"]["boundary_counts"])

    def test_default_numeric_mode_is_unchanged_and_framework_is_read_only(self) -> None:
        graph = self.build("MAIN-ENTRY.\nMOVE SPACE TO ACCESS-STATUS.\nGOBACK.")
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        default = build_exception_cfg(self.database, "FLOWROOT")
        explicit = build_exception_cfg(self.database, "FLOWROOT", framework_mode=False)
        repeated = build_exception_cfg(self.database, "FLOWROOT", framework_mode=True)
        self.assertEqual(default, explicit)
        self.assertTrue(default["boundaries"])
        self.assertEqual(default["cfg_version"], CFG_VERSION)
        self.assertEqual(default["cfg_mode"], "numeric_exception")
        self.assertEqual(graph, repeated)
        self.assertEqual(graph["cfg_version"], FRAMEWORK_CFG_VERSION)
        self.assertEqual(graph["cfg_mode"], "framework_control")
        self.assertFalse(graph["complete"])
        self.assertFalse(graph["runtime_execution_tested"])
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())

    def test_framework_mode_requires_boolean_and_loop_graph_obeys_budgets(self) -> None:
        self.build("MAIN-ENTRY.\nGOBACK.")
        for mode in (0, 1, None, "true"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                build_exception_cfg(self.database, "FLOWROOT", framework_mode=mode)
        graph = self.build("MAIN-ENTRY.\nPERFORM UNTIL END-FLAG = 'Y'\n"
                           "MOVE 'Y' TO END-FLAG\nEND-PERFORM.\nGOBACK.", max_nodes=3)
        self.assertEqual(len(graph["nodes"]), 1)
        self.assertEqual(graph["nodes"][0]["kind"], "BOUNDARY")
        self.assertTrue(graph["summary"]["truncated"])


if __name__ == "__main__":
    unittest.main()
