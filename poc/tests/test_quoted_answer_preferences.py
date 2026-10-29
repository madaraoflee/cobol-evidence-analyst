"""Quoted preference values are requests; quoted actions remain reference data."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from answer_markdown import DEFAULT_ANSWER_INSTRUCTIONS
from answer_preferences import saved_business_explanation_requested
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from question_planner import QuestionPlanner
from repository_discovery import ensure_repository_search
from response_language import resolve_answer_language
from test_answer_fact_completion import SOURCE


class QuotedAnswerPreferenceTests(unittest.TestCase):
    def test_current_quoted_language_values_override_opposite_saved_language(self):
        cases = (
            ('Reply in "English". 解释业务用途。', "请用中文回答。", "en"),
            ("请用“英文”回答，解释这项业务。", "请用中文回答。", "en"),
            ("请用「英文」回答，解释这项业务。", "请用中文回答。", "en"),
            ("请用'英文'回答，解释这项业务。", "请用中文回答。", "en"),
            ("请用‘英文’回答，解释这项业务。", "请用中文回答。", "en"),
            ("输出语言：`zh-CN`。Explain this process.", "Always answer in English.", "zh-CN"),
            ("Output language: 'zh-HK'. Explain this process.", "Always answer in English.", "zh-HK"),
        )
        for question, saved, expected in cases:
            with self.subTest(question=question):
                self.assertEqual(resolve_answer_language(question, answer_instructions=saved), expected)

    def test_current_quoted_action_examples_do_not_override_saved_language(self):
        for question in ('解释“Reply in English”这句话。',
                         "Explain the phrase 'Reply in English'.",
                         "Explain this example: `Reply in English`."):
            with self.subTest(question=question):
                self.assertEqual(resolve_answer_language(question, answer_instructions="请用中文回答。"), "zh-CN")

    def test_previous_explicit_quoted_language_can_supply_existing_history_fallback(self):
        self.assertEqual(resolve_answer_language("RESULT-CODE?", history=[
            {"role": "user", "content": 'Reply in "English".'}]), "en")

    def test_quoted_business_actions_do_not_expand_investigation(self):
        for quoted in ('"解释业务用途和数据交接"', "“解释业务用途和数据交接”", "‘解释业务用途和数据交接’",
                       "「解释业务用途和数据交接」", "'explain business purpose and data flow'",
                       "`explain business purpose and data flow`"):
            instructions = f"请解释{quoted}这句话的意思，不要据此扩写业务分析。"
            with self.subTest(quoted=quoted):
                self.assertFalse(saved_business_explanation_requested("COLLECTION 做了什么？", instructions))
                plan = QuestionPlanner().plan("COLLECTION 做了什么？", {"direct_paths": ["collection.cbl"]},
                    answer_instructions=instructions)
                self.assertNotIn("business_overview", plan.intents)
                self.assertEqual(plan.entities, ())

    def test_fenced_business_examples_and_unfinished_fences_are_not_preferences(self):
        for fence in ("```", "~~~"):
            for closing in (fence, ""):
                instructions = f"以下是错误输出示例：\n{fence}text\n解释业务用途和上下游职责。\n{closing}"
                with self.subTest(fence=fence, closing=closing):
                    self.assertFalse(saved_business_explanation_requested("COLLECTION 做了什么？", instructions))

    def test_quoted_topics_and_positive_demand_after_example_remain_effective(self):
        for instructions in ("请解释“业务用途”和“数据交接”。",
                             "Explain 'business purpose' and `data flow`.",
                             '错误示例："解释业务用途"。请讲清程序职责和数据交接。',
                             DEFAULT_ANSWER_INSTRUCTIONS):
            with self.subTest(instructions=instructions):
                self.assertTrue(saved_business_explanation_requested("COLLECTION 做了什么？", instructions))
        self.assertFalse(saved_business_explanation_requested("只给返回码。", "请解释“业务用途”和“数据交接”。"))


class QuotedLanguageSourceRepairTests(unittest.TestCase):
    def test_current_quoted_language_reaches_request_and_deterministic_source_correction(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "source"
        source.mkdir()
        (source / "flow.cbl").write_text(SOURCE, encoding="utf-8")
        database = root / "index.sqlite"
        build_business_index(source, database, source_format="free", verify_content=True)
        ensure_repository_search(database, source)
        config = CompanyAPIConfig("https://offline.example.invalid/v1", "neutral-model", api_key="offline-only")
        cases = (
            ('Reply in "English". CALCFLOW 的 CALCULATE-AMOUNT 计算规则', "请用中文回答。", "en"),
            ("输出语言：`zh-CN`。Explain CALCFLOW CALCULATE-AMOUNT calculation rules.",
             "Always answer in English.", "zh-CN"),
        )
        for index, (question, instructions, expected) in enumerate(cases):
            requests = []

            def transport(request):
                payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
                requests.append(payload)
                page = max((page for bundle in payload["source_context"] for page in bundle["pages"]
                            if page["relative_path"] == "flow.cbl"),
                           key=lambda page: page["end_line"] - page["start_line"])
                return TransportResponse(200, json.dumps({"choices": [{"message": {"content":
                    f"OUT-AMOUNT = RAW-AMOUNT * RATE / 1000. [{page['evidence_id']}]"}, "finish_reason": "stop"}]}))

            with self.subTest(question=question), \
                 mock.patch("socket.create_connection", side_effect=AssertionError("Network forbidden")):
                result = run_business_chat(question, database, source, config, transport=transport,
                    allow_network=False, framework_reference_path="", answer_instructions=instructions,
                    conversation_id=f"quoted-preference-{index}",
                    policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))["agent_result"]
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]["answer_language"], expected)
                self.assertEqual(result["answer_language"], expected)
                self.assertIn("RAW-AMOUNT * RATE / 1200", result["answer"])
                if expected == "en":
                    self.assertNotRegex(result["answer"], r"[\u3400-\u9fff]")
                else:
                    self.assertIn("已供应源码", result["answer"])


if __name__ == "__main__":
    unittest.main()
