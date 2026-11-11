"""Recover verified source work from a first turn with only a diagnostic."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from company_api import TransportResponse
from run_store import RunStore
from web_app import WorkbenchState
import test_completion_driven_chat as transport_fixture
import test_conversation_analysis_handoff as fixtures


class FirstFailedTurnHandoffTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.ConversationAnalysisHandoffTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.fixture = self.case.fixture
        self.identifier = self.case.conversation["id"]
        self.app = WorkbenchState(state_path=self.fixture.root / "settings.json",
                                  config_provider=lambda: self.fixture.config)
        self.app.conversation_store = self.case.store

    def fail_first(self, *, stored_question=None, blocked=False):
        self.fixture.question = "root.cbl 新的第五及第六项抵扣条件如何影响前面规则？"
        question = self.fixture.question
        job = "first-failed-job"
        self.case.store.append(self.identifier, "user", stored_question or question, run_id=job, status="pending")
        ordinary_requests = []

        def respond(payload, _number):
            if payload.get("schema") == "business-analysis-compaction/v1":
                return self.case.case.compact_reply(payload)
            ordinary_requests.append(payload)
            for page in self.fixture.pages(payload):
                if page["relative_path"] == "helper-1.cbl":
                    self.case.case.early_id = page["evidence_id"]
            if len(ordinary_requests) <= 4:
                return self.fixture.read_helper(len(ordinary_requests))
            return "SYNTHETIC_HTTP_FAILURE"

        def response(status, body):
            if status == 200 and json.loads(body)["choices"][0]["message"]["content"] == "SYNTHETIC_HTTP_FAILURE":
                return TransportResponse(500, '{"error":{"code":"internal_error"}}')
            return TransportResponse(status, body)

        with patch.object(transport_fixture, "TransportResponse", side_effect=response):
            failed = self.fixture.ask(respond, policy=self.case.case.policy, request_guard=7,
                                      conversation_id=self.identifier)
        self.assertFalse(failed["answer_available"])
        self.assertEqual(failed["answer_origin"], "diagnostic")
        saved = self.case.case.saved(failed)
        self.assertEqual(saved["uncertain_requests"], [])
        self.assertEqual(len(saved["checkpoint"]["prior_source_memory"]["analysis_ledger"]["generations"]), 2)
        self.app.conversation = self.case.store.get(self.identifier)
        project = {"runner_status": "COMPLETED", "agent_result": failed}
        if blocked:
            project["reason_code"] = "REMOTE_REQUEST_OUTCOME_UNKNOWN"
        self.app._record_answer({"conversation_id": self.identifier, "question": question,
                                 "conversation_history": []}, job, {"agent": project})
        self.assertEqual([row["status"] for row in self.case.store.get(self.identifier)["messages"]],
                         ["failed", "failed"])
        return failed

    def followup(self, question, *, inherited):
        history = self.case.store.model_history(self.identifier, max_characters=80, question=question,
                                               include_analysis_handoff=True)
        self.assertFalse(any(row["role"] == "assistant" for row in history))
        self.case.store.append(self.identifier, "user", question, run_id="new-question-job", status="pending")
        self.fixture.question = question
        observed = []

        def respond(payload, _number):
            observed.append(payload)
            answer = self.fixture.cited_answer(payload)
            if inherited:
                answer += f"早先抵扣仍要求尚有应收余额。[{self.case.case.early_id}]"
            return answer

        result = self.fixture.ask(respond, policy=replace(self.case.case.policy, max_model_requests=1),
            request_guard=len(self.fixture.requests) + 1, history=history, conversation_id=self.identifier)
        self.assertTrue(result["answer_available"], result.get("errors"))
        self.assertEqual(len(observed), 1)
        self.assertNotIn("draft_answer", observed[0])
        return result, observed[0], self.case.case.saved(result)

    def test_first_diagnostic_only_turn_passes_verified_generations_to_a_deictic_followup(self):
        failed = self.fail_first()
        before = self.case.case.saved(failed)
        result, payload, saved = self.followup("这个流程前面的抵扣还需要什么前提？", inherited=True)
        ledger = payload["analysis_ledger"]
        self.assertEqual(ledger["generation"], 2)
        self.assertTrue(any("尚有应收余额" in row["text"] for row in ledger["entries"]))
        self.assertIn(self.case.case.early_id, ledger["historical_source_references"])
        self.assertIn(self.case.case.early_id, {row["evidence_id"] for row in result["narrative"]["citations"]})
        self.assertEqual(saved["checkpoint"]["conversation_analysis_handoff"]["from_run_id"], failed["run_id"])
        self.assertNotEqual(saved["run_id"], failed["run_id"])
        self.assertEqual(len(saved["requests"]), 1)
        self.assertEqual(self.case.case.saved(failed), before)

    def test_first_diagnostic_with_a_different_original_question_cannot_supply_memory(self):
        self.fail_first(stored_question="root.cbl 第一项抵扣有哪些条件？")
        _, payload, saved = self.followup("root.cbl 前面有哪些抵扣条件？", inherited=False)
        self.assertFalse(payload.get("analysis_ledger", {}).get("generation"))
        self.assertIsNone(saved["checkpoint"].get("conversation_analysis_handoff"))

    def test_first_diagnostic_with_unknown_request_marker_cannot_supply_memory(self):
        self.fail_first(blocked=True)
        _, payload, saved = self.followup("root.cbl 前面有哪些抵扣条件？", inherited=False)
        self.assertFalse(payload.get("analysis_ledger", {}).get("generation"))
        self.assertIsNone(saved["checkpoint"].get("conversation_analysis_handoff"))

    def test_first_diagnostic_with_uncertain_saved_request_cannot_supply_memory(self):
        failed = self.fail_first()
        store = RunStore(self.fixture.root / "investigation-runs.sqlite")
        store.start_request(failed["run_id"], "uncertain-request", "a" * 64)
        self.assertTrue(store.load(failed["run_id"])["uncertain_requests"])
        _, payload, saved = self.followup("root.cbl 前面有哪些抵扣条件？", inherited=False)
        self.assertFalse(payload.get("analysis_ledger", {}).get("generation"))
        self.assertIsNone(saved["checkpoint"].get("conversation_analysis_handoff"))

    def test_first_diagnostic_with_changed_source_cannot_supply_memory(self):
        self.fail_first()
        path = self.fixture.source / "helper-1.cbl"
        original = path.read_text()
        changed = original.replace("SUBTRACT 1", "SUBTRACT 9")
        self.assertNotEqual(changed, original)
        path.write_text(changed)
        _, payload, saved = self.followup("root.cbl 前面有哪些抵扣条件？", inherited=False)
        self.assertFalse(payload.get("analysis_ledger", {}).get("generation"))
        self.assertIsNone(saved["checkpoint"].get("conversation_analysis_handoff"))
