"""Known provider error causes survive SSE without exposing provider text."""
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api_diagnostics import APIResponseDiagnostics
from company_api import APIClientError, CompanyAPIConfig, OpenAICompatibleChatClient, TransportResponse, UrllibTransport

KEY = "neutral-stream-fixture-key"
REQUEST_ID = "req_0123456789abcdef"
HEADERS = {"Content-Type": "text/event-stream", "x-request-id": REQUEST_ID, "Retry-After": "7"}


class StreamErrorDiagnosticTests(unittest.TestCase):
    def observe(self, code, *, native=False, key=KEY, question="Explain the business flow",
                prefix=False, json_status=None, headers=None):
        body = json.dumps({"error": {"code": code, "message": "private-provider-marker " + key},
                           "request_id": REQUEST_ID}).encode()
        response_headers = dict(HEADERS if headers is None else headers)
        if json_status is None:
            body = b"data: " + body + b"\n\n"
            if prefix:
                body = b'data: {"choices":[{"index":0,"delta":{"content":"Preserved business draft."},"finish_reason":null}]}\n\n' + body
        else:
            response_headers["Content-Type"] = "application/json"
        closed = threading.Event()
        if native:
            class Response:
                status = json_status or 200
                headers = response_headers
                remaining = body
                def __enter__(self): return self
                def __exit__(self, *args): closed.set()
                def read1(self, size):
                    value = self.remaining[:size]
                    self.remaining = self.remaining[size:]
                    return value
            transport = UrllibTransport()
            transport._opener = Mock()
            transport._opener.open.return_value = Response()
        else:
            transport = Mock(return_value=TransportResponse(json_status or 200, body, response_headers))
        diagnostics = APIResponseDiagnostics(protected_values=(key,))
        client = OpenAICompatibleChatClient(CompanyAPIConfig("https://offline.example.invalid/v1",
            "neutral-model", api_key=key, timeout_seconds=2), transport=transport, diagnostics=diagnostics)
        visible = []
        try:
            with self.assertRaises(APIClientError) as caught:
                client.complete(messages=[{"role": "user", "content": question}], on_text_delta=visible.append)
        finally:
            if native:
                self.assertTrue(closed.wait(2))
        safe = caught.exception.to_safe_dict()
        captured = diagnostics.to_dict()
        serialized = json.dumps({"error": safe, "capture": captured})
        self.assertNotIn(key, serialized)
        self.assertNotIn("private-provider-marker", serialized)
        self.assertEqual(transport._opener.open.call_count if native else transport.call_count, 1)
        return safe, captured, client.last_request_outcome, visible

    def test_explicit_sse_causes_keep_safe_headers_on_native_and_injected_paths(self):
        for native in (False, True):
            for code, expected in (("insufficient_quota", "quota_exhausted"),
                                   ("rate_limit_exceeded", "rate_limit"),
                                   ("context_length_exceeded", "context_too_large")):
                with self.subTest(native=native, code=code):
                    safe, captured, outcome, visible = self.observe(code, native=native)
                    self.assertEqual(safe["code"], "MODEL_ERROR_RESPONSE")
                    self.assertEqual(safe["http_status"], 200)
                    self.assertEqual(safe["diagnostic"]["category"], expected)
                    self.assertEqual(safe["diagnostic"]["provider_code"], code)
                    self.assertEqual(safe["diagnostic"]["upstream_request_id"], REQUEST_ID)
                    self.assertEqual(safe["diagnostic"]["upstream_request_id_source"], "header")
                    self.assertEqual(safe["diagnostic"]["retry_after_seconds"], 7)
                    self.assertEqual(captured["exchanges"][0]["http_status"], 200)
                    self.assertEqual(captured["exchanges"][0]["body_text"], "")
                    self.assertEqual(outcome, "unknown")
                    self.assertEqual(visible, [])

    def test_unknown_sse_code_stays_unconfirmed_without_echo(self):
        for native in (False, True):
            safe, captured, outcome, _ = self.observe("private-unknown-code", native=native)
            self.assertEqual(safe["diagnostic"]["category"], "invalid_response")
            self.assertNotIn("provider_code", safe["diagnostic"])
            self.assertNotIn("private-unknown-code", json.dumps({"safe": safe, "capture": captured}))
            self.assertEqual(outcome, "unknown")

    def test_reflected_error_code_and_request_id_are_not_trusted(self):
        for native in (False, True):
            safe, _, _, _ = self.observe("insufficient_quota", native=native, key="insufficient_quota")
            self.assertEqual(safe["diagnostic"]["category"], "invalid_response")
            self.assertNotIn("provider_code", safe["diagnostic"])
            safe, _, _, _ = self.observe("rate_limit_exceeded", native=native,
                question="Explain the literal rate_limit_exceeded and " + REQUEST_ID)
            self.assertEqual(safe["diagnostic"]["category"], "invalid_response")
            self.assertNotIn("provider_code", safe["diagnostic"])
            self.assertNotIn("upstream_request_id", safe["diagnostic"])

    def test_visible_text_is_preserved_without_retry_or_false_completion(self):
        for native in (False, True):
            safe, captured, outcome, visible = self.observe("rate_limit_exceeded", native=native, prefix=True)
            self.assertEqual(visible, ["Preserved business draft."])
            self.assertEqual(safe["diagnostic"]["category"], "rate_limit")
            self.assertEqual(outcome, "unknown")
            self.assertEqual(captured["request_count"], 1)
            self.assertEqual(captured["exchanges"][0]["body_text"], "")

    def test_ordinary_http500_keeps_existing_classification(self):
        for code, expected in (("insufficient_quota", "quota_exhausted"),
                               ("rate_limit_exceeded", "rate_limit"),
                               ("context_length_exceeded", "context_too_large")):
            safe, _, outcome, _ = self.observe(code, json_status=500)
            self.assertEqual(safe["code"], "HTTP_ERROR")
            self.assertEqual(safe["http_status"], 500)
            self.assertEqual(safe["diagnostic"]["category"], expected)
            self.assertEqual(outcome, "response_received")
