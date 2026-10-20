"""Received answers and their citations survive timeout, storage and restart."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from answer_delivery_fixture import run_scenario
from run_store import RunStore


class AnswerDeliveryWorkbenchTests(unittest.TestCase):
    def test_related_program_followup_retains_received_answer_after_optional_read_timeout(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch("company_api.UrllibTransport.__call__", side_effect=AssertionError("External network forbidden")):
            app, proof = run_scenario(temporary)
            self.assertEqual(proof["provider_requests"], 2)
            self.assertEqual(proof["clock_injection"], [{"started_at": 119.0, "stopped_at": 181.0}])
            result = proof["results"][-1]["agent"]["agent_result"]
            self.assertEqual(result["stop_reason"], "QUESTION_TIME_BUDGET_EXHAUSTED")
            self.assertEqual(result["status"], "PARTIAL")
            self.assertEqual(result["answer_origin"], "model")
            self.assertTrue(result["model_answer_recorded"])
            self.assertEqual(result["metrics"]["model_requests"], 1)
            self.assertIn(proof["requests"][-1]["answer"], result["answer"])
            self.assertEqual(result["metrics"]["history_messages"], 2)
            pages = [page for context in proof["requests"][-1]["payload"]["source_context"]
                     for page in context["pages"]]
            self.assertTrue(any("CALL 'HELPER01' USING REQUEST-COUNT" in page["source_text"] for page in pages))
            self.assertTrue(any("DISPLAY INPUT-COUNT" in page["source_text"] for page in pages))
            conversation = app.state()["conversation"]
            self.assertEqual(len(conversation["messages"]), 4)
            message = conversation["messages"][-1]
            self.assertIn(proof["requests"][-1]["answer"], message["content"])
            self.assertEqual(len(message["evidence_refs"]), 2)
            for ref in message["evidence_refs"]:
                evidence = app.evidence(ref["evidence_id"], conversation["id"], message["id"])
                self.assertEqual(evidence["spans"][0]["integrity"], "VALID")
            saved = RunStore(Path(temporary) / "output" / "investigation-runs.sqlite").load(result["run_id"])
            self.assertIn(proof["requests"][-1]["answer"], saved["checkpoint"]["accepted_draft"]["answer"])


if __name__ == "__main__":
    unittest.main()
