"""Offline checks for bounded output overrides and profile compatibility."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import company_api
from company_api import APIConfigurationError, CompanyAPIConfig, OpenAICompatibleChatClient, TransportResponse
from model_profiles import model_config
from run_agent import build_parser
from web_app import WorkbenchState, _environment_config


class ModelOutputBudgetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.env_file = self.root / "synthetic.env"
        self.values = {"COMPANY_API_BASE_URL": "https://neutral.example.invalid/v1",
                       "COMPANY_CHAT_MODEL": "neutral-model",
                       "COMPANY_API_KEY": "offline-neutral-credential"}
        self.write_env()

    def write_env(self, output_tokens=None):
        values = dict(self.values)
        if output_tokens is not None:
            values["COMPANY_MAX_OUTPUT_TOKENS"] = output_tokens
        self.env_file.write_text("".join(f"{key}={value}\n" for key, value in values.items()),
                                 encoding="utf-8")

    def actual_request(self, config):
        requests = []

        def transport(request):
            requests.append(json.loads(request.body))
            return TransportResponse(200, '{"choices":[{"message":{"content":"模拟业务说明"}}]}')

        OpenAICompatibleChatClient(config, transport=transport).complete(
            messages=[{"role": "user", "content": "解释模拟业务规则"}])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["max_tokens"], config.max_output_tokens)
        return requests[0]

    def profile(self, name, *, environ=None, **overrides):
        with mock.patch.object(company_api, "PROJECT_ENV_FILE", self.env_file), \
             mock.patch.dict(os.environ, {} if environ is None else environ, clear=True):
            return model_config(name, **overrides)

    def test_absent_setting_keeps_adapter_and_workbench_defaults(self):
        adapter = self.profile("adapter")
        workbench = self.profile("workbench")
        self.assertEqual((adapter.max_output_tokens, adapter.timeout_seconds), (1024, 20.0))
        self.assertEqual((workbench.max_output_tokens, workbench.timeout_seconds), (2048, 60.0))
        self.assertEqual(self.actual_request(adapter)["max_tokens"], 1024)
        self.assertEqual(self.actual_request(workbench)["max_tokens"], 2048)

    def test_file_setting_reaches_actual_web_workbench_request(self):
        self.write_env("4096")
        with mock.patch.object(company_api, "PROJECT_ENV_FILE", self.env_file), \
             mock.patch.dict(os.environ, {}, clear=True):
            config = _environment_config()
        self.assertEqual(config.max_output_tokens, 4096)
        self.assertEqual(config.timeout_seconds, 60.0)
        self.assertEqual(self.actual_request(config)["max_tokens"], 4096)

    def test_process_environment_overrides_local_file_for_both_profiles(self):
        self.write_env("3072")
        environment = {"COMPANY_MAX_OUTPUT_TOKENS": "6144"}
        for name in ("adapter", "workbench"):
            with self.subTest(profile=name):
                config = self.profile(name, environ=environment)
                self.assertEqual(self.actual_request(config)["max_tokens"], 6144)

    def test_explicit_python_override_precedes_environment_and_file(self):
        self.write_env("3072")
        environment = {"COMPANY_MAX_OUTPUT_TOKENS": "6144"}
        config = self.profile("workbench", environ=environment, max_output_tokens=4096)
        self.assertEqual(self.actual_request(config)["max_tokens"], 4096)
        direct = CompanyAPIConfig.from_env(environ={**self.values, **environment},
            env_file=self.env_file, max_output_tokens=2048)
        self.assertEqual(self.actual_request(direct)["max_tokens"], 2048)

    def test_cli_explicit_output_override_still_precedes_environment(self):
        args = build_parser().parse_args(["--database", str(self.root / "unused.sqlite"),
            "--question", "模拟业务问题", "--max-output-tokens", "4096"])
        config = CompanyAPIConfig.from_env(environ={**self.values, "COMPANY_MAX_OUTPUT_TOKENS": "6144"},
                                           max_output_tokens=args.max_output_tokens)
        self.assertEqual(self.actual_request(config)["max_tokens"], 4096)

    def test_valid_environment_bounds_are_sent_without_expanding_other_settings(self):
        for value in ("128", "2048", "8192"):
            with self.subTest(value=value):
                config = CompanyAPIConfig.from_env(environ={**self.values, "COMPANY_MAX_OUTPUT_TOKENS": value})
                self.assertEqual(self.actual_request(config)["max_tokens"], int(value))
                self.assertEqual(config.timeout_seconds, 20.0)
                self.assertEqual(config.safe_summary()["max_output_tokens"], int(value))

    def test_invalid_environment_budget_returns_safe_error_without_echo(self):
        invalid = ("", "127", "8193", "-1", "4096.0", "1e3", "true", "+4096", "４０９６",
                   "private-invalid-budget-marker", "9" * 5000)
        for value in invalid:
            with self.subTest(value_length=len(value)):
                with self.assertRaises(APIConfigurationError) as raised:
                    CompanyAPIConfig.from_env(environ={**self.values, "COMPANY_MAX_OUTPUT_TOKENS": value})
                self.assertEqual(str(raised.exception), "MAX_OUTPUT_TOKENS_INVALID")
                self.assertEqual(raised.exception.to_safe_dict(), {"code": "MAX_OUTPUT_TOKENS_INVALID"})

    def test_invalid_file_budget_is_reported_before_a_web_request(self):
        self.write_env("private-invalid-budget-marker")
        with mock.patch.object(company_api, "PROJECT_ENV_FILE", self.env_file), \
             mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(WorkbenchState, "framework", return_value={}):
            state = WorkbenchState(state_path=None).state()
        self.assertFalse(state["api_configured"])
        self.assertEqual(state["api_configuration_error"], "MAX_OUTPUT_TOKENS_INVALID")
        self.assertNotIn("private-invalid-budget-marker", json.dumps(state))
        for value in self.values.values():
            self.assertNotIn(value, json.dumps(state))

    def test_explicit_small_probe_budget_ignores_invalid_environment_override(self):
        environment = {**self.values, "COMPANY_MAX_OUTPUT_TOKENS": "private-invalid-budget-marker"}
        config = CompanyAPIConfig.from_env(environ=environment, max_output_tokens=32)
        self.assertEqual(self.actual_request(config)["max_tokens"], 32)
        profile = self.profile("workbench", environ=environment, max_output_tokens=32)
        self.assertEqual(self.actual_request(profile)["max_tokens"], 32)

    def test_explicit_environment_is_isolated_from_project_file(self):
        self.write_env("8192")
        with mock.patch.object(company_api, "PROJECT_ENV_FILE", self.env_file):
            config = CompanyAPIConfig.from_env(environ=self.values)
        self.assertEqual(self.actual_request(config)["max_tokens"], 1024)

    def test_model_connection_probe_remains_capped_at_32(self):
        config = self.profile("workbench", max_output_tokens=4096)
        requests = []

        def transport(request):
            requests.append(json.loads(request.body))
            return TransportResponse(200, '{"choices":[{"message":{"content":"OK"}}]}')

        app = WorkbenchState(config_provider=lambda: config, model_check_transport=transport,
                             state_path=None)
        self.assertTrue(app.check_model({})["usable"])
        self.assertEqual(requests[0]["max_tokens"], 32)
        self.assertEqual(config.max_output_tokens, 4096)


if __name__ == "__main__":
    unittest.main()
