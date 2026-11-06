"""Slow retained reports must not prevent the desktop HTTP shell from opening."""

from contextlib import closing
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyze_source import analyze_source
from conversation_store import ConversationStore
from report_view import report_sha256, write_report_view
from web_app import RequestError, WorkbenchState, create_server


class WorkspaceBackgroundRestoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source, self.output = self.root / "source", self.root / "output"
        self.source.mkdir()
        (self.source / "request.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. REQUEST-RULE.\nPROCEDURE DIVISION.\n"
            "DISPLAY 'APPROVED REQUEST'.\nGOBACK.\n", encoding="utf-8")
        analyze_source(self.source, self.output, source_format="free", analysis_mode="business", allow_network=False)
        store = ConversationStore(self.output, self.source)
        self.conversation = store.create()
        store.append(self.conversation["id"], "user", "Explain the business rule.")
        store.append(self.conversation["id"], "assistant", "Approved requests can proceed.")
        self.state_path = self.root / "workbench-state.json"
        self.state_path.write_text(json.dumps({"source": str(self.source), "output": str(self.output),
            "conversation_id": self.conversation["id"], "answer_instructions": "Explain business outcomes."}),
            encoding="utf-8")
        self.report_path = self.output / "diagnosis.json"
        report = json.loads(self.report_path.read_text())
        report["retained_details"] = "x" * 16_000_000
        self.report_path.write_text(json.dumps(report), encoding="utf-8")
        write_report_view(self.report_path, report)

    def blocked_hash(self):
        entered, release = threading.Event(), threading.Event()

        def slow_hash(path, *, check_cancel=None):
            entered.set()
            while not release.wait(0.01):
                if check_cancel:
                    check_cancel()
            return report_sha256(path, check_cancel=check_cancel)

        self.addCleanup(release.set)
        return entered, release, slow_hash

    def finish(self, app):
        app._startup_restore_thread.join(10)
        self.assertFalse(app._startup_restore_thread.is_alive())

    def test_http_shell_and_progress_are_available_while_full_report_hash_is_blocked(self):
        entered, release, slow_hash = self.blocked_hash()
        original_profile = self.state_path.read_bytes()
        with patch("report_view.report_sha256", side_effect=slow_hash):
            started = time.monotonic()
            app = WorkbenchState(state_path=self.state_path, background_restore=True)
            self.assertLess(time.monotonic() - started, 1)
            self.assertTrue(entered.wait(5))
            server = create_server(0, app=app)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                connection.request("GET", "/api/state")
                response = connection.getresponse()
                data = json.loads(response.read())
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(data["job"]["origin"], "workspace_restore")
                self.assertEqual(data["job"]["status"], "RUNNING")
                self.assertEqual(data["job"]["progress"]["phase"], "verifying_saved_reports")
                self.assertIsNone(data["project"]["snapshot_id"])
                self.assertIsNone(data["conversation"])
                with self.assertRaises(RequestError) as rejected:
                    app.configure_answer_instructions({"answer_instructions": "Overwrite during restoration."})
                self.assertEqual(rejected.exception.code, "JOB_ACTIVE")
                with self.assertRaises(RequestError) as rejected:
                    app.start({"source": str(self.source), "output": str(self.output), "question": "New request"})
                self.assertEqual(rejected.exception.code, "JOB_ACTIVE")
                release.set()
                self.finish(app)
                restored = app.state()
                self.assertEqual(restored["job"]["status"], "COMPLETED")
                self.assertEqual(restored["conversation"]["id"], self.conversation["id"])
                self.assertTrue(restored["project"]["snapshot_id"].startswith("sha256:"))
                self.assertEqual(restored["answer_instructions"], "Explain business outcomes.")
                self.assertEqual(self.state_path.read_bytes(), original_profile)
            finally:
                release.set()
                server.shutdown()
                server.server_close()
                thread.join(3)
                self.finish(app)

    def test_cancel_during_verification_preserves_profile_and_never_adopts_partial_state(self):
        entered, release, slow_hash = self.blocked_hash()
        before = self.state_path.read_bytes()
        database = self.output / "structural-index.sqlite"
        index_hash = hashlib.sha256(database.read_bytes()).hexdigest()
        with patch("report_view.report_sha256", side_effect=slow_hash):
            app = WorkbenchState(state_path=self.state_path, background_restore=True)
            self.assertTrue(entered.wait(5))
            app.cancel(app.job["job_id"])
            self.finish(app)
        self.assertEqual(app.job["status"], "CANCELLED")
        self.assertIsNone(app.project["snapshot_id"])
        self.assertIsNone(app.conversation)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), index_hash)
        self.assertTrue(app.source_workspaces)
        self.assertEqual(app.answer_instructions, "Explain business outcomes.")
        app.configure_answer_instructions({"answer_instructions": "Keep the saved library and explain outcomes."})
        profile = json.loads(self.state_path.read_text())
        self.assertEqual(profile["source"], str(self.source))
        self.assertEqual(profile["conversation_id"], self.conversation["id"])
        self.assertTrue(profile["source_workspaces"])
        restored = WorkbenchState(state_path=self.state_path)
        self.assertEqual(restored.conversation["id"], self.conversation["id"])
        self.assertEqual(restored.answer_instructions, "Keep the saved library and explain outcomes.")

    def test_cancelled_restore_can_import_another_library_without_forgetting_old_history(self):
        entered, release, slow_hash = self.blocked_hash()
        with patch("report_view.report_sha256", side_effect=slow_hash):
            app = WorkbenchState(state_path=self.state_path, background_restore=True)
            self.assertTrue(entered.wait(5))
            app.cancel(app.job["job_id"])
            self.finish(app)
        second_source, second_output = self.root / "second-source", self.root / "second-output"
        second_source.mkdir()
        (second_source / "second.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. SECOND-RULE.\nPROCEDURE DIVISION.\nSTOP RUN.\n")
        app.start({"source": str(second_source), "output": str(second_output), "source_format": "free",
                   "question": None, "allow_network": False})
        deadline = time.monotonic() + 10
        while app.job["status"] == "RUNNING" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(app.job["status"], "COMPLETED", app.job.get("error"))
        restarted = WorkbenchState(state_path=self.state_path)
        self.assertEqual(restarted.project["source"], str(second_source))
        self.assertIn(str(self.source), [row["source"] for row in restarted.source_workspaces.values()])
        self.assertIn(self.conversation["id"], [row["id"] for row in restarted.recent_conversations()])

    def test_equal_size_changed_report_is_still_fully_verified_before_adoption(self):
        with self.report_path.open("r+b") as stream:
            stream.seek(-1, 2)
            stream.write(b"!")
        app = WorkbenchState(state_path=self.state_path, background_restore=True)
        self.finish(app)
        self.assertIsNone(app.project["snapshot_id"])
        self.assertEqual(app.project["restore_notice"], "INDEX_RECHECK_REQUIRED")
        self.assertEqual(app.conversation["id"], self.conversation["id"])

    def test_restore_receipt_remains_available_when_automatic_upgrade_replaces_job(self):
        with closing(sqlite3.connect(self.output / "structural-index.sqlite")) as db, db:
            db.execute("UPDATE metadata SET value='business-sparse-v1.9' WHERE key='parser_version'")
        entered, release, slow_hash = self.blocked_hash()
        upgrading, finish_upgrade = threading.Event(), threading.Event()

        def analyzer(*args, **kwargs):
            upgrading.set()
            if not finish_upgrade.wait(10):
                raise AssertionError("test upgrade was not released")
            return analyze_source(*args, **kwargs)

        self.addCleanup(finish_upgrade.set)
        with patch("report_view.report_sha256", side_effect=slow_hash) as hashes:
            app = WorkbenchState(state_path=self.state_path, background_restore=True, analyzer=analyzer)
            self.assertTrue(entered.wait(5))
            restore_id = app.job["job_id"]
            release.set()
            self.finish(app)
            self.assertTrue(upgrading.wait(5))
            self.assertNotEqual(app.job["job_id"], restore_id)
            self.assertEqual(app.get_job(restore_id)["status"], "COMPLETED")
            self.assertEqual(app.job["origin"], "index_version_upgrade")
            self.assertEqual(app.conversation["id"], self.conversation["id"])
            finish_upgrade.set()
            deadline = time.monotonic() + 10
            while app.job["status"] == "RUNNING" and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(app.job["status"], "COMPLETED", app.job.get("error"))
            # Windows timestamps cannot prove the absence of equal-size edits;
            # every restore/upgrade stage must still read the original bytes.
            expected_reads = 4 if os.name == "nt" else 1
            self.assertEqual(sum(call.args[0] == self.report_path for call in hashes.call_args_list), expected_reads)


if __name__ == "__main__":
    unittest.main()
