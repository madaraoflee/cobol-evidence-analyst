"""Offline coverage checks for merged answer quality and detail preferences."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_synthesis import (answer_requirements, assess_business_answer,
                                build_analysis_brief, needs_synthesis_review)


class MergedAnswerPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.pages = [{"evidence_id": "ev:rule", "relative_path": "rule.cbl",
                       "source_text": "IF BASIS > ZERO\nCOMPUTE RESULT = BASIS * RATE\n"
                                      "ELSE\nMOVE ZERO TO RESULT\nEND-IF"}]
        self.investigation = {"required_items": [
            {"kind": kind, "status": "SATISFIED", "candidate_count": 1,
             "evidence_ids": ["ev:rule"]}
            for kind in ("formula", "inputs", "conditions", "result_adjustments")]}

    def test_brief_option_limits_implicit_detailed_calculation_requirements(self):
        question = "请详细解释最终金额怎么计算？"
        requirements = answer_requirements(question, self.investigation, self.pages, answer_detail="brief")
        self.assertEqual([item["kind"] for item in requirements], ["formula"])
        brief = build_analysis_brief(question, self.investigation, self.pages, [], 2048, answer_detail="brief")
        self.assertFalse(brief["detail_requested"])
        self.assertEqual(brief["required_answer_aspects"], requirements)

    def test_brief_option_reaches_assessment_and_synthesis_review(self):
        question, answer = "请详细解释最终金额怎么计算？", "最终金额为基础金额乘以系数。"
        review = assess_business_answer(question, answer, self.investigation, self.pages, answer_detail="brief")
        self.assertNotEqual(review["status"], "incomplete")
        self.assertEqual(review["missing_aspects"], [])
        self.assertFalse(needs_synthesis_review(question, answer, self.investigation,
            source_pages=self.pages, answer_detail="brief"))

    def test_default_detailed_accepts_concrete_short_answer(self):
        review = assess_business_answer("最终金额怎么计算？",
            "基础金额大于零时乘系数，否则归零。", self.investigation, self.pages)
        self.assertNotEqual(review["status"], "incomplete")
        self.assertEqual(review["required_aspects"], ["formula"])

    def test_explicit_detailed_request_keeps_supplied_aspect_checks(self):
        question = "请详细解释最终金额怎么计算？"
        review = assess_business_answer(question, "最终金额为基础金额乘以系数。",
                                        self.investigation, self.pages)
        self.assertEqual(review["status"], "incomplete")
        self.assertEqual(set(review["missing_aspects"]), {"inputs", "conditions", "result_adjustments"})
        self.assertTrue(needs_synthesis_review(question, "最终金额为基础金额乘以系数。",
            self.investigation, source_pages=self.pages))

    def test_brief_still_checks_explicitly_asked_conditions(self):
        review = assess_business_answer("最终金额怎么计算，条件和输入来源是什么？",
            "最终金额为基础金额乘以系数。", self.investigation, self.pages, answer_detail="brief")
        self.assertEqual(set(review["missing_aspects"]), {"inputs", "conditions"})
        self.assertEqual(review["status"], "incomplete")

    def test_short_answer_can_cover_all_explicit_detailed_aspects(self):
        review = assess_business_answer("请详细解释最终金额怎么计算？",
            "初始值8、1.25；正数基础值乘系数，否则归零。", self.investigation, self.pages)
        self.assertNotEqual(review["status"], "incomplete")
        self.assertEqual(review["missing_aspects"], [])

    def test_default_vague_calculation_still_gets_quality_review(self):
        answer = "最终金额由基础金额和系数计算。"
        review = assess_business_answer("最终金额怎么计算？", answer, self.investigation, self.pages)
        self.assertEqual(review["status"], "incomplete")
        self.assertEqual(review["missing_aspects"], ["formula"])
        self.assertTrue(needs_synthesis_review("最终金额怎么计算？", answer, self.investigation,
                                               source_pages=self.pages))


if __name__ == "__main__":
    unittest.main()
