"""Graph preview reuse never replaces source reads or keeps a database open."""

from contextlib import closing
import ctypes
from pathlib import Path
import os
import shutil
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import web_app


class WorkspaceGraphCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="graph-preview-cache-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.database = self.root / "structural-index.sqlite"
        self.snapshot = "snapshot-one"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript("""
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE source_files(relative_path TEXT PRIMARY KEY);
                CREATE TABLE code_units(unit_id TEXT PRIMARY KEY,program_name TEXT);
                CREATE TABLE evidence_spans(evidence_id TEXT PRIMARY KEY,relative_path TEXT,
                    start_line INTEGER,end_line INTEGER);
                CREATE TABLE relations(relation_id TEXT PRIMARY KEY,relative_path TEXT,
                    from_entity_id TEXT,target_name TEXT,relation_type TEXT,status TEXT,
                    target_entity_id TEXT,evidence_id TEXT);
                CREATE INDEX repo_relations_path ON relations(relative_path,relation_type);
            """)
            db.execute("INSERT INTO metadata VALUES('snapshot_id',?)", (self.snapshot,))
            db.execute("INSERT INTO source_files VALUES('entry.cbl')")
            db.execute("INSERT INTO code_units VALUES('entry','ENTRY-FLOW')")
            db.executemany("INSERT INTO evidence_spans VALUES(?,'entry.cbl',?,?)",
                ((f"e-{i}", i + 1, i + 1) for i in range(220)))
            db.executemany("INSERT INTO relations VALUES(?,'entry.cbl','entry','SERVICE-FLOW',"
                "'CALLS','resolved',NULL,?)", ((f"r-{i}", f"e-{i}") for i in range(220)))
        self.state = web_app.WorkbenchState(state_path=None, demo_output_root=self.root / "demo")

    def preview(self, database=None, snapshot=None):
        return self.state._relationship_preview(database or self.database, snapshot or self.snapshot)

    def change_target(self, target="CHANGED-FLOW", database=None):
        with closing(sqlite3.connect(database or self.database)) as db, db:
            db.execute("UPDATE relations SET target_name=? WHERE relation_id='r-0'", (target,))

    def expected(self):
        return web_app._relationships(self.database, self.snapshot)

    def test_unchanged_file_reuses_preview_but_checks_snapshot_and_isolates_results(self):
        original = web_app._relationships
        statements = []
        original_connect = sqlite3.connect

        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with mock.patch.object(web_app, "_relationships", wraps=original) as query:
            first = self.preview()
            expected = self.expected()
            statements.clear()
            with mock.patch.object(web_app.sqlite3, "connect", side_effect=connect):
                second = self.preview()
            self.assertEqual(query.call_count, 2)  # The second call above is the independent expected value.
            self.assertEqual(second, expected)
            self.assertEqual(len(second["edges"]), 200)
            self.assertTrue(second["truncated"])
            self.assertTrue(any(statement == "BEGIN" for statement in statements))
            self.assertTrue(any("FROM metadata" in statement for statement in statements))
            self.assertFalse(any("FROM relations" in statement for statement in statements))
            first["edges"][0]["target_name"] = "MUTATED-FIRST"
            second["edges"].clear()
            self.assertEqual(self.preview(), expected)

    def test_same_snapshot_database_update_is_observed(self):
        before = self.preview()
        self.change_target()
        with mock.patch.object(web_app, "_relationships", wraps=web_app._relationships) as query:
            after = self.preview()
            self.assertEqual(query.call_count, 1)
        self.assertNotEqual(after, before)
        self.assertEqual(after, self.expected())

    def test_header_change_invalidates_even_when_file_identity_is_reported_unchanged(self):
        before = self.preview()
        original_stat = self.database.stat()
        self.change_target()
        os.utime(self.database, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        path_stat = Path.stat

        def stat(path, *args, **kwargs):
            return original_stat if path == self.database else path_stat(path, *args, **kwargs)

        with mock.patch.object(Path, "stat", stat), mock.patch.object(web_app.os, "fstat", return_value=original_stat):
            after = self.preview()
        self.assertNotEqual(after, before)
        self.assertEqual(after, self.expected())

    def test_ctime_change_invalidates_with_identical_header_and_mtime(self):
        before = self.preview()
        original_stat = self.database.stat()
        changed_stat = SimpleNamespace(**{key: getattr(original_stat, key) for key in
            ("st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")})
        changed_stat.st_ctime_ns += 1
        path_stat = Path.stat

        def stat(path, *args, **kwargs):
            return changed_stat if path == self.database else path_stat(path, *args, **kwargs)

        with mock.patch.object(Path, "stat", stat), mock.patch.object(web_app.os, "fstat", return_value=changed_stat), \
                mock.patch.object(web_app, "_relationships", wraps=web_app._relationships) as query:
            self.assertEqual(self.preview(), before)
            self.assertEqual(query.call_count, 1)

    def test_nonempty_wal_reads_committed_changes_without_caching(self):
        before = self.preview()
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE relations SET target_name='WAL-TARGET' WHERE relation_id='r-0'")
            writer.commit()
            self.assertGreater(Path(str(self.database) + "-wal").stat().st_size, 0)
            with mock.patch.object(web_app, "_relationships", wraps=web_app._relationships) as query:
                after = self.preview()
                self.assertIsNone(self.state._graph_preview_cache)
                writer.execute("UPDATE relations SET target_name='NEXT-TARGET' WHERE relation_id='r-0'")
                writer.commit()
                later = self.preview()
                self.assertEqual(query.call_count, 2)
            self.assertIsNone(self.state._graph_preview_cache)
            self.assertNotEqual(before, after)
            self.assertNotEqual(after, later)
            self.assertEqual(later, self.expected())

    def test_checkpointed_wal_detects_change_when_ctime_is_creation_time(self):
        path_stat, descriptor_stat = Path.stat, os.fstat
        creation_time = self.database.stat().st_ctime_ns
        change_time = [1]

        def creation_stat(value):
            return SimpleNamespace(**{key: creation_time if key == "st_ctime_ns" else getattr(value, key)
                for key in ("st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")})

        def stat(path, *args, **kwargs):
            value = path_stat(path, *args, **kwargs)
            return creation_stat(value) if path == self.database else value

        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            with mock.patch.object(Path, "stat", stat), \
                    mock.patch.object(web_app.os, "fstat", side_effect=lambda fd: creation_stat(descriptor_stat(fd))), \
                    mock.patch.object(web_app, "_graph_file_change_time", side_effect=lambda fd: change_time[0], create=True):
                before = self.preview()
                captured = path_stat(self.database)
                header = self.database.read_bytes()[:100]
                writer.execute("UPDATE relations SET target_name='UPDATED-FLOW' WHERE relation_id='r-0'")
                writer.commit()
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                os.utime(self.database, ns=(captured.st_atime_ns, captured.st_mtime_ns))
                change_time[0] = 2
                self.assertEqual(self.database.read_bytes()[:100], header)
                self.assertEqual(path_stat(self.database).st_size, captured.st_size)
                self.assertEqual(Path(str(self.database) + "-wal").stat().st_size, 0)
                after = self.preview()
                self.assertEqual(before["edges"][0]["target_name"], "SERVICE-FLOW")
                self.assertEqual(after["edges"][0]["target_name"], "UPDATED-FLOW")
                self.assertEqual(after, self.expected())

    def test_checkpointed_wal_uses_real_filesystem_change_time(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.preview()
            self.assertIsNotNone(self.state._graph_preview_cache)
            captured = self.database.stat()
            with self.database.open("rb") as stream:
                header = stream.read(100)
                earlier_change_time = web_app._graph_file_change_time(stream.fileno())
            writer.execute("UPDATE relations SET target_name='UPDATED-FLOW' WHERE relation_id='r-0'")
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            os.utime(self.database, ns=(captured.st_atime_ns, captured.st_mtime_ns))
            with self.database.open("rb") as stream:
                self.assertEqual(stream.read(100), header)
                self.assertNotEqual(web_app._graph_file_change_time(stream.fileno()), earlier_change_time)
            self.assertEqual(self.database.stat().st_size, captured.st_size)
            self.assertEqual(Path(str(self.database) + "-wal").stat().st_size, 0)
            self.assertEqual(self.preview()["edges"][0]["target_name"], "UPDATED-FLOW")

    def test_missing_or_unstable_change_time_disables_reuse_and_closes_file(self):
        self.preview()
        with mock.patch.object(web_app, "_graph_file_change_time", return_value=None), \
                mock.patch.object(web_app, "_relationships", wraps=web_app._relationships) as query:
            self.preview()
            self.assertEqual(query.call_count, 1)
        self.assertIsNone(self.state._graph_preview_cache)
        with mock.patch.object(web_app, "_graph_file_change_time", side_effect=[1, 2]):
            self.preview()
        self.assertIsNone(self.state._graph_preview_cache)
        moved = self.root / "moved.sqlite"
        os.replace(self.database, moved)
        os.replace(moved, self.database)

    def test_windows_change_time_reads_basic_info_from_borrowed_handle(self):
        def observe(handle, kind, pointer, size):
            self.assertEqual((handle, kind, size), (123, 0, 40))
            self.assertEqual(type(pointer._obj).ChangeTime.offset, 24)
            pointer._obj.ChangeTime = 456
            return 1

        observer = mock.Mock(side_effect=observe)
        close_handle = mock.Mock()
        api = SimpleNamespace(kernel32=SimpleNamespace(
            GetFileInformationByHandleEx=observer, CloseHandle=close_handle))
        runtime = SimpleNamespace(get_osfhandle=mock.Mock(return_value=123))
        with mock.patch.object(web_app.sys, "platform", "win32"), \
                mock.patch.dict(sys.modules, {"msvcrt": runtime}), \
                mock.patch.object(ctypes, "windll", api, create=True):
            self.assertEqual(web_app._graph_file_change_time(7), 456)
            runtime.get_osfhandle.assert_called_with(7)
            observer.side_effect = None
            observer.return_value = 0
            self.assertIsNone(web_app._graph_file_change_time(7))
            observer.side_effect = OSError("metadata unavailable")
            self.assertIsNone(web_app._graph_file_change_time(7))
        close_handle.assert_not_called()

    def test_nonempty_rollback_journal_prevents_reuse(self):
        self.preview()
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=PERSIST")
            writer.execute("UPDATE relations SET target_name='JOURNAL-TARGET' WHERE relation_id='r-0'")
            writer.commit()
        self.assertGreater(Path(str(self.database) + "-journal").stat().st_size, 0)
        self.assertIsNone(web_app._graph_database_signature(self.database))
        with mock.patch.object(web_app, "_relationships", wraps=web_app._relationships) as query:
            self.assertEqual(self.preview(), self.expected())
            self.assertEqual(query.call_count, 2)
        self.assertIsNone(self.state._graph_preview_cache)

    def test_hit_checks_snapshot_even_when_signature_appears_unchanged(self):
        self.preview()
        signature = self.state._graph_preview_cache["signature"]
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE metadata SET value='snapshot-two' WHERE key='snapshot_id'")
        with mock.patch.object(web_app, "_graph_database_signature", return_value=signature):
            with self.assertRaisesRegex(ValueError, "relationship snapshot mismatch"):
                self.preview()
        self.assertIsNone(self.state._graph_preview_cache)

    def test_file_replacement_is_allowed_and_invalidates_the_entry(self):
        before = self.preview()
        original_stat = self.database.stat()
        replacement = self.root / "replacement.sqlite"
        shutil.copyfile(self.database, replacement)
        self.change_target("REPLACED-FLOW", replacement)
        os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        os.replace(replacement, self.database)
        after = self.preview()
        self.assertNotEqual(after, before)
        self.assertEqual(after, self.expected())

    def test_snapshot_query_error_discards_cached_entry(self):
        expected = self.preview()
        with mock.patch.object(web_app.sqlite3, "connect", side_effect=sqlite3.OperationalError("read failed")):
            with self.assertRaises(sqlite3.OperationalError):
                self.preview()
        self.assertIsNone(self.state._graph_preview_cache)
        with mock.patch.object(web_app, "_relationships", wraps=web_app._relationships) as query:
            self.assertEqual(self.preview(), expected)
            self.assertEqual(query.call_count, 1)

    def test_original_query_error_or_file_change_does_not_populate_cache(self):
        with mock.patch.object(web_app, "_relationships", side_effect=sqlite3.OperationalError("query failed")):
            with self.assertRaises(sqlite3.OperationalError):
                self.preview()
        self.assertIsNone(self.state._graph_preview_cache)
        original = web_app._relationships

        def changing_query(*args):
            result = original(*args)
            self.change_target()
            return result

        with mock.patch.object(web_app, "_relationships", side_effect=changing_query):
            self.preview()
        self.assertIsNone(self.state._graph_preview_cache)
        self.assertEqual(self.preview(), self.expected())

    def test_uncertain_signature_uses_original_query_and_never_caches(self):
        self.preview()
        with mock.patch.object(web_app, "_graph_database_signature", return_value=None), \
                mock.patch.object(web_app, "_relationships", wraps=web_app._relationships) as query:
            self.preview()
            self.preview()
            self.assertEqual(query.call_count, 2)
            self.assertIsNone(self.state._graph_preview_cache)
        malformed = self.root / "malformed.sqlite"
        malformed.write_bytes(b"not a database" * 20)
        self.assertIsNone(web_app._graph_database_signature(malformed))

    def test_only_one_database_and_limit_are_retained(self):
        self.preview()
        other = self.root / "other.sqlite"
        shutil.copyfile(self.database, other)
        with mock.patch.object(web_app, "_relationships", wraps=web_app._relationships) as query:
            self.preview(other)
            self.preview()
            with mock.patch.object(web_app, "MAX_GRAPH_EDGES", 20):
                self.assertEqual(len(self.preview()["edges"]), 20)
            self.assertEqual(query.call_count, 3)

    def test_cross_file_provenance_and_missing_nodes_keep_original_results(self):
        self.preview()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE evidence_spans SET relative_path='before.cbl' WHERE evidence_id='e-0'")
        changed = self.preview()
        self.assertEqual(changed, self.expected())
        self.assertEqual(changed["edges"][0]["relative_path"], "before.cbl")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DELETE FROM code_units")
        self.assertEqual(self.preview(), {"edges": [], "truncated": False, "snapshot_id": self.snapshot})

    def test_signature_change_during_hit_falls_back_without_storing(self):
        expected = self.preview()
        signature = self.state._graph_preview_cache["signature"]
        with mock.patch.object(web_app, "_graph_database_signature", side_effect=[signature, None]), \
                mock.patch.object(web_app, "_relationships", wraps=web_app._relationships) as query:
            self.assertEqual(self.preview(), expected)
            self.assertEqual(query.call_count, 1)
        self.assertIsNone(self.state._graph_preview_cache)

    def test_restore_and_failed_work_clear_the_entry(self):
        self.preview()
        self.state._restore_workspace()
        self.assertIsNone(self.state._graph_preview_cache)
        self.preview()
        self.state._restore_after_failure({})
        self.assertIsNone(self.state._graph_preview_cache)


if __name__ == "__main__":
    unittest.main()
