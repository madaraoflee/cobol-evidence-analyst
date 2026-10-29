"""Saved business requirements govern source-reading requests and delivery."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import business_analysis
from company_api import CompanyAPIConfig, TransportResponse


def source_plan(count=1):
    pages = [{"evidence_id": f"ev_page_{index:024x}", "relative_path": "collection.cbl",
              "start_line": index * 3 + 1, "end_line": index * 3 + 3,
              "source_sha256": "synthetic-source-version", "span_truncated": False,
              "source_text": "IF VALID-FLAG = 'Y'\nWRITE COLLECTION-RECORD\nEND-IF."}
             for index in range(count)]
    return {"snapshot_id": "synthetic-snapshot", "pages": pages,
            "outline": [], "boundaries": [], "programs": [],
            "coverage": {"total_pages": count, "selected_pages": count, "complete": True}}


class BusinessAnalysisSavedRequirementsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "source-test-model",
                                       api_key="offline-test-key")
        self.requests = []
        self.payloads = []

    def run_analysis(self, instructions="", *, count=1, answer=None, question="解释这项业务的用途。", **options):
        def transport(request):
            envelope = json.loads(request.body)
            payload = json.loads(envelope["messages"][-1]["content"])
            self.requests.append(envelope)
            self.payloads.append(payload)
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": answer or "有效记录被写入收款记录文件。 [ev_page_000000000000000000000000]"},
                "finish_reason": "stop"}]}, ensure_ascii=False))

        with mock.patch.object(business_analysis, "prepare_source_reading", return_value=source_plan(count)), \
             mock.patch.object(business_analysis, "read_source_page_batch", side_effect=lambda _, pages, **kw: pages), \
             mock.patch("company_api.UrllibTransport", side_effect=AssertionError("network created")):
            return business_analysis.run_business_analysis(question, self.root / "index.sqlite", self.root,
                self.config, entry_program="COLLECTION", framework_context={}, allow_network=False,
                transport=transport, answer_instructions=instructions, **options)

    def assert_saved_requests(self, instructions):
        self.assertTrue(self.requests)
        for envelope, payload in zip(self.requests, self.payloads):
            system = envelope["messages"][0]["content"]
            self.assertEqual(system.count(instructions), 1)
            self.assertNotIn("answer_instructions", payload)
            self.assertNotIn("answer_detail", payload)
            self.assertNotIn("按问题需要充分解释相关业务结论、步骤、条件、例外和依据", payload["task"])
            self.assertNotIn("简要说明结论和关键条件，保留必要依据", payload["task"])
            self.assertIn("保存", payload["task"])
            self.assertIn("[evidence_id]", payload["task"])
            self.assertNotIn("tools", envelope)
            self.assertNotIn("response_format", envelope)

    def test_saved_one_sentence_requirement_replaces_default_depth_without_format_gate(self):
        instructions = "只用一句话解释业务结果，不要添加步骤和其他栏目。"
        answer = "有效记录被写入收款记录文件。 [ev_page_000000000000000000000000]"
        result = self.run_analysis(instructions, answer_detail="detailed", answer=answer)["agent_result"]
        self.assertEqual(len(self.requests), 1)
        self.assert_saved_requests(instructions)
        self.assertEqual(result["answer"], answer)
        self.assertEqual(result["status"], "ANALYZED")

    def test_saved_business_focus_reaches_every_page_reduction_and_final_synthesis(self):
        instructions = "说明收款业务的用途及上下游使用者，不要逐行翻译程序，不要扩写无关业务规则。"
        result = self.run_analysis(instructions, count=3, reading_strategy="full_chain", max_pages=2)["agent_result"]
        self.assertEqual(len(self.requests), 5)
        self.assert_saved_requests(instructions)
        self.assertEqual(sum(bool(item["source_pages"]) for item in self.payloads), 3)
        self.assertEqual(sum(not item["source_pages"] for item in self.payloads), 2)
        self.assertTrue(all("内部资料" in payload["task"] for payload in self.payloads[:-1]))
        self.assertIn("最终业务解释", self.payloads[-1]["task"])
        self.assertEqual(result["reading_coverage"]["summarized_pages"], 3)

    def test_saved_business_focus_reaches_focused_page_summaries_and_synthesis(self):
        instructions = "仅说明这项业务解决什么问题，以及结果交给谁使用；不要复述代码。"
        self.run_analysis(instructions, count=3, reading_strategy="focused", max_pages=3)
        self.assertEqual(len(self.requests), 4)
        self.assert_saved_requests(instructions)
        self.assertTrue(all("内部资料" in payload["task"] for payload in self.payloads[:-1]))
        self.assertIn("最终业务解释", self.payloads[-1]["task"])

    def test_retry_uses_changed_requirements_and_clearing_restores_original_default(self):
        old = "只说明本题的业务用途。"
        changed = "只说明处理后的业务结果，用一句话回答。"
        self.run_analysis(old)
        self.run_analysis(changed)
        self.run_analysis("")
        self.assertIn(old, self.requests[0]["messages"][0]["content"])
        self.assertNotIn(old, self.requests[1]["messages"][0]["content"])
        self.assertEqual(self.requests[1]["messages"][0]["content"].count(changed), 1)
        self.assertNotIn(changed, self.requests[2]["messages"][0]["content"])
        self.assertNotIn("answer_detail", self.payloads[1])
        self.assertEqual(self.payloads[2]["answer_detail"], "detailed")
        self.assertTrue(self.payloads[2]["task"].startswith("按问题需要充分解释相关业务结论、步骤、条件、例外和依据；"))

    def test_saved_explicit_language_controls_request_and_local_response_metadata(self):
        instructions = "请用英文回答，说明业务用途。"
        result = self.run_analysis(instructions, answer="Valid records are written to the collection file. "
            "[ev_page_000000000000000000000000]")["agent_result"]
        self.assert_saved_requests(instructions)
        self.assertEqual(self.payloads[0]["answer_language"], "en")
        self.assertEqual(result["answer_language"], "en")

    def test_current_explicit_language_still_overrides_saved_language(self):
        self.run_analysis("请用英文回答。", question="请用中文回答：这项业务有什么用途？")
        self.assertEqual(self.payloads[0]["answer_language"], "zh-CN")

    def test_no_saved_requirements_keep_existing_brief_task(self):
        self.run_analysis(answer_detail="brief")
        self.assertEqual(self.payloads[0]["answer_detail"], "brief")
        self.assertTrue(self.payloads[0]["task"].startswith("简要说明结论和关键条件，保留必要依据；"))


if __name__ == "__main__":
    unittest.main()
