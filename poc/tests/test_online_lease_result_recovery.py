"""Returned analysis survives a subsequent one-shot online lease failure."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
from api_error_details import build_diagnostic, sanitize_diagnostic
from company_api import APIClientError
from online_material_format import MaterialError
from online_sqlite_store import OnlineSQLiteStore, supports_managed_wal
import test_online_workspace as workspace_fixture


class OnlineLeaseResultRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = workspace_fixture.OnlineWorkspaceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.private = 'fixture-private-source-token'
        self.connected = self.fixture.connect(credentials={
            'material_token': 'fixture-private-material-token', 'source_token': self.private})
        self.conversation = self.fixture.conversation(self.connected['workspace_id'])
        self.diagnostic = sanitize_diagnostic(build_diagnostic('HTTP_ERROR', http_status=500))
        self.previous = {'request_status': 'idle', 'step': 1, 'marker': 'last_persisted_progress'}
        self.returned = {'request_status': 'idle', 'step': 2, 'marker': 'new_returned_result',
                         'diagnostic': self.diagnostic}
        self.results = []
        self.faults = []

    def result_runner(self, *, status='failed', change=None):
        def runner(query, source, question, **options):
            result = self.fixture.runner(query, source, question, **options)
            options['progress']({'phase': 'reading', 'checkpoint': self.previous})
            result.update(status=status, error='HTTP_ERROR' if status == 'failed' else None,
                          diagnostic=self.diagnostic, checkpoint=deepcopy(self.returned))
            if change is not None:
                result = change(result)
            self.results.append(deepcopy(result))
            return result
        self.fixture.app.investigator = runner

    @contextmanager
    def cleanup_fault(self):
        """Use the real managed close when available; otherwise fault the lease seam.

        An older runtime keeps its real original DELETE/no-op lease policy;
        the seam still verifies Workspace's platform-independent error handling.
        """
        state = self.fixture.app.store
        if supports_managed_wal():
            original = OnlineSQLiteStore.close
            def close(store, *, checkpoint=False):
                target = store is state._analysis_database and checkpoint
                original(store, checkpoint=checkpoint)
                if target and not self.faults:
                    self.faults.append('managed_close')
                    raise sqlite3.OperationalError('Private raw cleanup detail')
            with mock.patch.object(OnlineSQLiteStore, 'close', new=close):
                yield
        else:
            original = state.analysis_writes
            @contextmanager
            def lease():
                with original():
                    yield
                self.faults.append('legacy_lease_boundary')
                raise sqlite3.OperationalError('Private raw cleanup detail')
            with mock.patch.object(state, 'analysis_writes', new=lease):
                yield

    def ask(self):
        job = self.fixture.ask(self.conversation['id'])
        for thread in list(self.fixture.app._threads.values()):
            thread.join(3)
        return job

    def saved(self, app=None):
        app = app or self.fixture.app
        with app.store.transaction() as db:
            return app.store.get(db, 'conversations', self.conversation['id'])

    def assert_returned_work_retained(self, job, expected_status):
        self.assertEqual(job['status'], expected_status)
        self.assertEqual(job['storage_error'], 'ONLINE_STATE_UNAVAILABLE')
        self.assertEqual(job['diagnostic'], self.diagnostic)
        saved = self.saved()
        self.assertEqual(saved['pending']['checkpoint'], self.results[-1]['checkpoint'])
        self.assertEqual(saved['pending']['storage_error'], 'ONLINE_STATE_UNAVAILABLE')
        self.assertEqual(saved['pending']['diagnostic'], self.diagnostic)
        answer = saved['messages'][-1]
        self.assertEqual(answer['role'], 'assistant')
        self.assertEqual(answer['content'], self.results[-1]['answer'])
        self.assertEqual(answer['status'], expected_status)
        self.assertEqual([{key: value for key, value in c.items() if key != 'key'} for c in answer['citations']],
                         self.results[-1]['citations'])
        self.assertEqual(answer['evidence'], self.results[-1]['evidence'])
        self.assertEqual(answer['coverage'], self.results[-1]['coverage'])
        self.assertNotIn('Private raw cleanup detail', json.dumps(saved))
        self.assertNotIn('Private raw cleanup detail', json.dumps(job))
        self.assertEqual(len(self.fixture.runner_calls), 1)
        self.assertEqual(len(self.faults), 1)
        return saved

    def test_returned_http_500_keeps_new_checkpoint_partial_evidence_and_diagnostic_after_cleanup_failure_and_restart(self):
        self.result_runner()
        with self.cleanup_fault():
            job = self.ask()
        self.assertEqual(job['error'], 'HTTP_ERROR')
        saved = self.assert_returned_work_retained(job, 'failed')
        restarted = self.fixture.make_app()
        self.assertEqual(self.saved(restarted), saved)
        self.assertEqual(restarted.state()['conversation']['pending']['diagnostic']['http_status'], 500)
        self.assertEqual(restarted.state()['conversation']['pending']['storage_error'], 'ONLINE_STATE_UNAVAILABLE')

    def test_returned_answered_is_saved_as_failed_when_cleanup_did_not_finish_normally(self):
        self.result_runner(status='answered')
        with self.cleanup_fault():
            job = self.ask()
        saved = self.assert_returned_work_retained(job, 'failed')
        self.assertEqual(saved['pending']['status'], 'failed')
        self.assertFalse(any(message['status'] == 'answered' for message in saved['messages']))

    def test_unknown_request_keeps_retry_confirmation_and_never_repeats_the_runner(self):
        def unknown(result):
            result['checkpoint']['request_status'] = 'unknown'
            result['error'] = 'REQUEST_TIMEOUT'
            return result
        self.result_runner(status='retry_confirmation_required', change=unknown)
        with self.cleanup_fault():
            job = self.ask()
        self.assert_returned_work_retained(job, 'retry_confirmation_required')
        public = self.fixture.app.state()['conversation']['pending']
        self.assertTrue(public['retry_confirmation_required'])
        self.assertTrue(public['can_resume'])
        self.assertEqual(len(self.fixture.runner_calls), 1)
        self.assertFalse(self.fixture.runner_calls[0][1]['approve_retry'])

    def test_returned_cancelled_stays_cancelled_with_its_latest_work(self):
        self.result_runner(status='cancelled')
        with self.cleanup_fault():
            job = self.ask()
        self.assert_returned_work_retained(job, 'cancelled')

    def test_runner_exception_keeps_original_failure_path_and_persisted_unknown_request(self):
        self.result_runner()
        with self.cleanup_fault():
            prior = self.ask()
        self.assertEqual(prior['storage_error'], 'ONLINE_STATE_UNAVAILABLE')
        self.fixture.runner_calls.clear()
        def runner(query, source, question, **options):
            self.fixture.runner_calls.append((question, options))
            options['progress']({'phase': 'requesting', 'checkpoint': {'request_status': 'unknown', 'step': 1}})
            raise APIClientError('HTTP_ERROR', http_status=500, diagnostic=self.diagnostic)
        self.fixture.app.investigator = runner
        with mock.patch.object(self.fixture.app, '_complete', wraps=self.fixture.app._complete) as complete:
            started = self.fixture.post('resume', {'conversation_id': self.conversation['id'],
                'allow_source_transfer': True}, 202)
            job = self.fixture.wait(started['job_id'])
            for thread in list(self.fixture.app._threads.values()):
                thread.join(3)
        self.assertEqual(job['error'], 'HTTP_ERROR')
        self.assertEqual(job['diagnostic'], self.diagnostic)
        self.assertNotIn('storage_error', job)
        complete.assert_not_called()
        self.assertEqual(self.saved()['pending']['checkpoint']['request_status'], 'unknown')
        self.assertEqual(len(self.fixture.runner_calls), 1)
        self.assertEqual(len(self.saved()['messages']), 2)
        self.assertNotIn('storage_error', self.saved()['pending'])

    def test_cleanup_failure_does_not_bypass_credential_guard_or_result_validation(self):
        changes = (
            ('ONLINE_CREDENTIAL_EXPOSURE', lambda result: {**result, 'answer': self.private}),
            ('ONLINE_INVALID_RESULT', lambda result: {**result, 'status': 'not-a-valid-result'}),
            ('ONLINE_INVALID_RESULT', lambda result: {**result, 'citations': ['invalid-citation']}),
        )
        for expected, change in changes:
            with self.subTest(expected=expected):
                self.faults.clear()
                self.conversation = self.fixture.conversation(self.connected['workspace_id'])
                self.result_runner(change=change)
                with self.cleanup_fault():
                    job = self.ask()
                self.assertEqual(job['error'], expected)
                self.assertNotIn('storage_error', job)
                saved = self.saved()
                self.assertEqual(len(saved['messages']), 1)
                self.assertEqual(saved['pending']['checkpoint'], self.previous)
                self.assertNotIn(self.private, json.dumps(saved))

    def test_normal_cleanup_after_a_failed_lease_clears_secondary_error_without_repeating_source_request_implicitly(self):
        self.result_runner()
        with self.cleanup_fault():
            failed = self.ask()
        self.assertEqual(failed['storage_error'], 'ONLINE_STATE_UNAVAILABLE')
        self.assertEqual(len(self.fixture.runner_calls), 1)
        self.result_runner(status='needs_continuation')
        started = self.fixture.post('resume', {'conversation_id': self.conversation['id'],
            'allow_source_transfer': True}, 202)
        job = self.fixture.wait(started['job_id'])
        for thread in list(self.fixture.app._threads.values()):
            thread.join(3)
        self.assertEqual(job['status'], 'needs_continuation')
        self.assertNotIn('storage_error', job)
        self.assertNotIn('storage_error', self.saved()['pending'])
        self.assertEqual(len(self.fixture.runner_calls), 2)
        self.assertEqual(self.fixture.runner_calls[-1][1]['checkpoint'], self.returned)

    def test_failed_result_persistence_is_not_reported_as_success_and_keeps_cleanup_as_cause(self):
        self.result_runner(status='answered')
        state, captured = self.fixture.app.store, []
        original_transaction, original_complete, original_fail = state.transaction, self.fixture.app._complete, self.fixture.app._fail_job
        fail_next = [False]
        @contextmanager
        def transaction():
            if fail_next[0]:
                fail_next[0] = False
                raise MaterialError('ONLINE_STATE_UNAVAILABLE')
            with original_transaction() as db:
                yield db
        def complete(*args, **options):
            fail_next[0] = True
            return original_complete(*args, **options)
        def fail(*args, **options):
            captured.append(sys.exc_info()[1])
            return original_fail(*args, **options)
        with self.cleanup_fault(), mock.patch.object(state, 'transaction', new=transaction), \
                mock.patch.object(self.fixture.app, '_complete', new=complete), \
                mock.patch.object(self.fixture.app, '_fail_job', new=fail):
            job = self.ask()
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['error'], 'ONLINE_STATE_UNAVAILABLE')
        self.assertEqual(len(self.saved()['messages']), 1)
        self.assertEqual(len(captured), 1)
        self.assertIsInstance(captured[0].__cause__, (MaterialError, sqlite3.OperationalError))
        self.assertEqual(len(self.fixture.runner_calls), 1)


if __name__ == '__main__':
    unittest.main()
