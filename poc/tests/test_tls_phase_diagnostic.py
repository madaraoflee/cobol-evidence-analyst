"""TLS communication failures must not assert an unobserved handshake phase."""
from pathlib import Path
import os
import socket
import ssl
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "poc"))
from api_error_details import format_diagnostic
from company_api import APIClientError, CompanyAPIConfig, OpenAICompatibleChatClient


class TLSPhaseDiagnosticTests(unittest.TestCase):
    def test_native_tls_failure_after_received_post_keeps_phase_unconfirmed_and_outcome_unknown(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(ROOT / "tools/fixtures/loopback-server.crt"),
                                str(ROOT / "tools/fixtures/loopback-server.key"))
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        observed = {}

        def respond():
            try:
                raw, _ = listener.accept()
                raw.settimeout(5)
                with context.wrap_socket(raw, server_side=True) as tls:
                    observed["handshake_completed"] = True
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = tls.recv(4096)
                        if not chunk:
                            raise AssertionError("incomplete synthetic HTTP headers")
                        data += chunk
                    head, body = data.split(b"\r\n\r\n", 1)
                    length = next(int(row.split(b":", 1)[1]) for row in head.split(b"\r\n")
                                  if row.lower().startswith(b"content-length:"))
                    while len(body) < length:
                        chunk = tls.recv(length - len(body))
                        if not chunk:
                            raise AssertionError("incomplete synthetic HTTP body")
                        body += chunk
                    observed["post_received"] = head.startswith(b"POST /v1/chat/completions HTTP/1.1")
                    observed["body_complete"] = len(body) == length and length > 0
                    # Deliberately corrupt the TLS response only after the verified
                    # handshake and complete request, using no external endpoint.
                    with socket.socket(fileno=tls.detach()) as plain:
                        plain.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
            except BaseException as error:
                observed["server_error"] = type(error).__name__
            finally:
                listener.close()

        thread = threading.Thread(target=respond, daemon=True)
        thread.start()
        self.addCleanup(lambda: thread.join(6))
        environment = {"SSL_CERT_FILE": str(ROOT / "tools/fixtures/loopback-server.crt"),
                       "NO_PROXY": "*", "no_proxy": "*"}
        with patch.dict(os.environ, environment):
            client = OpenAICompatibleChatClient(CompanyAPIConfig(
                base_url=f"https://127.0.0.1:{listener.getsockname()[1]}/v1",
                chat_model="offline-model", api_key="neutral-fixture-key", timeout_seconds=5),
                allow_network=True)
            with self.assertRaises(APIClientError) as caught:
                client.complete(messages=[{"role": "user", "content": "Synthetic loopback diagnostic."}])
        thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("server_error", observed)
        self.assertTrue(observed.get("handshake_completed"))
        self.assertTrue(observed.get("post_received"))
        self.assertTrue(observed.get("body_complete"))
        self.assertEqual(caught.exception.transport_reason, "tls_handshake_failed")
        self.assertEqual(client.last_request_outcome, "unknown")
        self.assertIn("具体阶段尚未确认", caught.exception.diagnostic["reason"])
        self.assertNotIn("无法建立安全连接", caught.exception.diagnostic["reason"])

    def test_legacy_reason_has_phase_unconfirmed_explanation_in_both_server_languages(self):
        error = APIClientError("TRANSPORT_ERROR", transport_reason="tls_handshake_failed")
        self.assertEqual(error.to_safe_dict()["transport_reason"], "tls_handshake_failed")
        self.assertIn("具体阶段尚未确认", format_diagnostic(error.diagnostic, language="zh-CN"))
        english = format_diagnostic(error.diagnostic, language="en")
        self.assertIn("failing phase is unconfirmed", english)
        self.assertNotIn("could not be established", english)


if __name__ == "__main__":
    unittest.main()
