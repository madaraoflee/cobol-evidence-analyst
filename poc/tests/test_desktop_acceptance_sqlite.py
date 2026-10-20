"""Acceptance observations close SQLite handles on success and exceptions."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.desktop_acceptance_runtime import read_journal
from tools.desktop_acceptance_scenarios import _dense, _index_stamp, _old_index_guard


REAL_CONNECT = sqlite3.connect


class TrackedConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.events = []
        self.fail_at = None
        self.closed = False

    def execute(self, sql, *args, **kwargs):
        self.events.append(sql)
        if self.fail_at and self.fail_at in sql:
            raise sqlite3.OperationalError("injected SQL failure")
        return super().execute(sql, *args, **kwargs)

    def commit(self):
        self.events.append("commit")
        if self.fail_at == "commit":
            raise sqlite3.OperationalError("injected commit failure")
        return super().commit()

    def __exit__(self, *args):
        self.events.append("transaction_exit")
        return super().__exit__(*args)

    def close(self):
        self.events.append("close")
        self.closed = True
        return super().close()


class DesktopAcceptanceSQLiteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.connections = []  # Keep strong references: garbage collection cannot hide a leak.

    def connect(self, *args, **kwargs):
        connection = REAL_CONNECT(*args, factory=TrackedConnection, **kwargs)
        self.connections.append(connection)
        connection.fail_at = self.fail_at
        return connection

    def assert_closed(self):
        self.assertTrue(self.connections)
        for connection in self.connections:
            self.assertTrue(connection.closed)
            self.assertEqual(connection.events.count("close"), 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def structural_index(self):
        path = self.root / "structural-index.sqlite"
        with closing(REAL_CONNECT(path)) as db, db:
            db.executescript("CREATE TABLE metadata(key TEXT,value TEXT);"
                "INSERT INTO metadata VALUES('parser_version','current');"
                "CREATE TABLE source_files(relative_path TEXT,sha256 TEXT,indexed_at_utc TEXT);"
                "INSERT INTO source_files VALUES('entry.cbl','version','time');")
        return path

    def test_index_stamp_closes_successful_and_failed_read_connections(self):
        self.structural_index()
        for failure in (None, "FROM source_files"):
            self.fail_at = failure
            with self.subTest(failure=failure), mock.patch("sqlite3.connect", side_effect=self.connect):
                if failure:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "injected"):
                        _index_stamp(self.root)
                else:
                    result = _index_stamp(self.root)
                    self.assertEqual(result["metadata"], [("parser_version", "current")])
                    self.assertEqual(result["sources"][0][0], "entry.cbl")
                self.assert_closed()

    def test_read_journal_closes_successful_and_failed_read_connections(self):
        with closing(REAL_CONNECT(self.root / "investigation-runs.sqlite")) as db, db:
            db.executescript("CREATE TABLE investigation_runs(run_id TEXT,state TEXT);"
                "INSERT INTO investigation_runs VALUES('run-one','running');"
                "CREATE TABLE investigation_requests(run_id TEXT,request_id TEXT,state TEXT,request_hash TEXT,response_json TEXT);")
        for failure in (None, "FROM investigation_requests"):
            self.fail_at = failure
            with self.subTest(failure=failure), mock.patch("sqlite3.connect", side_effect=self.connect):
                if failure:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "injected"):
                        read_journal(self.root)
                else:
                    self.assertEqual(read_journal(self.root)["runs"], [{"run_id": "run-one", "state": "running"}])
                self.assert_closed()

    def test_old_index_guard_preserves_transaction_order_and_closes_each_failure(self):
        path = self.structural_index()
        for failure in (None, "UPDATE metadata", "commit", "PRAGMA wal_checkpoint"):
            with closing(REAL_CONNECT(path)) as db, db:
                db.execute("UPDATE metadata SET value='current' WHERE key='parser_version'")
            self.fail_at = failure

            def analyze(*args, **kwargs):
                self.assertTrue(self.connections[-1].closed, "the database must close before workbench reentry")
                return {"job": {"status": "COMPLETED"}, "result": {"reason_code": "SOURCE_INDEX_UPDATE_REQUIRED"}}

            workbench = SimpleNamespace(analyze=mock.Mock(side_effect=analyze))
            with self.subTest(failure=failure), mock.patch("sqlite3.connect", side_effect=self.connect):
                if failure:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "injected"):
                        _old_index_guard(workbench, self.root, self.root, SimpleNamespace(requests=[]))
                    workbench.analyze.assert_not_called()
                else:
                    self.assertTrue(_old_index_guard(workbench, self.root, self.root, SimpleNamespace(requests=[])))
                    events = self.connections[-1].events
                    self.assertEqual(events[1:5], ["commit", "PRAGMA wal_checkpoint(TRUNCATE)", "transaction_exit", "close"])
                self.assert_closed()
            with closing(REAL_CONNECT(path)) as db:
                actual = db.execute("SELECT value FROM metadata WHERE key='parser_version'").fetchone()[0]
            self.assertEqual(actual, "current" if failure in {"UPDATE metadata", "commit"} else "business-sparse-previous")

    def test_dense_scope_read_closes_after_query_and_json_decode_failures(self):
        source = self.root / "source"
        source.mkdir()
        text = "IDENTIFICATION DIVISION.\nPROGRAM-ID. SAMPLE-RULE.\nPROCEDURE DIVISION.\nGOBACK.\n"
        (source / "calculate.cbl").write_text(text)
        scopes = self.root / "workspace/semantic-scopes"
        scopes.mkdir(parents=True)
        path = scopes / "scope.sqlite"
        ir = {"data_items": [{}] + [{"picture": "9"}] * 2000,
              "statements": [{"statement_type": "COMPUTE"}, {"statement_type": "MOVE"}]}
        with closing(REAL_CONNECT(path)) as db, db:
            db.execute("CREATE TABLE semantic_ir_documents(ir_json TEXT)")
            db.execute("INSERT INTO semantic_ir_documents VALUES(?)", (json.dumps(ir),))
        provider = mock.MagicMock()
        provider.__enter__.return_value = provider
        provider.requests = [{"source_pages": [{"source_text": text}]}]
        workbench = mock.MagicMock()
        workbench.__enter__.return_value = workbench
        workbench.analyze.return_value = {"result": {"agent_result": {"metrics": {"repository_rebuilt": False}}}}
        for failure in (None, "SELECT ir_json", "invalid_json"):
            self.fail_at = failure
            with closing(REAL_CONNECT(path)) as db, db:
                db.execute("UPDATE semantic_ir_documents SET ir_json=?", ("{" if failure == "invalid_json" else json.dumps(ir),))
            with self.subTest(failure=failure), mock.patch("sqlite3.connect", side_effect=self.connect), \
                    mock.patch("tools.desktop_acceptance_scenarios._source", return_value=source), \
                    mock.patch("tools.desktop_acceptance_scenarios.LoopbackProvider", return_value=provider), \
                    mock.patch("tools.desktop_acceptance_scenarios.NativeWorkbench", return_value=workbench), \
                    mock.patch("tools.desktop_acceptance_scenarios.write_profile", return_value={}), \
                    mock.patch("tools.desktop_acceptance_scenarios._index", return_value=0), \
                    mock.patch("tools.desktop_acceptance_scenarios._formula_checks", return_value={}), \
                    mock.patch("tools.desktop_acceptance_scenarios._row", side_effect=lambda *args, **kwargs: {"checks": kwargs["checks"]}):
                if failure:
                    with self.assertRaises(json.JSONDecodeError if failure == "invalid_json" else sqlite3.OperationalError):
                        _dense([], self.root, {"id": "dense_record", "repeats": 1})
                else:
                    rows = _dense([], self.root, {"id": "dense_record", "repeats": 1})
                    self.assertTrue(rows[0]["checks"]["completeness_dense_ir_fields"])
                self.assert_closed()


if __name__ == "__main__":
    unittest.main()
