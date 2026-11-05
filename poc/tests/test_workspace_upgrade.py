"""Automatic parser upgrades retain the original local workspace contract."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from analyze_source import analyze_source
from compilation_context import resolve_compilation_context
from conversation_store import ConversationStore
from repo_inventory import DEFAULT_EXTENSIONS
from workspace_upgrade import plan_workspace_upgrade


class WorkspaceUpgradeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source, self.output = self.root / "source", self.root / "output"
        self.source.mkdir()
        (self.source / "amount.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. AMOUNT-RULE.\n"
            "PROCEDURE DIVISION.\nDISPLAY 'AMOUNT CHECK'.\nGOBACK.\n", encoding="utf-8")
        analyze_source(self.source, self.output, source_format="free", analysis_mode="business",
                       allow_network=False)
        self.database = self.output / "structural-index.sqlite"
        self.saved = {"source": str(self.source), "output": str(self.output),
                      "restore_blocked": False, "answer_detail": "brief",
                      "source_preferences": {"source": str(self.source), "output": str(self.output),
                          "source_format": "free", "encoding": "auto", "extensions": sorted(DEFAULT_EXTENSIONS)}}

    def set_metadata(self, key, value, table="metadata"):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?)", (key, value))

    def set_old_parser(self):
        self.set_metadata("parser_version", "business-sparse-v1.8")

    def source_options(self):
        with closing(sqlite3.connect(self.database)) as db:
            return json.loads(db.execute("SELECT value FROM metadata WHERE key='source_options'").fetchone()[0])

    def assert_manual(self, reason=None):
        plan = plan_workspace_upgrade(self.saved)
        self.assertEqual(plan["status"], "manual", plan)
        self.assertNotIn("options", plan)
        if reason:
            self.assertEqual(plan["reason_code"], reason)

    def test_current_index_does_not_start_an_import(self):
        self.assertEqual(plan_workspace_upgrade(self.saved),
                         {"status": "current", "reason_code": "SOURCE_INDEX_CURRENT"})

    def test_known_old_parser_plans_offline_update_without_mutating_existing_data(self):
        self.set_old_parser()
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        plan = plan_workspace_upgrade(self.saved)
        self.assertEqual(plan["status"], "ready", plan)
        request = plan["options"]
        self.assertEqual(request["source"], str(self.source))
        self.assertEqual(request["output"], str(self.output))
        self.assertEqual(request["source_format"], "free")
        self.assertEqual(request["answer_detail"], "brief")
        self.assertFalse(request["allow_network"])
        self.assertIsNone(request["question"])
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)

    def test_finished_local_upgrade_is_not_repeated_on_the_next_start(self):
        self.set_old_parser()
        plan = plan_workspace_upgrade(self.saved)
        self.assertEqual(plan["status"], "ready")
        from web_app import _validate_options
        options = _validate_options(plan["options"])
        source, output = options.pop("source"), options.pop("output")
        options.pop("retry_message_id")
        analyze_source(source, output, **options)
        self.assertEqual(plan_workspace_upgrade(self.saved)["status"], "current")

    def test_previous_parser_upgrades_to_two_digit_version_offline(self):
        self.set_metadata("parser_version", "business-sparse-v1.9")
        original_inputs = self.source_options()
        plan = plan_workspace_upgrade(self.saved)
        self.assertEqual(plan["status"], "ready", plan)
        self.assertEqual(plan["parser_version"], "business-sparse-v1.9")
        self.assertFalse(plan["options"]["allow_network"])
        self.assertIsNone(plan["options"]["question"])
        from web_app import _validate_options
        options = _validate_options(plan["options"])
        source, output = options.pop("source"), options.pop("output")
        options.pop("retry_message_id")
        analyze_source(source, output, **options)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='parser_version'").fetchone()[0],
                             "business-sparse-v1.10")
        self.assertEqual(self.source_options(), original_inputs)
        self.assertEqual(plan_workspace_upgrade(self.saved)["status"], "current")

    def test_unknown_search_version_and_incomplete_search_require_explicit_recovery(self):
        self.set_metadata("version", "repository-text-v0", "repo_metadata")
        self.assert_manual("SOURCE_INDEX_VERSION_UNSUPPORTED")
        self.set_metadata("ready", "0", "repo_metadata")
        self.assert_manual("SOURCE_SEARCH_RECOVERY_REQUIRED")

    def test_future_or_unknown_versions_do_not_trigger_a_downgrade(self):
        for version in ("business-sparse-v2", "unknown", "business-sparse-v1.9-preview", "business-sparse-v1.3",
                        "business-sparse-v1.11", "business-sparse-v1.10-preview"):
            with self.subTest(version=version):
                self.set_metadata("parser_version", version)
                self.assert_manual("SOURCE_INDEX_VERSION_UNSUPPORTED")

    def test_different_source_owner_is_never_rebuilt(self):
        self.set_old_parser()
        self.set_metadata("source_root_hash", "0" * 64)
        self.assert_manual("SOURCE_ROOT_MISMATCH")

    def test_pending_or_failed_rollback_is_never_retried_automatically(self):
        self.set_old_parser()
        self.saved["restore_blocked"] = True
        self.assert_manual("SOURCE_UPDATE_RECOVERY_REQUIRED")
        self.saved["restore_blocked"] = False
        (self.output / "source-update-pending.json").write_text("{}", encoding="utf-8")
        self.assert_manual("SOURCE_UPDATE_RECOVERY_REQUIRED")

    def test_missing_and_corrupt_indexes_are_not_created_or_repaired_by_planning(self):
        self.database.unlink()
        self.assert_manual("SOURCE_INDEX_UNAVAILABLE")
        self.assertFalse(self.database.exists())
        self.database.write_bytes(b"not a database")
        self.assert_manual("SOURCE_INDEX_INVALID")
        self.assertEqual(self.database.read_bytes(), b"not a database")

    def test_user_source_setting_changes_are_not_silently_applied(self):
        self.set_old_parser()
        for key, value in (("source_format", "fixed"), ("encoding", "utf-8"),
                           ("extensions", [".cpy"]), ("source", str(self.root / "different-source"))):
            with self.subTest(setting=key):
                original = self.saved["source_preferences"][key]
                self.saved["source_preferences"][key] = value
                self.assert_manual("SOURCE_OPTIONS_CHANGED")
                self.saved["source_preferences"][key] = original

    def test_custom_compilation_conditions_require_exact_reproducible_context(self):
        self.set_old_parser()
        context_file = self.root / "compilation.json"
        context_input = {"compile_conditions": {"BATCH": True}, "conditions_complete": True,
                         "copy_libraries": ["copybooks"], "copy_library_order_known": True}
        context_file.write_text(json.dumps(context_input), encoding="utf-8")
        self.saved["source_preferences"]["compilation_context_path"] = str(context_file)
        options = self.source_options()
        options["compilation_context"] = resolve_compilation_context(context_input, source_options=options).to_dict()
        self.set_metadata("source_options", json.dumps(options))
        plan = plan_workspace_upgrade(self.saved)
        self.assertEqual(plan["status"], "ready", plan)
        self.assertEqual(plan["options"]["compilation_context_path"], str(context_file))
        context_file.write_text(json.dumps({**context_input, "compile_conditions": {"BATCH": False}}), encoding="utf-8")
        self.assert_manual("COMPILATION_CONTEXT_CHANGED")
        context_file.unlink()
        self.assert_manual("COMPILATION_CONTEXT_UNAVAILABLE")
        del self.saved["source_preferences"]["compilation_context_path"]
        self.assert_manual("COMPILATION_CONTEXT_CHANGED")

    def test_recorded_frontend_or_framework_context_is_not_replaced_by_defaults(self):
        self.set_old_parser()
        options = self.source_options()
        options["compilation_context"]["framework_version"] = "local-rules-v1"
        self.set_metadata("source_options", json.dumps(options))
        self.assert_manual("COMPILATION_CONTEXT_CHANGED")

    def test_previous_release_internal_frontend_can_upgrade_without_changing_compiler_inputs(self):
        self.set_metadata("parser_version", "business-sparse-v1.7")
        self.set_metadata("schema_version", "0.3")
        options = self.source_options()
        options["compilation_context"]["frontend_version"] = "cobol-facts-v5"
        self.set_metadata("source_options", json.dumps(options))
        self.assertEqual(plan_workspace_upgrade(self.saved)["status"], "ready")
        options["compilation_context"]["conditions_complete"] = True
        self.set_metadata("source_options", json.dumps(options))
        self.assert_manual("COMPILATION_CONTEXT_CHANGED")

    def test_unknown_or_future_schema_is_not_assumed_to_be_compatible(self):
        self.set_old_parser()
        for version in ("0.2", "0.5", "unknown"):
            with self.subTest(version=version):
                self.set_metadata("schema_version", version)
                self.assert_manual("SOURCE_INDEX_KIND_UNSUPPORTED")

    def test_failed_upgrade_attempt_is_bound_to_the_snapshot_and_known_compatible_target(self):
        self.set_old_parser()
        plan = plan_workspace_upgrade(self.saved)
        self.saved["index_upgrade_attempt"] = plan["attempt_key"]
        self.assert_manual("INDEX_UPGRADE_PREVIOUS_ATTEMPT")
        with patch("workspace_upgrade.PARSER_VERSION", "business-sparse-v1.11"):
            self.assert_manual("SOURCE_INDEX_VERSION_UNSUPPORTED")
        source_file = self.source / "amount.cbl"
        source_file.write_text(source_file.read_text().replace("AMOUNT CHECK", "UPDATED CHECK"), encoding="utf-8")
        analyze_source(self.source, self.output, source_format="free", analysis_mode="business",
                       allow_network=False)
        self.set_old_parser()
        self.assertEqual(plan_workspace_upgrade(self.saved)["status"], "ready")

    def test_previous_attempt_skips_integrity_and_source_table_scan_on_restart(self):
        self.set_old_parser()
        queries = []
        connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(queries.append)
            return connection

        with patch("workspace_upgrade.sqlite3.connect", side_effect=traced_connect):
            plan = plan_workspace_upgrade(self.saved)
        self.assertEqual(plan["status"], "ready")
        self.assertTrue(any("quick_check" in query.lower() for query in queries))
        self.assertTrue(any("from source_files" in query.lower() for query in queries))
        self.saved["index_upgrade_attempt"] = plan["attempt_key"]
        queries.clear()
        with patch("workspace_upgrade.sqlite3.connect", side_effect=traced_connect):
            self.assert_manual("INDEX_UPGRADE_PREVIOUS_ATTEMPT")
        self.assertFalse(any("quick_check" in query.lower() for query in queries))
        self.assertFalse(any("from source_files" in query.lower() for query in queries))

    def test_revalidation_preserves_scope_checks_without_repeating_integrity_scan(self):
        self.set_old_parser()
        checked = plan_workspace_upgrade(self.saved)
        queries = []
        connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(queries.append)
            return connection

        with patch("workspace_upgrade.sqlite3.connect", side_effect=traced_connect):
            repeated = plan_workspace_upgrade(self.saved, check_integrity=False)
        self.assertEqual(repeated, checked)
        self.assertFalse(any("quick_check" in query.lower() for query in queries))
        self.assertFalse(any("from source_files" in query.lower() for query in queries))
        report_path = self.output / "diagnosis.json"
        report = json.loads(report_path.read_text())
        report["build_report"]["snapshot_id"] = "sha256:" + "a" * 64
        report_path.write_text(json.dumps(report), encoding="utf-8")
        self.assertEqual(plan_workspace_upgrade(self.saved, check_integrity=False)["reason_code"],
                         "SOURCE_SCOPE_NOT_REPRODUCIBLE")

    def test_final_integrity_check_rejects_metadata_changed_since_planning(self):
        self.set_old_parser()
        connect = sqlite3.connect
        opened = 0
        queries = []

        def changed_connect(*args, **kwargs):
            nonlocal opened
            opened += 1
            if opened == 2:
                with closing(connect(self.database)) as db, db:
                    db.execute("UPDATE metadata SET value=? WHERE key='source_root_hash'", ("0" * 64,))
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(queries.append)
            return connection

        with patch("workspace_upgrade.sqlite3.connect", side_effect=changed_connect):
            self.assert_manual("SOURCE_INDEX_CHANGED_DURING_UPGRADE_CHECK")
        self.assertEqual(opened, 2)
        self.assertFalse(any("quick_check" in query.lower() for query in queries))

    def test_entry_scoped_library_is_not_expanded_during_automatic_upgrade(self):
        (self.source / "unrelated.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. OTHER-RULE.\n"
            "PROCEDURE DIVISION.\nDISPLAY 'OTHER CHECK'.\nGOBACK.\n", encoding="utf-8")
        analyze_source(self.source, self.output, entry="AMOUNT-RULE", index_mode="catalog",
                       source_format="free", analysis_mode="business", allow_network=False)
        self.set_old_parser()
        with closing(sqlite3.connect(self.database)) as db:
            original_paths = tuple(db.execute("SELECT relative_path FROM source_files"))
        self.assertEqual(original_paths, (("amount.cbl",),))
        report = json.loads((self.output / "diagnosis.json").read_text())
        self.assertEqual(report["scope"]["mode"], "entry_static_closure")
        self.assert_manual("SOURCE_SCOPE_NOT_REPRODUCIBLE")
        from web_app import WorkbenchState
        state_path = self.root / "scoped-workspace.json"
        state_path.write_text(json.dumps(self.saved), encoding="utf-8")
        analyzer = unittest.mock.Mock(side_effect=AssertionError("must not expand selected source scope"))
        app = WorkbenchState(state_path=state_path, analyzer=analyzer)
        self.assertIsNone(app.job)
        analyzer.assert_not_called()
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(tuple(db.execute("SELECT relative_path FROM source_files")), original_paths)

    def test_unknown_scope_or_report_for_another_snapshot_cannot_authorize_full_import(self):
        self.set_old_parser()
        report_path = self.output / "diagnosis.json"
        original = json.loads(report_path.read_text())
        for change in ("missing_scope", "different_snapshot", "different_root"):
            with self.subTest(change=change):
                report = json.loads(json.dumps(original))
                if change == "missing_scope":
                    report["build_report"].pop("scope")
                elif change == "different_snapshot":
                    report["build_report"]["snapshot_id"] = "sha256:" + "a" * 64
                else:
                    report["source_root"] = str(self.root / "different-source")
                report_path.write_text(json.dumps(report), encoding="utf-8")
                self.assert_manual("SOURCE_SCOPE_NOT_REPRODUCIBLE")

    def test_malformed_large_report_projection_cannot_crash_startup_planning(self):
        self.set_old_parser()
        for projection in (None, [], "invalid"):
            with self.subTest(projection=projection), patch("workspace_upgrade.DIRECT_REPORT_BYTES", 1):
                (self.output / "diagnosis-view.json").write_text(
                    json.dumps({"display_projection": projection}), encoding="utf-8")
                self.assert_manual("SOURCE_SCOPE_NOT_REPRODUCIBLE")

    def test_display_repair_marker_cannot_be_used_as_original_scope_proof(self):
        self.set_old_parser()
        report_path = self.output / "diagnosis.json"
        report = json.loads(report_path.read_text())
        report["index_upgrade_scope_unverified"] = True
        report_path.write_text(json.dumps(report), encoding="utf-8")
        self.assert_manual("SOURCE_SCOPE_NOT_REPRODUCIBLE")

    def test_unrepresentable_extensionless_scope_requires_manual_recovery(self):
        self.set_old_parser()
        options = self.source_options()
        options["include_extensionless"] = False
        self.set_metadata("source_options", json.dumps(options))
        self.assert_manual("SOURCE_OPTIONS_NOT_REPRODUCIBLE")

    def prepare_saved_history(self):
        self.set_metadata("parser_version", "business-sparse-v1.7")
        self.set_metadata("schema_version", "0.3")
        options = self.source_options()
        options["compilation_context"]["frontend_version"] = "cobol-facts-v5"
        self.set_metadata("source_options", json.dumps(options))
        store = ConversationStore(self.output, self.source)
        conversation = store.create()
        store.append(conversation["id"], "user", "What does the amount check do?")
        store.append(conversation["id"], "assistant", "A retained business explanation.")
        self.saved["conversation_id"] = conversation["id"]
        state_path = self.root / "workbench-state.json"
        state_path.write_text(json.dumps(self.saved), encoding="utf-8")
        (self.output / "business-notes.md").write_text("Keep this local export.\n", encoding="utf-8")
        return state_path, store.get(conversation["id"])

    def open_and_wait(self, state_path, *, failing=False):
        from web_app import WorkbenchState
        calls, threads = [], []

        def analyzer(source, output, **options):
            calls.append(options)
            threads.append(threading.current_thread())
            if failing:
                (output / "diagnosis.json").write_text("temporary interrupted report", encoding="utf-8")
                raise ValueError("SYNTHETIC_INDEX_UPGRADE_FAILURE")
            return analyze_source(source, output, **options)

        with patch("company_api.OpenAICompatibleChatClient.__init__",
                   side_effect=AssertionError("no model calls during automatic upgrade")):
            app = WorkbenchState(state_path=state_path, analyzer=analyzer)
            try:
                deadline = time.monotonic() + 10
                while app.job and app.job["status"] == "RUNNING" and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(app.job and app.job["status"] == "RUNNING")
            finally:
                app.cancel_event.set()
                for thread in threads:
                    thread.join(timeout=10)
                    self.assertFalse(thread.is_alive())
        return app, calls

    def test_startup_upgrade_keeps_history_and_exports_and_does_not_repeat(self):
        state_path, conversation = self.prepare_saved_history()
        app, calls = self.open_and_wait(state_path)
        self.assertEqual(app.job["status"], "COMPLETED", app.job)
        self.assertEqual(app.job["origin"], "index_version_upgrade")
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["allow_network"])
        self.assertIsNone(calls[0]["question"])
        self.assertEqual(app.conversation, conversation)
        self.assertEqual((self.output / "business-notes.md").read_text(), "Keep this local export.\n")
        restarted, second_calls = self.open_and_wait(state_path)
        self.assertEqual(second_calls, [])
        self.assertIsNone(restarted.job)
        self.assertEqual(restarted.conversation, conversation)

    def test_failed_upgrade_stays_bound_to_its_library_across_switch_and_restart(self):
        state_path, conversation = self.prepare_saved_history()
        other_source = self.root / "other-source"
        other_source.mkdir()
        (other_source / "amount.cbl").write_bytes((self.source / "amount.cbl").read_bytes())
        other_output = self.root / "other-output"
        analyze_source(other_source, other_output, source_format="free", analysis_mode="business",
                       allow_network=False)
        other = {"source": str(other_source), "output": str(other_output)}
        self.saved["source_workspaces"] = [other]
        state_path.write_text(json.dumps(self.saved), encoding="utf-8")

        failed, calls = self.open_and_wait(state_path, failing=True)
        self.assertEqual(failed.job["status"], "FAILED")
        self.assertEqual(len(calls), 1)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        attempt = state["index_upgrade_attempt"]
        # Upgrade an older profile which kept the marker only at top level.
        for workspace in state["source_workspaces"]:
            workspace.pop("index_upgrade_attempt", None)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        reopened, calls = self.open_and_wait(state_path, failing=True)
        self.assertIsNone(reopened.job)
        self.assertEqual(calls, [])
        original_key = reopened.project["workspace_id"]
        other_key = reopened._workspace_record(other)["workspace_id"]
        reopened.open_source_workspace({"workspace_id": other_key})
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotIn("index_upgrade_attempt", state)
        registry = {entry["workspace_id"]: entry for entry in state["source_workspaces"]}
        self.assertEqual(registry[original_key]["index_upgrade_attempt"], attempt)
        self.assertNotIn("index_upgrade_attempt", registry[other_key])
        self.assertTrue(reopened._cached_source_ready(registry[other_key]))
        self.assertFalse(reopened._cached_source_ready(registry[original_key]))

        reopened.open_source_workspace({"workspace_id": original_key})
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["index_upgrade_attempt"], attempt)
        restored, calls = self.open_and_wait(state_path, failing=True)
        self.assertEqual(calls, [])
        self.assertIsNone(restored.job)
        self.assertEqual(restored.conversation, conversation)

    def test_startup_checks_integrity_once_in_the_update_worker(self):
        state_path, conversation = self.prepare_saved_history()
        queries = []
        connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(queries.append)
            return connection

        with patch("workspace_upgrade.sqlite3.connect", side_effect=traced_connect):
            app, calls = self.open_and_wait(state_path)
        self.assertEqual(app.job["status"], "COMPLETED", app.job)
        self.assertEqual(len(calls), 1)
        self.assertEqual(app.conversation, conversation)
        self.assertEqual(sum("quick_check" in query.lower() for query in queries), 1)

    def test_slow_upgrade_integrity_check_does_not_block_opening_retained_history(self):
        from web_app import WorkbenchState
        state_path, conversation = self.prepare_saved_history()
        entered, release, opened = threading.Event(), threading.Event(), threading.Event()
        apps, errors = [], []
        integrity_threads = []
        analyzer = unittest.mock.Mock(side_effect=AssertionError("the integrity check is still blocked"))

        def slow_plan(saved, **options):
            if options.get("check_integrity", True):
                integrity_threads.append(threading.current_thread())
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test did not release integrity check")
            return plan_workspace_upgrade(saved, **options)

        def open_workspace():
            try:
                apps.append(WorkbenchState(state_path=state_path, analyzer=analyzer))
            except BaseException as error:
                errors.append(error)
            finally:
                opened.set()

        with patch("workspace_upgrade.plan_workspace_upgrade", side_effect=slow_plan), \
             patch("web_app.SourceUpdateBackup") as backup:
            opener = threading.Thread(target=open_workspace)
            opener.start()
            try:
                self.assertTrue(entered.wait(2), "upgrade check was not scheduled")
                self.assertTrue(opened.wait(2), "opening still waits for the full database scan")
                self.assertEqual(errors, [])
                app = apps[0]
                self.assertEqual(app.conversation, conversation)
                self.assertEqual(app.state()["job"]["status"], "RUNNING")
                self.assertIsNot(integrity_threads[0], opener)
                analyzer.assert_not_called()
                backup.assert_not_called()
                # Cancelling before integrity has passed cannot touch indexes.
                app.cancel(app.job["job_id"])
            finally:
                release.set()
                opener.join(timeout=5)
                for worker in integrity_threads:
                    if worker is not opener:
                        worker.join(timeout=5)
        self.assertEqual(apps[0].job["status"], "CANCELLED")
        analyzer.assert_not_called()
        backup.assert_not_called()

    def test_worker_rejects_changed_upgrade_identity_before_any_backup_or_parse(self):
        from web_app import WorkbenchState
        state_path, _ = self.prepare_saved_history()
        analyzer = unittest.mock.Mock(side_effect=AssertionError("changed index cannot be rebuilt"))
        check_threads = []

        def changed_plan(saved, **options):
            plan = plan_workspace_upgrade(saved, **options)
            if options.get("check_integrity", True):
                check_threads.append(threading.current_thread())
                return {**plan, "attempt_key": "different-checked-index"}
            return plan

        with patch("workspace_upgrade.plan_workspace_upgrade", side_effect=changed_plan), \
             patch("web_app.SourceUpdateBackup") as backup:
            app = WorkbenchState(state_path=state_path, analyzer=analyzer)
            deadline = time.monotonic() + 5
            while app.job["status"] == "RUNNING" and time.monotonic() < deadline:
                time.sleep(0.01)
            for worker in check_threads:
                worker.join(timeout=5)
        self.assertEqual(app.job["status"], "FAILED", app.job)
        analyzer.assert_not_called()
        backup.assert_not_called()

    def test_upgrade_integrity_scan_can_be_cancelled_between_database_pages(self):
        from analyze_source import AnalysisCancelled
        self.set_old_parser()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE integrity_scan_padding(value TEXT)")
            db.executemany("INSERT INTO integrity_scan_padding VALUES(?)", ((str(i),) for i in range(5000)))
        calls = []
        def cancel():
            calls.append(True)
            if len(calls) >= 2:
                raise AnalysisCancelled("cancel the database scan")
        with self.assertRaises(AnalysisCancelled):
            plan_workspace_upgrade(self.saved, check_cancel=cancel)
        self.assertEqual(len(calls), 2)

    def test_repaired_display_snapshot_cannot_authorize_upgrade_on_any_restart(self):
        state_path, conversation = self.prepare_saved_history()
        report_path = self.output / "diagnosis.json"
        original_report = json.loads(report_path.read_text())
        for location in ("build_report", "top_level"):
            report = json.loads(json.dumps(original_report))
            changed = report["build_report"] if location == "build_report" else report
            changed["snapshot_id"] = "sha256:" + "a" * 64
            report_path.write_text(json.dumps(report), encoding="utf-8")
            for restart in range(2):
                with self.subTest(location=location, restart=restart):
                    app, calls = self.open_and_wait(state_path)
                    self.assertEqual(calls, [])
                    self.assertIsNone(app.job)
                    self.assertEqual(app.conversation, conversation)
                    with closing(sqlite3.connect(self.database)) as db:
                        parser = db.execute("SELECT value FROM metadata WHERE key='parser_version'").fetchone()[0]
                    self.assertEqual(parser, "business-sparse-v1.7")

    def test_failed_startup_upgrade_restores_reports_and_history_without_automatic_retry(self):
        state_path, conversation = self.prepare_saved_history()
        previous_report = (self.output / "diagnosis.json").read_bytes()
        with closing(sqlite3.connect(self.database)) as db:
            previous_index = tuple(db.iterdump())
        app, calls = self.open_and_wait(state_path, failing=True)
        self.assertEqual(app.job["status"], "FAILED", app.job)
        self.assertEqual(len(calls), 1)
        self.assertEqual(app.conversation, conversation)
        self.assertEqual((self.output / "diagnosis.json").read_bytes(), previous_report)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(tuple(db.iterdump()), previous_index)
        self.assertEqual((self.output / "business-notes.md").read_text(), "Keep this local export.\n")
        restarted, second_calls = self.open_and_wait(state_path, failing=True)
        self.assertEqual(second_calls, [])
        self.assertIsNone(restarted.job)
        self.assertEqual(restarted.conversation, conversation)


if __name__ == "__main__":
    unittest.main()
