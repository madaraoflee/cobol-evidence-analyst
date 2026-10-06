"""Selected semantic work must stay independent of unrelated catalog size."""

from __future__ import annotations

from dataclasses import replace
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_index import build_business_index
from repository_discovery import ensure_repository_search
from semantic_scope import _select_paths, _source_pages, _semantic_connection, build_business_evidence, prepare_semantic_scope
from source_session import QuestionSourceSession
from structural_index import build_structural_index
from procedure_expansion import expand_program


class SemanticScopePerformanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.text = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. BILL-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 INPUT-VALUE PIC 9(8).\n01 RESULT-VALUE PIC 9(8).\n"
            "PROCEDURE DIVISION.\nMAIN.\nIF INPUT-VALUE > 0\n"
            "COMPUTE RESULT-VALUE = INPUT-VALUE * 2\nEND-IF\n"
            "MOVE RESULT-VALUE TO INPUT-VALUE.\nGOBACK.\n")
        (self.source / "bill-rule.cbl").write_text(self.text, encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)

    def test_no_copy_scope_does_not_scan_unrelated_program_catalog(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            template = db.execute("SELECT * FROM source_files LIMIT 1").fetchone()
            db.executemany("INSERT INTO source_files VALUES (?,?,?,?,?,?,?,?)", [
                (f"unrelated-{i}.cbl", *template[1:]) for i in range(12000)])
        statements = []
        connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            db = connect(*args, **kwargs)
            if Path(args[0]).resolve() == self.database.resolve():
                db.set_trace_callback(statements.append)
            return db

        with QuestionSourceSession(self.database, self.source) as session:
            with mock.patch("semantic_scope.sqlite3.connect", side_effect=traced_connect):
                with prepare_semantic_scope(self.database, session,
                        anchors=[{"relative_path": "bill-rule.cbl", "line": 10}], policy=AgentPolicy()) as scope:
                    self.assertEqual([item["relative_path"] for item in scope.input_manifest], ["bill-rule.cbl"])
                    group = build_business_evidence(scope, session,
                        anchor={"relative_path": "bill-rule.cbl", "line": 10}, policy=AgentPolicy())
                    self.assertTrue(group.observations)
            self.assertEqual([item["relative_path"] for item in session.source_manifest()], ["bill-rule.cbl"])
        self.assertNotIn("SELECT relative_path FROM source_files", statements)
        program_queries = [query for query in statements if "FROM code_units u" in query]
        self.assertTrue(program_queries)
        self.assertTrue(all("relative_path IN ('bill-rule.cbl')" in query for query in program_queries))

    def test_repeated_source_observations_decode_one_capture_once(self):
        with QuestionSourceSession(self.database, self.source) as session:
            capture = session.capture("bill-rule.cbl")
            scope = SimpleNamespace(source_map={}, source_session=session, _line_cache={})
            read_text = Path.read_text
            reads = []

            def counted_read(path, *args, **kwargs):
                if path == capture.path:
                    reads.append(path)
                return read_text(path, *args, **kwargs)

            with mock.patch.object(Path, "read_text", new=counted_read):
                pages = [_source_pages(scope, {"relative_path": "bill-rule.cbl",
                    "start_line": line, "end_line": line})[0] for line in range(1, 14)]
                repeat = _source_pages(scope, {"relative_path": "bill-rule.cbl",
                    "start_line": 1, "end_line": 13})[0]
            self.assertEqual(len(reads), 1)
            self.assertEqual(repeat["source_text"], self.text.rstrip("\n"))
            self.assertEqual([page["source_text"] for page in pages], self.text.splitlines())
            self.assertEqual({page["source_sha256"] for page in pages}, {capture.sha256})

    def test_repeated_dependency_edges_select_each_path_once(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO source_files SELECT 'shared.cbl',sha256,encoding,"
                "used_fallback_encoding,format_hint,artifact_kind,line_count,indexed_at_utc "
                "FROM source_files WHERE relative_path='bill-rule.cbl'")
            db.execute("INSERT INTO symbols VALUES ('shared-program','shared.cbl','Program',"
                "'SHARED','SHARED','SHARED','shared-unit','shared-evidence')")
            db.executemany("INSERT INTO relations VALUES (?,?,?,?,?,?,?,?,?,?)", [
                (f"repeat-call-{i}", "bill-rule.cbl", f"call-{i}", "CALLS", "SHARED",
                 "BILL-RULE", "shared-program", "confirmed", f"call-evidence-{i}", "{}")
                for i in range(12000)])
        checks = []
        with mock.patch("semantic_scope._semantic_connection",
                side_effect=lambda path, check: closing(sqlite3.connect(path))):
            paths, frontier = _select_paths(self.database,
                [{"relative_path": "bill-rule.cbl"}], ["bill-rule.cbl"],
                replace(AgentPolicy(), max_semantic_files=1), check_cancel=lambda: checks.append(1))
        self.assertEqual(paths, ["bill-rule.cbl"])
        self.assertEqual(frontier, [{"relative_path": "shared.cbl", "reason": "file_budget"}])
        self.assertEqual(len(checks), 2)

    def test_mapped_anchor_lookup_reuses_reverse_source_index(self):
        with QuestionSourceSession(self.database, self.source) as session:
            with prepare_semantic_scope(self.database, session,
                    anchors=[{"relative_path": "bill-rule.cbl", "line": 10}], policy=AgentPolicy()) as scope:
                scope.source_map = {"bill-rule.cbl": [{"origin": {
                    "relative_path": "bill-rule.cbl", "line": line}, "include_chain": []}
                    for line in range(1, 14)]}
                first = build_business_evidence(scope, session,
                    anchor={"relative_path": "bill-rule.cbl", "line": 10}, policy=AgentPolicy())
                cached = scope._origin_locations
                second = build_business_evidence(scope, session,
                    anchor={"relative_path": "bill-rule.cbl", "line": 12}, policy=AgentPolicy())
                self.assertIs(scope._origin_locations, cached)
                self.assertEqual(cached[("bill-rule.cbl", 10)], [("bill-rule.cbl", 10)])
                self.assertTrue(first.observations)
                self.assertTrue(second.observations)

    def test_long_sql_operation_preserves_budget_exception(self):
        failure = RuntimeError("LOCAL_BUDGET_EXHAUSTED")

        def exhausted():
            raise failure

        with self.assertRaises(RuntimeError) as caught:
            with _semantic_connection(self.database, exhausted) as db:
                db.execute("WITH RECURSIVE sequence(n) AS (VALUES(1) UNION ALL "
                    "SELECT n+1 FROM sequence WHERE n<1000000) SELECT SUM(n) FROM sequence").fetchone()
        self.assertIs(caught.exception, failure)
        with _semantic_connection(self.database, lambda: None) as db:
            self.assertEqual(db.execute("SELECT 1").fetchone()[0], 1)

    def test_sql_errors_keep_their_original_type(self):
        with self.assertRaises(sqlite3.OperationalError):
            with _semantic_connection(self.database, lambda: None) as db:
                db.execute("SELECT * FROM absent_table")

    def test_deep_index_budget_interrupts_sql_write_and_keeps_previous_snapshot(self):
        database = self.root / "structural.sqlite"
        report = build_structural_index(self.source, database, source_format="free", verify_content=True)
        with closing(sqlite3.connect(database)) as db, db:
            before = db.execute("SELECT COUNT(*) FROM code_units").fetchone()[0]
        (self.source / "bill-rule.cbl").write_text(self.text +
            "MOVE INPUT-VALUE TO RESULT-VALUE.\n" * 4000, encoding="utf-8")
        writing = False
        checks = []
        failure = RuntimeError("LOCAL_BUDGET_EXHAUSTED")

        def progress(event):
            nonlocal writing
            if event["phase"] == "writing":
                writing = True

        def exhausted():
            if writing:
                checks.append(1)
                if len(checks) == 4:
                    raise failure

        with self.assertRaises(RuntimeError) as caught:
            build_structural_index(self.source, database, source_format="free", verify_content=True,
                progress=progress, check_cancel=exhausted)
        self.assertIs(caught.exception, failure)
        with closing(sqlite3.connect(database)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM code_units").fetchone()[0], before)
            self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='snapshot_id'").fetchone()[0],
                             report["snapshot_id"])

    def test_copy_lexing_checks_budget_before_expanding_all_lines(self):
        (self.source / "bill-rule.cbl").write_text(self.text +
            "MOVE INPUT-VALUE TO RESULT-VALUE.\n" * 4000, encoding="utf-8")
        failure = RuntimeError("LOCAL_BUDGET_EXHAUSTED")
        checks = []

        def exhausted():
            checks.append(1)
            if len(checks) == 5:
                raise failure

        with QuestionSourceSession(self.database, self.source) as session:
            with self.assertRaises(RuntimeError) as caught:
                expand_program(session.mirror_root, "BILL-RULE", source_provider=session,
                    source_catalog=["bill-rule.cbl"], entry_relative_path="bill-rule.cbl",
                    check_cancel=exhausted)
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(checks), 5)


if __name__ == "__main__":
    unittest.main()
