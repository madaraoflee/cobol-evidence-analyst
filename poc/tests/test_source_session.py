from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
from repository_discovery import ensure_repository_search, repository_search_overview, retrieve_repository_context
from source_session import QuestionSourceSession, refresh_selected_sources


class SourceSessionTests(unittest.TestCase):
    def test_selected_capture_handles_bom_and_ignores_ctime_only_change(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            path = source / "entry.cbl"
            path.write_text("PROGRAM-ID. ENTRY.\nMOVE INPUT TO RESULT.\n", encoding="utf-16")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", verify_content=True)
            ensure_repository_search(database, source)
            with QuestionSourceSession(database, source) as session:
                first = session.capture("entry.cbl")
                self.assertEqual(first.encoding, "utf-16")
                self.assertEqual(session.captured_bytes, path.stat().st_size)
                self.assertIs(session.capture("entry.cbl"), first)
                self.assertEqual(session.captured_bytes, path.stat().st_size)
            path.chmod(0o600)
            with QuestionSourceSession(database, source) as session:
                second = session.capture("entry.cbl")
                self.assertEqual(second.sha256, first.sha256)

    def test_one_selected_file_refresh_preserves_other_facts_and_changes_search(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            changed = source / "change.cbl"
            changed.write_text("PROGRAM-ID. CHANGE.\nPROCEDURE DIVISION.\nMOVE OLD-LEVEL TO RESULT-LEVEL.\n", encoding="utf-8")
            (source / "other.cbl").write_text("PROGRAM-ID. OTHER.\nPROCEDURE DIVISION.\nMOVE KEEP-LEVEL TO OUTPUT-LEVEL.\n", encoding="utf-8")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", verify_content=True)
            overview = ensure_repository_search(database, source)
            with sqlite3.connect(database) as db:
                other_before = db.execute("SELECT sha256 FROM source_files WHERE relative_path='other.cbl'").fetchone()[0]
                old_count = db.execute("SELECT COUNT(*) FROM business_rules WHERE relative_path='change.cbl' AND normalized_text LIKE '%OLD-LEVEL%'").fetchone()[0]
            self.assertGreater(old_count, 0)
            stat = changed.stat()
            changed.write_text(changed.read_text().replace("OLD-LEVEL", "NEW-LEVEL"), encoding="utf-8")
            os.utime(changed, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            with QuestionSourceSession(database, source) as session:
                capture = session.capture("change.cbl")
                first_path = capture.path
                changed.write_text(changed.read_text().replace("NEW-LEVEL", "LATE-LEVEL"), encoding="utf-8")
                self.assertIs(session.capture("change.cbl"), capture)
                self.assertIn(b"NEW-LEVEL", first_path.read_bytes())
                refreshed = refresh_selected_sources(database, [capture], expected_snapshot_id=overview["snapshot_id"])
            self.assertEqual(refreshed["updated"], ["change.cbl"])
            self.assertNotEqual(refreshed["snapshot_id"], overview["snapshot_id"])
            with sqlite3.connect(database) as db:
                self.assertEqual(db.execute("SELECT sha256 FROM source_files WHERE relative_path='other.cbl'").fetchone()[0], other_before)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM business_rules WHERE relative_path='change.cbl' AND normalized_text LIKE '%OLD-LEVEL%'").fetchone()[0], 0)
                self.assertGreater(db.execute("SELECT COUNT(*) FROM business_rules WHERE relative_path='change.cbl' AND normalized_text LIKE '%NEW-LEVEL%'").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM repo_fts WHERE repo_fts MATCH ?", ('"old-level"',)).fetchone()[0], 0)
                self.assertGreater(db.execute("SELECT COUNT(*) FROM repo_fts WHERE repo_fts MATCH ?", ('"new-level"',)).fetchone()[0], 0)
            self.assertEqual(repository_search_overview(database, source)["snapshot_id"], refreshed["snapshot_id"])
            # A later disk edit does not rewrite this question's captured version.
            self.assertEqual(hashlib.sha256(b"PROGRAM-ID. CHANGE.\nPROCEDURE DIVISION.\nMOVE NEW-LEVEL TO RESULT-LEVEL.\n").hexdigest(), capture.sha256)

    def test_failed_transaction_keeps_old_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            path = source / "entry.cbl"
            path.write_text("PROGRAM-ID. ENTRY.\nMOVE FIRST-VALUE TO OUTPUT-VALUE.\n", encoding="utf-8")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", verify_content=True)
            overview = ensure_repository_search(database, source)
            path.write_text("PROGRAM-ID. ENTRY.\nMOVE SECOND-VALUE TO OUTPUT-VALUE.\n", encoding="utf-8")
            with QuestionSourceSession(database, source) as session:
                with mock.patch("structural_index._resolve_relations", side_effect=RuntimeError("fault")):
                    with self.assertRaisesRegex(RuntimeError, "fault"):
                        refresh_selected_sources(database, [session.capture("entry.cbl")],
                                                 expected_snapshot_id=overview["snapshot_id"])
            self.assertEqual(repository_search_overview(database, source)["snapshot_id"], overview["snapshot_id"])

    def test_local_refresh_re_resolves_inbound_call_when_target_becomes_ambiguous(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            (source / "caller.cbl").write_text(
                'PROGRAM-ID. CALLER.\nPROCEDURE DIVISION.\nCALL "TARGET".\n', encoding="utf-8")
            (source / "target.cbl").write_text("PROGRAM-ID. TARGET.\n", encoding="utf-8")
            alternate = source / "alternate.cbl"
            alternate.write_text("PROGRAM-ID. ALTERNATE.\n", encoding="utf-8")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", verify_content=True)
            overview = ensure_repository_search(database, source)
            with sqlite3.connect(database) as db:
                before = db.execute("SELECT status,target_entity_id FROM relations WHERE relative_path='caller.cbl' AND relation_type='CALLS'").fetchone()
            self.assertEqual(before[0], "confirmed")
            self.assertIsNotNone(before[1])
            alternate.write_text("PROGRAM-ID. TARGET.\n", encoding="utf-8")
            with QuestionSourceSession(database, source) as session:
                refreshed = refresh_selected_sources(database, [session.capture("alternate.cbl")],
                    expected_snapshot_id=overview["snapshot_id"])
            with sqlite3.connect(database) as db:
                after = db.execute("SELECT status,target_entity_id FROM relations WHERE relative_path='caller.cbl' AND relation_type='CALLS'").fetchone()
                self.assertEqual(db.execute("SELECT sha256 FROM source_files WHERE relative_path='target.cbl'").fetchone()[0],
                    hashlib.sha256((source / "target.cbl").read_bytes()).hexdigest())
            self.assertEqual(after[0], "candidate")
            self.assertIsNone(after[1])
            self.assertNotEqual(refreshed["snapshot_id"], overview["snapshot_id"])


if __name__ == "__main__":
    unittest.main()
