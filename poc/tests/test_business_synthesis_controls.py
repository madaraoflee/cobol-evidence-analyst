"""Adversarial omission checks over independently readable source rules.

These are bounded lexical checks. Rejecting a known omission does not establish
that an arbitrary answer is correct, nor that the source has been executed.
"""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_synthesis import answer_requirements, assess_business_answer, needs_synthesis_review


class SuppliedCalculationControlTests(unittest.TestCase):
    def material(self, text, *, identifier="ev:rule"):
        pages = [{"evidence_id": identifier, "relative_path": "rule.cbl", "source_text": text}]
        investigation = {"required_items": [
            {"kind": kind, "status": "SATISFIED", "candidate_count": 1,
             "evidence_ids": [identifier]}
            for kind in ("formula", "conditions", "result_adjustments")]}
        return investigation, pages

    def override_material(self):
        # Independent source truth: (-5, N) -> 0; (10, Y) -> 19. Applying only
        # the first formula would instead produce -10 and 20, respectively.
        return self.material("IF BASIS > ZERO\nCOMPUTE RESULT = BASIS * 2\n"
                             "ELSE\nMOVE ZERO TO RESULT\nEND-IF.\n"
                             "IF REVIEW-FLAG = 'Y'\nMOVE 19 TO RESULT\nEND-IF.")

    def test_default_and_brief_formula_only_require_real_branch_and_later_override(self):
        investigation, pages = self.override_material()
        for detail in ("detailed", "brief"):
            with self.subTest(detail=detail):
                review = assess_business_answer("RESULT 怎么计算？", "结果等于基础值乘以2。",
                                                investigation, pages, answer_detail=detail)
                self.assertEqual(set(review["missing_aspects"]),
                                 {"conditions", "result_adjustments", "result_overrides"})
                self.assertTrue(needs_synthesis_review("RESULT 怎么计算？", "结果等于基础值乘以2。",
                    investigation, source_pages=pages, answer_detail=detail))
                self.assertEqual(review["semantic_verification"], "unverified")

    def test_else_zero_cannot_hide_an_independent_final_override(self):
        investigation, pages = self.override_material()
        answer = "基础值大于零时乘以2，否则归零。"
        review = assess_business_answer("RESULT 怎么计算？", answer, investigation, pages)
        self.assertEqual(review["missing_aspects"], ["result_overrides"])
        requirement = next(item for item in answer_requirements("RESULT 怎么计算？", investigation, pages)
                           if item["kind"] == "result_adjustments")
        self.assertEqual(requirement["coverage_hints"][0]["target_field"], "RESULT")
        self.assertEqual(requirement["coverage_hints"][0]["value_markers"], ["19"])
        complete = answer + "随后复核标志为Y时改为19。"
        self.assertEqual(assess_business_answer("RESULT 怎么计算？", complete,
                                                investigation, pages)["missing_aspects"], [])

    def test_unconditional_formula_has_no_invented_exception_despite_adjustment_count(self):
        investigation, pages = self.material("COMPUTE RESULT = BASIS * 2.")
        for detail in ("detailed", "brief"):
            with self.subTest(detail=detail):
                requirements = answer_requirements("RESULT 怎么计算？", investigation, pages,
                                                    answer_detail=detail)
                self.assertEqual([item["kind"] for item in requirements], ["formula"])
                self.assertFalse(needs_synthesis_review("RESULT 怎么计算？", "结果为基础值的两倍。",
                    investigation, source_pages=pages, answer_detail=detail))

    def test_rounding_is_independent_of_an_else_explanation(self):
        investigation, pages = self.material("IF BASIS > ZERO\n"
            "COMPUTE RESULT ROUNDED = BASIS / 3\nELSE\nMOVE ZERO TO RESULT\nEND-IF.")
        answer = "基础值大于零时除以3，否则归零。"
        review = assess_business_answer("RESULT 怎么计算？", answer, investigation, pages)
        self.assertEqual(review["missing_aspects"], ["rounding"])
        complete = "基础值大于零时除以3并舍入，否则归零。"
        self.assertEqual(assess_business_answer("RESULT 怎么计算？", complete,
                                                investigation, pages)["missing_aspects"], [])

    def test_unrelated_fields_comments_and_display_literals_do_not_add_requirements(self):
        investigation, pages = self.material("*> IF REVIEW-FLAG = 'Y' MOVE 19 TO RESULT\n"
            "DISPLAY 'IF BASIS > ZERO ELSE MOVE ZERO TO RESULT'.\n"
            "COMPUTE RESULT = BASIS * 2.\n"
            "IF REVIEW-FLAG = 'Y'\nCOMPUTE OTHER-RESULT ROUNDED = BASIS / 3\n"
            "ELSE\nMOVE ZERO TO OTHER-RESULT\nEND-IF.")
        requirements = answer_requirements("RESULT 怎么计算？", investigation, pages)
        self.assertEqual([item["kind"] for item in requirements], ["formula"])

    def test_unsupplied_override_does_not_become_an_answer_requirement(self):
        investigation, pages = self.material("COMPUTE RESULT = BASIS * 2.")
        pages.append({"evidence_id": "ev:unrelated", "relative_path": "other.cbl",
                      "source_text": "IF REVIEW-FLAG = 'Y'\nMOVE 19 TO RESULT\nEND-IF."})
        requirements = answer_requirements("RESULT 怎么计算？", investigation, pages)
        self.assertEqual([item["kind"] for item in requirements], ["formula"])

    def test_equal_in_a_formula_is_not_an_applicability_condition(self):
        investigation, pages = self.material("IF BASIS = ZERO\nCOMPUTE RESULT = BASIS * 2\nEND-IF.")
        for answer in ("结果等于基础值乘以2。", "結果等於基礎值乘以2。", "The result equals the basis times 2."):
            with self.subTest(answer=answer):
                review = assess_business_answer("RESULT 的计算条件是什么？", answer, investigation, pages)
                self.assertIn("conditions", review["missing_aspects"])
        answer = "基础值等于零时，结果等于基础值乘以2。"
        self.assertEqual(assess_business_answer("RESULT 的计算条件是什么？", answer,
                                                investigation, pages)["missing_aspects"], [])

    def test_chinese_expression_word_requests_a_concrete_formula(self):
        investigation, pages = self.material("COMPUTE RESULT = BASIS * 2.")
        review = assess_business_answer("RESULT 的算式是什么？", "由参数计算结果。", investigation, pages)
        self.assertEqual(review["missing_aspects"], ["formula"])

    def test_adjacent_pages_preserve_else_and_later_override_in_physical_order(self):
        investigation, _ = self.override_material()
        chunks = [(1, "IF BASIS > ZERO\nCOMPUTE RESULT = BASIS * 2"),
                  (3, "ELSE\nMOVE ZERO TO RESULT\nEND-IF."),
                  (6, "IF REVIEW-FLAG = 'Y'\nMOVE 19 TO RESULT\nEND-IF.")]
        pages = [{"evidence_id": f"ev:part-{start}", "relative_path": "rule.cbl",
                  "start_line": start, "end_line": start + len(text.splitlines()) - 1,
                  "source_text": text} for start, text in chunks]
        for item in investigation["required_items"]:
            item["evidence_ids"] = [page["evidence_id"] for page in pages]
        answer = "基础值大于零时乘以2，否则归零；随后复核标志为Y时改为19。"
        self.assertEqual(assess_business_answer("RESULT 怎么计算？", answer,
                                                investigation, pages)["missing_aspects"], [])
        self.assertEqual(assess_business_answer("RESULT 怎么计算？", "基础值大于零时乘以2，否则归零。",
                                                investigation, pages)["missing_aspects"], ["result_overrides"])

    def test_adjacent_unconditional_overwrite_is_not_lost_and_gaps_are_not_joined(self):
        investigation, pages = self.material("COMPUTE RESULT = BASIS * 2.")
        pages[0].update(start_line=1, end_line=1)
        pages.append({"evidence_id": "ev:override", "relative_path": "rule.cbl",
                      "start_line": 2, "end_line": 2, "source_text": "MOVE 99 TO RESULT."})
        for item in investigation["required_items"]:
            item["evidence_ids"].append("ev:override")
        review = assess_business_answer("RESULT 怎么计算？", "结果为基础值的两倍。", investigation, pages)
        self.assertIn("result_overrides", review["missing_aspects"])
        pages[1].update(start_line=100, end_line=100)
        requirements = answer_requirements("RESULT 怎么计算？", investigation, pages)
        self.assertEqual([item["kind"] for item in requirements], ["formula"])

    def test_cross_page_unrelated_result_and_literal_keywords_do_not_add_requirements(self):
        investigation, pages = self.material("COMPUTE RESULT = FUNCTION LENGTH('ROUNDED GIVING').")
        pages.append({"evidence_id": "ev:other", "relative_path": "other.cbl",
                      "source_text": "IF OTHER-FLAG = 1\nCOMPUTE OTHER-RESULT = BASIS * 2\n"
                                     "ELSE\nMOVE ZERO TO OTHER-RESULT\nEND-IF."})
        for item in investigation["required_items"]:
            item["evidence_ids"].append("ev:other")
        self.assertEqual([item["kind"] for item in answer_requirements("RESULT 怎么计算？", investigation, pages)],
                         ["formula"])

    def test_input_name_does_not_filter_out_the_arithmetic_result(self):
        investigation, pages = self.material("MOVE 11 TO BASIS.\nIF BASIS > ZERO\n"
            "COMPUTE RESULT = BASIS * 2\nELSE\nMOVE ZERO TO RESULT\nEND-IF.\nMOVE 19 TO RESULT.")
        for question in ("BASIS 怎么参与计算？", "RESULT 怎么计算？"):
            with self.subTest(question=question):
                review = assess_business_answer(question, "结果为基础值的两倍。", investigation, pages)
                self.assertIn("conditions", review["missing_aspects"])
                self.assertIn("result_overrides", review["missing_aspects"])

    def test_move_result_is_not_filtered_by_unrelated_arithmetic(self):
        investigation, pages = self.material("COMPUTE OTHER-RESULT = 2 + 1.\nIF BASIS > ZERO\n"
                                            "MOVE BASIS TO RESULT\nELSE\nMOVE ZERO TO RESULT\nEND-IF.")
        review = assess_business_answer("RESULT 怎么计算？", "结果取自基础值。", investigation, pages)
        self.assertIn("conditions", review["missing_aspects"])
        self.assertIn("result_adjustments", review["missing_aspects"])

    def test_program_question_does_not_require_every_intermediate_counter_overwrite(self):
        investigation, pages = self.material("ADD 1 TO WORK-COUNT.\nADD 2 TO WORK-COUNT.\n"
            "IF BASIS > ZERO\nCOMPUTE RESULT = BASIS * 2\nELSE\nMOVE ZERO TO RESULT\nEND-IF.")
        answer = "基础值大于零时结果为两倍，否则归零。"
        review = assess_business_answer("程序最终金额怎么计算？", answer, investigation, pages)
        self.assertEqual(review["missing_aspects"], [])

    def test_zero_and_equivalent_number_spellings_cover_visible_overrides(self):
        for source_value, answer in (("ZERO", "基础值乘以2后，最终金额归零。"),
                                     ("10.0", "基础值乘以2后，最终金额改为10。"),
                                     ("-10", "基础值乘以2后，最终金额改为负十。")):
            with self.subTest(source_value=source_value):
                investigation, pages = self.material(f"COMPUTE RESULT = BASIS * 2.\nMOVE {source_value} TO RESULT.")
                self.assertEqual(assess_business_answer("RESULT 怎么计算？", answer,
                                                        investigation, pages)["missing_aspects"], [])
        investigation, pages = self.material("COMPUTE RESULT = BASIS * 2.\nCOMPUTE RESULT = BASIS * 2 + 1.")
        answer = "最终结果是基础值乘以2再加一。"
        self.assertEqual(assess_business_answer("RESULT 怎么计算？", answer,
                                                investigation, pages)["missing_aspects"], [])


if __name__ == "__main__":
    unittest.main()
