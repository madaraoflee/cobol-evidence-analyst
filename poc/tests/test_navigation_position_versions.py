"""Scope pruning requires current physical dependency evidence."""
from pathlib import Path
from contextlib import closing
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from business_map import build_business_map, question_boundaries
from question_investigation import _question_scopes
import repository_discovery as discovery
from poc.tests import test_bounded_navigation as fixtures


class NavigationPositionVersionTests(unittest.TestCase):
    def fixture(self):
        helper = fixtures.BoundedNavigationTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        helper.cycle(body='MAIN.\nGOBACK.\nFOCUS-RULE.\n'
            'CALL "UNAVAILABLE-WORKER" USING OUT-AMOUNT.\nEXIT.\nOUTSIDE-RULE.\n'
            'CALL "CYCLE-000".\nEXIT.')
        helper.build()
        question = "SCOPECASE 只看 FOCUS-RULE 的业务规则？"
        scopes = _question_scopes(helper.database, ["entry.cbl"], question)
        with closing(sqlite3.connect(helper.database)) as db:
            evidence = db.execute("SELECT evidence_id FROM relations WHERE relative_path=? AND target_name=?",
                ("entry.cbl", "UNAVAILABLE-WORKER")).fetchone()[0]
        return helper, question, scopes, evidence

    def test_stale_version_or_same_version_mislocated_call_cannot_prune_unknown(self):
        for corruption in ("old_version", "same_version_wrong_position", "unit_and_evidence_wrong_position", "wrong_content"):
            with self.subTest(corruption=corruption):
                helper, question, scopes, evidence = self.fixture()
                with closing(sqlite3.connect(helper.database)) as db, db:
                    if corruption == "old_version":
                        db.execute("UPDATE evidence_spans SET source_sha256=?,start_line=?,end_line=? WHERE evidence_id=?",
                            ("0" * 64, scopes[0]["end_line"] + 3, scopes[0]["end_line"] + 3, evidence))
                    elif corruption in {"same_version_wrong_position", "unit_and_evidence_wrong_position"}:
                        db.execute("UPDATE evidence_spans SET start_line=?,end_line=? WHERE evidence_id=?",
                            (scopes[0]["end_line"] + 3, scopes[0]["end_line"] + 3, evidence))
                        if corruption == "unit_and_evidence_wrong_position":
                            db.execute("UPDATE code_units SET start_line=?,end_line=? WHERE evidence_id=?",
                                (scopes[0]["end_line"] + 3, scopes[0]["end_line"] + 3, evidence))
                    else:
                        db.execute("UPDATE evidence_spans SET text=? WHERE evidence_id=?", ('DISPLAY "not a call".', evidence))
                with mock.patch.object(discovery, "_navigation_graph", wraps=discovery._navigation_graph) as graph:
                    result = build_business_map(helper.database, helper.source, question)
                self.assertTrue(graph.called)
                self.assertNotIn("navigation_scope", result)
                self.assertTrue(any(edge["target_name"] == "UNAVAILABLE-WORKER" for edge in result["relations"]))
                self.assertTrue(any(item.get("target_name") == "UNAVAILABLE-WORKER" for item in result["boundaries"]))

    def test_boundary_producer_never_labels_current_file_hash_as_old_evidence_version(self):
        helper, _, scopes, evidence = self.fixture()
        with closing(sqlite3.connect(helper.database)) as db, db:
            db.execute("UPDATE evidence_spans SET source_sha256=?,start_line=?,end_line=? WHERE evidence_id=?",
                ("0" * 64, scopes[0]["end_line"] + 3, scopes[0]["end_line"] + 3, evidence))
        with closing(discovery._connect(helper.database)) as db:
            current = db.execute("SELECT sha256 FROM source_files WHERE relative_path=?", ("entry.cbl",)).fetchone()[0]
            positioned = discovery._position_boundaries(db, [{"relative_path": "entry.cbl", "evidence_id": evidence,
                "relation_type": "CALLS", "target_name": "UNAVAILABLE-WORKER"}], {"entry.cbl"})
        self.assertEqual(positioned[0]["source_sha256"], "0" * 64)
        self.assertNotEqual(positioned[0]["source_sha256"], current)
        self.assertEqual(positioned[0]["position_status"], "unverified")
        self.assertNotIn("caller_line", positioned[0])
        relevant, catalog = question_boundaries(positioned, ["entry.cbl"], source_scopes=scopes,
                                               source_versions={"entry.cbl": current})
        self.assertEqual(relevant, positioned)
        self.assertFalse(catalog)

    def test_identical_outside_call_cannot_relocate_inside_occurrence(self):
        helper = fixtures.BoundedNavigationTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        helper.cycle(body='MAIN.\nGOBACK.\nFOCUS-RULE.\n'
            'CALL "UNAVAILABLE-WORKER" USING OUT-AMOUNT.\nEXIT.\nOUTSIDE-RULE.\n'
            'CALL "UNAVAILABLE-WORKER" USING OUT-AMOUNT.\nEXIT.')
        helper.build()
        question = "SCOPECASE 只看 FOCUS-RULE 的业务规则？"
        with closing(sqlite3.connect(helper.database)) as db, db:
            calls = db.execute("SELECT r.evidence_id,u.start_line,e.source_sha256 FROM relations r "
                "JOIN code_units u ON u.unit_id=r.from_entity_id JOIN evidence_spans e ON e.evidence_id=r.evidence_id "
                "WHERE r.relative_path=? AND r.target_name=? ORDER BY u.start_line",
                ("entry.cbl", "UNAVAILABLE-WORKER")).fetchall()
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][2], calls[1][2])
            db.execute("UPDATE evidence_spans SET start_line=?,end_line=? WHERE evidence_id=?",
                       (calls[1][1], calls[1][1], calls[0][0]))
        result = build_business_map(helper.database, helper.source, question)
        self.assertNotIn("navigation_scope", result)
        self.assertEqual(sum(edge["target_name"] == "UNAVAILABLE-WORKER" for edge in result["relations"]), 2)
        self.assertEqual(sum(item.get("target_name") == "UNAVAILABLE-WORKER" for item in result["boundaries"]), 2)

    def test_actual_source_position_and_content_supply_the_boundary_marker(self):
        helper, _, scopes, evidence = self.fixture()
        with closing(discovery._connect(helper.database)) as db:
            positioned = discovery._position_boundaries(db, [{"relative_path": "entry.cbl", "evidence_id": evidence,
                "relation_type": "CALLS", "target_name": "UNAVAILABLE-WORKER"}], {"entry.cbl"})
        self.assertEqual(positioned[0]["position_status"], "source_excerpt_match")
        self.assertTrue(scopes[0]["start_line"] <= positioned[0]["caller_line"] <= scopes[0]["end_line"])


if __name__ == "__main__":
    unittest.main()
