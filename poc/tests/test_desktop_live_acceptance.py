from __future__ import annotations

import copy
import hashlib
import json
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


if __name__ == "__main__":
    unittest.main()
