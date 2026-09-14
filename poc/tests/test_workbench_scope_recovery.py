from __future__ import annotations

from pathlib import Path
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
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snapshot = self.app.get_job(job)
            if snapshot["status"] != "RUNNING":
                return snapshot
            threading.Event().wait(0.005)
        self.fail("Workbench job did not complete")

    def test_oversized_entry_keeps_catalog_usable_and_next_smaller_entry_works(self) -> None:
        large = self.write("large.cbl", program("LARGE-ENTRY") + (" " * (256 * 1024)))
        with large.open("r+b") as handle:
            handle.truncate(64 * 1024 * 1024 + 1)
        self.write("small.cbl", program("SMALL-ENTRY"))
        with mock.patch("analyze_source.build_structural_index", side_effect=AssertionError("oversized entry must not parse")):
            result = self.finish(self.start(entry="large.cbl", question="Explain this program"))
        self.assertEqual(result["status"], "COMPLETED")
        project = result["result"]
        self.assertEqual(project["diagnosis"]["question_status"], "SCOPE_LIMIT")
        self.assertEqual(project["diagnosis"]["reason_code"], "ENTRY_SCOPE_LIMIT")
        self.assertTrue(project["diagnosis"]["catalog_ready"])
        self.assertEqual(len(project["programs"]), 2)
        self.assertIsNone(project["snapshot_id"])
        self.assertEqual(project["relations"]["edges"], [])
        self.assertFalse((self.output / "structural-index.sqlite").exists())
        next_result = self.finish(self.start(entry="small.cbl"))
        self.assertEqual(next_result["status"], "COMPLETED")
        self.assertIsNotNone(next_result["result"]["snapshot_id"])
        self.assertEqual(next_result["result"]["diagnosis"]["selected_entry"]["program_name"], "SMALL-ENTRY")

    def test_cancellation_keeps_durable_catalog_checkpoints_but_no_current_evidence(self) -> None:
        for index in range(5):
            self.write(f"{index}.cbl", program(f"WORK-{index}"))
        reached, release = threading.Event(), threading.Event()

        def pausing_analyzer(source, output, **kwargs):
            original_progress = kwargs["progress"]
            def pause_at_checkpoint(event):
                if event["phase"] == "catalog" and event["completed"] == 2:
                    reached.set()
                    if not release.wait(timeout=3):
                        raise AssertionError("Test cancellation was not released")
                original_progress(event)
            kwargs["progress"] = pause_at_checkpoint
            return analyze_source(source, output, **kwargs)

        self.app.analyzer = pausing_analyzer
        job = self.start()
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
        resumed = self.finish(self.start())
        self.assertEqual(resumed["status"], "COMPLETED")
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
