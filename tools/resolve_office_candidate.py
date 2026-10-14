"""Bind an office kit to one successful, trusted desktop candidate run.

This resolver reads Actions metadata only. It never builds, publishes, opens a
model connection, or chooses an artifact from a different run as a fallback.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

WORKFLOW_FILE = "windows-desktop.yml"
WORKFLOW_PATH = ".github/workflows/" + WORKFLOW_FILE
WORKFLOW_NAME = "Windows desktop x64"
TRUSTED_BRANCH = "main"
TRUSTED_EVENTS = frozenset({"push", "workflow_dispatch"})
APPLICATION_INPUTS = ("poc", "build_app.py", "desktop-requirements.txt", "assets", ".gitattributes")


def require(condition, code):
    if not condition:
        raise ValueError(code)


def positive_id(value, code):
    require(not isinstance(value, bool) and re.fullmatch(r"[1-9][0-9]*", str(value or "")), code)
    return int(value)


def commit_sha(value, code):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value), code)
    return value


def validate_workflow(workflow):
    require(isinstance(workflow, dict), "OFFICE_WORKFLOW_INVALID")
    positive_id(workflow.get("id"), "OFFICE_WORKFLOW_ID_INVALID")
    require(workflow.get("name") == WORKFLOW_NAME and workflow.get("path") == WORKFLOW_PATH,
            "OFFICE_WORKFLOW_IDENTITY_MISMATCH")
    require(workflow.get("state") == "active", "OFFICE_WORKFLOW_INACTIVE")
    return workflow


def validate_run(run, *, repository, workflow):
    require(isinstance(run, dict), "OFFICE_RUN_INVALID")
    for key in ("repository", "head_repository"):
        metadata = run.get(key)
        require(isinstance(metadata, dict) and metadata.get("full_name") == repository,
                "OFFICE_RUN_REPOSITORY_MISMATCH")
        positive_id(metadata.get("id"), "OFFICE_RUN_REPOSITORY_ID_INVALID")
    require(run.get("workflow_id") == workflow["id"] and run.get("name") == WORKFLOW_NAME
            and run.get("path") == WORKFLOW_PATH, "OFFICE_RUN_WORKFLOW_MISMATCH")
    require(run.get("head_branch") == TRUSTED_BRANCH and run.get("event") in TRUSTED_EVENTS,
            "OFFICE_RUN_UNTRUSTED_SOURCE")
    require(run.get("status") == "completed" and run.get("conclusion") == "success",
            "OFFICE_RUN_NOT_SUCCESSFUL")
    positive_id(run.get("id"), "OFFICE_RUN_ID_INVALID")
    positive_id(run.get("run_attempt"), "OFFICE_RUN_ATTEMPT_INVALID")
    commit_sha(run.get("head_sha"), "OFFICE_RUN_COMMIT_INVALID")
    return run


def validate_trigger(event, *, repository, run):
    require(isinstance(event, dict) and event.get("action") == "completed",
            "OFFICE_TRIGGER_INVALID")
    require(event.get("repository", {}).get("full_name") == repository,
            "OFFICE_TRIGGER_REPOSITORY_MISMATCH")
    trigger = event.get("workflow_run", {})
    for key in ("id", "workflow_id", "name", "path", "head_sha", "head_branch", "event",
                "status", "conclusion", "run_attempt"):
        require(trigger.get(key) == run.get(key), "OFFICE_TRIGGER_RUN_MISMATCH")
    for key in ("repository", "head_repository"):
        require(trigger.get(key, {}).get("full_name") == repository,
                "OFFICE_TRIGGER_REPOSITORY_MISMATCH")


def choose_artifact(artifacts, *, run, requested_artifact=""):
    require(isinstance(artifacts, list), "OFFICE_ARTIFACT_LIST_INVALID")
    expected_name = f'COBOLWorkbench-windows-x64-{run["head_sha"]}-{run["run_attempt"]}'
    matching = [item for item in artifacts if isinstance(item, dict) and item.get("name") == expected_name]
    require(len(matching) == 1, "OFFICE_CANDIDATE_ARTIFACT_NOT_UNIQUE")
    artifact = matching[0]
    artifact_id = positive_id(artifact.get("id"), "OFFICE_ARTIFACT_ID_INVALID")
    require(artifact.get("expired") is False and positive_id(artifact.get("size_in_bytes"),
            "OFFICE_ARTIFACT_SIZE_INVALID") > 0, "OFFICE_ARTIFACT_UNAVAILABLE")
    binding = artifact.get("workflow_run")
    require(isinstance(binding, dict), "OFFICE_ARTIFACT_RUN_BINDING_REQUIRED")
    require(binding.get("id") == run["id"] and binding.get("head_sha") == run["head_sha"]
            and binding.get("head_branch") == TRUSTED_BRANCH
            and binding.get("repository_id") == run["repository"]["id"]
            and binding.get("head_repository_id") == run["head_repository"]["id"],
            "OFFICE_ARTIFACT_RUN_MISMATCH")
    if requested_artifact:
        require(artifact_id == positive_id(requested_artifact, "OFFICE_REQUESTED_ARTIFACT_INVALID"),
                "OFFICE_REQUESTED_ARTIFACT_MISMATCH")
    return artifact


def validate_helper_contracts(*, helper_commit, checkout_commit, candidate_manifest,
                             current_manifest, candidate_application_inputs, helper_application_inputs):
    """Allow independent helper revisions only with the same app and case inputs."""
    require(commit_sha(helper_commit, "OFFICE_HELPER_COMMIT_INVALID") == checkout_commit,
            "OFFICE_HELPER_CHECKOUT_MISMATCH")
    require(hashlib.sha256(candidate_manifest).digest() == hashlib.sha256(current_manifest).digest(),
            "OFFICE_HELPER_ACCEPTANCE_CONTRACT_MISMATCH")
    for values in (candidate_application_inputs, helper_application_inputs):
        require(isinstance(values, dict) and set(values) == set(APPLICATION_INPUTS)
                and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value)
                        for value in values.values()), "OFFICE_APPLICATION_INPUTS_INVALID")
    require(candidate_application_inputs == helper_application_inputs,
            "OFFICE_HELPER_APPLICATION_SOURCE_MISMATCH")


def resolve_candidate(*, api, repository, event_name, event=None, app_commit="", run_id="",
                      artifact_id="", helper_commit):
    require(isinstance(repository, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository),
            "OFFICE_REPOSITORY_INVALID")
    helper_commit = commit_sha(helper_commit, "OFFICE_HELPER_COMMIT_INVALID")
    require(event_name in {"workflow_run", "workflow_dispatch"}, "OFFICE_EVENT_NOT_BUILDABLE")
    if app_commit:
        commit_sha(app_commit, "OFFICE_REQUESTED_COMMIT_INVALID")
    workflow = validate_workflow(api(f"repos/{repository}/actions/workflows/{WORKFLOW_FILE}"))
    if event_name == "workflow_run":
        require(not any((app_commit, run_id, artifact_id)), "OFFICE_TRIGGER_INPUTS_UNEXPECTED")
        require(isinstance(event, dict), "OFFICE_TRIGGER_INVALID")
        selected_id = positive_id(event.get("workflow_run", {}).get("id"), "OFFICE_TRIGGER_RUN_ID_INVALID")
        selection = "completed_workflow_run"
    elif run_id:
        selected_id = positive_id(run_id, "OFFICE_REQUESTED_RUN_INVALID")
        selection = "explicit_run"
    else:
        listing = api(f"repos/{repository}/actions/workflows/{workflow['id']}/runs", {
            "branch": TRUSTED_BRANCH, "status": "success", "exclude_pull_requests": "true", "per_page": 100})
        require(isinstance(listing, dict) and isinstance(listing.get("workflow_runs"), list),
                "OFFICE_RUN_LIST_INVALID")
        runs = [row for row in listing["workflow_runs"] if isinstance(row, dict)
                and row.get("head_branch") == TRUSTED_BRANCH and row.get("event") in TRUSTED_EVENTS
                and row.get("status") == "completed" and row.get("conclusion") == "success"]
        require(runs, "OFFICE_SUCCESSFUL_MAIN_RUN_REQUIRED")
        # The workflow endpoint orders runs by creation time. Never fall back
        # to an older EXE when this selected run lacks its accepted artifact.
        selected_id = positive_id(runs[0].get("id"), "OFFICE_RUN_ID_INVALID")
        selection = "latest_successful_main_run"
    run = validate_run(api(f"repos/{repository}/actions/runs/{selected_id}"),
                       repository=repository, workflow=workflow)
    require(run["id"] == selected_id, "OFFICE_RUN_ID_MISMATCH")
    if event_name == "workflow_run":
        validate_trigger(event, repository=repository, run=run)
    if app_commit:
        require(run["head_sha"] == app_commit, "OFFICE_REQUESTED_COMMIT_MISMATCH")
    listing = api(f"repos/{repository}/actions/runs/{selected_id}/artifacts", {"per_page": 100})
    require(isinstance(listing, dict) and isinstance(listing.get("artifacts"), list),
            "OFFICE_ARTIFACT_LIST_INVALID")
    require(listing.get("total_count") == len(listing["artifacts"]), "OFFICE_ARTIFACT_LIST_TRUNCATED")
    artifact = choose_artifact(listing["artifacts"], run=run, requested_artifact=artifact_id)
    return {"schema_version": "office-candidate-binding/v1", "repository": repository,
        "selection": selection, "workflow_id": workflow["id"], "workflow_path": WORKFLOW_PATH,
        "source_commit": run["head_sha"], "candidate_run_id": run["id"],
        "candidate_run_attempt": run["run_attempt"], "candidate_artifact_id": artifact["id"],
        "candidate_artifact_name": artifact["name"], "helper_source_commit": helper_commit,
        "model_calls": 0, "publication_performed": False}


class GitHubReadAPI:
    def __init__(self, token):
        require(bool(token), "OFFICE_GITHUB_READ_TOKEN_REQUIRED")
        self.token = token

    def __call__(self, path, query=None):
        require(path.startswith("repos/") and not any(part in path for part in ("..", "?", "#")),
                "OFFICE_API_PATH_INVALID")
        url = "https://api.github.com/" + path
        if query:
            url += "?" + urlencode(query)
        request = Request(url, headers={"Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        with urlopen(request, timeout=30) as response:
            return json.load(response)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    parser.add_argument("--event-file", type=Path, default=os.environ.get("GITHUB_EVENT_PATH"))
    parser.add_argument("--app-commit", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--artifact-id", default="")
    parser.add_argument("--helper-commit", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    event = json.loads(args.event_file.read_text(encoding="utf-8")) if args.event_file else None
    result = resolve_candidate(api=GitHubReadAPI(os.environ.get("GH_TOKEN", "")), repository=args.repository,
        event_name=args.event_name, event=event, app_commit=args.app_commit, run_id=args.run_id,
        artifact_id=args.artifact_id, helper_commit=args.helper_commit)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            for key, value in {"app_commit": result["source_commit"], "run_id": result["candidate_run_id"],
                    "artifact_id": result["candidate_artifact_id"], "helper_commit": result["helper_source_commit"]}.items():
                handle.write(f"{key}={value}\n")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
