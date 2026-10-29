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
            [self.page | {"replacement_trace": [{"old": ["SOURCE"], "new": ["TARGET"]}]}],
            [self.page | {"copy_instance": "copy-instance-one"}],
            [self.page | {"selection_reasons": ["repository_orientation"]}],
        )
        for pages in variants:
            with self.subTest(pages=pages):
                self.assertFalse(self.findings("rule.cbl 的完整源码未提供。", pages=pages))
                self.assertFalse(self.findings("我只能看到 rule.cbl 的部分代码。", pages=pages))
        self.assertFalse(self.findings("rule.cbl 的完整源码未提供。", working_set={}))

    def test_derived_ranges_cannot_fill_gaps_in_original_source_coverage(self):
        physical = self.page | {"end_line": 1, "source_text": "MAIN.", "evidence_id": "ev_first"}
        for provenance in ({"replacement_trace": [{"old": ["AMOUNT"], "new": ["OTHER"]}]},
                           {"copy_instance": "copy-instance-one"}):
            with self.subTest(provenance=provenance):
                derived = self.page | {"start_line": 2, "source_text": "MOVE 1 TO OTHER.\nGOBACK.",
                                       "evidence_id": "ev_derived", **provenance}
                available = supplied_source_availability([physical, derived], self.working_set)
                self.assertFalse(available["programs/rule.cbl"]["complete_source_supplied"])
                self.assertEqual(available["programs/rule.cbl"]["evidence_ids"], ["ev_first"])
                self.assertFalse(self.findings("rule.cbl 的完整源码未提供。", pages=[physical, derived]))

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

    def test_current_source_denial_can_bind_to_one_verified_complete_root(self):
        metadata = {**self.working_set, "root_paths": ["programs/rule.cbl"]}
        for text in ("本轮未提供完整源码，只能部分分析。", "当前程序的完整源码未提供。",
                     "目前我只能看到部分代码。", "本次只提供了部分源码。",
                     "金额乘以二。[ev_rule] 本轮未提供完整源码，只能部分分析。",
                     "本轮未提供完整源码。[ev_rule]",
                     "The current program source was not supplied.",
                     "I only have part of the source for this program."):
            with self.subTest(text=text):
                findings = self.findings(text, working_set=metadata)
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0]["relative_path"], "programs/rule.cbl")
                self.assertEqual(findings[0]["source_sha256"], "version-one")
                self.assertEqual(findings[0]["evidence_ids"], ["ev_rule"])

    def test_current_source_denial_requires_explicit_root_and_actual_full_coverage(self):
        text = "本轮未提供完整源码，只能部分分析。"
        for metadata in (self.working_set,
                         {**self.working_set, "root_paths": ["programs/rule.cbl", "other.cbl"]},
                         {**self.working_set, "root_paths": ["other.cbl"]},
                         {"root_paths": ["programs/rule.cbl"], "physical_complete": True}):
            with self.subTest(metadata=metadata):
                self.assertFalse(self.findings(text, working_set=metadata))
        metadata = {**self.working_set, "root_paths": ["programs/rule.cbl"]}
        for page in (self.page | {"source_sha256": "version-two"},
                     self.page | {"span_truncated": True},
                     self.page | {"end_line": 1, "source_text": "MAIN."},
                     self.page | {"include_chain": [{"path": "parent.cbl", "line": 1}]},
                     self.page | {"selection_reasons": ["repository_orientation"]}):
            with self.subTest(page=page):
                self.assertFalse(self.findings(text, pages=[page], working_set=metadata))

    def test_complete_root_does_not_establish_dependencies_or_explanation_completeness(self):
        metadata = {**self.working_set, "root_paths": ["programs/rule.cbl"]}
        for text in ("本轮未提供依赖程序的完整源码。", "本轮未提供整个项目的完整源码。",
                     "本轮只提供了部分系统代码。", "当前调用的程序源码未提供。",
                     "当前程序的运行参数未提供。", "当前程序只能部分分析。",
                     "本轮只能提供部分业务解读。", "当前程序的源码已经提供，但业务分析不完整。",
                     "本轮只提供了部分源码的解释。", "当前未提供完整源码的分析。",
                     "本轮没有提供所有源码。", "The current explanation of the source is incomplete.",
                     "如果本轮未提供完整源码，只能部分分析。", "本轮不是未提供完整源码。",
                     "历史回答称本轮未提供完整源码。", "当前程序的完整源码未提供时需要读取。",
                     "当前 external.cbl 源码未提供。", "本轮未提供 HELPER-RULE 的完整源码。",
                     "The current program's dependency source is missing.",
                     "The current project source is incomplete.",
                     "当前完整源码已提供。\n> 本轮未提供完整源码。"):
            with self.subTest(text=text):
                self.assertFalse(self.findings(text, working_set=metadata))

    def test_current_root_denial_still_detected_when_dependency_source_is_partial(self):
        dependency = self.page | {"relative_path": "helper.cbl", "evidence_id": "ev_helper", "end_line": 1,
                                  "source_text": "HELPER."}
        metadata = {**self.working_set, "root_paths": ["programs/rule.cbl"],
                    "source_manifest": [*self.working_set["source_manifest"],
                        {"relative_path": "helper.cbl", "sha256": "version-one", "line_count": 30}]}
        findings = self.findings("当前程序完整源码未提供。", pages=[self.page, dependency], working_set=metadata)
        self.assertEqual([item["relative_path"] for item in findings], ["programs/rule.cbl"])
        self.assertFalse(self.findings("当前依赖程序完整源码未提供。", pages=[self.page, dependency], working_set=metadata))


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

    def assert_local_repair(self, result):
        self.assertIn("金额按基础金额乘以二得到", result["answer"])
        self.assertNotIn("没有提供", result["answer"])
        self.assertNotIn("未提供完整源码", result["answer"])
        self.assertNotIn("无法完整解答", result["answer"])
        self.assertFalse(result["business_review"]["answer_completion"]["source_availability_contradiction"])
        trace = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text())
        repairs = trace["source_availability_repairs"]
        self.assertTrue(repairs)
        self.assertTrue(all(row["source_sha256"] for row in repairs))
        self.assertTrue(all(row["supplied_ranges"] for row in repairs))
        self.assertTrue(any("未提供" in row["original_text"] or "没有提供" in row["original_text"]
                            for row in repairs))
        self.assertFalse(result["claims_semantically_verified"])

    def test_first_false_denial_is_repaired_in_one_request(self):
        result = self.ask(["金额按基础金额乘以二得到。[evidence] 但 rule.cbl 源码本轮没有提供，无法完整解答。"])
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(result["business_review"]["synthesis_review_attempted"])
        self.assert_local_repair(result)

    def test_unnamed_current_source_denial_is_locally_repaired(self):
        result = self.ask(["金额按基础金额乘以二得到。[evidence] 本轮未提供完整源码，只能部分分析。"])
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("只能部分分析", result["answer"])
        self.assert_local_repair(result)

    def test_false_denial_in_revision_is_corrected_before_delivery(self):
        result = self.ask(["现有材料还没有选定源码。",
                          "金额按基础金额乘以二得到。[evidence] 但 rule.cbl 的完整源码本轮没有提供，无法完整解答。"])
        self.assertEqual(len(self.requests), 2)
        self.assert_local_repair(result)


if __name__ == "__main__":
    unittest.main()
