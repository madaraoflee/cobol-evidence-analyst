"""Offline source delivery and continuation regressions, not model quality scores."""

import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import _prompt_payload, run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search
from test_business_orientation import BATCH, QUERY, SETTLEMENT


SOURCES = {
    "entry.cbl": """IDENTIFICATION DIVISION.
PROGRAM-ID. PROJECTION.
*> Purpose: contract projection for an active contract.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 CONTRACT-AMOUNT PIC 9(6) VALUE 1000.
01 PROJECTED-AMOUNT PIC 9(8)V99.
PROCEDURE DIVISION.
MAIN.
CALL 'PROJECTOR' USING CONTRACT-AMOUNT PROJECTED-AMOUNT.
CALL 'REPORTER' USING PROJECTED-AMOUNT.
GOBACK.
""",
    "projector.cbl": """IDENTIFICATION DIVISION.
PROGRAM-ID. PROJECTOR.
DATA DIVISION.
LINKAGE SECTION.
01 INPUT-AMOUNT PIC 9(6).
01 OUTPUT-AMOUNT PIC 9(8)V99.
PROCEDURE DIVISION USING INPUT-AMOUNT OUTPUT-AMOUNT.
COMPUTE OUTPUT-AMOUNT = INPUT-AMOUNT * 1.05.
GOBACK.
""",
    "reporter.cbl": """IDENTIFICATION DIVISION.
PROGRAM-ID. REPORTER.
DATA DIVISION.
LINKAGE SECTION.
01 REPORT-AMOUNT PIC 9(8)V99.
PROCEDURE DIVISION USING REPORT-AMOUNT.
DISPLAY REPORT-AMOUNT.
GOBACK.
""",
}
SHALLOW = ("源码注释将 PROJECTION 描述为 contract projection，相关文件是 entry.cbl。"
           "若进一步说明具体处理什么、输入输出如何串起来，还需要查看主流程与计算子程序的实现。")
EXPLANATION = (
    "这项处理为有效合同计算投影金额，并把计算结果展示出来。"
    "主程序 PROJECTION 负责组织过程：先把 CONTRACT-AMOUNT 传入 PROJECTOR，"
    "由后者按输入金额乘 1.05 算出 OUTPUT-AMOUNT，结果通过第二个参数回到 PROJECTED-AMOUNT；"
    "随后主程序把这个结果交给 REPORTER，后者通过 DISPLAY 展示金额。"
    "因此 PROJECTOR 承担金额计算，REPORTER 承担结果展示，主程序把两步串起来。"
    "以源码声明的首次初始化金额 1000 为例，计算结果是 1050。"
    "这段源码展示的是参数传递和计算过程，没有读写数据库表或业务文件。"
)


class BusinessConceptPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name, text in SOURCES.items():
            (self.source / name).write_text(text, encoding="utf-8")
        self.database = self.root / "index.sqlite"
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)

    def run_question(self, replies, question="什么是 contract projection"):
        requests = []

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            pages = [page for bundle in payload.get("source_context", []) for page in bundle.get("pages", [])]
            text = replies[min(len(requests) - 1, len(replies) - 1)]
            identifiers = [page["evidence_id"] for page in pages]
            text += " " + " ".join("[" + identifier + "]" for identifier in identifiers)
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text},
                                                                     "finish_reason": "stop"}]}))

        with patch.object(socket.socket, "connect", side_effect=AssertionError("offline test")), \
                patch("socket.create_connection", side_effect=AssertionError("offline test")):
            result = run_business_chat(question, self.database, self.source,
                CompanyAPIConfig("https://offline.example.invalid/v1", "offline-only", api_key="offline-only"),
                transport=transport, framework_reference_path="",
                policy=AgentPolicy(max_model_requests=4, max_answer_revisions=1))
        return result["agent_result"], requests

    def test_concept_supplies_implementations_and_business_task_before_first_answer(self):
        result, requests = self.run_question([EXPLANATION])
        self.assertEqual(len(requests), 1, result)
        pages = [page for bundle in requests[0]["source_context"] for page in bundle["pages"]]
        self.assertEqual({page["relative_path"] for page in pages}, set(SOURCES))
        self.assertIn("COMPUTE OUTPUT-AMOUNT", "\n".join(page["source_text"] for page in pages))
        self.assertIn("source_walk", requests[0]["response_contract"]["business_explanation"])
        self.assertIn("business_overview", requests[0]["question_investigation"]["analysis_plan"]["intents"])
        self.assertIn("1050", result["answer"])
        self.assertEqual(result["status"], "ANALYZED", result.get("stop_reason"))
        self.assertEqual(result["stop_reason"], "sufficient_material")

    def test_deferred_implementation_explanation_gets_one_useful_synthesis(self):
        result, requests = self.run_question([SHALLOW, EXPLANATION])
        self.assertEqual(len(requests), 2, result)
        self.assertIn("draft_answer", requests[1])
        self.assertIn("还需要查看", requests[1]["draft_answer"])
        self.assertIn("COMPUTE OUTPUT-AMOUNT", "\n".join(page["source_text"]
            for context in requests[1]["source_context"] for page in context["pages"]))
        self.assertEqual(result["answer_completion"]["pending_investigation"], False)
        self.assertEqual(result["status"], "ANALYZED", result.get("stop_reason"))
        self.assertEqual(result["stop_reason"], "sufficient_material")
        self.assertIn("1050", result["answer"])
        self.assertNotIn("还需要查看", result["answer"])

    def test_repeated_deferral_does_not_loop_or_discard_received_text(self):
        result, requests = self.run_question([SHALLOW])
        self.assertGreaterEqual(len(requests), 2)
        self.assertLessEqual(len(requests), 3)
        self.assertTrue(result["answer"].strip())
        self.assertNotEqual(result["stop_reason"], "sufficient_material")

    def test_business_task_survives_optional_navigation_trimming(self):
        payload = {"question": "解释一下 contract projection", "answer_detail": "detailed",
                   "business_analysis_brief_omitted": True,
                   "source_context": [{"pages": [{"relative_path": "entry.cbl", "source_text": SOURCES["entry.cbl"]}]}]}
        contract = _prompt_payload(payload)["response_contract"]["business_explanation"]
        self.assertIn("source_walk", contract)
        self.assertIn("newcomer", contract["audience"])

    def test_business_concept_reads_batch_entry_online_function_and_data_roles(self):
        extra = {
            "nightjob.cl": "/* settlement processing entry */\n" + BATCH,
            "settle.cbl": SETTLEMENT.replace("prepare settlement output", "settlement processing: prepare output"),
            "balance.cbl": QUERY.replace("query account balance", "settlement processing: query account balance"),
            "ORDERLF.dds": "     A          R ORDERREC                  PFILE(ORDERPF)\n",
            "ORDERPF.dds": "     A          R ORDERREC\n     A            ORDERAMT       5P 0\n",
        }
        for name, source in extra.items():
            (self.source / name).write_text(source, encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        answer = (
            "这项结算业务把获准订单转换成结算结果，并提供账户余额查询功能。"
            "批处理入口通过 SBMJOB 提交调用 SETTLE 的作业请求，源码未规定生产调度时刻。"
            "SETTLE 读取 ORDER-FILE 的订单记录，APPROVAL-STATUS 为 Y 时将订单金额乘 2，"
            "写入 SETTLE-FILE 的结算记录；随后请求 RECORD-SERVICE 处理该记录。"
            "输入文件声明指向 ORDERLF，输出文件声明指向 SETTLEPF。"
            "余额查询程序 BALANCEQ 接收账户号，从 ACCOUNT_BALANCES 表按账户号查询余额，"
            "把结果放入 RESULT-AMOUNT 返回；它承担查询职责，不在该段 SQL 中修改余额。"
            "RECORD-SERVICE 的内部保存规则未包含在这些源码中，但前述订单筛选和结算计算可直接解释。"
        )
        result, requests = self.run_question([answer], question="什么是 settlement processing")
        pages = [page for bundle in requests[0]["source_context"] for page in bundle["pages"]]
        self.assertLessEqual(len(requests), 2, result)
        self.assertLessEqual(set(extra), {page["relative_path"] for page in pages})
        text = "\n".join(page["source_text"] for page in pages)
        for statement in ("SBMJOB", "FROM ACCOUNT_BALANCES", "WRITE SETTLE-REC", "PFILE(ORDERPF)"):
            self.assertIn(statement, text)
        self.assertIn("balance.cbl", {page["relative_path"] for page in pages})
        self.assertIn("业务", result["answer"])


if __name__ == "__main__":
    unittest.main()
