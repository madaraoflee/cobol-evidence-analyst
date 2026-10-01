"""Offline request-size and shared reading-budget regressions."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
import business_chat
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from evidence_context import EvidenceContext, ReadTask
import repository_discovery
from repository_discovery import ensure_repository_search


class BusinessSynthesisLimitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
                                       api_key="offline-neutral-credential")

    def build(self, *, long_source=False):
        filler = "DISPLAY 'UNCHANGED'.\n" * 700 if long_source else ""
        (self.source / "rule.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. FINAL-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 BASIS PIC 9(5) VALUE 8.\n01 FACTOR PIC 9V99 VALUE 1.25.\n"
            "01 FINAL-AMOUNT PIC 9(7)V99.\nPROCEDURE DIVISION.\nMAIN.\n"
            + filler + "IF BASIS > ZERO\nCOMPUTE FINAL-AMOUNT = BASIS * FACTOR\n"
            "ELSE\nMOVE ZERO TO FINAL-AMOUNT\nEND-IF.\nGOBACK.\n", encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)

    def assert_brief_references_are_actually_supplied(self, payload):
        actual = {page["evidence_id"] for bundle in payload["source_context"] for page in bundle["pages"]}
        actual.update(row["reference_id"] for row in payload["framework_references"])
        brief = payload.get("business_analysis_brief", {})
        supplied = {identifier for item in brief.get("supplied_material", [])
                    for identifier in item["supplied_reference_ids"]}
        self.assertLessEqual(supplied, actual)

    def test_optional_brief_does_not_break_valid_question_at_minimum_request_budget(self):
        self.build()
        prefix = "请详细解释 rule.cbl 的金额计算流程。"
        question = prefix + ("业务" * 3000)[:6000 - len(prefix)]
        policy = AgentPolicy(max_model_requests=1, max_answer_revisions=0, max_request_bytes=32768)
        requests, answers = [], []

        def transport(request):
            self.assertLessEqual(len(request.body), policy.max_request_bytes)
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            self.assertEqual(payload["question"], question)
            self.assert_brief_references_are_actually_supplied(payload)
            page = next(page for bundle in payload["source_context"] for page in bundle["pages"])
            self.assertEqual(page["relative_path"], "rule.cbl")
            text = f"当前原文保留业务分支，计算口径仍需结合其余源码说明。[{page['evidence_id']}]"
            requests.append(payload)
            answers.append(text)
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": text}, "finish_reason": "stop"}]}, ensure_ascii=False))

        output = run_business_chat(question, self.database, self.source, self.config,
            transport=transport, framework_reference_path="", policy=policy)
        result = output["agent_result"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["answer"], answers[0])
        self.assertTrue(result["model_answer_recorded"])
        self.assertTrue(requests[0]["business_analysis_brief_omitted"])
        self.assertNotIn("business_analysis_brief", requests[0])
        trace = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text(encoding="utf-8"))
        self.assertEqual(trace["final"]["final_answer_round_id"], "round-1")
        self.assertIn("business_analysis_brief_request_bytes",
                      {item["reason"] for item in trace["rounds"][0]["trim_events"]})
        citation = result["narrative"]["citations"][0]
        support = result["claims"][0]["support"][0]
        self.assertEqual(support["reference_id"], citation["evidence_id"])
        self.assertEqual(support["source_sha256"], citation["source_sha256"])
        self.assertLessEqual(set(trace["final"]["final_visible_ids"]),
                             {page["evidence_id"] for bundle in requests[0]["source_context"] for page in bundle["pages"]})

    def test_last_turn_continuation_and_preliminary_read_share_the_actual_cap(self):
        self.build(long_source=True)
        original_init = EvidenceContext.__init__
        original_map = business_chat.build_business_map
        original_read = repository_discovery.read_repository_context

        def with_open_read(context):
            original_init(context)
            context.tasks["neutral-open-read"] = ReadTask(
                "neutral-open-read", "rule.cbl", {"start_line": 1, "end_line": 800},
                next_start_line=1)

        def limited_map(*args, **kwargs):
            return {**original_map(*args, **kwargs), "spotlights": [],
                    "rule_leads": [], "semantic_anchors": []}

        for cap in (0, 1):
            with self.subTest(read_cap=cap):
                reads, requests = [], []

                def tracked_read(*args, **kwargs):
                    reads.append(kwargs)
                    return original_read(*args, **kwargs)

                def transport(request):
                    payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
                    requests.append(payload)
                    self.assert_brief_references_are_actually_supplied(payload)
                    self.assertTrue(payload["question_investigation"]["planned_actions"])
                    self.assertEqual(payload["investigation_budget"]["reads_per_turn"], 0)
                    return TransportResponse(200, '{"choices":[{"message":{"content":'
                        '"已知程序处理金额，计算式仍待核对。"},"finish_reason":"stop"}]}')

                policy = AgentPolicy(max_model_requests=1, max_answer_revisions=0,
                    max_reads_per_turn=cap, max_business_context_actions_per_turn=0,
                    initial_pages=1, initial_source_characters=512, read_source_characters=512)
                with mock.patch.object(EvidenceContext, "__init__", with_open_read), \
                     mock.patch.object(business_chat, "build_business_map", side_effect=limited_map), \
                     mock.patch.object(repository_discovery, "read_repository_context", side_effect=tracked_read):
                    output = run_business_chat("请详细解释 rule.cbl 的金额计算流程。",
                        self.database, self.source, self.config, transport=transport,
                        framework_reference_path="", policy=policy)
                self.assertEqual(len(requests), 1)
                self.assertEqual(len(reads), cap)
                self.assertEqual(output["agent_result"]["metrics"]["tool_calls"]["read"], cap)


if __name__ == "__main__":
    unittest.main()
