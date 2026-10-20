"""Captured procedural fragments and scenario prose have separate proof roles."""

import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claim_ledger import review_answer_claims
from claim_support import MAX_SOURCE_CHARS, check_statement_support, supplied_statement_facts


FORMULA = ("IF ORDER-COUNT > 0\n"
           " COMPUTE DUE-TOTAL ROUNDED =\n"
           " ORDER-COUNT * UNIT-RATE + HANDLING-FEE\n"
           " END-COMPUTE\nEND-IF")
CALLER = ("IF ACCESS-SWITCH = 1\n"
          " IF CLEAR-SWITCH = 1\n MOVE 0 TO DUE-TOTAL\n END-IF\nEND-IF")


def page(text, identifier="ev_formula", path="active/charge-part.cpy", first=41, **changes):
    return {"evidence_id": identifier, "relative_path": path,
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "start_line": first, "end_line": first + len(text.splitlines()) - 1,
            "source_text": text, "span_truncated": False, **changes}


class CapturedCopyClaimTests(unittest.TestCase):
    def pages(self):
        return [page(FORMULA), page(CALLER, "ev_caller", "entry.cbl", 170)]

    def review(self, text, pages=None):
        pages = self.pages() if pages is None else pages
        return review_answer_claims(text, pages, {row["evidence_id"]: row for row in pages},
                                   source_format="free")

    def test_headerless_multiline_compute_keeps_exact_physical_span_and_guard(self):
        captured = page(FORMULA)
        facts = supplied_statement_facts(captured, source_format="free")
        assignment = next(fact for fact in facts if fact["predicate"] == "assignment_statement")
        self.assertEqual(assignment["statement_kind"], "COMPUTE")
        self.assertEqual(assignment["target"], "DUE-TOTAL")
        self.assertEqual(assignment["expression"], ["ORDER-COUNT", "*", "UNIT-RATE", "+", "HANDLING-FEE"])
        self.assertTrue(assignment["rounded"])
        self.assertEqual((assignment["start_line"], assignment["end_line"]), (42, 44))
        self.assertEqual(assignment["source_text"], "\n".join(FORMULA.splitlines()[1:4]))
        self.assertEqual(assignment["guards"], [{"kind": "IF", "condition": "IF ORDER-COUNT > 0",
            "outcome": True, "start_line": 41, "end_line": 41}])
        assertion = {"predicate": "assignment_statement", "statement_kind": "COMPUTE",
            "target": "DUE-TOTAL", "expression": assignment["expression"], "rounded": True}
        verdict = check_statement_support(assertion, assignment, captured)
        self.assertEqual(verdict["support_status"], "supported")
        self.assertEqual(verdict["support_scope"], "source_statement_syntax")

    def test_fixed_and_free_captured_fragments_preserve_actual_lines(self):
        for source_format, source in [("free", FORMULA), ("fixed", "\n".join(
                f"{index:06d}     {line.strip()}" for index, line in enumerate(FORMULA.splitlines(), 100)))]:
            with self.subTest(source_format=source_format):
                captured = page(source)
                facts = supplied_statement_facts(captured, source_format=source_format)
                assignments = [f for f in facts if f["statement_kind"] == "COMPUTE"]
                self.assertEqual(len(assignments), 1)
                self.assertEqual((assignments[0]["start_line"], assignments[0]["end_line"]), (42, 44))
                self.assertEqual(assignments[0]["source_text"], "\n".join(source.splitlines()[1:4]))

    def test_incomplete_opaque_directive_and_invalid_capture_are_not_promoted(self):
        invalid = [page(FORMULA.rsplit("\n", 1)[0]),
            page(FORMULA.replace("END-COMPUTE", "UNSUPPORTED-STATEMENT")),
            page(">>IF FEATURE-X\n" + FORMULA + "\n>>END-IF"),
            page(FORMULA, span_truncated=True), page(FORMULA, source_sha256="invalid"),
            page(FORMULA, end_line=400),
            page("*> " + "padding" * MAX_SOURCE_CHARS + "\n" + FORMULA)]
        for captured in invalid:
            with self.subTest(lines=captured["end_line"], truncated=captured.get("span_truncated")):
                self.assertFalse(any(f["statement_kind"] == "COMPUTE"
                    for f in supplied_statement_facts(captured, source_format="free")))

    def test_existing_program_projection_is_not_duplicated(self):
        captured = page("PROCEDURE DIVISION.\n" + FORMULA + ".")
        assignments = [f for f in supplied_statement_facts(captured, source_format="free")
                       if f["statement_kind"] == "COMPUTE"]
        self.assertEqual(len(assignments), 1)

    def test_headerless_page_end_is_not_an_implicit_compute_terminator(self):
        source = "COMPUTE DUE-TOTAL ROUNDED =\n ORDER-COUNT * UNIT-RATE"
        for suffix, expected in [("", 0), ("\n", 0), (".", 1), ("\nEND-COMPUTE", 1)]:
            with self.subTest(suffix=repr(suffix)):
                assignments = [f for f in supplied_statement_facts(page(source + suffix), source_format="free")
                               if f["statement_kind"] == "COMPUTE"]
                self.assertEqual(len(assignments), expected)
        answer, ledger = self.review("COMPUTE DUE-TOTAL ROUNDED = ORDER-COUNT * UNIT-RATE。",
                                    [page(source), self.pages()[1]])
        self.assertNotIn("可直接确认", answer)
        self.assertEqual(ledger.entries[0].support_status, "unsupported")

    def test_correct_code_block_survives_later_same_target_zero_assignment(self):
        original = "计算片段：[ev_formula]\n\n```text\n" + FORMULA + "\n```"
        answer, ledger = self.review(original)
        self.assertEqual(answer, original)
        formula = next(row for row in ledger.entries if row.claim_type == "assignment_statement")
        self.assertEqual(formula.support_status, "supported")
        self.assertEqual(formula.evidence_ids, ["ev_formula"])
        self.assertEqual(ledger.to_dict()["revised_claim_count"], 0)
        self.assertFalse(ledger.to_dict()["prose_semantics_verified"])

    def test_wrong_complete_compute_is_repaired_from_compute_not_later_move(self):
        original = "```cobol\n" + FORMULA.replace("UNIT-RATE", "999") + "\n```"
        answer, ledger = self.review(original)
        self.assertNotIn("999", answer)
        self.assertIn("ORDER-COUNT * UNIT-RATE + HANDLING-FEE", answer)
        self.assertNotIn("DUE-TOTAL = 0", answer)
        formula = next(row for row in ledger.entries if row.claim_type == "assignment_statement")
        self.assertEqual(formula.support_status, "contradicted")
        self.assertEqual({row["evidence_id"] for row in formula.revised_claims}, {"ev_formula"})

    def test_move_alone_cannot_disprove_a_named_compute_occurrence(self):
        original = "COMPUTE DUE-TOTAL ROUNDED = ORDER-COUNT * UNIT-RATE + HANDLING-FEE。"
        answer, ledger = self.review(original, [self.pages()[1]])
        self.assertEqual(answer, original)
        self.assertEqual(ledger.entries[0].support_status, "unsupported")
        self.assertIsNone(ledger.entries[0].revision)
        fact = next(f for f in supplied_statement_facts(self.pages()[1], source_format="free")
                    if f["statement_kind"] == "MOVE")
        verdict = check_statement_support({"predicate": "assignment_statement", "statement_kind": "COMPUTE",
            "target": "DUE-TOTAL", "expression": ["999"], "rounded": False}, fact, self.pages()[1])
        self.assertEqual(verdict["support_status"], "unsupported")
        self.assertEqual(verdict["reason_code"], "SOURCE_OPERATION_NOT_COMPARABLE")

    def test_input_scenario_table_preserves_cells_and_is_not_source_if_proof(self):
        original = ("| 条件输入 | 处理 |\n|---|---|\n"
            "| `ACCESS-SWITCH = 0` 时，无论 `CLEAR-SWITCH` 是 0 还是 1 | 不执行计算或覆盖 |\n"
            "| `ACCESS-SWITCH = 0` 时 | 保持此前值 |\n")
        answer, ledger = self.review(original)
        self.assertEqual(answer, original)
        scenarios = [row for row in ledger.entries if row.claim_type == "condition_scenario"]
        self.assertEqual(len(scenarios), 2)
        self.assertTrue(all(row.support_status == "unsupported" and row.revision is None for row in scenarios))
        self.assertEqual(ledger.to_dict()["counts"]["contradicted"], 0)

    def test_nonexecution_paths_and_given_inputs_are_retained_without_verification(self):
        for original in ["`ACCESS-SWITCH = 0` 时，即使 `CLEAR-SWITCH = 1` 也不会把 DUE-TOTAL 置 0。",
                         "当 ACCESS-SWITCH = 0 时不执行计算。",
                         "假设 ACCESS-SWITCH = 0 时执行场景分析。",
                         "When ACCESS-SWITCH = 0, calculation does not execute.",
                         "When ACCESS-SWITCH = 0, computation is skipped."]:
            with self.subTest(original=original):
                answer, ledger = self.review(original)
                self.assertEqual(answer, original)
                scenarios = [row for row in ledger.entries if row.claim_type == "condition_scenario"]
                self.assertTrue(scenarios)
                self.assertTrue(all(row.support_status == "unsupported" for row in scenarios))
                self.assertEqual(ledger.to_dict()["revised_claim_count"], 0)

    def test_actual_wrong_source_if_and_positive_execution_rule_still_fail(self):
        for original in ["`IF ACCESS-SWITCH = 9`。[ev_caller]",
                         "只有 ACCESS-SWITCH = 9 时才执行计算。[ev_caller]",
                         "输入检查只在 ACCESS-SWITCH = 9 时执行计算。[ev_caller]",
                         "只有 ACCESS-SWITCH = 9 时，执行计算，但后续不执行归档。[ev_caller]",
                         "只有 ACCESS-SWITCH = 9 时，系统才执行计算，但后续不执行归档。[ev_caller]",
                         "只有 ACCESS-SWITCH = 9 时, 执行计算，但后续不执行归档。[ev_caller]",
                         "When ACCESS-SWITCH = 9, calculation executes, but later steps are skipped. [ev_caller]",
                         "只在 ACCESS-SWITCH = 9 时执行计算，后续不执行另外的步骤。[ev_caller]"]:
            with self.subTest(original=original):
                answer, ledger = self.review(original)
                self.assertNotIn("= 9", answer)
                self.assertIn("ACCESS-SWITCH = 1", answer)
                self.assertTrue(any(row.support_status == "contradicted" for row in ledger.entries))

    def test_scenario_role_does_not_hide_invalid_citation_or_independent_wrong_formula(self):
        original = "ACCESS-SWITCH = 0 时不执行计算。[ev_missing]"
        answer, ledger = self.review(original)
        self.assertNotIn("ev_missing", answer)
        self.assertNotIn("ACCESS-SWITCH = 0", answer)
        self.assertEqual(ledger.entries[0].invalid_evidence_ids, ["ev_missing"])
        original = ("ACCESS-SWITCH = 0 时不执行计算。"
                    "COMPUTE DUE-TOTAL ROUNDED = ORDER-COUNT * 999。[ev_formula]")
        answer, ledger = self.review(original)
        self.assertIn("ACCESS-SWITCH = 0", answer)
        self.assertNotIn("999", answer)
        self.assertTrue(any(row.support_status == "contradicted" for row in ledger.entries))

    def test_unsourced_initialization_claim_is_not_verified_by_formula_projection(self):
        original = "首次运行按常规初始化为 0.00，但源码没有显式初始化。"
        answer, ledger = self.review(original)
        self.assertEqual(answer, original)
        self.assertTrue(all(row.support_status == "unsupported" for row in ledger.entries))
        self.assertFalse(ledger.to_dict()["all_claims_supported"])
        self.assertFalse(ledger.to_dict()["prose_semantics_verified"])


if __name__ == "__main__":
    unittest.main()
