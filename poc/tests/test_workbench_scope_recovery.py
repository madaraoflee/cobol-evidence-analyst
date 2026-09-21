from __future__ import annotations

from pathlib import Path
from contextlib import closing
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from analyze_source import analyze_source  # noqa: E402
from company_api import CompanyAPIConfig  # noqa: E402
from web_app import RequestError, WorkbenchState  # noqa: E402


def program(name: str, statements: str = "           GOBACK.\n") -> str:
    return ("       IDENTIFICATION DIVISION.\n"
            f"       PROGRAM-ID. {name}.\n"
            "       PROCEDURE DIVISION.\n" + statements)


class WorkbenchScopeRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "output"
        self.config = CompanyAPIConfig(base_url="https://gateway.example.invalid/v1", chat_model="test-model", api_key="test-key")
        self.app = WorkbenchState(config_provider=lambda: self.config)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative: str, text: str) -> Path:
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def start(self, **options) -> str:
        result = self.app.start({"source": str(self.source), "output": str(self.output), **options})
        return result["job_id"]

    def finish(self, job: str) -> dict:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            snapshot = self.app.get_job(job)
            if snapshot["status"] != "RUNNING":
                return snapshot
            threading.Event().wait(0.005)
        self.fail("Workbench job did not complete")

    def test_large_business_entry_is_usable_and_next_smaller_entry_works(self) -> None:
        large = self.write("large.cbl", program("LARGE-ENTRY", ""))
        with large.open("a", encoding="utf-8") as handle:
            chunk = "           CONTINUE.\n" * 4096
            while handle.tell() <= 16 * 1024 * 1024:
                handle.write(chunk)
            handle.write("           GOBACK.\n")
        self.write("small.cbl", program("SMALL-ENTRY"))
        with mock.patch("analyze_source.build_structural_index", side_effect=AssertionError("business must not build a full statement graph")):
            result = self.finish(self.start(entry="large.cbl", question="Explain this program"))
        self.assertEqual(result["status"], "COMPLETED")
        project = result["result"]
        self.assertEqual(project["diagnosis"]["question_status"], "NETWORK_DISABLED")
        self.assertEqual(project["diagnosis"]["reason_code"], "SOURCE_INDEX_READY")
        self.assertEqual(project["diagnosis"]["build_report"]["index_kind"], "business_sparse")
        self.assertTrue(project["diagnosis"]["source_manifest_verified"])
        self.assertTrue(project["diagnosis"]["catalog_ready"])
        self.assertEqual(len(project["programs"]), 2)
        self.assertIsNotNone(project["snapshot_id"])
        self.assertEqual(project["relations"]["edges"], [])
        self.assertTrue((self.output / "structural-index.sqlite").exists())
        selected = next(item for item in project["programs"] if item["relative_path"] == "large.cbl")
        self.assertEqual(self.app.evidence(selected["evidence_id"])["spans"][0]["integrity"], "VALID")
        next_result = self.finish(self.start(entry="small.cbl"))
        self.assertEqual(next_result["status"], "COMPLETED")
        self.assertIsNotNone(next_result["result"]["snapshot_id"])
        self.assertEqual(next_result["result"]["diagnosis"]["selected_entry"]["program_name"], "SMALL-ENTRY")

    def cancel_after_second_file(self, phase: str, **options) -> None:
        for index in range(5):
            self.write(f"{index}.cbl", program(f"WORK-{index}"))
        reached, release = threading.Event(), threading.Event()

        def pausing_analyzer(source, output, **kwargs):
            original_progress = kwargs["progress"]
            def pause_at_checkpoint(event):
                if event["phase"] == phase and event["completed"] == 2:
                    reached.set()
                    if not release.wait(timeout=3):
                        raise AssertionError("Test cancellation was not released")
                original_progress(event)
            kwargs["progress"] = pause_at_checkpoint
            return analyze_source(source, output, **kwargs)

        self.app.analyzer = pausing_analyzer
        job = self.start(**options)
        try:
            self.assertTrue(reached.wait(timeout=3))
            self.assertTrue(self.app.cancel(job)["cancel_requested"])
        finally:
            release.set()
        stopped = self.finish(job)
        self.assertEqual(stopped["status"], "CANCELLED")
        self.assertIsNone(self.app.state()["project"]["snapshot_id"])
        with self.assertRaises(RequestError):
            self.app.evidence("ev_previous")
        self.app.analyzer = analyze_source

    def test_repository_search_cancellation_preserves_index_but_invalidates_current_evidence(self) -> None:
        self.cancel_after_second_file("repository_search")
        with closing(sqlite3.connect(self.output / "structural-index.sqlite")) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0], 5)
            snapshot_id = connection.execute("SELECT value FROM metadata WHERE key='snapshot_id'").fetchone()[0]
            self.assertEqual(connection.execute("SELECT value FROM repo_metadata WHERE key='ready'").fetchone()[0], "0")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM repo_sources").fetchone()[0], 0)
        resumed = self.finish(self.start())
        self.assertEqual(resumed["status"], "COMPLETED")
        diagnosis = resumed["result"]["diagnosis"]
        self.assertEqual(diagnosis["scope"]["mode"], "repository_index")
        self.assertEqual(diagnosis["build_report"]["files"]["skipped_unchanged"], 5)
        self.assertEqual(diagnosis["repository_search"]["updated_files"], 5)
        self.assertEqual(resumed["result"]["snapshot_id"], snapshot_id)
        self.assertTrue(diagnosis["source_manifest_verified"])
        self.assertEqual(len(resumed["result"]["programs"]), 5)

    def test_explicit_entry_catalog_cancellation_keeps_durable_checkpoints_but_no_current_evidence(self) -> None:
        self.cancel_after_second_file("catalog", entry="WORK-0")
        resumed = self.finish(self.start(entry="WORK-0"))
        self.assertEqual(resumed["status"], "COMPLETED")
        self.assertIsNotNone(resumed["result"]["snapshot_id"])
        self.assertEqual(resumed["result"]["diagnosis"]["scope"]["mode"], "entry_static_closure")
        stats = resumed["result"]["diagnosis"]["catalog_report"]["files"]
        self.assertEqual(stats["cached"], 2)
        self.assertEqual(stats["indexed_or_updated"], 3)
        self.assertEqual(len(resumed["result"]["programs"]), 5)

    def test_duplicate_program_keeps_unique_selection_key_after_detail_and_repeat(self) -> None:
        self.write("first/main.cbl", program("MAIN"))
        self.write("second/main.cbl", program("MAIN"))
        catalog = self.finish(self.start())["result"]
        selected = next(item for item in catalog["programs"] if item["relative_path"] == "first/main.cbl")
        detailed = self.finish(self.start(entry=selected["entry_key"]))["result"]
        selected_again = next(item for item in detailed["programs"] if item["relative_path"] == "first/main.cbl")
        self.assertEqual(selected_again["entry_key"], selected["entry_key"])
        repeated = self.finish(self.start(entry=selected_again["entry_key"]))
        self.assertEqual(repeated["status"], "COMPLETED")
        self.assertNotEqual(repeated["result"]["diagnosis"]["runner_status"], "BLOCKED")
        self.assertEqual(repeated["result"]["diagnosis"]["selected_entry"]["relative_path"], "first/main.cbl")


if __name__ == "__main__":
    unittest.main()
