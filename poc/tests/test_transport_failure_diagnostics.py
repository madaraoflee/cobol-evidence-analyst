from __future__ import annotations

import errno
import io
import json
import socket
import ssl
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from api_error_details import build_diagnostic, sanitize_diagnostic
from api_diagnostics import APIResponseDiagnostics  # noqa: E402
from company_api import (  # noqa: E402
    APIClientError, CompanyAPIConfig, OpenAICompatibleChatClient, UrllibTransport,
)


DETAIL = "credential-sentinel https://service.example/v1 request-sentinel"
REASONS = (
    "dns_resolution_failed", "tls_certificate_invalid", "tls_handshake_failed",
    "connection_refused", "connection_reset", "network_unreachable", "timeout",
)


def config() -> CompanyAPIConfig:
    return CompanyAPIConfig(
        base_url="https://service.example/v1", chat_model="test-chat-model",
        api_key="credential-sentinel", timeout_seconds=7,
    )


class FailingResponse:
    status = 200
    headers: dict[str, str] = {}

    def __init__(self, error: BaseException) -> None:
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read1(self, size):
        raise self.error


class TransportFailureDiagnosticsTests(unittest.TestCase):
    def safe_error(self, failure):
        result = failure.to_safe_dict()
        diagnostic = result.pop("diagnostic")
        self.assertEqual(diagnostic, sanitize_diagnostic(diagnostic))
        self.assertRegex(diagnostic["request_id"], r"^local-[a-f0-9]{32}$")
        self.assertNotIn(DETAIL, json.dumps(diagnostic))
        return result

    def test_transport_extension_accepts_upstream_diagnostic(self):
        diagnostic = build_diagnostic("HTTP_ERROR", http_status=429,
            body=json.dumps({"error": {"code": "insufficient_quota"}}))
        failure = APIClientError("HTTP_ERROR", http_status=429, diagnostic=diagnostic)
        self.assertEqual(failure.diagnostic, diagnostic)
        self.assertEqual(failure.to_safe_dict()["diagnostic"]["category"], "quota_exhausted")

    def assert_failure(
        self, error: BaseException, reason: str | None, *, reading: bool = False,
        injected: bool = False,
    ) -> APIClientError:
        diagnostics = APIResponseDiagnostics(protected_values=())
        opener = mock.Mock()
        if reading:
            opener.open.return_value = FailingResponse(error)
        else:
            opener.open.side_effect = error
        if injected:
            transport = mock.Mock(side_effect=error)
        else:
            with mock.patch("company_api.urllib.request.build_opener", return_value=opener):
                transport = UrllibTransport()
        client = OpenAICompatibleChatClient(config(), transport=transport, diagnostics=diagnostics)
        with self.assertRaises(APIClientError) as raised:
            client.complete(messages=[{"role": "user", "content": "request-sentinel"}])
        failure = raised.exception
        expected_code = "REQUEST_TIMEOUT" if reason == "timeout" else "TRANSPORT_ERROR"
        expected = {"code": expected_code}
        if reason is not None:
            expected["transport_reason"] = reason
        self.assertEqual(self.safe_error(failure), expected)
        self.assertEqual(failure.transport_reason, reason)
        self.assertEqual(str(failure), expected_code)
        self.assertIsNone(failure.http_status)
        if injected:
            self.assertEqual(transport.call_count, 1)
        else:
            self.assertEqual(opener.open.call_count, 1)
            self.assertEqual(opener.open.call_args.kwargs["timeout"], 7)
        output = diagnostics.to_dict()
        self.assertEqual(output["request_count"], 1)
        exchange = output["exchanges"][0]
        self.assertEqual(exchange["outcome_code"], expected_code)
        self.assertEqual(exchange.get("transport_reason"), reason)
        self.assertIsNone(exchange["http_status"])
        self.assertEqual(exchange["body_omitted_reason"], "NO_RESPONSE")
        self.assertEqual(exchange["body_text"], "")
        captured = json.dumps({"error": failure.to_safe_dict(), "diagnostics": output}) + repr(failure)
        for marker in ("credential-sentinel", "service.example", "request-sentinel"):
            self.assertNotIn(marker, captured)
        return failure

    def test_optional_reason_preserves_existing_error_contract(self) -> None:
        failure = APIClientError("HTTP_ERROR", http_status=503)
        self.assertEqual(self.safe_error(failure), {"code": "HTTP_ERROR", "http_status": 503})
        self.assertEqual(str(failure), "HTTP_ERROR (HTTP 503)")
        for reason in REASONS:
            with self.subTest(reason=reason):
                failure = APIClientError("TRANSPORT_ERROR", transport_reason=reason)
                self.assertEqual(self.safe_error(failure), {
                    "code": "TRANSPORT_ERROR", "transport_reason": reason,
                })

    def test_unrecognized_reason_cannot_escape_safe_serialization(self) -> None:
        for reason in (None, "", DETAIL, "TIMEOUT", ["timeout"], {"timeout": True}, 1):
            with self.subTest(reason=reason):
                failure = APIClientError("TRANSPORT_ERROR", transport_reason=reason)
                self.assertIsNone(failure.transport_reason)
                self.assertEqual(self.safe_error(failure), {"code": "TRANSPORT_ERROR"})
        failure.transport_reason = DETAIL
        self.assertEqual(self.safe_error(failure), {"code": "TRANSPORT_ERROR"})

    def test_open_failures_classify_direct_and_nested_url_errors(self) -> None:
        cases = (
            (TimeoutError(DETAIL), "timeout"),
            (socket.gaierror(socket.EAI_NONAME, DETAIL), "dns_resolution_failed"),
            (socket.herror(DETAIL), "dns_resolution_failed"),
            (ssl.SSLCertVerificationError(1, DETAIL), "tls_certificate_invalid"),
            (ssl.SSLError(ssl.SSL_ERROR_SSL, DETAIL), "tls_handshake_failed"),
            (ConnectionRefusedError(errno.ECONNREFUSED, DETAIL), "connection_refused"),
            (ConnectionRefusedError(DETAIL), "connection_refused"),
            (ConnectionResetError(errno.ECONNRESET, DETAIL), "connection_reset"),
            (ConnectionResetError(DETAIL), "connection_reset"),
            (OSError(errno.ENETUNREACH, DETAIL), "network_unreachable"),
            (OSError(errno.EHOSTUNREACH, DETAIL), "network_unreachable"),
            (OSError(errno.ETIMEDOUT, DETAIL), "timeout"),
        )
        for error, reason in cases:
            for depth in (0, 1, 2):
                with self.subTest(reason=reason, depth=depth):
                    wrapped = error
                    for _ in range(depth):
                        wrapped = urllib.error.URLError(wrapped)
                    self.assert_failure(wrapped, reason)

    def test_windows_socket_numbers_are_classified_without_message_parsing(self) -> None:
        cases = (
            (10060, "timeout"), (10061, "connection_refused"),
            (10054, "connection_reset"), (10051, "network_unreachable"),
            (10065, "network_unreachable"), (11001, "dns_resolution_failed"),
            (11002, "dns_resolution_failed"), (11003, "dns_resolution_failed"),
            (11004, "dns_resolution_failed"),
        )
        for number, reason in cases:
            for attribute in ("errno", "winerror"):
                with self.subTest(number=number, attribute=attribute):
                    error = OSError(0, DETAIL)
                    setattr(error, attribute, number)
                    self.assert_failure(urllib.error.URLError(error), reason)

    def test_unknown_proxy_or_connection_text_is_never_used_to_guess_reason(self) -> None:
        for text in (
            "Tunnel connection failed: 407 Proxy Authentication Required",
            "timed out CERTIFICATE_VERIFY_FAILED Connection refused DNS failed",
        ):
            with self.subTest(text=text):
                self.assert_failure(urllib.error.URLError(f"{text} {DETAIL}"), None)
        self.assert_failure(OSError(errno.EINVAL, DETAIL), None)
        self.assert_failure(urllib.error.URLError(RuntimeError(DETAIL)), None)

    def test_cyclic_url_error_reason_remains_bounded_and_generic(self) -> None:
        error = urllib.error.URLError(DETAIL)
        error.reason = error
        self.assert_failure(error, None)

    def test_read_failures_retain_timeout_reset_and_certificate_reasons(self) -> None:
        cases = (
            (TimeoutError(DETAIL), "timeout"),
            (ConnectionResetError(errno.ECONNRESET, DETAIL), "connection_reset"),
            (ssl.SSLCertVerificationError(1, DETAIL), "tls_certificate_invalid"),
            (ssl.SSLError(ssl.SSL_ERROR_SSL, DETAIL), None),
        )
        for error, reason in cases:
            with self.subTest(reason=reason):
                self.assert_failure(error, reason, reading=True)

    def test_total_read_deadline_records_timeout_reason(self) -> None:
        with mock.patch("company_api.time.monotonic", side_effect=[0, 0, 8, 8]):
            self.assert_failure(ValueError(DETAIL), "timeout", reading=True)

    def test_injected_transport_uses_only_known_types_and_preserves_safe_errors(self) -> None:
        for error, reason in (
            (urllib.error.URLError(TimeoutError(DETAIL)), "timeout"),
            (socket.gaierror(socket.EAI_AGAIN, DETAIL), "dns_resolution_failed"),
            (ConnectionRefusedError(errno.ECONNREFUSED, DETAIL), "connection_refused"),
            (ssl.SSLCertVerificationError(1, DETAIL), "tls_certificate_invalid"),
            (ssl.SSLError(ssl.SSL_ERROR_SSL, DETAIL), None),
            (RuntimeError(DETAIL), None),
            (APIClientError("TRANSPORT_ERROR", transport_reason="connection_reset"), "connection_reset"),
        ):
            with self.subTest(reason=reason, error_type=type(error).__name__):
                self.assert_failure(error, reason, injected=True)
        caused = RuntimeError(DETAIL)
        caused.__cause__ = TimeoutError(DETAIL)
        self.assert_failure(caused, None, injected=True)

    def test_http_error_retains_status_without_inventing_transport_reason(self) -> None:
        body = io.BytesIO(DETAIL.encode())
        error = urllib.error.HTTPError("https://service.example/v1", 407, DETAIL, {}, body)
        self.addCleanup(error.close)
        opener = mock.Mock()
        opener.open.side_effect = error
        with mock.patch("company_api.urllib.request.build_opener", return_value=opener):
            transport = UrllibTransport()
        diagnostics = APIResponseDiagnostics(protected_values=())
        client = OpenAICompatibleChatClient(config(), transport=transport, diagnostics=diagnostics)
        with self.assertRaises(APIClientError) as raised:
            client.complete(messages=[{"role": "user", "content": "request-sentinel"}])
        self.assertEqual(self.safe_error(raised.exception), {"code": "HTTP_ERROR", "http_status": 407})
        self.assertTrue(body.closed)
        exchange = diagnostics.to_dict()["exchanges"][0]
        self.assertNotIn("transport_reason", exchange)
        self.assertEqual(exchange["body_omitted_reason"], "ERROR_RESPONSE_BODY_OMITTED")
        self.assertNotIn(DETAIL, json.dumps(diagnostics.to_dict()))


if __name__ == "__main__":
    unittest.main()
