"""Received answers survive optional work; planning leaves time for delivery."""

import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from agent_policy import AgentPolicy
from analyze_source import AnalysisCancelled
import business_chat
from business_index import build_business_index
import evidence_answer
import question_investigation
import repository_discovery
from quality_trace import QualityTrace
from run_store import RunStore
import test_business_chat_latency as latency_fixtures


class ReceivedAnswerDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = latency_fixtures.BusinessChatLatencyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def ask(self, **kwargs):
        kwargs.setdefault("policy", AgentPolicy(max_question_seconds=180))
        return self.fixture.ask(**kwargs)

    def test_received_answer_is_saved_before_optional_read_reaches_deadline(self):
        fixture = self.fixture
        original_plan = question_investigation.build_question_investigation
        original_read = repository_discovery.read_repository_context
        reads = []

        def plan(*args, **kwargs):
            result = original_plan(*args, **kwargs)
            return {**result, "can_answer": False, "planned_actions": [{"tool": "read",
                "arguments": {"relative_path": "rule.cbl", "start_line": 2, "end_line": 2},
                "reason": "neutral_program_context"}]}

        def read(*args, **kwargs):
            if fixture.requests:
                reads.append(fixture.clock_value)
                fixture.clock_value = 181.0
                kwargs["check_cancel"]()
            return original_read(*args, **kwargs)

        def respond(payload):
            fixture.clock_value = 119.0
            return "已确认相关程序为 BILL-RULE；这段已收到的业务说明应保留。"

        with mock.patch.object(question_investigation, "build_question_investigation", side_effect=plan), \
                mock.patch.object(repository_discovery, "read_repository_context", side_effect=read), \
                mock.patch.object(business_chat, "wants_business_detail", return_value=False):
            result = self.ask(question="BILL-RULE 的相关程序有哪些？", respond=respond)
        self.assertEqual(reads, [119.0])
        self.assertEqual(len(fixture.requests), 1)
        self.assertTrue(result["model_answer_recorded"])
        self.assertEqual(result["answer_origin"], "model")
        self.assertIn("已收到的业务说明应保留", result["answer"])
        self.assertEqual(result["stop_reason"], "QUESTION_TIME_BUDGET_EXHAUSTED")
        saved = RunStore(fixture.root / "investigation-runs.sqlite").load(result["run_id"])
        self.assertEqual(saved["requests"][0]["state"], "completed")
        self.assertIn("已收到的业务说明应保留", saved["checkpoint"]["accepted_draft"]["answer"])

    def test_slow_planning_response_makes_second_request_answer_but_keeps_cheap_read(self):
        fixture = self.fixture
        reads = []
        original_read = repository_discovery.read_repository_context

        def read(*args, **kwargs):
            if fixture.requests:
                reads.append(fixture.clock_value)
                fixture.clock_value += 1
            return original_read(*args, **kwargs)

        def respond(payload):
            fixture.clock_value += 60
            if len(fixture.requests) == 1:
                return json.dumps({"read": [{"relative_path": "rule.cbl", "start_line": 2, "end_line": 2}]})
            self.assertEqual(payload["investigation_budget"]["remaining_model_requests"], 1)
            self.assertEqual(payload["investigation_budget"]["reads_per_turn"], 0)
            return fixture.answer(payload)

        with mock.patch.object(repository_discovery, "read_repository_context", side_effect=read):
            result = self.ask(respond=respond)
        self.assertEqual(reads, [60.0])
        self.assertEqual(len(fixture.requests), 2)
        self.assertTrue(result["model_answer_recorded"])
        self.assertLess(result["metrics"]["elapsed_seconds"], 180)
        self.assertEqual(result["metrics"]["policy"]["hard_question_seconds"], 180)

    def test_planning_reply_in_final_answer_reserve_does_not_start_more_tools(self):
        fixture = self.fixture
        reads = []
        original_read = repository_discovery.read_repository_context

        def read(*args, **kwargs):
            if fixture.requests:
                reads.append(fixture.clock_value)
            return original_read(*args, **kwargs)

        def respond(payload):
            if len(fixture.requests) == 1:
                fixture.clock_value = 135
                return json.dumps({"read": [{"relative_path": "rule.cbl", "start_line": 2, "end_line": 2}]})
            fixture.clock_value = 160
            self.assertEqual(payload["investigation_budget"]["reads_per_turn"], 0)
            return fixture.answer(payload)

        with mock.patch.object(repository_discovery, "read_repository_context", side_effect=read):
            result = self.ask(respond=respond)
        self.assertEqual(reads, [])
        self.assertEqual(len(fixture.requests), 2)
        self.assertTrue(result["model_answer_recorded"])

    def test_received_answer_preview_cannot_discard_journaled_response_at_deadline(self):
        fixture = self.fixture
        events = []
        original_run = business_chat.run_business_chat
        original_record = QualityTrace.record_remote_response

        def run(*args, **kwargs):
            return original_run(*args, progress=events.append, capture_api_responses=True, **kwargs)

        def recorded(trace, response):
            original_record(trace, response)
            # The response arrived before expiry. Only local journaling and
            # rendering cross the deadline; the transport remains bounded.
            fixture.clock_value = 181

        with mock.patch.object(business_chat, "run_business_chat", side_effect=run), \
                mock.patch.object(QualityTrace, "record_remote_response", new=recorded):
            result = self.ask(policy=AgentPolicy(max_model_requests=1, max_question_seconds=180))
        self.assertEqual(len(fixture.requests), 1)
        self.assertTrue(result["model_answer_recorded"])
        self.assertTrue(any(event.get("answer_preview") for event in events))
        self.assertIn("本金加费用", result["answer"])

    def test_user_cancellation_still_wins_over_received_answer_delivery(self):
        fixture = self.fixture
        cancelled = AnalysisCancelled("Neutral user cancellation")

        def cancel():
            if fixture.requests:
                raise cancelled

        with self.assertRaises(AnalysisCancelled) as caught:
            fixture.ask(check_cancel=cancel)
        self.assertIs(caught.exception, cancelled)

    def test_expiry_before_first_send_does_not_treat_prepared_source_as_supplied(self):
        fixture = self.fixture
        original_prepare = QualityTrace.prepare
        original_builder = evidence_answer.build_evidence_answer
        prepared_pages, fallback_pages = [], []

        def prepared(trace, **options):
            original_prepare(trace, **options)
            prepared_pages.extend(fixture.pages(options["payload"]))
            fixture.clock_value = 181

        def render(question, pages, investigation, **options):
            fallback_pages.extend(pages)
            return original_builder(question, pages, investigation, **options)

        with mock.patch.object(QualityTrace, "prepare", new=prepared), \
                mock.patch.object(evidence_answer, "build_evidence_answer", side_effect=render):
            result = self.ask()
        self.assertTrue(prepared_pages)
        self.assertEqual(fixture.requests, [])
        self.assertEqual(fallback_pages, [])
        self.assertEqual(result["answer_origin"], "diagnostic")
        self.assertFalse(result["model_answer_recorded"])
        self.assertEqual(result["stop_reason"], "QUESTION_TIME_BUDGET_EXHAUSTED")
        saved = RunStore(fixture.root / "investigation-runs.sqlite").load(result["run_id"])
        self.assertEqual(saved["requests"], [])

    def test_expiry_before_followup_send_keeps_previous_request_binding(self):
        fixture = self.fixture
        (fixture.source / "extra.cbl").write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. EXTRA-PROCESS.\n"
            "PROCEDURE DIVISION.\nDISPLAY 'EXTRA'.\nGOBACK.\n", encoding="utf-8")
        build_business_index(fixture.source, fixture.database, source_format="free", verify_content=True,
                             framework_reference_path="")
        repository_discovery.ensure_repository_search(fixture.database, fixture.source)
        original_prepare = QualityTrace.prepare
        original_builder = evidence_answer.build_evidence_answer
        prepared, fallback_pages = [], []

        def preparing(trace, **options):
            original_prepare(trace, **options)
            prepared.append(options["payload"])
            if len(prepared) == 2:
                fixture.clock_value = 181

        def render(question, pages, investigation, **options):
            fallback_pages.extend(pages)
            return original_builder(question, pages, investigation, **options)

        with mock.patch.object(QualityTrace, "prepare", new=preparing), \
                mock.patch.object(evidence_answer, "build_evidence_answer", side_effect=render):
            result = self.ask(respond=lambda payload: json.dumps({"read": [
                {"relative_path": "extra.cbl", "start_line": 1, "end_line": 5}]}))
        self.assertEqual(len(fixture.requests), 1)
        self.assertEqual(len(prepared), 2)
        supplied = {page["evidence_id"] for page in fixture.pages(fixture.requests[0])}
        unsent = {page["evidence_id"] for page in fixture.pages(prepared[1])} - supplied
        self.assertTrue(unsent)
        self.assertEqual({page["evidence_id"] for page in fallback_pages}, supplied)
        self.assertFalse(any(f"[{identifier}]" in result["answer"] for identifier in unsent))
        self.assertEqual(result["stop_reason"], "QUESTION_TIME_BUDGET_EXHAUSTED")


if __name__ == "__main__":
    unittest.main()
