"""The workbench reports a shared investigation limit, separate from an ETA."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import web_app
from web_app import WorkbenchState


class QuestionProgressTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.app = WorkbenchState()
        self.app.job = {"job_id": "test-job", "status": "RUNNING", "kind": "question", "progress": {"phase": "preparing"}}
        self.app.started_at = self.app.updated_at = self.app.phase_started_at = self.app.progress_updated_at = self.now
        patcher = mock.patch.object(web_app, "time", SimpleNamespace(monotonic=lambda: self.now))
        patcher.start()
        self.addCleanup(patcher.stop)

    def event(self, **changes):
        event = {"phase": "waiting_for_model", "completed": 1, "total": None,
                 "unit": "requests", "model_requests": 1, "retrieved_pages": 7,
                 "question_budget_seconds": 120, "question_remaining_seconds": 100}
        event.update(changes)
        self.app._progress("test-job", event)

    def test_polling_counts_down_from_event_time_without_resetting_or_mutating_it(self):
        self.event()
        self.now += 10
        first = self.app._job_snapshot()["progress"]
        self.assertEqual(first["question_remaining_seconds"], 90)
        self.assertEqual(first["phase_elapsed_seconds"], 10)
        self.assertEqual((first["model_requests"], first["retrieved_pages"]), (1, 7))
        self.now += 5
        self.assertEqual(self.app._job_snapshot()["progress"]["question_remaining_seconds"], 85)
        self.assertEqual(self.app.job["progress"]["question_remaining_seconds"], 100)
        self.now += 200
        self.assertEqual(self.app._job_snapshot()["progress"]["question_remaining_seconds"], 0)

    def test_next_request_resets_wait_duration_and_completed_snapshot_stops_the_clock(self):
        self.event()
        self.now += 20
        self.event(model_requests=2, question_remaining_seconds=80)
        self.now += 5
        self.app.job["status"] = "COMPLETED"
        self.app.updated_at = self.now
        self.now += 40
        progress = self.app._job_snapshot()["progress"]
        self.assertEqual(progress["question_remaining_seconds"], 75)
        self.assertEqual(progress["phase_elapsed_seconds"], 5)

    def test_verification_phase_does_not_inherit_investigation_countdown(self):
        self.event()
        self.now += 30
        self.app._progress("test-job", {"phase": "verifying_answer", "model_requests": 1, "unit": "steps"})
        progress = self.app._job_snapshot()["progress"]
        self.assertEqual(progress["phase"], "verifying_answer")
        self.assertEqual(progress["model_requests"], 1)
        self.assertNotIn("question_remaining_seconds", progress)
        self.assertEqual(progress["phase_elapsed_seconds"], 0)

    def test_arbitrary_payload_and_non_numeric_activity_are_not_exposed(self):
        self.event(question_remaining_seconds=float("nan"), question_budget_seconds=True,
                   model_requests="private-marker", retrieved_pages={"source": "private-marker"},
                   source_text="private-marker", api_key="private-marker")
        progress = self.app._job_snapshot()["progress"]
        for key in ("question_remaining_seconds", "question_budget_seconds", "model_requests", "retrieved_pages", "source_text", "api_key"):
            self.assertNotIn(key, progress)
        self.assertNotIn("private-marker", str(progress))
        self.event(question_remaining_seconds=140)
        self.assertEqual(self.app._job_snapshot()["progress"]["question_remaining_seconds"], 120)


if __name__ == "__main__":
    unittest.main()
