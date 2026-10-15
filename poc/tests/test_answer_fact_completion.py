"""Nonblocking completion of already supplied, question-scoped business facts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from answer_fact_completion import _guard_description, complete_supplied_answer
from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from claim_ledger import review_answer_claims
from repository_discovery import ensure_repository_search


SOURCE = """IDENTIFICATION DIVISION.
PROGRAM-ID. CALCFLOW.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 RAW-AMOUNT PIC S9(7)V99.
01 RATE PIC 9(3).
01 OUT-AMOUNT PIC S9(7)V99.
01 TOTAL-AMOUNT PIC S9(9)V99.
01 RESULT-CODE PIC 9(3).
PROCEDURE DIVISION.
MAIN.
IF RATE NOT = 0
  PERFORM CALCULATE-AMOUNT
END-IF.
GOBACK.
CALCULATE-AMOUNT.
COMPUTE OUT-AMOUNT = RAW-AMOUNT * RATE / 1200
ADD OUT-AMOUNT TO TOTAL-AMOUNT
PERFORM WRITE-RESULT.
EXIT.
WRITE-RESULT.
CONTINUE.
UNRELATED.
MOVE 444 TO RESULT-CODE.
"""


def page(source=SOURCE, identifier="ev_test_source"):
    return {"relative_path": "flow.cbl", "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "start_line": 1, "end_line": len(source.splitlines()), "source_text": source,
            "evidence_id": identifier}


def investigation(source=SOURCE, name="CALCULATE-AMOUNT", intents=("calculation", "accumulation")):
    lines = source.splitlines()
    first = lines.index(name + ".") + 1
    last = next((index for index in range(first, len(lines))
                 if lines[index].endswith(".") and lines[index].split()[0].rstrip(".") == "EXIT"), len(lines) - 1) + 1
    return {"analysis_plan": {"intents": intents, "entities": []},
            "source_scope": {"ranges": [{"relative_path": "flow.cbl", "source_sha256": page(source)["source_sha256"],
                "procedure_name": name, "start_line": first, "end_line": last}]}}


class SourceFactCompletionTests(unittest.TestCase):
    def replace_body(self, body, data=""):
        source = SOURCE.replace("PROCEDURE DIVISION.", data + "\nPROCEDURE DIVISION.")
        return source.replace("COMPUTE OUT-AMOUNT = RAW-AMOUNT * RATE / 1200\n"
                              "ADD OUT-AMOUNT TO TOTAL-AMOUNT\nPERFORM WRITE-RESULT.", body)

    def test_evaluate_branch_labels_are_not_boolean_negations(self):
        source = self.replace_body("EVALUATE RESULT-CODE\nWHEN 1\n"
            "COMPUTE OUT-AMOUNT = RAW-AMOUNT * 2\nWHEN OTHER\n"
            "COMPUTE OUT-AMOUNT = RAW-AMOUNT * 3\nEND-EVALUATE.")
        answer, _ = complete_supplied_answer("计算规则", "保留已有解释。", investigation(source),
            [page(source)], source_format="free")
        self.assertIn("`EVALUATE RESULT-CODE` 的 `WHEN 1` 分支", answer)
        self.assertIn("`EVALUATE RESULT-CODE` 的 `WHEN OTHER` 分支", answer)
        self.assertIn("OUT-AMOUNT = RAW-AMOUNT * 2", answer)
        self.assertIn("OUT-AMOUNT = RAW-AMOUNT * 3", answer)
        self.assertNotIn("`WHEN 1` 不成立", answer)
        self.assertNotIn("`WHEN OTHER` 不成立", answer)
        self.assertNotIn("不成立", _guard_description({"kind": "IF", "condition": "IF RATE = 1", "outcome": "unknown"}))

    def test_adjacent_when_shared_body_is_omitted_when_grouping_is_unresolved(self):
        source = self.replace_body("EVALUATE RESULT-CODE\nWHEN 1\nWHEN 2\n"
            "COMPUTE OUT-AMOUNT = RAW-AMOUNT * 2\nWHEN OTHER\nCONTINUE\nEND-EVALUATE.")
        original = "已确认原有业务解释。"
        answer, report = complete_supplied_answer("计算规则", original, investigation(source),
            [page(source)], source_format="free")
        self.assertTrue(answer.startswith(original))
        self.assertNotIn("OUT-AMOUNT = RAW-AMOUNT * 2", answer)
        self.assertNotIn("此前 WHEN 均未命中", answer)
        self.assertGreater(report["omitted_candidate_count"], 0)

    def test_same_target_with_different_rounded_settings_has_no_unified_rounding_claim(self):
        source = self.replace_body("EVALUATE RESULT-CODE\nWHEN 1\n"
            "COMPUTE OUT-AMOUNT ROUNDED = RAW-AMOUNT * 2\nWHEN OTHER\n"
            "COMPUTE OUT-AMOUNT = RAW-AMOUNT * 3\nEND-EVALUATE.")
        answer, _ = complete_supplied_answer("计算规则", "保留原有解释。", investigation(source),
            [page(source)], source_format="free")
        self.assertIn("PIC S9(7)V99", answer)
        self.assertIn("各条计算的 ROUNDED 设置不同", answer)
        self.assertNotIn("本次计算没有 ROUNDED", answer)
        self.assertNotIn("本次计算使用 ROUNDED", answer)

    def test_unmapped_io_assignment_guards_preserve_independent_answer_facts(self):
        bodies = [
            "READ INPUT-FILE INVALID KEY\nCOMPUTE OUT-AMOUNT = RAW-AMOUNT * 2 NOT INVALID KEY\n"
            "COMPUTE OUT-AMOUNT = RAW-AMOUNT * 3 END-READ.",
            "READ INPUT-FILE\nINVALID KEY\nREAD SECOND-FILE\nNOT INVALID KEY\n"
            "COMPUTE OUT-AMOUNT = RAW-AMOUNT * 2.",
            "EXEC SQL SET :RESULT-CODE = LENGTH('A' || 'B') END-EXEC\nREAD INPUT-FILE\n"
            "INVALID KEY\nCOMPUTE OUT-AMOUNT = RAW-AMOUNT * 2\nNOT INVALID KEY\nCONTINUE\nEND-READ.",
        ]
        original = "TOTAL-AMOUNT = RAW-AMOUNT * 4。原有业务解释保留。"
        for body in bodies:
            with self.subTest(body=body):
                source = self.replace_body("COMPUTE TOTAL-AMOUNT = RAW-AMOUNT * 4\n" + body)
                answer, report = complete_supplied_answer("计算规则", original, investigation(source),
                    [page(source)], source_format="free")
                self.assertTrue(answer.startswith(original))
                self.assertNotIn("OUT-AMOUNT = RAW-AMOUNT * 2", answer)
                self.assertNotIn("OUT-AMOUNT = RAW-AMOUNT * 3", answer)
                self.assertFalse(any(fact["kind"] == "assignment" and fact["statement"].startswith("OUT-AMOUNT")
                                     for fact in report["added_facts"]))

    def test_size_error_formula_addition_states_its_normal_result_condition(self):
        source = self.replace_body("COMPUTE OUT-AMOUNT = RAW-AMOUNT * 2\nON SIZE ERROR\n"
                                   "MOVE 12 TO RESULT-CODE\nEND-COMPUTE.")
        answer, _ = complete_supplied_answer("计算规则", "原业务说明。", investigation(source),
            [page(source)], source_format="free")
        self.assertIn("无 SIZE ERROR 时的算式为 `OUT-AMOUNT = RAW-AMOUNT * 2`", answer)
        self.assertNotIn("Decimal", answer)

    def test_normal_multiline_io_assignments_have_separate_supported_branch_spans(self):
        source = self.replace_body("READ INPUT-FILE\nINVALID KEY\nCOMPUTE OUT-AMOUNT = RAW-AMOUNT * 2\n"
            "NOT INVALID KEY\nCOMPUTE OUT-AMOUNT = RAW-AMOUNT * 3\nEND-READ.")
        original = "原有业务解释保留。"
        answer, report = complete_supplied_answer("计算规则", original, investigation(source),
            [page(source)], source_format="free")
        self.assertTrue(answer.startswith(original))
        self.assertIn("`READ INPUT-FILE` 的 INVALID KEY 分支时，`OUT-AMOUNT = RAW-AMOUNT * 2`", answer)
        self.assertIn("`READ INPUT-FILE` 的 NOT INVALID KEY 分支时，`OUT-AMOUNT = RAW-AMOUNT * 3`", answer)

    def test_default_chain_keeps_body_and_does_not_add_mixed_line_io_assignment(self):
        source = self.replace_body("COMPUTE TOTAL-AMOUNT = RAW-AMOUNT * 4\nREAD INPUT-FILE INVALID KEY\n"
            "COMPUTE OUT-AMOUNT = RAW-AMOUNT * 2 NOT INVALID KEY\n"
            "COMPUTE OUT-AMOUNT = RAW-AMOUNT * 3 END-READ.")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "flow.cbl").write_text(source)
            database = root / "index.sqlite"
            build_business_index(root, database, source_format="free")
            ensure_repository_search(database, root)
            calls = []
            def transport(request):
                payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
                calls.append(payload)
                supplied = [entry for bundle in payload["source_context"] for entry in bundle.get("pages", ())
                            if entry.get("relative_path") == "flow.cbl"]
                evidence = max(supplied, key=lambda entry: entry["end_line"] - entry["start_line"])
                text = "TOTAL-AMOUNT = RAW-AMOUNT * 4。[" + evidence["evidence_id"] + "] 原有业务解释保留。"
                return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}))
            result = run_business_chat("CALCFLOW 的 CALCULATE-AMOUNT 计算规则", database, root,
                CompanyAPIConfig("https://gateway.example.invalid/v1", "offline-test-model", api_key="local-test-only"),
                framework_reference_path="", transport=transport,
                policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))["agent_result"]
            self.assertEqual(len(calls), 1)
            self.assertIn("TOTAL-AMOUNT = RAW-AMOUNT * 4", result["answer"])
            self.assertIn("原有业务解释保留", result["answer"])
            self.assertNotIn("OUT-AMOUNT = RAW-AMOUNT * 2", result["answer"])
            self.assertNotIn("OUT-AMOUNT = RAW-AMOUNT * 3", result["answer"])

    def test_following_invocation_keeps_its_if_condition(self):
        source = SOURCE.replace("PERFORM WRITE-RESULT.", "IF RATE = 1\nPERFORM WRITE-RESULT\nEND-IF.")
        answer, _ = complete_supplied_answer("计算并累加", "OUT-AMOUNT = RAW-AMOUNT * RATE / 1200。",
            investigation(source), [page(source)], source_format="free")
        invocation = next(line for line in answer.split("。") if "PERFORM WRITE-RESULT" in line)
        self.assertIn("`IF RATE = 1` 成立", invocation)
        self.assertNotIn("随后发起", invocation)

    def test_nested_evaluate_io_and_qualified_condition_remain_on_the_invocation(self):
        source = self.replace_body("COMPUTE OUT-AMOUNT OF OUTPUT-REC = RAW-AMOUNT OF INPUT-REC * 2\n"
            "EVALUATE RESULT-CODE OF CHECK-REC\nWHEN 1\nREAD INPUT-FILE\nINVALID KEY\n"
            "IF RATE = 1\nPERFORM WRITE-RESULT\nEND-IF\nEND-READ\n"
            "WHEN OTHER\nCONTINUE\nEND-EVALUATE.",
            "01 INPUT-REC.\n05 RAW-AMOUNT PIC 9(7).\n01 OUTPUT-REC.\n05 OUT-AMOUNT PIC 9(7).\n"
            "01 CHECK-REC.\n05 RESULT-CODE PIC 9.\n05 RATE PIC 9.")
        answer, _ = complete_supplied_answer("计算并累加", "已给出当前公式。", investigation(source),
            [page(source)], source_format="free")
        invocation = next(line for line in answer.split("。") if "PERFORM WRITE-RESULT" in line)
        self.assertIn("`EVALUATE RESULT-CODE OF CHECK-REC` 的 `WHEN 1` 分支", invocation)
        self.assertIn("`IF RATE = 1` 成立", invocation)
        self.assertIn("`READ INPUT-FILE` 的 INVALID KEY 分支", invocation)
        self.assertIn("OUT-AMOUNT OF OUTPUT-REC = RAW-AMOUNT OF INPUT-REC * 2", answer)
        self.assertNotIn("`WHEN 1` 不成立", invocation)

    def test_qualified_unsupported_condition_does_not_become_an_unconditional_call(self):
        source = SOURCE.replace("PERFORM WRITE-RESULT.",
                                "IF RATE OF CONTROL-REC = 1\nPERFORM WRITE-RESULT\nEND-IF.")
        answer, report = complete_supplied_answer("计算并累加", "已给出当前公式。", investigation(source),
            [page(source)], source_format="free")
        self.assertNotIn("PERFORM WRITE-RESULT", answer)
        self.assertFalse(any(fact["kind"] == "following_invocation" for fact in report["added_facts"]))

    def test_same_line_io_clause_without_column_proof_does_not_promote_call(self):
        source = SOURCE.replace("PERFORM WRITE-RESULT.",
            "READ INPUT-FILE INVALID KEY PERFORM WRITE-RESULT NOT INVALID KEY CONTINUE END-READ.")
        answer, report = complete_supplied_answer("计算并累加", "已给出当前公式。", investigation(source),
            [page(source)], source_format="free")
        self.assertNotIn("PERFORM WRITE-RESULT", answer)
        self.assertFalse(any(fact["kind"] == "following_invocation" for fact in report["added_facts"]))

    def test_following_invocation_with_unresolved_control_is_not_promoted(self):
        source = SOURCE.replace("PERFORM WRITE-RESULT.",
                                "IF FUNCTION ABS(RATE) = 1\nPERFORM WRITE-RESULT\nEND-IF.")
        answer, report = complete_supplied_answer("计算并累加", "已给出当前公式。", investigation(source),
            [page(source)], source_format="free")
        self.assertNotIn("PERFORM WRITE-RESULT", answer)
        self.assertGreater(report["omitted_candidate_count"], 0)

    def test_missing_accumulation_invocation_guard_and_scale_are_source_bound(self):
        original = "OUT-AMOUNT = RAW-AMOUNT * RATE / 1200。[ev_test_source]"
        answer, report = complete_supplied_answer("CALCULATE-AMOUNT 如何计算并累加？", original,
            investigation(), [page()], source_format="free", answer_detail="brief")
        self.assertTrue(answer.startswith(original))
        self.assertIn("TOTAL-AMOUNT = OUT-AMOUNT + TOTAL-AMOUNT", answer)
        self.assertIn("PERFORM WRITE-RESULT", answer)
        self.assertIn("IF RATE NOT = 0", answer)
        self.assertIn("PIC S9(7)V99", answer)
        self.assertIn("没有 ROUNDED", answer)
        self.assertNotIn("444", answer)
        self.assertEqual(report["model_requests"], 0)
        self.assertTrue(report["answer_preserved"])
        self.assertTrue(all(fact["source_sha256"] == page()["source_sha256"] for fact in report["added_facts"]))

    def test_correct_short_formula_has_no_added_sections_or_blocking(self):
        original = "OUT-AMOUNT = RAW-AMOUNT * RATE / 1200。[ev_test_source]"
        answer, report = complete_supplied_answer("只给CALCULATE-AMOUNT公式，简短回答", original,
            investigation(intents=("calculation",)), [page()], source_format="free", answer_detail="brief")
        self.assertEqual(answer, original)
        self.assertEqual(report["added_facts"], [])

    def test_unknown_scope_version_and_truncated_pages_do_not_invent_facts(self):
        original = "已供应公式可见，后续待核对。"
        state = investigation()
        state["source_scope"]["ranges"][0]["source_sha256"] = "f" * 64
        for states, pages in ((state, [page()]), ({"analysis_plan": {"intents": ["calculation"]}}, [page()]),
                              (investigation(), [{**page(), "span_truncated": True}])):
            with self.subTest(states=states):
                answer, report = complete_supplied_answer("计算并累加", original, states, pages, source_format="free")
                self.assertEqual(answer, original)
                self.assertFalse(report["added_facts"])

    def test_duplicate_pages_do_not_duplicate_additions(self):
        answer, report = complete_supplied_answer("计算并累加", "OUT-AMOUNT = RAW-AMOUNT * RATE / 1200。",
            investigation(), [page(), page(identifier="ev_other")], source_format="free")
        self.assertEqual(answer.count("TOTAL-AMOUNT = OUT-AMOUNT + TOTAL-AMOUNT"), 1)

    def test_local_completion_and_claim_review_reuse_same_version_facts(self):
        original = "OUT-AMOUNT = RAW-AMOUNT * RATE / 1200。[ev_test_source]"
        cache = {}
        supplied = page()
        answer, _ = complete_supplied_answer("CALCULATE-AMOUNT计算并累加", original, investigation(),
            [supplied], source_format="free", source_fact_cache=cache)
        with mock.patch("claim_ledger.supplied_statement_facts", side_effect=AssertionError("unexpected reparse")):
            reviewed, ledger = review_answer_claims(answer, [supplied], {"ev_test_source": supplied},
                source_format="free", source_fact_cache=cache)
        self.assertIn("TOTAL-AMOUNT", reviewed)
        self.assertGreater(ledger.source_fact_count, 0)

    def test_clipped_scope_does_not_turn_a_missing_guard_into_an_unconditional_fact(self):
        clipped = {**page(), "start_line": 17, "end_line": 20,
                   "source_text": "\n".join(SOURCE.splitlines()[16:20])}
        answer, report = complete_supplied_answer("计算并累加", "已有正确结论。", investigation(),
            [clipped], source_format="free")
        self.assertEqual(answer, "已有正确结论。")
        self.assertFalse(report["added_facts"])

    def test_io_error_branch_is_retained_without_claiming_normal_success(self):
        source = SOURCE[:SOURCE.index("CALCULATE-AMOUNT.\nCOMPUTE")] + """CALCULATE-AMOUNT.
READ INPUT-FILE
 INVALID KEY
 MOVE 101 TO RESULT-CODE
 DISPLAY 'MISSING INPUT RECORD'
 NOT INVALID KEY
 MOVE 102 TO RESULT-CODE
END-READ.
EXIT.
"""
        answer, report = complete_supplied_answer("失败时RESULT-CODE为何改变？", "RESULT-CODE = 102。",
            investigation(source, intents=("exception", "overwrite")), [page(source)], source_format="free")
        self.assertIn("`READ INPUT-FILE` 的 INVALID KEY 分支", answer)
        self.assertIn("RESULT-CODE = 101", answer)
        self.assertNotIn("写入成功", answer)

    def test_default_chain_repairs_wrong_formula_and_keeps_downstream_facts_with_one_request(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "flow.cbl").write_text(SOURCE)
            database = root / "index.sqlite"
            build_business_index(root, database, source_format="free")
            ensure_repository_search(database, root)
            calls = []
            def transport(request):
                body = json.loads(request.body)
                payload = json.loads(body["messages"][-1]["content"])
                calls.append(payload)
                supplied = [entry for bundle in payload["source_context"] for entry in bundle.get("pages", ())
                            if entry.get("relative_path") == "flow.cbl" and entry["start_line"] <= 16 and entry["end_line"] >= 20]
                self.assertTrue(supplied)
                text = "OUT-AMOUNT = RAW-AMOUNT * RATE / 999。[" + supplied[0]["evidence_id"] + "] 其他已确认说明保留。"
                return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}))
            result = run_business_chat("CALCFLOW 的 CALCULATE-AMOUNT 如何计算并累加？", database, root,
                CompanyAPIConfig("https://gateway.example.invalid/v1", "offline-test-model", api_key="local-test-only"),
                framework_reference_path="", transport=transport,
                policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0), answer_detail="brief")["agent_result"]
            self.assertEqual(len(calls), 1)
            self.assertIn("1200", result["answer"])
            self.assertNotIn("/ 999", result["answer"])
            self.assertIn("TOTAL-AMOUNT = OUT-AMOUNT + TOTAL-AMOUNT", result["answer"])
            self.assertIn("PERFORM WRITE-RESULT", result["answer"])
            self.assertIn("其他已确认说明保留", result["answer"])
            self.assertGreater(result["claim_ledger"]["revised_claim_count"], 0)


if __name__ == "__main__":
    unittest.main()
