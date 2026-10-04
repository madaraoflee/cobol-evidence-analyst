"""Large legal multiline statements must stay cancellable and scan once."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from statement_facts import sql_code_only
from structural_index import _SQLCodeScanner, parse_document, read_source_document


class StructuralParserLatencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "rule.cbl"

    def document(self, data="01 RESULT-VALUE PIC 9(8).", body="GOBACK."):
        self.path.write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. BILL-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n" + data +
            "\nPROCEDURE DIVISION.\nMAIN.\n" + body + "\n", encoding="utf-8")
        return read_source_document(self.path, self.root, encoding="utf-8", source_format="free")

    def assert_cancelled_within_first_chunk(self, document):
        positions = []
        failure = RuntimeError("LOCAL_BUDGET_EXHAUSTED")

        def stop(position, total):
            positions.append(position)
            if position >= 256:
                raise failure

        with self.assertRaises(RuntimeError) as caught:
            parse_document(document, progress=stop)
        self.assertIs(caught.exception, failure)
        self.assertEqual(positions[0], 0)
        self.assertLessEqual(positions[-1], 257)
        self.assertLess(positions[-1], len(document.lines) // 10)

    def test_two_line_declarations_cannot_skip_progress_checkpoints(self):
        data = "\n".join(f"01 AMOUNT-{i}\nPIC 9(8)." for i in range(12500))
        self.assert_cancelled_within_first_chunk(self.document(data=data))

    def test_one_long_sql_statement_is_cancellable_inside_collection(self):
        body = "EXEC SQL\nSELECT AMOUNT\n" + "+ AMOUNT\n" * 5000 + \
               "INTO :RESULT-VALUE\nFROM RATE_TABLE\nEND-EXEC.\nGOBACK."
        self.assert_cancelled_within_first_chunk(self.document(body=body))

    def test_one_long_condition_is_cancellable_inside_collection(self):
        body = "IF RESULT-VALUE = 1\n" + "AND RESULT-VALUE = 1\n" * 5000 + \
               "MOVE 1 TO RESULT-VALUE\nEND-IF.\nGOBACK."
        self.assert_cancelled_within_first_chunk(self.document(body=body))

    def test_one_long_continuation_is_cancellable_inside_collection(self):
        body = "COMPUTE RESULT-VALUE = 1\n" + "+ 1\n" * 5000 + ".\nGOBACK."
        self.assert_cancelled_within_first_chunk(self.document(body=body))

    def test_sql_scan_work_is_linear_and_keeps_output_and_table_relations(self):
        body = "EXEC SQL\nSELECT AMOUNT\n" + "+ AMOUNT\n" * 5000 + \
               "INTO :RESULT-VALUE\nFROM RATE_TABLE\nEND-EXEC.\nGOBACK."
        document = self.document(body=body)
        chunks = []
        original = _SQLCodeScanner.feed

        def counted(scanner, text, *args, **kwargs):
            chunks.append(text)
            return original(scanner, text, *args, **kwargs)

        with mock.patch.object(_SQLCodeScanner, "feed", new=counted):
            parsed = parse_document(document)
        statement = next(unit for unit in parsed.code_units if unit.name == "EXEC_SQL")
        self.assertEqual(statement.parse_status, "complete")
        self.assertEqual(statement.normalized_text, "\n".join(body.splitlines()[:-1]))
        self.assertEqual(sum(map(len, chunks)), len(statement.normalized_text) - statement.normalized_text.count("\n"))
        self.assertEqual(len(chunks), 5005)
        edges = {(edge.relation_type, edge.target_name) for edge in parsed.relations
                 if edge.from_entity_id == statement.unit_id}
        self.assertIn(("WRITES", "RESULT-VALUE"), edges)
        self.assertIn(("SELECTS_FROM", "RATE_TABLE"), edges)

    def test_incremental_mask_matches_complete_sql_lexer(self):
        samples = [
            "EXEC SQL SELECT 'END-EXEC' INTO :RESULT-VALUE FROM RATE_TABLE END-EXEC.",
            "exec sql\nselect 'a''END-EXEC\nb' into :RESULT-VALUE\nfrom RATE_TABLE\nend-exec.",
            'EXEC SQL SELECT "a""END-EXEC\nb" INTO :RESULT-VALUE FROM RATE_TABLE\nEND-EXEC.',
            "EXEC SQL\nSELECT AMOUNT -- END-EXEC fake\nINTO :RESULT-VALUE\n"
                "FROM RATE_TABLE /* END-EXEC\n:FAKE */ END-EXEC.",
            "EXEC SQL SELECT AMOUNT INTO :RESULT-VALUE FROM RATE_TABLE END-EXEC /*\ncomment */.",
            "EXEC SQL SELECT 'unterminated\nEND-EXEC.",
            "EXEC SQL SELECT AMOUNT /* unterminated\nEND-EXEC.",
        ]
        for text in samples:
            with self.subTest(text=text):
                scanner = _SQLCodeScanner()
                code = "\n".join(scanner.feed(line) for line in text.split("\n"))
                actual = (code if scanner.mode is None else "", scanner.mode is None)
                self.assertEqual(actual, sql_code_only(text))

    def test_terminator_before_multiline_comment_keeps_the_complete_sql_span(self):
        body = "exec sql select AMOUNT into :RESULT-VALUE from RATE_TABLE end-exec /*\n"
        body += "END-EXEC stays a comment\ncomment */.\nGOBACK."
        parsed = parse_document(self.document(body=body))
        statement = next(unit for unit in parsed.code_units if unit.name == "EXEC_SQL")
        self.assertEqual(statement.parse_status, "complete")
        self.assertIn("comment */.", statement.normalized_text)
        self.assertEqual(statement.end_line - statement.start_line, 2)

    def test_final_checkpoint_runs_even_for_a_short_program(self):
        document = self.document()
        positions = []
        parse_document(document, progress=lambda position, total: positions.append((position, total)))
        self.assertEqual(positions[0], (0, len(document.lines)))
        self.assertEqual(positions[-1], (len(document.lines), len(document.lines)))


if __name__ == "__main__":
    unittest.main()
