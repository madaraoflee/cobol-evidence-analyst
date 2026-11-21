from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from business_index import build_business_index
from local_source_grep import grep_repository, grep_read_location
from repository_discovery import ensure_repository_search, read_repository_context


class LocalGrepGuardContextTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "source"
        self.root.mkdir()
        self.db = Path(tmp.name) / "source.sqlite"
        self.path = "collect.cbl"
        self.term = "SUBTRACT CREDIT-AMT"
        self.operation = " SUBTRACT CREDIT-AMT FROM NET-DUE"

    def build(self, body, *, fmt="free", term=None):
        source = ["IDENTIFICATION DIVISION.", "PROGRAM-ID. COLLECTION.", "PROCEDURE DIVISION.",
                  *body, "GOBACK."]
        if fmt == "fixed":
            source = [f"{i:06} {line}" for i, line in enumerate(source, 1)]
        (self.root / self.path).write_text("\n".join(source) + "\n", encoding="utf-8")
        build_business_index(self.root, self.db, source_format=fmt, verify_content=True)
        ensure_repository_search(self.db, self.root)
        return grep_repository(self.db, self.root, term or self.term)["matches"][0]

    def test_short_program_supplies_the_distant_condition_and_matching_debit_in_one_read(self):
        hit = self.build(["IF CREDIT-ALLOWED = 'Y'", *(["*> batch in progress"] * 67),
                          self.operation, "END-IF."], fmt="fixed")
        self.assertEqual(hit["line"], 72)
        location, leads = grep_read_location(self.db, hit, self.term, max_chars=12000)
        self.assertEqual((location["start_line"], location["end_line"]), (1, 74))
        self.assertFalse(leads)
        result = read_repository_context(self.db, self.root, **location)
        self.assertTrue(result["range_complete"])
        self.assertIn("IF CREDIT-ALLOWED", result["pages"][0]["source_text"])
        self.assertIn(self.operation, result["pages"][0]["source_text"])

    def test_small_budget_keeps_hit_and_returns_unread_nested_and_else_header_locations(self):
        hit = self.build(["IF VALID-ACCOUNT = 'Y'", " IF HOLD-CREDIT = 'Y'", "  DISPLAY 'HOLD'", " ELSE",
                          *(["*> " + "x" * 130] * 35), self.operation, " END-IF", "END-IF."])
        self.assertEqual(hit["line"], 43)
        previous = read_repository_context(self.db, self.root, relative_path=self.path,
            start_line=hit["line"] - 12, end_line=hit["line"] + 18, max_chars=512)
        self.assertFalse(any("SUBTRACT" in p["source_text"] for p in previous["pages"]))
        location, leads = grep_read_location(self.db, hit, self.term, max_chars=512)
        result = read_repository_context(self.db, self.root, **location, max_chars=512)
        visible = "\n".join(p["source_text"] for p in result["pages"])
        self.assertIn(self.operation, visible)
        self.assertNotIn("HOLD-CREDIT", visible)
        self.assertNotIn("VALID-ACCOUNT", visible)
        self.assertEqual(leads, [{"relative_path": self.path, "start_line": i, "end_line": i}
                                 for i in (4, 5, 7)])
        # The locator itself is not evidence. Each suggested source read must
        # actually return its own original before it can support an answer.
        for lead, expected in zip(leads, ("IF VALID-ACCOUNT", "IF HOLD-CREDIT", "ELSE"), strict=True):
            pages = read_repository_context(self.db, self.root, **lead, max_chars=512)["pages"]
            self.assertEqual(pages[0]["source_sha256"], hit["source_sha256"])
            self.assertIn(expected, pages[0]["source_text"])

    def test_unrelated_closed_condition_and_quoted_or_commented_if_never_widen_the_read(self):
        hit = self.build(["IF OTHER-FLAG = 'Y'", " DISPLAY 'X'", "END-IF.",
                          'DISPLAY "IF HOLD-FLAG = 1.".',
                          *(["*> IF UNRELATED-FLAG = 1"] * 42), self.operation + "."])
        location, leads = grep_read_location(self.db, hit, self.term, max_chars=12000)
        self.assertEqual(location["start_line"], hit["line"] - 12)
        self.assertFalse(leads)

    def test_changed_or_damaged_index_page_produces_no_guard_locator(self):
        hit = self.build(["IF ELIGIBLE = 'Y'", *(["*> source note"] * 30), self.operation, "END-IF."])
        def assert_basic(row):
            loc, leads = grep_read_location(self.db, row, self.term, max_chars=12000)
            self.assertEqual(loc["start_line"], row["line"] - 12)
            self.assertFalse(leads)
        assert_basic({**hit, "source_sha256": "a" * 64})
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE evidence_spans SET text=replace(text,'ELIGIBLE','INACTIVE') "
                       "WHERE evidence_id IN (SELECT evidence_id FROM repo_pages)")
        assert_basic(hit)

    def test_large_file_returns_only_matched_sentence_context_across_index_pages(self):
        hit = self.build([*(["DISPLAY 'PREVIOUS'."] * 410), "IF REMAINING-DUE > 0",
                          *(["*> unrelated punctuation."] * 80), self.operation, "END-IF.",
                          *(["DISPLAY 'LATER'."] * 230)])
        location, leads = grep_read_location(self.db, hit, self.term, max_chars=12000)
        self.assertEqual(location["start_line"], 414)
        self.assertEqual(location["end_line"], hit["line"] + 18)
        self.assertFalse(leads)
        pages = read_repository_context(self.db, self.root, **location)["pages"]
        self.assertTrue(any("IF REMAINING-DUE" in p["source_text"] for p in pages))
        self.assertTrue(any(self.operation in p["source_text"] for p in pages))
