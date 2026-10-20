"""Preserve business explanation limits through both actual wire modes.

These check the generation contract and source authority, not the semantic
correctness of a model answer. Real responses have a separate source review.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[2]),
               str(Path(__file__).resolve().parents[1])]

from agent_policy import AgentPolicy
from answer_markdown import BUSINESS_RUNTIME_BOUNDARY_POLICY
from business_chat import _fit_request
from company_api import CompanyAPIConfig
from tools.business_onboarding_fixture import BATCH_QUESTION, sources


class BusinessBoundaryContractTests(unittest.TestCase):
    def payload(self, compact):
        pages = [{"evidence_id": "ev_source_" + str(index), "relative_path": path,
                  "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
                  "start_line": 1, "end_line": len(text.splitlines()),
                  "source_text": text, "format_hint": "free"}
                 for index, (path, text) in enumerate(sources().items())]
        return {"question": BATCH_QUESTION, "answer_detail": "detailed",
                "source_context": [{"pages": pages}], "repository": {},
                "budget_wire_compact": compact,
                "business_map": {"source_identity": {"status": "resolved"},
                                 "direct_paths": ["settle.cbl"]},
                "question_investigation": {"state": "resolved", "required_items": [],
                                           "open_gaps": [], "can_answer": True}}

    def test_full_and_compact_requests_preserve_preconditions_and_all_source(self):
        config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-view",
                                  api_key="unused", max_output_tokens=8192)
        for compact in (False, True):
            with self.subTest(compact=compact):
                payload = self.payload(compact)
                original = deepcopy(payload["source_context"][0]["pages"])
                messages, size = _fit_request(config, payload, [],
                    AgentPolicy(max_request_bytes=32768))
                self.assertLessEqual(size, 32768)
                self.assertIn(BUSINESS_RUNTIME_BOUNDARY_POLICY, messages[0]["content"])
                wire = json.loads(messages[-1]["content"])
                actual = wire["source_context"][0]["pages"]
                self.assertEqual(actual, original)
                self.assertEqual(payload["source_context"][0]["pages"], original)
                self.assertTrue(wire["source_selection"]["source_supplied"])
                # Navigation is never an assertion of runtime execution.
                brief = wire["business_analysis_brief"]
                self.assertFalse(brief["business_explanation_contract"]["semantic_verification"])
                self.assertFalse(brief["business_orientation"]["scope"]["semantic_execution_verified"])

    def test_local_boundary_question_keeps_generation_limits_in_both_wire_modes(self):
        """A short follow-up keeps the limits without replacing its source."""
        config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-view",
                                  api_key="unused", max_output_tokens=8192)
        question = "空输入会处理或写入什么？提示送到哪里，会影响下一条吗？简要说明。"
        for compact in (False, True):
            with self.subTest(compact=compact):
                payload = self.payload(compact)
                payload.update(question=question, answer_detail="brief")
                original = deepcopy(payload["source_context"][0]["pages"])
                messages, size = _fit_request(config, payload, [],
                    AgentPolicy(max_request_bytes=32768))
                self.assertLessEqual(size, 32768)
                system = messages[0]["content"]
                # This is a wire-contract check, not an evaluation of generated prose.
                for clause in (
                    "每个独立摘要、表格或结语的空输入结论，就地写明",
                    "按所示初始状态且 OPEN/READ 正常、首次 READ 到 AT END 时",
                    "VALUE 仅初始化，不代表每次调用重置",
                    "‘不影响下一条’仅指正常业务控制路径",
                    "未知时不罗列丢弃、重定向等无证可能性",
                    "同一结论及影响当前问题的缺项只说明一次",
                ):
                    self.assertIn(clause, system)
                wire = json.loads(messages[-1]["content"])
                self.assertEqual(wire["question"], question)
                self.assertEqual(wire["source_context"][0]["pages"], original)
                self.assertTrue(wire["source_selection"]["source_supplied"])

if __name__ == "__main__":
    unittest.main()
