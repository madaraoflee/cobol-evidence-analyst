from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyze_source import analyze_source
from agent_policy import AgentPolicy
from company_api import CompanyAPIConfig, TransportResponse
from web_app import RequestError, WorkbenchState


class ConversationWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "quota.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. QUOTA.\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 RESERVED-COUNT PIC 9(4).\n01 CAPACITY PIC 9(4).\nPROCEDURE DIVISION.\n"
            "IF RESERVED-COUNT > CAPACITY DISPLAY 'CAPACITY EXCEEDED' END-IF.\nGOBACK.\n")
        self.output = self.root / "output"
        self.calls = []
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "neutral-model", api_key="test-credential")

        def transport(request):
            body = json.loads(request.body)
            self.calls.append(body)
            refs = re.findall(r"ev_page_[a-f0-9]+", json.dumps(body))
            content = "超过容量时会提示超额。" + (f"[{refs[0]}]" if refs else "")
            if "COMPUTE AVAILABLE-COUNT" in json.dumps(body):
                content += "可用数量等于容量减去已预留数量。"
            return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}))

        self.transport = transport

        def analyzer(source, output, **options):
            return analyze_source(source, output, transport=self.transport, **options)

        self.analyzer = analyzer
        self.state_path = self.root / "settings.json"
        self.app = WorkbenchState(analyzer=analyzer, config_provider=lambda: self.config, state_path=self.state_path)

    def run_job(self, **values):
        options = {"source": str(self.source), "output": str(self.output), "source_format": "free", **values}
        job_id = self.app.start(options)["job_id"]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            job = self.app.get_job(job_id)
            if job["status"] != "RUNNING":
                self.assertEqual(job["status"], "COMPLETED", job.get("error"))
                return job["result"]
            time.sleep(.01)
        self.fail("worker timeout")

    def wait_for_job(self, job_id):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            job = self.app.get_job(job_id)
            if job["status"] != "RUNNING":
                return job
            time.sleep(.01)
        self.fail("worker timeout")

    def test_two_turns_reuse_index_and_survive_restart_with_valid_history_citations(self):
        indexed = self.run_job()
        self.assertEqual(len(self.calls), 0)
        with patch("analyze_source.build_business_index", side_effect=AssertionError("must not rebuild")), \
             patch("analyze_source._verify_scope", side_effect=AssertionError("must not rescan")), \
             patch("analyze_source.ensure_repository_search", side_effect=AssertionError("must not refresh search")):
            first = self.run_job(question="How is CAPACITY checked?", allow_network=True)
            conversation_id = first["conversation"]["id"]
            second = self.run_job(question="那超过以后呢？", allow_network=True, conversation_id=conversation_id)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(second["conversation"]["messages"]), 4)
        self.assertEqual(second["snapshot_id"], indexed["snapshot_id"])
        self.assertTrue(second["diagnosis"]["index_reused"])
        self.assertEqual(self.calls[-1]["messages"][1]["content"], "How is CAPACITY checked?")
        self.assertEqual(self.calls[-1]["messages"][2]["role"], "assistant")
        self.assertIn("超过容量", self.calls[-1]["messages"][2]["content"])
        restored = WorkbenchState(analyzer=self.analyzer, config_provider=lambda: self.config, state_path=self.state_path)
        state = restored.state()
        self.assertEqual(state["conversation"]["id"], conversation_id)
        self.assertEqual(len(state["conversation"]["messages"]), 4)
        message = state["conversation"]["messages"][1]
        ref = message["evidence_refs"][0]
        evidence = restored.evidence(ref["evidence_id"], conversation_id, message["id"])
        self.assertEqual(evidence["spans"][0]["integrity"], "VALID")
        self.assertEqual(len(self.calls), 2)
        new = restored.new_conversation({})["conversation"]
        self.assertNotEqual(new["id"], conversation_id)
        self.assertEqual(new["messages"], [])
        self.assertEqual(restored.state()["project"]["snapshot_id"], indexed["snapshot_id"])

    def test_cold_and_warm_web_questions_send_selected_semantic_evidence(self):
        (self.source / "quota.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. QUOTA.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 RESERVED-COUNT PIC 9(4).\n"
            "01 CAPACITY PIC 9(4).\n01 AVAILABLE-COUNT PIC 9(4).\n"
            "PROCEDURE DIVISION.\nMAIN.\n"
            "IF RESERVED-COUNT > CAPACITY DISPLAY 'CAPACITY EXCEEDED' END-IF.\n"
            "COMPUTE AVAILABLE-COUNT = CAPACITY - RESERVED-COUNT.\nGOBACK.\n",
            encoding="utf-8")
        first = self.run_job(question="How is AVAILABLE-COUNT calculated?", allow_network=True)
        conversation_id = first["conversation"]["id"]
        second = self.run_job(question="What condition precedes that calculation?",
                              allow_network=True, conversation_id=conversation_id)
        for result, request in zip((first, second), self.calls):
            self.assertEqual(result["diagnosis"]["build_report"]["index_kind"], "business_sparse")
            self.assertIn("semantic_scope", result["agent"])
            payload = json.loads(request["messages"][-1]["content"])
            self.assertTrue(payload["evidence_groups"])
            self.assertIn("COMPUTE AVAILABLE-COUNT", json.dumps(payload["source_context"]))
            self.assertTrue(Path(result["agent"]["agent_result"]["metrics"]["quality_trace_path"]).is_file())
        self.assertTrue(second["diagnosis"]["index_reused"])
        self.assertEqual(len(self.calls), 2)

    def test_old_message_excerpt_survives_local_refresh(self):
        first = self.run_job(question="Explain CAPACITY", allow_network=True)
        conversation_id = first["conversation"]["id"]
        message = first["conversation"]["messages"][1]
        evidence_id = message["evidence_refs"][0]["evidence_id"]
        source_file = self.source / "quota.cbl"
        stat = source_file.stat()
        source_file.write_text(source_file.read_text().replace("> CAPACITY", "< CAPACITY"), encoding="utf-8")
        os.utime(source_file, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        second = self.run_job(question="Explain CAPACITY again", allow_network=True,
                              conversation_id=conversation_id)
        self.assertNotEqual(second["snapshot_id"], first["snapshot_id"])
        archived = self.app.evidence(evidence_id, conversation_id, message["id"])
        self.assertIn("RESERVED-COUNT > CAPACITY", archived["spans"][0]["source_text"])
        self.assertEqual(archived["snapshot_id"], first["snapshot_id"])

    def test_explicit_local_refresh_tracks_hash_changes_additions_deletions_and_renames(self):
        removed = self.source / "remove.cbl"
        removed.write_text("PROGRAM-ID. REMOVAL-RULE.\nPROCEDURE DIVISION.\nGOBACK.\n")
        renamed = self.source / "prior.cbl"
        renamed.write_text("PROGRAM-ID. MOVED-RULE.\nPROCEDURE DIVISION.\nGOBACK.\n")
        imported = self.run_job()
        first = self.run_job(question="Explain CAPACITY", allow_network=True)
        original_version = imported["source_version"]
        conversation_id = first["conversation"]["id"]
        original_messages = first["conversation"]["messages"]
        message = original_messages[1]
        evidence_id = message["evidence_refs"][0]["evidence_id"]
        source_file = self.source / "quota.cbl"
        stat = source_file.stat()
        original_text = source_file.read_text()
        source_file.write_text(original_text.replace("> CAPACITY", "< CAPACITY"))
        os.utime(source_file, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(source_file.stat().st_size, stat.st_size)
        self.assertEqual(source_file.stat().st_mtime_ns, stat.st_mtime_ns)
        removed.unlink()
        renamed.rename(self.source / "renamed.cbl")
        (self.source / "new.cbl").write_text(
            "PROGRAM-ID. NEW-RULE.\nPROCEDURE DIVISION.\nGOBACK.\n")
        request_count = len(self.calls)

        refreshed = self.run_job(verify_content=False)

        self.assertEqual(len(self.calls), request_count, "source refresh must remain offline")
        self.assertNotEqual(refreshed["snapshot_id"], imported["snapshot_id"])
        version = refreshed["source_version"]
        self.assertTrue(version["version_id"].startswith("local:"))
        self.assertEqual(version["provider"], "local")
        self.assertEqual(version["snapshot_id"], refreshed["snapshot_id"])
        self.assertNotEqual(version["version_id"], original_version["version_id"])
        self.assertEqual(version["previous_version_id"], original_version["version_id"])
        self.assertEqual(version["file_count"], 3)
        self.assertEqual(version["changes"], {"added": 2, "modified": 1, "removed": 2, "unchanged": 0})
        self.assertEqual(refreshed["source_versions"][0]["version_id"], version["version_id"])
        self.assertEqual(refreshed["conversation"]["id"], conversation_id)
        self.assertEqual(refreshed["conversation"]["messages"], original_messages)
        with sqlite3.connect(self.output / "structural-index.sqlite") as db:
            paths = [row[0] for row in db.execute("SELECT relative_path FROM source_files ORDER BY relative_path")]
            rules = "\n".join(row[0] for row in db.execute(
                "SELECT normalized_text FROM business_rules WHERE relative_path='quota.cbl'"))
        self.assertEqual(paths, ["new.cbl", "quota.cbl", "renamed.cbl"])
        self.assertIn("RESERVED-COUNT < CAPACITY", rules)
        self.assertNotIn("RESERVED-COUNT > CAPACITY", rules)
        archived = self.app.evidence(evidence_id, conversation_id, message["id"])
        self.assertIn("RESERVED-COUNT > CAPACITY", archived["spans"][0]["source_text"])
        self.assertEqual(archived["snapshot_id"], first["snapshot_id"])

    def test_unchanged_local_refresh_keeps_one_version_and_its_creation_time(self):
        first = self.run_job()
        second = self.run_job()

        self.assertEqual(len(self.calls), 0)
        self.assertEqual(second["snapshot_id"], first["snapshot_id"])
        self.assertEqual(second["source_version"]["version_id"], first["source_version"]["version_id"])
        self.assertEqual(second["source_version"]["created_at"], first["source_version"]["created_at"])
        self.assertGreaterEqual(second["source_version"]["checked_at"], first["source_version"]["checked_at"])
        self.assertEqual(len(second["source_versions"]), 1)
        self.assertEqual(second["source_versions"][0]["version_id"], first["source_version"]["version_id"])

    def test_local_version_history_and_old_renamed_source_excerpt_survive_restart(self):
        self.run_job()
        first = self.run_job(question="Explain CAPACITY", allow_network=True)
        conversation_id = first["conversation"]["id"]
        message = first["conversation"]["messages"][1]
        evidence_id = message["evidence_refs"][0]["evidence_id"]
        (self.source / "quota.cbl").rename(self.source / "renamed.cbl")
        refreshed = self.run_job()

        restored = WorkbenchState(analyzer=self.analyzer, config_provider=lambda: self.config,
                                  state_path=self.state_path)
        state = restored.state()
        self.assertEqual(state["conversation"]["id"], conversation_id)
        self.assertEqual(state["project"]["source_version"]["version_id"],
                         refreshed["source_version"]["version_id"])
        self.assertEqual([version["version_id"] for version in state["project"]["source_versions"]],
                         [version["version_id"] for version in refreshed["source_versions"]])
        self.assertEqual(len(state["project"]["source_versions"]), 2)
        archived = restored.evidence(evidence_id, conversation_id, message["id"])
        self.assertEqual(archived["spans"][0]["relative_path"], "quota.cbl")
        self.assertIn("RESERVED-COUNT > CAPACITY", archived["spans"][0]["source_text"])
        self.assertEqual(archived["snapshot_id"], first["snapshot_id"])

    def assert_local_refresh_rolled_back(self, before, message, evidence_id):
        state = self.app.state()
        self.assertEqual(state["project"], before["project"])
        self.assertEqual(state["conversation"], before["conversation"])
        with sqlite3.connect(self.output / "structural-index.sqlite") as db:
            snapshot = db.execute("SELECT value FROM metadata WHERE key='snapshot_id'").fetchone()[0]
            rules = "\n".join(row[0] for row in db.execute(
                "SELECT normalized_text FROM business_rules WHERE relative_path='quota.cbl'"))
        self.assertEqual(snapshot, before["project"]["snapshot_id"])
        self.assertIn("RESERVED-COUNT > CAPACITY", rules)
        self.assertNotIn("RESERVED-COUNT < CAPACITY", rules)
        restored = WorkbenchState(analyzer=self.analyzer, config_provider=lambda: self.config,
                                  state_path=self.state_path)
        restored_state = restored.state()
        self.assertEqual(restored_state["project"]["snapshot_id"], before["project"]["snapshot_id"])
        self.assertEqual(restored_state["project"]["source_versions"], before["project"]["source_versions"])
        self.assertEqual(restored_state["conversation"], before["conversation"])
        archived = restored.evidence(evidence_id, before["conversation"]["id"], message["id"])
        self.assertIn("RESERVED-COUNT > CAPACITY", archived["spans"][0]["source_text"])
        self.assertEqual(archived["snapshot_id"], before["project"]["snapshot_id"])

    def test_failed_local_refresh_after_index_and_version_write_restores_persisted_workspace(self):
        from source_versions import record_version

        self.run_job()
        first = self.run_job(question="Explain CAPACITY", allow_network=True)
        before = self.app.state()
        message = first["conversation"]["messages"][1]
        evidence_id = message["evidence_refs"][0]["evidence_id"]
        path = self.source / "quota.cbl"
        path.write_text(path.read_text().replace("> CAPACITY", "< CAPACITY"))
        written = []

        def fail_after_version_write(output, source, snapshot_id):
            version = record_version(output, source, snapshot_id)
            self.assertNotEqual(snapshot_id, before["project"]["snapshot_id"])
            self.assertNotEqual(version["version_id"], before["project"]["source_version"]["version_id"])
            written.append(version["version_id"])
            raise RuntimeError("update failure after writing the new version")

        with patch("web_app.record_version", side_effect=fail_after_version_write):
            job_id = self.app.start({"source": str(self.source), "output": str(self.output),
                                     "source_format": "free"})["job_id"]
            job = self.wait_for_job(job_id)
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(len(written), 1)
        self.assert_local_refresh_rolled_back(before, message, evidence_id)

    def test_cancelled_local_refresh_after_index_write_restores_persisted_workspace(self):
        self.run_job()
        first = self.run_job(question="Explain CAPACITY", allow_network=True)
        before = self.app.state()
        message = first["conversation"]["messages"][1]
        evidence_id = message["evidence_refs"][0]["evidence_id"]
        path = self.source / "quota.cbl"
        path.write_text(path.read_text().replace("> CAPACITY", "< CAPACITY"))
        written = []

        def cancel_after_index_write(source, output, **options):
            report = self.analyzer(source, output, **options)
            snapshot = report["build_report"]["snapshot_id"]
            self.assertNotEqual(snapshot, before["project"]["snapshot_id"])
            written.append(snapshot)
            self.app.cancel_event.set()
            return report

        self.app.analyzer = cancel_after_index_write
        job_id = self.app.start({"source": str(self.source), "output": str(self.output),
                                 "source_format": "free"})["job_id"]
        job = self.wait_for_job(job_id)
        self.assertEqual(job["status"], "CANCELLED")
        self.assertEqual(len(written), 1)
        self.assert_local_refresh_rolled_back(before, message, evidence_id)

    def test_source_switch_backup_progress_is_pollable_and_cancellable_before_analysis(self):
        self.run_job()
        before = self.app.state()
        original_index = (self.output / "structural-index.sqlite").read_bytes()
        source = self.root / "replacement-source"
        source.mkdir()
        (source / "replacement.cbl").write_text("PROGRAM-ID. REPLACEMENT.\nGOBACK.\n")
        reached, release = threading.Event(), threading.Event()
        original_progress = self.app._progress

        def pause_backup(job_id, event):
            original_progress(job_id, event)
            if event["phase"] == "backing_up" and not reached.is_set():
                reached.set()
                release.wait(5)

        self.app._progress = pause_backup
        with patch.object(self.app, "analyzer", side_effect=AssertionError("cancelled backup must not analyze")) as analyzer:
            job_id = self.app.start({"source": str(source), "output": str(self.output),
                                     "source_format": "free"})["job_id"]
            try:
                self.assertTrue(reached.wait(5), "backup did not publish progress")
                state = self.app.state()
                self.assertEqual(state["active_job_id"], job_id)
                self.assertEqual(state["job"]["kind"], "index")
                self.assertEqual(state["job"]["source"], str(source.resolve()))
                self.assertEqual(state["job"]["output"], str(self.output.resolve()))
                self.assertEqual(state["job"]["progress"]["phase"], "backing_up")
                self.assertEqual(state["job"]["progress"]["unit"], "bytes")
                self.assertGreater(state["job"]["progress"]["total"], 0)
                self.assertEqual(state["project"], before["project"])
                self.app.cancel(job_id)
            finally:
                release.set()
            job = self.wait_for_job(job_id)
            analyzer.assert_not_called()
        self.assertEqual(job["status"], "CANCELLED")
        self.assertEqual(self.app.state()["project"], before["project"])
        self.assertEqual((self.output / "structural-index.sqlite").read_bytes(), original_index)
        self.assertEqual(list(self.root.glob("source-update-*")), [])

    def test_new_output_source_switch_publishes_preparation_and_reconnect_metadata(self):
        self.run_job()
        before = self.app.state()
        source, output = self.root / "new-source", self.root / "new-output"
        source.mkdir()
        (source / "new-rule.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. NEW-RULE.\nPROCEDURE DIVISION.\nGOBACK.\n")
        reached, release = threading.Event(), threading.Event()
        original_progress = self.app._progress

        def pause_preparation(job_id, event):
            original_progress(job_id, event)
            if event["phase"] == "framework_reference" and not reached.is_set():
                reached.set()
                release.wait(5)

        self.app._progress = pause_preparation
        with patch("web_app.SourceUpdateBackup", side_effect=AssertionError("new output needs no backup")):
            job_id = self.app.start({"source": str(source), "output": str(output),
                                     "source_format": "free"})["job_id"]
            try:
                self.assertTrue(reached.wait(5), "preparation did not publish progress")
                state = self.app.state()
                self.assertEqual(state["job"]["status"], "RUNNING")
                self.assertEqual(state["job"]["kind"], "index")
                self.assertEqual(state["job"]["source"], str(source.resolve()))
                self.assertEqual(state["job"]["output"], str(output.resolve()))
                self.assertEqual(state["job"]["progress"]["phase"], "framework_reference")
                self.assertEqual(state["project"], before["project"])
            finally:
                release.set()
            job = self.wait_for_job(job_id)
        self.assertEqual(job["status"], "COMPLETED", job.get("error"))
        self.assertEqual(self.app.state()["project"]["source"], str(source.resolve()))
        self.assertEqual(job["result"]["source_version"]["file_count"], 1)
        self.assertEqual(len(self.calls), 0)

    def test_directory_symlink_version_store_is_rejected_before_analysis_and_preserves_workspace(self):
        self.run_job()
        first = self.run_job(question="Explain CAPACITY", allow_network=True)
        before = self.app.state()
        message = first["conversation"]["messages"][1]
        evidence_id = message["evidence_refs"][0]["evidence_id"]
        version_path = self.output / "source-versions.sqlite"
        saved_version_path = self.root / "saved-source-versions.sqlite"
        version_path.rename(saved_version_path)
        target = self.root / "directory-target"
        target.mkdir()
        version_path.symlink_to(target, target_is_directory=True)
        try:
            with patch.object(self.app, "analyzer", side_effect=AssertionError("must reject before analysis")) as analyzer:
                with self.assertRaises(RequestError) as error:
                    self.app.start({"source": str(self.source), "output": str(self.output),
                                    "source_format": "free"})
                self.assertEqual(error.exception.code, "INVALID_PATH")
                analyzer.assert_not_called()
            self.assertEqual(self.app.state()["project"], before["project"])
            self.assertEqual(self.app.state()["conversation"], before["conversation"])
            self.assertTrue(version_path.is_symlink())
            self.assertTrue(target.is_dir())
        finally:
            version_path.unlink()
            saved_version_path.rename(version_path)
        self.assert_local_refresh_rolled_back(before, message, evidence_id)

    def test_web_related_objects_use_authorized_frozen_result(self):
        result = self.run_job(question="where-used RESERVED-COUNT?", allow_network=True)
        message = result["conversation"]["messages"][-1]
        impact = message["impact_result"]
        self.assertGreater(impact["total"], 0)
        page = self.app.impact(impact["handle"], result["conversation"]["id"], message["id"])
        self.assertEqual(page["total"], impact["total"])
        exported = self.app.impact(impact["handle"], result["conversation"]["id"], message["id"], export=True)
        self.assertEqual(len(exported["jsonl"].splitlines()), impact["total"])
        with self.assertRaises(RequestError):
            self.app.impact(impact["handle"], result["conversation"]["id"], "another-message")

    def test_stale_display_report_is_repaired_from_database_without_reimport(self):
        indexed = self.run_job()
        diagnosis_path = self.output / "diagnosis.json"
        diagnosis = json.loads(diagnosis_path.read_text(encoding="utf-8"))
        diagnosis["source_options"]["source_format"] = "fixed"
        diagnosis["build_report"]["snapshot_id"] = "outdated-display-snapshot"
        diagnosis_path.write_text(json.dumps(diagnosis), encoding="utf-8")
        with patch("analyze_source.build_business_index", side_effect=AssertionError("must reuse database")):
            result = self.run_job(question="Explain CAPACITY", allow_network=True)
        self.assertTrue(result["diagnosis"]["index_reused"])
        self.assertEqual(result["snapshot_id"], indexed["snapshot_id"])
        self.assertTrue(result["diagnosis"]["report_repaired_from_index"])

    def test_framework_directory_configuration_persists_and_reaches_model(self):
        docs = self.root / "manuals"
        docs.mkdir()
        (docs / "guide.md").write_text("# Processing guide\nCAPACITY is the maximum reserved count before escalation.\n")
        status = self.app.configure_framework({"path": str(docs)})["framework_knowledge"]
        self.assertEqual(status["loaded_document_count"], 1)
        self.assertEqual(status["configured_path"], str(docs))
        self.run_job()
        result = self.run_job(question="Explain CAPACITY", allow_network=True)
        self.assertIn("before escalation", json.dumps(self.calls[-1]))
        self.assertTrue(result["agent"]["agent_result"]["framework_context"]["references"])
        restored = WorkbenchState(state_path=self.state_path, config_provider=lambda: self.config)
        self.assertEqual(restored.state()["framework_knowledge"]["loaded_document_count"], 1)

    def test_new_conversation_does_not_inherit_another_conversation_history(self):
        self.run_job()
        one = self.run_job(question="Explain RESERVED-COUNT", allow_network=True)
        two = self.run_job(question="Explain CAPACITY", allow_network=True)
        self.assertNotEqual(one["conversation"]["id"], two["conversation"]["id"])
        self.assertEqual(len(self.calls[-1]["messages"]), 2)
        self.assertEqual(len(self.app.state()["conversations"]), 2)

    def test_policy_reaches_first_question_and_cached_followup(self):
        self.app.policy_provider = lambda: AgentPolicy(max_model_requests=2)
        first = self.run_job(question="Explain CAPACITY", allow_network=True)
        self.assertEqual(first["agent"]["agent_result"]["metrics"]["policy"]["max_model_requests"], 2)
        second = self.run_job(question="What if it is exceeded?", allow_network=True,
                              conversation_id=first["conversation"]["id"])
        self.assertTrue(second["diagnosis"]["index_reused"])
        self.assertEqual(second["agent"]["agent_result"]["metrics"]["policy"]["max_model_requests"], 2)
        self.assertEqual(len(self.calls), 2)

    def test_oversized_saved_answer_preserves_question_in_actual_followup_request(self):
        first = self.run_job(question="Explain the CAPACITY exception rules.", allow_network=True)
        identifier = first["conversation"]["id"]
        answer = "Business rule detail. " * 2000
        with closing(sqlite3.connect(self.app.conversation_store.path)) as db, db:
            db.execute("UPDATE messages SET content=? WHERE conversation_id=? AND role='assistant'",
                       (answer, identifier))
        second = self.run_job(question="Explain the exception in that answer.", allow_network=True,
                              conversation_id=identifier)
        prior_messages = self.calls[-1]["messages"][1:-1]
        self.assertEqual(prior_messages[0]["content"], "Explain the CAPACITY exception rules.")
        self.assertEqual(prior_messages[1]["role"], "assistant")
        self.assertTrue(prior_messages[1]["content"].startswith("Business rule detail."))
        self.assertLessEqual(sum(len(message["content"]) for message in prior_messages), 18000)
        self.assertEqual(second["conversation"]["messages"][1]["content"], answer)

    def test_configured_history_budget_above_storage_default_reaches_model(self):
        self.app.policy_provider = lambda: AgentPolicy(max_history_characters=60000)
        first = self.run_job(question="Explain the CAPACITY rule.", allow_network=True)
        identifier = first["conversation"]["id"]
        answer = "Business rule detail. " * 2000
        with closing(sqlite3.connect(self.app.conversation_store.path)) as db, db:
            db.execute("UPDATE messages SET content=? WHERE conversation_id=? AND role='assistant'",
                       (answer, identifier))
        self.run_job(question="Explain the exception.", allow_network=True, conversation_id=identifier)
        self.assertEqual(self.calls[-1]["messages"][2]["content"], answer)
        self.assertEqual(len(self.calls), 2)

    def test_zero_history_budget_keeps_saved_conversation_without_sending_prior_messages(self):
        self.app.policy_provider = lambda: AgentPolicy(max_history_characters=0)
        first = self.run_job(question="Explain the CAPACITY rule.", allow_network=True)
        second = self.run_job(question="Explain RESERVED-COUNT.", allow_network=True,
                              conversation_id=first["conversation"]["id"])
        self.assertEqual(len(self.calls[-1]["messages"]), 2)
        self.assertEqual(second["agent"]["agent_result"]["metrics"]["history_messages"], 0)
        self.assertEqual(len(second["conversation"]["messages"]), 4)

    def test_raw_responses_require_opt_in_and_are_not_inherited_by_next_question(self):
        self.run_job()
        first = self.run_job(question="Explain CAPACITY", allow_network=True)
        self.assertNotIn("api_diagnostics", first["agent"])
        self.assertIn("超过容量", first["conversation"]["messages"][-1]["content"])
        second = self.run_job(question="Explain CAPACITY again", allow_network=True, capture_api_responses=True,
                              conversation_id=first["conversation"]["id"])
        self.assertTrue(second["agent"]["api_diagnostics"]["captured"])
        third = self.run_job(question="What if it is exceeded?", allow_network=True,
                             conversation_id=first["conversation"]["id"])
        self.assertNotIn("api_diagnostics", third["agent"])
        saved = json.loads((self.output / "agent-result.json").read_text())
        self.assertNotIn("api_diagnostics", saved)

    def test_invalid_policy_stops_before_network_and_does_not_discard_conversation(self):
        self.run_job()
        first = self.run_job(question="Explain CAPACITY", allow_network=True)
        self.app.policy_provider = lambda: (_ for _ in ()).throw(ValueError("private-value"))
        with self.assertRaisesRegex(Exception, "本次尚未调用模型") as caught:
            self.app.start({"source": str(self.source), "output": str(self.output),
                            "question": "Continue", "allow_network": True,
                            "conversation_id": first["conversation"]["id"]})
        self.assertNotIn("private-value", str(caught.exception))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.app.conversation_store.get(first["conversation"]["id"])["messages"]), 2)

    def test_authentication_failure_keeps_conversation_and_index_for_retry(self):
        indexed = self.run_job()
        self.transport = lambda request: TransportResponse(401, "{}")
        failed = self.run_job(question="Explain CAPACITY", allow_network=True)
        conversation = failed["conversation"]
        self.assertEqual(conversation["messages"][-1]["status"], "failed")
        self.assertIn("401", conversation["messages"][-1]["content"])
        self.assertEqual(failed["snapshot_id"], indexed["snapshot_id"])
        self.assertFalse(failed["agent"]["agent_result"]["model_answer_recorded"])
        self.assertEqual(len(conversation["messages"]), 2)

    def test_retry_failed_user_reuses_original_turn_and_rejects_duplicate_submissions(self):
        self.run_job()
        question = "Explain the CAPACITY rule."
        options = {"source": str(self.source), "output": str(self.output),
                   "source_format": "free", "question": question, "allow_network": True}
        with patch.object(self.app, "analyzer", side_effect=RuntimeError("temporary analysis failure")):
            failed = self.wait_for_job(self.app.start(options)["job_id"])
        self.assertEqual(failed["status"], "FAILED")
        original = failed["conversation"]
        self.assertEqual(len(original["messages"]), 1)
        original_user = original["messages"][0]
        self.assertEqual(original_user["status"], "failed")
        self.assertEqual(len(self.calls), 0)

        retry = {**options, "conversation_id": original["id"],
                 "retry_message_id": original_user["id"]}
        retry_job_id = self.app.start(retry)["job_id"]
        with self.assertRaises(RequestError) as duplicate:
            self.app.start(retry)
        self.assertEqual(duplicate.exception.status, 409)
        completed = self.wait_for_job(retry_job_id)
        self.assertEqual(completed["status"], "COMPLETED", completed.get("error"))
        messages = completed["result"]["conversation"]["messages"]
        self.assertEqual([message["role"] for message in messages], ["user", "assistant"])
        self.assertEqual(messages[0]["id"], original_user["id"])
        self.assertEqual(messages[0]["content"], question)
        self.assertEqual(messages[0]["status"], "completed")
        self.assertIn("超过容量", messages[1]["content"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.app.conversation_store.get(original["id"])["messages"]), 2)
        with self.assertRaises(RequestError) as already_completed:
            self.app.start(retry)
        self.assertEqual(already_completed.exception.status, 409)
        self.assertEqual(len(self.calls), 1)

    def test_retry_failed_answer_replaces_that_answer_in_place(self):
        self.run_job()
        question = "Explain the CAPACITY rule."
        working_transport = self.transport
        self.transport = lambda request: TransportResponse(401, "{}")
        failed = self.run_job(question=question, allow_network=True)
        original = failed["conversation"]
        self.assertEqual([message["status"] for message in original["messages"]], ["failed", "failed"])
        original_user, original_answer = original["messages"]
        self.assertIn("401", original_answer["content"])

        self.transport = working_transport
        completed = self.run_job(question=question, allow_network=True,
                                 conversation_id=original["id"], retry_message_id=original_user["id"])
        messages = completed["conversation"]["messages"]
        self.assertEqual(len(messages), 2)
        self.assertEqual([message["id"] for message in messages],
                         [original_user["id"], original_answer["id"]])
        self.assertEqual([message["status"] for message in messages], ["completed", "completed"])
        self.assertIn("超过容量", messages[1]["content"])
        self.assertNotIn("401", messages[1]["content"])
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(any(message.get("content") == question
                             for message in self.calls[0]["messages"][1:-1]))
        self.assertEqual(len(self.app.conversation_store.get(original["id"])["messages"]), 2)


if __name__ == "__main__":
    unittest.main()
