"""Source HTTP rehearsal of passive recovery and explicit retry acceptance."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.desktop_acceptance_scenarios import _checked_answer, _slow_request_diagnostic, run_scenarios


class UnknownRequestAcceptanceTests(unittest.TestCase):
    def manifest(self):
        return json.loads((ROOT / "docs/eval/desktop-joint-v1/manifest.json").read_text(encoding="utf-8"))

    def test_missing_model_answer_reports_recovery_stage_and_stop_reason_without_source(self):
        observation = {"job": {"status": "COMPLETED"}, "elapsed_seconds": 3.1,
            "result": {"agent_result": {"model_answer_recorded": False, "status": "PARTIAL",
                "stop_reason": "QUESTION_TIME_BUDGET_EXHAUSTED", "answer": "fixture answer omitted",
                "metrics": {"model_requests": 1, "timing_seconds": {"total_seconds": 3.0,
                    "provider_wait_seconds": 2.0}}}}}
        with self.assertRaises(AssertionError) as raised:
            _checked_answer(observation, SimpleNamespace(errors=[]), stage="explicit_retry_after_unknown")
        diagnostic = json.loads(str(raised.exception).split(": ", 1)[1])
        self.assertEqual(diagnostic["stage"], "explicit_retry_after_unknown")
        self.assertEqual(diagnostic["stop_reason"], "QUESTION_TIME_BUDGET_EXHAUSTED")
        self.assertEqual(diagnostic["model_requests"], 1)
        self.assertEqual(diagnostic["timing_seconds"]["provider_wait_seconds"], 2.0)
        self.assertNotIn("fixture answer omitted", str(raised.exception))

    def test_slow_precondition_reports_deadline_before_provider_receipt(self):
        workbench = SimpleNamespace(request=Mock(return_value={"status": "COMPLETED",
            "progress": {"phase": "finalizing", "elapsed_seconds": 3.1},
            "result": {"agent": {"reason_code": "QUESTION_TIME_BUDGET_EXHAUSTED",
                "agent_result": {"status": "PARTIAL", "stop_reason": "QUESTION_TIME_BUDGET_EXHAUSTED",
                    "metrics": {"model_requests": 0, "budget_stop_phase": "retrieving",
                        "timing_seconds": {"total_seconds": 3.0}}}}}}))
        provider = SimpleNamespace(requests=[], errors=[])
        diagnostic = _slow_request_diagnostic(workbench, "job", provider, elapsed_seconds=15.0)
        self.assertEqual(diagnostic["reason_code"], "DEADLINE_WITHOUT_PROVIDER_RECEIPT")
        self.assertEqual(diagnostic["provider_request_count"], 0)
        self.assertEqual(diagnostic["provider_error_count"], 0)
        self.assertEqual(diagnostic["job_status"], "COMPLETED")
        self.assertEqual(diagnostic["stop_reason"], "QUESTION_TIME_BUDGET_EXHAUSTED")
        self.assertEqual(diagnostic["model_requests"], 0)
        self.assertEqual(diagnostic["budget_stop_phase"], "retrieving")
        self.assertEqual(diagnostic["progress"]["phase"], "finalizing")
        workbench.request.assert_called_once_with("GET", "/api/jobs/job")

    def test_slow_precondition_distinguishes_provider_validation_from_no_request(self):
        provider = SimpleNamespace(requests=[{"payload": "private payload omitted"}],
            errors=["AssertionError: Supplied source version mismatch"])
        workbench = SimpleNamespace(request=Mock(return_value={"status": "COMPLETED"}))
        diagnostic = _slow_request_diagnostic(workbench, "job", provider, elapsed_seconds=15.0)
        self.assertEqual(diagnostic["reason_code"], "SYNTHETIC_PROVIDER_ERROR_BEFORE_SLOW_HEADERS")
        self.assertEqual(diagnostic["provider_request_count"], 1)
        self.assertEqual(diagnostic["provider_error_types"], {"AssertionError": 1})
        self.assertEqual(diagnostic["provider_validation_codes"], ["SOURCE_VERSION_MISMATCH"])
        self.assertNotIn("private payload omitted", json.dumps(diagnostic))
        provider.errors.clear()
        received = _slow_request_diagnostic(workbench, "job", provider, elapsed_seconds=15.0)
        self.assertEqual(received["reason_code"], "PROVIDER_RECEIVED_REQUEST_WITHOUT_SLOW_HEADERS")

    def test_slow_precondition_diagnostic_allowlists_text_and_finite_numeric_fields(self):
        secret = "private-source-path-key-and-payload"
        workbench = SimpleNamespace(request=Mock(return_value={"status": "RUNNING",
            "error": {"code": secret, "message": secret},
            "progress": {"phase": secret, "current_file": secret, "answer_preview": {"text": secret},
                "elapsed_seconds": 3.0, "model_requests": float("nan"), "retrieved_pages": secret},
            "result": {"agent": {"reason_code": secret, "agent_result": {"status": secret,
                "stop_reason": secret, "answer": secret, "metrics": {"model_requests": True,
                    "budget_stop_phase": secret, "timing_seconds": {"total_seconds": float("inf"),
                        "provider_wait_seconds": -1, "raw_response": secret}}}}}}))
        provider = SimpleNamespace(requests=[{"payload": secret}],
            errors=["AssertionError: " + secret, secret + ": " + secret])
        diagnostic = _slow_request_diagnostic(workbench, "job", provider, elapsed_seconds=float("inf"))
        self.assertNotIn(secret, json.dumps(diagnostic))
        self.assertEqual(diagnostic["provider_error_types"], {"AssertionError": 1, "OTHER": 1})
        self.assertEqual(diagnostic["provider_validation_codes"], [])
        self.assertIsNone(diagnostic["elapsed_seconds"])
        self.assertIsNone(diagnostic["model_requests"])
        self.assertIsNone(diagnostic["timing_seconds"]["total_seconds"])
        self.assertIsNone(diagnostic["progress"]["model_requests"])
        self.assertEqual(diagnostic["progress"]["phase"], "OTHER")

    def test_slow_precondition_preserves_primary_failure_when_job_diagnostic_fails(self):
        workbench = SimpleNamespace(request=Mock(side_effect=TimeoutError("private transport detail")))
        diagnostic = _slow_request_diagnostic(workbench, "job", SimpleNamespace(requests=[], errors=[]),
            elapsed_seconds=15.0)
        self.assertEqual(diagnostic["reason_code"], "WORKBENCH_JOB_DIAGNOSTIC_UNAVAILABLE")
        self.assertEqual(diagnostic["job_diagnostic_error_type"], "TimeoutError")
        self.assertNotIn("private transport detail", json.dumps(diagnostic))

    def test_missing_slow_headers_keeps_failure_and_reports_diagnostics_in_scenario_row(self):
        provider = SimpleNamespace(requests=[], errors=[])
        provider_context = MagicMock()
        provider_context.__enter__.return_value = provider
        workbench = SimpleNamespace(submit=Mock(return_value="job"), request=Mock(return_value={
            "status": "COMPLETED", "result": {"agent": {
                "reason_code": "QUESTION_TIME_BUDGET_EXHAUSTED", "agent_result": {
                    "status": "PARTIAL", "stop_reason": "QUESTION_TIME_BUDGET_EXHAUSTED"}}}}))
        workbench_context = MagicMock()
        workbench_context.__enter__.return_value = workbench
        slow = SimpleNamespace(started=SimpleNamespace(wait=Mock(return_value=False)))
        case = next(case for case in self.manifest()["cases"] if case["id"] == "deadline_unknown")
        module = "tools.desktop_acceptance_scenarios."
        with tempfile.TemporaryDirectory() as directory, \
             patch(module + "LoopbackProvider", return_value=provider_context), \
             patch(module + "NativeWorkbench", return_value=workbench_context), \
             patch(module + "SlowHeaders", return_value=slow), \
             patch(module + "write_profile", return_value={}) as profile, \
             patch(module + "_index", return_value=0.1), \
             patch(module + "_index_stamp", return_value={}):
            rows = run_scenarios(["unused-test-command"], directory,
                {"cases": [{**case, "repeats": 1}]})
        self.assertEqual(rows[0]["status"], "failed")
        self.assertFalse(any(rows[0]["checks"].values()))
        diagnostic = json.loads(rows[0]["error"].split("slow request: ", 1)[1])
        self.assertEqual(diagnostic["reason_code"], "DEADLINE_WITHOUT_PROVIDER_RECEIPT")
        slow.started.wait.assert_called_once_with(15)
        self.assertEqual(profile.call_args.args[2], {"max_question_seconds": 3, "hard_question_seconds": 3})

    def test_contract_retains_original_lifecycle_checks_samples_and_limits(self):
        manifest = self.manifest()
        self.assertEqual(len(manifest["cases"]), 25)
        self.assertEqual(sum(case["repeats"] for case in manifest["cases"]), 79)
        cases = {case["id"]: case for case in manifest["cases"]}
        required = {"lifecycle_remote_unknown_preserved", "lifecycle_no_automatic_resend",
                    "lifecycle_restart_restored", "lifecycle_unknown_isolated_between_conversations",
                    "lifecycle_explicit_retry_once_preserves_unknown"}
        for name, trigger_check, limits in (
            ("cancel_unknown", "lifecycle_cancel_prompt", {"cold_index_seconds": 60,
                "first_provider_seconds": 15, "question_seconds": 20, "cancel_seconds": 2}),
            ("deadline_unknown", "lifecycle_total_deadline_enforced", {"cold_index_seconds": 60,
                "first_provider_seconds": 2.5, "question_seconds": 8}),
        ):
            with self.subTest(case=name):
                case = cases[name]
                self.assertEqual(case["repeats"], 3)
                self.assertEqual(case["limits"], limits)
                self.assertEqual(set(case["assertions"]), required | {trigger_check})
                self.assertEqual(case["assertion_groups"]["lifecycle"], case["assertions"])

    def assert_source_http_recovery(self, name):
        case = next(case for case in self.manifest()["cases"] if case["id"] == name)
        with tempfile.TemporaryDirectory() as directory:
            rows = run_scenarios([sys.executable, str(ROOT / "poc/desktop_app.py")], directory,
                                 {"cases": [{**case, "repeats": 1}]})
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["status"], "passed", {"error": row.get("error"), "checks": row.get("checks")})
            self.assertEqual(set(row["checks"]), set(case["assertions"]))
            self.assertTrue(all(row["checks"].values()))
            self.assertEqual(row["request_counts"], {"before_restart": 1, "after_restart": 1,
                "after_history_open": 1, "after_independent_question": 2, "after_explicit_retry": 3})
            self.assertTrue(all(row["explicit_retry_checks"].values()))
            self.assertNotEqual(row["run_id"], row["explicit_retry_run_id"])
            self.assertEqual(row["explicit_independent_conversation_requests"], 1)
            self.assertEqual(row["explicit_retry_requests"], 1)
            self.assertEqual(row["remote_request_state"], "started")
            self.assertEqual(row["remote_outcome"], "unknown")
            self.assertIsNone(row["real_model_accuracy"])
            for field, limit in case["limits"].items():
                self.assertLessEqual(row["timings"][field], limit)

    def test_cancelled_request_restart_and_history_do_not_resend_but_explicit_retry_does(self):
        self.assert_source_http_recovery("cancel_unknown")

    def test_deadline_request_restart_and_history_do_not_resend_but_explicit_retry_does(self):
        self.assert_source_http_recovery("deadline_unknown")


if __name__ == "__main__":
    unittest.main()
