"""The packaged acceptance surface stays isolated and uses the existing server."""
import http.client
import hashlib
from copy import deepcopy
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
    NativeWorkbench, decoded_navigation, isolated_environment, write_profile)
from tools.desktop_acceptance_scenarios import (_activation_supply_checks, activation_sources,
    _aggregate_conversation, _conversation_origin_checks, run_scenarios)


class DesktopAcceptanceRuntimeTests(unittest.TestCase):
    def test_wire_navigation_consumer_retains_distinct_occurrences_and_raw_source(self):
        page = {"source_text": "CALL 'shared_navigation_facts'.", "source_sha256": "a" * 64}
        wire = {"prompt_projection": {"format": "shared-navigation/v1"},
            "shared_navigation_facts": {"nav-1": {"statement": "MOVE A TO B", "execution_verified": False}},
            "evidence_groups": [{"semantic_analysis": {"tool_results": [{"tool": "forward_slice",
                "statements": [{"navigation_fact_ref": "nav-1", "occurrence_id": "first"},
                               {"navigation_fact_ref": "nav-1", "occurrence_id": "second"}],
                "statements_shared_fields": {"guards": ["IF FLAG = 1"]}}]}}],
            "source_context": [{"pages": [page]}]}
        decoded = decoded_navigation(wire)
        statements = decoded["evidence_groups"][0]["semantic_analysis"]["tool_results"][0]["statements"]
        self.assertEqual([row["occurrence_id"] for row in statements], ["first", "second"])
        self.assertTrue(all(row["statement"] == "MOVE A TO B" and row["execution_verified"] is False
                            and row["guards"] == ["IF FLAG = 1"] for row in statements))
        self.assertEqual(decoded["source_context"][0]["pages"], [page])
        self.assertIn("navigation_fact_ref", wire["evidence_groups"][0]["semantic_analysis"]["tool_results"][0]["statements"][0])
        with self.assertRaisesRegex(AssertionError, "Unknown navigation contract"):
            decoded_navigation({"prompt_projection": {"format": "unknown"}})

    def test_wire_consumer_decodes_both_contracts_and_context_without_reading_raw_authority(self):
        raw = {"source_text": "DISPLAY 'navigation_fact_ref _values'.", "_values": ["raw"],
            "statements_columns": ["private"], "statements": [{"_values": ["raw"]}],
            "navigation_fact_ref": "untrusted-raw-id", "source_sha256": "a" * 64}
        wire = {"prompt_projection": {"format": "shared-navigation/v1"},
            "navigation_encoding": {"format": "column-values/v1"},
            "shared_navigation_facts": {"nav-1": {"statement": "MOVE A TO B", "execution_verified": False}},
            "business_map": {"relations_columns": ["target_name", "resolution"],
                "relations": [{"_values": ["WORKER", "confirmed"], "relation_id": "call-1"}]},
            "evidence_groups": [{"semantic_analysis": {"tool_results": [{"tool": "forward_slice",
                "statements_columns": ["navigation_fact_ref"],
                "statements": [{"_values": ["nav-1"], "occurrence_id": "first"},
                               {"_values": ["nav-1"], "occurrence_id": "second"}],
                "statements_shared_fields": {"guards": ["IF FLAG = 1"]}}]}}],
            "source_context": [{"pages": [raw], "working_set": deepcopy(raw),
                "call_chain": {"links_columns": ["caller_path", "target_name"],
                    "links": [{"_values": ["caller.cbl", "WORKER"], "relation_id": "call-1"}]},
                "outline": [{"units_columns": ["name", "complete_text_supplied"],
                    "units": [{"_values": ["MAIN", False]}]}]}],
            "framework_references": [deepcopy(raw)], "framework_facts": [deepcopy(raw)],
            "history": [deepcopy(raw)]}
        original = deepcopy(wire)
        decoded = decoded_navigation(wire)
        self.assertEqual(wire, original)
        self.assertEqual(decoded["business_map"]["relations"][0],
                         {"target_name": "WORKER", "resolution": "confirmed", "relation_id": "call-1"})
        statements = decoded["evidence_groups"][0]["semantic_analysis"]["tool_results"][0]["statements"]
        self.assertEqual([row["occurrence_id"] for row in statements], ["first", "second"])
        self.assertTrue(all(row["statement"] == "MOVE A TO B" and row["guards"] == ["IF FLAG = 1"] for row in statements))
        self.assertEqual(decoded["source_context"][0]["call_chain"]["links"][0]["caller_path"], "caller.cbl")
        self.assertFalse(decoded["source_context"][0]["outline"][0]["units"][0]["complete_text_supplied"])
        for key in ("framework_references", "framework_facts", "history"):
            self.assertEqual(decoded[key], original[key])
        self.assertEqual(decoded["source_context"][0]["pages"], [raw])
        self.assertEqual(decoded["source_context"][0]["working_set"], raw)

    def test_wire_consumer_decodes_legacy_columns_without_shared_projection(self):
        wire = {"navigation_encoding": {"format": "column-values/v1"},
            "business_map": {"relations_columns": ["target_name"], "relations": [{"_values": ["WORKER"]}]}}
        self.assertEqual(decoded_navigation(wire)["business_map"]["relations"], [{"target_name": "WORKER"}])
        with self.assertRaisesRegex(AssertionError, "Unknown navigation column contract"):
            decoded_navigation({"navigation_encoding": {"format": "future"}})
        wire["business_map"]["relations"][0]["_values"] = []
        with self.assertRaisesRegex(AssertionError, "Invalid navigation column row"):
            decoded_navigation(wire)
        with self.assertRaisesRegex(AssertionError, "Cyclic navigation record"):
            decoded_navigation({"prompt_projection": {"format": "shared-navigation/v1"},
                "shared_navigation_facts": {"loop": {"again": {"navigation_fact_ref": "loop"}}},
                "business_map": {"record": {"navigation_fact_ref": "loop"}}})

    def test_activation_acceptance_rejects_missing_or_forged_call_conditions(self):
        text = activation_sources()["launcher.cbl"]
        sha = hashlib.sha256(text.encode()).hexdigest()
        page = {"relative_path": "launcher.cbl", "source_sha256": sha, "evidence_id": "ev_caller",
            "start_line": 1, "end_line": len(text.splitlines()), "source_text": text, "span_truncated": False}
        guard = {"condition": "IF READY-FLAG = 1", "outcome": True, "evidence_ids": ["ev_caller"]}
        caller = {"target_name": "JOB-STEP", "relative_path": "launcher.cbl", "source_sha256": sha,
            "line": 9, "occurrence_id": "call-nine", "scope_known": True, "binding_confirmed": True,
            "compilation_status": "active", "conditions_supplied": True, "execution_verified": False,
            "evidence_ids": ["ev_caller"], "conditions": [guard]}
        payload = {"source_context": [{"pages": [page]}], "question_investigation": {
            "required_items": [{"kind": kind, "status": "SATISFIED"} for kind in
                ("entry_point", "caller_invocation", "activation_conditions")] + [{"kind": "scheduler_binding",
                "status": "OPEN", "reason": "external_schedule_binding_not_verified"}],
            "activation_evidence": {"callers": [caller], "scheduler_binding_verified": False, "execution_verified": False}}}
        self.assertTrue(all(_activation_supply_checks(payload).values()))
        for condition in ("IF READY-FLAG = 1", "READY-FLAG = 1"):
            candidate = deepcopy(payload)
            candidate["question_investigation"]["activation_evidence"]["callers"][0]["conditions"][0]["condition"] = condition
            self.assertTrue(all(_activation_supply_checks(candidate).values()))
        changes = [lambda p: p["question_investigation"]["activation_evidence"]["callers"][0].update(conditions=[]),
            lambda p: p["question_investigation"]["activation_evidence"]["callers"][0].update(evidence_ids=["ev_fake"]),
            lambda p: p["question_investigation"]["activation_evidence"]["callers"][0].update(source_sha256="0" * 64),
            lambda p: p["question_investigation"]["activation_evidence"]["callers"][0].update(line=8),
            lambda p: p["question_investigation"]["activation_evidence"]["callers"][0].update(scope_known=False),
            lambda p: p["question_investigation"]["activation_evidence"]["callers"][0].update(binding_confirmed=False),
            lambda p: p["question_investigation"]["activation_evidence"]["callers"][0].update(compilation_status="candidate"),
            lambda p: p["question_investigation"]["activation_evidence"]["callers"][0]["conditions"][0].update(outcome=False),
            lambda p: p["question_investigation"]["activation_evidence"]["callers"][0]["conditions"][0].update(evidence_ids=["ev_fake"]),
            lambda p: p["source_context"][0]["pages"][0].update(source_text=text.replace("IF READY-FLAG = 1", "IF READY-FLAG = 7")),
            lambda p: p["source_context"][0]["pages"][0].update(source_sha256="f" * 64),
            lambda p: p["question_investigation"]["activation_evidence"].update(scheduler_binding_verified=True)]
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                changed = deepcopy(payload)
                change(changed)
                self.assertFalse(all(_activation_supply_checks(changed).values()))

    def test_value_acceptance_requires_supplied_declarations_and_activation_conditions(self):
        import copy
        page = {"relative_path": "report.cbl", "source_sha256": "a" * 64,
                "evidence_id": "ev_initial", "start_line": 5, "end_line": 7}
        fields = ["TRAN-AMT", "PAGE-SUM", "ACCOUNT-SUM"]
        witnesses = [{"field": field, "status": "source_origin_supplied", "unknown_reasons": [],
            "reader_occurrences": ["read-" + field], "evidence_ids": ["ev_initial"],
            "execution_verified": False, "runtime_value_known": False,
            "activation_semantics": ["first_activation_or_initial_or_cancelled_program"],
            "initial_definitions": [{"field": field, "value": "25.00" if field == "TRAN-AMT" else "0",
                "status": "conditional_initialization_source", "runtime_value_known": False,
                "activation_semantics": "first_activation_or_initial_or_cancelled_program",
                "source_refs": [{"relative_path": "report.cbl", "source_sha256": "a" * 64,
                                 "start_line": line, "end_line": line}]}]}
            for line, field in enumerate(fields, 5)]
        trace = {"final": {"question_investigation": {"required_items": [{
            "kind": "inputs", "status": "SATISFIED", "source_flow_origins": witnesses}]}}}
        self.assertTrue(all(_conversation_origin_checks(trace, [page], fields).values()))
        for change in ({"evidence_ids": ["not-supplied"]}, {"status": "source_origin_unresolved"},
                       {"runtime_value_known": True}, {"activation_semantics": []},
                       {"initial_definitions": []}):
            changed = copy.deepcopy(trace)
            changed["final"]["question_investigation"]["required_items"][0]["source_flow_origins"][0].update(change)
            self.assertFalse(all(_conversation_origin_checks(changed, [page], fields).values()))
        for change in ({"source_sha256": "b" * 64}, {"start_line": 6}, {"span_truncated": True}):
            self.assertFalse(all(_conversation_origin_checks(trace, [{**page, **change}], fields).values()))
        self.assertFalse(all(_conversation_origin_checks(trace, [page], [*fields, "MISSING"]).values()))

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
                mock.patch("desktop_app.logging.basicConfig"), \
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
        # This in-process dispatch test does not exit like the real CLI. Keep
        # its mocked profile out of the process-wide logging configuration.
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("desktop_app.prepare_profile", return_value=Path(directory)), \
                mock.patch("desktop_app.logging.basicConfig"), \
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

    def test_soft_target_scenario_runs_actual_source_with_finite_deadline(self):
        manifest = json.loads((ROOT / "docs/eval/desktop-joint-v1/manifest.json").read_text())
        case = next(row for row in manifest["cases"] if row["id"] == "soft_target_answer")
        with tempfile.TemporaryDirectory() as directory:
            rows = run_scenarios([sys.executable, str(ROOT / "poc/desktop_app.py")],
                directory, {"cases": [{**case, "repeats": 1}]})
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["status"], "passed", row.get("error"))
            self.assertEqual(set(row["checks"]), set(case["assertions"]))
            self.assertTrue(all(row["checks"].values()), row["checks"])
            self.assertTrue(row["run_id"])
            self.assertTrue(row["answer_sha256"])
            self.assertGreaterEqual(row["timings"]["question_seconds"], 1.25)
            self.assertLessEqual(row["timings"]["question_seconds"], case["limits"]["question_seconds"])

    def test_unmeasured_manifest_assertion_fails_the_scenario(self):
        case = {"id": "claim_repair", "repeats": 1, "assertions": ["observed", "unmeasured"]}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("tools.desktop_acceptance_scenarios._claim", return_value=[{"checks": {"observed": True}}]):
            rows = run_scenarios(["unused"], directory, {"cases": [case]})
        self.assertEqual(rows[0]["status"], "failed")
        self.assertFalse(rows[0]["checks"]["unmeasured"])


if __name__ == "__main__":
    unittest.main()
