"""Offline language selection and delivery through the real chat pipeline."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from analysis_continuation import progress_snapshot
from business_chat import run_business_chat, _prompt_payload, _system_for_payload
from business_index import build_business_index
from company_api import APIClientError, CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search
from response_language import resolve_answer_language


class LanguageSelectionTests(unittest.TestCase):
    def test_current_question_wins_over_interface_and_chinese_history(self):
        history = [{"role": "user", "content": "请解释计算规则。"},
                   {"role": "assistant", "content": "先检查输入。"}]
        for question in ("Explain the calculation in RULE.cbl.", "WHAT DOES THIS PROGRAM DO?",
                         "Continue.", "Explain how 保留期 is calculated.",
                         "Explain the logic of 保单缴费日期修改处理"):
            with self.subTest(question=question):
                self.assertEqual(resolve_answer_language(question, locale="zh-HK", history=history), "en")

    def test_chinese_questions_with_english_identifiers_and_business_terms(self):
        for question in ("BATCH-WORKER怎样trigger？", "BATCH-WORKER 的batch怎么trigger的？",
                         "为什么输出 REVIEW？", "请解释 TIDE-WINDOW 逻辑。"):
            self.assertEqual(resolve_answer_language(question, locale="en"), "zh-CN", question)
        self.assertEqual(resolve_answer_language("請解釋這個程序的處理條件。", locale="en"), "zh-HK")

    def test_chinese_explanation_requests_keep_business_terms_without_switching_language(self):
        for prefix, expected in (("解释一下", "zh-CN"), ("说明", "zh-CN"), ("分析", "zh-CN"),
                                 ("讲解", "zh-CN"), ("解釋一下", "zh-HK"), ("說明", "zh-HK"),
                                 ("講解", "zh-HK")):
            question = prefix + " premium refund 以及 inforce illustration"
            for locale in ("zh-CN", "zh-HK", "en"):
                with self.subTest(question=question, locale=locale):
                    self.assertEqual(resolve_answer_language(question, locale=locale), expected)
        self.assertEqual(resolve_answer_language(
            "解释一下 premium refund 以及 inforce illustration。请用英文回答。"), "en")
        self.assertEqual(resolve_answer_language(
            "Explain premium refund and 保单说明 in this program.", locale="zh-CN"), "en")

    def test_explicit_language_requests_override_question_language(self):
        for question, expected in (("请用英文", "en"), ("请用英文回答：这里的逻辑是什么？", "en"),
            ("Explain this. Reply in Traditional Chinese.", "zh-HK"),
            ("Explain this. Answer in Simplified Chinese.", "zh-CN"),
            ("Do not answer in Chinese. Reply in English.", "en")):
            self.assertEqual(resolve_answer_language(question), expected, question)

    def test_quoted_instructions_and_source_do_not_set_answer_language(self):
        self.assertEqual(resolve_answer_language('Explain "请用中文回答" in this comment.'), "en")
        self.assertEqual(resolve_answer_language('请解释注释 "Reply in English"。'), "zh-CN")
        self.assertEqual(resolve_answer_language('Explain this:\n```cobol\nDISPLAY "请用中文回答".\n```'), "en")

    def test_language_mentions_are_not_answer_instructions(self):
        for question in ("这个程序是否使用英文文件名？", "为什么使用英文变量名？",
                         "文件以英文命名会影响计算吗？", "不用英文，解释这个程序。"):
            self.assertEqual(resolve_answer_language(question), "zh-CN", question)

    def test_ambiguous_identifiers_use_locale_then_prior_user(self):
        self.assertEqual(resolve_answer_language("RESULT-CODE?", locale="en"), "en")
        self.assertEqual(resolve_answer_language("123?", locale="zh-HK"), "zh-HK")
        self.assertEqual(resolve_answer_language("RESULT-CODE?", history=[
            {"role": "user", "content": "Explain this program."},
            {"role": "assistant", "content": "中文回答不应控制下一轮。"}]), "en")
        self.assertEqual(resolve_answer_language("RESULT-CODE?"), "zh-CN")

    def test_compacted_discovery_contract_keeps_language(self):
        payload = {"question": "Explain eligibility.", "answer_language": "en", "budget_wire_compact": True,
            "source_context": [{"pages": []}], "retrieval_status": {"state": "needs_discovery"},
            "investigation_budget": {"searches_per_turn": 1}, "prior_source_memory": {"blocks": []}}
        wire = _prompt_payload(payload)
        self.assertEqual(wire["response_mode"], "source_discovery")
        self.assertEqual(wire["response_contract"]["answer_language"], "en")
        self.assertIn("Answer in English (en)", _system_for_payload(payload))

    def test_progress_summary_and_gap_labels_follow_answer_language(self):
        event = progress_snapshot(2, {"required_items": [{"kind": "inputs", "status": "MISSING"},
            {"kind": "custom", "status": "MISSING"}]}, 3, answer_language="en")
        self.assertEqual(event["open_items"], ["Input sources", "Relevant business details"])
        self.assertNotRegex(json.dumps(event, ensure_ascii=False), r"[\u3400-\u9fff]")


class ChatLanguageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        (self.source / "decision.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. DECISION.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 RESULT-CODE PIC 99.\n"
            "PROCEDURE DIVISION.\nMAIN.\nMOVE 12 TO RESULT-CODE.\nGOBACK.\n", encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model", api_key="offline-key")
        self.requests = []
        self.progress = []

    def ask(self, question, respond, **options):
        def transport(request):
            envelope = json.loads(request.body)
            payload = json.loads(envelope["messages"][-1]["content"])
            self.requests.append(payload)
            self.assertEqual(payload["response_contract"]["answer_language"], payload["answer_language"])
            self.assertIn("Answer language for this turn:", envelope["messages"][0]["content"])
            return respond(payload, len(self.requests))
        with mock.patch("socket.create_connection", side_effect=AssertionError("offline only")):
            return run_business_chat(question, self.database, self.source, self.config,
                transport=transport, framework_reference_path="", progress=self.progress.append,
                policy=AgentPolicy(max_model_requests=3, max_answer_revisions=0), **options)["agent_result"]

    @staticmethod
    def reply(text, finish="stop"):
        return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": text},
            "finish_reason": finish}]}))

    def test_english_followup_and_continuation_ignore_previous_chinese_answers(self):
        def respond(payload, count):
            self.assertEqual(payload["answer_language"], "en")
            page = payload["source_context"][0]["pages"][0]
            if count == 1:
                return self.reply("The program returns `RESULT-CO", "length")
            return self.reply("DE` with value 12. [" + page["evidence_id"] + "]")
        result = self.ask("What does DECISION return?", respond, locale="zh-CN", history=[
            {"role": "user", "content": "DECISION 做什么？"}, {"role": "assistant", "content": "它设置结果。"}])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["answer_language"], "en")
        self.assertIn("RESULT-CODE", result["answer"])
        self.assertNotRegex(result["answer"], r"[\u3400-\u9fff]")
        self.assertTrue(result["narrative"]["citations"])

    def test_english_local_fallback_retains_evidence_and_english_caveats(self):
        def respond(payload, count):
            raise APIClientError("TRANSPORT_ERROR", transport_reason="connection_reset")
        result = self.ask("What value does DECISION assign to RESULT-CODE?", respond)
        self.assertEqual(result["answer_origin"], "local_source_facts")
        self.assertIn("RESULT-CODE", result["answer"])
        self.assertNotRegex(result["answer"], r"[\u3400-\u9fff]")
        self.assertTrue(result["narrative"]["citations"])

    def test_english_no_evidence_failure_has_english_diagnostic(self):
        result = self.ask("Where are the unrelated eligibility rules?", lambda payload, count:
            TransportResponse(401, json.dumps({"error": {"code": "invalid_api_key"}})))
        self.assertEqual(result["answer_origin"], "diagnostic")
        self.assertIn("401", result["answer"])
        self.assertNotRegex(result["answer"], r"[\u3400-\u9fff]")

    def test_mixed_business_question_keeps_chinese_in_prompt_and_local_diagnostic(self):
        result = self.ask("解释一下 premium refund 以及 inforce illustration", lambda payload, count:
            TransportResponse(401, json.dumps({"error": {"code": "invalid_api_key"}})), locale="zh-CN")
        self.assertTrue(self.requests)
        self.assertTrue(all(payload["answer_language"] == "zh-CN" for payload in self.requests))
        self.assertEqual(result["answer_language"], "zh-CN")
        self.assertEqual(result["answer_origin"], "diagnostic")
        self.assertRegex(result["answer"], r"[\u3400-\u9fff]")
        self.assertNotIn("No model answer", result["answer"])


if __name__ == "__main__":
    unittest.main()
