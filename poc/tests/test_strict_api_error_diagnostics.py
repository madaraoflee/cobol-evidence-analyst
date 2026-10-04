from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_loop import BoundedAgentLoop  # noqa: E402
from analyze_source import analyze_source  # noqa: E402
from api_diagnostics import APIResponseDiagnostics  # noqa: E402
from company_api import (  # noqa: E402
    APIClientError,
    APIConfigurationError,
    CompanyAPIConfig,
    OpenAICompatibleChatClient,
)
from run_agent import _not_ready, run_investigation  # noqa: E402
from structural_index import build_structural_index  # noqa: E402
from test_agent_loop import FakeClient, FakeTools, json_action  # noqa: E402
from test_run_agent import AgentReadyTransport, response  # noqa: E402


REFLECTED_CONTENT = "MOVE PRIVATE-RECORD TO INTERNAL-OUTPUT. secret-key-canary"


def config():
    return CompanyAPIConfig(
        "https://service.example/v1", "test-model", api_key="test-only-api-key",
    )


class FailedRequestTransport(AgentReadyTransport):
    def __init__(self, *, fail_probes=False):
        super().__init__()
        self.fail_probes = fail_probes

    def __call__(self, request):
        payload = json.loads(request.body or b"{}")
        if self.fail_probes:
            self.requests.append(request)
            if request.endpoint == "models":
                return response(404, {"error": {"message": REFLECTED_CONTENT}})
            return response(429, {"error": {
                "code": "insufficient_quota", "message": REFLECTED_CONTENT,
            }})
        if any(item.get("role") == "system" for item in payload.get("messages", [])):
            self.requests.append(request)
            return response(500, {"error": {
                "code": "context_length_exceeded", "message": REFLECTED_CONTENT,
            }})
        return super().__call__(request)


class StrictAPIErrorDiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temporary.name) / "strict.sqlite"
        build_structural_index(
            POC_ROOT / "fixtures" / "synthetic-insurance-v1", cls.database, quiet=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_loop_preserves_safe_failure_and_makes_only_one_model_request(self):
        transport = FailedRequestTransport()
        client = OpenAICompatibleChatClient(config(), transport=transport)
        result = BoundedAgentLoop(client, FakeTools()).run("Explain the amount")
        self.assertEqual(result["stop_reason"], "model_client_error")
        diagnostic = result["diagnostic"]
        self.assertEqual(diagnostic["http_status"], 500)
        self.assertEqual(result["diagnostics"][0]["diagnostic"], diagnostic)
        self.assertEqual(result["diagnostics"][0]["stage"], "model_request")
        self.assertIn(diagnostic["reason"], result["answer"])
        self.assertIn(diagnostic["request_id"], result["answer"])
        self.assertEqual(result["model_turns"], 1)
        self.assertEqual(len(transport.requests), 1)
        self.assertNotIn(REFLECTED_CONTENT, json.dumps(result))
        self.assertNotIn("test-only-api-key", json.dumps(result))

    def test_loop_reprojects_error_details_before_returning_them(self):
        error = APIClientError("HTTP_ERROR", http_status=500)
        error.diagnostic.update(
            reason=REFLECTED_CONTENT, next_step=REFLECTED_CONTENT,
            raw_response=REFLECTED_CONTENT,
        )
        client = mock.Mock()
        client.complete.side_effect = error
        result = BoundedAgentLoop(client, FakeTools()).run("Explain the amount")
        self.assertEqual(result["diagnostic"]["http_status"], 500)
        self.assertNotIn(REFLECTED_CONTENT, json.dumps(result))
        self.assertNotIn("raw_response", json.dumps(result))
        client.complete.assert_called_once()

    def test_unknown_client_exception_remains_a_safe_single_stop(self):
        client = mock.Mock()
        client.complete.side_effect = RuntimeError(REFLECTED_CONTENT)
        result = BoundedAgentLoop(client, FakeTools()).run("Explain the amount")
        self.assertEqual(result["stop_reason"], "model_client_error")
        self.assertEqual(result["diagnostic"]["category"], "system_error")
        self.assertEqual(result["diagnostic"]["request_id_source"], "local")
        self.assertNotIn(REFLECTED_CONTENT, json.dumps(result))
        client.complete.assert_called_once()

    def test_nested_error_capture_is_omitted_without_erasing_prior_response(self):
        collector = APIResponseDiagnostics(protected_values=())
        collector.record(
            phase="investigation", endpoint="chat/completions", http_status=200,
            outcome_code="RESPONSE_RECEIVED", body=b"Prior successful response.", elapsed_ms=1,
        )
        requests = []

        def transport(request):
            requests.append(request)
            return response(200, {"choices": [{"message": {
                "role": "assistant", "content": json.dumps({"error": {
                    "code": "insufficient_quota", "message": REFLECTED_CONTENT,
                }}),
            }}]})

        client = OpenAICompatibleChatClient(config(), transport=transport, diagnostics=collector)
        result = BoundedAgentLoop(client, FakeTools()).run("Explain the amount")
        self.assertEqual(result["stop_reason"], "model_protocol_error")
        self.assertEqual(result["diagnostic"]["category"], "invalid_response")
        capture = collector.to_dict()
        self.assertEqual(capture["exchanges"][0]["body_text"], "Prior successful response.")
        self.assertEqual(capture["exchanges"][-1]["body_text"], "")
        self.assertEqual(capture["exchanges"][-1]["body_omitted_reason"], "ERROR_RESPONSE_BODY_OMITTED")
        self.assertNotIn(REFLECTED_CONTENT, json.dumps({"result": result, "capture": capture}))
        self.assertEqual(len(requests), 1)

    def test_nested_error_in_plain_injected_client_stops_safely(self):
        result = BoundedAgentLoop(FakeClient([{"choices": [{"message": {
            "content": json.dumps({"error": {"message": REFLECTED_CONTENT}}),
        }}]}]), FakeTools()).run("Explain the amount")
        self.assertEqual(result["stop_reason"], "model_protocol_error")
        self.assertEqual(result["diagnostic"]["category"], "invalid_response")
        self.assertNotIn(REFLECTED_CONTENT, json.dumps(result))

    def test_unrecognized_protocol_body_is_omitted_after_an_accepted_tool_response(self):
        bodies = (
            {"detail": REFLECTED_CONTENT},
            {"choices": [{"message": {"content": json.dumps({"detail": REFLECTED_CONTENT})}}]},
            {"choices": [{"message": {"content": "```json\n" + json.dumps({"error": {
                "message": REFLECTED_CONTENT,
            }}) + "\n```"}}]},
        )
        for body in bodies:
            with self.subTest(body_keys=sorted(body)):
                collector = APIResponseDiagnostics(protected_values=())
                requests = []

                def transport(request):
                    requests.append(request)
                    if len(requests) == 1:
                        return response(200, json_action("search_code", {"query": "OUT-AMOUNT"}))
                    return response(200, body)

                client = OpenAICompatibleChatClient(config(), transport=transport, diagnostics=collector)
                result = BoundedAgentLoop(client, FakeTools()).run("Explain the amount")
                self.assertEqual(result["stop_reason"], "model_protocol_error")
                self.assertEqual(result["tool_calls_used"], 1)
                self.assertEqual(len(requests), 2)
                capture = collector.to_dict()
                self.assertIn("search_code", capture["exchanges"][0]["body_text"])
                self.assertEqual(capture["exchanges"][-1]["body_text"], "")
                self.assertEqual(capture["exchanges"][-1]["body_omitted_reason"], "ERROR_RESPONSE_BODY_OMITTED")
                self.assertNotIn(REFLECTED_CONTENT, json.dumps({"result": result, "capture": capture}))

    def test_local_type_and_value_errors_preserve_the_last_completed_capture(self):
        for error_type in (TypeError, ValueError):
            with self.subTest(error_type=error_type.__name__):
                collector = APIResponseDiagnostics(protected_values=())
                collector.record(
                    phase="investigation", endpoint="chat/completions", http_status=200,
                    outcome_code="RESPONSE_RECEIVED", body=b"Prior successful response.", elapsed_ms=1,
                )
                client = OpenAICompatibleChatClient(config(), diagnostics=collector)
                with mock.patch.object(client, "complete", side_effect=error_type(REFLECTED_CONTENT)) as complete:
                    result = BoundedAgentLoop(client, FakeTools()).run("Explain the amount")
                self.assertEqual(result["stop_reason"], "model_client_error")
                self.assertEqual(result["diagnostic"]["category"], "system_error")
                self.assertEqual(collector.to_dict()["exchanges"][0]["body_text"], "Prior successful response.")
                self.assertNotIn(REFLECTED_CONTENT, json.dumps(result))
                complete.assert_called_once()

    def test_runner_promotes_agent_failure_without_extra_calls(self):
        transport = FailedRequestTransport()
        output = run_investigation(
            "Explain the amount", self.database, config(), transport=transport,
            capture_api_responses=True,
        )
        self.assertEqual(output["runner_status"], "SAFE_STOP")
        self.assertEqual(output["diagnostic"], output["agent_result"]["diagnostic"])
        self.assertEqual(output["diagnostic"]["http_status"], 500)
        self.assertEqual(len(transport.requests), 6)
        self.assertNotIn(REFLECTED_CONTENT, json.dumps(output))
        self.assertNotIn("test-only-api-key", json.dumps(output))

    def test_runner_promotes_chat_failure_ahead_of_model_list_failure(self):
        transport = FailedRequestTransport(fail_probes=True)
        output = run_investigation(
            "Explain the amount", self.database, config(), transport=transport,
        )
        self.assertEqual(output["runner_status"], "NOT_READY")
        self.assertEqual(output["diagnostic"]["http_status"], 429)
        self.assertEqual(len(transport.requests), 2)
        self.assertNotIn(REFLECTED_CONTENT, json.dumps(output))

    def test_optional_probe_failure_does_not_become_a_completed_run_error(self):
        output = run_investigation(
            "Explain the amount", self.database, config(),
            transport=AgentReadyTransport(reject_tool_result=True),
        )
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertNotIn("diagnostic", output)

    def test_not_ready_ignores_unrecognized_diagnostic_fields(self):
        error = APIClientError("HTTP_ERROR", http_status=403)
        output = _not_ready("COMPANY_API_NOT_READY", capability_report={
            "capabilities": {"chat": {"status": "UNAVAILABLE", "evidence": {
                "diagnostic": {**error.diagnostic, "raw_response": REFLECTED_CONTENT},
            }}},
        })
        self.assertEqual(output["diagnostic"], error.diagnostic)
        self.assertNotIn("raw_response", output["diagnostic"])

    def test_index_reuse_preserves_safe_configuration_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "source", root / "output"
            source.mkdir()
            (source / "main.cbl").write_text(
                "IDENTIFICATION DIVISION.\nPROGRAM-ID. MAIN-ENTRY.\n"
                "PROCEDURE DIVISION.\nGOBACK.\n", encoding="utf-8",
            )
            analyze_source(source, output, analysis_mode="business", source_format="free")
            error = APIConfigurationError("API_KEY_MISSING")
            with mock.patch("business_chat.run_business_chat", side_effect=error) as chat:
                report = analyze_source(
                    source, output, question="Explain the amount", config=config(),
                    analysis_mode="business", reading_strategy="retrieval",
                    source_format="free", allow_network=True,
                )
            saved = json.loads((output / "agent-result.json").read_text(encoding="utf-8"))
            self.assertTrue(report["index_reused"])
            self.assertEqual(saved["diagnostic"], error.diagnostic)
            self.assertEqual(saved["runner_status"], "NOT_READY")
            chat.assert_called_once()
            self.assertIn(error.diagnostic["request_id"], (output / "agent-result.md").read_text(encoding="utf-8"))
            with mock.patch("business_chat.run_business_chat", return_value={
                "runner_status": "COMPLETED", "agent_result": {
                    "status": "PARTIAL", "answer": "Visible answer preserved.",
                },
            }):
                next_report = analyze_source(
                    source, output, question="Explain again", config=config(),
                    analysis_mode="business", reading_strategy="retrieval",
                    source_format="free", allow_network=True,
                )
            self.assertNotIn("diagnostic", next_report)

    def test_fresh_analysis_saves_configuration_failure_with_safe_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "source", root / "output"
            source.mkdir()
            (source / "main.cbl").write_text(
                "IDENTIFICATION DIVISION.\nPROGRAM-ID. MAIN-ENTRY.\n"
                "PROCEDURE DIVISION.\nGOBACK.\n", encoding="utf-8",
            )
            error = APIConfigurationError("API_KEY_MISSING")
            with mock.patch("analyze_source.run_investigation", side_effect=error):
                report = analyze_source(
                    source, output, question="Explain the amount", config=config(),
                    source_format="free", allow_network=True,
                )
            saved = json.loads((output / "agent-result.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["diagnostic"], error.diagnostic)
            self.assertEqual(report["diagnostic"], error.diagnostic)
            self.assertIn(error.diagnostic["request_id"], (output / "agent-result.md").read_text(encoding="utf-8"))

    def test_source_exception_does_not_echo_exception_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "source", root / "output"
            source.mkdir()
            (source / "main.cbl").write_text(
                "IDENTIFICATION DIVISION.\nPROGRAM-ID. MAIN-ENTRY.\n"
                "PROCEDURE DIVISION.\nGOBACK.\n", encoding="utf-8",
            )
            with mock.patch("analyze_source.build_structural_index", side_effect=RuntimeError(REFLECTED_CONTENT)):
                report = analyze_source(source, output, source_format="free")
            saved = json.loads((output / "agent-result.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["diagnostic"]["category"], "system_error")
            self.assertEqual(report["diagnostic"], saved["diagnostic"])
            self.assertEqual(saved["reason_code"], "SOURCE_ANALYSIS_FAILED")
            for name in ("agent-result.json", "agent-result.md", "diagnosis.json", "diagnosis.md"):
                text = (output / name).read_text(encoding="utf-8")
                self.assertNotIn(REFLECTED_CONTENT, text)
                self.assertIn(saved["diagnostic"]["request_id"], text)

    def test_source_invalidation_preserves_answer_and_prior_safe_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "source", root / "output"
            source.mkdir()
            (source / "main.cbl").write_text(
                "IDENTIFICATION DIVISION.\nPROGRAM-ID. MAIN-ENTRY.\n"
                "PROCEDURE DIVISION.\nGOBACK.\n", encoding="utf-8",
            )
            prior_error = APIClientError("HTTP_ERROR", http_status=429)
            with mock.patch("analyze_source.run_investigation", return_value={
                "runner_status": "SAFE_STOP", "diagnostic": prior_error.diagnostic,
                "agent_result": {"answer": "Visible answer preserved."},
            }), mock.patch("analyze_source._verify_scope", side_effect=[None, RuntimeError(REFLECTED_CONTENT)]):
                analyze_source(
                    source, output, question="Explain the amount", config=config(),
                    source_format="free", allow_network=True,
                )
            saved = json.loads((output / "agent-result.json").read_text(encoding="utf-8"))
            self.assertIsNone(saved["agent_result"])
            self.assertEqual(saved["diagnostic"]["category"], "system_error")
            self.assertEqual(saved["prior_diagnostic"], prior_error.diagnostic)
            self.assertEqual(saved["unaccepted_response"]["text"], "Visible answer preserved.")
            self.assertNotIn(REFLECTED_CONTENT, json.dumps(saved))


if __name__ == "__main__":
    unittest.main()
