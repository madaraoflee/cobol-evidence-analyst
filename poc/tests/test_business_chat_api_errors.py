"""Offline end-to-end coverage of safe provider failures in business chat."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from answer_diagnostics import build_answer_diagnostics
from api_error_details import build_diagnostic
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search
from report_view import project_report


class BusinessChatAPIErrorsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "amount.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. AMOUNT-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 BASE-AMOUNT PIC 9(5).\n01 FEE PIC 9(5).\n"
            "01 NET-AMOUNT PIC 9(5).\nPROCEDURE DIVISION.\n"
            "COMPUTE NET-AMOUNT = BASE-AMOUNT - FEE.\nGOBACK.\n",
            encoding="utf-8")
        self.database = self.root / "index.sqlite"
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
                                       api_key="synthetic-credential")
        self.calls = []

    def run_chat(self, replies):
        self.calls.clear()

        def transport(request):
            self.calls.append(request)
            self.assertLessEqual(len(self.calls), len(replies), "provider failure must not retry")
            return replies[len(self.calls) - 1]

        with mock.patch("socket.create_connection", side_effect=AssertionError("offline only")):
            return run_business_chat("NET-AMOUNT 如何扣减？", self.database, self.source,
                self.config, transport=transport, framework_reference_path="",
                capture_api_responses=True,
                policy=AgentPolicy(max_model_requests=4, max_answer_revisions=0))

    def test_explicit_reasons_survive_adapter_chat_and_shareable_summary(self):
        cases = ((500, "context_length_exceeded", "context_too_large"),
                 (404, "model_not_found", "model_unavailable"),
                 (429, "insufficient_quota", "quota_exhausted"),
                 (429, "rate_limit_exceeded", "rate_limit"))
        reflected = "PRIVATE-PROVIDER-REFLECTION: SUBTRACT UNKNOWN-FEE FROM UNKNOWN-BALANCE"
        for status, code, category in cases:
            with self.subTest(code=code):
                output = self.run_chat([TransportResponse(status, json.dumps({"error": {
                    "code": code, "message": reflected + self.config.api_key}}))])
                result = output["agent_result"]
                self.assertEqual(len(self.calls), 1)
                self.assertFalse(result["model_answer_recorded"])
                diagnostic = result["diagnostics"][-1]["diagnostic"]
                self.assertEqual(diagnostic["category"], category)
                self.assertEqual(diagnostic["evidence_source"], "provider_code")
                self.assertEqual(diagnostic["http_status"], status)
                self.assertIn(diagnostic["reason"], result["answer"])
                self.assertIn(diagnostic["next_step"], result["answer"])
                self.assertIn(diagnostic["request_id"], result["answer"])
                failure = result["diagnostic_summary"]["api_failures"][-1]
                self.assertEqual(failure["category"], category)
                self.assertEqual(failure["request_id"], diagnostic["request_id"])
                self.assertEqual(failure["stage"], "provider_request")
                serialized = json.dumps(output, ensure_ascii=False)
                self.assertNotIn(reflected, serialized)
                self.assertNotIn(self.config.api_key, serialized)
                self.assertEqual(output["api_diagnostics"]["exchanges"][-1]["body_text"], "")

    def test_unknown_500_and_429_html_or_text_do_not_guess_a_cause(self):
        for status, category in ((500, "http_5xx_unknown"), (429, "http_429_unknown")):
            for body in ("<html>PRIVATE-REFLECTION</html>", "PRIVATE-REFLECTION",
                         json.dumps({"error": {"message": "PRIVATE-REFLECTION"}})):
                with self.subTest(status=status, body=body):
                    output = self.run_chat([TransportResponse(status, body)])
                    diagnostic = output["agent_result"]["diagnostics"][-1]["diagnostic"]
                    self.assertEqual(diagnostic["category"], category)
                    self.assertNotIn("PRIVATE-REFLECTION", json.dumps(output))
                    self.assertEqual(len(self.calls), 1)

    def test_continuation_failure_keeps_original_body_and_correlated_diagnostic(self):
        original = "扣减金额为基础金额减费用，剩余条件尚未写完。"
        first = TransportResponse(200, json.dumps({"choices": [{"message": {
            "role": "assistant", "content": original}, "finish_reason": "length"}]}))
        second = TransportResponse(429, json.dumps({"error": {"code": "insufficient_quota",
                                                              "message": "PRIVATE-REFLECTION"}}))
        output = self.run_chat([first, second])
        result = output["agent_result"]
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result["answer"], original)
        self.assertEqual(result["narrative"]["text"], original)
        self.assertTrue(result["model_answer_recorded"])
        self.assertTrue(result["continuation_attempted"])
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["diagnostic_summary"]["api_failures"][-1]["category"], "quota_exhausted")
        self.assertNotIn("PRIVATE-REFLECTION", json.dumps(output))

    def test_nested_error_message_in_success_envelope_is_not_retained_as_raw_capture(self):
        for extra in ({}, {"timestamp": 123, "trace": "PRIVATE-REFLECTED-SOURCE"}):
            body = json.dumps({"choices": [{"message": {"role": "assistant", "content":
                json.dumps({"error": {"message": "PRIVATE-REFLECTED-SOURCE"}, **extra})},
                "finish_reason": "stop"}]})
            output = self.run_chat([TransportResponse(200, body)])
            self.assertFalse(output["agent_result"]["model_answer_recorded"])
            self.assertNotIn("PRIVATE-REFLECTED-SOURCE", json.dumps(output))
            self.assertEqual(output["api_diagnostics"]["exchanges"][-1]["body_text"], "")

    def test_structured_business_error_field_remains_business_data(self):
        text = json.dumps({"error": "负值时走例外流程", "net_amount_rule": "基础金额减费用"}, ensure_ascii=False)
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": text},
                                       "finish_reason": "stop"}]})
        output = self.run_chat([TransportResponse(200, body)])
        self.assertEqual(output["agent_result"]["answer"], text)
        self.assertTrue(output["agent_result"]["model_answer_recorded"])

    def test_rejected_success_status_envelopes_do_not_bypass_error_body_omission(self):
        reflected = "PRIVATE-REFLECTED-SOURCE"
        for response in ({"errors": [{"message": reflected}]},
                         {"status": "error", "message": reflected},
                         {"unexpected": reflected}):
            with self.subTest(response=response):
                output = self.run_chat([TransportResponse(200, json.dumps(response))])
                self.assertFalse(output["agent_result"]["model_answer_recorded"])
                self.assertNotIn(reflected, json.dumps(output))
                self.assertEqual(output["api_diagnostics"]["exchanges"][-1]["body_text"], "")

    def test_shareable_failure_projection_rebuilds_text_and_drops_unknown_fields(self):
        diagnostic = build_diagnostic("HTTP_ERROR", http_status=429,
            body=json.dumps({"error": {"code": "insufficient_quota"}}))
        poison = "PRIVATE-SOURCE-AND-PROMPT"
        poisoned = {**diagnostic, "reason": poison, "next_step": poison,
                    "provider_message": poison, "raw_response": poison}
        result = {"answer": poison, "diagnostics": [
            {"stage": "provider_request", "diagnostic": poisoned},
            {"stage": poison, "diagnostic": {"category": poison}}]}
        summary = build_answer_diagnostics(config=self.config, quality={}, result=result)
        self.assertEqual(len(summary["api_failures"]), 1)
        self.assertEqual(summary["api_failures"][0]["category"], "quota_exhausted")
        self.assertNotIn(poison, json.dumps(summary))

    def test_large_saved_result_keeps_correlation_before_display_budget_is_spent(self):
        diagnostic = build_diagnostic("HTTP_ERROR", http_status=500)
        projected = project_report({"agent_result": {"answer": "A" * 4_000_000,
            "narrative": {"text": "B" * 4_000_000}}, "diagnostic": diagnostic})
        self.assertEqual(projected["diagnostic"], diagnostic)


if __name__ == "__main__":
    unittest.main()
