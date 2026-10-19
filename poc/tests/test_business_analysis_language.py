"""Source-reading failures and local annotations follow the requested language."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_analysis import run_business_analysis
from company_api import CompanyAPIConfig, TransportResponse


def prepared_plan(count=1):
    source = "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2."
    pages = [{"evidence_id": f"ev_page_{index}", "relative_path": f"amount-{index}.cbl",
              "start_line": 1, "end_line": 1, "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
              "source_text": source, "span_truncated": False} for index in range(count)]
    return {"snapshot_id": "snapshot-test", "pages": pages,
            "outline": [{"relative_path": page["relative_path"], "program_name": f"AMOUNT-{index}"}
                        for index, page in enumerate(pages)],
            "boundaries": [], "coverage": {"total_pages": count, "selected_pages": count,
                "total_lines": count, "total_files": count, "selected_files": count, "complete": True}}


def response(text, *, finish_reason="stop"):
    return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": text},
                                                        "finish_reason": finish_reason}]}))


class BusinessAnalysisLanguageTests(unittest.TestCase):
    def run_analysis(self, transport, *, plan=None, question="Explain how the amount is calculated.", **options):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "index.sqlite"
            database.touch()
            with mock.patch("business_analysis.prepare_source_reading", return_value=plan or prepared_plan()):
                return run_business_analysis(question, database, root,
                    CompanyAPIConfig("https://gateway.example.invalid/v1", "offline-test-model", api_key="test-only"),
                    framework_context={}, transport=transport, **options)["agent_result"]

    def assert_english_local_text(self, result):
        self.assertEqual(result["answer_language"], "en")
        self.assertNotRegex(result["answer"], r"[\u3400-\u9fff]")
        for boundary in result["boundaries"]:
            self.assertNotRegex(boundary["message"], r"[\u3400-\u9fff]")

    def test_synthesis_failure_preserves_english_page_answers_and_citations(self):
        for strategy, count, heading in (("focused", 3, "Completed section explanations"),
                                         ("full_chain", 2, "Completed program explanations")):
            with self.subTest(strategy=strategy):
                supplied = prepared_plan(count)
                requests, explanations = [], []

                def transport(request):
                    payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
                    requests.append(payload)
                    self.assertEqual(payload["answer_language"], "en")
                    if payload["source_pages"]:
                        source = payload["source_pages"][0]
                        text = f"The amount is doubled in {source['relative_path']}. [{source['evidence_id']}]"
                        explanations.append(text)
                        return response(text)
                    return TransportResponse(503, "Temporary service failure")

                result = self.run_analysis(transport, plan=supplied, reading_strategy=strategy)
                self.assert_english_local_text(result)
                self.assertEqual(len(requests), count + 1)
                self.assertEqual(result["status"], "PARTIAL")
                self.assertIn(heading, result["answer"])
                for text in explanations:
                    self.assertIn(text, result["answer"])
                if strategy == "focused":
                    self.assertIn("amount-0.cbl: lines 1–1", result["answer"])
                self.assertEqual([item["text"] for item in result["page_summaries"]], explanations)
                self.assertEqual({item["evidence_id"] for item in result["narrative"]["citations"]},
                                 {page["evidence_id"] for page in supplied["pages"]})

    def test_all_failed_requests_render_english_diagnostic_and_preserved_plan(self):
        for strategy in ("focused", "full_chain"):
            for count in (1, 3):
                with self.subTest(strategy=strategy, count=count):
                    calls = []

                    def transport(request):
                        calls.append(request)
                        return TransportResponse(503, "Temporary service failure")

                    result = self.run_analysis(transport, plan=prepared_plan(count), reading_strategy=strategy)
                    self.assert_english_local_text(result)
                    self.assertEqual(result["status"], "ABSTAINED")
                    self.assertIn("source reading plan has been retained", result["answer"])
                    self.assertIn("503", result["answer"])
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(result["reading_coverage"]["summarized_pages"], 0)

    def test_empty_model_answer_uses_english_failure_without_diagnostic(self):
        result = self.run_analysis(lambda request: response(""))
        self.assert_english_local_text(result)
        self.assertEqual(result["status"], "ABSTAINED")
        self.assertIn("No readable business explanation was returned", result["answer"])
        self.assertIn("source reading plan has been retained", result["answer"])

    def test_completed_english_answer_remains_unchanged(self):
        text = "The amount doubles under the supplied calculation. [ev_page_0]"
        result = self.run_analysis(lambda request: response(text), locale="zh-CN")
        self.assert_english_local_text(result)
        self.assertEqual(result["status"], "ANALYZED")
        self.assertEqual(result["answer"], text)
        self.assertEqual(result["narrative"]["text"], text)

    def test_unknown_reference_placeholder_and_partial_boundaries_use_english(self):
        supplied = prepared_plan()
        supplied["coverage"]["call_chain"] = {"uncovered_calls": 1, "unresolved_calls": 1}
        text = "The amount is doubled. [ev_page_0] A separate rule has no supplied source. [ev_missing]"
        for finish in ("length", "content_filter"):
            with self.subTest(finish=finish):
                result = self.run_analysis(lambda request: response(text, finish_reason=finish), plan=supplied,
                    analysis_scope={"complete_dependency_closure": False})
                self.assert_english_local_text(result)
                self.assertEqual(result["status"], "PARTIAL")
                self.assertIn("[unconfirmed source reference]", result["answer"])
                self.assertIn("[ev_page_0]", result["answer"])
                self.assertNotIn("[ev_missing]", result["answer"])
                reasons = {item["reason"] for item in result["boundaries"]}
                self.assertTrue({"UNKNOWN_SOURCE_REFERENCE", "SOURCE_SCOPE_INCOMPLETE", "CALL_CHAIN_INCOMPLETE"} <= reasons)

    def test_chinese_request_still_uses_chinese_failure_text(self):
        result = self.run_analysis(lambda request: response(""), question="请解释金额怎样计算。", locale="en")
        self.assertEqual(result["answer_language"], "zh-CN")
        self.assertIn("本次未取得可阅读的业务解释", result["answer"])


if __name__ == "__main__":
    unittest.main()
