"""Captured business-state prose must not be rewritten as a false code claim."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from claim_ledger import review_answer_claims


SOURCE = '''IDENTIFICATION DIVISION.
PROGRAM-ID. COLLECTION-ENTRY.
DATA DIVISION.
LINKAGE SECTION.
01 REQUEST-RECORD.
   05 READ-STATUS PIC X(8).
   05 COLLECTION-RESULT PIC X(12).
   05 AMOUNT-DUE PIC 9(7)V99.
PROCEDURE DIVISION USING REQUEST-RECORD.
MAIN.
    MOVE SPACES TO COLLECTION-RESULT
    EVALUATE READ-STATUS
      WHEN "READY"
        MOVE "CALCULATED" TO COLLECTION-RESULT
      WHEN "NOTFOUND"
        MOVE "REVIEW" TO COLLECTION-RESULT
      WHEN OTHER
        MOVE "RETRY" TO COLLECTION-RESULT
    END-EVALUATE
    COMPUTE AMOUNT-DUE = 100 - 35
    GOBACK.
'''
# This complete sentence was preserved in the real response's draft ledger,
# before an unquoted status token caused destructive local replacement.
CAPTURED = ('抵扣按“账户余额 → 退款 → 奖励”的顺序执行，最终请求中的 `AMOUNT-DUE` 为 **65.00**，'
            '`COLLECTION-RESULT` 为 **`CALCULATED`**。')


class ClaimStatusNotationTests(unittest.TestCase):
    def review(self, text):
        page = {'evidence_id': 'ev_collection_state', 'relative_path': 'collection-entry.cbl',
                'source_sha256': hashlib.sha256(SOURCE.encode()).hexdigest(), 'source_text': SOURCE,
                'start_line': 1, 'end_line': len(SOURCE.splitlines()), 'span_truncated': False}
        return review_answer_claims(text, [page], {page['evidence_id']: page}, source_format='free')

    def test_captured_business_result_survives_draft_and_final_review(self):
        original = CAPTURED
        for _ in range(2):
            answer, ledger = self.review(original)
            self.assertEqual(answer, original)
            self.assertEqual(ledger.to_dict()['revised_claim_count'], 0)
            self.assertFalse(ledger.to_dict()['prose_semantics_verified'])
            status = next(e for e in ledger.entries if e.target_entity == 'COLLECTION-RESULT')
            self.assertNotEqual(status.support_status, 'contradicted')

    def test_natural_unquoted_status_is_not_an_exact_source_identifier_assertion(self):
        for text in ['COLLECTION-RESULT 为 CALCULATED', 'COLLECTION-RESULT 是 CALCULATED',
                     'COLLECTION-RESULT is CALCULATED', 'COLLECTION-RESULT 为 OTHER-CODE']:
            with self.subTest(text=text):
                original = text + '。[ev_collection_state]'
                answer, ledger = self.review(original)
                self.assertEqual(answer, original)
                self.assertFalse(any(e.support_status == 'contradicted' for e in ledger.entries))

    def test_prose_setting_can_display_string_characters_without_source_quotes(self):
        original = '设置 COLLECTION-RESULT 为 CALCULATED。[ev_collection_state]'
        answer, ledger = self.review(original)
        self.assertEqual(answer, original)
        self.assertTrue(any(e.reason_code == 'PROSE_LITERAL_REPRESENTATION_NOT_FORMALLY_VERIFIED'
                            for e in ledger.entries))

    def test_explicit_code_does_not_convert_bare_identifier_into_literal(self):
        for code in ['MOVE CALCULATED TO COLLECTION-RESULT', 'COLLECTION-RESULT = CALCULATED',
                     'COMPUTE COLLECTION-RESULT = CALCULATED']:
            with self.subTest(code=code):
                original = '`' + code + '`。[ev_collection_state]'
                answer, ledger = self.review(original)
                if code.startswith('COMPUTE'):
                    # There is no comparable COMPUTE for this target; do not
                    # invent a syntax proof by comparing unrelated verbs.
                    self.assertEqual(answer, original)
                    self.assertTrue(any(e.claim_type == 'assignment_statement' and e.expression == ['CALCULATED']
                                        for e in ledger.entries))
                else:
                    self.assertNotEqual(answer, original)
                    self.assertTrue(any(e.support_status == 'contradicted' for e in ledger.entries))
                self.assertFalse(any(e.reason_code == 'PROSE_LITERAL_REPRESENTATION_NOT_FORMALLY_VERIFIED'
                                     for e in ledger.entries))

    def test_wrong_explicit_status_and_numeric_formula_are_still_corrected(self):
        for code in ['MOVE "DECLINED" TO COLLECTION-RESULT', '设置 COLLECTION-RESULT 为 DECLINED',
                     'COMPUTE AMOUNT-DUE = 999']:
            with self.subTest(code=code):
                original = code + '。[ev_collection_state]另一个业务说明保留。'
                answer, ledger = self.review(original)
                self.assertNotEqual(answer, original)
                self.assertTrue(any(e.support_status == 'contradicted' for e in ledger.entries))
                self.assertIn('另一个业务说明保留', answer)


if __name__ == '__main__':
    unittest.main()
