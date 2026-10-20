"""Changed answer snapshots arrive without polling or a listening application."""

from email.message import Message
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyze_source import AnalysisCancelled
from company_api import CompanyAPIConfig, OpenAICompatibleChatClient, UrllibTransport
from web_app import MAX_PROGRESS_WAITERS, RequestError, RequestHandler, WorkbenchState


class ProgressWaitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source, self.output = self.root / "source", self.root / "output"
        self.source.mkdir()
        self.output.mkdir()
        self.app = WorkbenchState(state_path=self.root / "settings.json")
        self.job_id = "a" * 32
        self.app.job = {"job_id": self.job_id, "kind": "question", "status": "RUNNING",
                        "result": None, "cancel_requested": False,
                        "progress": {"phase": "waiting_for_model", "completed": 0}}
        now = time.monotonic()
        self.app.started_at = self.app.updated_at = self.app.phase_started_at = now

    def waiting_reader(self):
        entered, completed = threading.Event(), threading.Event()
        results = []
        original_wait = self.app.progress_changed.wait

        def observed_wait(timeout=None):
            entered.set()
            return original_wait(timeout)

        def read():
            try:
                results.append(self.app.wait_job(self.job_id, after=0, wait_ms=2000))
            except RequestError as error:
                results.append(error)
            finally:
                completed.set()

        patched = patch.object(self.app.progress_changed, "wait", observed_wait)
        patched.start()
        self.addCleanup(patched.stop)
        thread = threading.Thread(target=read)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.assertTrue(entered.wait(1), "Reader did not enter the bounded wait")
        return completed, results

    def test_preview_wakes_reader_before_generation_can_finish(self):
        completed, results = self.waiting_reader()
        self.app._progress(self.job_id, {"phase": "writing_answer", "answer_preview": {
            "text": "First actual fragment.", "round": 1, "stage": "draft"}})
        self.assertTrue(completed.wait(1), "Preview waited for completion instead of the change")
        self.assertEqual(results[0]["status"], "RUNNING")
        self.assertEqual(results[0]["progress"]["answer_preview"]["text"], "First actual fragment.")
        self.assertEqual(results[0]["progress_revision"], 1)
        self.assertIsNone(results[0]["result"])

    def test_native_stream_delta_reaches_waiter_before_the_next_response_read(self):
        completed, results = self.waiting_reader()

        def event(content="", finish=None):
            return ("data: " + json.dumps({"choices": [{"index": 0,
                "delta": {"content": content}, "finish_reason": finish}]}) + "\n\n").encode()

        def next_read():
            self.assertTrue(completed.wait(1), "Native delta did not wake the progress reader")
            self.assertEqual(results[0]["status"], "RUNNING")
            return event("Completed.", "stop")

        class Response:
            status = 200
            headers = {"Content-Type": "text/event-stream"}
            def __init__(self):
                self.chunks = iter([event("First. "), next_read, b"data: [DONE]\n\n"])
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def read1(self, size):
                chunk = next(self.chunks, b"")
                return chunk() if callable(chunk) else chunk

        transport = UrllibTransport()
        with patch.object(transport._opener, "open", return_value=Response()) as opening:
            client = OpenAICompatibleChatClient(
                CompanyAPIConfig("https://example.invalid/v1", "test-model", api_key="neutral-key"),
                transport=transport)
            result = client.complete(messages=[{"role": "user", "content": "Explain the rule."}],
                on_text_delta=lambda text: self.app._progress(self.job_id, {"phase": "writing_answer",
                    "answer_preview": {"text": text, "round": 1, "stage": "draft"}}))
        self.assertEqual(opening.call_count, 1)
        self.assertEqual(results[0]["progress"]["answer_preview"]["text"], "First. ")
        self.assertEqual(result["choices"][0]["message"]["content"], "First. Completed.")

    def test_change_before_wait_is_not_lost_and_response_is_detached(self):
        self.app._progress(self.job_id, {"phase": "reviewing_answer"})
        with patch.object(self.app.progress_changed, "wait", side_effect=AssertionError("Must not wait")):
            result = self.app.wait_job(self.job_id, after=0, wait_ms=20000)
        result["progress"]["phase"] = "mutated"
        self.assertEqual(self.app.get_job(self.job_id)["progress"]["phase"], "reviewing_answer")

    def test_review_phase_accepts_the_actual_reviewed_candidate_snapshot(self):
        self.app._progress(self.job_id, {"phase": "writing_answer", "answer_preview": {
            "text": "Initial candidate.", "round": 1, "stage": "answer"}})
        self.app._progress(self.job_id, {"phase": "reviewing_answer", "answer_preview": {
            "text": "Current candidate being checked.", "round": 1, "stage": "verifying"}})
        result = self.app.wait_job(self.job_id, after=1, wait_ms=0)
        self.assertEqual(result["progress"]["answer_preview"]["text"], "Current candidate being checked.")
        self.assertEqual(result["progress"]["answer_preview"]["stage"], "verifying")
        self.assertIsNone(result["result"])

    def test_idle_timeout_retains_revision_and_releases_waiter(self):
        result = self.app.wait_job(self.job_id, after=0, wait_ms=5)
        self.assertEqual(result["progress_revision"], 0)
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(self.app.progress_waiters, 0)

    def test_cancel_request_wakes_reader_without_promoting_preview(self):
        completed, results = self.waiting_reader()
        self.app.cancel(self.job_id)
        self.assertTrue(completed.wait(1))
        self.assertTrue(results[0]["cancel_requested"])
        self.assertIsNone(results[0]["result"])

    def test_job_replacement_wakes_and_rejects_the_old_reader(self):
        completed, results = self.waiting_reader()
        with self.app.lock:
            self.app.job = {**self.app.job, "job_id": "b" * 32}
            self.app._notify_job_changed()
        self.assertTrue(completed.wait(1))
        self.assertIsInstance(results[0], RequestError)
        self.assertEqual(results[0].code, "JOB_NOT_FOUND")
        self.assertEqual(self.app.progress_waiters, 0)

    def test_worker_completion_failure_and_cancellation_wake_waiters(self):
        for outcome in ("COMPLETED", "FAILED", "CANCELLED"):
            with self.subTest(outcome=outcome):
                self.app.job.update(status="RUNNING", progress_revision=0, result=None)
                self.app.cancel_event.clear()
                completed, results = self.waiting_reader()

                def analyzer(source, output, **options):
                    if outcome == "FAILED":
                        raise ValueError("synthetic failure")
                    if outcome == "CANCELLED":
                        raise AnalysisCancelled("synthetic cancellation")
                    (output / "programs.json").write_text(json.dumps({"programs": []}), encoding="utf-8")
                    (output / "agent-result.json").write_text("{}", encoding="utf-8")
                    return {"runner_status": "COMPLETED", "source_manifest_verified": False}

                self.app.analyzer = analyzer
                self.app._run(self.job_id, {"source": self.source, "output": self.output,
                                           "question": "", "allow_network": False})
                self.assertTrue(completed.wait(1), outcome)
                self.assertEqual(results[0]["status"], outcome)
                self.assertEqual(results[0]["progress_revision"], 1)

    def test_wait_arguments_and_concurrent_waiters_are_bounded(self):
        for after, wait_ms in ((True, 0), (-1, 0), (2**53, 0), (0, -1), (0, 20001), (0, True)):
            with self.subTest(after=after, wait_ms=wait_ms), self.assertRaises(RequestError):
                self.app.wait_job(self.job_id, after=after, wait_ms=wait_ms)
        self.app.progress_waiters = MAX_PROGRESS_WAITERS
        with self.assertRaises(RequestError) as raised:
            self.app.wait_job(self.job_id, after=0, wait_ms=0)
        self.assertEqual(raised.exception.status, 429)
        self.app.progress_waiters = 0

    def request(self, query, *, token=True):
        handler = object.__new__(RequestHandler)
        handler.path = f"/api/jobs/{self.job_id}/progress{query}"
        handler.server = SimpleNamespace(app=self.app, allowed_host="127.0.0.1:8765",
                                         origin="http://127.0.0.1:8765")
        handler.headers = Message()
        handler.headers["Host"] = handler.server.allowed_host
        if token:
            handler.headers["X-Session-Token"] = self.app.session_token
        captured = []
        handler._json = lambda status, value: captured.append((status, value))
        handler.do_GET()
        return captured[0]

    def test_handler_requires_session_token_before_waiting(self):
        with patch.object(self.app, "wait_job", side_effect=AssertionError("Unauthenticated wait")):
            status, result = self.request("?after=0&wait_ms=0", token=False)
        self.assertEqual(status, 403)
        self.assertEqual(result["error"]["code"], "INVALID_SESSION")
        self.assertEqual(self.request("?after=0&wait_ms=0")[0], 200)

    def test_handler_rejects_ambiguous_or_unbounded_parameters(self):
        for query in ("", "?after=0", "?after=0&after=1&wait_ms=0",
                      "?after=-1&wait_ms=0", "?after=1.5&wait_ms=0",
                      "?after=0&wait_ms=20001", "?after=0&wait_ms=0&extra=1",
                      "?after=9007199254740992&wait_ms=0"):
            with self.subTest(query=query):
                self.assertEqual(self.request(query)[0], 400)


if __name__ == "__main__":
    unittest.main()
