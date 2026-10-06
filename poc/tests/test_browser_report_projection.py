"""Large browser reports preserve full artifacts and content-version history."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyze_source import _write, analyze_source
from company_api import CompanyAPIConfig, TransportResponse
from report_view import DIRECT_REPORT_BYTES, write_report_view
import source_versions
from web_app import WorkbenchState


class BrowserReportProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="browser-report-projection-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "output"
        self.member = self.source / "entry.cbl"
        self.member.write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. REQUEST-FLOW.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 REQUEST-AMOUNT PIC 9 VALUE 1.\n01 RESULT-STATE PIC X(12).\n"
            "PROCEDURE DIVISION.\nIF REQUEST-AMOUNT > ZERO\n"
            "MOVE 'ACCEPTED' TO RESULT-STATE\nEND-IF.\nGOBACK.\n", encoding="utf-8")
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "neutral-model", api_key="test-key")
        self.requests = []
        self.complete_reports = []
        self.fingerprints = []

        def transport(request):
            body = json.loads(request.body)
            self.requests.append(body)
            references = re.findall(r"ev_page_[a-f0-9]+", json.dumps(body))
            self.assertTrue(references)
            answer = f"A positive request amount sets the accepted result state. [{references[0]}]"
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": answer}, "finish_reason": "stop"}]}))

        def analyzer(source, output, **options):
            report = analyze_source(source, output, transport=transport, quiet=True, **options)
            report["unresolved_dependencies"] = ["neutral source boundary " * 4096] * 220
            path = output / "diagnosis.json"
            _write(path, report)
            write_report_view(path, report)
            self.complete_reports.append(report)
            self.fingerprints.append(self.fingerprint(path))
            return report

        self.analyzer = analyzer
        self.app = WorkbenchState(analyzer=analyzer, state_path=self.root / "workspace.json",
                                  config_provider=lambda: self.config)

    def fingerprint(self, path):
        return path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()

    def run_job(self, **options):
        started = self.app.start({"source": str(self.source), "output": str(self.output),
            "encoding": "utf-8", "source_format": "free", **options})
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            job = self.app.get_job(started["job_id"])
            if job["status"] != "RUNNING":
                self.assertEqual(job["status"], "COMPLETED", job.get("error"))
                return job["result"]
            time.sleep(.005)
        self.fail("A large report must finish publishing its browser result.")

    def assert_bounded_browser_state(self, result):
        path = self.output / "diagnosis.json"
        self.assertGreater(path.stat().st_size, DIRECT_REPORT_BYTES)
        self.assertEqual(self.fingerprint(path), self.fingerprints[-1])
        complete = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(complete["unresolved_dependencies"], self.complete_reports[-1]["unresolved_dependencies"])
        self.assertNotIn("display_projection", complete)
        self.assertIsNot(result["diagnosis"], self.complete_reports[-1])
        self.assertTrue(result["diagnosis"]["display_projection"]["complete_report_on_disk"])
        self.assertGreater(result["diagnosis"]["display_projection"]["omitted_field_count"], 0)
        self.assertTrue(result["diagnosis"]["source_manifest_verified"])
        self.assertEqual(result["snapshot_id"], complete["build_report"]["snapshot_id"])
        state = self.app.state()
        self.assertEqual(state["project"]["diagnosis"], state["job"]["result"]["diagnosis"])
        self.assertLess(len(json.dumps(state, ensure_ascii=False).encode("utf-8")), DIRECT_REPORT_BYTES)

    def test_first_import_and_cached_question_publish_bounded_state_without_rewriting_full_report(self):
        initial = self.run_job()
        self.assert_bounded_browser_state(initial)
        self.assertFalse(self.requests)
        with mock.patch("analyze_source.build_business_index", side_effect=AssertionError("A cached question must reuse its index.")), \
             mock.patch("analyze_source.ensure_repository_search", side_effect=AssertionError("A cached question must reuse search pages.")):
            answered = self.run_job(question="How is REQUEST-AMOUNT checked?", allow_network=True)
        self.assertTrue(answered["diagnosis"]["index_reused"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(answered["agent"]["runner_status"], "COMPLETED")
        self.assert_bounded_browser_state(answered)
        restored = WorkbenchState(analyzer=self.analyzer, state_path=self.root / "workspace.json",
                                  config_provider=lambda: self.config)
        self.assertEqual(restored.project["snapshot_id"], initial["snapshot_id"])
        self.assertEqual(self.fingerprint(self.output / "diagnosis.json"), self.fingerprints[-1])
        self.assertTrue(restored.project["diagnosis"]["display_projection"]["complete_report_on_disk"])


class VersionSummaryReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="version-summary-read-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "output"
        self.output.mkdir()
        self.database = self.output / "structural-index.sqlite"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE source_files (relative_path TEXT PRIMARY KEY,sha256 TEXT NOT NULL)")

    def indexed_content(self, files):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DELETE FROM source_files")
            db.executemany("INSERT INTO source_files VALUES (?,?)", sorted(files.items()))

    def test_recording_a_version_reads_one_summary_and_preserves_manifest_change_counts(self):
        initial_files = {"entry.cbl": "a" * 64, "removed.cbl": "b" * 64, "stable.cbl": "c" * 64}
        self.indexed_content(initial_files)
        with mock.patch.object(source_versions, "versions", side_effect=AssertionError("Recording must not reread the whole history.")):
            initial = source_versions.record_version(self.output, self.source, "first-snapshot")
            checked = source_versions.record_version(self.output, self.source, "checked-snapshot")
            self.indexed_content({"entry.cbl": "d" * 64, "new.cbl": "e" * 64, "stable.cbl": "c" * 64})
            changed = source_versions.record_version(self.output, self.source, "changed-snapshot")
        self.assertEqual(checked["version_id"], initial["version_id"])
        self.assertEqual(checked["created_at"], initial["created_at"])
        self.assertEqual(checked["snapshot_id"], "checked-snapshot")
        self.assertEqual(changed["previous_version_id"], initial["version_id"])
        self.assertEqual(changed["changes"], {"added": 1, "modified": 1, "removed": 1, "unchanged": 1})
        history = source_versions.versions(self.output, self.source)
        self.assertEqual(history, [changed, checked])
        with closing(sqlite3.connect(self.output / "source-versions.sqlite")) as db:
            manifests = [json.loads(row[0]) for row in db.execute("SELECT manifest_json FROM source_versions ORDER BY revision")]
        self.assertEqual(manifests[0], initial_files)

    def test_version_summaries_do_not_select_any_manifest_text(self):
        self.indexed_content({"entry.cbl": "a" * 64})
        expected = source_versions.record_version(self.output, self.source, "first-snapshot")
        connection = sqlite3.connect(self.output / "source-versions.sqlite")
        self.addCleanup(connection.close)
        selected = []

        def authorize(action, table, column, database, context):
            if action == sqlite3.SQLITE_READ:
                selected.append((table, column))
                if table == "source_versions" and column == "manifest_json":
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        with mock.patch.object(source_versions.sqlite3, "connect", return_value=connection):
            actual = source_versions.versions(self.output, self.source)
        self.assertEqual(actual, [expected])
        self.assertNotIn(("source_versions", "manifest_json"), selected)

    def test_unchanged_version_does_not_load_the_previous_manifest(self):
        self.indexed_content({"entry.cbl": "a" * 64})
        initial = source_versions.record_version(self.output, self.source, "first-snapshot")
        original_connect = sqlite3.connect

        def authorize(action, table, column, database, context):
            if action == sqlite3.SQLITE_READ and table == "source_versions" and column == "manifest_json":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        def guarded_connect(database, *args, **options):
            connection = original_connect(database, *args, **options)
            if str(database).endswith("source-versions.sqlite"):
                connection.set_authorizer(authorize)
            return connection

        with mock.patch.object(source_versions.sqlite3, "connect", side_effect=guarded_connect):
            checked = source_versions.record_version(self.output, self.source, "checked-snapshot")
        self.assertEqual(checked["version_id"], initial["version_id"])
        self.assertEqual(checked["snapshot_id"], "checked-snapshot")
        self.assertEqual(source_versions.versions(self.output, self.source), [checked])


if __name__ == "__main__":
    unittest.main()
