"""A deliberate retry reads the current workspace; reconnect preserves its journal."""

import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_index import build_business_index
from compilation_context import CompilationContext
from repository_discovery import ensure_repository_search
import test_explicit_retry_recovery as fixtures


class ExplicitRetryCurrentSourceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ExplicitRetryRecoveryTests(
            methodName='test_repeated_interruptions_never_trigger_automatic_resubmission')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.policy = AgentPolicy(max_model_requests=3)

    def received_then_interrupted(self):
        fixture = self.fixture
        received = False

        def transport(request):
            nonlocal received
            response = fixture.success(request)
            received = True
            return response

        def interrupt():
            if received:
                raise RuntimeError('interrupted after received response')

        with self.assertRaisesRegex(RuntimeError, 'interrupted after received response'):
            fixture.invoke(transport=transport, check_cancel=interrupt)
        prior = fixture.store.unfinished(
            'sha256:' + hashlib.sha256(fixture.question.encode()).hexdigest(),
            hashlib.sha256(fixture.config.chat_model.encode()).hexdigest())
        self.assertEqual([row['state'] for row in prior['requests']], ['completed'])
        return prior

    def assert_preserved(self, before, replacement):
        after = self.fixture.store.load(before['run_id'])
        for field in ('metadata', 'requests', 'completed_steps', 'uncertain_requests'):
            self.assertEqual(after[field], before[field])
        self.assertEqual({key: after['checkpoint'][key] for key in before['checkpoint']}, before['checkpoint'])
        self.assertEqual(after['state'], 'superseded')
        self.assertEqual(after['checkpoint']['replacement_run_id'], replacement)

    def test_compilation_change_allows_explicit_retry_after_received_response(self):
        fixture = self.fixture
        prior = self.received_then_interrupted()
        context = CompilationContext(source_format='free', framework_version='R2.0',
                                     compile_conditions=(('FEATURE', True),))
        build_business_index(fixture.source, fixture.database, source_format='free',
                             verify_content=True, compilation_context=context)
        ensure_repository_search(fixture.database, fixture.source)
        automatic = fixture.invoke(resume_run_id=prior['run_id'])
        self.assertEqual(automatic['reason_code'], 'RESUME_COMPILATION_CONTEXT_CHANGED')
        self.assertEqual(len(fixture.calls), 1)
        self.assertEqual(fixture.store.load(prior['run_id']), prior)
        result = fixture.invoke(resume_run_id=prior['run_id'], start_new_investigation=True)
        self.assertTrue(result['agent_result'].get('model_answer_recorded'), result)
        replacement = result['agent_result']['run_id']
        self.assertNotEqual(replacement, prior['run_id'])
        self.assertEqual(len(fixture.calls), 2)
        self.assert_preserved(prior, replacement)

    def test_completed_answer_retry_uses_current_source_before_old_allowance_is_spent(self):
        fixture = self.fixture
        first = fixture.invoke()
        original_id = first['agent_result']['run_id']
        before = fixture.store.load(original_id)
        fixture.source_file.write_text(fixture.source_file.read_text().replace(
            'IN-AMOUNT * 2', 'IN-AMOUNT * 3'), encoding='utf-8')
        fixture.rebuild()
        restored = fixture.invoke(resume_run_id=original_id)
        self.assertEqual(restored['agent_result']['answer'], first['agent_result']['answer'])
        self.assertEqual(len(fixture.calls), 1)
        result = fixture.invoke(resume_run_id=original_id, start_new_investigation=True)
        self.assertNotEqual(result['agent_result']['run_id'], original_id)
        self.assertIn('IN-AMOUNT * 3', result['agent_result']['answer'])
        self.assertEqual(len(fixture.calls), 2)
        self.assertEqual(fixture.store.load(original_id), before)

    def test_explicit_retry_does_not_require_removed_old_evidence_path(self):
        fixture = self.fixture
        prior = self.received_then_interrupted()
        fixture.source_file.rename(fixture.source / 'current-entry.cbl')
        fixture.rebuild()
        result = fixture.invoke(resume_run_id=prior['run_id'], start_new_investigation=True)
        self.assertTrue(result['agent_result'].get('model_answer_recorded'), result)
        self.assertIn('IN-AMOUNT * 2', result['agent_result']['answer'])
        self.assertEqual(len(fixture.calls), 2)
        self.assert_preserved(prior, result['agent_result']['run_id'])


if __name__ == '__main__':
    unittest.main()
