"""Safe HTTP phase observations, with no model requests or network access."""

from dataclasses import replace
import io
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import company_api
from api_diagnostics import APIResponseDiagnostics
from company_api import APIClientError, CompanyAPIConfig, OpenAICompatibleChatClient, TransportResponse, UrllibTransport


class RequestTimingTests(unittest.TestCase):
    def setUp(self):
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
            api_key="private-credential", timeout_seconds=60)

    def complete(self, client, **options):
        return client.complete(messages=[{"role": "user", "content": "Explain the result"}], **options)

    def test_existing_injected_transport_and_body_observer_remain_compatible(self):
        body = json.dumps({"choices": [{"message": {"content": "结果"}}]}, ensure_ascii=False)
        observed = []
        response = TransportResponse(200, body, {}, None)
        client = OpenAICompatibleChatClient(self.config, transport=lambda request: response,
            request_observer=observed.append)
        self.assertEqual(self.complete(client)["choices"][0]["message"]["content"], "结果")
        self.assertEqual(len(observed), 1)
        self.assertIsInstance(observed[0], bytes)
        self.assertIn("messages", json.loads(observed[0]))
        self.assertEqual(client.last_request_timing["response_bytes"], len(body.encode("utf-8")))
        self.assertEqual(set(client.last_request_timing["timing_seconds"]), {"request_elapsed"})

    def test_native_chunks_distinguish_headers_first_body_byte_and_completion(self):
        clock = [0.0]

        class Response:
            status, headers = 200, {"X-Private": "private-header"}

            def __init__(self):
                self.chunks = iter(((0.2, b'{"choices":'), (0.3, b'[]}'), (0.4, b"")))

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read1(self, size):
                delay, chunk = next(self.chunks)
                clock[0] += delay
                return chunk

        def open_response(*args, **kwargs):
            clock[0] += 0.1
            return Response()

        transport = UrllibTransport()
        transport._opener = mock.Mock()
        transport._opener.open.side_effect = open_response
        client = OpenAICompatibleChatClient(self.config, transport=transport)
        with mock.patch.object(company_api, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            self.assertEqual(self.complete(client), {"choices": []})
        self.assertEqual(client.last_request_timing, {"timing_seconds": {
            "headers_received": 0.1, "first_response_byte": 0.3,
            "response_complete": 1.0, "request_elapsed": 1.0}, "response_bytes": 14})
        self.assertEqual(transport._opener.open.call_count, 1)

    def test_empty_body_has_completion_but_no_first_response_byte(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status, response.headers = 200, {}
        response.read1.return_value = b""
        transport = UrllibTransport()
        transport._opener = mock.Mock()
        transport._opener.open.return_value = response
        client = OpenAICompatibleChatClient(self.config, transport=transport)
        with self.assertRaisesRegex(APIClientError, "INVALID_JSON_RESPONSE"):
            self.complete(client)
        self.assertEqual(client.last_request_timing["response_bytes"], 0)
        self.assertIn("headers_received", client.last_request_timing["timing_seconds"])
        self.assertIn("response_complete", client.last_request_timing["timing_seconds"])
        self.assertNotIn("first_response_byte", client.last_request_timing["timing_seconds"])

    def test_timing_metadata_uses_only_allowlisted_numeric_fields(self):
        response = TransportResponse(200, b'{"choices": []}', timing_seconds={
            "headers_received": 0.1, "first_response_byte": "private-body",
            "response_complete": float("nan"), "url": "private-url",
            "authorization": "private-credential", "request_elapsed": "private-elapsed"},
            response_bytes=True)
        client = OpenAICompatibleChatClient(self.config, transport=lambda request: response)
        self.complete(client)
        metrics = client.last_request_timing
        self.assertEqual(set(metrics), {"timing_seconds"})
        self.assertEqual(set(metrics["timing_seconds"]), {"headers_received", "request_elapsed"})
        self.assertNotIn("private", json.dumps(metrics))

    def test_invalid_numeric_metadata_does_not_block_valid_answer(self):
        for value in (True, -1, float("inf"), float("-inf"), float("nan"), 10 ** 1000, None):
            with self.subTest(value_type=type(value).__name__):
                client = OpenAICompatibleChatClient(self.config, transport=lambda request: TransportResponse(
                    200, b'{"choices": []}', timing_seconds={"headers_received": value}, response_bytes=value))
                self.assertEqual(self.complete(client), {"choices": []})
                self.assertNotIn("headers_received", client.last_request_timing["timing_seconds"])

    def test_native_timeout_keeps_observed_phases_without_claiming_completion(self):
        blocked, release, finished = threading.Event(), threading.Event(), threading.Event()

        class Response:
            status, headers = 200, {}

            def __init__(self):
                self.reads = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                finished.set()

            def read1(self, size):
                self.reads += 1
                if self.reads == 1:
                    return b'{"choices":'
                blocked.set()
                release.wait(2)
                return b""

        def cancel_wait():
            if blocked.is_set():
                raise APIClientError("REQUEST_TIMEOUT", transport_reason="timeout")

        transport = UrllibTransport()
        transport._opener = mock.Mock()
        transport._opener.open.return_value = Response()
        client = OpenAICompatibleChatClient(self.config, transport=transport)
        try:
            with self.assertRaisesRegex(APIClientError, "REQUEST_TIMEOUT"):
                self.complete(client, check_cancel=cancel_wait)
            metrics = client.last_request_timing
            self.assertEqual(metrics["response_bytes"], 11)
            self.assertIn("headers_received", metrics["timing_seconds"])
            self.assertIn("first_response_byte", metrics["timing_seconds"])
            self.assertNotIn("response_complete", metrics["timing_seconds"])
            self.assertEqual(transport._opener.open.call_count, 1)
        finally:
            release.set()
            self.assertTrue(finished.wait(2))

    def test_http_error_keeps_safe_timings_without_reintroducing_body(self):
        response = TransportResponse(403, b'{"error":{"message":"private-body"}}',
            {"X-Private": "private-header"}, timing_seconds={"headers_received": 0.1,
                "first_response_byte": 0.2, "response_complete": 0.3}, response_bytes=36)
        diagnostics = APIResponseDiagnostics(protected_values=(self.config.resolve_api_key(),))
        client = OpenAICompatibleChatClient(self.config, transport=lambda request: response, diagnostics=diagnostics)
        with self.assertRaisesRegex(APIClientError, "HTTP_ERROR"):
            self.complete(client)
        exchange = diagnostics.to_dict()["exchanges"][0]
        self.assertEqual(exchange["request_timing"], client.last_request_timing)
        self.assertEqual(exchange["body_text"], "")
        self.assertEqual(exchange["body_omitted_reason"], "ERROR_RESPONSE_BODY_OMITTED")
        self.assertNotIn("private", json.dumps(exchange))

    def test_native_http_error_records_actual_read_phases_and_byte_count(self):
        body = b'{"error":{"message":"private-body"}}'
        error = urllib.error.HTTPError("https://neutral.example.invalid/private-path", 403,
            "private-reason", {"X-Private": "private-header"}, io.BytesIO(body))
        transport = UrllibTransport()
        transport._opener = mock.Mock()
        transport._opener.open.side_effect = error
        diagnostics = APIResponseDiagnostics(protected_values=(self.config.resolve_api_key(),))
        client = OpenAICompatibleChatClient(self.config, transport=transport, diagnostics=diagnostics)
        with self.assertRaisesRegex(APIClientError, "HTTP_ERROR"):
            self.complete(client)
        metrics = client.last_request_timing
        timings = metrics["timing_seconds"]
        self.assertEqual(metrics["response_bytes"], len(body))
        self.assertLessEqual(timings["headers_received"], timings["first_response_byte"])
        self.assertLessEqual(timings["first_response_byte"], timings["response_complete"])
        self.assertLessEqual(timings["response_complete"], timings["request_elapsed"])
        self.assertNotIn("private", json.dumps(diagnostics.to_dict()))

    def test_new_attempt_does_not_inherit_prior_request_phase_observations(self):
        responses = iter((TransportResponse(200, b'{"choices": []}', timing_seconds={"headers_received": 0.1}),
            APIClientError("TRANSPORT_ERROR")))

        def transport(request):
            item = next(responses)
            if isinstance(item, APIClientError):
                raise item
            return item

        client = OpenAICompatibleChatClient(self.config, transport=transport)
        self.complete(client)
        self.assertIn("headers_received", client.last_request_timing["timing_seconds"])
        with self.assertRaisesRegex(APIClientError, "TRANSPORT_ERROR"):
            self.complete(client)
        self.assertEqual(set(client.last_request_timing["timing_seconds"]), {"request_elapsed"})
        self.assertNotIn("response_bytes", client.last_request_timing)
        client.config = replace(self.config, timeout_seconds=0)
        with self.assertRaisesRegex(APIClientError, "TIMEOUT_INVALID"):
            self.complete(client)
        self.assertEqual(client.last_request_timing, {})


if __name__ == "__main__":
    unittest.main()
