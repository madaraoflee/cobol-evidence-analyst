"""Offline web checks for complete answer persistence and output-limit notices."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conversation_store import ConversationStore
from agent_policy import AgentPolicy
from run_store import RunStore, fingerprint
import hashlib
from web_app import RequestError, WorkbenchState, _validate_options


class WebAnswerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()

    def continuation_case(self):
        app = WorkbenchState(state_path=None)
        self.continuation_number = getattr(self, "continuation_number", 0) + 1
        output = (self.root / f"output-{self.continuation_number}").resolve()
        source = self.source.resolve()
        app.project = {"source": str(source), "output": str(output), "snapshot_id": "sha256:source"}
        app.conversation_store = ConversationStore(output, source)
        conversation = app.conversation_store.create("sha256:source")
        app.conversation_store.append(conversation["id"], "user", "Earlier question", run_id="earlier")
        app.conversation_store.append(conversation["id"], "assistant", "Earlier answer", run_id="earlier")
        question = "Explain the request conditions."
        user_id = app.conversation_store.append(conversation["id"], "user", question,
            status="pending", run_id="original-job")
        options = {"source": str(source), "output": str(output), "question": question,
            "conversation_id": conversation["id"], "entry": "request.cbl", "answer_detail": "brief", "locale": "en"}
        app._record_answer(options, "original-job", {"snapshot_id": "sha256:source", "agent": {
            "runner_status": "COMPLETED", "agent_result": {"run_id": "a" * 24, "status": "PARTIAL",
                "answer": "The known request condition.", "answer_origin": "model", "answer_available": True,
                "model_answer_recorded": True, "stop_reason": "request_budget", "snapshot_id": "sha256:source",
                "evidence_refs": [{"evidence_id": "ev-condition", "relative_path": "request.cbl"}]}}})
        payload = {**options, "allow_network": True, "entry": "different.cbl", "answer_detail": "detailed",
            "locale": "zh-HK", "continue_message_id": user_id}
        return app, payload

    def test_continuation_request_restores_original_scope_and_preserves_answer_while_pending(self):
        app, payload = self.continuation_case()
        original = app.conversation["messages"][-1]
        self.assertEqual(original["investigation_run_id"], "a" * 24)
        self.assertEqual(original["analysis_options"], {"entry": "request.cbl", "answer_detail": "brief", "locale": "en"})
        with patch("web_app.threading.Thread") as worker:
            response = app.start(payload)
        arguments = worker.call_args.kwargs["args"][1]
        self.assertTrue(arguments["continue_investigation"])
        self.assertEqual(arguments["resume_run_id"], "a" * 24)
        self.assertNotIn("start_new_investigation", arguments)
        self.assertEqual(arguments["entry"], "request.cbl")
        self.assertEqual(arguments["answer_detail"], "brief")
        self.assertEqual(arguments["locale"], "en")
        self.assertEqual([item["content"] for item in arguments["conversation_history"]], ["Earlier question", "Earlier answer"])
        self.assertEqual(app.conversation["messages"][-1]["content"], original["content"])
        self.assertEqual(app.conversation["messages"][-1]["investigation_run_id"], "a" * 24)
        self.assertEqual(app.conversation["messages"][-2]["status"], "pending")
        self.assertEqual(len(app.conversation["messages"]), 4)
        with self.assertRaises(RequestError) as duplicate:
            app.start(payload)
        self.assertEqual(duplicate.exception.code, "JOB_ACTIVE")
        self.assertEqual(response["status"], "RUNNING")

    def test_failed_continuation_retains_previous_answer_citations_and_next_continuation_id(self):
        app, payload = self.continuation_case()
        before = app.conversation["messages"][-1]
        with patch("web_app.threading.Thread") as worker:
            job_id = app.start(payload)["job_id"]
        options = worker.call_args.kwargs["args"][1]
        app._record_answer(options, job_id, {"agent": {"runner_status": "NOT_READY", "agent_result": {
            "run_id": "b" * 24, "answer_origin": "diagnostic", "answer_available": False,
            "model_answer_recorded": False, "answer": "No additional answer was received.", "stop_reason": "REQUEST_TIMEOUT"}}})
        after = app.conversation["messages"][-1]
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["content"], before["content"])
        self.assertEqual(after["evidence_refs"], before["evidence_refs"])
        self.assertEqual(after["snapshot_id"], before["snapshot_id"])
        self.assertEqual(after["investigation_run_id"], before["investigation_run_id"])
        self.assertEqual(after["analysis_options"], before["analysis_options"])
        self.assertEqual(after["status"], "partial")
        self.assertEqual(app.conversation["messages"][-2]["status"], "partial")

    def test_completed_continuation_replaces_same_turn_and_records_new_investigation_id(self):
        app, payload = self.continuation_case()
        original_ids = [message["id"] for message in app.conversation["messages"]]
        with patch("web_app.threading.Thread") as worker:
            job_id = app.start(payload)["job_id"]
        options = worker.call_args.kwargs["args"][1]
        app._record_answer(options, job_id, {"agent": {"runner_status": "COMPLETED", "agent_result": {
            "run_id": "b" * 24, "status": "ANALYZED", "answer": "The known request condition. Additional outcome.",
            "answer_origin": "model", "answer_available": True, "model_answer_recorded": True}}})
        self.assertEqual([message["id"] for message in app.conversation["messages"]], original_ids)
        answer = app.conversation["messages"][-1]
        self.assertIn("Additional outcome", answer["content"])
        self.assertEqual(answer["investigation_run_id"], "b" * 24)
        self.assertEqual(answer["status"], "completed")

    def test_legacy_or_unknown_investigation_is_retryable_without_claiming_a_continuation(self):
        for mode in ("legacy", "unknown"):
            with self.subTest(mode=mode):
                app, payload = self.continuation_case()
                answer = app.conversation["messages"][-1]
                details = {key: value for key, value in answer.items() if key not in {"id", "role", "content", "status", "run_id"}}
                if mode == "legacy":
                    details.pop("investigation_run_id", None)
                else:
                    details["retry_blocked_reason"] = "REMOTE_REQUEST_OUTCOME_UNKNOWN"
                app.conversation_store.replace_assistant(app.conversation["id"], answer["id"], answer["content"],
                    status="partial", run_id=answer["run_id"], details=details)
                with patch("web_app.threading.Thread") as worker:
                    with self.assertRaises(RequestError) as unavailable:
                        app.start(payload)
                    self.assertEqual(unavailable.exception.code, "CONTINUATION_NOT_AVAILABLE")
                    worker.assert_not_called()
                    retry = {key: value for key, value in payload.items() if key != "continue_message_id"}
                    retry["retry_message_id"] = payload["continue_message_id"]
                    app.start(retry)
                self.assertTrue(worker.call_args.kwargs["args"][1]["start_new_investigation"])

    def test_continuation_id_is_validated_and_cannot_be_combined_with_retry(self):
        payload = {"source": str(self.source), "output": str(self.root / "unused")}
        for value in (True, [], "unknown", "a" * 65):
            with self.subTest(value=value), self.assertRaises(RequestError):
                _validate_options({**payload, "continue_message_id": value})
        with self.assertRaises(RequestError):
            _validate_options({**payload, "continue_message_id": "a" * 32, "retry_message_id": "b" * 32})

    def legacy_continuation_case(self, *, entry="request.cbl", mismatch=None):
        app, payload = self.continuation_case()
        conversation = app.conversation
        answer = conversation["messages"][-1]
        details = {key: value for key, value in answer.items() if key not in
            {"id", "role", "content", "status", "run_id", "investigation_run_id", "analysis_options"}}
        details["metrics"] = {"quality_trace_path": "Z:\\older-workspace\\quality-traces\\" + "a" * 24 + ".json"}
        app.conversation_store.replace_assistant(conversation["id"], answer["id"], answer["content"],
            status="partial", run_id=answer["run_id"], details=details)
        app.conversation = app.conversation_store.get(conversation["id"])
        target = app.conversation_store.retry_target(conversation["id"], payload["continue_message_id"])
        policy = AgentPolicy()
        history = app.conversation_store.model_history(conversation["id"], before_position=target["position"],
            max_characters=policy.max_history_characters)
        identity = {"database_path": str(Path(payload["output"]) / "structural-index.sqlite"),
            "source_root": payload["source"], "entry_program": entry, "history": history,
            "framework_reference_path": None, "answer_detail": "brief", "conversation_id": conversation["id"],
            "answer_language": "en"}
        if mismatch == "source":
            identity["source_root"] = str(self.root / "different-source")
        if mismatch == "conversation":
            identity["conversation_id"] = "f" * 32
        metadata = {"question_id": "sha256:" + hashlib.sha256(payload["question"].encode()).hexdigest(),
            "analysis_identity": fingerprint(identity), "configuration": {"policy": policy.to_dict()}}
        if mismatch == "question":
            metadata["question_id"] = "sha256:another-question"
        store = RunStore(Path(payload["output"]) / "investigation-runs.sqlite")
        store.create("a" * 24, metadata)
        store.checkpoint("a" * 24, {}, status="completed")
        app.project["agent"] = {"agent_result": {"run_id": "a" * 24}}
        app.project["run_id"] = answer["run_id"]
        app.project["diagnosis"] = {"question": payload["question"], "entry_requested": entry}
        return app, payload

    def test_legacy_partial_answer_recovers_exact_local_record_without_opening_old_trace_path(self):
        for entry in (None, "request.cbl"):
            with self.subTest(entry=entry):
                app, payload = self.legacy_continuation_case(entry=entry)
                view = app._conversation_view()
                self.assertEqual(view["messages"][-1]["investigation_run_id"], "a" * 24)
                self.assertEqual(view["messages"][-1]["analysis_options"]["entry"], entry)
                self.assertNotIn("investigation_run_id", app.conversation["messages"][-1])
                with patch("web_app.threading.Thread") as worker:
                    app.start(payload)
                options = worker.call_args.kwargs["args"][1]
                self.assertEqual(options["resume_run_id"], "a" * 24)
                self.assertEqual(options["entry"], entry)
                self.assertTrue(options["continue_investigation"])

    def test_legacy_record_from_other_question_source_or_conversation_is_not_used(self):
        for mismatch in ("question", "source", "conversation"):
            with self.subTest(mismatch=mismatch):
                app, payload = self.legacy_continuation_case(mismatch=mismatch)
                self.assertNotIn("investigation_run_id", app._conversation_view()["messages"][-1])
                with patch("web_app.threading.Thread") as worker:
                    with self.assertRaises(RequestError) as unavailable:
                        app.start(payload)
                    self.assertEqual(unavailable.exception.code, "CONTINUATION_NOT_AVAILABLE")
                    worker.assert_not_called()

    def test_answer_detail_defaults_and_validation_preserve_old_requests(self):
        payload = {"source": str(self.source), "output": str(self.root / "output")}
        self.assertEqual(_validate_options(payload)["answer_detail"], "detailed")
        for value in ("brief", "detailed"):
            self.assertEqual(_validate_options({**payload, "answer_detail": value})["answer_detail"], value)
        for value in ("concise", True, [], 7):
            with self.subTest(value=value), self.assertRaises(RequestError):
                _validate_options({**payload, "answer_detail": value})

    def test_interface_locale_is_optional_and_accepts_only_supported_values(self):
        payload = {"source": str(self.source), "output": str(self.root / "output")}
        self.assertNotIn("locale", _validate_options(payload))
        for value in ("en", "zh-CN", "zh-HK"):
            self.assertEqual(_validate_options({**payload, "locale": value})["locale"], value)
        for value in ("fr", "en-US", True, [], 7):
            with self.subTest(value=value), self.assertRaises(RequestError):
                _validate_options({**payload, "locale": value})

    def test_missing_answer_uses_question_language_and_persists_language_metadata(self):
        app = WorkbenchState(state_path=None)
        app.conversation_store = ConversationStore(self.root / "output", self.source)
        conversation = app.conversation_store.create()
        question = "Explain the eligibility rules."
        app._record_answer({"conversation_id": conversation["id"], "question": question, "locale": "zh-HK"},
                           "missing-answer", {"agent": {"runner_status": "NOT_READY"}})
        message = ConversationStore(self.root / "output", self.source).get(conversation["id"])["messages"][0]
        self.assertEqual(message["content"], "No model answer was received. The conversation has been retained; you can retry.")
        self.assertEqual(message["answer_language"], "en")
        self.assertEqual(message["locale"], "zh-HK")

    def test_long_answer_and_length_status_survive_conversation_reload(self):
        app = WorkbenchState(state_path=None)
        app.conversation_store = ConversationStore(self.root / "output", self.source)
        conversation = app.conversation_store.create()
        run_id = "neutral-run"
        app.conversation_store.append(conversation["id"], "user", "Explain the eligibility rules.",
                                      status="pending", run_id=run_id)
        answer = "## Eligibility\n\n" + "A valid request requires sufficient capacity.\n\n" * 3000 + "TAIL-RETAINED"
        project = {"agent": {"runner_status": "COMPLETED", "agent_result": {
            "answer": answer, "status": "PARTIAL", "stop_reason": "MODEL_OUTPUT_TRUNCATED",
            "finish_reason": "length", "answer_truncated": True}}}
        app._record_answer({"conversation_id": conversation["id"], "answer_detail": "brief"},
                           run_id, project)
        saved = ConversationStore(self.root / "output", self.source).get(conversation["id"])
        message = saved["messages"][1]
        self.assertEqual(message["content"], answer)
        self.assertEqual(message["analysis_status"], "PARTIAL")
        self.assertEqual(message["finish_reason"], "length")
        self.assertTrue(message["answer_truncated"])
        self.assertEqual(message["answer_detail"], "brief")
        self.assertEqual(message["status"], "partial")

    def test_answer_preference_uses_existing_workspace_file(self):
        path = self.root / "workspace.json"
        app = WorkbenchState(state_path=path)
        app.answer_detail = "brief"
        app._save_workspace()
        self.assertEqual(json.loads(path.read_text())["answer_detail"], "brief")
        self.assertEqual(WorkbenchState(state_path=path).answer_detail, "brief")

    def test_source_fact_answer_origin_survives_workspace_history_reload(self):
        app = WorkbenchState(state_path=None)
        app.conversation_store = ConversationStore(self.root / "output", self.source)
        conversation = app.conversation_store.create()
        result = {"answer": "已读取版本中 FINAL-AMOUNT = BASIS * FACTOR。",
            "status": "PARTIAL", "stop_reason": "REQUEST_TIMEOUT",
            "answer_origin": "local_source_facts", "answer_available": True,
            "model_answer_recorded": False}
        app._record_answer({"conversation_id": conversation["id"]}, "source-fact-run",
                           {"agent": {"runner_status": "COMPLETED", "agent_result": result}})
        message = ConversationStore(self.root / "output", self.source).get(conversation["id"])["messages"][0]
        self.assertEqual(message["content"], result["answer"])
        self.assertEqual(message["analysis_status"], "PARTIAL")
        self.assertEqual(message["status"], "partial")
        self.assertEqual(message["answer_origin"], "local_source_facts")
        self.assertTrue(message["answer_available"])
        self.assertFalse(message["model_answer_recorded"])

    def test_projected_answer_records_display_limit_without_claiming_provider_truncation(self):
        app = WorkbenchState(state_path=None)
        app.conversation_store = ConversationStore(self.root / "output", self.source)
        conversation = app.conversation_store.create()
        project = {"agent": {"runner_status": "COMPLETED", "agent_result": {
            "answer": "Retained answer", "status": "ANALYZED", "finish_reason": "stop"},
            "display_projection": {"omitted": [{"path": "agent_result.answer",
                "retained_characters": 15, "total_characters": 1000015}]}}}
        app._record_answer({"conversation_id": conversation["id"]}, "neutral-run", project)
        message = app.conversation["messages"][0]
        self.assertEqual(message["content"], "Retained answer")
        self.assertTrue(message["answer_display_truncated"])
        self.assertEqual(message["answer_complete_characters"], 1000015)
        self.assertFalse(message["answer_truncated"])
        self.assertEqual(message["finish_reason"], "stop")


if __name__ == "__main__":
    unittest.main()
