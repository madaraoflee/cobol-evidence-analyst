from __future__ import annotations

import copy
from contextlib import contextmanager, ExitStack
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import desktop_live_acceptance as native
from live_business_eval import _fingerprint


class NativeLiveAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "entry.cbl").write_bytes(b"COMPUTE RESULT = INPUT * 2.\n")
        self.source_hash = native.digest(self.source / "entry.cbl")
        self.item = {"id": "formula", "question": "How is RESULT computed?", "gold": {
            "required_findings": [{"id": "formula-rule", "supporting_ranges": [
                {"path": "entry.cbl", "source_sha256": self.source_hash, "start_line": 1, "end_line": 1}]}],
            "forbidden_claims": [{"id": "invented"}]}, "reviewed_context": {"source_ranges": [
                {"path": "entry.cbl", "sha256": self.source_hash, "start_line": 1, "end_line": 1}]}}
        self.cases = self.root / "cases.json"
        regression = {key: value for key, value in self.item.items() if key != "question"}
        regression.update(split="regression", turns=[{"id": str(index), "question": question} for index, question in enumerate(
            ("How is RESULT computed?", "Why does it become zero?", "What about the second condition?"), 1)])
        self.items = [regression, {**copy.deepcopy(self.item), "id": "reserved", "split": "holdout"}]
        self.cases.write_text(json.dumps({"cases": self.items}), encoding="utf-8")
        self.runtime = {"source_commit": "a" * 40, "source_dirty": False,
                        "archive_sha256": "b" * 64, "binary_sha256": "c" * 64,
                        "execution_surface": "native_windows_http", "runtime_platform": "Windows"}
        journal = patch.object(native, "read_journal", return_value={"requests": [
            {"run_id": "native-run", "state": "completed", "request_hash": "f" * 64}]})
        self.journal = journal.start()
        self.addCleanup(journal.stop)

    def evaluate(self, **options):
        with patch.object(native, "inspect_package", return_value=self.runtime), \
             patch.object(native, "NativeWorkbench") as workbench:
            result = native.evaluate_native(archive=self.root / "package.zip", source_commit="a" * 40,
                source=self.source, cases=self.cases, output=self.root / "evaluation", **options)
            workbench.assert_not_called()
            return result

    def test_default_and_plan_cannot_start_exe_or_model(self):
        report = self.evaluate()
        self.assertEqual(report["status"], "planned")
        self.assertEqual(report["cases"], [])
        self.assertEqual(report["plan"]["preflight_model_requests"], 0)
        self.assertEqual(report["plan"]["user_turn_count"], 4)
        self.assertEqual(set(report["plan"]["office_required_checks"]), set(native.OFFICE_BASELINE_CHECKS))
        self.assertFalse((self.root / "evaluation/private-profile").exists())

    def test_unfrozen_or_changed_gold_fails_before_any_model(self):
        for invalid in ("missing_ranges", "changed_source", "unfrozen_range"):
            with self.subTest(invalid=invalid), patch.object(native, "NativeWorkbench") as workbench:
                items = copy.deepcopy(self.items)
                item = items[0]
                if invalid == "missing_ranges":
                    item["gold"]["required_findings"][0].pop("supporting_ranges")
                elif invalid == "changed_source":
                    item["reviewed_context"]["source_ranges"][0]["sha256"] = "f" * 64
                else:
                    item["gold"]["required_findings"][0]["supporting_ranges"][0].update(start_line=999, end_line=1000)
                self.cases.write_text(json.dumps({"cases": items}), encoding="utf-8")
                with self.assertRaises((AssertionError, ValueError)):
                    self.evaluate(allow_network=True, request_budget=5)
                workbench.assert_not_called()

    def test_single_easy_question_cannot_start_formal_model_acceptance(self):
        self.cases.write_text(json.dumps({"cases": [self.item]}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "CASE_SCOPE_INCOMPLETE"):
            self.evaluate(allow_network=True, request_budget=5)

    def test_remote_execution_requires_windows_and_explicit_budget(self):
        with patch.object(native.sys, "platform", "win32"), self.assertRaisesRegex(ValueError, "BUDGET"):
            self.evaluate(allow_network=True, request_budget=4)

    def test_loopback_fake_profile_cannot_claim_real_model_provenance(self):
        for url in ("https://127.0.0.1:1234/v1", "https://localhost/v1", "https://[::1]/v1"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "REMOTE_MODEL_PROFILE"):
                native.require_remote_profile({"COMPANY_API_BASE_URL": url})
        native.require_remote_profile({"COMPANY_API_BASE_URL": "https://model.example.test/v1"})

    def observation(self):
        history = [{"role": "user", "content": "Previous real question"},
                   {"role": "assistant", "content": "Previous actual answer"}]
        answer = "RESULT = INPUT * 2.[ev1]"
        trace = {"run_id": "native-run", "prompt_version": "business-chat-test",
            "configuration": {"policy": {"max_model_requests": 1},
                "model_fingerprint": hashlib.sha256(b"evaluation-model").hexdigest()},
            "final": {"final_answer_round_ids": ["round-1"]}, "rounds": [{"round_id": "round-1",
                "system_prompt_sha256": "e" * 64, "request_body_sha256": "f" * 64,
                "context": {"history": history}, "sources": [{"evidence_id": "ev1", "path": "entry.cbl",
                    "source_sha256": self.source_hash, "start_line": 1, "end_line": 1,
                    "excerpt_sha256": "d" * 64}], "framework": []}]}
        path = self.root / "trace.json"
        path.write_text(json.dumps(trace), encoding="utf-8")
        return {"job": {"status": "COMPLETED"}, "elapsed_seconds": 1.25,
            "state": {"conversation": {"messages": [{"content": answer}]}}, "result": {"runner_status": "READY",
                "agent_result": {"answer": answer, "model_answer_recorded": True, "status": "PARTIAL",
                    "metrics": {"quality_trace_path": str(path), "model_requests": 1}}}}, trace, history

    def test_identity_comes_from_actual_trace_not_current_python_prompt(self):
        observation, trace, history = self.observation()
        row = native.live_row(observation, {**self.item, "turn_id": "1", "conversation": "flow"},
                              self.runtime, native.digest(self.cases), "evaluation-model")
        self.assertEqual(row["history"], history)
        identity = row["review_identity"]
        self.assertEqual(identity["prompt"]["system_sha256"], "e" * 64)
        self.assertEqual(identity["history_sha256"], _fingerprint(history))
        self.assertEqual(row["measurement"]["elapsed_seconds"], 1.25)
        self.assertEqual(row["quality_status"], "not_reviewed")
        self.assertEqual(row["agent_status"], "PARTIAL")

    def test_missing_actual_prompt_history_or_visible_answer_rejects_identity(self):
        for field in ("prompt", "history", "visible", "model"):
            with self.subTest(field=field):
                observation, trace, _ = self.observation()
                if field == "prompt":
                    trace["rounds"][0].pop("system_prompt_sha256")
                elif field == "history":
                    trace["rounds"][0].pop("context")
                elif field == "visible":
                    observation["state"]["conversation"]["messages"][0]["content"] = "Other answer"
                else:
                    trace["configuration"]["model_fingerprint"] = "0" * 64
                Path(observation["result"]["agent_result"]["metrics"]["quality_trace_path"]).write_text(
                    json.dumps(trace), encoding="utf-8")
                with self.assertRaises((ValueError, AssertionError)):
                    native.live_row(observation, {**self.item, "turn_id": "1", "conversation": "flow"},
                                    self.runtime, native.digest(self.cases), "evaluation-model")

    def test_remote_uncertainty_is_read_from_actual_request_journal(self):
        observation, _, _ = self.observation()
        observation["result"]["reason_code"] = "REMOTE_REQUEST_OUTCOME_UNKNOWN"
        self.journal.return_value["requests"][0]["state"] = "started"
        row = native.live_row(observation, {**self.item, "turn_id": "1", "conversation": "flow"},
                              self.runtime, native.digest(self.cases), "evaluation-model")
        self.assertTrue(row["remote_result_unknown"])
        self.assertEqual(row["reason_code"], "REMOTE_REQUEST_OUTCOME_UNKNOWN")
        self.journal.return_value["requests"][0]["request_hash"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "JOURNAL_MISMATCH"):
            native.live_row(observation, {**self.item, "turn_id": "1", "conversation": "flow"},
                            self.runtime, native.digest(self.cases), "evaluation-model")

    @contextmanager
    def automatic_execution(self, *, unknown_revision=False, elapsed_seconds=1.25):
        """Exercise the paid-suite orchestration with local trace/journal records."""
        profile = self.root / "model.env"
        profile.write_bytes(b"COMPANY_API_BASE_URL=https://model.example.test/v1\n"
                            b"COMPANY_API_KEY=offline-test-credential\nCOMPANY_CHAT_MODEL=evaluation-model\n")
        binary = self.root / "offline-executable"
        binary.write_bytes(b"offline-executable-placeholder")
        runtime = {**self.runtime, "binary_sha256": native.digest(binary)}
        with ExitStack() as stack:
            stack.enter_context(patch.object(native.sys, "platform", "win32"))
            stack.enter_context(patch.object(native, "inspect_package", return_value=runtime))
            stack.enter_context(patch.object(native, "extract_package", return_value=binary))
            workbench_class = stack.enter_context(patch.object(native, "NativeWorkbench"))
            workbench = workbench_class.return_value.__enter__.return_value
            question_count = 0

            def analyze(source, workspace, question=None, **options):
                nonlocal question_count
                if question is None:
                    workspace.mkdir()
                    (workspace / "diagnosis.json").write_text(json.dumps({
                        "scope": {"file_count": 1, "total_scope_bytes": 29}, "program_count": 1,
                        "repository_search": {"indexed_files": 1, "content_hash_verified": True},
                        "build_report": {"snapshot_id": "frozen-index"}}), encoding="utf-8")
                    return {"job": {"status": "COMPLETED"}}
                question_count += 1
                if options.get("timeout", 0) < elapsed_seconds:
                    raise TimeoutError("Acceptance watchdog interrupted a model response")
                observation, trace, _ = self.observation()
                observation["elapsed_seconds"] = elapsed_seconds
                run_id = "native-run-" + str(question_count)
                trace["run_id"] = run_id
                trace["analysis_revision"] = "frozen-index"
                trace["configuration"]["policy"] = {"max_model_requests": 3, "max_question_seconds": 120}
                supplied = {**trace["rounds"][0]["sources"][0],
                            "excerpt": "COMPUTE RESULT = INPUT * 2."}
                supplied["excerpt_sha256"] = hashlib.sha256(supplied["excerpt"].encode()).hexdigest()
                trace["rounds"][0]["sources"] = [supplied]
                trace["rounds"][0]["context"]["sources"] = [supplied]
                trace["question_investigation_rounds"] = [{"round_id": "round-1", "status": "source_supported"}]
                agent = observation["result"]["agent_result"]
                agent["claim_ledger"] = {"supported_claim_count": 1, "unsupported_claim_count": 0}
                observation["state"]["conversation"]["id"] = options.get("conversation_id") or "conversation-" + str(question_count)
                requests = [{"run_id": run_id, "state": "completed", "request_hash": "f" * 64}]
                if unknown_revision:
                    agent["metrics"]["model_requests"] = 2
                    agent["stop_reason"] = "REQUEST_TIMEOUT"
                    agent["boundaries"] = [{"reason": "usable_draft_retained", "followup_error": "REQUEST_TIMEOUT"}]
                    observation["result"]["reason_code"] = "REQUEST_TIMEOUT"
                    requests.append({"run_id": run_id, "state": "started", "request_hash": "a" * 64})
                    trace["rounds"].append({"round_id": "round-2", "request_body_sha256": "a" * 64})
                    trace["question_investigation_rounds"].append({"round_id": "round-2", "status": "revision_pending"})
                self.journal.return_value = {"requests": requests}
                agent["metrics"]["quality_trace_path"] = str(self.root / f"trace-{question_count}.json")
                Path(agent["metrics"]["quality_trace_path"]).write_text(json.dumps(trace), encoding="utf-8")
                return observation

            workbench.analyze.side_effect = analyze
            yield workbench, {"archive": self.root / "package.zip", "source_commit": "a" * 40,
                "source": self.source, "cases": self.cases, "output": self.root / "evaluation",
                "profile_config": profile, "allow_network": True, "request_budget": 12,
                "max_model_requests": 3, "max_question_seconds": 120}

    def test_unknown_revision_retains_answer_and_stops_all_later_paid_turns(self):
        with self.automatic_execution(unknown_revision=True) as (workbench, options):
            with self.assertRaisesRegex(ValueError, "LIVE_REMOTE_REQUEST_OUTCOME_UNKNOWN"):
                native.evaluate_native(**options)
            self.assertEqual(workbench.analyze.call_count, 2, "Only local indexing and the first question may run")
            self.assertEqual(workbench.analyze.call_args_list[1].args[2], self.items[0]["turns"][0]["question"])
        report = json.loads((options["output"] / "LIVE-ACCEPTANCE.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "interrupted_attention_required")
        self.assertEqual(report["observed_model_requests"], 2)
        self.assertTrue(report["remote_result_unknown"])
        self.assertEqual(len(report["cases"]), 1)
        row = report["cases"][0]
        self.assertEqual(row["answer"], "RESULT = INPUT * 2.[ev1]")
        self.assertTrue(row["remote_result_unknown"])
        self.assertEqual(row["reason_code"], "REQUEST_TIMEOUT", "The journal, not an error-name guess, stops execution")
        identity, diagnostics = row["review_identity"], row["acceptance_diagnostics"]
        self.assertEqual(identity["answer_sha256"], hashlib.sha256(row["answer"].encode()).hexdigest())
        self.assertEqual(identity["source_versions"], [self.source_hash])
        self.assertEqual(identity["acceptance_diagnostics_sha256"], _fingerprint(diagnostics))
        self.assertEqual(diagnostics["question_investigation_rounds"][-1]["status"], "revision_pending")
        self.assertEqual(diagnostics["claim_ledger"]["supported_claim_count"], 1)
        self.assertEqual(row["actual_evidence_context"]["sources"][0]["excerpt"], "COMPUTE RESULT = INPUT * 2.")
        self.assertFalse((options["output"] / "HUMAN-REVIEW-TEMPLATE.json").exists())

    def test_completed_requests_continue_once_per_turn_and_keep_conversation(self):
        with self.automatic_execution() as (workbench, options):
            report = native.evaluate_native(**options)
            self.assertEqual(workbench.analyze.call_count, 5, "One local index plus each of the four turns once")
            paid = workbench.analyze.call_args_list[1:]
            self.assertEqual([call.args[2] for call in paid],
                             [turn["question"] for item in self.items for turn in native._turns(item)])
            self.assertEqual([call.kwargs["conversation_id"] for call in paid],
                             [None, "conversation-1", "conversation-1", None])
        self.assertEqual(report["status"], "completed_not_reviewed")
        self.assertEqual(report["observed_model_requests"], 4)
        self.assertFalse(report["remote_result_unknown"])
        self.assertEqual(len(report["cases"]), 4)
        template = json.loads((options["output"] / "HUMAN-REVIEW-TEMPLATE.json").read_text(encoding="utf-8"))
        self.assertEqual(len(template["reviews"]), 4)
        self.assertTrue(all(finding["result"] is None for row in template["reviews"] for finding in row["required_findings"]))

    def test_wait_budget_uses_actual_workbench_transport_profile_not_dotenv_guesses(self):
        from agent_policy import AgentPolicy
        from model_profiles import CompanyAPIConfig, model_config
        with patch.object(CompanyAPIConfig, "from_env") as configuration:
            model_config("workbench")
        actual = configuration.call_args.kwargs["timeout_seconds"]
        wait = native.execution_wait_policy(AgentPolicy(max_model_requests=3))
        self.assertEqual(wait["transport_timeout_seconds"], actual)
        self.assertEqual(actual, native.WORKBENCH_TIMEOUT_SECONDS)
        self.assertEqual(wait["watchdog_seconds"], 3 * actual + wait["local_processing_reserve_seconds"] + wait["verification_cleanup_reserve_seconds"])
        self.assertFalse(wait["application_hard_deadline_expected"])
        self.assertIs(wait["performance_target_interrupts_answer"], False)

    def test_external_watchdog_stays_finite_without_product_local_timer(self):
        from agent_policy import AgentPolicy
        for policy in (AgentPolicy(), AgentPolicy(max_model_requests=6, delivery_target_seconds=1)):
            with self.subTest(policy=policy.to_dict()):
                wait = native.execution_wait_policy(policy)
                self.assertEqual(wait["maximum_model_requests"], policy.max_model_requests)
                self.assertEqual(wait["local_processing_reserve_seconds"], 135)
                self.assertEqual(wait["verification_cleanup_reserve_seconds"], 45)
                self.assertEqual(wait["watchdog_seconds"],
                                 policy.max_model_requests * native.WORKBENCH_TIMEOUT_SECONDS + 135 + 45)
                self.assertTrue(math.isfinite(wait["watchdog_seconds"]))
                if policy.delivery_target_seconds is not None:
                    self.assertGreater(wait["watchdog_seconds"], policy.delivery_target_seconds)
                self.assertIsNone(policy.max_local_analysis_seconds)
                self.assertIsNone(policy.hard_question_seconds)
                self.assertIsNone(wait["hard_question_seconds"])
                self.assertIs(wait["application_hard_deadline_expected"], False)
                self.assertIs(wait["performance_target_interrupts_answer"], False)
        explicit = native.execution_wait_policy(AgentPolicy(max_local_analysis_seconds=7))
        self.assertEqual(explicit["local_processing_reserve_seconds"], 127)

    def test_stricter_deployment_time_limits_also_bound_acceptance_watchdog(self):
        from agent_policy import AgentPolicy
        fast = native.execution_wait_policy(AgentPolicy(max_question_seconds=1, max_model_requests=3))
        slow = native.execution_wait_policy(AgentPolicy(max_question_seconds=300,
            hard_question_seconds=300, max_model_requests=3))
        self.assertEqual(fast["watchdog_seconds"], 1 + fast["verification_cleanup_reserve_seconds"])
        self.assertEqual(slow["watchdog_seconds"], 300 + slow["verification_cleanup_reserve_seconds"])
        limited = native.execution_wait_policy(AgentPolicy(max_question_seconds=120,
            max_model_requests=3, hard_question_seconds=10))
        self.assertEqual(limited["watchdog_seconds"], 10 + limited["verification_cleanup_reserve_seconds"])

    def test_answer_at_hard_deadline_is_archived_with_separate_cleanup_watchdog(self):
        with self.automatic_execution(elapsed_seconds=120) as (workbench, options):
            report = native.evaluate_native(**options)
            self.assertEqual(workbench.analyze.call_count, 5)
            paid = workbench.analyze.call_args_list[1:]
            self.assertTrue(all(call.kwargs["timeout"] == 165 for call in paid))
        self.assertEqual(report["status"], "completed_not_reviewed")
        self.assertEqual(len(report["cases"]), 4)
        self.assertEqual(report["latency_seconds"]["p95"], 120)
        self.assertEqual(report["plan"]["policy"]["max_question_seconds"], 120)
        self.assertEqual(report["plan"]["policy"]["hard_question_seconds"], 120)
        self.assertIs(report["plan"]["execution_wait"]["performance_target_interrupts_answer"], False)
        self.assertEqual(report["quality_status"], "not_reviewed")

    def test_watchdog_captures_uncertain_request_and_unverified_draft_then_stops_suite(self):
        from run_store import RunStore
        with self.automatic_execution() as (workbench, options):
            original = workbench.analyze.side_effect

            def stalled(source, workspace, question=None, **kwargs):
                if question is None:
                    return original(source, workspace, question, **kwargs)
                store = RunStore(workspace / "investigation-runs.sqlite")
                store.create("stalled-run", {})
                store.start_request("stalled-run", "round-2", "f" * 64)
                store.checkpoint("stalled-run", {"request_payload": {"draft_answer": "Previously received draft."}})
                self.journal.return_value = {"requests": [{"run_id": "stalled-run", "request_id": "round-2",
                    "request_hash": "f" * 64, "state": "started"}]}
                raise TimeoutError("Acceptance safety watchdog")

            workbench.analyze.side_effect = stalled
            with self.assertRaisesRegex(TimeoutError, "safety watchdog"):
                native.evaluate_native(**options)
            self.assertEqual(workbench.analyze.call_count, 2)
        report = json.loads((options["output"] / "LIVE-ACCEPTANCE.json").read_text())
        self.assertEqual(report["status"], "interrupted_attention_required")
        self.assertEqual(report["request_journal_status"], "captured_before_shutdown")
        self.assertTrue(report["remote_result_unknown"])
        self.assertEqual(report["observed_model_requests"], 1)
        self.assertEqual(report["cases"], [])
        self.assertEqual(report["errors"][0]["error_type"], "TimeoutError")
        draft = report["interrupted_unverified_drafts"][0]
        self.assertEqual(draft["text"], "Previously received draft.")
        self.assertEqual(draft["verification"], "unverified")
        self.assertEqual(draft["citation_authority"], "not_granted")
        self.assertFalse(draft["model_answer_recorded"])
        self.assertFalse((options["output"] / "HUMAN-REVIEW-TEMPLATE.json").exists())

    def test_missing_interruption_journal_is_unknown_and_cannot_mask_original_failure(self):
        report = {"observed_model_requests": 0, "remote_result_unknown": False}
        with patch.object(native, "read_journal", side_effect=AssertionError("Journal unavailable")):
            native._capture_interrupted_requests(report, self.root / "missing-workspace")
        self.assertEqual(report["request_journal_status"], "unavailable_attention_required")
        self.assertTrue(report["remote_result_unknown"])


if __name__ == "__main__":
    unittest.main()
