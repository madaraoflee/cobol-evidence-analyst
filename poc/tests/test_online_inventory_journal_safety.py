"""Production online-inventory transactions survive restart and abrupt exits."""
from copy import deepcopy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / 'poc'), str(REPO / 'poc/tests')]
from online_inventory_store import OnlineInventoryStore
from online_material_format import MaterialError
from online_sqlite_store import supports_managed_wal


class InjectedPersistenceFailure(RuntimeError):
    pass


class OnlineInventoryJournalSafetyTests(unittest.TestCase):
    expected_mode = "WAL" if supports_managed_wal() else "DELETE"

    def setUp(self):
        self.assertIn(self.expected_mode, {'DELETE', 'TRUNCATE', 'PERSIST', 'WAL'},
            'The caller must explicitly choose expected_mode; this draft chooses no default.')
        import test_online_inventory_store as fixture_module
        self.fixture = fixture_module.OnlineInventoryStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.complete_work()
        self.store = self.fixture.store
        self.session = self.fixture.session
        self.options = self.fixture.options
        self.state = self.fixture.state
        self.publication = self.session.publish(self.state, 'Source-grounded original business explanation.', True)
        self.original_publication = deepcopy(self.publication.export_data())
        self.key, self.row = self.session.rows('findings', limit=1)[0]
        self.assert_mode_and_integrity(self.store)

    def assert_mode_and_integrity(self, store):
        with store.connection() as db:
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0].upper(), self.expected_mode)
            self.assertEqual(db.execute('PRAGMA synchronous').fetchone()[0], 2, 'FULL durability is required')
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def snapshot(self):
        with self.store.connection() as db:
            return {name: db.execute('SELECT * FROM ' + name + ' ORDER BY rowid').fetchall()
                for name in ('sessions', 'versions', 'records', 'tasks', 'actions', 'publications')}

    def reopen(self):
        reopened = OnlineInventoryStore(self.store.root)
        self.addCleanup(reopened.close)
        session = reopened.session(**self.options, identifier=self.session.id)
        publication = reopened.publication(self.publication.id, conversation_id=self.options['conversation_id'],
            workspace_id=self.options['workspace_id'], revision=self.options['manifest']['revision'])
        self.assert_mode_and_integrity(reopened)
        self.assertEqual(publication.export_data(), self.original_publication)
        return reopened, session, publication

    def crash_child(self, *, committed, legacy_delete):
        self.store.close()
        payload = {'options': self.options, 'session_id': self.session.id, 'key': self.key,
            'row': self.row, 'marker': 'committed-original-record' if committed else 'uncommitted-original-record'}
        path = self.store.root / ('crash-committed.json' if committed else 'crash-uncommitted.json')
        path.write_text(json.dumps(payload), encoding='utf-8')
        child = subprocess.run([sys.executable, str(Path(__file__).with_name('online_sqlite_crash_writer.py')),
            str(REPO), str(self.store.root), str(path), str(int(committed)), str(int(legacy_delete))],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(child.returncode, 74 if committed else 73, child.stdout + child.stderr)
        return payload['marker']

    def test_real_legacy_delete_database_reopens_without_losing_records_or_old_publication(self):
        before = self.snapshot()
        # Simulate an existing database left by the former production policy.
        self.store.close()
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=DELETE').fetchone()[0].upper(), 'DELETE')
        reopened, session, _ = self.reopen()
        self.assertEqual(session.count('findings'), len(self.original_publication['findings']))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(session.get('findings', self.key), self.row)
        self.assertEqual(reopened.path, self.store.path)

    def test_capture_exception_rolls_back_original_meta_records_versions_actions_and_tasks(self):
        before = self.snapshot()
        work = self.state['work_ledger']
        changed_state = {**self.state, 'work_ledger': {
            key: deepcopy(value) for key, value in work.items() if key != '_session'}}
        changed_work = changed_state['work_ledger']
        changed_work['_session'] = self.session
        changed_work['findings'][self.key] = deepcopy(self.row)
        changed_work['findings'][self.key]['summary'] = 'Changed business wording must be rolled back.'
        changed_work['legacy_checkpoint_recheck'] = not changed_work.get('legacy_checkpoint_recheck', False)
        save_meta = self.session._save_meta
        def fail_after_meta(db, meta):
            save_meta(db, meta)
            raise InjectedPersistenceFailure('Failure after actual records and metadata writes')
        with mock.patch.object(self.session, '_save_meta', side_effect=fail_after_meta):
            with self.assertRaises(InjectedPersistenceFailure):
                self.session.capture(changed_state, action_key='rollback-probe-action')
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.session.action_done('rollback-probe-action'))
        self.reopen()

    def test_abrupt_uncommitted_legacy_delete_hot_journal_recovers_original_transaction(self):
        before = self.snapshot()
        self.crash_child(committed=False, legacy_delete=True)
        journal = Path(str(self.store.path) + '-journal')
        self.assertTrue(journal.is_file(), 'The crash must actually leave an on-disk rollback journal')
        self.assertNotEqual(journal.read_bytes()[:8], b'\0' * 8, 'The rollback journal must be hot, not clean padding')
        _, session, _ = self.reopen()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(session.get('findings', self.key), self.row)
        self.assertNotIn('persistence_crash_probe', session.meta())

    def test_abrupt_exit_after_production_commit_preserves_record_and_meta_together(self):
        marker = self.crash_child(committed=True, legacy_delete=False)
        _, session, _ = self.reopen()
        row = session.get('findings', self.key)
        self.assertTrue(row['summary'].startswith(marker))
        self.assertEqual(session.meta()['persistence_crash_probe'], marker)
        self.assertEqual(row['citations'], self.row['citations'])
        self.assertEqual(row['path'], self.row['path'])
        self.assertEqual(session.count('findings'), len(self.original_publication['findings']))

    def test_concurrent_reader_and_writer_keep_original_snapshot_and_old_publication(self):
        writer_store = OnlineInventoryStore(self.store.root)
        self.addCleanup(writer_store.close)
        writer_session = writer_store.session(**self.options, identifier=self.session.id)
        reader_ready, writer_wrote = threading.Event(), threading.Event()
        errors, observed = [], []
        changed = {**self.row, 'summary': 'Later business interpretation with the same original citations.'}
        def reader():
            try:
                with self.store.connection() as db:
                    db.execute('BEGIN')
                    query = ('SELECT data FROM records WHERE session=? AND kind=? AND key=?',
                        (self.session.id, 'findings', self.key))
                    first = bytes(db.execute(*query).fetchone()[0])
                    reader_ready.set()
                    if not writer_wrote.wait(8):
                        raise AssertionError('Writer did not reach its production transaction')
                    second = bytes(db.execute(*query).fetchone()[0])
                    observed.append((first, second))
            except BaseException as error:
                errors.append(error)
                reader_ready.set()
        def writer():
            try:
                if not reader_ready.wait(8):
                    raise AssertionError('Reader did not start its production snapshot')
                with writer_store.connection() as db:
                    writer_session._put(db, 'findings', self.key, changed, changed['path'])
                    writer_wrote.set()
            except BaseException as error:
                errors.append(error)
                writer_wrote.set()
        threads = [threading.Thread(target=reader), threading.Thread(target=writer)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(12)
        self.assertFalse(any(thread.is_alive() for thread in threads), 'Concurrent transactions failed to finish')
        self.assertEqual(errors, [])
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][0], observed[0][1])
        self.assertEqual(self.session.get('findings', self.key), changed)
        self.reopen()

    def test_actual_database_and_all_sidecar_symlinks_fail_before_sqlite_open_without_external_changes(self):
        for suffix in ('', '-journal', '-wal', '-shm'):
            with self.subTest(sidecar=suffix), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / 'inventory'
                store = OnlineInventoryStore(root)
                store.close()
                target = Path(directory) / 'outside-target'
                target.write_bytes(b'External target must never be changed by inventory open.')
                before = (hashlib.sha256(target.read_bytes()).hexdigest(), target.stat().st_mtime_ns)
                path = Path(str(store.path) + suffix)
                if path.exists():
                    path.unlink()
                path.symlink_to(target)
                try:
                    with mock.patch('online_inventory_store.sqlite3.connect', side_effect=AssertionError('Unsafe SQLite open')) as connect:
                        with self.assertRaises(MaterialError):
                            OnlineInventoryStore(root)
                        connect.assert_not_called()
                    self.assertEqual((hashlib.sha256(target.read_bytes()).hexdigest(), target.stat().st_mtime_ns), before)
                finally:
                    path.unlink()



if __name__ == '__main__':
    unittest.main()
