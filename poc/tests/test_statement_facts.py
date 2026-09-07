from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from structural_index import (  # noqa: E402
    SourceDocument,
    build_structural_index,
    normalize_cobol_lines,
    parse_document,
    read_source_document,
)
from statement_facts import sentence_terminated  # noqa: E402


HEADER = """IDENTIFICATION DIVISION.
PROGRAM-ID. FACTCASE.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 WS-A PIC 9.
01 WS-B PIC 9.
01 WS-C PIC 9.
01 WS-D PIC 9.
01 OUT-AMOUNT PIC 9.
PROCEDURE DIVISION.
MAIN-PARA.
"""


def parse_source(body: str):
    text = HEADER + body
    lines, format_hint = normalize_cobol_lines(text)
    return parse_document(SourceDocument(
        relative_path="programs/factcase.cbl",
        source_sha256=hashlib.sha256(text.encode()).hexdigest(),
        encoding="utf-8", used_fallback_encoding=False,
        format_hint=format_hint, artifact_kind="cobol_program",
        raw_lines=tuple(text.splitlines()), lines=lines,
    ))


class ConditionSpanTests(unittest.TestCase):
    def test_sentence_period_closes_implicit_if_scope(self):
        parsed = parse_source("""IF WS-A = 1
MOVE 1 TO OUT-AMOUNT.
MOVE 2 TO WS-B.
""")
        moves = [unit for unit in parsed.code_units if unit.name == "MOVE"]
        controls = [edge for edge in parsed.relations
                    if edge.relation_type == "CONTROL_DEPENDS_ON"]
        self.assertEqual([edge.from_entity_id for edge in controls], [moves[0].unit_id])

    def test_period_on_inner_terminator_or_sql_closes_all_outer_scopes(self):
        terminators = [
            "IF WS-C = 3\nCONTINUE\nEND-IF.",
            "CONTINUE\nEND-EVALUATE.",
            "EXEC SQL SELECT FACTOR INTO :WS-C FROM RATE_TABLE END-EXEC.",
            "CONTINUE.",
        ]
        for terminator in terminators:
            with self.subTest(terminator=terminator):
                parsed = parse_source(
                    "IF WS-A = 1\nEVALUATE WS-B\nWHEN 2\n" + terminator
                    + "\nMOVE 2 TO OUT-AMOUNT.\n"
                )
                move = next(unit for unit in parsed.code_units if unit.name == "MOVE")
                self.assertFalse(any(edge.from_entity_id == move.unit_id
                                     and edge.relation_type == "CONTROL_DEPENDS_ON"
                                     for edge in parsed.relations))

    def test_decimal_literal_and_sql_comment_periods_do_not_close_scope(self):
        bodies = [
            "COMPUTE OUT-AMOUNT = 1.25",
            "DISPLAY 'text.'",
            "DISPLAY 'escaped '' text.'",
            "EXEC SQL SELECT FACTOR INTO :WS-C FROM RATE_TABLE END-EXEC -- comment.",
        ]
        for body in bodies:
            with self.subTest(body=body):
                parsed = parse_source(
                    "IF WS-A = 1\n" + body + "\nMOVE 2 TO OUT-AMOUNT\nEND-IF.\n"
                )
                move = next(unit for unit in parsed.code_units if unit.name == "MOVE")
                self.assertTrue(any(edge.from_entity_id == move.unit_id
                                    and edge.relation_type == "CONTROL_DEPENDS_ON"
                                    for edge in parsed.relations))
        self.assertFalse(sentence_terminated("DISPLAY 'unfinished."))
        self.assertTrue(sentence_terminated("DISPLAY 'text.'."))

    def test_real_fixture_retains_both_valid_status_exclusions(self):
        root = POC_ROOT / "fixtures" / "synthetic-insurance-v1"
        document = read_source_document(root / "programs" / "SYNP000.cbl", root)
        self.assertIsNotNone(document)
        parsed = parse_document(document)
        condition = next(unit for unit in parsed.code_units
                         if unit.unit_type == "Condition" and unit.start_line == 38)
        self.assertEqual((condition.start_line, condition.end_line), (38, 39))
        self.assertEqual(condition.parse_status, "complete")
        self.assertIn("AND IN-POLICY-STATUS NOT = 'I'", condition.normalized_text)
        evidence = next(span for span in parsed.evidence_spans
                        if span.evidence_id == condition.evidence_id)
        self.assertIn("003800", evidence.text)
        self.assertIn("003900", evidence.text)
        self.assertNotIn("004000", evidence.text)
        move = next(unit for unit in parsed.code_units
                    if unit.unit_type == "Statement" and unit.start_line == 40)
        edge = next(edge for edge in parsed.relations
                    if edge.from_entity_id == move.unit_id
                    and edge.relation_type == "CONTROL_DEPENDS_ON")
        self.assertEqual(edge.target_entity_id, condition.unit_id)
        self.assertEqual(edge.status, "confirmed")
        self.assertFalse(any(unit.name == "OTHER" and unit.start_line == 39
                             for unit in parsed.code_units))

    def test_parentheses_and_nested_true_false_branches_keep_separate_scopes(self):
        parsed = parse_source("""IF (WS-A = 1
OR WS-B = 2)
AND WS-C NOT = 3
IF WS-D = 4
MOVE 1 TO OUT-AMOUNT
ELSE
MOVE 2 TO OUT-AMOUNT
END-IF
ELSE
MOVE 3 TO OUT-AMOUNT
END-IF.
""")
        conditions = [unit for unit in parsed.code_units
                      if unit.unit_type == "Condition"]
        self.assertEqual(len(conditions), 2)
        self.assertEqual((conditions[0].start_line, conditions[0].end_line), (12, 14))
        self.assertTrue(all(unit.parse_status == "complete" for unit in conditions))
        moves = [unit for unit in parsed.code_units if unit.name == "MOVE"]
        outcomes = []
        for move in moves:
            outcomes.append([(edge.target_entity_id, edge.metadata["outcome"])
                             for edge in parsed.relations
                             if edge.from_entity_id == move.unit_id
                             and edge.relation_type == "CONTROL_DEPENDS_ON"])
        outer, inner = [unit.unit_id for unit in conditions]
        self.assertEqual(outcomes, [
            [(outer, "true"), (inner, "true")],
            [(outer, "true"), (inner, "false")],
            [(outer, "false")],
        ])

    def test_split_comparison_and_then_do_not_swallow_body(self):
        parsed = parse_source("""IF WS-A
IS NOT EQUAL TO
WS-B
THEN
MOVE 1 TO OUT-AMOUNT
END-IF.
""")
        condition = next(unit for unit in parsed.code_units
                         if unit.unit_type == "Condition")
        self.assertEqual((condition.start_line, condition.end_line), (12, 15))
        self.assertEqual(condition.parse_status, "complete")
        self.assertEqual({edge.target_name for edge in parsed.relations
                          if edge.from_entity_id == condition.unit_id
                          and edge.relation_type == "READS"}, {"WS-A", "WS-B"})
        self.assertTrue(any(unit.name == "MOVE" and unit.start_line == 16
                            for unit in parsed.code_units))

    def test_unsupported_conditions_do_not_publish_confirmed_control_facts(self):
        unsupported = [
            "IF WS-A = 1 MOVE 1 TO OUT-AMOUNT",
            "IF FUNCTION TEST(WS-A) = 1",
            "IF WS-A(1) = 2",
            "IF WS-A = 1 OR 2",
            "IF WS-A + WS-B > 1",
            "IF (WS-A = 1",
            "IF WS-A =",
        ]
        for condition_text in unsupported:
            with self.subTest(condition=condition_text):
                parsed = parse_source(condition_text + "\nMOVE 2 TO OUT-AMOUNT\nEND-IF.\n")
                condition = next(unit for unit in parsed.code_units
                                 if unit.unit_type == "Condition")
                self.assertEqual(condition.parse_status, "partial")
                self.assertFalse(any(edge.from_entity_id == condition.unit_id
                                     and edge.relation_type == "READS"
                                     for edge in parsed.relations))
                controls = [edge for edge in parsed.relations
                            if edge.relation_type == "CONTROL_DEPENDS_ON"]
                self.assertTrue(controls)
                self.assertTrue(all(edge.status == "unresolved" for edge in controls))
                self.assertTrue(all(edge.metadata["boundary"] ==
                                    "condition_syntax_not_supported" for edge in controls))

    def test_literal_statement_names_do_not_end_condition_scope(self):
        parsed = parse_source("""IF WS-A = 'END-IF MOVE'
MOVE 1 TO OUT-AMOUNT
END-IF.
""")
        condition = next(unit for unit in parsed.code_units
                         if unit.unit_type == "Condition")
        self.assertEqual(condition.parse_status, "complete")
        self.assertTrue(any(edge.relation_type == "CONTROL_DEPENDS_ON"
                            and edge.target_entity_id == condition.unit_id
                            for edge in parsed.relations))


class SqlHostAccessTests(unittest.TestCase):
    def _facts(self, body):
        parsed = parse_source(body)
        statement = next(unit for unit in parsed.code_units if unit.name == "EXEC_SQL")
        edges = [edge for edge in parsed.relations
                 if edge.from_entity_id == statement.unit_id]
        return parsed, statement, edges

    def test_select_into_and_indicator_outputs_are_distinct_from_inputs(self):
        _, statement, edges = self._facts("""EXEC SQL
SELECT :WS-A + AMOUNT, FACTOR
INTO :OUT-AMOUNT :OUT-IND, :OUT-FACTOR INDICATOR :FACTOR-IND
FROM RATE_TABLE
WHERE RATE_KEY = :WS-B AND FLAG = :WS-C
END-EXEC.
""")
        self.assertEqual(statement.parse_status, "complete")
        self.assertEqual((statement.start_line, statement.end_line), (12, 17))
        self.assertEqual({edge.target_name for edge in edges if edge.relation_type == "READS"},
                         {"WS-A", "WS-B", "WS-C"})
        self.assertEqual({edge.target_name for edge in edges if edge.relation_type == "WRITES"},
                         {"OUT-AMOUNT", "OUT-IND", "OUT-FACTOR", "FACTOR-IND"})
        for edge in edges:
            if edge.relation_type in {"READS", "WRITES"}:
                self.assertEqual(edge.metadata, {
                    "operation": "EXEC_SQL", "boundary": "sql_column_lineage_not_resolved",
                })

    def test_literals_and_sql_comments_cannot_inject_hosts_tables_or_terminators(self):
        _, statement, edges = self._facts("""EXEC SQL
SELECT 'END-EXEC :FAKE FROM FAKE_TABLE', AMOUNT
INTO :OUT-AMOUNT
FROM RATE_TABLE -- :FAKE_INPUT FROM COMMENT_TABLE END-EXEC
/* :BLOCK_INPUT FROM BLOCK_TABLE
END-EXEC */ WHERE RATE_KEY = :WS-A AND LABEL = 'it''s :NOT-A-HOST'
END-EXEC.
GOBACK.
""")
        self.assertEqual(statement.parse_status, "complete")
        self.assertEqual(statement.end_line, 18)
        self.assertEqual({(edge.relation_type, edge.target_name) for edge in edges}, {
            ("READS", "WS-A"), ("WRITES", "OUT-AMOUNT"),
            ("SELECTS_FROM", "RATE_TABLE"),
        })

    def test_single_line_sql_does_not_consume_following_cobol(self):
        parsed, statement, _ = self._facts(
            "EXEC SQL SELECT FACTOR INTO :WS-A FROM RATE_TABLE END-EXEC.\nGOBACK.\n"
        )
        self.assertEqual((statement.start_line, statement.end_line), (12, 12))
        self.assertTrue(any(unit.name == "GOBACK" and unit.start_line == 13
                            for unit in parsed.code_units))

    def test_sql_marker_inside_cobol_literal_is_not_a_sql_statement(self):
        parsed = parse_source("DISPLAY 'EXEC SQL :FAKE END-EXEC'.\nGOBACK.\n")
        self.assertFalse(any(unit.name == "EXEC_SQL" for unit in parsed.code_units))
        self.assertFalse(any(edge.target_name == "FAKE" for edge in parsed.relations))

    def test_supported_input_statements_and_fetch_directions(self):
        cases = [
            ("INSERT INTO RATE_TABLE VALUES (:WS-A, :WS-B)", {"WS-A", "WS-B"}, set()),
            ("UPDATE RATE_TABLE SET FACTOR = :WS-A WHERE RATE_KEY = :WS-B", {"WS-A", "WS-B"}, set()),
            ("DELETE FROM RATE_TABLE WHERE RATE_KEY = :WS-A", {"WS-A"}, set()),
            ("FETCH RATE_CURSOR INTO :WS-A, :WS-B", set(), {"WS-A", "WS-B"}),
            ("SELECT FACTOR FROM RATE_TABLE WHERE RATE_KEY = :WS-A", {"WS-A"}, set()),
        ]
        for sql, reads, writes in cases:
            with self.subTest(sql=sql):
                _, statement, edges = self._facts(f"EXEC SQL {sql} END-EXEC.\n")
                self.assertEqual(statement.parse_status, "complete")
                self.assertEqual({edge.target_name for edge in edges if edge.relation_type == "READS"}, reads)
                self.assertEqual({edge.target_name for edge in edges if edge.relation_type == "WRITES"}, writes)

    def test_unsupported_or_unterminated_sql_does_not_guess_host_directions(self):
        cases = [
            "EXEC SQL SELECT FACTOR INTO :WS-A FROM RATE_TABLE\nGOBACK.\n",
            "EXEC SQL SELECT FACTOR INTO :WS-A(1) FROM RATE_TABLE END-EXEC.\n",
            "EXEC SQL SELECT FACTOR INTO :WS-A.WS-B FROM RATE_TABLE END-EXEC.\n",
            "EXEC SQL SELECT FACTOR INTO OUT-AMOUNT FROM RATE_TABLE END-EXEC.\n",
            "EXEC SQL SELECT FACTOR INTO :WS-A FROM RATE_TABLE END-EXEC MOVE 1 TO WS-B\n",
            "EXEC SQL VALUES 1 INTO :WS-A END-EXEC.\n",
            "EXEC SQL EXECUTE RUN-STMT INTO :WS-A END-EXEC.\n",
            "EXEC SQL SELECT 'unterminated INTO :WS-A FROM RATE_TABLE END-EXEC.\n",
        ]
        for sql in cases:
            with self.subTest(sql=sql):
                _, statement, edges = self._facts(sql)
                self.assertEqual(statement.parse_status, "partial")
                self.assertFalse(any(edge.relation_type in {"READS", "WRITES"} for edge in edges))

    def test_sql_output_dialects_batches_and_malformed_shapes_abstain(self):
        cases = [
            "INSERT INTO RATE_TABLE VALUES (:WS-A) RETURNING FACTOR INTO :WS-B",
            "UPDATE RATE_TABLE SET FACTOR = :WS-A RETURNING FACTOR INTO :WS-B",
            "DELETE FROM RATE_TABLE WHERE RATE_KEY = :WS-A RETURNING FACTOR INTO :WS-B",
            "UPDATE RATE_TABLE SET FACTOR = :WS-A OUTPUT :WS-B",
            "SELECT FACTOR INTO :WS-A FROM RATE_TABLE; DELETE FROM RATE_TABLE WHERE RATE_KEY = :WS-B",
            "UPDATE RATE_TABLE SET FACTOR = :WS-A DELETE FROM RATE_TABLE WHERE RATE_KEY = :WS-B",
            "INSERT INTO RATE_TABLE SELECT :WS-A FROM OTHER_TABLE",
            "INSERT :WS-A",
            "INSERT INTO RATE_TABLE (:WS-A)",
            "UPDATE RATE_TABLE :WS-A",
            "UPDATE RATE_TABLE SET :WS-A",
            "DELETE :WS-A",
            "DELETE FROM RATE_TABLE INTO :WS-A",
            "SELECT INTO :WS-A FROM RATE_TABLE",
            "SELECT FACTOR INTO :WS-A FROM",
            "FETCH INTO :WS-A",
        ]
        for sql in cases:
            with self.subTest(sql=sql):
                _, statement, edges = self._facts(f"EXEC SQL {sql} END-EXEC.\n")
                self.assertEqual(statement.parse_status, "partial")
                self.assertFalse(any(edge.relation_type in {"READS", "WRITES"}
                                     for edge in edges))

    def test_sql_literal_or_comment_output_keywords_do_not_change_input_roles(self):
        _, statement, edges = self._facts("""EXEC SQL
UPDATE RATE_TABLE SET LABEL = 'RETURNING; OUTPUT', FACTOR = :WS-A
/* RETURNING :FAKE; */ WHERE RATE_KEY = :WS-B
END-EXEC.
""")
        self.assertEqual(statement.parse_status, "complete")
        self.assertEqual({edge.target_name for edge in edges if edge.relation_type == "READS"},
                         {"WS-A", "WS-B"})
        self.assertFalse(any(edge.relation_type == "WRITES" for edge in edges))

    def test_real_fixture_records_database_factor_writer_and_payment_mode_input(self):
        fixture = POC_ROOT / "fixtures" / "synthetic-insurance-v1"
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "index.sqlite"
            build_structural_index(fixture, database, quiet=True)
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                rows = connection.execute("""
                    SELECT r.relation_type, r.target_name, r.metadata_json,
                           c.start_line, c.end_line
                      FROM relations r JOIN code_units c
                        ON c.unit_id = r.from_entity_id
                     WHERE c.program_name = 'SYNP040' AND c.name = 'EXEC_SQL'
                       AND r.relation_type IN ('READS', 'WRITES')
                """).fetchall()
                facts = {(row["relation_type"], row["target_name"]): row for row in rows}
                self.assertIn(("WRITES", "WS-MODE-FACTOR"), facts)
                self.assertIn(("READS", "IN-PAYMENT-MODE"), facts)
                writer = facts[("WRITES", "WS-MODE-FACTOR")]
                self.assertEqual((writer["start_line"], writer["end_line"]), (34, 39))
                self.assertEqual(json.loads(writer["metadata_json"])["boundary"],
                                 "sql_column_lineage_not_resolved")
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
