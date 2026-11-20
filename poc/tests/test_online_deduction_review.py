"""Ordinary online investigations retain review boundaries across model rounds."""
from copy import deepcopy
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import test_online_automatic_source_search as fixture
import test_online_investigation as scenario
from online_deduction_review import review_covers_answer


class OnlineDeductionReviewTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.AutomaticSourceSearchTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.run_question = self.case.case.run_question

    @staticmethod
    def reviewed(context):
        ids = {p["citation"]["path"]: p["id"] for p in context["source_context"]}
        steps = [
            {"applies": "类型匹配才减准备金", "skips": "类型不匹配就跳过这笔", "effect": "剩余应收减少准备金金额",
             "citations": [ids["cash.cbl"]], "source_line": "SUBTRACT CREDIT-AMT FROM NET-DUE"},
            {"applies": "应收为正且退款余额严格小于应收", "skips": "相等或更大时跳过退款", "effect": "应收再减退款余额",
             "citations": [ids["cash.cbl"], ids["return.cbl"]], "source_line": "IF REFUND-BALANCE < DUE"},
        ]
        outcome = "最终应收为起始应收减去实际生效的扣减额"
        answer = "\n".join("，".join(s[k] for k in ("applies", "skips", "effect")) + " " +
                           " ".join("[" + i + "]" for i in s["citations"]) for s in steps)
        response = scenario.answer(context, answer + "\n" + outcome)
        response["deduction_review"] = {"steps": steps, "result": outcome, "result_citations": list(ids.values()),
                                        "result_source_line": "SUBTRACT REFUND-BALANCE FROM DUE"}
        return response

    def test_cited_checklist_must_describe_the_answer_delivered_to_the_reader(self):
        def wrong(context):
            response = self.reviewed(context)
            response["answer"] = scenario.answer(context, "先扣准备金，再处理退款。")["answer"]
            self.assertFalse(review_covers_answer(response, context["source_context"]))
            return response

        def corrected(context):
            self.assertEqual(context["observations"][-1]["result"]["issue"], "deduction_steps_incomplete")
            return self.reviewed(context)

        result = self.run_question([fixture.missing, wrong, corrected], question=self.case.question,
                                   answer_instructions="使用业务人员熟悉的措辞")
        self.assertEqual((result["status"], result["model_calls"]), ("answered", 3))
        self.assertIn("相等或更大时跳过退款", result["answer"])
        self.assertNotIn("deduction_review", result["answer"])

    def test_formatted_answer_cannot_omit_second_deduction_by_citing_both_programs(self):
        def incomplete(context):
            response = self.reviewed(context)
            review = response["deduction_review"]
            first = review["steps"][0]
            first["citations"] = list(response["citations"])
            review["steps"] = [first]
            review["result"] = "最终处理完毕"
            review["result_source_line"] = first["source_line"]
            response["answer"] = scenario.answer(context, "，".join(first[k] for k in
                ("applies", "skips", "effect")) + "。" + review["result"])["answer"]
            return response

        def correct(context):
            self.assertEqual(context["observations"][-1]["result"]["issue"], "deduction_steps_incomplete")
            return self.reviewed(context)

        output = self.run_question([fixture.missing, incomplete, correct], question=self.case.question,
                                   answer_instructions="用简短段落回答")
        self.assertEqual((output["status"], output["model_calls"]), ("answered", 3))
        self.assertIn("相等或更大时跳过退款", output["answer"])

    def test_step_anchors_accept_overlapping_windows_but_reject_a_lookalike_file(self):
        def inspect(context):
            response = self.reviewed(context)
            by_path = {p["citation"]["path"]: p for p in context["source_context"]}
            caller, worker = by_path["cash.cbl"], by_path["return.cbl"]
            closed = {"citations": [caller["id"], worker["id"]]}
            window = deepcopy(worker)
            window.update(id="E" + "a" * 24, text=worker["text"].splitlines()[3], complete_line_count=1)
            window["citation"].update(start=4, end=4)
            noise = deepcopy(window)
            noise.update(id="E" + "c" * 24)
            noise["citation"].update(path="unrelated.cbl", hash="4" * 64)
            pages = [worker, noise, caller, window]
            review = response["deduction_review"]
            response["citations"] += [noise["id"], window["id"]]
            second = review["steps"][1]
            second["citations"] = [window["id"]]
            self.assertTrue(review_covers_answer(response, pages, closed=closed))
            second["citations"] = [noise["id"]]
            self.assertFalse(review_covers_answer(response, pages, closed=closed))

            second["citations"] = [worker["id"]]
            review["result_source_line"] = review["steps"][0]["source_line"]
            self.assertFalse(review_covers_answer(response, pages, closed=closed))
            return self.reviewed(context)

        output = self.run_question([fixture.missing, inspect], question=self.case.question,
                                   answer_instructions="Explain in prose")
        self.assertEqual(output["status"], "answered")

    def test_unresolved_branches_continue_in_same_turn_with_verified_interim_visible(self):
        def partial(context):
            return scenario.answer(context, "已定位准备金扣减。", complete=False, remaining=["核对退款条件"])

        result = self.run_question([fixture.missing, partial, self.case.grounded], question=self.case.question)
        self.assertEqual((result["status"], result["model_calls"]), ("answered", 3))
        self.assertTrue(any(event["checkpoint"].get("progressive_answer", {}).get("remaining") == ["核对退款条件"]
                            for event in self.case.case.events if event["checkpoint"].get("progressive_answer")))

    def test_repeated_incomplete_final_has_visible_evidence_but_is_not_marked_complete(self):
        result = self.run_question([fixture.missing, self.case.grounded, self.case.grounded],
                                   question=self.case.question, answer_instructions="请逐步说明")
        self.assertEqual((result["status"], result["model_calls"]), ("needs_continuation", 3))
        self.assertTrue(result["checkpoint"]["progressive_answer"]["citations"])
        self.assertTrue(result["remaining"])

    def test_source_derived_complete_result_corrects_a_contradictory_self_review(self):
        def wrong(context):
            response = self.reviewed(context)
            original = response["deduction_review"]["steps"][1]
            for key, replacement in (("applies", "退款余额小于或等于应收"), ("skips", "只有余额更大才跳过")):
                response["answer"] = response["answer"].replace(original[key], replacement)
                original[key] = replacement
            self.assertTrue(review_covers_answer(response, context["source_context"]))
            return response

        result = self.run_question([fixture.missing, wrong], question=self.case.question)
        self.assertEqual(result["status"], "answered")
        self.assertNotIn("只有余额更大才跳过", result["answer"])
        self.assertIn("大于或等于", result["answer"])

    def test_mid_procedure_progress_shows_only_the_exact_cited_source_line(self):
        for quote, visible in (("IF NET-DUE > 0", True), ("IF NET-DUE >= 0", False)):
            with self.subTest(quote=quote):
                def select(context):
                    page = context["source_context"][0]
                    return {"actions": [scenario.read("return.cbl", 3, 7)],
                            "finding_quotes": [{"id": page["id"], "quote": quote}],
                            "remaining": ["核对退款程序"]}

                result = self.run_question([{"actions": [scenario.read("cash.cbl", 7, 9)]}, select],
                                           question=self.case.question, max_model_calls=2)
                preview = result["checkpoint"]["progressive_answer"]
                self.assertEqual(bool(preview), visible)
                if visible:
                    self.assertIn(quote, preview["answer"])
                    self.assertEqual(preview["remaining"], ["核对退款程序"])
