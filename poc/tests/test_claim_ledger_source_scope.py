"""Final-value limits use actual source coverage, not blanket missing-code prose."""

import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claim_ledger import review_answer_claims


def supplied(body, *, first=1, digest=None, identifier="ev_formula"):
    return {"relative_path": "rules/amount.cbl", "evidence_id": identifier,
            "source_sha256": digest or hashlib.sha256(body.encode()).hexdigest(),
            "start_line": first, "end_line": first + len(body.split("\n")) - 1,
            "source_text": body, "span_truncated": False}


def manifest(page, total):
    return {"source_manifest": [{"relative_path": page["relative_path"],
                                "sha256": page["source_sha256"], "line_count": total}]}


class ClaimLedgerSourceScopeTests(unittest.TestCase):
    def review(self, pages, working_set=None, answer=None, **options):
        answer = answer or "OUT-AMOUNT 最终等于 IN-AMOUNT * 2。[ev_formula] 已确认输入按条读取。"
        return review_answer_claims(answer, pages, {page["evidence_id"]: page for page in pages},
                                   working_set=working_set, source_format="free", **options)

    def test_complete_source_keeps_conditions_and_overwrites_without_missing_code_claim(self):
        body = "IF IN-AMOUNT > 0\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2\nELSE\nMOVE 0 TO OUT-AMOUNT\nEND-IF."
        page = supplied(body)
        answer, ledger = self.review([page], manifest(page, 5))
        self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", answer)
        self.assertIn("OUT-AMOUNT = 0", answer)
        self.assertIn("IF IN-AMOUNT > 0", answer)
        self.assertIn("相关源码已齐备", answer)
        self.assertIn("已确认输入按条读取", answer)
        self.assertNotIn("未供证代码", answer)
        self.assertNotIn("尚未纳入", answer)
        self.assertEqual(ledger.entries[0].support_status, "unsupported")
        self.assertFalse(ledger.to_dict()["prose_semantics_verified"])
        self.assertIn("source_complete_semantics_unverified",
                      [row["kind"] for row in ledger.entries[0].unresolved_requirements])

    def test_local_range_gap_is_exact_and_cannot_be_filled_with_another_version(self):
        page = supplied("COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.", first=2)
        other = supplied("MAIN.\nMOVE 0 TO OUT-AMOUNT.\nGOBACK.", identifier="ev_other")
        answer, ledger = self.review([page, other], manifest(page, 4))
        self.assertIn("rules/amount.cbl:1-1,3-4", answer)
        self.assertNotIn("相关源码已齐备", answer)
        self.assertNotIn("OUT-AMOUNT = 0", answer)
        gap = next(row for row in ledger.entries[0].unresolved_requirements if row["kind"] == "source_ranges_not_supplied")
        self.assertEqual(gap["ranges"], [{"start_line": 1, "end_line": 1}, {"start_line": 3, "end_line": 4}])
        self.assertEqual(gap["source_sha256"], page["source_sha256"])

    def test_missing_manifest_does_not_mean_that_code_was_not_supplied(self):
        page = supplied("COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.")
        answer, ledger = self.review([page])
        self.assertNotIn("未供证代码", answer)
        self.assertNotIn("尚未纳入", answer)
        self.assertNotIn("相关源码已齐备", answer)
        self.assertTrue(any(row["kind"] == "source_coverage_unestablished"
                            for row in ledger.entries[0].unresolved_requirements))

    def test_explicit_runtime_input_is_separate_from_supplied_source(self):
        page = supplied("ACCEPT IN-AMOUNT.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2.")
        answer, ledger = self.review([page], manifest(page, 2))
        self.assertIn("相关源码已齐备", answer)
        self.assertIn("IN-AMOUNT 由运行时输入", answer)
        self.assertNotIn("未提供", answer)
        self.assertTrue(any(row["kind"] == "external_runtime_input" and row["symbol"] == "IN-AMOUNT"
                            for row in ledger.entries[0].unresolved_requirements))
        page = supplied("MOVE 4 TO IN-AMOUNT.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2.")
        _, ledger = self.review([page], manifest(page, 2))
        self.assertFalse(any(row["kind"] == "external_runtime_input"
                             for row in ledger.entries[0].unresolved_requirements))

    def test_complete_literal_assignment_does_not_expand_final_value_support(self):
        page = supplied("MOVE 2 TO OUT-AMOUNT.")
        answer, ledger = self.review([page], manifest(page, 1), answer="OUT-AMOUNT 最终等于 2。[ev_formula]")
        self.assertIn("OUT-AMOUNT = 2", answer)
        self.assertEqual(ledger.entries[0].support_status, "unsupported")
        self.assertEqual(ledger.entries[0].support_scope, "source_statement_syntax")
        self.assertEqual(ledger.entries[0].revised_claims[0]["support_status"], "supported")

    def test_complete_source_qualification_obeys_answer_language(self):
        page = supplied("COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.")
        answer, _ = self.review([page], manifest(page, 1), answer="OUT-AMOUNT always = IN-AMOUNT * 2.[ev_formula]",
                               answer_language="en")
        self.assertIn("The relevant source is complete", answer)
        self.assertNotIn("code that was not supplied", answer)
        self.assertNotRegex(answer, r"[\u3400-\u9fff]")


class StraightLineFinalAssignmentTests(unittest.TestCase):
    def review(self, body="COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nGOBACK.", *, declarations=None,
               answer="OUT-AMOUNT 最终等于 IN-AMOUNT * 2。[ev_formula]", complete=True):
        source = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. AMOUNT-RULE.\nDATA DIVISION.\n"
                  "WORKING-STORAGE SECTION.\n" + (declarations or
                  "01 IN-AMOUNT PIC 9(5) VALUE 10.\n01 OUT-AMOUNT PIC 9(6).") +
                  "\nPROCEDURE DIVISION.\nMAIN.\n" + body)
        page = supplied(source)
        result, ledger = review_answer_claims(answer, [page], {page["evidence_id"]: page},
            working_set=manifest(page, page["end_line"]) if complete else None, source_format="free")
        return result, ledger

    def test_complete_simple_path_proves_conditional_last_assignment_not_invocation(self):
        answer, ledger = self.review()
        entry = ledger.entries[0]
        self.assertIn("退出前最后一次赋值", answer)
        self.assertIn("没有错误或精度损失", answer)
        self.assertNotIn("仍需核对控制流", answer)
        self.assertEqual(entry.support_status, "unsupported")
        self.assertEqual(entry.revision, "rendered_normal_path_final_assignment")
        proof = entry.revised_claims[0]
        self.assertEqual(proof["support_status"], "supported")
        self.assertEqual(proof["support_scope"], "last_assignment_on_normal_straight_line_path")
        self.assertEqual(proof["assertion"]["expression"], ["IN-AMOUNT", "*", "2"])
        self.assertIn("program_entry_executes", proof["preconditions"])
        self.assertFalse(ledger.to_dict()["prose_semantics_verified"])

    def test_later_overwrite_changes_proved_final_assignment_instead_of_promoting_first(self):
        answer, ledger = self.review("COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nMOVE 0 TO OUT-AMOUNT.\nGOBACK.")
        self.assertIn("退出前最后一次赋值", answer)
        self.assertIn("OUT-AMOUNT = 0", answer)
        self.assertNotIn("OUT-AMOUNT = IN-AMOUNT * 2", answer)
        self.assertEqual(ledger.entries[0].revised_claims[0]["assertion"]["expression"], ["0"])

    def test_two_assignments_on_one_line_bind_the_exact_last_expression(self):
        answer, ledger = self.review("COMPUTE OUT-AMOUNT = IN-AMOUNT * 2. MOVE 0 TO OUT-AMOUNT.\nGOBACK.")
        self.assertIn("退出前最后一次赋值", answer)
        self.assertIn("OUT-AMOUNT = 0", answer)
        self.assertNotIn("OUT-AMOUNT = IN-AMOUNT * 2", answer)
        self.assertEqual(ledger.entries[0].revised_claims[0]["assertion"]["expression"], ["0"])

    def test_branches_calls_early_exit_and_changed_operand_block_path_proof(self):
        bodies = ("IF IN-AMOUNT > 0\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2\nEND-IF.\nGOBACK.",
                  'COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nCALL "OTHER-RULE" USING OUT-AMOUNT.\nGOBACK.',
                  "GOBACK.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nGOBACK.",
                  "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nMOVE 0 TO IN-AMOUNT.\nGOBACK.",
                  "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2 ON SIZE ERROR MOVE 0 TO OUT-AMOUNT END-COMPUTE.\nGOBACK.",
                  "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2 NOT ON SIZE ERROR MOVE 0 TO OUT-AMOUNT END-COMPUTE.\nGOBACK.")
        for body in bodies:
            with self.subTest(body=body):
                answer, ledger = self.review(body)
                self.assertNotIn("退出前最后一次赋值", answer)
                self.assertTrue(all(entry.revision != "rendered_normal_path_final_assignment" for entry in ledger.entries))
                self.assertEqual(ledger.entries[0].support_status, "unsupported")

    def test_partial_source_and_alias_storage_cannot_prove_last_value(self):
        answer, ledger = self.review(complete=False)
        self.assertNotIn("退出前最后一次赋值", answer)
        self.assertNotEqual(ledger.entries[0].revision, "rendered_normal_path_final_assignment")
        answer, ledger = self.review(declarations=("01 IN-AMOUNT PIC 9(5) VALUE 10.\n"
            "01 OUT-AMOUNT PIC 9(6).\n01 ALIAS-AMOUNT REDEFINES OUT-AMOUNT PIC 9(6)."))
        self.assertNotIn("退出前最后一次赋值", answer)
        self.assertNotEqual(ledger.entries[0].revision, "rendered_normal_path_final_assignment")

    def test_same_sentence_statement_keeps_its_own_scope(self):
        answer, ledger = self.review("COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nMOVE 1 TO IN-AMOUNT.\nMOVE 0 TO OUT-AMOUNT.\nGOBACK.",
            answer="OUT-AMOUNT 最终等于 IN-AMOUNT * 2，IN-AMOUNT = 1。[ev_formula]")
        self.assertIn("OUT-AMOUNT = 0", answer)
        self.assertIn("IN-AMOUNT = 1", answer)
        self.assertEqual(answer.count("退出前最后一次赋值"), 2)
        # Each extracted final claim receives its own complete-path check.
        self.assertTrue(all(entry.revised_claims[0]["support_scope"] == "last_assignment_on_normal_straight_line_path"
                            for entry in ledger.entries if entry.revision == "rendered_normal_path_final_assignment"))


if __name__ == "__main__":
    unittest.main()
