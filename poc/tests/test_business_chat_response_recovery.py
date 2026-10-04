"""Offline regressions for usable answers and safe response failures."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
import business_chat
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import APIClientError, CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


def completion(content=None, *, finish_reason="stop", **message_fields):
    return TransportResponse(200, json.dumps({"choices": [{"message": {
        "role": "assistant", "content": content, **message_fields},
        "finish_reason": finish_reason}]}, ensure_ascii=False))


class BusinessChatResponseRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
                                       api_key="offline-neutral-credential")
        (self.source / "rule.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. FINAL-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 BASIS PIC S9(5) VALUE 8.\n"
            "01 FACTOR PIC 9V99 VALUE 1.25.\n"
            "01 FINAL-AMOUNT PIC 9(7)V99.\n"
            "PROCEDURE DIVISION.\nMAIN.\n"
            "IF BASIS > ZERO\nCOMPUTE FINAL-AMOUNT = BASIS * FACTOR\n"
            "ELSE\nMOVE ZERO TO FINAL-AMOUNT\nEND-IF.\nGOBACK.\n",
            encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.requests = []
        self.provider_echo = "PRIVATE_REMOTE_FAILURE_MARKER"

    def ask(self, respond):
        self.requests.clear()

        def transport(request):
            envelope = json.loads(request.body)
            self.assertNotIn("tools", envelope)
            self.assertNotIn("stream", envelope)
            payload = json.loads(envelope["messages"][-1]["content"])
            self.requests.append(payload)
            pages = [page for bundle in payload["source_context"] for page in bundle["pages"]]
            self.assertTrue(any("COMPUTE FINAL-AMOUNT = BASIS * FACTOR" in page["source_text"]
                                for page in pages))
            return respond(payload)

        return run_business_chat("FINAL-AMOUNT 怎么计算？", self.database, self.source,
            self.config, transport=transport, framework_reference_path="",
            policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))

    def assert_answer(self, output, expected):
        result = output["agent_result"]
        self.assertEqual(result["answer"], expected)
        self.assertTrue(result["model_answer_recorded"])
        self.assertIn(result["status"], {"ANALYZED", "PARTIAL"})
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["metrics"]["model_requests"], 1)

    def assert_abstained(self, output):
        result = output["agent_result"]
        self.assertEqual(result["status"], "ABSTAINED")
        self.assertFalse(result["model_answer_recorded"])
        self.assertEqual(output["runner_status"], "NOT_READY")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["metrics"]["model_requests"], 1)
        self.assertNotEqual(result["stop_reason"], "sufficient_material")
        return result

    def assert_safe_failure(self, output, code, stage, *, http_status=None):
        result = self.assert_abstained(output)
        self.assertNotIn("诊断：", result["answer"])
        self.assertLessEqual(len(result["answer"]), 50)
        self.assertEqual(output["reason_code"], code)
        error = next(item for item in result["diagnostics"] if item["code"] == code)
        self.assertEqual(error["stage"], stage)
        if http_status is not None:
            self.assertEqual(error["http_status"], http_status)
        surface = json.dumps({"answer": result["answer"], "diagnostics": result["diagnostics"],
                              "reason_code": output["reason_code"]}, ensure_ascii=False)
        for forbidden in (self.provider_echo, self.config.api_key, self.config.base_url,
                          self.config.chat_model):
            self.assertNotIn(forbidden, surface)
        return error

    def draft_followup(self, respond):
        """Create a real bounded reading gap before accepting the first draft."""
        (self.source / "rule.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. FINAL-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 BASIS PIC 9(5).\n01 FACTOR PIC 9V99 VALUE 1.25.\n"
            "01 FINAL-AMOUNT PIC 9(7)V99.\nPROCEDURE DIVISION.\nMAIN.\n"
            + "DISPLAY 'UNCHANGED'.\n" * 700
            + "MOVE 10 TO BASIS.\nIF BASIS > ZERO\n"
            "COMPUTE FINAL-AMOUNT = BASIS * FACTOR\nELSE\n"
            "MOVE ZERO TO FINAL-AMOUNT\nEND-IF.\nGOBACK.\n", encoding="utf-8")
        self.database.unlink()
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        original_map = business_chat.build_business_map

        def limited_map(*args, **kwargs):
            return {**original_map(*args, **kwargs), "spotlights": [],
                    "rule_leads": [], "semantic_anchors": []}

        self.requests.clear()
        draft = []

        def transport(request):
            envelope = json.loads(request.body)
            self.assertNotIn("tools", envelope)
            payload = json.loads(envelope["messages"][-1]["content"])
            self.requests.append(payload)
            if len(self.requests) == 1:
                page = payload["source_context"][0]["pages"][0]
                text = f"已知本程序处理金额，公式仍待补充。[{page['evidence_id']}]"
                draft.append(text)
                return completion(text)
            return respond(payload)

        with mock.patch.object(business_chat, "build_business_map", side_effect=limited_map):
            output = run_business_chat("rule.cbl 的金额怎么计算？", self.database, self.source,
                self.config, transport=transport, framework_reference_path="",
                policy=AgentPolicy(max_model_requests=3, max_answer_revisions=0,
                                   max_source_characters=512,
                                   initial_pages=1, initial_source_characters=512))
        return output, draft[0]

    def assert_retained_draft(self, output, draft, code, stage, *, http_status=None):
        result = output["agent_result"]
        self.assertEqual(result["answer"], draft)
        self.assertEqual(result["narrative"]["text"], draft)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertTrue(result["model_answer_recorded"])
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertEqual(output["reason_code"], code)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["metrics"]["model_requests"], 2)
        first_pages = self.requests[0]["source_context"][0]["pages"]
        later_pages = self.requests[1]["source_context"][0]["pages"]
        self.assertFalse(any("COMPUTE FINAL-AMOUNT" in page["source_text"] for page in first_pages))
        self.assertTrue(any("COMPUTE FINAL-AMOUNT" in page["source_text"] for page in later_pages))
        first_ids = {page["evidence_id"] for page in first_pages}
        later_ids = {page["evidence_id"] for page in later_pages}
        self.assertTrue(later_ids - first_ids)
        self.assertEqual({ref["evidence_id"] for ref in result["evidence_refs"]}, first_ids)
        citation = result["narrative"]["citations"][0]
        supplied = next(page for page in first_pages if page["evidence_id"] == citation["evidence_id"])
        for key in ("relative_path", "start_line", "end_line", "source_sha256"):
            self.assertEqual(citation[key], supplied[key])
        quality = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text(encoding="utf-8"))
        self.assertEqual(quality["final"]["final_answer_round_id"], "round-1")
        self.assertIn("usable_draft_retained", {item["reason"] for item in result["boundaries"]})
        error = next(item for item in result["diagnostics"] if item["code"] == code)
        self.assertEqual(error["stage"], stage)
        if http_status is not None:
            self.assertEqual(error["http_status"], http_status)
        surface = json.dumps({"answer": result["answer"], "diagnostics": result["diagnostics"],
                              "boundaries": result["boundaries"]}, ensure_ascii=False)
        for forbidden in (self.provider_echo, self.config.api_key, self.config.base_url,
                          self.config.chat_model):
            self.assertNotIn(forbidden, surface)

    def test_complete_business_openings_without_citations_survive_one_request(self):
        answers = (
            "先读取参数，再按基础金额乘系数计算，负值时结果归零。",
            "需要先确认条件：金额大于零时乘以费率，否则归零。",
            "First read the parameters, then multiply the base amount by the rate.",
        )
        for text in answers:
            with self.subTest(text=text):
                output = self.ask(lambda payload: completion(text))
                self.assert_answer(output, text)
                self.assertEqual(output["agent_result"]["narrative"]["citations"], [])

    def test_nested_business_action_fields_and_mixed_result_search_stay_answers(self):
        objects = (
            {"result": {"action": "基础金额乘系数，负值归零"}},
            {"result": {"actions": ["核对金额条件", "按系数计算结果"]}},
            {"result": "基础金额大于零时乘以系数，否则归零", "search": ["FACTOR"]},
        )
        for value in objects:
            with self.subTest(value=value):
                text = json.dumps(value, ensure_ascii=False)
                output = self.ask(lambda payload: completion(text))
                self.assert_answer(output, text)
                self.assertEqual(output["agent_result"]["metrics"]["tool_calls"]["search"], 0)

    def test_citation_first_answer_retains_body_and_real_source_reference(self):
        expected = []

        def respond(payload):
            page = next(page for bundle in payload["source_context"] for page in bundle["pages"]
                        if "COMPUTE FINAL-AMOUNT" in page["source_text"])
            text = f"[{page['evidence_id']}] 先读取参数，再按基础金额乘系数计算，负值时结果归零。"
            expected.append((text, page["evidence_id"]))
            return completion(text)

        output = self.ask(respond)
        self.assert_answer(output, expected[0][0])
        citations = output["agent_result"]["narrative"]["citations"]
        self.assertEqual([item["evidence_id"] for item in citations], [expected[0][1]])
        self.assertEqual(citations[0]["relative_path"], "rule.cbl")

    def test_pure_investigation_plans_do_not_become_answers(self):
        for text in ("需要先找公式和条件才能解释。", "先读取源码，确认公式后才能回答。",
                     "我需要先读取源码确认条件。",
                     "I need to read the source and locate the formula before I can answer."):
            with self.subTest(text=text):
                result = self.assert_abstained(self.ask(lambda payload: completion(text)))
                self.assertNotEqual(result["answer"], text)

    def test_malformed_and_empty_investigation_requests_do_not_become_answers(self):
        for text in ('{"search": [', '{"search":[]}', '{"read":[]}',
                     '{"framework_search":[]}', '{"tool":"scan_repository","arguments":{}}'):
            with self.subTest(text=text):
                output = self.ask(lambda payload: completion(text))
                self.assert_safe_failure(output, "INVALID_INVESTIGATION_ACTION", "response_validation")

    def test_first_provider_error_and_action_json_are_not_recorded(self):
        objects = (
            ({"error": {"message": self.provider_echo}}, "MODEL_ERROR_RESPONSE"),
            ({"action": "search_code", "arguments": {"query": self.provider_echo}}, "MODEL_ACTION_RESPONSE"),
            ({"tool_calls": [{"name": "search_code", "arguments": self.provider_echo}]}, "MODEL_ACTION_RESPONSE"),
        )
        for value, code in objects:
            with self.subTest(code=code, value=value):
                text = json.dumps(value)
                output = self.ask(lambda payload: completion(text))
                self.assert_safe_failure(output, code, "response_parse")

    def test_native_tool_calls_only_report_safe_response_shape(self):
        output = self.ask(lambda payload: completion(None, finish_reason="tool_calls", tool_calls=[{
            "id": "neutral-call", "type": "function", "function": {
                "name": "search_code", "arguments": json.dumps({"query": self.provider_echo})}}]))
        error = self.assert_safe_failure(output, "MODEL_TEXT_EMPTY", "response_parse")
        shape = error["response_shape"]
        self.assertEqual(shape["finish_reason"], "tool_calls")
        self.assertEqual(shape["content_shape"], "null")
        self.assertTrue(shape["tool_calls_present"])
        self.assertEqual(shape["choice_count"], 1)

    def test_token_limit_without_final_content_reports_reasoning_presence(self):
        for content in (None, ""):
            with self.subTest(content=content):
                output = self.ask(lambda payload: completion(content, finish_reason="length",
                                                             reasoning_content=self.provider_echo))
                error = self.assert_safe_failure(output, "MODEL_TEXT_EMPTY", "response_parse")
                shape = error["response_shape"]
                self.assertEqual(shape["finish_reason"], "length")
                self.assertEqual(shape["content_shape"], "null" if content is None else "string")
                self.assertTrue(shape["reasoning_present"])
                self.assertFalse(shape["tool_calls_present"])
                self.assertEqual(shape["choice_count"], 1)

    def test_unknown_finish_reason_cannot_echo_remote_metadata(self):
        output = self.ask(lambda payload: completion(None, finish_reason=self.provider_echo))
        error = self.assert_safe_failure(output, "MODEL_TEXT_EMPTY", "response_parse")
        self.assertEqual(error["response_shape"]["finish_reason"], "other")

    def test_http_failures_report_status_without_provider_echo(self):
        remote_body = json.dumps({"error": {"message": self.provider_echo,
            "credential": self.config.api_key, "url": self.config.base_url}})
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                output = self.ask(lambda payload: TransportResponse(status, remote_body))
                self.assert_safe_failure(output, "HTTP_ERROR", "provider_request", http_status=status)

    def test_transport_failure_does_not_echo_exception_details(self):
        def respond(payload):
            raise RuntimeError(self.provider_echo + " " + self.config.api_key + " " + self.config.base_url)

        output = self.ask(respond)
        self.assert_safe_failure(output, "TRANSPORT_ERROR", "provider_request")

    def test_classified_transport_failure_preserves_specific_safe_diagnosis(self):
        for reason, text in (("dns_resolution_failed", "找不到接口地址"),
                             ("tls_certificate_invalid", "连接验证失败"),
                             ("tls_handshake_failed", "加密连接失败"),
                             ("connection_refused", "接口拒绝连接"),
                             ("connection_reset", "接口连接中断"),
                             ("network_unreachable", "无法到达接口网络")):
            with self.subTest(reason=reason):
                def respond(payload):
                    raise APIClientError("TRANSPORT_ERROR", transport_reason=reason)

                output = self.ask(respond)
                error = self.assert_safe_failure(output, "TRANSPORT_ERROR", "provider_request")
                self.assertEqual(error["transport_reason"], reason)
                result = output["agent_result"]
                self.assertIn(text, result["answer"])
                trace = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text())
                self.assertEqual(trace["rounds"][0]["response"]["transport_reason"], reason)

    def test_invalid_json_response_reports_safe_provider_failure(self):
        output = self.ask(lambda payload: TransportResponse(200, self.provider_echo))
        self.assert_safe_failure(output, "INVALID_JSON_RESPONSE", "provider_request", http_status=200)

    def test_draft_survives_provider_error_and_action_envelopes(self):
        cases = (
            ({"error": {"message": self.provider_echo}}, "MODEL_ERROR_RESPONSE"),
            ({"action": "search_code", "arguments": {"query": self.provider_echo}}, "MODEL_ACTION_RESPONSE"),
        )
        for value, code in cases:
            with self.subTest(code=code):
                output, draft = self.draft_followup(lambda payload: completion(json.dumps(value)))
                self.assert_retained_draft(output, draft, code, "response_parse")

    def test_draft_survives_refusal_filter_and_empty_content(self):
        cases = (
            (completion(self.provider_echo, refusal=self.provider_echo), "MODEL_REFUSED", "response_validation"),
            (completion(self.provider_echo, finish_reason="content_filter"), "MODEL_CONTENT_FILTERED", "response_validation"),
            (completion(""), "MODEL_TEXT_EMPTY", "response_parse"),
        )
        for reply, code, stage in cases:
            with self.subTest(code=code):
                output, draft = self.draft_followup(lambda payload: reply)
                self.assert_retained_draft(output, draft, code, stage)

    def test_draft_survives_http_failure(self):
        body = json.dumps({"error": {"message": self.provider_echo,
                           "credential": self.config.api_key, "url": self.config.base_url}})
        for status in (401, 403, 429):
            with self.subTest(status=status):
                output, draft = self.draft_followup(lambda payload: TransportResponse(status, body))
                self.assert_retained_draft(output, draft, "HTTP_ERROR", "provider_request", http_status=status)


if __name__ == "__main__":
    unittest.main()
