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

    def test_condition_explanation_is_not_mistaken_for_investigation_deferral(self):
        self.build_source()
        explanations = (
            "需要先确认条件：基础值大于零时乘以系数，否则最终金额归零。",
            "需要先确认条件：仅基础值大于零时才按基础值乘以系数计算金额。",
            "先核对条件：基础值大于零时，最终金额等于基础值乘以系数；否则归零。",
            "先读取参数并按基础值乘以系数计算最终金额。",
            "First check the condition: if the basis is positive, multiply it by the factor; "
            "otherwise set the final amount to zero.",
            "First read the parameters and multiply the basis by the factor.",
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
