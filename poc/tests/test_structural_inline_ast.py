"""Token-AST projection into the legacy structural schema stays conservative."""

import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from structural_index import (
    INLINE_AST_ADAPTER_VERSION, SourceDocument, _stable_id,
    build_structural_index, normalize_cobol_lines, parse_document,
)
from agent_policy import AgentPolicy
from business_index import build_business_index
from repository_discovery import ensure_repository_search
from semantic_scope import prepare_semantic_scope
from source_session import QuestionSourceSession
from error_paths import ErrorContract, audit_error_paths
from exception_cfg import build_exception_cfg


HEADER = """IDENTIFICATION DIVISION.
PROGRAM-ID. RULE-CASE.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 FLAG PIC X.
01 OTHER-FLAG PIC X.
01 RESULT-VALUE PIC 9(8).
01 INPUT-VALUE PIC 9(8).
PROCEDURE DIVISION.
MAIN.
"""


def parse_body(body, source_format="free"):
    text = HEADER + body + "\n"
    if source_format == "fixed":
        text = "\n".join(f"{number:06d} {line}" for number, line in
                         enumerate(text.splitlines(), 1)) + "\n"
    lines, hint = normalize_cobol_lines(text, source_format)
    return parse_document(SourceDocument(
        relative_path="rule.cbl", source_sha256=hashlib.sha256(text.encode()).hexdigest(),
        encoding="utf-8", used_fallback_encoding=False, format_hint=hint,
        artifact_kind="cobol_program", raw_lines=tuple(text.splitlines()), lines=lines,
    ))


def statements(parsed, name):
    return [unit for unit in parsed.code_units if unit.unit_type == "Statement" and unit.name == name]


def edges(parsed, unit, kind):
    return [edge for edge in parsed.relations
            if edge.from_entity_id == unit.unit_id and edge.relation_type == kind]


class InlineAstStructuralTests(unittest.TestCase):
    def test_both_inline_if_branches_have_exact_writes_and_source_columns(self):
        body = "IF FLAG = 'Y' MOVE 1 TO RESULT-VALUE ELSE MOVE 2 TO RESULT-VALUE END-IF."
        parsed = parse_body(body)
        moves = statements(parsed, "MOVE")
        self.assertEqual([unit.normalized_text for unit in moves],
                         ["MOVE 1 TO RESULT-VALUE", "MOVE 2 TO RESULT-VALUE"])
        self.assertEqual([unit.parse_status for unit in moves], ["complete", "complete"])
        controls = [edges(parsed, unit, "CONTROL_DEPENDS_ON")[0] for unit in moves]
        self.assertEqual([edge.metadata["outcome"] for edge in controls], ["true", "false"])
        self.assertEqual(controls[0].target_entity_id, controls[1].target_entity_id)
        for unit, value in zip(moves, ("1", "2")):
            write, = edges(parsed, unit, "WRITES")
            self.assertEqual(write.target_name, "RESULT-VALUE")
            self.assertEqual(write.metadata["expression"], value)
            self.assertEqual(write.metadata["adapter_version"], INLINE_AST_ADAPTER_VERSION)
            start, end = write.metadata["source_start_column"], write.metadata["source_end_column"]
            self.assertEqual(body[start - 1:end - 1], unit.normalized_text)
            evidence = next(item for item in parsed.evidence_spans if item.evidence_id == unit.evidence_id)
            self.assertEqual(evidence.text, body)
            self.assertEqual((unit.start_line, unit.end_line), (11, 11))
        condition = next(unit for unit in parsed.code_units if unit.unit_type == "Condition")
        self.assertEqual([edge.target_name for edge in edges(parsed, condition, "READS")], ["FLAG"])

    def test_identical_statements_on_the_same_line_get_distinct_persisted_ids(self):
        body = "IF FLAG = 'Y' MOVE 1 TO RESULT-VALUE MOVE 1 TO RESULT-VALUE END-IF."
        parsed = parse_body(body)
        moves = statements(parsed, "MOVE")
        self.assertEqual(len(moves), 2)
        self.assertNotEqual(moves[0].unit_id, moves[1].unit_id)
        self.assertEqual(moves[0].normalized_text, moves[1].normalized_text)
        self.assertEqual([unit.unit_id for unit in moves],
                         [unit.unit_id for unit in statements(parse_body(body), "MOVE")])
        writes = [edges(parsed, unit, "WRITES")[0] for unit in moves]
        self.assertLess(writes[0].metadata["source_start_column"], writes[1].metadata["source_start_column"])
        self.assertLess(writes[0].metadata["statement_order"], writes[1].metadata["statement_order"])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "rule.cbl").write_text(HEADER + body + "\n", encoding="utf-8")
            database = root / "index.sqlite"
            build_structural_index(root, database, source_format="free", quiet=True)
            with closing(sqlite3.connect(database)) as connection:
                rows = connection.execute("SELECT metadata_json FROM relations WHERE relation_type='WRITES'").fetchall()
                self.assertEqual(len(rows), 2)
                self.assertTrue(all(json.loads(row[0])["adapter_version"] == INLINE_AST_ADAPTER_VERSION for row in rows))

    def test_old_structural_index_rebuilds_once_then_reuses_unchanged_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "rule.cbl").write_text(HEADER + "IF FLAG = 'Y' MOVE 1 TO RESULT-VALUE "
                                           "ELSE MOVE 2 TO RESULT-VALUE END-IF.\n", encoding="utf-8")
            database = root / "index.sqlite"
            build_structural_index(root, database, source_format="free", quiet=True)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("UPDATE metadata SET value='source-facts-v0.4' WHERE key='parser_version'")
                connection.execute("DELETE FROM relations WHERE relation_type='WRITES'")
                connection.commit()
            refreshed = build_structural_index(root, database, source_format="free", quiet=True)
            self.assertTrue(refreshed["parser_rebuild_required"])
            self.assertEqual(refreshed["files"]["indexed_or_updated"], 1)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM relations WHERE relation_type='WRITES'").fetchone()[0], 2)
            warm = build_structural_index(root, database, source_format="free", quiet=True)
            self.assertFalse(warm["parser_rebuild_required"])
            self.assertEqual(warm["files"]["indexed_or_updated"], 0)
            self.assertEqual(warm["files"]["skipped_unchanged"], 1)

    def test_semantic_scope_version_changes_cache_key_and_then_reuses_new_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            (source / "rule.cbl").write_text(HEADER + "IF FLAG = 'Y' MOVE 1 TO RESULT-VALUE "
                                             "ELSE MOVE 2 TO RESULT-VALUE END-IF.\n", encoding="utf-8")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", verify_content=True)
            ensure_repository_search(database, source)
            arguments = {"anchors": [{"relative_path": "rule.cbl", "line": 11}], "policy": AgentPolicy()}
            with QuestionSourceSession(database, source) as session:
                with mock.patch("semantic_scope.PARSER_VERSION", "source-facts-v0.4"):
                    with prepare_semantic_scope(database, session, **arguments) as old:
                        old_key = old.scope_key
                with prepare_semantic_scope(database, session, **arguments) as refreshed:
                    self.assertNotEqual(refreshed.scope_key, old_key)
                    self.assertFalse(refreshed.cache_hit)
                with mock.patch("semantic_scope.build_structural_index", side_effect=AssertionError("warm rebuild")):
                    with prepare_semantic_scope(database, session, **arguments) as warm:
                        self.assertEqual(warm.scope_key, refreshed.scope_key)
                        self.assertTrue(warm.cache_hit)

    def test_nested_else_binds_to_its_own_condition(self):
        parsed = parse_body("IF FLAG = 'Y' IF OTHER-FLAG = 'Y' MOVE 1 TO RESULT-VALUE "
                            "ELSE MOVE 2 TO RESULT-VALUE END-IF ELSE MOVE 3 TO RESULT-VALUE END-IF.")
        moves = statements(parsed, "MOVE")
        self.assertEqual([[edge.metadata["outcome"] for edge in edges(parsed, move, "CONTROL_DEPENDS_ON")]
                          for move in moves], [["true", "true"], ["true", "false"], ["false"]])

    def test_evaluate_keeps_ordered_alternatives_and_other(self):
        parsed = parse_body("EVALUATE INPUT-VALUE WHEN 1 WHEN 2 MOVE 1 TO RESULT-VALUE "
                            "WHEN 2 THRU 3 MOVE 2 TO RESULT-VALUE WHEN OTHER MOVE 3 TO RESULT-VALUE END-EVALUATE.")
        controls = [edges(parsed, move, "CONTROL_DEPENDS_ON")[0]
                    for move in statements(parsed, "MOVE")]
        self.assertEqual([edge.metadata["branch_index"] for edge in controls], [0, 1, 2])
        self.assertEqual([edge.metadata["prior_alternatives"] for edge in controls],
                         [[], [["1", "2"]], [["1", "2"], ["2 THRU 3"]]])
        self.assertEqual([edge.metadata["is_other"] for edge in controls], [False, False, True])
        self.assertTrue(all(edge.metadata["selector"] == "INPUT-VALUE" for edge in controls))

    def test_sequential_move_compute_uses_ast_reads_and_receivers(self):
        parsed = parse_body("MOVE INPUT-VALUE TO RESULT-VALUE COMPUTE RESULT-VALUE = RESULT-VALUE + 2.")
        move, = statements(parsed, "MOVE")
        compute, = statements(parsed, "COMPUTE")
        self.assertEqual([edge.target_name for edge in edges(parsed, move, "READS")], ["INPUT-VALUE"])
        self.assertEqual([edge.target_name for edge in edges(parsed, move, "WRITES")], ["RESULT-VALUE"])
        self.assertEqual([edge.target_name for edge in edges(parsed, compute, "READS")], ["RESULT-VALUE"])
        self.assertEqual([edge.target_name for edge in edges(parsed, compute, "WRITES")], ["RESULT-VALUE"])

    def test_legacy_line_id_sort_preserves_token_order_and_sentence_period(self):
        bodies = [
            "IF FLAG = 'Y' MOVE 1 TO RESULT-VALUE ELSE MOVE 2 TO RESULT-VALUE END-IF.",
            "IF FLAG = 'Y' MOVE 1 TO RESULT-VALUE ELSE MOVE 2 TO RESULT-VALUE.",
            "MOVE 1 TO RESULT-VALUE COMPUTE RESULT-VALUE = RESULT-VALUE + 2.",
            "IF FLAG = 'Y' MOVE 1 TO RESULT-VALUE. MOVE 2 TO RESULT-VALUE.",
            "IF FLAG = 'Y' MOVE 1 TO RESULT-VALUE END-IF. MOVE 2 TO RESULT-VALUE.",
        ]
        for body in bodies:
            with self.subTest(body=body):
                parsed = parse_body(body)
                units = [unit for unit in parsed.code_units if unit.unit_type == "Statement"]
                legacy_order = sorted(units, key=lambda unit: (unit.start_line, unit.end_line, unit.unit_id))
                self.assertEqual([unit.unit_id for unit in legacy_order], [unit.unit_id for unit in units])
                reconstructed = " ".join(unit.normalized_text for unit in legacy_order)
                self.assertEqual(reconstructed, body)

    def test_identifier_suffix_never_becomes_a_scope_terminator(self):
        samples = [
            ("IF FLAG = 'Y' MOVE 1 TO FIELD-END-IF.", "END_IF"),
            ("EVALUATE FLAG WHEN 'Y' MOVE 1 TO FIELD-END-EVALUATE.", "END_EVALUATE"),
            ("IF FLAG = 'Y' IF OTHER-FLAG = 'Y' MOVE 1 TO RESULT-VALUE END-IF.", "END_IF"),
            ("EVALUATE FLAG WHEN 'Y' EVALUATE OTHER-FLAG WHEN 'Y' MOVE 1 TO RESULT-VALUE END-EVALUATE.", "END_EVALUATE"),
        ]
        for body, marker in samples:
            with self.subTest(body=body):
                parsed = parse_body(body)
                expected = 1 if "OTHER-FLAG" in body else 0
                self.assertEqual(len(statements(parsed, marker)), expected)
                units = [unit for unit in parsed.code_units if unit.unit_type == "Statement"
                         or unit.unit_type == "Condition" and unit.name == "WHEN"]
                units.sort(key=lambda unit: (unit.start_line, unit.end_line, unit.unit_id))
                self.assertEqual(" ".join(unit.normalized_text for unit in units), body)
                with tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    (root / "rule.cbl").write_text(HEADER + body + "\n", encoding="utf-8")
                    database = root / "index.sqlite"
                    build_structural_index(root, database, source_format="free", quiet=True)
                    with closing(sqlite3.connect(database)) as connection:
                        count = connection.execute("SELECT COUNT(*) FROM code_units WHERE unit_type='Statement' AND name=?", (marker,)).fetchone()[0]
                        self.assertEqual(count, expected)

    def test_exception_cfg_consumer_preserves_following_unconditional_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "rule.cbl").write_text(HEADER + "IF FLAG = 'Y' MOVE 1 TO RESULT-VALUE "
                "END-IF. MOVE 2 TO RESULT-VALUE.\nGOBACK.\n", encoding="utf-8")
            database = root / "index.sqlite"
            build_structural_index(root, database, source_format="free", quiet=True)
            graph = build_exception_cfg(database, "RULE-CASE", framework_mode=True)
            self.assertTrue(graph["summary"]["supported_graph_closed"])
            nodes = {node["node_id"]: node for node in graph["nodes"]}
            pending = [(graph["entry_node_id"], [])]
            writes_by_path = []
            while pending:
                node_id, writes = pending.pop()
                node = nodes[node_id]
                if node["kind"] == "MOVE":
                    writes = [*writes, node["source"]]
                outgoing = [edge for edge in graph["edges"] if edge["source"] == node_id]
                if not outgoing:
                    writes_by_path.append(writes)
                pending.extend((edge["target"], writes) for edge in outgoing)
            self.assertCountEqual(writes_by_path, [["1", "2"], ["2"]])

    def test_error_auditor_consumer_recovers_inline_evaluate_selector(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "rule.cbl").write_text(HEADER + "EVALUATE INPUT-VALUE WHEN 1 MOVE ZERO TO RESULT-VALUE "
                "WHEN OTHER MOVE 9 TO RESULT-VALUE END-EVALUATE.\nGOBACK.\n", encoding="utf-8")
            database = root / "index.sqlite"
            build_structural_index(root, database, source_format="free", quiet=True)
            report = audit_error_paths(database, "RULE-CASE",
                (ErrorContract("RULE-CASE", ("INPUT-VALUE",), ("RESULT-VALUE",)),))
            clears = [item for item in report["observations"] if item["kind"] == "error_output_clear"]
            self.assertEqual(len(clears), 1)
            self.assertEqual(clears[0]["controls"][0]["selector"], "INPUT-VALUE")
            self.assertIsNotNone(clears[0]["controls"][0]["evaluate_id"])
            self.assertFalse(any(item["kind"] == "possible_output_overwrite_after_error_clear"
                                 for item in report["observations"]))

    def test_inline_compound_keeps_outer_scope_until_sentence_period(self):
        parsed = parse_body("IF FLAG = 'Y'\nIF OTHER-FLAG = 'Y' MOVE 1 TO RESULT-VALUE "
                            "ELSE MOVE 2 TO RESULT-VALUE END-IF\nMOVE 3 TO RESULT-VALUE\nEND-IF.\n"
                            "MOVE 4 TO RESULT-VALUE.")
        moves = statements(parsed, "MOVE")
        self.assertEqual([[edge.metadata["outcome"] for edge in edges(parsed, move, "CONTROL_DEPENDS_ON")]
                          for move in moves], [["true", "true"], ["true", "false"], ["true"], []])

    def test_inline_period_closes_outer_scope_even_before_comment(self):
        parsed = parse_body("IF FLAG = 'Y'\nIF OTHER-FLAG = 'Y' MOVE 1 TO RESULT-VALUE END-IF. *> note\n"
                            "MOVE 2 TO RESULT-VALUE.")
        first, second = statements(parsed, "MOVE")
        self.assertEqual(len(edges(parsed, first, "CONTROL_DEPENDS_ON")), 2)
        self.assertEqual(edges(parsed, second, "CONTROL_DEPENDS_ON"), [])

    def test_unknown_partial_and_opaque_lines_are_not_promoted(self):
        samples = [
            "IF FLAG = 'Y' MOVE 1 TO RESULT-VALUE",
            "IF FLAG = 'Y' DISPLAY 'note' MOVE 1 TO RESULT-VALUE END-IF.",
            "IF FLAG = 'Y' CALL 'RULE' MOVE 1 TO RESULT-VALUE END-IF.",
            "IF FLAG = 'Y' COMPUTE RESULT-VALUE = 1 ON SIZE ERROR MOVE 2 TO RESULT-VALUE END-COMPUTE END-IF.",
        ]
        for body in samples:
            with self.subTest(body=body):
                parsed = parse_body(body)
                self.assertFalse(any("adapter_version" in edge.metadata for edge in parsed.relations))
                self.assertFalse(any(edge.relation_type == "WRITES" for edge in parsed.relations))

    def test_multiple_roots_with_unknown_outer_sentence_scope_stay_legacy(self):
        parsed = parse_body("IF FLAG = 'Y'\nMOVE 1 TO RESULT-VALUE. MOVE 2 TO RESULT-VALUE.")
        self.assertFalse(any("adapter_version" in edge.metadata for edge in parsed.relations))

    def test_unterminated_last_receiver_can_continue_on_next_line(self):
        parsed = parse_body("MOVE 1 TO RESULT-VALUE MOVE 2 TO INPUT-VALUE\nRESULT-VALUE.")
        self.assertFalse(any("adapter_version" in edge.metadata for edge in parsed.relations))

    def test_simple_legacy_statement_id_and_metadata_are_unchanged(self):
        parsed = parse_body("MOVE 1 TO RESULT-VALUE.")
        move, = statements(parsed, "MOVE")
        self.assertEqual(move.unit_id, _stable_id("unit", "rule.cbl", "Statement", "RULE-CASE", "MOVE", 11,
                                                 "MOVE 1 TO RESULT-VALUE."))
        self.assertEqual(edges(parsed, move, "WRITES")[0].metadata,
                         {"expression": "1", "operation": "MOVE"})

    def test_fixed_format_columns_refer_to_original_source(self):
        parsed = parse_body("IF FLAG = 'Y' MOVE 1 TO X ELSE MOVE 2 TO X END-IF.", "fixed")
        moves = statements(parsed, "MOVE")
        self.assertEqual(len(moves), 2)
        for move in moves:
            write, = edges(parsed, move, "WRITES")
            raw = parsed.document.raw_lines[10]
            start, end = write.metadata["source_start_column"], write.metadata["source_end_column"]
            self.assertEqual(raw[start - 1:end - 1], move.normalized_text)
            self.assertGreater(start, 7)

    def test_quoted_comment_marker_and_statement_words_are_not_syntax(self):
        parsed = parse_body("IF FLAG = 'Y' MOVE 'ELSE *> MOVE 9' TO FLAG "
                            "ELSE MOVE 'it''s END-IF' TO FLAG END-IF. *> MOVE 8 TO RESULT-VALUE")
        moves = statements(parsed, "MOVE")
        self.assertEqual(len(moves), 2)
        self.assertEqual([edges(parsed, move, "WRITES")[0].target_name for move in moves], ["FLAG", "FLAG"])


if __name__ == "__main__":
    unittest.main()
