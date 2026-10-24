"""Missing derived search pages must not hide already imported source."""

import sqlite3
from contextlib import closing
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
from repository_discovery import (ensure_repository_search, read_repository_context,
                                  retrieve_repository_context)


class SearchCacheRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / "source"
        self.source.mkdir()
        self.database = self.source.parent / "index.sqlite"
        lines = ["IDENTIFICATION DIVISION.", "PROGRAM-ID. ROUTECASE.",
                 "DATA DIVISION.", "WORKING-STORAGE SECTION.",
                 "01 RESULT-CODE PIC 99.", "PROCEDURE DIVISION.", "MAIN."]
        lines += ["*> neutral padding " + "x" * 180 for _ in range(8500)]
        lines += ["MOVE 90 TO RESULT-CODE.", 'DISPLAY "LATEBUSINESSRULE" RESULT-CODE.', "GOBACK."]
        (self.source / "entry.cbl").write_text("\n".join(lines) + "\n")
        (self.source / "other.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. OTHER-RULE.\n"
            "PROCEDURE DIVISION.\nGOBACK.\n")
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)

    def remove_page(self, *, remove_source_page=False):
        with closing(sqlite3.connect(self.database)) as db, db:
            page = db.execute("SELECT page_id,start_line,end_line FROM repo_pages "
                "WHERE relative_path='entry.cbl' ORDER BY start_line DESC LIMIT 1").fetchone()
            db.execute("DELETE FROM repo_fts WHERE rowid=?", (page[0],))
            if remove_source_page:
                db.execute("DELETE FROM repo_pages WHERE page_id=?", (page[0],))
        return page

    def test_refresh_repairs_missing_search_entry_and_only_rebuilds_affected_file(self):
        self.remove_page()
        with patch.object(Path, "rglob", side_effect=AssertionError("No source directory rescan")):
            refreshed = ensure_repository_search(self.database, self.source, verify_content=True)
        self.assertEqual((refreshed["updated_files"], refreshed["cached_files"]), (1, 1))
        result = retrieve_repository_context(self.database, self.source, "LATEBUSINESSRULE")
        self.assertEqual(result["matched_file_count"], 1)
        self.assertIn("MOVE 90 TO RESULT-CODE.", "\n".join(p["source_text"] for p in result["pages"]))
        self.assertNotIn("raw_fallback", result)

    def test_refresh_repairs_missing_source_page_without_reimport(self):
        _, start, end = self.remove_page(remove_source_page=True)
        refreshed = ensure_repository_search(self.database, self.source)
        self.assertEqual((refreshed["updated_files"], refreshed["cached_files"]), (1, 1))
        result = read_repository_context(self.database, self.source, relative_path="entry.cbl",
                                         start_line=start, end_line=end, max_chars=32000)
        self.assertTrue(result["range_complete"])
        self.assertIn("MOVE 90 TO RESULT-CODE.", "\n".join(p["source_text"] for p in result["pages"]))

    def test_direct_read_recovers_internal_gap_before_refresh(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            page = db.execute("SELECT page_id,start_line,end_line FROM repo_pages "
                "WHERE relative_path='entry.cbl' ORDER BY start_line LIMIT 1 OFFSET 1").fetchone()
            db.execute("DELETE FROM repo_fts WHERE rowid=?", (page[0],))
            db.execute("DELETE FROM repo_pages WHERE page_id=?", (page[0],))
        result = read_repository_context(self.database, self.source, relative_path="entry.cbl",
            start_line=page[1], end_line=page[2] + 10, max_chars=32000)
        self.assertTrue(result["range_complete"])
        self.assertEqual(result["pages"][0]["start_line"], page[1])
        self.assertFalse(result["raw_fallback"]["source_directory_scanned"])

    def test_direct_read_recovers_missing_tail_before_refresh(self):
        _, start, end = self.remove_page(remove_source_page=True)
        result = read_repository_context(self.database, self.source, relative_path="entry.cbl",
                                         start_line=start - 5, end_line=end, max_chars=32000)
        self.assertTrue(result["range_complete"])
        self.assertIn("MOVE 90 TO RESULT-CODE.", "\n".join(p["source_text"] for p in result["pages"]))


if __name__ == "__main__":
    unittest.main()
