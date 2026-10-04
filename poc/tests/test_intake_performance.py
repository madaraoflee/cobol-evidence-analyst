"""Regression checks for complete, incremental repository intake."""

from contextlib import closing
from pathlib import Path
import os
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyze_source import AnalysisCancelled, analyze_source
from repository_discovery import discover_repository, ensure_repository_search
import business_index
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

    def test_rule_batches_keep_fields_and_repeated_occurrences_without_reparsing(self):
        self.write("main.cbl", program("MAIN", "RULE-BATCH") +
                   "COMPUTE BILL-AMOUNT = BASE-AMOUNT * RATE.\n" * 20 +
                   "".join(f"MOVE INPUT-{index} TO OUTPUT-{index}.\n" for index in range(2050)) +
                   "COMPUTE BILL-AMOUNT = BASE-AMOUNT * RATE.\n")
        with patch("business_index._extract_data_access", wraps=business_index._extract_data_access) as extract:
            self.intake()
        # The repeated rule before the flush is parsed once; the occurrence
        # after the flush still merges correctly through the persisted key.
        self.assertEqual(extract.call_count, 2052)
        with closing(sqlite3.connect(self.database)) as db:
            rows = db.execute("SELECT rule_id,occurrence_count,reads_json,writes_json,condition_json "
                              "FROM business_rules").fetchall()
            expected = {(key, name, role) for key, _, reads, writes, conditions in rows
                        for role, encoded in (("read", reads), ("write", writes), ("condition", conditions))
                        for name in json.loads(encoded)}
            self.assertEqual(set(db.execute("SELECT * FROM business_rule_fields")), expected)
            self.assertEqual(sorted(row[1] for row in rows).count(21), 1)

    def test_changed_files_remove_old_search_rows_in_one_batch(self):
        for index in range(8):
            self.write(f"member-{index}.cbl", program(f"MEMBER-{index}", "ORIGINALMARK") +
                       f"01 ORIGINAL-FIELD-{index} PIC 9.\n")
        self.intake()
        for index in range(7):
            self.write(f"member-{index}.cbl", program(f"MEMBER-{index}", "REPLACEDMARK") +
                       f"01 REPLACED-FIELD-{index} PIC 9.\n")
        (self.source / "member-7.cbl").unlink()
        statements = []
        original_connect = business_index._connect

        def observe(*args, **kwargs):
            db = original_connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db

        with patch("business_index._connect", side_effect=observe):
            self.intake()
        deletes = [sql for sql in statements if sql.startswith("DELETE FROM code_units_fts WHERE")]
        self.assertEqual(len(deletes), 1)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM code_units_fts f LEFT JOIN code_units u "
                                        "ON u.unit_id=f.unit_id WHERE u.unit_id IS NULL").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM code_units_fts").fetchone()[0],
                             db.execute("SELECT COUNT(*) FROM code_units").fetchone()[0])
        self.assertEqual(discover_repository(self.database, "ORIGINALMARK")["matched_file_count"], 0)
        self.assertEqual(discover_repository(self.database, "REPLACEDMARK")["matched_file_count"], 7)

    def test_incremental_resolution_updates_inbound_ambiguity_and_removal(self):
        self.write("caller.cbl", program("CALLER", "CALLER", 'CALL "TARGET".\n'))
        self.write("target.cbl", program("TARGET", "TARGET"))
        self.write("other.cbl", program("OTHER", "OTHER", "PERFORM SHARED-WORK.\n") + "SHARED-WORK.\nEXIT.\n")
        self.intake()

        def status():
            with closing(sqlite3.connect(self.database)) as db:
                return db.execute("SELECT status FROM relations WHERE relative_path='caller.cbl' "
                                  "AND relation_type='CALLS'").fetchone()[0]

        self.assertEqual(status(), "confirmed")
        self.write("duplicate.cbl", program("TARGET", "DUPLICATE"))
        with patch("business_index._resolve_relations", wraps=business_index._resolve_relations) as resolve:
            self.intake()
        self.assertEqual(status(), "candidate")
        self.assertEqual(resolve.call_args.kwargs["affected_paths"], {"target.cbl", "duplicate.cbl"})
        self.assertEqual(resolve.call_args.kwargs["affected_target_names"], {"TARGET"})
        self.write("duplicate.cbl", program("RENAMED-TARGET", "RENAMED"))
        self.intake()
        self.assertEqual(status(), "confirmed")
        (self.source / "target.cbl").unlink()
        self.intake()
        self.assertEqual(status(), "unresolved")

    def test_incremental_copy_aliases_preserve_ambiguity_and_qualified_boundaries(self):
        self.write("main.cbl", program("MAIN", "COPY-ALIAS", 'COPY "shared.cpy".\nCOPY SHARED OF LIBRARY.\n'))
        (self.source / "first").mkdir()
        (self.source / "second").mkdir()
        self.write("first/shared.cpy", "01 SHARED-RECORD PIC 9.\n")
        self.intake()

        def statuses():
            with closing(sqlite3.connect(self.database)) as db:
                return dict(db.execute("SELECT target_name,status FROM relations WHERE relation_type='INCLUDES_COPY'"))

        self.assertEqual(statuses(), {"SHARED.CPY": "confirmed", "SHARED": "candidate"})
        self.write("second/shared.cpy", "01 ANOTHER-RECORD PIC 9.\n")
        self.intake()
        self.assertEqual(statuses(), {"SHARED.CPY": "candidate", "SHARED": "candidate"})
        (self.source / "first/shared.cpy").unlink()
        self.intake()
        self.assertEqual(statuses(), {"SHARED.CPY": "confirmed", "SHARED": "candidate"})

    def test_preparing_index_builds_path_indexes_for_incremental_deletion(self):
        self.write("main.cbl", program("MAIN", "PRIMARYMARK"))
        events = []
        self.intake(progress=events.append)
        self.assertTrue(any(event.get("phase") == "preparing_index" for event in events))
        with closing(sqlite3.connect(self.database)) as db:
            for table, index in (("symbols", "idx_symbols_path"), ("evidence_spans", "idx_evidence_path")):
                plan = " ".join(str(row) for row in db.execute(
                    f"EXPLAIN QUERY PLAN DELETE FROM {table} WHERE relative_path=?", ("main.cbl",)))
                self.assertIn(index, plan)
                self.assertIn("SEARCH", plan)

    def test_copybook_change_updates_a_program_with_the_same_stored_scope(self):
        self.write("main.cbl", "IDENTIFICATION DIVISION.\nPROGRAM-ID. SHARED.\n"
                   "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 TARGET-NAME PIC X(8).\n"
                   "PROCEDURE DIVISION.\nCALL TARGET-NAME.\nGOBACK.\n")
        self.write("shared.cpy", "01 TARGET-NAME PIC X(8).\n")
        self.intake()
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT status FROM relations WHERE relation_type='CALL_TARGET_FROM'")
                             .fetchone()[0], "candidate")
        self.write("shared.cpy", "01 OTHER-NAME PIC X(8).\n")
        self.intake()
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT status FROM relations WHERE relation_type='CALL_TARGET_FROM'")
                             .fetchone()[0], "confirmed")

    def test_preparing_index_sql_can_be_cancelled_before_reading_source(self):
        self.write("main.cbl", program("MAIN", "PRIMARYMARK"))
        original_schema = business_index._ensure_schema
        preparing = False

        def expensive_schema(db):
            nonlocal preparing
            original_schema(db)
            preparing = True
            db.execute("WITH RECURSIVE numbers(n) AS (VALUES(0) UNION ALL "
                       "SELECT n+1 FROM numbers WHERE n<100000) SELECT SUM(n) FROM numbers").fetchone()

        def cancel_preparation():
            if preparing:
                raise AnalysisCancelled("cancelled while preparing source indexes")

        with patch("business_index._ensure_schema", side_effect=expensive_schema), \
             patch("business_index._verify_file", side_effect=AssertionError("source read after cancellation")):
            with self.assertRaisesRegex(AnalysisCancelled, "preparing source indexes"):
                business_index.build_business_index(self.source, self.database,
                    source_format="free", check_cancel=cancel_preparation, quiet=True)

    def test_batched_search_deletion_rolls_back_if_replacement_fails(self):
        self.write("first.cbl", program("FIRST", "PRIORFIRST"))
        self.write("second.cbl", program("SECOND", "PRIORSECOND"))
        self.intake()
        with closing(sqlite3.connect(self.database)) as db:
            old_rows = db.execute("SELECT rowid,* FROM code_units_fts ORDER BY rowid").fetchall()
        self.write("first.cbl", program("REPLACED-FIRST", "AFTERFIRST"))
        self.write("second.cbl", program("REPLACED-SECOND", "AFTERSECOND"))
        original = business_index._Facts.finish

        def fail_second(facts):
            if facts.relative == "second.cbl":
                raise ValueError("SYNTHETIC_REPLACEMENT_FAILURE")
            return original(facts)

        with patch("business_index._Facts.finish", new=fail_second):
            report = self.intake()
        self.assertFalse(report["source_manifest_verified"])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT rowid,* FROM code_units_fts ORDER BY rowid").fetchall(), old_rows)

    def test_framework_stage_candidates_use_existing_definition_lookup(self):
        from framework_semantics import _candidate_paths
        self.write("main.cbl", program("MAIN", "STAGE") + "RUN-WORK.\nGOBACK.\n")
        self.intake()
        with closing(sqlite3.connect(self.database)) as db:
            statements = []
            db.set_trace_callback(statements.append)
            paths = _candidate_paths(db, {"rules": [{"kind": "section", "symbol": "run-work"}]})
        self.assertEqual(paths, {"main.cbl"})
        self.assertFalse(any("FROM code_units" in sql for sql in statements))

    def test_business_benchmark_contains_large_program_and_resolved_dependencies(self):
        from benchmark_intake import run_benchmark
        report = run_benchmark(2, 100, workload="business", longest_program_lines=500)
        self.assertEqual(report["indexed_lines_after_edit"], 100 + 500 + 2 * 9 + 1)
        self.assertEqual(report["confirmed_call_edges"], 2)
        self.assertGreaterEqual(report["relation_count"], 2 * 13)
        self.assertTrue(report["full_text_complete"])
        self.assertTrue(report["deep_tail_retrieved"])
        self.assertEqual(report["warm_search"]["cached_files"], 4)

    def test_directory_discovery_reports_progress_before_traversal_and_path_validation(self):
        self.write("main.cbl", program("MAIN", "PRIMARYMARK"))
        self.write("worker.cbl", program("WORKER", "SECONDARYMARK"))
        events = []
        original_iterator = business_index.iter_source_files
        original_safe_path = business_index._safe_path

        def observed_iterator(*args, **kwargs):
            self.assertTrue(events, "directory traversal must not start without progress")
            self.assertEqual(events[0]["phase"], "discovery")
            self.assertEqual(events[0]["completed"], 0)
            self.assertIsNone(events[0]["total"])
            yield from original_iterator(*args, **kwargs)

        def observed_validation(*args, **kwargs):
            self.assertTrue(any(event.get("stage") == "validating" for event in events))
            return original_safe_path(*args, **kwargs)

        with patch("business_index.iter_source_files", side_effect=observed_iterator), \
             patch("business_index._safe_path", side_effect=observed_validation):
            report = business_index.build_business_index(self.source, self.database,
                source_format="free", progress=events.append, framework_reference_path="", quiet=True)
        self.assertEqual(report["diagnostics"]["program_count"], 2)
        listing = [event for event in events if event.get("stage") == "listing"]
        self.assertTrue(all(event["total"] is None for event in listing))
        validation = [event for event in events if event.get("stage") == "validating"]
        self.assertEqual(validation[-1]["completed"], 2)
        self.assertEqual(validation[-1]["total"], 2)

    def test_discovery_can_be_cancelled_before_any_source_body_is_read(self):
        self.write("main.cbl", program("MAIN", "PRIMARYMARK"))
        self.write("worker.cbl", program("WORKER", "SECONDARYMARK"))
        for stage in ("listing", "validating"):
            with self.subTest(stage=stage):
                cancelled = False

                def progress(event):
                    nonlocal cancelled
                    if event.get("stage") == stage and event.get("completed") == 1:
                        cancelled = True

                def check_cancel():
                    if cancelled:
                        raise AnalysisCancelled("discovery cancelled")

                with patch("business_index._verify_file", side_effect=AssertionError("source body read")):
                    with self.assertRaises(AnalysisCancelled):
                        business_index.build_business_index(self.source, self.database,
                            source_format="free", progress=progress, check_cancel=check_cancel,
                            framework_reference_path="", quiet=True)
                self.assertFalse(self.database.exists())

    def test_relation_lookup_index_exists_before_framework_checks(self):
        self.write("main.cbl", program("MAIN", "PRIMARYMARK", 'CALL "UNAVAILABLE".\n'))
        import framework_semantics
        refresh = framework_semantics.refresh_framework_index
        calls = []

        def check_index(connection, *args, **kwargs):
            calls.append(True)
            columns = [row[2] for row in connection.execute("PRAGMA index_info(repo_relations_path)")]
            self.assertEqual(columns, ["relative_path", "relation_type"])
            plan = connection.execute("EXPLAIN QUERY PLAN SELECT * FROM relations "
                "WHERE relative_path=? AND relation_type='INCLUDES_COPY'", ("main.cbl",)).fetchall()
            self.assertTrue(any("repo_relations_path" in row[3] and "SEARCH" in row[3] for row in plan))
            return refresh(connection, *args, **kwargs)

        with patch("framework_semantics.refresh_framework_index", side_effect=check_index):
            report = self.intake()
        self.assertEqual(calls, [True])
        self.assertTrue(report["source_manifest_verified"])

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

    def test_many_matching_files_do_not_rescan_a_full_diverse_preview(self):
        for index in range(40):
            self.write(f"member-{index:02}.cbl", program(f"MEMBER-{index}", "SHARED-TOPIC"))
        self.intake()
        original_max = max
        preview_scans = []

        def observed_max(values, *args, **kwargs):
            if isinstance(values, dict):
                preview_scans.append(len(values))
            return original_max(values, *args, **kwargs)

        with patch("repository_discovery.MAX_MATCHED_PAGES", 4), \
             patch("repository_discovery.max", observed_max, create=True):
            result = discover_repository(self.database, "SHARED-TOPIC")
        self.assertEqual(result["matched_file_count"], 40)
        self.assertEqual(len(result["selected_paths"]), 40)
        self.assertEqual(len(result["matched_pages"]), 4)
        self.assertEqual(len({page["relative_path"] for page in result["matched_pages"]}), 4)
        self.assertEqual(preview_scans, [])

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
