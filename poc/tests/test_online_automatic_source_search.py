"""An ordinary investigation can recover from an uncited early answer using pinned source."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).resolve().parents[1])]
import test_online_investigation as scenarios
from business_index import build_business_index
from online_material_export import export_material
from online_material_query import MaterialQuery
from online_material_reader import DirectoryObjectStore, MaterialReader


def missing(context):
    return {'answer': '没有足够的源码可确认扣减顺序。', 'citations': [], 'complete': False,
            'remaining': ['核对适用条件和扣减公式']}


class AutomaticSourceSearchTests(unittest.TestCase):
    def setUp(self):
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        root = base / 'src'
        root.mkdir()
        self.case = scenarios.OnlineInvestigationTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.source_bytes = {
            'cash.cbl': b'IDENTIFICATION DIVISION.\nPROGRAM-ID. CASHENTRY.\nPROCEDURE DIVISION.\n'
                        b'IF ITEM-TYPE = "1-RESERVECREDIT"\n SUBTRACT CREDIT-AMT FROM NET-DUE\nEND-IF.\n'
                        b'IF NET-DUE > 0\n CALL "APPLYREFUND" USING NET-DUE\nEND-IF.\nGOBACK.',
            'return.cbl': b'IDENTIFICATION DIVISION.\nPROGRAM-ID. APPLYREFUND.\nPROCEDURE DIVISION USING DUE.\n'
                          b'IF REFUND-BALANCE < DUE\n SUBTRACT REFUND-BALANCE FROM DUE\nEND-IF.\nGOBACK.',
            'notice.cbl': b'IDENTIFICATION DIVISION.\nPROGRAM-ID. NOTICE.\nPROCEDURE DIVISION.\nDISPLAY "READY".\nGOBACK.',
        }
        for name, raw in self.case.source_bytes.items():
            (root / name).write_bytes(raw)
        db, output = base / 'index.sqlite', base / 'material'
        report = build_business_index(root, db, source_format='free', verify_content=True, framework_reference_path='')
        export_material(db, root, output, repository_id='example/source', commit='b' * 40, build_report=report)
        self.case.query = MaterialQuery(MaterialReader(DirectoryObjectStore(output)))
        self.question = '保费收取时 reserve 和 refund 的扣减顺序及适用条件是什么？'

    def grounded(self, context):
        pages = {row['citation']['path']: row['text'] for row in context['source_context']}
        self.assertIn('SUBTRACT CREDIT-AMT FROM NET-DUE', pages['cash.cbl'])
        self.assertIn('IF NET-DUE > 0', pages['cash.cbl'])
        self.assertIn('SUBTRACT REFUND-BALANCE FROM DUE', pages['return.cbl'])
        return scenarios.answer(context, '先使用准备金，剩余应收大于零时调用退款处理。')

    def test_uncited_early_answer_triggers_literal_search_and_originals_before_next_request(self):
        result = self.case.run_question([missing, self.grounded], question=self.question)
        self.assertEqual(result['status'], 'answered', result['error'])
        self.assertEqual(result['model_calls'], 2)
        self.assertEqual({row['path'] for row in result['citations']}, {'cash.cbl', 'return.cbl'})
        self.assertEqual(set(self.case.source_calls), {'cash.cbl', 'return.cbl'})
        observations = result['checkpoint']['observations']
        self.assertTrue({'reserve', 'refund'} <= {row['action']['text'].lower() for row in observations
                        if row['action']['tool'] == 'locate_global'})
        self.assertFalse(result['coverage']['full_repository_or_runtime_proof'])

    def test_source_action_budget_resume_reads_pinned_hits_without_new_planner_or_duplicate_search(self):
        first = self.case.run_question([missing], question=self.question, max_actions=1, max_model_calls=1)
        self.assertEqual(first['status'], 'needs_continuation', first.get('error'))
        self.assertEqual(first['actions'], 1)
        self.assertFalse(first['citations'])
        second = self.case.run_question([self.grounded], question=self.question, checkpoint=first['checkpoint'])
        self.assertEqual(second['status'], 'answered', second.get('error'))
        search = [row['action']['text'] for row in second['checkpoint']['observations'] if row['action']['tool'] == 'locate_global']
        self.assertEqual(len(search), len(set(search)))
        self.assertEqual(second['model_calls'], 1)

    def test_unrelated_source_is_not_read_for_a_miss_and_search_is_not_restarted(self):
        result = self.case.run_question([missing] * 6, question='UNKNOWNKEY 的适用条件是什么')
        self.assertNotEqual(result['status'], 'answered')
        self.assertFalse(result['citations'])
        self.assertFalse(self.case.source_calls)
        locates = [row for row in result['checkpoint']['observations'] if row['action']['tool'] == 'locate_global']
        self.assertEqual(len(locates), 1)
        self.assertFalse(locates[0]['result']['absence_established'])

    def test_changed_source_and_missing_transfer_authorization_still_stop_the_fallback(self):
        denied = self.case.run_question([], question=self.question, allow_source_transfer=False)
        self.assertEqual(denied['error'], 'ONLINE_SOURCE_TRANSFER_REQUIRED')
        self.assertFalse(self.case.source_calls)
        def changed(path, **options):
            raw = self.case.source(path, **options)
            return b'X' + raw[1:]
        invalid = self.case.run_question([missing], question=self.question, source_reader=changed)
        self.assertEqual(invalid['error'], 'MATERIAL_SOURCE_HASH_MISMATCH')
        self.assertFalse(invalid['citations'])

    def test_chinese_balance_and_refund_lead_to_sources_without_english_in_question(self):
        result = self.case.run_question([missing, self.grounded], question='账户余额和退款按什么顺序抵扣？')
        self.assertEqual(result['status'], 'answered', result.get('error'))
        probes = {r['action']['text'].lower() for r in result['checkpoint']['observations']
                  if r['action']['tool'] == 'locate_global'}
        self.assertTrue({'balance', 'refund'} <= probes)
        self.assertFalse(result['coverage']['full_repository_or_runtime_proof'])

    def test_named_caller_follows_static_call_and_reads_both_originals(self):
        result = self.case.run_question([missing, self.grounded], question='cash.cbl 和它调用的程序在哪里处理？')
        self.assertEqual(result['status'], 'answered', result.get('error'))
        self.assertEqual(set(self.case.source_calls), {'cash.cbl', 'return.cbl'})
        actions = [r['action'] for r in result['checkpoint']['observations']]
        self.assertTrue(any(a['tool'] == 'relations' and a['path'] == 'cash.cbl' for a in actions))
        self.assertTrue(any(a['tool'] == 'locate_global' and a['text'].upper() == 'APPLYREFUND' for a in actions))

    def test_only_named_file_reads_its_original_and_preserves_scope(self):
        def ground_one(context):
            pages = context['source_context']
            self.assertTrue(pages)
            self.assertEqual({p['citation']['path'] for p in pages}, {'cash.cbl'})
            self.assertIn('IF NET-DUE > 0', pages[0]['text'])
            return scenarios.answer(context, '净应收大于零时调用退款程序。')
        result = self.case.run_question([missing, ground_one], question='只看 cash.cbl，这里什么时候才调用退款？')
        self.assertEqual(result['status'], 'answered', result.get('error'))
        self.assertEqual(set(self.case.source_calls), {'cash.cbl'})
        self.assertFalse(any(r['action']['tool'] == 'locate_global' for r in result['checkpoint']['observations']))

    def test_uncited_followup_obligations_survive_existing_work_and_action_budget_resume(self):
        prior = '核对原先的条件'
        unfinished = ['核对金额不足时是否继续扣减', '核对申请失效后的冲正']
        structured = {'answer': '尚需核对原文。', 'citations': [], 'complete': False,
                      'remaining': [prior, *unfinished]}
        first = self.case.run_question([{'actions': [{'tool': 'search', 'text': 'unusedterm'}],
                                         'remaining': [prior]}, structured],
                                       question=self.question, max_actions=2)
        expected = [prior, *unfinished]
        self.assertEqual(first['error'], 'ONLINE_ACTION_BUDGET_EXCEEDED')
        self.assertEqual(first['remaining'], expected)
        self.assertEqual(first['checkpoint']['remaining'], expected)
        def next_request(context):
            self.assertEqual(context['remaining'], expected)
            return self.grounded(context)
        second = self.case.run_question([next_request], question=self.question, checkpoint=first['checkpoint'])
        self.assertEqual(second['status'], 'answered', second.get('error'))

    def test_only_in_an_eligibility_question_does_not_restrict_source_search(self):
        result = self.case.run_question([missing, self.grounded],
            question='Can the refund apply only after reserve is used?')
        self.assertEqual(result['status'], 'answered', result.get('error'))
        self.assertEqual(set(self.case.source_calls), {'cash.cbl', 'return.cbl'})
