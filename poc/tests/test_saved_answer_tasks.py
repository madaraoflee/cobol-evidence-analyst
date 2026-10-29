"""Saved business requirements govern final tasks, keeping discovery intact."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import _fit_request
from company_api import CompanyAPIConfig


class SavedAnswerTaskTests(unittest.TestCase):
    def setUp(self):
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "neutral-model",
                                       api_key="offline-test-key")
        self.instructions = ("请解释业务用途、程序分工和数据交接，并说明相关改动影响。"
                             "使用两个自然段，不设栏目。")
        self.task = ("回答当前业务问题。用户未请求修改时不追加改码建议或反事实场景。"
                     "先给结论，再说明必要条件、顺序和结果，关键判断引用来源。")

    def payload(self, instructions, *, compact=False):
        return {"question": "REQUESTFLOW 做什么？", "answer_instructions": instructions,
                "source_context": [{"pages": []}], "business_map": {},
                "framework_references": [], "task": self.task, "budget_wire_compact": compact}

    def test_saved_content_and_order_do_not_compete_with_default_final_task(self):
        for compact in (False, True):
            with self.subTest(compact=compact):
                payload = self.payload(self.instructions, compact=compact)
                messages, _ = _fit_request(self.config, payload, [], AgentPolicy())
                system, wire = messages[0]["content"], json.loads(messages[-1]["content"])
                self.assertEqual(system.count(self.instructions), 1)
                self.assertIn("业务内容、分析范围要求优先", system)
                self.assertNotIn("用户未请求修改时不追加", wire["task"])
                self.assertNotIn("先给结论，再说明必要条件", wire["task"])
                self.assertIn("保存的回答要求", wire["task"])
                self.assertIn("business content, scope", wire["response_contract"]["organization"])
                self.assertNotIn("answer_instructions", wire)
                self.assertEqual(payload["task"], self.task)

    def test_no_saved_requirements_preserves_the_original_task(self):
        messages, _ = _fit_request(self.config, self.payload(""), [], AgentPolicy())
        wire = json.loads(messages[-1]["content"])
        self.assertEqual(wire["task"], self.task)

    def test_business_requirements_do_not_replace_source_discovery_protocol(self):
        payload = self.payload(self.instructions)
        payload.update(retrieval_status={"state": "needs_discovery"},
                       investigation_budget={"searches_per_turn": 1, "reads_per_turn": 1})
        messages, _ = _fit_request(self.config, payload, [], AgentPolicy())
        wire = json.loads(messages[-1]["content"])
        self.assertEqual(wire["response_mode"], "source_discovery")
        self.assertIn("JSON object", wire["response_contract"]["format"])
        self.assertIn("Do not write a business answer", wire["response_contract"]["scope"])
        self.assertEqual(messages[0]["content"].count(self.instructions), 1)


if __name__ == "__main__":
    unittest.main()
