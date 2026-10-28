"""Definition lookup preserves every selected definition and its source version."""

from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_map import _program_definition_rows


class ProgramDefinitionLookupTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        # Deliberately omit symbols: a definition must not depend on a second
        # catalog being complete, or silently disappear from the old result.
        self.db.executescript("""
            CREATE TABLE source_files(relative_path TEXT PRIMARY KEY,sha256 TEXT);
            CREATE TABLE code_units(unit_id TEXT PRIMARY KEY,relative_path TEXT,
                unit_type TEXT,name TEXT,program_name TEXT,start_line INTEGER,
                normalized_text TEXT);
            CREATE INDEX idx_units_path ON code_units(relative_path);
            CREATE INDEX idx_units_program ON code_units(program_name,unit_type);
        """)
        self.paths = [f"folder/member-{index:04d}.cbl" for index in range(1000)]
        for index, path in enumerate(self.paths):
            self.db.execute("INSERT INTO source_files VALUES(?,?)", (path, f"version-{index}"))
            self.db.execute("INSERT INTO code_units VALUES(?,?,?,?,?,?,?)",
                (f"statement-{index}", path, "Statement", "CONTINUE", "SHARED", 4, "CONTINUE."))
            if index != 2:
                self.db.execute("INSERT INTO code_units VALUES(?,?,?,?,?,?,?)",
                    (f"program-{index}", path, "Program", f"FLOW-{index}", None, 3, "PROGRAM-ID."))
        for number, name, line in ((1, "Z-FIRST", 6), (2, "A-SECOND", 6), (3, "EARLIER", 1)):
            self.db.execute("INSERT INTO code_units VALUES(?,?,?,?,?,?,?)",
                (f"nested-{number}", self.paths[1], "Program", name, name, line, "PROGRAM-ID."))
        self.db.execute("INSERT INTO code_units VALUES(?,?,?,?,?,?,?)",
            ("missing-source", "unknown.cbl", "Program", "MISSING-SOURCE", None, 1, "PROGRAM-ID."))
        self.db.commit()

    def original(self, paths):
        result = []
        for start in range(0, len(paths), 400):
            batch = paths[start:start + 400]
            slots = ",".join("?" for _ in batch)
            result.extend(tuple(row) for row in self.db.execute(
                "SELECT u.relative_path,u.name,f.sha256 FROM code_units u "
                "JOIN source_files f ON f.relative_path=u.relative_path "
                f"WHERE u.relative_path IN ({slots}) AND u.unit_type='Program' "
                "ORDER BY u.relative_path,u.start_line", batch))
        return result

    def read(self, paths, *, check_cancel=None):
        return [tuple(row) for row in _program_definition_rows(self.db, paths, len(self.paths), check_cancel)]

    def test_broad_rows_versions_duplicate_names_and_order_match_original(self):
        paths = sorted([*self.paths, "unknown.cbl"])
        actual = self.read(paths)
        self.assertEqual(actual, self.original(paths))
        self.assertEqual([row[1] for row in actual if row[0] == self.paths[1]],
                         ["EARLIER", "FLOW-1", "Z-FIRST", "A-SECOND"])
        self.assertNotIn("MISSING-SOURCE", [row[1] for row in actual])
        self.assertNotIn(self.paths[2], [row[0] for row in actual])

    def test_broad_subset_does_not_add_unselected_programs(self):
        paths = self.paths[::2]
        statements = []
        self.db.set_trace_callback(statements.append)
        actual = self.read(paths)
        self.assertEqual(actual, self.original(paths))
        self.assertTrue(any("INDEXED BY idx_units_program" in query for query in statements))
        self.assertEqual({row[0] for row in actual}, set(paths) - {self.paths[2]})

    def test_small_and_minority_selections_keep_original_path_lookup(self):
        for count in (1, 400, 401):
            with self.subTest(count=count):
                paths = self.paths[:count]
                statements = []
                self.db.set_trace_callback(statements.append)
                actual = self.read(paths)
                self.assertEqual(actual, self.original(paths))
                self.assertFalse(any("INDEXED BY idx_units_program" in query for query in statements))

    def test_snapshot_without_program_index_uses_original_lookup(self):
        self.db.execute("DROP INDEX idx_units_program")
        self.assertEqual(self.read(self.paths), self.original(self.paths))

    def test_cancellation_before_query_and_during_broad_iteration(self):
        statements = []
        self.db.set_trace_callback(statements.append)

        def immediately():
            raise RuntimeError("cancelled before query")

        with self.assertRaisesRegex(RuntimeError, "before query"):
            self.read(self.paths, check_cancel=immediately)
        self.assertEqual(statements, [])
        checks, rows = [], []

        def between_batches():
            checks.append(True)
            if len(checks) == 3:
                raise RuntimeError("cancelled between batches")

        with self.assertRaisesRegex(RuntimeError, "between batches"):
            for row in _program_definition_rows(self.db, self.paths, len(self.paths), between_batches):
                rows.append(row)
        self.assertEqual(len(rows), 400)

    def test_empty_selection_does_not_query(self):
        statements = []
        self.db.set_trace_callback(statements.append)
        self.assertEqual(self.read([]), [])
        self.assertEqual(statements, [])


if __name__ == "__main__":
    unittest.main()
