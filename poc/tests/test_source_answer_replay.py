"""A journaled tool response can resume into a useful, version-bound answer."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
import business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from quality_trace import QualityTrace
from repository_discovery import ensure_repository_search
from run_store import RunStore


class SourceAnswerReplayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.text = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. VALUE-WORKER.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 IN-AMOUNT PIC 9(5) VALUE 4.\n01 OUT-AMOUNT PIC 9(6).\n"
            "PROCEDURE DIVISION.\nMAIN.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nGOBACK.\n")
        (self.source / "entry.cbl").write_bytes(self.text.encode())
        self.database = self.root / "index.sqlite"
        build_business_index(self.source, self.database, source_format="free", verify_content=True,
                             framework_reference_path="")
        ensure_repository_search(self.database, self.source)
        self.store = RunStore(self.root / "investigation-runs.sqlite")
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only")
        self.policy = AgentPolicy(max_model_requests=1, max_answer_revisions=0)
        self.requests = []

    def ask(self, respond):
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            self.requests.append(deepcopy(payload))
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": respond(payload)},
                                                                     "finish_reason": "stop"}]}))
        return business_chat.run_business_chat("VALUE-WORKER OUT-AMOUNT 怎么计算？", self.database,
            self.source, self.config, framework_reference_path="", policy=self.policy, transport=transport)

    def interrupt_after_journal(self, respond):
        original = QualityTrace.record_remote_response

        def journal_then_crash(trace, response):
            original(trace, response)
            raise RuntimeError("interrupted_after_remote_response_journal")

        with mock.patch.object(QualityTrace, "record_remote_response", new=journal_then_crash):
            with self.assertRaisesRegex(RuntimeError, "interrupted_after_remote_response_journal"):
                self.ask(respond)
        self.assertEqual(len(self.requests), 1)

    def assert_original_version(self, result):
        self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", result["answer"])
        self.assertTrue(result["evidence_refs"])
        pages = {page["evidence_id"]: page for bundle in self.requests[0]["source_context"] for page in bundle["pages"]}
        for ref in result["evidence_refs"]:
            self.assertIn(ref["evidence_id"], pages)
            for key in ("source_sha256", "relative_path", "start_line", "end_line"):
                self.assertEqual(ref[key], pages[ref["evidence_id"]][key])
            self.assertEqual(ref["source_sha256"], hashlib.sha256(self.text.encode()).hexdigest())
        ledger = result["claim_ledger"]
        self.assertEqual(ledger["answer_sha256"], hashlib.sha256(result["answer"].encode()).hexdigest())
        self.assertTrue(any(entry["support_status"] == "supported" or entry["revised_claims"]
                            for entry in ledger["entries"]))
        saved = self.store.load(result["run_id"])
        self.assertEqual(len(saved["requests"]), 1)
        self.assertEqual(saved["uncertain_requests"], [])
        self.assertEqual(saved["requests"][0]["state"], "completed")
        return saved

    def test_tool_response_replay_keeps_original_source_facts_without_new_request(self):
        action = lambda _: json.dumps({"read": [{"relative_path": "entry.cbl", "start_line": 7, "end_line": 10}]})
        self.interrupt_after_journal(action)
        output = self.ask(lambda _: self.fail("A journaled response must be replayed without another model request"))
        result = output["agent_result"]
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["answer_origin"], "local_source_facts")
        self.assertIs(result["model_answer_recorded"], False)
        self.assertIs(result["answer_available"], True)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assert_original_version(result)

    def test_replay_uses_saved_payload_when_current_selection_would_be_empty(self):
        action = lambda _: json.dumps({"read": [{"relative_path": "entry.cbl", "start_line": 7, "end_line": 10}]})
        self.interrupt_after_journal(action)
        original_fit = business_chat._fit_request
        selections = []

        def changed_selection(config, payload, *args, **kwargs):
            result = original_fit(config, payload, *args, **kwargs)
            selections.append(1)
            payload["source_context"][0]["pages"] = []
            return result

        with mock.patch.object(business_chat, "_fit_request", side_effect=changed_selection):
            output = self.ask(lambda _: self.fail("Replay must not send the reconstructed selection"))
        self.assertTrue(selections)
        self.assertEqual(len(self.requests), 1)
        result = output["agent_result"]
        self.assertEqual(result["answer_origin"], "local_source_facts")
        saved = self.assert_original_version(result)
        trace = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text())
        self.assertEqual(len(trace["rounds"]), 1)
        self.assertEqual(trace["rounds"][0]["request_body_sha256"], saved["requests"][0]["request_hash"])
        self.assertIn("replay_processing_ms", trace["rounds"][0])

    def test_wrong_journaled_model_formula_still_passes_normal_ledger_repair(self):
        def wrong(payload):
            identifier = next(page["evidence_id"] for bundle in payload["source_context"] for page in bundle["pages"]
                              if "COMPUTE OUT-AMOUNT" in page["source_text"])
            return f"OUT-AMOUNT = IN-AMOUNT * 999。[{identifier}]"
        self.interrupt_after_journal(wrong)
        output = self.ask(lambda _: self.fail("Replay must not repeat the paid request"))
        result = output["agent_result"]
        self.assertEqual(result["answer_origin"], "model")
        self.assertIs(result["model_answer_recorded"], True)
        self.assertNotIn("999", result["answer"])
        self.assertGreater(result["claim_ledger"]["revised_claim_count"], 0)
        self.assertEqual(len(self.requests), 1)
        self.assert_original_version(result)


if __name__ == "__main__":
    unittest.main()
