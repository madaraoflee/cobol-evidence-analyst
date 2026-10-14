"""The packaged acceptance surface stays isolated and uses the existing server."""
import http.client
import json
import os
from pathlib import Path
import ssl
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "poc"))
sys.path.insert(0, str(ROOT))
import desktop_app
from tools.desktop_acceptance_runtime import (FIXTURES, LoopbackProvider,
    NativeWorkbench, isolated_environment, write_profile)
from tools.desktop_acceptance_scenarios import _aggregate_conversation, run_scenarios


class DesktopAcceptanceRuntimeTests(unittest.TestCase):
    def test_conversation_sample_covers_three_turns_without_inventing_extra_samples(self):
        turns = [{"id": "conversation", "turn_id": str(index), "sample_index": 1,
            "run_id": f"run-{index}", "answer_sha256": str(index) * 64,
            "question_index_reused": True, "elapsed_seconds": 0.2,
            "checks": {"quality_followup_correct": index != 2, "history_preserved": True}}
            for index in (1, 2, 3)]
        row = _aggregate_conversation(turns, started=10, finished=12, first_provider_seconds=0.1, cold=0.5)
        self.assertEqual(row["turn_id"], "1")
        self.assertEqual(row["run_id"], "run-3")
        self.assertEqual(row["answer_sha256"], "3" * 64)
        self.assertEqual(row["elapsed_seconds"], 2)
        self.assertEqual(row["timings"]["question_seconds"], 2)
        self.assertEqual(row["turns"], turns)
        self.assertFalse(row["checks"]["quality_followup_correct"])
        self.assertTrue(row["checks"]["history_preserved"])
        self.assertEqual(row["status"], "failed")
        self.assertFalse(row["index_reused"])
        self.assertTrue(row["question_index_reused"])
        with self.assertRaisesRegex(AssertionError, "exactly three"):
            _aggregate_conversation(turns[:2], started=10, finished=12, first_provider_seconds=0.1, cold=0.5)

    def test_conversation_repeats_emit_exactly_one_row_per_manifest_sample(self):
        case = {"id": "conversation", "repeats": 3, "assertions": ["quality_followup_correct"]}
        def measured(command, path, selected, sample_index):
            turns = [{"id": selected["id"], "turn_id": str(index), "sample_index": sample_index,
                "run_id": f"run-{sample_index}-{index}", "answer_sha256": str(index) * 64,
                "question_index_reused": True, "checks": {"quality_followup_correct": True}}
                for index in (1, 2, 3)]
            return [_aggregate_conversation(turns, started=10, finished=11,
                first_provider_seconds=0.1, cold=0.2)]
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("tools.desktop_acceptance_scenarios._conversation", side_effect=measured):
            rows = run_scenarios(["unused"], directory, {"cases": [case]})
        self.assertEqual(len(rows), 3)
        self.assertEqual([(row["turn_id"], row["sample_index"]) for row in rows], [("1", 1), ("1", 2), ("1", 3)])
        self.assertEqual(sum(len(row["turns"]) for row in rows), 9)
        self.assertTrue(all(row["status"] == "passed" for row in rows))

    def test_portable_launcher_passes_capture_only_when_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            profile.mkdir()
            process = mock.Mock(pid=42)
            process.poll.return_value = None
            launched = []
            def start(arguments, **options):
                launched.append(arguments)
                (profile / "acceptance-server.json").write_text(json.dumps(
                    {"pid": 42, "origin": "http://127.0.0.1:12345"}))
                return process
            with mock.patch("tools.desktop_acceptance_runtime.subprocess.Popen", side_effect=start), \
                    mock.patch.object(NativeWorkbench, "request", return_value={"session_token": "fixture", "api_configured": True}):
                for capture in (False, True):
                    workbench = NativeWorkbench(["binary"], profile, {}, root, capture_context=capture)
                    workbench.start()
                    workbench.stop()
                    self.assertEqual("--acceptance-capture-context" in launched[-1], capture)
                    self.assertNotIn("--browser", launched[-1])
            self.assertEqual(process.terminate.call_count, 2)

    def test_context_capture_requires_explicit_hidden_server_mode(self):
        with mock.patch("desktop_app.prepare_profile") as prepare, mock.patch("sys.stderr"), \
                self.assertRaises(SystemExit):
            desktop_app.main(["--acceptance-capture-context", "--data-dir", "profile"])
        prepare.assert_not_called()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch("desktop_app.prepare_profile", return_value=Path(directory)), \
                mock.patch("desktop_app.configure_tls"), \
                mock.patch("desktop_app.run_acceptance_server", return_value=0):
            arguments = ["--data-dir", directory, "--acceptance-server-info", str(Path(directory) / "info.json")]
            self.assertEqual(desktop_app.main(arguments + ["--acceptance-capture-context"]), 0)
            self.assertEqual(os.environ["WORKBENCH_ACCEPTANCE_CAPTURE_CONTEXT"], "1")
            self.assertEqual(desktop_app.main(arguments), 0)
            self.assertNotIn("WORKBENCH_ACCEPTANCE_CAPTURE_CONTEXT", os.environ)

    def test_hidden_mode_requires_explicit_profile_and_excludes_launch_modes(self):
        for extra in ([], ["--self-test"], ["--browser"], ["--open-config"]):
            arguments = ["--acceptance-server-info", "info.json"]
            if extra:
                arguments += ["--data-dir", "profile"] + extra
            with self.subTest(extra=extra), mock.patch("desktop_app.prepare_profile") as prepare, \
                    mock.patch("sys.stderr"), self.assertRaises(SystemExit) as error:
                desktop_app.main(arguments)
            self.assertEqual(error.exception.code, 2)
            prepare.assert_not_called()

    def test_acceptance_information_is_exclusive_and_inside_explicit_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            service = mock.Mock()
            service.server.origin = "http://127.0.0.1:12345"
            with mock.patch("desktop_app.DesktopServer") as server:
                server.return_value.start.return_value = service
                target = root / "acceptance.json"
                self.assertEqual(desktop_app.run_acceptance_server(root, target), 0)
                self.assertEqual(json.loads(target.read_text()),
                    {"schema_version": 1, "origin": service.server.origin, "pid": os.getpid()})
                service.thread.join.assert_called_once_with()
                service.close.assert_called_once_with()
                with self.assertRaises(ValueError):
                    desktop_app.run_acceptance_server(root, target)
                with self.assertRaises(ValueError):
                    desktop_app.run_acceptance_server(root, root.parent / "outside.json")
                self.assertEqual(server.call_count, 1)

    def test_acceptance_startup_failure_never_opens_native_dialog(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("desktop_app.prepare_profile", return_value=Path(directory)), \
                mock.patch("desktop_app.configure_tls"), \
                mock.patch("desktop_app.run_acceptance_server", side_effect=ValueError("fixture")), \
                mock.patch("desktop_app.show_startup_error") as dialog, mock.patch("sys.stderr"):
            self.assertEqual(desktop_app.main(["--data-dir", directory,
                "--acceptance-server-info", str(Path(directory) / "info.json")]), 1)
            dialog.assert_not_called()

    def test_environment_never_inherits_provider_profile_or_proxy(self):
        inherited = {"COMPANY_API_KEY": "private", "WORKBENCH_DATA_DIR": "/private", "FRAMEWORK_REFERENCE_PATH": "/private",
            "PYTHONPATH": "/private", "HTTPS_PROXY": "http://private", "BROWSER": "private",
            "AGENT_SETTINGS_PATH": "/private", "PATH": "runtime-bin"}
        with mock.patch.dict(os.environ, inherited, clear=True):
            actual = isolated_environment()
        self.assertEqual(actual, {"PATH": "runtime-bin", "NO_PROXY": "127.0.0.1,localhost"})

    def test_loopback_provider_requires_real_tls_and_preserves_actual_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with LoopbackProvider(root / "provider", lambda envelope, payload, index: "verified fixture") as provider:
                environment = write_profile(root / "profile", provider, {"max_question_seconds": 3})
                self.assertEqual(environment["SSL_CERT_FILE"], str(FIXTURES / "loopback-server.crt"))
                settings = json.loads((root / "profile/agent-settings.json").read_text())
                self.assertEqual(settings["agent"]["max_question_seconds"], 3)
                self.assertNotIn("COMPANY_API_KEY", environment)
                context = ssl.create_default_context(cafile=FIXTURES / "loopback-server.crt")
                connection = http.client.HTTPSConnection("127.0.0.1", provider.server.server_port, context=context, timeout=5)
                try:
                    body = json.dumps({"messages": [{"role": "user", "content": json.dumps({"question": "fixture"})}]})
                    connection.request("POST", "/v1/chat/completions", body, {"Content-Type": "application/json"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertIn("verified fixture", response.read().decode())
                finally:
                    connection.close()
                self.assertEqual(provider.requests[0]["payload"]["question"], "fixture")
                self.assertFalse(provider.errors)
                self.assertTrue((root / "provider/request-001.json").is_file())

    def test_scenario_error_is_a_failed_sample_with_no_fabricated_answer(self):
        case = {"id": "claim_repair", "repeats": 1, "assertions": ["quality_wrong_formula_repaired"]}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("tools.desktop_acceptance_scenarios._claim", side_effect=AssertionError("wrong formula")):
            rows = run_scenarios(["unused"], directory, {"cases": [case]})
            self.assertEqual(rows[0]["status"], "failed")
            self.assertFalse(rows[0]["checks"]["quality_wrong_formula_repaired"])
            self.assertIsNone(rows[0]["answer_sha256"])
            self.assertIn("wrong formula", rows[0]["error"])
            self.assertTrue((Path(directory) / "claim_repair/sample-01/failure.txt").is_file())

    def test_unmeasured_manifest_assertion_fails_the_scenario(self):
        case = {"id": "claim_repair", "repeats": 1, "assertions": ["observed", "unmeasured"]}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("tools.desktop_acceptance_scenarios._claim", return_value=[{"checks": {"observed": True}}]):
            rows = run_scenarios(["unused"], directory, {"cases": [case]})
        self.assertEqual(rows[0]["status"], "failed")
        self.assertFalse(rows[0]["checks"]["unmeasured"])


if __name__ == "__main__":
    unittest.main()
