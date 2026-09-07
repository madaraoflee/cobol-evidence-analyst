from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_loop import BoundedAgentLoop, QUESTION_COVERAGE_MESSAGE  # noqa: E402
from test_agent_claim_support import (  # noqa: E402
    CompleteComputeTools,
    structured_claim,
)
from test_agent_loop import FakeClient, FakeTools, json_action  # noqa: E402


def prose_claim(kind: str, text: str) -> dict[str, object]:
    return {
        "kind": kind,
        "claim": text,
        "support_status": "partial",
        "evidence_ids": ["ev_OUT-AMOUNT"],
    }


class AgentAnswerScopeTests(unittest.TestCase):
    def run_claims(
        self,
        claims: list[dict[str, object]],
        *,
        question: str = "金额怎样计算？",
        final_changes: dict[str, object] | None = None,
    ) -> dict[str, object]:
        arguments = {
            "claims": copy.deepcopy(claims),
            "evidence_ids": ["ev_OUT-AMOUNT"],
            "boundaries": [],
        }
        arguments.update(final_changes or {})
        responses = [
            json_action("search_code", {"query": "OUT-AMOUNT"}),
            json_action("read_evidence", {"evidence_ids": ["ev_OUT-AMOUNT"]}),
            json_action("final_answer", arguments),
        ]
        return BoundedAgentLoop(
            FakeClient(responses), CompleteComputeTools()
        ).run(question)

    def assert_question_unassessed(self, result: dict[str, object]) -> None:
        coverage = result["question_coverage"]
        self.assertEqual(coverage["status"], "not_assessed")
        self.assertEqual(coverage["reason_code"], "QUESTION_COVERAGE_NOT_CHECKED")
        self.assertIs(coverage["question_relevance_checked"], False)
        self.assertIs(coverage["business_completeness_checked"], False)
        self.assertEqual(coverage["message"], QUESTION_COVERAGE_MESSAGE)
        self.assertIn(QUESTION_COVERAGE_MESSAGE, result["answer"])
        self.assertEqual(result["stop_reason_scope"], "investigation_loop")

    def test_matching_statement_never_certifies_question_coverage(self) -> None:
        results = []
        for question in ("金额怎样计算？", "有效附加保障的参与条件是什么？"):
            with self.subTest(question=question):
                result = self.run_claims([structured_claim()], question=question)
                self.assertEqual(result["status"], "SUPPORTED_WITH_BOUNDARIES")
                self.assertEqual(result["stop_reason"], "completed")
                self.assertTrue(result["claims_semantically_verified"])
                self.assert_question_unassessed(result)
                self.assertLess(
                    result["answer"].index(QUESTION_COVERAGE_MESSAGE),
                    result["answer"].index("已核验所引源码中的单条计算语句"),
                )
                results.append(result)
        # A scripted planner can return the same true fact to an unrelated
        # question. Do not convert statement support into even partial coverage.
        self.assertEqual(results[0]["claims"], results[1]["claims"])
        self.assertEqual(results[0]["question_coverage"], results[1]["question_coverage"])

    def test_model_cannot_declare_coverage_complete(self) -> None:
        for additions in (
            {"question_coverage": {"status": "complete"}},
            {"stop_reason_scope": "complete_business_answer"},
        ):
            with self.subTest(additions=additions):
                result = self.run_claims([structured_claim()], final_changes=additions)
                self.assertEqual(result["status"], "ABSTAINED")
                self.assertEqual(result["stop_reason"], "invalid_final_answer")
                self.assertEqual(result["claims"], [])
                self.assert_question_unassessed(result)

    def test_empty_abstention_and_safety_stops_keep_question_scope(self) -> None:
        scenarios = (
            (
                "abstain",
                {"claims": [], "evidence_ids": [], "boundaries": ["缺少运行期参数。"]},
                "model_abstained",
            ),
            ("unapproved_action", {}, "unauthorized_tool"),
        )
        for action, arguments, reason in scenarios:
            with self.subTest(reason=reason):
                result = BoundedAgentLoop(
                    FakeClient([json_action(action, arguments)]), FakeTools()
                ).run("最终结果是多少？")
                self.assertEqual(result["status"], "ABSTAINED")
                self.assertEqual(result["stop_reason"], reason)
                self.assertEqual(result["claims"], [])
                self.assert_question_unassessed(result)

    def test_unsupported_formula_is_not_released_as_partial_answer(self) -> None:
        result = self.run_claims(
            [structured_claim(expression=["IN-AMOUNT", "+", "1"])]
        )
        self.assertEqual(result["stop_reason"], "unsupported_claim")
        self.assertEqual(result["claims"], [])
        self.assert_question_unassessed(result)

    def test_inferences_and_open_questions_are_shown_as_unverified(self) -> None:
        inference = "该中间金额可能表示调整前的金额。"
        question = "仍需确认运行期是否进入这一计算分支？"
        result = self.run_claims([
            structured_claim(),
            prose_claim("business_inference", inference),
            prose_claim("open_question", question),
        ])
        self.assertEqual(result["status"], "CITATION_VERIFIED_ONLY")
        self.assertFalse(result["claims_semantically_verified"])
        self.assertEqual(result["claims"][0]["support_status"], "supported")
        self.assertIn("## 业务推测（未核验）\n\n", result["answer"])
        self.assertIn("[业务推测；语义未核验] " + inference, result["answer"])
        self.assertIn("## 待确认问题\n\n", result["answer"])
        self.assertIn("[待确认问题；未形成结论] " + question, result["answer"])
        self.assert_question_unassessed(result)

    def test_inference_markup_is_escaped_and_does_not_form_a_link(self) -> None:
        result = self.run_claims([
            prose_claim("business_inference", "[查看](https://example.invalid) <b>推测</b>"),
        ])
        self.assertEqual(result["status"], "CITATION_VERIFIED_ONLY")
        self.assertIn("\\[查看\\](https://example.invalid)", result["answer"])
        self.assertIn("\\<b\\>推测\\</b\\>", result["answer"])
        self.assertNotIn("[查看](https://example.invalid)", result["answer"])

    def test_non_fact_claims_keep_the_existing_output_budget(self) -> None:
        result = self.run_claims([
            prose_claim("business_inference", "推" * 310),
            prose_claim("open_question", "问" * 310),
            prose_claim("business_inference", "测" * 310),
            prose_claim("open_question", "疑" * 310),
        ])
        self.assertEqual(result["status"], "ABSTAINED")
        self.assertEqual(result["stop_reason"], "model_output_budget_exceeded")
        self.assertEqual(result["claims"], [])
        self.assertNotIn("推" * 310, result["answer"])
        self.assert_question_unassessed(result)

    def test_non_fact_claim_cannot_be_model_certified_supported(self) -> None:
        for kind in ("business_inference", "open_question"):
            with self.subTest(kind=kind):
                claim = prose_claim(kind, "待业务确认的解释。")
                claim["support_status"] = "supported"
                result = self.run_claims([claim])
                self.assertEqual(result["stop_reason"], "invalid_final_answer")
                self.assertEqual(result["claims"], [])
                self.assert_question_unassessed(result)


if __name__ == "__main__":
    unittest.main()
