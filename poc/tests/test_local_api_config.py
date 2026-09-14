from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

import company_api
from company_api import APIConfigurationError, CompanyAPIConfig, OpenAICompatibleChatClient, TransportResponse
from web_app import WorkbenchState


LOCAL_KEY = "local-config-secret-canary"
LOCAL_MODEL = "local-deployment"
LOCAL_ENDPOINT = "https://gateway.example.invalid/v1"
SETTINGS = (f"COMPANY_API_BASE_URL={LOCAL_ENDPOINT}\n"
            f"COMPANY_API_KEY={LOCAL_KEY}\n"
            f"COMPANY_CHAT_MODEL={LOCAL_MODEL}\n")


class LocalAPIConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env_file = self.root / ".env"
        self.env_file.write_text(SETTINGS, encoding="utf-8")

    def config(self, **kwargs: object) -> CompanyAPIConfig:
        return CompanyAPIConfig.from_env(environ={}, env_file=self.env_file, **kwargs)

    def test_file_only_configuration_reaches_the_model_request_without_logging_values(self) -> None:
        config = self.config()
        self.assertEqual(config.api_key_source, "LOCAL_FILE")
        requests = []

        def transport(request):
            requests.append(request)
            return TransportResponse(200, b'{"choices":[{"message":{"content":"ok"}}]}')

        OpenAICompatibleChatClient(config, transport=transport, allow_network=True).complete(
            messages=[{"role": "user", "content": "Explain the selected source."}])
        request = requests[0]
        self.assertEqual(request.url, LOCAL_ENDPOINT + "/chat/completions")
        self.assertEqual(request.headers["Authorization"], "Bearer " + LOCAL_KEY)
        payload = json.loads(request.body)
        self.assertEqual(payload["model"], LOCAL_MODEL)
        self.assertNotIn(LOCAL_KEY, json.dumps(payload))
        safe = repr(config) + repr(request) + json.dumps(config.safe_summary())
        for value in (LOCAL_KEY, LOCAL_MODEL, LOCAL_ENDPOINT):
            self.assertNotIn(value, safe)

    def test_automatic_project_file_is_independent_of_working_directory(self) -> None:
        self.assertEqual(company_api.PROJECT_ENV_FILE, POC_ROOT.parent / ".env")
        unrelated = self.root / "unrelated-source"
        unrelated.mkdir()
        (unrelated / ".env").write_text("INVALID UNRELATED SETTINGS")
        previous = Path.cwd()
        try:
            os.chdir(unrelated)
            with mock.patch.object(company_api, "PROJECT_ENV_FILE", self.env_file), mock.patch.dict(os.environ, {}, clear=True):
                config = CompanyAPIConfig.from_env()
                self.assertEqual(config.resolve_api_key(), LOCAL_KEY)
                self.assertEqual(dict(os.environ), {})
        finally:
            os.chdir(previous)

    def test_explicit_options_override_environment_which_overrides_file(self) -> None:
        with mock.patch.object(company_api, "PROJECT_ENV_FILE", self.env_file), mock.patch.dict(os.environ, {
            "COMPANY_API_KEY": "environment-credential", "COMPANY_CHAT_MODEL": "environment-model",
        }, clear=True):
            config = CompanyAPIConfig.from_env(chat_model="explicit-model")
            self.assertEqual(config.chat_model, "explicit-model")
            self.assertEqual(config.base_url, LOCAL_ENDPOINT)
            self.assertEqual(config.resolve_api_key(), "environment-credential")
            self.assertEqual(config.api_key_source, "ENVIRONMENT")
            self.assertEqual(CompanyAPIConfig.from_env(api_key="explicit-credential").api_key_source, "EXPLICIT")
            with mock.patch.dict(os.environ, {"COMPANY_API_KEY": ""}):
                with self.assertRaisesRegex(APIConfigurationError, "API_KEY_MISSING"):
                    CompanyAPIConfig.from_env()

    def test_explicit_environment_remains_isolated_from_ambient_file_and_process(self) -> None:
        with mock.patch.object(company_api, "PROJECT_ENV_FILE", self.env_file), mock.patch.dict(os.environ, {"COMPANY_API_KEY": "ambient-key"}, clear=True):
            with self.assertRaisesRegex(APIConfigurationError, "API_KEY_MISSING"):
                CompanyAPIConfig.from_env(environ={"COMPANY_API_BASE_URL": LOCAL_ENDPOINT, "COMPANY_CHAT_MODEL": LOCAL_MODEL})

    def test_windows_bom_crlf_quotes_and_literal_special_characters(self) -> None:
        literal = r"secret#hash$VALUE${VALUE}%VALUE%`command`$(command)\n"
        text = ("# Local configuration\r\n"
                f'COMPANY_API_BASE_URL = "{LOCAL_ENDPOINT}" # gateway\r\n'
                f"export COMPANY_API_KEY='{literal}'\r\n"
                f"COMPANY_CHAT_MODEL={LOCAL_MODEL} # deployment\r\n"
                "UNRELATED_VARIABLE=ignored\r\n")
        self.env_file.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
        config = self.config()
        self.assertEqual(config.resolve_api_key(), literal)
        self.assertEqual(config.chat_model, LOCAL_MODEL)
        self.env_file.write_text(SETTINGS.replace(LOCAL_KEY, literal))
        self.assertEqual(self.config().resolve_api_key(), literal)

    def test_invalid_file_errors_never_echo_content(self) -> None:
        for body in (b"\xff", f"COMPANY_API_KEY='{LOCAL_KEY}".encode(),
                     f'COMPANY_API_KEY="{LOCAL_KEY}" extra'.encode(),
                     f"{LOCAL_KEY}\n".encode(), b"COMPANY_API_KEY=a\x00b"):
            with self.subTest(body_length=len(body)):
                self.env_file.write_bytes(body)
                with self.assertRaises(APIConfigurationError) as raised:
                    self.config()
                self.assertEqual(str(raised.exception), "ENV_FILE_INVALID")
                self.assertNotIn(LOCAL_KEY, str(raised.exception))

    def test_unreadable_or_oversized_file_has_safe_error(self) -> None:
        with mock.patch.object(Path, "open", side_effect=PermissionError(LOCAL_KEY)):
            with self.assertRaisesRegex(APIConfigurationError, "^ENV_FILE_UNREADABLE$"):
                self.config()
        self.env_file.write_bytes(b"#" * (company_api.MAX_ENV_FILE_BYTES + 1))
        with self.assertRaisesRegex(APIConfigurationError, "^ENV_FILE_TOO_LARGE$"):
            self.config()

    def test_missing_optional_file_keeps_environment_only_workflow(self) -> None:
        with mock.patch.object(company_api, "PROJECT_ENV_FILE", self.root / "absent.env"):
            with mock.patch.dict(os.environ, {
                "COMPANY_API_BASE_URL": LOCAL_ENDPOINT, "COMPANY_API_KEY": LOCAL_KEY,
                "COMPANY_CHAT_MODEL": LOCAL_MODEL,
            }, clear=True):
                self.assertEqual(CompanyAPIConfig.from_env().api_key_source, "ENVIRONMENT")

    def test_file_updates_are_not_cached_into_process_environment(self) -> None:
        with mock.patch.object(company_api, "PROJECT_ENV_FILE", self.env_file), mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(CompanyAPIConfig.from_env().chat_model, LOCAL_MODEL)
            self.env_file.write_text(SETTINGS + "COMPANY_CHAT_MODEL=revised-model\n")
            self.assertEqual(CompanyAPIConfig.from_env().chat_model, "revised-model")
            self.assertEqual(dict(os.environ), {})

    def test_web_state_reports_configuration_without_exposing_values(self) -> None:
        with mock.patch.object(company_api, "PROJECT_ENV_FILE", self.env_file), mock.patch.dict(os.environ, {}, clear=True):
            app = WorkbenchState()
            state = app.state()
            self.assertTrue(state["api_configured"])
            self.assertIsNone(state["api_configuration_error"])
            for value in (LOCAL_KEY, LOCAL_MODEL, LOCAL_ENDPOINT):
                self.assertNotIn(value, json.dumps(state))
            self.env_file.write_text(f'COMPANY_API_KEY="{LOCAL_KEY}')
            state = app.state()
            self.assertFalse(state["api_configured"])
            self.assertEqual(state["api_configuration_error"], "ENV_FILE_INVALID")
            self.assertNotIn(LOCAL_KEY, json.dumps(state))


if __name__ == "__main__":
    unittest.main()
