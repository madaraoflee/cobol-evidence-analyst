"""Offline regression coverage for stopping after provider HTTP failures."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import APIClientError, CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


class BusinessChatHTTPRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        (self.source / "rule.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. ORDER-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 RESULT-AMOUNT PIC 9(7).\n"
            "PROCEDURE DIVISION.\nMAIN.\nCOMPUTE RESULT-AMOUNT = 10 * 2.\nGOBACK.\n",
            encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", quiet=True,
                             framework_reference_path="")
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model",
                                       api_key="synthetic-key", max_output_tokens=2048)
        self.requests = []

    @staticmethod
    def answer(payload):
        page = next(page for page in payload["source_context"][0]["pages"]
                    if "COMPUTE RESULT-AMOUNT" in page["source_text"])
        return f"结果金额等于 10 乘以 2，得到 20。[{page['evidence_id']}]"

    def ask(self, script, *, limit=5, revisions=0):
        self.requests = []

        def transport(request):
            self.requests.append(request)
            self.assertLessEqual(len(self.requests), len(script), "provider failures must not retry")
            response = script[len(self.requests) - 1]
            if isinstance(response, BaseException):
                raise response
            if isinstance(response, TransportResponse):
                return response
            if isinstance(response, int):
                return TransportResponse(response, '{"error":{"message":"synthetic gateway failure"}}')
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            text = response(payload) if callable(response) else response
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}},
                ensure_ascii=False))

        output = run_business_chat("ORDER-RULE 的 RESULT-AMOUNT 怎么计算？", self.database,
            self.source, self.config, transport=transport, framework_reference_path="",
            policy=AgentPolicy(max_model_requests=limit, max_answer_revisions=revisions))
        self.result = output["agent_result"]
        self.trace = json.loads(Path(self.result["metrics"]["quality_trace_path"]).read_text(encoding="utf-8"))
        return self.result

    def assert_request_accounting(self, count):
        self.assertEqual(len(self.requests), count)
        self.assertEqual(self.result["metrics"]["model_requests"], count)
        self.assertEqual(self.result["model_turns"], count)
        self.assertEqual(len(self.result["metrics"]["request_bytes"]), count)
        self.assertEqual(len(self.trace["rounds"]), count)
        self.assertEqual(self.result["metrics"]["provider_retries"], [])
        self.assertTrue(all(json.loads(request.body)["max_tokens"] == 2048 for request in self.requests))

    def test_server_errors_stop_without_automatic_resend(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                result = self.ask([status], limit=5)
                self.assert_request_accounting(1)
                self.assertEqual(result["status"], "ABSTAINED")
                self.assertFalse(result["model_answer_recorded"])
                self.assertEqual(result["diagnostics"][-1]["http_status"], status)
                self.assertEqual(self.trace["rounds"][0]["response"]["error"], "HTTP_ERROR")
                self.assertNotIn("synthetic gateway failure", result["answer"])

    def test_other_http_statuses_also_stop_without_resend(self):
        for status in (400, 401, 403, 404, 408, 429, 501, 505):
            with self.subTest(status=status):
                result = self.ask([status])
                self.assert_request_accounting(1)
                self.assertEqual(result["diagnostics"][-1]["http_status"], status)

    def test_single_request_budget_records_failed_request_once(self):
        result = self.ask([503], limit=1)
        self.assert_request_accounting(1)
        self.assertEqual(result["diagnostics"][-1]["http_status"], 503)

    def test_timeout_and_non_http_errors_do_not_retry(self):
        for error in (APIClientError("REQUEST_TIMEOUT"), APIClientError("TRANSPORT_ERROR"),
                      APIClientError("INVALID_JSON_RESPONSE", http_status=500)):
            with self.subTest(code=error.code):
                result = self.ask([error])
                self.assert_request_accounting(1)
                self.assertEqual(result["diagnostics"][-1]["code"], error.code)

    def test_user_can_start_a_new_request_after_server_failure(self):
        self.ask([500])
        self.assert_request_accounting(1)
        result = self.ask([self.answer])
        self.assert_request_accounting(1)
        self.assertTrue(result["model_answer_recorded"])
        self.assertFalse(result["diagnostics"])
        self.assertEqual(self.trace["final"]["final_answer_round_id"], "round-1")

    def test_failure_after_investigation_does_not_trigger_resend(self):
        result = self.ask(['{"search":["RESULT-AMOUNT"]}', 503])
        self.assert_request_accounting(2)
        self.assertEqual(result["metrics"]["tool_calls"]["search"], 1)
        self.assertEqual(result["diagnostics"][-1]["http_status"], 503)
        self.assertFalse(result["model_answer_recorded"])

    def test_continuation_counts_each_send_once_and_preserves_its_budget(self):
        truncated = TransportResponse(200, json.dumps({"choices": [{"message": {
            "role": "assistant", "content": "结果金额依次计算，剩余条件尚未写完。"},
            "finish_reason": "length"}]}))
        for response in (self.answer, 503, truncated):
            with self.subTest(response=response):
                result = self.ask([truncated, response], limit=4)
                self.assert_request_accounting(2)
                self.assertTrue(result["continuation_attempted"])
                self.assertTrue(result["model_answer_recorded"])
                self.assertEqual(self.trace["rounds"][1]["stage"], "continue")
                self.assertEqual(len(self.trace["question_investigation_rounds"]), 2)
                continuation = json.loads(json.loads(self.requests[1].body)["messages"][-1]["content"])
                self.assertEqual(continuation["investigation_budget"]["remaining_model_requests"], 3)
                expected_round = "round-1" if response == 503 else "round-2"
                self.assertEqual(self.trace["final"]["final_answer_round_id"], expected_round)
        result = self.ask([truncated], limit=1)
        self.assert_request_accounting(1)
        self.assertFalse(result["continuation_attempted"])
        self.assertTrue(result["model_answer_recorded"])

    def test_failed_revision_keeps_draft_and_original_citation_round(self):
        with mock.patch("business_chat.needs_synthesis_review", return_value=True):
            result = self.ask([self.answer, 502], revisions=1)
        self.assert_request_accounting(2)
        self.assertEqual(self.trace["final"]["final_answer_round_id"], "round-1")
        self.assertTrue(result["model_answer_recorded"])
        self.assertIn("得到 20", result["answer"])
        self.assertTrue(result["narrative"]["citations"])
        visible = {source["evidence_id"] for source in self.trace["rounds"][0]["sources"]}
        self.assertLessEqual({ref["evidence_id"] for ref in result["narrative"]["citations"]}, visible)
        self.assertTrue(any(boundary.get("reason") == "usable_draft_retained" for boundary in result["boundaries"]))
        self.assertEqual(result["diagnostics"][-1]["http_status"], 502)


if __name__ == "__main__":
    unittest.main()
