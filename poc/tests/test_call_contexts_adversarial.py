"""Independent consistency mutations for bounded source-call contexts."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))
from call_contexts import audit_call_contexts
from structural_index import build_structural_index


class CallContextAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "index.sqlite"
        build_structural_index(POC_ROOT / "fixtures" / "call-context-v3" / "main",
                               self.database, quiet=True)

    def test_changed_callee_root_cannot_keep_confirmed_signature_correspondence(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            binding = connection.execute(
                "SELECT binding_id FROM call_bindings WHERE callee_program = 'CALCWRAP' "
                "AND parameter_position = 3 AND group_member_index = 0 LIMIT 1"
            ).fetchone()
            replacement = connection.execute(
                "SELECT symbol_id FROM symbols WHERE program_name = 'CALCWRAP' "
                "AND symbol_type = 'Field' AND name = 'WRAP-RESULT'"
            ).fetchone()
            self.assertIsNotNone(binding)
            self.assertIsNotNone(replacement)
            connection.execute("UPDATE call_bindings SET callee_symbol_id = ? WHERE binding_id = ?",
                               (replacement[0], binding[0]))
        with self.assertRaises(ValueError):
            audit_call_contexts(self.database, "PAIRMAIN")

    def test_changed_group_member_cannot_keep_confirmed_layout_correspondence(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            binding = connection.execute(
                "SELECT binding_id FROM call_bindings WHERE callee_program = 'CALCWRAP' "
                "AND parameter_position = 1 AND group_member_index = 1 LIMIT 1"
            ).fetchone()
            replacement = connection.execute(
                "SELECT symbol_id FROM symbols WHERE program_name = 'CALCWRAP' "
                "AND symbol_type = 'Field' AND name = 'WRAP-COUNT'"
            ).fetchone()
            self.assertIsNotNone(binding)
            self.assertIsNotNone(replacement)
            connection.execute("UPDATE call_bindings SET callee_symbol_id = ? WHERE binding_id = ?",
                               (replacement[0], binding[0]))
        with self.assertRaises(ValueError):
            audit_call_contexts(self.database, "PAIRMAIN")

    def test_dropped_group_member_cannot_claim_complete_parameter_mapping(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            row = connection.execute(
                "SELECT binding_id, callsite_id FROM call_bindings WHERE callee_program = 'CALCWRAP' "
                "AND parameter_position = 1 AND group_member_index = 2 LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(row)
            connection.execute("DELETE FROM call_bindings WHERE binding_id = ?", (row[0],))
        try:
            result = audit_call_contexts(self.database, "PAIRMAIN")
        except ValueError:
            return
        children = [context for context in result["contexts"] if context["via_callsite_id"] == row[1]]
        self.assertTrue(children)
        self.assertTrue(all(not context["parameter_mapping_complete"] for context in children))

    def test_zero_binding_budget_is_not_complete_and_does_not_mutate_index(self):
        before = self.database.read_bytes()
        result = audit_call_contexts(self.database, "PAIRMAIN", max_bindings=0)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(result["summary"]["parameter_mappings"], 0)
        self.assertTrue(result["summary"]["truncated"])
        self.assertTrue(all(not context["parameter_mapping_complete"]
                            for context in result["contexts"] if context["parent_context_id"] is not None))

    def test_root_only_budget_never_emits_an_extra_child(self):
        result = audit_call_contexts(self.database, "PAIRMAIN", max_contexts=1)
        self.assertEqual(len(result["contexts"]), 1)
        self.assertIsNone(result["contexts"][0]["parent_context_id"])
        self.assertTrue(result["summary"]["truncated"])
        self.assertIn("context_budget_exhausted", result["summary"]["boundary_counts"])


if __name__ == "__main__":
    unittest.main()
