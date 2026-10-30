from __future__ import annotations

from contextlib import closing
import hashlib
import json
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
from source_session import QuestionSourceSession, archive_evidence, read_archived_evidence, refresh_selected_sources
from source_catalog import refresh_source_catalog
from source_reading import _identify_page


class SourceSessionTests(unittest.TestCase):
    def test_archived_truncation_round_trips_with_copy_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "index.sqlite"
            for truncated in (False, True):
                with self.subTest(truncated=truncated):
                    page = {"relative_path": "shared.cpy", "source_sha256": "a" * 64,
                            "start_line": 3, "end_line": 3, "source_text": "MOVE 1 TO RESULT-VALUE",
                            "span_truncated": truncated, "include_chain": [],
                            "copy_instance": f"instance-{truncated}",
                            "replacement_trace": [{"from": "INPUT", "to": "RESULT"}]}
                    page["evidence_id"] = _identify_page(page)
                    archive_evidence(database, [page])
                    self.assertEqual(read_archived_evidence(database, page["evidence_id"]), page)

    def test_archive_can_add_but_never_clear_a_same_id_truncation_boundary(self):
        for earlier in ({}, {"span_truncated": False}):
            with self.subTest(earlier=earlier), tempfile.TemporaryDirectory() as folder:
                database = Path(folder) / "index.sqlite"
                page = {"relative_path": "entry.cbl", "source_sha256": "a" * 64,
                        "start_line": 3, "end_line": 3, "source_text": "CALL 'SERVICE' USING",
                        "include_chain": []}
                page["evidence_id"] = _identify_page(page)
                archive_evidence(database, [{**page, **earlier}])
                truncated = {**page, "span_truncated": True}
                self.assertEqual(_identify_page(truncated), page["evidence_id"])
                archive_evidence(database, [truncated])
                self.assertEqual(read_archived_evidence(database, page["evidence_id"]), truncated)
                for later in ({}, {"span_truncated": False}, {"span_truncated": True}):
                    archive_evidence(database, [{**page, **later}])
                    self.assertEqual(read_archived_evidence(database, page["evidence_id"]), truncated)

    def test_archived_copy_instances_round_trip_without_changing_legacy_chain_records(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "index.sqlite"
            base = {"relative_path": "shared.cpy", "source_sha256": "a" * 64,
                    "start_line": 3, "end_line": 3, "source_text": "MOVE 1 TO RESULT-VALUE."}
            chain = [{"relative_path": "entry.cbl", "start_line": 8}]
            legacy = {**base, "include_chain": chain, "evidence_id": "ev_page_586b3aa2ff27323d85118707"}
            archive_evidence(database, [legacy])
            with closing(sqlite3.connect(Path(folder) / "versioned-evidence.sqlite")) as db:
                stored = db.execute("SELECT include_chain_json FROM excerpts WHERE evidence_id=?",
                                    (legacy["evidence_id"],)).fetchone()[0]
                self.assertEqual(json.loads(stored), chain)
            self.assertEqual(read_archived_evidence(database, legacy["evidence_id"]), legacy)
            pages = []
            for instance, replacement in (("copy-first", "FIRST-VALUE"), ("copy-second", "SECOND-VALUE")):
                page = {**base, "include_chain": tuple(chain), "copy_instance": instance,
                        "replacement_trace": ({"old": ("SOURCE-VALUE",), "new": (replacement,)},)}
                page["evidence_id"] = _identify_page(page)
                pages.append(page)
            archive_evidence(database, pages)
            self.assertNotEqual(pages[0]["evidence_id"], pages[1]["evidence_id"])
            for page in pages:
                self.assertEqual(read_archived_evidence(database, page["evidence_id"]),
                                 json.loads(json.dumps(page)))
            self.assertEqual(read_archived_evidence(database, legacy["evidence_id"]), legacy)

    def test_capture_cannot_refresh_an_equal_content_snapshot_rebuilt_with_new_configuration(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = root / "source"
                source.mkdir()
                path = source / "entry.cbl"
                original = "PROGRAM-ID. ENTRY.\nPROCEDURE DIVISION.\n>>IF BATCH\nMOVE 1 TO RESULT-VALUE.\n>>END-IF\n"
                original_bytes = original.replace("\n", newline).encode("utf-8")
                path.write_bytes(original_bytes)
                database = root / "index.sqlite"
                build_business_index(source, database, source_format="free", verify_content=True,
                                     compilation_context={"compile_conditions": {"BATCH": False}})
                overview = ensure_repository_search(database, source)
                with QuestionSourceSession(database, source) as session:
                    path.write_bytes(original_bytes + ("*> pending source edit" + newline).encode("utf-8"))
                    captured = session.capture("entry.cbl")
                    path.write_bytes(original_bytes)
                    rebuilt = build_business_index(source, database, source_format="free", verify_content=True,
                        compilation_context={"compile_conditions": {"BATCH": True}})
                    ensure_repository_search(database, source)
                    self.assertEqual(rebuilt["snapshot_id"], overview["snapshot_id"])
                    with self.assertRaisesRegex(ValueError, "SNAPSHOT_CHANGED"):
                        refresh_selected_sources(database, [captured], expected_snapshot_id=overview["snapshot_id"])
                with closing(sqlite3.connect(database)) as db:
                    self.assertEqual(db.execute("SELECT sha256 FROM source_files WHERE relative_path='entry.cbl'").fetchone()[0],
                                     hashlib.sha256(original_bytes).hexdigest())

    def test_local_refresh_cannot_mix_current_facts_into_a_legacy_parser_index(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            path = source / "entry.cbl"
            path.write_text("PROGRAM-ID. ENTRY.\nPROCEDURE DIVISION.\nGOBACK.\n", encoding="utf-8")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", verify_content=True)
            overview = ensure_repository_search(database, source)
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("UPDATE metadata SET value='business-sparse-v1.9' WHERE key='parser_version'")
            path.write_text(path.read_text() + "*> pending source edit\n", encoding="utf-8")
            with QuestionSourceSession(database, source) as session, self.assertRaisesRegex(ValueError, "SNAPSHOT_CHANGED"):
                refresh_selected_sources(database, [session.capture("entry.cbl")],
                                         expected_snapshot_id=overview["snapshot_id"])
            self.assertEqual(repository_search_overview(database, source)["snapshot_id"], overview["snapshot_id"])

    def test_local_refresh_preserves_saved_compilation_conditions_and_copy_configuration(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = root / "source"
                source.mkdir()
                path = source / "entry.cbl"
                path.write_text("PROGRAM-ID. ENTRY.\nPROCEDURE DIVISION.\n"
                    ">>IF BATCH\nMOVE 1 TO ACTIVE-VALUE.\n>>ELSE\nMOVE 2 TO OTHER-VALUE.\n>>END-IF\n",
                    encoding="utf-8")
                database = root / "index.sqlite"
                context = {"compile_conditions": {"BATCH": enabled}, "conditions_complete": True,
                    "copy_libraries": ["custom-copybooks"], "copy_library_order_known": True,
                    "framework_version": "local-rules-v1", "nested_copy_replacing": True}
                build_business_index(source, database, source_format="free", verify_content=True,
                                     compilation_context=context)
                overview = ensure_repository_search(database, source)
                with closing(sqlite3.connect(database)) as db:
                    saved = db.execute("SELECT value FROM metadata WHERE key='source_options'").fetchone()[0]
                    expected = db.execute("SELECT normalized_text,parse_status FROM business_rules "
                        "JOIN business_rule_occurrences USING(rule_id) ORDER BY first_line").fetchall()
                path.write_text(path.read_text() + "*> changed source\n", encoding="utf-8")
                from business_index import _Facts
                with QuestionSourceSession(database, source) as session, mock.patch(
                        "business_index._Facts", wraps=_Facts) as fact_builder:
                    refresh_selected_sources(database, [session.capture("entry.cbl")],
                                             expected_snapshot_id=overview["snapshot_id"])
                with closing(sqlite3.connect(database)) as db:
                    actual = db.execute("SELECT normalized_text,parse_status FROM business_rules "
                        "JOIN business_rule_occurrences USING(rule_id) ORDER BY first_line").fetchall()
                    self.assertEqual(actual, expected)
                    self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='source_options'").fetchone()[0], saved)
                self.assertEqual(fact_builder.call_args.kwargs["compilation_context"].to_dict(),
                                 json.loads(saved)["compilation_context"])

    def test_local_refresh_preserves_extensionless_procedure_copy_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            (source / "entry.cbl").write_text("PROGRAM-ID. ENTRY.\nPROCEDURE DIVISION.\nCOPY CONTROL.\n", encoding="utf-8")
            control = source / "CONTROL"
            control.write_text("CONTROL-FLOW.\nMOVE 1 TO RESULT-VALUE.\n", encoding="utf-8")
            database = root / "index.sqlite"
            catalog = refresh_source_catalog(source, root / "catalog.sqlite", source_format="free")
            build_business_index(source, database, source_format="free", verify_content=True,
                include_extensionless=True, catalog=catalog, entry_program="ENTRY")
            overview = ensure_repository_search(database, source)
            control.write_text(control.read_text().replace("MOVE 1", "MOVE 2"), encoding="utf-8")
            with QuestionSourceSession(database, source) as session:
                refreshed = refresh_selected_sources(database, [session.capture("CONTROL")],
                                                     expected_snapshot_id=overview["snapshot_id"])
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT artifact_kind FROM source_files WHERE relative_path='CONTROL'").fetchone()[0], "copybook")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM symbols WHERE relative_path='CONTROL' AND symbol_type='Copybook'").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT status FROM relations WHERE relation_type='INCLUDES_COPY'").fetchone()[0], "confirmed")
            control.write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. NEW-PROGRAM.\n"
                               "PROCEDURE DIVISION.\nGOBACK.\n", encoding="utf-8")
            with QuestionSourceSession(database, source) as session:
                refresh_selected_sources(database, [session.capture("CONTROL")],
                                         expected_snapshot_id=refreshed["snapshot_id"])
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT artifact_kind FROM source_files WHERE relative_path='CONTROL'").fetchone()[0], "program")
                self.assertEqual(db.execute("SELECT symbol_type,name FROM symbols WHERE relative_path='CONTROL'").fetchall(),
                                 [("Program", "NEW-PROGRAM")])
                self.assertEqual(db.execute("SELECT status FROM relations WHERE relation_type='INCLUDES_COPY'").fetchone()[0], "unresolved")
            self.assertIn({"program_name": "NEW-PROGRAM", "relative_path": "CONTROL"},
                          repository_search_overview(database, source)["program_samples"])

    def test_local_refresh_recomputes_text_completeness_and_program_overview(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            path = source / "entry.cbl"
            original = "PROGRAM-ID. ENTRY.\nPROCEDURE DIVISION.\nGOBACK.\n"
            path.write_text(original, encoding="utf-8")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", verify_content=True)
            overview = ensure_repository_search(database, source)
            for text, complete, name in ((original.replace("ENTRY", "REVISED") + "*> " + "x" * 13000 + "\n", False, "REVISED"),
                                         (original, True, "ENTRY")):
                with self.subTest(complete=complete):
                    path.write_text(text, encoding="utf-8")
                    with QuestionSourceSession(database, source) as session:
                        refresh_selected_sources(database, [session.capture("entry.cbl")],
                                                 expected_snapshot_id=overview["snapshot_id"])
                    overview = repository_search_overview(database, source)
                    self.assertEqual(overview["full_text_complete"], complete)
                    self.assertEqual(overview["program_samples"], [{"program_name": name, "relative_path": "entry.cbl"}])
                    self.assertEqual(bool(overview["boundaries"]), not complete)

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
            with closing(sqlite3.connect(database)) as db, db:
                other_before = db.execute("SELECT sha256 FROM source_files WHERE relative_path='other.cbl'").fetchone()[0]
                old_count = db.execute("SELECT COUNT(*) FROM business_rules WHERE relative_path='change.cbl' AND normalized_text LIKE '%OLD-LEVEL%'").fetchone()[0]
            self.assertGreater(old_count, 0)
            stat = changed.stat()
            changed.write_text(changed.read_text().replace("OLD-LEVEL", "NEW-LEVEL"), encoding="utf-8")
            os.utime(changed, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            captured_digest = hashlib.sha256(changed.read_bytes()).hexdigest()
            with QuestionSourceSession(database, source) as session:
                capture = session.capture("change.cbl")
                first_path = capture.path
                changed.write_text(changed.read_text().replace("NEW-LEVEL", "LATE-LEVEL"), encoding="utf-8")
                self.assertIs(session.capture("change.cbl"), capture)
                self.assertIn(b"NEW-LEVEL", first_path.read_bytes())
                refreshed = refresh_selected_sources(database, [capture], expected_snapshot_id=overview["snapshot_id"])
            self.assertEqual(refreshed["updated"], ["change.cbl"])
            self.assertNotEqual(refreshed["snapshot_id"], overview["snapshot_id"])
            with closing(sqlite3.connect(database)) as db, db:
                self.assertEqual(db.execute("SELECT sha256 FROM source_files WHERE relative_path='other.cbl'").fetchone()[0], other_before)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM business_rules WHERE relative_path='change.cbl' AND normalized_text LIKE '%OLD-LEVEL%'").fetchone()[0], 0)
                self.assertGreater(db.execute("SELECT COUNT(*) FROM business_rules WHERE relative_path='change.cbl' AND normalized_text LIKE '%NEW-LEVEL%'").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM repo_fts WHERE repo_fts MATCH ?", ('"old-level"',)).fetchone()[0], 0)
                self.assertGreater(db.execute("SELECT COUNT(*) FROM repo_fts WHERE repo_fts MATCH ?", ('"new-level"',)).fetchone()[0], 0)
            self.assertEqual(repository_search_overview(database, source)["snapshot_id"], refreshed["snapshot_id"])
            # A later disk edit does not rewrite this question's captured version.
            self.assertEqual(captured_digest, capture.sha256)

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
            with closing(sqlite3.connect(database)) as db, db:
                before = db.execute("SELECT status,target_entity_id FROM relations WHERE relative_path='caller.cbl' AND relation_type='CALLS'").fetchone()
            self.assertEqual(before[0], "confirmed")
            self.assertIsNotNone(before[1])
            alternate.write_text("PROGRAM-ID. TARGET.\n", encoding="utf-8")
            with QuestionSourceSession(database, source) as session:
                refreshed = refresh_selected_sources(database, [session.capture("alternate.cbl")],
                    expected_snapshot_id=overview["snapshot_id"])
            with closing(sqlite3.connect(database)) as db, db:
                after = db.execute("SELECT status,target_entity_id FROM relations WHERE relative_path='caller.cbl' AND relation_type='CALLS'").fetchone()
                self.assertEqual(db.execute("SELECT sha256 FROM source_files WHERE relative_path='target.cbl'").fetchone()[0],
                    hashlib.sha256((source / "target.cbl").read_bytes()).hexdigest())
            self.assertEqual(after[0], "candidate")
            self.assertIsNone(after[1])
            self.assertNotEqual(refreshed["snapshot_id"], overview["snapshot_id"])


if __name__ == "__main__":
    unittest.main()
