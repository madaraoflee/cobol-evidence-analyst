from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from business_index import build_business_index
from online_material_export import export_material
from online_material_format import MaterialError, canonical_json, seal_manifest
from online_material_reader import DirectoryObjectStore, MaterialReader
from online_workspace import OnlineWorkbench


def program(name, body='DISPLAY "STATUS-READY".'):
    return f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nPROCEDURE DIVISION.\nMAIN-PARA.\n{body}\nGOBACK.\n".encode()


class OnlineWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_root = self.root / "online"
        self.materials = {}
        self.sources = {}
        self.manifests = {}
        self.factory_calls = []
        self.source_reads = []
        self.runner_calls = []
        self.apps = []
        self.create_material("a" * 40, "first", program("MAIN-ENTRY"))
        self.create_material("b" * 40, "second", program("MAIN-ENTRY", 'DISPLAY "STATUS-CHANGED".'))
        self.app = self.make_app()
        self.addCleanup(self.close_apps)

    def close_apps(self):
        for app in self.apps:
            app.close()
        for app in self.apps:
            for thread in list(app._threads.values()):
                thread.join(2)

    def create_material(self, commit, name, content, *, subdirectory="", skipped_files=0):
        source, material = self.root / (name + "-source"), self.root / name
        source.mkdir()
        (source / "main.cbl").write_bytes(content)
        for number in range(skipped_files):
            (source / f"excluded-{number:03}.cbl").write_bytes(b"\x00\xff\x00\xff")
        database = self.root / (name + ".sqlite")
        report = build_business_index(source, database, source_format="free", framework_reference_path="", verify_content=True)
        manifest = export_material(database, source, material, repository_id="example/source", commit=commit,
                                   subdirectory=subdirectory, build_report=report)
        self.materials["https://materials.invalid/" + name + "/"] = material
        self.sources[commit] = {"main.cbl": content}
        self.manifests[name] = manifest
        shutil.rmtree(source)
        database.unlink()

    def material_factory(self, location, credentials, expected, check_cancel):
        check_cancel()
        self.factory_calls.append((deepcopy(location), dict(credentials), dict(expected)))
        return MaterialReader(DirectoryObjectStore(self.materials[location["base_url"]]),
                              expected_repository=expected.get("repository"), expected_commit=expected.get("commit"),
                              expected_revision=expected.get("revision"), check_cancel=check_cancel)

    def source_factory(self, repository, credentials, check_cancel):
        def read(path, *, max_bytes):
            check_cancel()
            self.source_reads.append((repository["commit"], path, max_bytes))
            return self.sources[repository["commit"]][path]
        return read

    def runner(self, query, source, question, **options):
        self.runner_calls.append((question, options))
        options["progress"]({"phase": "reading", "checkpoint": {"request_status": "idle", "question": question}})
        hits = query.search("STATUS")
        self.assertEqual(hits["matches"][0]["relative_path"], "main.cbl")
        evidence = query.evidence("main.cbl", 4, 5, source)
        citation = {"id": "E" + "1" * 24, **evidence["citation"]}
        return {"status": "answered", "answer": "The verified status is displayed [" + citation["id"] + "].",
                "checkpoint": {"request_status": "idle"}, "citations": [citation],
                "evidence": [{"id": citation["id"], **evidence}], "coverage": hits["coverage"],
                "model_calls": 1, "actions": 2, "error": None}

    def make_app(self, **options):
        app = OnlineWorkbench(self.state_root, config_provider=lambda: None, material_factory=self.material_factory,
                              source_factory=self.source_factory, investigator=options.pop("investigator", self.runner), **options)
        self.apps.append(app)
        return app

    def post(self, path, data, expected=200, app=None):
        status, result = (app or self.app).dispatch("POST", "/api/online/" + path, data)
        self.assertEqual(status, expected, result)
        return result

    def wait(self, identifier, app=None):
        app = app or self.app
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, data = app.dispatch("GET", "/api/online/jobs/" + identifier)
            self.assertEqual(status, 200, data)
            if data["job"]["status"] != "running":
                return data["job"]
            time.sleep(0.005)
        self.fail("Job did not complete")

    def connect(self, name="first", *, credentials=None, app=None, updating=None):
        payload = {"location": {"kind": "https", "base_url": "https://materials.invalid/" + name + "/"},
                   "repository": "example/source", "commit": self.manifests[name]["repository"]["commit"],
                   "revision": self.manifests[name]["revision"]}
        if credentials is not None:
            payload["credentials"] = credentials
        if updating:
            payload["workspace_id"] = updating
        started = self.post("update" if updating else "connect", payload, 202, app=app)
        return self.wait(started["job_id"], app=app)

    def conversation(self, workspace_id, app=None):
        return self.post("conversations", {"workspace_id": workspace_id}, 201, app=app)["conversation"]

    def ask(self, conversation_id, question="What does this status mean?", **extra):
        started = self.post("question", {"conversation_id": conversation_id, "question": question,
                                         "allow_source_transfer": True, **extra}, 202)
        return self.wait(started["job_id"])

    def test_end_to_end_persists_online_history_without_local_source_or_index(self):
        sentinel = self.root / "local-workbench-state.json"
        sentinel.write_bytes(b'{"unchanged":true}')
        connected = self.connect()
        self.assertEqual(connected["status"], "completed")
        conversation = self.conversation(connected["workspace_id"])
        job = self.ask(conversation["id"])
        self.assertEqual(job["status"], "answered")
        self.assertEqual(job["transfer"]["source_requests"], 1)
        self.assertEqual(sentinel.read_bytes(), b'{"unchanged":true}')
        self.assertEqual([path.name for path in self.state_root.iterdir()], ["online-state.sqlite"])
        self.assertFalse(list(self.root.glob("*-source")))
        state = self.app.state()
        saved = state["conversation"]
        self.assertEqual([item["role"] for item in saved["messages"]], ["user", "assistant"])
        self.assertIsNone(saved["pending"])
        self.assertEqual(len(saved["messages"][-1]["citations"]), 1)
        reads_before = len(self.factory_calls)
        restarted = self.make_app()
        restored = restarted.state()
        self.assertEqual(len(self.factory_calls), reads_before)
        self.assertEqual(restored["conversation"], saved)
        self.assertFalse(restored["workspaces"][0]["connected"])
        status, response = restarted.dispatch("POST", "/api/online/question", {
            "conversation_id": conversation["id"], "question": "Again?", "allow_source_transfer": True})
        self.assertEqual((status, response["error"]), (409, "ONLINE_RECONNECT_REQUIRED"))

    def test_workspace_coverage_preserves_actual_skipped_files_and_incomplete_input(self):
        self.create_material("c" * 40, "skipped", program("MAIN-ENTRY"), skipped_files=20)
        connected = self.connect("skipped")
        self.assertEqual(connected["status"], "completed")
        coverage = self.app.state()["workspaces"][0]["coverage"]
        self.assertEqual(coverage["indexed_files"], 1)
        self.assertEqual(coverage["input_skip_count"], 20)
        self.assertFalse(coverage["input_coverage_complete"])
        self.assertEqual(len(coverage["input_skips"]), 16)
        self.assertTrue(coverage["input_skips_truncated"])
        self.assertTrue(all(item["relative_path"].startswith("excluded-") for item in coverage["input_skips"]))
        self.assertTrue(all("reason_code" in item for item in coverage["input_skips"]))

    def test_coverage_detail_limit_keeps_skip_count_and_completeness_visible(self):
        manifest = deepcopy(self.manifests["first"])
        manifest.pop("revision")
        manifest["coverage"]["input_skips"] = [{"relative_path": str(number) + "x" * 3000,
                                                "reason_code": "UNREADABLE_SOURCE"} for number in range(20)]
        manifest["coverage"]["build_scope"]["input_coverage_complete"] = False
        manifest = seal_manifest(manifest)
        (self.materials["https://materials.invalid/first/"] / "material.json").write_bytes(canonical_json(manifest))
        self.manifests["first"] = manifest
        self.assertEqual(self.connect()["status"], "completed")
        coverage = self.app.state()["workspaces"][0]["coverage"]
        self.assertLessEqual(len(canonical_json(coverage)), 16384)
        self.assertEqual(coverage["input_skip_count"], 20)
        self.assertFalse(coverage["input_coverage_complete"])
        self.assertEqual(coverage["input_skips"], [])
        self.assertTrue(coverage["input_skips_truncated"])

    def test_credentials_are_ram_only_and_reauthorization_restores_history_access(self):
        secrets = {"material_token": "material-secret-value", "source_token": "source-secret-value"}
        connected = self.connect(credentials=secrets)
        conversation = self.conversation(connected["workspace_id"])
        self.ask(conversation["id"])
        for value in secrets.values():
            self.assertNotIn(value.encode(), (self.state_root / "online-state.sqlite").read_bytes())
            self.assertNotIn(value, json.dumps(self.app.state()))
        restarted = self.make_app()
        self.post("credentials", {"workspace_id": connected["workspace_id"], "credentials": secrets}, app=restarted)
        self.assertTrue(restarted.state()["workspaces"][0]["connected"])
        self.assertEqual(restarted.state()["conversation"]["id"], conversation["id"])

    def test_new_revision_has_distinct_workspace_and_old_conversation_keeps_old_evidence(self):
        first = self.connect()
        conversation = self.conversation(first["workspace_id"])
        self.ask(conversation["id"])
        old_citation = self.app.state()["conversation"]["messages"][-1]["citations"][0]
        second = self.connect("second", updating=first["workspace_id"])
        self.assertEqual(second["status"], "completed")
        self.assertNotEqual(first["workspace_id"], second["workspace_id"])
        self.assertIsNone(self.app.state()["current_conversation_id"])
        self.post("open", {"conversation_id": conversation["id"]})
        status, evidence = self.app.dispatch("GET", "/api/online/evidence", query={
            "conversation_id": conversation["id"], "citation_id": old_citation["key"]})
        self.assertEqual(status, 200, evidence)
        self.assertIn("STATUS-READY", evidence["text"])
        self.assertEqual(evidence["citation"]["commit"], "a" * 40)
        self.assertEqual(self.source_reads[-1][0], "a" * 40)
        repeated = self.connect("first", updating=first["workspace_id"])
        self.assertTrue(repeated["unchanged"])
        self.assertEqual(self.app.state()["current_conversation_id"], conversation["id"])

    def test_followup_history_contains_bounded_navigation_hints_without_old_evidence_ids(self):
        connected = self.connect()
        conversation = self.conversation(connected["workspace_id"])
        self.assertEqual(self.ask(conversation["id"])["status"], "answered")
        first_citation = self.app.state()["conversation"]["messages"][-1]["citations"][0]
        reads_before = len(self.source_reads)
        self.assertEqual(self.ask(conversation["id"], "Explain that branch again")["status"], "answered")
        history = self.runner_calls[-1][1]["history"]
        self.assertEqual([item["role"] for item in history], ["user", "assistant"])
        hint = history[-1]["citations"][0]
        self.assertEqual(set(hint), {"path", "start", "end", "commit", "revision", "hash"})
        self.assertEqual(hint, {key: first_citation[key] for key in hint})
        self.assertEqual(hint["path"], "main.cbl")
        self.assertEqual(hint["commit"], "a" * 40)
        self.assertEqual(history[-1]["citations_omitted"], 0)
        self.assertGreater(len(self.source_reads), reads_before)
        with self.app.store.transaction() as db:
            saved = self.app._conversation(db, conversation["id"])
            saved["messages"][-1]["citations"] = [deepcopy(first_citation) for _ in range(70)]
            self.app._save_conversation(db, saved)
        self.assertEqual(self.ask(conversation["id"], "Recheck the stated branch")["status"], "answered")
        last_history = self.runner_calls[-1][1]["history"][-1]
        self.assertEqual(len(last_history["citations"]), 64)
        self.assertEqual(last_history["citations_omitted"], 6)

    def test_failed_update_keeps_selected_workspace_and_conversation(self):
        connected = self.connect()
        conversation = self.conversation(connected["workspace_id"])
        started = self.post("update", {"workspace_id": connected["workspace_id"], "repository": "example/source",
            "location": {"kind": "https", "base_url": "https://materials.invalid/second/"}, "commit": "a" * 40}, 202)
        failed = self.wait(started["job_id"])
        self.assertEqual(failed["error"], "MATERIAL_COMMIT_MISMATCH")
        state = self.app.state()
        self.assertEqual(state["current_workspace_id"], connected["workspace_id"])
        self.assertEqual(state["current_conversation_id"], conversation["id"])
        self.assertEqual(len(state["workspaces"]), 1)

    def test_changed_material_location_never_inherits_old_credentials_implicitly(self):
        credentials = {"material_token": "private-material-token", "source_token": "private-source-token"}
        connected = self.connect(credentials=credentials)
        original_calls = len(self.factory_calls)
        payload = {"workspace_id": connected["workspace_id"], "repository": "example/source", "location": {
            "kind": "https", "base_url": "https://materials.invalid/second/"}}
        status, response = self.app.dispatch("POST", "/api/online/update", payload)
        self.assertEqual((status, response["error"]), (409, "ONLINE_MATERIAL_REAUTH_REQUIRED"))
        self.assertEqual(len(self.factory_calls), original_calls)
        self.assertEqual(self.app.state()["current_workspace_id"], connected["workspace_id"])
        self.assertEqual(self.app._credentials[connected["workspace_id"]], credentials)
        started = self.post("update", {**payload, "credentials": {"material_token": "new-material-token"}}, 202)
        self.assertEqual(self.wait(started["job_id"])["status"], "completed")
        self.assertEqual(self.factory_calls[-1][1], {
            "material_token": "new-material-token", "source_token": "private-source-token"})

    def test_explicit_empty_material_fields_clear_them_and_preserve_source_scope(self):
        connected = self.connect(credentials={"material_token": "private-material-token", "source_token": "private-source-token"})
        started = self.post("update", {"workspace_id": connected["workspace_id"], "repository": "example/source",
            "location": {"kind": "https", "base_url": "https://materials.invalid/second/"},
            "credentials": {"material_token": "", "material_username": ""}}, 202)
        self.assertEqual(self.wait(started["job_id"])["status"], "completed")
        self.assertEqual(self.factory_calls[-1][1], {"source_token": "private-source-token"})

    def test_transfer_requires_explicit_permission_and_does_not_append_denied_question(self):
        connected = self.connect()
        conversation = self.conversation(connected["workspace_id"])
        for flag in (None, False, "true", 1):
            status, result = self.app.dispatch("POST", "/api/online/question", {
                "conversation_id": conversation["id"], "question": "Explain", "allow_source_transfer": flag})
            self.assertEqual((status, result["error"]), (403, "ONLINE_SOURCE_TRANSFER_REQUIRED"))
        self.assertEqual(self.app.state()["conversation"]["messages"], [])
        self.assertEqual(self.runner_calls, [])

    def test_connection_failure_before_model_request_can_resume_after_reauthorization(self):
        connected = self.connect()
        conversation = self.conversation(connected["workspace_id"])
        original_factory = self.app.material_factory
        def forbidden(*args):
            raise MaterialError("MATERIAL_UNAUTHORIZED")
        self.app.material_factory = forbidden
        failed = self.ask(conversation["id"])
        self.assertEqual(failed["error"], "MATERIAL_UNAUTHORIZED")
        self.assertEqual(self.runner_calls, [])
        self.assertTrue(self.app.state()["conversation"]["pending"]["can_resume"])
        self.app.material_factory = original_factory
        self.post("credentials", {"workspace_id": connected["workspace_id"], "credentials": {}})
        resumed = self.post("resume", {"conversation_id": conversation["id"], "allow_source_transfer": True}, 202)
        self.assertEqual(self.wait(resumed["job_id"])["status"], "answered")
        self.assertIsNone(self.runner_calls[-1][1]["checkpoint"])
        self.assertEqual(len([item for item in self.app.state()["conversation"]["messages"] if item["role"] == "user"]), 1)

    def test_later_navigation_is_not_replaced_when_connection_finishes(self):
        first = self.connect()
        conversation = self.conversation(first["workspace_id"])
        entered, released = threading.Event(), threading.Event()
        original_factory = self.app.material_factory
        def delayed(*args):
            entered.set()
            released.wait(2)
            return original_factory(*args)
        self.app.material_factory = delayed
        started = self.post("connect", {"repository": "example/source", "location": {
            "kind": "https", "base_url": "https://materials.invalid/second/"}}, 202)
        self.assertTrue(entered.wait(2))
        self.post("open", {"workspace_id": first["workspace_id"]})
        released.set()
        completed = self.wait(started["job_id"])
        self.assertFalse(completed["adopted"])
        self.assertEqual(self.app.state()["current_workspace_id"], first["workspace_id"])
        self.assertIsNone(self.app.state()["current_conversation_id"])

    def test_cancel_and_resume_preserve_committed_checkpoint_and_original_question(self):
        entered = threading.Event()
        def waiting(query, source, question, **options):
            options["progress"]({"phase": "searching", "checkpoint": {"request_status": "idle", "question": question, "completed": ["one"]}})
            entered.set()
            while True:
                options["check_cancel"]()
                time.sleep(0.005)
        self.app.investigator = waiting
        connected = self.connect()
        conversation = self.conversation(connected["workspace_id"])
        started = self.post("question", {"conversation_id": conversation["id"], "question": "Preserve this question", "allow_source_transfer": True}, 202)
        self.assertTrue(entered.wait(2))
        self.post("cancel", {"job_id": started["job_id"]})
        self.assertEqual(self.wait(started["job_id"])["status"], "cancelled")
        public = self.app.state()["conversation"]
        self.assertTrue(public["pending"]["can_resume"])
        self.assertNotIn("checkpoint", public["pending"])
        self.app.investigator = self.runner
        resumed = self.post("resume", {"conversation_id": conversation["id"], "allow_source_transfer": True}, 202)
        self.assertEqual(self.wait(resumed["job_id"])["status"], "answered")
        question, options = self.runner_calls[-1]
        self.assertEqual(question, "Preserve this question")
        self.assertEqual(options["checkpoint"]["completed"], ["one"])
        self.assertEqual(len([item for item in self.app.state()["conversation"]["messages"] if item["role"] == "user"]), 1)

    def test_unknown_model_outcome_requires_explicit_retry_after_restart(self):
        connected = self.connect()
        conversation = self.conversation(connected["workspace_id"])
        with self.app.store.transaction() as db:
            saved = self.app._conversation(db, conversation["id"])
            saved.update(status="running", pending={"question": "Uncertain request", "user_message_id": "c" * 32,
                "status": "running", "checkpoint": {"request_status": "in_flight"}})
            saved["messages"] = [{"id": "c" * 32, "role": "user", "content": "Uncertain request", "status": "pending"}]
            self.app._save_conversation(db, saved)
            job = self.app._new_job(db, "question", workspace_id=connected["workspace_id"], conversation_id=conversation["id"])
        def uncertain(query, source, question, **options):
            if not options["approve_retry"]:
                return {"status": "retry_confirmation_required", "answer": "", "checkpoint": options["checkpoint"],
                        "error": "ONLINE_MODEL_OUTCOME_UNKNOWN"}
            return self.runner(query, source, question, **options)
        restarted = self.make_app(investigator=uncertain)
        self.assertEqual(restarted.state()["conversation"]["status"], "interrupted")
        self.assertTrue(restarted.state()["conversation"]["pending"]["retry_confirmation_required"])
        self.post("credentials", {"workspace_id": connected["workspace_id"], "credentials": {}}, app=restarted)
        started = self.post("resume", {"conversation_id": conversation["id"], "allow_source_transfer": True}, 202, app=restarted)
        self.assertEqual(self.wait(started["job_id"], app=restarted)["status"], "retry_confirmation_required")
        started = self.post("resume", {"conversation_id": conversation["id"], "allow_source_transfer": True, "approve_retry": True}, 202, app=restarted)
        self.assertEqual(self.wait(started["job_id"], app=restarted)["status"], "answered")

    def test_export_is_version_bound_and_delete_does_not_change_other_conversations(self):
        connected = self.connect()
        first = self.conversation(connected["workspace_id"])
        self.ask(first["id"])
        second = self.conversation(connected["workspace_id"])
        for format in ("json", "markdown"):
            status, exported = self.app.dispatch("GET", "/api/online/export", query={"conversation_id": first["id"], "format": format})
            self.assertEqual(status, 200, exported)
            self.assertIn("a" * 40, exported["content"])
            self.assertIn("The verified status", exported["content"])
            self.assertNotIn('"checkpoint"', exported["content"])
        status, result = self.app.dispatch("DELETE", "/api/online/conversations/" + first["id"])
        self.assertEqual(status, 200, result)
        self.assertEqual(self.app.state()["current_conversation_id"], second["id"])
        self.assertEqual([item["id"] for item in self.app.state()["conversations"]], [second["id"]])

    def test_credentials_and_untrusted_locations_are_rejected_before_network(self):
        invalid_locations = [
            {"kind": "directory", "path": str(self.root)},
            {"kind": "https", "base_url": "https://user:secret@example.invalid/"},
            {"kind": "https", "base_url": "https://example.invalid/?token=secret"},
            {"kind": "https", "base_url": "file:///source"},
            {"kind": "downloads", "workspace": "sample", "repository": "source", "release_name": "../other"},
        ]
        for location in invalid_locations:
            status, result = self.app.dispatch("POST", "/api/online/connect", {"location": location, "repository": "example/source"})
            self.assertNotEqual(status, 202, result)
        self.assertFalse(self.factory_calls)
        status, result = self.app.dispatch("POST", "/api/online/connect", {
            "location": {"kind": "https", "base_url": "https://materials.invalid/first/"}, "repository": "example/source",
            "credentials": {"material_token": "token\nsecret"}})
        self.assertEqual(result["error"], "ONLINE_INVALID_CREDENTIAL")
        self.assertNotIn("token", json.dumps(result))

    def test_downloads_connect_and_release_lookup_keep_source_and_storage_identity_separate(self):
        self.app.material_factory = None
        manifest = self.manifests["first"]
        descriptor = {"repository": manifest["repository"], "revision": manifest["revision"]}
        release_name = "analysis-release-" + "d" * 64 + ".json"
        with mock.patch("online_distribution.DownloadsClient") as client, \
                mock.patch("online_distribution.load_release", return_value=descriptor) as load, \
                mock.patch("online_distribution.DownloadsObjectStore", return_value=DirectoryObjectStore(self.materials["https://materials.invalid/first/"])) as store:
            client.return_value.list_release_names.return_value = [release_name]
            client.return_value.list_release_entries.return_value = [{"name": release_name, "created_on": None, "size": 1000}]
            names = self.post("releases", {"workspace": "materials", "repository": "published",
                "credentials": {"material_token": "read-token"}})
            self.assertEqual(names["release_names"], [release_name])
            selected = self.post("release", {"workspace": "materials", "repository": "published", "release_name": release_name})
            self.assertEqual(selected["release"]["repository"]["identity"], "example/source")
            started = self.post("connect", {"repository": "example/source", "location": {"kind": "downloads",
                "workspace": "materials", "repository": "published", "release_name": release_name},
                "credentials": {"material_token": "read-token"}}, 202)
            completed = self.wait(started["job_id"])
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(load.call_args.args[1], release_name)
            self.assertEqual(store.call_args.args[1], manifest["revision"])
            self.assertEqual(store.call_args.kwargs["release"], descriptor)
        state = self.app.state()
        self.assertEqual(state["workspaces"][0]["repository"]["identity"], "example/source")
        self.assertEqual(state["workspaces"][0]["location"]["repository"], "published")
        self.assertNotIn("read-token", json.dumps(state))

    def test_source_basis_tracks_selected_release_and_survives_restart(self):
        self.app.material_factory = None
        manifest = self.manifests["first"]
        cases = [
            ("d", "online-release/v2", {"source_snapshot": {"sync_completeness": "not_verified"}}, "repository_snapshot"),
            ("e", "online-release/v1", {"sync_receipt": {"complete": True}}, "sync_attested"),
            ("f", "online-release/v2", {"source_snapshot": {"sync_completeness": "not_verified"}}, "repository_snapshot"),
        ]
        identifiers = []
        for suffix, schema, extra, expected in cases:
            descriptor = {"schema_version": schema, "repository": manifest["repository"],
                          "revision": manifest["revision"], **extra}
            name = "analysis-release-" + suffix * 64 + ".json"
            with mock.patch("online_distribution.DownloadsClient"), \
                    mock.patch("online_distribution.load_release", return_value=descriptor), \
                    mock.patch("online_distribution.DownloadsObjectStore", return_value=DirectoryObjectStore(self.materials["https://materials.invalid/first/"])):
                preview = self.post("release", {"workspace": "materials", "repository": "published", "release_name": name})
                self.assertEqual(preview["release"]["source_basis"], expected)
                job = self.post("connect", {"repository": "example/source", "location": {"kind": "downloads",
                    "workspace": "materials", "repository": "published", "release_name": name}}, 202)
                completed = self.wait(job["job_id"])
                self.assertEqual(completed["status"], "completed")
                identifiers.append(completed["workspace_id"])
            workspace = self.app.state()["workspaces"][0]
            self.assertEqual(workspace["source_basis"], expected)
            self.assertEqual(workspace["location"]["release_name"], name)
            restarted = self.make_app()
            self.assertEqual(restarted.state()["workspaces"][0]["source_basis"], expected)
        self.assertEqual(len(set(identifiers)), 1)
        self.assertEqual(len(self.app.state()["workspaces"]), 1)

    def test_https_and_old_workspace_states_do_not_claim_sync_completion(self):
        completed = self.connect()
        self.assertEqual(self.app.state()["workspaces"][0]["source_basis"], "unknown")
        with self.app.store.transaction() as db:
            workspace = self.app.store.get(db, "workspaces", completed["workspace_id"])
            workspace.pop("source_basis")
            self.app.store.put(db, "workspaces", workspace["id"], workspace)
        self.assertEqual(self.make_app().state()["workspaces"][0]["source_basis"], "unknown")
        with self.app.store.transaction() as db:
            stored = self.app.store.get(db, "workspaces", completed["workspace_id"])
            self.assertNotIn("source_basis", stored)

    def test_persisted_source_basis_is_a_bounded_status(self):
        completed = self.connect()
        for invalid in ("complete", ["sync_attested"], {"complete": True}):
            with self.app.store.transaction() as db:
                workspace = self.app.store.get(db, "workspaces", completed["workspace_id"])
                workspace["source_basis"] = invalid
                self.app.store.put(db, "workspaces", workspace["id"], workspace)
            with self.assertRaisesRegex(MaterialError, "ONLINE_STATE_INVALID"):
                self.app.state()

    def test_credentials_never_persist_when_investigator_echoes_them_in_result(self):
        token = "source-secret-value"
        def leaking(query, source, question, **options):
            options["progress"]({"phase": token, "checkpoint": {"request_status": "idle", "diagnostic": token}})
            return {"status": "failed", "answer": token, "checkpoint": {"request_status": "idle", "diagnostic": token},
                    "error": token}
        self.app.investigator = leaking
        connected = self.connect(credentials={"source_token": token})
        conversation = self.conversation(connected["workspace_id"])
        failed = self.ask(conversation["id"])
        self.assertEqual(failed["error"], "ONLINE_CREDENTIAL_EXPOSURE")
        self.assertNotIn(token.encode(), self.app.store.path.read_bytes())
        self.assertNotIn(token, json.dumps(self.app.state()))

    def test_account_name_in_repository_and_evidence_is_never_rewritten(self):
        def partial(query, source, question, **options):
            checkpoint = {"request_status": "not_sent", "repository": deepcopy(query.reader.manifest["repository"]),
                          "evidence_text": "example account source text"}
            options["progress"]({"checkpoint": checkpoint})
            return {"status": "needs_continuation", "answer": "example account remains to inspect", "checkpoint": checkpoint}
        self.app.investigator = partial
        connected = self.connect(credentials={"material_username": "example", "material_token": "private-material-token"})
        conversation = self.conversation(connected["workspace_id"])
        self.assertEqual(self.ask(conversation["id"])["status"], "needs_continuation")
        with self.app.store.transaction() as db:
            saved = self.app._conversation(db, conversation["id"])
        self.assertEqual(saved["pending"]["checkpoint"]["repository"]["identity"], "example/source")
        self.assertEqual(saved["pending"]["checkpoint"]["evidence_text"], "example account source text")
        self.app.investigator = self.runner
        resumed = self.post("resume", {"conversation_id": conversation["id"], "allow_source_transfer": True}, 202)
        self.assertEqual(self.wait(resumed["job_id"])["status"], "answered")
        self.assertEqual(self.runner_calls[-1][1]["checkpoint"]["repository"]["identity"], "example/source")

    def test_busy_job_cannot_be_replaced_or_deleted_and_close_is_nonblocking(self):
        entered = threading.Event()
        release = threading.Event()
        def waiting(query, source, question, **options):
            entered.set()
            release.wait(2)
            options["check_cancel"]()
            return self.runner(query, source, question, **options)
        self.app.investigator = waiting
        connected = self.connect()
        conversation = self.conversation(connected["workspace_id"])
        started = self.post("question", {"conversation_id": conversation["id"], "question": "Work", "allow_source_transfer": True}, 202)
        self.assertTrue(entered.wait(2))
        status, result = self.app.dispatch("DELETE", "/api/online/conversations/" + conversation["id"])
        self.assertEqual((status, result["error"]), (409, "ONLINE_BUSY"))
        started_at = time.monotonic()
        self.app.close()
        self.assertLess(time.monotonic() - started_at, 0.2)
        release.set()
        for thread in list(self.app._threads.values()):
            thread.join(2)
        with self.app.store.transaction() as db:
            self.assertEqual(self.app.store.get(db, "jobs", started["job_id"])["status"], "cancelled")

    def test_checkpoint_failure_is_transactional_and_does_not_erase_previous_progress(self):
        def large_checkpoint(query, source, question, **options):
            options["progress"]({"phase": "first", "checkpoint": {"request_status": "idle", "committed": True}})
            options["progress"]({"checkpoint": {"bad": "x" * (4 * 1024 * 1024)}})
        self.app.investigator = large_checkpoint
        connected = self.connect()
        conversation = self.conversation(connected["workspace_id"])
        job = self.ask(conversation["id"])
        self.assertEqual(job["error"], "ONLINE_CHECKPOINT_LIMIT")
        with self.app.store.transaction() as db:
            saved = self.app._conversation(db, conversation["id"])
        self.assertTrue(saved["pending"]["checkpoint"]["committed"])

    def test_state_database_and_root_symlinks_are_refused(self):
        other = self.root / "other"
        other.mkdir()
        linked = self.root / "linked"
        try:
            linked.symlink_to(other, target_is_directory=True)
        except OSError:
            self.skipTest("Symlinks unavailable")
        with self.assertRaises(MaterialError):
            OnlineWorkbench(linked)
        target = self.root / "state-target.sqlite"
        target.write_bytes(b"unchanged")
        new_root = self.root / "bad-root"
        new_root.mkdir()
        (new_root / "online-state.sqlite").symlink_to(target)
        with self.assertRaises(MaterialError):
            OnlineWorkbench(new_root)
        self.assertEqual(target.read_bytes(), b"unchanged")


if __name__ == "__main__":
    unittest.main()
