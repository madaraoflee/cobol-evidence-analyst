"""A missing search hit does not erase sources in the imported snapshot."""

from contextlib import closing
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
from repository_discovery import ensure_repository_search, retrieve_repository_context, read_repository_context
from source_reading import _identify_page
from source_session import QuestionSourceSession


class RawRetrievalFallbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.text = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. AMOUNT-RULE.\n"
                     "PROCEDURE DIVISION.\nCOMPUTE RESULT-AMOUNT = INPUT-AMOUNT * 2.\nGOBACK.\n")
        (self.source / "rule.cbl").write_text(self.text)
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)

    def retrieve(self, question, **kwargs):
        return retrieve_repository_context(self.database, self.source, question, **kwargs)

    def test_missing_fts_rows_recover_raw_evidence_and_reuse_question_cache(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DELETE FROM repo_fts")
        with QuestionSourceSession(self.database, self.source) as session:
            first = self.retrieve("RESULT-AMOUNT", source_session=session)
            second = self.retrieve("RESULT-AMOUNT", source_session=session)
        self.assertEqual(first["matched_page_count"], 0)
        self.assertEqual(first["raw_fallback"]["status"], "matched")
        self.assertFalse(first["orientation_only"])
        self.assertTrue(second["raw_fallback"]["cache_reused"])
        self.assertEqual(first["pages"][0]["evidence_id"], _identify_page(first["pages"][0]))
        self.assertIn("COMPUTE RESULT-AMOUNT", first["pages"][0]["source_text"])

    def test_missing_pages_read_only_the_exact_imported_file(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DELETE FROM repo_fts")
            connection.execute("DELETE FROM repo_pages")
        with mock.patch.object(Path, "rglob", side_effect=AssertionError("No directory walk")):
            result = self.retrieve("Explain rule.cbl")
        self.assertEqual(result["source_identity"]["direct_paths"], ["rule.cbl"])
        self.assertEqual(result["raw_fallback"]["status"], "matched")
        self.assertFalse(result["raw_fallback"]["source_directory_scanned"])
        self.assertEqual(result["pages"][0]["source_text"], self.text.rstrip("\n"))
        self.assertEqual(result["pages"][0]["selection_reasons"], ["imported_source_fallback"])

    def test_changed_imported_file_is_not_passed_off_as_the_indexed_version(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DELETE FROM repo_fts")
            connection.execute("DELETE FROM repo_pages")
        (self.source / "rule.cbl").write_text(self.text.replace("* 2", "* 7"))
        result = self.retrieve("Explain rule.cbl")
        self.assertEqual(result["pages"], [])
        self.assertEqual(result["raw_fallback"]["status"], "read_failed")
        self.assertEqual(result["raw_fallback"]["failures"][0]["reason"], "SOURCE_HASH_MISMATCH")

    def test_explicit_read_recovers_the_requested_range_when_search_pages_are_missing(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DELETE FROM repo_fts")
            connection.execute("DELETE FROM repo_pages")
        result = read_repository_context(self.database, self.source, relative_path="rule.cbl",
                                         start_line=4, end_line=4)
        self.assertTrue(result["range_complete"])
        self.assertEqual(result["pages"][0]["source_text"], self.text.splitlines()[3])
        self.assertEqual(result["pages"][0]["start_line"], 4)
        self.assertEqual(result["pages"][0]["end_line"], 4)

    def test_direct_read_failure_does_not_expose_absolute_local_path(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DELETE FROM repo_fts")
            connection.execute("DELETE FROM repo_pages")
        private_path = str(self.source / "rule.cbl")
        failed_file = mock.Mock()
        failed_file.stat.side_effect = PermissionError(13, "Permission denied", private_path)
        with mock.patch("repository_discovery._safe_file", return_value=failed_file):
            result = self.retrieve("Explain rule.cbl")
        self.assertEqual(result["pages"], [])
        self.assertEqual(result["raw_fallback"]["status"], "read_failed")
        self.assertEqual(result["raw_fallback"]["failures"],
                         [{"relative_path": "rule.cbl", "reason": "SOURCE_READ_FAILED"}])
        self.assertNotIn(private_path, json.dumps(result))

    def test_no_match_with_budget_exhaustion_is_not_a_verified_absence(self):
        with mock.patch("repository_discovery.RAW_FALLBACK_CHARACTERS", 1):
            result = self.retrieve("UNKNOWNMARKER")
        self.assertEqual(result["raw_fallback"]["status"], "budget_exhausted")
        self.assertEqual(result["raw_fallback"]["matched_pages"], 0)
        self.assertTrue(result["raw_fallback"]["exhausted"])
        self.assertTrue(any(item["reason"] == "bounded_raw_search_incomplete"
                            for item in result["boundaries"]))

    def test_stale_snapshot_is_not_mixed_with_current_source(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE repo_metadata SET value='other-snapshot' WHERE key='snapshot_id'")
        with self.assertRaisesRegex(ValueError, "REPOSITORY_SEARCH_STALE"):
            self.retrieve("Explain rule.cbl")

    def test_oversized_raw_line_is_reported_as_truncated_not_absent(self):
        (self.source / "rule.cbl").write_text(self.text + "DISPLAY '" + "X" * 14000 + "'.\n")
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DELETE FROM repo_fts")
            connection.execute("DELETE FROM repo_pages")
        result = read_repository_context(self.database, self.source,
                                         relative_path="rule.cbl", start_line=6, end_line=6)
        self.assertEqual(result["pages"], [])
        self.assertEqual(result["raw_fallback"]["status"], "read_truncated")
        self.assertFalse(result["range_complete"])
        self.assertEqual(result["next_start_line"], 6)

    def test_same_named_files_remain_ambiguous_instead_of_reading_the_first(self):
        for folder in ("first", "second"):
            destination = self.source / folder
            destination.mkdir()
            (destination / "rule.cbl").write_text(self.text)
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DELETE FROM repo_fts")
            connection.execute("DELETE FROM repo_pages")
        result = self.retrieve("Explain rule.cbl")
        self.assertEqual(result["source_identity"]["status"], "ambiguous")
        self.assertEqual(result["pages"], [])
        self.assertNotIn("raw_fallback", result)

    def test_unimported_file_is_not_read_even_when_it_exists_under_source_root(self):
        (self.source / "unimported.cbl").write_text(self.text)
        with mock.patch("repository_discovery._safe_file", side_effect=AssertionError("Not imported")):
            result = read_repository_context(self.database, self.source,
                                             relative_path="unimported.cbl", start_line=1, end_line=5)
        self.assertEqual(result["pages"], [])
        self.assertFalse(result["range_complete"])
        self.assertNotIn("raw_fallback", result)


if __name__ == "__main__":
    unittest.main()
