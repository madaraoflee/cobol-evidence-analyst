"""Offline regression coverage for bounded retries of transient HTTP failures."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from analyze_source import AnalysisCancelled
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

    def ask(self, script, *, limit=5):
        self.requests = []

        def transport(request):
            self.requests.append(request)
            response = script[len(self.requests) - 1]
            if isinstance(response, BaseException):
                raise response
            if isinstance(response, int):
                return TransportResponse(response, '{"error":{"message":"synthetic gateway failure"}}')
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            text = response(payload) if callable(response) else response
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}},
                ensure_ascii=False))

        with mock.patch("business_chat.time.sleep") as sleep:
            output = run_business_chat("ORDER-RULE 的 RESULT-AMOUNT 怎么计算？", self.database,
                self.source, self.config, transport=transport, framework_reference_path="",
                policy=AgentPolicy(max_model_requests=limit))
        self.sleep_calls = sleep.call_count
        self.sleep_seconds = sum(call.args[0] for call in sleep.call_args_list)
        self.result = output["agent_result"]
        self.trace = json.loads(Path(self.result["metrics"]["quality_trace_path"]).read_text(encoding="utf-8"))
        return self.result

    def assert_request_accounting(self, count):
        self.assertEqual(len(self.requests), count)
        self.assertEqual(self.result["metrics"]["model_requests"], count)
        self.assertEqual(self.result["model_turns"], count)
        self.assertEqual(len(self.result["metrics"]["request_bytes"]), count)
        self.assertEqual(len(self.trace["rounds"]), count)
        self.assertTrue(all(json.loads(request.body)["max_tokens"] == 2048 for request in self.requests))

    def test_server_error_retries_identical_request_and_binds_answer_to_successful_round(self):
        result = self.ask([500, self.answer], limit=2)
        self.assert_request_accounting(2)
        self.assertAlmostEqual(self.sleep_seconds, 1.0)
        self.assertEqual(self.requests[0].body, self.requests[1].body)
        self.assertEqual(self.requests[0].timeout_seconds, self.requests[1].timeout_seconds)
        self.assertEqual(self.requests[0].url, self.requests[1].url)
        self.assertEqual(self.trace["rounds"][0]["request_body_sha256"],
                         self.trace["rounds"][1]["request_body_sha256"])
        self.assertEqual(self.trace["rounds"][0]["response"]["error"], "HTTP_ERROR")
        self.assertEqual(self.trace["final"]["final_answer_round_id"], "round-2")
        visible = {source["evidence_id"] for source in self.trace["rounds"][1]["sources"]}
        self.assertTrue(result["narrative"]["citations"])
        self.assertLessEqual({ref["evidence_id"] for ref in result["narrative"]["citations"]}, visible)
        self.assertFalse(result["diagnostics"])
        self.assertEqual(result["status"], "ANALYZED")
        retry = result["metrics"]["provider_retries"]
        self.assertEqual(len(retry), 1)
        self.assertEqual({key: retry[0][key] for key in
            ("failed_round_id", "retry_round_id", "http_status", "outcome", "max_output_tokens")},
            {"failed_round_id": "round-1", "retry_round_id": "round-2", "http_status": 500,
             "outcome": "recovered", "max_output_tokens": 2048})
        self.assertEqual(retry[0]["request_bytes"], len(self.requests[0].body))
        self.assertEqual(result["metrics"]["usage"]["status"], "partial")

    def test_repeated_server_error_stops_after_one_retry_for_the_whole_question(self):
        result = self.ask([500, 500], limit=5)
        self.assert_request_accounting(2)
        self.assertAlmostEqual(self.sleep_seconds, 1.0)
        self.assertEqual(result["status"], "ABSTAINED")
        self.assertEqual(result["metrics"]["provider_retries"][0]["outcome"], "failed")
        self.assertEqual(result["diagnostics"][-1]["http_status"], 500)
        self.assertNotIn("synthetic gateway failure", result["answer"])

    def test_single_request_budget_does_not_allow_retry(self):
        result = self.ask([503], limit=1)
        self.assert_request_accounting(1)
        self.assertEqual(self.sleep_calls, 0)
        self.assertEqual(result["metrics"]["provider_retries"], [])
        self.assertEqual(result["diagnostics"][-1]["http_status"], 503)

    def test_only_selected_server_statuses_are_retried(self):
        for status in (502, 503, 504):
            with self.subTest(status=status):
                result = self.ask([status, self.answer], limit=2)
                self.assert_request_accounting(2)
                self.assertEqual(result["metrics"]["provider_retries"][0]["http_status"], status)
                self.assertFalse(result["diagnostics"])
        for status in (400, 401, 403, 404, 408, 429, 501, 505):
            with self.subTest(status=status):
                result = self.ask([status], limit=5)
                self.assert_request_accounting(1)
                self.assertEqual(self.sleep_calls, 0)
                self.assertEqual(result["metrics"]["provider_retries"], [])
                self.assertEqual(result["diagnostics"][-1]["http_status"], status)

    def test_timeout_and_non_http_errors_do_not_retry(self):
        for error in (APIClientError("REQUEST_TIMEOUT"), APIClientError("TRANSPORT_ERROR"),
                      APIClientError("INVALID_JSON_RESPONSE", http_status=500)):
            with self.subTest(code=error.code):
                result = self.ask([error], limit=5)
                self.assert_request_accounting(1)
                self.assertEqual(self.sleep_calls, 0)
                self.assertEqual(result["metrics"]["provider_retries"], [])
                self.assertEqual(result["diagnostics"][-1]["code"], error.code)

    def test_tool_reply_in_last_retry_slot_cannot_trigger_further_investigation(self):
        result = self.ask([500, '{"search":["RESULT-AMOUNT"]}'], limit=2)
        self.assert_request_accounting(2)
        self.assertEqual(result["metrics"]["tool_calls"]["search"], 0)
        self.assertFalse(result["model_answer_recorded"])
        self.assertEqual(result["metrics"]["provider_retries"][0]["outcome"], "recovered")

    def test_retry_consumes_a_round_before_subsequent_tool_and_answer(self):
        result = self.ask([500, '{"search":["RESULT-AMOUNT"]}', self.answer], limit=3)
        self.assert_request_accounting(3)
        self.assertEqual(result["metrics"]["tool_calls"]["search"], 1)
        self.assertEqual(self.trace["final"]["final_answer_round_id"], "round-3")
        final_payload = json.loads(json.loads(self.requests[-1].body)["messages"][-1]["content"])
        self.assertEqual(final_payload["investigation_budget"]["remaining_model_requests"], 1)
        self.assertEqual(final_payload["investigation_budget"]["searches_per_turn"], 0)

    def test_retry_allowance_is_shared_after_success_and_followup_tool_request(self):
        result = self.ask([500, '{"search":["RESULT-AMOUNT"]}', 503], limit=5)
        self.assert_request_accounting(3)
        self.assertAlmostEqual(self.sleep_seconds, 1.0)
        self.assertEqual(len(result["metrics"]["provider_retries"]), 1)
        self.assertEqual(result["diagnostics"][-1]["http_status"], 503)

    def test_failed_revision_retry_keeps_draft_and_original_citation_round(self):
        with mock.patch("business_chat.needs_synthesis_review", return_value=True):
            result = self.ask([self.answer, 502, 502], limit=3)
        self.assert_request_accounting(3)
        self.assertEqual(self.requests[1].body, self.requests[2].body)
        self.assertEqual(self.trace["final"]["final_answer_round_id"], "round-1")
        self.assertTrue(result["model_answer_recorded"])
        self.assertIn("得到 20", result["answer"])
        self.assertTrue(result["narrative"]["citations"])
        self.assertTrue(any(boundary.get("reason") == "usable_draft_retained" for boundary in result["boundaries"]))
        retry = result["metrics"]["provider_retries"][0]
        self.assertEqual((retry["failed_round_id"], retry["retry_round_id"], retry["outcome"]),
                         ("round-2", "round-3", "failed"))

    def test_cancellation_during_retry_delay_prevents_second_request(self):
        cancelled = False

        def cancel_during_wait(seconds):
            nonlocal cancelled
            cancelled = True

        def check_cancel():
            if cancelled:
                raise AnalysisCancelled("Synthetic user cancellation.")

        def transport(request):
            self.requests.append(request)
            return TransportResponse(503, "Synthetic service failure.")

        with mock.patch("business_chat.time.sleep", side_effect=cancel_during_wait) as sleep:
            with self.assertRaises(AnalysisCancelled):
                run_business_chat("ORDER-RULE 的 RESULT-AMOUNT 怎么计算？", self.database,
                    self.source, self.config, transport=transport, framework_reference_path="",
                    check_cancel=check_cancel, policy=AgentPolicy(max_model_requests=3))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(sleep.call_count, 1)


if __name__ == "__main__":
    unittest.main()
