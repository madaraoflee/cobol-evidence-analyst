"""Preserve branch labels when reviewing conditions in business tables."""

import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claim_ledger import review_answer_claims
from claim_support import check_statement_support, supplied_statement_facts
from draft_validation import validate_answer_draft


class ClaimConditionTableTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((Path(__file__).parent / "fixtures" / "order_condition_table_answer.json").read_text())
        self.pages = self.fixture["source_pages"]
        self.allowed = {page["evidence_id"]: page for page in self.pages}
        self.page = next(page for page in self.pages if page["relative_path"] == "a-policy.cbl")
        self.fact = next(fact for fact in supplied_statement_facts(self.page, source_format="free")
                         if fact["predicate"] == "condition_statement")

    def review(self, text):
        answer, ledger = review_answer_claims(text, self.pages, self.allowed, source_format="free")
        return answer, ledger.to_dict()

    def test_captured_model_table_survives_draft_and_final_review_verbatim(self):
        raw = self.fixture["raw_answer"]
        self.assertEqual(hashlib.sha256(raw.encode()).hexdigest(), self.fixture["raw_answer_sha256"])
        draft = validate_answer_draft(self.fixture["question"], raw, {}, self.pages, source_format="free")
        final, ledger = self.review(draft["answer"])
        self.assertEqual(draft["answer"], raw)
        self.assertEqual(final, raw)
        self.assertEqual(draft["ledger"]["revised_claim_count"], 0)
        self.assertEqual(ledger["revised_claim_count"], 0)
        self.assertEqual(ledger["counts"]["contradicted"], 0)
        self.assertIn("`ORDER-AMOUNT > 0` 成立 | `'00'`", final)
        self.assertIn("`ORDER-AMOUNT > 0` 不成立 | `'90'`", final)
        self.assertFalse(ledger["all_claims_supported"])

    def test_zero_notation_does_not_prove_a_source_contradiction_or_runtime_result(self):
        for literal in ("0", "00", "0.00", "ZEROS", "ZEROES"):
            with self.subTest(literal=literal):
                assertion = {"predicate": "condition_statement", "condition": ["ORDER-AMOUNT", ">", literal], "outcome": True}
                result = check_statement_support(assertion, self.fact, self.page)
                self.assertEqual(result["support_status"], "unsupported")
                self.assertEqual(result["reason_code"], "CONDITION_LITERAL_NOTATION_NOT_FORMALLY_VERIFIED")
        exact = {"predicate": "condition_statement", "condition": ["ORDER-AMOUNT", ">", "ZERO"], "outcome": True}
        self.assertEqual(check_statement_support(exact, self.fact, self.page)["support_status"], "supported")

    def test_real_threshold_or_operator_mismatch_repairs_only_condition_text(self):
        for condition in ("ORDER-AMOUNT > 1", "ORDER-AMOUNT >= ZERO"):
            for outcome, result_code in (("成立", "'00'"), ("不成立", "'90'")):
                with self.subTest(condition=condition, outcome=outcome):
                    text = ("| 判断时的金额 | 结果码 | 说明 |\n|---|---|---|\n"
                        f"| `{condition}` {outcome} | `{result_code}` | 保留这段业务说明 [{self.page['evidence_id']}] |")
                    answer, ledger = self.review(text)
                    self.assertEqual(answer, text.replace(condition, "ORDER-AMOUNT > ZERO"))
                    self.assertGreater(ledger["counts"]["contradicted"], 0)
                    self.assertEqual(answer.count("|"), text.count("|"))

    def test_quoted_zero_is_not_numeric_zero(self):
        for quoted in ("'ZERO'", '"ZERO"', "'0'"):
            with self.subTest(quoted=quoted):
                assertion = {"predicate": "condition_statement", "condition": ["ORDER-AMOUNT", ">", quoted], "outcome": True}
                result = check_statement_support(assertion, self.fact, self.page)
                self.assertEqual(result["support_status"], "contradicted")
                original = f"| `ORDER-AMOUNT > {quoted}` 不成立 | `'90'` |"
                answer, ledger = self.review(original)
                self.assertEqual(answer, original.replace(quoted, "ZERO"))
                self.assertGreater(ledger["counts"]["contradicted"], 0)


if __name__ == "__main__":
    unittest.main()
