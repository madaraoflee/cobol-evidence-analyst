"""Read-only production-path reproduction of a one-shot scoped close error."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from unittest import mock

REPO = Path('/private/tmp/cobol-global-formula-audit-tree-20261110')
OUT = Path('/private/tmp/cobol-online-lease-result-boundary-20261111')
sys.path[:0] = [str(REPO / 'poc'), str(REPO / 'poc/tests')]
from api_error_details import build_diagnostic, sanitize_diagnostic
from online_sqlite_store import OnlineSQLiteStore, supports_managed_wal
from test_online_workspace import OnlineWorkspaceTests

assert supports_managed_wal(), 'This reproduction needs an actually supported runtime'
files = ['poc/online_state.py', 'poc/online_workspace.py', 'poc/online_sqlite_store.py']
source_hashes = {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in files}
results = []
for inject in (False, True):
    fixture = OnlineWorkspaceTests()
    fixture.setUp()
    try:
        connected = fixture.connect()
        conversation = fixture.conversation(connected['workspace_id'])
        original_runner = fixture.runner
        safe = sanitize_diagnostic(build_diagnostic('HTTP_ERROR', http_status=500))
        previous = {'request_status': 'idle', 'step': 1, 'marker': 'last_persisted_progress'}
        returned = {'request_status': 'idle', 'step': 2, 'marker': 'new_completed_runner_result', 'diagnostic': safe}
        returned_results = []
        def runner(query, source, question, **options):
            partial = original_runner(query, source, question, **options)
            options['progress']({'phase': 'reading', 'checkpoint': previous})
            partial.update(status='failed', error='HTTP_ERROR', diagnostic=safe,
                answer='Verified partial business answer retained after the upstream HTTP 500.',
                checkpoint=returned)
            returned_results.append(deepcopy(partial))
            return partial
        fixture.app.investigator = runner
        close = OnlineSQLiteStore.close
        faults = []
        def fail_final_close_once(store, *, checkpoint=False):
            is_target = store is fixture.app.store._analysis_database and checkpoint
            close(store, checkpoint=checkpoint)
            if inject and is_target and not faults:
                faults.append('actual_managed_close_completed_then_operational_error')
                raise sqlite3.OperationalError('Injected one-shot analysis lease close error')
        with mock.patch.object(OnlineSQLiteStore, 'close', new=fail_final_close_once), \
                mock.patch.object(fixture.app, '_complete', wraps=fixture.app._complete) as complete:
            job = fixture.ask(conversation['id'])
            for thread in list(fixture.app._threads.values()):
                thread.join(3)
            public_saved = fixture.app.state()['conversation']
            with fixture.app.store.transaction() as db:
                saved = fixture.app.store.get(db, 'conversations', conversation['id'])
            observations = {'injected': inject, 'faults': faults, 'runner_return_count': len(returned_results),
                'complete_call_count': complete.call_count, 'job': job,
                'pending': saved['pending'], 'public_pending': public_saved['pending'], 'messages': saved['messages'],
                'expected_returned_checkpoint': returned, 'expected_returned_diagnostic': safe,
                'returned_partial_answer': returned_results[0]['answer'],
                'idle_files': sorted(path.name for path in fixture.state_root.iterdir()),
                'real_model_calls': 0, 'real_company_source_access': False}
            results.append(observations)
    finally:
        fixture.doCleanups()

assert source_hashes == {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in files}
result = {'kind': 'new_reproduction', 'sqlite': sqlite3.sqlite_version,
    'source_hashes_unchanged': source_hashes, 'cases': results}
output = OUT / 'before-result-cleanup-v2.json'
with output.open('x') as destination:
    json.dump(result, destination, indent=2)
    destination.write('\n')
for observed in results:
    print(json.dumps({'injected': observed['injected'], 'faults': observed['faults'],
        'runner_return_count': observed['runner_return_count'], 'complete_call_count': observed['complete_call_count'],
        'job_error': observed['job']['error'], 'job_diagnostic': observed['job'].get('diagnostic'),
        'pending_checkpoint': observed['pending']['checkpoint'],
        'assistant_answers': [m['content'] for m in observed['messages'] if m['role']=='assistant']}))
