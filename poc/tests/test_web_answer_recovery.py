"""Offline web checks for complete answer persistence and output-limit notices."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conversation_store import ConversationStore
from web_app import RequestError, WorkbenchState, _validate_options


class WebAnswerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()

    def test_answer_detail_defaults_and_validation_preserve_old_requests(self):
        payload = {"source": str(self.source), "output": str(self.root / "output")}
        self.assertEqual(_validate_options(payload)["answer_detail"], "detailed")
        for value in ("brief", "detailed"):
            self.assertEqual(_validate_options({**payload, "answer_detail": value})["answer_detail"], value)
        for value in ("concise", True, [], 7):
            with self.subTest(value=value), self.assertRaises(RequestError):
                _validate_options({**payload, "answer_detail": value})

    def test_long_answer_and_length_status_survive_conversation_reload(self):
        app = WorkbenchState(state_path=None)
        app.conversation_store = ConversationStore(self.root / "output", self.source)
        conversation = app.conversation_store.create()
        run_id = "neutral-run"
        app.conversation_store.append(conversation["id"], "user", "Explain the eligibility rules.",
                                      status="pending", run_id=run_id)
        answer = "## Eligibility\n\n" + "A valid request requires sufficient capacity.\n\n" * 3000 + "TAIL-RETAINED"
        project = {"agent": {"runner_status": "COMPLETED", "agent_result": {
            "answer": answer, "status": "PARTIAL", "stop_reason": "MODEL_OUTPUT_TRUNCATED",
            "finish_reason": "length", "answer_truncated": True}}}
        app._record_answer({"conversation_id": conversation["id"], "answer_detail": "brief"},
                           run_id, project)
        saved = ConversationStore(self.root / "output", self.source).get(conversation["id"])
        message = saved["messages"][1]
        self.assertEqual(message["content"], answer)
        self.assertEqual(message["analysis_status"], "PARTIAL")
        self.assertEqual(message["finish_reason"], "length")
        self.assertTrue(message["answer_truncated"])
        self.assertEqual(message["answer_detail"], "brief")
        self.assertEqual(message["status"], "completed")

    def test_answer_preference_uses_existing_workspace_file(self):
        path = self.root / "workspace.json"
        app = WorkbenchState(state_path=path)
        app.answer_detail = "brief"
        app._save_workspace()
        self.assertEqual(json.loads(path.read_text())["answer_detail"], "brief")
        self.assertEqual(WorkbenchState(state_path=path).answer_detail, "brief")

    def test_projected_answer_records_display_limit_without_claiming_provider_truncation(self):
        app = WorkbenchState(state_path=None)
        app.conversation_store = ConversationStore(self.root / "output", self.source)
        conversation = app.conversation_store.create()
        project = {"agent": {"runner_status": "COMPLETED", "agent_result": {
            "answer": "Retained answer", "status": "ANALYZED", "finish_reason": "stop"},
            "display_projection": {"omitted": [{"path": "agent_result.answer",
                "retained_characters": 15, "total_characters": 1000015}]}}}
        app._record_answer({"conversation_id": conversation["id"]}, "neutral-run", project)
        message = app.conversation["messages"][0]
        self.assertEqual(message["content"], "Retained answer")
        self.assertTrue(message["answer_display_truncated"])
        self.assertEqual(message["answer_complete_characters"], 1000015)
        self.assertFalse(message["answer_truncated"])
        self.assertEqual(message["finish_reason"], "stop")


if __name__ == "__main__":
    unittest.main()
