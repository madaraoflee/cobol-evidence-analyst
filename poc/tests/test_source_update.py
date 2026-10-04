"""A source update must reject artifact paths it cannot safely restore."""

from pathlib import Path
from contextlib import closing
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from source_update import SourceUpdateBackup


class SourceUpdateBackupTests(unittest.TestCase):
    def create_database(self, path, *, page_size=4096, rows=160):
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(f"PRAGMA page_size = {page_size}")
            connection.execute("CREATE TABLE records (identifier INTEGER PRIMARY KEY, payload BLOB)")
            connection.executemany("INSERT INTO records(payload) VALUES (zeroblob(32768))",
                                   [()] * rows)
            connection.commit()

    def assert_database_records(self, path, expected):
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM records").fetchone()[0], expected)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_backup_rejects_file_and_directory_symlinks_before_updating(self):
        with tempfile.TemporaryDirectory(prefix="source-update-") as folder:
            root = Path(folder)
            output = root / "output"
            output.mkdir()
            for target_is_directory in (False, True):
                with self.subTest(target_is_directory=target_is_directory):
                    target = root / ("directory-target" if target_is_directory else "file-target")
                    if target_is_directory:
                        target.mkdir()
                    else:
                        target.write_text("owned elsewhere")
                    artifact = output / "source-versions.sqlite"
                    artifact.symlink_to(target, target_is_directory=target_is_directory)
                    try:
                        with self.assertRaisesRegex(ValueError, "SOURCE_UPDATE_PATH_INVALID"):
                            backup = SourceUpdateBackup(output, [artifact.name])
                            backup.close()
                        self.assertTrue(artifact.is_symlink())
                        self.assertTrue(target.exists())
                    finally:
                        artifact.unlink()

    def test_database_and_file_backups_report_partial_byte_progress_on_same_filesystem(self):
        with tempfile.TemporaryDirectory(prefix="source-update-") as folder:
            root = Path(folder)
            output = root / "output"
            output.mkdir()
            database = output / "structural-index.sqlite"
            self.create_database(database, page_size=1024)
            report = output / "diagnosis.json"
            contents = b"neutral business data\n" * 160000
            report.write_bytes(contents)
            events = []
            backup = SourceUpdateBackup(output, [database.name, report.name], progress=events.append)
            try:
                self.assertEqual(backup.root.parent, output.parent)
                self.assertEqual(backup.root.stat().st_dev, output.stat().st_dev)
                self.assertEqual(set(output.iterdir()), {database, report})
                self.assert_database_records(backup.root / database.name, 160)
                self.assertEqual((backup.root / report.name).read_bytes(), contents)
                for name in (database.name, report.name):
                    partial = [event for event in events if event["current_file"] == name
                               and 0 < event["file_completed"] < event["file_total"]]
                    self.assertTrue(partial, name)
                expected_bytes = database.stat().st_size + report.stat().st_size
                self.assertEqual(events[-1]["completed"], expected_bytes)
                self.assertEqual(events[-1]["total"], expected_bytes)
                self.assertEqual(events[-1]["bytes_completed"], expected_bytes)
                self.assertTrue(all(event["phase"] == "backing_up" and event["unit"] == "bytes"
                                    for event in events))
            finally:
                temporary = backup.root
                backup.close()
            self.assertFalse(temporary.exists())

    def test_cancelled_database_backup_removes_partial_backup_without_mutating_original(self):
        with tempfile.TemporaryDirectory(prefix="source-update-") as folder:
            root = Path(folder)
            output = root / "output"
            output.mkdir()
            database = output / "structural-index.sqlite"
            self.create_database(database)
            events = []
            cancelled = False

            def progress(event):
                nonlocal cancelled
                events.append(event)
                if 0 < event.get("file_completed", 0) < event.get("file_total", 0):
                    cancelled = True

            def check_cancel():
                if cancelled:
                    raise RuntimeError("stop requested")

            with self.assertRaisesRegex(RuntimeError, "stop requested"):
                SourceUpdateBackup(output, [database.name], progress=progress, check_cancel=check_cancel)
            self.assertTrue(cancelled)
            self.assertLess(events[-1]["completed"], events[-1]["total"])
            self.assertEqual(set(root.iterdir()), {output})
            self.assertEqual(set(output.iterdir()), {database})
            self.assert_database_records(database, 160)

    def test_cancelled_file_copy_removes_partial_backup_without_mutating_original(self):
        with tempfile.TemporaryDirectory(prefix="source-update-") as folder:
            root = Path(folder)
            output = root / "output"
            output.mkdir()
            report = output / "diagnosis.json"
            contents = b"original data\n" * 240000
            report.write_bytes(contents)
            cancelled = False

            def progress(event):
                nonlocal cancelled
                if 0 < event.get("file_completed", 0) < event.get("file_total", 0):
                    cancelled = True

            def check_cancel():
                if cancelled:
                    raise RuntimeError("stop requested")

            with self.assertRaisesRegex(RuntimeError, "stop requested"):
                SourceUpdateBackup(output, [report.name], progress=progress, check_cancel=check_cancel)
            self.assertTrue(cancelled)
            self.assertEqual(set(root.iterdir()), {output})
            self.assertEqual(report.read_bytes(), contents)

    def test_exclusive_database_lock_has_bounded_wait_and_preserves_original(self):
        with tempfile.TemporaryDirectory(prefix="source-update-") as folder:
            root = Path(folder)
            output = root / "output"
            output.mkdir()
            database = output / "structural-index.sqlite"
            self.create_database(database, rows=1)
            events = []
            with closing(sqlite3.connect(database)) as writer:
                writer.execute("BEGIN EXCLUSIVE")
                started = time.monotonic()
                with self.assertRaisesRegex(ValueError, "SOURCE_UPDATE_BACKUP_BUSY"):
                    SourceUpdateBackup(output, [database.name], progress=events.append,
                                       busy_timeout_seconds=0.1)
                self.assertLess(time.monotonic() - started, 2)
                writer.rollback()
            self.assertTrue(events)
            self.assertEqual(set(root.iterdir()), {output})
            self.assert_database_records(database, 1)

    def test_waiting_for_database_lock_can_be_cancelled_before_lock_deadline(self):
        with tempfile.TemporaryDirectory(prefix="source-update-") as folder:
            root = Path(folder)
            output = root / "output"
            output.mkdir()
            database = output / "structural-index.sqlite"
            self.create_database(database, rows=1)
            checks = 0

            def check_cancel():
                nonlocal checks
                checks += 1
                if checks >= 6:
                    raise RuntimeError("stop requested")

            with closing(sqlite3.connect(database)) as writer:
                writer.execute("BEGIN EXCLUSIVE")
                started = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, "stop requested"):
                    SourceUpdateBackup(output, [database.name], check_cancel=check_cancel,
                                       busy_timeout_seconds=5)
                self.assertLess(time.monotonic() - started, 2)
                writer.rollback()
            self.assertEqual(set(root.iterdir()), {output})
            self.assert_database_records(database, 1)

    def test_wal_backup_restores_committed_rows_and_repeats_despite_cancel_flag(self):
        with tempfile.TemporaryDirectory(prefix="source-update-") as folder:
            output = Path(folder) / "output"
            output.mkdir()
            database = output / "structural-index.sqlite"
            report = output / "diagnosis.json"
            cancelled = False

            def check_cancel():
                if cancelled:
                    raise RuntimeError("stop requested")

            with closing(sqlite3.connect(database)) as writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE records (identifier INTEGER PRIMARY KEY, payload BLOB)")
                writer.execute("INSERT INTO records(payload) VALUES (zeroblob(32768))")
                writer.commit()
                self.assertTrue(Path(str(database) + "-wal").is_file())
                backup = SourceUpdateBackup(output, [database.name, report.name], check_cancel=check_cancel)
            try:
                cancelled = True
                for _ in range(2):
                    with closing(sqlite3.connect(database)) as writer:
                        writer.execute("INSERT INTO records(payload) VALUES (zeroblob(32768))")
                        writer.commit()
                    report.write_text("new result")
                    backup.restore()
                    self.assert_database_records(database, 1)
                    self.assertFalse(report.exists())
            finally:
                backup.close()

    def test_unwritable_output_parent_falls_back_without_hiding_other_errors(self):
        with tempfile.TemporaryDirectory(prefix="source-update-") as folder:
            output = Path(folder) / "output"
            output.mkdir()
            report = output / "diagnosis.json"
            report.write_text("original data")
            create_temporary = tempfile.mkdtemp
            calls = []

            def fallback_temporary(**options):
                calls.append(options)
                if "dir" in options:
                    raise PermissionError("parent is read only")
                return create_temporary(**options)

            with patch("source_update.tempfile.mkdtemp", side_effect=fallback_temporary):
                backup = SourceUpdateBackup(output, [report.name])
            try:
                self.assertEqual((backup.root / report.name).read_text(), "original data")
                self.assertEqual(calls[0]["dir"], output.parent)
                self.assertNotIn("dir", calls[1])
            finally:
                backup.close()
            with patch("source_update.tempfile.mkdtemp", side_effect=OSError("storage full")):
                with self.assertRaisesRegex(OSError, "storage full"):
                    SourceUpdateBackup(output, [report.name])
            self.assertEqual(report.read_text(), "original data")

    def test_invalid_database_error_is_propagated_and_cleans_temporary_backup(self):
        with tempfile.TemporaryDirectory(prefix="source-update-") as folder:
            root = Path(folder)
            output = root / "output"
            output.mkdir()
            database = output / "structural-index.sqlite"
            database.write_bytes(b"invalid database contents")
            with self.assertRaises(sqlite3.DatabaseError):
                SourceUpdateBackup(output, [database.name])
            self.assertEqual(database.read_bytes(), b"invalid database contents")
            self.assertEqual(set(root.iterdir()), {output})

    def test_failed_restore_copy_does_not_replace_existing_artifact_with_partial_data(self):
        with tempfile.TemporaryDirectory(prefix="source-update-") as folder:
            output = Path(folder) / "output"
            output.mkdir()
            report = output / "diagnosis.json"
            report.write_text("original result")
            backup = SourceUpdateBackup(output, [report.name])
            try:
                report.write_text("updated result")

                def fail_copy(source, destination):
                    Path(destination).write_text("partial result")
                    raise OSError("storage full")

                with patch("source_update.shutil.copyfile", side_effect=fail_copy):
                    with self.assertRaisesRegex(OSError, "storage full"):
                        backup.restore()
                self.assertEqual(report.read_text(), "updated result")
                self.assertEqual(set(output.iterdir()), {report})
                backup.restore()
                self.assertEqual(report.read_text(), "original result")
            finally:
                backup.close()


if __name__ == "__main__":
    unittest.main()
