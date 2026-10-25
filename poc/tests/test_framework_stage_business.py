"""Offline business evidence contracts; scripted answers are not model acceptance."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from agent_policy import AgentPolicy
from business_chat import _fit_request, run_business_chat
from business_handoffs import FACT_GROUPS
from business_index import build_business_index
from business_orientation import build_business_orientation
from business_synthesis import wants_business_overview
from company_api import CompanyAPIConfig, TransportResponse
from framework_semantics import build_framework_facts
from repository_discovery import ensure_repository_search
from test_prompt_projection import expand_projection


QUESTION = "ENTRYJOB 的业务处理流程是什么？"
MANUAL = """# Record processing R1.0

## Processing stages

| Section | Meaning | Caution |
| --- | --- | --- |
| 3000-READ | Obtain the request to review. | Execution depends on the caller. |
| 2000-WRITE | Request the approved change. | Stage numbering does not establish execution order. |

## Database I/O

CALL XXXXIO USING XXXX-PARAMS

The operation is selected by XXXX-FUNCTION.

### Operations

| Function | Meaning | Caution |
| --- | --- | --- |
| ADVANCE | Request the following record. | Inspect the returned status. |
| STORE | Request an update. | A request is not a committed result. |
"""
SOURCE = """IDENTIFICATION DIVISION.
PROGRAM-ID. ENTRYJOB.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 ROWS-PARAMS.
  05 ROWS-FUNCTION PIC X(8).
  05 ROWS-STATUS PIC X(4).
01 APPROVED PIC X.
PROCEDURE DIVISION.
MAIN.
    PERFORM 3000-READ.
    IF APPROVED = 'Y'
        PERFORM 2000-WRITE
    END-IF.
    GOBACK.
3000-READ SECTION.
    MOVE 'ADVANCE' TO ROWS-FUNCTION.
    CALL 'ROWSIO' USING ROWS-PARAMS.
2000-WRITE SECTION.
    MOVE 'STORE' TO ROWS-FUNCTION.
    CALL 'ROWSIO' USING ROWS-PARAMS.
"""


def source_page(source_bytes):
    text = source_bytes.decode("utf-8")
    return {"relative_path": "entry.cbl", "program_name": "ENTRYJOB",
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "evidence_id": "ev-stage-source", "start_line": 1,
            "end_line": len(text.splitlines()), "source_text": text, "format_hint": "free"}


def scripted_answer(payload):
    """A fixed response exercises plumbing; it cannot measure model understanding."""
    pages = [page for group in payload["source_context"] for page in group["pages"]]
    refs = [page["evidence_id"] for page in pages]
    refs.extend(ref["reference_id"] for ref in payload.get("framework_references", []))
    return ("先进入 3000-READ 请求下一条待处理记录；APPROVED 为 Y 时才进入 2000-WRITE 请求更新。"
            "STORE 只是更新请求，不能据此确认已持久化；实际结果仍需检查返回状态及提交证据。"
            + "".join(f"[{identifier}]" for identifier in dict.fromkeys(refs)))


class FrameworkStageBusinessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.path = self.source / "entry.cbl"
        self.path.write_text(SOURCE, encoding="utf-8")
        self.manual = self.root / "manual.md"
        self.manual.write_text(MANUAL, encoding="utf-8")
        self.database = self.root / "index.sqlite"
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model",
                                       api_key="local-test-key")
        self.requests = []
        self.envelopes = []
        for patcher in (
            mock.patch.dict(os.environ, {"FRAMEWORK_REFERENCE_PATH": ""}),
            mock.patch("socket.create_connection", side_effect=AssertionError("Offline test forbids network")),
            mock.patch.object(socket.socket, "connect", side_effect=AssertionError("Offline test forbids network")),
            mock.patch.object(socket.socket, "connect_ex", side_effect=AssertionError("Offline test forbids network")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.build()

    def build(self):
        report = build_business_index(self.source, self.database, source_format="free",
                                     verify_content=True, quiet=True, framework_reference_path=self.manual)
        ensure_repository_search(self.database, self.source)
        return report

    def material(self):
        pages = [source_page(self.path.read_bytes())]
        framework = build_framework_facts(self.database, pages, reference_path=self.manual)
        return pages, framework

    def project(self, pages=None, framework=None, references=None):
        if pages is None or framework is None:
            current_pages, current_framework = self.material()
            pages = current_pages if pages is None else pages
            framework = current_framework if framework is None else framework
        return build_business_orientation(QUESTION, {}, pages,
            framework_facts=framework["facts"],
            framework_references=framework["references"] if references is None else references)

    @staticmethod
    def facts(orientation):
        return {row["orientation_id"]: row for group in FACT_GROUPS for row in orientation[group]}

    @staticmethod
    def stage_edges(orientation):
        return [row for row in orientation["handoffs"]
                if row["relation"] == "requests_documented_framework_stage"]

    def request_orientation(self, payload):
        brief = expand_projection(payload).get("business_analysis_brief", {})
        self.assertIn("business_orientation", brief)
        return brief["business_orientation"]

    def assert_stage_business_evidence(self, orientation):
        facts = self.facts(orientation)
        stages = self.stage_edges(orientation)
        self.assertEqual({row["to"]["name"] for row in stages}, {"3000-READ", "2000-WRITE"})
        for edge in stages:
            self.assertEqual(edge["from"]["kind"], "perform_site")
            self.assertEqual(edge["to"]["kind"], "documented_stage")
            self.assertTrue(edge["stage_role"]["meaning"])
            self.assertTrue(edge["stage_role"]["caveat"])
        perform_lines = {}
        for edge in stages:
            origins = [facts[key] for key in edge["source_fact_ids"]]
            perform = [row for row in origins if row.get("kind") == "procedure_perform"]
            self.assertEqual(len(perform), 1, origins)
            perform_lines[edge["to"]["name"]] = perform[0]["source_locations"][0]["start_line"]
        self.assertLess(perform_lines["3000-READ"], perform_lines["2000-WRITE"])
        write = next(row for row in stages if row["to"]["name"] == "2000-WRITE")
        guards = [guard for key in write["conditions_from_fact_ids"] for guard in facts[key].get("guards", [])]
        self.assertTrue(any(guard["condition"] == "APPROVED = 'Y'" for guard in guards), guards)
        contained = [row for row in orientation["handoffs"]
                     if row["relation"] == "framework_stage_contains_operation_request"]
        self.assertEqual({(row["from"]["name"], row["to"]["name"]) for row in contained},
                         {("3000-READ", "ADVANCE"), ("2000-WRITE", "STORE")})
        store = next(row for row in contained if row["to"]["name"] == "STORE")
        self.assertEqual(store["stage_role"]["meaning"], "Request the approved change.")
        self.assertEqual(store["operation"]["caveat"], "A request is not a committed result.")
        for edge in stages + contained:
            self.assertFalse(edge["runtime_verified"])
            self.assertTrue(edge["limits"])
            self.assertTrue(edge["source_fact_ids"])
            self.assertLessEqual(set(edge["source_fact_ids"]), facts.keys())
            self.assertLessEqual(set(edge["conditions_from_fact_ids"]), set(edge["source_fact_ids"]))
            expected_refs = {ref for key in edge["source_fact_ids"] for ref in facts[key]["supplied_reference_ids"]}
            self.assertEqual(set(edge["supplied_reference_ids"]), expected_refs)
            self.assertTrue(any(ref.startswith("fw:") for ref in expected_refs))
            self.assertTrue(any(not ref.startswith("fw:") for ref in expected_refs))
        self.assertFalse(orientation["scope"]["semantic_execution_verified"])

    def ask(self, question=QUESTION, *, history=None):
        def transport(request):
            envelope = json.loads(request.body)
            payload = json.loads(envelope["messages"][-1]["content"])
            self.requests.append(payload)
            self.envelopes.append(envelope)
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": scripted_answer(payload)}, "finish_reason": "stop"}]},
                ensure_ascii=False))
        return run_business_chat(question, self.database, self.source, self.config,
            transport=transport, framework_reference_path=self.manual, history=history,
            policy=AgentPolicy(max_model_requests=1))["agent_result"]

    def test_real_index_binds_stage_role_to_perform_and_contained_operation(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                self.path.write_bytes(SOURCE.replace("\n", newline).encode("utf-8"))
                self.build()
                pages, framework = self.material()
                self.assertEqual(pages[0]["source_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
                self.assert_stage_business_evidence(self.project(pages, framework))

    def test_natural_business_flow_questions_keep_focused_questions_narrow(self):
        for question in (QUESTION, "ENTRYJOB 的業務處理流程是甚麼？", "请从业务角度说明 ENTRYJOB 流程"):
            with self.subTest(question=question):
                self.assertTrue(wants_business_overview(question))
        for question in ("只说明 ENTRYJOB 的业务目的", "ENTRYJOB 哪个字段返回错误？"):
            with self.subTest(question=question):
                self.assertFalse(wants_business_overview(question))

    def test_default_chat_receives_business_stage_chain_from_actual_source_and_manual(self):
        result = self.ask()
        self.assertEqual(len(self.requests), 1)
        orientation = self.request_orientation(self.requests[0])
        self.assert_stage_business_evidence(orientation)
        self.assertTrue(result["framework_context"]["facts"])
        self.assertTrue(any(citation["kind"] == "framework_reference"
                            for citation in result["narrative"]["citations"]))
        self.assertEqual(result["metrics"]["model_requests"], 1)

    def test_evaluate_stage_request_keeps_subject_and_selected_when_condition(self):
        changed = SOURCE.replace("    IF APPROVED = 'Y'\n        PERFORM 2000-WRITE\n    END-IF.",
            "    EVALUATE APPROVED\n        WHEN 'Y'\n            PERFORM 2000-WRITE\n"
            "        WHEN OTHER\n            CONTINUE\n    END-EVALUATE.")
        self.path.write_text(changed, encoding="utf-8")
        self.build()
        orientation = self.project()
        writes = [row for row in self.stage_edges(orientation) if row["to"]["name"] == "2000-WRITE"]
        self.assertEqual(len(writes), 1)
        write = writes[0]
        facts = self.facts(orientation)
        controlled = [facts[key] for key in write["conditions_from_fact_ids"]]
        guards = [guard for fact in controlled for guard in fact.get("guards", [])]
        self.assertTrue(any(guard.get("selector") == "APPROVED" and guard.get("condition") == "'Y'"
                            for guard in guards), guards)
        self.assertFalse(any(guard.get("condition") == "OTHER" for guard in guards), guards)
        for fact in controlled:
            for guard in fact["guards"]:
                self.assertTrue(any(span["start_line"] <= guard["start_line"]
                    and span["end_line"] >= guard["end_line"] for span in fact["source_locations"]))
        self.assertFalse(write["runtime_verified"])

    def test_missing_or_trimmed_manual_or_source_withdraws_documented_stage_edges(self):
        pages, framework = self.material()
        self.assert_stage_business_evidence(self.project(pages, framework))
        for supplied, references in (([], framework["references"]), (pages, []),
                (pages, [{**row, "text_truncated": True} for row in framework["references"]])):
            with self.subTest(source=bool(supplied), reference=bool(references)):
                projected = self.project(supplied, framework, references)
                self.assertFalse(self.stage_edges(projected))
                self.assertFalse([row for row in projected["handoffs"]
                    if row["relation"] == "framework_stage_contains_operation_request"])

    def test_actual_request_fit_rebuilds_after_stage_evidence_is_removed(self):
        self.ask()
        original = expand_projection(self.requests[0])
        self.assert_stage_business_evidence(self.request_orientation(original))
        fitted_messages, fitted_bytes = _fit_request(self.config, deepcopy(original), [])
        self.assertLessEqual(fitted_bytes, AgentPolicy().max_request_bytes)
        fitted_payload = json.loads(fitted_messages[-1]["content"])
        self.assert_stage_business_evidence(self.request_orientation(fitted_payload))
        for missing in ("source", "manual"):
            with self.subTest(missing=missing):
                payload = deepcopy(original)
                if missing == "source":
                    payload["source_context"][0]["pages"] = []
                else:
                    payload["framework_references"] = []
                messages, request_bytes = _fit_request(self.config, payload, [])
                self.assertLessEqual(request_bytes, AgentPolicy().max_request_bytes)
                actual = expand_projection(json.loads(messages[-1]["content"]))
                orientation = actual.get("business_analysis_brief", {}).get("business_orientation", {})
                self.assertFalse([row for row in orientation.get("handoffs", [])
                    if row["relation"] in {"requests_documented_framework_stage",
                                          "framework_stage_contains_operation_request"}])

    def test_changed_source_does_not_reuse_old_stage_binding(self):
        old_pages, old_framework = self.material()
        self.assert_stage_business_evidence(self.project(old_pages, old_framework))
        changed = SOURCE.replace("2000-WRITE", "2500-REVIEW")
        self.path.write_text(changed, encoding="utf-8")
        self.build()
        current_pages, current_framework = self.material()
        self.assertFalse(self.stage_edges(self.project(current_pages, old_framework)))
        current = self.project(current_pages, current_framework)
        self.assertEqual({row["to"]["name"] for row in self.stage_edges(current)}, {"3000-READ"})
        self.assertFalse(any(row["relation"] == "framework_stage_contains_operation_request"
                             and row["to"]["name"] == "STORE" for row in current["handoffs"]))

    def test_changed_manual_requires_current_role_and_reference_evidence(self):
        pages, old = self.material()
        self.assert_stage_business_evidence(self.project(pages, old))
        self.manual.write_text(MANUAL.replace("R1.0", "R1.1").replace(
            "Request the approved change.", "Request the approved change for later review."), encoding="utf-8")
        self.build()
        pages, current = self.material()
        self.assertFalse(self.stage_edges(self.project(pages, old, current["references"])))
        write = next(row for row in self.stage_edges(self.project(pages, current))
                     if row["to"]["name"] == "2000-WRITE")
        self.assertEqual(write["stage_role"]["meaning"], "Request the approved change for later review.")
        self.assertTrue(set(write["supplied_reference_ids"]) &
                        {row["reference_id"] for row in current["references"]})
        self.assertFalse(set(write["supplied_reference_ids"]) &
                         {row["reference_id"] for row in old["references"]})

    def test_related_followup_keeps_current_stage_conditions_and_operation_caveat(self):
        first = self.ask()
        history = [{"role": "user", "content": QUESTION}, {"role": "assistant",
            "content": first["answer"], "evidence_refs": first["evidence_refs"]}]
        second = self.ask("那这个业务处理流程，哪些情况下才进入更新阶段？", history=history)
        self.assertEqual(len(self.requests), 2)
        self.assert_stage_business_evidence(self.request_orientation(self.requests[-1]))
        self.assertEqual(self.envelopes[-1]["messages"][1:3],
                         [{"role": row["role"], "content": row["content"]} for row in history])
        self.assertEqual(second["metrics"]["history_messages"], 2)
        self.assertEqual(second["metrics"]["model_requests"], 1)


if __name__ == "__main__":
    unittest.main()
