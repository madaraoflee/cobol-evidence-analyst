from __future__ import annotations

import copy
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from analyze_source import AnalysisCancelled, analyze_source
from company_api import CompanyAPIConfig
from remote_source import RemoteSourceError
from web_app import RequestError, SOURCE_UPDATE_PENDING, WorkbenchState, create_server


REMOTE_URL = "https://code.example.invalid/projects/DEMO/repos/business-source/browse/stage?at=refs%2Fheads%2Fmain"
ACCESS_TOKEN = "remote-test-token-never-persist"
REMOTE_METADATA = {
    "provider": "bitbucket", "url": REMOTE_URL, "ref": "refs/heads/main",
    "subdirectory": "stage", "commit": "a" * 40,
}


class RemoteSourceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.remote_root = self.root / "remote-sources"
        self.state_path = self.root / "workbench-state.json"
        self.config = CompanyAPIConfig(
            base_url="https://gateway.example.invalid/v1", chat_model="test-model", api_key="test-model-key")
        self.app = self.new_app()
        self.local_source = self.root / "local-source"
        self.local_source.mkdir()
        self.write_source(self.local_source, "LOCAL-REQUEST")
        self.local_output = self.root / "local-output"
        result = self.finish(self.app.start({
            "source": str(self.local_source), "output": str(self.local_output),
            "allow_network": False, "encoding": "utf-8", "source_format": "fixed",
        })["job_id"])
        self.assertEqual(result["status"], "COMPLETED", result)
        conversation = self.app.new_conversation({})["conversation"]
        self.app.conversation_store.append(conversation["id"], "user", "Saved local question.", status="completed")
        self.app.get_conversation(conversation["id"])
        self.original_project = copy.deepcopy(self.app.project)
        self.original_conversation = copy.deepcopy(self.app.conversation)
        self.original_preferences = copy.deepcopy(self.app.source_preferences)
        self.original_files = self.file_contents(self.local_source, self.local_output)

    def new_app(self):
        app = WorkbenchState(state_path=self.state_path, remote_source_root=self.remote_root,
                             config_provider=lambda: self.config)
        app.framework_reference_path = str(self.root / "no-framework-reference")
        return app

    @staticmethod
    def write_source(directory, program):
        (directory / "request.cbl").write_text(
            "       IDENTIFICATION DIVISION.\n"
            f"       PROGRAM-ID. {program}.\n"
            "       PROCEDURE DIVISION.\n"
            "       MAIN.\n"
            "           DISPLAY 'READY'.\n"
            "           STOP RUN.\n", encoding="utf-8")

    @staticmethod
    def file_contents(*directories):
        return {str(path): path.read_bytes() for directory in directories
                for path in directory.rglob("*") if path.is_file()}

    def finish(self, job_id, *, app=None):
        app = app or self.app
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = app.get_job(job_id)
            if result["status"] != "RUNNING":
                return result
            threading.Event().wait(0.01)
        self.fail("Remote source job did not finish")

    @staticmethod
    def payload(**updates):
        return {"url": REMOTE_URL, "token": ACCESS_TOKEN,
                "encoding": "utf-8", "source_format": "fixed", **updates}

    def fetched_source(self, spec, destination, *, token=None, progress=None, check_cancel=None):
        self.assertEqual(token, ACCESS_TOKEN)
        self.assertFalse(destination.exists())
        destination.mkdir(parents=True)
        self.write_source(destination, "REMOTE-REQUEST")
        if check_cancel:
            check_cancel()
        return dict(REMOTE_METADATA)

    def assert_local_unchanged(self):
        self.assertEqual(self.app.project, self.original_project)
        self.assertEqual(self.app.conversation, self.original_conversation)
        self.assertEqual(self.app.source_preferences, self.original_preferences)
        self.assertEqual(self.file_contents(self.local_source, self.local_output), self.original_files)

    def assert_no_token_saved(self):
        self.assertNotIn(ACCESS_TOKEN, json.dumps(self.app.state()))
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(ACCESS_TOKEN.encode(), path.read_bytes(), str(path))

    def test_remote_success_indexes_in_separate_workspace_and_keeps_local_library(self):
        analyzer = mock.Mock(wraps=analyze_source)
        self.app.analyzer = analyzer
        with mock.patch("web_app.fetch_remote_source", side_effect=self.fetched_source) as fetch:
            accepted = self.app.start_remote(self.payload())
            result = self.finish(accepted["job_id"])
        self.assertEqual(result["status"], "COMPLETED", result)
        project = result["result"]
        self.assertEqual(project["source_origin"], "remote_repository")
        self.assertEqual(project["remote_source"]["commit"], REMOTE_METADATA["commit"])
        self.assertEqual(project["programs"][0]["program_name"], "REMOTE-REQUEST")
        self.assertTrue(Path(project["source"]).is_relative_to(self.remote_root))
        self.assertTrue(Path(project["output"]).is_relative_to(self.remote_root))
        self.assertNotEqual(project["source"], str(self.local_source))
        self.assertNotEqual(project["output"], str(self.local_output))
        self.assertIsNone(self.app.conversation)
        self.assertEqual(self.app.source_preferences, self.original_preferences)
        self.assertEqual(self.file_contents(self.local_source, self.local_output), self.original_files)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(analyzer.call_count, 1)
        self.assertFalse(analyzer.call_args.kwargs["allow_network"])
        self.assertFalse(analyzer.call_args.kwargs.get("question"))
        self.assertNotIn("token", analyzer.call_args.kwargs)
        self.assert_no_token_saved()

        restored = self.new_app()
        self.assertEqual(restored.project["source_origin"], "remote_repository")
        self.assertEqual(restored.project["remote_source"], project["remote_source"])
        self.assertEqual(restored.project["snapshot_id"], project["snapshot_id"])
        self.assertEqual(restored.source_preferences, self.original_preferences)
        with mock.patch("web_app.fetch_remote_source") as fetch_again:
            refreshed = self.finish(restored.start({
                "source": project["source"], "output": project["output"], "allow_network": False,
            })["job_id"], app=restored)
        self.assertEqual(refreshed["status"], "COMPLETED", refreshed)
        self.assertEqual(refreshed["result"]["remote_source"], project["remote_source"])
        fetch_again.assert_not_called()

    def test_remote_download_failure_keeps_local_source_conversation_and_preferences(self):
        error = RemoteSourceError("REMOTE_SOURCE_AUTH_FAILED", "Repository authentication failed.")
        with mock.patch("web_app.fetch_remote_source", side_effect=error):
            result = self.finish(self.app.start_remote(self.payload())["job_id"])
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "REMOTE_SOURCE_AUTH_FAILED")
        self.assert_local_unchanged()
        self.assert_no_token_saved()
        self.assertFalse(self.remote_root.exists() and any(self.remote_root.iterdir()))

    def test_remote_and_local_intake_share_reading_settings_and_saved_answer_requirements(self):
        context = self.root / "compilation.json"
        context.write_text(json.dumps({"dialect": "generic", "copy_libraries": ["copybooks"],
                                       "copy_library_order_known": True}), encoding="utf-8")
        reference = self.root / "framework.md"
        reference.write_text("# Source conventions\nRead the program's declared return fields.\n", encoding="utf-8")
        instructions = "Explain the business purpose, conditions and results."
        self.app.configure_answer_instructions({"answer_instructions": instructions})
        reading_options = {"encoding": "utf-8", "source_format": "fixed", "extensions": ".cbl,.cpy",
                           "answer_detail": "brief", "max_source_pages": 12, "reading_strategy": "focused",
                           "compilation_context_path": str(context), "framework_reference_path": str(reference)}
        analyzer = mock.Mock(wraps=analyze_source)
        self.app.analyzer = analyzer
        local_result = self.finish(self.app.start({
            "source": str(self.local_source), "output": str(self.local_output),
            "allow_network": False, **reading_options,
        })["job_id"])
        self.assertEqual(local_result["status"], "COMPLETED", local_result)
        local_arguments = analyzer.call_args.kwargs.copy()
        with mock.patch("web_app.fetch_remote_source", side_effect=self.fetched_source):
            remote_result = self.finish(self.app.start_remote(self.payload(**reading_options))["job_id"])
        self.assertEqual(remote_result["status"], "COMPLETED", remote_result)
        remote_arguments = analyzer.call_args.kwargs
        for key in (*reading_options, "answer_instructions", "allow_network", "index_mode", "verify_content"):
            self.assertEqual(remote_arguments[key], local_arguments[key], key)
        self.assertEqual(remote_arguments["answer_detail"], "brief")
        self.assertEqual(remote_arguments["max_source_pages"], 12)
        self.assertEqual(remote_arguments["answer_instructions"], instructions)
        self.assertEqual(remote_arguments["compilation_context_path"], str(context))
        self.assertEqual(remote_arguments["framework_reference_path"], str(reference))
        self.assertFalse(remote_arguments["allow_network"])
        for key in ("token", "url", "ref", "subdirectory", "_remote_source"):
            self.assertNotIn(key, remote_arguments)
        diagnosis = remote_result["result"]["diagnosis"]
        self.assertEqual(diagnosis["source_options"]["answer_detail"], "brief")
        self.assertEqual(diagnosis["source_options"]["max_source_pages"], 12)
        self.assertEqual(diagnosis["source_options"]["compilation_context_path"], str(context))

    def test_unexpected_download_exception_redacts_token_and_restores_local_library(self):
        with mock.patch("web_app.fetch_remote_source", side_effect=RuntimeError(ACCESS_TOKEN)):
            result = self.finish(self.app.start_remote(self.payload())["job_id"])
        self.assertEqual(result["status"], "FAILED", result)
        self.assertEqual(result["error"]["code"], "ANALYSIS_FAILED")
        self.assertNotIn(ACCESS_TOKEN, json.dumps(result))
        self.assert_local_unchanged()
        self.assert_no_token_saved()

    def test_question_on_downloaded_source_preserves_provenance_without_refetch(self):
        with mock.patch("web_app.fetch_remote_source", side_effect=self.fetched_source):
            initial = self.finish(self.app.start_remote(self.payload())["job_id"])["result"]
        with mock.patch("web_app.fetch_remote_source") as fetch_again:
            result = self.finish(self.app.start({
                "source": initial["source"], "output": initial["output"],
                "question": "Explain the request program.", "allow_network": False,
            })["job_id"])
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(result["result"]["source_origin"], "remote_repository")
        self.assertEqual(result["result"]["remote_source"], initial["remote_source"])
        self.assertEqual(self.new_app().project["remote_source"], initial["remote_source"])
        fetch_again.assert_not_called()
        self.assert_no_token_saved()

    def test_corrupt_saved_remote_metadata_does_not_fetch_or_revoke_valid_cached_index(self):
        with mock.patch("web_app.fetch_remote_source", side_effect=self.fetched_source):
            initial = self.finish(self.app.start_remote(self.payload())["job_id"])["result"]
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        corrupt = [[], {**REMOTE_METADATA, "commit": "unknown"},
                   {**REMOTE_METADATA, "url": "https://user:secret@code.example.invalid/projects/DEMO/repos/source"},
                   {**REMOTE_METADATA, "ref": ["unexpected"]}]
        with mock.patch("web_app.fetch_remote_source") as fetch:
            for metadata in corrupt:
                with self.subTest(metadata=metadata):
                    self.state_path.write_text(json.dumps({**saved, "remote_source": metadata}), encoding="utf-8")
                    restored = self.new_app()
                    self.assertEqual(restored.project["snapshot_id"], initial["snapshot_id"])
                    self.assertNotIn("remote_source", restored.project)
                    self.assertEqual(restored.source_preferences, self.original_preferences)
                    self.assertIsNone(restored.state()["active_job_id"])
            fetch.assert_not_called()

    def test_failed_remote_index_keeps_local_source_and_removes_partial_cache(self):
        def unreadable_source(spec, destination, **kwargs):
            destination.mkdir(parents=True)
            (destination / "unreadable.cbl").write_bytes(b"\xff\xfe\xff")
            return dict(REMOTE_METADATA)

        with mock.patch("web_app.fetch_remote_source", side_effect=unreadable_source):
            result = self.finish(self.app.start_remote(self.payload())["job_id"])
        self.assertEqual(result["status"], "FAILED", result)
        self.assertEqual(result["error"]["code"], "SOURCE_UPDATE_FAILED")
        self.assert_local_unchanged()
        self.assert_no_token_saved()
        self.assertFalse(self.remote_root.exists() and any(self.remote_root.iterdir()))

    def test_cancel_during_download_keeps_current_library_and_blocks_concurrent_jobs(self):
        entered, release = threading.Event(), threading.Event()

        def paused_fetch(spec, destination, *, check_cancel, **kwargs):
            destination.mkdir(parents=True)
            (destination / "partial.tmp").write_bytes(b"partial")
            entered.set()
            release.wait(5)
            check_cancel()
            return dict(REMOTE_METADATA)

        with mock.patch("web_app.fetch_remote_source", side_effect=paused_fetch):
            accepted = self.app.start_remote(self.payload())
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.app.get_job(accepted["job_id"])["source_origin"], "remote_repository")
                self.assert_local_unchanged()
                for submit in (
                    lambda: self.app.start_remote(self.payload()),
                    lambda: self.app.start({"source": str(self.local_source), "output": str(self.local_output)}),
                ):
                    with self.assertRaises(RequestError) as raised:
                        submit()
                    self.assertEqual(raised.exception.code, "JOB_ACTIVE")
                self.app.cancel(accepted["job_id"])
            finally:
                release.set()
            result = self.finish(accepted["job_id"])
        self.assertEqual(result["status"], "CANCELLED", result)
        self.assert_local_unchanged()
        self.assert_no_token_saved()
        self.assertFalse(self.remote_root.exists() and any(self.remote_root.iterdir()))

    def test_invalid_remote_options_never_download_or_change_local_state(self):
        cases = [
            self.payload(url="http://code.example.invalid/projects/DEMO/repos/source"),
            self.payload(url="https://user:secret@code.example.invalid/projects/DEMO/repos/source"),
            self.payload(source=str(self.local_source)), self.payload(output=str(self.local_output)),
            self.payload(allow_network=True), self.payload(question="Analyze immediately"),
            self.payload(encoding="nonexistent-encoding"), self.payload(source_format="unexpected"),
            self.payload(answer_detail="unexpected"), self.payload(max_source_pages=0),
            self.payload(max_source_pages=True), self.payload(reading_strategy="unexpected"),
            self.payload(compilation_context_path=str(self.root / "missing-compilation.json")),
            self.payload(compilation_context_path=[]), self.payload(framework_reference_path=[]),
            self.payload(token="unsafe\r\nHeader: value"),
        ]
        with mock.patch("web_app.fetch_remote_source") as fetch:
            for payload in cases:
                with self.subTest(payload_keys=sorted(payload)):
                    with self.assertRaises(RequestError):
                        self.app.start_remote(payload)
                    self.assert_local_unchanged()
            fetch.assert_not_called()
        self.assert_no_token_saved()

    def test_repeated_remote_connect_reuses_cached_snapshot_and_local_can_be_reopened(self):
        with mock.patch("web_app.fetch_remote_source", side_effect=self.fetched_source) as fetch:
            first = self.finish(self.app.start_remote(self.payload())["job_id"])["result"]
            reopened_cache = self.app.start_remote(self.payload())
        self.assertTrue(reopened_cache["source_reused"])
        self.assertNotIn("job_id", reopened_cache)
        self.assertEqual(first["source"], reopened_cache["project"]["source"])
        self.assertEqual(first["snapshot_id"], reopened_cache["project"]["snapshot_id"])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(first["output"], reopened_cache["project"]["output"])
        self.assertTrue(Path(first["source"]).is_dir())
        self.assertTrue(Path(first["output"]).is_dir())
        reopened = self.finish(self.app.start({
            "source": str(self.local_source), "output": str(self.local_output), "allow_network": False,
        })["job_id"])
        self.assertEqual(reopened["status"], "COMPLETED", reopened)
        self.assertEqual(reopened["result"]["source_origin"], "local_source")
        self.assertNotIn("remote_source", reopened["result"])
        self.assertEqual(self.app.conversation_store.get(self.original_conversation["id"]), self.original_conversation)

    def test_cached_source_without_questions_can_be_opened_after_switch_and_restart_without_network(self):
        with mock.patch("web_app.fetch_remote_source", side_effect=self.fetched_source):
            remote = self.finish(self.app.start_remote(self.payload())["job_id"])["result"]
        self.app.open_source_workspace({"workspace_id": self.original_project["workspace_id"]})
        with mock.patch("web_app.fetch_remote_source") as fetch, mock.patch("web_app.resolve_remote_source") as resolve:
            restored = self.new_app()
            saved = next(item for item in restored.state()["source_workspaces"]
                         if item["workspace_id"] == remote["workspace_id"])
            self.assertTrue(saved["available"])
            self.assertFalse(saved["active"])
            opened = restored.open_source_workspace({"workspace_id": remote["workspace_id"]})
            self.assertEqual(opened["project"]["snapshot_id"], remote["snapshot_id"])
            self.assertEqual(opened["project"]["source_origin"], "remote_repository")
            self.assertIsNone(opened["conversation"])
            self.assertEqual(opened["conversations"], [])
            again = restored.start_remote(self.payload())
            self.assertTrue(again["source_reused"])
            fetch.assert_not_called()
            resolve.assert_not_called()

    def test_reusing_current_cache_clears_failed_or_cancelled_index_and_preserves_conversation(self):
        with mock.patch("web_app.fetch_remote_source", side_effect=self.fetched_source):
            accepted = self.app.start_remote(self.payload())
            completed = self.finish(accepted["job_id"])
        remote = completed["result"]
        self.assertEqual(self.app.state()["job"]["status"], "COMPLETED")
        self.assertEqual(self.app.get_job(accepted["job_id"])["result"]["snapshot_id"], remote["snapshot_id"])
        conversation = self.app.new_conversation({})["conversation"]
        self.app.conversation_store.append(conversation["id"], "user", "Explain the request outcome.")
        self.app.conversation_store.append(conversation["id"], "assistant", "The available rule is retained.", status="partial")
        self.app.get_conversation(conversation["id"])
        before_conversation = copy.deepcopy(self.app.conversation)
        before_view = self.app.state()["conversation"]
        before_project = copy.deepcopy(self.app.project)
        error = RemoteSourceError("REMOTE_SOURCE_AUTH_FAILED", "Repository authentication failed.")
        for status in ("FAILED", "CANCELLED"):
            with self.subTest(status=status):
                def stop_update(*args, check_cancel=None, **kwargs):
                    if status == "CANCELLED":
                        self.app.cancel_event.set()
                        check_cancel()
                    raise error

                with mock.patch("web_app.resolve_remote_source", side_effect=stop_update):
                    failed_start = self.app.update_remote({"workspace_id": remote["workspace_id"], "token": ACCESS_TOKEN})
                    failed = self.finish(failed_start["job_id"])
                self.assertEqual(failed["status"], status)
                self.assertEqual(self.app.state()["job"]["status"], status)
                self.assertEqual(self.app.get_job(failed_start["job_id"])["status"], status)
                if status == "FAILED":
                    self.assertEqual(failed["error"]["code"], error.code)
                with mock.patch("web_app.fetch_remote_source") as fetch, mock.patch("web_app.resolve_remote_source") as resolve:
                    reopened = self.app.start_remote(self.payload())
                self.assertTrue(reopened["source_reused"])
                self.assertNotIn("job_id", reopened)
                self.assertEqual(reopened["project"], before_project)
                self.assertEqual(reopened["conversation"], before_view)
                self.assertEqual(self.app.conversation_store.get(conversation["id"]), before_conversation)
                refreshed = self.app.state()
                self.assertIsNone(refreshed["active_job_id"])
                self.assertIsNone(refreshed["job"])
                self.assertEqual(refreshed["conversation"], before_view)
                fetch.assert_not_called()
                resolve.assert_not_called()

    def test_reusing_current_cache_cannot_clear_running_update(self):
        with mock.patch("web_app.fetch_remote_source", side_effect=self.fetched_source):
            remote = self.finish(self.app.start_remote(self.payload())["job_id"])["result"]
        entered, release = threading.Event(), threading.Event()

        def paused_resolve(*args, **kwargs):
            entered.set()
            release.wait(5)
            return dict(REMOTE_METADATA)

        with mock.patch("web_app.resolve_remote_source", side_effect=paused_resolve):
            accepted = self.app.update_remote({"workspace_id": remote["workspace_id"], "token": ACCESS_TOKEN})
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(RequestError) as raised:
                    self.app.start_remote(self.payload())
                self.assertEqual(raised.exception.code, "JOB_ACTIVE")
                current = self.app.state()
                self.assertEqual(current["active_job_id"], accepted["job_id"])
                self.assertEqual(current["job"]["status"], "RUNNING")
            finally:
                release.set()
            completed = self.finish(accepted["job_id"])
        self.assertEqual(completed["status"], "COMPLETED", completed)
        self.assertEqual(completed["result"]["remote_update"]["status"], "up_to_date")

    def test_update_with_unchanged_commit_preserves_index_conversation_and_source_bytes(self):
        with mock.patch("web_app.fetch_remote_source", side_effect=self.fetched_source):
            remote = self.finish(self.app.start_remote(self.payload())["job_id"])["result"]
        conversation = self.app.new_conversation({})["conversation"]
        self.app.conversation_store.append(conversation["id"], "user", "A retained business question.", status="completed")
        self.app.get_conversation(conversation["id"])
        before_conversation = copy.deepcopy(self.app.conversation)
        before_files = self.file_contents(Path(remote["source"]), Path(remote["output"]))
        analyzer = mock.Mock(wraps=self.app.analyzer)
        self.app.analyzer = analyzer
        with mock.patch("web_app.resolve_remote_source", return_value=REMOTE_METADATA) as resolve, \
                mock.patch("web_app.fetch_remote_source") as fetch:
            result = self.finish(self.app.update_remote({"workspace_id": remote["workspace_id"],
                                "token": ACCESS_TOKEN})["job_id"])
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(result["result"]["remote_update"]["status"], "up_to_date")
        self.assertEqual(result["result"]["workspace_id"], remote["workspace_id"])
        self.assertEqual(result["source"], remote["source"])
        self.assertEqual(result["output"], remote["output"])
        self.assertEqual(self.app.conversation, before_conversation)
        self.assertEqual(self.file_contents(Path(remote["source"]), Path(remote["output"])), before_files)
        self.assertEqual(resolve.call_count, 1)
        fetch.assert_not_called()
        analyzer.assert_not_called()
        self.assertEqual(len(list(self.remote_root.iterdir())), 1)
        self.assertEqual(self.new_app().project["snapshot_id"], remote["snapshot_id"])
        self.assert_no_token_saved()

    def test_changed_reading_options_reindex_cached_files_without_downloading(self):
        with mock.patch("web_app.fetch_remote_source", side_effect=self.fetched_source):
            remote = self.finish(self.app.start_remote(self.payload())["job_id"])["result"]
        with mock.patch("web_app.fetch_remote_source") as fetch, mock.patch("web_app.resolve_remote_source") as resolve:
            response = self.app.start_remote(self.payload(source_format="auto", extensions=".cbl"))
            self.assertTrue(response["source_reused"])
            result = self.finish(response["job_id"])
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(result["result"]["workspace_id"], remote["workspace_id"])
        self.assertEqual(result["result"]["remote_source"], REMOTE_METADATA)
        self.assertEqual(result["result"]["diagnosis"]["source_options"]["source_format"], "auto")
        self.assertEqual(result["result"]["diagnosis"]["source_options"]["extensions"], [".cbl"])
        fetch.assert_not_called()
        resolve.assert_not_called()
        self.assertEqual(len(list(self.remote_root.iterdir())), 1)

    def test_unchanged_commit_rebuilds_missing_index_from_local_copy(self):
        with mock.patch("web_app.fetch_remote_source", side_effect=self.fetched_source):
            remote = self.finish(self.app.start_remote(self.payload())["job_id"])["result"]
        conversation = self.app.new_conversation({})["conversation"]
        self.app.conversation_store.append(conversation["id"], "user", "Retain this question during local repair.", status="completed")
        self.app.get_conversation(conversation["id"])
        before = copy.deepcopy(self.app.conversation)
        index = Path(remote["output"]) / "structural-index.sqlite"
        index.unlink()
        with mock.patch("web_app.resolve_remote_source", return_value=REMOTE_METADATA), \
                mock.patch("web_app.fetch_remote_source") as fetch:
            result = self.finish(self.app.update_remote({"workspace_id": remote["workspace_id"],
                                "token": ACCESS_TOKEN})["job_id"])
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertTrue(index.is_file())
        self.assertTrue(result["result"]["snapshot_id"])
        self.assertEqual(result["result"]["workspace_id"], remote["workspace_id"])
        self.assertTrue(result["result"]["remote_update"]["index_rebuilt"])
        self.assertEqual(result["result"]["remote_update"]["status"], "up_to_date")
        self.assertEqual(self.app.conversation, before)
        self.assertEqual(len(list(self.remote_root.iterdir())), 1)
        fetch.assert_not_called()

    def test_unchanged_commit_save_failure_rolls_back_metadata_and_registry(self):
        with mock.patch("web_app.fetch_remote_source", side_effect=self.fetched_source):
            remote = self.finish(self.app.start_remote(self.payload())["job_id"])["result"]
        before_project, before_registry = copy.deepcopy(self.app.project), copy.deepcopy(self.app.source_workspaces)
        save = self.app._save_workspace
        saves = 0

        def fail_once(*args, **kwargs):
            nonlocal saves
            saves += 1
            if saves == 1:
                raise OSError("test workspace save failure")
            return save(*args, **kwargs)

        with mock.patch("web_app.resolve_remote_source", return_value=REMOTE_METADATA), \
                mock.patch("web_app.fetch_remote_source") as fetch, \
                mock.patch.object(self.app, "_save_workspace", side_effect=fail_once):
            result = self.finish(self.app.update_remote({"workspace_id": remote["workspace_id"],
                                "token": ACCESS_TOKEN})["job_id"])
        self.assertEqual(result["status"], "FAILED", result)
        self.assertEqual(self.app.project, before_project)
        self.assertEqual(self.app.source_workspaces, before_registry)
        self.assertEqual(len(list(self.remote_root.iterdir())), 1)
        fetch.assert_not_called()

    def test_unchanged_commit_rebuilds_corrupt_index_locally_and_retains_conversation(self):
        with mock.patch("web_app.fetch_remote_source", side_effect=self.fetched_source):
            remote = self.finish(self.app.start_remote(self.payload())["job_id"])["result"]
        conversation = self.app.new_conversation({})["conversation"]
        self.app.conversation_store.append(conversation["id"], "user", "Keep the original business discussion.", status="completed")
        self.app.get_conversation(conversation["id"])
        before = copy.deepcopy(self.app.conversation)
        index = Path(remote["output"]) / "structural-index.sqlite"
        for corruption in ("header", "schema"):
            with self.subTest(corruption=corruption):
                damaged = bytearray(index.read_bytes())
                if corruption == "header":
                    damaged = b"damaged local index"
                else:
                    damaged[100] = 255
                index.write_bytes(damaged)
                with mock.patch("web_app.resolve_remote_source", return_value=REMOTE_METADATA), \
                        mock.patch("web_app.fetch_remote_source") as fetch:
                    result = self.finish(self.app.update_remote({"workspace_id": remote["workspace_id"],
                                        "token": ACCESS_TOKEN})["job_id"])
                self.assertEqual(result["status"], "COMPLETED", result)
                self.assertTrue(result["result"]["remote_update"]["index_rebuilt"])
                self.assertEqual(result["result"]["workspace_id"], remote["workspace_id"])
                self.assertEqual(self.app.conversation, before)
                self.assertEqual(self.new_app().project["snapshot_id"], result["result"]["snapshot_id"])
                self.assertEqual(len(list(self.remote_root.iterdir())), 1)
                fetch.assert_not_called()

    def test_failed_or_cancelled_local_repair_restores_missing_or_corrupt_index_and_reports(self):
        with mock.patch("web_app.fetch_remote_source", side_effect=self.fetched_source):
            remote = self.finish(self.app.start_remote(self.payload())["job_id"])["result"]
        conversation = self.app.new_conversation({})["conversation"]
        self.app.conversation_store.append(conversation["id"], "user", "Retain discussion through failed repair.", status="completed")
        self.app.get_conversation(conversation["id"])
        before_project = copy.deepcopy(self.app.project)
        before_registry = copy.deepcopy(self.app.source_workspaces)
        before_conversation = copy.deepcopy(self.app.conversation)
        source, output = Path(remote["source"]), Path(remote["output"])
        before_source = self.file_contents(source)
        reports = {path: path.read_bytes() for path in output.iterdir() if path.suffix in {".json", ".md"}}
        index = output / "structural-index.sqlite"
        original_analyzer = self.app.analyzer
        for condition in ("missing", "corrupt"):
            for cancelled in (False, True):
                with self.subTest(condition=condition, cancelled=cancelled):
                    for suffix in ("", "-wal", "-shm", "-journal"):
                        Path(str(index) + suffix).unlink(missing_ok=True)
                    if condition == "corrupt":
                        index.write_bytes(b"damaged local index")

                    def fail_after_rebuild(source, target, **options):
                        original_analyzer(source, target, **options)
                        if cancelled:
                            raise AnalysisCancelled("test cancelled repair")
                        raise OSError("test failed repair")

                    self.app.analyzer = fail_after_rebuild
                    with mock.patch("web_app.resolve_remote_source", return_value=REMOTE_METADATA), \
                            mock.patch("web_app.fetch_remote_source") as fetch:
                        result = self.finish(self.app.update_remote({"workspace_id": remote["workspace_id"],
                                            "token": ACCESS_TOKEN})["job_id"])
                    self.assertEqual(result["status"], "CANCELLED" if cancelled else "FAILED", result)
                    self.assertEqual(self.app.project, before_project)
                    self.assertEqual(self.app.source_workspaces, before_registry)
                    self.assertEqual(self.app.conversation, before_conversation)
                    self.assertEqual(self.file_contents(source), before_source)
                    self.assertEqual({path: path.read_bytes() for path in reports}, reports)
                    self.assertEqual(index.read_bytes() if index.exists() else None,
                                     b"damaged local index" if condition == "corrupt" else None)
                    for suffix in ("-wal", "-shm", "-journal"):
                        self.assertFalse(Path(str(index) + suffix).exists())
                    self.assertEqual(len(list(self.remote_root.iterdir())), 1)
                    self.assertFalse((output / SOURCE_UPDATE_PENDING).exists())
                    fetch.assert_not_called()

    def test_reconnect_missing_cached_index_rebuilds_locally(self):
        with mock.patch("web_app.fetch_remote_source", side_effect=self.fetched_source):
            remote = self.finish(self.app.start_remote(self.payload())["job_id"])["result"]
        (Path(remote["output"]) / "structural-index.sqlite").unlink()
        with mock.patch("web_app.fetch_remote_source") as fetch, mock.patch("web_app.resolve_remote_source") as resolve:
            response = self.app.start_remote(self.payload())
            self.assertTrue(response["source_reused"])
            result = self.finish(response["job_id"])
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertTrue(result["result"]["snapshot_id"])
        self.assertEqual(result["result"]["workspace_id"], remote["workspace_id"])
        fetch.assert_not_called()
        resolve.assert_not_called()

    def test_reconnect_corrupt_cached_index_rebuilds_without_network(self):
        with mock.patch("web_app.fetch_remote_source", side_effect=self.fetched_source):
            remote = self.finish(self.app.start_remote(self.payload())["job_id"])["result"]
        (Path(remote["output"]) / "structural-index.sqlite").write_bytes(b"damaged local index")
        with mock.patch("web_app.fetch_remote_source") as fetch, mock.patch("web_app.resolve_remote_source") as resolve:
            response = self.app.start_remote(self.payload())
            self.assertTrue(response["source_reused"])
            result = self.finish(response["job_id"])
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertTrue(result["result"]["snapshot_id"])
        self.assertEqual(result["result"]["workspace_id"], remote["workspace_id"])
        fetch.assert_not_called()
        resolve.assert_not_called()

    def test_new_remote_commit_indexes_new_snapshot_and_old_history_remains_available(self):
        with mock.patch("web_app.fetch_remote_source", side_effect=self.fetched_source):
            remote = self.finish(self.app.start_remote(self.payload())["job_id"])["result"]
        conversation = self.app.new_conversation({})["conversation"]
        self.app.conversation_store.append(conversation["id"], "user", "Explain the previous source.", status="completed")
        self.app.get_conversation(conversation["id"])
        before_files = self.file_contents(Path(remote["source"]), Path(remote["output"]))
        newer = {**REMOTE_METADATA, "commit": "b" * 40}

        def download(spec, destination, *, resolved=None, **kwargs):
            self.assertEqual(resolved, newer)
            metadata = self.fetched_source(spec, destination, **kwargs)
            self.write_source(destination, "UPDATED-REQUEST")
            return {**metadata, "commit": newer["commit"]}

        with mock.patch("web_app.resolve_remote_source", return_value=newer), \
                mock.patch("web_app.fetch_remote_source", side_effect=download) as fetch:
            result = self.finish(self.app.update_remote({"workspace_id": remote["workspace_id"],
                                "token": ACCESS_TOKEN})["job_id"])
        self.assertEqual(result["status"], "COMPLETED", result)
        current = result["result"]
        self.assertEqual(current["remote_update"]["status"], "updated")
        self.assertEqual(current["remote_source"]["commit"], newer["commit"])
        self.assertNotEqual(current["workspace_id"], remote["workspace_id"])
        self.assertEqual(current["programs"][0]["program_name"], "UPDATED-REQUEST")
        self.assertIsNone(self.app.conversation)
        self.assertEqual(self.file_contents(Path(remote["source"]), Path(remote["output"])), before_files)
        self.assertEqual(fetch.call_count, 1)
        old = self.app.open_source_conversation({"workspace_id": remote["workspace_id"],
                                                "conversation_id": conversation["id"]})
        self.assertEqual(old["project"]["snapshot_id"], remote["snapshot_id"])
        self.assertEqual(old["conversation"]["messages"][0]["content"], "Explain the previous source.")
        self.assert_no_token_saved()

    def test_update_auth_failure_and_cancel_leave_existing_cache_and_registry_unchanged(self):
        with mock.patch("web_app.fetch_remote_source", side_effect=self.fetched_source):
            remote = self.finish(self.app.start_remote(self.payload())["job_id"])["result"]
        before_project, before_registry = copy.deepcopy(self.app.project), copy.deepcopy(self.app.source_workspaces)
        before_files = self.file_contents(Path(remote["source"]), Path(remote["output"]))
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                def check(*args, check_cancel=None, **kwargs):
                    if cancel:
                        self.app.cancel_event.set()
                        check_cancel()
                    raise RemoteSourceError("REMOTE_SOURCE_AUTH_FAILED", "Repository authentication failed.")
                with mock.patch("web_app.resolve_remote_source", side_effect=check), mock.patch("web_app.fetch_remote_source") as fetch:
                    result = self.finish(self.app.update_remote({"workspace_id": remote["workspace_id"],
                                        "token": ACCESS_TOKEN})["job_id"])
                self.assertEqual(result["status"], "CANCELLED" if cancel else "FAILED")
                self.assertEqual(self.app.project, before_project)
                self.assertEqual(self.app.source_workspaces, before_registry)
                self.assertEqual(self.file_contents(Path(remote["source"]), Path(remote["output"])), before_files)
                self.assertEqual(len(list(self.remote_root.iterdir())), 1)
                fetch.assert_not_called()
        self.assert_no_token_saved()

    def test_cloud_pinned_link_only_follows_default_branch_when_explicitly_selected(self):
        url = "https://bitbucket.org/example-workspace/business-source/src/" + "a" * 40 + "/stage/"
        username = "test-reader@example.invalid"
        captured = []

        def download(spec, destination, *, username=None, **kwargs):
            captured.append(spec)
            self.assertEqual(username, "test-reader@example.invalid")
            self.fetched_source(spec, destination, **kwargs)
            return {**spec.public_metadata(), "commit": "a" * 40,
                    **({"default_branch": "main"} if spec.ref is None else {})}

        with mock.patch("web_app.fetch_remote_source", side_effect=download):
            first = self.finish(self.app.start_remote(self.payload(url=url, ref=None, username=username))["job_id"])
            followed = self.finish(self.app.start_remote(self.payload(url=url, ref=None, username=username,
                                        follow_default_branch=True))["job_id"])
        self.assertEqual(first["status"], "COMPLETED", first)
        self.assertEqual(followed["status"], "COMPLETED", followed)
        self.assertEqual(captured[0].ref, "a" * 40)
        self.assertIsNone(captured[1].ref)
        metadata = followed["result"]["remote_source"]
        self.assertEqual(metadata["provider"], "bitbucket_cloud")
        self.assertEqual(metadata["default_branch"], "main")
        self.assertEqual(self.new_app().project["remote_source"], metadata)
        self.assertNotIn(username, json.dumps(self.app.state()))
        self.assertNotIn(username, self.state_path.read_text())
        self.assert_no_token_saved()

    def test_completed_remote_snapshot_survives_immediate_local_switch_before_worker_exit(self):
        published, release = threading.Event(), threading.Event()
        app, delegate = self.app, self.app.lock
        remote_worker = None

        class PublicationGate:
            """Schedule the next user action as soon as completion is visible."""
            paused = False

            def __enter__(self):
                delegate.acquire()
                return self

            def __exit__(self, *exc):
                pause = (not self.paused and threading.current_thread() is remote_worker
                         and app.job and app.job["status"] == "COMPLETED"
                         and app.job.get("source_origin") == "remote_repository")
                if pause:
                    self.paused = True
                delegate.release()
                if pause:
                    published.set()
                    release.wait(5)

        def fetch(*args, **kwargs):
            nonlocal remote_worker
            remote_worker = threading.current_thread()
            return self.fetched_source(*args, **kwargs)

        self.app.lock = PublicationGate()
        with mock.patch("web_app.fetch_remote_source", side_effect=fetch):
            accepted = self.app.start_remote(self.payload())
            try:
                self.assertTrue(published.wait(3))
                remote_project = self.app.get_job(accepted["job_id"])["result"]
                local_job = self.app.start({"source": str(self.local_source),
                                            "output": str(self.local_output), "allow_network": False})
                self.assertEqual(self.finish(local_job["job_id"])["status"], "COMPLETED")
            finally:
                release.set()
                if remote_worker:
                    remote_worker.join(timeout=3)
        self.assertFalse(remote_worker.is_alive())
        self.assertTrue(Path(remote_project["source"]).is_dir())
        self.assertTrue((Path(remote_project["output"]) / "structural-index.sqlite").is_file())

    def test_remote_endpoint_requires_session_and_same_origin_before_downloading(self):
        server = create_server(0, app=self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def post(headers):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("POST", "/api/source/remote", json.dumps(self.payload()),
                                   {"Content-Type": "application/json", **headers})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        try:
            with mock.patch("web_app.fetch_remote_source", side_effect=self.fetched_source) as fetch:
                self.assertEqual(post({"Origin": server.origin})[0], 403)
                self.assertEqual(post({"Origin": "https://other.example.invalid",
                                       "X-Session-Token": self.app.session_token})[0], 403)
                fetch.assert_not_called()
                status, accepted = post({"Origin": server.origin, "X-Session-Token": self.app.session_token})
                self.assertEqual(status, 202, accepted)
                result = self.finish(accepted["job_id"])
                self.assertEqual(result["status"], "COMPLETED", result)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assert_no_token_saved()


if __name__ == "__main__":
    unittest.main()
