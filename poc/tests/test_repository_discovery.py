from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from business_index import build_business_index
from repository_discovery import _dependency_rows, _dependency_selection, discover_repository, ensure_repository_search
from source_reading import prepare_source_reading, read_source_page_batch


def program(name, body="GOBACK.\n"):
    return f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nPROCEDURE DIVISION.\n{body}"


class RepositoryDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.database = self.base / "index.sqlite"

    def write(self, relative, text):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def build(self):
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        return ensure_repository_search(self.database, self.source)

    def test_dependency_batches_preserve_unusual_and_missing_target_bindings(self):
        self.write("entry.cbl", "IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRY.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 TARGET-NAME PIC X(20).\n"
            "PROCEDURE DIVISION.\nCALL TARGET-NAME.\nGOBACK.\n")
        self.build()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            field = connection.execute("SELECT symbol_id FROM symbols WHERE symbol_type='Field' LIMIT 1").fetchone()[0]
            relation = connection.execute("SELECT * FROM relations LIMIT 1").fetchone()
            for index in range(1200):
                row = list(relation)
                row[0], row[6] = f"dynamic-{index:04d}", field if index % 2 else "missing-symbol"
                connection.execute("INSERT INTO relations VALUES (?,?,?,?,?,?,?,?,?,?)", row)
            expected = [dict(row) for row in connection.execute(
                "SELECT r.relative_path,r.relation_type,r.target_name,r.status,r.evidence_id,s.relative_path AS target_path "
                "FROM source_files f CROSS JOIN relations r ON r.relative_path=f.relative_path "
                "LEFT JOIN symbols s ON s.symbol_id=r.target_entity_id "
                "WHERE r.relation_type IN ('CALLS','CALL_TARGET_FROM','INCLUDES_COPY') ORDER BY r.relative_path,r.relation_id")]
            queries = []
            connection.set_trace_callback(queries.append)
            actual = list(_dependency_rows(connection))
            self.assertEqual(actual, expected)
            lookups = [query for query in queries if "WHERE symbol_id IN" in query]
            self.assertEqual(len(lookups), 1, "repeated bindings should not cause per-edge queries")
            self.assertEqual(len(actual), 1201)

    def test_empty_dependency_roots_do_not_read_the_relation_catalog(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            self.assertEqual(_dependency_selection(connection, set(), {"member.cbl"}, None),
                             ([], [], [], []))

    def test_new_concept_in_deep_comment_is_searchable_with_real_nearby_evidence(self):
        content = program("MAIN-ENTRY", 'MOVE 1 TO WORK-VALUE.\n' * 6000 + '*> 星辉额度 uses NebulaRewardCap and ASTEROID_LIMIT for this request.\nGOBACK.\n')
        self.write("main.cbl", content)
        self.write("other.cbl", program("OTHER-ENTRY"))
        overview = self.build()
        self.assertTrue(overview["full_text_complete"])
        self.assertGreater(overview["indexed_pages"], 10)
        for query in ("星辉额度", "Nebula reward cap", "ASTEROID_LIMIT"):
            with self.subTest(query=query):
                result = discover_repository(self.database, query)
                self.assertEqual(result["selected_paths"], ["main.cbl"])
                page = result["matched_pages"][0]
                self.assertIn("星辉额度", page["snippet"])
                self.assertGreater(page["start_line"], 5000)
                with closing(sqlite3.connect(self.database)) as connection:
                    evidence = connection.execute("SELECT text FROM evidence_spans WHERE evidence_id=?", (page["evidence_id"],)).fetchone()[0]
                self.assertEqual(evidence, "\n".join(content.splitlines()[page["start_line"] - 1:page["end_line"]]))

    def test_callers_dependencies_and_shared_copy_do_not_pull_in_siblings(self):
        self.write("entry.cbl", program("ENTRY-ONE", 'CALL "RULE-ONE".\n'))
        self.write("rule.cbl", program("RULE-ONE", '*> LUNAR-ALLOCATION decides the outcome.\nCOPY SHARED.\nCALL "WORKER-ONE".\n'))
        self.write("worker.cbl", program("WORKER-ONE"))
        self.write("shared.cpy", "01 SHARED-STATE PIC X.\n")
        self.write("separate.cbl", program("SEPARATE-ENTRY", 'COPY SHARED.\nCALL "WORKER-ONE".\n'))
        self.build()
        result = discover_repository(self.database, "LUNAR-ALLOCATION")
        self.assertEqual(set(result["selected_paths"]), {"entry.cbl", "rule.cbl", "worker.cbl", "shared.cpy"})
        self.assertNotIn("separate.cbl", result["selected_paths"])
        self.assertIn("caller_of:rule.cbl", next(row["reasons"] for row in result["selection_reasons"] if row["relative_path"] == "entry.cbl"))

    def test_direct_copy_match_finds_users_and_shared_hub_defers_without_hiding_candidates(self):
        self.write("common.cpy", "01 ORBITAL-LABEL PIC X(20).\n")
        for index in range(10):
            self.write(f"member-{index}.cbl", program(f"MEMBER-{index}", "COPY COMMON.\n"))
        self.build()
        result = discover_repository(self.database, "ORBITAL-LABEL")
        self.assertEqual(result["selected_paths"], ["common.cpy"])
        self.assertEqual({item["relative_path"] for item in result["deferred_candidates"]}, {f"member-{index}.cbl" for index in range(10)})
        self.assertFalse(result["dependency_expansion_complete"])
        (self.source / "member-9.cbl").unlink()
        for index in range(2, 9):
            (self.source / f"member-{index}.cbl").unlink()
        self.build()
        self.assertEqual(set(discover_repository(self.database, "ORBITAL-LABEL")["selected_paths"]), {"common.cpy", "member-0.cbl", "member-1.cbl"})

    def test_deep_reverse_call_chain_and_cycle_have_no_depth_cutoff(self):
        for index in range(40):
            target = f"MEMBER-{index + 1}" if index < 39 else "MEMBER-38"
            comment = "*> NOVEL-QUESTION-CONCEPT\n" if index == 39 else ""
            self.write(f"member-{index:02d}.cbl", program(f"MEMBER-{index}", comment + f'CALL "{target}".\n'))
        self.build()
        result = discover_repository(self.database, "NOVEL-QUESTION-CONCEPT")
        self.assertEqual(len(result["selected_paths"]), 40)
        self.assertTrue(result["dependency_expansion_complete"])
        self.assertEqual(discover_repository(self.database, "NOVEL-QUESTION-CONCEPT",
                                            fallback_to_repository=False), result)

    def test_no_lexical_match_falls_back_to_all_and_unavailable_targets_do_not_block(self):
        self.write("entry.cbl", program("MAIN-ENTRY", 'CALL "ABSENT-SERVICE".\nCALL TARGET-NAME.\n'))
        self.write("other.cbl", program("OTHER-ENTRY"))
        self.build()
        result = discover_repository(self.database, "UnrecordedHypothesis")
        self.assertTrue(result["fallback_all"])
        self.assertEqual(len(result["selected_paths"]), 2)
        self.assertEqual(len([row for row in result["boundaries"] if row["reason"] == "dependency_target_unresolved"]), 2)

    def test_refresh_reuses_tokens_but_removes_changed_and_deleted_source_matches(self):
        path = self.write("first.cbl", program("FIRST-ENTRY", "*> GLIMMERONE\n"))
        self.write("deleted.cbl", program("DELETED-ENTRY", "*> GLIMMERTWO\n"))
        first = self.build()
        prior_id = discover_repository(self.database, "GLIMMERONE")["matched_pages"][0]["evidence_id"]
        cached = ensure_repository_search(self.database, self.source)
        self.assertEqual((cached["updated_files"], cached["cached_files"]), (0, 2))
        self.assertEqual(discover_repository(self.database, "GLIMMERONE")["matched_pages"][0]["evidence_id"], prior_id)
        path.write_text(program("FIRST-ENTRY", "*> GLIMMERTHREE\n"), encoding="utf-8")
        (self.source / "deleted.cbl").unlink()
        self.write("new.cbl", program("NEW-ENTRY", "*> GLIMMERFOUR\n"))
        second = self.build()
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(second["deleted_files"], 1)
        self.assertEqual(second["updated_files"], 2)
        self.assertEqual(discover_repository(self.database, "GLIMMERONE")["matched_file_count"], 0)
        self.assertEqual(discover_repository(self.database, "GLIMMERTWO")["matched_file_count"], 0)
        self.assertEqual(discover_repository(self.database, "GLIMMERFOUR")["selected_paths"], ["new.cbl"])

    def test_failed_hash_refresh_invalidates_search_instead_of_returning_old_evidence(self):
        path = self.write("entry.cbl", program("MAIN-ENTRY", "*> CRYSTALVALUE\n"))
        self.build()
        path.write_text(program("MAIN-ENTRY", "*> CHANGEDVALUE\n"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SOURCE_HASH_MISMATCH"):
            ensure_repository_search(self.database, self.source)
        with self.assertRaisesRegex(ValueError, "REPOSITORY_SEARCH_STALE"):
            discover_repository(self.database, "CRYSTALVALUE")
        self.build()
        self.assertEqual(discover_repository(self.database, "CHANGEDVALUE")["matched_file_count"], 1)

    def test_changed_snapshot_requires_refresh_and_queries_cannot_inject_sql_or_fts(self):
        self.write("entry.cbl", program("MAIN-ENTRY", "*> COLORVALUE\n"))
        self.build()
        result = discover_repository(self.database, 'COLORVALUE" OR *; DROP TABLE source_files; --')
        self.assertIn("entry.cbl", result["selected_paths"])
        self.write("new.cbl", program("NEW-ENTRY"))
        build_business_index(self.source, self.database, source_format="free")
        with self.assertRaisesRegex(ValueError, "REPOSITORY_SEARCH_STALE"):
            discover_repository(self.database, "COLORVALUE")

    def test_reading_scope_accepts_multiple_programs_and_reports_repository_denominator(self):
        self.write("first.cbl", program("FIRST-ENTRY", 'CALL "SECOND-ENTRY".\n'))
        self.write("second.cbl", program("SECOND-ENTRY"))
        self.write("other.cbl", program("OTHER-ENTRY"))
        self.build()
        plan = prepare_source_reading(self.database, self.source, entry_program=None, question="Explain the outcome",
                                      include_paths=["first.cbl", "second.cbl"], reading_strategy="full_chain")
        self.assertIsNone(plan["entry"])
        self.assertEqual(plan["coverage"]["repository_total_files"], 3)
        self.assertEqual(plan["coverage"]["investigation_files"], 2)
        self.assertEqual(plan["coverage"]["omitted_repository_files"], 1)
        self.assertTrue(plan["coverage"]["complete"])
        self.assertFalse(plan["coverage"]["repository_complete"])
        self.assertEqual(plan["coverage"]["call_chain"]["covered_calls"], 1)
        self.assertEqual({page["relative_path"] for page in read_source_page_batch(self.database, plan["pages"])}, {"first.cbl", "second.cbl"})
        for paths in (["../escape.cbl"], ["unknown.cbl"], "first.cbl", []):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                prepare_source_reading(self.database, self.source, entry_program=None, question="Explain", include_paths=paths)

    def test_more_than_display_budget_keeps_every_matching_file(self):
        for index in range(205):
            self.write(f"member-{index:03d}.cbl", program(f"MEMBER-{index}", "*> SHARED-QUESTION-TOKEN\n"))
        self.build()
        result = discover_repository(self.database, "SHARED-QUESTION-TOKEN")
        self.assertEqual(result["matched_file_count"], 205)
        self.assertEqual(len(result["selected_paths"]), 205)
        self.assertEqual(len(result["matched_pages"]), 200)
        self.assertEqual(result["omitted_matched_pages"], 5)

    def test_additional_terms_preserve_original_hits_and_previews_rotate_across_files(self):
        self.write("large.cbl", program("LARGE-ENTRY", "*> OCEANVALUE is checked here.\n" * 9000))
        self.write("small.cbl", program("SMALL-ENTRY", "*> MOUNTAINVALUE is checked here.\n"))
        self.build()
        result = discover_repository(self.database, "OCEANVALUE", search_terms=["MOUNTAINVALUE"])
        self.assertEqual(result["matched_file_count"], 2)
        self.assertEqual({page["relative_path"] for page in result["matched_pages"][:2]}, {"large.cbl", "small.cbl"})

    def test_symlink_substitution_invalidates_cached_search(self):
        path = self.write("entry.cbl", program("MAIN-ENTRY", "*> TEXTVALUE\n"))
        self.build()
        original = path.read_text(encoding="utf-8")
        target = self.base / "outside.cbl"
        target.write_text(original, encoding="utf-8")
        path.unlink()
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "SOURCE_PATH_INVALID"):
            ensure_repository_search(self.database, self.source)
        with self.assertRaisesRegex(ValueError, "REPOSITORY_SEARCH_STALE"):
            discover_repository(self.database, "TEXTVALUE")

    def test_oversized_line_is_explicitly_partial_and_never_a_complete_citation(self):
        self.write("entry.cbl", program("MAIN-ENTRY", "*> WIDEVALUE " + "x" * 13000 + "\n"))
        overview = self.build()
        self.assertFalse(overview["full_text_complete"])
        result = discover_repository(self.database, "WIDEVALUE")
        self.assertIsNone(result["matched_pages"][0]["evidence_id"])
        self.assertTrue(result["matched_pages"][0]["span_truncated"])
        self.assertIn("source_line_exceeds_search_page_budget", {row["reason"] for row in result["boundaries"]})


if __name__ == "__main__":
    unittest.main()
