"""Only the captured local statement and its actual scope support a checked finding."""
import hashlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_deduction_review import deduction_review_requested, inspect_deduction
from source_reading import _identify_page


CALLER = """IDENTIFICATION DIVISION.
PROGRAM-ID. COLLECTOR.
PROCEDURE DIVISION.
IF BILL-TYPE = "TAX"
 SUBTRACT TAX-CREDIT FROM OPEN-DUE
END-IF.
IF OPEN-DUE > 2
 CALL "DISCOUNT" USING OPEN-DUE
END-IF.
GOBACK."""
WORKER = """IDENTIFICATION DIVISION.
PROGRAM-ID. DISCOUNT.
PROCEDURE DIVISION USING AMOUNT.
IF DISCOUNT-VALUE < AMOUNT
 SUBTRACT DISCOUNT-VALUE FROM AMOUNT
END-IF.
GOBACK."""
QUESTION = "tax 和 discount 的扣费顺序是什么？"
ANSWER = ('先检查 BILL-TYPE 是否等于 TAX，符合时减去 TAX-CREDIT，否则跳过这笔。'
          '随后若 OPEN-DUE 大于 2 则调用 DISCOUNT，否则跳过整个调用。'
          '当 DISCOUNT-VALUE 小于 AMOUNT 时再扣减，相等或更大时保留金额。'
          '最终应收等于初始金额减去实际扣掉的金额。')


class LocalDeductionReviewTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.files = {}
        self.session = SimpleNamespace(capture=self.files.__getitem__, index_configuration='captured',
            source_manifest=lambda: [dict(relative_path=name, sha256=file.sha256) for name, file in self.files.items()])

    def page(self, name, source, first=1, last=None):
        path = self.root / name
        path.write_text(source)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.files[name] = SimpleNamespace(path=path, encoding='utf-8', sha256=digest, source_format='free')
        lines = source.splitlines()
        end = len(lines) if last is None else last
        p = dict(source_text='\n'.join(lines[first - 1:end]), relative_path=name, start_line=first, end_line=end,
                 source_sha256=digest, span_truncated=False, source_verification='content_hash', format_hint='free')
        p['evidence_id'] = _identify_page(p)
        return p

    def proof(self, pages, question=QUESTION):
        return inspect_deduction(question, pages, source_session=self.session, answer_language='zh-CN')

    def test_missing_each_skip_boundary_order_or_result_is_reviewed(self):
        proof = self.proof([self.page('collector.cbl', CALLER), self.page('discount.cbl', WORKER)])
        self.assertTrue(proof.closed_answer)
        with_result = self.proof([self.page('collector.cbl', CALLER), self.page('discount.cbl', WORKER)],
                                 'tax 和 discount 的扣费顺序、跳过条件和最终应收是什么？')
        self.assertTrue(with_result.closed_answer)
        self.assertEqual(proof.missing_aspects(ANSWER), [])
        steps, conclusion = ANSWER.split('最终')
        self.assertEqual(proof.missing_aspects('核对下来，最终' + conclusion + steps), [])
        for phrase in ('否则跳过这笔', '否则跳过整个调用', '相等或更大时保留金额'):
            with self.subTest(skip=phrase):
                self.assertIn('skip_conditions', proof.missing_aspects(ANSWER.replace(phrase, '')))
        self.assertIn('processing_sequence', proof.missing_aspects(ANSWER.replace('小于', '小于或等于')))
        self.assertIn('processing_sequence', proof.missing_aspects(ANSWER.replace('大于 2', '大于 7')))
        self.assertIn('final_receivable', proof.missing_aspects(ANSWER.split('最终')[0]))
        self.assertFalse(deduction_review_requested('无需说明扣费顺序，只列字段类型。'))

    def test_last_item_in_sequence_does_not_hide_subsequent_skip_conditions(self):
        review = self.proof([])
        prose = ('先收税款，最后处理折扣。税款足额时从 BALANCE 扣除税款；不足时跳过。'
                 '折扣足额时从 BALANCE 扣除折扣；不足时跳过。')
        self.assertEqual(review.missing_aspects(prose), [])
        ask_for_result = self.proof([], question=QUESTION + '还要说明最终应收。')
        self.assertEqual(ask_for_result.missing_aspects(prose), ['final_receivable'])
        self.assertEqual(ask_for_result.missing_aspects(prose + '最终应收是初始应收减去已扣金额。'), [])
        self.assertIn('final_receivable', review.missing_aspects(prose.replace('BALANCE 扣除折扣', 'OTHER 扣除折扣')))

    def test_redacted_prefixes_extra_sources_and_stale_citations_never_close_the_path(self):
        a = self.page('collector.cbl', CALLER)
        b = self.page('discount.cbl', WORKER)
        tampered = dict(b, source_text=b['source_text'].replace(' < ', ' > '))
        tampered['evidence_id'] = _identify_page(tampered)
        self.assertFalse(self.proof([a, tampered]).closed_answer)
        for invalid in (tampered, dict(b, span_truncated=True), dict(b, source_sha256='e' * 64),
                        dict(b, evidence_id=a['evidence_id']), dict(b, include_chain=['elsewhere'])):
            with self.subTest(ref=invalid['evidence_id']):
                self.assertFalse(self.proof([invalid]).checked_findings)
        more = self.page('discount.cbl', WORKER + '\nMOVE 0 TO AMOUNT.', last=len(WORKER.splitlines()))
        self.assertFalse(self.proof([a, more]).closed_answer)
        self.assertTrue(self.proof([a, more]).checked_findings)
        b = self.page('discount.cbl', WORKER)
        self.assertFalse(self.proof([a, b, self.page('unknown.cbl', 'DISPLAY "discount".')]).closed_answer)
        self.files['discount.cbl'].path.unlink()
        self.assertFalse(self.proof([a, b]).closed_answer)

    def test_named_paragraph_operation_is_scoped_to_its_real_source(self):
        source = CALLER.replace('IF BILL-TYPE', 'CHARGE-RULE.\nIF BILL-TYPE')
        p = self.page('collector.cbl', source, first=4)
        proof = self.proof([p], question='TAX-CREDIT 在哪里扣减？')
        self.assertEqual(len(proof.checked_findings), 1)
        finding = proof.checked_findings[0]
        self.assertIn('SUBTRACT TAX-CREDIT FROM OPEN-DUE', finding['text'])
        self.assertIn('前提仍待核对', finding['text'])
        self.assertEqual(finding['sources'][0]['evidence_id'], p['evidence_id'])
        self.assertFalse(proof.closed_answer)
