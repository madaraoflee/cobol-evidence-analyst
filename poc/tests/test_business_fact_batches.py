from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import business_index
from benchmark_business_facts import fingerprint, long_program
from source_catalog import refresh_source_catalog
from structural_index import _statement_kind


class IndexWriteFailure(sqlite3.Connection):
    fail_create = False

    def execute(self, sql, parameters=()):
        if self.fail_create and sql.startswith("CREATE INDEX idx_symbols_lookup"):
            raise sqlite3.OperationalError("database is full")
        if sql == 'DROP INDEX "idx_symbols_lookup"':
            self.fail_create = True
        return super().execute(sql, parameters)


class BusinessFactBatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.database = self.base / "index.sqlite"

    def write(self, name, text):
        (self.source / name).write_text(text, encoding="utf-8")

    def build(self, database=None, **options):
        return business_index.build_business_index(self.source, database or self.database,
            source_format="free", framework_reference_path=self.base / "absent-reference.md", **options)

    def indexes(self, database=None):
        with closing(sqlite3.connect(database or self.database)) as connection:
            return dict(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='index'"))

    def test_long_multiple_program_ranges_search_row_order_and_all_facts_survive_batching(self):
        first, second = long_program(0, 2, 25000), long_program(1, 2, 25000)
        self.write("combined.cbl", first + second)
        self.write("shared-record.cpy", "01 SHARED-RECORD.\n 05 SHARED-AMOUNT PIC 9(12)V99.\n")
        one_row = self.base / "one-row.sqlite"
        with mock.patch.object(business_index, "FACT_BATCH_ROWS", 1):
            single_result = self.build(one_row)
        batch_result = self.build()
        self.assertEqual(fingerprint(one_row), fingerprint(self.database))
        self.assertEqual(single_result["scope"], batch_result["scope"])
        self.assertEqual(self.indexes(one_row), self.indexes())
        with closing(sqlite3.connect(self.database)) as connection:
            programs = connection.execute("SELECT name,start_line,end_line FROM code_units "
                "WHERE unit_type='Program' ORDER BY start_line").fetchall()
            self.assertEqual(programs, [("FLOW-00000", 2, 25000), ("FLOW-00001", 25002, 50000)])
            with closing(sqlite3.connect(one_row)) as other:
                self.assertEqual(connection.execute("SELECT rowid,unit_id FROM code_units_fts ORDER BY rowid").fetchall(),
                                 other.execute("SELECT rowid,unit_id FROM code_units_fts ORDER BY rowid").fetchall())
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_lookup_index_creation_failure_rolls_back_facts_and_index_ddl(self):
        self.write("flow.cbl", long_program(0, 1, 1000))
        original_connect = business_index._connect

        def connect(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path, factory=IndexWriteFailure)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            return connection

        with mock.patch.object(business_index, "_connect", connect):
            with self.assertRaisesRegex(sqlite3.OperationalError, "database is full"):
                self.build()
        self.assertTrue(business_index._COLD_LOOKUP_INDEXES <= self.indexes().keys())
        with closing(sqlite3.connect(self.database)) as connection:
            for table in ("source_files", "code_units", "code_units_fts", "symbols", "relations", "business_rules"):
                self.assertEqual(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        with mock.patch.object(business_index, "_connect", original_connect):
            self.assertEqual(self.build()["files"]["decoded"], 1)

    def test_cancellation_during_lookup_index_rebuild_keeps_the_empty_schema(self):
        self.write("flow.cbl", long_program(0, 1, 25000))
        rebuilding = False
        original_connect = business_index._connect

        def trace(sql):
            nonlocal rebuilding
            if sql.startswith("CREATE INDEX idx_symbols_lookup"):
                rebuilding = True

        def connect(path):
            connection = original_connect(path)
            connection.set_trace_callback(trace)
            return connection

        def check_cancel():
            if rebuilding:
                raise RuntimeError("CANCELLED")

        with mock.patch.object(business_index, "_connect", connect):
            with self.assertRaisesRegex(RuntimeError, "^CANCELLED$"):
                self.build(check_cancel=check_cancel)
        self.assertTrue(rebuilding)
        self.assertTrue(business_index._COLD_LOOKUP_INDEXES <= self.indexes().keys())
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM source_files").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM code_units_fts").fetchone()[0], 0)

    def test_warm_updates_and_entry_closure_keep_lookup_indexes_available(self):
        self.write("flow.cbl", long_program(0, 1, 1000))
        self.build()
        original = self.indexes()
        self.write("flow.cbl", long_program(0, 1, 1000) + "MOVE VALUE-0000 TO VALUE-0001.\n")
        with mock.patch.object(business_index, "_defer_cold_lookup_indexes", side_effect=AssertionError("must retain indexes")):
            self.assertEqual(self.build()["files"]["indexed_or_updated"], 1)
            catalog = refresh_source_catalog(self.source, self.base / "catalog.sqlite", source_format="free")
            result = self.build(self.base / "closure.sqlite", catalog=catalog, entry_program="FLOW-00000")
            self.assertEqual(result["files"]["decoded"], 1)
        self.assertEqual(self.indexes(), original)

    def test_nonempty_fact_table_is_not_treated_as_a_cold_empty_database(self):
        self.write("flow.cbl", long_program(0, 1, 1000))
        self.build()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DELETE FROM source_files")
            connection.commit()
        with mock.patch.object(business_index, "_defer_cold_lookup_indexes", side_effect=AssertionError("facts are present")):
            self.assertEqual(self.build()["files"]["decoded"], 1)

    def test_a_unique_lookup_index_is_never_temporarily_relaxed(self):
        self.write("flow.cbl", "IDENTIFICATION DIVISION.\nPROGRAM-ID. FLOW-ENTRY.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 REPEATED-VALUE PIC 9.\n01 REPEATED-VALUE PIC 9.\n"
            "PROCEDURE DIVISION.\nGOBACK.\n")
        with closing(business_index._connect(self.database)) as connection:
            business_index._ensure_schema(connection)
            business_index._ensure_business_rules(connection)
            connection.execute("DROP INDEX idx_symbols_lookup")
            connection.execute("CREATE UNIQUE INDEX idx_symbols_lookup ON symbols(symbol_type,name,program_name)")
            connection.commit()
        self.assertEqual(self.build()["files"]["decoded"], 1)
        with closing(sqlite3.connect(self.database)) as connection:
            indexes = {row[1]: row[2] for row in connection.execute("PRAGMA index_list(symbols)")}
            self.assertEqual(indexes["idx_symbols_lookup"], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM source_files").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM symbols WHERE name='REPEATED-VALUE'").fetchone()[0], 1)

    def test_statement_kind_keeps_case_hyphen_and_unknown_token_behavior(self):
        for text, expected in (("  mOvE value to other. ", "MOVE"), ("END-IF.", "END_IF"),
                               ("END_IF.", "END_IF"), ("EXEC SQL SELECT 1", "EXEC_SQL"),
                               ("EXEC  SQL SELECT 1", "OTHER"), ("MOVE-VALUE.", "OTHER"),
                               ("", "OTHER"), ("MOVE...", "MOVE")):
            with self.subTest(text=text):
                self.assertEqual(_statement_kind(text), expected)


if __name__ == "__main__":
    unittest.main()
