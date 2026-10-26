"""Saved action queues use fresh turn allowances without dropping distinct work."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_chat import run_business_chat
from company_api import TransportResponse
import test_business_action_budget as fixtures


class PendingActionAllowanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BusinessActionBudgetTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def run_queue(self, frontier):
        f = self.fixture
        requests = []

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            if len(requests) == 1:
                text = json.dumps({"search_concepts": "amount"})
            else:
                page = next(page for bundle in payload["source_context"] for page in bundle["pages"]
                            if page["relative_path"] == "detail.cbl")
                text = f"补充程序用附加金额乘以三，计算最终金额。[{page['evidence_id']}]"
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text},
                                                                   "finish_reason": "stop"}]}))

        result = run_business_chat("VALUEFLOW 的金额如何计算？", f.database, f.source, f.config,
            transport=transport, framework_reference_path="", answer_detail="brief",
            investigation_seed={"frontier": frontier},
            policy=replace(f.policy, max_model_requests=3, max_business_context_actions_per_turn=1,
                           max_evidence_groups=8))["agent_result"]
        self.assertEqual(len(requests), 2)
        self.assertTrue(result["model_answer_recorded"])
        return requests, result

    def test_distinct_pending_focus_waits_for_next_turn_and_executes_once(self):
        frontier = [{"tool": "focus", "arguments": [path]} for path in ("rule.cbl", "detail.cbl")]
        requests, result = self.run_queue(frontier)
        first = [row["focus"] for row in requests[0]["completed_actions"] if row.get("focus")]
        second = [row["focus"] for row in requests[1]["completed_actions"] if row.get("focus")]
        self.assertEqual(first, [["rule.cbl"]])
        self.assertEqual(second, [["rule.cbl"], ["detail.cbl"]])
        self.assertEqual(result["metrics"]["tool_calls"]["search"], 2)
        self.assertTrue(any(page["relative_path"] == "detail.cbl"
                            for bundle in requests[1]["source_context"] for page in bundle["pages"]))

    def test_pending_inspection_without_read_fallback_waits_for_fresh_allowance(self):
        frontier = [{"tool": "inspect_business_context", "arguments": {"relative_path": path, "line": 9}}
                    for path in ("rule.cbl", "detail.cbl")]
        requests, result = self.run_queue(frontier)
        def inspected(payload):
            return [row["inspect_business_context"]["relative_path"] for row in payload["completed_actions"]
                    if row.get("inspect_business_context")]
        self.assertEqual(inspected(requests[0]), ["rule.cbl"])
        self.assertEqual(inspected(requests[1]), ["rule.cbl", "detail.cbl"])
        self.assertEqual(result["metrics"]["tool_calls"]["inspect_business_context"], 2)
        self.assertTrue(any("COMPUTE FINAL-AMOUNT = EXTRA-AMOUNT * 3." in page["source_text"]
                            for bundle in requests[1]["source_context"] for page in bundle["pages"]))


if __name__ == "__main__":
    unittest.main()
