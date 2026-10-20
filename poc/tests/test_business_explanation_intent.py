"""Offline routing and real chat-path checks of business explanation requests."""

import hashlib
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
from business_synthesis import (assess_business_answer, build_analysis_brief,
    needs_synthesis_review, wants_business_overview)
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


SOURCE = """*> Purpose: query the available balance for one account.
IDENTIFICATION DIVISION.
PROGRAM-ID. BALANCE-QUERY.
DATA DIVISION.
LINKAGE SECTION.
01 ACCOUNT-ID PIC X(8).
01 RESULT-AMOUNT PIC 9(6).
PROCEDURE DIVISION USING ACCOUNT-ID RESULT-AMOUNT.
EXEC SQL
 SELECT AVAILABLE_BALANCE INTO :RESULT-AMOUNT
 FROM ACCOUNT_BALANCES WHERE ACCOUNT_ID = :ACCOUNT-ID
END-EXEC.
GOBACK.
"""
QUESTION = "帮我分析这个程序 balance.cbl"
INVENTORY = "`BALANCE-QUERY` 程序。读取 `ACCOUNT_BALANCES`；设置 `RESULT-AMOUNT`。"
EXPLANATION = (
    "这项功能按给定账号查询可用余额，把查询到的金额交给调用方。"
    "调用方提供账号，账户余额表提供查询结果，余额参数承接输出；"
    "这一过程只读取账户数据，不修改账户余额。"
    "原文没有给出业务上的余额口径，也没有说明查询失败时的返回约定。"
)


def page(text=SOURCE):
    return {"evidence_id": "ev_balance", "relative_path": "balance.cbl",
        "source_text": text, "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "start_line": 1, "end_line": len(text.splitlines()), "format_hint": "free"}


class BusinessExplanationIntentTests(unittest.TestCase):
    def test_common_program_and_newcomer_requests_receive_source_bound_orientation(self):
        for question in (
                "帮我分析这个程序", "这个程序是干嘛的？",
                "请用业务语言给我解释一下这段程序", "我是新人，讲讲 BALANCEQ",
                "请用业务语言解释这个程序，不要逐行翻译源码。",
                "介紹這個程式", "Please explain this program.",
                "Explain this program without line-by-line details.",
                "As a newcomer, explain BALANCEQ."):
            with self.subTest(question=question):
                self.assertTrue(wants_business_overview(question))
                brief = build_analysis_brief(question, {}, [page()], [], 4096)
                self.assertEqual(brief["business_explanation_contract"]["mode"], "business_overview")
                self.assertIn("business_purpose", brief["business_orientation"]["available_facets"])
                self.assertFalse(brief["business_explanation_contract"]["semantic_verification"])

    def test_narrow_and_explicit_implementation_questions_keep_direct_answers(self):
        for question in (
                "请用业务语言解释这个程序的金额计算公式",
                "我是新人，解释这个程序 O-K 字段的取值",
                "帮我分析这个程序的输入参数是什么",
                "帮我分析这个程序调用了谁", "请解释这个程序如何调用 FEE-RULE",
                "这个程序被哪些程序调用？", "请逐行解释这个程序", "请分析程序的代码实现",
                "逐句解释程序的功能", "Explain this program line by line.",
                "Explain this program's return value.", "Which programs call it?",
                "Explain the program's call graph.", "Describe the program call relationships.",
                "Explain when the program calls FEE-RULE.",
                "请简短解释这个程序。", "请说明业务流程，不用展开"):
            with self.subTest(question=question):
                self.assertFalse(wants_business_overview(question))
                brief = build_analysis_brief(question, {}, [page()], [], 4096)
                self.assertEqual(brief["business_explanation_contract"]["mode"], "direct_answer")
                self.assertNotIn("business_orientation", brief)

    def test_generic_completion_words_cannot_rescue_a_technical_inventory(self):
        for ending in ("这样就完成了处理。", "这是一个完整的处理流程。",
                       "上述操作已经全部执行完毕。", "This completes all the operations."):
            with self.subTest(ending=ending):
                answer = INVENTORY + ending + "[ev_balance]"
                assessment = assess_business_answer(QUESTION, answer, {}, [page()])
                self.assertEqual(assessment["status"], "incomplete")
                self.assertEqual(set(assessment["business_explanation_review"]["missing_aspects"]),
                                 {"business_purpose", "program_roles", "data_roles"})
                self.assertTrue(needs_synthesis_review(QUESTION, answer, {}, source_pages=[page()],
                                                       source_available=True, assessment=assessment))

    def test_specific_or_unfamiliar_business_explanations_are_not_rejected(self):
        for explanation in (EXPLANATION, "这一步形成应收凭据，尚未过账。",
                            "完成了账户余额查询。", "Eligible requests become bill records."):
            with self.subTest(explanation=explanation):
                assessment = assess_business_answer(QUESTION, explanation + "[ev_balance]", {}, [page()])
                self.assertFalse(assessment["business_explanation_review"]["technical_inventory_only"])
                self.assertEqual(assessment["missing_aspects"], [])
                self.assertFalse(needs_synthesis_review(QUESTION, explanation, {}, source_pages=[page()],
                                                        source_available=True, assessment=assessment))

    def test_overview_does_not_invent_a_purpose_from_an_opaque_program_name(self):
        source = SOURCE.replace(SOURCE.splitlines()[0] + "\n", "")
        brief = build_analysis_brief(QUESTION, {}, [page(source)], [], 4096)
        orientation = brief["business_orientation"]
        self.assertEqual(orientation["purpose_candidates"], [])
        self.assertNotIn("business_purpose", orientation["available_facets"])
        self.assertNotIn("business_purpose", [row["kind"] for row in brief["required_answer_aspects"]])

    def chat(self, *, technical_first):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "balance.cbl").write_bytes(SOURCE.encode("utf-8"))
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", verify_content=True)
            ensure_repository_search(database, source)
            config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model",
                                      api_key="offline-only")
            requests = []

            def transport(request):
                payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
                requests.append(payload)
                supplied = next(row for bundle in payload["source_context"] for row in bundle["pages"]
                                if row["relative_path"] == "balance.cbl")
                answer = (INVENTORY + "这样就完成了处理。" if technical_first and len(requests) == 1
                          else EXPLANATION)
                answer += f"[{supplied['evidence_id']}]"
                return TransportResponse(200, json.dumps({"choices": [{"message": {
                    "role": "assistant", "content": answer}, "finish_reason": "stop"}]}, ensure_ascii=False))

            with mock.patch.object(socket, "create_connection", side_effect=AssertionError("Offline test")), \
                 mock.patch.object(socket.socket, "connect", side_effect=AssertionError("Offline test")):
                output = run_business_chat(QUESTION, database, source, config, transport=transport,
                    framework_reference_path="", policy=AgentPolicy(max_model_requests=3, max_answer_revisions=1))
            return requests, output["agent_result"]

    def test_real_chat_revises_technical_draft_with_supplied_business_gaps(self):
        requests, result = self.chat(technical_first=True)
        self.assertEqual(len(requests), 2)
        self.assertIn("business_explanation", requests[0]["response_contract"])
        self.assertTrue(requests[0]["source_selection"]["source_supplied"])
        self.assertIn("这样就完成了处理", requests[1]["draft_answer"])
        self.assertTrue({"business_purpose", "program_roles", "data_roles"} <=
                        set(requests[1]["answer_review"]["missing_aspects"]))
        self.assertIn("按给定账号查询可用余额", result["answer"])
        self.assertNotIn("这样就完成了处理", result["answer"])
        self.assertTrue(result["business_review"]["synthesis_review_attempted"])

    def test_real_chat_delivers_clear_first_answer_without_an_extra_request(self):
        requests, result = self.chat(technical_first=False)
        self.assertEqual(len(requests), 1)
        self.assertIn("business_explanation", requests[0]["response_contract"])
        self.assertIn("按给定账号查询可用余额", result["answer"])
        self.assertFalse(result["business_review"]["synthesis_review_attempted"])


if __name__ == "__main__":
    unittest.main()
