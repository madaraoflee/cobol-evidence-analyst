"""Default workbench claim support with only in-process fake transports."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from claim_ledger import review_answer_claims
from claim_support import supplied_statement_facts
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search
from source_session import read_archived_evidence


def page(body, name="calc.cbl", identifier="ev_formula"):
    return {"evidence_id": identifier, "relative_path": name,
            "source_sha256": hashlib.sha256(body.encode()).hexdigest(), "source_text": body,
            "start_line": 1, "end_line": len(body.splitlines()), "span_truncated": False}


def response(text, finish="stop"):
    return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": text},
                                               "finish_reason": finish}]}, ensure_ascii=False))


class ClaimLedgerTests(unittest.TestCase):
    def review(self, answer, body):
        supplied = page(body)
        return review_answer_claims(answer, [supplied], {supplied["evidence_id"]: supplied}, source_format="free")

    def test_real_citation_does_not_support_wrong_formula_in_notation_or_prose(self):
        body = "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nMOVE 0 TO STATUS-CODE."
        for statement in ("OUT-AMOUNT = IN-AMOUNT * 999", "OUT-AMOUNT 是 IN-AMOUNT 乘以999",
                          "OUT-AMOUNT 是 IN-AMOUNT 的999倍", "输出金额按输入金额乘以999计算"):
            with self.subTest(statement=statement):
                answer, ledger = self.review(statement + "。[ev_formula] 状态码另行处理。[ev_formula]", body)
                self.assertNotIn("999", answer)
                self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", answer)
                self.assertIn("状态码另行处理", answer)
                self.assertEqual(ledger.entries[0].support_status, "contradicted")
                self.assertEqual(ledger.entries[0].source_versions[0]["source_sha256"], page(body)["source_sha256"])
                self.assertFalse(ledger.to_dict()["all_claims_supported"])

    def test_short_correct_formula_has_no_section_requirement(self):
        text = "OUT-AMOUNT = IN-AMOUNT * 2。[ev_formula]"
        answer, ledger = self.review(text, "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.")
        self.assertEqual(answer, text)
        self.assertTrue(ledger.to_dict()["all_claims_supported"])
        self.assertEqual(ledger.entries[0].support_scope, "source_statement_syntax")

    def test_parenthesized_correct_formula_is_not_truncated_or_rewritten(self):
        text = "WS-MONTHLY-INT = ( TRAN-CAT-BAL * DIS-INT-RATE ) / 1200。[ev_formula]"
        answer, ledger = self.review(text, "COMPUTE WS-MONTHLY-INT = ( TRAN-CAT-BAL * DIS-INT-RATE ) / 1200.")
        self.assertEqual(answer, text)
        self.assertEqual(ledger.entries[0].support_status, "supported")
        self.assertEqual(ledger.entries[0].expression, ["(", "TRAN-CAT-BAL", "*", "DIS-INT-RATE", ")", "/", "1200"])

    def test_power_operator_survives_markdown_normalization(self):
        body = "COMPUTE OUT-AMOUNT = IN-AMOUNT ** 2."
        for text in ("OUT-AMOUNT = IN-AMOUNT ** 2。[ev_formula]",
                     "**OUT-AMOUNT = IN-AMOUNT ** 2**。[ev_formula]",
                     "**公式：** `OUT-AMOUNT = IN-AMOUNT**2`。[ev_formula]"):
            with self.subTest(text=text):
                answer, ledger = self.review(text, body)
                self.assertEqual(answer, text)
                self.assertEqual(ledger.entries[0].support_status, "supported")
                self.assertEqual(ledger.entries[0].expression, ["IN-AMOUNT", "**", "2"])

    def test_unknown_compilation_branch_supports_syntax_without_final_value(self):
        body = "PROCEDURE DIVISION.\nMAIN.\n>>IF FEATURE-A\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\n>>END-IF\nGOBACK."
        answer, ledger = self.review("OUT-AMOUNT 最终等于 IN-AMOUNT * 2。[ev_formula] 输入来源另查。", body)
        self.assertIn("编译条件尚未确认", answer)
        self.assertIn("候选源码语句", answer)
        self.assertIn("输入来源另查", answer)
        self.assertEqual(ledger.entries[0].support_status, "unsupported")
        self.assertEqual(ledger.entries[0].revised_claims[0]["compilation_status"], "candidate")

    def test_supplied_compilation_context_selects_effective_formula(self):
        body = "PROCEDURE DIVISION.\nMAIN.\n>>IF FEATURE-A\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\n>>ELSE\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 3.\n>>END-IF\nGOBACK."
        supplied = page(body)
        text = "OUT-AMOUNT = IN-AMOUNT * 2。[ev_formula]"
        answer, ledger = review_answer_claims(text, [supplied], {"ev_formula": supplied},
            source_format="free", compilation_context={"compile_conditions": {"FEATURE-A": True}})
        self.assertEqual(answer, text)
        self.assertEqual(ledger.entries[0].support_status, "supported")
        self.assertTrue(ledger.compilation_context_fingerprint)
        answer, ledger = review_answer_claims(text, [supplied], {"ev_formula": supplied},
            source_format="free", compilation_context={"compile_conditions": {"FEATURE-A": False}})
        self.assertIn("OUT-AMOUNT = IN-AMOUNT * 3", answer)
        self.assertEqual(ledger.entries[0].support_status, "contradicted")

    def test_operator_words_inside_identifiers_and_literals_are_not_rewritten(self):
        for answer, body in (("OUT-AMOUNT = TIMES-RATE * 2。[ev_formula]", "COMPUTE OUT-AMOUNT = TIMES-RATE * 2."),
                             ("MOVE 'times' TO MESSAGE-TEXT。[ev_formula]", "MOVE 'times' TO MESSAGE-TEXT.")):
            with self.subTest(answer=answer):
                rendered, ledger = self.review(answer, body)
                self.assertEqual(rendered, answer)
                self.assertEqual(ledger.entries[0].support_status, "supported")

    def test_noop_procedure_does_not_acquire_business_effects_from_its_name(self):
        body = "PROCEDURE DIVISION.\n1400-COMPUTE-FEES.\n*> To be implemented\nEXIT.\n9000-FINISH.\nCONTINUE."
        wrong = "1400-COMPUTE-FEES 按余额的1%计算手续费。[ev_formula] 已提供段落原文。"
        answer, ledger = self.review(wrong, body)
        self.assertNotIn("1%", answer)
        self.assertIn("未实现本段计算或写入逻辑", answer)
        self.assertIn("已提供段落原文", answer)
        self.assertEqual(ledger.entries[0].reason_code, "COMPLETE_PROCEDURE_HAS_ONLY_NOOP_STATEMENTS")
        answer, ledger = self.review(wrong, "PROCEDURE DIVISION.\n1400-COMPUTE-FEES.\nEXIT.")
        self.assertEqual(answer, wrong)
        self.assertFalse(any(entry.reason_code == "COMPLETE_PROCEDURE_HAS_ONLY_NOOP_STATEMENTS" for entry in ledger.entries))

    def test_long_formula_factor_preserves_independent_correct_business_sentence(self):
        text = "基础值初始为11，大于5时乘以2再加29并舍入。[ev_formula]"
        body = "IF BASE-AMOUNT > 5\nCOMPUTE OUT-AMOUNT ROUNDED = BASE-AMOUNT * 2 + 29\nEND-IF."
        answer, ledger = self.review(text, body)
        self.assertEqual(answer, text)
        self.assertEqual(ledger.entries[0].support_status, "supported")
        self.assertEqual(ledger.entries[0].support_scope, "source_arithmetic_factor")
        self.assertFalse(ledger.to_dict()["all_claims_supported"])
        self.assertEqual(ledger.entries[1].reason_code, "PROSE_REMAINDER_NOT_CHECKED")
        uncertain = "输入乘以2后依照其他规则处理。[ev_formula]"
        answer, ledger = self.review(uncertain, "COMPUTE OUT-AMOUNT = IN-AMOUNT * RATE.")
        self.assertEqual(answer, uncertain)
        self.assertEqual(ledger.entries[0].support_status, "unsupported")

    def test_statement_presence_cannot_support_unconditional_final_value(self):
        text = "OUT-AMOUNT 总是等于 IN-AMOUNT * 2。[ev_formula] 输入金额另行读取。[ev_formula]"
        body = "IF IN-AMOUNT > 0\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2\nELSE\nMOVE 0 TO OUT-AMOUNT\nEND-IF."
        answer, ledger = self.review(text, body)
        self.assertNotIn("总是等于", answer)
        self.assertIn("IF IN-AMOUNT > 0", answer)
        self.assertIn("条件不成立", answer)
        self.assertIn("OUT-AMOUNT = 0", answer)
        self.assertIn("输入金额另行读取", answer)
        self.assertEqual(ledger.entries[0].reason_code, "FINAL_VALUE_REQUIRES_CONTROL_AND_OVERWRITE_ANALYSIS")
        self.assertEqual(ledger.entries[0].support_status, "unsupported")
        self.assertTrue(all(row["support_status"] == "supported" for row in ledger.entries[0].revised_claims))

    def test_final_value_review_includes_other_supplied_same_version_writes(self):
        body = "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nMOVE 0 TO OUT-AMOUNT."
        digest = hashlib.sha256(body.encode()).hexdigest()
        calc = {**page(body.splitlines()[0], identifier="ev_calc"), "source_sha256": digest}
        zero = {**page(body.splitlines()[1], identifier="ev_zero"), "source_sha256": digest,
                "start_line": 2, "end_line": 2}
        text = "OUT-AMOUNT = IN-AMOUNT * 2。[ev_calc] OUT-AMOUNT 最终等于 IN-AMOUNT * 2。[ev_calc] 输入仍需核对。"
        answer, ledger = review_answer_claims(text, [calc, zero], {"ev_calc": calc, "ev_zero": zero}, source_format="free")
        self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2。[ev_calc]", answer)
        self.assertEqual(ledger.entries[0].support_status, "supported")
        self.assertEqual(ledger.entries[1].support_status, "unsupported")
        self.assertIn("ev_zero", ledger.entries[1].reviewed_evidence_ids)
        self.assertIn("OUT-AMOUNT = 0", answer)
        self.assertNotIn("最终等于", answer)
        self.assertIn("输入仍需核对", answer)

    def test_final_value_review_keeps_program_and_version_boundaries(self):
        calc_body = "IDENTIFICATION DIVISION.\nPROGRAM-ID. FIRST-RULE.\nPROCEDURE DIVISION.\nMAIN.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nGOBACK."
        zero_body = "IDENTIFICATION DIVISION.\nPROGRAM-ID. SECOND-RULE.\nPROCEDURE DIVISION.\nMAIN.\nMOVE 0 TO OUT-AMOUNT.\nGOBACK."
        digest = hashlib.sha256((calc_body + "\n" + zero_body).encode()).hexdigest()
        calc = {**page(calc_body, identifier="ev_calc"), "source_sha256": digest}
        zero = {**page(zero_body, identifier="ev_zero"), "source_sha256": digest,
                "start_line": 7, "end_line": 12}
        variants = [zero, {**zero, "relative_path": "other.cbl"},
                    {**zero, "source_sha256": hashlib.sha256(zero_body.encode()).hexdigest()}]
        for other in variants:
            with self.subTest(other=other):
                answer, ledger = review_answer_claims("OUT-AMOUNT 最终等于 IN-AMOUNT * 2。[ev_calc]",
                    [calc, other], {"ev_calc": calc, "ev_zero": other}, source_format="free")
                self.assertEqual(ledger.entries[0].reviewed_evidence_ids, ["ev_calc"])
                self.assertEqual(ledger.entries[0].support_status, "unsupported")
                self.assertNotIn("OUT-AMOUNT = 0", answer)
                self.assertIn("未供证代码", answer)

    def test_exception_scope_keeps_formula_fact_without_unconditional_result_proof(self):
        body = "PROCEDURE DIVISION.\nMAIN.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2\nON SIZE ERROR MOVE 0 TO STATUS-CODE\nEND-COMPUTE.\nGOBACK."
        text = "OUT-AMOUNT = IN-AMOUNT * 2。[ev_formula]"
        answer, ledger = self.review(text, body)
        self.assertEqual(answer, text)
        self.assertEqual(ledger.entries[0].support_status, "supported")
        self.assertEqual(ledger.entries[0].support_scope, "source_statement_syntax")
        answer, ledger = self.review("OUT-AMOUNT 总是等于 IN-AMOUNT * 2。[ev_formula]", body)
        self.assertIn("无 SIZE ERROR", answer)
        self.assertEqual(ledger.entries[0].support_status, "unsupported")

    def test_move_arithmetic_condition_and_write_request_are_bounded_checks(self):
        cases = [("MOVE 9 TO OUT-AMOUNT", "MOVE 2 TO OUT-AMOUNT.", "OUT-AMOUNT = 2"),
                 ("OUT-AMOUNT = IN-AMOUNT * 9", "MULTIPLY 2 BY IN-AMOUNT GIVING OUT-AMOUNT.", "2 * IN-AMOUNT"),
                 ("IF IN-AMOUNT > 999", "IF IN-AMOUNT > 2\nMOVE 1 TO STATUS-CODE\nEND-IF.", "IF IN-AMOUNT > 2"),
                 ("WRITE WRONG-RECORD", "WRITE OUTPUT-RECORD.", None)]
        for wrong, body, expected in cases:
            with self.subTest(wrong=wrong):
                answer, ledger = self.review(wrong + "。[ev_formula]", body)
                self.assertNotIn(wrong, answer)
                if expected:
                    self.assertIn(expected, answer)
                else:
                    self.assertIn("尚未得到", answer)
                self.assertGreater(ledger.to_dict()["revised_claim_count"], 0)
        answer, ledger = self.review("WRITE OUTPUT-RECORD。[ev_formula]", "WRITE OUTPUT-RECORD.")
        self.assertEqual(ledger.entries[0].support_status, "supported")
        self.assertEqual(ledger.entries[0].claim_type, "write_statement")
        self.assertNotIn("success", ledger.to_dict()["scope"])

    def test_invalid_citation_reviews_its_statement_and_keeps_independent_prose(self):
        text = "OUT-AMOUNT = IN-AMOUNT * 999。[ev_missing] 输入由用户提供。[ev_formula]"
        answer, ledger = self.review(text, "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.")
        self.assertNotIn("999", answer)
        self.assertNotIn("ev_missing", answer)
        self.assertIn("输入由用户提供", answer)
        self.assertEqual(ledger.entries[0].invalid_evidence_ids, ["ev_missing"])
        self.assertIsNotNone(ledger.entries[0].revision)

    def test_english_numeric_sentence_end_keeps_independent_next_sentence(self):
        answer, ledger = self.review("OUT-AMOUNT = IN-AMOUNT * 999.[ev_formula] Input is supplied by the user.[ev_formula]",
                                    "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.")
        self.assertNotIn("999", answer)
        self.assertIn("Input is supplied by the user", answer)

    def test_unsupported_semantics_are_recorded_without_whole_answer_refusal(self):
        text = "客户最终一定收到款项。[ev_formula] OUT-AMOUNT = IN-AMOUNT * 2。[ev_formula]"
        answer, ledger = self.review(text, "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.")
        self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", answer)
        self.assertEqual(ledger.entries[0].support_status, "unsupported")
        self.assertFalse(ledger.to_dict()["all_claims_supported"])

    def test_ordinary_english_prose_is_not_an_unbound_code_assignment(self):
        body = "IF BASIS-VALUE > 0\nCOMPUTE FINAL-AMOUNT = BASIS-VALUE * FACTOR-VALUE\nELSE\nMOVE 0 TO FINAL-AMOUNT\nEND-IF."
        for text in ("First check the condition: if the basis is positive, multiply it by the factor; otherwise set the final amount to zero.",
                     "If no record is found, the result flag is R and the program displays 'No customer was found'."):
            with self.subTest(text=text):
                answer, ledger = self.review(text, body)
                self.assertEqual(answer, text)
                self.assertFalse(ledger.to_dict()["all_claims_supported"])

    def test_distinct_source_occurrences_are_not_merged_in_ledger(self):
        supplied = page("IF FLAG = 1\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2\nELSE\n"
                        "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2\nEND-IF.")
        facts = supplied_statement_facts(supplied, source_format="free")
        computes = [fact for fact in facts if fact["predicate"] == "assignment_statement"]
        self.assertEqual(len(computes), 2)
        self.assertEqual(len({fact["occurrence_id"] for fact in computes}), 2)
        answer, ledger = review_answer_claims("OUT-AMOUNT = IN-AMOUNT * 2。[ev_formula]", [supplied],
                                               {"ev_formula": supplied}, source_format="free")
        self.assertEqual(len(ledger.entries[0].occurrence_ids), 2)


class DefaultClaimGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.path = self.source / "calculate.cbl"
        self.path.write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. CALCULATE.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 IN-AMOUNT PIC 9(6)V99.\n01 OUT-AMOUNT PIC 9(6)V99.\n"
            "PROCEDURE DIVISION.\nMAIN.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nGOBACK.\n")
        self.database = self.root / "index.sqlite"
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="local-secret")

    def ask(self, respond, **options):
        self.requests = []
        def transport(request):
            envelope = json.loads(request.body)
            payload = json.loads(envelope["messages"][-1]["content"])
            self.requests.append(payload)
            supplied = [page for context in payload["source_context"] for page in context.get("pages", [])]
            evidence = next(page for page in supplied if "COMPUTE OUT-AMOUNT" in page["source_text"])
            return respond(payload, evidence, len(self.requests))
        return run_business_chat("OUT-AMOUNT 怎样计算？", self.database, self.source, self.config,
            transport=transport, framework_reference_path="", **options)["agent_result"]

    def test_default_fake_transport_wrong_formula_with_real_id_is_locally_repaired(self):
        result = self.ask(lambda _payload, evidence, _count:
            response(f"OUT-AMOUNT = IN-AMOUNT * 999。[{evidence['evidence_id']}] 输入金额保留原字段名称。"),
            policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))
        self.assertNotIn("999", result["answer"])
        self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", result["answer"])
        self.assertIn("输入金额保留原字段名称", result["answer"])
        self.assertEqual(result["metrics"]["model_requests"], 1)
        self.assertEqual(result["claim_ledger"]["counts"]["contradicted"], 1)
        self.assertEqual(result["claims_semantically_verified"], False)
        self.assertEqual(result["narrative"]["citations"][0]["source_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_default_power_formula_correct_and_wrong_claims_use_actual_supply(self):
        self.path.write_text(self.path.read_text().replace("IN-AMOUNT * 2", "IN-AMOUNT ** 2"))
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        for expression, expected in (("IN-AMOUNT ** 2", "supported"),
                                     ("IN-AMOUNT ** 999", "contradicted"),
                                     ("999", "contradicted")):
            with self.subTest(expression=expression):
                result = self.ask(lambda _payload, evidence, _count:
                    response(f"**OUT-AMOUNT = {expression}**。[{evidence['evidence_id']}] 输入名称沿用源码。"),
                    policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))
                self.assertIn("OUT-AMOUNT = IN-AMOUNT ** 2", result["answer"])
                self.assertNotIn("999", result["answer"])
                self.assertIn("输入名称沿用源码", result["answer"])
                self.assertEqual(result["claim_ledger"]["entries"][0]["support_status"], expected)
                self.assertEqual(result["metrics"]["model_requests"], 1)

    def test_default_final_value_cannot_ignore_uncited_supplied_overwrite(self):
        filler = "\n".join("DISPLAY 'UNCHANGED'." for _ in range(700))
        self.path.write_text(self.path.read_text().replace("GOBACK.", filler + "\nMOVE 0 TO OUT-AMOUNT.\nGOBACK."))
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        supplied_overwrite = []
        def respond(payload, calc, _count):
            pages = [page for context in payload["source_context"] for page in context.get("pages", [])]
            zero = next(page for page in pages if "MOVE 0 TO OUT-AMOUNT" in page["source_text"])
            self.assertNotIn("MOVE 0 TO OUT-AMOUNT", calc["source_text"])
            self.assertNotIn("COMPUTE OUT-AMOUNT", zero["source_text"])
            self.assertNotEqual(calc["evidence_id"], zero["evidence_id"])
            self.assertEqual(calc["source_sha256"], zero["source_sha256"])
            supplied_overwrite.append(zero["evidence_id"])
            return response(f"OUT-AMOUNT = IN-AMOUNT * 2。[{calc['evidence_id']}] "
                            f"OUT-AMOUNT 最终等于 IN-AMOUNT * 2。[{calc['evidence_id']}] 输入仍需核对。")
        result = self.ask(respond, policy=AgentPolicy(max_complete_source_characters=4096,
            max_source_characters=8192, max_model_requests=1, max_answer_revisions=0))
        self.assertEqual(result["metrics"]["model_requests"], 1)
        entries = result["claim_ledger"]["entries"]
        self.assertEqual(entries[0]["support_status"], "supported")
        self.assertEqual(entries[1]["support_status"], "unsupported")
        self.assertIn(supplied_overwrite[0], entries[1]["reviewed_evidence_ids"])
        self.assertIn("OUT-AMOUNT = 0", result["answer"])
        self.assertNotIn("最终等于", result["answer"])
        self.assertIn("输入仍需核对", result["answer"])

    def test_continuation_formula_passes_the_same_gate(self):
        def respond(_payload, evidence, count):
            if count == 1:
                return response(f"输入为 IN-AMOUNT。[{evidence['evidence_id']}]\n\n后续计算为", "length")
            return response(f"OUT-AMOUNT = IN-AMOUNT * 999。[{evidence['evidence_id']}]")
        result = self.ask(respond, policy=AgentPolicy(max_model_requests=2, max_answer_revisions=0))
        self.assertEqual(result["metrics"]["model_requests"], 2)
        self.assertNotIn("999", result["answer"])
        self.assertIn("输入为 IN-AMOUNT", result["answer"])
        self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", result["answer"])

    def test_synthesis_revision_formula_passes_the_same_gate(self):
        def respond(payload, evidence, count):
            if count == 1:
                return response(f"源码涉及 IN-AMOUNT。[{evidence['evidence_id']}]")
            self.assertIn("draft_answer", payload)
            return response(f"OUT-AMOUNT = IN-AMOUNT * 999。[{evidence['evidence_id']}]")
        result = self.ask(respond, policy=AgentPolicy(max_model_requests=2, max_answer_revisions=1))
        self.assertEqual(result["metrics"]["model_requests"], 2)
        self.assertNotIn("999", result["answer"])
        self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", result["answer"])
        self.assertEqual(result["claim_ledger"]["counts"]["contradicted"], 1)

    def test_live_change_cannot_revoke_captured_or_archived_answer_evidence(self):
        original_hash = hashlib.sha256(self.path.read_bytes()).hexdigest()
        def respond(_payload, evidence, _count):
            self.path.write_text(self.path.read_text().replace("* 2", "* 3"))
            return response(f"OUT-AMOUNT = IN-AMOUNT * 2。[{evidence['evidence_id']}]")
        result = self.ask(respond, policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))
        self.assertIn("* 2", result["answer"])
        entry = result["claim_ledger"]["entries"][0]
        self.assertEqual(entry["support_status"], "supported")
        self.assertEqual(entry["source_versions"][0]["source_sha256"], original_hash)
        identifier = entry["evidence_ids"][0]
        archived = read_archived_evidence(self.database, identifier)
        self.assertEqual(archived["source_sha256"], original_hash)
        historical, ledger = review_answer_claims(result["answer"], [archived], {identifier: archived}, source_format="free")
        self.assertEqual(historical, result["answer"])
        self.assertEqual(ledger.entries[0].support_status, "supported")


if __name__ == "__main__":
    unittest.main()
