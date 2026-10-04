"""Real source updates work with Python 3.10's smaller SQLite export surface."""

from contextlib import closing, contextmanager
import errno
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyze_source import analyze_source
from api_error_details import build_local_diagnostic, sanitize_diagnostic
import source_update
import web_app


LEGACY_MISSING_EXPORTS = (
    "SQLITE_BUSY", "SQLITE_LOCKED", "SQLITE_FULL", "SQLITE_READONLY",
    "SQLITE_CORRUPT", "SQLITE_NOTADB", "SQLITE_IOERR", "SQLITE_CANTOPEN",
    "SQLITE_ERROR", "SQLITE_NOMEM", "SQLITE_PERM", "SQLITE_ABORT",
    "SQLITE_INTERRUPT", "SQLITE_CONSTRAINT", "SQLITE_MISMATCH", "SQLITE_MISUSE",
    "SQLITE_AUTH", "SQLITE_RANGE", "SQLITE_TOOBIG", "SQLITE_PROTOCOL",
    "SQLITE_EMPTY", "SQLITE_FORMAT", "SQLITE_ROW",
)


@contextmanager
def legacy_sqlite_exports():
    """Reload against missing result codes, then restore the shared module."""
    saved = {name: getattr(sqlite3, name) for name in LEGACY_MISSING_EXPORTS if hasattr(sqlite3, name)}
    try:
        for name in saved:
            delattr(sqlite3, name)
        importlib.reload(source_update)
        yield source_update.SourceUpdateBackup
    finally:
        for name, value in saved.items():
            setattr(sqlite3, name, value)
        importlib.reload(source_update)


class LegacyErrorConnection:
    """Preserve real SQLite behavior while omitting newer exception metadata."""
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, *args, **kwargs):
        try:
            return self.connection.execute(*args, **kwargs)
        except sqlite3.Error as error:
            for name in ("sqlite_errorcode", "sqlite_errorname"):
                if hasattr(error, name):
                    delattr(error, name)
            raise


class SQLiteBackupCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sqlite-backup-compat-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / "output"
        self.output.mkdir()
        self.database = self.output / "structural-index.sqlite"

    def create_database(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE records (identifier INTEGER PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO records(value) VALUES ('original record')")
            connection.commit()

    def assert_original_database(self, path):
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM records").fetchall(), [("original record",)])
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def create_workbench(self):
        self.source = self.root / "source"
        self.source.mkdir()
        self.member = self.source / "entry.cbl"
        self.write_program("REQUEST-FLOW")
        self.state_path = self.root / "workspace.json"

        def analyzer(source, output, **options):
            return analyze_source(source, output, quiet=True, **options)

        return web_app.WorkbenchState(analyzer=analyzer, state_path=self.state_path)

    def write_program(self, name):
        self.member.write_text("IDENTIFICATION DIVISION.\n" + f"PROGRAM-ID. {name}.\n"
                               "PROCEDURE DIVISION.\nGOBACK.\n", encoding="utf-8")

    def import_sources(self, app):
        original_thread = threading.Thread
        workers = []

        def thread(*args, **kwargs):
            worker = original_thread(*args, **kwargs)
            workers.append(worker)
            return worker

        with mock.patch.object(web_app.threading, "Thread", side_effect=thread):
            started = app.start({"source": str(self.source), "output": str(self.output),
                                 "encoding": "utf-8", "source_format": "free", "allow_network": False})
            for worker in workers:
                worker.join(5)
                self.assertFalse(worker.is_alive(), "A local import must finish its worker and backup cleanup.")
        result = app.get_job(started["job_id"])
        self.assertNotEqual(result["status"], "RUNNING")
        return result

    def test_real_wal_backup_reports_progress_without_legacy_missing_constants(self):
        events = []
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("CREATE TABLE records (identifier INTEGER PRIMARY KEY, value TEXT)")
            writer.execute("INSERT INTO records(value) VALUES ('original record')")
            writer.execute("CREATE TABLE payloads (value BLOB)")
            writer.executemany("INSERT INTO payloads(value) VALUES (zeroblob(32768))", [()] * 80)
            writer.commit()
            self.assertTrue(Path(str(self.database) + "-wal").is_file())
            with legacy_sqlite_exports() as backup_type:
                self.assertFalse(hasattr(sqlite3, "SQLITE_BUSY"))
                self.assertFalse(hasattr(sqlite3, "SQLITE_LOCKED"))
                self.assertTrue(hasattr(sqlite3, "SQLITE_DONE"))
                backup = backup_type(self.output, [self.database.name], progress=events.append)
                try:
                    self.assert_original_database(backup.root / self.database.name)
                    self.assertTrue(any(0 < event.get("file_completed", 0) < event.get("file_total", 0)
                                        for event in events))
                    self.assertEqual(events[-1]["bytes_completed"], events[-1]["bytes_total"])
                    backup_root = backup.root
                finally:
                    backup.close()
                self.assertFalse(backup_root.exists())

    def test_reimport_reuses_output_and_finishes_with_real_backup_on_legacy_exports(self):
        app = self.create_workbench()
        with legacy_sqlite_exports() as backup_type, mock.patch.object(web_app, "SourceUpdateBackup", backup_type):
            first = self.import_sources(app)
            self.assertEqual(first["status"], "COMPLETED", first["error"])
            first_snapshot = first["result"]["snapshot_id"]
            self.write_program("REVIEW-FLOW")
            backups = []

            def backup(*args, **kwargs):
                instance = backup_type(*args, **kwargs)
                backups.append(instance)
                return instance

            with mock.patch.object(web_app, "SourceUpdateBackup", side_effect=backup):
                second = self.import_sources(app)
            self.assertEqual(second["status"], "COMPLETED", second["error"])
            self.assertNotEqual(second["result"]["snapshot_id"], first_snapshot)
            self.assertEqual([item["program_name"] for item in second["result"]["programs"]], ["REVIEW-FLOW"])
            self.assertEqual(len(backups), 1)
            self.assertIn("structural-index.sqlite", backups[0].existing)
            self.assertFalse(backups[0].root.exists())
            self.assertFalse(app.project.get("restore_blocked", False))

    def test_legacy_sqlite_messages_produce_safe_typed_diagnostics(self):
        cases = (
            ("database or disk is full", "storage_full"),
            ("attempt to write a readonly database", "storage_permission"),
            ("database is locked", "storage_locked"),
            ("database table is locked", "storage_locked"),
            ("database schema is locked", "storage_locked"),
            ("database disk image is malformed", "storage_corrupt"),
            ("file is not a database", "storage_corrupt"),
        )
        with legacy_sqlite_exports():
            for message, category in cases:
                with self.subTest(message=message):
                    error = sqlite3.OperationalError(message)
                    self.assertFalse(hasattr(error, "sqlite_errorcode"))
                    diagnostic = build_local_diagnostic(error, "REQUEST_FAILED", http_status=500)
                    self.assertEqual(diagnostic["category"], category)
                    self.assertEqual(diagnostic["evidence_source"], "local")
                    self.assertEqual(diagnostic["http_status"], 500)
                    self.assertEqual(sanitize_diagnostic(diagnostic), diagnostic)

    def test_unrecognized_legacy_sqlite_messages_do_not_classify_or_leak_text(self):
        errors = (sqlite3.OperationalError("database is locked private-context-marker"),
                  sqlite3.OperationalError("file is not a database", "private-context-marker"),
                  RuntimeError("database or disk is full"))
        with legacy_sqlite_exports():
            for error in errors:
                with self.subTest(error_type=type(error).__name__, args=error.args):
                    diagnostic = build_local_diagnostic(error, "REQUEST_FAILED")
                    self.assertEqual(diagnostic["category"], "system_error")
                    self.assertNotIn("private-context-marker", json.dumps(diagnostic))

    def test_numeric_extended_result_code_does_not_require_exported_constants(self):
        with legacy_sqlite_exports():
            for primary, category in ((5, "storage_locked"), (6, "storage_locked"), (13, "storage_full"),
                                      (8, "storage_permission"), (11, "storage_corrupt"), (26, "storage_corrupt")):
                with self.subTest(primary=primary):
                    error = sqlite3.OperationalError("private-context-marker")
                    error.sqlite_errorcode = (3 << 8) | primary
                    diagnostic = build_local_diagnostic(error, "REQUEST_FAILED")
                    self.assertEqual(diagnostic["category"], category)
                    self.assertNotIn("private-context-marker", json.dumps(diagnostic))

    def test_real_locked_database_without_new_exception_metadata_has_bounded_wait(self):
        self.create_database()
        original_connect = sqlite3.connect

        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            return (LegacyErrorConnection(connection)
                    if args and isinstance(args[0], str) and "?mode=ro" in args[0] else connection)

        with closing(original_connect(self.database)) as writer:
            writer.execute("BEGIN EXCLUSIVE")
            started = time.monotonic()
            with legacy_sqlite_exports() as backup_type, mock.patch.object(sqlite3, "connect", side_effect=connect):
                with self.assertRaisesRegex(ValueError, "SOURCE_UPDATE_BACKUP_BUSY") as caught:
                    backup_type(self.output, [self.database.name], busy_timeout_seconds=0.05)
            self.assertLess(time.monotonic() - started, 2)
            writer.rollback()
        self.assertEqual(build_local_diagnostic(caught.exception, "REQUEST_FAILED")["category"], "storage_locked")
        self.assertEqual(set(self.root.iterdir()), {self.output})
        self.assert_original_database(self.database)

    def test_preserved_backup_survives_close_and_keeps_original_database(self):
        self.create_database()
        report = self.output / "diagnosis.json"
        report.write_text('{"state":"original"}', encoding="utf-8")
        with legacy_sqlite_exports() as backup_type:
            backup = backup_type(self.output, [self.database.name, report.name])
            recovery = Path(backup.preserve())
            self.assertTrue(backup.keep_for_recovery)
            with closing(sqlite3.connect(self.database)) as writer:
                writer.execute("UPDATE records SET value='updated record'")
                writer.commit()
            report.write_text('{"state":"updated"}', encoding="utf-8")
            backup.close()
            backup.close()
            self.assertTrue(recovery.is_dir())
            self.assert_original_database(recovery / self.database.name)
            self.assertEqual((recovery / report.name).read_text(encoding="utf-8"), '{"state":"original"}')
            backup.restore()
            self.assert_original_database(self.database)
            self.assertEqual(report.read_text(encoding="utf-8"), '{"state":"original"}')

    def test_failed_import_and_rollback_keep_recovery_backup_and_block_restart_authority(self):
        app = self.create_workbench()
        with legacy_sqlite_exports() as backup_type, mock.patch.object(web_app, "SourceUpdateBackup", backup_type):
            initial = self.import_sources(app)
            self.assertEqual(initial["status"], "COMPLETED", initial["error"])
            snapshot = initial["result"]["snapshot_id"]
            self.member.unlink()
            with mock.patch.object(backup_type, "restore", side_effect=PermissionError(errno.EACCES, "private-context-marker")):
                failed = self.import_sources(app)
            self.assertEqual(failed["status"], "FAILED")
            self.assertIsNone(failed["result"])
            self.assertEqual(failed["error"]["code"], "SOURCE_UPDATE_ROLLBACK_FAILED")
            self.assertEqual(failed["error"]["diagnostic"]["category"], "storage_permission")
            self.assertNotIn("private-context-marker", json.dumps(failed))
            recovery = Path(failed["error"]["recovery_directory"])
            self.assertTrue(recovery.is_dir())
            with closing(sqlite3.connect(f"{(recovery / self.database.name).as_uri()}?mode=ro", uri=True)) as database:
                self.assertEqual(database.execute("SELECT value FROM metadata WHERE key='snapshot_id'").fetchone()[0], snapshot)
                self.assertEqual(database.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.assertTrue(saved["restore_blocked"])
            with mock.patch.object(web_app, "_relationships", side_effect=AssertionError("A blocked restart must not authorize graph facts.")):
                restarted = web_app.WorkbenchState(state_path=self.state_path)
            self.assertTrue(restarted.project["restore_blocked"])
            self.assertIsNone(restarted.project["snapshot_id"])
            self.assertEqual(restarted.project["programs"], [])
            self.assertTrue(recovery.is_dir())

    @unittest.skipUnless(os.name == "posix" and getattr(os, "geteuid", lambda: 0)() != 0,
                         "Real directory permission failures require a non-root POSIX user.")
    def test_real_permission_failure_keeps_marker_when_workspace_block_cannot_be_saved(self):
        app = self.create_workbench()
        initial = self.import_sources(app)
        self.assertEqual(initial["status"], "COMPLETED", initial["error"])
        old_snapshot = initial["result"]["snapshot_id"]
        root_mode, output_mode = self.root.stat().st_mode & 0o777, self.output.stat().st_mode & 0o777
        self.write_program("REVIEW-FLOW")

        def analyzer(source, output, **options):
            report = analyze_source(source, output, quiet=True, **options)
            # The new index and reports are complete, but version recording,
            # rollback and workspace persistence now encounter real failures.
            output.chmod(0o555)
            self.root.chmod(0o555)
            return report

        app.analyzer = analyzer
        try:
            failed = self.import_sources(app)
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        finally:
            self.root.chmod(root_mode)
            self.output.chmod(output_mode)
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error"]["code"], "SOURCE_UPDATE_ROLLBACK_FAILED")
        self.assertTrue(app.project["restore_blocked"])
        self.assertFalse(saved["restore_blocked"], "The read-only workspace could not persist its block flag.")
        marker = self.output / web_app.SOURCE_UPDATE_PENDING
        self.assertTrue(marker.is_file())
        recovery = Path(failed["error"]["recovery_directory"])
        self.assertTrue(recovery.is_dir())
        with closing(sqlite3.connect(f"{self.database.as_uri()}?mode=ro", uri=True)) as database:
            current = database.execute("SELECT value FROM metadata WHERE key='snapshot_id'").fetchone()[0]
            self.assertNotEqual(current, old_snapshot)
        with closing(sqlite3.connect(f"{(recovery / self.database.name).as_uri()}?mode=ro", uri=True)) as database:
            original = database.execute("SELECT value FROM metadata WHERE key='snapshot_id'").fetchone()[0]
            self.assertEqual(original, old_snapshot)
        restarted = web_app.WorkbenchState(state_path=self.state_path)
        self.assertTrue(restarted.project["restore_blocked"])
        self.assertIsNone(restarted.project["snapshot_id"])
        self.assertEqual(restarted.project["programs"], [])

    def test_successful_updates_create_marker_before_analysis_and_remove_it_after_publication(self):
        app = self.create_workbench()
        marker = self.output / web_app.SOURCE_UPDATE_PENDING
        observed = []

        def analyzer(source, output, **options):
            observed.append(json.loads(marker.read_text(encoding="utf-8")))
            return analyze_source(source, output, quiet=True, **options)

        app.analyzer = analyzer
        initial = self.import_sources(app)
        self.assertEqual(initial["status"], "COMPLETED", initial["error"])
        self.assertFalse(marker.exists())
        self.write_program("REVIEW-FLOW")
        backup_type, backups = web_app.SourceUpdateBackup, []

        def backup(*args, **kwargs):
            instance = backup_type(*args, **kwargs)
            backups.append(instance)
            return instance

        with mock.patch.object(web_app, "SourceUpdateBackup", side_effect=backup):
            updated = self.import_sources(app)
        self.assertEqual(updated["status"], "COMPLETED", updated["error"])
        self.assertEqual(len(observed), 2)
        self.assertEqual(len(backups), 1)
        self.assertNotIn(web_app.SOURCE_UPDATE_PENDING, backups[0].names)
        self.assertFalse(marker.exists())
        self.assertFalse(json.loads(self.state_path.read_text(encoding="utf-8"))["restore_blocked"])
        restarted = web_app.WorkbenchState(state_path=self.state_path)
        self.assertEqual(restarted.project["snapshot_id"], updated["result"]["snapshot_id"])

    def test_complete_rollback_keeps_marker_until_reports_and_workspace_are_restored(self):
        app = self.create_workbench()
        initial = self.import_sources(app)
        self.assertEqual(initial["status"], "COMPLETED", initial["error"])
        old_snapshot = initial["result"]["snapshot_id"]
        marker = self.output / web_app.SOURCE_UPDATE_PENDING
        self.member.unlink()
        backup_type, restore = web_app.SourceUpdateBackup, web_app.SourceUpdateBackup.restore
        observed = []

        def restoring(backup):
            observed.append(marker.exists())
            restore(backup)
            observed.append(marker.exists())

        with mock.patch.object(backup_type, "restore", restoring):
            failed = self.import_sources(app)
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error"]["code"], "SOURCE_UPDATE_FAILED")
        self.assertEqual(observed, [True, True], "Artifact rollback must not remove the lifecycle marker early.")
        self.assertFalse(marker.exists())
        self.assertEqual(app.project["snapshot_id"], old_snapshot)
        restarted = web_app.WorkbenchState(state_path=self.state_path)
        self.assertEqual(restarted.project["snapshot_id"], old_snapshot)

    def test_existing_unfinished_update_is_kept_after_failure_and_cleared_only_by_success(self):
        app = self.create_workbench()
        initial = self.import_sources(app)
        self.assertEqual(initial["status"], "COMPLETED", initial["error"])
        marker = self.output / web_app.SOURCE_UPDATE_PENDING
        marker.write_text('{"status":"pending"}', encoding="utf-8")
        blocked = web_app.WorkbenchState(state_path=self.state_path, analyzer=app.analyzer)
        self.assertTrue(blocked.project["restore_blocked"])
        self.member.unlink()
        failed = self.import_sources(blocked)
        self.assertEqual(failed["status"], "FAILED")
        self.assertTrue(marker.is_file())
        self.assertTrue(blocked.project["restore_blocked"])
        self.assertIsNone(blocked.project["snapshot_id"])
        self.write_program("RECOVERY-FLOW")
        recovered = self.import_sources(blocked)
        self.assertEqual(recovered["status"], "COMPLETED", recovered["error"])
        self.assertFalse(marker.exists())
        self.assertFalse(blocked.project.get("restore_blocked", False))
        restarted = web_app.WorkbenchState(state_path=self.state_path)
        self.assertEqual(restarted.project["snapshot_id"], recovered["result"]["snapshot_id"])

    def test_marker_write_failure_prevents_analysis_and_keeps_old_index(self):
        app = self.create_workbench()
        initial = self.import_sources(app)
        self.assertEqual(initial["status"], "COMPLETED", initial["error"])
        old_snapshot = initial["result"]["snapshot_id"]
        marker = self.output / web_app.SOURCE_UPDATE_PENDING
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path == marker and (args[0] if args else kwargs.get("mode", "r")) == "x":
                raise PermissionError(errno.EACCES, "marker write unavailable")
            return original_open(path, *args, **kwargs)

        app.analyzer = mock.Mock(side_effect=AssertionError("An unrecorded update cannot begin analysis."))
        with mock.patch.object(Path, "open", guarded_open):
            failed = self.import_sources(app)
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error"]["diagnostic"]["category"], "storage_permission")
        app.analyzer.assert_not_called()
        self.assertFalse(marker.exists())
        self.assertEqual(app.project["snapshot_id"], old_snapshot)

    def test_switching_workspaces_cannot_bypass_the_target_output_marker(self):
        app = self.create_workbench()
        initial = self.import_sources(app)
        self.assertEqual(initial["status"], "COMPLETED", initial["error"])
        marker = self.output / web_app.SOURCE_UPDATE_PENDING
        marker.write_text('{"status":"pending"}', encoding="utf-8")
        unrelated = web_app.WorkbenchState(analyzer=mock.Mock())
        self.assertNotEqual(unrelated.project["output"], str(self.output))
        with self.assertRaises(web_app.RequestError) as caught:
            unrelated.start({"source": str(self.source), "output": str(self.output),
                             "question": "Explain the request process.", "allow_network": False})
        self.assertEqual(caught.exception.code, "SOURCE_UPDATE_ROLLBACK_FAILED")
        self.assertEqual(caught.exception.status, 409)
        unrelated.analyzer.assert_not_called()
        self.assertIsNone(unrelated.job)


if __name__ == "__main__":
    unittest.main()
