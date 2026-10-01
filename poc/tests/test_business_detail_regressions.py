"""End-to-end source-supply and answer-binding checks with fixed offline replies.

The fake provider emits preselected text. These checks cover the runner's
evidence, budgets, and citation binding, not a language model's understanding.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from business_synthesis import wants_business_detail
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


CHAIN_MARKERS = (
    "MOVE 11 TO SEED-VALUE", "MOVE SEED-VALUE TO RAW-VALUE",
    "MOVE RAW-VALUE TO MID-VALUE", "MOVE MID-VALUE TO BASE-VALUE",
    "COMPUTE NET-VALUE = BASE-VALUE * 2",
)
REFERENCE = re.compile(r"\[((?:ev[_:-]|fw:)[^\]\r\n]{1,160})\]")


def source_pages(payload):
    return [page for bundle in payload["source_context"] for page in bundle.get("pages", [])]


class BusinessDetailIntentTests(unittest.TestCase):
    def test_stepwise_calculation_requests_business_detail(self):
        for question in ("请逐步计算净金额。", "請逐步計算淨金額。"):
            with self.subTest(question=question):
                self.assertTrue(wants_business_detail(question))

    def test_simple_calculation_keeps_existing_detail_classification(self):
        for question in ("净金额怎么计算？", "淨金額怎麼計算？", "How is the net amount calculated?"):
            with self.subTest(question=question):
                self.assertFalse(wants_business_detail(question))


class BusinessDetailRegressions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "synthetic-model",
                                       api_key="synthetic-only")
        self.requests = []
        self.responses = []
        self.request_bytes = []
        self.output_budgets = []
        self.network_attempts = []

        def forbidden(*args, **kwargs):
            self.network_attempts.append("connection")
            raise AssertionError("network forbidden in synthetic regression")

        for target, attribute in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                                  (socket, "create_connection")):
            patcher = mock.patch.object(target, attribute, side_effect=forbidden)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, path, name, body, data=""):
        (self.source / path).write_text(
            f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
            f"WORKING-STORAGE SECTION.\n{data}\nPROCEDURE DIVISION.\nMAIN.\n{body}\nGOBACK.\n",
            encoding="utf-8")

    def build(self):
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)

    def chain(self):
        body = ("PERFORM LOAD-BASE.\nIF BASE-VALUE > ZERO\n"
                "COMPUTE NET-VALUE = BASE-VALUE * 2\nELSE\nMOVE ZERO TO NET-VALUE\nEND-IF.\nGOBACK.\n"
                + "*> neutral first paragraph separation\n" * 150
                + "LOAD-BASE.\nPERFORM LOAD-MID.\nMOVE MID-VALUE TO BASE-VALUE.\nEXIT.\n"
                + "*> neutral second paragraph separation\n" * 150
                + "LOAD-MID.\nPERFORM LOAD-RAW.\nMOVE RAW-VALUE TO MID-VALUE.\nEXIT.\n"
                + "*> neutral third paragraph separation\n" * 150
                + "LOAD-RAW.\nPERFORM LOAD-SEED.\nMOVE SEED-VALUE TO RAW-VALUE.\nEXIT.\n"
                + "*> neutral fourth paragraph separation\n" * 150
                + "LOAD-SEED.\nMOVE 11 TO SEED-VALUE.\nEXIT.")
        self.write("chain.cbl", "VALUEFLOW", body,
                   "01 BASE-VALUE PIC 9(9).\n01 MID-VALUE PIC 9(9).\n01 RAW-VALUE PIC 9(9).\n"
                   "01 SEED-VALUE PIC 9(9).\n01 NET-VALUE PIC 9(9).")
        self.build()

    def cite(self, payload, literal):
        page = next((page for page in source_pages(payload) if literal in page["source_text"]), None)
        # Missing material remains a visible failing assertion after the run;
        # it is never manufactured as supplied source by the fake provider.
        return f"[{page['evidence_id']}]" if page else "[ev:missing-synthetic-source]"

    def chain_answer(self, payload):
        return ("输入从固定值11开始，经SEED、RAW、MID逐步传到基础值。"
                + "".join(self.cite(payload, marker) for marker in CHAIN_MARKERS[:4])
                + "\n\n基础值大于零时金额为基础值的两倍，否则金额归零。"
                + self.cite(payload, CHAIN_MARKERS[4]))

    def ask(self, question, respond, **options):
        def transport(request):
            envelope = json.loads(request.body)
            payload = json.loads(envelope["messages"][-1]["content"])
            self.requests.append(payload)
            self.request_bytes.append(len(request.body))
            self.output_budgets.append(envelope["max_tokens"])
            answer = respond(payload)
            if isinstance(answer, TransportResponse):
                return answer
            self.responses.append(answer)
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": answer}, "finish_reason": "stop"}]}, ensure_ascii=False))

        return run_business_chat(question, self.database, self.source, self.config,
            transport=transport, allow_network=False, framework_reference_path="",
            capture_context=True, **options)["agent_result"]

    def assert_first_material(self, markers):
        first = self.requests[0]
        text = "\n".join(page["source_text"] for page in source_pages(first))
        for marker in markers:
            self.assertTrue(marker in text, f"required local source missing before first provider request: {marker}")
        brief = first["business_analysis_brief"]
        self.assertTrue(brief["detail_requested"])
        self.assertEqual(brief["output_budget_tokens"], self.config.max_output_tokens)
        self.assertFalse(brief["semantic_execution_verified"])
        visible = {page["evidence_id"] for page in source_pages(first)}
        for item in brief["supplied_material"]:
            self.assertTrue(set(item["supplied_reference_ids"]) <= visible)
        policy = AgentPolicy()
        actions = [item for item in first.get("completed_actions", []) if item.get("automatic")]
        self.assertLessEqual(sum("read" in item for item in actions), policy.max_reads_per_turn)
        self.assertLessEqual(sum("inspect_business_context" in item for item in actions),
                             policy.max_business_context_actions_per_turn)

    def assert_answer_binding(self, result, policy=None):
        policy = policy or AgentPolicy()
        self.assertTrue(result["model_answer_recorded"])
        self.assertFalse(result["claims_semantically_verified"])
        self.assertEqual(self.network_attempts, [])
        self.assertLessEqual(result["metrics"]["model_requests"], policy.max_model_requests)
        self.assertEqual(result["metrics"]["policy"], policy.to_dict())
        quality = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text())
        round_id = quality["final"]["final_answer_round_id"]
        final_payload = self.requests[int(round_id.removeprefix("round-")) - 1]
        visible = {page["evidence_id"] for page in source_pages(final_payload)}
        citations = {item["evidence_id"]: item for item in result["narrative"]["citations"]
                     if item.get("evidence_id")}
        self.assertEqual(set(REFERENCE.findall(result["answer"])), set(citations))
        self.assertTrue(set(citations) <= visible)
        claims = result["claims"]
        self.assertGreaterEqual(len(claims), 2)
        paragraphs = [item.strip() for item in re.split(r"\n\s*\n", result["answer"]) if item.strip()]
        for claim in claims:
            self.assertEqual(claim["verification"], "unverified")
            self.assertEqual(claim["support_interpretation"], "cited_excerpt_not_verified_entailment")
            matching = [paragraph for paragraph in paragraphs
                        if REFERENCE.sub("", paragraph).strip() == claim["text"]]
            self.assertTrue(matching, "claim text must come from an actual answer paragraph")
            self.assertEqual(set(claim["evidence_ids"]), set(REFERENCE.findall(matching[0])))
            self.assertTrue(set(claim["evidence_ids"]) <= visible)
            self.assertEqual({item["reference_id"] for item in claim["support"]},
                             set(claim["evidence_ids"]))
            for support in claim["support"]:
                cited = citations[support["reference_id"]]
                for key in ("relative_path", "start_line", "end_line", "source_sha256"):
                    self.assertEqual(support[key], cited[key])

    def test_detailed_calculation_supplies_three_hop_input_origins_before_first_model(self):
        self.chain()
        result = self.ask("请详细解释 VALUEFLOW NET-VALUE 怎么计算，输入最初来自哪里以及何时归零？",
                          self.chain_answer)
        self.assert_first_material(CHAIN_MARKERS)
        self.assertEqual(result["metrics"]["model_requests"], 1)
        self.assertIn("固定值11", result["answer"])
        self.assertIn("两倍", result["answer"])
        inputs = next(item for item in result["investigation_state"]["question_investigation"]["required_items"]
                      if item["kind"] == "inputs")
        self.assertEqual(inputs["status"], "SATISFIED")
        self.assert_answer_binding(result)

    def test_stepwise_calculation_supplies_input_origins_and_binds_answer_claims(self):
        self.chain()
        result = self.ask("请逐步计算 VALUEFLOW NET-VALUE，说明输入最初来自哪里以及何时归零？",
                          self.chain_answer)
        self.assert_first_material(CHAIN_MARKERS)
        self.assertEqual(result["metrics"]["model_requests"], 1)
        self.assertEqual(result["business_review"]["answer_completion"]["status"], "not_assessed")
        self.assertEqual(result["business_review"]["answer_completion"]["semantic_verification"], "unverified")
        self.assert_answer_binding(result)

    def test_detailed_process_question_supplies_upstream_sources_without_formula_word(self):
        self.chain()
        result = self.ask("请详细分析 VALUEFLOW 的业务处理流程、输入来源、条件和结果去向。", self.chain_answer)
        self.assert_first_material(CHAIN_MARKERS)
        self.assertTrue(self.requests[0]["question_investigation"]["required_items"])
        self.assertEqual(result["metrics"]["model_requests"], 1)
        self.assert_answer_binding(result)

    def test_detailed_process_supplies_readable_internal_callee_before_first_model(self):
        self.write("caller.cbl", "REQUESTFLOW", "CALL 'FLAGFLOW' USING RESULT-FLAG.", "01 RESULT-FLAG PIC X.")
        self.write("leaf.cbl", "FLAGFLOW", "*> neutral internal paragraph separation\n" * 900
                   + "SET-RESULT.\nMOVE 'Y' TO RESULT-FLAG.", "01 RESULT-FLAG PIC X.")
        self.build()
        markers = ("CALL 'FLAGFLOW' USING RESULT-FLAG", "MOVE 'Y' TO RESULT-FLAG")
        result = self.ask("请详细分析 REQUESTFLOW 的处理目的、业务流程及返回标志如何决定。", lambda payload:
            "入口调用标志处理步骤并传入结果标志。" + self.cite(payload, markers[0])
            + "\n\n内部步骤把返回标志设为Y。" + self.cite(payload, markers[1]))
        self.assert_first_material(markers)
        self.assertIn("leaf.cbl", self.requests[0]["business_analysis_brief"]["available_source_paths"])
        self.assertEqual(self.requests[0]["business_analysis_brief"]["unavailable_or_runtime_targets"], [])
        self.assertIn("设为Y", result["answer"])
        self.assert_answer_binding(result)

    def test_missing_external_implementation_keeps_specific_caller_analysis_partial(self):
        markers = ("MOVE 11 TO BASE-VALUE", "COMPUTE NET-VALUE = BASE-VALUE * 2",
                   "CALL 'UNAVAILABLEFLOW' USING NET-VALUE", "IF NET-VALUE < ZERO")
        self.write("caller.cbl", "EXTERNALFLOW", "MOVE 11 TO BASE-VALUE.\n"
            "COMPUTE NET-VALUE = BASE-VALUE * 2.\nCALL 'UNAVAILABLEFLOW' USING NET-VALUE.\n"
            "IF NET-VALUE < ZERO\nMOVE ZERO TO NET-VALUE\nEND-IF.",
            "01 BASE-VALUE PIC 9(9).\n01 NET-VALUE PIC S9(9).")
        self.build()
        result = self.ask("请详细分析 EXTERNALFLOW 的业务流程、金额来源及返回后清零条件。", lambda payload:
            "调用外部步骤前，基础值为11，本地金额为基础值的两倍即22。"
            + self.cite(payload, markers[0]) + self.cite(payload, markers[1])
            + "\n\n随后把金额传给未提供实现的步骤；不能确认该步骤如何修改金额。" + self.cite(payload, markers[2])
            + "\n\n返回后若金额小于零则归零；其余返回值保留。" + self.cite(payload, markers[3]))
        self.assert_first_material(markers)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("UNAVAILABLEFLOW", json.dumps(self.requests[0]["business_analysis_brief"]["unavailable_or_runtime_targets"]))
        self.assertIn("即22", result["answer"])
        self.assertIn("不能确认该步骤", result["answer"])
        self.assertIn("小于零则归零", result["answer"])
        self.assert_answer_binding(result)

    def test_generic_partial_gets_one_bounded_synthesis_review(self):
        self.chain()
        drafts = []
        def respond(payload):
            if len(self.requests) == 1:
                drafts.append("当前资料不足以给出完整业务解释，需要补齐源码。" + self.cite(payload, CHAIN_MARKERS[4]))
                return drafts[0]
            return self.chain_answer(payload)
        result = self.ask("请详细分析 VALUEFLOW 的业务处理流程、输入来源、条件和结果去向。", respond)
        self.assert_first_material(CHAIN_MARKERS)
        self.assertEqual(result["metrics"]["model_requests"], 2)
        self.assertEqual(self.requests[1]["draft_answer"], drafts[0])
        self.assertIn("综合", self.requests[1]["task"])
        self.assertIn("固定值11", result["answer"])
        self.assertTrue(result["business_review"]["synthesis_review_attempted"])
        self.assert_answer_binding(result)

    def test_failed_synthesis_review_retains_draft_and_its_request_binding(self):
        self.chain()
        drafts = []
        def respond(payload):
            if len(self.requests) == 1:
                drafts.append("当前资料不足以完整解释输入来源。" + self.cite(payload, CHAIN_MARKERS[0])
                              + "\n\n已有计算规则是基础值乘以二。" + self.cite(payload, CHAIN_MARKERS[4]))
                return drafts[0]
            return TransportResponse(500, '{"error":{"message":"synthetic unavailable"}}')
        result = self.ask("请详细分析 VALUEFLOW 的业务处理流程、输入来源、条件和结果去向。", respond)
        self.assert_first_material(CHAIN_MARKERS)
        self.assertEqual(result["metrics"]["model_requests"], 2)
        self.assertEqual(result["answer"], drafts[0])
        self.assertTrue(result["business_review"]["synthesis_review_attempted"])
        self.assert_answer_binding(result)

    def test_long_draft_review_budget_preserves_full_answer_when_review_fails(self):
        self.write("rule.cbl", "FINAL-RULE", "IF BASIS > ZERO\n"
                   "COMPUTE FINAL-AMOUNT = BASIS * FACTOR\n"
                   "ELSE\nMOVE ZERO TO FINAL-AMOUNT\nEND-IF.",
                   "01 BASIS PIC 9(5) VALUE 8.\n01 FACTOR PIC 9V99 VALUE 1.25.\n"
                   "01 FINAL-AMOUNT PIC 9(7)V99.")
        self.build()
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "synthetic-model",
                                       api_key="synthetic-only", max_output_tokens=8192)
        policy = AgentPolicy(max_model_requests=2, max_request_bytes=32768)
        drafts = []
        def respond(payload):
            if len(self.requests) == 1:
                drafts.append("基础金额按系数计算，负值归零。\n\n" * 700
                              + "运行时外部状态无法确认。" + self.cite(payload, "COMPUTE FINAL-AMOUNT"))
                return drafts[0]
            return TransportResponse(500, '{"error":{"message":"synthetic unavailable"}}')
        result = self.ask("请详细解释 FINAL-AMOUNT 的计算流程。", respond, policy=policy)
        self.assertGreater(len(drafts[0].encode("utf-8")), policy.max_request_bytes)
        self.assertEqual(result["answer"], drafts[0])
        self.assertEqual(result["metrics"]["model_requests"], 2)
        self.assertTrue(all(size <= policy.max_request_bytes for size in self.request_bytes))
        self.assertEqual(self.output_budgets, [8192, 8192])
        self.assertTrue(result["business_review"]["synthesis_review_attempted"])
        self.assertIn("draft_answer", self.requests[1])
        self.assertLess(len(self.requests[1]["draft_answer"].encode("utf-8")), len(drafts[0].encode("utf-8")))
        for payload in self.requests:
            self.assertTrue(any("COMPUTE FINAL-AMOUNT" in page["source_text"] for page in source_pages(payload)))
        self.assert_answer_binding(result, policy=policy)


if __name__ == "__main__":
    unittest.main()
