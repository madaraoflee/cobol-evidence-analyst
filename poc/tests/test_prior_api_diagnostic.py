"""A later local failure must not hide a previously classified API failure."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]
from api_error_details import build_diagnostic
from web_app import WorkbenchState, _unaccepted_agent
import test_web_app as fixtures


class PriorAPIDiagnosticTests(unittest.TestCase):
    def test_real_failure_save_and_reopen_preserve_current_and_prior_causes(self):
        web = fixtures.WebAppTests(methodName="runTest")
        web.setUp()
        self.addCleanup(web.tearDown)
        web.app.state_path = web.root / "settings.json"
        source = web.source("neutral-source", "ENTRY-ONE")
        output = web.root / "output"
        prior = build_diagnostic("HTTP_ERROR", http_status=500,
            body=json.dumps({"error": {"code": "rate_limit_exceeded"}}))
        synthetic = {"runner_status": "SAFE_STOP", "diagnostic": prior,
            "agent_result": {"answer": "A retained synthetic business explanation."}}

        def fail_after_model(*args, **kwargs):
            if model.call_count:
                raise RuntimeError("Synthetic local verification failure")

        with patch("analyze_source.run_investigation", return_value=synthetic) as model, \
                patch("analyze_source._verify_scope", side_effect=fail_after_model):
            result = web.finish(web.submit(source, output, question="Explain the business flow",
                allow_network=True, reading_strategy="full_chain"))
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result["status"], "COMPLETED")
        saved = json.loads((output / "agent-result.json").read_text())
        self.assertEqual(saved["diagnostic"]["category"], "system_error")
        self.assertEqual(saved["prior_diagnostic"]["category"], "rate_limit")
        conversation = result["result"]["conversation"]
        for message in [conversation["messages"][-1],
                        web.app.get_conversation(conversation["id"])["conversation"]["messages"][-1]]:
            self.assertEqual(message["diagnostic"]["category"], "system_error")
            self.assertEqual(message["prior_diagnostic"], prior)
            self.assertFalse(message["answer_available"])
            self.assertEqual(message["status"], "failed")
        reopened = WorkbenchState(state_path=web.app.state_path, config_provider=lambda: web.config)
        message = reopened.get_conversation(conversation["id"])["conversation"]["messages"][-1]
        self.assertEqual(message["prior_diagnostic"], prior)
        self.assertEqual(message["diagnostic"]["category"], "system_error")
        self.assertFalse(message["answer_available"])

    def test_unaccepted_projection_preserves_only_safe_prior_fields(self):
        current = build_diagnostic("ANALYSIS_FAILED")
        prior = build_diagnostic("HTTP_ERROR", http_status=500,
            body=json.dumps({"error": {"code": "insufficient_quota"}}))
        unsafe = {**prior, "raw_response": "PRIVATE-RESPONSE", "api_key": "PRIVATE-KEY",
                  "reason": "PRIVATE-REASON", "next_step": "PRIVATE-INSTRUCTION"}
        agent = {"diagnostic": current, "prior_diagnostic": unsafe,
                 "agent_result": {"answer": "Unaccepted draft"}}
        original = deepcopy(agent)
        projected = _unaccepted_agent(agent, "SOURCE_ANALYSIS_FAILED")
        self.assertEqual(projected["diagnostic"], current)
        self.assertEqual(projected["prior_diagnostic"], prior)
        self.assertIsNone(projected["agent_result"])
        self.assertNotIn("PRIVATE-", json.dumps(projected))
        self.assertEqual(agent, original)

    def test_invalid_prior_does_not_replace_a_valid_current_diagnostic(self):
        current = build_diagnostic("ANALYSIS_FAILED")
        for invalid in (None, "PRIVATE-RESPONSE", {"category": "invented", "reason": "PRIVATE-REASON"}):
            with self.subTest(prior=invalid):
                projected = _unaccepted_agent({"diagnostic": current, "prior_diagnostic": invalid},
                                              "SOURCE_ANALYSIS_FAILED")
                self.assertEqual(projected["diagnostic"], current)
                self.assertNotIn("prior_diagnostic", projected)


if __name__ == "__main__":
    unittest.main()
