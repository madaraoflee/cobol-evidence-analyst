"""Offline source-availability checks bound to actual answer material."""

import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_chat import run_business_chat
from business_index import build_business_index
from business_synthesis import assess_business_answer
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search
from source_availability import source_availability_contradictions, supplied_source_availability


class SourceAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.page = {"relative_path": "programs/rule.cbl", "source_sha256": "version-one",
            "start_line": 1, "end_line": 3, "source_text": "MAIN.\nMOVE 1 TO AMOUNT.\nGOBACK.",
            "evidence_id": "ev_rule", "span_truncated": False}
        self.working_set = {"status": "supplied", "supplied_complete_paths": ["programs/rule.cbl"],
            "source_manifest": [{"relative_path": "programs/rule.cbl", "sha256": "version-one",
                                 "line_count": 3, "source_characters": len(self.page["source_text"])}]}

    def findings(self, text, *, pages=None, working_set=None):
        return source_availability_contradictions(text, [self.page] if pages is None else pages,
            self.working_set if working_set is None else working_set)

    def test_named_denial_matches_actual_path_or_unique_basename(self):
        for text in ("programs/rule.cbl 源码本轮没有提供。", "rule.cbl 本轮未提供。",
                     "programs\\rule.cbl 源码本轮没有提供。", "RULE.CBL 本轮未提供。",
                     "金额乘以二，但 `rule.cbl` 的源码没有提供，无法完整解答。",
                     "rule.cbl 的完整源码本轮没有提供。", "我只能看到 rule.cbl 的部分代码。",
                     "The source for rule.cbl was not supplied.", "I only have part of rule.cbl."):
            with self.subTest(text=text):
                findings = self.findings(text)
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0]["relative_path"], "programs/rule.cbl")

    def test_partial_denial_requires_matching_full_file_coverage(self):
        variants = (
            [self.page | {"start_line": 2, "end_line": 3, "source_text": "MOVE 1 TO AMOUNT.\nGOBACK."}],
            [self.page | {"source_sha256": "version-two"}],
            [self.page | {"span_truncated": True}],
            [self.page | {"source_text": "MAIN."}],
            [self.page | {"include_chain": [{"path": "caller.cbl", "line": 5}]}],
            [self.page | {"selection_reasons": ["repository_orientation"]}],
        )
        for pages in variants:
            with self.subTest(pages=pages):
                self.assertFalse(self.findings("rule.cbl 的完整源码未提供。", pages=pages))
                self.assertFalse(self.findings("我只能看到 rule.cbl 的部分代码。", pages=pages))
        self.assertFalse(self.findings("rule.cbl 的完整源码未提供。", working_set={}))

    def test_contiguous_same_version_ranges_prove_full_source_but_gaps_do_not(self):
        pages = [self.page | {"end_line": 1, "source_text": "MAIN.", "evidence_id": "ev_first"},
                 self.page | {"start_line": 2, "source_text": "MOVE 1 TO AMOUNT.\nGOBACK.", "evidence_id": "ev_rest"}]
        findings = self.findings("rule.cbl 的全文未提供。", pages=pages)
        self.assertEqual(findings[0]["evidence_ids"], ["ev_first", "ev_rest"])
        self.assertEqual(findings[0]["source_sha256"], "version-one")
        self.assertEqual(findings[0]["supplied_ranges"], [{"start_line": 1, "end_line": 1},
                                                       {"start_line": 2, "end_line": 3}])
        pages[1].update(start_line=3, source_text="GOBACK.")
        self.assertFalse(self.findings("rule.cbl 的全文未提供。", pages=pages))

    def test_canonical_final_blank_line_counts_as_a_supplied_physical_line(self):
        page = self.page | {"end_line": 2, "source_text": "MAIN.\n"}
        metadata = {"source_manifest": [{"relative_path": "programs/rule.cbl", "sha256": "version-one",
                                        "line_count": 2}]}
        findings = self.findings("rule.cbl 的全文未提供。", pages=[page], working_set=metadata)
        self.assertEqual(findings[0]["line_count"], 2)
        self.assertEqual(findings[0]["supplied_ranges"], [{"start_line": 1, "end_line": 2}])

    def test_real_callee_runtime_and_non_assertion_boundaries_are_preserved(self):
        for text in (
            "rule.cbl 已读取，external.cbl 的实现未提供。",
            "rule.cbl 调用 EXTERNAL-STEP 的实现未提供。",
            "rule.cbl 调用 HELPER 的源码未提供。",
            "rule.cbl calls HELPER whose code is unavailable.",
            "rule.cbl 的被调用程序实现未提供，无法确认副作用。",
            "rule.cbl 的运行输入未提供，无法计算实际金额。",
            "rule.cbl 源码已提供，数据库运行数据没有提供。",
            "如果 rule.cbl 未提供，就无法解答。",
            "rule.cbl 未提供时，界面禁用按钮。",
            "rule.cbl 不是没有提供。",
            "历史回答曾称“rule.cbl 源码未提供”。",
            "以下是别处的引述：`rule.cbl 源码未提供`。",
            "> rule.cbl 源码未提供。\n本轮已经读取源码。",
            "```text\nrule.cbl 源码未提供。\n```\n本轮已经读取源码。",
            "目前我只能看到部分代码。",
        ):
            with self.subTest(text=text):
                self.assertFalse(self.findings(text))

    def test_duplicate_basename_cannot_assert_one_implementation_was_supplied(self):
        other = self.page | {"relative_path": "other/rule.cbl", "evidence_id": "ev_other"}
        self.assertFalse(self.findings("rule.cbl 源码未提供。", pages=[self.page, other]))
        self.assertTrue(self.findings("programs/rule.cbl 源码未提供。", pages=[self.page, other]))
        metadata = {**self.working_set, "source_manifest": [*self.working_set["source_manifest"],
            {"relative_path": "other/rule.cbl", "sha256": "other-version", "line_count": 3}]}
        self.assertFalse(self.findings("rule.cbl 源码未提供。", working_set=metadata))

    def test_answer_depth_does_not_claim_source_completeness(self):
        pages = [self.page | {"end_line": 1, "source_text": "MAIN."}]
        findings = self.findings("rule.cbl 源码没有提供因此无法完整解答。", pages=pages)
        self.assertEqual(findings[0]["assertion"], "source_not_supplied")
        self.assertFalse(self.findings("rule.cbl 的完整源码没有提供因此无法完整解答。", pages=pages))

    def test_boolean_completeness_metadata_cannot_replace_actual_ranges(self):
        metadata = {"physical_complete": True, "supplied_complete_paths": ["programs/rule.cbl"]}
        availability = supplied_source_availability([self.page], metadata)
        self.assertFalse(availability["programs/rule.cbl"]["complete_source_supplied"])
        assessment = assess_business_answer("金额怎么计算？", "rule.cbl 全文未提供。", {},
            [self.page], working_set=metadata)
        self.assertFalse(assessment["source_availability_contradiction"])


class SourceAvailabilityChatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="source-availability-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.text = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. TOTAL-RULE.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 BASE-AMOUNT PIC 9(5) VALUE 10.\n"
            "01 TOTAL-AMOUNT PIC 9(6).\nPROCEDURE DIVISION.\nMAIN.\n"
            "COMPUTE TOTAL-AMOUNT = BASE-AMOUNT * 2.\nGOBACK.\n")
        (self.source / "rule.cbl").write_text(self.text, encoding="utf-8")
        self.database = self.root / "index.sqlite"
        build_business_index(self.source, self.database, source_format="free", framework_reference_path="", quiet=True)
        ensure_repository_search(self.database, self.source)
        self.requests = []
        for owner, attribute in ((socket, "create_connection"), (socket.socket, "connect"),
                                 (socket.socket, "connect_ex")):
            blocker = patch.object(owner, attribute, side_effect=AssertionError("Network forbidden"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def ask(self, replies):
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            self.requests.append(payload)
            page = next(page for bundle in payload["source_context"] for page in bundle["pages"]
                        if "COMPUTE TOTAL-AMOUNT" in page["source_text"])
            self.assertEqual(page["source_text"].splitlines(), self.text.splitlines())
            text = replies[min(len(self.requests) - 1, len(replies) - 1)]
            text = text.replace("[evidence]", "[" + page["evidence_id"] + "]")
            return TransportResponse(200, json.dumps({"choices": [{
                "message": {"content": text}, "finish_reason": "stop"}]}, ensure_ascii=False))
        return run_business_chat("rule.cbl 的金额怎么计算？", self.database, self.source,
            CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model", api_key="offline-only"),
            framework_reference_path="", transport=transport)["agent_result"]

    def test_first_answer_false_denial_gets_one_review_with_default_policy(self):
        result = self.ask(["金额按基础金额乘以二得到。[evidence] 但 rule.cbl 源码本轮没有提供，无法完整解答。",
                           "金额按基础金额乘以二得到。[evidence]"])
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(result["business_review"]["synthesis_review_attempted"])
        self.assertTrue(self.requests[1]["answer_review"]["source_availability_contradiction"])
        self.assertNotIn("没有提供", result["answer"])

    def test_review_synonym_cannot_promote_false_denial_to_success(self):
        result = self.ask(["现有材料还没有选定源码。",
                          "金额按基础金额乘以二得到。[evidence] 但 rule.cbl 的完整源码本轮没有提供，无法完整解答。"])
        self.assertEqual(len(self.requests), 2)
        self.assertNotEqual(result["status"], "ANALYZED")
        self.assertNotEqual(result["stop_reason"], "sufficient_material")
        self.assertTrue(result["business_review"]["answer_completion"]["source_availability_contradiction"])


if __name__ == "__main__":
    unittest.main()
