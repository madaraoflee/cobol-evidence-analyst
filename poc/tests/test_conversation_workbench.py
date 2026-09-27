from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyze_source import analyze_source
from company_api import CompanyAPIConfig, TransportResponse
from web_app import WorkbenchState


class ConversationWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "quota.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. QUOTA.\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 RESERVED-COUNT PIC 9(4).\n01 CAPACITY PIC 9(4).\nPROCEDURE DIVISION.\n"
            "IF RESERVED-COUNT > CAPACITY DISPLAY 'CAPACITY EXCEEDED' END-IF.\nGOBACK.\n")
        self.output = self.root / "output"
        self.calls = []
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "neutral-model", api_key="test-credential")

        def transport(request):
            body = json.loads(request.body)
            self.calls.append(body)
            refs = re.findall(r"ev_page_[a-f0-9]+", json.dumps(body))
            content = "超过容量时会提示超额。" + (f"[{refs[0]}]" if refs else "")
            return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}))

        self.transport = transport

        def analyzer(source, output, **options):
            return analyze_source(source, output, transport=self.transport, **options)

        self.analyzer = analyzer
        self.state_path = self.root / "settings.json"
        self.app = WorkbenchState(analyzer=analyzer, config_provider=lambda: self.config, state_path=self.state_path)

    def run_job(self, **values):
        options = {"source": str(self.source), "output": str(self.output), "source_format": "free", **values}
        job_id = self.app.start(options)["job_id"]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            job = self.app.get_job(job_id)
            if job["status"] != "RUNNING":
                self.assertEqual(job["status"], "COMPLETED", job.get("error"))
                return job["result"]
            time.sleep(.01)
        self.fail("worker timeout")

    def test_two_turns_reuse_index_and_survive_restart_with_valid_history_citations(self):
        indexed = self.run_job()
        self.assertEqual(len(self.calls), 0)
        with patch("analyze_source.build_business_index", side_effect=AssertionError("must not rebuild")), \
             patch("analyze_source._verify_scope", side_effect=AssertionError("must not rescan")), \
             patch("analyze_source.ensure_repository_search", side_effect=AssertionError("must not refresh search")):
            first = self.run_job(question="How is CAPACITY checked?", allow_network=True)
            conversation_id = first["conversation"]["id"]
            second = self.run_job(question="那超过以后呢？", allow_network=True, conversation_id=conversation_id)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(second["conversation"]["messages"]), 4)
        self.assertEqual(second["snapshot_id"], indexed["snapshot_id"])
        self.assertTrue(second["diagnosis"]["index_reused"])
        self.assertEqual(self.calls[-1]["messages"][1]["content"], "How is CAPACITY checked?")
        self.assertEqual(self.calls[-1]["messages"][2]["role"], "assistant")
        self.assertIn("超过容量", self.calls[-1]["messages"][2]["content"])
        restored = WorkbenchState(analyzer=self.analyzer, config_provider=lambda: self.config, state_path=self.state_path)
        state = restored.state()
        self.assertEqual(state["conversation"]["id"], conversation_id)
        self.assertEqual(len(state["conversation"]["messages"]), 4)
        message = state["conversation"]["messages"][1]
        ref = message["evidence_refs"][0]
        evidence = restored.evidence(ref["evidence_id"], conversation_id, message["id"])
        self.assertEqual(evidence["spans"][0]["integrity"], "VALID")
        self.assertEqual(len(self.calls), 2)
        new = restored.new_conversation({})["conversation"]
        self.assertNotEqual(new["id"], conversation_id)
        self.assertEqual(new["messages"], [])
        self.assertEqual(restored.state()["project"]["snapshot_id"], indexed["snapshot_id"])

    def test_framework_directory_configuration_persists_and_reaches_model(self):
        docs = self.root / "manuals"
        docs.mkdir()
        (docs / "guide.md").write_text("# Processing guide\nCAPACITY is the maximum reserved count before escalation.\n")
        status = self.app.configure_framework({"path": str(docs)})["framework_knowledge"]
        self.assertEqual(status["loaded_document_count"], 1)
        self.assertEqual(status["configured_path"], str(docs))
        self.run_job()
        result = self.run_job(question="Explain CAPACITY", allow_network=True)
        self.assertIn("before escalation", json.dumps(self.calls[-1]))
        self.assertTrue(result["agent"]["agent_result"]["framework_context"]["references"])
        restored = WorkbenchState(state_path=self.state_path, config_provider=lambda: self.config)
        self.assertEqual(restored.state()["framework_knowledge"]["loaded_document_count"], 1)

    def test_new_conversation_does_not_inherit_another_conversation_history(self):
        self.run_job()
        one = self.run_job(question="Explain RESERVED-COUNT", allow_network=True)
        two = self.run_job(question="Explain CAPACITY", allow_network=True)
        self.assertNotEqual(one["conversation"]["id"], two["conversation"]["id"])
        self.assertEqual(len(self.calls[-1]["messages"]), 2)
        self.assertEqual(len(self.app.state()["conversations"]), 2)

    def test_authentication_failure_keeps_conversation_and_index_for_retry(self):
        indexed = self.run_job()
        self.transport = lambda request: TransportResponse(401, "{}")
        failed = self.run_job(question="Explain CAPACITY", allow_network=True)
        conversation = failed["conversation"]
        self.assertEqual(conversation["messages"][-1]["status"], "failed")
        self.assertIn("401", conversation["messages"][-1]["content"])
        self.assertEqual(failed["snapshot_id"], indexed["snapshot_id"])
        self.assertFalse(failed["agent"]["agent_result"]["model_answer_recorded"])
        self.assertEqual(len(conversation["messages"]), 2)


if __name__ == "__main__":
    unittest.main()
