"""Recovery diagnostics do not multiply a previously delivered answer."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_online_workspace as workspace_fixture
from company_api import APIClientError
from online_investigation import run_investigation


class ScriptedClient:
    def __init__(self, replies, *, outcome="received"):
        self.replies = iter(replies)
        self.last_request_outcome = outcome
        self.calls = 0

    def complete(self, *, messages, check_cancel):
        check_cancel()
        self.calls += 1
        value = next(self.replies)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value(json.loads(messages[-1]["content"]))
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]}


def partial_answer(context):
    identifiers = [item["id"] for item in context["source_context"]]
    return {"answer": "已确认当前状态的处理。 " + " ".join("[" + item + "]" for item in identifiers),
            "citations": identifiers, "complete": False, "remaining": ["继续核对子程序分支"]}


class OnlineRecoveryAnswerHistoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = workspace_fixture.OnlineWorkspaceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.results = []
        self.client = ScriptedClient([
            {"actions": [{"tool": "read", "path": "main.cbl", "start_line": 4, "end_line": 5}]},
            partial_answer,
        ])

        def runner(query, source, question, **options):
            result = run_investigation(query, source, question, client=self.client, **options)
            self.results.append(deepcopy(result))
            return result

        self.fixture.app.investigator = runner
        connected = self.fixture.connect()
        self.conversation = self.fixture.conversation(connected["workspace_id"])
        job = self.fixture.ask(self.conversation["id"])
        self.assertEqual(job["status"], "needs_continuation")
        self.initial_result = deepcopy(self.results[-1])
        self.initial_messages = self.fixture.app.state()["conversation"]["messages"]
        self.assertEqual(len(self.initial_messages), 2)

    def resume(self, *, approve_retry=False):
        job = self.fixture.post("resume", {"conversation_id": self.conversation["id"],
            "allow_source_transfer": True, "approve_retry": approve_retry}, 202)
        return self.fixture.wait(job["job_id"])

    def return_result(self, result):
        self.fixture.app.investigator = lambda *args, **options: deepcopy(result)

    def test_real_runner_repeated_failures_and_unknown_outcomes_keep_one_partial_answer(self):
        for _ in range(3):
            self.client = ScriptedClient([APIClientError("HTTP_ERROR")])
            job = self.resume()
            self.assertEqual(job["status"], "failed")
            self.assertEqual(self.client.calls, 1)
            self.assertEqual(self.results[-1]["answer"], self.initial_result["answer"])
            saved = self.fixture.app.state()["conversation"]
            self.assertEqual(len(saved["messages"]), 2)
            self.assertEqual(saved["messages"][-1], self.initial_messages[-1])
            self.assertEqual(saved["pending"]["status"], "failed")
        self.client = ScriptedClient([APIClientError("REQUEST_TIMEOUT")], outcome="unknown")
        self.assertEqual(self.resume()["status"], "retry_confirmation_required")
        self.assertEqual(self.resume()["status"], "retry_confirmation_required")
        self.assertEqual(self.client.calls, 1, "unknown outcomes must not be automatically retried")
        saved = self.fixture.app.state()["conversation"]
        self.assertEqual(len(saved["messages"]), 2)
        self.assertTrue(saved["pending"]["retry_confirmation_required"])
        old_key = self.initial_messages[-1]["citations"][0]["key"]
        status, evidence = self.fixture.app.dispatch("GET", "/api/online/evidence", query={
            "conversation_id": self.conversation["id"], "citation_id": old_key})
        self.assertEqual(status, 200, evidence)
        self.assertIn("STATUS-READY", evidence["text"])
        def complete(context):
            return {**partial_answer(context), "complete": True, "remaining": []}
        self.client = ScriptedClient([complete])
        self.assertEqual(self.resume(approve_retry=True)["status"], "answered")
        self.assertIsNone(self.fixture.app.state()["conversation"]["pending"])
        self.assertEqual(self.client.calls, 1)

    def test_cancelled_recovery_preserves_original_answer_without_another_copy(self):
        retained = deepcopy(self.initial_result)
        retained.update(status="cancelled", error="ONLINE_CANCELLED")
        self.return_result(retained)
        for _ in range(3):
            self.assertEqual(self.resume()["status"], "cancelled")
            saved = self.fixture.app.state()["conversation"]
            self.assertEqual(len(saved["messages"]), 2)
            self.assertEqual(saved["messages"][-1], self.initial_messages[-1])
            self.assertEqual(saved["status"], "cancelled")
            self.assertTrue(saved["pending"]["can_resume"])

    def test_actual_completion_with_identical_text_is_a_new_answer(self):
        def complete(context):
            result = partial_answer(context)
            result.update(complete=True, remaining=[])
            return result
        self.client = ScriptedClient([complete])
        self.assertEqual(self.resume()["status"], "answered")
        saved = self.fixture.app.state()["conversation"]
        self.assertEqual(len(saved["messages"]), 3)
        self.assertEqual(saved["messages"][-1]["content"], self.initial_messages[-1]["content"])
        self.assertEqual(saved["messages"][-1]["status"], "answered")
        self.assertIsNone(saved["pending"])

    def test_new_partial_delivery_is_preserved_even_with_identical_text(self):
        retained = deepcopy(self.initial_result)
        retained["remaining"] = ["继续核对新的业务分支"]
        self.return_result(retained)
        self.assertEqual(self.resume()["status"], "needs_continuation")
        self.assertEqual(len(self.fixture.app.state()["conversation"]["messages"]), 3)

    def test_changed_answer_or_citations_or_evidence_or_coverage_is_not_discarded(self):
        changes = {
            "answer": lambda result: result.update(answer=result["answer"] + " 新增核对结论。"),
            "citations": lambda result: result["citations"].append({**result["citations"][0], "id": "E" + "2" * 24, "start": 1}),
            "evidence": lambda result: result["evidence"].append({"id": "E" + "3" * 24, "text": "new observed evidence"}),
            "coverage": lambda result: result["coverage"].update(boundary_count=999),
        }
        for index, (kind, change) in enumerate(changes.items(), 3):
            with self.subTest(kind=kind):
                retained = deepcopy(self.initial_result)
                retained.update(status="failed", error="HTTP_ERROR")
                change(retained)
                self.return_result(retained)
                self.assertEqual(self.resume()["status"], "failed")
                saved = self.fixture.app.state()["conversation"]
                self.assertEqual(len(saved["messages"]), index)
                self.assertEqual(saved["messages"][-1]["content"], retained["answer"])
                if kind == "citations":
                    new_key = saved["messages"][-1]["citations"][-1]["key"]
                    self.assertNotEqual(new_key, self.initial_messages[-1]["citations"][0]["key"])
                    status, evidence = self.fixture.app.dispatch("GET", "/api/online/evidence", query={
                        "conversation_id": self.conversation["id"], "citation_id": new_key})
                    self.assertEqual(status, 200, evidence)
                    self.assertIn("IDENTIFICATION DIVISION", evidence["text"])

    def test_identical_answer_for_a_new_question_is_not_deduplicated(self):
        complete = deepcopy(self.initial_result)
        complete.update(status="answered", error=None)
        self.return_result(complete)
        self.assertEqual(self.resume()["status"], "answered")
        retained = deepcopy(self.initial_result)
        retained.update(status="failed", error="HTTP_ERROR")
        self.return_result(retained)
        self.assertEqual(self.fixture.ask(self.conversation["id"], "A new business question")["status"], "failed")
        saved = self.fixture.app.state()["conversation"]
        self.assertEqual([item["role"] for item in saved["messages"]], ["user", "assistant", "assistant", "user", "assistant"])
        self.assertEqual(saved["messages"][-1]["content"], self.initial_messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
