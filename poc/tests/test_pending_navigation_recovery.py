"""Interrupted investigations retain concrete navigation before asking again."""
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import business_chat
from company_api import TransportResponse
from run_store import RunStore
import test_business_action_budget as fixtures


class PendingNavigationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BusinessActionBudgetTests('test_model_read_waits_for_fresh_allowance_and_supplies_requested_source')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def recover(self, action, *, manual=""):

        f = self.fixture
        clock = {'now': 0.0}
        received = {'value': False}
        policy = replace(f.policy, max_model_requests=3, hard_question_seconds=120)
        requests = []

        def transport(request):
            payload = json.loads(json.loads(request.body)['messages'][-1]['content'])
            requests.append(payload)
            if len(requests) == 1:
                received['value'] = True
                text = json.dumps(action)
            else:
                page = next(page for bundle in payload['source_context'] for page in bundle['pages'])
                text = f"已读取相关计算源码。[{page['evidence_id']}]"
            return TransportResponse(200, json.dumps({'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}]}))

        def progress(event):
            if received['value'] and event.get('phase') == 'retrieving':
                raise RuntimeError('paused before local navigation')

        options = dict(transport=transport, framework_reference_path=manual, policy=policy,
                       conversation_id='saved-navigation', answer_detail='brief')
        with patch.object(business_chat, 'time', SimpleNamespace(monotonic=lambda: clock['now'])):
            with self.assertRaisesRegex(RuntimeError, 'paused before local navigation'):
                business_chat.run_business_chat('VALUEFLOW 的金额如何计算？', f.database, f.source,
                                               f.config, progress=progress, **options)
            store = RunStore(f.root / 'investigation-runs.sqlite')
            with store.connect() as db:
                run_id = db.execute('SELECT run_id FROM investigation_runs ORDER BY created_at DESC LIMIT 1').fetchone()[0]
            saved = store.load(run_id)
            self.assertEqual({row['tool'] for row in saved['checkpoint']['frontier']}, set(action))
            self.assertEqual(len(requests), 1)
            clock['now'] = 130.0
            second = business_chat.run_business_chat('VALUEFLOW 的金额如何计算？', f.database, f.source,
                f.config, start_new_investigation=True, resume_run_id=run_id, **options)
        self.assertTrue(second['agent_result']['model_answer_recorded'])
        self.assertEqual(len(requests), 2)
        return requests[1], second['agent_result']

    def test_explicit_retry_executes_saved_search_before_next_provider_request(self):
        payload, result = self.recover({'search': ['DETAILRULE']})
        self.assertTrue(any(row.get('search') == ['DETAILRULE'] for row in payload['completed_actions']))
        self.assertIn('detail.cbl', {page['relative_path'] for bundle in payload['source_context'] for page in bundle['pages']})
        self.assertEqual(result['metrics']['tool_calls']['search'], 1)
        self.assertEqual(payload['investigation_budget']['searches_per_turn'], 0)

    def test_saved_search_and_focus_still_share_one_navigation_operation(self):
        payload, result = self.recover({'search': ['DETAILRULE'], 'focus': ['detail.cbl']})
        self.assertTrue(any(row.get('focus') == ['detail.cbl'] for row in payload['completed_actions']))
        self.assertTrue(any(row.get('search') == ['DETAILRULE'] for row in payload['completed_actions']))
        self.assertEqual(result['metrics']['tool_calls']['search'], 1)

    def test_saved_framework_lookup_restores_document_evidence(self):
        from test_framework_semantics import MANUAL
        manual = self.fixture.root / 'reference.md'
        manual.write_text(MANUAL)
        payload, result = self.recover({'framework_search': ['ADVANCE']}, manual=str(manual))
        self.assertTrue(any(row.get('framework_search') == 'ADVANCE' and row['references'] > 0
                            for row in payload['completed_actions']))
        self.assertTrue(payload['framework_references'])
        self.assertEqual(result['metrics']['tool_calls']['framework_search'], 1)

    def test_saved_concept_lookup_is_not_silently_discarded(self):
        payload, _ = self.recover({'search_concepts': 'amount'})
        self.assertTrue(any(row.get('search_concepts') == 'amount' for row in payload['completed_actions']))

    def test_saved_impact_lookup_is_not_silently_discarded(self):
        payload, _ = self.recover({'list_impact': 'VALUEFLOW'})
        self.assertTrue(any(row.get('list_impact') == 'VALUEFLOW' and 'handle' in row
                            for row in payload['completed_actions']))


if __name__ == '__main__':
    unittest.main()
