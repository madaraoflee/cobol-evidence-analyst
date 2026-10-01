from __future__ import annotations

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


class QuestionInvestigationChatTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model",
                                       api_key="local-test-secret")
        self.requests = []

    def write(self, path, name, body, data=""):
        (self.source / path).write_text(
            f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
            f"WORKING-STORAGE SECTION.\n{data}\nPROCEDURE DIVISION.\nMAIN.\n{body}\nGOBACK.\n",
            encoding="utf-8")

    def build(self):
        build_business_index(self.source, self.database, source_format="free")
        ensure_repository_search(self.database, self.source)

    def ask(self, question, respond, **options):
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            self.requests.append(payload)
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": respond(payload)}, "finish_reason": "stop"}]},
                ensure_ascii=False))
        return run_business_chat(question, self.database, self.source, self.config,
            transport=transport, framework_reference_path=options.pop("framework_reference_path", ""),
            **options)["agent_result"]

    def calculation(self):
        self.write("rule.cbl", "FINAL-RULE", "IF BASIS > ZERO\n"
            "COMPUTE FINAL-AMOUNT = BASIS * FACTOR\n"
            "ELSE\nMOVE ZERO TO FINAL-AMOUNT\nEND-IF.",
            "01 BASIS PIC 9(5).\n01 FACTOR PIC 9V99 VALUE 1.25.\n01 FINAL-AMOUNT PIC 9(7)V99.")
        self.build()

    def final_reply(self, payload):
        page = next(page for page in payload["source_context"][0]["pages"]
                    if "COMPUTE FINAL-AMOUNT" in page["source_text"])
        self.assertIn("IF BASIS > ZERO", page["source_text"])
        self.assertIn("MOVE ZERO TO FINAL-AMOUNT", page["source_text"])
        return f"基础金额大于零时按基础金额乘系数计算；否则结果为零。[{page['evidence_id']}]"

    def assert_input_partial(self, result):
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["stop_reason"], "question_evidence_incomplete")
        items = result["investigation_state"]["question_investigation"]["required_items"]
        inputs = next(item for item in items if item["kind"] == "inputs")
        self.assertEqual(inputs["reason"], "input_source_not_located")
        self.assertIn("BASIS", inputs["fields"])

    def test_unknown_object_cannot_be_located_by_orientation_call_dependency(self):
        self.write("a-entry.cbl", "ENTRY-RULE", 'CALL "CALLED-RULE".')
        for index in range(8):
            self.write(f"sample-{index}.cbl", f"SAMPLE-{index}", "DISPLAY 'UNRELATED'.")
        self.write("z-called.cbl", "CALLED-RULE", "COMPUTE RESULT-AMOUNT = 1 + 2.",
                   "01 RESULT-AMOUNT PIC 9(4).")
        self.build()
        result = self.ask("星尘结算是什么？", lambda payload: "这是一个结算计算功能。")
        self.assertEqual(self.requests[0]["source_context"][0]["pages"], [])
        self.assertEqual(self.requests[0]["retrieval_status"]["state"], "needs_discovery")
        self.assertEqual(result["status"], "ABSTAINED")
        self.assertEqual(result["stop_reason"], "RETRIEVAL_UNRESOLVED")
        self.assertEqual(result["investigation"]["retrieval_status"], "unresolved")

    def test_plain_text_deferral_gets_bounded_followup_with_actual_formula(self):
        self.calculation()
        result = self.ask("FINAL-AMOUNT 怎么计算？", lambda payload:
            "需要先找公式和条件才能解释。" if len(self.requests) == 1 else self.final_reply(payload))
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(self.requests[1]["source_context"][0]["pages"])
        self.assertIn("question_investigation", self.requests[1])
        self.assert_input_partial(result)
        self.assertEqual(result["metrics"]["policy"]["max_model_requests"], 5)

    def test_malformed_action_is_reprompted_and_never_recorded_as_business_answer(self):
        self.calculation()
        result = self.ask("FINAL-AMOUNT 怎么计算？", lambda payload:
            '{"read":[{"relative_path":"rule.cbl", "start_line": 8}'
            if len(self.requests) == 1 else self.final_reply(payload))
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(self.requests[1]["source_context"][0]["pages"])
        self.assert_input_partial(result)
        self.requests.clear()
        result = self.ask("FINAL-AMOUNT 怎么计算？", lambda payload: '{"search": [',
            policy=AgentPolicy(max_model_requests=1))
        self.assertEqual(result["status"], "ABSTAINED")
        self.assertNotEqual(result["stop_reason"], "sufficient_material")

    def test_budget_cannot_promote_deferral_to_sufficient_material(self):
        self.calculation()
        result = self.ask("FINAL-AMOUNT 怎么计算？", lambda payload: "需要先找公式和条件。",
            policy=AgentPolicy(max_model_requests=1))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["status"], "ABSTAINED")
        self.assertNotEqual(result["stop_reason"], "sufficient_material")

    def test_unknown_citation_remains_partial_after_citation_is_removed(self):
        self.calculation()
        result = self.ask("FINAL-AMOUNT 怎么计算？", lambda payload:
            "按基础金额乘系数计算。[ev_unknown_rule]")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertNotEqual(result["stop_reason"], "sufficient_material")
        self.assertNotIn("ev_unknown_rule", result["answer"])

    def test_business_prose_citations_links_and_json_are_not_investigation_requests(self):
        self.calculation()
        def citation_first(payload):
            page = next(page for page in payload["source_context"][0]["pages"]
                        if "COMPUTE FINAL-AMOUNT" in page["source_text"])
            return f"[{page['evidence_id']}] 基础金额乘系数计算。"
        for respond in (citation_first,
                        lambda payload: "[处理说明](#result) 基础金额乘系数计算。",
                        lambda payload: '{"rate":1.25,"meaning":"计算系数"}',
                        lambda payload: "处理需要先读取参数再计算，基础金额乘系数得到结果。"):
            with self.subTest(respond=respond):
                self.requests.clear()
                result = self.ask("FINAL-AMOUNT 怎么计算？", respond)
                self.assertEqual(len(self.requests), 1)
                self.assert_input_partial(result)

    def test_unknown_tool_descriptor_is_reprompted(self):
        self.calculation()
        result = self.ask("FINAL-AMOUNT 怎么计算？", lambda payload:
            '{"tool":"scan_repository","arguments":{}}'
            if len(self.requests) == 1 else self.final_reply(payload))
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(self.requests[1]["source_context"][0]["pages"])
        self.assert_input_partial(result)

    def test_request_trimming_reuses_candidates_and_reports_only_visible_evidence(self):
        self.write("rule.cbl", "FINAL-RULE", "MOVE 10 TO BASIS.\n"
            "COMPUTE FINAL-AMOUNT = BASIS * FACTOR.\n" +
            "DISPLAY 'UNCHANGED SOURCE PAYLOAD'.\n" * 1600 +
            "IF BASIS > 1\nADD 2 TO FINAL-AMOUNT\nEND-IF.",
            "01 BASIS PIC 9(5).\n01 FACTOR PIC 9V99 VALUE 1.25.\n01 FINAL-AMOUNT PIC 9(7)V99.")
        self.build()
        import question_investigation
        with mock.patch("question_investigation._indexed_candidates",
                        wraps=question_investigation._indexed_candidates) as indexed:
            result = self.ask("FINAL-RULE FINAL-AMOUNT怎么计算？",
                lambda payload: "已提供计算式及后续调整候选。",
                policy=AgentPolicy(max_model_requests=1, max_request_bytes=32768))
        self.assertEqual(indexed.call_count, 1)
        self.assertGreater(result["metrics"]["question_investigation"]["calls"], 1)
        self.assertEqual(result["metrics"]["question_investigation"]["candidate_queries"], 1)
        self.assertLessEqual(result["metrics"]["request_bytes"][0], 32768)
        visible = {page["evidence_id"] for page in self.requests[0]["source_context"][0]["pages"]}
        for item in self.requests[0]["question_investigation"]["required_items"]:
            self.assertTrue(set(item["evidence_ids"]).issubset(visible))
        self.assertEqual(result["status"], "ANALYZED")

    def test_transmission_gap_does_not_reacquire_already_retrieved_source(self):
        self.calculation()
        import business_chat
        original_fit = business_chat._fit_request
        def omit_source(config, payload, history, policy=None, *, trim_events=None,
                        investigation_builder=None):
            self.assertTrue(payload["source_context"][0]["pages"])
            payload["source_context"][0]["pages"] = []
            return original_fit(config, payload, history, policy,
                trim_events=trim_events, investigation_builder=investigation_builder)
        with mock.patch.object(business_chat, "_fit_request", side_effect=omit_source):
            result = self.ask("FINAL-AMOUNT 怎么计算？", lambda payload:
                "已有公式候选，但本轮原文未供应，不能完整确认。")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["source_context"][0]["pages"], [])
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["stop_reason"], "evidence_incomplete")
        self.assertEqual(result["metrics"]["tool_calls"]["read"], 0)
        self.assertEqual(result["metrics"]["tool_calls"]["inspect_business_context"], 0)
        self.assertEqual(result["metrics"]["question_investigation"]["automatic_actions"], 0)
        investigation = result["investigation_state"]["question_investigation"]
        self.assertEqual(investigation["state"], "bounded_partial")
        self.assertIn("retrieved_source_not_visible", {gap["reason"] for gap in investigation["open_gaps"]})

    def test_material_not_in_pool_can_still_be_automatically_supplied(self):
        self.write("rule.cbl", "FINAL-RULE", "DISPLAY 'UNCHANGED'.\n" * 700 +
            "MOVE 10 TO BASIS.\nIF BASIS > ZERO\n"
            "COMPUTE FINAL-AMOUNT = BASIS * FACTOR\n"
            "ELSE\nMOVE ZERO TO FINAL-AMOUNT\nEND-IF.",
            "01 BASIS PIC 9(5).\n01 FACTOR PIC 9V99 VALUE 1.25.\n01 FINAL-AMOUNT PIC 9(7)V99.")
        self.build()
        import business_chat
        original_map = business_chat.build_business_map
        def limited_navigation(*args, **kwargs):
            return {**original_map(*args, **kwargs), "spotlights": [],
                    "rule_leads": [], "semantic_anchors": []}
        def respond(payload):
            if len(self.requests) == 1:
                return "需要先找公式和条件才能解释。"
            pages = payload["source_context"][0]["pages"]
            formula = next(page for page in pages if "COMPUTE FINAL-AMOUNT" in page["source_text"])
            self.assertTrue(any("IF BASIS > ZERO" in page["source_text"] for page in pages))
            self.assertTrue(any("MOVE ZERO TO FINAL-AMOUNT" in page["source_text"] for page in pages))
            return f"基础金额大于零时按基础金额乘系数计算；否则结果为零。[{formula['evidence_id']}]"
        with mock.patch.object(business_chat, "build_business_map", side_effect=limited_navigation):
            result = self.ask("rule.cbl 的金额怎么计算？", respond,
                policy=AgentPolicy(max_source_characters=4096,
                                   initial_pages=1, initial_source_characters=512))
        self.assertEqual(len(self.requests), 2)
        self.assertFalse(any("COMPUTE FINAL-AMOUNT" in page["source_text"]
                             for page in self.requests[0]["source_context"][0]["pages"]))
        self.assertTrue(any("COMPUTE FINAL-AMOUNT" in page["source_text"]
                            for page in self.requests[1]["source_context"][0]["pages"]))
        self.assertGreater(result["metrics"]["tool_calls"]["inspect_business_context"], 0)
        self.assertLessEqual(result["metrics"]["tool_calls"]["inspect_business_context"], 2)
        self.assertEqual(result["status"], "ANALYZED")


if __name__ == "__main__":
    unittest.main()
