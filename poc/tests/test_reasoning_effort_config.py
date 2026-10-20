"""Optional generation effort is explicit, safe and stable in wire identity."""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import company_api
from company_api import APIClientError, APIConfigurationError, CompanyAPIConfig, OpenAICompatibleChatClient, TransportResponse
from model_profiles import model_config


class ReasoningEffortConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.env_file = Path(temporary.name) / "neutral.env"
        self.environment = {"COMPANY_API_BASE_URL": "https://gateway.example.invalid/v1",
                            "COMPANY_API_KEY": "neutral-test-credential", "COMPANY_CHAT_MODEL": "neutral-model"}
        self.env_file.write_text("".join(f"{key}={value}\n" for key, value in self.environment.items()), encoding="utf-8")

    def config(self, **options):
        return CompanyAPIConfig.from_env(environ=self.environment, **options)

    @staticmethod
    def capture(config, *, stream=False):
        requests, texts = [], []

        def transport(request):
            requests.append(json.loads(request.body))
            if stream:
                return TransportResponse(200, 'data: {"choices":[{"delta":{"content":"Business answer"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
                                         {"Content-Type": "text/event-stream"})
            return TransportResponse(200, '{"choices":[{"message":{"content":"Business answer"},"finish_reason":"stop"}]}')

        result = OpenAICompatibleChatClient(config, transport=transport).complete(
            messages=[{"role": "user", "content": "Explain the source"}],
            **({"on_text_delta": texts.append} if stream else {}))
        return requests, texts, result

    def test_absent_effort_omits_wire_parameter_for_unknown_endpoints(self):
        config = self.config()
        requests, _, _ = self.capture(config)
        self.assertIsNone(config.reasoning_effort)
        self.assertEqual(config.generation_parameters(), {})
        self.assertIsNone(config.safe_summary()["reasoning_effort"])
        self.assertEqual(set(requests[0]), {"model", "messages", "max_tokens"})

    def test_all_allowed_explicit_efforts_reach_the_actual_request(self):
        for effort in ("low", "medium", "high"):
            with self.subTest(effort=effort):
                config = self.config(reasoning_effort=effort)
                requests, _, _ = self.capture(config)
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]["reasoning_effort"], effort)
                self.assertEqual(config.safe_summary()["reasoning_effort"], effort)

    def test_streaming_request_uses_the_same_optional_parameter(self):
        requests, texts, result = self.capture(self.config(reasoning_effort="low"), stream=True)
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0]["stream"])
        self.assertEqual(requests[0]["reasoning_effort"], "low")
        self.assertEqual("".join(texts), "Business answer")
        self.assertEqual(result["choices"][0]["message"]["content"], "Business answer")

    def test_explicit_api_options_override_environment_and_file(self):
        self.env_file.write_text(self.env_file.read_text() + "COMPANY_REASONING_EFFORT=high\n", encoding="utf-8")
        self.assertEqual(CompanyAPIConfig.from_env(environ={}, env_file=self.env_file).reasoning_effort, "high")
        options = {"environ": {"COMPANY_REASONING_EFFORT": "medium"}, "env_file": self.env_file}
        self.assertEqual(CompanyAPIConfig.from_env(**options).reasoning_effort, "medium")
        self.assertEqual(CompanyAPIConfig.from_env(**{**options, "reasoning_effort": "low"}).reasoning_effort, "low")
        self.assertEqual(options["environ"], {"COMPANY_REASONING_EFFORT": "medium"})

    def test_adapter_and_workbench_profiles_read_the_same_explicit_setting(self):
        with mock.patch.object(company_api, "PROJECT_ENV_FILE", self.env_file), \
                mock.patch.dict(os.environ, {"COMPANY_REASONING_EFFORT": "medium"}, clear=True):
            for profile in ("adapter", "workbench"):
                with self.subTest(profile=profile):
                    self.assertEqual(model_config(profile).reasoning_effort, "medium")
                    config = model_config(profile, reasoning_effort="low")
                    requests, _, _ = self.capture(config)
                    self.assertEqual(requests[0]["reasoning_effort"], "low")

    def test_explicit_environment_is_isolated_from_ambient_effort(self):
        with mock.patch.dict(os.environ, {"COMPANY_REASONING_EFFORT": "high"}):
            self.assertIsNone(self.config().reasoning_effort)

    def test_invalid_values_are_rejected_without_leaking_them_or_sending(self):
        for value in ("private-invalid-marker", "", "LOW", "max", 1, True, [], {}):
            with self.subTest(value=value):
                config = replace(self.config(), reasoning_effort=value)
                with self.assertRaisesRegex(APIConfigurationError, "^REASONING_EFFORT_INVALID$"):
                    config.validate()
                self.assertNotIn("private-invalid-marker", repr(config) + json.dumps(config.safe_summary()))
                transport = mock.Mock()
                with self.assertRaisesRegex(APIClientError, "^REASONING_EFFORT_INVALID$"):
                    OpenAICompatibleChatClient(config, transport=transport).complete(
                        messages=[{"role": "user", "content": "Question"}])
                transport.assert_not_called()

    def test_invalid_environment_can_be_overridden_explicitly_without_echoing_it(self):
        options = {"environ": {**self.environment, "COMPANY_REASONING_EFFORT": "private-invalid-marker"}}
        with self.assertRaisesRegex(APIConfigurationError, "^REASONING_EFFORT_INVALID$"):
            CompanyAPIConfig.from_env(**options)
        self.assertEqual(CompanyAPIConfig.from_env(**options, reasoning_effort="low").reasoning_effort, "low")

    def test_generation_parameters_preserve_default_hash_and_distinguish_overrides(self):
        config = self.config()
        original = {"base_url": config.base_url, "model": config.chat_model,
                    "timeout_seconds": config.timeout_seconds, "max_output_tokens": config.max_output_tokens,
                    "api_style": config.api_style}
        digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        self.assertEqual(digest(original), digest({**original, **config.generation_parameters()}))
        identities = {digest({**original, **replace(config, reasoning_effort=value).generation_parameters()})
                      for value in (None, "low", "medium", "high")}
        self.assertEqual(len(identities), 4)

    def test_unsupported_parameter_response_is_not_retried_without_it(self):
        transport = mock.Mock(return_value=TransportResponse(400,
            '{"error":{"message":"Unsupported generation option"}}'))
        client = OpenAICompatibleChatClient(self.config(reasoning_effort="low"), transport=transport)
        with self.assertRaises(APIClientError):
            client.complete(messages=[{"role": "user", "content": "Question"}])
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(json.loads(transport.call_args.args[0].body)["reasoning_effort"], "low")


if __name__ == "__main__":
    unittest.main()
