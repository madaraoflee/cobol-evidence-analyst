"""Offline replay of a neutral business overview through the actual checker."""

import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from claim_ledger import review_answer_claims
from draft_validation import validate_answer_draft
from tools.business_onboarding_fixture import ONLINE_QUESTION, scripted_answer, sources


class BusinessExplanationClaimTests(unittest.TestCase):
    def page(self, source=None, identifier="ev_balance", path="balance.cbl"):
        source = source or sources()["balance.cbl"]
        return {"evidence_id": identifier, "relative_path": path,
                "source_text": source, "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "start_line": 1, "end_line": len(source.splitlines()), "span_truncated": False}

    def review(self, text, pages=None):
        pages = pages or [self.page()]
        return review_answer_claims(text, pages, {row["evidence_id"]: row for row in pages},
                                   source_format="free")

    def test_actual_online_answer_survives_draft_and_final_review_verbatim(self):
        supplied = self.page()
        original = scripted_answer({"question": ONLINE_QUESTION,
                                    "source_context": [{"pages": [supplied]}]})
        draft = validate_answer_draft(ONLINE_QUESTION, original, {}, [supplied], source_format="free")
        answer, ledger = self.review(draft["answer"])
        self.assertEqual(answer, original)
        self.assertEqual(draft["ledger"]["revised_claim_count"], 0)
        self.assertEqual(ledger.to_dict()["revised_claim_count"], 0)
        self.assertIn("否则为 90", answer)
        self.assertIn("失败路径未重新赋值余额，使用者应结合返回状态判断是否采用该余额", answer)
        entries = [row for row in ledger.entries if row.target_entity == "RESPONSE-CODE"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].support_status, "unsupported")
        self.assertEqual(entries[0].reason_code, "PROSE_LITERAL_REPRESENTATION_NOT_FORMALLY_VERIFIED")
        self.assertFalse(ledger.to_dict()["prose_semantics_verified"])

    def test_quoted_code_spelling_is_not_numeric_conversion(self):
        for quote in ["'", '"']:
            source = sources()["balance.cbl"].replace("'00'", quote + "00" + quote)
            for text in ["设置 RESPONSE-CODE 为 00", "设置 RESPONSE-CODE 等于 00",
                         "set RESPONSE-CODE is 00"]:
                with self.subTest(quote=quote, text=text):
                    original = text + "。[ev_balance]"
                    answer, ledger = self.review(original, [self.page(source)])
                    self.assertEqual(answer, original)
                    self.assertFalse(any(row.support_status == "supported" for row in ledger.entries))
        for value in ["0", "00.0", "99"]:
            original = f"设置 RESPONSE-CODE 为 {value}。[ev_balance]"
            answer, ledger = self.review(original)
            self.assertNotEqual(answer, original)
            self.assertTrue(any(row.support_status == "contradicted" for row in ledger.entries))

    def test_literal_source_move_keeps_exact_token_check(self):
        for value in ["00", "'99'"]:
            with self.subTest(value=value):
                original = f"MOVE {value} TO RESPONSE-CODE。[ev_balance]"
                answer, ledger = self.review(original)
                self.assertNotEqual(answer, original)
                self.assertIn("RESPONSE-CODE = '00'", answer)
                self.assertTrue(any(row.support_status == "contradicted" for row in ledger.entries))

    def test_representation_boundary_does_not_hide_wrong_guard_formula_or_citation(self):
        cases = [
            ("只有 SQLCODE = 99 时设置 RESPONSE-CODE 为 00。[ev_balance]", "SQLCODE = 99"),
            ("设置 RESPONSE-CODE 为 00。[ev_missing]", "ev_missing"),
        ]
        for original, wrong in cases:
            with self.subTest(original=original):
                answer, ledger = self.review(original)
                self.assertNotIn(wrong, answer)
                self.assertGreater(ledger.to_dict()["revised_claim_count"], 0)
        original = "设置 RESPONSE-CODE 为 00；COMPUTE DUE-AMOUNT = BASE-AMOUNT * 999。[ev_balance]"
        source = sources()["balance.cbl"].replace("GOBACK.", "COMPUTE DUE-AMOUNT = BASE-AMOUNT * 2.\nGOBACK.")
        answer, ledger = self.review(original, [self.page(source)])
        self.assertIn("设置 RESPONSE-CODE 为 00", answer)
        self.assertNotIn("999", answer)
        self.assertIn("BASE-AMOUNT * 2", answer)
        self.assertTrue(any(row.support_status == "contradicted" for row in ledger.entries))

    def test_real_error_cannot_delete_following_business_clause(self):
        original = ("MOVE '99' TO RESPONSE-CODE；"
                    "使用者应结合返回状态判断是否采用该余额。[ev_balance]")
        answer, ledger = self.review(original)
        self.assertNotIn("'99'", answer)
        self.assertIn("RESPONSE-CODE = '00'", answer)
        self.assertIn("使用者应结合返回状态判断是否采用该余额。[ev_balance]", answer)
        self.assertTrue(any(row.support_status == "contradicted" for row in ledger.entries))

    def test_semicolons_inside_source_literals_and_inline_code_are_not_split(self):
        for original, source in [
            ("`MOVE 'A; B；C' TO MESSAGE-TEXT`。[ev_balance]", "MOVE 'A; B；C' TO MESSAGE-TEXT."),
            ("MOVE 'A; B；C' TO MESSAGE-TEXT。[ev_balance]", "MOVE 'A; B；C' TO MESSAGE-TEXT."),
        ]:
            with self.subTest(original=original):
                answer, ledger = self.review(original, [self.page(source)])
                self.assertEqual(answer, original)
                self.assertEqual(len(ledger.entries), 1)
                self.assertEqual(ledger.entries[0].support_status, "supported")

    def test_other_program_character_code_cannot_rescue_numeric_mismatch(self):
        target = self.page("MOVE 1 TO RESPONSE-CODE.")
        other = self.page("MOVE '00' TO RESPONSE-CODE.", "ev_other", "other.cbl")
        original = "设置 RESPONSE-CODE 为 00。[ev_balance]"
        answer, ledger = self.review(original, [target, other])
        self.assertNotEqual(answer, original)
        self.assertTrue(any(row.support_status == "contradicted" for row in ledger.entries))
        self.assertNotIn("ev_other", answer)


if __name__ == "__main__":
    unittest.main()
