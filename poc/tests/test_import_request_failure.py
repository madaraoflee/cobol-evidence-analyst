"""Import failures retain useful local diagnostics and finish rollback explicitly."""

from contextlib import closing
import errno
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api_error_details
from analyze_source import analyze_source
from company_api import CompanyAPIConfig
from source_update import SourceUpdateBackup
from web_app import RequestError, WorkbenchState


SENSITIVE_MARKERS = ("private-export-marker", "local-secret-marker", "private-provider-marker")


class ImportRequestFailureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="import-request-failure-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "output"
        self.member = self.source / "entry.cbl"
        self.member.write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. REQUEST-FLOW.\n"
            "PROCEDURE DIVISION.\nGOBACK.\n", encoding="utf-8")
        self.reports = []
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="test-key")

        def analyzer(source, output, **options):
            report = analyze_source(source, output, quiet=True, **options)
            self.reports.append(report)
            return report

        self.analyzer = analyzer
        self.app = WorkbenchState(analyzer=analyzer, state_path=self.root / "workspace.json",
                                  config_provider=lambda: self.config)
        initial = self.update()
        self.assertEqual(initial["status"], "COMPLETED", initial.get("error"))
        self.old_snapshot = initial["result"]["snapshot_id"]
        self.old_evidence = initial["result"]["programs"][0]["evidence_id"]
        self.conversation = self.app.new_conversation({})["conversation"]
        self.message_id = self.app.conversation_store.append(
            self.conversation["id"], "assistant", "Earlier source explanation.",
            details={"snapshot_id": self.old_snapshot,
                     "evidence_refs": [{"evidence_id": self.old_evidence}]})
        self.app.conversation = self.app.conversation_store.get(self.conversation["id"])
        self.app._save_workspace()

    def update(self):
        started = self.app.start({"source": str(self.source), "output": str(self.output),
                                  "encoding": "auto", "source_format": "free", "allow_network": False})
        deadline = time.monotonic() + 3
        result = self.app.get_job(started["job_id"])
        while result["status"] == "RUNNING" and time.monotonic() < deadline:
            time.sleep(0.005)
            result = self.app.get_job(started["job_id"])
        self.assertNotEqual(result["status"], "RUNNING", "An import failure must not leave an orphaned running job.")
        return result

    def assert_failure(self, result, category, code=None):
        self.assertEqual(result["status"], "FAILED")
        self.assertIsNone(result["result"])
        if code is not None:
            self.assertEqual(result["error"]["code"], code)
        diagnostic = result["error"].get("diagnostic")
        self.assertIsNotNone(diagnostic)
        self.assertEqual(diagnostic["category"], category)
        self.assertEqual(diagnostic["evidence_source"], "local")
        self.assertRegex(diagnostic["request_id"], r"^local-[0-9a-f]{32}$")
        self.assertEqual(api_error_details.sanitize_diagnostic(diagnostic), diagnostic)
        return diagnostic

    def assert_old_workspace_is_usable(self):
        self.assertEqual(self.app.project["snapshot_id"], self.old_snapshot)
        self.assertEqual(self.app.conversation["id"], self.conversation["id"])
        self.assertEqual(self.app.evidence(self.old_evidence)["snapshot_id"], self.old_snapshot)
        historical = self.app.evidence(self.old_evidence, self.conversation["id"], self.message_id)
        self.assertEqual(historical["snapshot_id"], self.old_snapshot)
        self.assertEqual(historical["status"], "OK")

    def assert_authority_is_unavailable(self):
        self.assertIsNone(self.app.project["snapshot_id"])
        self.assertEqual(self.app.project["source"], str(self.source))
        self.assertEqual(self.app.project["output"], str(self.output))
        with self.assertRaises(RequestError) as caught:
            self.app.evidence(self.old_evidence)
        self.assertEqual(caught.exception.code, "NO_CURRENT_INDEX")

    def test_all_binary_source_update_explains_no_usable_source_and_restores_historical_references(self):
        self.member.write_bytes(b"\x00" * 128)
        result = self.update()
        diagnostic = self.assert_failure(result, "source_empty", "SOURCE_UPDATE_FAILED")
        failed_report = self.reports[-1]
        self.assertEqual(failed_report["reason_code"], "SOURCE_ANALYSIS_FAILED")
        self.assertFalse(failed_report["source_manifest_verified"])
        self.assertEqual(failed_report["build_report"]["input_skips"],
                         [{"relative_path": "entry.cbl", "reason_code": "SOURCE_ENCODING_INVALID"}])
        self.assertEqual(diagnostic["request_id"], failed_report["diagnostic"]["request_id"])
        self.assert_old_workspace_is_usable()

    def test_empty_source_update_explains_the_empty_source_and_keeps_the_old_workspace(self):
        self.member.unlink()
        result = self.update()
        self.assert_failure(result, "source_empty", "SOURCE_UPDATE_FAILED")
        self.assert_old_workspace_is_usable()

    def test_builder_storage_failures_keep_their_category_and_restore_the_old_workspace(self):
        cases = ((PermissionError(errno.EACCES, "private-export-marker"), "storage_permission"),
                 (OSError(errno.ENOSPC, "local-secret-marker"), "storage_full"))
        for error, category in cases:
            with self.subTest(category=category), \
                    mock.patch("analyze_source.build_business_index", side_effect=error):
                result = self.update()
            diagnostic = self.assert_failure(result, category, "SOURCE_UPDATE_FAILED")
            self.assertEqual(diagnostic["request_id"], self.reports[-1]["diagnostic"]["request_id"])
            self.assert_old_workspace_is_usable()

    def test_backup_storage_failures_keep_their_category_and_restore_the_old_workspace(self):
        cases = ((PermissionError(errno.EACCES, "private-export-marker"), "storage_permission"),
                 (OSError(errno.ENOSPC, "local-secret-marker"), "storage_full"))
        for error, category in cases:
            with self.subTest(category=category), \
                    mock.patch.object(SourceUpdateBackup, "_backup_database", side_effect=error):
                result = self.update()
            self.assert_failure(result, category)
            self.assert_old_workspace_is_usable()

    def test_workspace_persistence_failure_ends_the_job_without_claiming_a_restored_snapshot(self):
        with mock.patch.object(self.app, "_save_workspace", side_effect=PermissionError(errno.EACCES, "private-export-marker")):
            result = self.update()
        self.assert_failure(result, "storage_permission", "SOURCE_UPDATE_ROLLBACK_FAILED")
        self.assert_authority_is_unavailable()

    def test_backup_restore_failure_ends_the_job_without_claiming_a_restored_snapshot(self):
        self.member.unlink()
        with mock.patch.object(SourceUpdateBackup, "restore", side_effect=PermissionError(errno.EACCES, "private-export-marker")):
            result = self.update()
        self.assert_failure(result, "storage_permission", "SOURCE_UPDATE_ROLLBACK_FAILED")
        self.assert_authority_is_unavailable()

    def test_malformed_generated_program_report_is_classified_without_losing_the_old_workspace(self):
        def malformed_program_report(source, output, **options):
            report = self.analyzer(source, output, **options)
            (output / "programs.json").write_text('{"private-export-marker":', encoding="utf-8")
            return report

        self.app.analyzer = malformed_program_report
        result = self.update()
        self.assert_failure(result, "report_invalid")
        self.assert_old_workspace_is_usable()

    def test_unknown_exception_does_not_expose_sensitive_context_in_the_job_response(self):
        message = " | ".join(SENSITIVE_MARKERS)
        with mock.patch("analyze_source.build_business_index", side_effect=RuntimeError(message)):
            result = self.update()
        self.assert_failure(result, "system_error")
        safe_json = json.dumps(result, ensure_ascii=False)
        for marker in SENSITIVE_MARKERS:
            self.assertNotIn(marker, safe_json)
        self.assert_old_workspace_is_usable()

    def test_local_diagnostic_uses_typed_errno_and_exact_internal_codes(self):
        windows_lock = PermissionError(errno.EACCES, "private-provider-marker")
        windows_lock.winerror = 32
        cases = (
            (OSError(errno.ENOSPC, "private-export-marker"), "storage_full"),
            (PermissionError(errno.EACCES, "local-secret-marker"), "storage_permission"),
            (windows_lock, "storage_locked"),
            (json.JSONDecodeError("private-export-marker", "local-secret-marker", 0), "report_invalid"),
            (ValueError("invalid report"), "report_invalid"),
            (ValueError("invalid report view"), "report_invalid"),
            (ValueError("report view does not match complete report"), "report_invalid"),
            (ValueError("SOURCE_SNAPSHOT_MISMATCH"), "source_snapshot_mismatch"),
            (ValueError("SOURCE_ENCODING_INVALID"), "source_encoding_invalid"),
            (ValueError("SOURCE_CHANGED_DURING_READ"), "source_changed"),
            (ValueError("SOURCE_HASH_MISMATCH"), "source_changed"),
            (ValueError("SOURCE_INDEX_EMPTY"), "source_empty"),
            (RuntimeError("This SQLite build does not include FTS5 support."), "fts_unavailable"),
            (RuntimeError("This Python SQLite build does not include FTS5 support."), "fts_unavailable"),
        )
        for error, category in cases:
            with self.subTest(category=category):
                diagnostic = api_error_details.build_local_diagnostic(error, "REQUEST_FAILED", http_status=500)
                self.assertEqual(diagnostic["category"], category)
                self.assertEqual(diagnostic["evidence_source"], "local")
                self.assertEqual(diagnostic["http_status"], 500)
                self.assertEqual(api_error_details.sanitize_diagnostic(diagnostic), diagnostic)
                for marker in SENSITIVE_MARKERS:
                    self.assertNotIn(marker, json.dumps(diagnostic))

    def test_sqlite_corruption_uses_the_primary_error_code(self):
        database = self.root / "corrupt.sqlite"
        database.write_bytes(b"invalid database private-export-marker")
        with closing(sqlite3.connect(database)) as db:
            try:
                db.execute("SELECT name FROM sqlite_master").fetchall()
            except sqlite3.DatabaseError as error:
                diagnostic = api_error_details.build_local_diagnostic(error, "REQUEST_FAILED")
            else:
                self.fail("The fixture must produce a real SQLite corruption error.")
        self.assertEqual(diagnostic["category"], "storage_corrupt")
        self.assertNotIn("private-export-marker", json.dumps(diagnostic))

    def test_sqlite_lock_uses_the_primary_error_code(self):
        database = self.root / "locked.sqlite"
        with closing(sqlite3.connect(database)) as owner:
            owner.execute("CREATE TABLE records (value TEXT)")
            owner.commit()
            owner.execute("BEGIN EXCLUSIVE")
            with closing(sqlite3.connect(database, timeout=0)) as reader:
                try:
                    reader.execute("SELECT * FROM records").fetchall()
                except sqlite3.OperationalError as error:
                    diagnostic = api_error_details.build_local_diagnostic(error, "REQUEST_FAILED")
                else:
                    self.fail("The fixture must produce a real SQLite lock error.")
        self.assertEqual(diagnostic["category"], "storage_locked")

    def test_unrecognized_messages_cannot_spoof_a_specific_local_failure(self):
        for error in (RuntimeError("storage full private-export-marker"),
                      ValueError("SOURCE_ENCODING_INVALID private-provider-marker"),
                      Exception("permission denied local-secret-marker")):
            with self.subTest(error_type=type(error).__name__):
                diagnostic = api_error_details.build_local_diagnostic(error, "REQUEST_FAILED")
                self.assertEqual(diagnostic["category"], "system_error")
                for marker in SENSITIVE_MARKERS:
                    self.assertNotIn(marker, json.dumps(diagnostic))


if __name__ == "__main__":
    unittest.main()
