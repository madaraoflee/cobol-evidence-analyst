"""Offline regressions for the language of locally repaired answer segments."""

import hashlib
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claim_ledger import _render_fact, review_answer_claims


def source_page(body):
    return {"evidence_id": "ev_formula", "relative_path": "modules/calculate.cbl",
            "source_sha256": hashlib.sha256(body.encode()).hexdigest(), "source_text": body,
            "start_line": 1, "end_line": len(body.splitlines()), "span_truncated": False}


class ClaimLedgerLanguageTests(unittest.TestCase):
    def review(self, answer, body="COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.", **options):
        source = source_page(body)
        return review_answer_claims(answer, [source], {"ev_formula": source},
                                    source_format="free", **options)

    def assert_english(self, answer):
        self.assertIsNone(re.search(r"[\u3400-\u9fff]", answer), answer)

    def test_english_correction_keeps_source_reference_and_independent_prose(self):
        answer, ledger = self.review(
            "OUT-AMOUNT = IN-AMOUNT * 999.[ev_formula] Input comes from the record.[ev_formula]")
        self.assert_english(answer)
        self.assertIn("The supplied source directly confirms:", answer)
        self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", answer)
        self.assertIn("(modules/calculate.cbl:1)[ev_formula]", answer)
        self.assertIn("Input comes from the record.[ev_formula]", answer)
        self.assertNotIn("999", answer)
        self.assertEqual(ledger.entries[0].support_status, "contradicted")

    def test_missing_citation_repair_uses_requested_answer_language(self):
        answer, ledger = self.review("OUT-AMOUNT = IN-AMOUNT * 999。[ev_missing]", answer_language="en")
        self.assert_english(answer)
        self.assertIn("The supplied source directly confirms:", answer)
        self.assertIn("[ev_formula]", answer)
        self.assertNotIn("ev_missing", answer)
        self.assertEqual(ledger.entries[0].invalid_evidence_ids, ["ev_missing"])

    def test_unsupported_formula_gets_english_uncertainty(self):
        answer, ledger = self.review("OTHER-AMOUNT = 999.[ev_missing]", answer_language="en")
        self.assert_english(answer)
        self.assertIn("does not yet support this formula, condition, or effect", answer)
        self.assertNotIn("999", answer)
        self.assertEqual(ledger.entries[0].revision, "scoped_uncertainty")

    def test_missing_prose_citation_gets_english_qualification(self):
        answer, ledger = self.review("The record comes from an external feed.[ev_missing]")
        self.assert_english(answer)
        self.assertIn("The record comes from an external feed.", answer)
        self.assertIn("evidence that was not supplied", answer)
        self.assertNotIn("ev_missing", answer)
        self.assertEqual(ledger.entries[0].reason_code, "INVALID_CITATION_REQUIRES_REVIEW")

    def test_final_value_correction_localizes_guards_and_scope(self):
        body = ("IF IN-AMOUNT > 0\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2\n"
                "ELSE\nMOVE 0 TO OUT-AMOUNT\nEND-IF.")
        answer, ledger = self.review("OUT-AMOUNT always = IN-AMOUNT * 2.[ev_formula]", body,
                                     answer_language="en")
        self.assert_english(answer)
        self.assertIn("Under source control condition", answer)
        self.assertIn("condition is false:", answer)
        self.assertIn("OUT-AMOUNT = 0", answer)
        self.assertIn("the final value still requires checking control flow", answer)
        self.assertEqual(ledger.entries[0].reason_code, "FINAL_VALUE_REQUIRES_CONTROL_AND_OVERWRITE_ANALYSIS")

    def test_empty_procedure_correction_stays_english(self):
        body = ("PROCEDURE DIVISION.\n1400-COMPUTE-FEES.\nEXIT.\n"
                "9000-FINISH.\nCONTINUE.")
        answer, ledger = self.review("1400-COMPUTE-FEES computes the fees.[ev_formula]", body)
        self.assert_english(answer)
        self.assertIn("The cited source directly confirms:", answer)
        self.assertIn("its calculation or write logic is not implemented", answer)
        self.assertEqual(ledger.entries[0].reason_code, "COMPLETE_PROCEDURE_HAS_ONLY_NOOP_STATEMENTS")

    def test_fact_qualifications_are_english_and_preserve_code(self):
        fact = {"predicate": "write_statement", "effect": "WRITE", "target": "OUTPUT-RECORD",
                "relative_path": "modules/calculate.cbl", "start_line": 7, "evidence_id": "ev_formula",
                "guards": [{"condition": "READY-FLAG = 1", "outcome": False}],
                "normal_result_condition": "no_size_error", "compilation_status": "candidate"}
        answer = _render_fact(fact, answer_language="en")
        self.assert_english(answer)
        self.assertIn("Compilation conditions are unconfirmed", answer)
        self.assertIn("condition is false: READY-FLAG = 1", answer)
        self.assertIn("when there is no SIZE ERROR", answer)
        self.assertIn("`WRITE OUTPUT-RECORD`", answer)
        self.assertIn("success requires runtime confirmation", answer)
        self.assertIn("(modules/calculate.cbl:7)[ev_formula]", answer)

    def test_english_repairs_preserve_original_source_literals(self):
        answer, _ = self.review('MESSAGE-TEXT = "wrong".[ev_formula]', 'MOVE "原始文字" TO MESSAGE-TEXT.',
                                answer_language="en")
        self.assertIn("The supplied source directly confirms:", answer)
        self.assertIn('MESSAGE-TEXT = "原始文字"', answer)
        self.assertNotIn("wrong", answer)

    def test_explicit_english_overrides_language_of_rejected_answer(self):
        answer, _ = self.review("OUT-AMOUNT 最终等于 IN-AMOUNT * 2。[ev_formula]", answer_language="en")
        self.assert_english(answer)
        self.assertIn("the final value still requires checking", answer)

    def test_chinese_defaults_and_traditional_selection_remain_available(self):
        answer, _ = self.review("WRITE OTHER-RECORD。[ev_formula]")
        self.assertIn("尚未得到所引源码支持", answer)
        answer, _ = self.review("OTHER-AMOUNT = 9.[ev_missing]", answer_language="zh-HK")
        self.assertIn("尚未得到所引源碼支持", answer)
        self.assertNotIn("源码", answer)

    def test_supported_english_answer_stays_exactly_unchanged(self):
        original = "OUT-AMOUNT = IN-AMOUNT * 2.[ev_formula] Input comes from the record.[ev_formula]"
        answer, ledger = self.review(original, answer_language="en")
        self.assertEqual(answer, original)
        self.assertEqual(ledger.to_dict()["revised_claim_count"], 0)

    def test_call_description_with_real_condition_is_not_an_assignment(self):
        body = 'IF READY-FLAG = 1\nCALL "JOB-STEP"\nEND-IF.'
        original = ("Under `IF READY-FLAG = 1`, this is an in-program call request."
                    "[ev_formula]")
        answer, ledger = self.review(original, body, answer_language="en")
        self.assertEqual(answer, original)
        self.assertEqual(ledger.to_dict()["revised_claim_count"], 0)
        self.assertFalse(any(entry.target_entity == "THIS" for entry in ledger.entries))
        self.assertTrue(any(entry.claim_type == "condition_statement" and entry.support_status == "supported"
                            for entry in ledger.entries))

    def test_field_declaration_and_role_descriptions_are_not_formulas(self):
        body = ("DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 OUT-AMOUNT PIC 9(6)V99.\n"
                "PROCEDURE DIVISION.\nMAIN.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nGOBACK.")
        for original in ("`OUT-AMOUNT` is declared as `PIC 9(6)V99`.[ev_formula]",
                         "OUT-AMOUNT is defined with a numeric picture.[ev_formula]",
                         "OUT-AMOUNT is an output amount.[ev_formula]",
                         "OUT-AMOUNT is used as an output field.[ev_formula]"):
            with self.subTest(original=original):
                answer, ledger = self.review(original, body, answer_language="en")
                self.assertEqual(answer, original)
                self.assertEqual(ledger.to_dict()["revised_claim_count"], 0)
                self.assertTrue(all(entry.claim_type == "business_explanation" for entry in ledger.entries))
                self.assertFalse(ledger.to_dict()["all_claims_supported"])

    def test_english_is_arithmetic_notation_is_still_checked(self):
        original = "OUT-AMOUNT is IN-AMOUNT * 2.[ev_formula]"
        answer, ledger = self.review(original, answer_language="en")
        self.assertEqual(answer, original)
        self.assertEqual(ledger.entries[0].support_status, "supported")
        answer, ledger = self.review("OUT-AMOUNT is IN-AMOUNT * 999.[ev_formula]", answer_language="en")
        self.assert_english(answer)
        self.assertNotIn("999", answer)
        self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", answer)
        self.assertEqual(ledger.entries[0].support_status, "contradicted")

    def test_article_named_source_operands_remain_valid_notation(self):
        for field in ("A", "AN", "THE"):
            for expression, source_expression in ((f"{field} * 2", f"{field} * 2"),
                    (f"{field} times 2", f"{field} * 2"),
                    (f"{field} OF INPUT-RECORD * 2", f"{field} OF INPUT-RECORD * 2")):
                with self.subTest(field=field, expression=expression):
                    original = f"OUT-AMOUNT is {expression}.[ev_formula]"
                    answer, ledger = self.review(original, f"COMPUTE OUT-AMOUNT = {source_expression}.",
                                                 answer_language="en")
                    self.assertEqual(answer, original)
                    self.assertEqual(ledger.entries[0].support_status, "supported")


if __name__ == "__main__":
    unittest.main()
