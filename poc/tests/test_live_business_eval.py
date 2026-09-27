from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from company_api import APIClientError, CompanyAPIConfig
from live_business_eval import evaluate


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

            def answer(question, _database, _source, _config, *, history, **_options):
                history_sizes.append(len(history))
                return {"runner_status": "COMPLETED", "agent_result": {
                    "answer": "Requests above four are reviewed.",
                    "narrative": {"citations": [{"evidence_id": "ev-test", "relative_path": "request.cbl"}]},
                    "evidence_refs": [{"evidence_id": "ev-test", "relative_path": "request.cbl"}],
                    "investigation": {"business_map": {"selected_paths": ["request.cbl"]}},
                    "metrics": {"model_requests": 1}}}

            with mock.patch("live_business_eval.CompanyAPIConfig.from_env", return_value=config), \
                    mock.patch("live_business_eval.OpenAICompatibleChatClient.complete",
                               return_value={"choices": [{"message": {"role": "assistant", "content": "OK"}}]}), \
                    mock.patch("live_business_eval.run_business_chat", side_effect=answer):
                report = evaluate(source, root / "evaluation", cases, allow_network=True, source_format="free")
            self.assertEqual(report["model_preflight"]["status"], "responded")
            self.assertEqual(history_sizes, [0, 2])
            self.assertEqual(len(report["cases"]), 2)
            self.assertEqual(report["cases"][0]["expected_paths_cited"], ["request.cbl"])
            self.assertEqual(report["cases"][0]["review_checks"], ["Explain the threshold"])
            self.assertEqual(report["quality_status"], "not_reviewed")
            self.assertIsNotNone(report["latency_seconds"]["p95"])


if __name__ == "__main__":
    unittest.main()
