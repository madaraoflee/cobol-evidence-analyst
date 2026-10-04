"""A restart reuses a published source snapshot without another intake."""

from contextlib import ExitStack, closing, contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyze_source import analyze_source
from company_api import CompanyAPIConfig, TransportResponse
from conversation_store import ConversationStore
from report_view import DIRECT_REPORT_BYTES, VIEW_REPORT_BYTES, write_report_view
from source_session import QuestionSourceSession, refresh_selected_sources
from source_versions import record_version
from web_app import WorkbenchState


def program(name, outcome="ACCEPTED"):
    return ("IDENTIFICATION DIVISION.\n"
            f"PROGRAM-ID. {name}.\n"
            "DATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n"
            "01 REQUEST-AMOUNT PIC 9 VALUE 1.\n"
            "01 RESULT-STATE PIC X(12).\n"
            "PROCEDURE DIVISION.\n"
            "IF REQUEST-AMOUNT > ZERO\n"
            f"MOVE '{outcome}' TO RESULT-STATE\n"
            "END-IF.\n"
            "GOBACK.\n")


class WorkspaceRestartTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="workspace-restart-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "output"
        self.database = self.output / "structural-index.sqlite"
        self.state_path = self.root / "workspace.json"
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="local-test-key")
        self.requests = []
        (self.source / "entry.cbl").write_text(program("REQUEST-FLOW"), encoding="utf-8")
        (self.source / "separate.cbl").write_text(program("SEPARATE-FLOW"), encoding="utf-8")
        self.report = self.ingest()
        self.snapshot = self.report["build_report"]["snapshot_id"]
        self.assertTrue(self.report["source_manifest_verified"])
        self.conversation = ConversationStore(self.output, self.source).create(self.snapshot)
        self.version = record_version(self.output, self.source, self.snapshot)
        self.save_state()

    def ingest(self, **options):
        return analyze_source(self.source, self.output, encoding="utf-8", source_format="free",
            extensions=(".cbl",), analysis_mode="business", index_mode=options.pop("index_mode", "full"),
            reading_strategy="retrieval", verify_content=True, quiet=True,
            framework_reference_path=self.root / "missing-guide.md", **options)

    def save_state(self, **changes):
        saved = {"source": str(self.source), "output": str(self.output),
                 "conversation_id": self.conversation["id"], "answer_detail": "brief",
                 "framework_reference_path": str(self.root / "missing-guide.md")}
        saved.update(changes)
        self.state_path.write_text(json.dumps(saved), encoding="utf-8")

    def write_json(self, name, value):
        (self.output / name).write_text(json.dumps(value), encoding="utf-8")

    def read_json(self, name):
        return json.loads((self.output / name).read_text(encoding="utf-8"))

    def fingerprint(self, path):
        return path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()

    @contextmanager
    def no_intake(self):
        targets = ("analyze_source.build_business_index", "analyze_source.build_structural_index",
                   "analyze_source.ensure_repository_search", "analyze_source._verify_scope",
                   "business_index.build_business_index", "repository_discovery.ensure_repository_search",
                   "business_index.iter_source_files", "repo_inventory.iter_source_files")
        with ExitStack() as stack:
            for target in targets:
                stack.enter_context(mock.patch(target, side_effect=AssertionError("A restart or cached question must not import sources.")))
            yield

    @contextmanager
    def no_source_reads(self):
        original_open = Path.open
        source = self.source

        def guarded_open(path, *args, **kwargs):
            if path.resolve().is_relative_to(source):
                raise AssertionError("Startup must not read source contents.")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", guarded_open):
            yield

    def transport(self, request):
        envelope = json.loads(request.body)
        payload = json.loads(envelope["messages"][-1]["content"])
        self.requests.append(payload)
        pages = [page for context in payload.get("source_context", []) for page in context.get("pages", [])]
        self.assertTrue(pages, "The existing index should supply the requested program.")
        page = next(page for page in pages if page["relative_path"] == "entry.cbl")
        answer = f"A positive request amount sets the result state shown in the source. [{page['evidence_id']}]"
        return TransportResponse(200, json.dumps({"choices": [{"message": {
            "role": "assistant", "content": answer}, "finish_reason": "stop"}]}))

    def restart(self):
        def analyzer(source, output, **options):
            return analyze_source(source, output, transport=self.transport, quiet=True, **options)

        with self.no_intake(), self.no_source_reads():
            return WorkbenchState(state_path=self.state_path, config_provider=lambda: self.config,
                                  analyzer=analyzer)

    def assert_restored(self, app, snapshot=None):
        self.assertEqual(app.project["source"], str(self.source))
        self.assertEqual(app.project["output"], str(self.output))
        self.assertEqual(app.project["snapshot_id"], snapshot or self.snapshot)
        self.assertTrue(app.project["diagnosis"]["source_manifest_verified"])
        self.assertTrue(app.project["programs"])

    def assert_unavailable(self, app):
        self.assertIsNone(app.project["snapshot_id"])
        self.assertEqual(app.project["source"], str(self.source))
        self.assertEqual(app.project["output"], str(self.output))
        self.assertEqual(app.project["programs"], [])

    def test_restart_restores_paths_preferences_versions_and_conversation(self):
        app = self.restart()
        self.assert_restored(app)
        self.assertEqual(app.answer_detail, "brief")
        self.assertEqual(app.conversation["id"], self.conversation["id"])
        self.assertEqual(app.project["source_version"]["version_id"], self.version["version_id"])
        self.assertEqual(app.project["diagnosis"]["repository_search"]["snapshot_id"], self.snapshot)

    def test_healthy_restart_does_not_rewrite_complete_display_reports(self):
        paths = [self.output / name for name in ("diagnosis.json", "programs.json")]
        before = {path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
                  for path in paths}
        app = self.restart()
        self.assert_restored(app)
        after = {path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
                 for path in paths}
        self.assertEqual(after, before)

    def test_legacy_large_diagnosis_is_converted_once_without_changing_the_original(self):
        report = self.read_json("diagnosis.json")
        report["unresolved_dependencies"] = ["neutral source boundary " * 4096] * 220
        self.write_json("diagnosis.json", report)
        path = self.output / "diagnosis.json"
        view_path = self.output / "diagnosis-view.json"
        view_path.unlink(missing_ok=True)
        self.assertGreater(path.stat().st_size, DIRECT_REPORT_BYTES)
        before = self.fingerprint(path)
        app = self.restart()
        self.assert_restored(app)
        self.assertEqual(self.fingerprint(path), before)
        self.assertTrue(view_path.is_file())
        self.assertLess(view_path.stat().st_size, VIEW_REPORT_BYTES)
        self.assertTrue(app.project["diagnosis"]["display_projection"]["complete_report_on_disk"])
        original_open = Path.open

        def forbid_complete_diagnosis(path_to_open, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if path_to_open.resolve() == path and "b" not in mode:
                raise AssertionError("Later restarts must not parse the complete large diagnosis.")
            return original_open(path_to_open, *args, **kwargs)

        with mock.patch.object(Path, "open", forbid_complete_diagnosis), \
                mock.patch("report_view.project_report", side_effect=AssertionError("The existing view must be reused.")):
            restored_again = self.restart()
        self.assert_restored(restored_again)
        self.assertEqual(self.fingerprint(path), before)

    def test_projected_diagnosis_repair_preserves_complete_report_and_updates_programs(self):
        report = self.read_json("diagnosis.json")
        report["unresolved_dependencies"] = ["neutral source boundary " * 128] * 100
        self.write_json("diagnosis.json", report)
        path = self.output / "diagnosis.json"
        programs_path = self.output / "programs.json"
        before = self.fingerprint(path)
        previous_programs_hash = self.fingerprint(programs_path)[1]
        (self.source / "entry.cbl").write_text(program("REQUEST-FLOW", "REVIEW"), encoding="utf-8")
        with QuestionSourceSession(self.database, self.source) as session:
            changed = refresh_selected_sources(self.database, [session.capture("entry.cbl")],
                                               expected_snapshot_id=self.snapshot)
        current_snapshot = changed["snapshot_id"]
        self.assertNotEqual(current_snapshot, self.snapshot)
        with mock.patch("web_app.DIRECT_REPORT_BYTES", 10000), \
                mock.patch("report_view.DIRECT_REPORT_BYTES", 10000):
            write_report_view(path, report)
            app = self.restart()
        self.assert_restored(app, current_snapshot)
        self.assertTrue(app.project["diagnosis"]["display_projection"]["complete_report_on_disk"])
        self.assertEqual(app.project["diagnosis"]["build_report"]["snapshot_id"], current_snapshot)
        self.assertEqual(self.fingerprint(path), before)
        self.assertEqual(self.read_json("programs.json")["snapshot_id"], current_snapshot)
        self.assertNotEqual(self.fingerprint(programs_path)[1], previous_programs_hash)
        current_evidence = next(item["evidence_id"] for item in app.project["programs"]
                                if item["relative_path"] == "entry.cbl")
        self.assertEqual(app.evidence(current_evidence)["snapshot_id"], current_snapshot)

    def test_diagnosis_write_or_view_failure_does_not_block_programs_repair(self):
        from analyze_source import _write

        diagnosis = (self.output / "diagnosis.json").read_bytes()
        programs = (self.output / "programs.json").read_bytes()
        (self.source / "entry.cbl").write_text(program("REQUEST-FLOW", "REVIEW"), encoding="utf-8")
        with QuestionSourceSession(self.database, self.source) as session:
            changed = refresh_selected_sources(self.database, [session.capture("entry.cbl")],
                                               expected_snapshot_id=self.snapshot)
        current_snapshot = changed["snapshot_id"]
        for operation in ("write", "view"):
            with self.subTest(operation=operation):
                (self.output / "diagnosis.json").write_bytes(diagnosis)
                (self.output / "programs.json").write_bytes(programs)
                failures = []

                def failing_write(path, value, **options):
                    if path.name == "diagnosis.json":
                        failures.append(path.name)
                        raise OSError("diagnosis write unavailable")
                    return _write(path, value, **options)

                def failing_view(path, report):
                    if path.name == "diagnosis.json":
                        failures.append(path.name)
                        raise OSError("diagnosis view unavailable")
                    return write_report_view(path, report)

                patch = mock.patch("analyze_source._write", side_effect=failing_write) if operation == "write" else \
                    mock.patch("report_view.write_report_view", side_effect=failing_view)
                with patch:
                    app = self.restart()
                self.assert_restored(app, current_snapshot)
                self.assertEqual(failures, ["diagnosis.json"])
                repaired = self.read_json("programs.json")
                self.assertEqual(repaired["snapshot_id"], current_snapshot)
                current_evidence = next(item["evidence_id"] for item in repaired["programs"]
                                        if item["relative_path"] == "entry.cbl")
                self.assertEqual(app.evidence(current_evidence)["snapshot_id"], current_snapshot)

    def test_incomplete_program_rows_are_recovered_even_when_the_snapshot_matches(self):
        self.write_json("programs.json", {"snapshot_id": self.snapshot, "programs": [{}]})
        app = self.restart()
        self.assert_restored(app)
        repaired = self.read_json("programs.json")
        self.assertEqual(repaired["snapshot_id"], self.snapshot)
        self.assertEqual({item["program_name"] for item in repaired["programs"]},
                         {"REQUEST-FLOW", "SEPARATE-FLOW"})
        self.assertTrue(all(item["start_line"] > 0 and item["evidence_id"] for item in repaired["programs"]))

    def test_selected_file_refresh_repairs_reports_and_next_question_reuses_index(self):
        stale_programs = self.read_json("programs.json")
        previous_evidence = next(item["evidence_id"] for item in stale_programs["programs"]
                                 if item["relative_path"] == "entry.cbl")
        (self.source / "entry.cbl").write_text(program("REQUEST-FLOW", "REVIEW"), encoding="utf-8")
        with QuestionSourceSession(self.database, self.source) as session:
            changed = refresh_selected_sources(self.database, [session.capture("entry.cbl")],
                                               expected_snapshot_id=self.snapshot)
        current_snapshot = changed["snapshot_id"]
        self.assertNotEqual(current_snapshot, self.snapshot)
        self.assertEqual(self.read_json("diagnosis.json")["build_report"]["snapshot_id"], self.snapshot)
        app = self.restart()
        self.assert_restored(app, current_snapshot)
        current_evidence = next(item["evidence_id"] for item in app.project["programs"]
                                if item["relative_path"] == "entry.cbl")
        self.assertNotEqual(current_evidence, previous_evidence)
        self.assertEqual(app.evidence(current_evidence)["snapshot_id"], current_snapshot)
        self.assertEqual(self.read_json("diagnosis.json")["build_report"]["snapshot_id"], current_snapshot)
        self.assertEqual(self.read_json("programs.json")["snapshot_id"], current_snapshot)
        self.assertEqual(app.project["diagnosis"]["scope"], self.report["scope"])
        options = app.project["diagnosis"]["source_options"]
        with self.no_intake():
            job = app.start({"source": str(self.source), "output": str(self.output),
                             "question": "Explain REQUEST-FLOW", "allow_network": True,
                             "encoding": options["encoding"], "source_format": options["source_format"],
                             "extensions": ",".join(options["extensions"]),
                             "conversation_id": self.conversation["id"]})
            deadline = time.monotonic() + 10
            result = app.get_job(job["job_id"])
            while result["status"] == "RUNNING" and time.monotonic() < deadline:
                time.sleep(0.005)
                result = app.get_job(job["job_id"])
        self.assertEqual(result["status"], "COMPLETED", result.get("error"))
        self.assertEqual(result["result"]["diagnosis"]["reason_code"], "SOURCE_INDEX_REUSED")
        self.assertTrue(result["result"]["diagnosis"]["index_reused"])
        self.assertEqual(result["result"]["agent"]["runner_status"], "COMPLETED")
        self.assertEqual(result["result"]["agent"]["agent_result"]["snapshot_id"], current_snapshot)
        self.assertTrue(self.requests)

    def test_programs_missing_invalid_or_from_another_snapshot_are_recovered_from_sql(self):
        program_path = self.output / "programs.json"
        original = program_path.read_bytes()
        for mode in ("missing", "invalid", "array", "old_snapshot"):
            with self.subTest(mode=mode):
                program_path.write_bytes(original)
                if mode == "missing":
                    program_path.unlink()
                elif mode == "invalid":
                    program_path.write_text("{invalid", encoding="utf-8")
                elif mode == "array":
                    self.write_json("programs.json", [])
                else:
                    self.write_json("programs.json", {"snapshot_id": "sha256:older-display", "programs": [
                        {"program_name": "OLDER-DISPLAY", "relative_path": "absent.cbl", "evidence_id": "ev_absent"}]})
                app = self.restart()
                self.assert_restored(app)
                self.assertEqual({item["program_name"] for item in app.project["programs"]},
                                 {"REQUEST-FLOW", "SEPARATE-FLOW"})
                self.assertEqual(self.read_json("programs.json")["snapshot_id"], self.snapshot)

    def test_import_options_are_restored_from_sql_instead_of_stale_display(self):
        report = self.read_json("diagnosis.json")
        report["source_options"].update(encoding="auto", source_format="fixed", extensions=[".old"],
                                        include_extensionless=True)
        self.write_json("diagnosis.json", report)
        app = self.restart()
        self.assert_restored(app)
        with closing(sqlite3.connect(self.database)) as db:
            expected = json.loads(db.execute("SELECT value FROM metadata WHERE key='source_options'").fetchone()[0])
        for key in ("extensions", "include_extensionless", "encoding", "source_format"):
            self.assertEqual(app.project["diagnosis"]["source_options"][key], expected[key])

    def test_missing_or_invalid_agent_report_does_not_discard_the_workspace(self):
        path = self.output / "agent-result.json"
        for mode in ("missing", "invalid"):
            with self.subTest(mode=mode):
                if mode == "missing":
                    path.unlink(missing_ok=True)
                else:
                    path.write_text("{invalid", encoding="utf-8")
                app = self.restart()
                self.assert_restored(app)
                self.assertEqual(app.conversation["id"], self.conversation["id"])
                self.assertFalse((app.project.get("agent") or {}).get("agent_result"))

    def test_deleted_conversation_does_not_discard_the_workspace(self):
        with closing(sqlite3.connect(self.output / "conversations.sqlite")) as db, db:
            db.execute("DELETE FROM conversations WHERE id=?", (self.conversation["id"],))
        app = self.restart()
        self.assert_restored(app)
        self.assertIsNone(app.conversation)
        self.assertEqual(app.project["source_version"]["version_id"], self.version["version_id"])

    def test_invalid_conversation_store_does_not_discard_the_workspace(self):
        (self.output / "conversations.sqlite").write_bytes(b"invalid conversation database")
        app = self.restart()
        self.assert_restored(app)
        self.assertIsNone(app.conversation)

    def test_version_list_error_does_not_discard_the_workspace(self):
        with mock.patch("web_app.versions", side_effect=sqlite3.DatabaseError("invalid version database")):
            app = self.restart()
        self.assert_restored(app)
        self.assertEqual(app.conversation["id"], self.conversation["id"])
        self.assertEqual(app.project["source_versions"], [])
        self.assertIsNone(app.project["source_version"])

    def test_corrupt_version_store_is_retained_while_the_workspace_is_restored(self):
        path = self.output / "source-versions.sqlite"
        contents = b"invalid version database"
        path.write_bytes(contents)
        before = path.stat().st_mtime_ns
        app = self.restart()
        self.assert_restored(app)
        self.assertEqual(app.project["source_versions"], [])
        self.assertIsNone(app.project["source_version"])
        self.assertEqual(path.read_bytes(), contents)
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_agent_from_another_snapshot_does_not_authorize_old_citations(self):
        self.write_json("agent-result.json", {"runner_status": "COMPLETED", "agent_result": {
            "snapshot_id": "sha256:older-answer", "answer": "Earlier explanation. [ev_old_answer]",
            "status": "ANALYZED", "evidence_refs": [{"evidence_id": "ev_old_answer"}]}})
        app = self.restart()
        self.assert_restored(app)
        self.assertIsNone(app.project["agent"]["agent_result"])
        self.assertEqual(app.project["agent"]["reason_code"], "ANSWER_SNAPSHOT_MISMATCH")

    def test_failed_or_cancelled_intake_does_not_authorize_an_old_ready_index(self):
        for status in ("BLOCKED", "CANCELLED"):
            with self.subTest(status=status):
                report = dict(self.report)
                report.update(runner_status=status, source_manifest_verified=False)
                self.write_json("diagnosis.json", report)
                self.assert_unavailable(self.restart())

    def test_invalid_diagnosis_or_wrong_report_source_does_not_authorize_the_index(self):
        for mode in ("array", "wrong_source"):
            with self.subTest(mode=mode):
                report = dict(self.report)
                if mode == "array":
                    report = []
                else:
                    report["source_root"] = str(self.root / "another-source")
                self.write_json("diagnosis.json", report)
                self.assert_unavailable(self.restart())

    def test_entry_scope_is_not_promoted_to_full_repository_on_restart(self):
        report = self.ingest(index_mode="catalog", entry="REQUEST-FLOW")
        self.snapshot = report["build_report"]["snapshot_id"]
        self.assertEqual(report["scope"]["mode"], "entry_static_closure")
        app = self.restart()
        self.assert_restored(app)
        self.assertEqual(app.project["diagnosis"]["scope"], report["scope"])
        self.assertEqual(app.project["diagnosis"]["scope"]["mode"], "entry_static_closure")

    def test_missing_or_wrong_source_hash_does_not_authorize_the_index(self):
        for value in (None, "another-root"):
            with self.subTest(value=value):
                with closing(sqlite3.connect(self.database)) as db, db:
                    if value is None:
                        db.execute("DELETE FROM metadata WHERE key='source_root_hash'")
                    else:
                        db.execute("INSERT OR REPLACE INTO metadata VALUES ('source_root_hash',?)", (value,))
                self.assert_unavailable(self.restart())

    def test_unpublished_repository_search_does_not_authorize_the_index(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE repo_metadata SET value='0' WHERE key='ready'")
        self.assert_unavailable(self.restart())

    def test_repository_snapshot_mismatch_does_not_authorize_the_index(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE repo_metadata SET value='sha256:other-search' WHERE key='snapshot_id'")
        self.assert_unavailable(self.restart())

    def test_search_overview_snapshot_mismatch_does_not_authorize_the_index(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            overview = json.loads(db.execute("SELECT value FROM repo_metadata WHERE key='overview'").fetchone()[0])
            overview["snapshot_id"] = "sha256:other-overview"
            db.execute("UPDATE repo_metadata SET value=? WHERE key='overview'", (json.dumps(overview),))
        self.assert_unavailable(self.restart())

    def test_array_workspace_state_is_ignored_without_crashing_startup(self):
        self.state_path.write_text("[]", encoding="utf-8")
        app = self.restart()
        self.assertIsNone(app.project["snapshot_id"])

    def test_missing_source_directory_does_not_create_files_or_authorize_the_index(self):
        shutil.rmtree(self.source)
        before = {path.relative_to(self.output) for path in self.output.rglob("*")}
        app = self.restart()
        self.assert_unavailable(app)
        self.assertFalse(self.source.exists())
        self.assertEqual({path.relative_to(self.output) for path in self.output.rglob("*")}, before)

    def test_missing_database_does_not_create_a_replacement_or_authorize_the_index(self):
        self.database.unlink()
        before = {path.relative_to(self.output) for path in self.output.rglob("*")}
        app = self.restart()
        self.assert_unavailable(app)
        self.assertFalse(self.database.exists())
        self.assertEqual({path.relative_to(self.output) for path in self.output.rglob("*")}, before)

    def test_missing_output_directory_is_retained_as_input_without_recreating_it(self):
        shutil.rmtree(self.output)
        app = self.restart()
        self.assert_unavailable(app)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
