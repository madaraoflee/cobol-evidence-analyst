"""Repeated tool locations must not consume distinct-source reading slots."""

from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import _actions, run_business_chat
from company_api import TransportResponse
import test_business_action_budget as fixtures


class DuplicateActionReadTests(unittest.TestCase):
    def test_duplicate_reads_leave_allowance_for_the_next_distinct_location(self):
        first = {"relative_path": "detail.cbl", "start_line": 1, "end_line": 10}
        second = {"relative_path": "result.cbl", "start_line": 1, "end_line": 10}
        # JSON member order does not create a different source location.
        repeated = dict(reversed(list(first.items())))
        action = _actions(json.dumps({"read": [first, repeated, second]}),
                          AgentPolicy(max_reads_per_turn=2))
        self.assertEqual(action["read"], [first, second])

    def test_distinct_ranges_in_one_file_still_receive_separate_slots(self):
        first = {"relative_path": "detail.cbl", "start_line": 1, "end_line": 10}
        later = {"relative_path": "detail.cbl", "start_line": 11, "end_line": 20}
        action = _actions(json.dumps({"read": [first, later]}), AgentPolicy(max_reads_per_turn=2))
        self.assertEqual(action["read"], [first, later])

    def test_duplicate_inspections_leave_shared_allowance_for_another_tool(self):
        first = {"relative_path": "detail.cbl", "line": 9}
        later = {"relative_path": "result.cbl", "line": 9}
        action = _actions(json.dumps({"inspect_business_context": [first, first],
                                     "backward_slice": [later]}),
                          AgentPolicy(max_business_context_actions_per_turn=2))
        self.assertEqual(action["inspect_business_context"], [first])
        self.assertEqual(action["backward_slice"], [later])

    def test_duplicate_searches_leave_allowance_for_another_business_term(self):
        action = _actions(json.dumps({"search": ["POLICY", "POLICY", "BENEFIT"]}),
                          AgentPolicy(max_searches_per_turn=2))
        self.assertEqual(action["search"], ["POLICY", "BENEFIT"])

    def test_duplicate_framework_searches_leave_allowance_for_another_operation(self):
        action = _actions(json.dumps({"framework_search": ["FETCHROW", "FETCHROW", "HOLDROW"]}),
                          AgentPolicy(max_framework_searches_per_turn=2))
        self.assertEqual(action["framework_search"], ["FETCHROW", "HOLDROW"])

    def test_duplicate_semantic_tools_leave_shared_allowance_for_the_next_kind(self):
        first = {"relative_path": "detail.cbl", "line": 9}
        later = {"relative_path": "result.cbl", "line": 9}
        action = _actions(json.dumps({"backward_slice": [first, first], "forward_slice": [later]}),
                          AgentPolicy(max_business_context_actions_per_turn=2))
        self.assertEqual(action["backward_slice"], [first])
        self.assertEqual(action["forward_slice"], [later])

    def test_identical_read_batch_executes_once_and_preserves_grounded_answer(self):
        fixture = fixtures.BusinessActionBudgetTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        requests = []

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            if len(requests) == 1:
                text = json.dumps({"read": [fixture.model_read, fixture.model_read]})
            else:
                page = next(page for bundle in payload["source_context"] for page in bundle["pages"]
                            if page["relative_path"] == "detail.cbl")
                text = f"补充程序将附加金额乘以三得到最终金额。[{page['evidence_id']}]"
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text},
                "finish_reason": "stop"}]}, ensure_ascii=False))

        output = run_business_chat("VALUEFLOW", fixture.database, fixture.source, fixture.config,
            transport=transport, framework_reference_path="", answer_detail="brief",
            policy=replace(fixture.policy, max_reads_per_turn=3))
        answer = output["agent_result"]
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertEqual(len(requests), 2)
        self.assertEqual(answer["metrics"]["tool_calls"]["read"], 1)
        self.assertEqual(sum(action.get("read") == fixture.model_read
                             for action in requests[-1]["completed_actions"]), 1)
        self.assertIn("附加金额乘以三", answer["answer"])
        self.assertIn("detail.cbl", {row["relative_path"] for row in answer["narrative"]["citations"]})


if __name__ == "__main__":
    unittest.main()
