from __future__ import annotations

from copy import deepcopy
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from online_http_fixture import CONFIG, OnlineFixture
from web_app import WorkbenchState, create_server


class OnlineWebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = OnlineFixture(self.root)
        self.app = WorkbenchState(config_provider=lambda: CONFIG, state_path=self.root / "profile" / "workbench-state.json",
                                  demo_output_root=self.root / "demo")
        self.original_project = deepcopy(self.app.project)
        self.server = create_server(0, app=self.app)
        self.server.online_app = self.fixture.controller()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temporary.cleanup()

    def request(self, method, target, payload=None, headers=None, authenticated=True):
        values = {"Origin": self.server.origin}
        if authenticated:
            values["X-Session-Token"] = self.app.session_token
        if headers:
            values.update(headers)
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            values["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.request(method, target, body=body, headers=values)
            response = connection.getresponse()
            raw = response.read()
            data = json.loads(raw) if "application/json" in response.getheader("Content-Type", "") else raw.decode()
            return response.status, data
        finally:
            connection.close()

    def finish(self, identifier):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status, response = self.request("GET", "/api/online/jobs/" + identifier)
            self.assertEqual(status, 200, response)
            if response["job"]["status"] != "running":
                return response["job"]
            threading.Event().wait(.01)
        self.fail("Online job timed out")

    def connect(self, label="v1", *, updating=False):
        payload = self.fixture.connect_payload(label)
        if updating:
            payload["workspace_id"] = self.request("GET", "/api/online/state")[1]["current_workspace_id"]
        status, result = self.request("POST", "/api/online/update" if updating else "/api/online/connect", payload)
        self.assertEqual(status, 202, result)
        job = self.finish(result["job_id"])
        self.assertEqual(job["status"], "completed", job)
        return job["workspace_id"]

    def ask(self, workspace):
        status, result = self.request("POST", "/api/online/conversations", {"workspace_id": workspace})
        self.assertEqual(status, 201, result)
        conversation = result["conversation"]["id"]
        status, result = self.request("POST", "/api/online/question", {"conversation_id": conversation,
                                      "question": "输入金额满足什么条件才会处理，异常分支是什么？",
                                      "allow_source_transfer": True})
        self.assertEqual(status, 202, result)
        job = self.finish(result["job_id"])
        self.assertEqual(job["status"], "answered", job)
        return conversation

    def test_business_question_citations_export_and_restart_without_producer_sources(self):
        workspace = self.connect()
        conversation = self.ask(workspace)
        status, state = self.request("GET", "/api/online/state")
        self.assertEqual(status, 200)
        answer = state["conversation"]["messages"][-1]
        self.assertIn("大于 100", answer["content"])
        self.assertEqual(len(answer["citations"]), 3)
        citation = answer["citations"][0]
        status, evidence = self.request("GET", f"/api/online/evidence?conversation_id={conversation}&citation_id={citation['key']}")
        self.assertEqual(status, 200, evidence)
        self.assertEqual(evidence["citation"]["commit"], "a" * 40)
        self.assertTrue(evidence["text"])
        for format in ("json", "markdown"):
            status, export = self.request("GET", f"/api/online/export?conversation_id={conversation}&format={format}")
            self.assertEqual(status, 200, export)
            self.assertIn("100", export["content"])
            self.assertNotIn("synthetic-material-secret", export["content"])
        self.assertEqual({path for _, path in self.fixture.source_reads}, {"main.cbl", "worker.cbl", "records.cpy"})
        self.assertFalse(list(self.root.glob("producer-*")))
        self.assertFalse(list(self.root.glob("*.sqlite")))
        self.assertEqual(self.app.project, self.original_project)
        self.assertFalse(self.app.state_path.exists())
        for path in (self.root / "profile" / "online").rglob("*"):
            if path.is_file():
                self.assertNotIn(b"synthetic-material-secret", path.read_bytes())
                self.assertNotIn(b"synthetic-source-secret", path.read_bytes())
        self.server.online_app.close()
        self.server.online_app = self.fixture.controller()
        state = self.request("GET", "/api/online/state")[1]
        self.assertEqual(state["conversation"]["id"], conversation)
        self.assertFalse(state["workspaces"][0]["connected"])
        self.assertTrue(state["conversation"]["messages"][-1]["content"])
        status, result = self.request("POST", "/api/online/question", {"conversation_id": conversation,
                                     "question": "继续解释", "allow_source_transfer": True})
        self.assertEqual(status, 409, result)

    def test_weekly_version_adoption_preserves_old_conversation_and_failed_update(self):
        old = self.connect()
        conversation = self.ask(old)
        self.fixture.add_revision("b" * 40, "v2")
        new = self.connect("v2", updating=True)
        self.assertNotEqual(old, new)
        state = self.request("GET", "/api/online/state")[1]
        self.assertIsNone(state["conversation"])
        self.assertEqual(len(state["workspaces"]), 2)
        payload = self.fixture.connect_payload("v2")
        payload.update(workspace_id=new, commit="c" * 40)
        status, response = self.request("POST", "/api/online/update", payload)
        self.assertEqual(status, 202)
        job = self.finish(response["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.request("GET", "/api/online/state")[1]["current_workspace_id"], new)
        status, state = self.request("POST", "/api/online/open", {"conversation_id": conversation})
        self.assertEqual(status, 200, state)
        self.assertEqual(state["current_workspace_id"], old)
        self.assertEqual(state["conversation"]["messages"][-1]["citations"][0]["commit"], "a" * 40)

    def test_followup_reuses_source_locations_but_rechecks_original_bytes(self):
        workspace = self.connect()
        conversation = self.ask(workspace)
        before = len(self.fixture.source_reads)
        status, result = self.request("POST", "/api/online/question", {"conversation_id": conversation,
                                     "question": "再解释这个分支", "allow_source_transfer": True})
        self.assertEqual(status, 202, result)
        job = self.finish(result["job_id"])
        self.assertEqual(job["status"], "answered", job)
        client = self.fixture.models[-1]
        self.assertTrue(client.requests[0]["history"])
        hints = [hint for message in client.requests[0]["history"] for hint in message.get("citation_hints", [])]
        self.assertEqual({hint["path"] for hint in hints}, {"main.cbl", "worker.cbl", "records.cpy"})
        self.assertGreater(len(self.fixture.source_reads), before)
        self.assertEqual(len(client.requests), 2)

    def test_original_authentication_guards_and_explicit_source_consent_apply(self):
        for method, path, body in (("GET", "/api/online/state", None),
                                   ("POST", "/api/online/connect", self.fixture.connect_payload()),
                                   ("DELETE", "/api/online/conversations/" + "a" * 32, None)):
            self.assertEqual(self.request(method, path, body, authenticated=False)[0], 403)
            self.assertEqual(self.request(method, path, body, headers={"Origin": "https://outside.invalid"})[0], 403)
        self.assertEqual(self.request("GET", "/api/online/state?x=1&x=2")[0], 400)
        self.assertEqual(self.request("POST", "/api/online/connect?x=1", {})[0], 400)
        self.assertFalse(self.fixture.models)
        workspace = self.connect()
        _, response = self.request("POST", "/api/online/conversations", {"workspace_id": workspace})
        status, response = self.request("POST", "/api/online/question", {"conversation_id": response["conversation"]["id"], "question": "分析"})
        self.assertEqual(status, 403, response)
        self.assertFalse(self.fixture.models)

    def test_online_page_has_separate_assets_and_original_page_only_adds_entry(self):
        for path in ("/online", "/online.html", "/online.js", "/online.css"):
            status, _ = self.request("GET", path, authenticated=False)
            self.assertEqual(status, 200, path)
        _, content = self.request("GET", "/")
        self.assertIn('href="/online"', content)
        self.assertIn('id="connect-remote-button"', content)
        self.assertIsNone(self.app.job)


if __name__ == "__main__":
    unittest.main()
