"""Scoped progress-write leases preserve idle layout and state recovery."""
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
from online_material_format import MaterialError
from online_sqlite_store import supports_managed_wal
from online_state import OnlineState


class OnlineStateManagedWritesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'online'
        self.state = OnlineState(self.root)
        self.addCleanup(self.state.close)
        with self.state.transaction() as db:
            self.state.put(db, 'jobs', 'job', {'id': 'job', 'value': 'original'})

    def assert_idle_layout(self):
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ['online-state.sqlite'])
        with self.state.transaction() as db:
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            self.assertEqual(db.execute('PRAGMA synchronous').fetchone()[0], 2)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_idle_single_file_layout_survives_real_progress_writes_and_final_lease_cleanup(self):
        self.assert_idle_layout()
        with self.state.analysis_writes():
            with self.state.transaction() as db:
                available = supports_managed_wal()
                self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'wal' if available else 'delete')
                self.assertEqual(db.execute('PRAGMA synchronous').fetchone()[0], 2)
                if available:
                    self.assertIs(db.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE), True)
                    self.assertEqual(db.execute('PRAGMA wal_autocheckpoint').fetchone()[0], 1000)
                self.state.put(db, 'jobs', 'job', {'id': 'job', 'value': 'progress-saved'})
        self.assert_idle_layout()
        self.state.close()
        reopened = OnlineState(self.root)
        self.addCleanup(reopened.close)
        with reopened.transaction() as db:
            self.assertEqual(reopened.get(db, 'jobs', 'job'), {'id': 'job', 'value': 'progress-saved'})

    def test_nested_leases_keep_wal_until_the_last_lease_without_changing_idle_layout(self):
        with self.state.analysis_writes():
            with self.state.transaction() as first:
                self.state.put(first, 'settings', 'first', {'value': 1})
            with self.state.analysis_writes():
                with self.state.transaction() as second:
                    self.state.put(second, 'settings', 'second', {'value': 2})
                self.assertIs(first is second, supports_managed_wal())
            with self.state.transaction() as third:
                self.assertEqual(third.execute('PRAGMA journal_mode').fetchone()[0], 'wal' if supports_managed_wal() else 'delete')
                self.assertEqual(self.state.get(third, 'settings', 'second'), {'value': 2})
        self.assert_idle_layout()

    def test_concurrent_job_leases_keep_other_jobs_active_and_end_with_single_file(self):
        entered, second_entered, first_exited = threading.Event(), threading.Event(), threading.Event()
        errors, modes = [], []
        def first():
            try:
                with self.state.analysis_writes():
                    entered.set()
                    if not second_entered.wait(5):
                        raise AssertionError('Second analysis lease did not enter')
                first_exited.set()
            except BaseException as error:
                errors.append(error)
                first_exited.set()
        def second():
            try:
                if not entered.wait(5):
                    raise AssertionError('First analysis lease did not enter')
                with self.state.analysis_writes():
                    second_entered.set()
                    if not first_exited.wait(5):
                        raise AssertionError('First analysis lease did not finish')
                    with self.state.transaction() as db:
                        modes.append(db.execute('PRAGMA journal_mode').fetchone()[0])
                        self.state.put(db, 'settings', 'concurrent', {'value': 'complete'})
            except BaseException as error:
                errors.append(error)
                second_entered.set()
        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(8)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(modes, ['wal' if supports_managed_wal() else 'delete'])
        self.assert_idle_layout()

    def test_unsafe_stock_engine_and_explicit_override_do_not_enter_managed_wal(self):
        import online_state
        with mock.patch('online_sqlite_store.sqlite3.sqlite_version_info', (3, 50, 4)):
            with self.assertRaises(MaterialError) as caught:
                OnlineState(Path(self.temporary.name) / 'unsupported-mode', journal_mode='WAL')
            self.assertEqual(caught.exception.code, 'ONLINE_STATE_UNAVAILABLE')
        with mock.patch.object(online_state, 'supports_managed_wal', return_value=False):
            with self.state.analysis_writes():
                with self.state.transaction() as db:
                    self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        explicit = OnlineState(Path(self.temporary.name) / 'explicit', journal_mode='DELETE')
        self.addCleanup(explicit.close)
        with explicit.analysis_writes():
            with explicit.transaction() as db:
                self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        self.assert_idle_layout()

    def test_transaction_failure_during_analysis_keeps_original_record_and_restores_idle_layout(self):
        with self.assertRaisesRegex(RuntimeError, 'original failure'):
            with self.state.analysis_writes():
                with self.state.transaction() as db:
                    self.state.put(db, 'jobs', 'job', {'id': 'job', 'value': 'must-rollback'})
                    self.state.put(db, 'settings', 'uncommitted', {'value': 1})
                    raise RuntimeError('original failure')
        self.assert_idle_layout()
        with self.state.transaction() as db:
            self.assertEqual(self.state.get(db, 'jobs', 'job'), {'id': 'job', 'value': 'original'})
            self.assertIsNone(self.state.get(db, 'settings', 'uncommitted'))

    def test_nested_state_transaction_rolls_back_only_inner_work_under_safe_lease(self):
        if not supports_managed_wal():
            with self.assertRaises(MaterialError) as caught:
                OnlineState(Path(self.temporary.name) / 'unsupported', journal_mode='WAL')
            self.assertEqual(caught.exception.code, 'ONLINE_STATE_UNAVAILABLE')
        else:
            with self.state.analysis_writes():
                with self.state.transaction() as outer:
                    self.state.put(outer, 'settings', 'first', {'value': 1})
                    with self.assertRaisesRegex(RuntimeError, 'inner failure'):
                        with self.state.transaction() as inner:
                            self.state.put(inner, 'settings', 'second', {'value': 2})
                            raise RuntimeError('inner failure')
                    self.state.put(outer, 'settings', 'third', {'value': 3})
            self.assert_idle_layout()
            with self.state.transaction() as db:
                self.assertEqual(self.state.get(db, 'settings', 'first'), {'value': 1})
                self.assertIsNone(self.state.get(db, 'settings', 'second'))
                self.assertEqual(self.state.get(db, 'settings', 'third'), {'value': 3})

    def test_active_transaction_cannot_be_closed_or_switched_to_an_analysis_lease(self):
        with self.state.transaction() as db:
            with self.assertRaises(sqlite3.ProgrammingError):
                self.state.close()
            if supports_managed_wal():
                with self.assertRaises(MaterialError) as caught:
                    with self.state.analysis_writes():
                        pass
                self.assertEqual(caught.exception.code, 'ONLINE_STATE_UNAVAILABLE')
            self.state.put(db, 'settings', 'retained', {'value': True})
        self.assert_idle_layout()

    def test_abrupt_process_exit_recovers_uncommitted_hot_journal_and_committed_state(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                self.state.close()
                child = subprocess.run([sys.executable, str(Path(__file__).with_name('online_state_crash_writer.py')),
                    str(self.root), str(int(committed))], capture_output=True, text=True, timeout=20)
                self.assertEqual(child.returncode, 74 if committed else 73, child.stdout + child.stderr)
                if not committed:
                    journal = Path(str(self.state.path) + '-journal')
                    self.assertTrue(journal.is_file())
                    self.assertNotEqual(journal.read_bytes()[:8], b'\0' * 8)
                reopened = OnlineState(self.root)
                self.addCleanup(reopened.close)
                with reopened.transaction() as db:
                    actual = reopened.get(db, 'jobs', 'job')
                    self.assertEqual(actual['value'], 'committed' if committed else 'original')
                    self.assertEqual(reopened.get(db, 'settings', 'marker'), {'value': 'committed'} if committed else None)
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                reopened.close()
                self.assert_idle_layout()

    def fail_exit_lock(self, state):
        original, calls = state._mode_locked, [0]
        @contextmanager
        def injected():
            calls[0] += 1
            if calls[0] == 2:
                raise sqlite3.OperationalError('Synthetic lease exit lock timeout')
            with original():
                yield
        return mock.patch.object(state, '_mode_locked', injected)

    def test_exit_lock_timeout_release_is_drained_by_next_transaction_lease_or_close(self):
        if not supports_managed_wal():
            with self.state.analysis_writes():
                pass
            self.assert_idle_layout()
        else:
            for recovery in ('transaction', 'lease', 'close'):
                with self.subTest(recovery=recovery):
                    state = OnlineState(Path(self.temporary.name) / ('release-' + recovery))
                    self.addCleanup(state.close)
                    with state.transaction() as db:
                        state.put(db, 'settings', 'retained', {'value': recovery})
                    with self.fail_exit_lock(state):
                        with self.assertRaises(MaterialError) as caught:
                            with state.analysis_writes():
                                pass
                    self.assertEqual(caught.exception.code, 'ONLINE_STATE_UNAVAILABLE')
                    if recovery == 'transaction':
                        with state.transaction() as db:
                            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
                    elif recovery == 'lease':
                        with state.analysis_writes():
                            with state.transaction() as db:
                                self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
                    else:
                        state.close()
                    self.assertEqual(state._analysis_depth, 0)
                    self.assertFalse(state._analysis_leases)
                    self.assertEqual(sorted(path.name for path in state.root.iterdir()), ['online-state.sqlite'])
                    with state.transaction() as db:
                        self.assertEqual(state.get(db, 'settings', 'retained'), {'value': recovery})
                        self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_exit_lock_timeout_keeps_original_job_error_and_later_close_recovers_idle_layout(self):
        original = RuntimeError('Original analysis failure')
        if supports_managed_wal():
            with self.fail_exit_lock(self.state):
                with self.assertRaises(RuntimeError) as caught:
                    with self.state.analysis_writes():
                        raise original
            self.assertIs(caught.exception, original)
            if callable(getattr(original, 'add_note', None)):
                self.assertTrue(any('lease cleanup failed' in note for note in getattr(original, '__notes__', [])))
        else:
            with self.assertRaises(RuntimeError) as caught:
                with self.state.analysis_writes():
                    raise original
            self.assertIs(caught.exception, original)
        self.state.close()
        self.assert_idle_layout()
        with self.state.transaction() as db:
            self.assertEqual(self.state.get(db, 'jobs', 'job'), {'id': 'job', 'value': 'original'})

    def test_released_lease_waits_until_sql_transaction_commits_before_checkpointing(self):
        lease = self.state.analysis_writes()
        lease.__enter__()
        exited = False
        try:
            with self.state.transaction() as db:
                self.state.put(db, 'settings', 'first', {'value': 1})
                lease.__exit__(None, None, None)
                exited = True
                self.assertTrue(db.in_transaction)
                self.state.put(db, 'settings', 'second', {'value': 2})
        finally:
            if not exited:
                lease.__exit__(None, None, None)
        self.assert_idle_layout()
        self.assertEqual(self.state._analysis_depth, 0)
        with self.state.transaction() as db:
            self.assertEqual(self.state.get(db, 'settings', 'first'), {'value': 1})
            self.assertEqual(self.state.get(db, 'settings', 'second'), {'value': 2})

    def test_state_database_and_every_sidecar_are_rejected_before_open_without_touching_external_file(self):
        for suffix in ('', '-wal', '-shm', '-journal'):
            with self.subTest(sidecar=suffix), tempfile.TemporaryDirectory() as directory:
                state = OnlineState(Path(directory) / 'state')
                state.close()
                target = Path(directory) / 'outside'
                target.write_bytes(b'External original content')
                before = (hashlib.sha256(target.read_bytes()).hexdigest(), target.stat().st_mtime_ns)
                path = Path(str(state.path) + suffix)
                if path.exists():
                    path.unlink()
                path.symlink_to(target)
                try:
                    with mock.patch('online_sqlite_store.sqlite3.connect', side_effect=AssertionError('Unsafe SQLite open')) as connect:
                        with self.assertRaises(MaterialError):
                            with state.transaction():
                                pass
                        connect.assert_not_called()
                    self.assertEqual((hashlib.sha256(target.read_bytes()).hexdigest(), target.stat().st_mtime_ns), before)
                finally:
                    path.unlink()


class OnlineWorkspaceManagedWritesTests(unittest.TestCase):
    def setUp(self):
        import test_online_workspace as fixture_module
        self.fixture = fixture_module.OnlineWorkspaceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_actual_progress_callbacks_use_lease_and_done_is_published_after_idle_layout_returns(self):
        fixture, modes, completed_layouts = self.fixture, [], []
        original_runner = fixture.runner
        def runner(query, source, question, **options):
            for index in range(3):
                options['progress']({'phase': 'investigating', 'checkpoint': {'request_status': 'idle', 'step': index}})
                with fixture.app.store.transaction() as db:
                    modes.append(db.execute('PRAGMA journal_mode').fetchone()[0])
            return original_runner(query, source, question, **options)
        fixture.app.investigator = runner
        original_complete = fixture.app._complete
        def complete(*arguments, **options):
            completed_layouts.append(sorted(path.name for path in fixture.state_root.iterdir()))
            return original_complete(*arguments, **options)
        with mock.patch.object(fixture.app, '_complete', side_effect=complete):
            connected = fixture.connect()
            conversation = fixture.conversation(connected['workspace_id'])
            result = fixture.ask(conversation['id'])
        self.assertEqual(result['status'], 'answered')
        self.assertEqual(modes, ['wal' if supports_managed_wal() else 'delete'] * 3)
        self.assertEqual(completed_layouts, [['online-state.sqlite']])
        self.assertEqual(sorted(path.name for path in fixture.state_root.iterdir()), ['online-state.sqlite'])

    def test_close_cancels_without_joining_remote_work_then_releases_both_stores_after_cleanup(self):
        fixture, entered, release = self.fixture, threading.Event(), threading.Event()
        def runner(query, source, question, **options):
            options['progress']({'phase': 'investigating', 'checkpoint': {'request_status': 'idle'}})
            entered.set()
            release.wait(5)
            options['check_cancel']()
            raise AssertionError('Cancellation was not delivered')
        fixture.app.investigator = runner
        connected = fixture.connect()
        conversation = fixture.conversation(connected['workspace_id'])
        started = fixture.post('question', {'conversation_id': conversation['id'], 'question': 'Explain current status.',
            'allow_source_transfer': True}, 202)
        self.assertTrue(entered.wait(3))
        inventory = fixture.app.inventory_store
        try:
            with mock.patch.object(fixture.app.store, 'close', wraps=fixture.app.store.close) as state_close, \
                    mock.patch.object(inventory, 'close', wraps=inventory.close) as inventory_close:
                fixture.app.close()
                state_close.assert_not_called()
                inventory_close.assert_not_called()
                release.set()
                for thread in list(fixture.app._threads.values()):
                    thread.join(3)
                self.assertFalse(fixture.app._threads)
                self.assertGreaterEqual(state_close.call_count, 1)
                self.assertGreaterEqual(inventory_close.call_count, 1)
        finally:
            release.set()
        with fixture.app.store.transaction() as db:
            self.assertEqual(fixture.app.store.get(db, 'jobs', started['job_id'])['status'], 'cancelled')
        fixture.app.close()

    def test_online_cleanup_error_still_releases_http_socket(self):
        from web_app import LocalServer
        server = LocalServer.__new__(LocalServer)
        server.job_stream_stop = threading.Event()
        server.socket = mock.Mock()
        error = sqlite3.OperationalError('Synthetic bounded online close failure')
        server.online_app = SimpleNamespace(close=mock.Mock(side_effect=error))
        with self.assertRaises(sqlite3.OperationalError) as caught:
            server.server_close()
        self.assertIs(caught.exception, error)
        self.assertTrue(server.job_stream_stop.is_set())
        server.socket.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
