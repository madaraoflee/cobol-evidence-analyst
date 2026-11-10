"""Real loopback drip-body deadlines and cooperative cancellation."""
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import online_source_client as source_module
import online_workspace as workspace_module
from online_material_format import MaterialError


@contextmanager
def loopback_drip(cancel_event=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            for _ in range(100):
                time.sleep(0.02)
                if cancel_event is not None:
                    cancel_event.set()
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    class Opener:
        def __init__(self):
            self.requests, self.reads, self.closed = 0, 0, False
        def open(self, request, timeout):
            self.requests += 1
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=timeout)
            connection.request("GET", "/neutral-source")
            response = connection.getresponse()
            owner = self
            class Wrapped:
                status, headers = response.status, response.headers
                def geturl(self):
                    return request.full_url
                def read1(self, count):
                    owner.reads += 1
                    return response.read1(count)
                def read(self, count):
                    raise AssertionError("Whole-body buffered reads must not replace read1")
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    owner.closed = True
                    response.close()
                    connection.close()
            return Wrapped()
    try:
        yield Opener()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


class OnlineSourceDeadlineTests(unittest.TestCase):
    def test_real_continuously_arriving_body_reaches_total_deadline_and_is_not_retried(self):
        with loopback_drip() as opener, mock.patch.object(source_module, "REQUEST_TIMEOUT_SECONDS", 0.5):
            client = source_module.PinnedSourceClient("neutral", "source", "a" * 40, opener=opener)
            started = time.monotonic()
            with self.assertRaises(MaterialError) as caught:
                client.read_file("entry.cbl", max_bytes=100)
            elapsed = time.monotonic() - started
            self.assertEqual(caught.exception.code, "SOURCE_TIMEOUT")
            self.assertGreaterEqual(opener.reads, 2)
            self.assertLess(elapsed, 1.5)
            self.assertLess(opener.reads, 100)
            self.assertEqual(opener.requests, 1)
            self.assertTrue(opener.closed)

    def test_actual_workspace_source_cancels_during_body_instead_of_after_full_download(self):
        requested = threading.Event()
        def check_cancel():
            if requested.is_set():
                raise MaterialError("ONLINE_CANCELLED")
        with loopback_drip(requested) as opener:
            original = source_module.PinnedSourceClient
            def factory(*args, **options):
                return original(*args, opener=opener, **options)
            workbench = object.__new__(workspace_module.OnlineWorkbench)
            workbench.source_factory = None
            with mock.patch.object(workspace_module, "PinnedSourceClient", side_effect=factory):
                read = workbench._source({"identity": "neutral/source", "commit": "a" * 40,
                                          "subdirectory": ""}, {}, check_cancel)
                started = time.monotonic()
                with self.assertRaises(MaterialError) as caught:
                    read("entry.cbl", max_bytes=100)
                elapsed = time.monotonic() - started
            self.assertEqual(caught.exception.code, "ONLINE_CANCELLED")
            self.assertLess(elapsed, 1.2)
            self.assertLessEqual(opener.reads, 2)
            self.assertEqual(opener.requests, 1)
            self.assertTrue(opener.closed)

    def test_cancelled_before_open_sends_no_request(self):
        opener = mock.Mock()
        client = source_module.PinnedSourceClient("neutral", "source", "a" * 40, opener=opener,
            check_cancel=mock.Mock(side_effect=MaterialError("ONLINE_CANCELLED")))
        with self.assertRaises(MaterialError) as caught:
            client.read_file("entry.cbl")
        self.assertEqual(caught.exception.code, "ONLINE_CANCELLED")
        opener.open.assert_not_called()

    def test_header_open_elapsed_time_is_rejected_on_return_without_reading_body(self):
        clock, body = [0.0], mock.Mock()
        class Opened:
            def __enter__(self):
                return body
            def __exit__(self, *_):
                pass
        opener = mock.Mock()
        def open_response(*_, **__):
            clock[0] = 31.0
            return Opened()
        opener.open.side_effect = open_response
        with mock.patch.object(source_module.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(MaterialError) as caught:
                source_module._https_read("https://neutral.example.invalid/source", opener=opener)
        self.assertEqual(caught.exception.code, "SOURCE_TIMEOUT")
        body.read1.assert_not_called()
        self.assertEqual(opener.open.call_count, 1)

    def test_ordinary_read_and_existing_socket_timeout_classification_remain_compatible(self):
        import test_online_source_client as fixture
        opener = fixture.Opener(fixture.Response(b"source"))
        client = source_module.PinnedSourceClient("neutral", "source", "a" * 40, opener=opener)
        self.assertEqual(client.read_file("entry.cbl"), b"source")
        self.assertEqual(len(opener.requests), 1)
        for prefix in ("SOURCE", "MATERIAL"):
            with self.subTest(prefix=prefix), self.assertRaises(MaterialError) as caught:
                source_module._https_read("https://neutral.example.invalid/source", prefix=prefix,
                                          opener=fixture.Opener(error=TimeoutError()))
            self.assertEqual(caught.exception.code, prefix + "_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
