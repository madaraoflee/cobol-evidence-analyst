from __future__ import annotations

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


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from analyze_source import analyze_source
from company_api import CompanyAPIConfig, TransportResponse
from web_app import RequestError, WorkbenchState, _pick_local_folder, create_server


TEST_KEY = "local-test-credential-do-not-serialize"
TEST_ENDPOINT = "https://gateway.example.invalid/v1"
TEST_MODEL = "test-chat-model"


class WebAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.web = self.root / "web"
        self.web.mkdir()
        for name, contents in (("index.html", "<!doctype html><title>Source workbench</title>"),
                               ("app.js", "'use strict';"), ("i18n.js", "'use strict';"), ("styles.css", "body { color: black; }")):
            (self.web / name).write_text(contents, encoding="utf-8")
        self.config = CompanyAPIConfig(base_url=TEST_ENDPOINT, chat_model=TEST_MODEL, api_key=TEST_KEY)
        self.app = WorkbenchState(config_provider=lambda: self.config, demo_output_root=self.root / "demo-runs")
        self.server = create_server(0, app=self.app, web_root=self.web)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.token = self.request("GET", "/api/state")[1]["session_token"]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, method: str, path: str, payload: object = None, *, headers: dict | None = None, authenticated: bool = True) -> tuple[int, object, dict]:
        request_headers = {"Origin": self.server.origin}
        if authenticated and hasattr(self, "token"):
            request_headers["X-Session-Token"] = self.token
        if headers:
            request_headers.update(headers)
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            content = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw.decode("utf-8")
            return response.status, content, dict(response.getheaders())
        finally:
            connection.close()

    def source(self, name: str, program: str, *, suffix: str = ".cbl") -> Path:
        root = self.root / name
        root.mkdir()
        (root / ("batch" + suffix)).write_text(
            "       IDENTIFICATION DIVISION.\n"
            f"       PROGRAM-ID. {program}.\n"
            "       DATA DIVISION.\n"
            "       WORKING-STORAGE SECTION.\n"
            "       01 WS-COUNT PIC 9 VALUE 0.\n"
            "       PROCEDURE DIVISION.\n"
            "       MAIN.\n"
            "           ADD 1 TO WS-COUNT.\n"
            "           CALL 'STOCK-WRITER' USING WS-COUNT.\n"
            "           STOP RUN.\n",
            encoding="utf-8",
        )
        return root

    def submit(self, source: Path, output: Path, **options: object) -> str:
        status, result, _ = self.request("POST", "/api/analyze", {"source": str(source), "output": str(output), **options})
        self.assertEqual(status, 202, result)
        return result["job_id"]

    def finish(self, job_id: str) -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, result, _ = self.request("GET", f"/api/jobs/{job_id}")
            self.assertEqual(status, 200, result)
            if result["status"] != "RUNNING":
                return result
            threading.Event().wait(0.01)
        self.fail("Analysis job did not finish")

    def test_state_has_only_safe_configuration_metadata_and_static_routes_are_bounded(self) -> None:
        status, state, headers = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(state["api_configured"])
        self.assertIsNone(state["api_configuration_error"])
        self.assertIsNone(state["project"]["agent"])
        self.assertIsNone(state["project"]["snapshot_id"])
        for secret in (TEST_KEY, TEST_ENDPOINT, TEST_MODEL):
            self.assertNotIn(secret, json.dumps(state))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        for path in ("/", "/index.html", "/app.js", "/i18n.js", "/styles.css"):
            self.assertEqual(self.request("GET", path)[0], 200)
        (self.web / ".env").write_text("COMPANY_API_KEY=" + TEST_KEY, encoding="utf-8")
        for path in ("/../company_api.py", "/%2e%2e/company_api.py", "/web_app.py", "/api/state?token=anything",
                     "/.env", "/../.env", "/%2eenv", "/.env.example"):
            self.assertEqual(self.request("GET", path)[0], 404)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_cross_site_host_and_session_checks_apply_before_work(self) -> None:
        for headers in ({"Origin": "https://other.example.invalid"}, {"Origin": "null"},
                        {"Host": "other.example.invalid"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.request("GET", "/api/state", headers=headers)[0], 403)
        self.assertEqual(self.request("POST", "/api/analyze", {}, authenticated=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/evidence?id=ev_unknown", authenticated=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/analyze", {}, headers={"X-Session-Token": "wrong"})[0], 403)
        self.assertEqual(self.request("OPTIONS", "/api/analyze")[0], 403)
        self.assertIsNone(self.app.job)

    def test_model_check_calls_chat_once_without_source_and_returns_only_safe_status(self) -> None:
        requests = []

        def transport(request):
            requests.append(request)
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": "OK"}}]}))

        self.app.model_check_transport = transport
        status, result, _ = self.request("POST", "/api/model-check", {})
        self.assertEqual(status, 200)
        self.assertEqual(result, {"usable": True, "code": "MODEL_REPLY_RECEIVED",
                                  "http_status": 200, "model_returned": True})
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].endpoint, "chat/completions")
        self.assertEqual(requests[0].timeout_seconds, 15)
        payload = json.loads(requests[0].body)
        self.assertEqual(payload["max_tokens"], 32)
        self.assertEqual(payload["messages"], [{"role": "user", "content": "Reply with the single word OK."}])
        self.assertNotIn("source", json.dumps(payload).lower())
        for secret in (TEST_KEY, TEST_ENDPOINT, TEST_MODEL):
            self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(self.request("POST", "/api/model-check", {}, authenticated=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/model-check", {}, headers={"Origin": "https://other.example.invalid"})[0], 403)
        self.assertEqual(self.request("POST", "/api/model-check", {"prompt": "read source"})[0], 400)
        self.assertEqual(len(requests), 1)

    def test_model_check_distinguishes_auth_timeout_and_empty_reply(self) -> None:
        def response(status, body):
            self.app.model_check_transport = lambda request: TransportResponse(status, body)
            actual_status, result, _ = self.request("POST", "/api/model-check", {})
            self.assertEqual(actual_status, 200)
            return result

        for code in (401, 403):
            with self.subTest(http_status=code):
                result = response(code, "{}")
                self.assertEqual(result["code"], "HTTP_ERROR")
                self.assertEqual(result["http_status"], code)
                self.assertFalse(result["usable"])
        result = response(200, json.dumps({"choices": [{"message": {
            "role": "assistant", "content": ""}}]}))
        self.assertEqual(result["code"], "MODEL_TEXT_EMPTY")
        self.assertFalse(result["model_returned"])
        self.app.model_check_transport = lambda request: (_ for _ in ()).throw(TimeoutError())
        result = self.request("POST", "/api/model-check", {})[1]
        self.assertEqual(result["code"], "REQUEST_TIMEOUT")
        self.assertIsNone(result["http_status"])

    def test_folder_picker_returns_selected_directory_without_importing_it(self) -> None:
        folder = self.root / "selected"
        folder.mkdir()
        seen = []
        self.app.folder_picker = lambda initial: seen.append(initial) or str(folder)
        status, result, _ = self.request("POST", "/api/pick-folder", {"initial_path": str(folder)})
        self.assertEqual(status, 200)
        self.assertEqual(result, {"cancelled": False, "path": str(folder.resolve())})
        self.assertEqual(seen, [folder])
        self.assertIsNone(self.app.job)
        self.assertIsNone(self.app.project["source"])
        self.assertEqual(self.request("POST", "/api/pick-folder", {}, authenticated=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/pick-folder", {}, headers={"Origin": "https://other.example.invalid"})[0], 403)
        self.assertEqual(self.request("POST", "/api/pick-folder", {"path": str(folder)})[0], 400)
        self.app.folder_picker = lambda initial: None
        self.assertEqual(self.request("POST", "/api/pick-folder", {})[1], {"cancelled": True, "path": None})
        self.app.folder_picker = lambda initial: (_ for _ in ()).throw(RuntimeError("desktop unavailable"))
        status, result, _ = self.request("POST", "/api/pick-folder", {})
        self.assertEqual((status, result["error"]["code"]), (503, "FOLDER_PICKER_UNAVAILABLE"))

    def test_native_folder_picker_runs_a_bounded_child_process(self) -> None:
        folder = self.root / "selected"
        folder.mkdir()
        with mock.patch("web_app.subprocess.run", return_value=mock.Mock(returncode=0, stdout=json.dumps({"path": str(folder)}))) as run:
            self.assertEqual(_pick_local_folder(self.root), str(folder.resolve()))
        self.assertEqual(run.call_args.args[0][1], "-c")
        self.assertEqual(run.call_args.kwargs["timeout"], 300)
        with mock.patch("web_app.subprocess.run", return_value=mock.Mock(returncode=0, stdout='{"path": null}')):
            self.assertIsNone(_pick_local_folder(self.root))
        with mock.patch("web_app.subprocess.run", side_effect=OSError("private local path")):
            with self.assertRaises(RequestError) as raised:
                _pick_local_folder(self.root)
        self.assertEqual(raised.exception.code, "FOLDER_PICKER_UNAVAILABLE")

    def test_native_folder_picker_falls_back_to_macos_system_dialog_without_shell_interpolation(self) -> None:
        folder = self.root / "selected folder's name"
        folder.mkdir()
        unavailable_tk = mock.Mock(returncode=0, stdout='{"error":"FOLDER_PICKER_UNAVAILABLE"}')
        chosen = mock.Mock(returncode=0, stdout=str(folder) + "\n")
        with mock.patch("web_app.sys.platform", "darwin"), mock.patch(
            "web_app.subprocess.run", side_effect=[unavailable_tk, chosen]
        ) as run:
            self.assertEqual(_pick_local_folder(self.root), str(folder.resolve()))
        argv = run.call_args_list[1].args[0]
        self.assertEqual(argv[:4], ["/usr/bin/osascript", "-l", "JavaScript", "-e"])
        self.assertEqual(argv[-1], str(self.root))
        self.assertNotIn(str(self.root), argv[4])
        with mock.patch("web_app.sys.platform", "darwin"), mock.patch(
            "web_app.subprocess.run", side_effect=[unavailable_tk, mock.Mock(returncode=0, stdout="__CANCELLED__\n")]
        ):
            self.assertIsNone(_pick_local_folder(self.root))

    def test_framework_cases_read_actual_sources_and_prepare_offline(self) -> None:
        self.assertEqual(self.request("GET", "/api/framework-demo", authenticated=False)[0], 403)
        status, demo, _ = self.request("GET", "/api/framework-demo")
        self.assertEqual(status, 200, demo)
        self.assertEqual({case["id"] for case in demo["cases"]}, {"online", "client_server", "batch"})
        case = next(case for case in demo["cases"] if case["id"] == "batch")
        self.assertTrue(case["evidence"])
        self.assertTrue(any("SECTION" in item["source_text"] for item in case["evidence"]))
        with mock.patch("analyze_source.run_investigation", side_effect=AssertionError("preparation must stay offline")) as model:
            status, started, _ = self.request("POST", "/api/framework-demo/prepare", {"case_id": "batch", "locale": "en"})
            self.assertEqual(status, 202, started)
            job = self.finish(started["job_id"])
        self.assertFalse(model.called)
        self.assertEqual(job["status"], "COMPLETED", job.get("error"))
        project = job["result"]
        self.assertEqual(project["source_origin"], "synthetic_framework")
        self.assertIsNone(project["diagnosis"]["selected_entry"])
        self.assertEqual(project["diagnosis"]["scope"]["mode"], "repository_index")
        self.assertEqual(project["agent"]["runner_status"], "NOT_REQUESTED")
        question = case["question"]
        self.assertIsNone(project["diagnosis"]["question"])
        self.assertEqual(started["suggested_question"], question["en"] if isinstance(question, dict) else question)
        status, reused, _ = self.request("POST", "/api/framework-demo/prepare", {"case_id": "online"})
        self.assertEqual(status, 202)
        self.assertEqual(reused["status"], "READY")
        self.assertEqual(reused["project"]["snapshot_id"], project["snapshot_id"])
        self.assertEqual(reused["project"]["run_id"], started["job_id"])
        self.assertTrue(reused["project"]["diagnosis"]["repository_search"]["full_text_complete"])
        self.assertTrue(reused["conversation"]["id"])
        self.app.conversation_store.append(reused["conversation"]["id"], "user", "What does this branch do?", status="completed")
        self.app.conversation = self.app.conversation_store.get(reused["conversation"]["id"])
        self.app.project["run_id"] = "prior-question-run"
        status, another, _ = self.request("POST", "/api/framework-demo/prepare", {"case_id": "client_server"})
        self.assertEqual(status, 202)
        self.assertEqual(another["status"], "READY")
        self.assertEqual(another["conversation"]["id"], reused["conversation"]["id"])
        self.assertEqual(another["conversation"]["messages"][0]["content"], "What does this branch do?")
        self.assertEqual(another["project"]["run_id"], "prior-question-run")

    def test_framework_case_requests_reject_unknown_case_and_custom_paths(self) -> None:
        for payload in ({"case_id": "../outside"}, {"case_id": "unknown"},
                        {"case_id": "online", "source": "/outside"},
                        {"case_id": "online", "allow_network": True},
                        {"case_id": "online", "locale": "invalid"}):
            with self.subTest(payload=payload):
                self.assertIn(self.request("POST", "/api/framework-demo/prepare", payload)[0], {400, 404})
        self.assertEqual(self.request("POST", "/api/framework-demo/prepare", {"case_id": "online"}, authenticated=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/framework-demo?path=/outside")[0], 404)
        self.assertIsNone(self.app.job)

    def test_actual_source_builds_catalog_graph_and_verifiable_evidence_without_network(self) -> None:
        source = self.source("source", "STOCK-UPDATE")
        with mock.patch("analyze_source.run_investigation", side_effect=AssertionError("network must stay off")) as model:
            job = self.submit(source, self.root / "output", question="解释这个程序", entry="STOCK-UPDATE")
            result = self.finish(job)
        self.assertEqual(result["status"], "COMPLETED")
        project = result["result"]
        self.assertEqual(project["programs"][0]["program_name"], "STOCK-UPDATE")
        self.assertEqual(project["diagnosis"]["question_status"], "NETWORK_DISABLED")
        self.assertEqual(project["agent"]["runner_status"], "NETWORK_DISABLED")
        self.assertEqual(project["relations"]["edges"][0]["target_name"], "STOCK-WRITER")
        self.assertEqual(project["relations"]["edges"][0]["source_program"], "STOCK-UPDATE")
        self.assertFalse(model.called)
        evidence_id = project["programs"][0]["evidence_id"]
        status, evidence, _ = self.request("GET", f"/api/evidence?id={evidence_id}")
        self.assertEqual(status, 200)
        self.assertEqual(evidence["run_id"], job)
        self.assertEqual(evidence["snapshot_id"], project["snapshot_id"])
        self.assertEqual(evidence["spans"][0]["integrity"], "VALID")
        self.assertIn("PROGRAM-ID. STOCK-UPDATE", evidence["spans"][0]["source_text"])
        self.assertFalse(evidence["spans"][0]["span_truncated"])

    def test_custom_extensions_and_empty_optional_fields_are_supported(self) -> None:
        source = self.source("members", "STOCK-IMPORT", suffix=".member")
        job = self.submit(source, self.root / "output", extensions=".member", entry="", question="", encoding="", source_format="")
        result = self.finish(job)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["result"]["programs"][0]["program_name"], "STOCK-IMPORT")
        self.assertEqual(result["result"]["diagnosis"]["question_status"], "NOT_REQUESTED")
        self.assertEqual(result["result"]["diagnosis"]["source_options"]["reading_strategy"], "retrieval")
        self.assertEqual(result["result"]["diagnosis"]["source_options"]["max_source_pages"], 4)

    def test_mock_model_is_called_only_with_explicit_network_permission(self) -> None:
        source = self.source("source", "STOCK-UPDATE")

        def simulated_investigation(question, database, config, **kwargs):
            self.assertTrue(kwargs["allow_network"])
            self.assertIn("STOCK-UPDATE", kwargs["entry_program"])
            self.assertEqual(kwargs["analysis_mode"], "business")
            self.assertEqual(kwargs["source_root"], source.resolve())
            with sqlite3.connect(database) as connection:
                snapshot = connection.execute("SELECT value FROM metadata WHERE key='snapshot_id'").fetchone()[0]
            return {"runner_status": "COMPLETED", "reason_code": "AGENT_RUN_COMPLETED", "agent_result": {
                "snapshot_id": snapshot, "status": "ABSTAINED", "answer": "模拟模型没有给出业务结论。",
                "claims": [], "boundaries": [], "stop_reason": "model_abstained",
            }}

        with mock.patch("analyze_source.run_investigation", side_effect=simulated_investigation) as model:
            job = self.submit(source, self.root / "output", entry="STOCK-UPDATE", question="解释这个程序", allow_network=True)
            result = self.finish(job)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result["result"]["diagnosis"]["question_status"], "ABSTAINED")
        self.assertEqual(result["result"]["agent"]["agent_result"]["status"], "ABSTAINED")
        for secret in (TEST_KEY, TEST_ENDPOINT, TEST_MODEL):
            self.assertNotIn(secret, json.dumps(result))

    def test_refresh_clears_old_answers_and_evidence_while_only_one_job_runs(self) -> None:
        first = self.source("first", "STOCK-READ")
        second = self.source("second", "STOCK-WRITE")
        initial_id = self.submit(first, self.root / "output", entry="STOCK-READ")
        initial = self.finish(initial_id)["result"]
        old_evidence = initial["programs"][0]["evidence_id"]
        started, release = threading.Event(), threading.Event()

        def delayed_analyzer(*args, **kwargs):
            started.set()
            release.wait(3)
            return analyze_source(*args, **kwargs)

        self.app.analyzer = delayed_analyzer
        job_id = self.submit(second, self.root / "output", entry="STOCK-WRITE")
        self.assertTrue(started.wait(1))
        try:
            state = self.request("GET", "/api/state")[1]
            self.assertEqual(state["active_job_id"], job_id)
            self.assertEqual(state["project"]["source"], str(second.resolve()))
            self.assertIsNone(state["project"]["agent"])
            self.assertEqual(state["project"]["programs"], [])
            self.assertEqual(state["project"]["relations"]["edges"], [])
            self.assertEqual(self.request("GET", f"/api/evidence?id={old_evidence}")[0], 409)
            self.assertEqual(self.request("GET", f"/api/jobs/{initial_id}")[0], 404)
            self.assertEqual(self.request("POST", "/api/analyze", {"source": str(first), "output": str(self.root / "another")})[0], 409)
        finally:
            release.set()
        result = self.finish(job_id)
        self.assertEqual(result["result"]["programs"][0]["program_name"], "STOCK-WRITE")
        self.assertEqual(self.request("GET", f"/api/evidence?id={old_evidence}")[0], 404)

    def test_conversation_and_framework_routes_use_local_state_and_session_guard(self) -> None:
        source = self.source("source", "REQUEST-REVIEW")
        self.finish(self.submit(source, self.root / "output"))
        status, value, _ = self.request("POST", "/api/conversations", {})
        self.assertEqual(status, 201)
        identifier = value["conversation"]["id"]
        self.assertEqual(value["conversation"]["messages"], [])
        status, loaded, _ = self.request("GET", f"/api/conversations/{identifier}")
        self.assertEqual(status, 200)
        self.assertEqual(loaded["conversation"]["id"], identifier)
        self.assertEqual(self.request("GET", f"/api/conversations/{identifier}", authenticated=False)[0], 403)
        folder = self.root / "manuals"
        folder.mkdir()
        (folder / "guide.md").write_text("# Request processing\nRequests progress through validation.\n", encoding="utf-8")
        status, value, _ = self.request("POST", "/api/framework", {"path": str(folder)})
        self.assertEqual(status, 200)
        self.assertEqual(value["framework_knowledge"]["loaded_document_count"], 1)
        self.assertEqual(value["framework_knowledge"]["configured_path"], str(folder))
        self.assertEqual(self.request("POST", "/api/framework", {"path": str(folder)}, authenticated=False)[0], 403)

    def test_failed_refresh_cannot_expose_preserved_old_sqlite(self) -> None:
        source = self.source("source", "STOCK-READ")
        initial_id = self.submit(source, self.root / "output", entry="STOCK-READ")
        initial = self.finish(initial_id)["result"]
        evidence_id = initial["programs"][0]["evidence_id"]
        empty = self.root / "empty"
        empty.mkdir()
        result = self.finish(self.submit(empty, self.root / "output"))
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["result"]["diagnosis"]["runner_status"], "BLOCKED")
        self.assertIsNone(result["result"]["snapshot_id"])
        self.assertEqual(result["result"]["programs"], [])
        self.assertEqual(result["result"]["relations"]["edges"], [])
        self.assertIsNone(result["result"]["agent"]["agent_result"])
        self.assertEqual(self.request("GET", f"/api/evidence?id={evidence_id}")[0], 409)

    def test_invalid_new_source_submission_clears_previous_browser_authority(self) -> None:
        source = self.source("source", "STOCK-READ")
        old_job = self.submit(source, self.root / "output", entry="STOCK-READ")
        old = self.finish(old_job)["result"]
        evidence_id = old["programs"][0]["evidence_id"]
        status, _, _ = self.request("POST", "/api/analyze", {
            "source": str(self.root / "missing-source"), "output": str(self.root / "output"),
        })
        self.assertEqual(status, 400)
        current = self.request("GET", "/api/state")[1]["project"]
        self.assertIsNone(current["snapshot_id"])
        self.assertIsNone(current["agent"])
        self.assertEqual(current["programs"], [])
        self.assertEqual(current["relations"]["edges"], [])
        self.assertEqual(self.request("GET", f"/api/jobs/{old_job}")[0], 404)
        self.assertEqual(self.request("GET", f"/api/evidence?id={evidence_id}")[0], 409)

    def test_unsafe_output_paths_and_browser_credentials_are_rejected(self) -> None:
        source = self.source("source", "STOCK-READ")
        unrelated = self.root / "documents"
        unrelated.mkdir()
        (unrelated / "keep.txt").write_text("keep", encoding="utf-8")
        linked = self.root / "linked-output"
        linked.symlink_to(unrelated, target_is_directory=True)
        for output in (source, source / "inside", source.parent, unrelated, linked, Path("relative/output")):
            status, result, _ = self.request("POST", "/api/analyze", {"source": str(source), "output": str(output)})
            self.assertEqual(status, 400, result)
        status, _, _ = self.request("POST", "/api/analyze", {"source": str(source), "output": str(self.root / "safe"), "api_key": TEST_KEY})
        self.assertEqual(status, 400)
        self.assertEqual((unrelated / "keep.txt").read_text(), "keep")
        self.assertIsNone(self.app.job)

    def test_unexpected_adapter_error_is_redacted_and_keeps_no_answer(self) -> None:
        source = self.source("source", "STOCK-READ")
        self.app.analyzer = mock.Mock(side_effect=RuntimeError(TEST_KEY + " " + TEST_ENDPOINT))
        result = self.finish(self.submit(source, self.root / "output"))
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "ANALYSIS_FAILED")
        for secret in (TEST_KEY, TEST_ENDPOINT):
            self.assertNotIn(secret, json.dumps(result))
        self.assertIsNone(self.request("GET", "/api/state")[1]["project"]["agent"])


if __name__ == "__main__":
    unittest.main()
