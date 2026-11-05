"""Exercise lost-receipt recovery through the ordinary workbench submission."""
from concurrent.futures import ThreadPoolExecutor
import copy
import http.client
import json
import threading
import unittest
from unittest.mock import patch

import test_conversation_workbench as workbench_fixtures
from web_app import RequestError, WorkbenchState, create_server


class QuestionSubmissionReceiptTests(unittest.TestCase):
    def setUp(self):
        self.fixture = workbench_fixtures.ConversationWorkbenchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.run_job()
        self.app = self.fixture.app
        self.payload = {
            "source": str(self.fixture.source), "output": str(self.fixture.output),
            "source_format": "free", "question": "How is CAPACITY checked?",
            "allow_network": True, "submission_id": "a" * 32,
        }

    def test_concurrent_and_completed_replays_keep_one_turn_and_one_request(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.app.start(self.payload), range(2)))
        self.assertEqual(responses[0]["job_id"], responses[1]["job_id"])
        self.fixture.wait_for_job(responses[0]["job_id"])
        saved = copy.deepcopy(self.app.conversation)
        repeated = self.app.start(self.payload)
        self.assertEqual(repeated["job_id"], responses[0]["job_id"])
        self.assertEqual(repeated["status"], "COMPLETED")
        self.assertEqual(len(self.fixture.calls), 1)
        self.assertEqual(self.app.conversation, saved)
        self.assertEqual(len(saved["messages"]), 2)
        self.assertEqual(self.app.state()["submission_protocol"], "session-receipt/v1")

    def test_old_receipt_after_another_job_does_not_replace_current_work(self):
        first = self.app.start(self.payload)
        self.fixture.wait_for_job(first["job_id"])
        original_conversation = self.app.conversation["id"]
        next_payload = {**self.payload, "submission_id": "b" * 32, "question": "Explain RESERVED-COUNT."}
        second = self.app.start(next_payload)
        self.fixture.wait_for_job(second["job_id"])
        current = copy.deepcopy(self.app.project)
        recovered = self.app.start(self.payload)
        self.assertEqual(recovered["job_id"], first["job_id"])
        self.assertTrue(recovered["job_replaced"])
        self.assertEqual(recovered["conversation"]["id"], original_conversation)
        self.assertEqual(self.app.project, current)
        self.assertEqual(self.app.job["job_id"], second["job_id"])
        self.assertEqual(len(self.fixture.calls), 2)

    def test_changed_payload_cannot_reuse_an_accepted_submission(self):
        first = self.app.start(self.payload)
        self.fixture.wait_for_job(first["job_id"])
        with self.assertRaises(RequestError) as error:
            self.app.start({**self.payload, "question": "Explain RESERVED-COUNT."})
        self.assertEqual(error.exception.code, "SUBMISSION_CHANGED")
        self.assertEqual(len(self.fixture.calls), 1)
        self.assertEqual(len(self.app.conversation["messages"]), 2)

    def test_failure_after_start_does_not_authorize_resubmission(self):
        original = self.app._start
        def lose_response(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("Local response unavailable")
        with patch.object(self.app, "_start", side_effect=lose_response):
            with self.assertRaises(OSError):
                self.app.start(self.payload)
        self.fixture.wait_for_job(self.app.job["job_id"])
        with self.assertRaises(RequestError) as error:
            self.app.start(self.payload)
        self.assertEqual(error.exception.code, "SUBMISSION_UNCONFIRMED")
        self.assertEqual(len(self.fixture.calls), 1)
        self.assertEqual(len(self.app.conversation["messages"]), 2)

    def test_service_restart_rejects_original_http_session_before_any_request(self):
        first = self.app.start(self.payload)
        self.fixture.wait_for_job(first["job_id"])
        original_token = self.app.session_token
        restarted = self.fixture.workers.track(WorkbenchState(analyzer=self.fixture.analyzer,
            config_provider=lambda: self.fixture.config, state_path=self.fixture.state_path))
        server = create_server(0, app=restarted)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("POST", "/api/analyze", json.dumps(self.payload), {
                    "Origin": server.origin, "Content-Type": "application/json", "X-Session-Token": original_token})
                response = connection.getresponse()
                body = json.loads(response.read())
                self.assertEqual(response.status, 403)
                self.assertEqual(body["error"]["code"], "INVALID_SESSION")
            finally:
                connection.close()
            self.assertEqual(len(self.fixture.calls), 1)
            self.assertEqual(len(restarted.state()["conversation"]["messages"]), 2)
            self.assertIsNone(restarted.job)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
