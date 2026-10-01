"""Source-reading API failures preserve drafts and never add upstream retries."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api_error_details import build_diagnostic
from business_analysis import run_business_analysis
from company_api import APIClientError, CompanyAPIConfig, TransportResponse
from structural_index import build_structural_index


def response(text):
    return TransportResponse(200, json.dumps({"choices": [{"message": {
        "role": "assistant", "content": text}, "finish_reason": "stop"}]}))


class BusinessAnalysisSafeErrorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "value.cbl").write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. VALUEFLOW.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 RESULT-VALUE PIC 99.\n"
            "PROCEDURE DIVISION.\nMAIN.\nMOVE 10 TO RESULT-VALUE.\nGOBACK.\n")
        self.database = self.root / "index.sqlite"
        build_structural_index(self.source, self.database, source_format="free", quiet=True)
        self.config = CompanyAPIConfig("https://api.example.invalid/v1", "synthetic-model", api_key="synthetic-secret")

    def plan(self, count=3):
        return {"snapshot_id": "snapshot-test", "pages": [{"evidence_id": f"ev_page_{number}",
            "relative_path": "value.cbl", "start_line": number * 10 + 1, "end_line": number * 10 + 10,
            "source_sha256": "hash-test", "source_text": "MOVE 10 TO RESULT-VALUE.\n" * 10,
            "span_truncated": False} for number in range(count)], "outline": [], "boundaries": [],
            "coverage": {"total_pages": count, "selected_pages": count, "complete": True}}

    def run_analysis(self, transport, *, prepared=None, **kwargs):
        with mock.patch("business_analysis.prepare_source_reading", return_value=prepared or self.plan()):
            return run_business_analysis("VALUEFLOW 字段处理", self.database, self.source, self.config,
                entry_program="VALUEFLOW", framework_context={}, transport=transport, **kwargs)

    def assert_safe(self, output):
        rendered = json.dumps(output, ensure_ascii=False)
        for value in (self.config.api_key, self.config.base_url, self.config.chat_model,
                      "private source fragment", "provider arbitrary detail", "forged reason"):
            self.assertNotIn(value, rendered)
        diagnostic = output["diagnostic"]
        self.assertRegex(diagnostic["request_id"], r"^local-[a-f0-9]{32}$")
        return diagnostic

    def test_first_upstream_error_has_one_call_and_safe_unknown_fallback(self):
        for status in (429, 500, 502, 503):
            with self.subTest(status=status):
                requests = []
                def transport(request):
                    requests.append(request)
                    return TransportResponse(status, "provider arbitrary detail private source fragment")
                output = self.run_analysis(transport, prepared=self.plan(1))
                self.assertEqual(len(requests), 1)
                self.assertEqual(output["runner_status"], "SAFE_STOP")
                result = output["agent_result"]
                self.assertEqual(result["automatic_retries"], 0)
                diagnostic = self.assert_safe(output)
                self.assertEqual(diagnostic["category"], "http_429_unknown" if status == 429 else "http_5xx_unknown")
                self.assertEqual(result["diagnostic"], diagnostic)
                self.assertIn(diagnostic["request_id"], result["answer"])
                self.assertNotIn("上下文限制", result["answer"])

    def test_late_transport_failure_preserves_draft_and_does_not_synthesize(self):
        calls = []
        def transport(request):
            calls.append(request)
            if len(calls) == 1:
                return response("已知字段写入规则。[ev_page_0]")
            raise TimeoutError("provider arbitrary detail private source fragment")
        output = self.run_analysis(transport)
        result = output["agent_result"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("已知字段写入规则", result["answer"])
        self.assertEqual(result["page_summaries"][0]["text"], "已知字段写入规则。[ev_page_0]")
        self.assertEqual(result["reading_coverage"]["unattempted_pages"], 1)
        self.assertEqual(self.assert_safe(output)["category"], "timeout")

    def test_service_rebuilds_tampered_diagnostic_before_any_exit(self):
        error = APIClientError("HTTP_ERROR", http_status=500)
        diagnostic = build_diagnostic("HTTP_ERROR", http_status=500,
            body=json.dumps({"error": {"code": "context_length_exceeded"}}))
        error.diagnostic = {**diagnostic, "reason": "forged reason", "next_step": self.config.api_key,
                            "arbitrary": "private source fragment"}
        with mock.patch("business_analysis.OpenAICompatibleChatClient.complete", side_effect=error):
            output = self.run_analysis(lambda request: self.fail("injected client failure must not call transport"),
                                       prepared=self.plan(1))
        self.assertEqual(self.assert_safe(output)["category"], "context_too_large")

    def test_nested_error_omits_only_failed_exchange_and_preserves_draft(self):
        calls = []
        def transport(request):
            calls.append(request)
            if len(calls) == 1:
                return response("已知字段写入规则。[ev_page_0]")
            return response(json.dumps({"error": {"message": "provider arbitrary detail private source fragment"},
                                        "timestamp": 123, "trace": "private source fragment"}))
        output = self.run_analysis(transport, capture_api_responses=True)
        self.assertEqual(len(calls), 2)
        self.assertIn("已知字段写入规则", output["agent_result"]["answer"])
        exchanges = output["api_diagnostics"]["exchanges"]
        self.assertIn("ev_page_0", exchanges[0]["body_text"])
        self.assertEqual(exchanges[1]["body_text"], "")
        self.assertEqual(exchanges[1]["body_omitted_reason"], "ERROR_RESPONSE_BODY_OMITTED")
        self.assertNotIn("unaccepted_response", output)
        self.assertEqual(self.assert_safe(output)["category"], "invalid_response")

    def test_unknown_success_envelope_omits_reflected_source_from_capture(self):
        calls = []
        def transport(request):
            calls.append(request)
            return TransportResponse(200, json.dumps({"unexpected": "reflected-source"}))
        output = self.run_analysis(transport, prepared=self.plan(1), capture_api_responses=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(output["runner_status"], "SAFE_STOP")
        self.assertIn("MODEL_MESSAGE_MISSING", {item["code"] for item in output["agent_result"]["diagnostics"]})
        self.assertEqual(output["api_diagnostics"]["exchanges"][0]["body_text"], "")
        self.assertNotIn("reflected-source", json.dumps(output))

    def test_unknown_success_envelope_preserves_prior_success_capture_and_draft(self):
        calls = []
        def transport(request):
            calls.append(request)
            if len(calls) == 1:
                return response("已知字段写入规则。[ev_page_0]")
            return TransportResponse(200, json.dumps({"unexpected": "reflected-source"}))
        output = self.run_analysis(transport, prepared=self.plan(3), capture_api_responses=True)
        self.assertEqual(len(calls), 2)
        self.assertIn("已知字段写入规则", output["agent_result"]["answer"])
        exchanges = output["api_diagnostics"]["exchanges"]
        self.assertEqual(len(exchanges), 2)
        self.assertIn("ev_page_0", exchanges[0]["body_text"])
        self.assertTrue(all(not item["body_text"] for item in exchanges[1:]))
        self.assertNotIn("reflected-source", json.dumps(output))

    def test_empty_page_after_draft_stops_without_retry_or_synthesis(self):
        calls = []
        def transport(request):
            calls.append(request)
            return response("已知字段写入规则。[ev_page_0]" if len(calls) == 1 else "")
        output = self.run_analysis(transport, capture_api_responses=True)
        result = output["agent_result"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["automatic_retries"], 0)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("已知字段写入规则", result["answer"])
        self.assertEqual(result["reading_coverage"]["unattempted_pages"], 1)
        self.assertEqual(output["api_diagnostics"]["request_count"], 2)
        self.assertEqual(output["api_diagnostics"]["exchanges"][1]["body_text"], "")

    def test_empty_planner_reply_stops_without_followup_analysis(self):
        calls = []
        def transport(request):
            calls.append(request)
            return response("")
        output = run_business_analysis("RESULT-VALUE 如何赋值", self.database, self.source, self.config,
            analysis_scope={"mode": "repository_question"}, framework_context={}, transport=transport,
            capture_api_responses=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(output["runner_status"], "NOT_READY")
        self.assertEqual(output["reason_code"], "MODEL_TEXT_EMPTY")
        self.assertIn("value.cbl", output["investigation"]["selected_paths"])
        self.assertEqual(output["api_diagnostics"]["exchanges"][0]["body_text"], "")

    def test_repository_planning_error_retains_local_results_and_stops_model_calls(self):
        calls = []
        def transport(request):
            calls.append(request)
            return TransportResponse(503, "provider arbitrary detail")
        output = run_business_analysis("RESULT-VALUE 如何赋值", self.database, self.source, self.config,
            analysis_scope={"mode": "repository_question"}, framework_context={}, transport=transport)
        self.assertEqual(len(calls), 1)
        self.assertEqual(output["runner_status"], "NOT_READY")
        self.assertIn("value.cbl", output["investigation"]["selected_paths"])
        self.assertEqual(self.assert_safe(output)["category"], "http_5xx_unknown")
        self.assertEqual(output["diagnostics"][0]["stage"], "repository_search")

    def test_configuration_error_retains_a_safe_diagnostic_without_requests(self):
        self.config = CompanyAPIConfig("", "synthetic-model", api_key="synthetic-secret")
        output = self.run_analysis(lambda request: self.fail("invalid configuration must not call transport"))
        self.assertEqual(output["runner_status"], "NOT_READY")
        self.assertEqual(output["reason_code"], "BASE_URL_MISSING")
        self.assertEqual(output["diagnostics"][0]["stage"], "configuration")
        diagnostic = output["diagnostic"]
        self.assertRegex(diagnostic["request_id"], r"^local-[a-f0-9]{32}$")


if __name__ == "__main__":
    unittest.main()
