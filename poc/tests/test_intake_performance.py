"""Regression checks for complete, incremental repository intake."""

from contextlib import closing
from pathlib import Path
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyze_source import analyze_source
from repository_discovery import discover_repository, ensure_repository_search
import repository_discovery


def program(name, topic, call=""):
    return (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\n"
            f"PROCEDURE DIVISION.\n*> {topic}\n{call}GOBACK.\n")


class IncrementalIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.output = self.base / "output"
        self.database = self.output / "structural-index.sqlite"

    def write(self, name, content):
        path = self.source / name
        path.write_text(content, encoding="utf-8")
        return path

    def intake(self, **options):
        return analyze_source(self.source, self.output, index_mode="catalog",
                              analysis_mode="business", reading_strategy="retrieval",
                              source_format="free", **options)

    def test_new_repository_files_do_not_scan_existing_fts_to_delete_absent_facts(self):
        for index in range(32):
            self.write(f"member-{index:02}.cbl", program(f"MEMBER-{index}", "COMMON-TOPIC",
                       f'CALL "MEMBER-{(index + 1) % 32}".\n'))
        with patch("business_index._delete_file_facts", side_effect=AssertionError("new file deletion")):
            report = self.intake()
        self.assertEqual(report["build_report"]["files"]["indexed_or_updated"], 32)
        self.assertEqual(report["repository_search"]["indexed_files"], 32)
        self.assertTrue(report["repository_search"]["full_text_complete"])
        self.assertEqual(len(discover_repository(self.database, "COMMON-TOPIC")["selected_paths"]), 32)

    def test_warm_web_intake_reuses_structure_and_search_without_source_body_reads(self):
        self.write("main.cbl", program("MAIN", "PRIMARYMARK", 'CALL "WORKER".\n'))
        self.write("worker.cbl", program("WORKER", "SECONDARYMARK"))
        before = self.intake()
        with patch("business_index._verify_file", side_effect=AssertionError("source hashed")), \
             patch("business_index._physical_lines", side_effect=AssertionError("source parsed")), \
             patch("analyze_source._verify_scope", side_effect=AssertionError("repository rehashed")), \
             patch("repository_discovery._verified_lines", side_effect=AssertionError("search source reread")), \
             patch("repository_discovery._verified_pages", side_effect=AssertionError("search source rebuilt")):
            after = self.intake()
        self.assertTrue(after["source_manifest_verified"])
        self.assertEqual(after["build_report"]["snapshot_id"], before["build_report"]["snapshot_id"])
        self.assertEqual(after["build_report"]["files"]["metadata_cache_reused"], 2)
        self.assertEqual(after["repository_search"]["metadata_cache_reused"], 2)
        self.assertEqual(after["repository_search"]["content_hash_verified"], 0)
        self.assertEqual(discover_repository(self.database, "SECONDARYMARK")["matched_file_count"], 1)

    def test_one_changed_file_refreshes_its_text_and_preserves_other_cached_pages(self):
        self.write("main.cbl", program("MAIN", "OLDMARK"))
        self.write("worker.cbl", program("WORKER", "STABLEMARK"))
        self.intake()
        self.write("main.cbl", program("MAIN", "REPLACEMENTMARK", 'CALL "WORKER".\n'))
        with patch("repository_discovery._verified_pages", wraps=repository_discovery._verified_pages) as pages:
            refreshed = self.intake()
        self.assertEqual(pages.call_count, 1)
        self.assertEqual(refreshed["build_report"]["files"]["indexed_or_updated"], 1)
        self.assertEqual(refreshed["repository_search"]["metadata_cache_reused"], 1)
        self.assertEqual(discover_repository(self.database, "OLDMARK")["matched_file_count"], 0)
        found = discover_repository(self.database, "REPLACEMENTMARK")
        self.assertEqual(found["matched_file_count"], 1)
        self.assertEqual(set(found["selected_paths"]), {"main.cbl", "worker.cbl"})

    def test_explicit_full_verification_detects_edit_with_preserved_size_and_mtime(self):
        path = self.write("main.cbl", program("MAIN", "ALPHAMARK"))
        self.intake()
        original = path.stat()
        path.write_text(program("MAIN", "GAMMAMARK"), encoding="utf-8")
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        refreshed = self.intake(verify_content=True)
        self.assertEqual(refreshed["build_report"]["files"]["content_hash_verified"], 1)
        self.assertEqual(refreshed["repository_search"]["content_hash_verified"], 1)
        self.assertEqual(discover_repository(self.database, "GAMMAMARK")["matched_file_count"], 1)
        self.assertEqual(discover_repository(self.database, "ALPHAMARK")["matched_file_count"], 0)

    def test_missing_persisted_evidence_rebuilds_text_even_when_metadata_matches(self):
        self.write("main.cbl", program("MAIN", "TRACEABLE-TOPIC"))
        self.intake()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DELETE FROM evidence_spans WHERE evidence_id IN (SELECT evidence_id FROM repo_pages)")
            connection.commit()
        with patch("repository_discovery._verified_pages", wraps=repository_discovery._verified_pages) as pages:
            refreshed = ensure_repository_search(self.database, self.source)
        self.assertEqual(pages.call_count, 1)
        self.assertEqual(refreshed["updated_files"], 1)
        self.assertEqual(discover_repository(self.database, "TRACEABLE-TOPIC")["matched_file_count"], 1)

    def test_forced_search_verification_rejects_changed_content_with_preserved_metadata(self):
        path = self.write("main.cbl", program("MAIN", "ALPHAMARK"))
        self.intake()
        original = path.stat()
        path.write_text(program("MAIN", "GAMMAMARK"), encoding="utf-8")
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, "SOURCE_HASH_MISMATCH"):
            ensure_repository_search(self.database, self.source, verify_content=True)
        with self.assertRaisesRegex(ValueError, "REPOSITORY_SEARCH_STALE"):
            discover_repository(self.database, "ALPHAMARK")

    def test_legacy_rule_keys_remain_joinable_and_are_replaced_only_when_file_changes(self):
        self.write("main.cbl", program("MAIN", "RULEMARK", "COMPUTE RESULT-VALUE = INPUT-VALUE * 2.\n"))
        self.intake()
        with closing(sqlite3.connect(self.database)) as connection:
            current = connection.execute("SELECT rule_id FROM business_rules").fetchone()[0]
            legacy = "rule_" + current.rsplit("_", 1)[-1]
            connection.execute("UPDATE business_rules SET rule_id=? WHERE rule_id=?", (legacy, current))
            connection.execute("UPDATE business_rule_fields SET rule_id=? WHERE rule_id=?", (legacy, current))
            connection.commit()
        warm = self.intake()
        self.assertEqual(warm["build_report"]["files"]["indexed_or_updated"], 0)
        with closing(sqlite3.connect(self.database)) as connection:
            fields = connection.execute("SELECT b.rule_id,f.field_name FROM business_rules b JOIN business_rule_fields f "
                                        "ON f.rule_id=b.rule_id WHERE f.field_role='write'").fetchall()
        self.assertIn((legacy, "RESULT-VALUE"), fields)
        self.write("main.cbl", program("MAIN", "RULEMARK", "COMPUTE RESULT-VALUE = INPUT-VALUE * 3.\n"))
        self.intake()
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM business_rules WHERE rule_id=?", (legacy,)).fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM business_rule_fields f LEFT JOIN business_rules b "
                                                "ON f.rule_id=b.rule_id WHERE b.rule_id IS NULL").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
