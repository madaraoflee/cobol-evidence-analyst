"""Reusing a lexical pass must preserve fields, guards and source locations."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import business_index
import statement_facts
from structural_index import _extract_data_access, _unique_identifiers


class StatementTokenReuseTests(unittest.TestCase):
    def test_header_prefilter_preserves_format_validation_and_unicode_identity(self):
        with patch.object(statement_facts, "clean_cobol_line", side_effect=AssertionError("ordinary statement rescanned")):
            self.assertEqual(statement_facts.program_identifiers("COMPUTE TOTAL = AMOUNT * RATE."), ())
        with self.assertRaises(ValueError):
            statement_facts.program_identifiers("MOVE A TO B.", source_format="unsupported")
        self.assertEqual(statement_facts.program_identifier("PROGRAM-ıD. LEDGER."), "LEDGER")

    def test_reused_tokens_preserve_assignment_and_reference_facts(self):
        statements = [
            "COMPUTE AMOUNT OF OUTPUT-REC ROUNDED = AMOUNT IN INPUT-REC * 1.25.",
            "MOVE 'IF CALL COPY' TO MESSAGE-TEXT.",
            "MOVE 'A''B' TO NAME-TEXT.",
            "COMPUTE RESULT = INPUT + 1 ON SIZE ERROR MOVE 0 TO RESULT END-COMPUTE.",
            "ADD INPUT TO TOTAL GIVING FIRST-RESULT SECOND-RESULT.",
            "SUBTRACT INPUT FROM FIRST-RESULT SECOND-RESULT.",
            "DIVIDE INPUT INTO TOTAL GIVING RESULT REMAINDER REST.",
            "IF AMOUNT OF INPUT-REC > 0 AND FLAG = 'YES'.",
            "EVALUATE INPUT(1:2)",
            "WHEN OTHER",
            "COMPUTE RESULT = INPUT *> comment\n + RATE.",
            "MOVE INPUT(1) TO OUTPUT(2).",
            "MOVE INPUT-AMOUNT(INDEX-NUM) TO OUTPUT-AMOUNT(OFFSET-NUM:LENGTH-NUM).",
        ]
        for text in statements:
            with self.subTest(text=text):
                tokens = statement_facts.lex_cobol(text)
                expected = (_extract_data_access(text), _unique_identifiers(text))
                lexer = statement_facts.lex_cobol
                def avoid_repeating_statement(fragment, **options):
                    self.assertNotEqual(fragment, text, "Statement was tokenized twice")
                    return lexer(fragment, **options)
                with patch.object(statement_facts, "lex_cobol", side_effect=avoid_repeating_statement):
                    self.assertEqual(expected, (_extract_data_access(text, tokens=tokens),
                                                _unique_identifiers(text, tokens=tokens)))

    def test_symbolic_subscripts_and_reference_modification_remain_inputs(self):
        text = "MOVE INPUT-AMOUNT(INDEX-NUM) TO OUTPUT-AMOUNT(OFFSET-NUM:LENGTH-NUM)."
        facts = statement_facts.assignment_facts(text, tokens=statement_facts.lex_cobol(text))
        self.assertEqual(set(facts.reads), {"INPUT-AMOUNT(INDEX-NUM)", "INDEX-NUM", "OFFSET-NUM", "LENGTH-NUM"})
        self.assertEqual(facts.writes, ("OUTPUT-AMOUNT(OFFSET-NUM:LENGTH-NUM)",))

    def test_streaming_index_keeps_complete_rule_and_occurrence_tables(self):
        source_text = "\n".join([
            "IDENTIFICATION DIVISION.", "PROGRAM-ID. LEDGER.",
            "DATA DIVISION.", "WORKING-STORAGE SECTION.",
            "01 INPUT-AMOUNT PIC 9(5).", "01 TOTAL-AMOUNT PIC 9(7).",
            "PROCEDURE DIVISION.", "MAIN-WORK.",
            "IF INPUT-AMOUNT > 0",
            "COMPUTE TOTAL-AMOUNT = INPUT-AMOUNT * 2",
            "ELSE", "MOVE 0 TO TOTAL-AMOUNT", "END-IF.",
            "IF INPUT-AMOUNT > 5 ADD 1 TO TOTAL-AMOUNT ELSE MOVE 0 TO TOTAL-AMOUNT END-IF.",
            "MOVE 'IF COMPUTE' TO TOTAL-AMOUNT.",
            "COMPUTE TOTAL-AMOUNT = INPUT-AMOUNT + 1 ON SIZE ERROR MOVE 0 TO TOTAL-AMOUNT END-COMPUTE.",
            "GOBACK.",
            "COMPUTE TOTAL-AMOUNT = INPUT-AMOUNT ` 3.",
            "MOVE INPUT-AMOUNT TO",
        ]) + "\n"
        original_access, original_identifiers = business_index._extract_data_access, business_index._unique_identifiers
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "ledger.cbl").write_text(source_text, encoding="utf-8")
            def build(name):
                database = root / name
                business_index.build_business_index(source, database, source_format="free", quiet=True,
                    framework_reference_path=root / "absent.md")
                with closing(sqlite3.connect(database)) as db:
                    return {table: sorted(db.execute(f"SELECT * FROM {table}"), key=repr) for table in
                        ("business_rules", "business_rule_fields", "business_rule_occurrences", "relations", "code_units")}
            optimized = build("optimized.sqlite")
            with patch.object(business_index, "_extract_data_access", side_effect=lambda text, **kw: original_access(text)), \
                 patch.object(business_index, "_unique_identifiers", side_effect=lambda text, **kw: original_identifiers(text)):
                reparsed = build("reparsed.sqlite")
            self.assertEqual(optimized, reparsed)


if __name__ == "__main__":
    unittest.main()
