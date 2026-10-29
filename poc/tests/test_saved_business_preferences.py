"""Saved business content preferences affect evidence work, not output format."""
from __future__ import annotations

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
from answer_markdown import DEFAULT_ANSWER_INSTRUCTIONS
from answer_preferences import saved_business_explanation_requested
from business_chat import run_business_chat
from business_explanation_cache import business_explanation_cache
from business_index import build_business_index
from business_map import build_business_map
from business_synthesis import answer_requirements, assess_business_answer, build_analysis_brief, wants_business_overview
from company_api import CompanyAPIConfig, TransportResponse
from draft_validation import validate_answer_draft
from question_investigation import build_question_investigation
from question_planner import QuestionPlanner
from repository_discovery import ensure_repository_search


INSTRUCTIONS = ("解释结果码时必须说明业务后果和下游使用者；若仓库已有调用方，"
                "要讲清它收到结果后选择什么分支。不要只回答字段赋值或罗列程序名。格式不限。")
DECISION = """IDENTIFICATION DIVISION.
PROGRAM-ID. DECISION.
DATA DIVISION.
LINKAGE SECTION.
01 RESULT-CODE PIC 99.
PROCEDURE DIVISION USING RESULT-CODE.
MAIN.
MOVE 12 TO RESULT-CODE.
GOBACK.
"""
CALLER = """IDENTIFICATION DIVISION.
PROGRAM-ID. REQUESTFLOW.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 RESULT-CODE PIC 99.
01 ALLOW-PROCESS PIC X.
PROCEDURE DIVISION.
MAIN.
CALL 'DECISION' USING RESULT-CODE.
IF RESULT-CODE = 12
    MOVE 'N' TO ALLOW-PROCESS
ELSE
    MOVE 'Y' TO ALLOW-PROCESS
END-IF.
GOBACK.
"""


class SavedBusinessPreferenceTests(unittest.TestCase):
    def test_business_requests_are_distinct_from_presentation_and_negation(self):
        for instructions in (INSTRUCTIONS, DEFAULT_ANSWER_INSTRUCTIONS,
                             "Explain the business purpose, program responsibilities and data handoffs for a newcomer."):
            with self.subTest(instructions=instructions):
                self.assertTrue(saved_business_explanation_requested("DECISION 返回什么？", instructions))
        for instructions in ("", "只用栏目「业务结论」和「处理依据」，保持简短。",
                             "请使用两个标题：业务目的、数据流程。", "Use headings Business purpose and Data flow.",
                             "不要解释业务流程或上下游职责，只给当前值。", "无需介绍业务流程。",
                             "Do not clarify the business purpose.", "Never walk through the business process.",
                             "例如：“解释业务用途”只是栏目示意，不是本次分析要求。"):
            with self.subTest(instructions=instructions):
                self.assertFalse(saved_business_explanation_requested("DECISION 返回什么？", instructions))

    def test_current_narrow_requirements_and_bound_fields_win(self):
        for question in ("只说明 DECISION 的返回值。", "一句话回答 DECISION 返回什么。",
                         "只分析 decision.cbl。", "What is the value of RESULT-CODE?",
                         "请翻译这个字段的定义。", "返回码是多少？", "What is the return code?", "是否会调用 X？"):
            with self.subTest(question=question):
                self.assertFalse(saved_business_explanation_requested(question, INSTRUCTIONS))
        self.assertFalse(saved_business_explanation_requested("这个字段呢？", INSTRUCTIONS,
                                                              field_entities=["RESULT-CODE"]))
        self.assertFalse(saved_business_explanation_requested("解释这里", INSTRUCTIONS,
            source_scopes=[{"exclusive": True, "procedure_name": "MAIN"}]))

    def test_positive_business_demand_survives_neighboring_negation_and_formatting(self):
        for instructions in ("不要解释代码，请讲清业务目的、程序职责和数据交接。",
                             "不要解释代码而要讲清业务目的和数据交接。",
                             "请使用两个栏目，解释业务目的和数据交接。",
                             "Use two sections and explain business purpose and data handoffs.",
                             "Do not explain code, describe the business purpose and downstream consumers."):
            with self.subTest(instructions=instructions):
                self.assertTrue(saved_business_explanation_requested("DECISION 返回什么？", instructions))

    def test_saved_examples_never_become_source_entities_or_new_intents(self):
        mapping = {"rule_leads": [{"reads": ["EXAMPLE-FIELD"], "writes": []}],
                   "direct_paths": ["decision.cbl"], "selected_paths": ["decision.cbl"]}
        plan = QuestionPlanner().plan("DECISION 返回什么？", mapping,
            answer_instructions=INSTRUCTIONS + " 示例名称 EXAMPLE-FIELD，示例路径 absent.cbl。")
        self.assertIn("business_overview", plan.intents)
        self.assertEqual(plan.entities, ())
        self.assertEqual(plan.paths, ("decision.cbl",))
        self.assertNotIn("impact", plan.intents)


class SavedBusinessContentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        for name, text in (("decision.cbl", DECISION), ("caller.cbl", CALLER)):
            (self.source / name).write_text(text, encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", quiet=True, verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.question = "DECISION 返回什么？"
        self.mapping = build_business_map(self.database, self.source, self.question)
        self.pages = [{"relative_path": name, "source_text": text, "text": text,
                       "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
                       "start_line": 1, "end_line": len(text.splitlines()),
                       "evidence_id": "ev:test:" + name, "format_hint": "free"}
                      for name, text in (("decision.cbl", DECISION), ("caller.cbl", CALLER))]
        self.short_answer = "程序把 RESULT-CODE 设置为12并返回调用方。[ev:test:decision.cbl]"

    def investigate(self, instructions="", **options):
        return build_question_investigation(self.question, self.mapping, database_path=self.database,
            source_pages=self.pages, answer_instructions=instructions, **options)

    def test_saved_business_demand_nominates_source_backed_omission_without_headings(self):
        investigation = self.investigate(INSTRUCTIONS)
        self.assertIn("business_overview", investigation["analysis_plan"]["intents"])
        brief = build_analysis_brief(self.question, investigation, self.pages, [], 4096,
                                     answer_instructions=INSTRUCTIONS)
        self.assertIn("program_roles", {row["kind"] for row in brief["required_answer_aspects"]})
        review = assess_business_answer(self.question, self.short_answer, investigation, self.pages,
                                        answer_instructions=INSTRUCTIONS)
        self.assertEqual(review["status"], "incomplete")
        self.assertIn("program_roles", review["missing_aspects"])
        corrected = ("REQUESTFLOW 把是否准入的判定交给 DECISION；它通过结果参数收到12后，"
                     "把 ALLOW-PROCESS 设为N，走不允许后续处理的分支。"
                     "这里说明的是调用正常返回时的处理规则。[ev:test:caller.cbl][ev:test:decision.cbl]")
        accepted = assess_business_answer(self.question, corrected, investigation, self.pages,
                                          answer_instructions=INSTRUCTIONS)
        self.assertNotIn("program_roles", accepted["missing_aspects"])
        self.assertNotIn("#", corrected)
        draft = validate_answer_draft(self.question, self.short_answer, investigation, self.pages,
            source_format="free", append_source_facts=False, answer_instructions=INSTRUCTIONS)
        self.assertIn("program_roles", draft["assessment"]["missing_aspects"])

    def test_related_unread_source_uses_existing_business_read_plans(self):
        working_set = {"snapshot_id": self.mapping["snapshot_id"],
                       "candidate_paths": ["decision.cbl", "caller.cbl"]}
        plain = build_question_investigation(self.question, self.mapping, database_path=self.database,
                                             source_pages=self.pages[:1], working_set=working_set)
        requested = build_question_investigation(self.question, self.mapping, database_path=self.database,
            source_pages=self.pages[:1], working_set=working_set, answer_instructions=INSTRUCTIONS)
        self.assertFalse(plain["planned_actions"])
        self.assertTrue(any(action["tool"] == "read" and action["arguments"]["relative_path"] == "caller.cbl"
                            for action in requested["planned_actions"]))

    def test_changed_and_cleared_preferences_do_not_reuse_different_candidate_modes(self):
        self.question = "DECISION 怎么处理？"
        cache = {}
        first = self.investigate(candidate_cache=cache)
        second = self.investigate(INSTRUCTIONS, candidate_cache=cache)
        third = self.investigate(INSTRUCTIONS, candidate_cache=cache)
        cleared = self.investigate(candidate_cache=cache)
        self.assertNotIn("business_overview", first["analysis_plan"]["intents"])
        self.assertIn("business_overview", second["analysis_plan"]["intents"])
        self.assertFalse(second["candidate_cache"]["hit"])
        self.assertTrue(third["candidate_cache"]["hit"])
        self.assertFalse(cleared["candidate_cache"]["hit"])
        self.assertNotIn("business_overview", cleared["analysis_plan"]["intents"])
        with business_explanation_cache():
            plain = answer_requirements(self.question, second, self.pages)
            requested = answer_requirements(self.question, second, self.pages, answer_instructions=INSTRUCTIONS)
            cleared = answer_requirements(self.question, second, self.pages)
        self.assertEqual(plain, cleared)
        self.assertNotIn("program_roles", {row["kind"] for row in plain})
        self.assertIn("program_roles", {row["kind"] for row in requested})

    def test_presentation_only_instructions_do_not_expand_reads_or_require_business_sections(self):
        instructions = "只用两个栏目「业务结论」和「处理依据」，不要其他标题。"
        investigation = self.investigate(instructions)
        self.assertNotIn("business_overview", investigation["analysis_plan"]["intents"])
        self.assertFalse(investigation["planned_actions"])
        review = assess_business_answer(self.question, self.short_answer, investigation, self.pages,
                                        answer_instructions=instructions)
        self.assertNotIn("program_roles", review["required_aspects"])

    def test_actual_chat_repairs_obvious_business_omission_using_saved_requirements(self):
        requests, failures = [], []

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            try:
                pages = [page for bundle in payload["source_context"] for page in bundle["pages"]]
                decision = next(page for page in pages if page["relative_path"] == "decision.cbl")
                if len(requests) == 1:
                    content = "程序把 RESULT-CODE 设置为12并返回调用方。" + f"[{decision['evidence_id']}]"
                else:
                    caller = next(page for page in pages if page["relative_path"] == "caller.cbl"
                                  and "MOVE 'N' TO ALLOW-PROCESS" in page["source_text"])
                    content = ("REQUESTFLOW 把是否准入的判定交给 DECISION；调用正常返回12时，"
                               "调用方把 ALLOW-PROCESS 设为N，走不允许后续处理的分支。"
                               f"[{caller['evidence_id']}][{decision['evidence_id']}]")
            except Exception as error:
                failures.append(error)
                raise
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": content},
                                                                      "finish_reason": "stop"}]}, ensure_ascii=False))

        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")), \
                mock.patch.object(socket, "create_connection", side_effect=AssertionError("Network forbidden")):
            result = run_business_chat(self.question, self.database, self.source,
                CompanyAPIConfig("https://offline.example.invalid/v1", "neutral-model", api_key="offline-only"),
                transport=transport, allow_network=False, framework_reference_path="", answer_instructions=INSTRUCTIONS,
                policy=AgentPolicy(max_model_requests=3, max_answer_revisions=1))["agent_result"]
        if failures:
            raise failures[0]
        self.assertEqual(len(requests), 2)
        self.assertIn("program_roles", requests[1]["answer_review"]["missing_aspects"])
        self.assertIn("不允许后续处理", result["answer"])
        self.assertTrue(result["answer_available"])
        self.assertFalse(result["answer_completion"]["semantic_verification"] is True)


if __name__ == "__main__":
    unittest.main()
