"""Bounded import, incremental scope indexing, progress and cancellation."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyze_source import AnalysisCancelled, analyze_source
from structural_index import build_structural_index
from web_app import WorkbenchState


def source_text(name: str, extra: str = "") -> str:
    return f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n01 V PIC 9.\n{extra}\nPROCEDURE DIVISION.\nADD 1 TO V.\nGOBACK.\n"


class LargeIntakeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "sources"
        self.source.mkdir()
        self.output = self.root / "output"
        (self.source / "main.cbl").write_text(source_text("MAIN-PROGRAM", "COPY UNAVAILABLE-AREA."))
        (self.source / "unrelated.cbl").write_text(source_text("OTHER-PROGRAM"))

    def test_catalog_import_never_invokes_full_parser_and_has_no_evidence_authority(self):
        with mock.patch("analyze_source.build_structural_index", side_effect=AssertionError("full parse on import")):
            report = analyze_source(self.source, self.output, index_mode="catalog")
        self.assertTrue(report["catalog_ready"])
        self.assertEqual(report["runner_status"], "INDEX_READY")
        self.assertEqual(report["program_count"], 2)
        self.assertFalse(report["source_manifest_verified"])
        self.assertFalse((self.output / "structural-index.sqlite").exists())

    def test_question_builds_only_selected_entry_and_preserves_whole_catalog(self):
        with mock.patch("analyze_source.build_structural_index", wraps=build_structural_index) as build:
            report = analyze_source(self.source, self.output, index_mode="catalog", entry="MAIN-PROGRAM", question="Explain")
        self.assertEqual(build.call_args.kwargs["max_source_bytes"], 16 * 1024 * 1024)
        self.assertEqual(report["question_status"], "NETWORK_DISABLED")
        self.assertEqual(report["scope"]["detail_file_count"], 1)
        self.assertEqual(report["scope"]["max_scope_bytes"], 16 * 1024 * 1024)
        self.assertEqual(report["scope"]["budget_kind"], "source_input_bytes")
        self.assertFalse(report["scope"]["memory_usage_bounded"])
        self.assertEqual(report["program_count"], 2)
        self.assertTrue(report["unresolved_dependencies"])
        self.assertTrue(report["source_manifest_verified"])
        with sqlite3.connect(self.output / "structural-index.sqlite") as connection:
            self.assertEqual(connection.execute("SELECT relative_path FROM source_files").fetchall(), [("main.cbl",)])

    def test_runner_rejects_entry_over_16_mib_before_detailed_parse(self):
        with (self.source / "main.cbl").open("r+b") as handle:
            handle.truncate(16 * 1024 * 1024 + 1)
        with mock.patch("analyze_source.build_structural_index", side_effect=AssertionError("large entry must not be parsed")):
            report = analyze_source(self.source, self.output, index_mode="catalog", entry="main.cbl", question="Explain")
        self.assertTrue(report["catalog_ready"])
        self.assertEqual(report["reason_code"], "ENTRY_SCOPE_LIMIT")
        self.assertEqual(report["question_status"], "SCOPE_LIMIT")
        self.assertFalse(report["source_manifest_verified"])
        self.assertEqual(report["scope"]["max_scope_bytes"], 16 * 1024 * 1024)
        self.assertTrue(any("16 MiB" in message and "不是内存上限" in message for message in report["messages"]))


    def test_duplicate_program_path_remains_selectable_after_detailed_analysis(self):
        (self.source / "other-version.cbl").write_text(source_text("MAIN-PROGRAM"))
        first = analyze_source(self.source, self.output, index_mode="catalog", entry="main.cbl", question="Explain")
        self.assertTrue(first["source_manifest_verified"])
        entries = json.loads((self.output / "programs.json").read_text())["programs"]
        selected = next(item for item in entries if item["relative_path"] == "main.cbl")
        self.assertIn("::MAIN-PROGRAM::", selected["entry_key"])
        second = analyze_source(self.source, self.output, index_mode="catalog", entry=selected["entry_key"], question="Explain")
        self.assertTrue(second["source_manifest_verified"])
        self.assertEqual(second["selected_entry"]["relative_path"], "main.cbl")


    def test_unchanged_scope_skips_decode_parse_and_derived_rebuild(self):
        database = self.output / "structural-index.sqlite"
        first = build_structural_index(self.source, database, include_paths=["main.cbl"], quiet=True)
        with mock.patch("structural_index.read_source_document", side_effect=AssertionError("unchanged decode")), \
             mock.patch("structural_index.rebuild_copy_expansions", side_effect=AssertionError("unchanged derived rebuild")):
            second = build_structural_index(self.source, database, include_paths=["main.cbl"], quiet=True)
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(second["files"]["skipped_unchanged"], 1)

    def test_force_verification_rereads_unchanged_source(self):
        database = self.output / "structural-index.sqlite"
        build_structural_index(self.source, database, include_paths=["main.cbl"], quiet=True)
        import structural_index
        with mock.patch("structural_index.read_source_document", wraps=structural_index.read_source_document) as read:
            build_structural_index(self.source, database, include_paths=["main.cbl"], quiet=True, verify_content=True)
        self.assertEqual(read.call_count, 1)

    def test_cancellation_during_parse_rolls_back_previous_index(self):
        database = self.output / "structural-index.sqlite"
        first = build_structural_index(self.source, database, include_paths=["main.cbl"], quiet=True)
        (self.source / "main.cbl").write_text(source_text("CHANGED-PROGRAM"))
        def progress(event):
            if event["phase"] == "parsing":
                raise AnalysisCancelled("test cancellation")
        with self.assertRaises(AnalysisCancelled):
            build_structural_index(self.source, database, include_paths=["main.cbl"], quiet=True, progress=progress)
        with sqlite3.connect(database) as connection:
            self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key='snapshot_id'").fetchone()[0], first["snapshot_id"])
            self.assertEqual(connection.execute("SELECT name FROM symbols WHERE symbol_type='Program'").fetchone()[0], "MAIN-PROGRAM")

    def test_fresh_scope_size_budget_is_checked_before_content_reads(self):
        with mock.patch("structural_index.read_source_document", side_effect=AssertionError("budget must be checked first")):
            with self.assertRaisesRegex(ValueError, "byte budget"):
                build_structural_index(self.source, self.output / "index.sqlite", include_paths=["main.cbl"], max_source_bytes=10)


    def test_scope_path_escape_rejected(self):
        outside = self.root / "outside.cbl"
        outside.write_text(source_text("OUTSIDE"))
        with self.assertRaises(ValueError):
            build_structural_index(self.source, self.output / "index.sqlite", include_paths=["../outside.cbl"])

    def test_job_progress_survives_state_refresh_and_cancel_does_not_restore_old_answer(self):
        started, release = threading.Event(), threading.Event()
        def delayed(source, output, **options):
            options["progress"]({"phase": "catalog", "completed": 3, "total": 10,
                "unit": "files", "current_file": "main.cbl", "bytes_completed": 20, "bytes_total": 100})
            started.set()
            release.wait(3)
            options["check_cancel"]()
            return analyze_source(source, output, **options)
        app = WorkbenchState(analyzer=delayed)
        job = app.start({"source": str(self.source), "output": str(self.output)})
        self.assertTrue(started.wait(1))
        current = app.state()["job"]
        self.assertEqual(current["progress"]["completed"], 3)
        self.assertEqual(current["progress"]["total"], 10)
        self.assertGreaterEqual(current["progress"]["elapsed_seconds"], 0)
        self.assertEqual(current["progress"]["current_file"], "main.cbl")
        self.assertTrue(app.cancel(job["job_id"])["cancel_requested"])
        release.set()
        deadline = time.monotonic() + 3
        while app.get_job(job["job_id"])["status"] == "RUNNING" and time.monotonic() < deadline:
            threading.Event().wait(0.005)
        self.assertEqual(app.get_job(job["job_id"])["status"], "CANCELLED")
        self.assertIsNone(app.state()["project"]["snapshot_id"])
        self.assertIsNone(app.state()["project"]["agent"])
        self.assertEqual(app.state()["project"]["programs"], [])

    def test_failed_detail_keeps_catalog_navigation_but_no_evidence(self):
        app = WorkbenchState()
        with mock.patch("analyze_source.build_structural_index", side_effect=RuntimeError("detail parse failed")):
            job_id = app.start({"source": str(self.source), "output": str(self.output),
                "entry": "MAIN-PROGRAM", "question": "Explain"})["job_id"]
            deadline = time.monotonic() + 3
            while app.get_job(job_id)["status"] == "RUNNING" and time.monotonic() < deadline:
                threading.Event().wait(0.005)
        project = app.get_job(job_id)["result"]
        self.assertTrue(project["diagnosis"]["catalog_ready"])
        self.assertEqual(len(project["programs"]), 2)
        self.assertIsNone(project["snapshot_id"])
        self.assertIsNone(project["agent"]["agent_result"])


    def test_workbench_default_catalog_can_be_followed_by_scoped_analysis(self):
        app = WorkbenchState()
        def finish(job_id):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                job = app.get_job(job_id)
                if job["status"] != "RUNNING":
                    return job
                threading.Event().wait(0.005)
            self.fail("job timeout")
        request = {"source": str(self.source), "output": str(self.output)}
        imported = finish(app.start(request)["job_id"])
        self.assertEqual(imported["status"], "COMPLETED")
        self.assertTrue(imported["result"]["diagnosis"]["catalog_ready"])
        self.assertIsNone(imported["result"]["snapshot_id"])
        entry = next(item["entry_key"] for item in imported["result"]["programs"] if item["program_name"] == "MAIN-PROGRAM")
        analyzed = finish(app.start({**request, "entry": entry, "question": "Explain"})["job_id"])
        self.assertEqual(analyzed["status"], "COMPLETED")
        self.assertIsNotNone(analyzed["result"]["snapshot_id"])
        self.assertEqual(len(analyzed["result"]["programs"]), 2)
        self.assertEqual(analyzed["result"]["diagnosis"]["scope"]["detail_file_count"], 1)


if __name__ == "__main__":
    unittest.main()
