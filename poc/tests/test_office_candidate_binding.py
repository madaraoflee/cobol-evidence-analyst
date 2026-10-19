"""Offline run/artifact identity contracts; no Actions or model requests."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import resolve_office_candidate as resolver


class OfficeCandidateBindingTests(unittest.TestCase):
    def setUp(self):
        self.repository = "example/analysis-workbench"
        self.workflow = {"id": 42, "name": resolver.WORKFLOW_NAME,
            "path": resolver.WORKFLOW_PATH, "state": "active"}
        self.run = {"id": 123, "workflow_id": 42, "name": resolver.WORKFLOW_NAME,
            "path": resolver.WORKFLOW_PATH, "head_sha": "a" * 40,
            "head_branch": "main", "event": "push", "status": "completed",
            "conclusion": "success", "run_attempt": 2,
            "repository": {"id": 1, "full_name": self.repository},
            "head_repository": {"id": 1, "full_name": self.repository}}
        self.artifact = {"id": 456, "name": "COBOLWorkbench-windows-x64-" + "a" * 40 + "-2",
            "expired": False, "size_in_bytes": 1234,
            "workflow_run": {"id": 123, "head_sha": "a" * 40, "head_branch": "main",
                "repository_id": 1, "head_repository_id": 1}}
        self.artifacts = [self.artifact]
        self.runs = [self.run]
        self.api_calls = []

    def api(self, path, query=None):
        self.api_calls.append((path, query))
        if path.endswith("/workflows/windows-desktop.yml"):
            return copy.deepcopy(self.workflow)
        if path.endswith("/workflows/42/runs"):
            self.assertEqual(query["branch"], "main")
            self.assertEqual(query["status"], "success")
            self.assertEqual(query["exclude_pull_requests"], "true")
            return {"workflow_runs": copy.deepcopy(self.runs)}
        if re.search(r"/runs/[0-9]+$", path):
            return copy.deepcopy(self.run)
        if path.endswith("/runs/123/artifacts"):
            return {"total_count": len(self.artifacts), "artifacts": copy.deepcopy(self.artifacts)}
        self.fail("Unexpected API request: " + path)

    def resolve(self, **kwargs):
        return resolver.resolve_candidate(api=self.api, repository=self.repository,
            event_name=kwargs.pop("event_name", "workflow_dispatch"), helper_commit="b" * 40, **kwargs)

    def trigger(self):
        return {"action": "completed", "repository": {"full_name": self.repository},
                "workflow_run": copy.deepcopy(self.run)}

    def test_empty_dispatch_selects_latest_main_and_same_run_attempt_artifact(self):
        result = self.resolve()
        self.assertEqual(result["selection"], "latest_successful_main_run")
        self.assertEqual(result["source_commit"], "a" * 40)
        self.assertEqual(result["candidate_run_id"], 123)
        self.assertEqual(result["candidate_run_attempt"], 2)
        self.assertEqual(result["candidate_artifact_id"], 456)
        self.assertEqual(result["helper_source_commit"], "b" * 40)
        self.assertEqual(result["model_calls"], 0)
        self.assertFalse(result["publication_performed"])

    def test_explicit_three_inputs_are_validated_without_latest_run_search(self):
        result = self.resolve(run_id="123", app_commit="a" * 40, artifact_id="456")
        self.assertEqual(result["selection"], "explicit_run")
        self.assertFalse(any(path.endswith("/workflows/42/runs") for path, _ in self.api_calls))

    def test_partial_inputs_only_derive_from_selected_run(self):
        result = self.resolve(run_id="123")
        self.assertEqual(result["candidate_artifact_id"], 456)
        self.assertEqual(result["source_commit"], "a" * 40)
        with self.assertRaisesRegex(ValueError, "REQUESTED_COMMIT_MISMATCH"):
            self.resolve(app_commit="c" * 40)
        with self.assertRaisesRegex(ValueError, "REQUESTED_ARTIFACT_MISMATCH"):
            self.resolve(artifact_id="999")

    def test_completed_workflow_binds_event_to_fresh_api_metadata(self):
        result = self.resolve(event_name="workflow_run", event=self.trigger())
        self.assertEqual(result["selection"], "completed_workflow_run")
        self.assertFalse(any(path.endswith("/workflows/42/runs") for path, _ in self.api_calls))

    def test_trigger_metadata_mismatch_is_not_ignored(self):
        for key, value in (("head_sha", "c" * 40), ("run_attempt", 1), ("head_branch", "topic"),
                           ("id", 999), ("conclusion", "failure")):
            event = self.trigger()
            event["workflow_run"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.resolve(event_name="workflow_run", event=event)

    def test_trigger_repository_and_action_are_required(self):
        for change in ("repository", "head_repository", "action"):
            event = self.trigger()
            if change == "action":
                event["action"] = "requested"
            elif change == "repository":
                event["repository"]["full_name"] = "other/workbench"
            else:
                event["workflow_run"]["head_repository"]["full_name"] = "other/workbench"
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.resolve(event_name="workflow_run", event=event)

    def test_push_and_pull_request_only_lint_never_resolve_build(self):
        for event in ("push", "pull_request", "schedule"):
            with self.subTest(event=event), self.assertRaisesRegex(ValueError, "EVENT_NOT_BUILDABLE"):
                self.resolve(event_name=event)
        self.assertEqual(self.api_calls, [])

    def test_run_must_be_successful_main_from_expected_workflow(self):
        original = copy.deepcopy(self.run)
        for key, value in (("head_branch", "topic"), ("event", "pull_request"), ("status", "in_progress"),
                           ("conclusion", "failure"), ("workflow_id", 999), ("path", ".github/workflows/other.yml")):
            self.run = {**copy.deepcopy(original), key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.resolve(run_id="123")

    def test_run_repository_and_head_repository_must_both_match(self):
        for key in ("repository", "head_repository"):
            original = copy.deepcopy(self.run)
            self.run[key]["full_name"] = "other/workbench"
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "RUN_REPOSITORY_MISMATCH"):
                self.resolve(run_id="123")
            self.run = original

    def test_wrong_workflow_configuration_cannot_select_a_candidate(self):
        for key, value in (("name", "Other workflow"), ("path", ".github/workflows/other.yml"),
                           ("state", "disabled_manually")):
            workflow = {**self.workflow, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                resolver.validate_workflow(workflow)

    def test_artifact_from_other_attempt_or_failure_diagnostics_is_not_a_candidate(self):
        self.artifact["name"] = "COBOLWorkbench-windows-x64-" + "a" * 40 + "-1"
        self.artifacts.append({**self.artifact, "name": "failed-native-acceptance-" + "a" * 40 + "-2"})
        with self.assertRaisesRegex(ValueError, "CANDIDATE_ARTIFACT_NOT_UNIQUE"):
            self.resolve()

    def test_latest_candidate_absence_does_not_fall_back_to_an_older_run(self):
        self.runs.append({**self.run, "id": 122, "head_sha": "c" * 40})
        self.artifacts.clear()
        with self.assertRaisesRegex(ValueError, "CANDIDATE_ARTIFACT_NOT_UNIQUE"):
            self.resolve()
        self.assertFalse(any("/runs/122" in path for path, _ in self.api_calls))

    def test_expired_or_duplicate_exact_artifact_is_rejected(self):
        self.artifact["expired"] = True
        with self.assertRaisesRegex(ValueError, "ARTIFACT_UNAVAILABLE"):
            self.resolve()
        self.artifact["expired"] = False
        self.artifacts.append({**self.artifact, "id": 457})
        with self.assertRaisesRegex(ValueError, "CANDIDATE_ARTIFACT_NOT_UNIQUE"):
            self.resolve()

    def test_artifact_run_branch_sha_and_repository_binding_are_checked(self):
        original = copy.deepcopy(self.artifact)
        for key, value in (("id", 999), ("head_sha", "c" * 40), ("head_branch", "topic"),
                           ("repository_id", 2), ("head_repository_id", 2)):
            self.artifact["workflow_run"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "ARTIFACT_RUN_MISMATCH"):
                self.resolve()
            self.artifact = copy.deepcopy(original)
            self.artifacts = [self.artifact]
        self.artifact.pop("workflow_run")
        with self.assertRaisesRegex(ValueError, "ARTIFACT_RUN_BINDING_REQUIRED"):
            self.resolve()

    def test_malformed_ids_shas_and_trigger_inputs_are_rejected(self):
        for kwargs in ({"run_id": "123\nartifact_id=999"}, {"artifact_id": "0"},
                       {"app_commit": "A" * 40}, {"event_name": "workflow_run", "event": self.trigger(), "run_id": "123"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.resolve(**kwargs)

    def test_listing_must_be_complete(self):
        real_api = self.api
        def truncated(path, query=None):
            response = real_api(path, query)
            if path.endswith("/artifacts"):
                response["total_count"] = 101
            return response
        with self.assertRaisesRegex(ValueError, "ARTIFACT_LIST_TRUNCATED"):
            resolver.resolve_candidate(api=truncated, repository=self.repository, event_name="workflow_dispatch",
                                       helper_commit="b" * 40)

    def test_reader_only_performs_fixed_host_get_requests(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps({"ok": True}).encode()
        with patch.object(resolver, "urlopen", return_value=Response()) as opened:
            self.assertEqual(resolver.GitHubReadAPI("test-token")("repos/example/workbench/actions/runs/123"), {"ok": True})
        request = opened.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://api.github.com/repos/example/workbench/actions/runs/123")

    def test_independent_helper_revision_requires_exact_application_and_manifest(self):
        arguments = {"helper_commit": "b" * 40, "checkout_commit": "b" * 40,
            "candidate_manifest": b'{"frozen": "contract"}', "current_manifest": b'{"frozen": "contract"}',
            "candidate_application_inputs": {path: "d" * 40 for path in resolver.APPLICATION_INPUTS},
            "helper_application_inputs": {path: "d" * 40 for path in resolver.APPLICATION_INPUTS}}
        resolver.validate_helper_contracts(**arguments)
        for key, value, code in (("checkout_commit", "c" * 40, "HELPER_CHECKOUT_MISMATCH"),
                                ("current_manifest", b'{"frozen": "new-contract"}', "ACCEPTANCE_CONTRACT_MISMATCH"),
                                ("helper_application_inputs", {path: "e" * 40 for path in resolver.APPLICATION_INPUTS},
                                 "APPLICATION_SOURCE_MISMATCH")):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, code):
                resolver.validate_helper_contracts(**{**arguments, key: value})

    def test_workflow_requires_manual_dispatch_and_has_no_old_candidate_defaults(self):
        workflow = (ROOT / ".github/workflows/windows-office-acceptance.yml").read_text(encoding="utf-8")
        triggers = workflow.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertEqual([line.strip().removesuffix(":") for line in triggers.splitlines()
                          if line.startswith("  ") and not line.startswith("   ")], ["workflow_dispatch"])
        self.assertIn("github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'", workflow)
        self.assertIn("steps.candidate.outputs.artifact_id", workflow)
        self.assertIn("steps.candidate.outputs.run_id", workflow)
        self.assertIn("FROZEN_HELPER_COMMIT", workflow)
        self.assertIn("validate_helper_contracts", workflow)
        self.assertIn("candidate_application_inputs=application_inputs(app_commit)", workflow)
        for obsolete in ("44fcb5f9e769ae559c9cd6b5b12d396959613998", "38593469204", "11913165911"):
            self.assertNotIn(obsolete, workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("actions: read", workflow)
        self.assertNotIn("contents: write", workflow)


if __name__ == "__main__":
    unittest.main()
