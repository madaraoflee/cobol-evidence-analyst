"""One bounded answer recovery, with unchanged evidence and complete accounting."""

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
from company_api import APIClientError, CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


def response(content=None, *, finish="stop", usage=None, **message_fields):
    raw = {"choices": [{"message": {"role": "assistant", "content": content, **message_fields},
                        "finish_reason": finish}]}
    if usage is not None:
        raw["usage"] = usage
    return TransportResponse(200, json.dumps(raw, ensure_ascii=False))


class EmptyLengthRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
                                       api_key="offline-neutral-credential", max_output_tokens=8192)
        (self.source / "rule.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. AMOUNT-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 BASIS PIC 9(5).\n01 FACTOR PIC 9V99.\n01 FINAL-AMOUNT PIC 9(7)V99.\n"
            "PROCEDURE DIVISION.\nMAIN.\n"
            "IF BASIS > ZERO\nCOMPUTE FINAL-AMOUNT = BASIS * FACTOR\n"
            "ELSE\nMOVE ZERO TO FINAL-AMOUNT\nEND-IF.\nGOBACK.\n", encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.requests = []
        self.envelopes = []
        self.hidden = "HIDDEN_REASONING_MUST_NOT_BECOME_AN_ANSWER"

    def ask(self, respond, *, budget=5, revisions=1):
        self.requests.clear()
        self.envelopes.clear()

        def transport(request):
            envelope = json.loads(request.body)
            self.envelopes.append(envelope)
            payload = json.loads(envelope["messages"][-1]["content"])
            self.requests.append(payload)
            return respond(len(self.requests), payload)

        output = run_business_chat("AMOUNT-RULE 的 FINAL-AMOUNT 怎么计算？", self.database, self.source,
            self.config, transport=transport, framework_reference_path="",
            policy=AgentPolicy(max_model_requests=budget, max_answer_revisions=revisions))
        self.result = output["agent_result"]
        self.trace = json.loads(Path(self.result["metrics"]["quality_trace_path"]).read_text())
        return output

    def grounded(self, payload, *, finish="stop", usage=None):
        pages = payload["source_context"][0]["pages"]
        reference = next(page["evidence_id"] for page in pages if "COMPUTE FINAL-AMOUNT" in page["source_text"])
        return response("BASIS 大于零时，FINAL-AMOUNT 按 BASIS × FACTOR 计算；否则赋零。"
                        "实际金额取决于输入的 BASIS 和 FACTOR，当前源码不能证明运行时数值。"
                        f"[{reference}]", finish=finish, usage=usage)

    def test_empty_length_then_answer_reuses_evidence_and_accounts_for_both_rounds(self):
        usage1 = {"prompt_tokens": 100, "completion_tokens": 8192, "total_tokens": 8292}
        usage2 = {"prompt_tokens": 110, "completion_tokens": 80, "total_tokens": 190}
        output = self.ask(lambda turn, payload: response(None, finish="length", usage=usage1,
            reasoning_content=self.hidden) if turn == 1 else self.grounded(payload, usage=usage2))
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0]["source_context"], self.requests[1]["source_context"])
        self.assertEqual(self.requests[0]["framework_references"], self.requests[1]["framework_references"])
        self.assertEqual(self.requests[0]["completed_actions"], self.requests[1]["completed_actions"])
        self.assertEqual(self.envelopes[0]["messages"][:-1], self.envelopes[1]["messages"][:-1])
        self.assertEqual(self.requests[1]["investigation_budget"], {
            "remaining_model_requests": 4, "searches_per_turn": 0, "reads_per_turn": 0,
            "framework_searches_per_turn": 0, "business_context_actions_per_turn": 0})
        self.assertIn("先写结论", self.requests[1]["task"])
        self.assertEqual(self.result["model_turns"], 2)
        self.assertEqual(self.result["metrics"]["model_requests"], 2)
        self.assertEqual(len(self.result["metrics"]["request_bytes"]), 2)
        self.assertTrue(self.result["metrics"]["empty_length_recovery_attempted"])
        self.assertFalse(self.result["answer_truncated"])
        self.assertFalse(self.result["continuation_attempted"])
        self.assertEqual(self.result["metrics"]["usage"]["total_tokens"], 8482)
        self.assertEqual(self.result["metrics"]["usage"]["reported_requests"], 2)
        self.assertEqual(self.result["metrics"]["usage"]["status"], "complete")
        first, second = self.trace["rounds"]
        self.assertEqual(first["response"]["usage"], usage1)
        self.assertEqual(first["response"]["error"], "MODEL_TEXT_EMPTY")
        self.assertEqual(first["response"]["response_shape"]["finish_reason"], "length")
        self.assertTrue(first["response"]["response_shape"]["reasoning_present"])
        self.assertEqual(second["stage"], "empty_length_recovery")
        self.assertEqual(second["response"]["usage"], usage2)
        self.assertEqual(self.trace["final"]["final_answer_round_ids"], ["round-2"])
        self.assertEqual(self.trace["final"]["final_answer_round_id"], "round-2")
        self.assertTrue(self.result["narrative"]["citations"])
        self.assertTrue(set(self.trace["final"]["cited_ids"]) <= set(self.trace["final"]["final_visible_ids"]))
        self.assertNotIn(self.hidden, json.dumps([output, self.trace, self.envelopes]))

    def test_second_empty_length_stops_without_a_loop(self):
        output = self.ask(lambda *_: response("", finish="length", reasoning_content=self.hidden), budget=8)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(output["runner_status"], "NOT_READY")
        self.assertEqual(output["reason_code"], "MODEL_TEXT_EMPTY")
        self.assertEqual(len(self.trace["rounds"]), 2)
        self.assertTrue(all(row["response"]["error"] == "MODEL_TEXT_EMPTY" for row in self.trace["rounds"]))
        self.assertFalse(self.result["model_answer_recorded"])
        self.assertNotIn(self.hidden, json.dumps([output, self.trace, self.envelopes]))

    def test_one_request_budget_does_not_retry(self):
        output = self.ask(lambda *_: response(None, finish="length"), budget=1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(output["runner_status"], "NOT_READY")
        self.assertFalse(self.result["metrics"]["empty_length_recovery_attempted"])

    def test_empty_stop_filter_refusal_auth_and_timeout_are_not_retried(self):
        cases = [response(""), response(None, finish="content_filter"),
                 response(None, finish="length", refusal="Cannot provide an answer"),
                 TransportResponse(401, '{"error":{"message":"no access"}}'),
                 APIClientError("REQUEST_TIMEOUT")]
        for reply in cases:
            with self.subTest(reply=type(reply).__name__):
                def respond(*_):
                    if isinstance(reply, Exception):
                        raise reply
                    return reply
                self.ask(respond)
                self.assertEqual(len(self.requests), 1)
                self.assertFalse(self.result["metrics"]["empty_length_recovery_attempted"])

    def test_recovery_tool_request_is_not_executed_or_reprompted(self):
        output = self.ask(lambda turn, _: response(None, finish="length") if turn == 1
                          else response('{"search":["UNNEEDED-LOOKUP"]}'), budget=8)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(output["runner_status"], "NOT_READY")
        self.assertNotIn("UNNEEDED-LOOKUP", json.dumps(self.result["tool_trace"]))

    def test_recovery_truncated_body_is_retained_without_third_continuation(self):
        output = self.ask(lambda turn, payload: response(None, finish="length") if turn == 1
                          else self.grounded(payload, finish="length"), budget=8)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertTrue(self.result["answer_truncated"])
        self.assertFalse(self.result["continuation_attempted"])
        self.assertEqual(self.trace["final"]["final_answer_round_ids"], ["round-2"])

    def test_empty_length_after_an_investigation_round_does_not_retry(self):
        output = self.ask(lambda turn, _: response('{"search":["FINAL-AMOUNT"]}') if turn == 1
                          else response(None, finish="length"), budget=8)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(output["runner_status"], "NOT_READY")
        self.assertFalse(self.result["metrics"]["empty_length_recovery_attempted"])

    def test_ambiguous_choices_do_not_infer_a_length_failure(self):
        choices = [{"message": {"role": "assistant", "content": None}, "finish_reason": reason}
                   for reason in ("length", "stop")]
        self.ask(lambda *_: TransportResponse(200, json.dumps({"choices": choices})))
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.result["metrics"]["empty_length_recovery_attempted"])


    def test_recovery_transport_error_stops_and_keeps_both_round_diagnostics(self):
        def respond(turn, payload):
            if turn == 1:
                return response(None, finish="length", reasoning_content=self.hidden)
            raise APIClientError("REQUEST_TIMEOUT")
        output = self.ask(respond, budget=8)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(output["reason_code"], "REQUEST_TIMEOUT")
        self.assertEqual([r["response"]["error"] for r in self.trace["rounds"]],
                         ["MODEL_TEXT_EMPTY", "REQUEST_TIMEOUT"])
        self.assertIsNone(self.result["finish_reason"])
        self.assertFalse(self.result["answer_truncated"])


if __name__ == "__main__":
    unittest.main()
