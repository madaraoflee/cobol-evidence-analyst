"""A known completed empty response earns one answer attempt, never a loop."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
import business_chat
from company_api import APIClientError, TransportResponse
import test_business_chat_empty_length_recovery as fixtures
import test_business_chat_response_recovery as draft_fixtures


class EmptyStopRecoveryTests(unittest.TestCase):
    # Reuse the neutral source fixture without inheriting its length tests.
    setUp = fixtures.EmptyLengthRecoveryTests.setUp
    grounded = fixtures.EmptyLengthRecoveryTests.grounded

    def ask(self, respond, *, budget=8, question=None, answer_instructions="", conversation_id=None):
        self.requests.clear()
        self.envelopes.clear()
        self.ask_number = getattr(self, "ask_number", 0) + 1

        def transport(request):
            envelope = json.loads(request.body)
            self.envelopes.append(envelope)
            payload = json.loads(envelope["messages"][-1]["content"])
            self.requests.append(payload)
            return respond(len(self.requests), payload)

        output = business_chat.run_business_chat(
            question or "AMOUNT-RULE 的 FINAL-AMOUNT 怎么计算？", self.database, self.source,
            self.config, transport=transport, framework_reference_path="",
            answer_instructions=answer_instructions,
            conversation_id=conversation_id or f"empty-stop-case-{self.ask_number}",
            policy=AgentPolicy(max_model_requests=budget, max_answer_revisions=0))
        self.result = output["agent_result"]
        self.trace = json.loads(Path(self.result["metrics"]["quality_trace_path"]).read_text())
        return output

    def assert_recovery_accounting(self):
        self.assertTrue(self.result["metrics"]["empty_stop_recovery_attempted"])
        self.assertFalse(self.result["metrics"]["empty_length_recovery_attempted"])
        self.assertEqual(self.result["metrics"]["model_requests"], len(self.requests))
        self.assertEqual(len(self.trace["rounds"]), len(self.requests))
        self.assertEqual(self.trace["rounds"][-1]["stage"], "empty_stop_recovery")
        recovered = next(row for row in self.result["boundaries"]
                         if row["reason"] == "model_empty_stop_recovery")
        self.assertEqual(recovered["requests"], 1)

    def test_completed_empty_stop_gets_one_same_evidence_answer_with_saved_system(self):
        instructions = '用「业务结论」和「处理依据」解释。\n保留引用、双引号 " 与换行。'
        usage1 = {"prompt_tokens": 100, "completion_tokens": 351, "total_tokens": 451,
                  "completion_tokens_details": {"reasoning_tokens": 351}}
        usage2 = {"prompt_tokens": 110, "completion_tokens": 50, "total_tokens": 160}
        output = self.ask(lambda turn, payload: fixtures.response("", usage=usage1)
                          if turn == 1 else self.grounded(payload, usage=usage2),
                          answer_instructions=instructions)
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(self.result["answer_available"])
        self.assertTrue(self.result["model_answer_recorded"])
        self.assertFalse(self.result["answer_truncated"])
        self.assert_recovery_accounting()
        first, second = self.trace["rounds"]
        self.assertEqual(first["response"]["error"], "MODEL_TEXT_EMPTY")
        self.assertEqual(first["response"]["response_shape"]["finish_reason"], "stop")
        self.assertEqual(first["response"]["raw_content_characters"], 0)
        self.assertEqual(first["response"]["usage"], usage1)
        self.assertEqual(second["response"]["usage"], usage2)
        self.assertEqual(self.result["metrics"]["usage"]["total_tokens"], 611)
        for key in ("source_context", "framework_references", "framework_facts", "completed_actions"):
            self.assertEqual(self.requests[0][key], self.requests[1][key])
        self.assertEqual(self.envelopes[0]["messages"][:-1], self.envelopes[1]["messages"][:-1])
        for envelope in self.envelopes:
            self.assertEqual(envelope["messages"][0]["role"], "system")
            self.assertEqual(envelope["messages"][0]["content"].count(instructions), 1)
        self.assertNotIn("长度上限", self.requests[1]["task"])
        self.assertEqual(self.requests[1]["investigation_budget"], {
            "remaining_model_requests": 7, "searches_per_turn": 0, "reads_per_turn": 0,
            "framework_searches_per_turn": 0, "business_context_actions_per_turn": 0})
        self.assertEqual(self.trace["final"]["final_answer_round_ids"], ["round-2"])

    def test_recovery_that_is_empty_again_stops_after_two_received_responses(self):
        for second_finish in ("stop", "length"):
            with self.subTest(second_finish=second_finish):
                output = self.ask(lambda turn, _: fixtures.response(
                    None, finish="stop" if turn == 1 else second_finish), budget=8)
                self.assertEqual(len(self.requests), 2)
                self.assert_recovery_accounting()
                self.assertEqual(output["reason_code"], "MODEL_TEXT_EMPTY")
                self.assertFalse(self.result["model_answer_recorded"])
                self.assertTrue(all(row["response"]["error"] == "MODEL_TEXT_EMPTY"
                                    for row in self.trace["rounds"]))
                self.assertNotEqual(self.result["stop_reason"], "sufficient_material")

    def test_recovery_length_body_is_preserved_as_partial_without_third_request(self):
        output = self.ask(lambda turn, payload: fixtures.response("") if turn == 1
                          else self.grounded(payload, finish="length"))
        self.assertEqual(len(self.requests), 2)
        self.assert_recovery_accounting()
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertTrue(self.result["model_answer_recorded"])
        self.assertTrue(self.result["answer_truncated"])
        self.assertEqual(self.result["finish_reason"], "length")
        self.assertEqual(self.result["status"], "PARTIAL")
        self.assertIn("BASIS × FACTOR", self.result["answer"])
        self.assertFalse(self.result["continuation_attempted"])
        self.assertEqual(self.trace["final"]["final_answer_round_ids"], ["round-2"])

    def test_one_request_budget_keeps_completed_empty_stop_without_recovery(self):
        self.ask(lambda *_: fixtures.response(""), budget=1)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.result["metrics"]["empty_stop_recovery_attempted"])
        self.assertFalse(self.result["metrics"]["empty_length_recovery_attempted"])
        self.assertEqual(self.result["stop_reason"], "MODEL_TEXT_EMPTY")

    def test_known_empty_stop_after_source_read_uses_only_that_rounds_evidence(self):
        output = self.ask(lambda turn, payload:
            fixtures.EmptyLengthRecoveryTests.read_action() if turn == 1 else
            fixtures.response("") if turn == 2 else self.grounded(payload), budget=3)
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertEqual(len(self.requests), 3)
        self.assert_recovery_accounting()
        self.assertEqual(self.result["metrics"]["tool_calls"]["read"], 1)
        for key in ("source_context", "framework_references", "framework_facts", "completed_actions"):
            self.assertEqual(self.requests[1][key], self.requests[2][key])
        self.assertEqual(self.envelopes[1]["messages"][:-1], self.envelopes[2]["messages"][:-1])
        boundary = next(row for row in self.result["boundaries"]
                        if row["reason"] == "model_empty_stop_recovery")
        self.assertEqual(boundary["from_round_id"], "round-2")
        self.assertEqual(self.trace["final"]["final_answer_round_ids"], ["round-3"])

    def test_empty_stop_with_retained_draft_does_not_trigger_answer_replacement(self):
        output, original = draft_fixtures.BusinessChatResponseRecoveryTests.draft_followup(
            self, lambda _: fixtures.response(""))
        result = output["agent_result"]
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["answer"], original)
        self.assertEqual(result["narrative"]["text"], original)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertTrue(result["model_answer_recorded"])
        self.assertFalse(result["metrics"]["empty_stop_recovery_attempted"])
        self.assertFalse(result["metrics"]["empty_length_recovery_attempted"])
        self.assertFalse(any(row["reason"] == "model_empty_stop_recovery" for row in result["boundaries"]))

    def test_refusal_filter_tools_and_unknown_transport_results_do_not_recover(self):
        cases = [fixtures.response(None, finish="content_filter"),
                 fixtures.response(None, refusal="Cannot provide an answer"),
                 fixtures.response(None, tool_calls=[{"id": "call-1", "type": "function",
                    "function": {"name": "search", "arguments": "{}"}}]),
                 fixtures.response(None, function_call={"name": "search", "arguments": "{}"}),
                 TransportResponse(401, '{"error":{"message":"no access"}}'),
                 APIClientError("REQUEST_TIMEOUT"), APIClientError("TRANSPORT_ERROR")]
        for failed in cases:
            with self.subTest(failed=type(failed).__name__):
                def respond(*_):
                    if isinstance(failed, Exception):
                        raise failed
                    return failed
                self.ask(respond)
                self.assertEqual(len(self.requests), 1)
                self.assertFalse(self.result["metrics"]["empty_stop_recovery_attempted"])
                self.assertFalse(self.result["metrics"]["empty_length_recovery_attempted"])

    def test_missing_or_ambiguous_choices_do_not_turn_into_an_empty_stop_retry(self):
        cases = [{}, {"choices": []}, {"choices": [
            {"message": {"role": "assistant", "content": None}, "finish_reason": finish}
            for finish in ("stop", "stop")]}, {"choices": [
            {"message": {"role": "assistant", "content": None}, "finish_reason": finish}
            for finish in ("stop", "length")]}]
        for raw in cases:
            with self.subTest(raw=raw):
                self.ask(lambda *_: TransportResponse(200, json.dumps(raw)))
                self.assertEqual(len(self.requests), 1)
                self.assertFalse(self.result["metrics"]["empty_stop_recovery_attempted"])
                self.assertFalse(self.result["metrics"]["empty_length_recovery_attempted"])

    def test_refusal_parts_and_unrecognized_content_do_not_authorize_empty_recovery(self):
        visible = "The source supplies a business result."
        cases = [
            [{"type": "refusal", "refusal": "Cannot provide an answer"}],
            {"type": "text", "text": visible},
            [{"type": "business_text", "text": visible}],
            0, 42, True,
            [{"type": "text"}],
            [{"type": "text", "text": None}],
            [{"type": "output_text", "text": 0}],
            [{"type": "text", "text": {"value": visible}}],
            ["unrecognized text part"],
            [{"type": "text", "text": "", "refusal": "Cannot provide an answer"}],
            [{"type": "text", "text": "", "tool_calls": [{"id": "call-1"}]}],
            [{"type": "output_text", "text": "", "function_call": {"name": "search"}}],
        ]
        for content in cases:
            with self.subTest(content=content):
                self.ask(lambda *_: fixtures.response(content))
                self.assertEqual(len(self.requests), 1)
                self.assertFalse(self.result["metrics"]["empty_stop_recovery_attempted"])
                self.assertFalse(self.result["metrics"]["empty_length_recovery_attempted"])
                self.assertFalse(any(row["reason"] == "model_empty_stop_recovery"
                                     for row in self.result["boundaries"]))

    def test_empty_text_parts_and_recognized_json_wrappers_can_recover_once(self):
        contents = [None, [], [{"type": "text", "text": ""}],
                    [{"type": "text", "text": ""}, {"type": "output_text", "text": " \t\n"}],
                    json.dumps({"answer": ""}), json.dumps({"text": ""}),
                    json.dumps({"answer": "", "text": " \n"})]
        initial_responses = [fixtures.response(content) for content in contents]
        initial_responses.append(TransportResponse(200, json.dumps({"choices": [{
            "message": {"role": "assistant"}, "finish_reason": "stop"}]})))
        for initial in initial_responses:
            with self.subTest(initial=initial.body):
                output = self.ask(lambda turn, payload: initial if turn == 1 else self.grounded(payload))
                self.assertEqual(output["runner_status"], "COMPLETED")
                self.assertEqual(len(self.requests), 2)
                self.assert_recovery_accounting()
                self.assertTrue(self.result["model_answer_recorded"])
                self.assertFalse(self.result["answer_truncated"])
                self.assertEqual(self.requests[0]["source_context"], self.requests[1]["source_context"])
                self.assertEqual(self.envelopes[0]["messages"][:-1], self.envelopes[1]["messages"][:-1])

    def test_empty_discovery_cannot_recover_without_business_source(self):
        self.ask(lambda *_: fixtures.response(""), question="How is a shipping schedule allocated?")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["source_context"][0]["pages"], [])
        self.assertEqual(self.trace["rounds"][0]["stage"], "discover")
        self.assertFalse(self.result["metrics"]["empty_stop_recovery_attempted"])

    def test_source_metadata_without_text_does_not_authorize_empty_stop_recovery(self):
        fit = business_chat._fit_request

        def metadata_only(config, payload, *args, **kwargs):
            for bundle in payload["source_context"]:
                for page in bundle.get("pages", []):
                    page["source_text"] = ""
            payload["framework_references"] = []
            return fit(config, payload, *args, **kwargs)

        with mock.patch.object(business_chat, "_fit_request", side_effect=metadata_only):
            self.ask(lambda *_: fixtures.response(""))
        self.assertTrue(self.requests[0]["source_context"][0]["pages"])
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.result["metrics"]["empty_stop_recovery_attempted"])

    def test_recovery_transport_failure_records_two_attempts_without_repeating_unknown_result(self):
        def respond(turn, _):
            if turn == 1:
                return fixtures.response("")
            raise APIClientError("REQUEST_TIMEOUT")
        output = self.ask(respond)
        self.assertEqual(len(self.requests), 2)
        self.assert_recovery_accounting()
        self.assertEqual(output["reason_code"], "REQUEST_TIMEOUT")
        self.assertEqual([row["response"]["error"] for row in self.trace["rounds"]],
                         ["MODEL_TEXT_EMPTY", "REQUEST_TIMEOUT"])

    def test_recovery_tool_request_is_not_executed_or_reprompted(self):
        self.ask(lambda turn, _: fixtures.response("") if turn == 1 else
                 fixtures.response('{"search":["UNNEEDED-LOOKUP"]}'))
        self.assertEqual(len(self.requests), 2)
        self.assert_recovery_accounting()
        self.assertNotIn("UNNEEDED-LOOKUP", json.dumps(self.result["tool_trace"]))

    def test_unknown_request_result_is_not_retried_when_same_question_is_reopened(self):
        def timeout(*_):
            raise APIClientError("REQUEST_TIMEOUT")
        self.ask(timeout, conversation_id="unknown-result-scope")
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.result["metrics"]["empty_stop_recovery_attempted"])
        self.ask(lambda *_: self.fail("An unknown prior result must not send another request."),
                 conversation_id="unknown-result-scope")
        self.assertEqual(len(self.requests), 0)
        self.assertFalse(self.result["metrics"]["empty_stop_recovery_attempted"])


if __name__ == "__main__":
    unittest.main()
