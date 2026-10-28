from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from repository_identity import identity_blocks_analysis, resolve_source_identity


class FrameworkDocumentIdentityTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.executescript("""
            CREATE TABLE source_files(relative_path TEXT);
            CREATE TABLE symbols(
                symbol_type TEXT, name TEXT, program_name TEXT, relative_path TEXT);
            INSERT INTO source_files VALUES ('rules/collection.cbl');
            INSERT INTO symbols VALUES (
                'Program', 'COLLECTION-RULE', 'COLLECTION-RULE', 'rules/collection.cbl');
        """)

    def test_reference_markdown_paths_do_not_become_missing_source_constraints(self):
        questions = (
            "为什么没有读到 Collection_Navigation.md？",
            "为什么没有读到 业务索引/Collection_Navigation.md？",
            "为什么没有读到 `业务索引/Collection_Navigation.md`？",
            '为什么没有读到 "业务 索引\\Collection_Navigation.MARKDOWN"？',
            "Read guides/Collection_Navigation.md",
        )
        for question in questions:
            with self.subTest(question=question):
                identity = resolve_source_identity(self.connection, question)
                self.assertEqual(identity["status"], "none")
                self.assertFalse(identity_blocks_analysis(identity))
                self.assertFalse(identity["hard_constraint"])

    def test_document_reference_does_not_hide_explicit_source_path_or_program(self):
        questions = (
            "请根据 `业务索引/Collection_Navigation.md` 解释 `rules/collection.cbl`",
            "请根据 `业务索引/Collection_Navigation.md` 解释 rules/collection.cbl",
            "根据 业务索引/Collection_Navigation.md 解释 COLLECTION-RULE",
            '根据 "业务 索引\\Collection_Navigation.markdown" 解释 rules/collection.cbl',
        )
        for question in questions:
            with self.subTest(question=question):
                identity = resolve_source_identity(self.connection, question)
                self.assertEqual(identity["status"], "resolved")
                self.assertEqual(identity["direct_paths"], ["rules/collection.cbl"])
                self.assertFalse(identity_blocks_analysis(identity))
                self.assertFalse(any("Navigation" in item for item in identity["requested"]))

    def test_indexed_markdown_path_remains_a_source_identity(self):
        self.connection.execute("INSERT INTO source_files VALUES ('notes/embedded.md')")
        for question in ("Read notes/embedded.md", "Read `notes/embedded.md`", "Read NOTES/EMBEDDED.MD"):
            with self.subTest(question=question):
                identity = resolve_source_identity(self.connection, question)
                self.assertEqual(identity["status"], "resolved")
                self.assertEqual(identity["direct_paths"], ["notes/embedded.md"])
                self.assertTrue(identity["hard_constraint"])

    def test_missing_source_and_text_paths_keep_their_constraints(self):
        for path in ("unknown/collection.cbl", "unknown/collection.txt"):
            with self.subTest(path=path):
                identity = resolve_source_identity(self.connection, f"Read `{path}`")
                self.assertEqual(identity["status"], "not_found")
                self.assertTrue(identity["hard_constraint"])
                self.assertTrue(identity_blocks_analysis(identity))


if __name__ == "__main__":
    unittest.main()
