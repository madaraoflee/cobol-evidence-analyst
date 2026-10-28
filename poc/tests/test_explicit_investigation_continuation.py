"""Continuing pending work retains answers but opens a distinct request ledger."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
import business_chat
from company_api import TransportResponse
from business_index import build_business_index
from compilation_context import CompilationContext
from repository_discovery import ensure_repository_search
import test_explicit_retry_recovery as fixtures


class ExplicitInvestigationContinuationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ExplicitRetryRecoveryTests(
            methodName="test_repeated_interruptions_never_trigger_automatic_resubmission")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.policy = AgentPolicy(max_model_requests=1, max_answer_revisions=0)

    def continue_run(self, prior, **options):
        return self.fixture.invoke(continue_investigation=True,
            resume_run_id=prior["agent_result"]["run_id"], **options)

    def assert_new_ledger(self, prior, continued):
        fixture = self.fixture
        old_id = prior["agent_result"]["run_id"]
        new_id = continued["agent_result"]["run_id"]
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(continued["agent_result"]["metrics"]["model_requests"], 1)
        saved = fixture.store.load(new_id)
        self.assertEqual(len(saved["requests"]), 1)
        self.assertEqual(saved["checkpoint"]["quality"]["continued_from_run_id"], old_id)
        return saved

    def test_completed_finite_run_continues_with_fresh_allowance_and_existing_answer(self):
        fixture = self.fixture
        prior = fixture.invoke()
        before = fixture.store.load(prior["agent_result"]["run_id"])
        self.assertEqual(before["state"], "completed")
        continued = self.continue_run(prior)
        self.assertEqual(fixture.calls[-1]["draft_answer"], prior["agent_result"]["answer"])
        self.assertTrue(continued["agent_result"]["answer_available"])
        self.assert_new_ledger(prior, continued)
        self.assertEqual(fixture.store.load(before["run_id"]), before)
        self.assertEqual(len(fixture.calls), 2)

    def test_completed_run_reconnect_still_replays_without_another_provider_attempt(self):
        fixture = self.fixture
        prior = fixture.invoke()
        replay = fixture.invoke(resume_run_id=prior["agent_result"]["run_id"])
        self.assertEqual(replay["agent_result"]["answer"], prior["agent_result"]["answer"])
        self.assertEqual(len(fixture.calls), 1)

    def test_two_continuations_keep_distinct_round_one_source_origins(self):
        fixture = self.fixture
        helper = fixture.source / "helper.cbl"
        helper.write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. SUPPORT-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 STATUS-CODE PIC 9.\n"
            "PROCEDURE DIVISION.\nMOVE 7 TO STATUS-CODE.\nGOBACK.\n", encoding="utf-8")
        fixture.rebuild()
        prior = fixture.invoke()
        old_id = prior["agent_result"]["run_id"]
        saved = fixture.store.load(old_id)
        checkpoint = deepcopy(saved["checkpoint"])
        checkpoint["frontier"] = [{"tool": "read", "arguments": {
            "relative_path": "helper.cbl", "start_line": 1,
            "end_line": len(helper.read_text().splitlines())}, "reason": "pending_source_read"}]
        checkpoint["analysis_continuation"].update(idle_tool_rounds=2, stop_reason="request_budget",
            revision_requests=2, total_revision_requests=99, reviewed_gaps=["earlier-gap"])
        fixture.store.checkpoint(old_id, checkpoint, status="completed")

        def explain_both(request):
            payload = fixture.capture(request)
            self.assertIn("draft_answer", payload)
            pages = [page for bundle in payload["source_context"] for page in bundle["pages"]]
            support = next(page for page in pages if page["relative_path"] == "helper.cbl")
            root = next(page for page in pages if page["relative_path"] == "entry.cbl")
            text = (f"OUT-AMOUNT = IN-AMOUNT * 2。[{root['evidence_id']}] "
                    f"STATUS-CODE is set to 7.[{support['evidence_id']}]")
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text},
                "finish_reason": "stop"}]}))

        first = self.continue_run(prior, transport=explain_both)
        first_saved = self.assert_new_ledger(prior, first)
        self.assertEqual(first_saved["checkpoint"]["analysis_continuation"]["total_revision_requests"], 0)
        self.assertIn("STATUS-CODE is set to 7", first["agent_result"]["answer"])
        second = self.continue_run(first, transport=explain_both)
        second_saved = self.assert_new_ledger(first, second)
        self.assertIn("STATUS-CODE is set to 7", second["agent_result"]["answer"])
        self.assertEqual(second_saved["checkpoint"]["source_memory_context"]["run_id"], old_id)
        origins = second_saved["checkpoint"]["prior_source_memory"]["origins"]
        self.assertGreaterEqual(len({origin["request_body_sha256"] for origin in origins.values()}), 2)
        self.assertEqual(len(first_saved["requests"]), 1)
        self.assertEqual(len(fixture.calls), 3)

    def test_legacy_finite_policy_can_continue_under_progress_defaults(self):
        fixture = self.fixture
        prior = fixture.invoke()
        fixture.policy = AgentPolicy()
        continued = self.continue_run(prior)
        self.assert_new_ledger(prior, continued)
        self.assertEqual(fixture.calls[-1]["draft_answer"], prior["agent_result"]["answer"])
        self.assertNotEqual(continued.get("reason_code"), "RESUME_IDENTITY_MISMATCH")

    def test_explicit_continue_does_not_restore_removed_legacy_deadline(self):
        fixture = self.fixture
        fixture.policy = AgentPolicy(max_question_seconds=180)
        prior = fixture.invoke()
        saved = fixture.store.load(prior["agent_result"]["run_id"])
        checkpoint = deepcopy(saved["checkpoint"])
        checkpoint["question_budget"].update(elapsed_seconds=999, hard_deadline_epoch_seconds=0)
        fixture.store.checkpoint(saved["run_id"], checkpoint, status="completed")
        fixture.policy = AgentPolicy()
        continued = self.continue_run(prior)
        self.assert_new_ledger(prior, continued)
        self.assertIsNone(continued["agent_result"]["metrics"]["policy"]["hard_question_seconds"])
        self.assertIsNone(continued["agent_result"]["metrics"]["delivery_target"]["hard_limit_seconds"])

    def test_earlier_pipeline_continues_with_current_prompt_and_preserved_candidate(self):
        fixture = self.fixture
        prior = fixture.invoke()
        saved = fixture.store.load(prior["agent_result"]["run_id"])
        metadata = {**saved["metadata"], "pipeline_version": "earlier-source-pipeline",
                    "prompt_version": "earlier-answer-prompt"}
        with fixture.store.connect() as database:
            database.execute("UPDATE investigation_runs SET metadata_json=? WHERE run_id=?",
                (json.dumps(metadata), saved["run_id"]))
        blocked = fixture.invoke(resume_run_id=saved["run_id"])
        self.assertEqual(blocked["reason_code"], "RESUME_PIPELINE_VERSION_CHANGED")
        self.assertEqual(len(fixture.calls), 1)
        continued = self.continue_run(prior)
        self.assertEqual(fixture.calls[-1]["draft_answer"], prior["agent_result"]["answer"])
        current = self.assert_new_ledger(prior, continued)
        self.assertEqual(current["metadata"]["pipeline_version"], business_chat.PIPELINE_VERSION)
        self.assertEqual(current["metadata"]["prompt_version"], business_chat.PROMPT_VERSION)
        self.assertTrue(continued["agent_result"]["answer_available"])

    def test_unknown_remote_outcome_retains_original_answer_without_resending(self):
        fixture = self.fixture
        prior = fixture.invoke()
        run_id = prior["agent_result"]["run_id"]
        saved = fixture.store.load(run_id)
        fixture.store.checkpoint(run_id, saved["checkpoint"], status="attention")
        fixture.store.start_request(run_id, "pending-request", "f" * 64)
        before = fixture.store.load(run_id)
        continued = self.continue_run(prior)
        self.assertEqual(continued["reason_code"], "REMOTE_REQUEST_OUTCOME_UNKNOWN")
        self.assertEqual(continued["agent_result"]["answer"], prior["agent_result"]["answer"])
        self.assertEqual(fixture.store.load(run_id), before)
        self.assertEqual(len(fixture.calls), 1)

    def test_failed_continuation_keeps_previously_received_answer(self):
        fixture = self.fixture
        prior = fixture.invoke()

        def unavailable(request):
            fixture.capture(request)
            raise TimeoutError("simulated interrupted provider")

        continued = self.continue_run(prior, transport=unavailable)
        self.assertEqual(continued["agent_result"]["answer"], prior["agent_result"]["answer"])
        self.assertTrue(continued["agent_result"]["answer_available"])
        saved = self.assert_new_ledger(prior, continued)
        self.assertTrue(saved["uncertain_requests"])
        blocked = self.continue_run(continued)
        self.assertEqual(blocked["reason_code"], "REMOTE_REQUEST_OUTCOME_UNKNOWN")
        self.assertEqual(blocked["agent_result"]["answer"], prior["agent_result"]["answer"])
        self.assertEqual(len(fixture.calls), 2)

    def test_changed_source_uses_current_source_without_copying_old_draft(self):
        fixture = self.fixture
        prior = fixture.invoke()
        fixture.source_file.write_text(fixture.source_file.read_text().replace(
            "IN-AMOUNT * 2", "IN-AMOUNT * 3"), encoding="utf-8")
        fixture.rebuild()
        continued = self.continue_run(prior)
        self.assertIn("IN-AMOUNT * 3", continued["agent_result"]["answer"])
        self.assertNotIn("draft_answer", fixture.calls[-1])
        self.assert_new_ledger(prior, continued)

    def test_removed_old_source_path_uses_current_location_without_waiting(self):
        fixture = self.fixture
        prior = fixture.invoke()
        fixture.source_file.rename(fixture.source / "current-entry.cbl")
        fixture.rebuild()
        continued = self.continue_run(prior)
        self.assertTrue(continued["agent_result"]["answer_available"])
        self.assertNotIn("draft_answer", fixture.calls[-1])
        self.assert_new_ledger(prior, continued)

    def test_changed_compilation_settings_start_current_investigation(self):
        fixture = self.fixture
        prior = fixture.invoke()
        context = CompilationContext(source_format="free", framework_version="R2.0",
                                     compile_conditions=(("FEATURE", True),))
        build_business_index(fixture.source, fixture.database, source_format="free",
                             verify_content=True, compilation_context=context)
        ensure_repository_search(fixture.database, fixture.source)
        continued = self.continue_run(prior)
        self.assertTrue(continued["agent_result"]["answer_available"])
        self.assertNotIn("draft_answer", fixture.calls[-1])
        self.assert_new_ledger(prior, continued)

    def test_truncated_answer_continuation_appends_instead_of_replacing_received_text(self):
        fixture = self.fixture

        def truncated(request):
            payload = fixture.capture(request)
            page = next(page for bundle in payload["source_context"] for page in bundle["pages"]
                        if page["relative_path"] == "entry.cbl")
            text = f"OUT-AMOUNT = IN-AMOUNT * 2。[{page['evidence_id']}]\n\nFurther explanation:"
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text},
                "finish_reason": "length"}]}))

        prior = fixture.invoke(transport=truncated)
        self.assertTrue(prior["agent_result"]["answer_truncated"])

        def finish(request):
            payload = fixture.capture(request)
            self.assertTrue(payload.get("draft_continuation"))
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "content": " the initial input value is 4."}, "finish_reason": "stop"}]}))

        continued = self.continue_run(prior, transport=finish)
        self.assertIn(prior["agent_result"]["answer"], continued["agent_result"]["answer"])
        self.assertIn("the initial input value is 4", continued["agent_result"]["answer"])
        self.assert_new_ledger(prior, continued)


if __name__ == "__main__":
    unittest.main()
