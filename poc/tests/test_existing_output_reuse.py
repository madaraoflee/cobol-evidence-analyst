"""An existing library stays reusable without clearing conversations or exports."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyze_source import OUTPUT_DATABASE_NAMES, _paths, analyze_source
from company_api import CompanyAPIConfig
from conversation_store import ConversationStore
from run_store import RunStore
from web_app import RequestError, WorkbenchState, _validate_options


class ExistingOutputReuseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.output = self.root / "output"
        self.source.mkdir()
        (self.source / "orders.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. ORDER-READER.\n"
            "PROCEDURE DIVISION.\nGOBACK.\n", encoding="utf-8")
        self.payload = {"source": str(self.source), "output": str(self.output), "source_format": "free"}

    def build(self):
        report = analyze_source(self.source, self.output, source_format="free", index_mode="full",
                                analysis_mode="business", reading_strategy="retrieval")
        self.assertTrue(report["source_manifest_verified"])
        return report

    def test_existing_database_sidecars_do_not_require_an_empty_output(self):
        self.output.mkdir()
        for database in OUTPUT_DATABASE_NAMES:
            for suffix in ("-wal", "-shm", "-journal"):
                with self.subTest(database=database, suffix=suffix):
                    sidecar = self.output / (database + suffix)
                    sidecar.write_bytes(b"preserved sidecar")
                    self.assertEqual(_validate_options(self.payload)["output"], self.output)
                    self.assertEqual(sidecar.read_bytes(), b"preserved sidecar")
                    sidecar.unlink()

    def test_live_investigation_journal_is_accepted_without_deleting_it(self):
        self.output.mkdir()
        store = RunStore(self.output / "investigation-runs.sqlite")
        with store.connect() as database:
            database.execute("INSERT INTO derivation_cache VALUES ('local','key','{}')")
            journal = self.output / "investigation-runs.sqlite-journal"
            self.assertTrue(journal.is_file())
            original = journal.read_bytes()
            self.assertEqual(_validate_options(self.payload)["output"], self.output)
            self.assertEqual(journal.read_bytes(), original)
        with store.connect() as database:
            self.assertEqual(database.execute("SELECT value_json FROM derivation_cache").fetchone()[0], "{}")

    def test_recognized_library_with_exports_can_refresh_and_keep_history(self):
        self.assert_refresh_preserves_history_and_exports(index_missing=False)

    def test_missing_index_can_be_rebuilt_beside_history_and_exports(self):
        self.assert_refresh_preserves_history_and_exports(index_missing=True)

    def assert_refresh_preserves_history_and_exports(self, *, index_missing):
        report = self.build()
        store = ConversationStore(self.output, self.source)
        conversation = store.create(report["build_report"]["snapshot_id"])
        store.append(conversation["id"], "user", "Explain order selection", status="completed")
        store.append(conversation["id"], "assistant", "Saved business explanation", status="completed")
        before = store.get(conversation["id"])
        if index_missing:
            (self.output / "structural-index.sqlite").unlink()
        exports = self.output / "saved-reports"
        exports.mkdir()
        report_path = exports / "business-notes.txt"
        report_path.write_bytes(b"keep the user's notes exactly\n")
        extra = self.output / "desktop.ini"
        extra.write_bytes(b"folder display preferences")
        extra_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (report_path, extra)}
        state_path = self.root / "workspace.json"
        state_path.write_text(json.dumps({"source": str(self.source), "output": str(self.output),
                                         "conversation_id": conversation["id"]}), encoding="utf-8")
        config = CompanyAPIConfig(base_url="https://gateway.example.invalid/v1", api_key="test", chat_model="test")
        app = WorkbenchState(state_path=state_path, config_provider=lambda: config)
        self.assertEqual(app.state()["conversation"], before)
        if index_missing:
            self.assertIsNone(app.state()["project"]["snapshot_id"])
            self.assertFalse((self.output / "structural-index.sqlite").exists())
        threads = []
        thread_class = threading.Thread

        def worker(*args, **kwargs):
            thread = thread_class(*args, **kwargs)
            threads.append(thread)
            return thread

        with patch("web_app.threading.Thread", side_effect=worker):
            job_id = app.start(self.payload)["job_id"]
        try:
            threads[0].join(timeout=15)
            self.assertFalse(threads[0].is_alive(), "local refresh did not finish")
            self.assertEqual(app.get_job(job_id)["status"], "COMPLETED")
        finally:
            if threads[0].is_alive():
                app.cancel(job_id)
                threads[0].join(timeout=15)
        self.assertEqual(store.get(conversation["id"]), before)
        self.assertEqual(app.state()["conversation"], before)
        for path, digest in extra_hashes.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        restarted = WorkbenchState(state_path=state_path, config_provider=lambda: config)
        self.assertEqual(restarted.state()["conversation"], before)
        self.assertEqual(restarted.state()["project"]["snapshot_id"], app.state()["project"]["snapshot_id"])

    def test_retained_history_requires_matching_source_and_does_not_create_missing_stores(self):
        self.output.mkdir()
        notes = self.output / "notes.txt"
        notes.write_bytes(b"retain notes")
        history_path = self.output / "conversations.sqlite"
        with self.assertRaises(RequestError):
            _validate_options(self.payload)
        self.assertFalse(history_path.exists())
        store = ConversationStore(self.output, self.source)
        empty_hash = hashlib.sha256(history_path.read_bytes()).hexdigest()
        with self.assertRaises(RequestError):
            _validate_options(self.payload)
        self.assertEqual(hashlib.sha256(history_path.read_bytes()).hexdigest(), empty_hash)
        conversation = store.create("old-snapshot")
        store.append(conversation["id"], "user", "Retain this question")
        before = hashlib.sha256(history_path.read_bytes()).hexdigest()
        self.assertEqual(_validate_options(self.payload)["output"], self.output)
        other_source = self.root / "other-source"
        other_source.mkdir()
        with self.assertRaises(RequestError):
            _validate_options({**self.payload, "source": str(other_source)})
        self.assertEqual(hashlib.sha256(history_path.read_bytes()).hexdigest(), before)
        self.assertEqual(notes.read_bytes(), b"retain notes")

    def test_history_does_not_override_a_present_conflicting_or_corrupt_index(self):
        self.build()
        store = ConversationStore(self.output, self.source)
        conversation = store.create("old-snapshot")
        store.append(conversation["id"], "user", "Retain this question")
        before = store.get(conversation["id"])
        (self.output / "notes.txt").write_bytes(b"retain notes")
        database = self.output / "structural-index.sqlite"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("UPDATE metadata SET value=? WHERE key='source_root_hash'", ("0" * 64,))
        with self.assertRaises(RequestError):
            _validate_options(self.payload)
        database.write_bytes(b"invalid index retained")
        with self.assertRaises(RequestError):
            _validate_options(self.payload)
        self.assertEqual(database.read_bytes(), b"invalid index retained")
        self.assertEqual(store.get(conversation["id"]), before)
        self.assertEqual((self.output / "notes.txt").read_bytes(), b"retain notes")

    def test_history_filename_and_partial_schema_do_not_authorize_an_unrelated_folder(self):
        self.output.mkdir()
        notes = self.output / "notes.txt"
        notes.write_bytes(b"retain notes")
        history = self.output / "conversations.sqlite"
        with closing(sqlite3.connect(history)) as connection, connection:
            connection.execute("CREATE TABLE conversations (id TEXT, source_key TEXT)")
            connection.execute("CREATE TABLE messages (content TEXT)")
            connection.execute("INSERT INTO conversations VALUES ('one', ?)",
                               (hashlib.sha256(str(self.source).encode()).hexdigest(),))
        before = history.read_bytes()
        with self.assertRaises(RequestError):
            _validate_options(self.payload)
        self.assertEqual(history.read_bytes(), before)
        self.assertEqual(notes.read_bytes(), b"retain notes")

    def test_mismatched_or_unrecognized_directory_remains_protected(self):
        self.build()
        notes = self.output / "keep.txt"
        notes.write_text("untouched", encoding="utf-8")
        other_source = self.root / "other-source"
        other_source.mkdir()
        with self.assertRaises(RequestError):
            _validate_options({**self.payload, "source": str(other_source)})
        with closing(sqlite3.connect(self.output / "structural-index.sqlite")) as connection, connection:
            connection.execute("UPDATE metadata SET value='unknown' WHERE key='snapshot_id'")
        with self.assertRaises(RequestError):
            _validate_options(self.payload)
        self.assertEqual(notes.read_text(encoding="utf-8"), "untouched")

    def test_unrecognized_directory_is_not_authorized_by_report_name_alone(self):
        self.output.mkdir()
        (self.output / "diagnosis.json").write_text(json.dumps({"source_root": str(self.source)}))
        (self.output / "keep.txt").write_text("untouched", encoding="utf-8")
        with self.assertRaises(RequestError):
            _validate_options(self.payload)
        self.assertEqual((self.output / "keep.txt").read_text(), "untouched")

    def test_existing_index_can_be_repaired_when_display_report_is_missing(self):
        self.build()
        (self.output / "diagnosis.json").unlink()
        (self.output / "saved-notes.txt").write_text("untouched", encoding="utf-8")
        self.assertEqual(_validate_options(self.payload)["output"], self.output)
        self.assertFalse((self.output / "diagnosis.json").exists())
        self.assertEqual((self.output / "saved-notes.txt").read_text(), "untouched")

    def test_all_database_sidecars_reject_redirected_or_directory_paths(self):
        self.output.mkdir()
        target = self.root / "unrelated.txt"
        target.write_bytes(b"do not modify")
        for database in OUTPUT_DATABASE_NAMES:
            for suffix in ("", "-wal", "-shm", "-journal"):
                with self.subTest(database=database, suffix=suffix):
                    path = self.output / (database + suffix)
                    path.mkdir()
                    with self.assertRaises(ValueError):
                        _paths(self.source, self.output)
                    path.rmdir()
                    path.symlink_to(target)
                    try:
                        with self.assertRaises(RequestError):
                            _validate_options(self.payload)
                    finally:
                        path.unlink()
        self.assertEqual(target.read_bytes(), b"do not modify")


if __name__ == "__main__":
    unittest.main()
