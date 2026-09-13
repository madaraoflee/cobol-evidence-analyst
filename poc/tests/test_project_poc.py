from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

import run_project_poc  # noqa: E402


PROGRAM = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. REQUESTJOB.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01 REQUEST-STATE PIC X(4).
       PROCEDURE DIVISION.
           MOVE 'OPEN' TO REQUEST-STATE
           GOBACK.
"""


class ProjectPocTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.program = self.source / "REQUESTJOB.cbl"
        self.program.write_text(PROGRAM, encoding="utf-8")
        self.output = self.root / "output"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def audit(self) -> dict:
        source_hash = hashlib.sha256(self.program.read_bytes()).hexdigest()
        reference = {"origin": {"relative_path": self.program.name, "line": 7, "source_hash": source_hash},
                     "include_chain": []}
        return {
            "schema_version": "1.0", "entry_program": "REQUESTJOB", "profile": None,
            "source_files": [{"relative_path": path.name, "source_hash": hashlib.sha256(path.read_bytes()).hexdigest()}
                             for path in sorted(self.source.iterdir()) if path.is_file() and path.name != "IGNORED.cbl"],
            "scope": {"source_expansion_complete": True, "compiler_equivalent": False,
                      "control_flow_complete": False, "runtime_verified": False, "question_answered": False},
            "observations": {"performs": [], "calls": [], "includes": [], "native_io": [],
                             "procedure_definitions": [{"name": "REQUESTJOB", "references": [reference]}]},
            "declared_contracts": {"entry": None, "io_contracts": [], "record_decisions": [], "artifact_requirements": []},
            "boundaries": [], "summary": {"expanded_lines": 8, "performs": 0, "calls": 0, "boundaries": 0},
        }

    def test_generic_bundle_records_question_without_answering_or_modifying_source(self) -> None:
        before = self.program.read_bytes()
        with mock.patch.object(run_project_poc, "audit_framework", return_value=self.audit()) as auditor:
            bundle = run_project_poc.build_project_poc(self.source, self.output, "REQUESTJOB",
                                                       question="Why was the request skipped?")
        auditor.assert_called_once_with(self.source.resolve(), "REQUESTJOB", None,
                                        extensions=run_project_poc.DEFAULT_EXTENSIONS)
        self.assertEqual(before, self.program.read_bytes())
        self.assertEqual("RECORDED_NOT_ANSWERED", bundle["question_status"])
        self.assertFalse(bundle["privacy"]["network_calls"])
        self.assertFalse(bundle["privacy"]["target_program_executed"])
        self.assertEqual("raw_source", bundle["provenance"]["index_scope"])
        self.assertEqual("derived_source_observations", bundle["provenance"]["framework_scope"])
        self.assertFalse(bundle["provenance"]["derived_observations_used_by_existing_query_or_cfg_tools"])
        files = {path.name for path in self.output.iterdir()}
        self.assertTrue({"structural-index.sqlite", "framework-report.json", "framework-report.md", "inventory.json"} <= files)
        self.assertTrue(files <= {"structural-index.sqlite", "framework-report.json", "framework-report.md", "inventory.json",
                                  "structural-index.sqlite-shm", "structural-index.sqlite-wal"})
        report = json.loads((self.output / "framework-report.json").read_text(encoding="utf-8"))
        self.assertEqual(bundle, report)
        self.assertFalse(report["inventory"]["privacy"]["source_text_included"])
        self.assertIn("未回答该问题", (self.output / "framework-report.md").read_text(encoding="utf-8"))

    def test_all_three_scans_share_extensions_and_include_extensionless(self) -> None:
        self.program.rename(self.source / "REQUESTJOB.custom")
        self.program = self.source / "REQUESTJOB.custom"
        (self.source / "SECONDJOB").write_text(PROGRAM.replace("REQUESTJOB", "SECONDJOB"), encoding="utf-8")
        (self.source / "IGNORED.cbl").write_text(PROGRAM.replace("REQUESTJOB", "IGNORED"), encoding="utf-8")
        extensions = frozenset({".custom"})
        with mock.patch.object(run_project_poc, "audit_framework", return_value=self.audit()) as auditor:
            bundle = run_project_poc.build_project_poc(self.source, self.output, "REQUESTJOB", extensions=extensions)
        self.assertEqual(extensions, auditor.call_args.kwargs["extensions"])
        self.assertTrue(bundle["selection"]["include_extensionless"])
        self.assertEqual(2, bundle["build_report"]["files"]["decoded"])
        self.assertEqual(2, bundle["inventory"]["snapshot"]["decoded_file_count"])
        with sqlite3.connect(bundle["artifacts"]["database"]) as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM symbols WHERE symbol_type='Program'")}
        self.assertEqual({"REQUESTJOB", "SECONDJOB"}, names)

    def test_real_auditor_accepts_arbitrary_non_amount_entry(self) -> None:
        bundle = run_project_poc.build_project_poc(self.source, self.output, "REQUESTJOB")
        self.assertEqual("REQUESTJOB", bundle["audit"]["entry_program"])
        self.assertEqual(1, bundle["build_report"]["files"]["decoded"])
        self.assertTrue(bundle["provenance"]["audit_files_match_raw_index"])
        self.assertFalse(bundle["audit"]["scope"]["question_answered"])
        self.assertIn("framework_profile_missing", {item["reason"] for item in bundle["audit"]["boundaries"]})

    def test_existing_empty_output_is_supported_but_repeat_cannot_overwrite(self) -> None:
        self.output.mkdir()
        with mock.patch.object(run_project_poc, "audit_framework", return_value=self.audit()):
            run_project_poc.build_project_poc(self.source, self.output, "REQUESTJOB")
            report = (self.output / "framework-report.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "empty"):
                run_project_poc.build_project_poc(self.source, self.output, "REQUESTJOB")
            self.assertEqual(report, (self.output / "framework-report.json").read_bytes())

    def test_rejects_source_output_overlap_before_audit_or_writes(self) -> None:
        for target in (self.source, self.source / "nested", self.root):
            with self.subTest(target=target), mock.patch.object(run_project_poc, "audit_framework") as auditor:
                with self.assertRaisesRegex(ValueError, "non-nested"):
                    run_project_poc.build_project_poc(self.source, target, "REQUESTJOB")
                auditor.assert_not_called()
        self.assertFalse((self.source / "nested").exists())

    def test_resolves_parent_symlink_before_overlap_check(self) -> None:
        link = self.root / "source-alias"
        try:
            link.symlink_to(self.source, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic links are unavailable in this test environment.")
        with self.assertRaisesRegex(ValueError, "non-nested"):
            run_project_poc.build_project_poc(self.source, link / "nested", "REQUESTJOB")
        self.assertFalse((self.source / "nested").exists())

    def test_rejects_output_symlink_even_if_target_empty(self) -> None:
        target = self.root / "empty"
        target.mkdir()
        try:
            self.output.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic links are unavailable in this test environment.")
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            run_project_poc.build_project_poc(self.source, self.output, "REQUESTJOB")
        self.assertEqual([], list(target.iterdir()))

    def test_profile_must_be_object_and_validation_precedes_output(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON object"):
            run_project_poc.build_project_poc(self.source, self.output, "REQUESTJOB", profile=[])
        with mock.patch.object(run_project_poc, "audit_framework", side_effect=ValueError("Invalid profile")):
            with self.assertRaisesRegex(ValueError, "Invalid profile"):
                run_project_poc.build_project_poc(self.source, self.output, "REQUESTJOB", profile={})
        self.assertFalse(self.output.exists())

    def test_changed_source_between_audit_and_index_is_rejected(self) -> None:
        audit = self.audit()
        self.program.write_text(PROGRAM.replace("OPEN", "WAIT"), encoding="utf-8")
        with mock.patch.object(run_project_poc, "audit_framework", return_value=audit):
            with self.assertRaisesRegex(ValueError, "audit/index"):
                run_project_poc.build_project_poc(self.source, self.output, "REQUESTJOB")
        self.assertFalse((self.output / "framework-report.json").exists())

    def test_changed_source_after_index_is_rejected(self) -> None:
        original_builder = run_project_poc.build_structural_index

        def changed(*args, **kwargs):
            result = original_builder(*args, **kwargs)
            self.program.write_text(PROGRAM.replace("OPEN", "WAIT"), encoding="utf-8")
            return result

        with mock.patch.object(run_project_poc, "audit_framework", return_value=self.audit()), \
             mock.patch.object(run_project_poc, "build_structural_index", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "Source changed during"):
                run_project_poc.build_project_poc(self.source, self.output, "REQUESTJOB")
        self.assertFalse((self.output / "framework-report.json").exists())

    def test_new_source_between_audit_and_index_is_rejected(self) -> None:
        audit = self.audit()
        (self.source / "ADDED.cpy").write_text("           CONTINUE.\n", encoding="utf-8")
        with mock.patch.object(run_project_poc, "audit_framework", return_value=audit):
            with self.assertRaisesRegex(ValueError, "audit/index"):
                run_project_poc.build_project_poc(self.source, self.output, "REQUESTJOB")
        self.assertFalse((self.output / "framework-report.json").exists())

    def test_new_source_after_index_is_rejected(self) -> None:
        original_builder = run_project_poc.build_structural_index

        def changed(*args, **kwargs):
            result = original_builder(*args, **kwargs)
            (self.source / "ADDED.cpy").write_text("           CONTINUE.\n", encoding="utf-8")
            return result

        with mock.patch.object(run_project_poc, "audit_framework", return_value=self.audit()), \
             mock.patch.object(run_project_poc, "build_structural_index", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "file set changed"):
                run_project_poc.build_project_poc(self.source, self.output, "REQUESTJOB")
        self.assertFalse((self.output / "framework-report.json").exists())

    def test_late_database_collision_is_never_indexed_or_overwritten(self) -> None:
        original_validator = run_project_poc._validated_paths
        calls = 0

        def collide(*args):
            nonlocal calls
            result = original_validator(*args)
            calls += 1
            if calls == 2:
                self.output.mkdir()
                (self.output / "structural-index.sqlite").write_bytes(b"existing-user-data")
            return result

        with mock.patch.object(run_project_poc, "audit_framework", return_value=self.audit()), \
             mock.patch.object(run_project_poc, "_validated_paths", side_effect=collide), \
             mock.patch.object(run_project_poc, "build_structural_index") as builder:
            with self.assertRaises(FileExistsError):
                run_project_poc.build_project_poc(self.source, self.output, "REQUESTJOB")
            builder.assert_not_called()
        self.assertEqual(b"existing-user-data", (self.output / "structural-index.sqlite").read_bytes())

    def test_cli_json_and_local_profile_are_forwarded(self) -> None:
        profile = {"test": "declaration"}
        profile_path = self.root / "profile.json"
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        stream = io.StringIO()
        with mock.patch.object(run_project_poc, "audit_framework", return_value=self.audit()) as auditor, \
             contextlib.redirect_stdout(stream):
            status = run_project_poc.main(["--source", str(self.source), "--output", str(self.output),
                                          "--entry", "REQUESTJOB", "--profile", str(profile_path),
                                          "--question", "Which state changes?"])
        summary = json.loads(stream.getvalue())
        self.assertEqual(0, status)
        self.assertEqual(profile, auditor.call_args.args[2])
        self.assertEqual("COMPLETED", summary["runner_status"])
        self.assertFalse(summary["question_answered"])
        self.assertFalse(summary["full_business_analysis_verified"])

    def test_cli_invalid_profile_reports_failure_without_output(self) -> None:
        profile_path = self.root / "profile.json"
        profile_path.write_text("[]", encoding="utf-8")
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = run_project_poc.main(["--source", str(self.source), "--output", str(self.output),
                                          "--entry", "REQUESTJOB", "--profile", str(profile_path)])
        self.assertEqual(2, status)
        self.assertEqual("FAILED", json.loads(stream.getvalue())["runner_status"])
        self.assertFalse(self.output.exists())

    def test_report_exposes_copy_origin_and_inclusion_without_asserting_execution(self) -> None:
        audit = self.audit()
        origin = {"origin": {"relative_path": "copybooks/CONTROL.cpy", "line": 4, "source_hash": "a"},
                  "include_chain": [{"relative_path": "REQUESTJOB.cbl", "line": 8, "source_hash": "b"}]}
        audit["observations"]["performs"] = [{"target": "2000-READ", "target_status": "unique_definition",
                                                "references": [origin], "target_definitions": []}]
        audit["observations"]["calls"] = [{"target": "DATAACCESS", "target_kind": "literal",
                                             "references": [origin], "function_value_at_call": "not_resolved"}]
        report = run_project_poc.render_framework_report(audit, "<script>question</script> | test")
        self.assertIn("copybooks/CONTROL.cpy:4", report)
        self.assertIn("REQUESTJOB.cbl:8", report)
        self.assertIn("not_resolved", report)
        self.assertIn("不表示该分支一定执行", report)
        self.assertNotIn("<script>", report)
        self.assertIn("&lt;script&gt;", report)


if __name__ == "__main__":
    unittest.main()
