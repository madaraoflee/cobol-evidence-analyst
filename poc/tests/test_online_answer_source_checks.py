"""A genuine source citation can still contradict the business answer that cites it."""
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).resolve().parents[1])]
import test_online_investigation as cases


class OnlineAnswerSourceChecksTests(unittest.TestCase):
    def setUp(self):
        self.fixture = cases.OnlineInvestigationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_visible_file_denial_is_corrected_using_original_without_repeating_the_read(self):
        def misread(context):
            return cases.answer(context, '本轮未提供 main.cbl 的源码，无法回答。')

        def revise(context):
            self.assertEqual(context['observations'][-1]['result']['issue'], 'supplied_source_denied')
            self.assertIn('IF INPUT-AMOUNT > 0', context['source_context'][0]['text'])
            return cases.answer(context, '输入金额大于零时调用处理子程序。')

        result = self.fixture.run_question([{'actions': [cases.read()]}, misread, revise], question='正数金额由谁处理？')
        self.assertEqual(result['status'], 'answered', result['error'])
        self.assertIn('大于零', result['answer'])
        self.assertEqual(self.fixture.source_calls, ['main.cbl'])

    def test_repeated_denial_stops_without_publishing_it_and_preserves_verified_locations(self):
        def misread(context):
            return cases.answer(context, 'The source for main.cbl was not provided.')

        result = self.fixture.run_question([{'actions': [cases.read()]}, misread, misread], question='Explain the positive amount.')
        self.assertEqual(result['status'], 'needs_continuation')
        self.assertNotIn('was not provided', result['answer'])
        self.assertEqual(result['model_calls'], 3)
        self.assertTrue(result['remaining'])
        self.assertEqual(result['citations'][0]['path'], 'main.cbl')

    def test_real_rule_gap_or_unread_callee_is_still_reportable(self):
        draft = 'main.cbl 中无法确认费率；未提供 worker.cbl 的源码。'
        result = self.fixture.run_question([{'actions': [cases.read()]}, lambda c: cases.answer(c, draft, complete=False, remaining=['核对子程序'])])
        self.assertIn(draft, result['answer'])
        self.assertEqual(result['model_calls'], 2)

    def test_display_text_cannot_establish_the_order_of_applying_funds(self):
        def wrong(context):
            self.assertIn("DISPLAY 'PROCESS'", context['source_context'][0]['text'])
            return cases.answer(context, '先用账户余额，再用退款。')

        def correct(context):
            self.assertTrue(any(o['result'].get('issue') == 'display_only_sequence' for o in context['observations']))
            return cases.answer(context, '已核对的语句显示提示，实际扣减顺序仍需核对。', complete=False, remaining=['查看实际扣减分支'])

        result = self.fixture.run_question([{'actions': [cases.read('worker.cbl', 5, 5)]}, wrong, correct], question='账户余额和退款按什么顺序抵扣？')
        self.assertEqual(result['status'], 'needs_continuation', result['error'])
        self.assertNotIn('先用账户余额', result['answer'])
        self.assertIn('仍需核对', result['answer'])

    def test_partial_label_cannot_hide_a_definite_sequence_claim(self):
        def unsupported(context):
            return cases.answer(context, '先抵扣余额，然后抵扣退款。', complete=False, remaining=['确认限额'])

        def revise(context):
            self.assertTrue(any(o['result'].get('issue') == 'display_only_sequence' for o in context['observations']))
            return cases.answer(context, '无法确认是否先抵扣余额，然后抵扣退款。', complete=False, remaining=['查看扣款程序'])

        result = self.fixture.run_question([{'actions': [cases.read('worker.cbl', 5, 5)]}, unsupported, revise],
                                           question='退款和余额按什么顺序抵扣？')
        self.assertEqual(result['status'], 'needs_continuation', result['error'])
        self.assertIn('无法确认是否', result['answer'])

    def test_display_question_and_execution_sources_do_not_trigger_the_sequence_check(self):
        for question, action in [('结算提示按什么顺序显示？', cases.read('worker.cbl', 5, 5)),
                                 ('结算按什么顺序执行？', cases.read('main.cbl', 8, 11))]:
            with self.subTest(question=question):
                result = self.fixture.run_question([{'actions': [action]}, cases.answer], question=question)
                self.assertEqual(result['status'], 'answered', result['error'])
                self.assertEqual(result['model_calls'], 2)

    def test_qualified_order_is_preserved_in_complete_partial_and_continued_answers(self):
        question = '保费余额和退款按什么顺序抵扣？'
        for text, complete in [
            ('目前没有证据表明先扣余额，然后扣退款。', False),
            ('不能断定扣减顺序。显示的是界面提示。', True),
            ('先扣余额，再扣退款？当前证据只显示提示。', True),
            ('先扣余额，再扣退款。这个顺序尚未证实。', False),
            ('It is not the case that we first deduct credit then refunds.', False),
            ('First deduct credit then refunds. That sequence has not been confirmed.', True),
        ]:
            with self.subTest(text=text):
                first = self.fixture.run_question([{'actions': [cases.read('worker.cbl', 5, 5)]}],
                                                  question=question, max_model_calls=1)
                result = self.fixture.run_question([lambda c: cases.answer(c, text, complete=complete)],
                                                   question=question, checkpoint=first['checkpoint'])
                self.assertIn(text, result['answer'])
                self.assertEqual(result['model_calls'], 1)
                self.assertEqual(result['status'], 'answered' if complete else 'needs_continuation')

    def test_unrelated_uncertainty_does_not_excuse_an_explicit_sequence(self):
        question = '保费余额和退款按什么顺序抵扣？'
        for text in ['限额尚未确认。先扣余额，再扣退款。', '先扣余额，后扣退款。',
                     'First deduct credit then refunds. The limit is unknown.', 'Credits are applied before refunds.']:
            with self.subTest(text=text):
                def revise(context):
                    self.assertTrue(any(o['result'].get('issue') == 'display_only_sequence' for o in context['observations']))
                    return cases.answer(context, '扣减顺序仍需核对。', complete=False, remaining=['查扣减程序'])
                result = self.fixture.run_question([{'actions': [cases.read('worker.cbl', 5, 5)]},
                    lambda c: cases.answer(c, text, complete=False, remaining=['确认限额']), revise], question=question)
                self.assertEqual(result['model_calls'], 3)
                self.assertIn('扣减顺序仍需核对', result['answer'])

    def test_nested_file_denial_must_name_the_same_path_in_the_denial_itself(self):
        from online_answer_checks import answer_source_issue
        pages = [{'id': 'E1', 'text': 'GOBACK.', 'citation': {'path': 'batch/settle.cbl'}}]
        for text, contradicted in [
            ('对于 online 目录，未提供 settle.cbl 的源码。', False),
            ('已核对 batch/settle.cbl 的源码。对于 online 目录，未提供 settle.cbl 的源码。', False),
            ('No source was provided for online/settle.cbl.', False),
            ('本轮没有提供 `batch/settle.cbl` 的源码。', True),
            ('The source for `batch/settle.cbl` was not provided.', True),
        ]:
            with self.subTest(text=text):
                issue = answer_source_issue('执行入口在哪里？', {'answer': text, 'complete': False, 'citations':['E1']}, pages)
                self.assertEqual(issue is not None, contradicted)
