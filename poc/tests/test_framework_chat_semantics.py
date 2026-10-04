from __future__ import annotations

import copy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import _fit_request, _request_manifest, run_business_chat
from business_index import build_business_index
from business_map import build_business_map
from company_api import CompanyAPIConfig, TransportResponse
from question_investigation import build_question_investigation
from repository_discovery import ensure_repository_search


class FrameworkChatSemanticsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="local-test-key")
        self.reference = {"reference_id": "fw:operation", "heading": "Record operation", "page": None,
            "text": "FETCHROW retrieves the row matching the supplied key.", "text_truncated": False,
            "start_line": 1, "end_line": 1, "selection_reason": "source_marker"}

    def build(self, *, formula=True, dynamic=False, implementation=False, two_calls=False):
        target = "TARGET-NAME" if dynamic else "'RECORD-SERVICE'"
        body = "MOVE 'FETCHROW' TO ACTION-CODE.\nCALL " + target + " USING ACTION-CODE REQUEST-ROW.\n"
        if two_calls:
            body += "CALL 'RECORD-SERVICE' USING ACTION-CODE REQUEST-ROW.\n"
        if formula:
            body += "COMPUTE RESULT-AMOUNT = 10 * 2.\n"
        source = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. ORDER-RULE.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 ACTION-CODE PIC X(12).\n01 REQUEST-ROW PIC X(20).\n"
            "01 TARGET-NAME PIC X(20).\n01 RESULT-AMOUNT PIC 9(7).\n"
            "PROCEDURE DIVISION.\nMAIN.\n" + body + "GOBACK.\n")
        (self.source / "rule.cbl").write_text(source, encoding="utf-8")
        if implementation:
            (self.source / "service.cbl").write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. RECORD-SERVICE.\n"
                "PROCEDURE DIVISION.\nMAIN.\nDISPLAY 'AVAILABLE'.\nGOBACK.\n", encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.page = {"relative_path": "rule.cbl", "program_name": "ORDER-RULE",
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(), "evidence_id": "ev:caller",
            "start_line": 1, "end_line": len(source.splitlines()), "source_text": source,
            "selection_reasons": ["question_match"]}
        with closing(sqlite3.connect(self.database)) as db:
            first, last = db.execute("SELECT e.start_line,e.end_line FROM relations r "
                "JOIN evidence_spans e USING(evidence_id) WHERE r.relative_path='rule.cbl' "
                "AND r.relation_type IN ('CALLS','CALL_TARGET_FROM') ORDER BY e.start_line LIMIT 1").fetchone()
        self.fact = {"fact_id": "operation:fetch", "kind": "framework_operation", "relative_path": "rule.cbl",
            "source_sha256": self.page["source_sha256"], "program_name": "ORDER-RULE", "start_line": first,
            "end_line": last, "target_name": "TARGET-NAME" if dynamic else "RECORD-SERVICE", "relation_type": "CALLS",
            "operation": {"value": "FETCHROW", "meaning": "Retrieve a row by its key.", "caveat": "Returned data is unknown."},
            "function_field": "ACTION-CODE", "argument": "REQUEST-ROW",
            "source_ranges": [{"start_line": first, "end_line": last, "role": "callsite"},
                              {"start_line": first - 1, "end_line": first - 1, "role": "operation_value"}],
            "reference_ids": [self.reference["reference_id"]], "source_evidence_ids": [self.page["evidence_id"]],
            "interpretation_basis": "documented_framework_rule", "dependency_covered": True,
            "runtime_verified": False}
        self.question = "ORDER-RULE 的处理流程是什么？"
        self.business_map = build_business_map(self.database, self.source, self.question)

    def investigate(self, pages=None, *, framework_facts=(), question=None):
        return build_question_investigation(question or self.question, self.business_map,
            database_path=self.database, source_pages=[self.page] if pages is None else pages,
            framework_facts=framework_facts)

    @staticmethod
    def item(result, kind):
        return next(item for item in result["required_items"] if item["kind"] == kind)

    def payload(self):
        return {"question": self.question, "repository": {}, "business_map": {},
            "source_context": [{"pages": [self.page], "call_chain": {"links": [], "omitted_links": 0},
                                "outline": [], "notices": []}],
            "framework_references": [copy.deepcopy(self.reference)], "framework_facts": [copy.deepcopy(self.fact)]}

    def test_documented_operation_resolves_only_matching_external_dependency(self):
        self.build()
        before = self.item(self.investigate(), "dependencies")
        after = self.item(self.investigate(framework_facts=[self.fact]), "dependencies")
        self.assertEqual(before["reason"], "external_implementation_unavailable")
        self.assertEqual(after["status"], "SATISFIED")
        self.assertEqual(after["reason"], "documented_framework_rule")
        self.assertEqual(after["framework_fact_ids"], [self.fact["fact_id"]])

    def test_another_call_to_same_target_retains_its_missing_implementation(self):
        self.build(two_calls=True)
        dependency = self.item(self.investigate(framework_facts=[self.fact]), "dependencies")
        self.assertEqual(dependency["status"], "UNRESOLVED")
        self.assertEqual(dependency["reason"], "external_implementation_unavailable")

    def test_dynamic_target_cannot_be_resolved_by_operation_fact(self):
        self.build(dynamic=True)
        dependency = self.item(self.investigate(framework_facts=[self.fact]), "dependencies")
        self.assertEqual(dependency["status"], "UNRESOLVED")
        self.assertEqual(dependency["reason"], "runtime_target_unresolved")

    def test_available_implementation_remains_the_source_of_behavior(self):
        self.build(implementation=True)
        dependency = self.item(self.investigate(framework_facts=[self.fact]), "dependencies")
        self.assertNotIn("framework_fact_ids", dependency)
        self.assertNotEqual(dependency["reason"], "documented_framework_rule")

    def test_resolving_framework_call_does_not_supply_a_missing_formula(self):
        self.build(formula=False)
        result = self.investigate(framework_facts=[self.fact], question="ORDER-RULE RESULT-AMOUNT 怎么计算？")
        self.assertEqual(self.item(result, "dependencies")["reason"], "documented_framework_rule")
        formula = self.item(result, "formula")
        self.assertEqual(formula["reason"], "formula_not_located")
        self.assertEqual(formula["status"], "OPEN")

    def test_final_request_rechecks_fact_source_and_reference_visibility(self):
        self.build()
        for absent in ("reference", "source", "truncated_reference"):
            with self.subTest(absent=absent):
                payload = self.payload()
                if absent == "reference":
                    payload["framework_references"] = []
                elif absent == "source":
                    payload["source_context"][0]["pages"] = []
                else:
                    payload["framework_references"][0]["text_truncated"] = True
                messages, _ = _fit_request(self.config, payload, [], investigation_builder=self.investigate)
                actual = json.loads(messages[-1]["content"])
                self.assertEqual(actual["framework_facts"], [])
                self.assertEqual(self.item(actual["question_investigation"], "dependencies")["reason"],
                                 "external_implementation_unavailable")

    def test_call_visible_without_operation_assignment_does_not_resolve_dependency(self):
        self.build()
        payload = self.payload()
        first, last = self.fact["start_line"], self.fact["end_line"]
        payload["source_context"][0]["pages"] = [{**self.page, "start_line": first, "end_line": last,
            "source_text": "\n".join(self.page["source_text"].splitlines()[first - 1:last])}]
        messages, _ = _fit_request(self.config, payload, [], investigation_builder=self.investigate)
        actual = json.loads(messages[-1]["content"])
        self.assertEqual(actual["framework_facts"], [])
        dependency = self.item(actual["question_investigation"], "dependencies")
        self.assertTrue(dependency["evidence_ids"])
        self.assertEqual(dependency["reason"], "external_implementation_unavailable")

    def test_candidate_cache_retains_unresolved_state_when_fact_is_no_longer_visible(self):
        self.build()
        cache = {}
        arguments = {"database_path": self.database, "source_pages": [self.page], "candidate_cache": cache}
        first = build_question_investigation(self.question, self.business_map,
            framework_facts=[self.fact], **arguments)
        second = build_question_investigation(self.question, self.business_map, framework_facts=[], **arguments)
        self.assertEqual(self.item(first, "dependencies")["reason"], "documented_framework_rule")
        self.assertTrue(second["candidate_cache"]["hit"])
        self.assertEqual(self.item(second, "dependencies")["reason"], "external_implementation_unavailable")

    def test_request_budget_can_drop_fact_and_restore_dependency_gap(self):
        self.build()
        payload = self.payload()
        payload["framework_facts"][0]["operation"]["caveat"] = "Declared boundary. " * 3000
        trims = []
        messages, size = _fit_request(self.config, payload, [], AgentPolicy(max_request_bytes=32768),
            trim_events=trims, investigation_builder=self.investigate)
        actual = json.loads(messages[-1]["content"])
        self.assertLessEqual(size, 32768)
        self.assertEqual(actual["framework_facts"], [])
        self.assertTrue(actual["source_context"][0]["pages"])
        self.assertEqual(self.item(actual["question_investigation"], "dependencies")["reason"],
                         "external_implementation_unavailable")
        self.assertTrue(any(row["reason"] == "framework_fact_request_bytes" for row in trims))

    def test_manifest_captures_immutable_facts_for_retained_answer(self):
        self.build()
        payload = self.payload()
        manifest = _request_manifest(payload)
        payload["framework_facts"][0]["operation"]["meaning"] = "Later interpretation"
        payload["framework_facts"].clear()
        self.assertEqual(manifest["framework_facts"][0]["operation"]["meaning"], "Retrieve a row by its key.")

    def test_chat_sends_compiled_fact_and_keeps_source_and_manual_citations(self):
        self.build()
        requests = []

        def compile_facts(database, pages, **options):
            return {"facts": [copy.deepcopy(self.fact)], "references": [copy.deepcopy(self.reference)],
                    "status": "MATCHED", "compiler_version": "test"}

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            fact = payload["framework_facts"][0]
            text = "按提供的键读取记录，然后将结果金额设为二十。" + "".join(
                f"[{identifier}]" for identifier in fact["source_evidence_ids"] + fact["reference_ids"])
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": text}, "finish_reason": "stop"}]}, ensure_ascii=False))

        with mock.patch("business_chat.build_framework_facts", side_effect=compile_facts):
            result = run_business_chat(self.question, self.database, self.source, self.config,
                transport=transport, framework_reference_path="", policy=AgentPolicy(max_model_requests=1))["agent_result"]
        self.assertEqual(len(requests), 1)
        dependency = self.item(requests[0]["question_investigation"], "dependencies")
        self.assertEqual(dependency["reason"], "documented_framework_rule")
        self.assertTrue(result["framework_context"]["facts"])
        self.assertIn("[fw:operation]", result["answer"])
        self.assertFalse(any(row.get("reason") == "unsupported_citations" for row in result["boundaries"]))

    def documented_index(self, operation="ADVANCE", *, filler_lines=0):
        manual = self.root / "manual.md"
        manual.write_text("# Record processing\n\n## Database I/O\n\n"
            "CALL XXXXIO USING XXXX-PARAMS\n\nThe operation is selected by XXXX-FUNCTION.\n\n"
            "### Operations\n\n| Function | Meaning | Caution |\n| --- | --- | --- |\n"
            "| ADVANCE | Request the following record. | Inspect the returned status. |\n"
            "| STORE | Request an update. | A request is not a committed result. |\n", encoding="utf-8")
        (self.source / "rule.cbl").write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. ORDER-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 ROWS-PARAMS.\n"
            "  05 ROWS-FUNCTION PIC X(8).\n  05 ROWS-STATUS PIC X(4).\n"
            "PROCEDURE DIVISION.\nREAD-STAGE SECTION.\n"
            + "DISPLAY 'UNRELATED FILLER STATEMENT'.\n" * filler_lines +
            f"MOVE '{operation}' TO ROWS-FUNCTION.\nCALL 'ROWSIO' USING ROWS-PARAMS.\n"
            "IF ROWS-STATUS = 'DONE' CONTINUE END-IF.\nGOBACK.\n", encoding="utf-8")
        report = build_business_index(self.source, self.database, source_format="free",
            verify_content=True, framework_reference_path=manual, quiet=True)
        ensure_repository_search(self.database, self.source)
        return manual, report

    def test_real_offline_index_and_compiler_feed_documented_operation_to_chat(self):
        manual, report = self.documented_index()
        self.assertGreater(report["framework_semantics"]["fact_count"], 0)
        requests = []

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            fact = next(fact for fact in payload["framework_facts"] if fact["kind"] == "framework_operation")
            self.assertEqual(fact["operation"]["value"], "ADVANCE")
            self.assertTrue(fact["dependency_covered"])
            self.assertFalse(fact["runtime_verified"])
            self.assertEqual(self.item(payload["question_investigation"], "dependencies")["status"], "SATISFIED")
            source_ids = {page["evidence_id"] for page in payload["source_context"][0]["pages"]}
            reference_ids = {ref["reference_id"] for ref in payload["framework_references"]}
            self.assertLessEqual(set(fact["source_evidence_ids"]), source_ids)
            self.assertLessEqual(set(fact["reference_ids"]), reference_ids)
            answer = "先请求下一条记录，再检查返回状态。" + "".join(
                f"[{identifier}]" for identifier in fact["source_evidence_ids"] + fact["reference_ids"])
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": answer}, "finish_reason": "stop"}]}, ensure_ascii=False))

        result = run_business_chat("ORDER-RULE 的处理流程是什么？", self.database, self.source, self.config,
            transport=transport, framework_reference_path=manual,
            policy=AgentPolicy(max_model_requests=1))["agent_result"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["framework_context"]["facts"], requests[0]["framework_facts"])
        self.assertTrue(any(citation["kind"] == "framework_reference" for citation in result["narrative"]["citations"]))

    def test_real_unknown_operation_keeps_external_dependency_gap(self):
        manual, report = self.documented_index(operation="UNLISTED")
        requests = []

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            self.assertFalse(any(fact.get("dependency_covered") for fact in payload["framework_facts"]))
            dependency = self.item(payload["question_investigation"], "dependencies")
            self.assertEqual(dependency["status"], "UNRESOLVED")
            self.assertEqual(dependency["reason"], "external_implementation_unavailable")
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": "先设置操作值，再调用记录服务并检查返回状态。"}, "finish_reason": "stop"}]}))

        result = run_business_chat("ORDER-RULE 的处理流程是什么？", self.database, self.source, self.config,
            transport=transport, framework_reference_path=manual,
            policy=AgentPolicy(max_model_requests=1))["agent_result"]
        self.assertEqual(len(requests), 1)
        self.assertFalse(any(fact.get("dependency_covered") for fact in result["framework_context"]["facts"]))

    def test_natural_term_question_reads_missing_layout_within_existing_budget(self):
        manual, report = self.documented_index(filler_lines=1800)
        self.assertGreater(report["framework_semantics"]["fact_count"], 0)
        requests = []

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            facts = [fact for fact in payload["framework_facts"] if fact.get("dependency_covered")]
            self.assertEqual(len(facts), 1)
            self.assertEqual(facts[0]["operation"]["value"], "ADVANCE")
            dependency = self.item(payload["question_investigation"], "dependencies")
            self.assertEqual(dependency["reason"], "documented_framework_rule")
            self.assertTrue(any("01 ROWS-PARAMS." in page["source_text"]
                                for page in payload["source_context"][0]["pages"]))
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": "先请求下一条记录，再检查返回状态。"}, "finish_reason": "stop"}]}))

        result = run_business_chat("ADVANCE 如何处理？", self.database, self.source, self.config,
            transport=transport, framework_reference_path=manual,
            policy=AgentPolicy(max_model_requests=1, max_reads_per_turn=1))["agent_result"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["metrics"]["tool_calls"]["read"], 1)
        self.assertTrue(any(action.get("reason") == "framework_layout_source_not_supplied"
                            for action in result["investigation_state"]["completed_actions"]))

    def test_missing_layout_does_not_expand_disabled_read_budget(self):
        manual, _ = self.documented_index(filler_lines=1800)
        requests = []

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            self.assertFalse(any(fact.get("dependency_covered") for fact in payload["framework_facts"]))
            self.assertEqual(self.item(payload["question_investigation"], "dependencies")["reason"],
                             "external_implementation_unavailable")
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": "先设置操作值，再调用记录服务并检查状态。"}, "finish_reason": "stop"}]}))

        result = run_business_chat("ADVANCE 如何处理？", self.database, self.source, self.config,
            transport=transport, framework_reference_path=manual,
            policy=AgentPolicy(max_model_requests=1, max_reads_per_turn=0))["agent_result"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["metrics"]["tool_calls"]["read"], 0)


if __name__ == "__main__":
    unittest.main()
