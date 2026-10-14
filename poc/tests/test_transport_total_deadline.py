"""A native request has a total deadline before headers arrive, without retries."""

from __future__ import annotations

import json
import http.client
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import company_api
from company_api import APIClientError, CompanyAPIConfig, OpenAICompatibleChatClient, TransportRequest, TransportResponse, UrllibTransport


class EmptyResponse:
    status = 200
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read1(self, size):
        return b""


class TransportTotalDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.release = threading.Event()
        self.workers = []
        self.addCleanup(self.cleanup_workers)

    def cleanup_workers(self):
        self.release.set()
        for finished in self.workers:
            self.assertTrue(finished.wait(2), "Abandoned test worker did not exit")

    def blocked_transport(self):
        entered, finished = threading.Event(), threading.Event()
        self.workers.append(finished)

        def open_response(*args, **kwargs):
            entered.set()
            try:
                self.release.wait(2)
                return EmptyResponse()
            finally:
                finished.set()

        transport = UrllibTransport()
        transport._opener = mock.Mock()
        transport._opener.open.side_effect = open_response
        return transport, entered

    @staticmethod
    def request(timeout=0.05):
        return TransportRequest("GET", "https://example.invalid/v1", {}, timeout_seconds=timeout)

    @staticmethod
    def config():
        return CompanyAPIConfig("https://example.invalid/v1", "test-model", api_key="neutral-test-key", timeout_seconds=2)

    def test_blocked_connection_and_headers_obey_total_deadline(self):
        transport, entered = self.blocked_transport()
        started = time.monotonic()
        with self.assertRaises(APIClientError) as raised:
            transport(self.request())
        elapsed = time.monotonic() - started
        self.assertTrue(entered.is_set())
        self.assertEqual(raised.exception.code, "REQUEST_TIMEOUT")
        self.assertLess(elapsed, 0.5)
        self.assertFalse(self.release.is_set(), "The caller must finish before a blocked opener")
        self.assertEqual(transport._opener.open.call_count, 1)

    def test_real_http_header_parser_cannot_reset_total_deadline_with_drips(self):
        reader, writer = socket.socketpair()
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        sent, finished = [], threading.Event()
        self.workers.append(finished)

        def drip_headers():
            try:
                writer.sendall(b"HTTP/1.1 200 OK\r\n")
                while not self.release.wait(0.01) and len(sent) < 90:
                    writer.sendall(b"X-Progress: waiting\r\n")
                    sent.append(1)
                writer.sendall(b"Content-Length: 0\r\n\r\n")
            except OSError:
                pass
            finally:
                finished.set()

        thread = threading.Thread(target=drip_headers, daemon=True)
        thread.start()
        self.addCleanup(lambda: thread.join(2))

        def open_response(*args, timeout):
            reader.settimeout(timeout)
            response = http.client.HTTPResponse(reader)
            response.begin()
            return response

        transport = UrllibTransport()
        transport._opener = mock.Mock()
        transport._opener.open.side_effect = open_response
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(APIClientError, "REQUEST_TIMEOUT"):
                transport(self.request(0.08))
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertGreater(len(sent), 1, "Header bytes must arrive inside the inactivity timeout")
            self.assertFalse(finished.is_set(), "The HTTP headers must still be arriving at timeout")
            self.assertEqual(transport._opener.open.call_count, 1)
        finally:
            self.release.set()

    def test_cancel_during_connection_propagates_original_exception(self):
        transport, entered = self.blocked_transport()
        client = OpenAICompatibleChatClient(self.config(), transport=transport)
        failure = RuntimeError("local cancellation")

        def cancel():
            if entered.is_set():
                raise failure

        started = time.monotonic()
        with self.assertRaises(RuntimeError) as raised:
            client.complete(messages=[{"role": "user", "content": "neutral question"}], check_cancel=cancel)
        self.assertIs(raised.exception, failure)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(transport._opener.open.call_count, 1)

    def test_abandoned_workers_keep_concurrency_slots_until_they_exit(self):
        slots = threading.BoundedSemaphore(2)
        with mock.patch.object(company_api, "_NATIVE_REQUEST_SLOTS", slots):
            first, _ = self.blocked_transport()
            second, _ = self.blocked_transport()
            third = UrllibTransport()
            third._opener = mock.Mock()
            for transport in (first, second):
                with self.assertRaisesRegex(APIClientError, "REQUEST_TIMEOUT"):
                    transport(self.request())
            with self.assertRaisesRegex(APIClientError, "TRANSPORT_BUSY"):
                third(self.request())
            third._opener.open.assert_not_called()
            self.release.set()
            for finished in self.workers:
                self.assertTrue(finished.wait(2))
            # The opener has returned but the worker may still be closing the
            # abandoned response. Await the slot itself instead of sleeping.
            self.assertTrue(slots.acquire(timeout=2))
            self.assertTrue(slots.acquire(timeout=2))
            slots.release()
            slots.release()

    def test_remaining_timeout_is_forwarded_and_cannot_expand_configuration(self):
        received = []
        def transport(request):
            received.append(request.timeout_seconds)
            return TransportResponse(200, json.dumps({"choices": []}))
        client = OpenAICompatibleChatClient(self.config(), transport=transport)
        for timeout in (0.5, 10):
            client.complete(messages=[{"role": "user", "content": "question"}], timeout_seconds=timeout)
        self.assertEqual(received, [0.5, 2])
        for timeout in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(APIClientError, "REQUEST_TIMEOUT"):
                    client.complete(messages=[{"role": "user", "content": "question"}], timeout_seconds=timeout)
        self.assertEqual(len(received), 2)

    def test_injected_transport_stays_on_caller_thread_and_late_response_is_rejected(self):
        caller = threading.get_ident()
        clock = [0.0]
        def transport(request):
            self.assertEqual(threading.get_ident(), caller)
            clock[0] = 3.0
            return TransportResponse(200, json.dumps({"choices": []}))
        client = OpenAICompatibleChatClient(self.config(), transport=transport)
        with mock.patch.object(company_api.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(APIClientError, "REQUEST_TIMEOUT"):
                client.complete(messages=[{"role": "user", "content": "question"}])

    def test_pre_request_cancel_never_calls_transport_or_request_observer(self):
        transport, observer = mock.Mock(), mock.Mock()
        client = OpenAICompatibleChatClient(self.config(), transport=transport, request_observer=observer)
        failure = RuntimeError("local cancellation")
        with self.assertRaises(RuntimeError) as raised:
            client.complete(messages=[{"role": "user", "content": "question"}], check_cancel=mock.Mock(side_effect=failure))
        self.assertIs(raised.exception, failure)
        transport.assert_not_called()
        observer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
