"""Execution questions keep source investigation without formula-only retries."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
from business_map import build_business_map
from business_synthesis import answer_requirements, assess_business_answer
from question_intent import calculation_intent
from question_investigation import build_question_investigation
from repository_discovery import ensure_repository_search


class CalculationQuestionIntentTests(unittest.TestCase):
    def test_execution_and_skip_questions(self):
        for question in (
            "后续基础计算和动态路由还会执行吗？",
            "校验失败后是否执行计算？",
            "计算会不会继续执行？",
            "后面的计算会执行吗？",
            "为什么校验出错就跳过计算？",
            "计算在哪种情况下会被跳过？",
            "後續基礎計算和動態路由還會執行嗎？",
            "校驗失敗後是否執行計算？",
            "Will the calculation still run after validation fails?",
            "Will the calculation be skipped?",
            "How to skip calculation when validation fails?",
            "校验失败后 calculation 是否继续执行？",
        ):
            with self.subTest(question=question):
                self.assertEqual(calculation_intent(question), "execution")

    def test_formula_and_mixed_questions_keep_calculation_rules(self):
        for question in (
            "RESULT 怎么计算？", "RESULT 怎麼計算？", "RESULT 的算式是什么？",
            "BASIS 怎么参与计算？", "RESULT 如何计算？", "程序的计算规则是什么？",
            "请详细解释计算流程。", "請完整說明計算過程。", "计算金额。",
            "How is the result calculated?", "How to calculate the amount?",
            "Explain the calculation.", "What is the calculation basis?",
            "Show the detailed calculation workflow.",
            "计算是否执行，具体公式是什么？", "是否执行计算，以及怎么算？",
            "後續計算還會執行嗎？請說明詳細計算流程。",
            "Will the calculation run, and how is the amount computed?",
            "Will the calculation be skipped? Explain its formula as well.",
            "是否跳过计算？需要时的计算方式又是什么？",
            "会不会继续计算？同时解释舍入规则。",
            "Explain calculation. Does the next step execute?",
            "金额是否计算正确？", "是否计算复利，还是按单利计算？",
            "金額是否計算正確？", "是否計算複利，還是按單利計算？",
            "Is the total calculated using a fixed rate or a percentage?",
            "Is the result computed by dividing the annual amount by 12?",
            "Is the amount still computed after validation fails?",
            "Does the program calculate after a validation error?",
        ):
            with self.subTest(question=question):
                self.assertEqual(calculation_intent(question), "rules")

    def test_other_questions_do_not_gain_calculation_obligations(self):
        for question in ("程序的业务规则是什么？", "是否执行后面的程序？",
                         "What is the field type?", "Why is this record skipped?"):
            with self.subTest(question=question):
                self.assertIsNone(calculation_intent(question))

    def test_actual_validation_question_does_not_require_unexecuted_formula_or_rounding(self):
        path = Path(__file__).resolve().parents[2] / "docs/eval/quality-challenges-v1/complex-main.json"
        cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
        question = next(case["question"] for case in cases
                        if case["id"] == "validation-priority-and-final-result")
        self.assertEqual(calculation_intent(question), "execution")
        pages = [{"evidence_id": "ev:calculation", "relative_path": "calculation.cbl",
                  "source_text": "IF PROCESS-STATUS = ZERO\n"
                    "COMPUTE RESULT ROUNDED = BASIS / 3\nELSE\nMOVE ZERO TO RESULT\nEND-IF."}]
        investigation = {"required_items": [
            {"kind": kind, "status": "SATISFIED", "candidate_count": 1,
             "evidence_ids": ["ev:calculation"]}
            for kind in ("formula", "conditions", "result_adjustments")]}
        requirements = answer_requirements(question, investigation, pages)
        self.assertNotIn("formula", {item["kind"] for item in requirements})
        self.assertFalse(any(item.get("coverage_hints") for item in requirements))
        review = assess_business_answer(question,
            "输入状态X先触发状态11和输入错误，后面的校验保留首个错误。"
            "后续基础计算和动态路由均跳过，结果映射返回该错误且两个金额为零。",
            investigation, pages)
        self.assertNotIn("formula", review["missing_aspects"])
        self.assertNotIn("rounding", review["missing_aspects"])


class ExecutionQuestionInvestigationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / "source"
        self.source.mkdir()
        self.database = Path(temporary.name) / "index.sqlite"
        self.text = (
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. VALUEFLOW.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 PROCESS-STATUS PIC 9 VALUE 1.\n"
            "01 BASIS PIC 9(7) VALUE 10.\n"
            "01 RESULT PIC 9(7)V99 VALUE ZERO.\n"
            "PROCEDURE DIVISION.\nMAIN.\n"
            "IF PROCESS-STATUS = ZERO\nCOMPUTE RESULT ROUNDED = BASIS / 3\n"
            "ELSE\nMOVE ZERO TO RESULT\nEND-IF.\nGOBACK.\n")
        (self.source / "flow.cbl").write_text(self.text, encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", quiet=True,
                             framework_reference_path="")
        ensure_repository_search(self.database, self.source)

    def investigate(self, question, complete):
        lines = self.text.splitlines()
        end = len(lines) if complete else 4
        page = {"relative_path": "flow.cbl", "start_line": 1, "end_line": end,
                "source_text": "\n".join(lines[:end]),
                "source_sha256": hashlib.sha256((self.source / "flow.cbl").read_bytes()).hexdigest(),
                "evidence_id": "ev:flow", "selection_reasons": ["question_match"]}
        result = build_question_investigation(question,
            build_business_map(self.database, self.source, question),
            database_path=self.database, source_pages=[page])
        return result, [page]

    def test_missing_execution_context_still_plans_source_investigation(self):
        for question in ("VALUEFLOW 出错后还会执行计算吗？",
                         "Will VALUEFLOW still run the calculation after an error?"):
            with self.subTest(question=question):
                result, _ = self.investigate(question, complete=False)
                steps = next(item for item in result["required_items"] if item["kind"] == "business_steps")
                formula = next(item for item in result["required_items"] if item["kind"] == "formula")
                self.assertGreater(steps["candidate_count"], 0)
                self.assertGreater(steps["missing_count"], 0)
                self.assertEqual(formula["status"], "NOT_APPLICABLE")
                self.assertFalse(result["can_answer"])
                self.assertTrue(any(action["reason"] == "business_steps_source_not_supplied"
                                    for action in result["planned_actions"]))

    def test_complete_execution_context_has_no_formula_or_rounding_obligation(self):
        question = "VALUEFLOW 出错后还会执行计算吗？"
        result, pages = self.investigate(question, complete=True)
        steps = next(item for item in result["required_items"] if item["kind"] == "business_steps")
        self.assertEqual(steps["status"], "SATISFIED")
        self.assertTrue(result["can_answer"])
        self.assertEqual(result["planned_actions"], [])
        requirements = answer_requirements(question, result, pages)
        self.assertNotIn("formula", {item["kind"] for item in requirements})
        self.assertFalse(any(item.get("coverage_hints") for item in requirements))

    def test_mixed_question_retains_formula_and_rounding_obligations(self):
        question = "VALUEFLOW 出错后还会执行计算吗，具体公式是什么？"
        result, pages = self.investigate(question, complete=True)
        formula = next(item for item in result["required_items"] if item["kind"] == "formula")
        self.assertEqual(formula["status"], "SATISFIED")
        review = assess_business_answer(question, "状态为零时才执行计算，否则结果归零。", result, pages)
        self.assertIn("formula", review["missing_aspects"])
        self.assertIn("rounding", review["missing_aspects"])


if __name__ == "__main__":
    unittest.main()
