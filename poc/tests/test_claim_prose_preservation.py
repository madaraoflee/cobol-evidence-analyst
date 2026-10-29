"""Preserve independent prose only around precisely bounded source repairs."""

import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_synthesis import assess_business_answer
import claim_ledger
from claim_ledger import _assertions, review_answer_claims
from draft_validation import validate_answer_draft


SOURCE = """*> Purpose: prepare charge details for eligible requests.
IDENTIFICATION DIVISION.
PROGRAM-ID. CHARGE-PREP.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 RESPONSE-CODE PIC XX.
01 BASE-AMOUNT PIC 9(5).
01 DUE-AMOUNT PIC 9(7).
PROCEDURE DIVISION.
MOVE '00' TO RESPONSE-CODE.
COMPUTE DUE-AMOUNT = BASE-AMOUNT * 2 END-COMPUTE.
GOBACK.
"""
PURPOSE = "这项处理为符合要求的请求准备收费明细"
REFERENCE = "[ev_charge]"


class ClaimProsePreservationTests(unittest.TestCase):
    def setUp(self):
        self.page = {"evidence_id": "ev_charge", "relative_path": "charge.cbl",
            "source_text": SOURCE, "source_sha256": hashlib.sha256(SOURCE.encode()).hexdigest(),
            "start_line": 1, "end_line": len(SOURCE.splitlines()), "format_hint": "free"}

    def review(self, answer):
        return review_answer_claims(answer, [self.page], {"ev_charge": self.page}, source_format="free")

    def assert_preserved(self, answer, *, missing="'99'", retained=PURPOSE):
        result, ledger = self.review(answer)
        self.assertIn(retained, result)
        self.assertNotIn(missing, result)
        repaired = [row for row in ledger.entries if row.revision == "rendered_explicit_statement_span"]
        self.assertEqual(len(repaired), 1)
        self.assertEqual(repaired[0].support_status, "contradicted")
        prose = [row for row in ledger.entries if row.reason_code == "PROSE_REMAINDER_NOT_CHECKED"]
        self.assertEqual(len(prose), 1)
        self.assertEqual(prose[0].support_status, "unsupported")
        self.assertIn(retained, prose[0].text)
        self.assertFalse(ledger.to_dict()["prose_semantics_verified"])
        self.assertFalse(ledger.to_dict()["all_claims_supported"])
        return result

    def test_move_repair_preserves_independent_business_introduction(self):
        result = self.assert_preserved(f"{PURPOSE}，MOVE '99' TO RESPONSE-CODE。{REFERENCE}")
        self.assertIn("RESPONSE-CODE = '00'", result)

    def test_any_trailing_prose_keeps_conservative_whole_segment_repair(self):
        for suffix in (PURPOSE, "故请求被拒绝", "仅对有效请求执行"):
            for prefix in ("", f"{PURPOSE}，"):
                with self.subTest(prefix=prefix, suffix=suffix):
                    result, ledger = self.review(
                        f"{prefix}MOVE '99' TO RESPONSE-CODE，{suffix}。{REFERENCE}")
                    self.assertNotIn("'99'", result)
                    self.assertNotIn(suffix, result)
                    self.assertFalse(any(row.revision == "rendered_explicit_statement_span"
                                         for row in ledger.entries))

    def test_inline_statement_and_quoted_comma_operand_are_replaced_as_a_whole(self):
        for code, wrong in (("`MOVE '99' TO RESPONSE-CODE`", "'99'"),
                            ("MOVE 'A,B；C' TO RESPONSE-CODE", "'A,B；C'")):
            with self.subTest(code=code):
                result = self.assert_preserved(f"{PURPOSE}，{code}。{REFERENCE}", missing=wrong)
                self.assertIn("RESPONSE-CODE = '00'", result)

    def test_complete_compute_expression_and_terminator_are_replaced(self):
        for code in ("COMPUTE DUE-AMOUNT = BASE-AMOUNT * 999 END-COMPUTE",
                     "`COMPUTE DUE-AMOUNT = BASE-AMOUNT * (999 + 1) END-COMPUTE`"):
            with self.subTest(code=code):
                result = self.assert_preserved(f"{PURPOSE}，{code}。{REFERENCE}", missing="999")
                self.assertIn("DUE-AMOUNT = BASE-AMOUNT * 2", result)
                self.assertNotIn("END-COMPUTE", result)

    def test_english_independent_prose_remains_unverified(self):
        prose = "This service prepares charge details for eligible requests"
        self.assert_preserved(f"{prose}, MOVE '99' TO RESPONSE-CODE. {REFERENCE}", retained=prose)

    def test_control_causal_and_numeric_context_keeps_conservative_whole_segment_repair(self):
        for answer in (
                f"{PURPOSE}，如果请求已批准，则 MOVE '99' TO RESPONSE-CODE。{REFERENCE}",
                f"{PURPOSE}，MOVE '99' TO RESPONSE-CODE，因此本次收费失败。{REFERENCE}",
                f"{PURPOSE}，MOVE '99' TO RESPONSE-CODE，返回的错误码是99。{REFERENCE}",
                f"仅对有效请求执行，MOVE '99' TO RESPONSE-CODE。{REFERENCE}",
                f"因而请求被拒绝，MOVE '99' TO RESPONSE-CODE。{REFERENCE}",
                f"{PURPOSE}，仅对有效请求执行，MOVE '99' TO RESPONSE-CODE。{REFERENCE}",
                f"{PURPOSE}，IF RESPONSE-CODE = '99' MOVE '99' TO RESPONSE-CODE。{REFERENCE}"):
            with self.subTest(answer=answer):
                result, ledger = self.review(answer)
                self.assertNotIn("'99'", result)
                self.assertNotIn("因此本次收费失败", result)
                self.assertNotIn("错误码是99", result)
                self.assertNotIn("如果请求已批准", result)
                self.assertFalse(any(row.revision == "rendered_explicit_statement_span" for row in ledger.entries))

    def test_ambiguous_multiple_targets_and_repeated_statements_do_not_preserve_rejected_text(self):
        for code in ("MOVE '99' TO RESPONSE-CODE OTHER-CODE",
                     "MOVE '99' TO RESPONSE-CODE，MOVE '99' TO RESPONSE-CODE"):
            with self.subTest(code=code):
                result, ledger = self.review(f"{PURPOSE}，{code}。{REFERENCE}")
                self.assertNotIn("'99'", result)
                self.assertNotIn("OTHER-CODE", result)
                self.assertFalse(any(row.revision == "rendered_explicit_statement_span" for row in ledger.entries))

    def test_unknown_natural_notation_and_invalid_citations_do_not_gain_partial_repair(self):
        for answer in (f"{PURPOSE}，设置 RESPONSE-CODE 为 '99'。{REFERENCE}",
                       f"{PURPOSE}，MOVE '99' TO RESPONSE-CODE。[ev_missing]"):
            with self.subTest(answer=answer):
                result, ledger = self.review(answer)
                self.assertNotIn("'99'", result)
                self.assertNotIn("ev_missing", result)
                self.assertFalse(any(row.revision == "rendered_explicit_statement_span" for row in ledger.entries))

    def test_statement_inside_quoted_prose_is_not_selected_as_an_independent_span(self):
        text = f'{PURPOSE}，"MOVE \'99\' TO RESPONSE-CODE"。{REFERENCE}'
        assertion = {"predicate": "assignment_statement", "target": "RESPONSE-CODE",
                     "expression": ["'99'"], "rounded": False}
        self.assertIsNone(claim_ledger._independent_statement_span(text, assertion))
        text = f"{PURPOSE}，COMPUTE DUE-AMOUNT = BASE-AMOUNT * 999 = 999。{REFERENCE}"
        for assertion in _assertions(text):
            self.assertIsNone(claim_ledger._independent_statement_span(text, assertion))

    def test_actual_draft_final_review_and_completion_keep_the_business_explanation(self):
        question = "请解释这个程序的业务目的"
        answer = f"{PURPOSE}，MOVE '99' TO RESPONSE-CODE。{REFERENCE}"
        reviewed = validate_answer_draft(question, answer, {}, [self.page], source_format="free")
        final, final_ledger = self.review(reviewed["answer"])
        assessment = assess_business_answer(question, final, {}, [self.page])
        self.assertIn(PURPOSE, final)
        self.assertNotIn("'99'", final)
        self.assertEqual(final, reviewed["answer"])
        self.assertEqual(final_ledger.to_dict()["revised_claim_count"], 0)
        self.assertEqual(assessment["missing_aspects"], [])
        self.assertFalse(assessment["business_explanation_review"]["semantic_verification"])
        self.assertTrue(any(row["reason_code"] == "PROSE_REMAINDER_NOT_CHECKED"
                            and row["support_status"] == "unsupported" for row in reviewed["ledger"]["entries"]))


if __name__ == "__main__":
    unittest.main()
