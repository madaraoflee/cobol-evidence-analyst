"""Evidence progress, newcomer delivery, and bounded nonproductive planning."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[2]), str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from agent_policy import AgentPolicy
from company_api import APIClientError
from run_store import RunStore
import test_business_chat_latency as latency_fixtures
from tools.business_onboarding_fixture import fixture_identity
from tools.desktop_acceptance_business import business_policy_matches
from tools.desktop_acceptance_scenarios import run_scenarios


class BusinessFirstDeliveryTests(unittest.TestCase):
    def test_policy_probe_honors_explicit_six_calls_without_a_default_soft_target(self):
        defaults = AgentPolicy().to_dict()
        configured = {"max_model_requests": 6, "max_question_seconds": None,
                      "hard_question_seconds": None, "max_answer_revisions": 0}
        runtime = AgentPolicy(**configured).to_dict()
        self.assertIsNone(defaults["max_model_requests"])
        self.assertEqual(runtime["max_model_requests"], 6)
        self.assertIsNone(runtime["delivery_target_seconds"])
        self.assertIsNone(runtime["hard_question_seconds"])
        self.assertTrue(business_policy_matches(runtime, configured))
        self.assertTrue(business_policy_matches(defaults, {}))

    def test_policy_probe_rejects_hard_deadline_and_mismatched_or_missing_explicit_caps(self):
        configured = {"max_model_requests": 6, "max_investigation_rounds": 2,
                      "max_answer_revisions": 0}
        runtime = AgentPolicy(**configured).to_dict()
        self.assertTrue(business_policy_matches(runtime, configured))
        for change in ({"hard_question_seconds": 60}, {"max_model_requests": 8},
                       {"max_investigation_rounds": 6}, {"max_answer_revisions": 1}):
            with self.subTest(change=change):
                self.assertFalse(business_policy_matches({**runtime, **change}, configured))
        for field in ("hard_question_seconds", *configured):
            with self.subTest(missing=field):
                self.assertFalse(business_policy_matches({key: value for key, value in runtime.items()
                                                         if key != field}, configured))

    def test_default_answer_can_arrive_after_previous_total_deadline(self):
        fixture = latency_fixtures.BusinessChatLatencyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        def respond(payload):
            fixture.clock_value = 301
            return fixture.answer(payload)
        result = fixture.ask(respond=respond, policy=AgentPolicy(max_model_requests=1))
        self.assertTrue(result["model_answer_recorded"])
        self.assertIn("本金加费用", result["answer"])
        self.assertNotEqual(result["stop_reason"], "QUESTION_TIME_BUDGET_EXHAUSTED")
        self.assertIsNone(result["metrics"]["policy"]["hard_question_seconds"])
        self.assertGreaterEqual(result["metrics"]["elapsed_seconds"], 301)
        self.assertEqual(len(fixture.requests), 1)

    def test_repeated_read_without_new_evidence_converges_before_call_limit(self):
        fixture = latency_fixtures.BusinessChatLatencyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        def respond(payload):
            return json.dumps({"read": [{"relative_path": "rule.cbl", "start_line": 2, "end_line": 2}]})
        result = fixture.ask(question="BILL-AMOUNT 怎么计算？", respond=respond)
        self.assertLessEqual(len(fixture.requests), 3)
        self.assertEqual(fixture.requests[-1]["investigation_budget"]["reads_per_turn"], 0)
        self.assertIsNone(result["metrics"]["policy"]["hard_question_seconds"])
        self.assertNotEqual(result["stop_reason"], "QUESTION_TIME_BUDGET_EXHAUSTED")

    def test_hidden_output_limit_preserves_visible_text_and_prevents_unknown_resend(self):
        fixture = latency_fixtures.BusinessChatLatencyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        def respond(payload):
            failure = APIClientError("MODEL_REASONING_LIMIT_EXCEEDED")
            failure.remote_outcome_unknown = True
            failure.partial_response = {"choices": [{"message": {"role": "assistant",
                "content": fixture.answer(payload)}, "finish_reason": None}]}
            raise failure
        result = fixture.ask(respond=respond)
        self.assertEqual(len(fixture.requests), 1)
        self.assertTrue(result["model_answer_recorded"])
        self.assertTrue(result["answer_truncated"])
        self.assertIn("本金加费用", result["answer"])
        self.assertIsNone(result["finish_reason"])
        self.assertEqual(result["stop_reason"], "MODEL_REASONING_LIMIT_EXCEEDED")
        saved = RunStore(fixture.root / "investigation-runs.sqlite").load(result["run_id"])
        self.assertTrue(saved["uncertain_requests"])
        resumed = fixture.ask(respond=respond)
        self.assertEqual(len(fixture.requests), 1)
        self.assertFalse(resumed["resume_state"]["automatic_resend"])

    def test_business_onboarding_rehearses_actual_desktop_http_for_batch_and_query(self):
        root = Path(__file__).resolve().parents[2]
        manifest = json.loads((root / "docs/eval/desktop-joint-v1/manifest.json").read_text())
        selected = {"business_batch_overview", "business_online_overview"}
        cases = [case for case in manifest["cases"] if case["id"] in selected]
        self.assertEqual(len(cases), 2)
        for case in cases:
            self.assertEqual(case["fixture_sha256"], fixture_identity())
            self.assertEqual(case["repeats"], 3)
            self.assertEqual(case["policy"]["max_model_requests"], 6)
            self.assertIsNone(case["policy"]["hard_question_seconds"])
        with tempfile.TemporaryDirectory() as temporary:
            rows = run_scenarios([sys.executable, str(root / "poc/desktop_app.py")], temporary,
                {**manifest, "cases": [{**case, "repeats": 1} for case in cases]})
        self.assertEqual({row["id"] for row in rows}, selected)
        self.assertTrue(all(row["status"] == "passed" for row in rows),
                        [{key: row.get(key) for key in ("id", "error", "checks")} for row in rows])


if __name__ == "__main__":
    unittest.main()
