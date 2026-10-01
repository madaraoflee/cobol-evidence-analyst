"""Offline classification and privacy tests for safe API error explanations."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api_error_details import MAX_ERROR_BODY_BYTES, build_diagnostic, format_diagnostic, sanitize_diagnostic
from api_diagnostics import APIResponseDiagnostics
from company_api import APIClientError, CompanyAPIConfig, OpenAICompatibleChatClient, TransportRequest, TransportResponse, UrllibTransport, probe_capabilities

KEY = "neutral-protected-secret"
URL = "https://service.example.invalid/v1"
MODEL = "neutral-private-model"
SOURCE = "01 PRIVATE-SOURCE-AREA PIC X(90)."


def config():
    return CompanyAPIConfig(URL, MODEL, api_key=KEY)


def body(code=None, message=None, **fields):
    return json.dumps({"error": {**fields, **({"code": code} if code else {}), **({"message": message} if message else {})}})


class APIErrorDetailsTests(unittest.TestCase):
    def fail(self, status, raw, headers=None, diagnostics=None):
        client = OpenAICompatibleChatClient(config(), transport=lambda request: TransportResponse(status, raw, headers or {}), diagnostics=diagnostics)
        with self.assertRaises(APIClientError) as raised:
            client.complete(messages=[{"role": "user", "content": SOURCE}])
        self.assertEqual(raised.exception.code, "HTTP_ERROR" if not 200 <= status < 300 else "MODEL_ERROR_RESPONSE")
        self.assertEqual(raised.exception.http_status, status)
        return raised.exception

    def test_known_structured_codes_have_distinct_fixed_explanations(self):
        cases = ((413, "request_too_large", "request_too_large"), (400, "context_length_exceeded", "context_too_large"),
                 (404, "model_not_found", "model_unavailable"), (400, "unsupported_parameter", "unsupported_feature"),
                 (400, "max_tokens_exceeded", "output_limit"), (429, "insufficient_quota", "quota_exhausted"),
                 (429, "rate_limit_exceeded", "rate_limit"), (401, "invalid_api_key", "authentication"),
                 (403, "permission_denied", "permission"), (500, "server_error", "http_5xx_unknown"))
        for status, provider_code, category in cases:
            with self.subTest(provider_code=provider_code):
                error = self.fail(status, body(provider_code, f"{KEY} {URL} {MODEL} {SOURCE}"))
                self.assertEqual(error.diagnostic["category"], category)
                self.assertEqual(error.diagnostic["provider_code"], provider_code)
                self.assertEqual(error.diagnostic["evidence_source"], "provider_code")
                safe = json.dumps(error.to_safe_dict(), ensure_ascii=False) + str(error) + format_diagnostic(error.diagnostic)
                for forbidden in (KEY, URL, MODEL, SOURCE):
                    self.assertNotIn(forbidden, safe)
                self.assertRegex(error.diagnostic["request_id"], r"^local-[0-9a-f]{32}$")

    def test_unknown_429_and_500_do_not_guess_a_cause(self):
        for status, raw, category in ((429, body(message="token quota?"), "http_429_unknown"),
                                     (500, body(message="token input too long"), "http_5xx_unknown"),
                                     (404, body(message="not exposed"), "request_rejected"),
                                     (500, "<html>" + SOURCE + KEY + "</html>", "http_5xx_unknown")):
            with self.subTest(status=status, raw=raw):
                self.assertEqual(self.fail(status, raw).diagnostic["category"], category)

    def test_param_requires_an_explicit_anchored_message(self):
        cases = (("max_tokens is too large: 16000. This model supports at most 8192 completion tokens.", "output_limit"),
                 ("Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.", "unsupported_feature"),
                 ("unclassified problem", "request_rejected"),
                 ("source says max_tokens is too large", "request_rejected"))
        for message, category in cases:
            with self.subTest(message=message):
                self.assertEqual(self.fail(400, body(message=message, type="invalid_request_error", param="max_tokens")).diagnostic["category"], category)

    def test_all_rejected_responses_are_omitted_even_when_capture_enabled(self):
        for status, raw in ((429, body("insufficient_quota", SOURCE + KEY)),
                            (500, "<html>" + SOURCE + KEY + "</html>"),
                            (200, body(message=SOURCE + KEY)), (200, "malformed:" + SOURCE + KEY),
                            (200, '["' + SOURCE + '"]')):
            with self.subTest(status=status):
                collector = APIResponseDiagnostics(protected_values=(KEY, URL, MODEL))
                client = OpenAICompatibleChatClient(config(), transport=lambda request: TransportResponse(status, raw), diagnostics=collector)
                with self.assertRaises(APIClientError):
                    client.complete(messages=[{"role": "user", "content": SOURCE}])
                exchange = collector.to_dict()["exchanges"][0]
                self.assertEqual(exchange["body_text"], "")
                self.assertEqual(exchange["body_bytes"], len(raw.encode()))
                self.assertEqual(exchange["body_omitted_reason"], "ERROR_RESPONSE_BODY_OMITTED")
                self.assertIsNotNone(sanitize_diagnostic(exchange["diagnostic"]))
                self.assertNotIn(SOURCE, json.dumps(collector.to_dict()))

    def test_successful_assistant_json_text_is_not_a_top_level_error(self):
        raw = json.dumps({"choices": [{"message": {"role": "assistant", "content": '{"error":"ordinary business datum"}'}}]})
        collector = APIResponseDiagnostics(protected_values=(KEY,))
        client = OpenAICompatibleChatClient(config(), transport=lambda request: TransportResponse(200, raw), diagnostics=collector)
        result = client.complete(messages=[{"role": "user", "content": SOURCE}])
        self.assertEqual(result["choices"][0]["message"]["content"], '{"error":"ordinary business datum"}')
        self.assertNotEqual(collector.to_dict()["exchanges"][0]["body_text"], "")
        client.suppress_error_capture("MODEL_ERROR_RESPONSE")
        self.assertEqual(collector.to_dict()["exchanges"][0]["body_text"], "")

    def test_explicit_alternate_error_envelopes_are_omitted_and_fail_once(self):
        envelopes = ({"errors": [{"message": SOURCE}]}, {"status": "error", "message": SOURCE},
                     {"status": "failed", "detail": SOURCE}, {"code": 503, "message": SOURCE})
        for envelope in envelopes:
            with self.subTest(envelope=envelope):
                collector = APIResponseDiagnostics(protected_values=(KEY, URL, MODEL))
                raw = json.dumps(envelope)
                self.assertEqual(self.fail(200, raw, diagnostics=collector).diagnostic["category"], "invalid_response")
                capture = collector.to_dict()
                self.assertEqual(capture["request_count"], 1)
                self.assertEqual(capture["exchanges"][0]["body_text"], "")
                self.assertNotIn(SOURCE, json.dumps(capture))

    def test_known_error_code_survives_unrelated_gateway_metadata(self):
        for code, category in (("context_length_exceeded", "context_too_large"),
                               ("insufficient_quota", "quota_exhausted")):
            collector = APIResponseDiagnostics(protected_values=(KEY, URL, MODEL))
            raw = json.dumps({"error": {"code": code, "message": SOURCE},
                              "timestamp": 123, "trace": SOURCE})
            error = self.fail(500, raw, diagnostics=collector)
            self.assertEqual(error.diagnostic["category"], category)
            self.assertEqual(error.diagnostic["evidence_source"], "provider_code")
            self.assertNotIn(SOURCE, json.dumps(error.to_safe_dict()))
            self.assertNotIn(SOURCE, json.dumps(collector.to_dict()))

    def test_safe_ids_allowlist_and_reflection_checks(self):
        cases = (("req_0123456789abcdef", ""), ("request-0123456789abcdef", ""),
                 ("req_private-source", ""), ("req_deadbeef", "DISPLAY 'req_deadbeef'."),
                 ("req_DEADBEEF", "DISPLAY 'req_deadbeef'."),
                 ("req_deadbeef", ""), (URL, ""))
        for identifier, request in cases:
            with self.subTest(identifier=identifier, request=request):
                protected = (KEY, URL, MODEL, "deadbeef") if identifier == "req_deadbeef" and not request else (KEY, URL, MODEL)
                diagnostic = build_diagnostic("HTTP_ERROR", http_status=429, headers={"X-Request-ID": identifier, "Retry-After": "12"}, protected_values=protected, request_body=request)
                expected = identifier in {"req_0123456789abcdef", "request-0123456789abcdef"}
                self.assertEqual("upstream_request_id" in diagnostic, expected)
                self.assertEqual(diagnostic["retry_after_seconds"], 12)
                self.assertEqual(diagnostic["request_id_source"], "local")
        diagnostic = build_diagnostic("HTTP_ERROR", http_status=429, headers={"Retry-After": "tomorrow"})
        self.assertNotIn("retry_after_seconds", diagnostic)

    def test_sanitizer_rebuilds_fields_and_handles_untrusted_types(self):
        original = build_diagnostic("HTTP_ERROR", http_status=429)
        forged = {**original, "reason": SOURCE, "next_step": KEY, "extra": URL, "provider_code": SOURCE,
                  "retry_after_seconds": True, "upstream_request_id": KEY}
        self.assertNotIn(SOURCE, json.dumps(sanitize_diagnostic(forged)))
        self.assertNotIn(KEY, format_diagnostic(forged))
        for key, value in (("category", []), ("request_id", {}), ("evidence_source", []),
                           ("upstream_request_id_source", {}), ("provider_code", [])):
            with self.subTest(key=key):
                result = sanitize_diagnostic({**original, key: value})
                if key in {"category", "request_id"}:
                    self.assertIsNone(result)
                else:
                    self.assertIsInstance(result, dict)
        self.assertIsNone(sanitize_diagnostic({"category": "quota_exhausted"}))

    def test_local_500_failures_are_system_errors_and_do_not_look_upstream(self):
        for code in ("REQUEST_FAILED", "ANALYSIS_FAILED", "INTERNAL_ERROR"):
            diagnostic = build_diagnostic(code, http_status=500)
            self.assertEqual((diagnostic["category"], diagnostic["evidence_source"]), ("system_error", "local"))
        self.assertEqual(build_diagnostic("MODEL_ERROR_RESPONSE", http_status=200)["category"], "invalid_response")

    def test_unknown_feature_failure_does_not_assert_unsupported(self):
        def transport(request):
            payload = json.loads(request.body or b"{}")
            if request.endpoint == "models":
                return TransportResponse(404, body(message="not exposed"))
            if "tools" in payload or "response_format" in payload:
                return TransportResponse(400, body(message="unclassified failure"))
            return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": "OK"}}]}))
        report = probe_capabilities(config(), transport=transport)
        for name in ("tool_calling", "strict_json"):
            self.assertEqual(report["capabilities"][name]["status"], "UNAVAILABLE")
            self.assertIsNotNone(sanitize_diagnostic(report["capabilities"][name]["evidence"]["diagnostic"]))
        self.assertTrue(any(event.get("diagnostic") for event in report["audit"]))

    def test_urllib_reads_error_body_with_a_byte_limit_and_closes_it(self):
        class Reader(io.BytesIO):
            read_bytes = 0
            def read1(self, size=-1):
                data = self.read(size)
                self.read_bytes += len(data)
                return data
        stream = Reader((body("insufficient_quota") + " " * (MAX_ERROR_BODY_BYTES * 2)).encode())
        error = urllib.error.HTTPError(URL, 429, KEY, {}, stream)
        transport = UrllibTransport()
        transport._opener = mock.Mock()
        transport._opener.open.side_effect = error
        response = transport(TransportRequest("POST", URL, {}, b"{}", 2, "chat/completions"))
        self.assertLessEqual(len(response.body), MAX_ERROR_BODY_BYTES + 1)
        self.assertTrue(stream.closed)
        self.assertEqual(build_diagnostic("HTTP_ERROR", http_status=429, body=response.body)["category"], "http_429_unknown")

    def test_urllib_wrapped_timeout_is_timeout_without_exception_text(self):
        for reason, category in ((TimeoutError(SOURCE + KEY), "timeout"), (OSError(SOURCE + KEY), "connection")):
            transport = UrllibTransport()
            transport._opener = mock.Mock()
            transport._opener.open.side_effect = urllib.error.URLError(reason)
            with self.assertRaises(APIClientError) as raised:
                transport(TransportRequest("POST", URL, {}, b"{}", 2, "chat/completions"))
            self.assertEqual(raised.exception.diagnostic["category"], category)
            self.assertNotIn(SOURCE, json.dumps(raised.exception.to_safe_dict()))

    def test_urllib_error_body_read_obeys_remaining_deadline(self):
        stream = io.BytesIO(body("insufficient_quota").encode())
        error = urllib.error.HTTPError(URL, 429, KEY, {}, stream)
        transport = UrllibTransport()
        transport._opener = mock.Mock()
        transport._opener.open.side_effect = error
        with mock.patch("company_api.time.monotonic", side_effect=[0.0, 0.01, 0.5]):
            response = transport(TransportRequest("POST", URL, {}, b"{}", 0.1, "chat/completions"))
        self.assertEqual(response.body, b"")
        self.assertTrue(stream.closed)

    def test_suppression_after_capture_limit_preserves_earlier_success(self):
        collector = APIResponseDiagnostics(protected_values=())
        for index in range(33):
            collector.record(phase="investigation", endpoint="chat/completions", http_status=200,
                             outcome_code="RESPONSE_RECEIVED", body="successful draft", elapsed_ms=1)
        collector.suppress_latest_error(outcome_code="MODEL_PROTOCOL_ERROR")
        result = collector.to_dict()
        self.assertEqual(result["request_count"], 33)
        self.assertEqual(result["exchanges"][-1]["body_text"], "successful draft")


if __name__ == "__main__":
    unittest.main()
