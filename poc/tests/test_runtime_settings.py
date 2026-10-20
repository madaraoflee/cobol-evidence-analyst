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

    def test_missing_and_null_timers_use_progress_defaults_with_finite_calls(self):
        for settings in ({}, {"max_model_requests": None, "max_question_seconds": None,
                              "hard_question_seconds": None, "max_initial_context_seconds": None,
                              "max_local_analysis_seconds": None}):
            with self.subTest(settings=settings):
                self.write({"version": 1, "agent": settings})
                original = self.path.read_bytes()
                policy = load_agent_policy(self.path)
                self.assertEqual(policy.max_model_requests, 6)
                self.assertIsNone(policy.max_question_seconds)
                self.assertIsNone(policy.hard_question_seconds)
                self.assertIsNone(policy.max_initial_context_seconds)
                self.assertIsNone(policy.max_local_analysis_seconds)
                self.assertEqual(self.path.read_bytes(), original)

    def test_explicit_local_timers_are_not_guessed_or_rewritten(self):
        for initial, local in ((3, 15), (2, 8), (None, 15), (3, None)):
            with self.subTest(initial=initial, local=local):
                self.write({"version": 1, "agent": {
                    "max_initial_context_seconds": initial, "max_local_analysis_seconds": local}})
                original = self.path.read_bytes()
                policy = load_agent_policy(self.path)
                self.assertEqual((policy.max_initial_context_seconds,
                                  policy.max_local_analysis_seconds), (initial, local))
                self.assertEqual(self.path.read_bytes(), original)

    def test_example_uses_progress_defaults_without_adding_time_cutoffs(self):
        policy = load_agent_policy(Path(__file__).resolve().parents[1] / "agent-settings.example.json")
        self.assertEqual(policy, AgentPolicy())

    def test_saved_explicit_deadline_is_not_guessed_to_be_an_old_default(self):
        for settings, expected in (({"max_question_seconds": 180, "hard_question_seconds": 180}, 180),
                                   ({"max_question_seconds": 7}, 7),
                                   ({"hard_question_seconds": 120}, 120)):
            with self.subTest(settings=settings):
                self.write({"version": 1, "agent": settings})
                policy = load_agent_policy(self.path)
                self.assertEqual(policy.hard_question_seconds, expected)
                self.assertEqual(policy.max_question_seconds, expected)

    def test_soft_target_alone_does_not_migrate_into_a_hard_deadline(self):
        self.write({"version": 1, "agent": {"delivery_target_seconds": 120}})
        policy = load_agent_policy(self.path)
        self.assertEqual(policy.effective_delivery_target_seconds, 120)
        self.assertIsNone(policy.hard_question_seconds)

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
