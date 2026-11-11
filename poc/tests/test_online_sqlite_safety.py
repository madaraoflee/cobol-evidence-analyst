"""Online-only engine policy, transaction boundaries and owned resources."""
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import online_sqlite_store as managed
from online_material_format import MaterialError


class OnlineSQLiteSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'online.sqlite'

    def store(self, **options):
        store = managed.OnlineSQLiteStore(self.path, **options)
        self.addCleanup(store.close)
        return store

    def assert_unsupported_wal(self):
        with self.assertRaises(sqlite3.NotSupportedError):
            self.store(journal_mode='WAL')

    def test_documented_fixed_versions_are_required_and_stock_windows_version_is_not_safe(self):
        for version, expected in (((3, 50, 4), False), ((3, 50, 6), False), ((3, 50, 7), True),
                ((3, 44, 5), False), ((3, 44, 6), True), ((3, 51, 2), False),
                ((3, 51, 3), True), ((3, 53, 2), True)):
            with self.subTest(version=version):
                self.assertIs(managed.wal_version_is_safe(version), expected)

    def test_unsafe_engine_refuses_explicit_wal_before_open_and_default_retains_per_operation_delete(self):
        with mock.patch.object(managed.sqlite3, 'sqlite_version_info', (3, 50, 4)):
            with mock.patch.object(managed.sqlite3, 'connect', side_effect=AssertionError('Unsafe WAL must not open')) as connect:
                self.assert_unsupported_wal()
                connect.assert_not_called()
            store = self.store()
            with store.connection() as first:
                self.assertEqual(first.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
                self.assertEqual(first.execute('PRAGMA synchronous').fetchone()[0], 2)
                first.execute('CREATE TABLE content(value)')
                first.execute('INSERT INTO content VALUES(1)')
            with store.connection() as second:
                self.assertIsNot(first, second)
                self.assertEqual(second.execute('SELECT * FROM content').fetchall(), [(1,)])
            with self.assertRaises(sqlite3.ProgrammingError):
                first.execute('SELECT 1')

    def test_missing_configuration_api_refuses_wal_without_a_silent_api_fallback(self):
        with mock.patch.object(managed.sqlite3, 'sqlite_version_info', (3, 53, 2)), \
                mock.patch.object(managed.sqlite3, 'Connection', SimpleNamespace()), \
                mock.patch.object(managed.sqlite3, 'connect', side_effect=AssertionError('Missing API must fail before open')) as connect:
            self.assertFalse(managed.supports_managed_wal())
            self.assert_unsupported_wal()
            connect.assert_not_called()

    def test_default_runtime_policy_applies_full_and_supported_configuration_then_lazily_reopens(self):
        available = managed.supports_managed_wal()
        store = self.store()
        with store.connection() as first:
            self.assertEqual(first.execute('PRAGMA journal_mode').fetchone()[0], 'wal' if available else 'delete')
            self.assertEqual(first.execute('PRAGMA synchronous').fetchone()[0], 2)
            if available:
                self.assertIs(first.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE), True)
                self.assertEqual(first.execute('PRAGMA wal_autocheckpoint').fetchone()[0], 1000)
            first.execute('CREATE TABLE content(value)')
            first.execute('INSERT INTO content VALUES(1)')
        with store.connection() as second:
            self.assertIs(first is second, available)
            self.assertEqual(second.execute('SELECT * FROM content').fetchall(), [(1,)])
        store.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            first.execute('SELECT 1')
        with store.connection() as third:
            self.assertIsNot(first, third)
            self.assertEqual(third.execute('SELECT * FROM content').fetchall(), [(1,)])
            self.assertEqual(third.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_full_is_applied_and_read_back_before_any_journal_mode_switch(self):
        observations = []
        class ObservedConnection(sqlite3.Connection):
            def execute(connection, sql, *arguments):
                normalized = sql.upper().replace(' ', '')
                if normalized.startswith('PRAGMAJOURNAL_MODE='):
                    self.assertEqual(super(ObservedConnection, connection).execute('PRAGMA synchronous').fetchone()[0], 2)
                    self.assertIn('PRAGMASYNCHRONOUS', observations)
                cursor = super().execute(sql, *arguments)
                observations.append(normalized)
                return cursor
        original = sqlite3.connect
        def connect(*arguments, **options):
            return original(*arguments, **options, factory=ObservedConnection)
        with mock.patch.object(managed.sqlite3, 'connect', side_effect=connect):
            store = self.store()
            with store.connection() as db:
                db.execute('CREATE TABLE content(value)')
        self.assertTrue(any(sql.startswith('PRAGMAJOURNAL_MODE=') for sql in observations))

    def test_runtime_policy_preserves_nested_outer_rollback_or_refuses_unsupported_wal(self):
        if not managed.supports_managed_wal():
            self.assert_unsupported_wal()
        else:
            store = self.store()
            with store.connection() as db:
                db.execute('CREATE TABLE content(value)')
            with self.assertRaisesRegex(RuntimeError, 'outer failure'):
                with store.connection():
                    # The outer context has not written yet. Releasing the
                    # nested savepoint must not commit this insertion.
                    with store.connection() as inner:
                        inner.execute('INSERT INTO content VALUES(1)')
                    raise RuntimeError('outer failure')
            with store.connection() as db:
                self.assertEqual(db.execute('SELECT * FROM content').fetchall(), [])

    def test_runtime_policy_preserves_trigger_rollback_error_and_makes_outer_rollback_only(self):
        if not managed.supports_managed_wal():
            self.assert_unsupported_wal()
        else:
            for operation in ('trigger', 'insert_or_rollback'):
                with self.subTest(operation=operation):
                    store = self.store()
                    with store.connection() as db:
                        db.execute('DROP TABLE IF EXISTS content')
                        db.execute('CREATE TABLE content(value UNIQUE)')
                        if operation == 'trigger':
                            db.execute("CREATE TRIGGER reject_value BEFORE INSERT ON content WHEN NEW.value=2 BEGIN SELECT RAISE(ROLLBACK,'rejected original value'); END")
                    inner_errors = []
                    with self.assertRaises(sqlite3.OperationalError):
                        with store.connection() as outer:
                            outer.execute('INSERT INTO content VALUES(1)')
                            try:
                                with store.connection() as inner:
                                    inner.execute('INSERT INTO content VALUES(2)' if operation == 'trigger' else
                                        'INSERT OR ROLLBACK INTO content VALUES(1)')
                            except sqlite3.IntegrityError as error:
                                inner_errors.append(error)
                            outer.execute('INSERT INTO content VALUES(3)')
                    self.assertEqual(len(inner_errors), 1)
                    self.assertIn('rejected original value' if operation == 'trigger' else 'UNIQUE constraint', str(inner_errors[0]))
                    with store.connection() as db:
                        self.assertEqual(db.execute('SELECT * FROM content').fetchall(), [])
                        db.execute('INSERT INTO content VALUES(4)')
                    with store.connection() as db:
                        self.assertEqual(db.execute('SELECT * FROM content').fetchall(), [(4,)])
                    store.close()

    def test_real_rollback_failure_does_not_replace_the_primary_error_or_leave_a_poisoned_connection(self):
        for kind in (RuntimeError, MaterialError):
            with self.subTest(error=kind.__name__):
                store = self.store()
                with store.connection() as db:
                    db.execute('CREATE TABLE IF NOT EXISTS content(value)')
                original = kind('ORIGINAL_FAILURE')
                with self.assertRaises(kind) as caught:
                    with store.connection() as db:
                        db.execute('INSERT INTO content VALUES(1)')
                        db.close()
                        raise original
                self.assertIs(caught.exception, original)
                if callable(getattr(original, 'add_note', None)):
                    self.assertTrue(any('rollback cleanup failed' in note for note in getattr(original, '__notes__', [])))
                with store.connection() as db:
                    self.assertEqual(db.execute('SELECT * FROM content').fetchall(), [])
                store.close()

    def test_cleanup_notes_cannot_hide_the_original_exception_when_note_api_is_missing_or_broken(self):
        class NoNoteError(RuntimeError):
            add_note = None
        class BrokenNoteError(RuntimeError):
            def add_note(self, note):
                raise ValueError('note handling unavailable')
        for kind in (NoNoteError, BrokenNoteError):
            with self.subTest(error=kind.__name__):
                store = self.store()
                original = kind('ORIGINAL_FAILURE')
                with self.assertRaises(kind) as caught:
                    with store.connection() as db:
                        db.close()
                        raise original
                self.assertIs(caught.exception, original)
                store.close()

    def test_runtime_policy_rejects_active_managed_close_and_bounds_contending_lock_waits(self):
        store = self.store(timeout=0.05)
        if managed.supports_managed_wal():
            with store.connection() as db:
                db.execute('CREATE TABLE content(value)')
                with self.assertRaises(sqlite3.ProgrammingError):
                    store.close()
                db.execute('INSERT INTO content VALUES(1)')
        else:
            self.assert_unsupported_wal()
        held, release = threading.Event(), threading.Event()
        def hold_lock():
            with store._locked():
                held.set()
                release.wait(3)
        thread = threading.Thread(target=hold_lock)
        thread.start()
        self.assertTrue(held.wait(2))
        started = time.monotonic()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                store.close()
            self.assertLess(time.monotonic() - started, 1)
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        store.close()

    def _assert_windows_blocks_live_database_rename(self, path, backup, store, db):
        self.assertIs(store._db, db)
        self.assertFalse(db.in_transaction)
        info = path.stat()
        before = (path.read_bytes(), info.st_mtime_ns, info.st_dev, info.st_ino)
        with self.assertRaises(PermissionError) as blocked:
            path.rename(backup)
        self.assertEqual(blocked.exception.winerror, 32)
        after = path.stat()
        self.assertEqual((path.read_bytes(), after.st_mtime_ns, after.st_dev, after.st_ino), before)
        self.assertFalse(backup.exists())
        self.assertEqual(db.execute('SELECT * FROM content').fetchall(), [(1,)])
        self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_runtime_policy_rechecks_every_actual_symlink_before_reusing_a_cached_connection(self):
        if not managed.supports_managed_wal():
            self.assert_unsupported_wal()
        else:
            for index, suffix in enumerate(('', '-wal', '-shm', '-journal')):
                with self.subTest(sidecar=suffix):
                    path = self.path.with_name('cached-' + str(index) + '.sqlite')
                    store = managed.OnlineSQLiteStore(path)
                    self.addCleanup(store.close)
                    with store.connection() as db:
                        db.execute('CREATE TABLE content(value)')
                        db.execute('INSERT INTO content VALUES(1)')
                    target = self.path.with_name('outside-' + str(index))
                    target.write_bytes(b'External target must stay unchanged.')
                    original = target.read_bytes(), target.stat().st_mtime_ns
                    sidecar = Path(str(path) + suffix)
                    backup = sidecar.with_name(sidecar.name + '.original')
                    existed = sidecar.exists()
                    windows_locked = sys.platform == 'win32' and existed and suffix in ('', '-wal', '-shm')
                    if windows_locked:
                        self._assert_windows_blocks_live_database_rename(sidecar, backup, store, db)
                        original_is_symlink = Path.is_symlink
                        def changed_path(candidate):
                            return candidate == sidecar or original_is_symlink(candidate)
                        observation = mock.patch.object(Path, 'is_symlink', new=changed_path)
                    else:
                        if existed:
                            sidecar.rename(backup)
                        sidecar.symlink_to(target)
                        observation = nullcontext()
                    issued_sql = []
                    db.set_trace_callback(issued_sql.append)
                    try:
                        with observation:
                            with mock.patch.object(managed.sqlite3, 'connect', side_effect=AssertionError('Guard must reject before open')) as connect:
                                with self.assertRaises(MaterialError) as caught:
                                    with store.connection():
                                        self.fail('Unsafe cached path was yielded')
                                self.assertEqual(caught.exception.code, 'ONLINE_STATE_INVALID')
                                connect.assert_not_called()
                            self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns), original)
                            with self.assertRaises(sqlite3.ProgrammingError):
                                db.execute('SELECT 1')
                        self.assertEqual(issued_sql, [])
                    finally:
                        if not windows_locked:
                            sidecar.unlink()
                            if existed:
                                backup.rename(sidecar)
                    with self.assertRaises(MaterialError):
                        with store.connection():
                            pass

    def test_runtime_policy_never_keeps_writing_a_replaced_regular_database_inode(self):
        if not managed.supports_managed_wal():
            self.assert_unsupported_wal()
        else:
            store = self.store()
            with store.connection() as original:
                original.execute('CREATE TABLE content(value)')
                original.execute('INSERT INTO content VALUES(1)')
            backup = self.path.with_name('original.sqlite')
            if sys.platform == 'win32':
                self._assert_windows_blocks_live_database_rename(self.path, backup, store, original)
                observed_replacement = self.path.with_name('observed-replacement.sqlite')
                observed_replacement.write_bytes(b'Replacement file must not be modified through a cached descriptor.')
                virtual_original = observed_replacement.read_bytes(), observed_replacement.stat().st_mtime_ns
                actual_identity = managed._identity
                def changed_identity(candidate):
                    return actual_identity(observed_replacement) if candidate == self.path else actual_identity(candidate)
                observation = mock.patch.object(managed, '_identity', side_effect=changed_identity)
            else:
                self.path.rename(backup)
                self.path.write_bytes(b'Replacement file must not be modified through a cached descriptor.')
                observation = nullcontext()
            replacement = self.path.read_bytes(), self.path.stat().st_mtime_ns
            issued_sql = []
            original.set_trace_callback(issued_sql.append)
            with observation:
                with self.assertRaises(MaterialError):
                    with store.connection():
                        self.fail('A replaced database was yielded')
                self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), replacement)
                with self.assertRaises(sqlite3.ProgrammingError):
                    original.execute('SELECT 1')
            self.assertEqual(issued_sql, [])
            if sys.platform == 'win32':
                self.assertEqual((observed_replacement.read_bytes(), observed_replacement.stat().st_mtime_ns), virtual_original)

    def test_invalid_lock_timeouts_cannot_create_an_unbounded_wait(self):
        for timeout in (-1, float('inf'), float('nan')):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    self.store(timeout=timeout)
        store = self.store(timeout=0)
        store.close()


if __name__ == '__main__':
    unittest.main()
