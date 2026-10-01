from __future__ import annotations

import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from analyze_source import analyze_source
from company_api import CompanyAPIConfig
from conversation_store import ConversationStore
from test_run_agent import AgentReadyTransport, response
from web_app import WorkbenchState, create_server


class PlainAnswerTransport(AgentReadyTransport):
    def __init__(self, on_answer=None):
        super().__init__()
        self.on_answer = on_answer

    def __call__(self, request):
        payload = json.loads(request.body or b"{}")
        if any(item.get("role") == "system" for item in payload.get("messages", [])):
            self.requests.append(request)
            if self.on_answer:
                self.on_answer()
            return response(200, {"choices": [{"message": {
                "role": "assistant", "content": "The visible program increments the counter. <script>untrusted</script>",
            }, "finish_reason": "stop"}]})
        return super().__call__(request)


class WebAPIDiagnosticsTests(unittest.TestCase):
    def test_delete_conversation_requires_session_and_preserves_other_conversations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            app = WorkbenchState()
            app.conversation_store = ConversationStore(root / "output", source)
            server = create_server(0, app=app)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def request(method, path, *, token=None, payload=None):
                headers = {"Origin": server.origin}
                if token is not None:
                    headers["X-Session-Token"] = token
                body = None
                if payload is not None:
                    body = json.dumps(payload).encode("utf-8")
                    headers["Content-Type"] = "application/json"
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                try:
                    connection.request(method, path, body=body, headers=headers)
                    result = connection.getresponse()
                    return result.status, json.loads(result.read())
                finally:
                    connection.close()

            try:
                token = request("GET", "/api/state")[1]["session_token"]
                status, first = request("POST", "/api/conversations", token=token, payload={})
                self.assertEqual(status, 201)
                status, second = request("POST", "/api/conversations", token=token, payload={})
                self.assertEqual(status, 201)
                first_id = first["conversation"]["id"]
                second_id = second["conversation"]["id"]

                self.assertEqual(request("DELETE", f"/api/conversations/{first_id}")[0], 403)
                self.assertEqual(request("GET", f"/api/conversations/{first_id}", token=token)[0], 200)

                status, deleted = request("DELETE", f"/api/conversations/{first_id}", token=token)
                self.assertEqual(status, 200)
                self.assertEqual([item["id"] for item in deleted["conversations"]], [second_id])
                self.assertEqual(request("GET", f"/api/conversations/{first_id}", token=token)[0], 404)
                self.assertEqual(request("GET", f"/api/conversations/{second_id}", token=token)[0], 200)
                self.assertEqual(request("DELETE", f"/api/conversations/{'0' * 32}", token=token)[0], 404)
                app.project["agent"] = {"agent_result": {"answer": "Earlier answer"}}
                app.project["conversation"] = second["conversation"]
                app.job = {"status": "COMPLETED", "result": {"conversation": second["conversation"]}}
                status, deleted = request("DELETE", f"/api/conversations/{second_id}", token=token)
                self.assertEqual(status, 200)
                self.assertEqual(deleted["conversations"], [])
                self.assertIsNone(app.conversation)
                self.assertIsNone(app.project["agent"])
                self.assertNotIn("conversation", app.project)
                self.assertIsNone(app.job)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_web_job_preserves_plain_answer_in_page_state_and_report(self):
        self.exercise_run()

    def test_live_source_change_preserves_answer_bound_to_sent_excerpt(self):
        self.exercise_run(change_source=True)

    def test_multi_program_business_question_runs_without_an_entry_and_keeps_missing_implementation(self):
        self.exercise_run(repository=True)

    def exercise_run(self, change_source=False, repository=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "entry.cbl").write_text(
                "IDENTIFICATION DIVISION.\nPROGRAM-ID. COUNTER-ENTRY.\n"
                "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 WS-COUNT PIC 9.\n"
                "PROCEDURE DIVISION.\nADD 1 TO WS-COUNT.\nGOBACK.\n",
                encoding="utf-8",
            )
            if repository:
                (source / "counter-output.cbl").write_text(
                    "IDENTIFICATION DIVISION.\nPROGRAM-ID. COUNTER-OUTPUT.\n"
                    "PROCEDURE DIVISION.\nCALL 'EXTERNAL-OUTPUT'.\nGOBACK.\n", encoding="utf-8")
            config = CompanyAPIConfig(base_url="https://gateway.example.invalid/v1",
                                      chat_model="test-model", api_key="diagnostic-test-key")
            transport = PlainAnswerTransport(on_answer=(lambda: (source / "entry.cbl").write_text(
                "IDENTIFICATION DIVISION.\nPROGRAM-ID. CHANGED-ENTRY.\nPROCEDURE DIVISION.\nGOBACK.\n"
            )) if change_source else None)

            def analyzer(source, output, **options):
                return analyze_source(source, output, transport=transport, **options)

            app = WorkbenchState(analyzer=analyzer, config_provider=lambda: config)
            options = {"source": str(source), "output": str(root / "output"),
                       "entry": "entry.cbl", "question": "Explain the counter.", "allow_network": True,
                       "capture_api_responses": True}
            if repository:
                options.pop("entry")
                options["question"] = "Explain how the counter result is made available across this repository."
            job_id = app.start(options)["job_id"]
            job = self.finish(app, job_id)
            self.assertEqual(job["status"], "COMPLETED", job.get("error"))
            agent = job["result"]["agent"]
            if change_source:
                self.assertEqual(agent["runner_status"], "COMPLETED")
                self.assertTrue(agent["agent_result"]["model_answer_recorded"])
                self.assertIn("increments the counter", agent["agent_result"]["answer"])
                self.assertTrue(agent["agent_result"]["evidence_refs"])
            else:
                self.assertEqual(agent["runner_status"], "COMPLETED")
                self.assertIn("increments the counter", agent["agent_result"]["narrative"]["text"])
                self.assertFalse(agent["agent_result"]["claims_semantically_verified"])
                self.assertTrue(all(claim["verification"] == "unverified"
                                    for claim in agent["agent_result"]["claims"]))
            exchanges = agent["api_diagnostics"]["exchanges"]
            actual = [item for item in exchanges if item["phase"] == "investigation"]
            if repository:
                self.assertEqual(len(actual), 1)
                project = job["result"]
                self.assertIsNone(project["diagnosis"]["entry_requested"])
                self.assertIsNone(project["diagnosis"]["selected_entry"])
                self.assertEqual(project["diagnosis"]["question"], options["question"])
                self.assertEqual(agent["agent_result"]["investigation"]["mode"], "retrieval")
                self.assertEqual(agent["agent_result"]["investigation"]["repository_file_count"], 2)
                self.assertTrue(project["diagnosis"]["unresolved_dependencies"])
            else:
                self.assertEqual(len(actual), 1)
            self.assertEqual(actual[0]["http_status"], 200)
            self.assertIn("increments the counter", actual[0]["body_text"])
            self.assertEqual(app.state()["project"]["agent"], agent)
            saved = json.loads((root / "output" / "agent-result.json").read_text(encoding="utf-8"))
            self.assertEqual(saved, agent)
            for secret in (config.api_key, config.base_url, config.chat_model):
                self.assertNotIn(secret, json.dumps(agent))

            # A later offline run cannot inherit the preceding response transcript.
            count = len(transport.requests)
            options["allow_network"] = False
            options["question"] = "A new offline question."
            second = self.finish(app, app.start(options)["job_id"])
            self.assertEqual(second["result"]["agent"]["runner_status"], "NETWORK_DISABLED")
            self.assertNotIn("api_diagnostics", second["result"]["agent"])
            self.assertEqual(len(transport.requests), count)
            self.assertNotIn("increments the counter", (root / "output" / "agent-result.json").read_text())

    def finish(self, app, job_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = app.get_job(job_id)
            if job["status"] != "RUNNING":
                return job
            threading.Event().wait(0.01)
        self.fail("Analysis did not finish within its test deadline")


if __name__ == "__main__":
    unittest.main()
