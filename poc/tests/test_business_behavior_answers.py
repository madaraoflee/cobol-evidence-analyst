"""Offline behavior explanation coverage and bounded revision integration.

Fixed provider replies test material supply and obvious omission handling, not
the business understanding or answer quality of an actual language model.
"""

from __future__ import annotations

import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from business_synthesis import assess_business_answer, build_analysis_brief, needs_synthesis_review
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


BODY = "IF ELIGIBLE-FLAG = 'N'\nMOVE 'S' TO RESULT-STATUS\nGOBACK\nEND-IF.\nWRITE BILL-RECORD.\nGOBACK."
COMPLETE = "资格标志为 N 时，将结果状态设为 S 并返回，本次不写入账单；否则执行账单记录写入。"
GENERIC = "程序先校验资格，然后根据条件处理并更新记录。"


def material(body=BODY):
    return ({"required_items": [{"kind": "business_steps", "status": "SATISFIED",
                                "candidate_count": 3, "evidence_ids": ["ev:behavior"]}]},
            [{"evidence_id": "ev:behavior", "relative_path": "billing.cbl",
              "source_text": body, "start_line": 1, "end_line": len(body.splitlines()),
              "source_sha256": "synthetic-version"}])


class BehaviorOmissionTests(unittest.TestCase):
    def test_generic_flow_cannot_substitute_for_conditions_and_conditional_return(self):
        investigation, pages = material()
        result = assess_business_answer("解释出账逻辑", GENERIC, investigation, pages)
        self.assertIn("behavior_conditions", result["missing_aspects"])
        self.assertIn("behavior_exits", result["missing_aspects"])
        self.assertEqual(result["semantic_verification"], "unverified")

    def test_concrete_brief_answer_does_not_need_another_request(self):
        investigation, pages = material()
        for detail in ("brief", "detailed"):
            result = assess_business_answer("解释出账逻辑", COMPLETE, investigation, pages,
                                            answer_detail=detail)
            self.assertEqual(result["missing_aspects"], [])
            self.assertFalse(needs_synthesis_review("解释出账逻辑", COMPLETE, investigation,
                source_pages=pages, answer_detail=detail, assessment=result))

    def test_simple_unconditional_write_does_not_invent_an_exception(self):
        investigation, pages = material("WRITE BILL-RECORD.\nGOBACK.")
        result = assess_business_answer("解释出账逻辑", "本段写入账单记录。", investigation, pages)
        self.assertEqual(result["missing_aspects"], [])
        self.assertNotIn("behavior_exits", result["required_aspects"])
        self.assertNotIn("behavior_conditions", result["required_aspects"])

    def test_read_only_flow_does_not_require_a_record_change(self):
        investigation, pages = material("READ CUSTOMER-FILE.\nGOBACK.")
        answer = "系统读取客户文件的下一条记录，供调用方使用。"
        result = assess_business_answer("这个读取流程做什么？", answer, investigation, pages)
        self.assertEqual(result["missing_aspects"], [])
        self.assertNotIn("behavior_outcomes", result["required_aspects"])

    def test_natural_business_paraphrases_are_not_required_to_name_technical_flags(self):
        investigation, pages = material()
        for answer in ("取消的申请直接退回调用方；其余申请落账。",
                       "網上申請交給線上審批，櫃台申請交給櫃台審批。",
                       "Online applications go to online approval; branch applications go to branch approval."):
            with self.subTest(answer=answer):
                result = assess_business_answer("解释业务流程", answer, investigation, pages)
                self.assertEqual(result["missing_aspects"], [])
                # This is acceptance of a non-generic explanation, not proof
                # that these fixed replies are supported by this source.
                self.assertEqual(result["semantic_verification"], "unverified")

    def test_english_boilerplate_is_nominated_without_a_minimum_answer_length(self):
        investigation, pages = material()
        result = assess_business_answer("Explain the workflow",
            "The system checks eligibility and updates records according to rules.", investigation, pages)
        self.assertIn("behavior_conditions", result["missing_aspects"])

    def test_trimmed_material_removes_guide_and_omission_requirements(self):
        investigation, pages = material()
        before = build_analysis_brief("解释出账逻辑", investigation, pages, [], 8192)
        self.assertTrue(before["behavior_guide"]["observations"])
        after = build_analysis_brief("解释出账逻辑", investigation, [], [], 8192)
        self.assertNotIn("behavior_guide", after)
        self.assertEqual(after["required_answer_aspects"], [])

    def test_parameter_question_keeps_its_narrow_scope(self):
        investigation, pages = material()
        result = assess_business_answer("ELIGIBLE-FLAG 的字段类型是什么？", "单字符。", investigation, pages)
        self.assertEqual(result["required_aspects"], [])

    def test_reusing_same_assessment_does_not_scan_source_again(self):
        investigation, pages = material()
        result = assess_business_answer("解释出账逻辑", GENERIC, investigation, pages)
        with mock.patch("business_synthesis.assess_business_answer", side_effect=AssertionError("duplicate scan")):
            self.assertTrue(needs_synthesis_review("解释出账逻辑", GENERIC, investigation,
                                                   source_pages=pages, assessment=result))


class BehaviorAnswerIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        (self.source / "billing.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. BILL-RULE.\n"
            "ENVIRONMENT DIVISION.\nINPUT-OUTPUT SECTION.\nFILE-CONTROL.\n"
            "SELECT BILL-FILE ASSIGN TO 'BILLS'.\nDATA DIVISION.\nFILE SECTION.\n"
            "FD BILL-FILE.\n01 BILL-RECORD PIC X(20).\nWORKING-STORAGE SECTION.\n"
            "01 ELIGIBLE-FLAG PIC X VALUE 'N'.\n01 RESULT-STATUS PIC X.\n"
            "PROCEDURE DIVISION.\nMAIN.\n" + BODY + "\n", encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", quiet=True,
                             framework_reference_path="")
        ensure_repository_search(self.database, self.source)
        self.requests = []
        for owner, name in ((socket, "create_connection"), (socket.socket, "connect")):
            patcher = mock.patch.object(owner, name, side_effect=AssertionError("offline only"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def ask(self, replies, question="billing.cbl 的出账逻辑是什么？"):
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            self.requests.append(payload)
            pages = [page for group in payload["source_context"] for page in group["pages"]]
            supplied = {page["evidence_id"] for page in pages}
            guide = payload.get("business_analysis_brief", {}).get("behavior_guide", {})
            for observation in guide.get("observations", []):
                self.assertLessEqual(set(observation["supplied_reference_ids"]), supplied)
            text = replies[min(len(self.requests) - 1, len(replies) - 1)] + f"[{pages[0]['evidence_id']}]"
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text},
                "finish_reason": "stop"}]}, ensure_ascii=False))
        return run_business_chat(question, self.database, self.source,
            CompanyAPIConfig("https://offline.example.invalid/v1", "neutral-model", api_key="synthetic-only"),
            allow_network=False, transport=transport, framework_reference_path="",
            policy=AgentPolicy(max_model_requests=3))["agent_result"]

    def test_generic_answer_gets_one_bounded_revision_with_actual_behavior_material(self):
        result = self.ask([GENERIC, COMPLETE])
        self.assertEqual(len(self.requests), 2)
        guide = self.requests[0]["business_analysis_brief"]["behavior_guide"]
        self.assertIn("early_exit", {row["kind"] for row in guide["observations"]})
        self.assertIn("behavior_conditions", self.requests[1]["answer_review"]["missing_aspects"])
        self.assertIn(COMPLETE, result["answer"])

    def test_complete_first_answer_stays_one_request(self):
        result = self.ask([COMPLETE])
        self.assertEqual(len(self.requests), 1)
        self.assertIn(COMPLETE, result["answer"])

    def test_natural_why_question_enters_behavior_investigation(self):
        self.ask([COMPLETE], question="billing.cbl 为什么没有生成账单？")
        self.assertEqual(len(self.requests), 1)
        self.assertIn("business_steps", {row["kind"] for row in
            self.requests[0]["question_investigation"]["required_items"]})


if __name__ == "__main__":
    unittest.main()
