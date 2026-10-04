from __future__ import annotations

import copy
import json
import unittest
from unittest import mock

import test_web_app as web_tests
from api_error_details import build_diagnostic
from company_api import APIClientError, TransportResponse


class WebSafeAPIErrorTests(unittest.TestCase):
    def setUp(self):
        self.web = web_tests.WebAppTests(methodName="runTest")
        self.web.setUp()
        self.addCleanup(self.web.tearDown)

    def test_model_check_returns_classified_safe_error_and_ids_without_provider_text(self):
        private = "PRIVATE-CREDENTIAL-SOURCE-MARKER"
        body = json.dumps({"error": {"code": "insufficient_quota", "message": private,
                                     "request_id": "req-" + "b" * 16}})
        self.web.app.model_check_transport = lambda request: TransportResponse(429, body)
        status, result, _ = self.web.request("POST", "/api/model-check", {})
        self.assertEqual(status, 200)
        self.assertEqual(result["code"], "HTTP_ERROR")
        self.assertEqual(result["diagnostic"]["category"], "quota_exhausted")
        self.assertEqual(result["diagnostic"]["http_status"], 429)
        self.assertRegex(result["diagnostic"]["request_id"], r"^local-[a-f0-9]{32}$")
        self.assertNotIn(private, json.dumps(result))
        self.assertNotIn("body", result["diagnostic"])

    def test_worker_failure_retains_safe_metadata_and_keeps_original_project_answer(self):
        diagnostic = build_diagnostic("HTTP_ERROR", http_status=413,
            body=json.dumps({"error": {"code": "context_length_exceeded"}}))
        self.web.app.project["agent"] = {"agent_result": {"answer": "Earlier saved answer."}}
        self.web.app.analyzer = mock.Mock(side_effect=APIClientError("HTTP_ERROR", http_status=413, diagnostic=diagnostic))
        source = self.web.source("neutral-source", "DEBITFLOW")
        output = self.web.root / "output"
        # Production stores resolved paths. macOS temporary paths may alias
        # /private/var, so the fixture must use that same project identity.
        self.web.app.project.update(source=str(source.resolve()), output=str(output.resolve()))
        result = self.web.finish(self.web.submit(source, output, question="Explain the neutral debit process."))
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "ANALYSIS_FAILED")
        self.assertEqual(result["error"]["diagnostic"]["category"], "context_too_large")
        self.assertEqual(result["error"]["diagnostic"]["request_id"], diagnostic["request_id"])
        self.assertEqual(self.web.app.project["agent"]["agent_result"]["answer"], "Earlier saved answer.")

    def test_failed_question_preserves_established_project_index_and_answer(self):
        source = self.web.source("established-source", "DEBITFLOW")
        output = self.web.root / "established-output"
        initial = self.web.finish(self.web.submit(source, output))
        self.assertEqual(initial["status"], "COMPLETED")
        self.assertTrue(self.web.app.project["snapshot_id"])
        self.assertEqual(self.web.app.project["source"], str(source.resolve()))
        self.web.app.project["agent"] = {"agent_result": {"answer": "Earlier saved answer."}}
        before = copy.deepcopy(self.web.app.project)
        diagnostic = build_diagnostic("TRANSPORT_ERROR")
        self.web.app.analyzer = mock.Mock(side_effect=APIClientError("TRANSPORT_ERROR", diagnostic=diagnostic))
        result = self.web.finish(self.web.submit(source, output,
            question="Explain the neutral debit process.", allow_network=True))
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(self.web.app.analyzer.call_count, 1)
        self.assertEqual(self.web.app.project, before)
        self.assertEqual(result["error"]["diagnostic"]["request_id"], diagnostic["request_id"])
        self.assertEqual(result["error"]["diagnostic"]["category"], "connection")
        self.assertTrue((output / "structural-index.sqlite").is_file())
        self.assertEqual(self.web.app.conversation["messages"][-1]["status"], "failed")

    def test_synchronous_http_500_provides_local_safe_diagnostic_without_exception_text(self):
        private = "PRIVATE-ADAPTER-ERROR"
        with mock.patch.object(self.web.app, "framework_demo", side_effect=RuntimeError(private)), \
             self.assertLogs("web_app", level="WARNING") as logs:
            status, result, _ = self.web.request("GET", "/api/framework-demo")
        self.assertEqual(status, 500)
        self.assertEqual(result["error"]["code"], "REQUEST_FAILED")
        self.assertEqual(result["error"]["diagnostic"]["category"], "system_error")
        self.assertEqual(result["error"]["diagnostic"]["evidence_source"], "local")
        self.assertRegex(result["error"]["diagnostic"]["request_id"], r"^local-[a-f0-9]{32}$")
        self.assertNotIn(private, json.dumps(result))
        self.assertIn(result["error"]["diagnostic"]["request_id"], "\n".join(logs.output))
        self.assertNotIn(private, "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
