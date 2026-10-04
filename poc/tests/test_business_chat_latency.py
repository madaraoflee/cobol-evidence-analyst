"""Offline checks for prompt delivery when optional local inspection is slow."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from analyze_source import AnalysisCancelled
import business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search
import semantic_scope


class BusinessChatLatencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.text = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. BILL-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 BILL-DATE PIC 9(8) VALUE 20300101.\n"
            "01 TODAY-DATE PIC 9(8) VALUE 20300102.\n"
            "01 PRINCIPAL PIC 9(7)V99 VALUE 10.\n"
            "01 FEE PIC 9(5)V99 VALUE 2.\n"
            "01 BILL-AMOUNT PIC 9(8)V99 VALUE ZERO.\n"
            "PROCEDURE DIVISION.\nMAIN.\n"
            "*> billing notice premium calculation\n"
            "IF BILL-DATE <= TODAY-DATE AND PRINCIPAL > ZERO\n"
            "COMPUTE BILL-AMOUNT = PRINCIPAL + FEE\n"
            "DISPLAY BILL-AMOUNT\nEND-IF.\nGOBACK.\n")
        (self.source / "rule.cbl").write_text(self.text, encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True,
                             framework_reference_path="")
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
                                       api_key="offline-only")
        self.clock_value = 0.0
        self.clock = SimpleNamespace(monotonic=lambda: self.clock_value)
        self.requests = []
        self.transport_calls = []
        for owner, attribute in ((socket, "create_connection"), (socket.socket, "connect"),
                                  (socket.socket, "connect_ex")):
            patcher = mock.patch.object(owner, attribute, side_effect=AssertionError("Network forbidden"))
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def pages(payload):
        return [page for bundle in payload["source_context"] for page in bundle["pages"]]

    def answer(self, payload):
        page = next(page for page in self.pages(payload)
                    if "COMPUTE BILL-AMOUNT" in page["source_text"])
        return ("账单日期已到且本金为正时，将本金加费用得到金额并输出通知；"
                f"否则跳过。[{page['evidence_id']}]")

    def ask(self, *, question="billing notice 的触发条件和premium金额如何计算？",
            respond=None, policy=None, check_cancel=None):
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            self.requests.append(payload)
            self.transport_calls.append(self.clock_value)
            text = respond(payload) if respond else self.answer(payload)
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text},
                "finish_reason": "stop"}]}, ensure_ascii=False))

        with mock.patch.object(business_chat, "time", self.clock):
            return business_chat.run_business_chat(question, self.database, self.source, self.config,
                transport=transport, allow_network=False, framework_reference_path="",
                policy=policy or AgentPolicy(), check_cancel=check_cancel)["agent_result"]

    def test_optional_semantic_budget_keeps_raw_conditions_formula_and_output(self):
        def slow_prepare(*args, check_cancel, **kwargs):
            self.clock_value = 4.0
            check_cancel()
            raise AssertionError("Optional analysis continued beyond its budget")

        with mock.patch.object(semantic_scope, "prepare_semantic_scope", side_effect=slow_prepare) as prepare:
            result = self.ask(policy=AgentPolicy(max_model_requests=1))
        self.assertGreater(prepare.call_count, 0)
        self.assertEqual(len(self.requests), 1)
        supplied = "\n".join(page["source_text"] for page in self.pages(self.requests[0]))
        for rule in ("IF BILL-DATE <= TODAY-DATE AND PRINCIPAL > ZERO",
                     "COMPUTE BILL-AMOUNT = PRINCIPAL + FEE", "DISPLAY BILL-AMOUNT"):
            self.assertIn(rule, supplied)
        self.assertTrue(result["model_answer_recorded"])
        self.assertIn("本金加费用", result["answer"])
        budgets = [item for item in result["boundaries"] if item.get("reason") == "local_analysis_budget_reached"]
        self.assertEqual(len(budgets), 1)
        self.assertEqual(budgets[0]["stage"], "initial_context")
        self.assertEqual(budgets[0]["limit_seconds"], 3)
        self.assertTrue(budgets[0]["source_text_retained"])
        self.assertFalse(any(item.get("reason") == "semantic_scope_unavailable" for item in result["boundaries"]))
        self.assertEqual(result["metrics"]["timing_seconds"]["first_model_request_seconds"], 4.0)

    def test_smaller_total_budget_also_limits_initial_optional_work(self):
        def slow_prepare(*args, check_cancel, **kwargs):
            self.clock_value = 2.0
            check_cancel()
            raise AssertionError("The smaller total allowance was ignored")

        with mock.patch.object(semantic_scope, "prepare_semantic_scope", side_effect=slow_prepare):
            result = self.ask(policy=AgentPolicy(max_initial_context_seconds=3,
                max_local_analysis_seconds=1, max_model_requests=1))
        self.assertEqual(len(self.requests), 1)
        budget = next(item for item in result["boundaries"]
                      if item.get("reason") == "local_analysis_budget_reached")
        self.assertEqual(budget["limit_seconds"], 1)
        self.assertTrue(result["model_answer_recorded"])

    def test_user_cancellation_takes_precedence_over_optional_budget(self):
        cancel_requested = False
        failure = AnalysisCancelled("Cancelled by the user")

        def user_cancel():
            if cancel_requested:
                raise failure

        def cancelled_prepare(*args, check_cancel, **kwargs):
            nonlocal cancel_requested
            self.clock_value = 4.0
            cancel_requested = True
            check_cancel()
            raise AssertionError("Cancellation was swallowed")

        with mock.patch.object(semantic_scope, "prepare_semantic_scope", side_effect=cancelled_prepare):
            with self.assertRaises(AnalysisCancelled) as caught:
                self.ask(check_cancel=user_cancel)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.requests, [])

    def test_provider_wait_is_excluded_and_followup_gets_total_local_budget(self):
        provider_finished = False
        followup_inspections = []
        original = semantic_scope.build_business_evidence

        def inspected(*args, check_cancel, **kwargs):
            if provider_finished:
                followup_inspections.append(1)
                if len(followup_inspections) == 1:
                    # Four local seconds exceed the initial limit, but remain
                    # within the separate total local allowance after turn one.
                    self.clock_value += 4.0
                check_cancel()
            return original(*args, check_cancel=check_cancel, **kwargs)

        def respond(payload):
            nonlocal provider_finished
            self.clock_value += 120.0
            if len(self.requests) == 1:
                provider_finished = True
                return json.dumps({"inspect_business_context": [
                    {"relative_path": "rule.cbl", "line": 14}]})
            return self.answer(payload)

        with mock.patch.object(semantic_scope, "build_business_evidence", side_effect=inspected):
            result = self.ask(respond=respond, policy=AgentPolicy(max_model_requests=2))
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(followup_inspections)
        self.assertTrue(result["model_answer_recorded"])
        self.assertFalse(any(item.get("reason") == "local_analysis_budget_reached" for item in result["boundaries"]))
        timings = result["metrics"]["timing_seconds"]
        self.assertEqual(timings["provider_wait_seconds"], 240.0)
        self.assertEqual(timings["local_processing_seconds"], 4.0)
        self.assertEqual(timings["total_seconds"], 244.0)

    def test_complete_program_is_supplied_before_optional_deep_parsing(self):
        with mock.patch.object(semantic_scope, "prepare_semantic_scope",
                side_effect=AssertionError("Already complete program was parsed again")) as prepare:
            result = self.ask(question="BILL-RULE 的出账条件、金额计算和输出流程是什么？")
        prepare.assert_not_called()
        complete = [page for page in self.pages(self.requests[0])
                    if "complete_working_set" in page.get("selection_reasons", [])]
        self.assertEqual(len(complete), 1)
        self.assertEqual(complete[0]["source_text"], self.text.rstrip("\n"))
        self.assertFalse(complete[0].get("span_truncated", False))
        self.assertEqual(complete[0]["end_line"], len(self.text.splitlines()))
        self.assertTrue(result["model_answer_recorded"])


if __name__ == "__main__":
    unittest.main()
