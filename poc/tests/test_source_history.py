from __future__ import annotations

from contextlib import closing
import copy
import http.client
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyze_source import analyze_source
from company_api import CompanyAPIConfig
from conversation_store import ConversationStore
from remote_source import RemoteSourceError
from web_app import RequestError, WorkbenchState, create_server


class SourceHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state_path = self.root / "state.json"
        self.config = CompanyAPIConfig(base_url="https://api.example.invalid/v1", chat_model="test-model", api_key="test-only")
        self.app = self.new_app()
        self.local = self.root / "local-source"
        self.local.mkdir()
        self.write_source(self.local)
        self.local_output = self.root / "local-output"
        self.local_reference = str(self.root / "local-reference")
        self.app.framework_reference_path = self.local_reference
        self.finish(self.app.start({"source": str(self.local), "output": str(self.local_output), "allow_network": False})["job_id"])
        self.local_workspace = self.app.project["workspace_id"]
        self.local_conversation = self.add_conversation("请解释申请校验的条件与结果")

    def new_app(self):
        app = WorkbenchState(state_path=self.state_path, remote_source_root=self.root / "remote-sources", config_provider=lambda: self.config)
        if not app.framework_reference_path:
            app.framework_reference_path = str(self.root / "no-reference")
        return app

    @staticmethod
    def write_source(source):
        (source / "request.cbl").write_text(
            "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. REQUEST.\n"
            "       PROCEDURE DIVISION.\n           STOP RUN.\n", encoding="utf-8")

    def finish(self, identifier):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            value = self.app.get_job(identifier)
            if value["status"] != "RUNNING":
                return value
            threading.Event().wait(.002)
        self.fail("Job did not finish")

    def add_conversation(self, question):
        identifier = self.app.new_conversation({})["conversation"]["id"]
        self.app.conversation_store.append(identifier, "user", question, status="completed")
        self.app.conversation_store.append(identifier, "assistant", "The source defines the conditions.", status="completed")
        self.app.get_conversation(identifier)
        return identifier

    def import_remote(self):
        def fetched(spec, destination, **kwargs):
            destination.mkdir()
            self.write_source(destination)
            return {**spec.public_metadata(), "commit": "a" * 40}
        with mock.patch("web_app.fetch_remote_source", side_effect=fetched):
            result = self.finish(self.app.start_remote({
                "url": "https://code.example.invalid/projects/DEMO/repos/business-source/browse/stage",
                "framework_reference_path": str(self.root / "remote-reference"),
            })["job_id"])
        self.assertEqual(result["status"], "COMPLETED", result)
        self.remote_workspace = self.app.project["workspace_id"]
        self.remote_conversation = self.add_conversation("Explain the request update workflow")
        return result

    def test_recent_history_groups_known_local_and_remote_sources_and_survives_restart(self):
        result = self.import_remote()
        self.assertIn("recent_conversations", result["result"])
        rows = self.app.state()["recent_conversations"]
        self.assertEqual({row["id"] for row in rows}, {self.local_conversation, self.remote_conversation})
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id[self.local_conversation]["source_origin"], "local_source")
        self.assertEqual(by_id[self.remote_conversation]["source_origin"], "remote_repository")
        self.assertEqual(by_id[self.remote_conversation]["source_label"], "business-source / stage")
        self.assertEqual(by_id[self.local_conversation]["message_count"], 2)
        self.assertEqual(self.new_app().state()["recent_conversations"], rows)
        self.assertEqual(self.app.state()["conversation"]["workspace_id"], self.remote_workspace)

    def test_open_restores_own_source_framework_and_conversation_without_analysis(self):
        self.import_remote()
        self.app.answer_instructions = "Use a concise explanation."
        with mock.patch("web_app.fetch_remote_source") as fetch, mock.patch.object(self.app, "analyzer") as analyzer:
            opened = self.app.open_source_conversation({"workspace_id": self.local_workspace, "conversation_id": self.local_conversation})
        fetch.assert_not_called()
        analyzer.assert_not_called()
        self.assertEqual(opened["project"]["source"], str(self.local))
        self.assertEqual(opened["conversation"]["id"], self.local_conversation)
        self.assertEqual(opened["conversation"]["workspace_id"], self.local_workspace)
        self.assertEqual(self.app.framework_reference_path, self.local_reference)
        self.assertEqual(self.app.answer_instructions, "Use a concise explanation.")
        self.assertTrue(opened["project"]["snapshot_id"])
        self.assertIsNone(self.app.job)
        self.assertEqual(self.new_app().project["source"], str(self.local))

    def test_wrong_source_and_unknown_workspace_do_not_switch_or_delete(self):
        self.import_remote()
        current = copy.deepcopy(self.app.project)
        for workspace, conversation in ((self.remote_workspace, self.local_conversation), ("0" * 64, self.local_conversation)):
            with self.assertRaises(RequestError):
                self.app.open_source_conversation({"workspace_id": workspace, "conversation_id": conversation})
            with self.assertRaises(RequestError):
                self.app.delete_source_conversation(workspace, conversation)
            self.assertEqual(self.app.project, current)
        self.assertEqual(len(self.app.recent_conversations()), 2)

    def test_delete_inactive_source_conversation_does_not_switch_current_source(self):
        self.import_remote()
        current = copy.deepcopy(self.app.project)
        response = self.app.delete_source_conversation(self.local_workspace, self.local_conversation)
        self.assertEqual(self.app.project, current)
        self.assertEqual(response["conversation"]["id"], self.remote_conversation)
        self.assertEqual([row["id"] for row in response["recent_conversations"]], [self.remote_conversation])
        self.assertEqual(ConversationStore(self.local_output, self.local).list(), [])

    def test_missing_source_keeps_history_but_never_reuses_active_source_authority(self):
        self.import_remote()
        self.local.rename(self.root / "moved-source")
        result = self.app.open_source_conversation({"workspace_id": self.local_workspace, "conversation_id": self.local_conversation})
        self.assertEqual(result["conversation"]["id"], self.local_conversation)
        self.assertEqual(result["project"]["source"], str(self.local))
        self.assertIsNone(result["project"]["snapshot_id"])
        self.assertEqual(result["project"]["restore_notice"], "SOURCE_PATH_UNAVAILABLE")
        self.assertIsNone(result["project"]["agent"])

    def test_failed_import_and_failed_state_commit_do_not_register_candidates(self):
        before = copy.deepcopy(self.app.source_workspaces)
        with mock.patch("web_app.fetch_remote_source", side_effect=RemoteSourceError("REMOTE_SOURCE_AUTH_FAILED", "Access denied.")):
            result = self.finish(self.app.start_remote({"url": "https://code.example.invalid/projects/DEMO/repos/source"})["job_id"])
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(self.app.source_workspaces, before)
        other = self.root / "other-source"
        other.mkdir()
        self.write_source(other)
        original_save = self.app._save_workspace
        def fail_new(options=None):
            if self.app.project.get("source") == str(other):
                original_save(options)
                raise OSError("test persistence failure after writing state")
            return original_save(options)
        with mock.patch.object(self.app, "_save_workspace", side_effect=fail_new):
            result = self.finish(self.app.start({"source": str(other), "output": str(self.root / "other-output")})["job_id"])
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(self.app.source_workspaces, before)
        self.assertEqual(self.new_app().source_workspaces, before)

    def test_failed_switch_persistence_preserves_original_project_and_registry(self):
        self.import_remote()
        before = copy.deepcopy(self.app.project)
        registry = copy.deepcopy(self.app.source_workspaces)
        with mock.patch.object(self.app, "_save_workspace", side_effect=OSError("cannot save")):
            with self.assertRaises(RequestError) as failure:
                self.app.open_source_conversation({"workspace_id": self.local_workspace, "conversation_id": self.local_conversation})
        self.assertEqual(failure.exception.code, "WORKSPACE_SWITCH_FAILED")
        self.assertEqual(self.app.project, before)
        self.assertEqual(self.app.source_workspaces, registry)
        self.assertEqual(self.app.conversation["id"], self.remote_conversation)

    def test_legacy_remote_state_seeds_verified_local_preferences_without_scanning(self):
        self.import_remote()
        saved = json.loads(self.state_path.read_text())
        saved.pop("source_workspaces")
        self.state_path.write_text(json.dumps(saved))
        restored = self.new_app()
        self.assertEqual({row["source_origin"] for row in restored.recent_conversations()}, {"local_source", "remote_repository"})
        saved["source_preferences"]["source"] = str(self.root / "wrong-source")
        self.state_path.write_text(json.dumps(saved))
        restored = self.new_app()
        self.assertEqual([row["source_origin"] for row in restored.recent_conversations()], ["remote_repository"])
        self.assertFalse((self.root / "wrong-source").exists())

    def test_listing_missing_or_unrelated_transcripts_does_not_create_files(self):
        self.import_remote()
        path = self.local_output / "conversations.sqlite"
        path.unlink()
        with mock.patch("web_app.ConversationStore", side_effect=AssertionError("read-only listing created store")):
            rows = self.app.recent_conversations()
        self.assertEqual([row["id"] for row in rows], [self.remote_conversation])
        self.assertFalse(path.exists())

    def test_open_missing_transcript_does_not_create_files_or_switch_source(self):
        self.import_remote()
        before = copy.deepcopy(self.app.project)
        saved = self.state_path.read_bytes()
        path = self.local_output / "conversations.sqlite"
        path.unlink()
        with self.assertRaises(RequestError) as failure:
            self.app.open_source_conversation({"workspace_id": self.local_workspace, "conversation_id": self.local_conversation})
        self.assertEqual(failure.exception.code, "CONVERSATION_NOT_FOUND")
        self.assertEqual(failure.exception.status, 404)
        self.assertFalse(path.exists())
        self.assertEqual(self.app.project, before)
        self.assertEqual(self.app.conversation["id"], self.remote_conversation)
        self.assertEqual(self.state_path.read_bytes(), saved)

    def test_running_job_blocks_cross_source_actions(self):
        self.import_remote()
        self.app.job = {"status": "RUNNING"}
        for action in (lambda: self.app.open_source_conversation({"workspace_id": self.local_workspace, "conversation_id": self.local_conversation}),
                       lambda: self.app.delete_source_conversation(self.local_workspace, self.local_conversation)):
            with self.assertRaises(RequestError) as failure:
                action()
            self.assertEqual(failure.exception.code, "JOB_ACTIVE")

    def test_source_aware_routes_require_authenticated_same_origin_requests(self):
        self.import_remote()
        server = create_server(0, app=self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(method, route, payload=None, *, authenticated=True):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            headers = {"Origin": server.origin, "Content-Type": "application/json"}
            if authenticated:
                headers["X-Session-Token"] = self.app.session_token
            try:
                connection.request(method, route, json.dumps(payload) if payload is not None else None, headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        payload = {"workspace_id": self.local_workspace, "conversation_id": self.local_conversation}
        self.assertEqual(request("POST", "/api/conversations/open", payload, authenticated=False)[0], 403)
        status, result = request("POST", "/api/conversations/open", payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["conversation"]["id"], self.local_conversation)
        route = f"/api/recent-conversations/{self.remote_workspace}/{self.remote_conversation}"
        self.assertEqual(request("DELETE", route, authenticated=False)[0], 403)
        self.assertEqual(request("DELETE", route)[0], 200)
        self.assertEqual(self.app.project["workspace_id"], self.local_workspace)


if __name__ == "__main__":
    unittest.main()
