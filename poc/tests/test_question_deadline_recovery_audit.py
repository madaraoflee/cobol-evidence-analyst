"""Independent deadline checks at response, checkpoint and verification boundaries."""

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from analyze_source import AnalysisCancelled
import business_chat
from business_index import build_business_index
from company_api import APIClientError, CompanyAPIConfig, TransportResponse
from quality_trace import QualityTrace
from repository_discovery import ensure_repository_search
from run_store import RunStore, fingerprint
from source_session import QuestionSourceSession


class QuestionDeadlineRecoveryAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.source.joinpath("entry.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRYPOINT.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 IN-AMOUNT PIC 9(5) VALUE 4.\n"
            "01 OUT-AMOUNT PIC 9(5).\nPROCEDURE DIVISION.\nMAIN.\n"
            "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nGOBACK.\n", encoding="utf-8")
        self.database = self.root / "index.sqlite"
        build_business_index(self.source, self.database, source_format="free", verify_content=True,
                             framework_reference_path="")
        ensure_repository_search(self.database, self.source)
        self.store = RunStore(self.root / "investigation-runs.sqlite")
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model", api_key="offline-only")
        self.clock_value = 0.0
        self.clock = SimpleNamespace(monotonic=lambda: self.clock_value)
        self.calls = 0
        self.policy = AgentPolicy(max_question_seconds=120, hard_question_seconds=120,
                                  max_model_requests=3, max_answer_revisions=0)

    def answer(self, request, *, factor=2):
        self.calls += 1
        payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
        evidence = next(page["evidence_id"] for page in payload["source_context"][0]["pages"]
                        if "COMPUTE OUT-AMOUNT" in page["source_text"])
        return TransportResponse(200, json.dumps({"choices": [{"message": {
            "content": f"OUT-AMOUNT = IN-AMOUNT * {factor}。[{evidence}]"}, "finish_reason": "stop"}]}))

    def ask(self, **options):
        with mock.patch.object(business_chat, "time", self.clock):
            return business_chat.run_business_chat("ENTRYPOINT OUT-AMOUNT 怎么计算？", self.database,
                self.source, self.config, framework_reference_path="", policy=self.policy,
                **({"transport": self.answer} | options))

    def test_unknown_remote_result_blocks_an_ordinary_reentry(self):
        def timed_out(request):
            self.calls += 1
            raise APIClientError("REQUEST_TIMEOUT")

        first = self.ask(transport=timed_out)
        saved = self.store.load(first["agent_result"]["run_id"])
        self.assertEqual(saved["state"], "attention")
        self.assertEqual(len(saved["uncertain_requests"]), 1)
        second = self.ask(transport=timed_out)
        self.assertEqual(self.calls, 1, "An uncertain request must not be silently repeated")
        self.assertEqual(second["reason_code"], "REMOTE_REQUEST_OUTCOME_UNKNOWN")

    def test_received_answer_at_deadline_still_passes_claim_ledger(self):
        original = QualityTrace.record_remote_response

        def recorded(trace, response):
            original(trace, response)
            self.clock_value = 121.0

        with mock.patch.object(QualityTrace, "record_remote_response", new=recorded):
            first = self.ask(transport=lambda request: self.answer(request, factor=999))
        result = first["agent_result"]
        self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", result["answer"])
        self.assertNotIn("999", result["answer"])
        self.assertGreater(result["claim_ledger"]["revised_claim_count"], 0)
        self.assertEqual(self.calls, 1)

    def test_policy_change_does_not_repeat_an_uncertain_request(self):
        def timed_out(request):
            self.calls += 1
            raise APIClientError("REQUEST_TIMEOUT")
        self.ask(transport=timed_out)
        self.policy = replace(self.policy, max_question_seconds=180)
        result = self.ask(transport=timed_out)
        self.assertEqual(self.calls, 1)
        self.assertEqual(result["reason_code"], "REMOTE_REQUEST_OUTCOME_UNKNOWN")

    def test_timeout_setting_change_does_not_repeat_an_uncertain_request(self):
        def timed_out(request):
            self.calls += 1
            raise APIClientError("REQUEST_TIMEOUT")

        self.ask(transport=timed_out)
        self.config = replace(self.config, timeout_seconds=60)
        result = self.ask(transport=timed_out)
        self.assertEqual(self.calls, 1)
        self.assertEqual(result["reason_code"], "REMOTE_REQUEST_OUTCOME_UNKNOWN")

    def test_interruption_after_trace_save_does_not_hide_uncertain_request(self):
        original = QualityTrace.save

        def timed_out(request):
            self.calls += 1
            raise APIClientError("REQUEST_TIMEOUT")

        def saved_then_interrupted(trace, **options):
            original(trace, **options)
            raise RuntimeError("interrupted_after_trace_save")

        with mock.patch.object(QualityTrace, "save", new=saved_then_interrupted):
            with self.assertRaisesRegex(RuntimeError, "interrupted_after_trace_save"):
                self.ask(transport=timed_out)
        result = self.ask(transport=timed_out)
        self.assertEqual(self.calls, 1)
        self.assertEqual(result["reason_code"], "REMOTE_REQUEST_OUTCOME_UNKNOWN")

    def test_deadline_during_requested_tool_preserves_resumable_frontier(self):
        received = False

        def action(request):
            nonlocal received
            self.calls += 1
            received = True
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": json.dumps({
                "read": [{"relative_path": "entry.cbl", "start_line": 7, "end_line": 10}]})},
                "finish_reason": "stop"}]}))

        def progress(event):
            if received and event.get("phase") == "retrieving":
                self.clock_value = 121.0

        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model",
                                       api_key="offline-only", timeout_seconds=30)
        first = self.ask(transport=action, progress=progress)
        result = first["agent_result"]
        self.assertEqual(result["stop_reason"], "QUESTION_TIME_BUDGET_EXHAUSTED")
        saved = self.store.load(result["run_id"])
        self.assertEqual(saved["state"], "running")
        self.assertFalse(saved["uncertain_requests"])
        self.clock_value = 130.0
        resumed = self.ask()
        self.assertEqual(resumed["agent_result"]["run_id"], result["run_id"])
        self.assertEqual(resumed["reason_code"], "QUESTION_TIME_BUDGET_EXHAUSTED")
        self.assertEqual(self.calls, 1)
        self.assertTrue(self.store.load(result["run_id"])["checkpoint"].get("frontier"))

    def test_saved_response_replay_finishes_the_original_trace_round(self):
        original = QualityTrace.record_remote_response

        def interrupt(trace, response):
            original(trace, response)
            raise RuntimeError("interrupted_after_response_saved")

        with mock.patch.object(QualityTrace, "record_remote_response", new=interrupt):
            with self.assertRaisesRegex(RuntimeError, "interrupted_after_response_saved"):
                self.ask()
        result = self.ask()["agent_result"]
        self.assertEqual(self.calls, 1)
        trace = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text(encoding="utf-8"))
        self.assertIsNotNone(trace["rounds"][0]["response"])
        self.assertEqual(trace["rounds"][0]["response"]["finish_reason"], "stop")

    def legacy_policy_metadata(self, run_id, *, previous_pipeline=False, invalid=False):
        saved = self.store.load(run_id)
        metadata = saved["metadata"]
        metadata["configuration"]["policy"].pop("delivery_target_seconds")
        if previous_pipeline:
            metadata["pipeline_version"] = "question-evidence-v30"
        if invalid:
            metadata["configuration"]["policy"]["max_question_seconds"] = "invalid"
        with self.store.connect() as database:
            database.execute("UPDATE investigation_runs SET metadata_json=?,identity=? WHERE run_id=?",
                (json.dumps(metadata), fingerprint(metadata), run_id))
        return self.store.load(run_id)

    def interrupted_received_response(self):
        original = QualityTrace.record_remote_response
        run_ids = []

        def interrupt(trace, response):
            original(trace, response)
            run_ids.append(trace.data["run_id"])
            raise RuntimeError("interrupted_after_response_saved")

        with mock.patch.object(QualityTrace, "record_remote_response", new=interrupt):
            with self.assertRaisesRegex(RuntimeError, "interrupted_after_response_saved"):
                self.ask()
        return run_ids[0]

    def test_auto_discovered_old_policy_missing_new_field_still_hits_upgrade_gate(self):
        run_id = self.interrupted_received_response()
        before = self.legacy_policy_metadata(run_id, previous_pipeline=True)
        self.assertEqual(before["state"], "running")
        self.assertFalse(before["uncertain_requests"])
        self.assertEqual([row["state"] for row in before["requests"]], ["completed"])
        self.assertIsNotNone(before["checkpoint"]["question_budget"])
        self.clock_value = 130
        for options in ({}, {"resume_run_id": run_id}, {"start_new_investigation": True}):
            with self.subTest(options=options):
                result = self.ask(**options)
                self.assertEqual(result["reason_code"], "RESUME_PIPELINE_VERSION_CHANGED")
                self.assertEqual(result["agent_result"]["run_id"], run_id)
                self.assertEqual(self.calls, 1)
                self.assertEqual(self.store.load(run_id), before)
                with self.store.connect() as database:
                    self.assertEqual(database.execute("SELECT COUNT(*) FROM investigation_runs").fetchone()[0], 1)

    def test_invalid_saved_policy_does_not_become_default_compatible_policy(self):
        run_id = self.interrupted_received_response()
        before = self.legacy_policy_metadata(run_id, previous_pipeline=True, invalid=True)
        result = self.ask(resume_run_id=run_id)
        self.assertEqual(result["reason_code"], "RESUME_IDENTITY_MISMATCH")
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.store.load(run_id), before)

    def test_missing_optional_policy_field_keeps_expired_budget_on_same_pipeline(self):
        def action(request):
            self.calls += 1
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": json.dumps({
                "read": [{"relative_path": "entry.cbl", "start_line": 7, "end_line": 10}]})},
                "finish_reason": "stop"}]}))

        def progress(event):
            if self.calls and event.get("phase") == "retrieving":
                self.clock_value = 121

        first = self.ask(transport=action, progress=progress)["agent_result"]
        self.assertEqual(first["stop_reason"], "QUESTION_TIME_BUDGET_EXHAUSTED")
        before = self.legacy_policy_metadata(first["run_id"])
        self.assertEqual(before["state"], "running")
        self.assertFalse(before["uncertain_requests"])
        self.assertEqual([row["state"] for row in before["requests"]], ["completed"])
        self.clock_value = 130
        second = self.ask()
        self.assertEqual(second["reason_code"], "QUESTION_TIME_BUDGET_EXHAUSTED")
        self.assertEqual(second["agent_result"]["run_id"], first["run_id"])
        self.assertEqual(self.calls, 1)
        after = self.store.load(first["run_id"])
        self.assertEqual(after["requests"], before["requests"])
        old_budget, new_budget = before["checkpoint"]["question_budget"], after["checkpoint"]["question_budget"]
        self.assertGreaterEqual(new_budget["elapsed_seconds"], old_budget["elapsed_seconds"])
        self.assertLessEqual(new_budget["hard_deadline_epoch_seconds"], old_budget["hard_deadline_epoch_seconds"])

    def expired_planning_run(self):
        def action(request):
            self.calls += 1
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": json.dumps({
                "read": [{"relative_path": "entry.cbl", "start_line": 7, "end_line": 10}]})},
                "finish_reason": "stop"}]}))

        def progress(event):
            if self.calls and event.get("phase") == "retrieving":
                self.clock_value = 121

        first = self.ask(transport=action, progress=progress)["agent_result"]
        self.assertEqual(first["stop_reason"], "QUESTION_TIME_BUDGET_EXHAUSTED")
        saved = self.store.load(first["run_id"])
        self.assertGreaterEqual(saved["checkpoint"]["question_budget"]["elapsed_seconds"], 121)
        self.assertEqual(saved["checkpoint"]["question_budget"]["hard_limit_seconds"], 120)
        self.assertFalse(saved["uncertain_requests"])
        return first["run_id"]

    def remove_current_default_deadline(self):
        self.policy = replace(self.policy, max_question_seconds=None, hard_question_seconds=None)
        self.assertIsNone(self.policy.hard_question_seconds)

    def test_new_default_without_deadline_preserves_expired_ledger_until_explicit_retry(self):
        run_id = self.expired_planning_run()
        before = self.store.load(run_id)
        self.remove_current_default_deadline()
        for options in ({}, {"resume_run_id": run_id}):
            with self.subTest(options=options):
                result = self.ask(**options)
                self.assertEqual(result["reason_code"], "QUESTION_TIME_BUDGET_EXHAUSTED")
                self.assertEqual(result["agent_result"]["run_id"], run_id)
                self.assertEqual(self.calls, 1)
                self.assertEqual(self.store.load(run_id)["requests"], before["requests"])
                with self.store.connect() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM investigation_runs").fetchone()[0], 1)
        retried = self.ask(start_new_investigation=True)
        self.assertNotEqual(retried["agent_result"]["run_id"], run_id)
        self.assertEqual(self.calls, 2)
        self.assertIsNone(retried["agent_result"]["metrics"]["policy"]["hard_question_seconds"])
        self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", retried["agent_result"]["answer"])
        self.assertTrue(self.store.load(run_id)["checkpoint"]["superseded_by_explicit_retry"])

    def test_new_default_replays_completed_response_with_original_finite_policy(self):
        run_id = self.interrupted_received_response()
        self.remove_current_default_deadline()
        result = self.ask()["agent_result"]
        self.assertEqual(result["run_id"], run_id)
        self.assertEqual(self.calls, 1)
        self.assertEqual(result["metrics"]["policy"]["hard_question_seconds"], 120)
        self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", result["answer"])

    def test_default_change_and_configuration_change_cannot_bypass_old_pipeline_gate(self):
        run_id = self.interrupted_received_response()
        before = self.legacy_policy_metadata(run_id, previous_pipeline=True)
        self.remove_current_default_deadline()
        self.config = replace(self.config, timeout_seconds=60)
        for options in ({}, {"resume_run_id": run_id}, {"start_new_investigation": True}):
            with self.subTest(options=options):
                result = self.ask(**options)
                self.assertEqual(result["reason_code"], "RESUME_PIPELINE_VERSION_CHANGED")
                self.assertEqual(result["agent_result"]["run_id"], run_id)
                self.assertEqual(self.calls, 1)
                self.assertEqual(self.store.load(run_id), before)

    def test_removed_deadline_does_not_silently_reset_other_incompatible_configuration(self):
        run_id = self.interrupted_received_response()
        self.remove_current_default_deadline()
        self.config = replace(self.config, timeout_seconds=60)
        result = self.ask()
        self.assertEqual(result["reason_code"], "RESUME_IDENTITY_MISMATCH")
        self.assertEqual(result["agent_result"]["run_id"], run_id)
        self.assertEqual(self.calls, 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM investigation_runs").fetchone()[0], 1)

    def test_legacy_metadata_restores_finite_limit_without_budget_checkpoint(self):
        from datetime import datetime, timedelta, timezone
        run_id = self.expired_planning_run()
        saved = self.store.load(run_id)
        checkpoint = saved["checkpoint"]
        checkpoint.pop("question_budget")
        self.store.checkpoint(run_id, checkpoint, status="running")
        with self.store.connect() as db:
            db.execute("UPDATE investigation_runs SET created_at=? WHERE run_id=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=130)).isoformat(), run_id))
        self.remove_current_default_deadline()
        result = self.ask()
        self.assertEqual(result["reason_code"], "QUESTION_TIME_BUDGET_EXHAUSTED")
        self.assertEqual(result["agent_result"]["run_id"], run_id)
        self.assertEqual(self.calls, 1)

    def test_unknown_outcome_is_never_resent_across_default_and_pipeline_gates(self):
        def lost(request):
            self.calls += 1
            raise APIClientError("REQUEST_TIMEOUT")
        first = self.ask(transport=lost)
        run_id = first["agent_result"]["run_id"]
        before = self.store.load(run_id)
        self.remove_current_default_deadline()
        for options in ({}, {"start_new_investigation": True}):
            with self.subTest(options=options):
                result = self.ask(transport=lost, **options)
                self.assertEqual(result["reason_code"], "REMOTE_REQUEST_OUTCOME_UNKNOWN")
                self.assertEqual(result["agent_result"]["run_id"], run_id)
                self.assertEqual(self.calls, 1)
                self.assertEqual(self.store.load(run_id)["requests"], before["requests"])
        self.legacy_policy_metadata(run_id, previous_pipeline=True)
        for options in ({}, {"start_new_investigation": True}):
            with self.subTest(pipeline_changed=True, options=options):
                result = self.ask(transport=lost, **options)
                self.assertEqual(result["reason_code"], "RESUME_PIPELINE_VERSION_CHANGED")
                self.assertEqual(result["agent_result"]["run_id"], run_id)
                self.assertEqual(self.calls, 1)
                self.assertEqual(self.store.load(run_id)["requests"], before["requests"])

    def test_deadline_while_revalidating_saved_sources_preserves_the_same_run(self):
        original_record = QualityTrace.record_remote_response
        original_capture = QuestionSourceSession.capture
        run_ids = []

        def interrupt(trace, response):
            original_record(trace, response)
            run_ids.append(trace.data["run_id"])
            raise RuntimeError("interrupted_after_response_saved")

        with mock.patch.object(QualityTrace, "record_remote_response", new=interrupt):
            with self.assertRaisesRegex(RuntimeError, "interrupted_after_response_saved"):
                self.ask()
        before = self.store.load(run_ids[0])
        self.assertTrue(before["checkpoint"]["source_pages"])
        self.clock_value = 130.0

        def slow_capture(session, path):
            self.clock_value = 251.0
            return original_capture(session, path)

        with mock.patch.object(QuestionSourceSession, "capture", new=slow_capture):
            stopped = self.ask()
        self.assertEqual(stopped["reason_code"], "QUESTION_TIME_BUDGET_EXHAUSTED")
        self.assertEqual(stopped["agent_result"]["run_id"], run_ids[0])
        self.assertEqual(stopped["agent_result"]["metrics"]["budget_stop_phase"], "validating_saved_sources")
        saved = self.store.load(run_ids[0])
        self.assertEqual(saved["state"], "running")
        self.assertFalse(saved["uncertain_requests"])
        self.assertEqual(saved["checkpoint"]["source_pages"], before["checkpoint"]["source_pages"])
        self.assertEqual(saved["requests"], before["requests"])
        self.assertEqual(saved["completed_steps"], before["completed_steps"])
        self.assertEqual(saved["checkpoint"]["frontier"], before["checkpoint"]["frontier"])

        self.clock_value = 260.0
        resumed = self.ask()["agent_result"]
        self.assertEqual(resumed["run_id"], run_ids[0])
        self.assertEqual(self.calls, 1)
        self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", resumed["answer"])
        self.assertTrue(resumed["claim_ledger"]["all_claims_supported"])

    def test_deadline_before_second_send_does_not_relabel_completed_response(self):
        original = QualityTrace.prepare

        def prepare(trace, **options):
            original(trace, **options)
            if len(trace.data["rounds"]) == 1:
                self.clock_value = 121.0

        def action(request):
            self.calls += 1
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": json.dumps({
                "read": [{"relative_path": "entry.cbl", "start_line": 7, "end_line": 10}]})},
                "finish_reason": "stop"}]}))

        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model",
                                       api_key="offline-only", timeout_seconds=30)
        with mock.patch.object(QualityTrace, "prepare", new=prepare):
            result = self.ask(transport=action)["agent_result"]
        self.assertEqual(self.calls, 1)
        trace = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text(encoding="utf-8"))
        self.assertEqual(len(trace["rounds"]), 1)
        self.assertIsNone(trace["rounds"][0]["response"]["error"])
        self.assertEqual(trace["rounds"][0]["response"]["parsed_action"], ["read"])

    def test_cancellation_wins_when_initial_lookup_also_hits_deadline(self):
        cancel_requested = False

        def cancel():
            if cancel_requested:
                raise AnalysisCancelled("cancelled")

        def slow_map(*args, check_cancel, **kwargs):
            nonlocal cancel_requested
            self.clock_value = 121.0
            cancel_requested = True
            check_cancel()

        with mock.patch.object(business_chat, "build_business_map", side_effect=slow_map):
            with self.assertRaises(AnalysisCancelled):
                self.ask(check_cancel=cancel)
        self.assertEqual(self.calls, 0)

    def test_initial_deadline_reconnect_does_not_reset_budget(self):
        def slow_map(*args, check_cancel, **kwargs):
            self.clock_value = 121.0
            check_cancel()

        with mock.patch.object(business_chat, "build_business_map", side_effect=slow_map):
            first = self.ask()
        run_id = first["agent_result"]["run_id"]
        self.assertEqual(self.store.load(run_id)["state"], "running")
        self.clock_value = 130.0
        second = self.ask()
        self.assertEqual(second["agent_result"]["run_id"], run_id)
        self.assertEqual(self.calls, 0)
        self.assertEqual(second["reason_code"], "QUESTION_TIME_BUDGET_EXHAUSTED")
        third = self.ask(start_new_investigation=True)
        self.assertEqual(self.calls, 1)
        self.assertNotEqual(third["agent_result"]["run_id"], run_id)
        self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", third["agent_result"]["answer"])

    def test_explicit_retry_never_repeats_an_uncertain_remote_request(self):
        def lost(request):
            self.calls += 1
            raise APIClientError("REQUEST_TIMEOUT")
        self.ask(transport=lost)
        self.clock_value = 130.0
        second = self.ask(start_new_investigation=True)
        self.assertEqual(self.calls, 1)
        self.assertEqual(second["reason_code"], "REMOTE_REQUEST_OUTCOME_UNKNOWN")

    def test_explicit_retry_checks_saved_source_before_reusing_frontier(self):
        original = QualityTrace.record_remote_response
        saved_ids = []

        def interrupt(trace, response):
            original(trace, response)
            saved_ids.append(trace.data["run_id"])
            raise RuntimeError("saved_before_interruption")

        with mock.patch.object(QualityTrace, "record_remote_response", new=interrupt):
            with self.assertRaisesRegex(RuntimeError, "saved_before_interruption"):
                self.ask()
        source = self.source / "entry.cbl"
        source.write_text(source.read_text().replace("IN-AMOUNT * 2", "IN-AMOUNT * 3"))
        self.clock_value = 130.0
        second = self.ask(start_new_investigation=True)
        self.assertEqual(second["reason_code"], "RESUME_SOURCE_VERSION_CHANGED")
        self.assertEqual(second["agent_result"]["run_id"], saved_ids[0])
        self.assertEqual(self.calls, 1)
        self.assertFalse(self.store.load(saved_ids[0])["checkpoint"].get("superseded_by_explicit_retry"))

    def test_long_provider_timeout_does_not_disable_initial_source_discovery(self):
        def discover(request):
            if not self.calls:
                self.calls += 1
                return TransportResponse(200, json.dumps({"choices": [{"message": {
                    "content": json.dumps({"search": ["OUT-AMOUNT"]})}, "finish_reason": "stop"}]}))
            return self.answer(request)

        for timeout in (120, 300):
            with self.subTest(timeout=timeout):
                self.calls = 0
                self.config = replace(self.config, timeout_seconds=timeout)
                with mock.patch.object(business_chat, "time", self.clock):
                    result = business_chat.run_business_chat("每月结算规则的计算公式是什么？", self.database,
                        self.source, self.config, framework_reference_path="", policy=self.policy,
                        transport=discover)["agent_result"]
                self.assertEqual(self.calls, 2)
                self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", result["answer"])


if __name__ == "__main__":
    unittest.main()
