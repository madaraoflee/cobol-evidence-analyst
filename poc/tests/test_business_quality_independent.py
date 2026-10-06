"""Independent offline black-box acceptance for business evidence and stopping.

The synthetic replies are adversarial control inputs, never business-quality
scores. Assertions inspect the adapter's actual encoded request and physical
source excerpts. COBOL_INDEPENDENT_POC selects a frozen baseline in a fresh
process; COBOL_INDEPENDENT_ARTIFACTS preserves reproducible requests/results.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

POC_ROOT = Path(os.environ.get("COBOL_INDEPENDENT_POC", Path(__file__).resolve().parents[1])).resolve()
sys.path.insert(0, str(POC_ROOT))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search

POC_HASHES = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(POC_ROOT.glob("*.py"))}

FORMULA = "COMPUTE NET-VALUE ROUNDED = BASE-VALUE * FACTOR-VALUE + 29"
INPUT = "MOVE 211 TO BASE-VALUE"
CONDITION = "IF ELIGIBLE-FLAG = 'Y'"
OVERRIDE = "MOVE ZERO TO NET-VALUE"


def pages(payload):
    return [page for context in payload["source_context"] for page in context.get("pages", [])]


def reply(text):
    return TransportResponse(200, json.dumps({"choices": [{"message": {
        "role": "assistant", "content": text}, "finish_reason": "stop"}]}, ensure_ascii=False))


def cobol(name, body, data=""):
    return (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
            f"WORKING-STORAGE SECTION.\n{data}\nPROCEDURE DIVISION.\nMAIN.\n{body}\nGOBACK.\n")


def deep_body(*, alias="RZQX", perform=False):
    # Alias deliberately never occurs in the formula or its operands.
    body = [f"DISPLAY '{alias} PERIOD'."]
    body += [f"COMPUTE WORK-VALUE = {i} + 1." for i in range(15)]
    if perform:
        body += ["PERFORM INITIALIZE-VALUES.", "PERFORM APPLY-VALUES.", "GOBACK."]
    body += [f"*> neutral record explanation {i}" for i in range(1200)]
    body += ["INITIALIZE-VALUES.", INPUT]
    body += [f"*> neutral cross paragraph explanation {i}" for i in range(110)]
    body += ["APPLY-VALUES.", CONDITION]
    body += [f"*> neutral nested calculation explanation {i}" for i in range(380)]
    body += [FORMULA, "ELSE", OVERRIDE, "END-IF.", "IF REVIEW-FLAG = 'Y'",
             "MOVE 19 TO NET-VALUE", "END-IF."]
    return "\n".join(body)


DATA = ("01 NET-VALUE PIC 9(9)V99.\n01 BASE-VALUE PIC 9(9).\n"
        "01 FACTOR-VALUE PIC 9V99 VALUE 2.\n01 ELIGIBLE-FLAG PIC X VALUE 'Y'.\n"
        "01 REVIEW-FLAG PIC X VALUE 'N'.\n01 WORK-VALUE PIC 9(9).")


class IndependentBusinessQualityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="independent-business-gold-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.reference = self.root / "absent-reference.md"
        self.requests = []
        self.output = None
        self.timings = {}
        self.source_text = {}
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model",
                                       api_key="synthetic-only")
        for target in ((socket, "create_connection"), (socket.socket, "connect")):
            patcher = mock.patch.object(*target, side_effect=AssertionError("Offline gold forbids network"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.archive)

    def archive(self):
        destination = os.environ.get("COBOL_INDEPENDENT_ARTIFACTS")
        if not destination:
            return
        folder = Path(destination) / self._testMethodName
        folder.mkdir(parents=True, exist_ok=True)
        value = {"poc_root": str(POC_ROOT), "synthetic_only": True, "network": False,
                 "poc_hashes_at_import": POC_HASHES,
                 "assertions_evaluate": "encoded request evidence and bounded stopping only",
                 "timing_seconds": self.timings,
                 "source_files": {path: {"sha256": hashlib.sha256((self.source / path).read_bytes()).hexdigest(),
                                          "text": text} for path, text in self.source_text.items()},
                 "requests": self.requests, "output": self.output}
        (folder / "record.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        for index, request in enumerate(self.requests, 1):
            (folder / f"request-{index:02}.json").write_text(request["body"] + "\n")
        if self.output:
            trace_path = Path(self.output["agent_result"]["metrics"]["quality_trace_path"])
            if trace_path.exists():
                (folder / "quality-trace.json").write_bytes(trace_path.read_bytes())

    def write(self, path, text):
        destination = self.source / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        self.source_text[path] = text

    def build(self):
        started = time.perf_counter()
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.timings["cold_build"] = time.perf_counter() - started

    def catalog(self, *, calls=True):
        for index in range(16):
            statement = f'CALL "ROUTINE-{(index + 1) % 16}".' if calls else "CONTINUE."
            self.write(f"neutral-{index:02}.cbl", cobol(f"ROUTINE-{index}", statement))

    def ask(self, question, responder, *, policy=None):
        def transport(request):
            body = request.body.decode("utf-8") if isinstance(request.body, bytes) else request.body
            envelope = json.loads(request.body)
            payload = json.loads(envelope["messages"][-1]["content"])
            self.assertNotIn("tools", envelope)
            self.assertNotIn("response_format", envelope)
            for page in pages(payload):
                text = self.source_text[page["relative_path"]]
                self.assertEqual(page["source_text"], "\n".join(
                    text.splitlines()[page["start_line"] - 1:page["end_line"]]))
                self.assertEqual(page["source_sha256"], hashlib.sha256((self.source / page["relative_path"]).read_bytes()).hexdigest())
            self.requests.append({"body": body,
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "payload": payload,
                "bytes": len(body.encode()),
                "literal_source_visibility": {literal: [page["relative_path"] for page in pages(payload)
                    if literal in page["source_text"]] for literal in (FORMULA, INPUT, CONDITION, OVERRIDE)}})
            result = responder(payload, len(self.requests))
            response = result if isinstance(result, TransportResponse) else reply(result)
            self.requests[-1]["synthetic_reply_body"] = (response.body.decode("utf-8")
                if isinstance(response.body, bytes) else response.body)
            return response
        started = time.perf_counter()
        self.output = run_business_chat(question, self.database, self.source, self.config,
            transport=transport, allow_network=False, framework_reference_path=self.reference,
            policy=policy)
        self.timings["ask"] = time.perf_counter() - started
        return self.output["agent_result"]

    def visible_answer(self, payload, _turn):
        cited = [page["evidence_id"] for page in pages(payload) if FORMULA in page["source_text"]]
        return ("所示计算式：" + FORMULA if cited else "未见该计算式。") + " ".join(f"[{value}]" for value in cited)

    def assert_request_has(self, payload, literals, *, path=None):
        actual = [page for page in pages(payload) if path is None or page["relative_path"] == path]
        for literal in literals:
            self.assertTrue(any(literal in page["source_text"] for page in actual),
                            f"Required physical source absent from actual request: {literal}")

    def assert_unresolved(self, result):
        self.assertNotEqual(result["status"], "ANALYZED")
        self.assertNotEqual(result["stop_reason"], "sufficient_material")
        self.assertFalse(result["model_answer_recorded"])
        self.assertEqual(result["narrative"]["citations"], [])

    def assert_trace_and_budget(self, result, policy=AgentPolicy()):
        self.assertLessEqual(len(self.requests), policy.max_model_requests)
        trace = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text())
        self.assertEqual(len(trace["rounds"]), len(self.requests))
        for sent, traced in zip(self.requests, trace["rounds"]):
            self.assertEqual(traced["request_body_sha256"], sent["body_sha256"])
            self.assertEqual(traced["request_bytes"], sent["bytes"])
            self.assertLessEqual(sent["bytes"], policy.max_request_bytes)
            self.assertLessEqual(sum(len(page["source_text"]) for page in pages(sent["payload"])),
                                 policy.max_source_characters)
            source_ids = {page["evidence_id"] for page in pages(sent["payload"])}
            for group in sent["payload"].get("evidence_groups", []):
                self.assertLessEqual(set(group.get("visible_evidence_ids", [])), source_ids)
                if group.get("complete_text_supplied"):
                    self.assertLessEqual(set(group.get("required_evidence_ids", [])), source_ids)
                    self.assertFalse(group.get("open_frontier"))
        final_round = trace["final"].get("final_answer_round_id")
        if final_round:
            index = next(index for index, item in enumerate(trace["rounds"])
                         if item["round_id"] == final_round)
            final_payload = self.requests[index]["payload"]
            final_ids = {page["evidence_id"] for page in pages(final_payload)} | {
                reference["reference_id"] for reference in final_payload["framework_references"]}
            for citation in result["narrative"]["citations"]:
                self.assertIn(citation.get("evidence_id", citation.get("reference_id")), final_ids)
        else:
            self.assertEqual(result["narrative"]["citations"], [])
        self.assertFalse(result["claims_semantically_verified"])

    def test_unknown_zero_hit_call_catalog_does_not_become_analyzed(self):
        self.catalog()
        self.build()
        result = self.ask("未知调整期间的业务规则是什么？", lambda _p, _t: "处理结果一律增加17。")
        self.assert_unresolved(result)
        self.assertEqual(self.requests[0]["payload"]["retrieval_status"]["state"], "needs_discovery")
        self.assertEqual(pages(self.requests[0]["payload"]), [])
        self.assert_trace_and_budget(result)

    def test_chinese_formula_planning_prose_requires_action_and_real_request_evidence(self):
        self.catalog()
        self.write("z-value.cbl", cobol("VALUEFLOW", deep_body(alias="RZAQ"), DATA))
        self.build()
        def responder(payload, turn):
            if turn == 1:
                return "要回答这个问题，需要先找到对应的公式和适用条件。"
            if turn == 2:
                return '{"search":["RZAQ"]}'
            return self.visible_answer(payload, turn)
        result = self.ask("特殊调整期间的最终金额如何计算？", responder)
        self.assertGreaterEqual(len(self.requests), 3)
        self.assert_request_has(self.requests[-1]["payload"], [FORMULA, INPUT, CONDITION, OVERRIDE], path="z-value.cbl")
        self.assert_trace_and_budget(result)

    def test_positive_unsupported_prose_zero_hit_is_retried_and_rejected(self):
        self.catalog()
        self.build()
        result = self.ask("未知折让如何计算？", lambda _p, _t: "折让等于金额乘以0.8。")
        self.assertEqual(len(self.requests), 2)
        self.assert_unresolved(result)
        self.assert_trace_and_budget(result)

    def test_zero_hit_search_keeps_orientation_dependencies_out_of_answer_evidence(self):
        self.catalog()
        self.build()
        result = self.ask("未知折让如何计算？", lambda _p, t:
                          '{"search":["UNSEEN-ADJUSTMENT"]}' if t == 1 else "没有相关资料。")
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(not pages(sent["payload"]) for sent in self.requests))
        self.assert_unresolved(result)
        self.assert_trace_and_budget(result)

    def test_malformed_discovery_action_is_bounded_without_success(self):
        self.catalog()
        self.build()
        result = self.ask("未知折让如何计算？", lambda _p, _t: '{"search":["UNSEEN"]')
        self.assertLessEqual(len(self.requests), 2)
        self.assert_unresolved(result)
        self.assert_trace_and_budget(result)

    def test_discovery_single_request_budget_cannot_promote_unsupported_prose(self):
        self.catalog()
        self.build()
        policy = AgentPolicy(max_model_requests=1)
        result = self.ask("未知期间金额如何计算？", lambda _p, _t: "金额按三倍计算。", policy=policy)
        self.assertEqual(len(self.requests), 1)
        self.assert_unresolved(result)
        self.assert_trace_and_budget(result, policy)

    def test_business_period_alias_has_final_deep_formula_input_condition_and_override(self):
        self.write("period.cbl", cobol("PERIODFLOW", deep_body(), DATA))
        self.build()
        result = self.ask("RZQX 期间的最终金额如何计算？", self.visible_answer)
        self.assert_request_has(self.requests[-1]["payload"],
            [FORMULA, INPUT, CONDITION, OVERRIDE, "MOVE 19 TO NET-VALUE"], path="period.cbl")
        self.assert_trace_and_budget(result)

    def test_alias_call_does_not_finish_with_only_entry_display_and_call_metadata(self):
        self.write("entry.cbl", cobol("REQUESTFLOW", "DISPLAY 'RZTQ PERIOD'.\nCALL 'VALUELEAF'."))
        self.write("leaf.cbl", cobol("VALUELEAF", deep_body(alias="NEUTRAL-PERIOD"), DATA))
        self.build()
        result = self.ask("RZTQ 期间的最终金额如何计算？", self.visible_answer)
        self.assert_request_has(self.requests[-1]["payload"], [FORMULA, INPUT, CONDITION, OVERRIDE], path="leaf.cbl")
        self.assert_request_has(self.requests[-1]["payload"], ["CALL 'VALUELEAF'."], path="entry.cbl")
        self.assert_trace_and_budget(result)

    def test_perform_cross_paragraph_rounded_formula_is_actual_source(self):
        self.write("perform.cbl", cobol("PARAGRAPHFLOW", deep_body(alias="RZPX", perform=True), DATA))
        self.build()
        result = self.ask("RZPX 期间的金额如何计算？", self.visible_answer)
        self.assert_request_has(self.requests[-1]["payload"],
            [FORMULA, INPUT, CONDITION, "PERFORM INITIALIZE-VALUES.", "PERFORM APPLY-VALUES."], path="perform.cbl")
        self.assert_trace_and_budget(result)

    def test_copy_input_and_alias_formula_are_both_original_physical_excerpts(self):
        self.write("shared.cpy", "01 COPY-BASE PIC 9(9) VALUE 211.\n")
        body = "COPY SHARED.\n" + deep_body(alias="RZCP").replace(INPUT, "MOVE COPY-BASE TO BASE-VALUE")
        self.write("copyflow.cbl", cobol("COPYFLOW", body, DATA))
        self.build()
        result = self.ask("RZCP 期间的金额如何计算，输入来自哪里？", self.visible_answer)
        self.assert_request_has(self.requests[-1]["payload"], [FORMULA, CONDITION, "MOVE COPY-BASE TO BASE-VALUE"], path="copyflow.cbl")
        self.assert_request_has(self.requests[-1]["payload"], ["01 COPY-BASE PIC 9(9) VALUE 211."], path="shared.cpy")
        self.assert_trace_and_budget(result)

    def test_manual_business_alias_does_not_substitute_for_program_formula(self):
        self.catalog()
        self.write("z-value.cbl", cobol("VALUEFLOW", deep_body(alias="RZMA"), DATA))
        self.reference.write_text("# Operation conventions\n\nSPECIAL-ADJUSTMENT 期间对应 RZMA；本节仅解释名称，计算以源码为准。\n")
        self.build()
        def responder(payload, turn):
            if turn == 1:
                return "金额等于基础金额乘17。"
            if turn == 2:
                return '{"search":["RZMA"]}'
            return self.visible_answer(payload, turn)
        result = self.ask("SPECIAL-ADJUSTMENT 期间的源码公式如何计算？", responder)
        self.assertGreaterEqual(len(self.requests), 3)
        self.assert_request_has(self.requests[-1]["payload"], [FORMULA, INPUT, CONDITION], path="z-value.cbl")
        self.assert_trace_and_budget(result)

    def test_manual_definition_can_still_be_answered_with_question_reference(self):
        self.catalog(calls=False)
        self.reference.write_text("# Operation conventions\n\nSPECIAL-ADJUSTMENT 定义为下一个工作日处理。\n")
        self.build()
        def responder(payload, _turn):
            reference = next(r for r in payload["framework_references"] if "SPECIAL-ADJUSTMENT" in r["text"])
            return f"手册定义为下一个工作日处理。[{reference['reference_id']}]"
        result = self.ask("框架手册中 SPECIAL-ADJUSTMENT 的定义是什么？", responder)
        self.assertEqual(result["status"], "ANALYZED")
        self.assertEqual(result["evidence_refs"], [])
        self.assertEqual(len(self.requests), 1)
        self.assert_trace_and_budget(result)

    def test_question_matched_manual_alone_cannot_close_source_formula_obligation(self):
        self.catalog(calls=False)
        self.reference.write_text("# Operation conventions\n\nSPECIAL-ADJUSTMENT 只描述处理期间，不包含金额公式。\n")
        self.build()
        result = self.ask("SPECIAL-ADJUSTMENT 的源码计算公式是什么？", lambda _p, _t: "公式为基础值乘17。")
        self.assert_unresolved(result)
        self.assertTrue(all(not pages(sent["payload"]) for sent in self.requests))
        self.assert_trace_and_budget(result)

    def test_orientation_framework_source_marker_does_not_answer_unknown_business(self):
        self.catalog()
        self.reference.write_text("# Routine conventions\n\nROUTINE-1 负责接收记录，内部金额规则由实现确定。\n")
        self.build()
        result = self.ask("未知折让业务的公式是什么？", lambda _p, _t: "折让为金额乘17。")
        self.assert_unresolved(result)
        self.assertEqual(self.requests[0]["payload"]["retrieval_status"]["state"], "needs_discovery")
        self.assertTrue(all(not pages(sent["payload"]) for sent in self.requests))
        self.assert_trace_and_budget(result)

    def test_small_source_budget_reports_missing_material_instead_of_sufficient(self):
        self.write("budget.cbl", cobol("BUDGETFLOW", deep_body(alias="RZBG"), DATA))
        self.build()
        policy = AgentPolicy(max_model_requests=1, max_source_characters=512,
                             initial_source_characters=512)
        result = self.ask("RZBG 期间的完整金额公式是什么？", self.visible_answer, policy=policy)
        required = [FORMULA, INPUT, CONDITION, OVERRIDE]
        supplied = all(any(literal in page["source_text"] for page in pages(self.requests[-1]["payload"]))
                       for literal in required)
        if not supplied:
            self.assertNotEqual(result["status"], "ANALYZED")
            self.assertNotEqual(result["stop_reason"], "sufficient_material")
        self.assert_trace_and_budget(result, policy)

    def test_missing_external_implementation_cannot_be_sufficient_for_complete_formula(self):
        self.write("caller.cbl", cobol("CALLERFLOW", "DISPLAY 'RZEX PERIOD'.\nCALL 'UNAVAILABLECALC' USING NET-VALUE.", DATA))
        self.build()
        result = self.ask("RZEX 期间的完整最终公式是什么？", lambda _p, _t: "完整公式为基础值乘2。")
        self.assertNotEqual(result["status"], "ANALYZED")
        self.assertNotEqual(result["stop_reason"], "sufficient_material")
        self.assert_trace_and_budget(result)

    def test_explicit_absent_program_keeps_identity_gap(self):
        self.catalog()
        self.build()
        result = self.ask("程序 UNAVAILABLECALC 的计算规则是什么？", lambda _p, _t: "结果按2倍计算。")
        self.assertNotEqual(result["status"], "ANALYZED")
        # An unquoted unknown ordinary name is currently a lexical query. Keep
        # this original counterexample and require it cannot become success;
        # explicit identity syntax is tested separately below.
        self.assertNotEqual(result["stop_reason"], "sufficient_material")
        self.assertTrue(all(not pages(sent["payload"]) for sent in self.requests))
        self.assert_trace_and_budget(result)

    def test_explicit_program_id_not_found_control(self):
        self.catalog()
        self.build()
        result = self.ask("PROGRAM-ID: UNAVAILABLECALC 的计算规则是什么？", lambda _p, _t: "结果按2倍计算。")
        self.assertNotEqual(result["status"], "ANALYZED")
        self.assertEqual(self.requests[0]["payload"]["business_map"]["source_identity"]["status"], "not_found")
        self.assertTrue(all(not pages(sent["payload"]) for sent in self.requests))
        self.assert_trace_and_budget(result)

    def test_known_caller_formula_survives_missing_external_dependency_as_partial(self):
        known = "COMPUTE NET-VALUE = BASE-VALUE * 2."
        self.write("caller.cbl", cobol("KNOWNFLOW", "DISPLAY 'RZKN PERIOD'.\n" + known +
                   "\nCALL 'UNAVAILABLECALC' USING NET-VALUE.", DATA))
        self.build()
        def responder(payload, _turn):
            page = next(page for page in pages(payload) if known in page["source_text"])
            return (f"已知调用前金额为基础值乘2；外部调整的实现待补充。[{page['evidence_id']}]")
        result = self.ask("RZKN 期间的完整金额规则，包括已知计算和外部调整是什么？", responder)
        self.assertTrue(result["model_answer_recorded"])
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("调用前金额为基础值乘2", result["answer"])
        self.assertNotEqual(result["stop_reason"], "sufficient_material")
        self.assert_request_has(self.requests[-1]["payload"], [known, "CALL 'UNAVAILABLECALC' USING NET-VALUE."])
        self.assert_trace_and_budget(result)

    def test_duplicate_program_ids_keep_all_candidates_unselected(self):
        self.write("one.cbl", cobol("DUPLICATEFLOW", "COMPUTE NET-VALUE = 2.", DATA))
        self.write("two.cbl", cobol("DUPLICATEFLOW", "COMPUTE NET-VALUE = 7.", DATA))
        self.build()
        result = self.ask("DUPLICATEFLOW 的计算规则是什么？", lambda _p, _t: "结果等于2。")
        self.assertNotEqual(result["status"], "ANALYZED")
        self.assertEqual(self.requests[0]["payload"]["business_map"]["source_identity"]["status"], "ambiguous")
        self.assertTrue(all(not pages(sent["payload"]) for sent in self.requests))
        self.assert_trace_and_budget(result)

    def test_explicit_read_past_eof_finishes_at_physical_end_without_phantom_gap(self):
        text = cobol("EOFFLOW", "MOVE 211 TO BASE-VALUE.\n" + FORMULA + ".", DATA)
        self.write("eof.cbl", text)
        self.build()
        end = len(text.splitlines())
        def responder(payload, turn):
            if turn == 1:
                return json.dumps({"read": [{"relative_path": "eof.cbl",
                    "start_line": end - 2, "end_line": end + 17}]})
            return self.visible_answer(payload, turn)
        policy = AgentPolicy(max_model_requests=2, max_answer_revisions=0)
        result = self.ask("EOFFLOW 的最终金额计算规则是什么？", responder, policy=policy)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["status"], "ANALYZED")
        self.assertEqual(result["stop_reason"], "sufficient_material")
        self.assertEqual(result["investigation_state"]["open_tasks"], [])
        self.assertEqual(result["metrics"]["tool_calls"]["read"], 1)
        self.assert_request_has(self.requests[-1]["payload"], [FORMULA], path="eof.cbl")
        self.assert_trace_and_budget(result, policy)

    def test_business_prose_about_reading_parameters_is_not_mistaken_for_deferral(self):
        self.write("natural.cbl", cobol("NATURALFLOW", INPUT + ".\n" + CONDITION + "\n" +
                   FORMULA + "\nELSE\n" + OVERRIDE + "\nEND-IF.", DATA))
        self.build()
        def responder(payload, _turn):
            page = next(page for page in pages(payload) if FORMULA in page["source_text"])
            return (f"[{page['evidence_id']}] 处理需要先读取参数，再计算金额；"
                    "符合条件时按基础值乘系数后加29，其他情况下归零。")
        result = self.ask("NATURALFLOW 的金额规则是什么？", responder)
        self.assertEqual(result["status"], "ANALYZED")
        self.assertEqual(len(self.requests), 1)
        self.assertIn("处理需要先读取参数", result["answer"])
        self.assertTrue(result["narrative"]["citations"])
        self.assert_trace_and_budget(result)


if __name__ == "__main__":
    unittest.main()
