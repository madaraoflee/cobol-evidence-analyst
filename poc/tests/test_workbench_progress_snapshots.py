"""Progress snapshots stay useful without gaining final-answer authority."""

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyze_source import AnalysisCancelled
from conversation_store import ConversationStore
from web_app import WorkbenchState


class WorkbenchProgressSnapshotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source, self.output = self.root / "source", self.root / "output"
        self.source.mkdir()
        self.output.mkdir()
        self.app = WorkbenchState(state_path=self.root / "settings.json")
        self.job_id = "current-job"
        self.app.job = {"job_id": self.job_id, "status": "RUNNING", "result": None,
                        "cancel_requested": False, "progress": {"phase": "preparing", "completed": 0}}
        self.app.started_at = self.app.updated_at = self.app.phase_started_at = time.monotonic()
        self.summary = {"round": 2, "summary": "Confirmed the calculation; checking a later override.",
                        "evidence_count": 4, "completed_items": 2, "total_items": 3,
                        "open_items": ["Later override condition"]}
        self.preview = {"round": 2, "stage": "draft", "text": "The base amount is multiplied by the rate."}

    def publish(self, **event):
        self.app._progress(self.job_id, {"phase": "writing_answer", **event})

    def test_phase_updates_preserve_independent_latest_snapshots_without_a_countdown(self):
        self.publish(analysis_update=self.summary)
        self.publish(answer_preview=self.preview)
        self.publish(phase="reviewing_answer", model_requests=2)
        progress = self.app.get_job(self.job_id)["progress"]
        self.assertEqual(progress["analysis_update"], self.summary)
        self.assertEqual(progress["answer_preview"], self.preview)
        self.assertNotIn("question_remaining_seconds", progress)
        self.assertNotIn("question_budget_seconds", progress)
        self.summary["open_items"].clear()
        self.preview["text"] = "Mutated caller object"
        self.assertEqual(progress["analysis_update"]["open_items"], ["Later override condition"])
        self.assertNotEqual(progress["answer_preview"]["text"], self.preview["text"])

    def test_preview_and_analysis_fields_are_bounded_and_do_not_pass_arbitrary_objects(self):
        summary = {**self.summary, "summary": "s" * 3000, "open_items": ["x" * 500] * 40,
                   "provider": {"raw_body": "private-envelope"}}
        preview = {**self.preview, "text": "a" * 30000, "tool_result": {"read": "private-envelope"}}
        self.publish(analysis_update=summary, answer_preview=preview, provider_response="private-envelope")
        progress = self.app.get_job(self.job_id)["progress"]
        self.assertEqual(len(progress["analysis_update"]["summary"]), 2000)
        self.assertEqual(len(progress["analysis_update"]["open_items"]), 32)
        self.assertTrue(all(len(item) == 300 for item in progress["analysis_update"]["open_items"]))
        self.assertEqual(len(progress["answer_preview"]["text"]), 24000)
        self.assertNotIn("private-envelope", json.dumps(progress))

    def test_malformed_snapshots_cannot_replace_the_last_valid_draft(self):
        self.publish(analysis_update=self.summary, answer_preview=self.preview)
        invalid_summaries = [[], {**self.summary, "round": True},
                             {**self.summary, "completed_items": 4},
                             {**self.summary, "summary": {"raw": "provider"}},
                             {**self.summary, "open_items": [{"read": "source"}]}]
        invalid_previews = [[], {**self.preview, "round": 0}, {**self.preview, "text": {}},
                            {**self.preview, "stage": []}, {**self.preview, "stage": "provider_response"}]
        for summary, preview in zip(invalid_summaries, invalid_previews):
            with self.subTest(summary=summary, preview=preview):
                self.publish(analysis_update=summary, answer_preview=preview)
                progress = self.app.get_job(self.job_id)["progress"]
                self.assertEqual(progress["analysis_update"], self.summary)
                self.assertEqual(progress["answer_preview"], self.preview)

    def test_replaced_job_starts_empty_and_rejects_old_worker_updates(self):
        self.publish(analysis_update=self.summary, answer_preview=self.preview)
        self.app.job["status"] = "COMPLETED"
        with patch("web_app.threading.Thread"):
            new_job = self.app.start({"source": str(self.source), "output": str(self.output)})
        self.assertNotEqual(new_job["job_id"], self.job_id)
        self.assertNotIn("analysis_update", self.app.job["progress"])
        self.assertNotIn("answer_preview", self.app.job["progress"])
        with self.assertRaises(AnalysisCancelled):
            self.publish(answer_preview=self.preview)

    def test_completed_job_clears_draft_and_keeps_final_result_separate(self):
        def analyzer(source, output, **options):
            options["progress"]({"phase": "writing_answer", "analysis_update": self.summary,
                                 "answer_preview": self.preview})
            (output / "programs.json").write_text(json.dumps({"programs": []}), encoding="utf-8")
            (output / "agent-result.json").write_text("{}", encoding="utf-8")
            return {"runner_status": "COMPLETED", "source_manifest_verified": False}

        self.app.analyzer = analyzer
        self.app._run(self.job_id, {"source": self.source, "output": self.output,
                                   "question": "", "allow_network": False})
        job = self.app.get_job(self.job_id)
        self.assertEqual(job["status"], "COMPLETED", job.get("error"))
        self.assertNotIn("answer_preview", job["progress"])
        self.assertEqual(job["progress"]["analysis_update"], self.summary)
        self.assertNotIn(self.preview["text"], json.dumps(job["result"]))
        with self.assertRaises(AnalysisCancelled):
            self.publish(answer_preview=self.preview)
        self.assertNotIn("answer_preview", self.app.get_job(self.job_id)["progress"])

    def test_cancelled_job_keeps_display_draft_without_recording_a_final_answer(self):
        def analyzer(source, output, **options):
            options["progress"]({"phase": "writing_answer", "answer_preview": self.preview})
            self.app.cancel(self.job_id)
            options["check_cancel"]()

        self.app.analyzer = analyzer
        self.app._run(self.job_id, {"source": self.source, "output": self.output,
                                   "question": "", "allow_network": False})
        job = self.app.get_job(self.job_id)
        self.assertEqual(job["status"], "CANCELLED")
        self.assertEqual(job["progress"]["answer_preview"], self.preview)
        self.assertIsNone(job["result"])
        self.assertIsNone(job["conversation"])

    def test_conversation_record_preserves_specific_completion_and_material_gap(self):
        self.app.conversation_store = ConversationStore(self.output, self.source)
        self.app.conversation = self.app.conversation_store.create()
        identifier = self.app.conversation["id"]
        self.app.conversation_store.append(identifier, "user", "Explain the amount.",
                                           status="pending", run_id=self.job_id)
        completion = {"status": "incomplete", "reason": "supplied_business_aspects_unexplained",
                      "missing_aspects": ["override"]}
        agent = {"runner_status": "COMPLETED", "agent_result": {
            "status": "PARTIAL", "answer": "The amount starts from the base value.",
            "answer_available": True, "model_answer_recorded": True, "answer_origin": "model",
            "business_review": {"answer_completion": completion}, "material_gap": "source_not_read",
            "stop_reason": "source_not_read"}}
        self.app._record_answer({"conversation_id": identifier}, self.job_id, {"agent": agent})
        message = self.app.conversation["messages"][-1]
        self.assertEqual(message["answer_completion"], completion)
        self.assertEqual(message["material_gap"], "source_not_read")
        self.assertEqual(message["stop_reason"], "source_not_read")
        self.assertEqual(message["status"], "partial")
        self.assertNotIn("answer_preview", message)


if __name__ == "__main__":
    unittest.main()
