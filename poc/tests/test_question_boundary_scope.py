from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from business_map import question_boundaries


class QuestionBoundaryScopeTests(unittest.TestCase):
    def test_other_program_is_catalog_diagnostic_not_question_unknown(self):
        current = {"relative_path": "entry.cbl", "target_name": "UNAVAILABLE", "relation_type": "CALLS"}
        other = {"relative_path": "other.cbl", "target_name": "MISSING", "relation_type": "CALLS"}
        relevant, catalog = question_boundaries([current, other], ["entry.cbl"])
        self.assertEqual(relevant, [current])
        self.assertEqual(catalog[0]["target_name"], "MISSING")
        self.assertEqual(catalog[0]["scope_basis"], "outside_locally_selected_source_paths")

    def test_unlocated_effect_and_copy_layout_remain_conservative(self):
        unlocated = {"reason": "unknown_effect_not_bounded"}
        copy = {"relative_path": "entry.cbl", "start_line": 2, "relation_type": "INCLUDES_COPY"}
        scope = {"relative_path": "entry.cbl", "source_sha256": "a" * 64, "start_line": 10, "end_line": 15}
        relevant, catalog = question_boundaries([unlocated, copy], ["entry.cbl"],
            source_scopes=[scope], source_versions={"entry.cbl": "a" * 64})
        self.assertEqual(relevant, [unlocated, copy])
        self.assertFalse(catalog)

    def test_only_version_matched_callsite_outside_explicit_occurrences_is_partitioned(self):
        inside = {"relative_path": "entry.cbl", "start_line": 12, "relation_type": "CALLS",
                  "source_sha256": "a" * 64, "position_status": "source_excerpt_match"}
        outside = {"relative_path": "entry.cbl", "start_line": 20, "relation_type": "CALLS",
                   "source_sha256": "a" * 64, "position_status": "source_excerpt_match"}
        scope = {"relative_path": "entry.cbl", "source_sha256": "a" * 64, "start_line": 10, "end_line": 15}
        for version in (None, "b" * 64, "a" * 64):
            with self.subTest(version=version):
                relevant, catalog = question_boundaries([inside, outside], ["entry.cbl"],
                    source_scopes=[scope], source_versions={"entry.cbl": version})
                self.assertIn(inside, relevant)
                self.assertEqual(bool(catalog), version == "a" * 64)
                if catalog:
                    self.assertEqual(catalog[0]["scope_basis"], "outside_version_matched_requested_occurrences")

    def test_old_or_unproved_callsite_position_cannot_narrow_an_unknown_effect(self):
        scope = {"relative_path": "entry.cbl", "source_sha256": "a" * 64, "start_line": 10, "end_line": 15}
        for version, status in ((None, None), ("b" * 64, "source_excerpt_match"),
                                ("a" * 64, "unverified"), ("a" * 64, None)):
            with self.subTest(version=version, status=status):
                boundary = {"relative_path": "entry.cbl", "start_line": 20, "relation_type": "CALLS",
                            "source_sha256": version, "position_status": status}
                relevant, catalog = question_boundaries([boundary], ["entry.cbl"], source_scopes=[scope],
                                                       source_versions={"entry.cbl": "a" * 64})
                self.assertEqual(relevant, [boundary])
                self.assertFalse(catalog)

    def test_missing_source_binding_cannot_claim_narrow_unknown_scope(self):
        boundary = {"relative_path": "other.cbl", "start_line": 20, "relation_type": "CALLS"}
        relevant, catalog = question_boundaries([boundary], [])
        self.assertEqual(relevant, [boundary])
        self.assertFalse(catalog)

    def test_compound_question_keeps_outside_procedure_effects(self):
        boundary = {"relative_path": "entry.cbl", "start_line": 20, "relation_type": "CALLS"}
        scope = {"relative_path": "entry.cbl", "source_sha256": "a" * 64,
                 "start_line": 10, "end_line": 15, "exclusive": False}
        relevant, catalog = question_boundaries([boundary], ["entry.cbl"], source_scopes=[scope],
                                               source_versions={"entry.cbl": "a" * 64})
        self.assertEqual(relevant, [boundary])
        self.assertFalse(catalog)


if __name__ == "__main__":
    unittest.main()
