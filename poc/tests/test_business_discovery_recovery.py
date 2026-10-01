from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import _actions, run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


class BusinessDiscoveryRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model",
                                       api_key="local-test-secret")
        for index in range(24):
            self.write(f"entry-{index:03}.cbl", f"ROUTINE-{index}", "DISPLAY 'UNRELATED'.")
        self.write("z-calculation.cbl", "CALCULATION-ENTRY",
            "IF BASE-AMOUNT > ZERO\n"
            "COMPUTE FINAL-PREMIUM = BASE-AMOUNT * PREMIUM-RATE\n"
            "ELSE\nMOVE ZERO TO FINAL-PREMIUM\nEND-IF.",
            "01 BASE-AMOUNT PIC 9(5).\n01 PREMIUM-RATE PIC 9V99.\n"
            "01 FINAL-PREMIUM PIC 9(7)V99.")
        build_business_index(self.source, self.database, source_format="free")
        ensure_repository_search(self.database, self.source)
        self.requests = []

    def write(self, path, name, body, data=""):
        (self.source / path).write_text(
            f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
            f"WORKING-STORAGE SECTION.\n{data}\nPROCEDURE DIVISION.\nMAIN.\n{body}\nGOBACK.\n",
            encoding="utf-8")

    def ask(self, responder, *, question="保费具体怎么计算？", **kwargs):
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            self.requests.append(payload)
            reply = responder(payload)
            if isinstance(reply, TransportResponse):
                return reply
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": reply}, "finish_reason": "stop"}]}, ensure_ascii=False))
        return run_business_chat(question, self.database, self.source,
            self.config, transport=transport,
            framework_reference_path=kwargs.pop("framework_reference_path", ""), **kwargs)

    def formula_answer(self, payload):
        self.assertEqual(payload["retrieval_status"]["state"], "source_candidates")
        self.assertTrue(payload["retrieval_status"]["query_expansion_attempted"])
        pages = payload["source_context"][0]["pages"]
        page = next(page for page in pages if "COMPUTE FINAL-PREMIUM" in page["source_text"])
        self.assertIn("IF BASE-AMOUNT > ZERO", page["source_text"])
        self.assertTrue(payload["evidence_groups"])
        return f"基础金额大于零时，保费等于基础金额乘费率；否则保费为零。[{page['evidence_id']}]"

    def test_cross_language_discovery_excludes_random_sources_and_accepts_explained_action(self):
        def respond(payload):
            if len(self.requests) == 1:
                self.assertEqual(payload["source_context"][0]["pages"], [])
                self.assertEqual(payload["source_context"][0]["outline"], [])
                self.assertEqual(payload["retrieval_status"]["state"], "needs_discovery")
                self.assertFalse(payload["retrieval_status"]["source_absence_proven"])
                self.assertIn("这一轮只做检索规划", payload["task"])
                self.assertLess(len(json.dumps(payload).encode()), 14000)
                return '我先查找对应业务字段。\n```json\n{"search":["PREMIUM"]}\n```'
            return self.formula_answer(payload)

        output = self.ask(respond)
        self.assertEqual(len(self.requests), 2)
        result = output["agent_result"]
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["stop_reason"], "question_evidence_incomplete")
        inputs = next(item for item in result["investigation_state"]["question_investigation"]["required_items"]
                      if item["kind"] == "inputs")
        self.assertEqual(inputs["reason"], "input_source_not_located")
        self.assertEqual(set(inputs["fields"]), {"BASE-AMOUNT", "PREMIUM-RATE"})
        self.assertIn("乘费率", result["answer"])
        self.assertTrue(result["narrative"]["citations"])
        self.assertTrue(all(ref["kind"] == "source_page" for ref in result["narrative"]["citations"]))
        trace = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text())
        self.assertEqual(trace["rounds"][0]["stage"], "discover")
        self.assertEqual(trace["rounds"][0]["sources"], [])
        self.assertTrue(trace["rounds"][1]["sources"])

    def test_premature_no_data_answer_gets_one_discovery_retry_then_real_evidence(self):
        def respond(payload):
            if len(self.requests) == 1:
                return "没有找到对应公司或资料。"
            if len(self.requests) == 2:
                self.assertIn("上一轮没有执行检索", payload["task"])
                return '{"search":["PREMIUM"]}'
            return self.formula_answer(payload)

        output = self.ask(respond)
        self.assertEqual(len(self.requests), 3)
        self.assertIn("乘费率", output["agent_result"]["answer"])
        self.assertNotIn("没有找到对应公司", output["agent_result"]["answer"])

    def test_no_source_discovered_cannot_be_recorded_as_sufficient_material(self):
        output = self.ask(lambda payload: "没有找到对应公司或资料。")
        self.assertEqual(len(self.requests), 2)
        result = output["agent_result"]
        self.assertEqual(result["status"], "ABSTAINED")
        self.assertFalse(result["model_answer_recorded"])
        self.assertEqual(result["stop_reason"], "RETRIEVAL_UNRESOLVED")
        self.assertEqual(result["investigation"]["retrieval_status"], "unresolved")
        self.assertEqual(result["narrative"]["citations"], [])
        self.assertIn("不表示程序或资料不存在", result["answer"])

    def test_failed_expansion_records_zero_hits_without_restarting_unbounded_search(self):
        def respond(payload):
            return '{"search":["UNKNOWN-TERM"]}' if len(self.requests) == 1 else "没有资料。"

        output = self.ask(respond)
        result = output["agent_result"]
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(output["reason_code"], "RETRIEVAL_UNRESOLVED")
        self.assertTrue(result["investigation"]["query_expansion_attempted"])
        self.assertEqual(result["investigation"]["searches"][-1]["matched_files"], 0)

    def test_discovery_recovery_respects_single_request_budget_and_provider_refusal(self):
        output = self.ask(lambda payload: "没有资料。", policy=AgentPolicy(max_model_requests=1))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(output["reason_code"], "RETRIEVAL_UNRESOLVED")
        self.requests.clear()
        output = self.ask(lambda payload: TransportResponse(200, json.dumps({"choices": [{
            "message": {"role": "assistant", "content": "", "refusal": "Request declined."},
            "finish_reason": "stop"}]})))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(output["reason_code"], "MODEL_REFUSED")

    def test_ambiguous_or_business_json_is_not_executed_as_search(self):
        self.assertIsNone(_actions('Explanation.\n```json\n{"rate":2}\n```'))
        self.assertIsNone(_actions('```json\n{"search":["FIRST"]}\n```\n'
                                   '```json\n{"search":["SECOND"]}\n```'))

    def test_question_matched_manual_remains_usable_without_claiming_source_was_found(self):
        manual = self.root / "manual.md"
        manual.write_text("# Deferred processing\n\nDEFERRED-SETTLEMENT retains requests until the next working day.\n",
                          encoding="utf-8")

        def respond(payload):
            self.assertEqual(payload["retrieval_status"]["state"], "framework_candidates")
            self.assertEqual(payload["source_context"][0]["pages"], [])
            self.assertIn("不能把手册约定写成程序已实现", payload["task"])
            reference = payload["framework_references"][0]
            return f"手册规定请求保留到下一个工作日。[{reference['reference_id']}]"

        output = self.ask(respond, question="框架手册中 DEFERRED-SETTLEMENT 的含义是什么？",
            framework_reference_path=manual, policy=AgentPolicy(max_model_requests=1))
        self.assertEqual(len(self.requests), 1)
        self.assertIn("下一个工作日", output["agent_result"]["answer"])
        self.assertEqual(output["agent_result"]["status"], "ANALYZED")
        self.assertEqual(output["agent_result"]["investigation"]["retrieval_status"], "framework_candidates")
        self.assertEqual(output["agent_result"]["evidence_refs"], [])


if __name__ == "__main__":
    unittest.main()
