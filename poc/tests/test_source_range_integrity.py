"""Explicit source reads preserve whole lines and report every coverage gap."""

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
import repository_discovery
from repository_discovery import ensure_repository_search, read_repository_context
from source_reading import _identify_page
from source_session import QuestionSourceSession


class SourceRangeIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"

    def build(self, body, *, newline="\n"):
        self.lines = ["IDENTIFICATION DIVISION.", "PROGRAM-ID. RANGE-RULE.",
                      "PROCEDURE DIVISION.", *body, "GOBACK."]
        self.path = self.source / "entry.cbl"
        self.path.write_bytes((newline.join(self.lines) + newline).encode("utf-8"))
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        build_business_index(self.source, self.database, source_format="free",
                             framework_reference_path="", verify_content=True)
        ensure_repository_search(self.database, self.source)

    def read(self, **options):
        return read_repository_context(self.database, self.source, relative_path="entry.cbl",
            start_line=4, end_line=len(self.lines), **options)

    def assert_exact_pages(self, result, maximum):
        self.assertLessEqual(sum(len(page["source_text"]) for page in result["pages"]), maximum)
        for page in result["pages"]:
            self.assertEqual(page["source_sha256"], self.digest)
            self.assertEqual(page["source_text"], "\n".join(
                self.lines[page["start_line"] - 1:page["end_line"]]))
            self.assertFalse(page["span_truncated"])
            self.assertEqual(page["evidence_id"], _identify_page(page))

    def long_condition(self):
        return 'IF FLAG = "Y" ' + 'OR FLAG = "Y" ' * 1100

    def test_long_index_truncated_condition_is_recovered_within_explicit_read_budget(self):
        self.build([self.long_condition(), 'DISPLAY "條件完整"', "END-IF."], newline="\r\n")
        with closing(sqlite3.connect(self.database)) as db:
            self.assertTrue(db.execute("SELECT 1 FROM repo_pages WHERE span_truncated=1").fetchone())
        result = self.read(max_chars=20000)
        self.assertTrue(result["range_complete"])
        self.assertIsNone(result["next_start_line"])
        self.assertEqual(result["file_total_lines"], len(self.lines))
        self.assertTrue(self.long_condition() in "\n".join(page["source_text"] for page in result["pages"]),
                        "The complete long condition must be supplied, not just its following statement")
        self.assert_exact_pages(result, 20000)

    def test_unread_long_condition_cannot_be_hidden_by_a_supplied_tail(self):
        self.build([self.long_condition(), 'DISPLAY "READY"', "END-IF."])
        result = self.read(max_chars=512)
        self.assertFalse(result["range_complete"])
        self.assertEqual(result["next_start_line"], 4)
        self.assertFalse(result["end_of_file"])
        self.assert_exact_pages(result, 512)

    def test_missing_middle_index_page_is_recovered_without_skipping_unread_lines(self):
        self.build(["*> " + "X" * 7000 for _ in range(4)])
        with closing(sqlite3.connect(self.database)) as db, db:
            rows = db.execute("SELECT page_id,start_line,end_line FROM repo_pages ORDER BY start_line").fetchall()
            self.assertGreaterEqual(len(rows), 4)
            missing = rows[1]
            db.execute("DELETE FROM repo_pages WHERE page_id=?", (missing[0],))
        result = self.read(max_chars=32000)
        self.assertTrue(any(page["end_line"] == len(self.lines) for page in result["pages"]))
        self.assertTrue(result["range_complete"])
        self.assertIsNone(result["next_start_line"])
        self.assertTrue(result["end_of_file"])
        self.assertFalse(result["raw_fallback"]["source_directory_scanned"])
        self.assert_exact_pages(result, 32000)
        limited = self.read(max_chars=512)
        self.assertFalse(limited["range_complete"])
        self.assertEqual(limited["next_start_line"], 4)
        self.assertFalse(limited["end_of_file"])
        self.assert_exact_pages(limited, 512)

    def test_direct_range_read_does_not_use_the_small_file_search_limit_or_scan_other_paths(self):
        self.build([self.long_condition(), "END-IF."])
        with mock.patch("repository_discovery.RAW_FALLBACK_FILE_BYTES", 8), \
                mock.patch.object(Path, "rglob", side_effect=AssertionError("No directory walk")):
            result = self.read(max_chars=20000)
        self.assertTrue(result["range_complete"])
        self.assertFalse(result["raw_fallback"]["source_directory_scanned"])
        self.assertEqual(result["raw_fallback"]["checked_pages"], 0)
        self.assert_exact_pages(result, 20000)

    def test_changed_long_source_is_not_relabelled_with_the_indexed_hash(self):
        self.build([self.long_condition(), "END-IF."])
        self.path.write_text(self.path.read_text().replace('FLAG = "Y"', 'FLAG = "N"'))
        result = self.read(max_chars=20000)
        self.assertEqual(result["pages"], [])
        self.assertFalse(result["range_complete"])
        self.assertEqual(result["next_start_line"], 4)
        self.assertEqual(result["raw_fallback"]["status"], "read_failed")
        self.assertEqual(result["raw_fallback"]["failures"][0]["reason"], "SOURCE_HASH_MISMATCH")

    def test_question_cache_distinguishes_read_budgets_and_keeps_the_captured_version(self):
        self.build([self.long_condition(), "END-IF."])
        with QuestionSourceSession(self.database, self.source) as session, \
                mock.patch("repository_discovery._verified_pages", wraps=repository_discovery._verified_pages) as verified:
            short = self.read(max_chars=512, source_session=session)
            self.assertFalse(short["range_complete"])
            self.path.write_text(self.path.read_text().replace('FLAG = "Y"', 'FLAG = "N"'))
            complete = self.read(max_chars=20000, source_session=session)
            repeated = self.read(max_chars=20000, source_session=session)
            self.assertEqual(verified.call_count, 2)
        self.assertTrue(complete["range_complete"])
        self.assertTrue(repeated["raw_fallback"]["cache_reused"])
        self.assertEqual(complete["pages"], repeated["pages"])
        self.assert_exact_pages(complete, 20000)

    def test_cancellation_during_stream_verification_never_persists_staged_pages(self):
        self.build([self.long_condition(), "END-IF.", *["*> " + "X" * 1000 for _ in range(150)]])
        with closing(sqlite3.connect(self.database)) as db:
            before = db.execute("SELECT COUNT(*) FROM evidence_spans").fetchone()[0]
        original = repository_discovery._verified_pages
        started, checks = False, 0

        class Cancelled(RuntimeError):
            pass

        def pages(*args, **kwargs):
            nonlocal started
            started = True
            return original(*args, **kwargs)

        def cancel():
            nonlocal checks
            if started:
                checks += 1
                if checks == 2:
                    raise Cancelled("Stop while verifying the selected source")

        with mock.patch("repository_discovery._verified_pages", side_effect=pages):
            with self.assertRaises(Cancelled):
                self.read(max_chars=20000, check_cancel=cancel)
        self.assertEqual(checks, 2)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM evidence_spans").fetchone()[0], before)


if __name__ == "__main__":
    unittest.main()
