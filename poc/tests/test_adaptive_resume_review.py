"""Independent end-to-end recovery checks for adaptive investigations."""

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

import test_completion_driven_chat as fixtures
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import TransportResponse
from quality_trace import QualityTrace
from repository_discovery import ensure_repository_search
from run_store import RunStore


class UserCancelled(RuntimeError):
    pass


class AdaptiveResumeReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CompletionDrivenChatTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.question = "root.cbl 的金额如何计算，输入初值来自哪里？"
        self.requests = []
        build_business_index(self.fixture.source, self.fixture.database, source_format="free",
                             verify_content=True, framework_reference_path="", quiet=True)
        ensure_repository_search(self.fixture.database, self.fixture.source)

    def ask(self, respond, progress=lambda event: None):
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            self.requests.append(payload)
            self.assertEqual(payload["question"], self.fixture.question)
            self.assertLessEqual(len(self.requests), 5)
            response = respond(payload, len(self.requests))
            text, finish = response if isinstance(response, tuple) else (response, "stop")
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "content": text}, "finish_reason": finish}]}, ensure_ascii=False))

        return run_business_chat(self.fixture.question, self.fixture.database, self.fixture.source,
            self.fixture.config, framework_reference_path="", transport=transport, progress=progress)["agent_result"]

    @staticmethod
    def cancel_before_second_request(event):
        if event.get("phase") == "waiting_for_model" and event.get("model_requests") == 2:
            raise UserCancelled("Cancelled before the second remote request")

    def saved(self):
        store = RunStore(self.fixture.database.parent / "investigation-runs.sqlite")
        with store.connect() as database:
            run_id = database.execute("SELECT run_id FROM investigation_runs").fetchone()[0]
        return store.load(run_id)

    def test_processed_draft_resumes_next_review_instead_of_replaying_prior_gap(self):
        def respond(payload, number):
            if number == 1:
                return self.fixture.cited_answer(payload, "金额按基础金额乘以二得到。")
            return self.fixture.cited_answer(payload)

        with self.assertRaises(UserCancelled):
            self.ask(respond, self.cancel_before_second_request)
        saved = self.saved()
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(saved["checkpoint"].get("accepted_draft"))
        self.assertFalse(saved["uncertain_requests"])
        result = self.ask(respond)
        self.assertEqual(len(self.requests), 2)
        self.assertIn("初值为10", result["answer"])
        self.assertNotIn("inputs", result["business_review"]["answer_completion"]["missing_aspects"])

    def test_processed_tool_response_does_not_repeat_its_read_on_resume(self):
        def respond(payload, number):
            return self.fixture.read_helper(1) if number == 1 else self.fixture.cited_answer(payload)

        with self.assertRaises(UserCancelled):
            self.ask(respond, self.cancel_before_second_request)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.saved()["uncertain_requests"])
        result = self.ask(respond)
        self.assertEqual(len(self.requests), 2)
        reads = [action for action in self.requests[-1]["completed_actions"]
                 if action.get("read", {}).get("relative_path") == "helper-1.cbl"]
        self.assertEqual(len(reads), 1, "A locally processed tool response must not be replayed")
        self.assertTrue(result["answer_available"])

    def test_unprocessed_durable_response_replays_without_another_provider_request(self):
        record_response = QualityTrace.record_remote_response

        def record_then_cancel(trace, response):
            record_response(trace, response)
            raise UserCancelled("Cancelled after receiving but before processing the response")

        respond = lambda payload, number: self.fixture.cited_answer(payload)
        with patch.object(QualityTrace, "record_remote_response", record_then_cancel):
            with self.assertRaises(UserCancelled):
                self.ask(respond)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.saved()["uncertain_requests"])
        result = self.ask(respond)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("初值为10", result["answer"])
        self.assertTrue(result["answer_available"])

    def test_cancelled_final_verification_resumes_without_a_new_provider_request(self):
        def cancel_during_verification(event):
            if event.get("phase") == "verifying_answer":
                raise UserCancelled("Cancelled during local final verification")

        def respond(payload, number):
            self.assertEqual(number, 1, "An already complete answer only needs local verification")
            return self.fixture.cited_answer(payload)

        with self.assertRaises(UserCancelled):
            self.ask(respond, cancel_during_verification)
        self.assertEqual(len(self.requests), 1)
        saved = self.saved()
        self.assertTrue(saved["checkpoint"].get("accepted_draft"))
        self.assertFalse(saved["uncertain_requests"])
        result = self.ask(respond)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["status"], "ANALYZED")
        self.assertIn("初值为10", result["answer"])
        self.assertIn("乘以二", result["answer"])
        self.assertFalse(result["business_review"]["answer_completion"]["missing_aspects"])

    def test_unprocessed_continuation_replays_with_original_draft_and_no_extra_request(self):
        record_response = QualityTrace.record_remote_response

        def respond(payload, number):
            self.assertLessEqual(number, 2, "A durable continuation must not require another request")
            if number == 1:
                return self.fixture.cited_answer(payload), "length"
            return self.fixture.cited_answer(payload, "补充说明：本段没有后续覆盖。"), "stop"

        def record_then_cancel(trace, response):
            record_response(trace, response)
            if len(self.requests) == 2:
                raise UserCancelled("Cancelled after receiving the continuation")

        with patch.object(QualityTrace, "record_remote_response", record_then_cancel):
            with self.assertRaises(UserCancelled):
                self.ask(respond)
        self.assertEqual(len(self.requests), 2)
        self.assertFalse(self.saved()["uncertain_requests"])
        result = self.ask(respond)
        self.assertEqual(len(self.requests), 2)
        self.assertIn("初值为10", result["answer"])
        self.assertIn("乘以二", result["answer"])
        self.assertIn("补充说明", result["answer"])
        self.assertFalse(result["business_review"]["answer_completion"]["missing_aspects"])


if __name__ == "__main__":
    unittest.main()
