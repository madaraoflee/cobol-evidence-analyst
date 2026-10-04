"""Offline regressions for source actions deferred by a per-turn allowance."""

from dataclasses import replace
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
from company_api import CompanyAPIConfig, TransportResponse
import repository_discovery


class PendingSourceReadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
                                       api_key="offline-only")
        # The supported read-only configuration isolates the shared per-turn
        # reading limit from optional semantic inspection.
        self.policy = AgentPolicy(max_business_context_actions_per_turn=0, max_evidence_groups=0,
                                  max_complete_source_characters=36000)
        self.requests, self.answers = [], []
        for target, attribute in ((socket, "create_connection"), (socket.socket, "connect"),
                                  (socket.socket, "connect_ex")):
            patcher = mock.patch.object(target, attribute, side_effect=AssertionError("Network forbidden"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self, count=8):
        header = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. VALUEFLOW.\nDATA DIVISION.\n"
                  "WORKING-STORAGE SECTION.\n"
                  + "".join(f"01 AMOUNT-{index} PIC 9(7)V99 VALUE 1.\n" for index in range(count))
                  + "PROCEDURE DIVISION.\nMAIN.\n"
                  + "".join(f"PERFORM RULE-{index}.\n" for index in range(count)) + "GOBACK.\n")
        body = "".join("*> neutral source separation\n" * 400
            + f"RULE-{index}.\nCOMPUTE AMOUNT-{index} = AMOUNT-{index} * 2.\nEXIT.\n"
            for index in range(count))
        (self.source / "rule.cbl").write_text(header + body, encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        repository_discovery.ensure_repository_search(self.database, self.source)

    @staticmethod
    def pages(payload):
        return [page for bundle in payload["source_context"] for page in bundle["pages"]]

    def ask(self, *, policy=None, fail_followup=False):
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            self.requests.append(payload)
            if fail_followup and len(self.requests) > 1:
                return TransportResponse(500, '{"error":{"code":"server_error"}}')
            page = next(page for page in self.pages(payload) if "COMPUTE AMOUNT-" in page["source_text"])
            answer = f"已读金额按原值乘以二。[{page['evidence_id']}]"
            self.answers.append(answer)
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": answer},
                "finish_reason": "stop"}]}, ensure_ascii=False))

        return run_business_chat("VALUEFLOW 的金额如何计算？", self.database, self.source, self.config,
            transport=transport, framework_reference_path="", policy=policy or self.policy)["agent_result"]

    def assert_first_draft_retained(self, result):
        self.assertEqual(result["answer"], self.answers[0])
        self.assertTrue(result["model_answer_recorded"])
        self.assertEqual(result["status"], "PARTIAL")
        first_ids = {page["evidence_id"] for page in self.pages(self.requests[0])}
        self.assertTrue(result["narrative"]["citations"])
        self.assertLessEqual({row["evidence_id"] for row in result["narrative"]["citations"]}, first_ids)

    def test_calculation_reads_pending_source_on_next_budgeted_turn(self):
        self.build()
        result = self.ask()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0]["investigation_budget"]["reads_per_turn"], 0)
        self.assertTrue(self.requests[0]["question_investigation"]["planned_actions"])
        first_text = "\n".join(page["source_text"] for page in self.pages(self.requests[0]))
        final_text = "\n".join(page["source_text"] for page in self.pages(self.requests[-1]))
        self.assertNotIn("COMPUTE AMOUNT-7", first_text)
        self.assertIn("COMPUTE AMOUNT-7", final_text)
        self.assertEqual(result["stop_reason"], "sufficient_material")
        self.assertEqual(result["metrics"]["tool_calls"]["read"], 4)
        previous_reads = 0
        for payload in self.requests:
            reads = sum("read" in action for action in payload["completed_actions"])
            self.assertLessEqual(reads - previous_reads, self.policy.max_reads_per_turn)
            previous_reads = reads

    def test_final_request_budget_does_not_allow_an_extra_turn(self):
        self.build()
        result = self.ask(policy=replace(self.policy, max_model_requests=1))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["metrics"]["tool_calls"]["read"], 3)
        self.assertEqual(result["stop_reason"], "request_budget")
        self.assert_first_draft_retained(result)

    def test_disabled_read_and_inspection_tools_do_not_create_followup_turns(self):
        self.build()
        result = self.ask(policy=replace(self.policy, max_reads_per_turn=0))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["metrics"]["tool_calls"]["read"], 0)
        self.assertEqual(result["metrics"]["tool_calls"]["inspect_business_context"], 0)
        self.assert_first_draft_retained(result)

    def test_followup_server_failure_keeps_the_first_draft_and_citations(self):
        self.build()
        result = self.ask(fail_followup=True)
        self.assertEqual(len(self.requests), 2)
        self.assert_first_draft_retained(result)
        self.assertEqual(result["diagnostics"][-1]["http_status"], 500)
        self.assertEqual(result["metrics"]["provider_retries"], [])

    def test_failed_reads_are_not_repeated_on_empty_followup_turns(self):
        self.build(count=7)
        original = repository_discovery.read_repository_context
        failures = []

        def read(*args, **kwargs):
            if kwargs.get("start_line", 0) >= 2000:
                failures.append((kwargs["start_line"], kwargs["end_line"]))
                raise ValueError("Source is unavailable")
            return original(*args, **kwargs)

        with mock.patch.object(repository_discovery, "read_repository_context", side_effect=read):
            result = self.ask()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(failures), 3)
        self.assertEqual(len(failures), len(set(failures)))
        self.assert_first_draft_retained(result)

    def test_transmission_omissions_do_not_spend_all_remaining_requests(self):
        self.build()
        result = self.ask(policy=replace(self.policy, max_source_characters=4096))
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(self.requests[-1]["question_investigation"]["planned_actions"])
        self.assertIn("retrieved_source_not_visible", {
            row.get("reason") for row in result["investigation_state"]["completed_actions"]})
        self.assertEqual(result["metrics"]["tool_calls"]["read"], 4)
        self.assertEqual(result["stop_reason"], "question_evidence_incomplete")


if __name__ == "__main__":
    unittest.main()
