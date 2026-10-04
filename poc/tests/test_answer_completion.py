"""Offline answer-completion contracts over real, fully supplied source.

Provider replies are fixed test inputs. These checks verify recovery, final
status and source delivery; they do not claim to test model understanding.
"""

from __future__ import annotations

import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search
from business_synthesis import assess_answer_completion, needs_synthesis_review


QUESTION = "rule.cbl 的 FINAL-AMOUNT 怎么计算？"
BLANKET_LIMITATION = (
    "需要核对源码中的业务字段和计算公式，目前不足以确认 FINAL-AMOUNT 的计算口径。"
)
BUSINESS_ANSWER = (
    "基础值大于零时，最终金额为基础值乘以系数；否则最终金额为零。"
    "当前初始值是 8 和 1.25，因此正数分支的金额为 10。"
)
SOURCE_MARKERS = (
    "01 BASIS PIC S9(5) VALUE 8.",
    "01 FACTOR PIC 9V99 VALUE 1.25.",
    "IF BASIS > ZERO",
    "COMPUTE FINAL-AMOUNT = BASIS * FACTOR",
    "MOVE ZERO TO FINAL-AMOUNT",
    "GOBACK.",
)


class AnswerCompletionTests(unittest.TestCase):
    def test_conditional_uncertainty_is_not_itself_an_unfinished_answer(self):
        for answer in ("若资料不足以确认身份则拒绝申请。",
                       "If the evidence is insufficient to confirm identity the request is rejected."):
            with self.subTest(answer=answer):
                self.assertNotEqual(assess_answer_completion(answer)["status"], "incomplete")
                self.assertFalse(needs_synthesis_review("何时拒绝申请？", answer, {}, source_available=True))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="answer-completion-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "synthetic-model",
                                       api_key="synthetic-only")
        self.requests = []
        self.network_attempts = []

        def forbidden(*args, **kwargs):
            self.network_attempts.append("connection")
            raise AssertionError("network forbidden in answer-completion regression")

        for target, attribute in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                                  (socket, "create_connection")):
            patcher = mock.patch.object(target, attribute, side_effect=forbidden)
            patcher.start()
            self.addCleanup(patcher.stop)

    def build_source(self, *, external_call=False):
        tail = "CALL 'EXTERNAL-STEP' USING FINAL-AMOUNT.\n" if external_call else ""
        (self.source / "rule.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. FINAL-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 BASIS PIC S9(5) VALUE 8.\n"
            "01 FACTOR PIC 9V99 VALUE 1.25.\n"
            "01 FINAL-AMOUNT PIC 9(7)V99.\n"
            "PROCEDURE DIVISION.\nMAIN.\n"
            "IF BASIS > ZERO\nCOMPUTE FINAL-AMOUNT = BASIS * FACTOR\n"
            "ELSE\nMOVE ZERO TO FINAL-AMOUNT\nEND-IF.\n" + tail + "GOBACK.\n",
            encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)

    def ask(self, replies, *, question=QUESTION, policy=None):
        self.requests = []
        policy = policy or AgentPolicy(max_model_requests=3)

        def transport(request):
            envelope = json.loads(request.body)
            payload = json.loads(envelope["messages"][-1]["content"])
            self.requests.append(payload)
            reply = replies[min(len(self.requests) - 1, len(replies) - 1)]
            if callable(reply):
                reply = reply(payload)
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": reply}, "finish_reason": "stop"}]},
                ensure_ascii=False))

        output = run_business_chat(question, self.database, self.source, self.config,
            transport=transport, allow_network=False, framework_reference_path="", policy=policy)
        self.assertEqual(self.network_attempts, [])
        self.assertLessEqual(len(self.requests), policy.max_model_requests)
        self.assertEqual(output["agent_result"]["metrics"]["model_requests"], len(self.requests))
        return output

    def assert_complete_source_supplied(self):
        first = self.requests[0]
        bundle = first["source_context"][0]
        supplied = "\n".join(page["source_text"] for page in bundle["pages"])
        for marker in SOURCE_MARKERS:
            self.assertIn(marker, supplied)
        self.assertTrue(bundle["working_set"]["physical_complete"])
        self.assertTrue(bundle["working_set"]["closure_complete"])
        self.assertTrue(first["question_investigation"]["can_answer"])
        items = {item["kind"]: item["status"]
                 for item in first["question_investigation"]["required_items"]}
        for kind in ("formula", "inputs", "conditions", "result_adjustments"):
            self.assertEqual(items[kind], "SATISFIED")

    def assert_not_successful_answer(self, output):
        result = output["agent_result"]
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["stop_reason"], "answer_incomplete")
        self.assertEqual(result["business_review"]["answer_completion"]["status"], "incomplete")
        self.assertNotEqual(output["reason_code"], "BUSINESS_CHAT_COMPLETED")

    def test_ordinary_question_reviews_uncited_blanket_limitation_with_complete_source(self):
        self.build_source()
        output = self.ask([BLANKET_LIMITATION, BUSINESS_ANSWER])
        self.assert_complete_source_supplied()
        result = output["agent_result"]
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["answer"], BUSINESS_ANSWER)
        self.assertEqual(result["status"], "ANALYZED")
        self.assertEqual(result["stop_reason"], "sufficient_material")
        self.assertTrue(result["business_review"]["synthesis_review_attempted"])
        self.assertEqual(result["narrative"]["citations"], [])

    def test_general_question_reviews_a_blanket_limitation_without_formula_requirements(self):
        self.build_source()
        answer = "该程序根据基础值和系数生成最终金额，非正数基础值的结果为零。"
        output = self.ask([BLANKET_LIMITATION, answer], question="rule.cbl 是做什么的？")
        self.assertEqual(self.requests[0]["question_investigation"]["required_items"], [])
        self.assertTrue(self.requests[0]["source_context"][0]["working_set"]["physical_complete"])
        self.assertEqual(output["agent_result"]["answer"], answer)
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(output["agent_result"]["business_review"]["synthesis_review_attempted"])

    def test_repeated_blanket_limitation_is_not_a_successful_answer(self):
        self.build_source()
        output = self.ask([BLANKET_LIMITATION])
        self.assert_complete_source_supplied()
        self.assertGreaterEqual(len(self.requests), 2)
        self.assert_not_successful_answer(output)
        self.assertEqual(output["agent_result"]["answer"], BLANKET_LIMITATION)

    def test_detailed_review_cannot_accept_the_same_blanket_limitation(self):
        self.build_source()
        output = self.ask([BLANKET_LIMITATION],
                          question="请详细解释 rule.cbl 的 FINAL-AMOUNT 怎么计算？")
        self.assert_complete_source_supplied()
        self.assertTrue(output["agent_result"]["business_review"]["synthesis_review_attempted"])
        self.assert_not_successful_answer(output)

    def test_detailed_vague_answer_reviews_the_supplied_but_unexplained_aspects(self):
        self.build_source()
        question = ("请详细解释 rule.cbl 的 FINAL-AMOUNT 如何计算，"
                    "包括输入来源、条件和不满足时的结果。")
        output = self.ask(["最终金额由基础值与系数计算。", BUSINESS_ANSWER], question=question)
        self.assert_complete_source_supplied()
        self.assertEqual(len(self.requests), 2)
        review = self.requests[1]["answer_review"]
        self.assertEqual(set(review["missing_aspects"]),
                         {"formula", "inputs", "conditions", "result_adjustments"})
        self.assertEqual(self.requests[1]["draft_answer"], "最终金额由基础值与系数计算。")
        result = output["agent_result"]
        self.assertEqual(result["answer"], BUSINESS_ANSWER)
        self.assertEqual(result["status"], "ANALYZED")
        self.assertTrue(result["business_review"]["synthesis_review_attempted"])
        self.assertEqual(result["business_review"]["answer_completion"]["missing_aspects"], [])
        self.assertFalse(result["claims_semantically_verified"])

    def test_partial_revision_cannot_discard_the_drafts_formula_and_inputs(self):
        self.build_source()
        def draft(payload):
            identifier = payload["source_context"][0]["pages"][0]["evidence_id"]
            return "基础值初始8、系数初始1.25，最终金额为基础值乘以系数。" + f"[{identifier}]"
        def revision(payload):
            identifier = payload["source_context"][0]["pages"][0]["evidence_id"]
            return "基础值大于零时走处理分支，否则最终金额为零。" + f"[{identifier}]"
        output = self.ask([draft, revision],
            question="请详细解释 rule.cbl 的 FINAL-AMOUNT 如何计算，包括输入来源、条件与其他分支。")
        result = output["agent_result"]
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0]["source_context"], self.requests[1]["source_context"])
        self.assertEqual(result["answer"], draft(self.requests[0]))
        self.assertEqual(set(result["business_review"]["answer_completion"]["missing_aspects"]),
                         {"conditions", "result_adjustments"})
        self.assertEqual(result["status"], "PARTIAL")
        self.assertTrue(any(item.get("revision_error") == "ANSWER_COVERAGE_REGRESSED"
                            for item in result["boundaries"]))
        trace = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text())
        self.assertEqual(trace["final"]["final_answer_round_id"], "round-1")
        self.assertEqual(trace["final"]["final_answer_round_ids"], ["round-1"])
        self.assertEqual([item["evidence_id"] for item in result["narrative"]["citations"]],
                         [self.requests[0]["source_context"][0]["pages"][0]["evidence_id"]])

    def test_partial_revision_can_fill_a_gap_without_losing_explained_aspects(self):
        self.build_source()
        draft = "基础值初始8、系数初始1.25，最终金额为基础值乘以系数。"
        revision = "基础值初始8、系数初始1.25；基础值大于零时，最终金额为基础值乘以系数。"
        result = self.ask([draft, revision],
            question="请详细解释 rule.cbl 的 FINAL-AMOUNT 如何计算，包括输入来源、条件与其他分支。")["agent_result"]
        self.assertEqual(result["answer"], revision)
        self.assertEqual(result["business_review"]["answer_completion"]["missing_aspects"],
                         ["result_adjustments"])
        self.assertFalse(any(item.get("reason") == "usable_draft_retained" for item in result["boundaries"]))

    def test_generic_calculation_answer_is_not_success_after_bounded_review(self):
        self.build_source()
        for answer in ("最终金额由基础值与系数计算。",
                       "最终金额就是程序的计算结果。",
                       "**最终金额**由基础值与系数计算。",
                       "[业务说明](https://offline.example.invalid/info/amount)中说明金额由参数计算。"):
            with self.subTest(answer=answer):
                output = self.ask([answer])
                self.assert_complete_source_supplied()
                self.assertEqual(len(self.requests), 2)
                self.assert_not_successful_answer(output)
                completion = output["agent_result"]["business_review"]["answer_completion"]
                self.assertEqual(completion["reason"], "supplied_business_aspects_unexplained")
                self.assertEqual(set(completion["missing_aspects"]),
                                 {"formula", "conditions", "result_adjustments"})
                trace = json.loads(Path(output["agent_result"]["metrics"]["quality_trace_path"])
                                   .read_text(encoding="utf-8"))
                self.assertEqual(set(trace["final"]["answer_completion"]["missing_aspects"]),
                                 {"formula", "conditions", "result_adjustments"})

    def test_explicit_sources_conditions_and_else_are_checked_without_detail_keyword(self):
        self.build_source()
        output = self.ask(["最终金额为基础值乘以系数。", BUSINESS_ANSWER],
            question="rule.cbl 的 FINAL-AMOUNT 怎么计算，输入来源、条件和不满足时结果是什么？")
        self.assert_complete_source_supplied()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(set(self.requests[1]["answer_review"]["missing_aspects"]),
                         {"inputs", "conditions", "result_adjustments"})
        self.assertEqual(output["agent_result"]["answer"], BUSINESS_ANSWER)

    def test_short_concrete_answers_need_no_word_count_or_extra_review(self):
        self.build_source()
        for answer in ("初始值8、1.25；正数基础值乘系数，否则归零。",
                       "基础值预设8、系数预设1.25；正数基础值乘系数，其他情况结果为0。",
                       "Initially 8 and 1.25; if the basis is positive, multiply by the factor; otherwise zero."):
            with self.subTest(answer=answer):
                output = self.ask([answer], question="请详细解释 rule.cbl 的 FINAL-AMOUNT 怎么计算？")
                self.assert_complete_source_supplied()
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(output["agent_result"]["status"], "ANALYZED")
                self.assertEqual(output["agent_result"]["business_review"]["answer_completion"]["missing_aspects"], [])

    def test_explicit_initial_values_cannot_be_replaced_by_formula_only(self):
        self.build_source()
        output = self.ask(["最终金额为基础值乘以系数。", BUSINESS_ANSWER],
            question="rule.cbl 的 FINAL-AMOUNT 怎么计算，BASIS 和 FACTOR 的初值分别是什么？")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(set(self.requests[1]["answer_review"]["missing_aspects"]),
                         {"inputs", "conditions", "result_adjustments"})
        self.assertEqual(output["agent_result"]["answer"], BUSINESS_ANSWER)

    def test_plain_addition_and_subtraction_are_valid_calculation_explanations(self):
        for operator, word in (("+", "加"), ("-", "减")):
            with self.subTest(operator=operator):
                self.build_source()
                path = self.source / "rule.cbl"
                path.write_text(path.read_text().replace("BASIS * FACTOR", f"BASIS {operator} FACTOR"))
                build_business_index(self.source, self.database, source_format="free", verify_content=True)
                ensure_repository_search(self.database, self.source)
                answer = f"基础值大于零时，最终金额为基础值{word}系数；否则归零。"
                output = self.ask([answer])
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(output["agent_result"]["status"], "ANALYZED")

    def test_budget_cannot_turn_unexplained_supplied_formula_into_success(self):
        self.build_source()
        for policy in (AgentPolicy(max_model_requests=1),
                       AgentPolicy(max_model_requests=3, max_answer_revisions=0)):
            with self.subTest(policy=policy.to_dict()):
                output = self.ask(["最终金额由基础值与系数计算。"], policy=policy)
                self.assert_complete_source_supplied()
                self.assertEqual(len(self.requests), 1)
                self.assert_not_successful_answer(output)

    def test_request_or_revision_budget_cannot_turn_deferral_into_success(self):
        self.build_source()
        policies = (
            AgentPolicy(max_model_requests=1),
            AgentPolicy(max_model_requests=3, max_answer_revisions=0),
        )
        for policy in policies:
            with self.subTest(policy=policy.to_dict()):
                output = self.ask([BLANKET_LIMITATION], policy=policy)
                self.assert_complete_source_supplied()
                self.assert_not_successful_answer(output)
                self.assertEqual(output["agent_result"]["answer"], BLANKET_LIMITATION)

    def test_review_that_loses_the_business_explanation_preserves_the_draft(self):
        self.build_source()
        drafts = []

        def first_reply(payload):
            identifier = payload["source_context"][0]["pages"][0]["evidence_id"]
            drafts.append("当前资料不足以完整解释输入来源。\n\n"
                          + BUSINESS_ANSWER + f"[{identifier}]")
            return drafts[0]

        output = self.ask([first_reply, BLANKET_LIMITATION],
                          question="请详细解释 rule.cbl 的 FINAL-AMOUNT 怎么计算？")
        self.assert_complete_source_supplied()
        self.assertEqual(len(self.requests), 2)
        result = output["agent_result"]
        self.assertEqual(result["answer"], drafts[0])
        self.assertTrue(result["model_answer_recorded"])
        self.assertEqual(result["narrative"]["citations"][0]["evidence_id"],
                         self.requests[0]["source_context"][0]["pages"][0]["evidence_id"])
        quality = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text(encoding="utf-8"))
        self.assertEqual(quality["final"]["final_answer_round_id"], "round-1")

    def test_traditional_and_english_limitations_recover_without_detail_keyword(self):
        self.build_source()
        limitations = (
            "目前資料不足以確認 FINAL-AMOUNT 的計算口徑，需要核對源碼中的業務欄位和公式。",
            "The supplied source is insufficient to determine how FINAL-AMOUNT is calculated. "
            "I need to check the business fields and formulas first.",
            "I need to check the business fields and formulas in the source before I can "
            "confirm how FINAL-AMOUNT is calculated.",
            "我需要先读取源码并计算金额后才能回答。",
            "I need to read the source and calculate the amount before I can answer.",
        )
        for limitation in limitations:
            with self.subTest(reply=limitation):
                output = self.ask([limitation, BUSINESS_ANSWER])
                self.assert_complete_source_supplied()
                self.assertEqual(output["agent_result"]["answer"], BUSINESS_ANSWER)
                self.assertEqual(len(self.requests), 2)

    def test_search_absence_messages_review_the_source_already_supplied(self):
        self.build_source()
        limitations = (
            "该程序处理最终金额。目前找不到完整计算口径。",
            "未找到 FINAL-AMOUNT 的计算公式。",
            "没找到 FINAL-AMOUNT 的计算规则。",
            "没有找到 FINAL-AMOUNT 的输入来源。",
            "目前找不到 FINAL-AMOUNT 的完整計算口徑。",
            "尚未找到 FINAL-AMOUNT 的計算公式。",
            "沒有找到相關源碼。",
            "I couldn't find the formula for FINAL-AMOUNT.",
            "I can’t find the source code for FINAL-AMOUNT.",
            "We could not locate the input source for FINAL-AMOUNT.",
            "No formula for FINAL-AMOUNT was found.",
            "No matching source code is available.",
        )
        for limitation in limitations:
            with self.subTest(reply=limitation):
                output = self.ask([limitation, BUSINESS_ANSWER])
                self.assert_complete_source_supplied()
                self.assertEqual(output["agent_result"]["answer"], BUSINESS_ANSWER)
                self.assertEqual(len(self.requests), 2)
                self.assertTrue(output["agent_result"]["business_review"]["synthesis_review_attempted"])

    def test_repeated_source_not_found_reply_does_not_complete_the_answer(self):
        self.build_source()
        output = self.ask(["未找到 FINAL-AMOUNT 的计算公式。"])
        self.assert_complete_source_supplied()
        self.assertGreaterEqual(len(self.requests), 2)
        self.assert_not_successful_answer(output)

    def test_missing_record_or_customer_is_a_business_branch(self):
        (self.source / "rule.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. RECORD-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 RECORD-FOUND PIC X VALUE 'N'.\n"
            "01 RESULT-FLAG PIC X.\nPROCEDURE DIVISION.\nMAIN.\n"
            "IF RECORD-FOUND = 'N'\nMOVE 'R' TO RESULT-FLAG\n"
            "DISPLAY '未找到记录'\nDISPLAY 'No customer was found'\n"
            "END-IF.\nGOBACK.\n", encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        answers = (
            "找不到记录时，程序将结果标志设为 R 并显示“未找到记录”。",
            "未找到客户时返回 R，程序显示“No customer was found”。",
            "If no record is found, the result flag is R and the program displays 'No customer was found'.",
        )
        for answer in answers:
            with self.subTest(reply=answer):
                output = self.ask([answer], question="rule.cbl 找不到记录时会怎样？")
                supplied = "\n".join(page["source_text"]
                                     for page in self.requests[0]["source_context"][0]["pages"])
                self.assertIn("IF RECORD-FOUND = 'N'", supplied)
                self.assertIn("DISPLAY '未找到记录'", supplied)
                self.assertEqual(output["agent_result"]["answer"], answer)
                self.assertEqual(len(self.requests), 1)
                self.assertFalse(output["agent_result"]["business_review"]["synthesis_review_attempted"])
                self.assertFalse(assess_answer_completion(answer)["limitation_detected"])

    def test_not_found_message_in_program_output_is_not_an_analysis_limitation(self):
        answer = "程序返回“找不到计算公式”并拒绝请求。"
        completion = assess_answer_completion(answer)
        self.assertNotEqual(completion["status"], "incomplete")
        self.assertFalse(completion["limitation_detected"])

    def test_missing_external_source_keeps_the_known_calculation(self):
        self.build_source(external_call=True)
        answer = BUSINESS_ANSWER + "\n\n未找到 EXTERNAL-STEP 的源码，调用后的金额取决于该外部步骤。"
        output = self.ask([answer])
        result = output["agent_result"]
        self.assertEqual(result["answer"], answer)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(len(self.requests), 1)
        self.assertNotEqual(result["business_review"]["answer_completion"]["status"], "incomplete")
        self.assertTrue(result["business_review"]["answer_completion"]["limitation_detected"])

    def test_condition_explanation_is_not_mistaken_for_investigation_deferral(self):
        self.build_source()
        explanations = (
            "需要先确认条件：基础值大于零时乘以系数，否则最终金额归零。",
            "需要先确认条件：仅基础值大于零时才按基础值乘以系数计算金额，否则归零。",
            "先核对条件：基础值大于零时，最终金额等于基础值乘以系数；否则归零。",
            "先读取参数，仅基础值大于零时按基础值乘以系数计算最终金额，否则归零。",
            "First check the condition: if the basis is positive, multiply it by the factor; "
            "otherwise set the final amount to zero.",
            "First read the parameters; if the basis is positive, multiply it by the factor; otherwise zero.",
        )
        for answer in explanations:
            with self.subTest(answer=answer):
                output = self.ask([answer])
                self.assert_complete_source_supplied()
                self.assertEqual(output["agent_result"]["answer"], answer)
                self.assertEqual(output["agent_result"]["status"], "ANALYZED")
                self.assertEqual(len(self.requests), 1)

    def test_insufficient_funds_rule_and_failure_message_are_business_answers(self):
        (self.source / "rule.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. REQUEST-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 AVAILABLE-AMOUNT PIC 9(5) VALUE 8.\n"
            "01 DUE-AMOUNT PIC 9(5) VALUE 10.\n"
            "01 RESULT-FLAG PIC X.\nPROCEDURE DIVISION.\nMAIN.\n"
            "IF AVAILABLE-AMOUNT < DUE-AMOUNT\nMOVE 'R' TO RESULT-FLAG\n"
            "DISPLAY 'Insufficient funds'\nDISPLAY '无法确认订单'\nEND-IF.\nGOBACK.\n",
            encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        answers = (
            "可用金额不足以支付应缴金额时申请被拒绝。",
            "Insufficient funds cause the request to be rejected.",
            "程序在可用金额低于应缴金额时输出“Insufficient funds”并拒绝申请。",
            "程序在可用金额低于应缴金额时输出“无法确认订单”并拒绝申请。",
        )
        for answer in answers:
            with self.subTest(answer=answer):
                output = self.ask([answer], question="rule.cbl 在什么条件下拒绝申请？")
                result = output["agent_result"]
                supplied = "\n".join(page["source_text"]
                                     for page in self.requests[0]["source_context"][0]["pages"])
                self.assertIn("IF AVAILABLE-AMOUNT < DUE-AMOUNT", supplied)
                self.assertIn("DISPLAY 'Insufficient funds'", supplied)
                self.assertEqual(result["answer"], answer)
                self.assertEqual(result["status"], "ANALYZED")
                self.assertNotEqual(result["business_review"]["answer_completion"]["status"], "incomplete")
                self.assertEqual(len(self.requests), 1)

    def test_specific_external_unknown_keeps_the_supplied_business_explanation(self):
        self.build_source(external_call=True)
        answer = (
            "调用外部步骤之前，基础值大于零时，最终金额为基础值乘以系数，否则为零。"
            "当前初始值 8 乘以 1.25，得到调用前的金额 10。\n\n"
            "随后将金额传入 EXTERNAL-STEP。其实现未提供，无法确认它是否会修改金额，"
            "因此返回后的金额尚不能确定。"
        )
        output = self.ask([answer])
        result = output["agent_result"]
        self.assertEqual(result["answer"], answer)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertTrue(result["model_answer_recorded"])
        self.assertEqual(len(self.requests), 1)
        self.assertNotEqual(result["business_review"]["answer_completion"]["status"], "incomplete")
        self.assertNotEqual(result["stop_reason"], "sufficient_material")


if __name__ == "__main__":
    unittest.main()
