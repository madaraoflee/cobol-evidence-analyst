"""A received malformed-call response cannot reset its recovery allowance."""

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from business_chat import run_business_chat
from business_index import build_business_index
from company_api import TransportResponse
from quality_trace import QualityTrace
from repository_discovery import ensure_repository_search
from run_store import RunStore
import test_completion_driven_chat as fixtures
from test_business_chat_markup_protocol import machine_read


class ReceivedResponseInterrupted(RuntimeError):
    pass


class MarkupProtocolResumeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CompletionDrivenChatTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.requests = []
        self.messages = []
        self.instructions = "先解释金额处理规则，保留原文引用。"
        build_business_index(self.fixture.source, self.fixture.database, source_format="free",
                             verify_content=True, framework_reference_path="", quiet=True)
        ensure_repository_search(self.fixture.database, self.fixture.source)

    def ask(self, respond):
        def transport(request):
            messages = json.loads(request.body)["messages"]
            payload = json.loads(messages[-1]["content"])
            self.messages.append(messages)
            self.requests.append(payload)
            if len(self.requests) > 3:
                raise AssertionError("Malformed-call recovery failed to converge")
            text = respond(payload, len(self.requests))
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "content": text}, "finish_reason": "stop"}]}, ensure_ascii=False))

        return run_business_chat(self.fixture.question, self.fixture.database, self.fixture.source,
            self.fixture.config, transport=transport, framework_reference_path="",
            answer_instructions=self.instructions,
            conversation_id="markup-recovery-resume")["agent_result"]

    def saved(self):
        store = RunStore(self.fixture.root / "investigation-runs.sqlite")
        with store.connect() as database:
            run_id = database.execute("SELECT run_id FROM investigation_runs").fetchone()[0]
        return store.load(run_id)

    def interrupt_after_received(self, request_number):
        record = QualityTrace.record_remote_response

        def record_then_interrupt(trace, response):
            record(trace, response)
            if len(self.requests) == request_number:
                raise ReceivedResponseInterrupted("Received response has not been processed")

        return patch.object(QualityTrace, "record_remote_response", record_then_interrupt)

    def assert_original_context(self):
        self.assertTrue(all(payload["question"] == self.fixture.question for payload in self.requests))
        self.assertTrue(all(messages[0]["role"] == "system" and
                            messages[0]["content"].count(self.instructions) == 1
                            for messages in self.messages))
        self.assertTrue(all(any("COMPUTE TOTAL-AMOUNT = BASE-AMOUNT * 2" in page["source_text"]
                                for page in self.fixture.pages(payload)) for payload in self.requests))
        self.assertFalse(any(action.get("read", {}).get("relative_path") == "helper-7.cbl"
                             for payload in self.requests for action in payload["completed_actions"]))

    def test_second_received_bad_packet_replays_without_a_third_provider_request(self):
        with self.interrupt_after_received(2), self.assertRaises(ReceivedResponseInterrupted):
            self.ask(lambda *_: machine_read())
        before = self.saved()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(before["checkpoint"]["processed_model_round"], 1)
        self.assertTrue(before["checkpoint"]["investigation_action_recovery_attempted"])
        self.assertTrue(before["checkpoint"]["markup_protocol_recovery_pending"])
        self.assertFalse(before["uncertain_requests"])

        result = self.ask(lambda payload, _: self.fixture.cited_answer(payload))
        self.assertEqual(len(self.requests), 2, "Replaying a failed correction must not grant another request")
        self.assertEqual(result["stop_reason"], "INVALID_INVESTIGATION_ACTION")
        self.assertEqual(result["metrics"]["model_requests"], 2)
        self.assertNotIn("<wire:", result["answer"])
        self.assertEqual(self.saved()["state"], "completed")
        self.assertFalse(self.saved()["uncertain_requests"])
        self.assert_original_context()

    def test_first_received_bad_packet_still_earns_its_one_correction_after_replay(self):
        with self.interrupt_after_received(1), self.assertRaises(ReceivedResponseInterrupted):
            self.ask(lambda *_: machine_read())
        before = self.saved()
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(before["checkpoint"].get("investigation_action_recovery_attempted"))
        self.assertFalse(before["uncertain_requests"])

        result = self.ask(lambda payload, _: self.fixture.cited_answer(payload))
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["status"], "ANALYZED")
        self.assertIn("金额按基础金额乘以二得到20", result["answer"])
        self.assertIn("上一轮返回的标签调用格式无效", self.requests[1]["task"])
        self.assertGreater(self.requests[1]["investigation_budget"]["reads_per_turn"], 0)
        self.assertTrue(result["narrative"]["citations"])
        after = self.saved()
        self.assertTrue(after["checkpoint"]["investigation_action_recovery_attempted"])
        self.assertFalse(after["checkpoint"]["markup_protocol_recovery_pending"])
        self.assertFalse(after["uncertain_requests"])
        self.assert_original_context()


if __name__ == "__main__":
    unittest.main()
