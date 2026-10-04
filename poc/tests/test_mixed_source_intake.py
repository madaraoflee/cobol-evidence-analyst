"""Mixed local exports retain usable sources, retry inputs and real workspaces."""

from contextlib import ExitStack, closing, contextmanager
import errno
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

from analyze_source import analyze_source
from company_api import CompanyAPIConfig, TransportResponse
from web_app import FRAMEWORK_DEMO_SOURCE, WorkbenchState


def program(name, note="READY"):
    return ("IDENTIFICATION DIVISION.\n"
            f"PROGRAM-ID. {name}.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 REQUEST-COUNT PIC 9 VALUE 1.\n"
            "01 RESULT-STATE PIC X(12).\n"
            "PROCEDURE DIVISION.\nMAIN-FLOW.\n"
            "IF REQUEST-COUNT > ZERO\nMOVE 'ACCEPTED' TO RESULT-STATE\nEND-IF.\n"
            f"DISPLAY '{note}'.\nGOBACK.\n")


class MixedSourceIntakeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mixed-source-intake-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "results"
        self.state_path = self.root / "workspace.json"
        self.requests = []
        self.reports = []
        self.started = []
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="test-key")

        def analyzer(source, output, **options):
            report = analyze_source(source, output, transport=self.transport, quiet=True, **options)
            self.reports.append(report)
            return report

        self.analyzer = analyzer
        self.app = self.workbench()

    def workbench(self):
        return WorkbenchState(analyzer=self.analyzer, config_provider=lambda: self.config,
                              state_path=self.state_path, demo_output_root=self.root / "case-results")

    def transport(self, request):
        body = json.loads(request.body)
        self.requests.append(body)
        references = re.findall(r"ev_page_[a-f0-9]+", json.dumps(body))
        self.assertTrue(references, "The indexed source must reach the simulated model.")
        content = f"A positive REQUEST-COUNT sets RESULT-STATE to ACCEPTED. [{references[0]}]"
        return TransportResponse(200, json.dumps({"choices": [{"message": {
            "role": "assistant", "content": content}, "finish_reason": "stop"}]}))

    def write(self, relative, text, encoding="utf-8", source=None):
        path = (source or self.source) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode(encoding))
        return path

    def wait(self, started):
        if started.get("status") == "READY":
            return {"status": "COMPLETED", "result": started["project"]}
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            job = self.app.get_job(started["job_id"])
            if job["status"] != "RUNNING":
                return job
            time.sleep(0.005)
        self.fail("Local source intake did not finish.")

    def intake(self, **options):
        started = self.app.start({"source": str(self.source), "output": str(self.output),
            "encoding": "auto", "source_format": "free", "extensions": ".cbl,.cpy", "allow_network": False, **options})
        self.started.append(started)
        return self.wait(started)

    def completed(self, job):
        self.assertEqual(job["status"], "COMPLETED", job.get("error"))
        project = job["result"]
        self.assertTrue(project["snapshot_id"])
        self.assertTrue(project["diagnosis"]["source_manifest_verified"])
        return project

    def mixed_sources(self):
        self.write("entry.cbl", program("REQUEST-FLOW"))
        self.write("bom16.cbl", program("UNICODE-FLOW"), "utf-16")
        self.write("bom32.cbl", program("WIDE-FLOW"), "utf-32")
        self.write("legacy.cbl", program("LEGACY-FLOW", "中性說明"), "cp950")
        self.write("EXTENSIONLESS", program("MEMBER-FLOW"))
        (self.source / "binary-member").write_bytes(b"\x00\x01\x02\x00" * 64)
        (self.source / "manual.pdf").write_bytes(b"%PDF-1.7\n\x00\x01")
        (self.source / "archive.zip").write_bytes(b"PK\x03\x04\x00\x00")

    def assert_skips(self, project, paths, *, candidate, decoded, reason="SOURCE_ENCODING_INVALID"):
        report = project["diagnosis"]
        self.assertEqual(report["runner_status"], "NEEDS_ATTENTION")
        self.assertFalse(report["scope"]["input_coverage_complete"])
        build = report["build_report"]
        self.assertEqual(build["files"]["candidate"], candidate)
        self.assertEqual(build["files"]["decoded"], decoded)
        self.assertEqual(build["files"]["unreadable_or_binary"], len(paths))
        self.assertEqual({item["relative_path"] for item in build["input_skips"]}, set(paths))
        for item in build["input_skips"]:
            self.assertEqual(item["reason_code"], reason)
            self.assertFalse(Path(item["relative_path"]).is_absolute())
            self.assertNotIn("private-file-error", json.dumps(item))

    @contextmanager
    def no_whole_source_intake(self):
        with ExitStack() as stack:
            for target in ("analyze_source.build_business_index", "analyze_source.build_structural_index",
                           "analyze_source.ensure_repository_search", "analyze_source._verify_scope",
                           "business_index.iter_source_files", "repo_inventory.iter_source_files"):
                stack.enter_context(mock.patch(target, side_effect=AssertionError("Cached questions must not rescan the source directory.")))
            yield

    def test_mixed_import_indexes_readable_encodings_and_skips_binary_members_only(self):
        self.mixed_sources()
        project = self.completed(self.intake())
        self.assert_skips(project, {"binary-member"}, candidate=6, decoded=5)
        self.assertEqual(len(self.requests), 0)
        self.assertEqual(len(project["programs"]), 5)
        self.assertTrue(project["diagnosis"]["repository_search"]["full_text_complete"])
        with closing(sqlite3.connect(self.output / "structural-index.sqlite")) as connection:
            encodings = dict(connection.execute("SELECT relative_path,encoding FROM source_files"))
            self.assertEqual(set(encodings), {"entry.cbl", "bom16.cbl", "bom32.cbl", "legacy.cbl", "EXTENSIONLESS"})
            self.assertEqual(encodings["bom16.cbl"], "utf-16")
            self.assertEqual(encodings["bom32.cbl"], "utf-32")
            self.assertEqual(encodings["legacy.cbl"], "cp950")

    def test_wrong_selected_encoding_skips_one_file_and_all_bad_update_retains_the_prior_index(self):
        self.write("entry.cbl", program("REQUEST-FLOW"))
        legacy = self.write("legacy.cbl", program("LEGACY-FLOW", "中性說明"), "cp950")
        project = self.completed(self.intake(encoding="utf-8"))
        self.assert_skips(project, {"legacy.cbl"}, candidate=2, decoded=1)
        snapshot = project["snapshot_id"]
        evidence = project["programs"][0]["evidence_id"]
        (self.source / "entry.cbl").unlink()
        failed = self.intake(encoding="utf-8")
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error"]["code"], "SOURCE_UPDATE_FAILED")
        self.assertEqual(failed["error"]["diagnostic"]["category"], "source_empty")
        failed_report = self.reports[-1]
        self.assertFalse(failed_report["scope"]["input_coverage_complete"])
        failed_files = failed_report["build_report"]["files"]
        self.assertEqual(failed_files["candidate"], 1)
        self.assertEqual(failed_files["decoded"], 0)
        self.assertEqual(failed_files["unreadable_or_binary"], 1)
        self.assertEqual(failed_report["build_report"]["input_skips"], [{"relative_path": "legacy.cbl", "reason_code": "SOURCE_ENCODING_INVALID"}])
        self.assertEqual(self.app.project["snapshot_id"], snapshot)
        self.assertEqual(self.app.evidence(evidence)["snapshot_id"], snapshot)
        with closing(sqlite3.connect(self.output / "structural-index.sqlite")) as connection:
            self.assertEqual(connection.execute("SELECT relative_path FROM source_files").fetchall(), [("entry.cbl",)])
        self.write(legacy.name, program("LEGACY-FLOW"))
        repaired = self.completed(self.intake(encoding="utf-8"))
        self.assertEqual(repaired["diagnosis"]["build_report"]["input_skips"], [])
        self.assertTrue(repaired["diagnosis"]["scope"]["input_coverage_complete"])
        self.assertEqual({item["relative_path"] for item in repaired["programs"]}, {"legacy.cbl"})

    def test_unreadable_member_is_typed_without_aborting_other_sources(self):
        self.write("entry.cbl", program("REQUEST-FLOW"))
        blocked = self.write("blocked.cbl", program("BLOCKED-FLOW"))
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError(errno.EACCES, "private-file-error")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", guarded_open):
            project = self.completed(self.intake())
        self.assert_skips(project, {"blocked.cbl"}, candidate=2, decoded=1, reason="SOURCE_READ_FAILED")
        self.assertEqual({item["program_name"] for item in project["programs"]}, {"REQUEST-FLOW"})

    def test_a_previously_indexed_binary_file_loses_old_facts_and_both_search_indexes_then_recovers(self):
        self.write("entry.cbl", program("REQUEST-FLOW"))
        changing = self.write("changing.cbl", program("OBSOLETE-FLOW", "obsoleteflowmarker"))
        self.completed(self.intake())
        database = self.output / "structural-index.sqlite"
        with closing(sqlite3.connect(database)) as connection:
            self.assertGreater(connection.execute("SELECT COUNT(*) FROM code_units_fts WHERE program_name='OBSOLETE-FLOW'").fetchone()[0], 0)
            self.assertGreater(connection.execute("SELECT COUNT(*) FROM repo_fts WHERE repo_fts MATCH 'obsoleteflowmarker'").fetchone()[0], 0)
        changing.write_bytes(b"\x00\x01\x02\x00" * 64)
        updated = self.completed(self.intake())
        self.assert_skips(updated, {"changing.cbl"}, candidate=2, decoded=1)
        self.assertEqual({item["program_name"] for item in updated["programs"]}, {"REQUEST-FLOW"})
        with closing(sqlite3.connect(database)) as connection:
            for table in ("source_files", "code_units", "symbols", "evidence_spans", "repo_sources", "repo_pages", "repo_source_state"):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table} WHERE relative_path=?", (changing.name,)).fetchone()[0], 0, table)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM code_units_fts WHERE program_name='OBSOLETE-FLOW'").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM repo_fts WHERE repo_fts MATCH 'obsoleteflowmarker'").fetchone()[0], 0)
        self.write(changing.name, program("REPAIRED-FLOW", "repairedflowmarker"))
        repaired = self.completed(self.intake())
        self.assertTrue(repaired["diagnosis"]["scope"]["input_coverage_complete"])
        self.assertEqual(repaired["diagnosis"]["build_report"]["input_skips"], [])
        with closing(sqlite3.connect(database)) as connection:
            self.assertGreater(connection.execute("SELECT COUNT(*) FROM code_units_fts WHERE program_name='REPAIRED-FLOW'").fetchone()[0], 0)
            self.assertGreater(connection.execute("SELECT COUNT(*) FROM repo_fts WHERE repo_fts MATCH 'repairedflowmarker'").fetchone()[0], 0)

    def test_restart_and_simulated_question_reuse_the_mixed_index_without_source_discovery(self):
        self.mixed_sources()
        indexed = self.completed(self.intake())
        original_open = Path.open

        def startup_open(path, *args, **kwargs):
            if path.resolve().is_relative_to(self.source):
                raise AssertionError("A restart must not read source contents.")
            return original_open(path, *args, **kwargs)

        with self.no_whole_source_intake():
            with mock.patch.object(Path, "open", startup_open):
                self.app = self.workbench()
            self.assertEqual(self.app.project["snapshot_id"], indexed["snapshot_id"])
            answered = self.completed(self.intake(question="Explain REQUEST-COUNT in entry.cbl.", allow_network=True))
        self.assertEqual(answered["snapshot_id"], indexed["snapshot_id"])
        self.assertTrue(answered["diagnosis"]["index_reused"])
        self.assertGreaterEqual(len(self.requests), 1)
        self.assertIn("positive REQUEST-COUNT", answered["conversation"]["messages"][-1]["content"])

    def test_real_success_and_failed_retry_preferences_survive_demo_and_restart(self):
        self.write("entry.cbl", program("REQUEST-FLOW"))
        reference = str(self.root / "first-guide.md")
        real = self.completed(self.intake(encoding="utf-8", extensions=".cbl", framework_reference_path=reference))
        conversation = self.app.new_conversation({})["conversation"]
        retry_source = self.root / "retry-source"
        retry_source.mkdir()
        retry_output = self.root / "retry-results"
        self.write("legacy.cbl", program("RETRY-FLOW", "中性說明"), "cp950", source=retry_source)
        retry_reference = str(self.root / "retry-guide.md")
        failed = self.intake(source=str(retry_source), output=str(retry_output), encoding="utf-8",
                             extensions=".cbl", framework_reference_path=retry_reference)
        self.assertEqual(failed["status"], "FAILED")
        expected = {"source": str(retry_source), "output": str(retry_output), "encoding": "utf-8",
                    "source_format": "free", "extensions": [".cbl"], "framework_reference_path": retry_reference}
        self.assertEqual(self.started[-1]["source_preferences"], expected)
        self.assertEqual(self.app.state()["source_preferences"], expected)
        demo = self.completed(self.wait(self.app.prepare_framework_demo({"case_id": "online", "locale": "en"})))
        self.assertEqual(demo["source_origin"], "synthetic_framework")
        self.assertEqual(self.app.state()["source_preferences"], expected)
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["source"], str(self.source))
        self.assertEqual(saved["output"], str(self.output))
        self.assertEqual(saved["source_preferences"], expected)
        self.app = self.workbench()
        self.assertEqual(self.app.project["snapshot_id"], real["snapshot_id"])
        self.assertEqual(self.app.conversation["id"], conversation["id"])
        self.assertEqual(self.app.state()["source_preferences"], expected)
        self.write("legacy.cbl", program("RETRY-FLOW"), source=retry_source)
        repaired = self.completed(self.intake(source=str(retry_source), output=str(retry_output), encoding="utf-8",
                                              extensions=".cbl", framework_reference_path=retry_reference))
        self.completed(self.wait(self.app.prepare_framework_demo({"case_id": "online", "locale": "en"})))
        self.app = self.workbench()
        self.assertEqual(self.app.project["snapshot_id"], repaired["snapshot_id"])
        self.assertEqual(self.app.project["source"], str(retry_source))
        self.assertEqual(self.app.state()["source_preferences"], expected)

    def test_a_first_failed_import_and_then_demo_leave_restart_empty_with_retry_inputs(self):
        self.write("legacy.cbl", program("RETRY-FLOW", "中性說明"), "cp950")
        failed = self.intake(encoding="utf-8", extensions=".cbl")
        self.assertEqual(failed["status"], "FAILED")
        expected = self.app.state()["source_preferences"]
        self.assertEqual(expected["source"], str(self.source))
        self.completed(self.wait(self.app.prepare_framework_demo({"case_id": "online", "locale": "en"})))
        restarted = self.workbench()
        self.assertIsNone(restarted.project["source"])
        self.assertIsNone(restarted.project["snapshot_id"])
        self.assertEqual(restarted.project["programs"], [])
        self.assertIsNone(restarted.conversation)
        self.assertEqual(restarted.state()["source_preferences"], expected)

    def test_legacy_saved_example_never_becomes_the_startup_workspace_or_real_retry_inputs(self):
        self.state_path.write_text(json.dumps({"source": str(FRAMEWORK_DEMO_SOURCE.resolve()),
            "output": str(self.root / "legacy-case-results"), "conversation_id": "old-example",
            "encoding": "utf-8", "source_format": "free"}), encoding="utf-8")
        restarted = self.workbench()
        self.assertIsNone(restarted.project["source"])
        self.assertIsNone(restarted.project["output"])
        self.assertIsNone(restarted.project["snapshot_id"])
        self.assertIsNone(restarted.conversation)
        self.assertIsNone(restarted.state()["source_preferences"])


if __name__ == "__main__":
    unittest.main()
