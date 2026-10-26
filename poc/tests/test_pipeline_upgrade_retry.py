"""Upgrade recovery uses saved ledgers without replaying old requests."""

import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import PIPELINE_VERSION, PROMPT_VERSION
import test_explicit_retry_recovery as fixtures


class PipelineUpgradeRetryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ExplicitRetryRecoveryTests(
            methodName='test_repeated_interruptions_never_trigger_automatic_resubmission')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def interrupted_after_response(self, version_field):
        fixture = self.fixture
        received = False

        def transport(request):
            nonlocal received
            response = fixture.success(request)
            received = True
            return response

        def interrupt():
            if received:
                raise RuntimeError('interrupted after saved response')

        with self.assertRaisesRegex(RuntimeError, 'interrupted after saved response'):
            fixture.invoke(transport=transport, check_cancel=interrupt)
        prior = fixture.store.unfinished(
            'sha256:' + hashlib.sha256(fixture.question.encode()).hexdigest(),
            hashlib.sha256(fixture.config.chat_model.encode()).hexdigest())
        self.assertFalse(prior['uncertain_requests'])
        self.assertEqual([request['state'] for request in prior['requests']], ['completed'])
        metadata = {**prior['metadata'], version_field: 'older-version'}
        with fixture.store.connect() as db:
            db.execute('UPDATE investigation_runs SET metadata_json=? WHERE run_id=?',
                       (json.dumps(metadata), prior['run_id']))
        return fixture.store.load(prior['run_id'])

    def check_upgrade_retry(self, version_field, *, explicit_id, allowance):
        fixture = self.fixture
        fixture.policy = AgentPolicy(max_model_requests=allowance)
        prior = self.interrupted_after_response(version_field)
        original_calls = len(fixture.calls)
        options = {'resume_run_id': prior['run_id']} if explicit_id else {}
        blocked = fixture.invoke(**options)
        self.assertEqual(blocked['reason_code'], 'RESUME_PIPELINE_VERSION_CHANGED')
        self.assertEqual(len(fixture.calls), original_calls)
        self.assertEqual(fixture.store.load(prior['run_id']), prior)

        result = fixture.invoke(start_new_investigation=True, **options)
        answer = result['agent_result']
        self.assertTrue(answer.get('model_answer_recorded'), result)
        replacement = answer['run_id']
        self.assertNotEqual(replacement, prior['run_id'])
        self.assertEqual(len(fixture.calls), original_calls + 1)
        saved = fixture.store.load(replacement)
        self.assertEqual(saved['metadata']['pipeline_version'], PIPELINE_VERSION)
        self.assertEqual(saved['metadata']['prompt_version'], PROMPT_VERSION)
        retired = fixture.store.load(prior['run_id'])
        self.assertEqual(retired['state'], 'superseded')
        for field in ('metadata', 'requests', 'completed_steps', 'uncertain_requests'):
            self.assertEqual(retired[field], prior[field])
        self.assertEqual({key: retired['checkpoint'][key] for key in prior['checkpoint']},
                         prior['checkpoint'])
        self.assertEqual(retired['checkpoint']['replacement_run_id'], replacement)

        # The UI may still carry the original run ID after another reload.
        # A second explicit retry is fresh; it must not rewrite the old ledger.
        repeated = fixture.invoke(resume_run_id=prior['run_id'], start_new_investigation=True)
        self.assertTrue(repeated['agent_result']['model_answer_recorded'])
        self.assertNotEqual(repeated['agent_result']['run_id'], replacement)
        self.assertEqual(len(fixture.calls), original_calls + 2)
        self.assertEqual(fixture.store.load(prior['run_id']), retired)

    def test_pipeline_upgrade_explicit_saved_id_starts_current_run(self):
        self.check_upgrade_retry('pipeline_version', explicit_id=True, allowance=2)

    def test_prompt_upgrade_explicit_question_retry_starts_current_run(self):
        self.check_upgrade_retry('prompt_version', explicit_id=False, allowance=1)

    def test_failed_replacement_creation_preserves_original_ledger(self):
        fixture = self.fixture
        prior = self.interrupted_after_response('pipeline_version')
        original_calls = len(fixture.calls)
        with patch.object(fixtures.RunStore, 'create', side_effect=RuntimeError('cannot create replacement')):
            with self.assertRaisesRegex(RuntimeError, 'cannot create replacement'):
                fixture.invoke(start_new_investigation=True, resume_run_id=prior['run_id'])
        self.assertEqual(fixture.store.load(prior['run_id']), prior)
        self.assertEqual(len(fixture.calls), original_calls)


if __name__ == '__main__':
    unittest.main()
