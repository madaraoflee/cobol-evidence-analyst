from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from company_api import (  # noqa: E402
    APIClientError,
    APIConfigurationError,
    CompanyAPIConfig,
    MAX_REQUEST_BYTES,
    OpenAICompatibleChatClient,
    TransportRequest,
    TransportResponse,
    _NoRedirectHandler,
    main,
    probe_capabilities,
)


API_KEY = "unit-test-key-should-never-appear"
BASE_URL = "https://internal-api.example/v1"
CHAT_MODEL = "internal-chat-model"


def json_response(status: int, value: object) -> TransportResponse:
    return TransportResponse(
        status_code=status,
        body=json.dumps(value, separators=(",", ":")).encode("utf-8"),
    )


class SuccessfulTransport:
    def __init__(self) -> None:
        self.requests: list[TransportRequest] = []

    def __call__(self, request: TransportRequest) -> TransportResponse:
        self.requests.append(request)
        if request.endpoint == "models":
            return json_response(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": CHAT_MODEL, "object": "model"},
                        {"id": "another-model", "object": "model"},
                    ],
                },
            )
        payload = json.loads((request.body or b"{}").decode("utf-8"))
        if request.endpoint == "embeddings":
            return json_response(
                200,
                {"data": [{"embedding": [0.25, -0.5, 0.75], "index": 0}]},
            )
        if any(
            isinstance(message, dict) and message.get("role") == "tool"
            for message in payload.get("messages", [])
        ):
            return json_response(
                200,
                {
                    "id": "chat-tool-result",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "DONE",
                            }
                        }
                    ],
                },
            )
        if "tools" in payload:
            return json_response(
                200,
                {
                    "id": "chat-tool",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-probe",
                                        "type": "function",
                                        "function": {
                                            "name": "capability_probe",
                                            "arguments": '{"ok":true}',
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                },
            )
        if "response_format" in payload:
            return json_response(
                200,
                {
                    "id": "chat-json",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": '{"ok":true}',
                            }
                        }
                    ],
                },
            )
        return json_response(
            200,
            {
                "id": "chat-basic",
                "choices": [
                    {"message": {"role": "assistant", "content": "OK"}}
                ],
            },
        )


class CompanyAPIConfigTests(unittest.TestCase):
    def test_from_env_reads_only_documented_variables(self) -> None:
        environment = {
            "COMPANY_API_BASE_URL": BASE_URL,
            "COMPANY_API_KEY": API_KEY,
            "COMPANY_CHAT_MODEL": CHAT_MODEL,
            "COMPANY_EMBEDDING_MODEL": "internal-embedding-model",
            "COMPANY_API_STYLE": "openai_compatible",
            "UNRELATED_SECRET": "do-not-read",
        }

        config = CompanyAPIConfig.from_env(environ=environment)

        self.assertEqual(config.resolve_api_key(), API_KEY)
        self.assertEqual(config.api_key_source, "ENVIRONMENT")
        self.assertEqual(config.embedding_model, "internal-embedding-model")
        safe_repr = repr(config)
        self.assertNotIn(API_KEY, safe_repr)
        self.assertNotIn(BASE_URL, safe_repr)
        self.assertNotIn(CHAT_MODEL, safe_repr)

    def test_explicit_key_takes_precedence_over_environment(self) -> None:
        with mock.patch.dict(os.environ, {"COMPANY_API_KEY": "environment-key"}):
            config = CompanyAPIConfig(
                base_url=BASE_URL,
                chat_model=CHAT_MODEL,
                api_key=API_KEY,
            )

        self.assertEqual(config.resolve_api_key(), API_KEY)
        self.assertEqual(config.api_key_source, "EXPLICIT")

    def test_custom_environment_never_falls_back_to_process_key(self) -> None:
        environment = {
            "COMPANY_API_BASE_URL": BASE_URL,
            "COMPANY_CHAT_MODEL": CHAT_MODEL,
        }
        with mock.patch.dict(
            os.environ, {"COMPANY_API_KEY": "ambient-key"}, clear=True
        ):
            with self.assertRaises(APIConfigurationError) as raised:
                CompanyAPIConfig.from_env(environ=environment)

        self.assertEqual(raised.exception.code, "API_KEY_MISSING")

    def test_http_requires_explicit_localhost_exception(self) -> None:
        with self.assertRaises(APIConfigurationError) as raised:
            CompanyAPIConfig(
                base_url="http://company.example/v1",
                chat_model=CHAT_MODEL,
                api_key=API_KEY,
            ).validate()
        self.assertEqual(raised.exception.code, "BASE_URL_INVALID")

        CompanyAPIConfig(
            base_url="http://127.0.0.1:8080/v1",
            chat_model=CHAT_MODEL,
            api_key=API_KEY,
            allow_insecure_localhost=True,
        ).validate()

    def test_redirect_handler_rejects_all_redirects(self) -> None:
        self.assertIsNone(
            _NoRedirectHandler().redirect_request(
                mock.Mock(),
                mock.Mock(),
                302,
                "Found",
                mock.Mock(),
                "https://other.example/v1",
            )
        )

    def test_missing_and_invalid_configuration_errors_are_safe(self) -> None:
        with self.assertRaises(APIConfigurationError) as missing:
            CompanyAPIConfig.from_env(environ={})
        self.assertEqual(missing.exception.code, "BASE_URL_MISSING")

        config = CompanyAPIConfig(
            base_url=f"https://user:{API_KEY}@example.test/v1?token={API_KEY}",
            chat_model=CHAT_MODEL,
            api_key=API_KEY,
        )
        with self.assertRaises(APIConfigurationError) as invalid:
            config.validate()
        self.assertEqual(str(invalid.exception), "BASE_URL_INVALID")
        self.assertNotIn(API_KEY, str(invalid.exception))

        with self.assertRaises(APIConfigurationError) as malformed:
            CompanyAPIConfig(
                base_url="https://[invalid/v1",
                chat_model=CHAT_MODEL,
                api_key=API_KEY,
            ).validate()
        self.assertEqual(str(malformed.exception), "BASE_URL_INVALID")


class ChatClientTests(unittest.TestCase):
    def make_config(self, **overrides: object) -> CompanyAPIConfig:
        values: dict[str, object] = {
            "base_url": BASE_URL,
            "chat_model": CHAT_MODEL,
            "api_key": API_KEY,
        }
        values.update(overrides)
        return CompanyAPIConfig(**values)  # type: ignore[arg-type]

    def test_client_is_network_disabled_without_explicit_transport_or_opt_in(self) -> None:
        client = OpenAICompatibleChatClient(self.make_config())

        with self.assertRaises(APIClientError) as raised:
            client.complete(messages=[{"role": "user", "content": "hello"}])

        self.assertEqual(raised.exception.code, "NETWORK_DISABLED")

    def test_complete_preserves_tool_calls_and_builds_expected_request(self) -> None:
        transport = SuccessfulTransport()
        tool = {
            "type": "function",
            "function": {
                "name": "capability_probe",
                "parameters": {"type": "object"},
            },
        }
        client = OpenAICompatibleChatClient(
            self.make_config(), transport=transport
        )

        result = client.complete(
            messages=[{"role": "user", "content": "use the tool"}],
            tools=[tool],
        )

        expected_calls = [
            {
                "id": "call-probe",
                "type": "function",
                "function": {
                    "name": "capability_probe",
                    "arguments": '{"ok":true}',
                },
            }
        ]
        self.assertEqual(result["choices"][0]["message"]["tool_calls"], expected_calls)  # type: ignore[index]
        request = transport.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.endpoint, "chat/completions")
        self.assertEqual(request.url, f"{BASE_URL}/chat/completions")
        self.assertEqual(request.headers["Authorization"], f"Bearer {API_KEY}")
        payload = json.loads((request.body or b"{}").decode("utf-8"))
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertEqual(payload["tools"], [tool])
        self.assertEqual(payload["max_tokens"], 1024)
        self.assertNotIn(API_KEY, payload)

    def test_request_size_limit_rejects_before_transport(self) -> None:
        transport = SuccessfulTransport()
        client = OpenAICompatibleChatClient(
            self.make_config(), transport=transport
        )

        with self.assertRaises(APIClientError) as raised:
            client.complete(
                messages=[
                    {"role": "user", "content": "x" * (MAX_REQUEST_BYTES + 1)}
                ]
            )

        self.assertEqual(raised.exception.code, "REQUEST_TOO_LARGE")
        self.assertEqual(transport.requests, [])

    def test_remote_http_error_never_echoes_body_url_or_key(self) -> None:
        remote_body = {
            "error": {
                "message": f"bad credential {API_KEY} at {BASE_URL}/chat/completions"
            }
        }

        def transport(_: TransportRequest) -> TransportResponse:
            return json_response(401, remote_body)

        client = OpenAICompatibleChatClient(
            self.make_config(), transport=transport
        )
        with self.assertRaises(APIClientError) as raised:
            client.complete(messages=[{"role": "user", "content": "hello"}])

        rendered = str(raised.exception)
        self.assertEqual(raised.exception.http_status, 401)
        self.assertNotIn(API_KEY, rendered)
        self.assertNotIn(BASE_URL, rendered)
        self.assertNotIn("bad credential", rendered)

    def test_echoed_key_is_redacted_from_success_result(self) -> None:
        def transport(_: TransportRequest) -> TransportResponse:
            return json_response(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": f"gateway echoed {API_KEY}",
                            }
                        }
                    ]
                },
            )

        result = OpenAICompatibleChatClient(
            self.make_config(), transport=transport
        ).complete(messages=[{"role": "user", "content": "hello"}])

        rendered = json.dumps(result)
        self.assertNotIn(API_KEY, rendered)
        self.assertIn("[REDACTED]", rendered)


class CapabilityProbeTests(unittest.TestCase):
    def make_config(self, **overrides: object) -> CompanyAPIConfig:
        values: dict[str, object] = {
            "base_url": BASE_URL,
            "chat_model": CHAT_MODEL,
            "api_key": API_KEY,
        }
        values.update(overrides)
        return CompanyAPIConfig(**values)  # type: ignore[arg-type]

    def test_default_probe_returns_auditable_not_run_without_network(self) -> None:
        report = probe_capabilities(self.make_config())

        self.assertEqual(report["overall_status"], "NOT_RUN")
        self.assertEqual(report["execution"]["transport_mode"], "DISABLED")  # type: ignore[index]
        self.assertEqual(report["execution"]["request_count"], 0)  # type: ignore[index]
        self.assertTrue(
            all(
                capability["reason_code"] == "NETWORK_DISABLED"
                for capability in report["capabilities"].values()  # type: ignore[union-attr]
            )
        )

    def test_successful_probe_detects_each_behavior_without_sensitive_output(self) -> None:
        transport = SuccessfulTransport()

        report = probe_capabilities(self.make_config(), transport=transport)

        self.assertEqual(report["overall_status"], "COMPLETED")
        self.assertEqual(report["execution"]["transport_mode"], "INJECTED")  # type: ignore[index]
        self.assertFalse(report["execution"]["urllib_network_allowed"])  # type: ignore[index]
        self.assertEqual(report["execution"]["request_count"], 5)  # type: ignore[index]
        for name in ("models", "chat", "tool_calling", "strict_json"):
            self.assertEqual(report["capabilities"][name]["status"], "SUPPORTED")  # type: ignore[index]
        serialized = json.dumps(report, ensure_ascii=False)
        for sensitive in (
            API_KEY,
            BASE_URL,
            CHAT_MODEL,
            "Reply with exactly OK",
            '{"ok":true}',
        ):
            self.assertNotIn(sensitive, serialized)
        self.assertTrue(report["privacy"]["api_key_recorded"] is False)  # type: ignore[index]
        self.assertEqual(
            report["agent_readiness"]["mode"], "NATIVE_TOOL_CALLING"  # type: ignore[index]
        )
        tool_requests = [
            json.loads(request.body or b"{}")
            for request in transport.requests
            if request.endpoint == "chat/completions"
            and request.body
            and "tools" in json.loads(request.body)
        ]
        self.assertEqual(len(tool_requests), 2)
        self.assertEqual(tool_requests[1]["messages"][-1]["role"], "tool")
        self.assertEqual(
            tool_requests[1]["messages"][-1]["tool_call_id"], "call-probe"
        )

    def test_empty_chat_message_is_not_reported_as_supported(self) -> None:
        successful = SuccessfulTransport()

        def transport(request: TransportRequest) -> TransportResponse:
            if request.endpoint == "chat/completions":
                payload = json.loads(request.body or b"{}")
                if "tools" not in payload and "response_format" not in payload:
                    return json_response(200, {"choices": [{"message": {}}]})
            return successful(request)

        report = probe_capabilities(self.make_config(), transport=transport)

        self.assertEqual(
            report["capabilities"]["chat"]["status"], "INDETERMINATE"  # type: ignore[index]
        )
        self.assertEqual(report["agent_readiness"]["mode"], "UNAVAILABLE")  # type: ignore[index]

        def ambiguous_transport(request: TransportRequest) -> TransportResponse:
            if request.endpoint == "chat/completions":
                payload = json.loads(request.body or b"{}")
                if "tools" not in payload and "response_format" not in payload:
                    choice = {"message": {"role": "assistant", "content": "OK"}}
                    return json_response(200, {"choices": [choice, choice]})
            return successful(request)

        ambiguous_report = probe_capabilities(
            self.make_config(), transport=ambiguous_transport
        )
        self.assertEqual(
            ambiguous_report["capabilities"]["chat"]["status"],  # type: ignore[index]
            "INDETERMINATE",
        )

    def test_tool_result_round_trip_failure_uses_json_fallback(self) -> None:
        successful = SuccessfulTransport()

        def transport(request: TransportRequest) -> TransportResponse:
            if request.endpoint == "chat/completions" and request.body:
                payload = json.loads(request.body)
                if any(
                    message.get("role") == "tool"
                    for message in payload.get("messages", [])
                ):
                    return json_response(400, {"error": {"message": "rejected"}})
            return successful(request)

        report = probe_capabilities(self.make_config(), transport=transport)

        self.assertEqual(
            report["capabilities"]["tool_calling"]["status"], "UNSUPPORTED"  # type: ignore[index]
        )
        self.assertEqual(
            report["agent_readiness"]["mode"], "VALIDATED_JSON_FALLBACK"  # type: ignore[index]
        )

    def test_models_endpoint_is_informational_for_agent_readiness(self) -> None:
        successful = SuccessfulTransport()

        def transport(request: TransportRequest) -> TransportResponse:
            if request.endpoint == "models":
                return json_response(404, {"error": {"message": "not exposed"}})
            return successful(request)

        report = probe_capabilities(self.make_config(), transport=transport)

        self.assertEqual(report["overall_status"], "COMPLETED")
        self.assertEqual(
            report["capabilities"]["models"]["status"], "UNSUPPORTED"  # type: ignore[index]
        )
        self.assertEqual(
            report["agent_readiness"]["mode"], "NATIVE_TOOL_CALLING"  # type: ignore[index]
        )

    def test_strict_json_rejection_is_explicitly_unsupported(self) -> None:
        successful = SuccessfulTransport()

        def transport(request: TransportRequest) -> TransportResponse:
            if request.endpoint == "chat/completions" and request.body:
                payload = json.loads(request.body.decode("utf-8"))
                if "response_format" in payload:
                    return json_response(
                        400,
                        {"error": {"message": f"unsupported {API_KEY}"}},
                    )
            return successful(request)

        report = probe_capabilities(self.make_config(), transport=transport)

        strict = report["capabilities"]["strict_json"]  # type: ignore[index]
        self.assertEqual(strict["status"], "UNSUPPORTED")
        self.assertEqual(strict["reason_code"], "FEATURE_REQUEST_REJECTED")
        self.assertEqual(strict["evidence"]["http_status"], 400)
        self.assertNotIn(API_KEY, json.dumps(report))

    def test_embedding_probe_is_optional_and_records_only_dimensions(self) -> None:
        transport = SuccessfulTransport()

        report = probe_capabilities(
            self.make_config(embedding_model="internal-embedding-model"),
            transport=transport,
            probe_embeddings=True,
        )

        embedding = report["capabilities"]["embeddings"]  # type: ignore[index]
        self.assertEqual(embedding["status"], "SUPPORTED")
        self.assertEqual(embedding["evidence"]["vector_dimensions"], 3)
        self.assertNotIn("internal-embedding-model", json.dumps(report))

    def test_embedding_rejects_bool_and_non_finite_values(self) -> None:
        for vector in ([True], [float("nan")], [float("inf")], [float("-inf")]):
            with self.subTest(vector=vector):
                successful = SuccessfulTransport()

                def transport(request: TransportRequest) -> TransportResponse:
                    if request.endpoint == "embeddings":
                        return json_response(200, {"data": [{"embedding": vector}]})
                    return successful(request)

                report = probe_capabilities(
                    self.make_config(embedding_model="embedding-model"),
                    transport=transport,
                    probe_embeddings=True,
                )
                self.assertNotEqual(
                    report["capabilities"]["embeddings"]["status"],  # type: ignore[index]
                    "SUPPORTED",
                )

    def test_transport_exception_text_is_never_recorded(self) -> None:
        def failing_transport(_: TransportRequest) -> TransportResponse:
            raise RuntimeError(f"failed with {API_KEY} via {BASE_URL}")

        report = probe_capabilities(
            self.make_config(), transport=failing_transport
        )

        rendered = json.dumps(report)
        self.assertNotIn(API_KEY, rendered)
        self.assertNotIn(BASE_URL, rendered)
        self.assertEqual(
            report["capabilities"]["models"]["reason_code"],  # type: ignore[index]
            "TRANSPORT_ERROR",
        )

    def test_unsupported_api_style_makes_no_request(self) -> None:
        transport = SuccessfulTransport()

        report = probe_capabilities(
            self.make_config(api_style="vendor_specific"), transport=transport
        )

        self.assertEqual(report["overall_status"], "UNSUPPORTED")
        self.assertEqual(transport.requests, [])
        self.assertTrue(
            all(
                capability["reason_code"] == "API_STYLE_UNSUPPORTED"
                for capability in report["capabilities"].values()  # type: ignore[union-attr]
            )
        )

    def test_cli_is_offline_by_default_and_prints_no_configuration_values(self) -> None:
        environment = {
            "COMPANY_API_BASE_URL": BASE_URL,
            "COMPANY_API_KEY": API_KEY,
            "COMPANY_CHAT_MODEL": CHAT_MODEL,
            "COMPANY_API_STYLE": "openai_compatible",
        }
        output = io.StringIO()
        with mock.patch.dict(os.environ, environment, clear=True):
            with contextlib.redirect_stdout(output):
                exit_code = main([])

        rendered = output.getvalue()
        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(rendered)["overall_status"], "NOT_RUN")
        self.assertNotIn(API_KEY, rendered)
        self.assertNotIn(BASE_URL, rendered)
        self.assertNotIn(CHAT_MODEL, rendered)


if __name__ == "__main__":
    unittest.main()
