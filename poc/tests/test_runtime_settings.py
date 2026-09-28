"""Deployment settings are bounded and never silently replace a failed budget."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_policy import AgentPolicy
from runtime_settings import MAX_SETTINGS_BYTES, load_agent_policy


class RuntimeSettingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "settings.json"

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_absent_default_uses_existing_defaults_without_creating_files(self):
        with patch("runtime_settings.DEFAULT_SETTINGS_PATH", self.path):
            self.assertEqual(load_agent_policy(environ={}), AgentPolicy())
        self.assertFalse(self.path.exists())

    def test_explicit_missing_file_does_not_fall_back_to_larger_default_budget(self):
        with self.assertRaisesRegex(ValueError, "^AGENT_SETTINGS_UNREADABLE$"):
            load_agent_policy(self.path)

    def test_valid_local_policy_and_environment_path_are_equivalent(self):
        self.write({"version": 1, "agent": {"max_model_requests": 2, "max_history_characters": 3000}})
        explicit = load_agent_policy(self.path)
        environment = {"AGENT_SETTINGS_PATH": str(self.path), "UNRELATED": "unchanged"}
        self.assertEqual(load_agent_policy(environ=environment), explicit)
        self.assertEqual(explicit.max_model_requests, 2)
        self.assertEqual(explicit.max_history_characters, 3000)
        self.assertEqual(environment["UNRELATED"], "unchanged")

    def test_invalid_settings_errors_do_not_echo_supplied_values(self):
        for value in ({"version": True, "agent": {}}, {"version": 2, "agent": {}},
                      {"version": 1, "agent": {"max_model_requests": 0}},
                      {"version": 1, "agent": {"max_model_requests": "private-value"}},
                      {"version": 1, "agent": {}, "private-value": "private-value"}):
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaisesRegex(ValueError, "^AGENT_SETTINGS_INVALID$"):
                    load_agent_policy(self.path)

    def test_oversized_file_is_bounded_before_json_parsing(self):
        self.path.write_bytes(b"x" * (MAX_SETTINGS_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "^AGENT_SETTINGS_TOO_LARGE$"):
            load_agent_policy(self.path)


if __name__ == "__main__":
    unittest.main()
