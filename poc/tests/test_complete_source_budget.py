"""Offline request captures for an independent, bounded whole-root allowance."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import _fit_request, run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from evidence_context import EvidenceContext
from repository_discovery import ensure_repository_search
from source_reading import _identify_page
from source_session import archive_evidence, read_archived_evidence


def program(name, size, *, comment="*> neutral physical source context", calls=""):
    header = (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
              "WORKING-STORAGE SECTION.\n01 BASIS PIC 9(5) VALUE 8.\n"
              "01 FACTOR PIC 9V99 VALUE 1.25.\n01 RESULT-AMOUNT PIC 9(7)V99.\n"
              "PROCEDURE DIVISION.\nMAIN.\n*> billing statement processing\n"
              + calls + "PERFORM CALCULATE-AMOUNT.\nPERFORM OUTPUT-AMOUNT.\nGOBACK.\n")
    return (header + (comment + "\n") * (size // (len(comment) + 1))
            + "CALCULATE-AMOUNT.\nIF BASIS > ZERO\n"
              "COMPUTE RESULT-AMOUNT = BASIS * FACTOR\nELSE\n"
              "MOVE ZERO TO RESULT-AMOUNT\nEND-IF.\nEXIT.\n"
              "OUTPUT-AMOUNT.\nDISPLAY RESULT-AMOUNT.\nEXIT.\n")


class CompleteSourceBudgetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
                                       api_key="offline-only")
        self.requests, self.request_sizes = [], []
        self.files = {}
        for owner, name in ((socket, "create_connection"), (socket.socket, "connect"),
                            (socket.socket, "connect_ex")):
            patcher = mock.patch.object(owner, name, side_effect=AssertionError("Network forbidden"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self, files):
        self.files = files
        for path, text in files.items():
            (self.source / path).write_text(text, encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True,
                             framework_reference_path="", quiet=True)
        ensure_repository_search(self.database, self.source)

    @staticmethod
    def pages(payload):
        return payload["source_context"][0]["pages"]

    def ask(self, *, policy=None, question="请分析 root.cbl 的完整业务流程。", entry=None):
        policy = replace(policy or AgentPolicy(), max_model_requests=1, max_answer_revisions=0)

        def transport(request):
            self.assertLessEqual(len(request.body), policy.max_request_bytes)
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            self.requests.append(payload)
            self.request_sizes.append(len(request.body))
            pages = self.pages(payload)
            page = next((page for page in pages if "COMPUTE RESULT-AMOUNT" in page["source_text"]), pages[0])
            answer = f"已读原文保留金额的计算与输出规则。[{page['evidence_id']}]"
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": answer},
                "finish_reason": "stop"}]}, ensure_ascii=False))

        result = run_business_chat(question, self.database, self.source, self.config,
            policy=policy, transport=transport, framework_reference_path="", entry_program=entry)["agent_result"]
        self.assertEqual(len(self.requests), 1)
        for page in self.pages(self.requests[0]):
            text = self.files[page["relative_path"]]
            self.assertEqual(page["source_text"], "\n".join(text.splitlines()[page["start_line"] - 1:page["end_line"]]))
            self.assertEqual(page["source_sha256"], hashlib.sha256(text.encode()).hexdigest())
            self.assertEqual(page["evidence_id"], _identify_page(page))
        return self.requests[0], result

    def test_long_resolved_root_is_supplied_whole_on_the_first_request(self):
        text = program("ROOT-RULE", 51000)
        self.build({"root.cbl": text})
        payload, result = self.ask()
        pages = self.pages(payload)
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["source_text"], text.rstrip("\n"))
        metadata = payload["source_context"][0]["working_set"]
        self.assertTrue(metadata["physical_complete"])
        self.assertTrue(metadata["complete_root_budget_applied"])
        self.assertEqual(metadata["max_source_characters"], 36000)
        self.assertEqual(metadata["max_complete_source_characters"], 128000)
        self.assertEqual(result["metrics"]["model_requests"], 1)
        self.assertEqual(result["metrics"]["provider_retries"], [])

    def test_explicit_complete_limit_can_be_smaller_than_the_excerpt_limit(self):
        self.build({"root.cbl": program("ROOT-RULE", 51000)})
        payload, _ = self.ask(policy=AgentPolicy(max_complete_source_characters=512))
        metadata = payload["source_context"][0]["working_set"]
        self.assertEqual(metadata["status"], "fallback")
        self.assertFalse(metadata["physical_complete"])
        self.assertLessEqual(sum(len(page["source_text"]) for page in self.pages(payload)), 36000)
        self.assertGreater(sum(len(page["source_text"]) for page in self.pages(payload)), 512)

    def test_small_excerpt_limit_does_not_disable_the_explicit_whole_root_budget(self):
        text = program("ROOT-RULE", 51000)
        self.build({"root.cbl": text})
        payload, _ = self.ask(policy=AgentPolicy(max_source_characters=512,
                                                max_complete_source_characters=60000))
        self.assertEqual(self.pages(payload)[0]["source_text"], text.rstrip("\n"))
        self.assertTrue(payload["source_context"][0]["working_set"]["complete_root_budget_applied"])

    def test_unresolved_question_and_selected_entry_keep_the_excerpt_budget(self):
        self.build({"root.cbl": program("ROOT-RULE", 51000)})
        payload, _ = self.ask(question="billing 的处理逻辑是什么？", entry="root.cbl")
        self.assertEqual(payload["business_map"]["source_identity"]["status"], "none")
        self.assertNotIn("working_set", payload["source_context"][0])
        self.assertLessEqual(sum(len(page["source_text"]) for page in self.pages(payload)), 36000)

    def test_dependencies_do_not_receive_the_roots_larger_allowance(self):
        self.build({"root.cbl": program("ROOT-RULE", 51000,
                        calls="CALL 'LARGE-RULE'.\nCALL 'SMALL-RULE'.\n"),
                    "large.cbl": program("LARGE-RULE", 39000),
                    "small.cbl": program("SMALL-RULE", 6000)})
        payload, _ = self.ask()
        complete = {page["relative_path"] for page in self.pages(payload)
                    if "complete_working_set" in page.get("selection_reasons", [])}
        self.assertEqual(complete, {"root.cbl", "small.cbl"})
        non_root = [page for page in self.pages(payload) if page["relative_path"] != "root.cbl"]
        self.assertLessEqual(sum(len(page["source_text"]) for page in non_root), 36000)
        metadata = payload["source_context"][0]["working_set"]
        self.assertIn("large.cbl", metadata["omitted_candidate_paths"])
        self.assertEqual(metadata["max_source_characters"], 36000)

    def test_encoded_limits_restore_related_excerpts_instead_of_empty_source(self):
        cases = ((51000, "*> neutral physical source context", 32768),
                 (100000, "*> 中性业务原文保留全部条件和金额计算步骤", 230000),
                 (51000, "*> 中性业务原文保留全部条件和金额计算步骤", 32768))
        for size, comment, maximum in cases:
            with self.subTest(size=size, request_limit=maximum):
                self.requests.clear()
                self.request_sizes.clear()
                self.build({"root.cbl": program("ROOT-RULE", size, comment=comment)})
                payload, result = self.ask(policy=AgentPolicy(max_request_bytes=maximum))
                self.assertTrue(self.pages(payload))
                self.assertTrue(any("COMPUTE RESULT-AMOUNT" in page["source_text"] for page in self.pages(payload)))
                self.assertLessEqual(sum(len(page["source_text"]) for page in self.pages(payload)), 36000)
                metadata = payload["source_context"][0]["working_set"]
                self.assertEqual(metadata["transmission_fallback_reason"], "complete_source_request_bytes")
                self.assertFalse(metadata["physical_complete"])
                self.assertEqual(metadata["transmission_status"], "partial")
                self.assertIn("root.cbl", metadata["omitted_complete_paths"])
                self.assertTrue(result["narrative"]["citations"])

    def test_derived_fallback_ranges_are_physical_and_can_be_read_from_the_archive(self):
        text = "\n".join(f"*> 中性业务说明与处理条件第{index}行" for index in range(1200))
        page = {"relative_path": "root.cbl", "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "start_line": 1, "end_line": 1200, "source_text": text, "span_truncated": False,
                "selection_reasons": ["complete_working_set"]}
        page["evidence_id"] = _identify_page(page)
        context = EvidenceContext()
        context.accept({"pages": [page], "metadata": {"status": "supplied", "root_paths": ["root.cbl"],
            "source_manifest": [{"relative_path": "root.cbl", "sha256": page["source_sha256"],
                "line_count": 1200, "source_characters": len(text), "evidence_id": page["evidence_id"]}]}},
            "complete_working_set")
        payload = {"question": "请说明业务规则。", "repository": {}, "business_map": {},
                   "source_context": context.bundle([page]), "framework_references": []}

        def fallback(actual, *, max_page_bytes=None):
            if max_page_bytes is None:
                return context.fallback_pages(36000)
            narrowed = context.narrow_page(actual["source_context"][0]["pages"][0], max_page_bytes)
            return [narrowed] if narrowed else []

        _, size = _fit_request(self.config, payload, [], AgentPolicy(max_request_bytes=32768),
                              source_fallback_builder=fallback)
        self.assertLessEqual(size, 32768)
        supplied = self.pages(payload)
        self.assertEqual(len(supplied), 1)
        self.assertLess(supplied[0]["end_line"], 1200)
        self.assertEqual(supplied[0]["source_text"], "\n".join(
            text.splitlines()[supplied[0]["start_line"] - 1:supplied[0]["end_line"]]))
        archive_evidence(self.database, supplied)
        archived = read_archived_evidence(self.database, supplied[0]["evidence_id"])
        self.assertEqual(archived["source_text"], supplied[0]["source_text"])
        self.assertEqual(archived["source_sha256"], supplied[0]["source_sha256"])


if __name__ == "__main__":
    unittest.main()
