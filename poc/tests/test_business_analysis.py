from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from business_analysis import run_business_analysis  # noqa: E402
from answer_markdown import BUSINESS_ANSWER_POLICY
from company_api import CompanyAPIConfig, MAX_REQUEST_BYTES, TransportResponse  # noqa: E402
from structural_index import build_structural_index  # noqa: E402


SOURCE = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. PREMIUM-CALC.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01 BASE-AMOUNT PIC 9(7)V99 VALUE 100.
       01 PREMIUM PIC 9(7)V99.
       PROCEDURE DIVISION.
       CALCULATE-PREMIUM.
           COMPUTE PREMIUM = BASE-AMOUNT * 0.05
           DISPLAY PREMIUM
           GOBACK.
"""


def config() -> CompanyAPIConfig:
    return CompanyAPIConfig(base_url="https://service.example/v1", chat_model="test-text-model",
                            api_key="test-business-secret")


def response(content: object, *, finish_reason: str = "stop") -> TransportResponse:
    return TransportResponse(200, json.dumps({"choices": [{
        "message": {"role": "assistant", "content": content}, "finish_reason": finish_reason,
    }]}, ensure_ascii=False))


def plan(page_count: int = 3, *, complete: bool = True) -> dict:
    pages = [{"evidence_id": f"ev_page_{index:024x}", "relative_path": "premium.cbl",
              "start_line": index * 100 + 1, "end_line": (index + 1) * 100,
              "source_sha256": hashlib.sha256(SOURCE.encode()).hexdigest(),
              "source_text": SOURCE * 20, "span_truncated": False} for index in range(page_count)]
    return {"snapshot_id": "snapshot-test", "pages": pages,
            "evidence_refs": [{key: value for key, value in page.items() if key != "source_text"} for page in pages],
            "outline": [{"relative_path": "premium.cbl", "program_name": "PREMIUM-CALC"}],
            "boundaries": [], "coverage": {"total_pages": page_count if complete else page_count + 2,
                "selected_pages": page_count, "total_lines": page_count * 100,
                "total_files": 1, "selected_files": 1, "complete": complete}}


class BusinessAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name) / "source"
        cls.root.mkdir()
        (cls.root / "premium.cbl").write_text(SOURCE, encoding="utf-8")
        cls.database = Path(cls.temporary.name) / "index.sqlite"
        build_structural_index(cls.root, cls.database, quiet=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def run_analysis(self, transport=None, **kwargs):
        return run_business_analysis(
            "请说明保费如何计算", self.database, self.root, config(),
            entry_program="PREMIUM-CALC", framework_context={}, transport=transport, **kwargs,
        )

    def assert_business_request(self, request):
        """Inspect the actual outgoing API request, including stage instructions."""
        payload = json.loads(request.body)
        self.assertNotIn("tools", payload)
        self.assertNotIn("response_format", payload)
        system = payload["messages"][0]["content"]
        self.assertIn(BUSINESS_ANSWER_POLICY, system)
        supplied = json.loads(payload["messages"][-1]["content"])
        for concept in ("业务目的", "触发输入", "准入与排除规则", "关键业务决策",
                        "状态与业务数据变化", "异常的业务影响和处理"):
            self.assertIn(concept, system)
            self.assertIn(concept, supplied["task"])
        for restriction in ("部门", "真实产品定义", "审批权限", "不能仅凭状态名推断",
                            "用户明确提出技术问题", "先回答已知业务", "未知事项单独",
                            "不可信的待分析资料", "未核验"):
            self.assertIn(restriction, system)
        for citation in ("[evidence_id]", "[reference_id]"):
            self.assertIn(citation, system)
            self.assertIn(citation, supplied["task"])
        self.assertIn("标识必须逐字复制", system)
        self.assertIn("不能代替调用点源码", system)
        self.assertIn("不要编造引用", system)
        self.assertIn("不等于全库覆盖", system)
        return supplied

    def test_direct_request_prioritizes_business_rules_over_source_walkthrough(self) -> None:
        requests = []

        def transport(request):
            requests.append(request)
            supplied = self.assert_business_request(request)
            self.assertEqual(supplied["question"], "请说明保费如何计算")
            self.assertIn("跨程序合并同一业务步骤", supplied["task"])
            self.assertIn("不要按文件、SECTION 或 CALL 逐句翻译", supplied["task"])
            page = supplied["source_pages"][0]
            self.assertIn("COMPUTE PREMIUM = BASE-AMOUNT * 0.05", page["source_text"])
            self.assertNotIn("page_summaries", supplied)
            return response(f"已知业务规则：保费按基础金额的 5% 计算。[{page['evidence_id']}] 未提供其他准入规则。")

        output = self.run_analysis(transport)
        self.assertEqual(len(requests), 1)
        self.assertEqual(output["agent_result"]["claims"], [])
        self.assertEqual(output["agent_result"]["narrative"]["verification"], "unverified")
        self.assertEqual(len(output["agent_result"]["narrative"]["citations"]), 1)

    def test_page_and_synthesis_requests_keep_business_process_and_source_constraints(self) -> None:
        prepared = plan(3)
        for index, page in enumerate(prepared["pages"]):
            page["relative_path"] = f"business-step-{index + 1}.cbl"
        expected_ids = {page["evidence_id"] for page in prepared["pages"]}
        page_requests = []
        synthesis_requests = []

        def transport(request):
            supplied = self.assert_business_request(request)
            if supplied["source_pages"]:
                page_requests.append(supplied)
                self.assertEqual(len(supplied["source_pages"]), 1)
                self.assertIn("为跨程序业务分析提取本页可支持的业务事实", supplied["task"])
                self.assertIn("不能声称读过其他源码页", supplied["task"])
                self.assertIn("未知事项单独保留", supplied["task"])
                self.assertNotIn("page_summaries", supplied)
                evidence_id = supplied["source_pages"][0]["evidence_id"]
                return response(f"业务计算规则：保费为基础金额的 5%。[{evidence_id}] 其他业务条件未提供。")
            synthesis_requests.append(supplied)
            self.assertIn("跨程序整合成连贯的业务过程", supplied["task"])
            self.assertIn("不要强行连接独立流程", supplied["task"])
            self.assertIn("不要按文件、SECTION、CALL 或页码逐项翻译", supplied["task"])
            self.assertIn("框架用于理解行为", supplied["task"])
            self.assertIn("未知事项单独说明其影响", supplied["task"])
            self.assertEqual({item["evidence_id"] for item in supplied["page_summaries"]}, expected_ids)
            self.assertTrue(all(item["evidence_id"] in item["text"] for item in supplied["page_summaries"]))
            first_id = prepared["pages"][0]["evidence_id"]
            return response(f"业务处理以基础金额为输入，按 5% 计算保费并输出结果。[{first_id}] 未知事项：没有其他准入规则资料。")

        with mock.patch("business_analysis.prepare_source_reading", return_value=prepared):
            output = self.run_analysis(transport)
        self.assertEqual(len(page_requests), 3)
        self.assertEqual(len(synthesis_requests), 1)
        self.assertEqual(output["agent_result"]["model_turns"], 4)
        self.assertEqual(output["agent_result"]["reading_coverage"]["summarized_pages"], 3)
        self.assertEqual(output["agent_result"]["narrative"]["verification"], "unverified")
        self.assertEqual(output["agent_result"]["claims"], [])

    def test_real_small_program_explained_in_one_plain_chat_request(self) -> None:
        requests = []

        def transport(request):
            requests.append(request)
            payload = json.loads(request.body)
            self.assertNotIn("tools", payload)
            self.assertNotIn("response_format", payload)
            supplied = json.loads(payload["messages"][-1]["content"])
            page = supplied["source_pages"][0]
            self.assertIn("COMPUTE PREMIUM", page["source_text"])
            return response(f"程序将基础金额乘以 5% 计算保费，并输出计算结果。[{page['evidence_id']}]")

        output = self.run_analysis(transport, capture_api_responses=True)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].endpoint, "chat/completions")
        self.assertEqual(output["runner_status"], "COMPLETED")
        result = output["agent_result"]
        self.assertEqual(result["status"], "ANALYZED")
        self.assertIn("基础金额乘以 5%", result["answer"])
        self.assertEqual(result["narrative"]["verification"], "unverified")
        self.assertEqual(result["claims"], [])
        self.assertFalse(result["claims_semantically_verified"])
        self.assertEqual(len(result["narrative"]["citations"]), 1)
        self.assertEqual(result["reading_coverage"]["sent_pages"], 1)
        self.assertEqual(result["reading_coverage"]["summarized_pages"], 1)
        self.assertTrue(result["reading_coverage"]["complete"])
        self.assertEqual(output["api_diagnostics"]["request_count"], 1)

    def test_supported_text_wrappers_and_content_arrays(self) -> None:
        variants = (
            "```json\n{\"answer\":\"业务正文\"}\n```",
            '{"content":"业务正文"}', '{"text":"业务正文"}',
            [{"type": "text", "text": "业务"}, {"type": "text", "text": "正文"}],
            '{"answer":[{"type":"text","text":"业务正文"}]}',
        )
        for value in variants:
            with self.subTest(value=value):
                output = self.run_analysis(lambda request: response(value))
                self.assertEqual(output["agent_result"]["status"], "ANALYZED")
                self.assertEqual(output["agent_result"]["answer"].replace("\n", ""), "业务正文")

    def test_empty_reply_reports_explicit_failure(self) -> None:
        for content, expected in (("", "MODEL_TEXT_EMPTY"), ('{"answer":""}', "MODEL_TEXT_EMPTY")):
            with self.subTest(content=content):
                output = self.run_analysis(lambda request: response(content), capture_api_responses=True)
                self.assertEqual(output["runner_status"], "SAFE_STOP")
                self.assertEqual(output["agent_result"]["status"], "ABSTAINED")
                self.assertEqual(output["agent_result"]["diagnostics"][0]["code"], expected)
                self.assertEqual(output["api_diagnostics"]["request_count"], 1)
                self.assertEqual(output["agent_result"]["automatic_retries"], 0)
                self.assertEqual(output["agent_result"]["reading_coverage"]["sent_pages"], 1)

    def test_structured_business_reply_is_preserved_without_guessing_fields(self) -> None:
        content = '{"business_purpose":"计算保费","rules":["基础金额乘以5%"]}'
        output = self.run_analysis(lambda request: response(content), capture_api_responses=True)
        result = output["agent_result"]
        self.assertEqual(result["answer"], content)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["narrative"]["verification"], "unverified")
        self.assertEqual(result["automatic_retries"], 0)
        self.assertEqual(output["api_diagnostics"]["request_count"], 1)
        self.assertIn("MODEL_STRUCTURED_TEXT_PRESERVED", {item["code"] for item in result["diagnostics"]})
        self.assertEqual(result["claims"], [])

    def test_nested_business_wrapper_is_preserved_but_empty_wrappers_still_fail(self) -> None:
        for content in ('{"answer":{"rules":["基础金额乘以5%"]}}', '{"content":["业务条目"]}'):
            with self.subTest(content=content):
                result = self.run_analysis(lambda request: response(content))["agent_result"]
                self.assertEqual(result["answer"], content)
                self.assertEqual(result["status"], "PARTIAL")
                self.assertEqual(result["automatic_retries"], 0)
        for content in ('{"answer":{}}', '{"content":[]}', '{"answer":null}'):
            with self.subTest(content=content):
                self.assertEqual(self.run_analysis(lambda request: response(content))["agent_result"]["status"], "ABSTAINED")

    def full_chain_plan(self, count=145):
        prepared = plan(count)
        for index, page in enumerate(prepared["pages"]):
            page["relative_path"] = f"worker-{index % 10:02d}.cbl"
            page["source_text"] = f"COMPUTE CHARGE-{index} = AMOUNT * 0.03."
        prepared["outline"] = [{"relative_path": f"worker-{index:02d}.cbl", "program_names": [f"WORKER-{index:02d}"]}
                               for index in range(10)]
        return prepared

    def test_full_chain_exceeds_128_pages_with_bounded_hierarchical_requests(self) -> None:
        prepared = self.full_chain_plan()
        requests, delivered, events = [], [], []

        def transport(request):
            requests.append(request)
            self.assertLess(len(request.body), MAX_REQUEST_BYTES)
            supplied = self.assert_business_request(request)
            if supplied["source_pages"]:
                page = supplied["source_pages"][0]
                delivered.append(page["evidence_id"])
                if supplied.get("page_summaries"):
                    self.assertTrue(all(item["relative_path"] == page["relative_path"] for item in supplied["page_summaries"]))
                return response(f"业务费用按金额的3%计算，规则页{len(delivered)}。[{page['evidence_id']}]")
            self.assertLessEqual(len(supplied["page_summaries"]), 8)
            self.assertLessEqual(sum(len(item["text"]) for item in supplied["page_summaries"]), 8 * 3000)
            return response("业务过程汇总：" + " ".join(item["text"] for item in supplied["page_summaries"])[:2200])

        with mock.patch("business_analysis.prepare_source_reading", return_value=prepared):
            output = self.run_analysis(transport, reading_strategy="full_chain", max_pages=2, progress=events.append)
        result = output["agent_result"]
        self.assertEqual(len(delivered), 145)
        self.assertEqual(len(set(delivered)), 145)
        self.assertEqual(result["reading_coverage"]["summarized_pages"], 145)
        self.assertEqual(result["reading_coverage"]["completed_batches"], 73)
        self.assertEqual(result["reading_coverage"]["unattempted_pages"], 0)
        self.assertTrue(result["reading_coverage"]["complete"])
        self.assertEqual(len(result["program_summaries"]), 10)
        self.assertTrue(all(item["text"] and item["complete"] for item in result["program_summaries"]))
        self.assertEqual(sum(item["summarized_pages"] for item in result["program_summaries"]), 145)
        self.assertTrue(any(event["completed"] == 145 and event["total"] == 145 for event in events))
        self.assertEqual(len(result["page_summaries"]), 145)
        self.assertGreater(len(requests), 145)
        self.assertEqual(result["status"], "ANALYZED")

    def test_full_chain_late_page_failure_preserves_prior_explanations_without_extra_requests(self) -> None:
        prepared = self.full_chain_plan(35)
        failed_id = prepared["pages"][25]["evidence_id"]
        delivered = []

        def transport(request):
            supplied = json.loads(json.loads(request.body)["messages"][-1]["content"])
            if supplied["source_pages"]:
                page = supplied["source_pages"][0]
                delivered.append(page["evidence_id"])
                if page["evidence_id"] == failed_id:
                    return TransportResponse(502, "temporary page failure")
                return response(f"已知业务规则来自 {page['relative_path']}。[{page['evidence_id']}]")
            return TransportResponse(503, "temporary synthesis failure")

        with mock.patch("business_analysis.prepare_source_reading", return_value=prepared):
            result = self.run_analysis(transport, reading_strategy="full_chain", max_pages=3)["agent_result"]
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["reading_coverage"]["summarized_pages"], 25)
        self.assertEqual(result["reading_coverage"]["sent_pages"], 26)
        self.assertEqual(result["reading_coverage"]["unattempted_pages"], 9)
        self.assertEqual(result["automatic_retries"], 0)
        self.assertEqual(result["model_turns"], 26)
        self.assertEqual(len(result["page_summaries"]), 25)
        self.assertNotIn(prepared["pages"][-1]["evidence_id"], {item["evidence_id"] for item in result["page_summaries"]})
        self.assertTrue(all(item["text"] for item in result["program_summaries"] if item["summarized_pages"]))
        self.assertIn("已完成的程序业务解释", result["answer"])

    def test_full_chain_stops_on_authentication_or_refusal_and_keeps_prior_result(self) -> None:
        for failure in (TransportResponse(401, "unauthorized"), TransportResponse(403, "forbidden"),
                        TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": None, "refusal": "cannot continue"}}]}))):
            with self.subTest(failure=failure):
                prepared = self.full_chain_plan()
                requests = []
                def transport(request):
                    requests.append(request)
                    if len(requests) == 1:
                        return response(f"已知业务规则保留。[{prepared['pages'][0]['evidence_id']}]")
                    return failure
                with mock.patch("business_analysis.prepare_source_reading", return_value=prepared):
                    result = self.run_analysis(transport, reading_strategy="full_chain", max_pages=2)["agent_result"]
                self.assertEqual(len(requests), 2)
                self.assertEqual(result["status"], "PARTIAL")
                self.assertEqual(result["reading_coverage"]["sent_pages"], 2)
                self.assertEqual(result["reading_coverage"]["unattempted_pages"], 143)
                self.assertEqual(result["reading_coverage"]["summarized_pages"], 1)
                self.assertIn("已知业务规则保留", result["answer"])
                self.assertEqual(len(result["page_summaries"]), 1)

    def test_error_objects_are_omitted_and_old_action_objects_remain_unaccepted(self) -> None:
        for content, code in (
            ('{"error":{"message":"backend unavailable"}}', "MODEL_ERROR_RESPONSE"),
            ('{"action":"search_code","arguments":{"query":"PREMIUM"}}', "MODEL_ACTION_RESPONSE"),
            ('{"tool_calls":[{"name":"search_code"}]}', "MODEL_ACTION_RESPONSE"),
        ):
            with self.subTest(code=code):
                output = self.run_analysis(lambda request: response(content), capture_api_responses=True)
                self.assertEqual(output["runner_status"], "SAFE_STOP")
                self.assertEqual(output["agent_result"]["status"], "ABSTAINED")
                self.assertFalse(output["agent_result"]["model_answer_recorded"])
                if code == "MODEL_ERROR_RESPONSE":
                    self.assertNotIn("unaccepted_response", output)
                    self.assertEqual(output["api_diagnostics"]["exchanges"][0]["body_text"], "")
                    self.assertNotIn(content, json.dumps(output))
                else:
                    self.assertEqual(output["unaccepted_response"], {"reason_code": code, "text": content})
                self.assertEqual(output["api_diagnostics"]["request_count"], 1)

    def test_nonempty_later_choice_is_used_without_retrying_an_empty_first_choice(self) -> None:
        body = {"choices": [{"message": {"role": "assistant", "content": ""}},
                            {"message": {"role": "assistant", "content": "保费按基础金额的5%计算。"}}]}
        output = self.run_analysis(lambda request: TransportResponse(200, json.dumps(body)), capture_api_responses=True)
        self.assertEqual(output["agent_result"]["answer"], "保费按基础金额的5%计算。")
        self.assertEqual(output["agent_result"]["status"], "ANALYZED")
        self.assertEqual(output["api_diagnostics"]["request_count"], 1)
        self.assertIn("MODEL_NONEMPTY_CHOICE_SELECTED", {item["code"] for item in output["agent_result"]["diagnostics"]})

    def test_refusal_retains_partial_content_without_retry_or_false_completion(self) -> None:
        for content in ("已知规则是基础金额乘以5%，其他部分未返回。", None):
            body = {"choices": [{"message": {"role": "assistant", "content": content,
                                              "refusal": "不能继续提供其余内容。"}}]}
            with self.subTest(content=content):
                output = self.run_analysis(lambda request: TransportResponse(200, json.dumps(body)), capture_api_responses=True)
                result = output["agent_result"]
                self.assertEqual(output["api_diagnostics"]["request_count"], 1)
                self.assertEqual(result["automatic_retries"], 0)
                self.assertEqual(result["stop_reason"], "model_refused")
                self.assertFalse(result["reading_coverage"]["complete"])
                if content:
                    self.assertEqual(result["status"], "PARTIAL")
                    self.assertEqual(result["answer"], content)
                else:
                    self.assertEqual(result["status"], "ABSTAINED")
                    self.assertEqual(output["unaccepted_response"]["reason_code"], "MODEL_REFUSED")

    def test_refusal_in_first_choice_is_not_bypassed_using_another_choice(self) -> None:
        body = {"choices": [{"message": {"role": "assistant", "content": None, "refusal": "服务端拒绝。"}},
                            {"message": {"role": "assistant", "content": "不应借备用答案忽略拒绝。"}}]}
        output = self.run_analysis(lambda request: TransportResponse(200, json.dumps(body)), capture_api_responses=True)
        self.assertEqual(output["agent_result"]["status"], "ABSTAINED")
        self.assertNotIn("备用答案", output["agent_result"]["answer"])
        self.assertEqual(output["api_diagnostics"]["request_count"], 1)

    def test_scope_and_planned_call_chain_reach_every_request_without_rejecting_visible_source(self) -> None:
        prepared = plan(3)
        prepared["outline"].append({"relative_path": "excluded.cbl", "source_status": "excluded", "paragraphs": ["STALE-STRUCTURE"]})
        prepared["call_chain"] = {"scope": "reading_plan", "total_links": 1, "truncated": False, "links": [{
            "caller_path": "premium.cbl", "caller_program": "PREMIUM-CALC", "target_name": "EXTERNAL-CALC",
            "resolution": "unresolved", "target_source_status": "missing", "selection_complete": False,
        }]}
        scope = {"selected_file_count": 3, "max_files": 24, "selected_source_bytes": 1000,
                 "max_total_source_bytes": 16_000_000, "max_depth": 24, "complete_dependency_closure": False,
                 "boundaries": [{"relative_path": "premium.cbl", "target_name": f"TARGET-{index}",
                                 "status": "DYNAMIC_TARGET" if index == 0 else "FILE_LIMIT"} for index in range(60)]}
        calls = []

        def transport(request):
            calls.append(request)
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            sent_scope = payload["source_scope"]
            self.assertEqual(sent_scope["selected_file_count"], 3)
            self.assertEqual(sent_scope["max_files"], 24)
            self.assertEqual(sent_scope["selected_source_bytes"], 1000)
            self.assertEqual(sent_scope["max_total_source_bytes"], 16_000_000)
            self.assertFalse(sent_scope["complete_dependency_closure"])
            self.assertEqual(len(sent_scope["boundaries"]), 40)
            self.assertEqual(sent_scope["omitted_boundary_count"], 20)
            self.assertEqual(payload["call_chain"]["scope"], "reading_plan")
            self.assertFalse(payload["call_chain"]["runtime_paths_verified"])
            self.assertEqual(payload["call_chain"]["links"][0]["target_source_status"], "missing")
            self.assertNotIn("STALE-STRUCTURE", json.dumps(payload))
            return response("现有源码可确认基础金额乘以5%；外部计算实现未提供。")

        with mock.patch("business_analysis.prepare_source_reading", return_value=prepared):
            result = self.run_analysis(transport, analysis_scope=scope)["agent_result"]
        self.assertEqual(len(calls), 4)
        self.assertIn("现有源码可确认", result["answer"])
        self.assertEqual(result["reading_coverage"]["summarized_pages"], 3)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertTrue(result["reading_coverage"]["complete"])
        self.assertIn("SOURCE_SCOPE_INCOMPLETE", {item["reason"] for item in result["boundaries"]})

    def test_call_chain_gap_marks_partial_without_discarding_completed_source_explanation(self) -> None:
        prepared = plan(1)
        prepared["coverage"]["call_chain"] = {"resolved_calls": 2, "covered_calls": 1,
                                               "uncovered_calls": 1, "unresolved_calls": 3}
        with mock.patch("business_analysis.prepare_source_reading", return_value=prepared):
            result = self.run_analysis(lambda request: response("已读部分可计算保费，后续依赖仍待确认。"))["agent_result"]
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["answer"], "已读部分可计算保费，后续依赖仍待确认。")
        self.assertTrue(result["reading_coverage"]["complete"])
        boundary = next(item for item in result["boundaries"] if item["reason"] == "CALL_CHAIN_INCOMPLETE")
        self.assertEqual(boundary["uncovered_calls"], 1)
        self.assertEqual(boundary["unresolved_calls"], 3)

    def test_final_response_length_preserves_text_and_is_partial(self) -> None:
        output = self.run_analysis(lambda request: response("已有的业务解释", finish_reason="length"))
        result = output["agent_result"]
        self.assertEqual(result["answer"], "已有的业务解释")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["stop_reason"], "output_truncated")
        self.assertFalse(result["reading_coverage"]["complete"])
        self.assertEqual(len(result["reading_coverage"]["truncated_response_pages"]), 1)

    def test_filtered_output_keeps_partial_text_and_empty_filter_does_not_retry(self) -> None:
        for text in ("已返回的部分说明", ""):
            calls = []

            def transport(request):
                calls.append(request)
                return response(text, finish_reason="content_filter")

            with self.subTest(text=text):
                result = self.run_analysis(transport)["agent_result"]
                self.assertEqual(len(calls), 1)
                self.assertIn("MODEL_CONTENT_FILTERED", {item["code"] for item in result["diagnostics"]})
                if text:
                    self.assertEqual(result["status"], "PARTIAL")
                    self.assertEqual(result["stop_reason"], "model_response_filtered")
                    self.assertEqual(result["answer"], text)
                    self.assertFalse(result["reading_coverage"]["complete"])
                else:
                    self.assertEqual(result["status"], "ABSTAINED")

    def test_page_failure_stops_requests_and_keeps_successful_page_explanations(self) -> None:
        prepared = plan()
        requests = []

        def transport(request):
            requests.append(request)
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            pages = payload["source_pages"]
            if not pages:
                return TransportResponse(502, "summary gateway error")
            index = prepared["pages"].index(next(page for page in prepared["pages"] if page["evidence_id"] == pages[0]["evidence_id"]))
            if index == 1:
                raise TimeoutError("remote request detail must not escape")
            return response(f"第 {index + 1} 页解释：计算并输出保费。[{pages[0]['evidence_id']}]")

        with mock.patch("business_analysis.prepare_source_reading", return_value=prepared):
            output = self.run_analysis(transport, capture_api_responses=True)
        result = output["agent_result"]
        self.assertEqual(len(requests), 2)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("第 1 页解释", result["answer"])
        self.assertNotIn("第 3 页解释", result["answer"])
        self.assertNotIn("第 2 页解释", result["answer"])
        self.assertEqual(result["reading_coverage"]["sent_pages"], 2)
        self.assertEqual(result["reading_coverage"]["summarized_pages"], 1)
        self.assertEqual(result["reading_coverage"]["summarized_lines"], 100)
        self.assertEqual(result["reading_coverage"]["failed_pages"], 2)
        self.assertEqual(result["reading_coverage"]["unattempted_pages"], 1)
        self.assertEqual(len(result["evidence_refs"]), 2)
        self.assertEqual({item["code"] for item in result["diagnostics"]}, {"REQUEST_TIMEOUT", "MODEL_REQUESTS_STOPPED"})
        self.assertEqual(result["automatic_retries"], 0)
        self.assertEqual(len(result["page_summaries"]), 1)
        self.assertNotIn("remote request detail", json.dumps(output))

    def test_successful_synthesis_uses_bounded_summaries_without_source_accumulation(self) -> None:
        requests = []
        prepared = plan(48)

        def transport(request):
            requests.append(request)
            self.assertLessEqual(len(request.body), MAX_REQUEST_BYTES)
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            if payload["source_pages"]:
                self.assertEqual(len(payload["source_pages"]), 1)
                self.assertNotIn("page_summaries", payload)
                return response("保费计算的业务说明。" * 1_000)
            self.assertEqual(len(payload["page_summaries"]), 48)
            self.assertLessEqual(sum(len(item["text"]) for item in payload["page_summaries"]), 36_000)
            return response("综合解释：程序分阶段计算保费。")

        with mock.patch("business_analysis.prepare_source_reading", return_value=prepared):
            output = self.run_analysis(transport, max_pages=48)
        result = output["agent_result"]
        self.assertEqual(len(requests), 49)
        self.assertEqual(result["model_turns"], 49)
        self.assertEqual(result["status"], "ANALYZED")
        self.assertEqual(result["reading_coverage"]["summarized_pages"], 48)
        self.assertIn("综合解释", result["answer"])
        self.assertNotIn("api_diagnostics", output)

    def test_unknown_citations_removed_and_framework_source_is_only_reference(self) -> None:
        prepared = plan(1)
        source_id = prepared["pages"][0]["evidence_id"]
        framework = {"status": "LOADED", "references": [{
            "reference_id": "fw:123456789abcdef0:1-2", "heading": "Input processing",
            "page": 2, "text": "The initialization stage prepares input values.",
        }]}
        with mock.patch("business_analysis.prepare_source_reading", return_value=prepared):
            output = run_business_analysis(
                "说明输入处理", self.database, self.root, config(), framework_context=framework,
                transport=lambda request: response(f"已提供源码 [{source_id}] 与框架资料 [fw:123456789abcdef0:1-2]。"
                                                   "未知来源 [ev_missing]。"),
            )
        result = output["agent_result"]
        self.assertEqual(len(result["narrative"]["citations"]), 2)
        self.assertEqual(result["narrative"]["citation_scope"], "source_reference_only")
        self.assertNotIn("[ev_missing]", result["answer"])
        self.assertIn("未确认来源引用", result["answer"])
        self.assertIn("UNKNOWN_SOURCE_REFERENCE", {item["code"] for item in result["diagnostics"]})

    def test_cooperative_cancel_stops_before_next_request(self) -> None:
        class Cancelled(RuntimeError):
            pass

        requests = []
        progress_events = []

        def transport(request):
            requests.append(request)
            return response("第一页说明")

        def check_cancel():
            if requests:
                raise Cancelled()

        with mock.patch("business_analysis.prepare_source_reading", return_value=plan()):
            with self.assertRaises(Cancelled):
                self.run_analysis(transport, check_cancel=check_cancel, progress=progress_events.append)
        self.assertEqual(len(requests), 1)
        self.assertEqual(progress_events[0]["phase"], "reading_sources")
        self.assertEqual(progress_events[-1]["phase"], "analyzing_pages")

    def test_reading_selection_limit_is_partial_without_discarding_explanation(self) -> None:
        with mock.patch("business_analysis.prepare_source_reading", return_value=plan(1, complete=False)):
            output = self.run_analysis(lambda request: response("已选源码的业务解释"))
        self.assertEqual(output["agent_result"]["status"], "PARTIAL")
        self.assertEqual(output["agent_result"]["reading_coverage"]["total_pages"], 3)
        self.assertEqual(output["agent_result"]["reading_coverage"]["summarized_pages"], 1)
        self.assertIn("已选源码", output["agent_result"]["answer"])

    def test_no_network_and_source_failure_issue_no_requests(self) -> None:
        output = self.run_analysis(capture_api_responses=True)
        self.assertEqual(output["reason_code"], "NETWORK_DISABLED")
        self.assertEqual(output["api_diagnostics"]["exchanges"], [])
        with mock.patch("business_analysis.prepare_source_reading", side_effect=ValueError("SOURCE_HASH_MISMATCH")):
            failed = self.run_analysis(lambda request: self.fail("must not call API"), capture_api_responses=True)
        self.assertEqual(failed["reason_code"], "SOURCE_HASH_MISMATCH")
        self.assertEqual(failed["api_diagnostics"]["exchanges"], [])

    def test_configuration_reflection_is_redacted_from_business_body(self) -> None:
        cfg = config()
        output = self.run_analysis(lambda request: response(f"业务说明 {cfg.resolve_api_key()} {cfg.base_url} {cfg.chat_model}"))
        rendered = json.dumps(output)
        for secret in (cfg.resolve_api_key(), cfg.base_url, cfg.chat_model):
            self.assertNotIn(secret, rendered)

    def test_received_empty_text_stops_without_automatic_parsing_recovery(self) -> None:
        calls = []

        def transport(request):
            calls.append(request)
            self.assertEqual(len(calls), 1)
            return response("")

        result = self.run_analysis(transport, capture_api_responses=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["agent_result"]["status"], "ABSTAINED")
        self.assertEqual(result["agent_result"]["reading_coverage"]["sent_pages"], 1)
        self.assertEqual(result["agent_result"]["reading_coverage"]["summarized_pages"], 0)
        self.assertEqual(result["agent_result"]["model_turns"], 1)
        self.assertEqual(result["agent_result"]["automatic_retries"], 0)
        self.assertEqual(result["api_diagnostics"]["request_count"], 1)

    def test_authorization_failures_are_not_retried(self) -> None:
        for status in (401, 403):
            calls = []

            def transport(request):
                calls.append(request)
                return TransportResponse(status, "denied")

            with self.subTest(status=status):
                result = self.run_analysis(transport)
                self.assertEqual(len(calls), 1)
                self.assertEqual(result["agent_result"]["diagnostics"][0]["http_status"], status)

    def test_framework_matches_from_other_snapshot_or_entry_are_not_sent(self) -> None:
        for same_snapshot in (True, False):
            prepared = plan(1)
            prepared["entry"] = {"program_name": "PREMIUM-CALC", "relative_path": "premium.cbl"}
            framework = {"status": "MATCHED", "document": {"title": "Framework reference"},
                         "coverage": {"snapshot_id": "snapshot-test" if same_snapshot else "snapshot-old",
                                      "entry_program": "OTHER-PROGRAM" if same_snapshot else "PREMIUM-CALC",
                                      "entry_relative_path": "premium.cbl"},
                         "source_matches": [{"evidence_id": "ev_old"}],
                         "references": [{"reference_id": "fw:old:1-2", "text": "old source vocabulary"}]}

            def transport(request):
                supplied = json.loads(json.loads(request.body)["messages"][-1]["content"])
                self.assertEqual(supplied["framework_references"], [])
                return response("现有源码的说明")

            with self.subTest(same_snapshot=same_snapshot), mock.patch("business_analysis.prepare_source_reading", return_value=prepared):
                result = run_business_analysis("解释程序", self.database, self.root, config(),
                                               framework_context=framework, transport=transport)
            selected = result["agent_result"]["framework_context"]
            self.assertEqual(selected["reason_code"], "FRAMEWORK_SOURCE_CONTEXT_STALE")
            self.assertEqual(selected["source_matches"], [])
            self.assertEqual(selected["status"], "LOADED")
            self.assertEqual(framework["source_matches"], [{"evidence_id": "ev_old"}])


if __name__ == "__main__":
    unittest.main()
