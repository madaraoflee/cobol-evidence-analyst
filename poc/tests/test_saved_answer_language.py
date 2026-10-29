"""Saved explicit language choices reach prompts and local source repairs."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search
from response_language import resolve_answer_language
from test_answer_fact_completion import SOURCE


class SavedLanguageSelectionTests(unittest.TestCase):
    def test_saved_explicit_language_precedes_question_prose_and_interface(self):
        cases = (
            ("解释这项业务。", "Always answer in English.", "en"),
            ("解释这项业务。", "请始终用英语回答。", "en"),
            ("解释这项业务。", "回答必须使用英文。", "en"),
            ("解释这项业务。", "Use English for all responses.", "en"),
            ("解释这项业务。", "Always use English.", "en"),
            ("解释这项业务。", "Language: English", "en"),
            ("Explain the business process.", "请统一用中文回答。", "zh-CN"),
            ("Explain the business process.", "回答语言：zh-CN", "zh-CN"),
            ("Explain the business process.", 'Output language: "zh-CN"', "zh-CN"),
            ("Explain the business process.", "回答语言：`zh-HK`", "zh-HK"),
            ("Explain the business process.", "请用简体回答。", "zh-CN"),
            ("Explain the business process.", "請用繁體回答。", "zh-HK"),
            ("Explain the business process.", "Response language: zh-TW", "zh-HK"),
        )
        for question, instructions, expected in cases:
            with self.subTest(instructions=instructions):
                self.assertEqual(resolve_answer_language(question, locale="en", answer_instructions=instructions), expected)

    def test_current_explicit_language_precedes_saved_requirement(self):
        for question, instructions, expected in (
            ("请用中文回答，这项业务有什么作用？", "Always answer in English.", "zh-CN"),
            ("Reply in English. 解释这项业务。", "请用繁体中文回答。", "en"),
            ("Explain the process. Answer in Traditional Chinese.", "Answer in English.", "zh-HK"),
            ("输出语言：zh-CN。解释这项业务。", "Response language: en", "zh-CN"),
        ):
            with self.subTest(question=question):
                self.assertEqual(resolve_answer_language(question, answer_instructions=instructions), expected)

    def test_saved_prose_section_names_and_examples_do_not_select_language(self):
        for instructions in (
            "Use headings Batch job, Online function / Programs, Tables and Files.",
            "English headings are useful for source identification.",
            "The heading names use English.",
            "不要用英文回答。", "Do not always answer in English.",
            "Never reply in English.", "禁止默认用英文回答。",
            "示例：请用英文回答。", "英文回答示例：输入、规则、结果。",
            "Example:\nReply in English.",
            "示例：\n请用英文回答。\n\n正文说明业务角色。",
            'Explain the phrase "Reply in English" as an example.',
            "Explain the phrase 'Reply in English' as an example.",
            "```text\nReply in English.\n```",
        ):
            with self.subTest(instructions=instructions):
                self.assertEqual(resolve_answer_language("解释这项业务。", answer_instructions=instructions), "zh-CN")
        self.assertEqual(resolve_answer_language("Explain the process.",
            answer_instructions="请面向新人说明处理对象、输入、规则和结果。"), "en")

    def test_missing_explicit_language_preserves_locale_history_and_defaults(self):
        instructions = "业务结论与处理依据各用一段说明。"
        self.assertEqual(resolve_answer_language("RESULT-CODE?", locale="en", answer_instructions=instructions), "en")
        self.assertEqual(resolve_answer_language("RESULT-CODE?", history=[
            {"role": "user", "content": "Explain the process."}], answer_instructions=instructions), "en")
        self.assertEqual(resolve_answer_language("RESULT-CODE?", answer_instructions=instructions), "zh-CN")
        self.assertEqual(resolve_answer_language("Explain the process.", answer_instructions=None), "en")


class SavedLanguageSourceRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "flow.cbl").write_text(SOURCE, encoding="utf-8")
        self.database = self.root / "index.sqlite"
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only")

    def test_local_formula_correction_uses_effective_saved_or_current_language(self):
        cases = (
            ("CALCFLOW 的 CALCULATE-AMOUNT 计算规则", "Always answer in English.", "en"),
            ("Explain CALCFLOW CALCULATE-AMOUNT calculation rules.", "请用中文回答。", "zh-CN"),
            ("请用中文回答 CALCFLOW 的 CALCULATE-AMOUNT 计算规则", "Always answer in English.", "zh-CN"),
            ("Reply in English. CALCFLOW 的 CALCULATE-AMOUNT 计算规则", "请用中文回答。", "en"),
        )
        for index, (question, instructions, expected) in enumerate(cases):
            with self.subTest(question=question, instructions=instructions):
                requests = []
                def transport(request):
                    envelope = json.loads(request.body)
                    payload = json.loads(envelope["messages"][-1]["content"])
                    requests.append(payload)
                    source = max((row for bundle in payload["source_context"] for row in bundle["pages"]
                                  if row["relative_path"] == "flow.cbl"),
                                 key=lambda row: row["end_line"] - row["start_line"])
                    return TransportResponse(200, json.dumps({"choices": [{"message": {"content":
                        "OUT-AMOUNT = RAW-AMOUNT * RATE / 1000. [" + source["evidence_id"] + "]"},
                        "finish_reason": "stop"}]}))
                with mock.patch("socket.create_connection", side_effect=AssertionError("offline only")):
                    result = run_business_chat(question, self.database, self.source, self.config,
                        transport=transport, framework_reference_path="", answer_instructions=instructions,
                        conversation_id=f"saved-language-{index}",
                        policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))["agent_result"]
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]["answer_language"], expected)
                self.assertEqual(result["answer_language"], expected)
                self.assertIn("RAW-AMOUNT * RATE / 1200", result["answer"])
                self.assertNotIn("RATE / 1000", result["answer"])
                if expected == "en":
                    self.assertNotRegex(result["answer"], r"[\u3400-\u9fff]")
                    self.assertIn("supplied source", result["answer"].lower())
                else:
                    self.assertIn("已供应源码", result["answer"])


if __name__ == "__main__":
    unittest.main()
