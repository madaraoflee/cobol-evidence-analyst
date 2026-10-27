"""Reuse immutable local layouts without removing evidence or invalidation."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import framework_layout
import framework_semantics
from source_session import QuestionSourceSession
import test_framework_nested_copy_binding as nested_fixture


class FrameworkLayoutReuseTests(unittest.TestCase):
    def setUp(self):
        self.case = nested_fixture.FrameworkNestedCopyBindingTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.fixture = self.case.case
        self.leaf = self.case.nested(replacing=True)

    def stored(self):
        with closing(sqlite3.connect(self.fixture.database)) as db:
            return db.execute("SELECT * FROM framework_file_semantics ORDER BY relative_path").fetchall()

    def clear_bindings(self):
        with closing(sqlite3.connect(self.fixture.database)) as db, db:
            db.execute("DELETE FROM framework_file_semantics")

    def test_import_reuses_binding_layout_and_preserves_identical_facts(self):
        with patch("framework_layout._context", wraps=framework_layout._context) as context:
            report = self.fixture.build()
        self.assertEqual(report["framework_semantics"]["covered_external_calls"], 1)
        self.assertEqual([call.args[1:] for call in context.call_args_list], [("entry.cbl", "ENTRYJOB")])
        shared = self.stored()
        self.clear_bindings()
        validate = framework_layout.validate_framework_layouts

        def separate_layout(db, facts, **options):
            options.pop("contexts", None)
            return validate(db, facts, **options)

        with patch("framework_layout.validate_framework_layouts", side_effect=separate_layout), \
             patch("framework_layout._context", wraps=framework_layout._context) as context:
            self.fixture.build()
        self.assertEqual(context.call_count, 2)
        self.assertEqual(self.stored(), shared)

    def test_request_rebinding_reuses_layout_without_changing_returned_evidence(self):
        self.fixture.build()
        pages = self.case.pages()
        self.clear_bindings()
        with QuestionSourceSession(self.fixture.database, self.fixture.source) as session, \
             patch("framework_layout._context", wraps=framework_layout._context) as context:
            shared = framework_semantics.build_framework_facts(self.fixture.database, pages,
                reference_path=self.fixture.manual, source_session=session)
        self.assertEqual(context.call_count, 1)
        self.assertTrue(shared["facts"][0]["dependency_covered"])
        validate = framework_layout.validate_framework_layouts

        def separate_layout(db, facts, **options):
            options.pop("contexts", None)
            return validate(db, facts, **options)

        with QuestionSourceSession(self.fixture.database, self.fixture.source) as session, \
             patch("framework_layout.validate_framework_layouts", side_effect=separate_layout), \
             patch("framework_layout._context", wraps=framework_layout._context) as context:
            separate = framework_semantics.build_framework_facts(self.fixture.database, pages,
                reference_path=self.fixture.manual, source_session=session)
        self.assertEqual(context.call_count, 2)
        self.assertEqual(shared, separate)

    def test_direct_literal_skips_unused_digest_but_rechecks_changed_member(self):
        self.fixture.caller.write_text(self.fixture.caller.read_text().replace("MOVE 'KEY' TO ROWS-KEY.\n", ""))
        with patch("framework_semantics._copy_layout_digest", side_effect=AssertionError("unused digest")):
            self.assertEqual(self.fixture.build()["framework_semantics"]["covered_external_calls"], 1)
            self.assertTrue(self.case.facts()["facts"][0]["dependency_covered"])
            self.assertEqual(self.fixture.build()["framework_semantics"]["files_reused"], 1)
            original = self.leaf.read_text()
            self.leaf.write_text(original.replace("TEMPLATE-FUNCTION PIC X(8)", "TEMPLATE-FUNCTION PIC X(3)"))
            self.assertEqual(self.fixture.build()["framework_semantics"]["covered_external_calls"], 0)
            fact, = self.case.facts()["facts"]
            self.assertFalse(fact["dependency_covered"])
            self.assertEqual(fact["reason"], "framework_function_literal_truncated")
            self.leaf.write_text(original)
            self.assertEqual(self.fixture.build()["framework_semantics"]["covered_external_calls"], 1)

    def test_dependent_binding_keeps_digest_and_deep_change_invalidation(self):
        with patch("framework_semantics._copy_layout_digest", wraps=framework_semantics._copy_layout_digest) as digest:
            self.fixture.build()
        self.assertEqual(digest.call_count, 1)
        with closing(sqlite3.connect(self.fixture.database)) as db:
            facts = json.loads(db.execute("SELECT bindings_json FROM framework_file_semantics").fetchone()[0])
        self.assertTrue(facts[0]["layout_dependency_digest"])
        with patch("framework_semantics._copy_layout_digest", wraps=framework_semantics._copy_layout_digest) as digest:
            self.assertEqual(self.fixture.build()["framework_semantics"]["files_reused"], 1)
        self.assertEqual(digest.call_count, 1)
        self.leaf.write_text(self.leaf.read_text().replace("TEMPLATE-KEY PIC", "TEMPLATE-KEY REDEFINES TEMPLATE-FUNCTION PIC"))
        with patch("framework_semantics._copy_layout_digest", wraps=framework_semantics._copy_layout_digest) as digest:
            report = self.fixture.build()["framework_semantics"]
        self.assertEqual(digest.call_count, 1)
        self.assertEqual(report["files_rebuilt"], 1)
        self.assertEqual(report["covered_external_calls"], 0)
        self.assertFalse(self.case.facts()["facts"][0]["dependency_covered"])


if __name__ == "__main__":
    unittest.main()
