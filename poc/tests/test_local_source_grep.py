from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from business_index import build_business_index
import local_source_grep as grep
from repository_discovery import ensure_repository_search
from source_session import QuestionSourceSession


class LocalSourceGrepTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name, "source")
        self.root.mkdir()
        self.db = Path(temp.name, "library.sqlite")

    def build(self):
        build_business_index(self.root, self.db, source_format="free", verify_content=True)
        ensure_repository_search(self.db, self.root)

    def search(self, query, **kwargs):
        return grep.grep_repository(self.db, self.root, query, **kwargs)

    def test_literal_pagination_covers_noisy_pages_then_late_file_without_copying(self):
        term = "a.*[27] $(x)"
        (self.root / "first.cbl").write_text(("\n".join(["*> " + term.upper()] * 44)) + "\n")
        (self.root / "late.cbl").write_text("\n\n*> end " + term)
        (self.root.parent / "outside.cbl").write_text(term)
        with patch("repository_discovery.PAGE_CHARS", 47):
            self.build()
        with QuestionSourceSession(self.db, self.root) as session, patch.object(
                session, "capture", side_effect=AssertionError("whole-library capture")), patch.object(
                grep, "MAX_PAGES", 3):
            cursor, locations = None, []
            for _ in range(50):
                page = self.search(term, cursor=cursor, limit=4, source_session=session)
                self.assertLessEqual(page["checked_pages"], 3)
                self.assertLessEqual(len(page["matches"]), 4)
                self.assertEqual(page["boundaries"], [])
                locations.extend((m["relative_path"], m["line"]) for m in page["matches"])
                if page["reached_end"]:
                    self.assertTrue(page["complete"])
                    self.assertIsNone(page["next_cursor"])
                    break
                self.assertFalse(page["complete"])
                self.assertIsInstance(page["next_cursor"], str)
                self.assertNotEqual(page["next_cursor"], cursor)
                cursor = page["next_cursor"]
            else:
                self.fail("literal search did not finish")
            self.assertEqual(locations, [("first.cbl", i) for i in range(1, 45)] + [("late.cbl", 3)])
            self.assertEqual(session.captured_bytes, 0)

    def test_empty_window_resumes_past_128_pages_and_can_be_cancelled(self):
        term = "TARGET-AFTER-BULK"
        filler = "*> twenty character filler\n"
        (self.root / "bulk.cbl").write_text(filler * 140 + "*> " + term)
        with patch("repository_discovery.PAGE_CHARS", len(filler) + 1):
            self.build()
        with patch.object(grep, "MAX_PAGES", 130):
            first = self.search(term)
            self.assertEqual(first["checked_pages"], 130)
            self.assertFalse(first["complete"])
            self.assertTrue(first["next_cursor"])
            self.assertEqual(first["matches"], [])
            calls = 0

            def cancel():
                nonlocal calls
                calls += 1
                if calls == 5:
                    raise RuntimeError("test cancellation")

            with self.assertRaisesRegex(RuntimeError, "test cancellation"):
                self.search(term, cursor=first["next_cursor"], check_cancel=cancel)
            last = self.search(term, cursor=first["next_cursor"])
            self.assertTrue(last["complete"])
            self.assertEqual(last["matches"][0]["line"], 141)
            self.assertLess(last["checked_pages"], 20)

    def test_oversized_physical_line_uses_import_encoding_and_verified_bytes(self):
        term = "應收扣除"
        for i, encoding in enumerate(("utf-16", "cp950")):
            (self.root / f"rule{i}.cbl").write_bytes(("\r\n*> " + "A" * 16000 + term + "\r\n").encode(encoding))
        self.build()
        found = self.search(term)
        self.assertTrue(found["complete"], found)
        self.assertEqual([(row["relative_path"], row["line"]) for row in found["matches"]],
                         [("rule0.cbl", 2), ("rule1.cbl", 2)])
        (self.root / "rule1.cbl").write_text(term, encoding="cp950")
        changed = self.search(term)
        self.assertTrue(changed["reached_end"])
        self.assertFalse(changed["complete"])
        self.assertEqual([row["relative_path"] for row in changed["matches"]], ["rule0.cbl"])
        self.assertEqual(changed["boundaries"][0]["reason_code"], "SOURCE_HASH_MISMATCH")
        vanished = Mock()
        vanished.stat.side_effect = FileNotFoundError()

        def disappear_after_path_check(root, relative):
            return vanished if relative == "rule1.cbl" else root / relative

        with patch.object(grep, "_safe_file", side_effect=disappear_after_path_check):
            race = self.search(term)
        self.assertTrue(race["reached_end"])
        self.assertFalse(race["complete"])
        self.assertEqual([row["relative_path"] for row in race["matches"]], ["rule0.cbl"])
        self.assertEqual(race["boundaries"][0]["reason_code"], "SOURCE_PATH_INVALID")

    def test_missing_page_keeps_later_hits_and_coverage_gap_across_continuations(self):
        (self.root / "rule.cbl").write_text("*> mark-1\n*> mark-2\n*> mark-3\n")
        with patch("repository_discovery.PAGE_CHARS", 12):
            self.build()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM repo_pages WHERE start_line=1")
        with patch.object(grep, "RAW_FILE_BYTES", 1):
            first = self.search("mark", limit=1)
            self.assertEqual([m["line"] for m in first["matches"]], [2])
            self.assertEqual(first["boundaries"][0]["reason_code"], "GREP_RAW_FILE_TOO_LARGE")
            last = self.search("mark", limit=1, cursor=first["next_cursor"])
            self.assertEqual([m["line"] for m in last["matches"]], [3])
            self.assertTrue(last["reached_end"])
            self.assertFalse(last["complete"])

        # The exceptional disk-read cache holds line locations for this immutable hash.
        # A later question rechecks disk when it resumes an old cursor.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM repo_pages")
        with QuestionSourceSession(self.db, self.root) as session:
            first = self.search("mark", limit=1, source_session=session)
            (self.root / "rule.cbl").write_text("*> different bytes")
            resumed = self.search("mark", cursor=first["next_cursor"], source_session=session)
            self.assertTrue(resumed["complete"])
            self.assertEqual([m["line"] for m in resumed["matches"]], [2, 3])
            self.assertEqual(first["matches"][0]["source_sha256"], resumed["matches"][0]["source_sha256"])
            self.assertEqual(session.captured_bytes, 0)
        with QuestionSourceSession(self.db, self.root) as later:
            changed = self.search("mark", cursor=first["next_cursor"], source_session=later)
            self.assertEqual(changed["matches"], [])
            self.assertFalse(changed["complete"])
            self.assertEqual(changed["boundaries"][0]["reason_code"], "SOURCE_HASH_MISMATCH")

    def test_cursor_binds_scope_literal_and_generation_and_rejects_invalid_paths(self):
        for name in ("one.cbl", "two.cbl"):
            (self.root / name).write_text("*> FLAG\n*> flag\n")
        self.build()
        (self.root / "unimported.cbl").write_text("*> flag\n")
        self.assertTrue(self.search("flag", paths=[])["complete"])
        self.assertEqual(self.search("flag", paths=[])["matches"], [])
        paths = ["one.cbl"]
        first = self.search("Flag", paths=paths, limit=1)
        token = first["next_cursor"]
        self.assertEqual([m["line"] for m in self.search("FLAG", paths=paths, cursor=token)["matches"]], [2])
        self.assertEqual({m["relative_path"] for m in self.search("flag")["matches"]}, {"one.cbl", "two.cbl"})
        for kwargs in ({"paths": None}, {"query": "another", "paths": paths}, {"paths": ["two.cbl"]}):
            params = {"query": "flag", "cursor": token, **kwargs}
            with self.assertRaisesRegex(ValueError, "GREP_CURSOR_INVALID"):
                self.search(**params)
        with self.assertRaisesRegex(ValueError, "SOURCE_PATH_INVALID"):
            self.search("flag", paths=["../one.cbl"])
        self.build()
        with self.assertRaisesRegex(ValueError, "GREP_CURSOR_INVALID"):
            self.search("flag", paths=paths, cursor=token)
