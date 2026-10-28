"""Configured business documents reach chat and lead to verified source reads."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_analysis import _framework_for_prompt
from business_chat import _framework_prompt_references, run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


SOURCE = """IDENTIFICATION DIVISION.
PROGRAM-ID. COLLECTIONFLOW.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 REQUEST-STATE PIC X.
01 PROCESS-STATUS PIC 99.
PROCEDURE DIVISION.
MAIN.
    IF REQUEST-STATE NOT = 'A'
        MOVE 11 TO PROCESS-STATUS
        GOBACK
    END-IF.
    MOVE ZERO TO PROCESS-STATUS.
    GOBACK.
"""


class FrameworkDocumentChatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        (self.source / "jobs").mkdir(parents=True)
        (self.source / "jobs" / "collect.cbl").write_text(SOURCE, encoding="utf-8")
        self.database = self.root / "index.sqlite"
        build_business_index(self.source, self.database, source_format="free", quiet=True,
                             verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.manuals = self.root / "manuals"
        self.manuals.mkdir()
        for number in range(14):
            (self.manuals / f"guide-{number:02d}.md").write_text(
                f"# Processing conventions {number}\n\n"
                "REQUEST-STATE is a shared request marker. Inspect its returned state.\n",
                encoding="utf-8")
        self.name = "zz_Payment_Layer1_Business_Navigation.md"
        self.document = self.manuals / self.name
        self.document.write_text(
            "# Scheduled collection business index\n\n"
            "## 预约扣款\n\n"
            "预约扣款入口 `jobs/collect.cbl` lines 8–14. "
            "Navigation only; verify the implementation in source.\n", encoding="utf-8")
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "neutral-model",
                                       api_key="offline-test-key")
        self.requests = []

    def ask(self, question, respond, *, history=(), requests=1):
        transport_errors = []

        def transport(request):
            envelope = json.loads(request.body)
            payload = json.loads(envelope["messages"][-1]["content"])
            self.requests.append(payload)
            try:
                content = respond(payload)
            except Exception as error:
                transport_errors.append(error)
                raise
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": content}, "finish_reason": "stop"}]},
                ensure_ascii=False))

        with mock.patch("company_api.UrllibTransport", side_effect=AssertionError("network created")):
            result = run_business_chat(question, self.database, self.source, self.config,
                transport=transport, allow_network=False, framework_reference_path=self.manuals,
                history=history, policy=AgentPolicy(max_model_requests=requests))["agent_result"]
        if transport_errors:
            raise transport_errors[0]
        return result

    def reference(self, payload):
        references = [item for item in payload["framework_references"]
                      if item.get("document_name") == self.name]
        self.assertTrue(references, "The requested document must reach the actual model request")
        self.assertTrue(any("jobs/collect.cbl" in item["text"] for item in references))
        return references[0]

    def test_followup_reads_named_document_despite_old_failed_lookup_and_noisy_manuals(self):
        def respond(payload):
            reference = self.reference(payload)
            return f"已读取业务索引，其中指向扣款程序入口。[{reference['reference_id']}]"

        result = self.ask(f"为什么你没有读到 {self.name}？", respond, history=[
            {"role": "user", "content": "预约扣款什么情况下跳过？"},
            {"role": "assistant", "content": "此前检索没有定位到业务索引正文。"}])
        self.assertEqual(len(self.requests), 1)
        citations = result["narrative"]["citations"]
        self.assertTrue(any(item.get("document_name") == self.name and item.get("document_sha256")
                            == hashlib.sha256(self.document.read_bytes()).hexdigest() for item in citations))

    def check_business_source_read(self, question):
        def respond(payload):
            if len(self.requests) == 1:
                reference = self.reference(payload)
                path = re.search(r"`([^`]+\.cbl)`", reference["text"])[1]
                return json.dumps({"read": [{"relative_path": path, "start_line": 8,
                                              "end_line": 14}]})
            pages = [page for bundle in payload["source_context"] for page in bundle["pages"]]
            page = next(page for page in pages if page["relative_path"] == "jobs/collect.cbl"
                        and "MOVE 11 TO PROCESS-STATUS" in page["source_text"])
            return ("请求状态不是 A 时，程序把处理状态设为 11 并立即返回，跳过后续扣款处理。"
                    f"[{page['evidence_id']}]")

        result = self.ask(question, respond, requests=2)
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(any(item.get("read", {}).get("relative_path") == "jobs/collect.cbl"
                            for item in self.requests[-1]["completed_actions"]))
        self.assertIn("处理状态设为 11", result["answer"])
        self.assertTrue(result["evidence_refs"])

    def test_business_index_in_first_question_guides_a_real_source_read(self):
        self.check_business_source_read("预约扣款什么情况下会跳过？")

    def test_broad_business_and_change_question_finds_topic_in_document_filename(self):
        self.check_business_source_read("分析当前系统 payment 相关的业务信息；如果要改 payment，"
                                       "要注意什么，测试要测试什么？")

    def test_explicit_document_survives_full_bound_framework_rule_allowance(self):
        operations = [{"reference_id": f"fw:operation-{number}", "document_name": "runtime.md",
                       "text": f"ACTION-{number} retrieves a row.", "selection_reason": "source_marker"}
                      for number in range(10)]
        document = {"reference_id": "fw:business-index", "document_name": self.name,
                    "text": "Read jobs/collect.cbl for the collection entry.",
                    "selection_reason": "question_only"}
        framework = {"references": [*operations, document],
                     "coverage": {"document_lookup": [self.name]}}
        references, _ = _framework_prompt_references(framework, {}, [], AgentPolicy(),
            _framework_for_prompt, priority_ids=[item["reference_id"] for item in operations])
        self.assertIn("fw:business-index", {item["reference_id"] for item in references})
        self.assertLessEqual(len(references), 10)
        self.assertTrue(any(item["reference_id"].startswith("fw:operation-") for item in references))

        references, _ = _framework_prompt_references({"references": operations},
            {document["reference_id"]: {**document, "selection_reason": "framework_search"}},
            [document["reference_id"]], AgentPolicy(), _framework_for_prompt,
            priority_ids=[item["reference_id"] for item in operations])
        self.assertIn("fw:business-index", {item["reference_id"] for item in references})

    def test_current_lookup_survives_full_named_document_allowance(self):
        passages = [{"reference_id": f"fw:index-{number}", "document_name": self.name,
                     "text": f"Business entry {number} needs source verification.",
                     "selection_reason": "question_only"} for number in range(10)]
        current = {"reference_id": "fw:operation-lookup", "document_name": "runtime.md",
                   "text": "FETCH-ROW retrieves the selected row.", "selection_reason": "framework_search"}
        references, _ = _framework_prompt_references({"references": passages,
            "coverage": {"document_lookup": [self.name]}}, {current["reference_id"]: current},
            [current["reference_id"]], AgentPolicy(), _framework_for_prompt)
        self.assertIn("fw:operation-lookup", {item["reference_id"] for item in references})
        self.assertTrue(any(item["document_name"] == self.name for item in references))
        self.assertLessEqual(len(references), 10)


if __name__ == "__main__":
    unittest.main()
