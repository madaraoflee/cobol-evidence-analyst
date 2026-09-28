from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from company_api import APIClientError, CompanyAPIConfig
from agent_policy import AgentPolicy
from live_business_eval import evaluate, main


class LiveBusinessEvalTests(unittest.TestCase):
    def test_authentication_failure_stops_before_indexing_or_sending_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            (source / "request.cbl").write_text("PROGRAM-ID. REQUEST.\n", encoding="utf-8")
            cases = root / "cases.json"
            cases.write_text(json.dumps({"cases": [{"id": "request", "question": "How is a request handled?"}]}),
                             encoding="utf-8")
            config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="local-test-secret")
            with mock.patch("live_business_eval.CompanyAPIConfig.from_env", return_value=config), \
                    mock.patch("live_business_eval.OpenAICompatibleChatClient.complete",
                               side_effect=APIClientError("HTTP_ERROR", http_status=401)), \
                    mock.patch("live_business_eval.build_business_index",
                               side_effect=AssertionError("source index should not start")):
                report = evaluate(source, root / "evaluation", cases, allow_network=True)
            self.assertEqual(report["model_preflight"]["http_status"], 401)
            self.assertEqual(report["cases"], [])
            self.assertFalse((root / "evaluation").exists())

    def test_answer_report_keeps_full_latency_and_followup_context_for_review(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            (source / "request.cbl").write_text(
                "IDENTIFICATION DIVISION.\nPROGRAM-ID. REQUEST.\nPROCEDURE DIVISION.\n"
                "MAIN.\nIF REQUEST-VALUE > 4\nMOVE 'REVIEW' TO REQUEST-STATE\nEND-IF.\nGOBACK.\n",
                encoding="utf-8")
            cases = root / "cases.json"
            cases.write_text(json.dumps({"cases": [
                {"id": "initial", "conversation": "flow", "question": "When is review needed?",
                 "expected_source_paths": ["request.cbl"], "review_checks": ["Explain the threshold"]},
                {"id": "followup", "conversation": "flow", "question": "What happens next?",
                 "expected_source_paths": ["request.cbl"]}]}), encoding="utf-8")
            config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="local-test-secret")
            history_sizes = []
            received_policies = []
            policy = AgentPolicy(max_model_requests=3)

            def answer(question, _database, _source, _config, *, history, **_options):
                history_sizes.append(len(history))
                received_policies.append(_options["policy"])
                return {"runner_status": "COMPLETED", "agent_result": {
                    "answer": "Requests above four are reviewed.",
                    "narrative": {"citations": [{"evidence_id": "ev-test", "relative_path": "request.cbl"}]},
                    "evidence_refs": [{"evidence_id": "ev-test", "relative_path": "request.cbl"}],
                    "investigation": {"selected_paths": ["request.cbl"],
                                      "business_map": {"selected_paths": ["request.cbl", "candidate.cbl"]}},
                    "metrics": {"model_requests": 1, "policy": policy.to_dict(),
                                "usage": {"status": "complete", "total_tokens": 240},
                                "tool_calls": {"search": 1, "read": 2, "framework_search": 1},
                                "request_bytes": [1200]}}}

            with mock.patch("live_business_eval.CompanyAPIConfig.from_env", return_value=config), \
                    mock.patch("live_business_eval.OpenAICompatibleChatClient.complete",
                               return_value={"choices": [{"message": {"role": "assistant", "content": "OK"}}]}), \
                    mock.patch("live_business_eval.run_business_chat", side_effect=answer):
                report = evaluate(source, root / "evaluation", cases, allow_network=True,
                                  source_format="free", policy=policy)
            self.assertEqual(report["model_preflight"]["status"], "responded")
            self.assertEqual(history_sizes, [0, 2])
            self.assertEqual(received_policies, [policy, policy])
            self.assertEqual(len(report["cases"]), 2)
            self.assertEqual(report["cases"][0]["expected_paths_cited"], ["request.cbl"])
            self.assertEqual(report["cases"][0]["expected_paths_retrieved"], ["request.cbl"])
            self.assertEqual(report["cases"][0]["navigation_source_paths"], ["candidate.cbl", "request.cbl"])
            self.assertEqual(report["cases"][0]["retrieved_source_paths"], ["request.cbl"])
            self.assertEqual(report["cases"][0]["usage"]["total_tokens"], 240)
            self.assertEqual(report["cases"][0]["tool_calls"]["framework_search"], 1)
            self.assertEqual(report["cases"][0]["request_bytes"], [1200])
            self.assertEqual(report["cases"][0]["review_checks"], ["Explain the threshold"])
            self.assertEqual(report["quality_status"], "not_reviewed")
            self.assertIsNotNone(report["latency_seconds"]["p95"])

    def test_navigation_candidates_do_not_count_as_source_supplied_to_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            cases = root / "cases.json"
            cases.write_text(json.dumps({"cases": [{"id": "candidate", "question": "Explain the rule.",
                               "expected_source_paths": ["candidate.cbl"]}]}), encoding="utf-8")
            config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="test-key")
            result = {"runner_status": "COMPLETED", "agent_result": {
                "answer": "A response for review.", "metrics": {"model_requests": 1},
                "investigation": {"business_map": {"selected_paths": ["candidate.cbl"]},
                                  "selected_paths": ["supplied.cbl"]}}}
            with mock.patch("live_business_eval.CompanyAPIConfig.from_env", return_value=config), \
                    mock.patch("live_business_eval.OpenAICompatibleChatClient.complete",
                               return_value={"choices": [{"message": {"content": "OK"}}]}), \
                    mock.patch("live_business_eval.build_business_index"), \
                    mock.patch("live_business_eval.ensure_repository_search",
                               return_value={"snapshot_id": "test-snapshot", "indexed_files": 2}), \
                    mock.patch("live_business_eval.run_business_chat", return_value=result):
                report = evaluate(source, root / "evaluation", cases, allow_network=True, framework="")
            case = report["cases"][0]
            self.assertEqual(case["expected_paths_navigated"], ["candidate.cbl"])
            self.assertEqual(case["retrieved_source_paths"], ["supplied.cbl"])
            self.assertEqual(case["expected_paths_retrieved"], [])
            self.assertEqual(case["expected_paths_cited"], [])
            self.assertEqual(report["quality_status"], "not_reviewed")

    def test_plan_reports_bounds_without_any_model_calls_or_index_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            cases = root / "cases.json"
            cases.write_text(json.dumps({"cases": [
                {"id": "one", "question": "Initial question"},
                {"id": "two", "question": "Follow-up question"}]}), encoding="utf-8")
            config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="test-key",
                                      max_output_tokens=512)
            with mock.patch("live_business_eval.CompanyAPIConfig.from_env", return_value=config), \
                    mock.patch("live_business_eval.OpenAICompatibleChatClient",
                               side_effect=AssertionError("model client must not be constructed")), \
                    mock.patch("live_business_eval.build_business_index",
                               side_effect=AssertionError("source must not be indexed")):
                report = evaluate(source, root / "evaluation", cases, allow_network=True, plan=True,
                                  policy={"max_model_requests": 2, "max_request_bytes": 48000})
            self.assertEqual(report["model_preflight"]["status"], "planned")
            self.assertEqual(report["plan"]["case_count"], 2)
            self.assertEqual(report["plan"]["max_model_requests"], 5)
            self.assertEqual(report["plan"]["max_request_bytes"], 48000)
            self.assertEqual(report["plan"]["max_output_tokens_per_request"], 512)
            self.assertEqual(report["plan"]["timeout_seconds_per_request"], config.timeout_seconds)
            self.assertFalse(report["plan"]["matches_workbench_defaults"])
            self.assertEqual(report["plan"]["configuration_scope"], "evaluation_runner")
            self.assertEqual(report["plan"]["workbench_defaults"], {"timeout_seconds": 60.0, "max_output_tokens": 2048})
            self.assertEqual(report["plan"]["preflight_max_output_tokens"], 32)
            self.assertIsNone(report["plan"]["currency_estimate"])
            self.assertEqual(report["plan"]["spend_authorization"], "not_granted_by_request_budget")
            self.assertEqual(report["cases"], [])
            self.assertFalse((root / "evaluation").exists())

    def test_network_is_disabled_by_default_and_cli_dry_run_uses_policy_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            cases = root / "cases.json"
            cases.write_text(json.dumps({"cases": [{"id": "one", "question": "Explain the flow"}]}), encoding="utf-8")
            settings = root / "settings.json"
            settings.write_text(json.dumps({"version": 1, "agent": {"max_model_requests": 2}}), encoding="utf-8")
            config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="test-key")
            with mock.patch("live_business_eval.CompanyAPIConfig.from_env", return_value=config), \
                    mock.patch("live_business_eval.OpenAICompatibleChatClient",
                               side_effect=AssertionError("network must stay disabled")), mock.patch("builtins.print"):
                report = evaluate(source, root / "evaluation", cases)
                code = main(["--source", str(source), "--evaluation-dir", str(root / "evaluation"),
                             "--cases", str(cases), "--dry-run", "--agent-settings", str(settings)])
            self.assertEqual(report["model_preflight"]["status"], "network_not_enabled")
            self.assertEqual(code, 0)
            saved = json.loads((root / "evaluation" / "live-business-eval.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["plan"]["max_model_requests"], 3)
            self.assertEqual(saved["plan"]["max_model_requests_per_case"], 2)


if __name__ == "__main__":
    unittest.main()
