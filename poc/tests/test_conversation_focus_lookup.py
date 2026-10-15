"""Targeted symbol lookups keep full qualification and versioned ambiguity."""

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import conversation_focus as focus


class ConversationFocusLookupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "facts.sqlite"
        self.writer = sqlite3.connect(self.path)
        self.addCleanup(self.writer.close)
        self.writer.executescript("""
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE source_files(relative_path TEXT PRIMARY KEY,sha256 TEXT);
            CREATE TABLE code_units(unit_id TEXT PRIMARY KEY,start_line INTEGER,parse_status TEXT);
            CREATE TABLE symbols(symbol_id TEXT PRIMARY KEY,relative_path TEXT,symbol_type TEXT,
                name TEXT,program_name TEXT,qualified_name TEXT,definition_unit_id TEXT);
            CREATE INDEX idx_symbols_lookup ON symbols(symbol_type,name,program_name);
            CREATE INDEX idx_symbols_path ON symbols(relative_path);
            CREATE TABLE relations(relative_path TEXT,relation_type TEXT,status TEXT,target_entity_id TEXT);
            CREATE INDEX idx_relations_path ON relations(relative_path);
            INSERT INTO metadata VALUES('snapshot_id','snapshot-one');
            INSERT INTO metadata VALUES('source_root_hash','root-one');
            INSERT INTO metadata VALUES('source_options','{"compilation_context":{"dialect":"unknown"}}');
            INSERT INTO metadata VALUES('parser_version','local-parser-one');
            INSERT INTO source_files VALUES('root.cbl','root-sha-one');
            INSERT INTO source_files VALUES('other.cbl','other-sha-one');
            INSERT INTO source_files VALUES('layout.cpy','layout-sha-one');
        """)
        self.writer.commit()

    def symbol(self, identity, name="AMOUNT", *, path="root.cbl", qualifier=None, candidate=False, line=10):
        qualified = "ROOT::" + name + (" OF " + qualifier if qualifier else "")
        self.writer.execute("INSERT INTO code_units VALUES(?,?,?)", ("unit:" + identity, line,
                                                                  "candidate" if candidate else "parsed"))
        self.writer.execute("INSERT INTO symbols VALUES(?,?,?,?,?,?,?)", (identity, path, "Field", name,
                            "ROOT", qualified, "unit:" + identity))

    def connect(self):
        self.writer.commit()
        reader = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        self.addCleanup(reader.close)
        return reader

    def bindings(self, reader, question, paths=("root.cbl",)):
        return focus._bindings(reader, focus._mentions(question), paths)

    def test_late_qualified_candidate_is_not_lost_after_257_other_matches(self):
        for index in range(400):
            self.symbol(f"field:{index:03}", qualifier=f"GROUP-{index:03}", line=index + 1)
        reader = self.connect()
        bindings, unresolved = self.bindings(reader, "AMOUNT OF GROUP-399")
        self.assertFalse(unresolved)
        self.assertIn("AMOUNT OF GROUP-399", bindings)
        self.assertEqual(bindings["AMOUNT OF GROUP-399"]["symbol_id"], "field:399")
        self.assertEqual(bindings["AMOUNT OF GROUP-399"]["definition_line"], 400)
        bindings, unresolved = self.bindings(reader, "AMOUNT")
        self.assertEqual(bindings, {})
        self.assertEqual(unresolved, {"AMOUNT"})

    def test_unrelated_common_name_cannot_hide_a_unique_field(self):
        for index in range(400):
            self.symbol(f"common:{index}", qualifier=f"GROUP-{index}")
        self.symbol("unique", "UNIQUE-AMOUNT", line=901)
        reader = self.connect()
        bound, unresolved = self.bindings(reader, "AMOUNT UNIQUE-AMOUNT")
        self.assertEqual(unresolved, {"AMOUNT"})
        self.assertIn("UNIQUE-AMOUNT", bound)
        self.assertEqual(bound["UNIQUE-AMOUNT"]["symbol_id"], "unique")

    def test_copy_candidates_and_path_scopes_stay_separate(self):
        self.symbol("root", qualifier="INPUT-REC")
        self.symbol("copy", path="layout.cpy", qualifier="INPUT-REC")
        self.symbol("other", path="other.cbl", qualifier="INPUT-REC")
        self.writer.execute("INSERT INTO relations VALUES('root.cbl','INCLUDES_COPY','confirmed','copy')")
        reader = self.connect()
        bound, unresolved = self.bindings(reader, "AMOUNT OF INPUT-REC")
        self.assertEqual(bound, {})
        self.assertEqual(unresolved, {"AMOUNT OF INPUT-REC"})
        bound, unresolved = self.bindings(reader, "AMOUNT OF INPUT-REC", paths=("other.cbl",))
        self.assertFalse(unresolved)
        self.assertEqual(bound["AMOUNT OF INPUT-REC"]["symbol_id"], "other")
        self.assertEqual(self.bindings(reader, "AMOUNT OF INPUT-REC", paths=()), ({}, set()))

    def test_candidate_parse_and_unresolved_copy_never_become_binding(self):
        self.symbol("candidate", qualifier="INPUT-REC", candidate=True)
        self.symbol("not-included", path="layout.cpy", qualifier="OUTPUT-REC")
        self.writer.execute("INSERT INTO relations VALUES('root.cbl','INCLUDES_COPY','ambiguous','not-included')")
        reader = self.connect()
        self.assertEqual(self.bindings(reader, "AMOUNT OF INPUT-REC"), ({}, {"AMOUNT OF INPUT-REC"}))
        self.assertEqual(self.bindings(reader, "AMOUNT OF OUTPUT-REC"), ({}, set()))

    def test_global_lookup_keeps_every_qualified_candidate_without_materializing_all_declarations(self):
        for index in range(400):
            self.symbol(f"common:{index:03}", qualifier=f"GROUP-{index:03}")
        self.symbol("unique", "UNIQUE-AMOUNT", path="other.cbl")
        reader = self.connect()
        statements = []
        reader.set_trace_callback(statements.append)
        bound, unresolved = self.bindings(reader, "AMOUNT UNIQUE-AMOUNT", paths=None)
        self.assertEqual(unresolved, {"AMOUNT"})
        self.assertEqual(bound["UNIQUE-AMOUNT"]["symbol_id"], "unique")
        joins = [sql for sql in statements if "JOIN code_units" in sql]
        self.assertEqual(len(joins), 1)
        self.assertIn("WHERE s.symbol_id IN ('unique')", joins[0])
        self.assertFalse(any("LIMIT" in sql for sql in statements if "FROM symbols s" in sql))

    def test_global_bare_twelve_thousand_candidates_use_covering_count_without_declaration_reads(self):
        for index in range(12000):
            self.symbol(f"common:{index:05}", qualifier=f"GROUP-{index:05}")
        reader = self.connect()
        statements = []
        reader.set_trace_callback(statements.append)
        self.assertEqual(self.bindings(reader, "AMOUNT", paths=None), ({}, {"AMOUNT"}))
        count = next(sql for sql in statements if "SELECT s.name,COUNT(*)" in sql)
        self.assertFalse(any("SELECT s.symbol_id,s.name,s.qualified_name" in sql or
                             "JOIN code_units" in sql for sql in statements))
        plan = reader.execute("EXPLAIN QUERY PLAN " + count).fetchall()
        self.assertTrue(any("COVERING INDEX idx_symbols_lookup" in row[3] for row in plan))
        self.assertFalse(any("LIMIT" in sql for sql in statements if "FROM symbols s" in sql))

    def test_global_qualified_and_mixed_references_keep_the_last_of_twelve_thousand_candidates(self):
        for index in range(12000):
            self.symbol(f"common:{index:05}", qualifier=f"GROUP-{index:05}", line=index + 1)
        reader = self.connect()
        for question, expected_unresolved in (("AMOUNT OF GROUP-11999", set()),
                                              ("AMOUNT; AMOUNT OF GROUP-11999", {"AMOUNT"})):
            bound, unresolved = self.bindings(reader, question, paths=None)
            self.assertEqual(unresolved, expected_unresolved)
            self.assertEqual(bound["AMOUNT OF GROUP-11999"]["symbol_id"], "common:11999")
            self.assertEqual(bound["AMOUNT OF GROUP-11999"]["definition_line"], 12000)

    def test_global_count_preserves_missing_candidate_parse_and_symbol_type_ambiguity(self):
        self.symbol("candidate", "CANDIDATE-AMOUNT", candidate=True)
        self.symbol("field", "SAME-NAME")
        self.symbol("condition", "SAME-NAME")
        self.writer.execute("UPDATE symbols SET symbol_type='ConditionName' WHERE symbol_id='condition'")
        reader = self.connect()
        bound, unresolved = self.bindings(reader, "MISSING-AMOUNT CANDIDATE-AMOUNT SAME-NAME", paths=None)
        self.assertEqual(bound, {})
        self.assertEqual(unresolved, {"CANDIDATE-AMOUNT", "SAME-NAME"})

    def test_global_count_cache_invalidates_on_database_change_without_metadata_change(self):
        self.symbol("first")
        reader = self.connect()
        bound, unresolved = self.bindings(reader, "AMOUNT", paths=None)
        self.assertEqual(bound["AMOUNT"]["symbol_id"], "first")
        self.assertFalse(unresolved)
        self.symbol("second", path="other.cbl", qualifier="OUTPUT-REC")
        self.writer.commit()
        self.assertEqual(self.bindings(reader, "AMOUNT", paths=None), ({}, {"AMOUNT"}))

    def test_global_count_cache_invalidates_on_wal_change_without_metadata_change(self):
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.symbol("first", qualifier="INPUT-REC")
        reader = self.connect()
        bound, unresolved = self.bindings(reader, "AMOUNT", paths=None)
        self.assertEqual(bound["AMOUNT"]["symbol_id"], "first")
        self.assertFalse(unresolved)
        wal = Path(str(self.path) + "-wal")
        self.assertTrue(wal.is_file())
        self.symbol("second", path="other.cbl", qualifier="OUTPUT-REC")
        self.writer.commit()
        self.assertEqual(self.bindings(reader, "AMOUNT", paths=None), ({}, {"AMOUNT"}))
        bound, unresolved = self.bindings(reader, "AMOUNT OF OUTPUT-REC", paths=None)
        self.assertEqual(bound["AMOUNT OF OUTPUT-REC"]["symbol_id"], "second")
        self.assertFalse(unresolved)
        self.writer.execute("DELETE FROM symbols WHERE symbol_id='second'")
        self.writer.execute("UPDATE source_files SET sha256='root-sha-two' WHERE relative_path='root.cbl'")
        self.writer.commit()
        bound, unresolved = self.bindings(reader, "AMOUNT", paths=None)
        self.assertEqual(bound["AMOUNT"]["source_sha256"], "root-sha-two")
        self.assertFalse(unresolved)

    def test_local_selection_uses_existing_path_index_and_leaves_schema_unchanged(self):
        self.symbol("root", qualifier="INPUT-REC")
        self.symbol("other", path="other.cbl", qualifier="INPUT-REC")
        reader = self.connect()
        before = reader.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        statements = []
        reader.set_trace_callback(statements.append)
        bound, unresolved = self.bindings(reader, "AMOUNT OF INPUT-REC")
        self.assertEqual(bound["AMOUNT OF INPUT-REC"]["symbol_id"], "root")
        self.assertFalse(unresolved)
        selection = next(sql for sql in statements if "SELECT s.symbol_id,s.name,s.qualified_name" in sql)
        self.assertIn("INDEXED BY idx_symbols_path", selection)
        plan = reader.execute("EXPLAIN QUERY PLAN " + selection).fetchall()
        self.assertTrue(any("idx_symbols_path (relative_path=?)" in row[3] for row in plan))
        self.assertEqual(reader.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall(), before)

    def test_cache_is_bound_to_current_paths_entities_and_snapshot(self):
        self.symbol("root", qualifier="INPUT-REC")
        self.symbol("other", path="other.cbl", qualifier="INPUT-REC")
        reader = self.connect()
        statements = []
        reader.set_trace_callback(statements.append)
        first, _ = self.bindings(reader, "AMOUNT OF INPUT-REC")
        first["AMOUNT OF INPUT-REC"]["source_sha256"] = "caller-change"
        statements.clear()
        again, _ = self.bindings(reader, "AMOUNT OF INPUT-REC")
        self.assertEqual(again["AMOUNT OF INPUT-REC"]["source_sha256"], "root-sha-one")
        self.assertFalse(any("SELECT s.symbol_id,s.name,s.qualified_name" in sql for sql in statements))
        other, _ = self.bindings(reader, "AMOUNT OF INPUT-REC", paths=("other.cbl",))
        self.assertEqual(other["AMOUNT OF INPUT-REC"]["symbol_id"], "other")
        self.assertEqual(self.bindings(reader, "MISSING-FIELD"), ({}, set()))
        self.writer.execute("UPDATE source_files SET sha256='root-sha-two' WHERE relative_path='root.cbl'")
        self.writer.execute("UPDATE metadata SET value='snapshot-two' WHERE key='snapshot_id'")
        self.writer.commit()
        rebound, _ = self.bindings(reader, "AMOUNT OF INPUT-REC")
        self.assertEqual(rebound["AMOUNT OF INPUT-REC"]["source_sha256"], "root-sha-two")

    def test_compilation_change_invalidates_lookup_without_source_snapshot_change(self):
        self.symbol("root", qualifier="INPUT-REC")
        reader = self.connect()
        self.bindings(reader, "AMOUNT OF INPUT-REC")
        self.writer.execute("UPDATE metadata SET value=? WHERE key='source_options'",
                            ('{"compilation_context":{"dialect":"target-profile","compile_conditions":["MODE-A"]}}',))
        self.writer.commit()
        statements = []
        reader.set_trace_callback(statements.append)
        rebound, _ = self.bindings(reader, "AMOUNT OF INPUT-REC")
        self.assertEqual(rebound["AMOUNT OF INPUT-REC"]["symbol_id"], "root")
        self.assertTrue(any("SELECT s.symbol_id,s.name,s.qualified_name" in sql for sql in statements))

    def test_caller_transaction_is_preserved_and_own_read_snapshot_is_closed(self):
        self.symbol("root", qualifier="INPUT-REC")
        reader = self.connect()
        self.bindings(reader, "AMOUNT OF INPUT-REC")
        self.assertFalse(reader.in_transaction)
        reader.execute("BEGIN")
        self.bindings(reader, "AMOUNT OF INPUT-REC")
        self.assertTrue(reader.in_transaction)
        reader.rollback()
        self.writer.execute("UPDATE source_files SET sha256='pending-sha' WHERE relative_path='root.cbl'")
        bound, _ = self.bindings(self.writer, "AMOUNT OF INPUT-REC")
        self.assertEqual(bound["AMOUNT OF INPUT-REC"]["source_sha256"], "pending-sha")
        self.assertTrue(self.writer.in_transaction)
        self.writer.rollback()

    def test_older_snapshot_without_path_index_stays_correct(self):
        self.symbol("root", qualifier="INPUT-REC")
        self.writer.execute("DROP INDEX idx_symbols_path")
        reader = self.connect()
        bound, unresolved = self.bindings(reader, "AMOUNT OF INPUT-REC")
        self.assertFalse(unresolved)
        self.assertEqual(bound["AMOUNT OF INPUT-REC"]["symbol_id"], "root")


if __name__ == "__main__":
    unittest.main()
