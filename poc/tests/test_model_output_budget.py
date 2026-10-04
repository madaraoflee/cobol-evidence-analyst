"""Offline checks for bounded output overrides and profile compatibility."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import company_api
import analyze_source
import run_agent
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

    def test_budget_provenance_identifies_profile_and_override_source(self):
        for name in ("adapter", "workbench"):
            config = self.profile(name)
            self.assertEqual(config.profile_name, name)
            self.assertEqual(config.output_limit_source, "profile")
            self.assertEqual(config.safe_summary()["profile_name"], name)
        self.write_env("3072")
        self.assertEqual(self.profile("workbench").output_limit_source, "dotenv")
        environment = {"COMPANY_MAX_OUTPUT_TOKENS": "6144"}
        self.assertEqual(self.profile("workbench", environ=environment).output_limit_source,
                         "environment")
        config = self.profile("workbench", environ=environment, max_output_tokens=4096)
        self.assertEqual(config.output_limit_source, "explicit")
        self.assertEqual(config.profile_name, "workbench")
        safe = repr(config) + json.dumps(config.safe_summary())
        for value in self.values.values():
            self.assertNotIn(value, safe)

    def test_config_metadata_uses_only_fixed_safe_values(self):
        direct = CompanyAPIConfig(self.values["COMPANY_API_BASE_URL"],
            self.values["COMPANY_CHAT_MODEL"], api_key=self.values["COMPANY_API_KEY"])
        self.assertEqual(direct.profile_name, "custom")
        self.assertEqual(direct.output_limit_source, "unknown")
        for kwargs in ({"profile_name": "private-profile-canary"},
                       {"output_limit_source": "private-source-canary"}):
            config = CompanyAPIConfig(self.values["COMPANY_API_BASE_URL"],
                self.values["COMPANY_CHAT_MODEL"], api_key=self.values["COMPANY_API_KEY"], **kwargs)
            with self.assertRaises(APIConfigurationError):
                config.validate()
            self.assertNotIn("private-", repr(config) + json.dumps(config.safe_summary()))

    def test_cli_budget_precedence_preserves_each_entry_default(self):
        entrypoints = (
            (run_agent.main, ["--database", str(self.root / "unused.sqlite"),
                              "--question", "模拟业务问题"], 1024, "adapter"),
            (company_api.main, [], 1024, "adapter"),
            (analyze_source.main, ["--source", str(self.root), "--output", str(self.root),
                                  "--reading-strategy", "focused"], 4096, "analysis"),
        )
        for main, arguments, default, name in entrypoints:
            cases = (({}, None, [], default, "profile"),
                     ({}, "3072", [], 3072, "dotenv"),
                     ({"COMPANY_MAX_OUTPUT_TOKENS": "6144"}, "3072", [], 6144, "environment"),
                     ({"COMPANY_MAX_OUTPUT_TOKENS": "6144"}, "3072",
                      ["--max-output-tokens", "4096"], 4096, "explicit"))
            for environment, stored, flag, expected, source in cases:
                self.write_env(stored)
                with self.subTest(entrypoint=main.__module__, source=source), \
                     mock.patch.object(company_api, "PROJECT_ENV_FILE", self.env_file), \
                     mock.patch.dict(os.environ, environment, clear=True), \
                     mock.patch("sys.stdout", new_callable=io.StringIO), \
                     mock.patch.object(run_agent, "run_investigation", return_value={"runner_status": "COMPLETED"}) as run, \
                     mock.patch.object(company_api, "probe_capabilities", return_value={
                         "overall_status": "COMPLETED", "agent_readiness": {"ready": True}}) as probe, \
                     mock.patch.object(analyze_source, "analyze_source", return_value={
                         "runner_status": "COMPLETED", "question_status": "ANSWERED"}) as analyze:
                    self.assertEqual(main([*arguments, *flag]), 0)
                    if main is analyze_source.main:
                        config = CompanyAPIConfig.from_env(**analyze.call_args.kwargs["api_options"])
                    else:
                        config = run.call_args.args[2] if main is run_agent.main else probe.call_args.args[0]
                    self.assertEqual(self.actual_request(config)["max_tokens"], expected)
                    self.assertEqual(config.profile_name, name)
                    self.assertEqual(config.output_limit_source, source)

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
                safe = raised.exception.to_safe_dict()
                self.assertEqual(safe["code"], "MAX_OUTPUT_TOKENS_INVALID")
                self.assertEqual(safe["diagnostic"]["evidence_source"], "local")
                self.assertRegex(safe["diagnostic"]["request_id"], r"^local-[0-9a-f]{32}$")
                self.assertNotIn("private-invalid-budget-marker", json.dumps(safe))
                self.assertNotIn("9" * 5000, json.dumps(safe))

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
