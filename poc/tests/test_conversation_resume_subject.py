"""Resume a locally validated conversation focus without repeating a request."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search
from run_store import RunStore


class ConversationResumeSubjectTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for filename, program, factor, call in (
                ("entry.cbl", "ENTRYPOINT", 2, 'CALL "DETAILPOINT".\n'),
                ("detail.cbl", "DETAILPOINT", 3, "")):
            (self.source / filename).write_text(
                "IDENTIFICATION DIVISION.\nPROGRAM-ID. " + program + ".\n"
                "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
                "01 IN-AMOUNT PIC 9(5) VALUE 4.\n01 OUT-AMOUNT PIC 9(5).\n"
                "PROCEDURE DIVISION.\nMAIN.\n"
                f"COMPUTE OUT-AMOUNT = IN-AMOUNT * {factor}.\n" + call + "GOBACK.\n",
                encoding="utf-8")
        self.database = self.root / "facts.sqlite"
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model",
                                       api_key="offline-only")

    def test_focus_and_completed_read_response_survive_process_interruption(self):
        requests, pending_interrupt = [], False

        def transport(request):
            nonlocal pending_interrupt
            messages = json.loads(request.body)["messages"]
            payload = json.loads(messages[-1]["content"])
            requests.append(payload)
            self.assertEqual(len(messages), 2, "Recovery state must not become provider history.")
            if len(requests) == 1:
                self.assertEqual(payload["business_map"]["direct_paths"], ["entry.cbl"])
                self.assertFalse(payload["business_map"]["source_identity"]["hard_constraint"])
                content = json.dumps({"focus": ["detail.cbl"]})
            elif len(requests) == 2:
                self.assertEqual(payload["business_map"]["direct_paths"], ["detail.cbl"])
                self.assertEqual(payload["session_subject"]["root_paths"], ["detail.cbl"])
                content = json.dumps({"read": [{"relative_path": "detail.cbl",
                                               "start_line": 8, "end_line": 10}]})
                pending_interrupt = True
            else:
                self.assertEqual(len(requests), 3, "Only the next unfinished request may be sent.")
                self.assertEqual(payload["business_map"]["direct_paths"], ["detail.cbl"])
                self.assertEqual(payload["session_subject"]["root_paths"], ["detail.cbl"])
                self.assertEqual(payload["session_subject"]["focus_entities"], ["OUT-AMOUNT"])
                self.assertTrue(any(action.get("read", {}).get("relative_path") == "detail.cbl"
                                    for action in payload["completed_actions"]))
                page = next(page for bundle in payload["source_context"] for page in bundle["pages"]
                            if page["relative_path"] == "detail.cbl"
                            and "IN-AMOUNT * 3" in page["source_text"])
                content = f"OUT-AMOUNT = IN-AMOUNT * 3。[{page['evidence_id']}]"
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": content}, "finish_reason": "stop"}]}))

        def interrupt():
            if pending_interrupt:
                raise RuntimeError("simulated_process_interruption_after_read_response")

        question = "ENTRYPOINT 的 OUT-AMOUNT 怎么计算？"
        policy = AgentPolicy(max_model_requests=4, max_answer_revisions=0,
                             max_initial_context_seconds=30, max_local_analysis_seconds=60)
        arguments = {"transport": transport, "policy": policy, "framework_reference_path": ""}
        with self.assertRaisesRegex(RuntimeError, "simulated_process_interruption"):
            run_business_chat(question, self.database, self.source, self.config,
                              check_cancel=interrupt, **arguments)
        self.assertEqual(len(requests), 2)
        store = RunStore(self.root / "investigation-runs.sqlite")
        pending = store.unfinished("sha256:" + hashlib.sha256(question.encode()).hexdigest(),
                                   hashlib.sha256(self.config.chat_model.encode()).hexdigest())
        self.assertEqual(pending["checkpoint"]["navigation_state"], {
            "search_terms": ["detail.cbl"], "focus_paths": ["detail.cbl"]})
        self.assertEqual(pending["checkpoint"]["session_subject"]["root_paths"], ["detail.cbl"])
        self.assertEqual(len(pending["requests"]), 2)
        self.assertFalse(pending["uncertain_requests"])
        completed_response = json.loads(pending["requests"][-1]["response_json"])
        self.assertIn("remote_response", completed_response)

        resumed = run_business_chat(question, self.database, self.source, self.config, **arguments)
        result = resumed["agent_result"]
        self.assertEqual(len(requests), 3)
        self.assertEqual(result["run_id"], pending["run_id"])
        self.assertIn("IN-AMOUNT * 3", result["answer"])
        self.assertEqual(result["investigation_state"]["session_subject"]["root_paths"], ["detail.cbl"])
        self.assertEqual(result["investigation_state"]["session_subject"]["focus_entities"], ["OUT-AMOUNT"])
        journal = store.load(result["run_id"])
        self.assertEqual(journal["state"], "completed")
        self.assertEqual(len(journal["requests"]), 3)
        self.assertFalse(journal["checkpoint"]["exactly_once"])


if __name__ == "__main__":
    unittest.main()
