from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from statement_parser import parse_statements


class StatementParserTests(unittest.TestCase):
    def assert_complete(self, source, **options):
        result = parse_statements(source, **options)
        self.assertTrue(result.complete, result.boundaries)
        return result

    def test_inline_if_preserves_both_assignments_and_guards(self):
        source = "IF FLAG = 'Y' MOVE 1 TO RESULT-VALUE ELSE MOVE 2 TO RESULT-VALUE END-IF."
        result = self.assert_complete(source)
        self.assertEqual([(w.target, w.expression) for w in result.writes],
                         [("RESULT-VALUE", "1"), ("RESULT-VALUE", "2")])
        self.assertEqual([w.guards[0].outcome for w in result.writes], [True, False])
        self.assertEqual([w.order for w in result.writes], [0, 1])

    def test_multiline_and_inline_have_equal_guard_and_write_meaning(self):
        inline = self.assert_complete("IF A > 0 COMPUTE X = A * 2 ELSE MOVE 0 TO X END-IF MOVE X TO Y.")
        multiline = self.assert_complete("IF A > 0\n COMPUTE X = A * 2\nELSE\n MOVE 0 TO X\nEND-IF\nMOVE X TO Y.")
        projection = lambda r: [(w.target, w.expression, [(g.condition, g.outcome) for g in w.guards]) for w in r.writes]
        self.assertEqual(projection(inline), projection(multiline))

    def test_else_binds_to_nearest_unclosed_if(self):
        result = self.assert_complete("IF A = 1 IF B = 2 MOVE 3 TO X ELSE MOVE 4 TO X END-IF ELSE MOVE 5 TO X END-IF.")
        self.assertEqual([[g.outcome for g in w.guards] for w in result.writes],
                         [[True, True], [True, False], [False]])

    def test_period_ends_all_implicit_if_scopes(self):
        result = self.assert_complete("IF A = 1 IF B = 2 MOVE 3 TO X. MOVE 4 TO X.")
        self.assertEqual([len(w.guards) for w in result.writes], [2, 0])

    def test_otherwise_missing_scope_does_not_promote_partial_if(self):
        result = parse_statements("MOVE 0 TO X. IF A = 1 MOVE 2 TO X")
        self.assertFalse(result.complete)
        self.assertEqual([w.expression for w in result.writes], ["0"])
        self.assertEqual(result.boundaries[0].raw_text, "IF A = 1 MOVE 2 TO X")

    def test_explicit_partial_input_never_promotes_facts(self):
        source = "MOVE 1 TO X END-IF."
        result = parse_statements(source, complete=False)
        self.assertFalse(result.complete)
        self.assertEqual(result.statements, ())
        self.assertEqual(result.boundaries[0].raw_text, source)

    def test_evaluate_first_matching_order_and_other(self):
        result = self.assert_complete("EVALUATE STATUS WHEN 1 MOVE 10 TO X WHEN 1 THRU 3 MOVE 20 TO X WHEN OTHER MOVE 0 TO X END-EVALUATE.")
        guards = [w.guards[0] for w in result.writes]
        self.assertEqual([g.branch_index for g in guards], [0, 1, 2])
        self.assertEqual(guards[1].prior_alternatives, (("1",),))
        self.assertEqual(guards[2].prior_alternatives, (("1",), ("1 THRU 3",)))
        self.assertTrue(guards[2].is_other)
        self.assertEqual(guards[0].selector, "STATUS")

    def test_evaluate_true_with_nested_if_and_grouped_when(self):
        result = self.assert_complete("EVALUATE TRUE WHEN A > 0 WHEN B = 1 IF C = 2 MOVE 3 TO X END-IF WHEN OTHER CONTINUE END-EVALUATE.")
        self.assertEqual(len(result.writes), 1)
        self.assertEqual([g.kind for g in result.writes[0].guards], ["EVALUATE", "IF"])
        self.assertEqual(result.writes[0].guards[0].alternatives, ("A > 0", "B = 1"))

    def test_evaluate_false_preserves_selector_not_inverted_guess(self):
        result = self.assert_complete("EVALUATE FALSE WHEN A = 1 MOVE 2 TO X WHEN OTHER MOVE 3 TO X END-EVALUATE")
        self.assertEqual(result.writes[0].guards[0].selector, "FALSE")

    def test_evaluate_also_is_explicit_boundary(self):
        result = parse_statements("EVALUATE A ALSO B WHEN 1 ALSO 2 MOVE 3 TO X END-EVALUATE.")
        self.assertFalse(result.complete)
        self.assertFalse(result.writes)

    def test_when_after_other_does_not_promote_evaluate(self):
        result = parse_statements("EVALUATE A WHEN OTHER MOVE 1 TO X WHEN 2 MOVE 3 TO X END-EVALUATE.")
        self.assertFalse(result.complete)
        self.assertFalse(result.writes)

    def test_successive_assignments_and_conditional_overwrite_are_ordered(self):
        result = self.assert_complete("MOVE 0 TO X IF BASE > 0 COMPUTE X ROUNDED = BASE * 2 END-IF IF REVIEW = 'Y' MOVE 19 TO X END-IF.")
        self.assertEqual([w.expression for w in result.writes], ["0", "BASE * 2", "19"])
        self.assertTrue(result.writes[1].rounded)
        self.assertEqual(result.writes[2].guards[0].condition, "REVIEW = 'Y'")

    def test_literals_do_not_create_control_or_comments(self):
        result = self.assert_complete("MOVE 'IF A ELSE *> END-IF.' TO TEXT-VALUE.\nMOVE 'can''t' TO OTHER-VALUE. *> MOVE 9 TO X\nGOBACK.")
        self.assertEqual([w.expression for w in result.writes], ["'IF A ELSE *> END-IF.'", "'can''t'"])
        self.assertEqual([w.target for w in result.writes], ["TEXT-VALUE", "OTHER-VALUE"])

    def test_comments_and_multiline_arithmetic_keep_original_spans(self):
        source = "  COMPUTE X = A + *> ignored\r\n B * 2 END-COMPUTE."
        result = self.assert_complete(source, start_line=41)
        statement = result.statements[0]
        self.assertEqual((statement.span.start_line, statement.span.end_line), (41, 42))
        self.assertEqual(statement.span.start_column, 3)
        self.assertEqual(source[statement.span.start_offset:statement.span.end_offset], statement.text)
        self.assertEqual(statement.reads, ("A", "B"))
        self.assertIn("*> ignored", statement.text)

    def test_decimal_period_and_signed_numbers(self):
        result = self.assert_complete("IF A > 1.25 MOVE -2.5 TO X ELSE COMPUTE X = -(A + 2) / 4 END-IF.")
        self.assertEqual(result.writes[0].expression, "-2.5")
        self.assertEqual(result.writes[1].expression, "-(A + 2) / 4")

    def test_nested_boolean_conditions_are_consumed_completely(self):
        result = self.assert_complete("IF (A = 1 OR B > 2) AND NOT C = 3 MOVE 1 TO X END-IF.")
        self.assertEqual(len(result.writes), 1)
        malformed = parse_statements("IF A = 1 OR 2 MOVE 1 TO X END-IF.")
        self.assertFalse(malformed.complete)

    def test_unknown_nested_structure_invalidates_whole_compound(self):
        source = "IF A = 1 MOVE 1 TO X SEARCH ITEMS WHEN B = 1 MOVE 2 TO X END-SEARCH END-IF. MOVE 3 TO X."
        result = parse_statements(source)
        self.assertFalse(result.complete)
        self.assertEqual(result.writes, ())
        self.assertEqual(result.boundaries[0].raw_text, source)

    def test_unknown_top_level_tail_retains_only_completed_prefix(self):
        result = parse_statements("MOVE 1 TO X. EXEC SQL SELECT A END-EXEC. MOVE 2 TO X.")
        self.assertFalse(result.complete)
        self.assertEqual([w.expression for w in result.writes], ["1"])
        self.assertTrue(result.boundaries[0].raw_text.startswith("EXEC SQL"))

    def test_compute_exception_clause_is_opaque_not_silently_discarded(self):
        result = self.assert_complete("COMPUTE X = A ON SIZE ERROR MOVE 0 TO X END-COMPUTE.")
        self.assertTrue(result.statements[0].effects_unknown)
        self.assertFalse(result.writes)

    def test_exception_compute_does_not_erase_other_evaluate_arms(self):
        result = self.assert_complete("EVALUATE MODE WHEN 1 COMPUTE X = A ON SIZE ERROR MOVE 0 TO X END-COMPUTE WHEN OTHER MOVE 2 TO X END-EVALUATE.")
        self.assertEqual([s.kind for s in result.statements], ["COMPUTE", "MOVE"])
        self.assertEqual([w.expression for w in result.writes], ["2"])
        self.assertEqual(result.statements[0].guards[0].alternatives, ("1",))
        self.assertTrue(result.writes[0].guards[0].is_other)

    def test_incomplete_exception_scope_is_not_complete(self):
        result = parse_statements("COMPUTE X = A ON SIZE ERROR MOVE 0 TO X.")
        self.assertFalse(result.complete)
        self.assertFalse(result.writes)

    def test_opaque_handler_cannot_swallow_outer_scope(self):
        result = parse_statements("IF A = 1 READ INPUT-FILE AT END MOVE 0 TO X END-IF END-READ.")
        self.assertFalse(result.complete)
        self.assertFalse(result.writes)

    def test_opaque_handler_preserves_balanced_nested_if(self):
        result = self.assert_complete("COMPUTE X = A ON SIZE ERROR IF B = 1 MOVE 0 TO X ELSE MOVE 2 TO X END-IF END-COMPUTE.")
        self.assertTrue(result.statements[0].effects_unknown)
        self.assertFalse(result.writes)

    def test_full_unit_is_not_mistaken_for_statement_body(self):
        result = parse_statements("IDENTIFICATION DIVISION. PROGRAM-ID. SAMPLE.")
        self.assertFalse(result.complete)
        self.assertFalse(result.nodes)

    def test_unterminated_literal_invalidates_input(self):
        result = parse_statements("MOVE 1 TO X. MOVE 'open TO Y")
        self.assertFalse(result.complete)
        self.assertFalse(result.writes)
        self.assertEqual(result.boundaries[0].reason, "unterminated_literal")

    def test_fixed_source_preserves_columns_and_comment_lines(self):
        source = "000100*MOVE 9 TO X.\n000200     IF FLAG = 'Y'\n000300         MOVE 1 TO X\n000400     END-IF.\n"
        result = self.assert_complete(source, source_format="fixed", start_line=10)
        self.assertEqual(len(result.writes), 1)
        self.assertEqual((result.writes[0].span.start_line, result.writes[0].span.start_column), (12, 16))

    def test_fixed_continuation_is_not_guessed(self):
        result = parse_statements("000100     MOVE 'A' TO X\n000200-    'B'.", source_format="fixed")
        self.assertFalse(result.complete)
        self.assertFalse(result.writes)

    def test_token_and_nesting_budgets_are_boundaries(self):
        self.assertFalse(parse_statements("MOVE 1 TO X", max_tokens=2).complete)
        result = parse_statements("IF A = 1 IF B = 2 MOVE 3 TO X END-IF END-IF", max_depth=2)
        self.assertFalse(result.complete)
        self.assertFalse(result.writes)

    def test_results_are_serializable_and_source_preserving(self):
        result = self.assert_complete("MOVE 1 TO X Y. GOBACK.")
        serialized = json.loads(json.dumps(asdict(result)))
        self.assertEqual(serialized["source"], result.source)
        self.assertEqual([w["target"] for w in serialized["writes"]], ["X", "Y"])

    def test_opaque_call_keeps_guard_and_unknown_effects(self):
        result = self.assert_complete("IF FLAG = 'Y' CALL 'SUBTASK' USING ARGUMENT END-CALL MOVE 1 TO X END-IF.")
        self.assertEqual([s.kind for s in result.statements], ["CALL", "MOVE"])
        self.assertTrue(result.statements[0].effects_unknown)
        self.assertEqual(result.statements[0].writes, ())
        self.assertEqual(result.statements[0].guards, result.statements[1].guards)

    def test_opaque_perform_and_io_do_not_stop_later_material(self):
        result = self.assert_complete("PERFORM SUBTASK READ INPUT-FILE INTO RECORD-VALUE END-READ WRITE OUTPUT-RECORD MOVE 1 TO X.")
        self.assertEqual([s.kind for s in result.statements], ["PERFORM", "READ", "WRITE", "MOVE"])
        self.assertTrue(all(s.effects_unknown for s in result.statements[:3]))
        self.assertEqual([w.target for w in result.writes], ["X"])

    def test_inline_loop_is_opaque_not_unconditional_assignments(self):
        result = self.assert_complete("PERFORM UNTIL FLAG = 'Y' MOVE 1 TO X END-PERFORM MOVE 2 TO X.")
        self.assertEqual([s.kind for s in result.statements], ["PERFORM", "MOVE"])
        self.assertEqual([w.expression for w in result.writes], ["2"])
        self.assertTrue(result.statements[0].effects_unknown)

    def test_file_handler_remains_opaque_with_original_source(self):
        result = self.assert_complete("READ INPUT-FILE AT END MOVE 'Y' TO DONE-FLAG END-READ MOVE 2 TO X.")
        self.assertTrue(result.statements[0].effects_unknown)
        self.assertIn("AT END", result.statements[0].text)
        self.assertEqual([w.target for w in result.writes], ["X"])

    def test_incomplete_opaque_scope_does_not_promote_following_body(self):
        result = parse_statements("PERFORM UNTIL FLAG = 'Y' MOVE 1 TO X")
        self.assertFalse(result.complete)
        self.assertFalse(result.writes)


    def test_text_comparisons_do_not_allow_text_arithmetic(self):
        self.assert_complete("IF 'A' = 'B' MOVE 1 TO X END-IF.")
        self.assert_complete("IF TEXT-VALUE = SPACES MOVE 1 TO X END-IF.")
        for condition in ("'A' + 1 = 2", "SPACES * 2 = X", "'2' / 2 = 1"):
            with self.subTest(condition=condition):
                result = parse_statements(f"IF {condition} MOVE 1 TO X END-IF.")
                self.assertFalse(result.complete)
                self.assertEqual(result.writes, ())

    def test_variable_times_loop_cannot_close_outer_loop_early(self):
        source = ("PERFORM UNTIL DONE = 1 PERFORM LIMIT TIMES MOVE 1 TO X "
                  "END-PERFORM MOVE 2 TO X END-PERFORM MOVE 3 TO X.")
        result = self.assert_complete(source)
        self.assertEqual([s.kind for s in result.statements], ["PERFORM", "MOVE"])
        self.assertEqual([w.expression for w in result.writes], ["3"])
        self.assertIn("MOVE 2 TO X", result.statements[0].text)

    def test_variable_times_loop_in_exception_handler_keeps_outer_scope(self):
        result = self.assert_complete(
            "COMPUTE X = A ON SIZE ERROR PERFORM LIMIT TIMES "
            "MOVE 0 TO X END-PERFORM END-COMPUTE MOVE 3 TO X.")
        self.assertEqual([w.expression for w in result.writes], ["3"])
        self.assertTrue(result.statements[0].effects_unknown)

    def test_evaluate_selector_has_its_own_source_span(self):
        source = "EVALUATE STATUS\nWHEN 1 MOVE 2 TO X\nWHEN OTHER MOVE 3 TO X\nEND-EVALUATE."
        result = self.assert_complete(source, start_line=70)
        for write in result.writes:
            guard = write.guards[0]
            self.assertEqual((guard.selector_span.start_line, guard.selector_span.end_line), (70, 70))
            self.assertEqual(source[guard.selector_span.start_offset:guard.selector_span.end_offset], "EVALUATE STATUS")
        self.assertEqual([w.guards[0].span.start_line for w in result.writes], [71, 72])


if __name__ == "__main__":
    unittest.main()
