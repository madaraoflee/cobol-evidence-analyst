"""Slow useful investigations continue; complete answers need no extra round."""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

import business_chat
from agent_policy import AgentPolicy
from question_budget import QuestionBudget
from run_store import RunStore
import test_completion_driven_chat as fixtures


class DefaultInvestigationDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CompletionDrivenChatTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.question = "root.cbl 的金额如何计算，输入初值来自哪里？"
        self.now = 0.0
        self.clock = SimpleNamespace(monotonic=lambda: self.now,
                                     time=lambda: 2000000000 + self.now)

    def ask(self, *, initial_delay=0, response_delay=0, complete_first=False, policy=None):
        original_history = business_chat._history_messages

        def history(*args, **kwargs):
            # Advance the question clock at the end of real local context
            # assembly; no sleep or provider/network access is needed.
            self.now += initial_delay
            return original_history(*args, **kwargs)

        def budget(*args, **kwargs):
            return QuestionBudget(*args, **{**kwargs, "clock": self.clock.monotonic,
                                           "wall_clock": self.clock.time})

        def respond(payload, number):
            self.assertTrue(any("VALUE 10" in page["source_text"]
                                for page in self.fixture.pages(payload)))
            if number == 1:
                self.now += response_delay
                if not complete_first:
                    return self.fixture.cited_answer(payload, "金额按基础金额乘以二得到。")
            else:
                self.assertIn("乘以二", payload["draft_answer"])
                self.assertIn("inputs", payload["answer_review"]["missing_aspects"])
            return self.fixture.cited_answer(payload)

        with patch.object(business_chat, "time", self.clock), \
                patch.object(business_chat, "QuestionBudget", side_effect=budget), \
                patch.object(business_chat, "_history_messages", side_effect=history):
            return self.fixture.ask(respond, request_guard=2,
                                    **({"policy": policy} if policy is not None else {}))

    def assert_completed_with_review(self, result):
        self.assertEqual(len(self.fixture.requests), 2)
        self.assertEqual(result["status"], "ANALYZED")
        self.assertEqual(result["stop_reason"], "sufficient_material")
        self.assertEqual(result["business_review"]["answer_completion"]["missing_aspects"], [])
        self.assertIn("基础金额初值为10", result["answer"])
        self.assertIn("乘以二得到20", result["answer"])
        self.assertEqual(result["metrics"]["policy"]["max_investigation_rounds"], 6)
        self.assertEqual(result["metrics"]["policy"]["max_model_requests"], 8)
        self.assertIsNone(result["metrics"]["delivery_target"]["seconds"])

    def test_default_reviews_incomplete_first_answer_after_twenty_second_response(self):
        result = self.ask(response_delay=20)
        self.assert_completed_with_review(result)
        self.assertEqual(result["metrics"]["provider_request_timings"][0]["stage"], "investigate")

    def test_default_keeps_tools_and_review_after_sixteen_second_initial_context(self):
        result = self.ask(initial_delay=16)
        self.assert_completed_with_review(result)
        first = self.fixture.requests[0]["investigation_budget"]
        self.assertEqual(first["remaining_model_requests"], 8)
        self.assertGreater(first["searches_per_turn"], 0)
        self.assertGreater(first["reads_per_turn"], 0)
        saved = RunStore(self.fixture.root / "investigation-runs.sqlite").load(result["run_id"])
        self.assertEqual([row["stage"] for row in saved["checkpoint"]["quality"]["rounds"]],
                         ["investigate", "investigate"])

    def test_complete_answer_finishes_in_one_request_even_when_response_is_slow(self):
        result = self.ask(response_delay=120, complete_first=True)
        self.assertEqual(len(self.fixture.requests), 1)
        self.assertEqual(result["status"], "ANALYZED")
        self.assertEqual(result["stop_reason"], "sufficient_material")
        self.assertEqual(result["business_review"]["answer_completion"]["missing_aspects"], [])

    def test_explicit_delivery_target_still_stops_optional_review(self):
        result = self.ask(response_delay=20, policy=AgentPolicy(delivery_target_seconds=60))
        self.assertEqual(len(self.fixture.requests), 1)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["stop_reason"], "answer_incomplete")
        self.assertIn("inputs", result["business_review"]["answer_completion"]["missing_aspects"])
        self.assertEqual(result["metrics"]["delivery_target"]["seconds"], 60)


if __name__ == "__main__":
    unittest.main()
