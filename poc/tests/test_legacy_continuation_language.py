"""Recover old partial web turns after an explicit output-language change."""

from copy import deepcopy
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from company_api import TransportResponse
from run_store import RunStore, fingerprint
from web_app import RequestError
import test_conversation_workbench as workbench_fixtures
import test_web_answer_recovery as recovery_fixtures


class LegacyContinuationLanguageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = recovery_fixtures.WebAnswerRecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_saved_chinese_recovers_english_record_and_is_used_for_new_request(self):
        app, payload = self.fixture.legacy_continuation_case()
        current = "请用中文回答。按业务目的与结果说明。"
        app.answer_instructions = current
        store = RunStore(Path(payload["output"]) / "investigation-runs.sqlite")
        before = deepcopy(store.load("a" * 24))
        view = app._conversation_view()
        self.assertEqual(view["messages"][-1]["investigation_run_id"], "a" * 24)
        with patch("web_app.threading.Thread") as worker:
            app.start(payload)
        options = worker.call_args.kwargs["args"][1]
        self.assertEqual(options["resume_run_id"], "a" * 24)
        self.assertEqual(options["answer_instructions"], current)
        self.assertEqual(options["locale"], "en")
        self.assertEqual(store.load("a" * 24), before)

    def test_nonlanguage_scope_mismatches_remain_unavailable(self):
        for mismatch in ("question", "source", "conversation"):
            with self.subTest(mismatch=mismatch):
                app, payload = self.fixture.legacy_continuation_case(mismatch=mismatch)
                app.answer_instructions = "请用中文回答。"
                self.assertNotIn("investigation_run_id", app._conversation_view()["messages"][-1])
                with patch("web_app.threading.Thread") as worker:
                    with self.assertRaises(RequestError) as failure:
                        app.start(payload)
                    self.assertEqual(failure.exception.code, "CONTINUATION_NOT_AVAILABLE")
                    worker.assert_not_called()

    def test_older_identity_without_language_key_keeps_the_same_scope_check(self):
        app, payload = self.fixture.legacy_continuation_case(entry=None)
        conversation = app.conversation
        target = app.conversation_store.retry_target(conversation["id"], payload["continue_message_id"])
        history = app.conversation_store.model_history(conversation["id"], before_position=target["position"],
            max_characters=AgentPolicy().max_history_characters)
        identity = {"database_path": str(Path(payload["output"]) / "structural-index.sqlite"),
            "source_root": payload["source"], "entry_program": None, "history": history,
            "framework_reference_path": None, "answer_detail": "brief", "conversation_id": conversation["id"]}
        store = RunStore(Path(payload["output"]) / "investigation-runs.sqlite")
        metadata = {**store.load("a" * 24)["metadata"], "analysis_identity": fingerprint(identity)}
        with store.connect() as database:
            database.execute("UPDATE investigation_runs SET metadata_json=? WHERE run_id=?",
                             (json.dumps(metadata), "a" * 24))
        app.answer_instructions = "请用中文回答。"
        self.assertEqual(app._conversation_view()["messages"][-1]["investigation_run_id"], "a" * 24)

    def test_flagged_unknown_outcome_still_blocks_the_worker(self):
        app, payload = self.fixture.legacy_continuation_case()
        app.answer_instructions = "请用中文回答。"
        assistant = app.conversation["messages"][-1]
        details = {key: value for key, value in assistant.items()
                   if key not in {"id", "role", "content", "status", "run_id"}}
        details["retry_blocked_reason"] = "REMOTE_REQUEST_OUTCOME_UNKNOWN"
        app.conversation_store.replace_assistant(app.conversation["id"], assistant["id"], assistant["content"],
            status="partial", run_id=assistant["run_id"], details=details)
        with patch("web_app.threading.Thread") as worker:
            with self.assertRaises(RequestError) as failure:
                app.start(payload)
            self.assertEqual(failure.exception.code, "CONTINUATION_NOT_AVAILABLE")
            worker.assert_not_called()


class LegacyContinuationUnknownLedgerTests(unittest.TestCase):
    def test_recovered_record_with_unknown_request_does_not_call_provider(self):
        fixture = workbench_fixtures.ConversationWorkbenchTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.run_job()
        fixture.app.policy_provider = lambda: AgentPolicy(max_model_requests=1, max_answer_revisions=0)
        question = "How is CAPACITY checked?"
        def first_answer(request):
            envelope = json.loads(request.body)
            fixture.calls.append(envelope)
            identifier = re.search(r"ev_page_[a-f0-9]+", json.dumps(envelope))[0]
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content":
                "The program reports excess capacity when RESERVED-COUNT > CAPACITY.[" + identifier + "]"},
                "finish_reason": "length"}]}))
        fixture.transport = first_answer
        prior = fixture.run_job(question=question, allow_network=True, answer_detail="brief", locale="en")
        conversation = prior["conversation"]
        user, assistant = conversation["messages"]
        old_id = assistant["investigation_run_id"]
        details = {key: value for key, value in assistant.items()
                   if key not in {"id", "role", "content", "status", "run_id", "investigation_run_id", "analysis_options"}}
        fixture.app.conversation_store.replace_assistant(conversation["id"], assistant["id"], assistant["content"],
            status="partial", run_id=assistant["run_id"], details=details)
        journal = RunStore(fixture.output / "investigation-runs.sqlite")
        journal.start_request(old_id, "pending-review", "request-digest")
        saved = deepcopy(journal.load(old_id))
        fixture.app.answer_instructions = "请用中文回答。"
        before = len(fixture.calls)
        def must_not_send(_request):
            self.fail("An unknown provider request was resent")
        fixture.transport = must_not_send
        result = fixture.run_job(question=question, allow_network=True, conversation_id=conversation["id"],
            continue_message_id=user["id"], answer_detail="brief", locale="en")
        self.assertEqual(result["agent"]["reason_code"], "REMOTE_REQUEST_OUTCOME_UNKNOWN")
        self.assertFalse(result["agent"]["agent_result"]["answer_available"])
        self.assertEqual(len(fixture.calls), before)
        self.assertEqual(journal.load(old_id), saved)


if __name__ == "__main__":
    unittest.main()
