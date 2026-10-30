"""Saved system instructions survive restart and reach an actual analysis request."""

import http.client
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyze_source import analyze_source
from answer_markdown import DEFAULT_ANSWER_INSTRUCTIONS
from company_api import CompanyAPIConfig, TransportResponse
from web_app import RequestError, WorkbenchState, create_server


class WorkspaceSettingsPersistenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "workspace.json"

    def test_concurrent_instances_publish_their_own_complete_settings(self):
        apps = [WorkbenchState(state_path=self.path) for _ in range(2)]
        pending = threading.Barrier(2)
        original_replace = Path.replace
        published = []
        publication_lock = threading.Lock()

        def replace(path, target):
            if target != self.path:
                return original_replace(path, target)
            pending.wait(timeout=10)
            with publication_lock:
                settings = json.loads(path.read_text(encoding="utf-8"))
                result = original_replace(path, target)
                published.append(settings["answer_instructions"])
                return result

        def save(index):
            requirement = f"Explain business result {index}."
            return apps[index].configure_answer_instructions({"answer_instructions": requirement})

        with patch.object(Path, "replace", replace), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, range(2)))
        self.assertEqual([result["answer_instructions"] for result in results],
                         [app.answer_instructions for app in apps])
        self.assertCountEqual(published, [app.answer_instructions for app in apps])
        self.assertEqual(WorkbenchState(state_path=self.path).answer_instructions, published[-1])
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_failed_publication_preserves_previous_settings_and_removes_temporary_file(self):
        app = WorkbenchState(state_path=self.path)
        app.configure_answer_instructions({"answer_instructions": "Keep the original requirements."})
        original = self.path.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("cannot replace settings")):
            with self.assertRaises(RequestError) as raised:
                app.configure_answer_instructions({"answer_instructions": "Apply revised requirements."})
        self.assertEqual(raised.exception.code, "REQUEST_FAILED")
        self.assertEqual(app.answer_instructions, "Keep the original requirements.")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(WorkbenchState(state_path=self.path).answer_instructions, app.answer_instructions)
        self.assertEqual(list(self.root.iterdir()), [self.path])


class WebAnswerInstructionsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "workspace.json"
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only")
        self.app = WorkbenchState(config_provider=lambda: self.config, state_path=self.path)
        self.server = create_server(0, app=self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.instructions = '  用「业务目的」和「处理结果」两栏说明。\n保留 "引号"、空行和格式。\n\n'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, method, path, payload=None, authenticated=True):
        headers = {"Origin": self.server.origin}
        if authenticated:
            headers["X-Session-Token"] = self.server.app.session_token
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def save(self, text):
        status, result = self.request("POST", "/api/answer-instructions", {"answer_instructions": text})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["answer_instructions"], text)

    def test_save_before_import_preserves_original_text_and_survives_restart(self):
        self.save(self.instructions)
        restarted = WorkbenchState(config_provider=lambda: self.config, state_path=self.path)
        self.assertEqual(restarted.answer_instructions, self.instructions)
        self.assertEqual(restarted.state()["default_answer_instructions"], DEFAULT_ANSWER_INSTRUCTIONS)
        self.server.app = restarted
        self.assertEqual(self.request("GET", "/api/state")[1]["answer_instructions"], self.instructions)
        self.save("")
        self.assertEqual(WorkbenchState(state_path=self.path).answer_instructions, "")

    def test_old_settings_and_unrestricted_writing_formats_work(self):
        self.path.write_text(json.dumps({"source": "", "output": "", "answer_detail": "brief"}), encoding="utf-8")
        self.assertEqual(WorkbenchState(state_path=self.path).answer_instructions, "")
        for text in ("只用一段英文，不要标题。", "\n\t", "Custom format:\n" + "business explanation " * 500):
            self.save(text)
        self.assertIsNone(self.app.job)

    def test_failed_save_preserves_previous_active_and_persisted_requirements(self):
        self.save(self.instructions)
        with patch.object(self.app, "_save_workspace", side_effect=OSError("cannot write")):
            status, _ = self.request("POST", "/api/answer-instructions", {"answer_instructions": "replacement"})
        self.assertEqual(status, 500)
        self.assertEqual(self.app.answer_instructions, self.instructions)
        self.assertEqual(WorkbenchState(state_path=self.path).answer_instructions, self.instructions)

    def test_route_uses_existing_session_guard_and_only_checks_input_type(self):
        status, _ = self.request("POST", "/api/answer-instructions", {"answer_instructions": "draft"}, authenticated=False)
        self.assertEqual(status, 403)
        for payload in ({}, {"answer_instructions": None}, {"answer_instructions": []}):
            self.assertEqual(self.request("POST", "/api/answer-instructions", payload)[0], 400)
        self.assertEqual(self.app.answer_instructions, "")

    def test_saved_requirements_reach_model_and_edit_during_request_applies_next_time(self):
        source = self.root / "source"
        source.mkdir()
        (source / "decision.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. DECISION.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 RESULT-CODE PIC 99.\nPROCEDURE DIVISION.\n"
            "MOVE 12 TO RESULT-CODE.\nGOBACK.\n", encoding="utf-8")
        requests = []
        entered = threading.Event()
        proceed = threading.Event()

        def transport(request):
            envelope = json.loads(request.body)
            requests.append(envelope)
            entered.set()
            if not proceed.wait(10):
                raise RuntimeError("test request was not released")
            payload = json.loads(envelope["messages"][-1]["content"])
            page = next(page for bundle in payload["source_context"] for page in bundle["pages"]
                        if page["relative_path"] == "decision.cbl")
            answer = f"程序给调用方返回结果码12。[{page['evidence_id']}]"
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": answer}, "finish_reason": "stop"}]}))

        self.app.analyzer = lambda *args, **kwargs: analyze_source(*args, transport=transport, **kwargs)
        self.save(self.instructions)
        status, job = self.request("POST", "/api/analyze", {
            "source": str(source), "output": str(self.root / "output"), "source_format": "free",
            "question": "DECISION 返回什么？", "reading_strategy": "retrieval", "allow_network": True})
        self.assertEqual(status, 202, job)
        try:
            self.assertTrue(entered.wait(10))
            self.save("只写一段业务解释。")
        finally:
            proceed.set()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            _, final = self.request("GET", "/api/jobs/" + job["job_id"])
            if final["status"] != "RUNNING":
                break
            threading.Event().wait(0.01)
        self.assertEqual(final["status"], "COMPLETED", final)
        self.assertEqual(len(requests), 1)
        self.assertIn(self.instructions, requests[0]["messages"][0]["content"])
        self.assertNotIn("只写一段业务解释。", requests[0]["messages"][0]["content"])
        self.assertEqual(WorkbenchState(config_provider=lambda: self.config, state_path=self.path).answer_instructions,
                         "只写一段业务解释。")


if __name__ == "__main__":
    unittest.main()
