"""Shared physical preprocessing and statement boundaries for I/O arithmetic."""

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
from claim_support import supplied_statement_facts
from repo_inventory import normalize_cobol_text
from statement_facts import assignment_facts, clean_cobol_line, io_clause_prefix
from structural_index import normalize_cobol_lines


HEADER = """IDENTIFICATION DIVISION.
PROGRAM-ID. BOUNDARY-FLOW.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 RAW-AMOUNT PIC 9(7)V99.
01 OUT-AMOUNT PIC 9(7)V99.
01 RESULT-CODE PIC 99.
PROCEDURE DIVISION.
MAIN.
"""


def page(body, source_format="free"):
    source = HEADER + body
    if source_format == "fixed":
        source = "\n".join("       " + line for line in source.splitlines()) + "\n"
    return {"evidence_id": "ev_boundary", "relative_path": "boundary.cbl", "start_line": 1,
            "end_line": len(source.splitlines()), "source_text": source,
            "source_sha256": hashlib.sha256(source.encode()).hexdigest()}


class SharedIOBoundaryTests(unittest.TestCase):
    def test_normal_io_clause_headers_do_not_extend_compute_occurrences(self):
        supplied = page("READ INPUT-FILE\nINVALID KEY\nCOMPUTE OUT-AMOUNT = RAW-AMOUNT * 2\n"
                        "NOT INVALID KEY\nCOMPUTE OUT-AMOUNT = RAW-AMOUNT * 3\nEND-READ.\n")
        facts = [fact for fact in supplied_statement_facts(supplied, source_format="free")
                 if fact["statement_kind"] == "COMPUTE"]
        self.assertEqual([fact["expression"] for fact in facts], [["RAW-AMOUNT", "*", "2"], ["RAW-AMOUNT", "*", "3"]])
        lines = supplied["source_text"].splitlines()
        for fact in facts:
            self.assertEqual(fact["start_line"], fact["end_line"])
            self.assertEqual(fact["source_text"], lines[fact["start_line"] - 1])
            self.assertNotIn("INVALID KEY", fact["source_text"])
            self.assertIsNone(fact["normal_result_condition"])
            self.assertFalse(fact["exception_effects_unverified"])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "boundary.cbl").write_text(supplied["source_text"])
            database = root / "index.sqlite"
            build_business_index(root, database, source_format="free")
            with closing(sqlite3.connect(database)) as db:
                rules = db.execute("SELECT first_line,last_line,normalized_text FROM business_rules "
                                   "WHERE rule_kind='COMPUTE' ORDER BY first_line").fetchall()
            self.assertEqual(len(rules), 2)
            self.assertTrue(all(first == last and "INVALID KEY" not in code for first, last, code in rules))

    def test_free_leading_multiplication_is_preserved_by_all_normalizers_and_facts(self):
        supplied = page("COMPUTE OUT-AMOUNT = RAW-AMOUNT\n* 2\nEND-COMPUTE.\n")
        self.assertEqual(clean_cobol_line("* 2", "free")[0], "* 2")
        self.assertIn("* 2", "\n".join(line.text for line in normalize_cobol_lines(supplied["source_text"], "free")[0]))
        self.assertIn("* 2", normalize_cobol_text(supplied["source_text"], "free")[0])
        facts = [fact for fact in supplied_statement_facts(supplied, source_format="free")
                 if fact["statement_kind"] == "COMPUTE"]
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["expression"], ["RAW-AMOUNT", "*", "2"])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "boundary.cbl").write_text(supplied["source_text"])
            database = root / "index.sqlite"
            build_business_index(root, database, source_format="free")
            with closing(sqlite3.connect(database)) as db:
                expression = db.execute("SELECT normalized_text FROM business_rules WHERE rule_kind='COMPUTE'").fetchone()[0]
            self.assertIn("RAW-AMOUNT * 2", expression)

    def test_fixed_indicator_comments_and_free_inline_comments_keep_their_roles(self):
        self.assertEqual(clean_cobol_line("000100*not executable", "fixed")[0], "")
        self.assertEqual(normalize_cobol_lines("000100*not executable", "fixed")[0], ())
        self.assertEqual(normalize_cobol_text("000100*not executable", "fixed")[0], "")
        literal = "DISPLAY 'CALL *> PERFORM' *> actual comment"
        for clean in (clean_cobol_line(literal, "free")[0],
                      normalize_cobol_lines(literal, "free")[0][0].text,
                      normalize_cobol_text(literal, "free")[0]):
            self.assertIn("'CALL *> PERFORM'", clean)
            self.assertNotIn("actual comment", clean)

    def test_only_actual_arithmetic_size_error_has_a_normal_result_contract(self):
        supported = page("COMPUTE OUT-AMOUNT = RAW-AMOUNT * 2\nON SIZE ERROR\n"
                         "MOVE 12 TO RESULT-CODE\nNOT ON SIZE ERROR\nCONTINUE\nEND-COMPUTE.\n")
        compute = next(fact for fact in supplied_statement_facts(supported, source_format="free")
                       if fact["statement_kind"] == "COMPUTE")
        self.assertEqual(compute["normal_result_condition"], "no_size_error")
        for statement in ("COMPUTE OUT-AMOUNT = RAW-AMOUNT * 2 NOT INVALID KEY",
                          "COMPUTE OUT-AMOUNT = RAW-AMOUNT * 2 AT END",
                          "MOVE RAW-AMOUNT TO OUT-AMOUNT ON SIZE ERROR"):
            facts = assignment_facts(statement)
            self.assertTrue(facts.effects_unknown)
            self.assertNotIn("arithmetic_size_error_clause", facts.diagnostics)
            supplied = page(statement + ".\n")
            self.assertFalse(any(fact.get("normal_result_condition") == "no_size_error"
                                 for fact in supplied_statement_facts(supplied, source_format="free")))

    def test_io_prefix_is_lexical_and_does_not_cut_arithmetic_handlers(self):
        for source in ("INVALID KEY", "NOT INVALID KEY", "AT END", "NOT AT END"):
            self.assertEqual(io_clause_prefix(source), source)
        for source in ("NOT ON SIZE ERROR", "ON SIZE ERROR", "'NOT INVALID KEY'", "DISPLAY 'AT END'", "NOT"):
            self.assertIsNone(io_clause_prefix(source))

    def test_split_io_marker_is_conservative_and_cannot_publish_truncated_formula(self):
        supplied = page("READ INPUT-FILE\nINVALID KEY\nCOMPUTE OUT-AMOUNT = RAW-AMOUNT\n"
                        "* 2\nNOT\nINVALID KEY\nCONTINUE\nEND-READ.\n")
        formulas = [fact for fact in supplied_statement_facts(supplied, source_format="free")
                    if fact["statement_kind"] == "COMPUTE"]
        self.assertTrue(all(fact["expression"] == ["RAW-AMOUNT", "*", "2"] for fact in formulas))
        self.assertFalse(any(fact.get("normal_result_condition") == "no_size_error" for fact in formulas))


if __name__ == "__main__":
    unittest.main()
