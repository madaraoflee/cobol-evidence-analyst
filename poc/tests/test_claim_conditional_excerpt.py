"""Conditional source excerpts must not lose ELSE or invent its control role."""
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2])]
from claim_ledger import review_answer_claims
from claim_support import supplied_statement_facts
from draft_validation import validate_answer_draft
from tools.business_onboarding_fixture import sources


class ConditionalExcerptClaimTests(unittest.TestCase):
    def page(self, source=None, identifier="ev_balance", path="balance.cbl"):
        source = sources()[path] if source is None else source
        return {"evidence_id": identifier, "relative_path": path, "source_text": source,
                "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "start_line": 1, "end_line": len(source.splitlines()), "span_truncated": False}

    def review(self, text, pages=None):
        pages = [self.page()] if pages is None else pages
        answer, ledger = review_answer_claims(text, pages, {p["evidence_id"]: p for p in pages},
                                             source_format="free")
        return answer, ledger.to_dict()

    def test_inline_else_move_uses_actual_false_guard_and_physical_source_span(self):
        page = self.page()
        facts = supplied_statement_facts(page, source_format="free")
        moves = [fact for fact in facts if fact["predicate"] == "assignment_statement"]
        self.assertEqual([(f["start_line"], f["end_line"], f["expression"]) for f in moves],
                         [(18, 18, ["'00'"]), (19, 19, ["'90'"])])
        self.assertEqual([f["guards"][0]["outcome"] for f in moves], ["true", "false"])
        self.assertEqual(moves[1]["source_text"], "ELSE MOVE '90' TO RESPONSE-CODE")
        self.assertEqual(moves[1]["guards"][0]["condition"], "IF SQLCODE = 0")

    def test_incomplete_quoted_branch_preserves_business_premise_but_is_not_verified(self):
        original = ("**不是。** 源码里的失败分支是 "
                    "`IF SQLCODE = 0 ... ELSE MOVE '90' TO RESPONSE-CODE`，"
                    "这是**查询语句正常执行返回之后**的控制流判断。[ev_balance]")
        answer, ledger = self.review(original)
        self.assertEqual(answer, original)
        self.assertEqual(ledger["revised_claim_count"], 0)
        self.assertIn("INCOMPLETE_CONTROL_EXCERPT_NOT_FORMALLY_VERIFIED",
                      [row["reason_code"] for row in ledger["entries"]])
        self.assertFalse(ledger["all_claims_supported"])

    def test_complete_if_else_keeps_both_branch_values(self):
        original = ("`IF SQLCODE = 0 MOVE '00' TO RESPONSE-CODE "
                    "ELSE MOVE '90' TO RESPONSE-CODE END-IF`。[ev_balance]")
        answer, ledger = self.review(original)
        self.assertEqual(answer, original)
        assignments = [row for row in ledger["entries"] if row["claim_type"] == "assignment_statement"]
        self.assertEqual(len(assignments), 2)
        self.assertEqual([row["support_status"] for row in assignments], ["supported", "supported"])
        self.assertEqual(ledger["revised_claim_count"], 0)
        self.assertFalse(ledger["prose_semantics_verified"])

    def test_complete_wrong_branch_value_or_swapped_branches_still_repaired(self):
        for left, right in [("'00'", "'99'"), ("'90'", "'90'"), ("'90'", "'00'")]:
            with self.subTest(left=left, right=right):
                original = (f"`IF SQLCODE = 0 MOVE {left} TO RESPONSE-CODE "
                            f"ELSE MOVE {right} TO RESPONSE-CODE END-IF`。[ev_balance]")
                answer, ledger = self.review(original)
                self.assertNotEqual(answer, original)
                self.assertGreater(ledger["counts"]["contradicted"], 0)
                self.assertNotIn("'99'", answer)
                self.assertIn("RESPONSE-CODE = '00'", answer)
                self.assertIn("RESPONSE-CODE = '90'", answer)

    def test_wrong_if_and_independent_move_or_formula_are_not_hidden_by_excerpt(self):
        for original in (
            "`IF SQLCODE = 999 ... ELSE MOVE '90' TO RESPONSE-CODE`。[ev_balance]",
            "`IF SQLCODE = 0 ... ELSE MOVE '90' TO RESPONSE-CODE`；"
            "MOVE '999' TO RESPONSE-CODE。[ev_balance]",
        ):
            with self.subTest(original=original):
                answer, ledger = self.review(original)
                self.assertNotIn("999", answer)
                self.assertGreater(ledger["counts"]["contradicted"], 0)
        source = sources()["balance.cbl"].replace("GOBACK.", "COMPUTE DUE-AMOUNT = INPUT-AMOUNT * 2.\nGOBACK.")
        original = ("`IF SQLCODE = 0 ... ELSE MOVE '90' TO RESPONSE-CODE`；"
                    "COMPUTE DUE-AMOUNT = INPUT-AMOUNT * 999。[ev_balance]")
        answer, ledger = self.review(original, [self.page(source)])
        self.assertNotIn("999", answer)
        self.assertIn("INPUT-AMOUNT * 2", answer)
        self.assertGreater(ledger["counts"]["contradicted"], 0)

    def test_fake_citation_and_incomplete_source_cannot_be_supported(self):
        original = "`IF SQLCODE = 0 ... ELSE MOVE '90' TO RESPONSE-CODE`。[ev_missing]"
        answer, ledger = self.review(original)
        self.assertNotIn("ev_missing", answer)
        self.assertGreater(ledger["revised_claim_count"], 0)
        for source in ("ELSE MOVE '90' TO RESPONSE-CODE", "IF SQLCODE = 0\nMOVE '00' TO RESPONSE-CODE\nELSE MOVE"):
            facts = supplied_statement_facts(self.page(source), source_format="free")
            self.assertFalse(any(f.get("expression") == ["'90'"] for f in facts))
        truncated = {**self.page(), "span_truncated": True}
        self.assertEqual(supplied_statement_facts(truncated, source_format="free"), [])

    def test_real_neutral_model_answer_replays_verbatim_through_draft_and_final(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "conditional_boundary_answer.json").read_text())
        original = fixture["raw_answer"]
        self.assertEqual(hashlib.sha256(original.encode()).hexdigest(), fixture["raw_answer_sha256"])
        pages = [self.page(identifier=identifier, path=path)
                 for path, identifier in fixture["evidence_ids"].items()]
        for page in pages:
            self.assertEqual(page["source_sha256"], fixture["source_sha256"][page["relative_path"]])
        draft = validate_answer_draft(fixture["question"], original, {}, pages, source_format="free")
        answer, ledger = self.review(draft["answer"], pages)
        self.assertEqual(draft["answer"], original)
        self.assertEqual(answer, original)
        self.assertEqual(ledger["revised_claim_count"], 0)
        self.assertEqual(draft["ledger"]["revised_claim_count"], 0)
        # This is preservation, not endorsement of every model sentence.
        self.assertFalse(ledger["all_claims_supported"])
        self.assertIn("账户不存在 | 是", answer)


if __name__ == "__main__":
    unittest.main()
