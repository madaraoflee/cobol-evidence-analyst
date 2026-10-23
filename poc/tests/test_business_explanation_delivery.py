"""Offline delivery checks; scripted replies are not real-model quality evidence."""

from copy import deepcopy
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "poc"))
sys.path.insert(0, str(ROOT / "tools"))

from agent_policy import AgentPolicy
from answer_markdown import BUSINESS_EXPLANATION_POLICY, BUSINESS_EXPLANATION_COMPACT_POLICY
import business_chat
from business_index import build_business_index
from business_onboarding_fixture import BATCH_QUESTION, ONLINE_QUESTION, sources
from business_synthesis import build_analysis_brief, project_analysis_brief, wants_business_overview
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


def source_authored_reply(payload):
    """Deliberately fixed prose to exercise delivery, never an inferred answer."""
    pages = [page for context in payload["source_context"] for page in context.get("pages", [])]
    def ref(path, text):
        return next(page["evidence_id"] for page in pages
                    if page["relative_path"] == path and text in page["source_text"])
    if payload["question"] == BATCH_QUESTION:
        main = ref("settle.cbl", "WRITE SETTLEMENT-REC")
        job = ref("nightjob.clle", "CALL PGM(SETTLE)")
        return (f"这条流程把符合条件的订单整理成结算输出记录，尚不代表付款或记账。[{main}]\n\n"
                "nightjob.clle 声明把 ORDERIN 覆盖到 APPDATA/ORDERS，并请求运行 SETTLE；"
                f"撤销覆盖命令位于调用正常返回之后。实际调度和覆盖效果需运行配置确认。[{job}]\n\n"
                "SETTLE 从 ORDER-IN 逐笔读取订单，只有状态 A 且金额大于零时，"
                "才把订单编号和金额直接复制到结算记录，并请求写向 SETTLEMENT-OUT。"
                "输入文件 ASSIGN 到 ORDERIN，输出文件 ASSIGN 到 SETTLEOUT。"
                f"不满足条件时仅发出 ORDER SKIPPED 提示，不进入该写出分支。[{main}]")
    main = ref("balance.cbl", "FROM ACCOUNT_BALANCES")
    return ("BALANCEQ 帮操作员按账户号取回已有的可用余额，注释将其定位为在线查询功能。"
            "调用方给出 ACCOUNT-KEY，程序按 ACCOUNT_ID 查询 ACCOUNT_BALANCES，"
            "把 AVAILABLE_BALANCE 放入 CURRENT-BALANCE 交回调用方。"
            f"这张表是余额来源；当前程序查询余额，不计算余额或记账。[{main}]\n\n"
            "到达 SQLCODE 判断时，零分支设置 RESPONSE-CODE 为 00，非零分支设置为 90。"
            "后一分支没有给余额补零，因此调用方应结合状态采用余额，"
            f"不能把 90 单独解释为账户不存在。屏幕及调用者尚未在这些源码中出现。[{main}]")


class BusinessExplanationDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name, text in sources().items():
            (self.source / name).write_text(text, encoding="utf-8")
        self.database = self.root / "index.sqlite"
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "offline-only", api_key="offline-only")

    def ask(self, question, *, first_purpose_only=False):
        captured, envelopes, replies = [], [], []
        original = business_chat._prompt_payload
        def projection(payload):
            captured.append(deepcopy(payload))
            return original(payload)
        def transport(request):
            envelope = json.loads(request.body)
            envelopes.append(envelope)
            payload = json.loads(envelope["messages"][-1]["content"])
            answer = ("SETTLE 整理符合受理要求的订单。" if first_purpose_only and len(envelopes) == 1
                      else source_authored_reply(payload))
            replies.append(answer)
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": answer},
                                                                   "finish_reason": "stop"}]}))
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")), \
                patch.object(socket, "create_connection", side_effect=AssertionError("Network forbidden")), \
                patch.object(business_chat, "_prompt_payload", side_effect=projection):
            output = business_chat.run_business_chat(question, self.database, self.source, self.config,
                transport=transport, framework_reference_path="", capture_context=True,
                policy=AgentPolicy(max_model_requests=2, max_answer_revisions=1))
        return output, captured, envelopes, replies

    def test_connected_business_explanations_keep_source_and_need_one_scripted_reply(self):
        for question, relation in ((BATCH_QUESTION, "writes_record_to"), (ONLINE_QUESTION, "reads_sql_table")):
            with self.subTest(question=question):
                output, captured, envelopes, replies = self.ask(question)
                self.assertEqual(len(envelopes), 1)
                self.assertEqual(output["agent_result"]["answer"], replies[0])
                payload = captured[-1]
                orientation = payload["business_analysis_brief"]["business_orientation"]
                self.assertTrue(any(row["relation"] == relation for row in orientation["handoffs"]))
                sent = json.loads(envelopes[0]["messages"][-1]["content"])
                self.assertEqual(sent["source_context"], business_chat._prompt_payload(payload)["source_context"])
                self.assertIn("business_explanation", sent["response_contract"])
                for compact in (False, True):
                    variant = {**payload, "budget_wire_compact": compact}
                    system = business_chat._system_for_payload(variant)
                    guidance = BUSINESS_EXPLANATION_COMPACT_POLICY if compact else BUSINESS_EXPLANATION_POLICY
                    self.assertEqual(system.count(guidance), 1)
                    self.assertIn("business_explanation", business_chat._prompt_payload(variant)["response_contract"])

    def test_clear_purpose_only_gap_gets_one_existing_bounded_revision(self):
        output, captured, envelopes, replies = self.ask(BATCH_QUESTION, first_purpose_only=True)
        self.assertEqual(len(envelopes), 2)
        self.assertEqual(output["agent_result"]["answer"], replies[-1])
        reviews = [payload["answer_review"] for payload in captured if payload.get("answer_review")]
        self.assertTrue(any({"program_roles", "data_roles"} <= set(review["missing_aspects"]) for review in reviews))
        self.assertEqual(output["agent_result"]["metrics"]["model_requests"], 2)

    def test_cross_role_questions_do_not_expand_narrow_questions(self):
        for question in ("请讲清 batch job、online programs、tables、files 的关系。",
                         "Explain how batch programs, online programs, tables and files fit together.",
                         "請解釋批次程式、資料表和檔案之間的關係。"):
            with self.subTest(question=question):
                self.assertTrue(wants_business_overview(question))
        for question in ("请解释 batch 程序的返回值表示什么。", "解释 online program 的错误码表示什么？",
                         "请解释 batch 程序的异常表现。", "Which programs call it?",
                         "请解释批处理程序从哪个文件读取订单。",
                         "请解释批处理程序读文件失败会怎样。",
                         "Explain which file this batch program reads.",
                         "Explain what happens when this online program reads an absent table.",
                         "简短解释 batch 程序和文件的关系。"):
            with self.subTest(question=question):
                self.assertFalse(wants_business_overview(question))

    def test_unknown_contract_guidance_survives_projection(self):
        brief = build_analysis_brief(BATCH_QUESTION, {"state": "located"}, [], [], 2048)
        original = deepcopy(brief)
        brief["business_explanation_contract"]["task"] += " Preserve this custom condition."
        projected = project_analysis_brief(brief)
        self.assertEqual(projected["business_explanation_contract"], brief["business_explanation_contract"])
        self.assertNotEqual(projected["business_explanation_contract"], original["business_explanation_contract"])


if __name__ == "__main__":
    unittest.main()
