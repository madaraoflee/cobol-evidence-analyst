"""Do not replace caller behavior merely because it mentions a no-op callee."""

from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from claim_ledger import review_answer_claims
from test_answer_fact_completion import SOURCE, page


class NoopClaimAttributionTests(unittest.TestCase):
    def review(self, text, pages=None):
        pages = pages or [page()]
        return review_answer_claims(text, pages,
            {row["evidence_id"]: {**row, "kind": "source_page"} for row in pages},
            source_format="free", answer_language="en")

    def test_caller_formula_and_requested_noop_call_remain_in_the_answer(self):
        for text in (
            "When RATE is nonzero, the program calculates OUT-AMOUNT = RAW-AMOUNT * RATE / 1200, "
            "then accumulates TOTAL-AMOUNT = OUT-AMOUNT + TOTAL-AMOUNT and requests "
            "PERFORM WRITE-RESULT.[ev_test_source]",
            "主流程计算金额后执行 PERFORM WRITE-RESULT。[ev_test_source]",
            "WRITE-RESULT结束后主流程计算金额。[ev_test_source]",
            "`WRITE-RESULT` returns control and the caller calculates the amount.[ev_test_source]",
            "WRITE-RESULT 返回主流程并由主流程计算金额。[ev_test_source]",
        ):
            with self.subTest(text=text):
                answer, ledger = self.review(text)
                self.assertEqual(answer, text)
                self.assertFalse(any(entry.reason_code == "COMPLETE_PROCEDURE_HAS_ONLY_NOOP_STATEMENTS"
                                     for entry in ledger.entries))
                self.assertEqual(ledger.to_dict()["revised_claim_count"], 0)

    def test_direct_false_claim_about_noop_body_is_still_corrected(self):
        for text in ("`WRITE-RESULT` writes the output record.[ev_test_source]",
                     "WRITE-RESULT计算金额。[ev_test_source]"):
            with self.subTest(text=text):
                answer, ledger = self.review(text + " Another business step remains unknown.")
                self.assertIn("its calculation or write logic is not implemented", answer)
                self.assertIn("Another business step remains unknown.", answer)
                self.assertEqual(ledger.entries[0].reason_code, "COMPLETE_PROCEDURE_HAS_ONLY_NOOP_STATEMENTS")

    def test_same_name_noop_in_another_cited_file_cannot_revoke_active_body(self):
        inactive = page()
        active = page(SOURCE.replace("WRITE-RESULT.\nCONTINUE.",
                                    "WRITE-RESULT.\nMOVE 1 TO RESULT-CODE."), "ev_active_source")
        active["relative_path"] = "active.cbl"
        for citations in ("[ev_active_source]", "", "[ev_active_source][ev_test_source]"):
            with self.subTest(citations=citations):
                text = "WRITE-RESULT updates the result code." + citations
                answer, ledger = self.review(text, [inactive, active])
                self.assertEqual(answer, text)
                self.assertFalse(any(entry.reason_code == "COMPLETE_PROCEDURE_HAS_ONLY_NOOP_STATEMENTS"
                                     for entry in ledger.entries))


if __name__ == "__main__":
    unittest.main()
