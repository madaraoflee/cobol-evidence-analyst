"""Continue one answer without reopening its consumed output allowance."""

import unittest

import test_completion_driven_chat as fixtures
from agent_policy import AgentPolicy
from run_store import RunStore


class AnswerOutputResumeTests(unittest.TestCase):
    def fixture(self):
        fixture = fixtures.CompletionDrivenChatTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_explicit_continue_preserves_exhausted_output_and_partial_answer(self):
        fixture = self.fixture()

        def truncated(payload, number):
            return (fixture.cited_answer(payload) if number == 1 else
                    f"\n\n业务说明片段 {number}。"), "length"

        prior = fixture.ask(truncated, request_guard=8)
        store = RunStore(fixture.root / "investigation-runs.sqlite")
        before = store.load(prior["run_id"])
        budget = before["checkpoint"]["answer_output_budget"]
        self.assertEqual(budget["output_requests"], budget["max_output_requests"])

        continued = fixture.ask(lambda *_: self.fail("An exhausted answer earned another request"),
            request_guard=8, resume_run_id=prior["run_id"], continue_investigation=True)
        self.assertEqual(len(fixture.requests), 8)
        self.assertEqual(continued["answer"], prior["answer"])
        self.assertTrue(continued["answer_truncated"])
        self.assertEqual(continued["stop_reason"], "MODEL_OUTPUT_TRUNCATED")
        retained = store.load(continued["run_id"])["checkpoint"]["answer_output_budget"]
        self.assertGreaterEqual(retained["output_characters"], budget["output_characters"])
        for field in ("max_output_characters", "max_output_requests", "output_requests", "output_ids"):
            self.assertEqual(retained[field], budget[field])
        self.assertEqual(store.load(prior["run_id"]), before)

    def test_explicit_continue_consumes_remaining_allowance_without_reset(self):
        fixture = self.fixture()

        def truncated(payload, number):
            return (fixture.cited_answer(payload) if number == 1 else
                    "\n\n金额处理的规则继续解释。"), "length"

        prior = fixture.ask(truncated, request_guard=2, policy=AgentPolicy(max_model_requests=2))
        store = RunStore(fixture.root / "investigation-runs.sqlite")
        original = store.load(prior["run_id"])["checkpoint"]["answer_output_budget"]
        self.assertEqual(original["output_requests"], 2)
        continued = fixture.ask(lambda *_: "\n\n本题已知金额结果为20。", request_guard=3,
            policy=AgentPolicy(max_model_requests=2), resume_run_id=prior["run_id"],
            continue_investigation=True)
        saved = store.load(continued["run_id"])["checkpoint"]["answer_output_budget"]
        self.assertEqual(saved["output_requests"], 3)
        self.assertEqual(saved["max_output_requests"], original["max_output_requests"])
        self.assertGreater(saved["output_characters"], original["output_characters"])
        self.assertTrue(set(original["output_ids"]) <= set(saved["output_ids"]))
        self.assertTrue(continued["answer"].startswith(prior["answer"]))
        self.assertFalse(continued["answer_truncated"])


if __name__ == "__main__":
    unittest.main()
