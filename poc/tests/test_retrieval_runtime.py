from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from business_index import build_business_index
from repository_discovery import (ensure_repository_search, read_repository_context,
                                  repository_search_overview, retrieve_repository_context)
import repository_discovery


def program(name, body="GOBACK.\n"):
    return f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nPROCEDURE DIVISION.\n{body}"


class RetrievalRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.database = self.base / "index.sqlite"

    def write(self, relative, text):
        path = self.source / relative
        path.write_text(text, encoding="utf-8")
        return path

    def build(self):
        build_business_index(self.source, self.database, source_format="free")
        return ensure_repository_search(self.database, self.source)

    def test_repeated_questions_use_snapshot_without_source_reads_or_directory_walk(self):
        self.write("selected.cbl", program("SELECTED-ENTRY", "*> ORBITAL-LIMIT\n"))
        for index in range(80):
            self.write(f"unrelated-{index:03}.cbl", program(f"UNRELATED-{index}"))
        overview = self.build()
        with mock.patch("repository_discovery._verified_lines", side_effect=AssertionError("unchanged source read")), \
             mock.patch.object(Path, "open", side_effect=AssertionError("source read")), \
             mock.patch.object(Path, "rglob", side_effect=AssertionError("directory scan")):
            self.assertEqual(repository_search_overview(self.database, self.source)["snapshot_id"], overview["snapshot_id"])
            for _ in range(3):
                result = retrieve_repository_context(self.database, self.source, "ORBITAL-LIMIT")
                self.assertEqual(result["selected_paths"], ["selected.cbl"])
                self.assertEqual(result["cache"]["checked_files"], 1)
                self.assertEqual(result["cache"]["content_verified_files"], 0)
                self.assertFalse(result["cache"]["source_directory_scanned"])
                self.assertIn("ORBITAL-LIMIT", result["pages"][0]["source_text"])

    def test_changed_hit_is_not_sent_as_current_but_other_hits_remain_usable(self):
        changed = self.write("first.cbl", program("FIRST-ENTRY", "*> CRYSTAL-LIMIT\n"))
        self.write("second.cbl", program("SECOND-ENTRY", "*> CRYSTAL-LIMIT\n"))
        self.write("other.cbl", program("OTHER-ENTRY"))
        self.build()
        changed.write_text(program("FIRST-ENTRY", "*> REPLACED-RULE\n"), encoding="utf-8")
        with mock.patch("repository_discovery._verified_lines", wraps=repository_discovery._verified_lines) as verify:
            result = retrieve_repository_context(self.database, self.source, "CRYSTAL-LIMIT")
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(result["selected_paths"], ["second.cbl"])
        self.assertTrue(result["needs_refresh"])
        self.assertEqual(result["boundaries"][0]["relative_path"], "first.cbl")
        self.assertNotIn("REPLACED-RULE", str(result["pages"]))

    def test_touch_without_content_change_verifies_only_hit_once_and_updates_stat_cache(self):
        path = self.write("selected.cbl", program("SELECTED-ENTRY", "*> ORBITAL-LIMIT\n"))
        self.write("other.cbl", program("OTHER-ENTRY"))
        self.build()
        before = path.stat()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
        first = retrieve_repository_context(self.database, self.source, "ORBITAL-LIMIT")
        self.assertEqual(first["cache"]["content_verified_files"], 1)
        second = retrieve_repository_context(self.database, self.source, "ORBITAL-LIMIT")
        self.assertEqual(second["cache"]["content_verified_files"], 0)
        self.assertEqual(second["cache"]["reused_files"], 1)

    def test_large_program_returns_bounded_exact_citations_near_deep_question(self):
        text = program("LONG-ENTRY", "MOVE 1 TO WORK-VALUE.\n" * 18000 + "*> DISTANT-RESERVE-RULE\nCOMPUTE FINAL-VALUE = 17 * INPUT-VALUE.\n")
        self.write("long.cbl", text)
        self.build()
        result = retrieve_repository_context(self.database, self.source, "DISTANT-RESERVE-RULE", max_pages=4, max_chars=5000)
        self.assertLessEqual(sum(len(page["source_text"]) for page in result["pages"]), 5000)
        self.assertLessEqual(len(result["pages"]), 4)
        self.assertGreater(result["pages"][0]["start_line"], 17500)
        self.assertIn("FINAL-VALUE", result["pages"][0]["source_text"])
        with closing(sqlite3.connect(self.database)) as connection:
            for page in result["pages"]:
                self.assertEqual(page["source_text"], "\n".join(text.splitlines()[page["start_line"] - 1:page["end_line"]]))
                self.assertEqual(connection.execute("SELECT text FROM evidence_spans WHERE evidence_id=?", (page["evidence_id"],)).fetchone()[0], page["source_text"])

    def test_unknown_question_uses_small_orientation_without_all_repository_fallback(self):
        for index in range(40):
            self.write(f"entry-{index:03}.cbl", program(f"ENTRY-{index}"))
        self.build()
        result = retrieve_repository_context(self.database, self.source, "UnseenHypothesis", max_pages=3)
        self.assertTrue(result["orientation_only"])
        self.assertFalse(result["fallback_all"])
        self.assertEqual(len(result["pages"]), 3)
        self.assertEqual(result["cache"]["checked_files"], 3)
        self.assertEqual(result["matched_file_count"], 0)

    def test_read_by_range_and_crop_citation_supports_followup_without_rescanning(self):
        text = program("MAIN-ENTRY", "\n".join(f"MOVE {number} TO WORK-VALUE." for number in range(1500)) + "\n")
        self.write("main.cbl", text)
        self.build()
        result = read_repository_context(self.database, self.source, relative_path="main.cbl", start_line=700, end_line=705)
        self.assertTrue(result["range_complete"])
        self.assertEqual(result["pages"][0]["source_text"], "\n".join(text.splitlines()[699:705]))
        second = read_repository_context(self.database, self.source, evidence_id=result["pages"][0]["evidence_id"])
        self.assertEqual(second["pages"][0]["evidence_id"], result["pages"][0]["evidence_id"])
        self.assertEqual(second["cache"]["content_verified_files"], 0)

    def test_local_relations_include_closed_calls_and_copy_without_expanding_long_chain(self):
        self.write("main.cbl", program("MAIN-ENTRY", '*> SKY-RESERVE\nCOPY SHARED.\nCALL "CLOSED-HELPER".\nCALL "WORKER-0".\n'))
        self.write("shared.cpy", "01 SHARED-VALUE PIC X.\n")
        for index in range(30):
            self.write(f"worker-{index:02}.cbl", program(f"WORKER-{index}", f'CALL "WORKER-{index + 1}".\n'))
        self.build()
        result = retrieve_repository_context(self.database, self.source, "SKY-RESERVE", max_pages=6)
        self.assertLessEqual(len(result["selected_paths"]), 6)
        self.assertNotIn("worker-29.cbl", result["selected_paths"])
        closed = next(row for row in result["call_chain"]["links"] if row["target_name"] == "CLOSED-HELPER")
        self.assertEqual(closed["target_source_status"], "unavailable")
        self.assertFalse(closed["runtime_verified"])
        self.assertTrue(any(row["relation_type"] == "INCLUDES_COPY" for row in result["call_chain"]["links"]))

    def test_bounded_range_read_is_contiguous_and_reports_next_unread_line(self):
        text = program("MAIN-ENTRY", "\n".join(f"MOVE {number} TO WORK-VALUE." for number in range(1500)) + "\n")
        self.write("main.cbl", text)
        self.build()
        result = read_repository_context(self.database, self.source, relative_path="main.cbl",
                                         start_line=400, end_line=1000, max_chars=1500)
        self.assertFalse(result["range_complete"])
        pages = result["pages"]
        self.assertEqual(pages[0]["start_line"], 400)
        for previous, following in zip(pages, pages[1:]):
            self.assertEqual(previous["end_line"] + 1, following["start_line"])
        self.assertEqual(result["next_start_line"], pages[-1]["end_line"] + 1)
        self.assertLessEqual(result["coverage"]["selected_characters"], 1500)
        tail = read_repository_context(self.database, self.source, relative_path="main.cbl", start_line=1490)
        self.assertTrue(tail["end_of_file"])
        self.assertTrue(tail["range_complete"])

    def test_prior_index_without_new_state_table_reuses_existing_stat_manifest(self):
        self.write("main.cbl", program("MAIN-ENTRY", "*> AURORA-THRESHOLD\n"))
        self.build()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DROP TABLE repo_source_state")
            connection.commit()
        with mock.patch("repository_discovery._verified_lines", side_effect=AssertionError("source reread")):
            result = retrieve_repository_context(self.database, self.source, "AURORA-THRESHOLD")
        self.assertEqual(result["cache"]["reused_files"], 1)
        self.assertEqual(result["selected_paths"], ["main.cbl"])

    def test_source_symlink_substitution_and_read_path_escape_do_not_leak_text(self):
        path = self.write("main.cbl", program("MAIN-ENTRY", "*> AURORA-THRESHOLD\n"))
        self.build()
        outside = self.base / "outside.cbl"
        outside.write_text("PRIVATE-OUTSIDE-CONTENT", encoding="utf-8")
        path.unlink()
        path.symlink_to(outside)
        result = retrieve_repository_context(self.database, self.source, "AURORA-THRESHOLD")
        self.assertEqual(result["pages"], [])
        self.assertTrue(result["needs_refresh"])
        for relative in ("../outside.cbl", "/outside.cbl", "C:/outside.cbl"):
            with self.subTest(relative=relative), self.assertRaisesRegex(ValueError, "SOURCE_PATH_INVALID"):
                read_repository_context(self.database, self.source, relative_path=relative)

    def test_followup_terms_rank_before_old_question_matches(self):
        for index in range(12):
            self.write(f"old-{index:02}.cbl", program(f"OLD-{index}", "*> REPEATED-ORIGINAL-TERM\n"))
        self.write("new.cbl", program("NEW-ENTRY", "*> NEW-RESEARCH-TARGET\n"))
        self.build()
        result = retrieve_repository_context(self.database, self.source, "REPEATED-ORIGINAL-TERM",
                                             search_terms=["NEW-RESEARCH-TARGET"], max_pages=3)
        self.assertEqual(result["pages"][0]["relative_path"], "new.cbl")
        self.assertEqual(result["matched_file_count"], 13)

    def test_large_matching_file_does_not_hide_other_files_from_top_results(self):
        self.write("huge.cbl", program("HUGE-ENTRY", "*> DISTINCTIVE-TOPIC\n" * 50000))
        self.write("small.cbl", program("SMALL-ENTRY", "*> DISTINCTIVE-TOPIC\n"))
        self.build()
        result = retrieve_repository_context(self.database, self.source, "DISTINCTIVE-TOPIC", max_pages=3)
        self.assertEqual(result["matched_file_count"], 2)
        self.assertIn("small.cbl", result["selected_paths"])

    def test_incremental_structure_refresh_only_hashes_changed_files(self):
        first = self.write("first.cbl", program("FIRST-ENTRY"))
        self.write("second.cbl", program("SECOND-ENTRY"))
        self.build()
        with mock.patch("business_index._verify_file", side_effect=AssertionError("unchanged source read")):
            cached = build_business_index(self.source, self.database, source_format="free")
        self.assertEqual(cached["files"]["metadata_cache_reused"], 2)
        self.assertEqual(cached["files"]["content_hash_verified"], 0)
        first.write_text(program("FIRST-ENTRY", "*> EDITED-RULE\n"), encoding="utf-8")
        import business_index
        with mock.patch("business_index._verify_file", wraps=business_index._verify_file) as verify:
            changed = build_business_index(self.source, self.database, source_format="free")
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(changed["files"]["metadata_cache_reused"], 1)
        self.assertEqual(changed["files"]["content_hash_verified"], 1)
        with mock.patch("business_index._verify_file", wraps=business_index._verify_file) as verify:
            build_business_index(self.source, self.database, source_format="free", verify_content=True)
        self.assertEqual(verify.call_count, 2)

    def test_followup_reserves_history_pages_and_resolves_program_names_and_entry_keys(self):
        self.write("history.cbl", program("PRIOR-ENTRY", "*> RESERVE-BALANCE\n"))
        for index in range(12):
            self.write(f"other-{index:02}.cbl", program(f"OTHER-{index}", "*> STATUS-STATE STATUS-STATE\n"))
        self.build()
        for prior in ("history.cbl", "PRIOR-ENTRY", "history.cbl::PRIOR-ENTRY::2"):
            with self.subTest(prior=prior):
                result = retrieve_repository_context(self.database, self.source, "What changes its STATUS-STATE?",
                                                     prior_paths=[prior], max_pages=3)
                self.assertEqual(result["pages"][0]["relative_path"], "history.cbl")
                self.assertIn("conversation_context", result["pages"][0]["selection_reasons"])
                self.assertTrue(any(path.startswith("other-") for path in result["selected_paths"]))

    def test_snapshot_root_and_integrity_are_checked_without_source_rescan(self):
        self.write("main.cbl", program("MAIN-ENTRY"))
        self.build()
        with self.assertRaisesRegex(ValueError, "SOURCE_ROOT_MISMATCH"):
            repository_search_overview(self.database, self.base)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE repo_metadata SET value='0' WHERE key='ready'")
            connection.commit()
        with self.assertRaisesRegex(ValueError, "REPOSITORY_SEARCH_STALE"):
            repository_search_overview(self.database, self.source)


if __name__ == "__main__":
    unittest.main()
