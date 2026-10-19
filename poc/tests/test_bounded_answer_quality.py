"""Adversarial answer and memory regressions through the real chat workflow."""

import json
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from agent_policy import AgentPolicy
from run_store import RunStore
import test_completion_driven_chat as fixtures


class BoundedAnswerQualityTests(unittest.TestCase):
    def setUp(self):
        self.chat = fixtures.CompletionDrivenChatTests()
        self.chat.setUp()
        self.addCleanup(self.chat.doCleanups)
        self.chat.question = "root.cbl 的金额如何计算，输入初值来自哪里？"

    def test_more_complete_keywords_cannot_replace_a_supported_formula_with_an_error(self):
        def respond(payload, number):
            text = ("TOTAL-AMOUNT = BASE-AMOUNT * 2。" if number == 1 else
                    "基础金额初值来自 VALUE 10，TOTAL-AMOUNT = BASE-AMOUNT * 999。")
            return self.chat.cited_answer(payload, text)

        result = self.chat.ask(respond, request_guard=3)
        self.assertEqual(len(self.chat.requests), 2)
        self.assertIn("TOTAL-AMOUNT = BASE-AMOUNT * 2", result["answer"])
        self.assertNotIn("999", result["answer"])
        self.assertTrue(any(row.get("revision_error") == "ANSWER_FACTS_REGRESSED"
                            for row in result["boundaries"]))
        self.assertFalse(result["claims_semantically_verified"])

    def test_a_true_citation_does_not_promote_a_false_finding_into_memory(self):
        def respond(payload, number):
            if number == 1:
                page = next(p for p in self.chat.pages(payload) if p["relative_path"] == "root.cbl")
                action = json.loads(self.chat.read_helper(1))
                action["findings"] = [{"text": "TOTAL-AMOUNT = BASE-AMOUNT * 999。",
                    "evidence_ids": [page["evidence_id"]]}]
                return json.dumps(action)
            memory = payload["prior_source_memory"]
            self.assertEqual(memory["confirmed_facts"], [])
            self.assertNotIn("999", json.dumps(memory))
            self.assertTrue(any(row["content_kind"] == "source_reference" for row in memory["blocks"]))
            return self.chat.cited_answer(payload)

        result = self.chat.ask(respond, request_guard=2)
        saved = RunStore(self.chat.database.parent / "investigation-runs.sqlite").load(result["run_id"])
        self.assertTrue(saved["checkpoint"]["prior_source_memory"]["retracted_blocks"])
        self.assertIn("乘以二", result["answer"])
        self.assertNotIn("999", result["answer"])

    def test_memory_can_be_disabled_without_disabling_bounded_adaptive_investigation(self):
        def respond(payload, number):
            self.assertNotIn("prior_source_memory", payload)
            return self.chat.read_helper(1) if number == 1 else self.chat.cited_answer(payload)

        result = self.chat.ask(respond, request_guard=2,
            policy=AgentPolicy(max_model_requests=2, source_memory_enabled=False))
        self.assertEqual(len(self.chat.requests), 2)
        self.assertTrue(result["metrics"]["completion_driven"])
        self.assertIn("乘以二", result["answer"])

    def test_a_supported_source_denial_is_repaired_without_paid_rewriting(self):
        def respond(payload, number):
            return self.chat.cited_answer(payload) + " root.cbl 的字段处理源码未提供。"

        result = self.chat.ask(respond, request_guard=1)
        self.assertEqual(len(self.chat.requests), 1)
        self.assertNotIn("未提供", result["answer"])
        self.assertIn("乘以二", result["answer"])
        self.assertFalse(result["answer_completion"]["source_availability_contradiction"])


if __name__ == "__main__":
    unittest.main()
