"""Finishing streamed text does not finish an incomplete business answer."""

import json
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

import test_completion_driven_chat as fixtures
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import TransportResponse
from repository_discovery import ensure_repository_search


class AdaptivePostContinuationReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CompletionDrivenChatTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.question = "root.cbl 的金额如何计算，输入初值来自哪里？"
        self.requests = []
        build_business_index(self.fixture.source, self.fixture.database, source_format="free",
                             verify_content=True, framework_reference_path="", quiet=True)
        ensure_repository_search(self.fixture.database, self.fixture.source)

    def ask(self, respond_after_continuation, expected_requests):
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            self.requests.append(payload)
            number = len(self.requests)
            self.assertLessEqual(number, expected_requests, "Quality review did not converge")
            self.assertEqual(payload["question"], self.fixture.question)
            if number == 1:
                text = self.fixture.cited_answer(payload, "金额按基础金额乘以二得到。")
                finish = "length"
            elif number == 2:
                text = self.fixture.cited_answer(payload, "最终金额由该计算规则得到。")
                finish = "stop"
            else:
                text = respond_after_continuation(payload, number)
                finish = "stop"
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text},
                                        "finish_reason": finish}]}, ensure_ascii=False))

        result = run_business_chat(self.fixture.question, self.fixture.database, self.fixture.source,
            self.fixture.config, framework_reference_path="", transport=transport,
            progress=self.fixture.events.append)["agent_result"]
        # An injected transport assertion is converted into a safe failure by
        # the application; enforce the convergence bound outside that boundary.
        self.assertEqual(len(self.requests), expected_requests)
        self.assertNotIn("TRANSPORT_ERROR", result["stop_reason"])
        return result

    def assert_review_context(self, payload):
        self.assertIn("乘以二", payload["draft_answer"])
        self.assertIn("最终金额由该计算规则得到", payload["draft_answer"])
        self.assertIn("inputs", payload["answer_review"]["missing_aspects"])
        self.assertFalse(payload["analysis_memory"]["draft_is_evidence"])
        self.assertGreater(payload["investigation_budget"]["reads_per_turn"], 0)
        self.assertTrue(any("VALUE 10" in page["source_text"] for page in self.fixture.pages(payload)))

    def test_stopped_continuation_reenters_quality_review_and_completes_inputs(self):
        def respond(payload, number):
            self.assert_review_context(payload)
            return self.fixture.cited_answer(payload)

        result = self.ask(respond, expected_requests=3)
        self.assertIn("初值为10", result["answer"])
        self.assertIn("乘以二", result["answer"])
        self.assertFalse(result["answer_truncated"])
        self.assertFalse(result["business_review"]["answer_completion"]["missing_aspects"])

    def test_post_continuation_review_can_read_more_source_before_synthesis(self):
        def respond(payload, number):
            self.assert_review_context(payload)
            if number == 3:
                self.assertNotIn("helper-1.cbl", {page["relative_path"] for page in self.fixture.pages(payload)})
                return self.fixture.read_helper(1)
            helper = next(page for page in self.fixture.pages(payload)
                          if page["relative_path"] == "helper-1.cbl")
            self.assertEqual(helper["source_text"], self.fixture.files["helper-1.cbl"].rstrip("\n"))
            return self.fixture.cited_answer(payload)

        result = self.ask(respond, expected_requests=4)
        self.assertIn("初值为10", result["answer"])
        self.assertFalse(result["business_review"]["answer_completion"]["missing_aspects"])

    def test_same_missing_input_with_new_wording_stops_after_quality_review(self):
        def respond(payload, number):
            self.assert_review_context(payload)
            return self.fixture.cited_answer(payload, "计算规则是把基础金额翻倍，作为最终金额。")

        result = self.ask(respond, expected_requests=3)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("inputs", result["business_review"]["answer_completion"]["missing_aspects"])
        self.assertEqual(result["metrics"]["convergence_stop_reason"], "answer_review_no_progress")


if __name__ == "__main__":
    unittest.main()
