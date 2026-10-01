"""Offline discovery recovery when a generic comment hides the real formula."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


class WeakCalculationDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="weak-calculation-discovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "synthetic-model",
                                       api_key="synthetic-only")
        self.requests = []
        self.write("display.cbl", "DISPLAY-TASK",
                   "*> 仅显示金额。\nDISPLAY DISPLAY-AMT.",
                   "01 DISPLAY-AMT PIC 9(5) VALUE ZERO.")
        self.write("premium.cbl", "PREMIUM-CALC",
                   "COMPUTE PRM-AMT = BASE-AMT * RATE.",
                   "01 BASE-AMT PIC 9(5) VALUE 8.\n"
                   "01 RATE PIC 9V99 VALUE 1.25.\n01 PRM-AMT PIC 9(7)V99.")

    def write(self, path, name, body, data):
        (self.source / path).write_text(
            f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
            f"WORKING-STORAGE SECTION.\n{data}\nPROCEDURE DIVISION.\n{body}\nGOBACK.\n",
            encoding="utf-8")

    def ask(self, question, responder, *, policy=None):
        build_business_index(self.source, self.database, source_format="free")
        ensure_repository_search(self.database, self.source)

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            self.requests.append(payload)
            reply = responder(payload)
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": reply}, "finish_reason": "stop"}]},
                ensure_ascii=False))

        return run_business_chat(question, self.database, self.source, self.config,
            transport=transport, allow_network=False, framework_reference_path="", policy=policy)

    def formula_answer(self, payload):
        page = next(page for page in payload["source_context"][0]["pages"]
                    if "COMPUTE PRM-AMT" in page["source_text"])
        return ("保费金额由基础金额乘以费率得到，结果存入 PRM-AMT。"
                "基础金额初值为 8，费率初值为 1.25，因此本程序当前给定的初值计算为 10。"
                "计算完成后程序返回，所示代码没有进一步修改保费结果。"
                f"[{page['evidence_id']}]")

    def test_generic_chinese_comment_requires_expansion_to_real_formula(self):
        def respond(payload):
            if len(self.requests) == 1:
                self.assertEqual(payload["retrieval_status"]["state"], "needs_discovery")
                self.assertTrue(payload["retrieval_status"]["current_question_match_observed"])
                self.assertFalse(payload["retrieval_status"]["query_expansion_attempted"])
                self.assertIn("仅有词面匹配", payload["task"])
                self.assertEqual(payload["source_context"][0]["pages"], [])
                return '{"search":["PRM-AMT"]}'
            self.assertEqual(payload["retrieval_status"]["state"], "source_candidates")
            self.assertTrue(payload["retrieval_status"]["query_expansion_attempted"])
            return self.formula_answer(payload)

        output = self.ask("保费金额怎么计算？", respond)
        self.assertEqual(len(self.requests), 2)
        self.assertIn("计算为 10", output["agent_result"]["answer"])
        self.assertEqual(output["agent_result"]["status"], "ANALYZED")

    def test_exact_field_calculation_does_not_add_discovery_request(self):
        def respond(payload):
            self.assertEqual(payload["retrieval_status"]["state"], "source_candidates")
            self.assertFalse(payload["retrieval_status"]["query_expansion_attempted"])
            return self.formula_answer(payload)

        self.ask("PRM-AMT 怎么计算？", respond)
        self.assertEqual(len(self.requests), 1)

    def test_unsuccessful_expansion_does_not_make_old_comment_a_formula_match(self):
        def respond(payload):
            if len(self.requests) <= 2:
                self.assertEqual(payload["retrieval_status"]["state"], "needs_discovery")
                self.assertEqual(payload["source_context"][0]["pages"], [])
                return ('{"search":["UNKNOWN-TERM"]}' if len(self.requests) == 1
                        else '{"search":["PRM-AMT"]}')
            self.assertEqual(payload["retrieval_status"]["state"], "source_candidates")
            return self.formula_answer(payload)

        output = self.ask("保费金额怎么计算？", respond)
        self.assertEqual(len(self.requests), 3)
        self.assertIn("计算为 10", output["agent_result"]["answer"])

    def test_explicit_file_without_formula_does_not_expand_to_other_files(self):
        def respond(payload):
            self.assertEqual(payload["business_map"]["source_identity"]["status"], "resolved")
            self.assertEqual(payload["retrieval_status"]["state"], "source_candidates")
            self.assertFalse(payload["retrieval_status"]["query_expansion_attempted"])
            page = payload["source_context"][0]["pages"][0]
            return ("这个程序把初值为零的 DISPLAY-AMT 显示出来，然后执行 GOBACK 返回。"
                    "所示完整程序只有显示动作，没有对这个字段执行算术计算。"
                    f"[{page['evidence_id']}]")

        output = self.ask("display.cbl 的金额怎么计算？", respond)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(output["agent_result"]["investigation"]["query_expansion_attempted"])

    def test_external_calculation_boundary_does_not_restart_word_discovery(self):
        self.write("fee.cbl", "FEE-ENTRY",
                   "*> 费用计算委托给外部子程序。\nCALL 'EXTERNAL-CALC' USING INPUT-AMT.",
                   "01 INPUT-AMT PIC 9(5) VALUE 8.")

        def respond(payload):
            self.assertEqual(payload["retrieval_status"]["state"], "source_candidates")
            formula = next(item for item in payload["question_investigation"]["required_items"]
                           if item["kind"] == "formula")
            self.assertEqual(formula["reason"], "external_implementation_unavailable")
            page = next(page for page in payload["source_context"][0]["pages"]
                        if "CALL 'EXTERNAL-CALC'" in page["source_text"])
            return ("费用计算由 EXTERNAL-CALC 子程序执行，调用者把初值为 8 的 INPUT-AMT 传给它。"
                    "调用返回后本程序直接返回，原文没有显示其他金额调整步骤。"
                    "当前源码没有该外部子程序的实现，因此调用者能够确认的是传入金额与调用位置。"
                    f"[{page['evidence_id']}]")

        self.ask("费用怎么计算？", respond)
        self.assertEqual(len(self.requests), 1)

    def test_weak_match_discovery_keeps_single_request_budget(self):
        output = self.ask("保费金额怎么计算？", lambda payload: "本次尚未定位到公式。",
                          policy=AgentPolicy(max_model_requests=1))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(output["reason_code"], "RETRIEVAL_UNRESOLVED")


if __name__ == "__main__":
    unittest.main()
