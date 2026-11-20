"""A new online question can revisit the body and source locations of a prior answer."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).resolve().parents[1])]
import test_online_investigation as investigation
import test_online_workspace as workspace
from online_investigation import _action, run_investigation
import online_workspace
from online_material_format import MaterialError


class FollowupHistoryPagesTests(unittest.TestCase):
    def setUp(self):
        self.fixture = investigation.OnlineInvestigationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_last_branch_from_persisted_long_answer_is_visible_on_first_new_question_request(self):
        fixture = workspace.OnlineWorkspaceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.create_material('c' * 40, 'conditions', workspace.program('CALCULATE',
            'IF INPUT-AMOUNT < 100\n SUBTRACT BASE-CREDIT FROM AMOUNT\nELSE\n SUBTRACT EXTRA-CREDIT FROM AMOUNT\nEND-IF.'))
        branch = None
        first_question = '分别说明两个条件和金额。'
        def runner(query, source, question, **options):
            if question == first_question:
                def describe(context):
                    return investigation.answer(context, '签发及收款的业务说明。' * 340 + '最后一项是 ' + branch)
                actions = [{'actions': [investigation.read('main.cbl', 4, 10)]}, describe]
            else:
                def inspect(context):
                    prior = context['history'][-1]
                    self.assertTrue(prior['content_truncated'])
                    self.assertNotIn(branch, prior['content'])
                    self.assertIn(branch, prior['content_tail']['text'])
                    self.assertEqual(context['source_context'], [])
                    hint, = prior['citation_hints']
                    return {'actions': [investigation.read(hint['path'], hint['start'], hint['end'])]}
                def confirm(context):
                    self.assertIn('IF INPUT-AMOUNT < 100', context['source_context'][0]['text'])
                    self.assertIn(branch, context['source_context'][0]['text'])
                    return investigation.answer(context, '本轮已按原文核对 ' + branch)
                actions = [inspect, confirm]
            return run_investigation(query, source, question, client=self.fixture.client(actions), **options)
        fixture.app = fixture.make_app(investigator=runner)
        connected = fixture.connect('conditions')
        for branch in ('BASE-CREDIT', 'EXTRA-CREDIT'):
            with self.subTest(branch=branch):
                conversation = fixture.conversation(connected['workspace_id'])
                self.assertEqual(fixture.ask(conversation['id'], first_question)['status'], 'answered')
                started_reads = len(fixture.source_reads)
                result = fixture.ask(conversation['id'], '刚才最后一项的适用条件是什么？')
                self.assertEqual(result['status'], 'answered', result.get('error'))
                self.assertEqual(len(fixture.source_reads), started_reads + 1)
                self.assertIn(branch, fixture.app.state()['conversation']['messages'][-1]['content'])

    def test_paging_recovers_every_character_and_displaced_citation_without_granting_evidence(self):
        f = self.fixture
        _, history = f.prior_history()
        original = '📘保单\n' * 3500 + '中段抵扣条件' + '费用é\n' * 3000
        history[-1]['content'] = original
        hint = history[-1]['citations'][0]
        history[-1]['citations'] = [dict(hint, path=f'rule-{i:02d}.cbl') for i in range(45)]
        pages = []
        hints = []
        def review(context):
            row = context['history'][-1]
            self.assertLess(len(json.dumps(context['history'], ensure_ascii=False).encode()), 24000)
            self.assertEqual(context['source_context'], [])
            self.assertFalse(row['citation_hints_verified_for_current_answer'])
            self.assertGreater(row['citation_hints_omitted'], 0)
            action = {'tool': 'read_history', 'ref': row['history_ref']}
            if context['observations']:
                result = context['observations'][-1]['result']
                self.assertFalse(result['verified_for_current_answer'])
                if result['part'] == 'text':
                    self.assertEqual(result['offset'], sum(map(len, pages)))
                    pages.append(result['text'])
                else:
                    self.assertEqual(result['offset'], len(hints))
                    hints.extend(result['citation_hints'])
                if result['next_offset'] is not None:
                    return {'actions': [{**action, 'part': result['part'], 'offset': result['next_offset']}]}
                if result['part'] == 'citations':
                    return {'actions': [investigation.read()]}
                return {'actions': [{**action, 'part': 'citations'}]}
            return {'actions': [action]}
        def route(context):
            return investigation.answer(context) if context['source_context'] else review(context)
        result = f.run_question([route] * 20, max_model_calls=20, history=history, question='上次回答中段如何计费？')
        self.assertEqual(result['status'], 'answered', result['error'])
        self.assertEqual(''.join(pages), original)
        self.assertEqual([x['path'] for x in hints], [x['path'] for x in history[-1]['citations']])
        self.assertEqual(f.source_calls, ['main.cbl'])

    def test_receipt_cannot_resolve_to_a_changed_or_unrelated_message_on_resume(self):
        f = self.fixture
        _, history = f.prior_history()
        history[-1]['content'] = '费用' * 5500 + '原有尾项'
        def pages(context):
            ref = context['history'][-1]['history_ref']
            return {'actions': [{'tool': 'read_history', 'ref': ref},
                                {'tool': 'read_history', 'ref': ref, 'offset': 4000}]}
        first = f.run_question([pages], history=history, max_actions=1, max_model_calls=1)
        ref = first['checkpoint']['pending_actions'][0]['ref']
        self.assertEqual(first['status'], 'needs_continuation')
        change = deepcopy(history)
        change[-1]['content'] = '费用' * 5500 + '不同尾项'
        seen = []
        def inspect(context):
            seen.append(context['observations'][-1]['result'])
            return {'actions': [investigation.read()]}
        for prior in [history, change]:
            result = f.run_question([inspect], history=prior, checkpoint=deepcopy(first['checkpoint']), max_model_calls=1)
            self.assertFalse(result['citations'])
        self.assertEqual(seen[0]['text'], history[-1]['content'][4000:8000])
        self.assertEqual(seen[1]['error'], 'ONLINE_HISTORY_PAGE_UNAVAILABLE')
        for invalid in ({'ref': '../outside'}, {'part': 'source'}, {'offset': True}, {'offset': -1}, {'extra': 'x'}):
            with self.subTest(invalid=invalid), self.assertRaises(MaterialError):
                _action({'tool': 'read_history', 'ref': ref, **invalid})

    def test_catalog_recovers_an_older_turn_without_loading_unrelated_messages_or_future_turns(self):
        f = self.fixture
        _, previous = f.prior_history()
        target = '这个分支的金额进入后续计算。'
        saved = [{'id': 'early', 'role': 'assistant', 'content': target, 'citations': previous[-1]['citations']}]
        saved.extend({'id': str(i), 'role': 'user', 'content': 'Question ' + str(i)} for i in range(50))
        saved.extend([{'id': 'current', 'role': 'user', 'content': '现在追问最早的计算'},
                      {'id': 'future', 'role': 'assistant', 'content': 'future content'}])
        loaded = []
        def load(message):
            loaded.append(message['id'])
            return {**message, 'message_id': message['id']}
        history = online_workspace._PriorMessages({'messages': saved, 'pending': {'user_message_id': 'current'}}, load)
        def find(context):
            self.assertEqual(len(context['history']), 20)
            self.assertEqual(context['history_omitted'], 31)
            self.assertNotIn('early', loaded)
            return {'actions': [{'tool': 'history_catalog', 'offset': 50, 'limit': 4}]}
        def details(context):
            catalog = context['observations'][-1]['result']
            self.assertEqual(catalog['total'], 51)
            entry, = catalog['messages']
            self.assertEqual(entry['opening'], target)
            return {'actions': [{'tool': 'read_history', 'ref': entry['history_ref'], 'part': 'citations'}]}
        def locate(context):
            self.assertFalse(context['source_context'])
            hint, = context['observations'][-1]['result']['citation_hints']
            return {'actions': [investigation.read(hint['path'], hint['start'], hint['end'])]}
        result = f.run_question([find, details, locate, investigation.answer], history=history)
        self.assertEqual(result['status'], 'answered', result.get('error'))
        self.assertEqual(f.source_calls, ['main.cbl'])
        self.assertEqual(set(loaded), {'early', *map(str, range(30, 50))})
        self.assertEqual(len(loaded), 21)

    def test_other_conversation_and_revision_cannot_supply_prior_text_or_stale_source(self):
        f = self.fixture
        _, history = f.prior_history()
        history[-1]['message_id'] = 'first-conversation-message'
        f.run_question([{'actions': [investigation.read()]}], history=history, max_model_calls=1)
        ref = f.contexts[0]['history'][-1]['history_ref']
        other = deepcopy(history)
        other[-1]['message_id'] = 'second-conversation-message'
        denied = f.run_question([{'actions': [{'tool': 'read_history', 'ref': ref}]}], history=other, max_model_calls=1)
        self.assertEqual(denied['checkpoint']['observations'][-1]['result']['error'], 'ONLINE_HISTORY_PAGE_UNAVAILABLE')
        other[-1]['citations'][0]['revision'] = 'a' * 64
        earlier = other + [{'role': 'user', 'content': 'next question'} for _ in range(20)]
        denied = f.run_question([{'actions': [{'tool': 'history_catalog', 'offset': 20}]}], history=earlier, max_model_calls=1)
        self.assertEqual(denied['checkpoint']['observations'][-1]['result']['error'], 'ONLINE_HISTORY_REVISION_MISMATCH')
        self.assertFalse(denied['citations'])
        def source(path, *, max_bytes):
            return b'X' * len(f.source_bytes[path])
        denied = f.run_question([{'actions': [{'tool': 'read_history', 'ref': ref, 'part': 'citations'}]},
                                 {'actions': [investigation.read()]}], history=history, source_reader=source)
        self.assertEqual(denied['error'], 'MATERIAL_SOURCE_HASH_MISMATCH')
        self.assertFalse(denied['citations'])
