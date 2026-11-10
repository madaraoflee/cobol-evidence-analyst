"""Online saved requirements and safe failure diagnostics survive recovery."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_online_investigation as investigation_fixture
import test_online_workspace as workspace_fixture
from api_error_details import build_diagnostic, sanitize_diagnostic
from company_api import APIClientError, TransportResponse
from online_investigation import MAX_PROMPT_BYTES, run_investigation
from online_material_format import MaterialError, canonical_json
from online_web_bridge import dispatch_online


class ScriptedClient:
    def __init__(self, replies, *, outcome="received"):
        self.replies = iter(replies)
        self.last_request_outcome = outcome
        self.messages = []

    def complete(self, *, messages, check_cancel):
        check_cancel()
        self.messages.append(deepcopy(messages))
        result = next(self.replies)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            result = result(json.loads(messages[-1]["content"]))
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}


class OnlineAnswerRequirementsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = investigation_fixture.OnlineInvestigationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def partial(self, **options):
        return self.fixture.run_question([
            {"actions": [investigation_fixture.read()]},
            lambda context: investigation_fixture.answer(context, complete=False, remaining=["Check the worker branch"]),
        ], **options)

    def test_full_saved_requirements_language_priority_and_protocol_reach_every_model_call(self):
        instructions = "  请用繁体中文回答。\n" + "详细解释业务对象、条件和结果，不逐行翻译源码。\n" * 400 + "最后核对全部调用关系。  "
        result = self.fixture.run_question([
            {"actions": [investigation_fixture.read()]}, investigation_fixture.answer,
        ], question="Explain the payment sequence.", answer_instructions=instructions, locale="en")
        self.assertEqual(result["status"], "answered", result)
        self.assertEqual(result["answer_language"], "zh-HK")
        self.assertEqual(result["checkpoint"]["answer_requirements"]["answer_instructions"], instructions)
        self.assertEqual(len(self.fixture.requests), 2)
        for request in self.fixture.requests:
            system = request["messages"][0]["content"]
            self.assertIn(instructions, system)
            self.assertIn("(zh-HK)", system)
            self.assertIn("investigation JSON protocol above remains mandatory", system)
            self.assertIn("Keep source identifiers, literal values, quoted source and citation IDs unchanged", system)
        self.assertIn("MOVE 'REVIEW' TO RESULT-STATE", self.fixture.contexts[-1]["source_context"][0]["text"])
        explicit = self.fixture.run_question([
            {"actions": [investigation_fixture.read()]}, investigation_fixture.answer,
        ], question="Please answer in English: explain the payment sequence.", answer_instructions=instructions, locale="zh-CN")
        self.assertEqual(explicit["answer_language"], "en")

    def test_prose_and_locale_fallback_match_existing_language_resolver(self):
        for question, locale, expected in (("Explain the rules.", "zh-HK", "en"),
                                            ("说明业务条件", "en", "zh-CN"), ("MAIN-ENTRY?", "zh-HK", "zh-HK")):
            with self.subTest(question=question):
                result = self.fixture.run_question([{"actions": [investigation_fixture.read()]}], question=question,
                    answer_instructions="Keep original identifiers and explain business purpose.", locale=locale, max_model_calls=1)
                self.assertEqual(result["answer_language"], expected)

    def test_legacy_checkpoint_defaults_only_new_fields_and_rejects_other_changes(self):
        first = self.partial()
        checkpoint = deepcopy(first["checkpoint"])
        for key in ("answer_requirements", "revision_draft", "diagnostic"):
            checkpoint.pop(key)
        resumed = self.fixture.run_question([investigation_fixture.answer], checkpoint=checkpoint)
        self.assertEqual(resumed["status"], "answered", resumed)
        for key, value in (("unexpected", True), ("repository", {"identity": "other/source"}),
                           ("answer_requirements", {"answer_instructions": "", "answer_language": []})):
            invalid = {**checkpoint, key: value}
            with self.subTest(key=key), self.assertRaises(MaterialError):
                self.fixture.run_question([], checkpoint=invalid)

    def test_changed_requirements_revise_pending_answer_with_all_evidence_and_unfinished_work(self):
        saved = []
        def journal(event):
            saved.append(deepcopy(event))
            pending = event["checkpoint"]["pending_response"]
            if event["phase"] == "model_response_received" and pending and "answer" in pending:
                raise OSError("simulated journal failure")
        first = self.fixture.run_question([
            {"actions": [investigation_fixture.read()], "remaining": ["Check the entry conditions"]},
            lambda context: investigation_fixture.answer(context, "Earlier draft", complete=False, remaining=["Check the worker branch"]),
        ], answer_instructions="Write a short English explanation.", progress=journal)
        self.assertEqual(first["status"], "failed")
        checkpoint = saved[-1]["checkpoint"]
        old_pending = deepcopy(checkpoint["pending_response"])
        checkpoint["pending_actions"] = [investigation_fixture.read("worker.cbl", 1, 6)]
        before = len(self.fixture.requests)
        resumed = self.fixture.run_question([lambda context: investigation_fixture.answer(context, "Revised business explanation")],
            checkpoint=checkpoint, answer_instructions="用简体中文详细解释业务条件和全部调用关系。")
        self.assertEqual(resumed["status"], "answered", resumed)
        self.assertEqual(len(self.fixture.requests) - before, 1)
        context = self.fixture.contexts[-1]
        self.assertEqual(context["revision_draft"], old_pending)
        self.assertEqual(context["remaining"], ["Check the entry conditions"])
        self.assertEqual(context["revision_draft"]["remaining"], ["Check the worker branch"])
        self.assertEqual({item["citation"]["path"] for item in context["source_context"]}, {"main.cbl", "worker.cbl"})
        self.assertTrue(set(item["id"] for item in checkpoint["evidence"]) <= set(item["id"] for item in resumed["evidence"]))
        self.assertIn("Revised business explanation", resumed["answer"])
        self.assertNotIn("Earlier draft", resumed["answer"])
        self.assertIsNone(resumed["checkpoint"]["revision_draft"])

    def test_answered_checkpoint_same_requirements_replays_but_changed_requirements_regenerates(self):
        first = self.fixture.run_question([{"actions": [investigation_fixture.read()]}, investigation_fixture.answer],
            answer_instructions="Explain business purpose.")
        before = len(self.fixture.requests)
        replay = self.fixture.run_question([], checkpoint=first["checkpoint"], answer_instructions="Explain business purpose.")
        self.assertEqual(replay["status"], "answered")
        self.assertEqual(len(self.fixture.requests), before)
        revised = self.fixture.run_question([lambda context: investigation_fixture.answer(context, "Detailed revision")],
            checkpoint=first["checkpoint"], answer_instructions="Explain all supported business branches in detail.")
        self.assertEqual(revised["status"], "answered", revised)
        self.assertEqual(len(self.fixture.requests), before + 1)
        self.assertEqual(self.fixture.contexts[-1]["revision_draft"]["answer"], first["answer"])

    def test_unknown_outcome_preserves_diagnostic_and_old_requirements_until_explicit_retry(self):
        diagnostic = build_diagnostic("REQUEST_TIMEOUT")
        client = ScriptedClient([APIClientError("REQUEST_TIMEOUT", diagnostic=diagnostic)], outcome="unknown")
        first = run_investigation(self.fixture.query, self.fixture.source, "Explain the rules", client=client,
            allow_source_transfer=True, answer_instructions="Earlier requirement")
        checkpoint = deepcopy(first["checkpoint"])
        rejected = run_investigation(self.fixture.query, self.fixture.source, "Explain the rules", client=client,
            allow_source_transfer=True, checkpoint=checkpoint, answer_instructions="New full requirement")
        self.assertEqual(rejected["status"], "retry_confirmation_required")
        self.assertEqual(rejected["diagnostic"], sanitize_diagnostic(diagnostic))
        self.assertEqual(rejected["checkpoint"]["answer_requirements"], checkpoint["answer_requirements"])
        self.assertEqual(len(client.messages), 1)
        retry = ScriptedClient([{"actions": [investigation_fixture.read()]}, investigation_fixture.answer])
        completed = run_investigation(self.fixture.query, self.fixture.source, "Explain the rules", client=retry,
            allow_source_transfer=True, checkpoint=rejected["checkpoint"], approve_retry=True,
            answer_instructions="New full requirement")
        self.assertEqual(completed["status"], "answered", completed)
        self.assertIsNone(completed["diagnostic"])
        self.assertIsNone(completed["checkpoint"]["diagnostic"])

    def test_prompt_capacity_keeps_full_requirements_and_revision_draft_for_later_resume(self):
        first = self.partial(answer_instructions="Earlier requirement")
        full_instructions = "x" * (MAX_PROMPT_BYTES - 1)
        before = len(self.fixture.requests)
        failed = self.fixture.run_question([], checkpoint=first["checkpoint"], answer_instructions=full_instructions)
        self.assertEqual((failed["status"], failed["error"]), ("failed", "ONLINE_PROMPT_CAPACITY_LIMIT"))
        self.assertEqual(len(self.fixture.requests), before)
        self.assertEqual(failed["checkpoint"]["answer_requirements"]["answer_instructions"], full_instructions)
        self.assertEqual(failed["checkpoint"]["revision_draft"]["answer"], first["answer"])
        self.assertEqual(failed["checkpoint"]["evidence"], first["checkpoint"]["evidence"])
        self.assertFalse(failed["answer"])
        recovered = self.fixture.run_question([investigation_fixture.answer], checkpoint=failed["checkpoint"],
            answer_instructions="Explain the supported business rules in detail.")
        self.assertEqual(recovered["status"], "answered", recovered)
        self.assertEqual(self.fixture.contexts[-1]["revision_draft"]["answer"], first["answer"])

    def test_http_diagnostics_are_safe_specific_only_with_evidence_and_never_auto_retried(self):
        cases = ((500, {}, "http_5xx_unknown"), (429, {}, "http_429_unknown"),
                 (500, {"code": "rate_limit_exceeded"}, "rate_limit"),
                 (500, {"code": "insufficient_quota"}, "quota_exhausted"))
        for status, error, expected in cases:
            with self.subTest(status=status, error=error):
                before = len(self.fixture.requests)
                response = TransportResponse(status, json.dumps({"error": {**error, "message": "private-source-and-secret"}}))
                failed = self.fixture.run_question([response])
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["error"], "HTTP_ERROR")
                self.assertEqual(failed["diagnostic"]["category"], expected)
                self.assertEqual(failed["diagnostic"]["http_status"], status)
                self.assertEqual(failed["checkpoint"]["diagnostic"], failed["diagnostic"])
                self.assertEqual(len(self.fixture.requests) - before, 1)
                self.assertNotIn("private-source-and-secret", json.dumps(failed))

    def test_revision_capacity_failure_retains_original_pending_answer_without_marking_answered(self):
        first = self.partial(answer_instructions="Earlier requirement")
        checkpoint = first["checkpoint"]
        limit = len(canonical_json(checkpoint)) + 30
        before = len(self.fixture.requests)
        with mock.patch("online_investigation.MAX_CHECKPOINT_BYTES", limit):
            failed = self.fixture.run_question([], checkpoint=checkpoint, answer_instructions="New requirement " * 100)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "ONLINE_CHECKPOINT_CAPACITY_LIMIT")
        self.assertEqual(failed["checkpoint"]["answer"], checkpoint["answer"])
        self.assertEqual(failed["checkpoint"]["evidence"], checkpoint["evidence"])
        self.assertEqual(failed["checkpoint"]["answer_requirements"], checkpoint["answer_requirements"])
        self.assertEqual(len(self.fixture.requests), before)


class OnlineRequirementsWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = workspace_fixture.OnlineWorkspaceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def conversation(self):
        return self.fixture.conversation(self.fixture.connect()["workspace_id"])

    def saved_checkpoint(self, identifier):
        with self.fixture.app.store.transaction() as db:
            return deepcopy(self.fixture.app.store.get(db, "conversations", identifier)["pending"]["checkpoint"])

    def resume(self, identifier, **options):
        started = self.fixture.post("resume", {"conversation_id": identifier, "allow_source_transfer": True, **options}, 202)
        return self.fixture.wait(started["job_id"])

    def test_bridge_exposes_live_saved_requirements_read_only(self):
        app = SimpleNamespace(state_path=self.fixture.root / "local-state.json", config_provider=lambda: None,
                              answer_instructions="First saved requirement")
        server = SimpleNamespace(app=app, online_app=None, online_lock=threading.RLock())
        with mock.patch("online_workspace.OnlineWorkbench") as factory:
            factory.return_value.dispatch.return_value = (200, {})
            self.assertEqual(dispatch_online(server, "GET", "/api/online/state"), (200, {}))
            provider = factory.call_args.kwargs["answer_instructions_provider"]
            self.assertEqual(provider(), app.answer_instructions)
            app.answer_instructions = "Updated saved requirement"
            self.assertEqual(provider(), app.answer_instructions)
            self.assertEqual(factory.call_args.kwargs["config_provider"], app.config_provider)
        self.assertFalse(app.state_path.exists())

    def test_new_questions_and_followups_receive_latest_full_requirements_and_locale(self):
        saved = ["First saved requirement"]
        self.fixture.app.answer_instructions_provider = lambda: saved[0]
        conversation = self.conversation()
        self.assertEqual(self.fixture.ask(conversation["id"], locale="en")["status"], "answered")
        saved[0] = "最新完整要求\n" * 4000
        self.assertEqual(self.fixture.ask(conversation["id"], "Explain the worker too", locale="zh-HK")["status"], "answered")
        first, followup = [item[1] for item in self.fixture.runner_calls]
        self.assertEqual((first["answer_instructions"], first["locale"]), ("First saved requirement", "en"))
        self.assertEqual((followup["answer_instructions"], followup["locale"]), (saved[0], "zh-HK"))
        self.assertEqual([item["role"] for item in followup["history"]], ["user", "assistant"])

    def test_oversized_saved_requirements_leave_durable_progress_recoverable(self):
        instructions = ["Earlier requirement"]
        self.fixture.app.answer_instructions_provider = lambda: instructions[0]
        client = ScriptedClient([{"actions": [investigation_fixture.read(end=5)]},
            lambda context: investigation_fixture.answer(context, complete=False, remaining=["Check the remaining branch"])])
        def runner(query, source, question, **options):
            return run_investigation(query, source, question, client=client, **options)
        self.fixture.app.investigator = runner
        conversation = self.conversation()
        self.assertEqual(self.fixture.ask(conversation["id"], locale="en")["status"], "needs_continuation")
        original = self.saved_checkpoint(conversation["id"])
        original_messages = self.fixture.app.state()["conversation"]["messages"]
        instructions[0] = "x" * (MAX_PROMPT_BYTES + 1)
        failed = self.resume(conversation["id"])
        self.assertEqual((failed["status"], failed["error"]), ("failed", "ONLINE_PROMPT_CAPACITY_LIMIT"))
        self.assertEqual(len(client.messages), 2)
        self.assertEqual(self.saved_checkpoint(conversation["id"]), original)
        self.assertEqual(self.fixture.app.state()["conversation"]["messages"], original_messages)
        self.assertEqual(self.fixture.app.state()["conversation"]["pending"]["error"], "ONLINE_PROMPT_CAPACITY_LIMIT")
        instructions[0] = "Current complete requirement"
        client = ScriptedClient([investigation_fixture.answer])
        self.assertEqual(self.resume(conversation["id"])["status"], "answered")
        self.assertEqual(len(client.messages), 1)
        self.assertIn(instructions[0], client.messages[0][0]["content"])
        self.assertEqual(json.loads(client.messages[0][-1]["content"])["revision_draft"]["answer"], original["answer"])

    def test_diagnostics_survive_reload_unknown_gate_and_clear_on_success(self):
        diagnostic = build_diagnostic("REQUEST_TIMEOUT")
        client = ScriptedClient([APIClientError("REQUEST_TIMEOUT", diagnostic=diagnostic)], outcome="unknown")
        def runner(query, source, question, **options):
            return run_investigation(query, source, question, client=client, **options)
        self.fixture.app.investigator = runner
        conversation = self.conversation()
        failed = self.fixture.ask(conversation["id"])
        self.assertEqual(failed["status"], "retry_confirmation_required")
        self.assertEqual(failed["diagnostic"], sanitize_diagnostic(diagnostic))
        reloaded = self.fixture.make_app(investigator=runner)
        pending = reloaded.state()["conversation"]["pending"]
        self.assertEqual(pending["diagnostic"], failed["diagnostic"])
        self.assertTrue(pending["retry_confirmation_required"])
        rejected = self.resume(conversation["id"])
        self.assertEqual(rejected["diagnostic"], failed["diagnostic"])
        self.assertEqual(len(client.messages), 1)
        client = ScriptedClient([{"actions": [investigation_fixture.read(end=5)]}, investigation_fixture.answer])
        completed = self.resume(conversation["id"], approve_retry=True)
        self.assertEqual(completed["status"], "answered", completed)
        self.assertIsNone(completed["diagnostic"])
        self.assertIsNone(self.fixture.app.state()["conversation"]["pending"])

    def test_untrusted_diagnostic_fields_are_removed_from_all_persisted_projections(self):
        diagnostic = build_diagnostic("HTTP_ERROR", http_status=500)
        diagnostic.update(reason="private-error-body", debug="private-source", provider_code="unknown-private-code")
        def runner(query, source, question, **options):
            checkpoint = {"request_status": "failed", "diagnostic": diagnostic}
            options["progress"]({"phase": "model_request_failed", "checkpoint": checkpoint})
            return {"status": "failed", "answer": "", "error": "HTTP_ERROR", "checkpoint": checkpoint, "diagnostic": diagnostic}
        self.fixture.app.investigator = runner
        conversation = self.conversation()
        job = self.fixture.ask(conversation["id"])
        self.assertEqual(job["diagnostic"], sanitize_diagnostic(diagnostic))
        checkpoint = self.saved_checkpoint(conversation["id"])
        self.assertEqual(checkpoint["diagnostic"], sanitize_diagnostic(diagnostic))
        persisted = self.fixture.app.store.path.read_bytes()
        for private in (b"private-error-body", b"private-source", b"unknown-private-code"):
            self.assertNotIn(private, persisted)
        self.assertEqual(self.fixture.make_app().state()["conversation"]["pending"]["diagnostic"], sanitize_diagnostic(diagnostic))

    def test_synchronous_provider_failure_keeps_top_level_safe_diagnostic(self):
        diagnostic = build_diagnostic("HTTP_ERROR", http_status=500)
        def failure():
            raise APIClientError("HTTP_ERROR", diagnostic=diagnostic)
        self.fixture.app.answer_instructions_provider = failure
        status, result = self.fixture.app.dispatch("POST", "/api/online/question", {"allow_source_transfer": True})
        self.assertEqual((status, result["error"]), (502, "HTTP_ERROR"))
        self.assertEqual(result["diagnostic"], sanitize_diagnostic(diagnostic))


if __name__ == "__main__":
    unittest.main()
