"""Authenticated local job streams publish received text before completion."""

from email.message import Message
from email.parser import BytesHeaderParser
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import web_app
from company_api import CompanyAPIConfig
from web_app import MAX_JOB_STREAMS, RequestHandler, WorkbenchState


class MemoryConnection:
    def __init__(self):
        self.timeout = None
        self.closed = False

    def gettimeout(self):
        return self.timeout

    def settimeout(self, timeout):
        self.timeout = timeout

    def close(self):
        self.closed = True


class MemoryResponse:
    """A synchronized HTTP byte sink; no network permissions are required."""

    def __init__(self, connection):
        self.connection = connection
        self.condition = threading.Condition()
        self.buffer = bytearray()
        self.position = 0
        self.finished = False

    def write(self, data):
        with self.condition:
            if self.connection.closed:
                raise BrokenPipeError()
            self.buffer.extend(data)
            self.condition.notify_all()
        return len(data)

    def flush(self):
        pass

    def finish(self):
        with self.condition:
            self.finished = True
            self.condition.notify_all()

    def headers(self):
        with self.condition:
            if not self.condition.wait_for(lambda: b"\r\n\r\n" in self.buffer or self.finished, 2):
                raise TimeoutError("Missing HTTP response headers")
            header_end = self.buffer.find(b"\r\n\r\n")
            if header_end < 0:
                raise AssertionError("Request ended before returning HTTP headers")
            header = bytes(self.buffer[:header_end])
            status_line, header_lines = header.split(b"\r\n", 1)
            self.status = int(status_line.split()[1])
            self.header_values = BytesHeaderParser().parsebytes(header_lines)
            self.position = header_end + 4
        return self

    def getheader(self, name, default=None):
        return self.header_values.get(name, default)

    def readline(self, maximum=1_000_000):
        with self.condition:
            if not self.condition.wait_for(lambda: b"\n" in self.buffer[self.position:] or self.finished, 2):
                raise TimeoutError("Missing stream line")
            end = self.buffer.find(b"\n", self.position)
            end = len(self.buffer) if end < 0 else end + 1
            end = min(end, self.position + maximum)
            line = bytes(self.buffer[self.position:end])
            self.position = end
            return line

    def read(self):
        with self.condition:
            if not self.condition.wait_for(lambda: self.finished, 2):
                raise TimeoutError("Stream did not end")
            value = bytes(self.buffer[self.position:])
            self.position = len(self.buffer)
            return value

    def close(self):
        self.connection.close()


class WebJobStreamTests(unittest.TestCase):
    def setUp(self):
        self.app = WorkbenchState(config_provider=lambda: CompanyAPIConfig(
            "https://example.invalid/v1", "neutral-model", api_key="neutral-test-credential"))
        self.job_id = "1" * 32
        self.seed_job()
        self.server = SimpleNamespace(app=self.app, origin="http://127.0.0.1:8080",
            allowed_host="127.0.0.1:8080", job_stream_slots=threading.BoundedSemaphore(MAX_JOB_STREAMS),
            job_stream_stop=threading.Event())
        self.requests = []
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.job_stream_stop.set()
        for thread, response in self.requests:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            response.close()

    def seed_job(self, status="RUNNING"):
        with self.app.lock:
            now = time.monotonic()
            self.app.started_at = self.app.updated_at = self.app.phase_started_at = self.app.progress_updated_at = now
            self.app.job = {"job_id": self.job_id, "kind": "question", "status": status,
                            "result": None, "error": None, "cancel_requested": False,
                            "progress": {"phase": "waiting_for_model", "completed": 0, "total": None}}

    def request(self, *, headers=None, job_id=None, suffix="/events"):
        connection = MemoryConnection()
        response = MemoryResponse(connection)
        handler = object.__new__(RequestHandler)
        handler.server = self.server
        handler.connection = connection
        handler.wfile = response
        handler.command = "GET"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "GET / HTTP/1.1"
        handler.close_connection = True
        handler.path = f"/api/jobs/{job_id or self.job_id}{suffix}"
        request_headers = {"Host": self.server.allowed_host, "Origin": self.server.origin,
                           "X-Session-Token": self.app.session_token}
        if headers:
            request_headers.update(headers)
        handler.headers = Message()
        for key, value in request_headers.items():
            handler.headers[key] = value

        def run():
            try:
                handler.do_GET()
            finally:
                response.finish()

        thread = threading.Thread(target=run, daemon=True)
        self.requests.append((thread, response))
        thread.start()
        return connection, response.headers()

    def read_job(self, response):
        event_type = None
        while True:
            line = response.readline(1_000_000)
            self.assertTrue(line, "The subscription ended before the next job update")
            if line.startswith(b"event:"):
                event_type = line[6:].strip()
            if line.startswith(b"data:"):
                self.assertEqual(event_type, b"job")
                return json.loads(line[5:])

    def test_received_preview_arrives_while_job_is_still_running_then_terminal_closes(self):
        _, response = self.request()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type").startswith("text/event-stream"))
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.getheader("Cross-Origin-Resource-Policy"), "same-origin")
        self.assertIsNone(response.getheader("Content-Length"))
        self.assertEqual(self.read_job(response)["status"], "RUNNING")

        self.app._progress(self.job_id, {"phase": "writing_answer", "model_requests": 1,
            "answer_preview": {"text": "First received sentence.", "round": 1, "stage": "draft"},
            "reasoning_content": "private-test-marker", "unexpected": "private-test-marker"})
        update = self.read_job(response)
        self.assertEqual(update["status"], "RUNNING")
        self.assertEqual(update["progress"]["answer_preview"]["text"], "First received sentence.")
        self.assertNotIn("private-test-marker", json.dumps(update))
        with self.app.lock:
            self.app.job.update(status="COMPLETED", result={"answer": "Complete answer."})
            self.app.updated_at = time.monotonic()
        terminal = self.read_job(response)
        self.assertEqual(terminal["status"], "COMPLETED")
        self.assertEqual(terminal["result"], {"answer": "Complete answer."})
        self.assertEqual(response.read(), b"\n")

    def test_initial_completed_snapshot_is_delivered_once_and_stream_ends(self):
        for status in ("COMPLETED", "FAILED", "CANCELLED"):
            with self.subTest(status=status):
                self.seed_job(status)
                _, response = self.request()
                self.assertEqual(self.read_job(response)["status"], status)
                self.assertEqual(response.read(), b"\n")

    def test_authentication_and_origin_checks_precede_streaming(self):
        for headers in ({"X-Session-Token": ""}, {"X-Session-Token": "wrong"},
                        {"Origin": "https://other.example.invalid"},
                        {"Host": "other.example.invalid"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                _, response = self.request(headers=headers)
                self.assertEqual(response.status, 403)
                self.assertTrue(response.getheader("Content-Type").startswith("application/json"))
                self.assertIn("error", json.loads(response.read()))
        _, response = self.request(job_id="2" * 32)
        self.assertEqual(response.status, 404)
        self.assertEqual(json.loads(response.read())["error"]["code"], "JOB_NOT_FOUND")

    def test_connection_limit_returns_json_and_ordinary_polling_still_works(self):
        for _ in range(MAX_JOB_STREAMS):
            self.assertTrue(self.server.job_stream_slots.acquire(blocking=False))
        try:
            _, response = self.request()
            self.assertEqual(response.status, 503)
            self.assertEqual(json.loads(response.read())["error"]["code"], "JOB_STREAM_LIMIT")
            _, ordinary = self.request(suffix="")
            self.assertEqual(ordinary.status, 200)
            self.assertEqual(json.loads(ordinary.read())["status"], "RUNNING")
        finally:
            for _ in range(MAX_JOB_STREAMS):
                self.server.job_stream_slots.release()

    def test_reconnection_receives_latest_cumulative_preview_without_replaying_requests(self):
        for text in ("First sentence.", "First sentence. Second sentence."):
            self.app._progress(self.job_id, {"phase": "writing_answer", "answer_preview": {
                "text": text, "round": 1, "stage": "draft"}})
        self.seed_job_preserving_progress()
        _, response = self.request()
        update = self.read_job(response)
        self.assertEqual(update["progress"]["answer_preview"]["text"], "First sentence. Second sentence.")
        self.assertEqual(update["status"], "COMPLETED")
        self.assertEqual(response.read(), b"\n")

    def seed_job_preserving_progress(self):
        with self.app.lock:
            self.app.job["status"] = "COMPLETED"
            self.app.updated_at = time.monotonic()

    def test_idle_heartbeat_and_disconnect_release_slot_without_cancelling_job(self):
        for _ in range(MAX_JOB_STREAMS - 1):
            self.assertTrue(self.server.job_stream_slots.acquire(blocking=False))
        acquired = False
        try:
            with mock.patch.object(web_app, "JOB_STREAM_HEARTBEAT_SECONDS", 0.02), \
                    mock.patch.object(web_app, "JOB_STREAM_INTERVAL_SECONDS", 0.005):
                connection, response = self.request()
                self.assertEqual(self.read_job(response)["status"], "RUNNING")
                self.assertEqual(response.readline(), b"\n")
                self.assertEqual(response.readline(), b": keepalive\n")
                self.assertFalse(self.server.job_stream_slots.acquire(blocking=False))
                response.close()
                connection.close()
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline and not acquired:
                    acquired = self.server.job_stream_slots.acquire(timeout=0.02)
                self.assertTrue(acquired, "A disconnected subscriber must release its bounded slot")
                self.assertEqual(self.app.job["status"], "RUNNING")
                self.assertFalse(self.app.cancel_event.is_set())
        finally:
            for _ in range(MAX_JOB_STREAMS - 1 + int(acquired)):
                self.server.job_stream_slots.release()


if __name__ == "__main__":
    unittest.main()
