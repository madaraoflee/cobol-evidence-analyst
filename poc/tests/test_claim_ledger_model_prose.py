"""Regressions from a real model answer to neutral, locally authored source."""

import hashlib
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claim_ledger import review_answer_claims


FEE_ID = "ev_page_eedf37efdaa217d174b754ef"
ENTRY_ID = "ev_page_110efdf954c5348f26e847d6"
FEE = """IDENTIFICATION DIVISION.
PROGRAM-ID. FEE-RULE.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 RATE PIC 9V99 VALUE 1.25.
LINKAGE SECTION.
01 BASE-AMOUNT PIC 9(7)V99.
01 FINAL-FEE PIC 9(7)V99.
01 REQUEST-STATE PIC X(6).
PROCEDURE DIVISION USING BASE-AMOUNT FINAL-FEE REQUEST-STATE.
    COMPUTE FINAL-FEE ROUNDED = BASE-AMOUNT * RATE / 100
    END-COMPUTE
    IF FINAL-FEE < 2
        MOVE 2 TO FINAL-FEE
    END-IF
    IF FINAL-FEE > 15
        MOVE 15 TO FINAL-FEE
    END-IF
    IF REQUEST-STATE = 'REVIEW'
        MOVE 0 TO FINAL-FEE
    END-IF
    GOBACK.
"""
ENTRY = """IDENTIFICATION DIVISION.
PROGRAM-ID. ENTRY-RULE.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 BASE-AMOUNT PIC 9(7)V99 VALUE 500.
01 FINAL-FEE PIC 9(7)V99 VALUE 0.
01 REQUEST-STATE PIC X(6) VALUE 'NORMAL'.
01 REQUEST-KIND PIC X(8) VALUE 'STANDARD'.
PROCEDURE DIVISION.
    IF REQUEST-KIND = 'STANDARD'
""" + "        CONTINUE\n" * 300 + """        CALL 'FEE-RULE' USING BASE-AMOUNT FINAL-FEE REQUEST-STATE
        END-CALL
    END-IF
    GOBACK.
"""


def page(source, name="fee.cbl", identifier=FEE_ID):
    return {"evidence_id": identifier, "relative_path": name, "source_text": source,
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(), "span_truncated": False,
            "start_line": 1, "end_line": len(source.splitlines())}


class ClaimLedgerModelProseTests(unittest.TestCase):
    def review(self, answer, pages=None, **options):
        pages = pages or [page(FEE), page(ENTRY, "entry.cbl", ENTRY_ID)]
        return review_answer_claims(answer, pages, {row["evidence_id"]: row for row in pages},
                                   source_format="free", **options)

    def test_actual_model_answer_keeps_conditions_initial_value_and_table_exactly(self):
        original = (Path(__file__).resolve().parents[1] / "fixtures/claim-review-neutral/live-fee-answer.md").read_text()
        cache = {}
        for _ in range(2):
            answer, ledger = self.review(original, source_fact_cache=cache)
            self.assertEqual(answer, original)
            self.assertEqual(ledger.to_dict()["revised_claim_count"], 0)
            self.assertFalse(ledger.to_dict()["prose_semantics_verified"])
            value = next(row for row in ledger.entries if row.claim_type == "declaration_value")
            self.assertEqual(value.support_status, "supported")
            self.assertEqual(value.support_scope, "source_declared_initial_value")
            self.assertEqual(value.source_versions[0]["start_line"], 5)
        # This typo is present in the saved raw model response, not introduced
        # by claim review. The checker must not disguise the source of it.
        self.assertIn("把手最终续费", original)

    def test_wrong_only_when_condition_is_repaired_and_other_body_survives(self):
        text = "只有 `REQUEST-KIND = 'OTHER'` 时才发起调用。[" + ENTRY_ID + "]保留独立说明。"
        answer, ledger = self.review(text)
        self.assertNotIn("'OTHER'", answer)
        self.assertIn("REQUEST-KIND = 'STANDARD'", answer)
        self.assertIn("保留独立说明", answer)
        self.assertTrue(any(row.support_status == "contradicted" for row in ledger.entries))

    def test_table_wrong_formula_is_repaired_with_cells_and_adjacent_rows_preserved(self):
        text = ("| 项目 | 算式 | 说明 |\n|---|---|---|\n"
                "| 正常计算 | `FINAL-FEE ROUNDED = BASE-AMOUNT * 999` | 原说明[" + FEE_ID + "] |\n"
                "| 示例 | 6.25 | 示例保留 |\n")
        answer, ledger = self.review(text)
        self.assertNotIn("999", answer)
        self.assertIn("FINAL-FEE ROUNDED = BASE-AMOUNT * RATE / 100", answer)
        self.assertEqual(answer.splitlines()[0:2], text.splitlines()[0:2])
        self.assertEqual(answer.splitlines()[-1], text.splitlines()[-1])
        self.assertEqual([line.count("|") for line in answer.splitlines()], [4, 4, 4, 4])
        self.assertIn("原说明", answer)
        self.assertTrue(any(row.support_status == "contradicted" for row in ledger.entries))

    def test_wrong_table_header_condition_is_checked_without_deleting_header(self):
        text = "| 本金 | `REQUEST-STATE='OTHER'` 最终收费 |\n|---|---|\n| 100 | 0 |\n"
        answer, ledger = self.review(text)
        self.assertNotIn("'OTHER'", answer)
        self.assertIn("REQUEST-STATE = 'REVIEW'", answer)
        self.assertTrue(answer.startswith("| 本金 |"))
        self.assertEqual([line.count("|") for line in answer.splitlines()], [3, 3, 3])
        self.assertTrue(any(row.support_status == "contradicted" for row in ledger.entries))

    def test_declaration_value_does_not_prove_a_final_or_runtime_value(self):
        source = FEE.replace("    GOBACK.", "    MOVE 2 TO RATE.\n    GOBACK.")
        initial = "声明的初始值 `RATE = 1.25`。"
        answer, ledger = self.review(initial, [page(source)])
        self.assertEqual(answer, initial)
        self.assertEqual(ledger.entries[0].support_scope, "source_declared_initial_value")
        answer, ledger = self.review("声明的初始值 `RATE = 1.25`，RATE 最终 = 1.25。", [page(source)])
        self.assertTrue(any(row.claim_type == "business_state_value" and row.support_status != "supported"
                            for row in ledger.entries))
        self.assertIn("RATE 最终 = 1.25", answer)
        linkage = FEE.replace("01 RATE PIC 9V99 VALUE 1.25.", "01 RATE PIC 9V99.")
        _, ledger = self.review(initial, [page(linkage)])
        self.assertEqual(ledger.entries[0].support_status, "unsupported")

    def test_wrong_declared_initial_literal_is_repaired_to_the_value_clause(self):
        answer, ledger = self.review("工作存储中的 `RATE = 9.99`。")
        self.assertNotIn("9.99", answer)
        self.assertIn("声明的初始值：`RATE = 1.25`", answer)
        self.assertTrue(any(row.support_status == "contradicted" for row in ledger.entries))

    def test_uncited_same_name_in_two_programs_stays_unverified(self):
        other = FEE.replace("FEE-RULE", "OTHER-RULE").replace("VALUE 1.25", "VALUE 2.50")
        text = "工作存储中的 `RATE = 1.25`。"
        answer, ledger = self.review(text, [page(FEE), page(other, "other.cbl", "ev_other")])
        self.assertEqual(answer, text)
        self.assertEqual(ledger.entries[0].support_status, "unsupported")
        self.assertEqual(ledger.entries[0].reason_code, "DECLARATION_IDENTITY_AMBIGUOUS")

    def test_assignment_language_cannot_borrow_source_if_condition(self):
        original = "程序执行赋值 `REQUEST-STATE = 'REVIEW'`。"
        answer, ledger = self.review(original)
        self.assertEqual(answer, original)
        self.assertEqual(ledger.entries[0].claim_type, "assignment_statement")
        self.assertEqual(ledger.entries[0].reason_code, "SOURCE_ASSERTION_NOT_LOCALLY_VERIFIABLE")
        self.assertFalse(any(row.support_status == "supported" for row in ledger.entries))

    def test_actual_cross_program_derivation_is_retained_and_caller_citation_rebound(self):
        original = (Path(__file__).resolve().parents[1] / "fixtures/claim-review-neutral/live-entry-answer.md").read_text()
        answer, ledger = self.review(original)
        self.assertEqual(re.sub(r"\[ev_[^\]]+\]", "", answer), re.sub(r"\[ev_[^\]]+\]", "", original))
        self.assertNotIn("该处公式、条件或效果尚未得到", answer)
        rebound = [row for row in ledger.entries if row.revision == "declaration_citation_rebound"]
        self.assertEqual({row.target_entity for row in rebound}, {"BASE-AMOUNT", "REQUEST-STATE"})
        self.assertTrue(all(row.evidence_ids == [ENTRY_ID] for row in rebound))
        derived = [row for row in ledger.entries if row.claim_type == "derived_numeric_result"]
        self.assertEqual(len(derived), 1)
        self.assertEqual(derived[0].expression, ["6.25"])
        self.assertEqual(derived[0].support_status, "unsupported")
        self.assertEqual(derived[0].reason_code, "DERIVED_RESULT_NOT_FORMALLY_VERIFIED")
        second, _ = self.review(answer)
        self.assertEqual(second, answer, "evidence rebinding must not append duplicate citations")

    def test_numeric_initial_value_equivalence_is_exact_and_does_not_round(self):
        for literal in ["500", "500.00", "+500.000"]:
            with self.subTest(literal=literal):
                text = f"调用方初值 `BASE-AMOUNT = {literal}`。[{ENTRY_ID}]"
                answer, ledger = self.review(text)
                self.assertEqual(answer, text)
                self.assertEqual(ledger.entries[0].support_status, "supported")
                self.assertEqual(ledger.entries[0].support_scope, "source_declared_initial_value")
        for literal in ["500.01", "500.00000000000000000001"]:
            with self.subTest(literal=literal):
                answer, ledger = self.review(f"调用方初值 `BASE-AMOUNT = {literal}`。[{ENTRY_ID}]")
                self.assertNotIn(literal, answer)
                self.assertIn("BASE-AMOUNT = 500", answer)
                self.assertEqual(ledger.entries[0].support_status, "contradicted")

    def test_derived_numeric_result_does_not_bypass_wrong_formula_condition_or_input(self):
        cases = [
            ("按调用方初值 `BASE-AMOUNT = 500.00` 推导：`FINAL-FEE ROUNDED = BASE-AMOUNT * 999`。因此 `FINAL-FEE` 最终为6.25。", "999"),
            ("按调用方初值 `BASE-AMOUNT = 500.00` 推导：只有 `REQUEST-KIND='OTHER'` 时调用。因此 `FINAL-FEE` 最终为6.25。", "'OTHER'"),
            ("按调用方初值 `BASE-AMOUNT = 500.01` 推导：500.01 × 1.25 ÷ 100。因此 `FINAL-FEE` 最终为6.25。", "500.01"),
            ("按调用方初值 `REQUEST-STATE = 'REVIEW'` 推导：没有额外假设。因此 `FINAL-FEE` 最终为6.25。", "初值 `REQUEST-STATE = 'REVIEW'`")]
        for text, wrong in cases:
            with self.subTest(text=text):
                answer, ledger = self.review(text)
                self.assertNotIn(wrong, answer)
                self.assertTrue(any(row.revision for row in ledger.entries))
                derived = [row for row in ledger.entries if row.claim_type == "derived_numeric_result"]
                self.assertEqual(len(derived), 1)
                self.assertEqual(derived[0].support_status, "unsupported")

    def test_explicit_assignment_does_not_borrow_later_derivation_context(self):
        for text in ["源码设置 FINAL-FEE 最终为999。", "源码设置 FINAL-FEE 最终为999。按调用方初值 BASE-AMOUNT=500 推导：正常收费。",
                     "源码设置 FINAL-FEE 最终为999，然后按调用方初值 BASE-AMOUNT=500 推导：正常收费。"]:
            with self.subTest(text=text):
                answer, ledger = self.review(text)
                self.assertNotIn("999", answer)
                self.assertFalse(any(row.claim_type == "derived_numeric_result" for row in ledger.entries))

    def test_explicit_source_assignments_cannot_borrow_a_derivation_exception(self):
        for statement in ["源码最终 MOVE 999 TO FINAL-FEE", "COMPUTE FINAL-FEE 最终为999",
                          "源码执行赋值 FINAL-FEE 最终为999", "最终写入 FINAL-FEE=999",
                          "源码最终设置 `FINAL-FEE = 999`", "源码最终设为 `FINAL-FEE = 999`",
                          "源码最终置为 `FINAL-FEE = 999`", "源碼最終設置 `FINAL-FEE = 999`",
                          "源碼最終設為 `FINAL-FEE = 999`", "源碼最終置為 `FINAL-FEE = 999`",
                          "set FINAL-FEE finally = 999", "assign FINAL-FEE finally = 999"]:
            with self.subTest(statement=statement):
                text = "按给定输入 BASE-AMOUNT=500 推导：" + statement + "。"
                answer, ledger = self.review(text)
                self.assertNotIn("999", answer)
                self.assertFalse(any(row.claim_type == "derived_numeric_result" for row in ledger.entries))
                self.assertTrue(any(row.support_status == "contradicted" for row in ledger.entries))

    def test_numeric_derivation_is_unverified_even_when_its_value_looks_plausible(self):
        text = "按给定输入 BASE-AMOUNT=500 推导：FINAL-FEE 最终为999。"
        answer, ledger = self.review(text)
        self.assertIn("FINAL-FEE 最终为999", answer)
        derived = next(row for row in ledger.entries if row.claim_type == "derived_numeric_result")
        self.assertEqual(derived.support_status, "unsupported")
        self.assertEqual(derived.support_scope, "unverified_conditional_numeric_result")

    def test_actual_default_list_condition_and_width_drafts_remain_exactly_unchanged(self):
        directory = Path(__file__).resolve().parents[1] / "fixtures/claim-review-neutral"
        for name in ["live-fee-defaults-draft.md", "live-fee-condition-draft.md", "live-entry-width-draft.md",
                     "live-entry-state-values-draft.md", "live-entry-uncited-list-draft.md",
                     "live-fee-intermediate-scalar-draft.md", "live-fee-chained-equation-draft.md"]:
            with self.subTest(name=name):
                original = (directory / name).read_text()
                answer, ledger = self.review(original)
                self.assertEqual(answer, original)
                self.assertEqual(ledger.to_dict()["revised_claim_count"], 0)
                self.assertFalse(ledger.to_dict()["all_claims_supported"])

    def test_partial_mathematical_derivation_cannot_become_a_source_contradiction(self):
        expressions = [
            "FINAL-FEE = BASE-AMOUNT × 1.25 ÷ 100 = BASE-AMOUNT × 1.25%",
            "FINAL-FEE = BASE-AMOUNT * 1.25 / 100 = BASE-AMOUNT * 0.0125",
            "FINAL-FEE = BASE-AMOUNT * 999%",
            "FINAL-FEE = BASE-AMOUNT × 999",
            "COMPUTE FINAL-FEE = BASE-AMOUNT * 999 +",
            "COMPUTE FINAL-FEE = BASE-AMOUNT * (999",
            "COMPUTE FINAL-FEE = BASE-AMOUNT * 999 EXTRA-TOKEN",
        ]
        for expression in expressions:
            for citation in ("", f"[{FEE_ID}]"):
                with self.subTest(expression=expression, citation=citation):
                    original = expression + "。" + citation
                    answer, ledger = self.review(original)
                    self.assertEqual(answer, original)
                    self.assertTrue(all(row.support_status == "unsupported" for row in ledger.entries))
                    self.assertTrue(all(row.revision is None for row in ledger.entries))
                    self.assertFalse(ledger.to_dict()["prose_semantics_verified"])

    def test_complete_ascii_source_formula_and_move_still_reject_actual_mismatch(self):
        for expression in ["COMPUTE FINAL-FEE ROUNDED = BASE-AMOUNT * 999.",
                           "COMPUTE FINAL-FEE ROUNDED = BASE-AMOUNT * 999 END-COMPUTE.",
                           "COMPUTE FINAL-FEE ROUNDED = (BASE-AMOUNT * 999) END-COMPUTE.",
                           "MOVE 999 TO FINAL-FEE.", "FINAL-FEE = BASE-AMOUNT * 999。"]:
            with self.subTest(expression=expression):
                answer, ledger = self.review(expression + f"[{FEE_ID}]")
                self.assertNotIn("999", answer)
                self.assertTrue(any(row.support_status == "contradicted" for row in ledger.entries))
        correct = f"COMPUTE FINAL-FEE ROUNDED = BASE-AMOUNT * RATE / 100 END-COMPUTE。[{FEE_ID}]"
        answer, ledger = self.review(correct)
        self.assertEqual(answer, correct)
        self.assertTrue(any(row.support_status == "supported" for row in ledger.entries))

    def test_unknown_math_does_not_hide_separate_wrong_source_assignment_or_condition(self):
        derivation = "FINAL-FEE = BASE-AMOUNT × 1.25 ÷ 100 = BASE-AMOUNT × 1.25%。"
        for wrong in ["源码执行 MOVE 999 TO FINAL-FEE。", "COMPUTE FINAL-FEE = BASE-AMOUNT * 999。",
                      "只有 REQUEST-KIND='OTHER' 时调用。"]:
            with self.subTest(wrong=wrong):
                answer, ledger = self.review(derivation + wrong)
                self.assertIn(derivation, answer)
                self.assertNotIn("999", answer)
                self.assertNotIn("'OTHER'", answer)
                self.assertTrue(any(row.claim_type == "mathematical_expression" and row.support_status == "unsupported"
                                    for row in ledger.entries))
                self.assertTrue(any(row.support_status == "contradicted" for row in ledger.entries))

    def test_default_list_is_declaration_only_until_another_clause_or_action(self):
        original = ("ENTRY-RULE 的默认值包括 REQUEST-KIND='STANDARD'、REQUEST-STATE='NORMAL'、"
                    "FINAL-FEE=0、BASE-AMOUNT=500；下文按调用前将BASE-AMOUNT设为该值的假设计算。")
        answer, ledger = self.review(original)
        self.assertEqual(answer, original)
        declarations = [row for row in ledger.entries if row.claim_type == "declaration_value"]
        self.assertEqual(len(declarations), 4)
        self.assertTrue(all(row.support_status == "supported" for row in declarations))
        for wrong in [original.replace("BASE-AMOUNT=500", "BASE-AMOUNT=500.01"),
                      original.replace("FINAL-FEE=0", "FINAL-FEE=999"),
                      "默认值包括 FINAL-FEE=0；源码设置 FINAL-FEE=999。",
                      "默认值包括 BASE-AMOUNT=500、源码设置 FINAL-FEE=999。"]:
            with self.subTest(wrong=wrong):
                answer, ledger = self.review(wrong)
                self.assertTrue(any(row.support_status == "contradicted" for row in ledger.entries))
                self.assertNotIn("999", answer)
                self.assertNotIn("500.01", answer)

    def test_local_condition_cues_do_not_borrow_trailing_initial_value(self):
        for text in ["ENTRY-RULE只在 REQUEST-KIND='STANDARD' 时执行调用；FINAL-FEE保持原值（其初值为0）。",
                     "先执行最低收费、最高收费、REQUEST-STATE='REVIEW' 时置0这三个判断。"]:
            with self.subTest(text=text):
                answer, ledger = self.review(text)
                self.assertEqual(answer, text)
                self.assertEqual(ledger.entries[0].claim_type, "condition_statement")
                self.assertEqual(ledger.entries[0].support_status, "supported")
                wrong = text.replace("'STANDARD'", "'OTHER'").replace("'REVIEW'", "'OTHER'")
                answer, ledger = self.review(wrong)
                self.assertNotIn("'OTHER'", answer)
                self.assertTrue(any(row.support_status == "contradicted" for row in ledger.entries))

    def test_field_width_is_prose_and_field_name_is_not_a_final_result_marker(self):
        original = "REQUEST-KIND 为 8 位定长字符，与字面量比较。"
        answer, ledger = self.review(original)
        self.assertEqual(answer, original)
        self.assertEqual(ledger.entries[0].support_status, "unsupported")
        self.assertEqual(ledger.entries[0].claim_type, "business_explanation")
        for wrong in ["COMPUTE FINAL-FEE = 999。", "按给定输入 BASE-AMOUNT=500 推导：COMPUTE FINAL-FEE = 999。"]:
            with self.subTest(wrong=wrong):
                answer, ledger = self.review(wrong)
                self.assertNotIn("999", answer)
                self.assertFalse(any(row.claim_type == "derived_numeric_result" for row in ledger.entries))

    def test_uncited_unparsed_equalities_and_conditional_final_facts_never_erase_prose(self):
        examples = ["- REQUEST-KIND = 'STANDARD' 成立：进入 IF 体。",
                    "- BASE-AMOUNT = 500\n- REQUEST-STATE = 'NORMAL'\n- REQUEST-KIND = 'STANDARD'",
                    f"如果状态是REVIEW，最后会被 MOVE 0 TO FINAL-FEE 覆盖，手续费为0。[{FEE_ID}]"]
        for original in examples:
            with self.subTest(original=original):
                answer, ledger = self.review(original)
                self.assertEqual(answer, original)
                self.assertFalse(ledger.to_dict()["all_claims_supported"])
                self.assertTrue(all(row.revision is None for row in ledger.entries))
                self.assertTrue(any(row.support_status == "unsupported" for row in ledger.entries))

    def test_unverified_checker_result_cannot_be_upgraded_to_contradiction(self):
        from unittest.mock import patch
        original = f"FINAL-FEE ROUNDED = BASE-AMOUNT * 999。[{FEE_ID}]"
        uncertain = {"support_status": "unsupported", "reason_code": "SOURCE_FORM_NOT_SUPPORTED",
                     "checker_version": "bounded-test", "support_scope": "source_statement_syntax"}
        with patch("claim_ledger.check_statement_support", return_value=uncertain):
            answer, ledger = self.review(original)
        self.assertEqual(answer, original)
        self.assertEqual(ledger.entries[0].support_status, "unsupported")
        self.assertIsNone(ledger.entries[0].revision)
        answer, ledger = self.review(original)
        self.assertNotIn("999", answer)
        self.assertEqual(ledger.entries[0].support_status, "contradicted")

    def test_natural_scalar_state_and_result_without_source_assignment_role_stay_unverified(self):
        for original in ["REQUEST-STATE 为 'NORMAL'。", "FINAL-FEE 最终为6.25。", "FINAL-FEE 最终为999。",
                         "FINAL-FEE 最终为999。按调用方初值 BASE-AMOUNT=500 推导：正常收费。",
                         "FINAL-FEE is 6.25.", "- ① 后 FINAL-FEE = 1.25；", "FINAL-FEE = 999。",
                         "REQUEST-STATE = 'NORMAL'。"]:
            with self.subTest(original=original):
                answer, ledger = self.review(original)
                self.assertEqual(answer, original)
                state = next(row for row in ledger.entries if row.claim_type == "business_state_value")
                self.assertEqual(state.support_status, "unsupported")
                self.assertEqual(state.reason_code, "BUSINESS_STATE_VALUE_NOT_FORMALLY_VERIFIED")
                self.assertIsNone(state.revision)
        answer, ledger = self.review(f"FINAL-FEE 为6.25。[ev_missing]")
        self.assertNotIn("6.25", answer)
        self.assertEqual(ledger.entries[0].invalid_evidence_ids, ["ev_missing"])

    def test_callee_same_name_assignment_cannot_refute_cited_caller_state(self):
        original = f"COMPUTE FINAL-FEE = 999。[{ENTRY_ID}]"
        answer, ledger = self.review(original)
        self.assertEqual(answer, original)
        self.assertEqual(ledger.entries[0].support_status, "unsupported")
        self.assertEqual(ledger.entries[0].reason_code, "SOURCE_ASSERTION_NOT_LOCALLY_VERIFIABLE")
        self.assertIsNone(ledger.entries[0].revision)
        for wrong in [f"COMPUTE FINAL-FEE = 999。[{FEE_ID}]", f"MOVE 999 TO FINAL-FEE。[{FEE_ID}]",
                      f"FINAL-FEE = BASE-AMOUNT * 999。[{FEE_ID}]",
                      f"只有 REQUEST-KIND='OTHER' 时调用。[{ENTRY_ID}]"]:
            with self.subTest(wrong=wrong):
                answer, ledger = self.review(wrong)
                self.assertNotIn("999", answer)
                self.assertNotIn("'OTHER'", answer)
                self.assertTrue(any(row.support_status == "contradicted" for row in ledger.entries))

    def test_cited_assignment_repair_does_not_mix_another_programs_same_named_field(self):
        def source(program, value):
            return (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {program}.\nDATA DIVISION.\n"
                    "WORKING-STORAGE SECTION.\n01 RESULT-AMOUNT PIC 9(3).\n"
                    f"PROCEDURE DIVISION.\nMOVE {value} TO RESULT-AMOUNT.\nGOBACK.")
        pages = [page(source("CALLER-RULE", 1), "caller.cbl", "ev_caller"),
                 page(source("CALLEE-RULE", 9), "callee.cbl", "ev_callee")]
        statement = "CALLER-RULE 源码设置 RESULT-AMOUNT = 999。"
        answer, ledger = self.review(statement + "[ev_caller]", pages)
        self.assertNotIn("999", answer)
        self.assertIn("RESULT-AMOUNT = 1", answer)
        self.assertNotIn("RESULT-AMOUNT = 9", answer)
        self.assertNotIn("ev_callee", answer)
        entry = ledger.entries[0]
        self.assertEqual(entry.support_status, "contradicted")
        self.assertEqual([row["evidence_id"] for row in entry.revised_claims], ["ev_caller"])
        # Missing or invalid citations retain the existing supplied-fact
        # recovery path, rather than inheriting a valid citation's scope.
        for citation in ("", "[ev_missing]", "[ev_caller][ev_missing]"):
            with self.subTest(citation=citation):
                answer, ledger = self.review(statement + citation, pages)
                self.assertNotIn("999", answer)
                self.assertIn("RESULT-AMOUNT = 1", answer)
                self.assertIn("RESULT-AMOUNT = 9", answer)
                self.assertEqual({row["evidence_id"] for row in ledger.entries[0].revised_claims},
                                 {"ev_caller", "ev_callee"})

    def test_declaration_colon_list_inherits_role_across_introductory_clauses_only(self):
        original = ("按 ENTRY-RULE 的声明初值，首次初始化并进入调用时，传入的值分别是："
                    "BASE-AMOUNT = 500、FINAL-FEE = 0、REQUEST-STATE = 'NORMAL'。")
        answer, ledger = self.review(original)
        self.assertEqual(answer, original)
        self.assertEqual([row.claim_type for row in ledger.entries], ["declaration_value"] * 3)
        self.assertTrue(all(row.support_status == "supported" for row in ledger.entries))
        wrong = original.replace("FINAL-FEE = 0", "FINAL-FEE = 999")
        answer, ledger = self.review(wrong)
        self.assertNotIn("999", answer)
        self.assertTrue(any(row.support_status == "contradicted" for row in ledger.entries))


if __name__ == "__main__":
    unittest.main()
