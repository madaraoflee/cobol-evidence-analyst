"""Findings protocol fragments never become streamed or persisted answers."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


class FindingsPreviewBoundaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="findings-preview-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        (self.source / "root.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. TOTAL-RULE.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 BASE-AMOUNT PIC 9(5) VALUE 10.\n"
            "01 TOTAL-AMOUNT PIC 9(6).\nPROCEDURE DIVISION.\nMAIN.\n"
            "COMPUTE TOTAL-AMOUNT = BASE-AMOUNT * 2.\nGOBACK.\n", encoding="utf-8")
        (self.source / "helper.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. HELPER-RULE.\nPROCEDURE DIVISION.\n"
            "MAIN.\nDISPLAY 'HELPER'.\nGOBACK.\n", encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True,
                             framework_reference_path="", quiet=True)
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig("https://example.invalid/v1", "test-model",
                                       api_key="preview-test-credential")
        self.events, self.requests = [], []
        self.note_marker = "candidate-finding-protocol-marker"

    def ask(self, first_reply):
        final_answer = ""

        def transport(request):
            nonlocal final_answer
            envelope = json.loads(request.body)
            self.assertIs(envelope.get("stream"), True)
            payload = json.loads(envelope["messages"][-1]["content"])
            self.requests.append(payload)
            self.assertLessEqual(len(self.requests), 4, "Findings recovery did not converge")
            pages = [page for bundle in payload.get("source_context", []) for page in bundle["pages"]]
            if len(self.requests) == 1:
                identifier = next(page["evidence_id"] for page in pages if page["relative_path"] == "root.cbl")
                final_answer = ("基础金额初值来自 BASE-AMOUNT 声明中的 VALUE 10。"
                    "TOTAL-AMOUNT 按 BASE-AMOUNT 乘以二计算，得到20。" + f"[{identifier}]")
                parts = first_reply(identifier)
            else:
                parts = [final_answer]
            events = []
            for part in parts:
                events.append("data: " + json.dumps({"choices": [{"index": 0,
                    "delta": {"content": part}, "finish_reason": None}]}, ensure_ascii=False) + "\n\n")
            events.append('data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n')
            events.append("data: [DONE]\n\n")
            return TransportResponse(200, "".join(events).encode("utf-8"),
                                     {"Content-Type": "text/event-stream"})

        result = run_business_chat("root.cbl 的金额如何计算，输入初值来自哪里？", self.database,
            self.source, self.config, transport=transport, progress=self.events.append,
            framework_reference_path="", policy=AgentPolicy(max_answer_revisions=0))["agent_result"]
        self.assertGreaterEqual(len(self.requests), 2)
        self.assertEqual(result["answer"], final_answer)
        self.assertNotIn(self.note_marker, result["answer"])
        for event in self.events:
            preview = event.get("answer_preview", {}).get("text", "")
            self.assertNotIn(self.note_marker, preview)
            self.assertNotIn('"findings"', preview)
            self.assertNotIn('"read"', preview)
        self.assertTrue(any(event.get("answer_preview", {}).get("text") == final_answer for event in self.events))
        return result

    def object(self, identifier, *, read=True):
        value = {"findings": [{"text": self.note_marker, "evidence_ids": [identifier]}]}
        if read:
            value["read"] = [{"relative_path": "helper.cbl", "start_line": 1, "end_line": 6}]
        return json.dumps(value)

    def test_intro_followed_by_fragmented_findings_and_read_never_previews_json(self):
        def reply(identifier):
            value = self.object(identifier)
            return ["继续核对相关源码。\n", value[:5], value[5:12], value[12:]]
        self.ask(reply)

    def test_findings_only_object_is_recovered_without_preview_or_final_leak(self):
        self.ask(lambda identifier: [self.object(identifier, read=False)])

    def test_intro_and_malformed_findings_only_is_not_a_final_answer(self):
        self.ask(lambda identifier: ["继续核对相关源码。\n", '{"fin', 'dings": [',
                                     '{"text":"' + self.note_marker + '"'])

    def test_findings_with_unexpected_answer_field_is_not_a_final_answer(self):
        self.ask(lambda identifier: [json.dumps({"findings": [], "answer": self.note_marker})])

    def test_inline_intro_and_findings_protocol_is_not_a_final_answer(self):
        self.ask(lambda identifier: ["继续核对相关源码：", self.object(identifier)])


if __name__ == "__main__":
    unittest.main()
