"""Saved writing requirements reach real request bodies without format gates."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from answer_markdown import DEFAULT_ANSWER_INSTRUCTIONS
import business_chat
from business_analysis import run_business_analysis
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search
from run_store import RunStore


class AnswerInstructionsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "facts.sqlite"
        (self.source / "decision.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. DECISION.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 RESULT-CODE PIC 99.\n"
            "PROCEDURE DIVISION.\nMAIN.\nMOVE 12 TO RESULT-CODE.\nGOBACK.\n", encoding="utf-8")
        (self.source / "helper.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. HELPER.\n"
            "PROCEDURE DIVISION.\nDISPLAY 'READY'.\nGOBACK.\n", encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only")
        self.instructions = '请使用我的栏目「业务结论」和「处理依据」。\n保留双引号 " 和换行；不要套用其他章节。'
        self.requests = []
        self.sent = []

    @staticmethod
    def reply(text, finish="stop"):
        return TransportResponse(200, json.dumps({"choices": [{"message": {
            "role": "assistant", "content": text}, "finish_reason": finish}]}, ensure_ascii=False))

    def transport(self, callback):
        def send(request):
            envelope = json.loads(request.body)
            self.requests.append(envelope)
            payload = json.loads(envelope["messages"][-1]["content"])
            self.sent.append(payload)
            return callback(payload, len(self.requests))
        return send

    def answer(self, payload):
        page = next(page for bundle in payload["source_context"] for page in bundle["pages"]
                    if page["relative_path"] == "decision.cbl")
        return f"业务结论：入口把结果码设置为12，再返回调用方。[{page['evidence_id']}]"

    def assert_saved_system(self, instructions=None):
        expected = self.instructions if instructions is None else instructions
        self.assertTrue(self.requests)
        for envelope, payload in zip(self.requests, self.sent):
            self.assertEqual(envelope["messages"][0]["role"], "system")
            self.assertEqual(envelope["messages"][0]["content"].count(expected), 1)
            self.assertIn("优先于内置默认写作格式", envelope["messages"][0]["content"])
            self.assertNotIn("answer_instructions", payload)
            self.assertNotIn("tools", envelope)
            self.assertNotIn("response_format", envelope)

    def ask(self, callback, **options):
        return business_chat.run_business_chat("DECISION 返回什么？", self.database, self.source, self.config,
            transport=self.transport(callback), framework_reference_path="", answer_instructions=self.instructions,
            policy=AgentPolicy(max_model_requests=3, max_answer_revisions=0), **options)

    def test_custom_format_is_applied_and_not_required_by_a_new_output_gate(self):
        output = self.ask(lambda payload, _: self.reply(self.answer(payload)))
        self.assert_saved_system()
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(output["agent_result"]["answer_available"])
        # An ordinary sourced paragraph still delivers even without every
        # default heading or custom heading; this feature adds no format gate.
        self.assertEqual(output["agent_result"]["answer"], self.answer(self.sent[0]))

    def test_saved_requirements_reach_followup_source_read_and_final_answer(self):
        def respond(payload, number):
            if number == 1:
                return self.reply(json.dumps({"read": [{"relative_path": "helper.cbl",
                                                       "start_line": 1, "end_line": 4}]}))
            return self.reply(self.answer(payload))
        output = self.ask(respond)
        self.assertEqual(len(self.requests), 2)
        self.assert_saved_system()
        self.assertTrue(output["agent_result"]["answer_available"])

    def test_saved_requirements_reach_continuation_request(self):
        def respond(payload, number):
            if number == 1:
                return self.reply("业务结论：程序先给结果码", "length")
            return self.reply(self.answer(payload))
        output = self.ask(respond)
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(self.sent[1]["draft_continuation"])
        self.assert_saved_system()
        self.assertTrue(output["agent_result"]["answer_available"])

    def test_compact_and_review_requests_keep_original_requirements_once(self):
        payload = {"question": "解释业务流程", "answer_instructions": self.instructions,
                   "budget_wire_compact": True, "answer_review": {"semantic_risk_candidates": [{"kind": "scope"}]},
                   "source_context": [{"pages": []}], "framework_references": [], "business_map": {}}
        messages, _ = business_chat._fit_request(self.config, payload, [], AgentPolicy())
        self.assertEqual(messages[0]["content"].count(self.instructions), 1)
        for label in ("Batch job", "Online function / Programs", "Tables", "Files"):
            self.assertIn(label, messages[0]["content"])
        self.assertNotIn("answer_instructions", json.loads(messages[-1]["content"]))

    def test_saved_requirements_reach_actual_answer_revision(self):
        (self.source / "lookup.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. LOOKUP.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 ACCOUNT-KEY PIC 9(8).\n01 CURRENT-BALANCE PIC 9(8).\n"
            "01 RESULT-CODE PIC XX.\n01 SQLCODE PIC S9(9).\nPROCEDURE DIVISION.\n"
            "EXEC SQL SELECT AVAILABLE_BALANCE INTO :CURRENT-BALANCE FROM ACCOUNT_BALANCES "
            "WHERE ACCOUNT_ID = :ACCOUNT-KEY END-EXEC.\n"
            "IF SQLCODE = ZERO MOVE '47' TO RESULT-CODE ELSE MOVE '63' TO RESULT-CODE END-IF.\n"
            "GOBACK.\n", encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        def respond(payload, number):
            page = next(page for bundle in payload["source_context"] for page in bundle["pages"]
                        if "END-EXEC" in page["source_text"])
            text = ("任何异常都一定返回 '63'。" if number == 1 else
                    "查询正常返回并且控制流到达 IF 时，SQLCODE 为零则设置 RESULT-CODE 为 '47'，"
                    "否则为 '63'。未到达判断的异常及非法输入的查询结果不能据此保证。")
            return self.reply(text + f"[{page['evidence_id']}]")
        result = business_chat.run_business_chat("LOOKUP 的查询怎样返回结果码？", self.database,
            self.source, self.config, answer_instructions=self.instructions, answer_detail="brief",
            transport=self.transport(respond), framework_reference_path="",
            policy=AgentPolicy(max_model_requests=2, max_answer_revisions=1))
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(self.sent[1]["answer_review"]["semantic_risk_candidates"])
        self.assert_saved_system()
        self.assertIn("查询正常返回", result["agent_result"]["answer"])

    def test_source_reading_analysis_receives_saved_requirements(self):
        def respond(payload, _):
            page = payload["source_pages"][0]
            return self.reply(f"业务结论：程序设置结果码12并返回。[{page['evidence_id']}]")
        output = run_business_analysis("DECISION 返回什么？", self.database, self.source, self.config,
            entry_program="DECISION", transport=self.transport(respond), framework_context={},
            answer_instructions=self.instructions)
        self.assert_saved_system()
        self.assertIn("12", output["agent_result"]["answer"])

    def test_changed_requirements_do_not_replay_saved_answer(self):
        original = self.ask(lambda payload, _: self.reply(self.answer(payload)))
        saved_id = original["agent_result"]["run_id"]
        journal = RunStore(self.root / "investigation-runs.sqlite")
        saved = journal.load(saved_id)
        # A recoverable run can retain an answer even when its final delivery
        # was interrupted. A new saved format must not reuse that draft.
        journal.checkpoint(saved_id, saved["checkpoint"], status="attention")
        self.requests.clear()
        self.sent.clear()
        self.instructions = "只用一段业务说明，保留来源。"
        result = self.ask(lambda payload, _: self.reply(self.answer(payload)))
        self.assert_saved_system()
        self.assertTrue(result["agent_result"]["answer_available"])
        self.assertNotEqual(result["agent_result"]["run_id"], saved_id)

    def test_default_template_classifies_objects_without_inventing_business_context(self):
        for label in ("Batch job", "Online function / Programs", "Tables", "Files"):
            self.assertIn(label, DEFAULT_ANSWER_INSTRUCTIONS)
        for distinction in ("区分数据库表与程序内部表", "区分业务数据文件与源码/COPY定义",
                            "没有在线入口依据", "当前资料未确认", "追问只展开"):
            self.assertIn(distinction, DEFAULT_ANSWER_INSTRUCTIONS)
        system = business_chat._system_for_payload({"question": "解释业务流程",
                                                   "answer_instructions": DEFAULT_ANSWER_INSTRUCTIONS})
        self.assertEqual(system.count(DEFAULT_ANSWER_INSTRUCTIONS), 1)
        custom = business_chat._system_for_payload({"question": "解释业务流程",
                                                   "answer_instructions": self.instructions})
        self.assertNotIn(DEFAULT_ANSWER_INSTRUCTIONS, custom)


if __name__ == "__main__":
    unittest.main()
