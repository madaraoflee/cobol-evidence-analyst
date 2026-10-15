"""I/O branch syntax from the shared, literal-aware source front end."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from statement_facts import io_clause_guards
from structural_index import normalize_cobol_lines


class IOClauseFactsTests(unittest.TestCase):
    def guards(self, source):
        return io_clause_guards(normalize_cobol_lines(source, "free")[0])

    def test_error_and_normal_clauses_keep_their_own_physical_ranges(self):
        guards = self.guards("READ INPUT-FILE\nINVALID KEY\nMOVE 101 TO RESULT-CODE\n"
                             "NOT INVALID KEY\nMOVE 0 TO RESULT-CODE\nEND-READ.")
        self.assertEqual([(row["condition"], row["start_line"], row["end_line"]) for row in guards],
                         [("INVALID KEY", 2, 3), ("NOT INVALID KEY", 4, 5)])
        self.assertTrue(all(row["operation_text"] == "READ INPUT-FILE" for row in guards))

    def test_literals_and_comments_cannot_create_io_or_branch_scopes(self):
        self.assertEqual(self.guards("DISPLAY 'READ INPUT-FILE INVALID KEY END-READ'.\n"
                                     "*> READ INPUT-FILE INVALID KEY\nCONTINUE."), [])

    def test_open_tail_and_compile_directives_remain_unproven(self):
        self.assertEqual(self.guards("READ INPUT-FILE\nINVALID KEY\nMOVE 101 TO RESULT-CODE"), [])
        self.assertEqual(self.guards(">>IF FEATURE\nREAD INPUT-FILE\nINVALID KEY\nMOVE 101 TO RESULT-CODE\nEND-READ.\n>>END-IF"), [])

    def test_nested_io_guards_retain_both_outer_and_inner_conditions(self):
        guards = self.guards("READ FIRST-FILE\nINVALID KEY\nREAD SECOND-FILE\nNOT INVALID KEY\n"
                             "MOVE 101 TO RESULT-CODE\nEND-READ\nEND-READ.")
        covering = [row for row in guards if row["start_line"] <= 5 <= row["end_line"]]
        self.assertEqual({(row["operation_text"], row["condition"]) for row in covering},
                         {("READ FIRST-FILE", "INVALID KEY"), ("READ SECOND-FILE", "NOT INVALID KEY")})

    def test_mixed_line_branch_marker_is_not_line_level_proof(self):
        lines = normalize_cobol_lines("READ INPUT-FILE INVALID KEY\n"
            "COMPUTE OUT-AMOUNT = RAW-AMOUNT * 2 NOT INVALID KEY\n"
            "COMPUTE OUT-AMOUNT = RAW-AMOUNT * 3 END-READ.", "free")[0]
        facts = io_clause_guards(lines, include_operation_scopes=True)
        scope = next(row for row in facts if row["kind"] == "io_operation_scope")
        self.assertIn(2, scope["ambiguous_lines"])

    def test_implicit_nested_period_retains_unknown_outer_scope(self):
        lines = normalize_cobol_lines("READ FIRST-FILE\nINVALID KEY\nREAD SECOND-FILE\n"
            "NOT INVALID KEY\nCOMPUTE OUT-AMOUNT = RAW-AMOUNT * 2.", "free")[0]
        facts = io_clause_guards(lines, include_operation_scopes=True)
        self.assertTrue(any(row["kind"] == "io_scope_unknown" and row["start_line"] <= 5 <= row["end_line"]
                            for row in facts))

    def test_embedded_sql_lexical_boundary_is_unknown_instead_of_no_guards(self):
        lines = normalize_cobol_lines("EXEC SQL SET :RESULT-CODE = LENGTH('A' || 'B') END-EXEC\n"
            "READ INPUT-FILE\nINVALID KEY\nMOVE 101 TO RESULT-CODE\nEND-READ.", "free")[0]
        facts = io_clause_guards(lines, include_operation_scopes=True)
        self.assertTrue(any(row["kind"] == "io_scope_unknown" and row.get("reason") == "source_lexical_boundary"
                            for row in facts))


if __name__ == "__main__":
    unittest.main()
