"""Model source requests remain valid when automatic work spends the turn budget."""

from dataclasses import replace
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
import question_investigation
from repository_discovery import ensure_repository_search


class BusinessActionBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        (self.source / "rule.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. VALUEFLOW.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 BASE-AMOUNT PIC 9(5) VALUE 10.\n"
            "01 TOTAL-AMOUNT PIC 9(6).\nPROCEDURE DIVISION.\nMAIN.\n"
            "COMPUTE TOTAL-AMOUNT = BASE-AMOUNT * 2.\nGOBACK.\n",
            encoding="utf-8")
        (self.source / "detail.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. DETAILRULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 EXTRA-AMOUNT PIC 9(5) VALUE 7.\n"
            "01 FINAL-AMOUNT PIC 9(6).\nPROCEDURE DIVISION.\nMAIN.\n"
            "COMPUTE FINAL-AMOUNT = EXTRA-AMOUNT * 3.\nGOBACK.\n",
            encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free",
                             framework_reference_path="", quiet=True)
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
                                       api_key="offline-only")
        self.policy = AgentPolicy(max_model_requests=2, max_reads_per_turn=1,
            max_searches_per_turn=1, max_business_context_actions_per_turn=0,
            max_evidence_groups=0, max_answer_revisions=0)
        self.requests = []
        self.model_read = {"relative_path": "detail.cbl", "start_line": 1, "end_line": 10}
        for owner, attribute in ((socket, "create_connection"), (socket.socket, "connect"),
                                 (socket.socket, "connect_ex")):
            patcher = mock.patch.object(owner, attribute, side_effect=AssertionError("Network forbidden"))
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def pages(payload):
        return [page for bundle in payload["source_context"] for page in bundle["pages"]]

    @staticmethod
    def automatic_plan(*args, completed_actions, **kwargs):
        # Fix the planning decision while keeping indexing, source reads,
        # request assembly, citations and action scheduling real.
        done = any(action.get("read", {}).get("relative_path") == "rule.cbl"
                   for action in completed_actions)
        return {"required_items": [], "planned_actions": [] if done else [{
            "tool": "read", "arguments": {"relative_path": "rule.cbl", "start_line": 5,
                                           "end_line": 9},
            "reason": "dependency_source_not_supplied"}],
            "open_gaps": [], "state": "located" if done else "needs_evidence",
            "can_answer": done}

    def ask(self, *, mixed=False, policy=None):
        policy = policy or self.policy

        def transport(request):
            envelope = json.loads(request.body)
            self.assertNotIn("tools", envelope)
            payload = json.loads(envelope["messages"][-1]["content"])
            self.requests.append(payload)
            if len(self.requests) == 1:
                action = {"read": [self.model_read]}
                if mixed:
                    action["search"] = ["VALUEFLOW"]
                text = json.dumps(action)
            elif policy.max_reads_per_turn:
                page = next(page for page in self.pages(payload)
                            if "COMPUTE FINAL-AMOUNT = EXTRA-AMOUNT * 3." in page["source_text"])
                text = f"补充源码将附加金额乘以三得到最终金额。[{page['evidence_id']}]"
            else:
                self.assertNotIn("detail.cbl", {page["relative_path"] for page in self.pages(payload)})
                page = next(page for page in self.pages(payload) if page["relative_path"] == "rule.cbl")
                text = ("当前已读程序按基础金额乘以二计算；补充程序尚未读取，不能确认其行为。"
                        f"[{page['evidence_id']}]")
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text},
                "finish_reason": "stop"}]}, ensure_ascii=False))

        with mock.patch.object(question_investigation, "build_question_investigation",
                               side_effect=self.automatic_plan):
            return run_business_chat("VALUEFLOW 的金额如何计算？", self.database, self.source,
                self.config, transport=transport, framework_reference_path="", policy=policy)

    def assert_budgeted_answer(self, output, policy):
        result = output["agent_result"]
        self.assertTrue(result["model_answer_recorded"])
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertNotEqual(output.get("reason_code"), "INVALID_INVESTIGATION_ACTION")
        self.assertNotIn("INVALID_INVESTIGATION_ACTION", {row["code"] for row in result["diagnostics"]})
        self.assertLessEqual(len(self.requests), policy.max_model_requests)
        self.assertEqual(result["metrics"]["model_requests"], len(self.requests))
        previous = {"read": 0, "search": 0, "inspect_business_context": 0}
        limits = {"read": policy.max_reads_per_turn, "search": policy.max_searches_per_turn,
                  "inspect_business_context": policy.max_business_context_actions_per_turn}
        for payload in self.requests:
            for tool, limit in limits.items():
                count = sum(tool in action for action in payload["completed_actions"])
                self.assertLessEqual(count - previous[tool], limit)
                previous[tool] = count
        for tool in limits:
            self.assertEqual(result["metrics"]["tool_calls"][tool], previous[tool])
        return result

    def test_model_read_waits_for_fresh_allowance_and_supplies_requested_source(self):
        output = self.ask()
        result = self.assert_budgeted_answer(output, self.policy)
        self.assertEqual(len(self.requests), 2)
        first, second = self.requests
        self.assertEqual(first["investigation_budget"]["reads_per_turn"], 0)
        self.assertEqual(sum("read" in action for action in first["completed_actions"]), 1)
        self.assertNotIn("detail.cbl", {page["relative_path"] for page in self.pages(first)})
        self.assertIn("detail.cbl", {page["relative_path"] for page in self.pages(second)})
        deferred = [action for action in second["completed_actions"]
                    if action.get("read") == self.model_read]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0]["reason"], "model_action_deferred_for_budget")
        self.assertEqual(result["metrics"]["tool_calls"]["read"], 2)
        self.assertIn("附加金额乘以三", result["answer"])
        cited = {row["relative_path"] for row in result["narrative"]["citations"]}
        self.assertIn("detail.cbl", cited)

    def test_mixed_search_does_not_discard_a_read_deferred_for_budget(self):
        output = self.ask(mixed=True)
        result = self.assert_budgeted_answer(output, self.policy)
        self.assertEqual(len(self.requests), 2)
        first, second = self.requests
        self.assertEqual(first["investigation_budget"]["reads_per_turn"], 0)
        self.assertNotIn("detail.cbl", {page["relative_path"] for page in self.pages(first)})
        self.assertTrue(any(action.get("search") == ["VALUEFLOW"]
                            for action in second["completed_actions"]))
        self.assertTrue(any(action.get("read") == self.model_read and
                            action.get("reason") == "model_action_deferred_for_budget"
                            for action in second["completed_actions"]))
        self.assertEqual(result["metrics"]["tool_calls"]["search"], 1)
        self.assertEqual(result["metrics"]["tool_calls"]["read"], 2)
        self.assertIn("附加金额乘以三", result["answer"])

    def test_disabled_reads_get_a_bounded_answer_without_invalid_action_failure(self):
        policy = replace(self.policy, max_reads_per_turn=0, max_model_requests=3)
        output = self.ask(policy=policy)
        result = self.assert_budgeted_answer(output, policy)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["metrics"]["tool_calls"]["read"], 0)
        self.assertIn("补充程序尚未读取", result["answer"])
        # A disabled tool requires an answer with known limits, not another
        # action-repair prompt that keeps offering investigation allowance.
        self.assertEqual(self.requests[1]["investigation_budget"]["searches_per_turn"], 0)
        for payload in self.requests:
            self.assertEqual(payload["investigation_budget"]["reads_per_turn"], 0)
            self.assertNotIn("detail.cbl", {page["relative_path"] for page in self.pages(payload)})
        self.assertNotIn("model_action_deferred_for_budget", {row["reason"] for row in result["boundaries"]})

    def test_final_request_cannot_create_an_extra_model_or_read_turn(self):
        policy = replace(self.policy, max_model_requests=1)
        output = self.ask(policy=policy)
        result = output["agent_result"]
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["metrics"]["model_requests"], 1)
        self.assertEqual(result["metrics"]["tool_calls"]["read"], 1)
        self.assertFalse(result["model_answer_recorded"])
        self.assertEqual(output["reason_code"], "ANSWER_NOT_PRODUCED")
        self.assertNotIn("detail.cbl", {page["relative_path"] for page in self.pages(self.requests[0])})


if __name__ == "__main__":
    unittest.main()
