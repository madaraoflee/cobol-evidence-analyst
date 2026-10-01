from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from api_diagnostics import (  # noqa: E402
    APIResponseDiagnostics, MAX_BODY_CHARACTERS, MAX_CAPTURED_EXCHANGES,
    MAX_TOTAL_BODY_BYTES,
)
from company_api import (  # noqa: E402
    APIClientError, CompanyAPIConfig, OpenAICompatibleChatClient,
    TransportResponse, UrllibTransport,
)
from run_agent import run_investigation  # noqa: E402
from structural_index import build_structural_index  # noqa: E402


KEY = "diagnostic-test-credential"
BASE_URL = "https://service.example/v1"
MODEL = "diagnostic-chat-model"
EMBEDDING_MODEL = "diagnostic-embedding-model"


def config() -> CompanyAPIConfig:
    return CompanyAPIConfig(
        base_url=BASE_URL, chat_model=MODEL, embedding_model=EMBEDDING_MODEL,
        api_key=KEY,
    )


def collector() -> APIResponseDiagnostics:
    return APIResponseDiagnostics(protected_values=(KEY, BASE_URL, MODEL, EMBEDDING_MODEL))


def message_response(content: str) -> TransportResponse:
    return TransportResponse(200, json.dumps({
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }, ensure_ascii=False))


class DiagnosticTransport:
    """An API with strict JSON support whose investigation violates the contract."""

    def __init__(self, body: str = "程序计算保费，但是我没有按照动作协议返回。") -> None:
        self.body = body
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.endpoint == "models":
            return TransportResponse(200, json.dumps({"data": [{"id": MODEL}]}))
        payload = json.loads(request.body)
        if any(message.get("role") == "system" for message in payload["messages"]):
            return message_response(self.body)
        if "tools" in payload:
            return TransportResponse(400, '{"error":"tools unsupported"}')
        if "response_format" in payload:
            return message_response('{"ok":true}')
        return message_response("OK")


class APIResponseCaptureTests(unittest.TestCase):
    def test_invalid_json_and_http_error_are_captured_before_validation(self) -> None:
        for status, body, expected in (
            (200, "not valid JSON", "INVALID_JSON_RESPONSE"),
            (200, '["unexpected shape"]', "INVALID_RESPONSE_SHAPE"),
            (502, "gateway returned ordinary text", "HTTP_ERROR"),
        ):
            with self.subTest(status=status, body=body):
                diagnostics = collector()
                client = OpenAICompatibleChatClient(
                    config(), diagnostics=diagnostics,
                    transport=lambda request: TransportResponse(status, body),
                )
                with self.assertRaises(APIClientError) as raised:
                    client.complete(messages=[{"role": "user", "content": "check"}])
                self.assertEqual(raised.exception.code, expected)
                exchange = diagnostics.to_dict()["exchanges"][0]
                self.assertEqual(exchange["outcome_code"], expected)
                self.assertEqual(exchange["body_text"], "")
                self.assertEqual(exchange["body_omitted_reason"], "ERROR_RESPONSE_BODY_OMITTED")
                self.assertEqual(exchange["http_status"], status)
                self.assertEqual(exchange["body_bytes"], len(body.encode("utf-8")))

    def test_reflected_credentials_and_configuration_are_redacted(self) -> None:
        reflected = json.dumps({
            "message": f"{KEY} {BASE_URL} {MODEL} {EMBEDDING_MODEL}",
            "api_key": "unconfigured-key",
            "nested": {"Authorization": "Bearer other-secret", "access_token": "other-token"},
            "content": json.dumps({"client_secret": "nested-secret", "password": "nested-password"}),
            "credentials": {"value": "arbitrary-secret"},
        })
        diagnostics = collector()
        client = OpenAICompatibleChatClient(
            config(), diagnostics=diagnostics,
            transport=lambda request: TransportResponse(200, reflected),
        )
        client.complete(messages=[{"role": "user", "content": "private request body"}])
        captured = json.dumps(diagnostics.to_dict())
        for secret in (
            KEY, BASE_URL, MODEL, EMBEDDING_MODEL, "unconfigured-key", "other-secret",
            "other-token", "nested-secret", "nested-password", "arbitrary-secret",
            "private request body",
        ):
            self.assertNotIn(secret, captured)
        self.assertTrue(diagnostics.to_dict()["exchanges"][0]["body_redacted"])

    def test_noncanonical_unicode_escapes_cannot_restore_configured_secrets(self) -> None:
        # Valid JSON can encode any ASCII character as a unicode escape. The
        # frontend must not recover a protected value by parsing this preview.
        escaped_key = "\\u0064" + KEY[1:]
        embedded = json.dumps({"text": KEY}).replace(KEY, escaped_key)
        body = '{"message":"' + escaped_key + '","content":' + json.dumps(embedded) + '}'
        diagnostics = collector()
        client = OpenAICompatibleChatClient(
            config(), diagnostics=diagnostics,
            transport=lambda request: TransportResponse(200, body),
        )
        client.complete(messages=[{"role": "user", "content": "check"}])
        decoded = json.loads(diagnostics.to_dict()["exchanges"][0]["body_text"])
        self.assertNotIn(KEY, json.dumps(decoded))
        self.assertNotIn(KEY, json.dumps(json.loads(decoded["content"])))

    def test_malformed_json_credential_body_is_omitted(self) -> None:
        prefix = "x" * (MAX_BODY_CHARACTERS - 3)
        body = prefix + KEY + '\n{"api_key":"unknown-credential'
        diagnostics = collector()
        client = OpenAICompatibleChatClient(
            config(), diagnostics=diagnostics,
            transport=lambda request: TransportResponse(200, body),
        )
        with self.assertRaises(APIClientError):
            client.complete(messages=[{"role": "user", "content": "check"}])
        exchange = diagnostics.to_dict()["exchanges"][0]
        self.assertFalse(exchange["body_truncated"])
        self.assertEqual(exchange["body_text"], "")
        self.assertEqual(exchange["body_omitted_reason"], "ERROR_RESPONSE_BODY_OMITTED")
        self.assertNotIn("unknown-credential", json.dumps(exchange))

    def test_timeout_records_safe_code_without_exception_or_request_details(self) -> None:
        def timeout(request):
            raise TimeoutError(f"request {KEY} {BASE_URL} private-data")

        diagnostics = collector()
        client = OpenAICompatibleChatClient(config(), transport=timeout, diagnostics=diagnostics)
        with self.assertRaises(APIClientError) as raised:
            client.complete(messages=[{"role": "user", "content": "private request"}])
        self.assertEqual(raised.exception.code, "REQUEST_TIMEOUT")
        output = diagnostics.to_dict()
        self.assertEqual(output["request_count"], 1)
        exchange = output["exchanges"][0]
        self.assertEqual(exchange["outcome_code"], "REQUEST_TIMEOUT")
        self.assertEqual(exchange["body_omitted_reason"], "NO_RESPONSE")
        self.assertIsNone(exchange["http_status"])
        self.assertEqual(exchange["body_text"], "")
        self.assertNotIn("private", json.dumps(output))

    def test_urllib_error_body_is_explicitly_omitted(self) -> None:
        diagnostics = collector()
        transport = UrllibTransport()
        error_body = io.BytesIO(b"gateway private content")
        transport._opener = mock.Mock()
        error = urllib.error.HTTPError(
            BASE_URL, 401, "rejected", {}, error_body
        )
        self.addCleanup(error.close)
        transport._opener.open.side_effect = error
        client = OpenAICompatibleChatClient(config(), transport=transport, diagnostics=diagnostics)
        with self.assertRaises(APIClientError):
            client.complete(messages=[{"role": "user", "content": "check"}])
        exchange = diagnostics.to_dict()["exchanges"][0]
        self.assertEqual(exchange["body_omitted_reason"], "ERROR_RESPONSE_BODY_OMITTED")
        self.assertEqual(exchange["http_status"], 401)
        self.assertEqual(exchange["body_text"], "")
        self.assertTrue(error_body.closed)
        self.assertEqual(exchange["body_bytes"], len(b"gateway private content"))

    def test_collection_has_per_response_total_and_count_limits(self) -> None:
        diagnostics = collector()
        body = json.dumps({"text": "响应" * 20_000}, ensure_ascii=False)
        requests = []

        def transport(request):
            requests.append(request)
            return TransportResponse(200, body)

        client = OpenAICompatibleChatClient(config(), transport=transport, diagnostics=diagnostics)
        for _ in range(MAX_CAPTURED_EXCHANGES + 3):
            client.complete(messages=[{"role": "user", "content": "check"}])
        output = diagnostics.to_dict()
        self.assertEqual(len(output["exchanges"]), MAX_CAPTURED_EXCHANGES)
        self.assertEqual(output["omitted_exchange_count"], 3)
        self.assertEqual(output["request_count"], len(requests))
        self.assertTrue(output["truncated"])
        self.assertTrue(all(
            len(item["body_text"]) <= MAX_BODY_CHARACTERS and item["body_truncated"]
            for item in output["exchanges"]
        ))
        self.assertLessEqual(sum(
            len(item["body_text"].encode("utf-8")) for item in output["exchanges"]
        ), MAX_TOTAL_BODY_BYTES)

    def test_network_disabled_creates_no_exchange(self) -> None:
        diagnostics = collector()
        client = OpenAICompatibleChatClient(config(), diagnostics=diagnostics)
        with self.assertRaises(APIClientError) as raised:
            client.complete(messages=[{"role": "user", "content": "check"}])
        self.assertEqual(raised.exception.code, "NETWORK_DISABLED")
        self.assertEqual(diagnostics.to_dict()["exchanges"], [])

    def test_invalid_transport_response_is_counted_once(self) -> None:
        diagnostics = collector()
        client = OpenAICompatibleChatClient(
            config(), transport=lambda request: None, diagnostics=diagnostics,
        )
        with self.assertRaises(APIClientError):
            client.complete(messages=[{"role": "user", "content": "check"}])
        self.assertEqual(diagnostics.to_dict()["request_count"], 1)
        self.assertEqual(diagnostics.to_dict()["exchanges"][0]["outcome_code"], "TRANSPORT_RESPONSE_INVALID")


class RunnerDiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temporary.name) / "diagnostics.sqlite"
        build_structural_index(
            POC_ROOT / "fixtures" / "synthetic-insurance-v1", cls.database, quiet=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_plain_assistant_body_is_omitted_after_contract_rejection(self) -> None:
        transport = DiagnosticTransport()
        output = run_investigation(
            "请解释这个程序", self.database, config(), transport=transport,
            capture_api_responses=True,
        )
        self.assertEqual(output["runner_status"], "SAFE_STOP")
        self.assertEqual(output["reason_code"], "AGENT_SAFETY_STOPPED")
        self.assertEqual(output["stop_detail"], {
            "reason": "model_protocol_error", "code": "MODEL_PROTOCOL_ERROR",
        })
        diagnostics = output["api_diagnostics"]
        self.assertEqual(diagnostics["request_count"], len(transport.requests))
        investigation = diagnostics["exchanges"][-1]
        self.assertEqual(investigation["phase"], "investigation")
        self.assertEqual(investigation["http_status"], 200)
        self.assertEqual(investigation["outcome_code"], "MODEL_PROTOCOL_ERROR")
        self.assertEqual(investigation["body_text"], "")
        self.assertEqual(investigation["body_omitted_reason"], "ERROR_RESPONSE_BODY_OMITTED")
        self.assertNotIn(transport.body, json.dumps(output["capability_report"], ensure_ascii=False))
        self.assertFalse(output["capability_report"]["privacy"]["response_bodies_recorded"])

    def test_probe_failure_omits_body_when_runner_not_ready(self) -> None:
        body = "The service returned an unexpected authentication page."
        output = run_investigation(
            "check", self.database, config(),
            transport=lambda request: TransportResponse(200, body),
            capture_api_responses=True,
        )
        self.assertEqual(output["runner_status"], "NOT_READY")
        exchanges = output["api_diagnostics"]["exchanges"]
        self.assertEqual(len(exchanges), 2)
        self.assertTrue(all(item["body_text"] == "" for item in exchanges))
        self.assertTrue(all(item["body_omitted_reason"] == "ERROR_RESPONSE_BODY_OMITTED" for item in exchanges))
        self.assertTrue(all(item["phase"] == "capability_probe" for item in exchanges))
        self.assertNotIn(body, json.dumps(output["capability_report"]))

    def test_unsupported_features_preserve_chat_and_rejection_responses(self) -> None:
        def unsupported(request):
            if request.endpoint == "models":
                return TransportResponse(200, '{"data":[]}')
            payload = json.loads(request.body)
            if "tools" in payload or "response_format" in payload:
                return TransportResponse(400, '{"error":"feature unsupported"}')
            return message_response("OK")

        output = run_investigation(
            "check", self.database, config(), transport=unsupported,
            capture_api_responses=True,
        )
        self.assertEqual(output["runner_status"], "NOT_READY")
        self.assertEqual(output["reason_code"], "COMPANY_API_NOT_READY")
        self.assertEqual(output["capability_report"]["capabilities"]["chat"]["status"], "SUPPORTED")
        exchanges = output["api_diagnostics"]["exchanges"]
        self.assertEqual([item["http_status"] for item in exchanges], [200, 200, 400, 400])
        self.assertIn('"content": "OK"', exchanges[1]["body_text"])
        self.assertEqual(exchanges[-1]["body_text"], "")
        self.assertEqual(exchanges[-1]["body_omitted_reason"], "ERROR_RESPONSE_BODY_OMITTED")

    def test_default_capture_is_off_and_adds_no_requests(self) -> None:
        disabled, enabled = DiagnosticTransport(), DiagnosticTransport()
        output = run_investigation("check", self.database, config(), transport=disabled)
        captured = run_investigation(
            "check", self.database, config(), transport=enabled, capture_api_responses=True,
        )
        self.assertNotIn("api_diagnostics", output)
        self.assertEqual(len(disabled.requests), len(enabled.requests))
        self.assertEqual(output["agent_result"], captured["agent_result"])

    def test_no_network_or_unknown_entry_is_empty_capture(self) -> None:
        for entry in (None, "MISSING-PROGRAM"):
            with self.subTest(entry=entry):
                output = run_investigation(
                    "check", self.database, config(), entry_program=entry,
                    capture_api_responses=True,
                )
                self.assertEqual(output["runner_status"], "NOT_READY")
                self.assertEqual(output["api_diagnostics"]["exchanges"], [])


if __name__ == "__main__":
    unittest.main()
