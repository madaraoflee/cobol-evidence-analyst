"""Regressions for documented, unambiguous business investigation actions."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import _SYSTEM, _action_reply_invalid, _actions, run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from impact_results import impact_page
from repository_discovery import ensure_repository_search


class BusinessActionContractTests(unittest.TestCase):
    def parse(self, value):
        text = json.dumps(value, ensure_ascii=False)
        return text, _actions(text)

    def test_empty_optional_focus_preserves_other_requested_actions(self):
        requests = (
            {"search": ["TARGET-FLAG"]},
            {"read": [{"relative_path": "rule.cbl", "start_line": 1, "end_line": 20}]},
            {"list_impact": "TARGET-FLAG"},
        )
        for request in requests:
            with self.subTest(request=request):
                _, expected = self.parse(request)
                text, action = self.parse({**request, "focus": []})
                self.assertEqual(action, expected)
                self.assertFalse(_action_reply_invalid(text, action))

    def test_empty_focus_alone_never_becomes_a_business_answer(self):
        text, action = self.parse({"focus": []})
        self.assertTrue(_action_reply_invalid(text, action))
        self.assertFalse(action and any(action.values()))

    def test_impact_identifier_has_one_unambiguous_meaning_across_shapes(self):
        values = ("TARGET-FLAG", {"identifier": "TARGET-FLAG"}, ["TARGET-FLAG"])
        _, expected = self.parse({"list_impact": values[0]})
        for value in values:
            with self.subTest(value=value):
                text, action = self.parse({"list_impact": value})
                self.assertEqual(action, expected)
                self.assertEqual(action["list_impact"], "TARGET-FLAG")
                self.assertFalse(_action_reply_invalid(text, action))

    def test_concept_query_has_one_unambiguous_meaning_across_shapes(self):
        values = ("calculation rule", {"query": "calculation rule"}, ["calculation rule"])
        _, expected = self.parse({"search_concepts": values[0]})
        for value in values:
            with self.subTest(value=value):
                text, action = self.parse({"search_concepts": value})
                self.assertEqual(action, expected)
                self.assertEqual(action["search_concepts"], "calculation rule")
                self.assertFalse(_action_reply_invalid(text, action))

    def test_multiple_scalar_arguments_are_not_silently_reduced_to_one(self):
        for tool in ("list_impact", "search_concepts"):
            with self.subTest(tool=tool):
                text, action = self.parse({tool: ["FIRST-FLAG", "SECOND-FLAG"]})
                self.assertFalse(action and action.get(tool))
                self.assertTrue(_action_reply_invalid(text, action))

    def test_business_objects_and_labeled_examples_remain_answer_text(self):
        responses = (
            '{"result":"The flag controls the calculation.","search":["TARGET-FLAG"]}',
            '{"result":{"action":"Calculate the amount."}}',
            '例如：\n```json\n{"search":["TARGET-FLAG"]}\n```',
            'Example:\n{"list_impact":{"identifier":"TARGET-FLAG"}}',
        )
        for text in responses:
            with self.subTest(text=text):
                action = _actions(text)
                self.assertIsNone(action)
                self.assertFalse(_action_reply_invalid(text, action))

    def test_prompt_documents_an_executable_example_for_every_action(self):
        decoder = json.JSONDecoder()
        examples = []
        for match in re.finditer(r"\{", _SYSTEM):
            try:
                value, _ = decoder.raw_decode(_SYSTEM[match.start():])
            except ValueError:
                continue
            if isinstance(value, dict):
                examples.append(value)
        tools = ("search", "read", "framework_search", "inspect_business_context",
                 "list_impact", "search_concepts", "focus")
        for tool in tools:
            with self.subTest(tool=tool):
                candidates = [value for value in examples if tool in value]
                self.assertTrue(candidates, f"Missing concrete {tool} argument example in system prompt")
                actions = [_actions(json.dumps(value)) for value in candidates]
                self.assertTrue(any(action and action.get(tool) for action in actions),
                                f"No executable {tool} example in system prompt")
        inspections = [value["inspect_business_context"] for value in examples
                       if "inspect_business_context" in value]
        self.assertTrue(any(
            isinstance(location, dict)
            and isinstance(location.get("relative_path"), str)
            and type(location.get("line")) is int
            for inspection in inspections
            for location in (inspection if isinstance(inspection, list) else [inspection])),
            "Inspection example must document both relative_path and line")


class BusinessActionImpactIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.source = root / "source"
        self.source.mkdir()
        self.database = root / "index.sqlite"
        self.paths = {f"process-{index}.cbl" for index in range(3)}
        for index, path in enumerate(sorted(self.paths)):
            (self.source / path).write_text(
                f"IDENTIFICATION DIVISION.\nPROGRAM-ID. PROCESS-{index}.\n"
                "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
                "01 SHARED-RECORD PIC 9 VALUE 1.\n"
                "01 OUTPUT-RECORD PIC 9.\n"
                "PROCEDURE DIVISION.\nMAIN.\n"
                "MOVE SHARED-RECORD TO OUTPUT-RECORD.\nGOBACK.\n", encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
                                       api_key="offline-neutral-credential")
        self.requests = []
        self.expected_citations = {}

    def answer_from(self, payload):
        impact = payload["impact_result"]
        self.assertEqual(impact["total"], 3)
        self.assertEqual(impact["counts"], {"program": 3})
        self.assertEqual({row["relative_path"] for row in impact["rows"]}, self.paths)
        pages = [page for bundle in payload["source_context"] for page in bundle["pages"]]
        cited = {page["relative_path"]: page for page in pages
                 if "MOVE SHARED-RECORD TO OUTPUT-RECORD" in page["source_text"]}
        self.assertEqual(set(cited), self.paths)
        self.expected_citations = {page["evidence_id"]: path for path, page in cited.items()}
        return "索引中有 3 个程序引用该字段：\n" + "\n".join(
            f"- {path} 将 SHARED-RECORD 移到 OUTPUT-RECORD。[{page['evidence_id']}]"
            for path, page in sorted(cited.items())) + "\n改变该字段的值会影响这三个赋值结果。"

    def ask(self, first_reply, *, recovery=False):
        self.requests = []

        def transport(request):
            envelope = json.loads(request.body)
            self.assertNotIn("tools", envelope)
            payload = json.loads(envelope["messages"][-1]["content"])
            self.requests.append(payload)
            self.assertLessEqual(len(self.requests), 2, "A usable second answer needs no further request")
            if len(self.requests) == 1:
                text = first_reply
            else:
                if recovery:
                    self.assertIn("不要再次输出动作 JSON", payload["task"])
                    for budget in ("searches_per_turn", "reads_per_turn",
                                   "framework_searches_per_turn", "business_context_actions_per_turn"):
                        self.assertEqual(payload["investigation_budget"][budget], 0)
                text = self.answer_from(payload)
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": text}, "finish_reason": "stop"}]}, ensure_ascii=False))

        output = run_business_chat(
            "当前使用 SHARED-RECORD 的程序有哪些？全部列出，并说明修改字段值的影响。",
            self.database, self.source, self.config, transport=transport,
            framework_reference_path="", answer_detail="brief",
            policy=AgentPolicy(max_model_requests=4, max_answer_revisions=0))
        result = output["agent_result"]
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertTrue(result["model_answer_recorded"])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["metrics"]["model_requests"], 2)
        self.assertEqual({citation["evidence_id"]: citation["relative_path"]
                          for citation in result["narrative"]["citations"]}, self.expected_citations)
        impact = result["impact_result"]
        saved = impact_page(self.database, impact["handle"])
        self.assertEqual(saved["total"], 3)
        self.assertEqual({row["relative_path"] for row in saved["rows"]}, self.paths)
        return output

    def test_impact_argument_variants_and_empty_focus_reach_a_cited_answer(self):
        requests = (
            {"list_impact": "SHARED-RECORD"},
            {"list_impact": {"identifier": "SHARED-RECORD"}},
            {"list_impact": ["SHARED-RECORD"]},
            {"search": ["SHARED-RECORD"], "focus": []},
        )
        for request in requests:
            with self.subTest(request=request):
                output = self.ask(json.dumps(request))
                self.assertNotEqual(output["reason_code"], "INVALID_INVESTIGATION_ACTION")
                completed = self.requests[1]["completed_actions"]
                if "list_impact" in request:
                    self.assertTrue(any(action.get("list_impact") == "SHARED-RECORD"
                                        and action.get("total") == 3 for action in completed))
                else:
                    self.assertGreaterEqual(output["agent_result"]["metrics"]["tool_calls"]["search"], 1)

    def test_malformed_action_recovers_once_using_the_existing_sources(self):
        output = self.ask('{"list_impact": [', recovery=True)
        first_ids = {page["evidence_id"] for bundle in self.requests[0]["source_context"]
                     for page in bundle["pages"]}
        second_ids = {page["evidence_id"] for bundle in self.requests[1]["source_context"]
                      for page in bundle["pages"]}
        self.assertEqual(second_ids, first_ids)
        result = output["agent_result"]
        self.assertTrue(any(boundary.get("reason") == "investigation_action_recovery"
                            and boundary.get("next_step") == "answer_from_evidence"
                            for boundary in result["boundaries"]))
        trace = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text(encoding="utf-8"))
        self.assertEqual(trace["rounds"][0]["response"]["error"], "INVALID_INVESTIGATION_ACTION")
        self.assertIsNone(trace["rounds"][1]["response"]["error"])

    def test_malformed_continuation_preserves_the_cited_impact_draft(self):
        drafts = []

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            self.requests.append(payload)
            self.assertLessEqual(len(self.requests), 2)
            if len(self.requests) == 1:
                text = self.answer_from(payload) + "\n影响范围的边界如下："
                drafts.append(text)
                finish_reason = "length"
            else:
                self.assertTrue(payload["draft_continuation"])
                text, finish_reason = '{"list_impact": [', "stop"
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": text}, "finish_reason": finish_reason}]}, ensure_ascii=False))

        output = run_business_chat(
            "当前使用 SHARED-RECORD 的程序有哪些？全部列出，并说明修改字段值的影响。",
            self.database, self.source, self.config, transport=transport,
            framework_reference_path="", answer_detail="brief",
            policy=AgentPolicy(max_model_requests=4, max_answer_revisions=0))
        result = output["agent_result"]
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["answer"], drafts[0])
        self.assertTrue(result["model_answer_recorded"])
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(output["reason_code"], "INVALID_INVESTIGATION_ACTION")
        self.assertEqual({citation["evidence_id"]: citation["relative_path"]
                          for citation in result["narrative"]["citations"]}, self.expected_citations)
        self.assertTrue(any(boundary.get("reason") == "usable_draft_retained"
                            for boundary in result["boundaries"]))


if __name__ == "__main__":
    unittest.main()
