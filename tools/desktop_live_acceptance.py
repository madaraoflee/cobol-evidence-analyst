"""Measure the final Windows delivery locally after explicit model authorization.

Planning is the default. Reports, source excerpts, configuration and review forms
stay on the target computer; this command does not publish or upload anything.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import secrets
import shutil
import statistics
import sqlite3
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "poc"))
from agent_policy import AgentPolicy
from company_api import _read_local_env
from live_business_eval import (_cases, _turns, _reviewed_context, _safe_write, _evidence_metadata,
    _measurement, _review_identity, _percentile, review_template)
from release_acceptance import _required_ranges, _range_covered, _live_case_scope, OFFICE_BASELINE_CHECKS
from desktop_acceptance import inspect_package, extract_package, digest, require
from desktop_acceptance_runtime import NativeWorkbench, isolated_environment, read_journal
from acceptance_diagnostics import capture_diagnostics
from live_business_eval import _fingerprint
from model_profiles import WORKBENCH_TIMEOUT_SECONDS


def execution_wait_policy(policy):
    """Leave cleanup time after the application's shared hard deadline.

    The frozen desktop uses model_config('workbench'), whose timeout is a
    shared constant, not a dotenv setting. Reserve every allowed exchange plus
    optional local analysis, two minutes for source/context preparation and
    45 seconds for collecting results/checkpoints. The application deadline
    still limits investigation and model waits. This is a conservative watchdog,
    not an assertion that all local operations have a mathematical time bound.
    """
    transport = float(WORKBENCH_TIMEOUT_SECONDS)
    local_reserve = 120 + policy.max_local_analysis_seconds
    cleanup = 45
    wait = policy.max_model_requests * transport + local_reserve + cleanup
    if policy.hard_question_seconds is not None:
        wait = min(wait, policy.hard_question_seconds + cleanup)
    return {"basis": "workbench_transport_and_local_cleanup",
        "transport_timeout_seconds": transport,
        "maximum_model_requests": policy.max_model_requests,
        "local_processing_reserve_seconds": local_reserve,
        "verification_cleanup_reserve_seconds": cleanup,
        "hard_question_seconds": policy.hard_question_seconds,
        "application_hard_deadline_expected": policy.hard_question_seconds is not None,
        "watchdog_seconds": wait,
        "performance_target_seconds": policy.max_question_seconds,
        "performance_target_interrupts_answer": False}


def _capture_interrupted_requests(report, workspace):
    """Archive actual uncertainty before shutting down the application process."""
    try:
        requests = read_journal(workspace)["requests"]
    except (OSError, ValueError, sqlite3.Error, AssertionError, KeyError):
        report["request_journal_status"] = "unavailable_attention_required"
        report["remote_result_unknown"] = True
        return
    report["request_journal_status"] = "captured_before_shutdown"
    report["interrupted_requests"] = [{key: row[key] for key in
        ("run_id", "request_id", "state", "request_hash") if key in row} for row in requests]
    report["observed_model_requests"] = max(report["observed_model_requests"], len(requests))
    report["remote_result_unknown"] = any(row.get("state") != "completed" for row in requests)
    journal = Path(workspace) / "investigation-runs.sqlite"
    if not journal.is_file():
        return
    drafts = []
    try:
        with closing(sqlite3.connect(journal.resolve().as_uri() + "?mode=ro", uri=True)) as database:
            for run_id in dict.fromkeys(row["run_id"] for row in requests if row.get("state") != "completed"):
                row = database.execute("SELECT checkpoint_json FROM investigation_runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    continue
                checkpoint = json.loads(row[0])
                draft = (checkpoint.get("request_payload") or {}).get("draft_answer")
                if isinstance(draft, str) and draft.strip():
                    drafts.append({"run_id": run_id, "text": draft, "answer_sha256": hashlib.sha256(draft.encode()).hexdigest(),
                        "origin": "interrupted_request_draft", "verification": "unverified",
                        "citation_authority": "not_granted", "model_answer_recorded": False})
    except (OSError, ValueError, sqlite3.Error, KeyError):
        report["draft_recovery_status"] = "unavailable_attention_required"
    if drafts:
        report["interrupted_unverified_drafts"] = drafts


def require_remote_profile(settings):
    target = urlsplit(settings.get("COMPANY_API_BASE_URL", ""))
    host = (target.hostname or "").lower()
    loopback = host == "localhost" or host.endswith(".localhost")
    try:
        loopback |= ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    require(target.scheme == "https" and bool(host) and not loopback,
            "LIVE_REMOTE_MODEL_PROFILE_REQUIRED")


def validate_gold(source, items, framework):
    """Reject an unfrozen question set before any remote request is possible."""
    for item in items:
        for turn in _turns(item):
            merged = {**item, **turn}
            require(bool(_required_ranges(merged)), "LIVE_GOLD_SUPPORTING_RANGES_REQUIRED")
            context = _reviewed_context(source, merged, framework)
            require(bool(context["sources"]), "LIVE_REVIEWED_SOURCE_REQUIRED")
            hashes = {row["path"]: row["source_sha256"] for row in context["sources"]}
            for span in _required_ranges(merged):
                require(hashes.get(span["path"]) == span["source_sha256"], "LIVE_GOLD_SOURCE_MISMATCH")
                require(_range_covered(span, context["sources"]), "LIVE_GOLD_RANGE_NOT_FROZEN")
            require(not merged.get("history"), "LIVE_INITIAL_HISTORY_REQUIRES_REAL_TURNS")


def live_row(observation, item, runtime, cases_sha256, model):
    """Bind review identity to the actual EXE trace, including trimmed history."""
    require(observation["job"]["status"] == "COMPLETED", "LIVE_WORKBENCH_JOB_FAILED")
    output = observation.get("result") or {}
    agent = output.get("agent_result") or {}
    metrics = agent.get("metrics") or {}
    trace_path = Path(metrics["quality_trace_path"])
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    journal = read_journal(trace_path.parent.parent)
    requests = [row for row in journal["requests"] if row["run_id"] == trace["run_id"]]
    require(bool(requests) and len(requests) == metrics.get("model_requests"), "LIVE_REQUEST_JOURNAL_MISMATCH")
    request_hashes = {row["request_hash"] for row in requests}
    final = trace.get("final") or {}
    round_ids = set(final.get("final_answer_round_ids", []))
    if final.get("final_answer_round_id"):
        round_ids.add(final["final_answer_round_id"])
    rounds = [row for row in trace.get("rounds", []) if row.get("round_id") in round_ids]
    require(bool(rounds), "LIVE_ANSWER_REQUEST_UNAVAILABLE")
    require(all(row.get("request_body_sha256") in request_hashes for row in rounds),
            "LIVE_TRACE_JOURNAL_MISMATCH")
    system_hashes = {row.get("system_prompt_sha256") for row in rounds}
    require(len(system_hashes) == 1 and None not in system_hashes, "LIVE_ACTUAL_PROMPT_UNAVAILABLE")
    require(all(isinstance(row.get("context", {}).get("history"), list) for row in rounds),
            "LIVE_ACTUAL_HISTORY_UNAVAILABLE")
    # A revised/continued answer uses the final request's actual frozen history.
    history = rounds[-1]["context"]["history"]
    context = {kind: [entry for row in rounds for entry in row.get(kind, [])]
               for kind in ("sources", "framework")}
    answer = agent.get("answer", "")
    require(bool(answer.strip()) and agent.get("model_answer_recorded") is True, "LIVE_ANSWER_UNAVAILABLE")
    visible = (observation["state"].get("conversation") or {}).get("messages", [])
    require(visible and visible[-1].get("content") == answer, "LIVE_VISIBLE_ANSWER_MISMATCH")
    require(trace.get("configuration", {}).get("model_fingerprint") == hashlib.sha256(model.encode()).hexdigest(),
            "LIVE_MODEL_IDENTITY_MISMATCH")
    row = {"id": item["id"], "turn_id": item["turn_id"], "question": item["question"],
        "conversation": item["conversation"], "history": history, "run_id": trace["run_id"],
        "answer": answer, "agent_status": agent.get("status"), "runner_status": output.get("runner_status"),
        "reason_code": output.get("reason_code", agent.get("reason_code")), "stop_reason": agent.get("stop_reason"),
        "remote_result_unknown": any(part.get("state") != "completed" for part in requests),
        "truncated": agent.get("answer_truncated", False), "elapsed_seconds": observation["elapsed_seconds"],
        "model_requests": metrics.get("model_requests"), "metrics": metrics,
        "policy": trace["configuration"]["policy"], "quality_status": "not_reviewed",
        "supplied_evidence": _evidence_metadata(context),
        "expected_source_paths": item.get("expected_source_paths", [])}
    row["measurement"] = _measurement(row, runtime, cases_sha256)
    row["review_identity"] = _review_identity(run_id=row["run_id"], case_id=row["id"],
        turn_id=row["turn_id"], branch="automatic", answer=answer, model=model,
        prompt={"version": trace.get("prompt_version", "unknown"), "system_sha256": system_hashes.pop(),
                "request_sha256": [part["request_body_sha256"] for part in rounds]},
        policy=row["policy"], gold=item.get("gold") or {}, context=context,
        source_snapshot_id=trace.get("analysis_revision", agent.get("snapshot_id")),
        framework_revision=item.get("framework_revision"), question=item["question"], history=history,
        runtime_provenance=runtime, measurement=row["measurement"])
    row["acceptance_diagnostics"] = capture_diagnostics(trace, agent, trace_sha256=digest(trace_path))
    identity = row["review_identity"]
    identity["acceptance_diagnostics_sha256"] = _fingerprint(row["acceptance_diagnostics"])
    identity["identity_sha256"] = _fingerprint({key: value for key, value in identity.items()
                                              if key != "identity_sha256"})
    row["actual_evidence_context"] = {"sources": [entry for part in rounds
        for entry in part["context"].get("sources", [])], "framework": [entry for part in rounds
        for entry in part["context"].get("framework", [])]}
    return row


def evaluate_native(*, archive, source_commit, source, cases, output, profile_config=None,
                    allow_network=False, request_budget=None, max_model_requests=5,
                    max_question_seconds=120, hard_question_seconds=None, source_format="auto", encoding="auto",
                    framework="", ca_file=None, compilation_context=None, office_checks=(), plan=False):
    source, cases = Path(source).resolve(strict=True), Path(cases).resolve(strict=True)
    output = Path(output).resolve()
    require(source.is_dir() and not source.is_relative_to(output) and not output.is_relative_to(source),
            "LIVE_OUTPUT_SOURCE_OVERLAP")
    items = _cases(cases)
    scope_errors = []
    require(_live_case_scope(items, scope_errors), "LIVE_CASE_SCOPE_INCOMPLETE")
    office_checks = list(dict.fromkeys([*OFFICE_BASELINE_CHECKS, *office_checks]))
    framework = str(Path(framework).resolve(strict=True)) if framework else ""
    validate_gold(source, items, framework)
    artifact = inspect_package(archive, source_commit)
    runtime = {**artifact, "transport_kind": "remote_http"}
    context_path = Path(compilation_context).resolve(strict=True) if compilation_context else None
    if context_path:
        runtime["compilation_context_sha256"] = digest(context_path)
    source_options = {"source_format": source_format, "encoding": encoding}
    if context_path:
        source_options["compilation_context_path"] = str(context_path)
    policy = AgentPolicy(max_model_requests=max_model_requests, max_question_seconds=max_question_seconds,
                         hard_question_seconds=hard_question_seconds)
    wait_policy = execution_wait_policy(policy)
    turns = sum(len(_turns(item)) for item in items)
    maximum = turns * policy.max_model_requests
    report = {"schema_version": "live-business-eval/v3", "evaluation_run_id": secrets.token_hex(12),
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "runtime_provenance": runtime,
        "cases_sha256": digest(cases), "case_count": len(items), "cases": [], "errors": [],
        "plan": {"mode": "automatic", "user_turn_count": turns, "max_model_requests": maximum,
                 "requests_per_turn": policy.max_model_requests, "preflight_model_requests": 0,
                 "execution_wait": wait_policy,
                 "retry_policy": "no_automatic_retries", "policy": policy.to_dict(),
                 "office_required_checks": office_checks},
        "index_seconds": None, "latency_seconds": {"p50": None, "p95": None},
        "observed_model_requests": 0, "remote_result_unknown": False,
        "quality_status": "not_reviewed", "status": "planned"}
    from office_acceptance_checks import office_environment
    report["office_environment"] = office_environment()
    output.mkdir(parents=True, exist_ok=False)
    report_path = output / "LIVE-ACCEPTANCE.json"
    _safe_write(report_path, report)
    if plan or not allow_network:
        return report
    require(sys.platform == "win32", "NATIVE_WINDOWS_EXECUTION_REQUIRED")
    require(type(request_budget) is int and request_budget >= maximum, "LIVE_REQUEST_BUDGET_INSUFFICIENT")
    require(profile_config is not None, "LIVE_EXPLICIT_PROFILE_REQUIRED")
    config_path = Path(profile_config).resolve(strict=True)
    require(config_path.is_file() and not config_path.is_relative_to(output), "LIVE_PROFILE_INVALID")
    settings = _read_local_env(config_path)
    require_remote_profile(settings)
    model = settings.get("COMPANY_CHAT_MODEL", "")
    require(bool(model) and settings.get("COMPANY_API_BASE_URL") and settings.get("COMPANY_API_KEY"),
            "LIVE_PROFILE_INCOMPLETE")
    environment = isolated_environment()
    if ca_file:
        environment["SSL_CERT_FILE"] = str(Path(ca_file).resolve(strict=True))
    profile = output / "private-profile"
    profile.mkdir()
    shutil.copyfile(config_path, profile / ".env")
    (profile / "agent-settings.json").write_text(json.dumps({"version": 1, "agent": policy.to_dict()}), encoding="utf-8")
    environment["AGENT_SETTINGS_PATH"] = str(profile / "agent-settings.json")
    binary = extract_package(archive, output / "extracted")
    require(digest(binary) == artifact["binary_sha256"], "LIVE_EXTRACTED_BINARY_MISMATCH")
    report.update(status="running", configuration={"model_fingerprint": hashlib.sha256(model.encode()).hexdigest()})
    _safe_write(report_path, report)
    conversation_ids = {}
    request_count = 0
    with NativeWorkbench([binary], profile, environment, output / "runtime", capture_context=True) as workbench:
        started = time.monotonic()
        index = workbench.analyze(source, output / "workspace", timeout=3600,
                                  **source_options, allow_network=False)
        require(index["job"]["status"] == "COMPLETED", "LIVE_INDEX_FAILED")
        report["index_seconds"] = time.monotonic() - started
        diagnosis = json.loads((output / "workspace/diagnosis.json").read_text(encoding="utf-8"))
        scope, search = diagnosis.get("scope", {}), diagnosis.get("repository_search", {})
        report["source_inventory"] = {"file_count": scope.get("file_count"),
            "source_bytes": scope.get("total_scope_bytes"), "program_count": diagnosis.get("program_count"),
            "index_snapshot_id": diagnosis.get("build_report", {}).get("snapshot_id"),
            "indexed_files": search.get("indexed_files"),
            "content_hash_verified": search.get("content_hash_verified"),
            "scope": "index_snapshot_not_live_repository", "source_options": diagnosis.get("source_options")}
        inventory = report["source_inventory"]
        require(all(type(inventory.get(key)) is int and inventory[key] > 0
                    for key in ("file_count", "source_bytes", "indexed_files"))
                and bool(inventory.get("index_snapshot_id")), "LIVE_INDEX_INVENTORY_UNAVAILABLE")
        _safe_write(report_path, report)
        for item in items:
            for turn in _turns(item):
                merged = {**item, **turn, "id": item["id"], "turn_id": turn["id"],
                          "conversation": item.get("conversation", item["id"])}
                try:
                    require(request_count + policy.max_model_requests <= request_budget, "LIVE_REQUEST_BUDGET_EXHAUSTED")
                    require(digest(cases) == report["cases_sha256"], "LIVE_CASES_CHANGED_DURING_EVALUATION")
                    if context_path:
                        require(digest(context_path) == runtime["compilation_context_sha256"],
                                "LIVE_COMPILATION_CONTEXT_CHANGED")
                    validate_gold(source, [{key: value for key, value in merged.items() if key != "turns"}], framework)
                    observation = workbench.analyze(source, output / "workspace", merged["question"],
                        conversation_id=conversation_ids.get(merged["conversation"]),
                        timeout=wait_policy["watchdog_seconds"], **source_options,
                        answer_detail="detailed", framework_reference_path=framework)
                    row = live_row(observation, merged, runtime, report["cases_sha256"], model)
                    require(type(row["model_requests"]) is int and row["model_requests"] <= policy.max_model_requests,
                            "LIVE_REQUEST_COUNT_INVALID")
                    request_count += row["model_requests"]
                    report["cases"].append(row)
                    report.update(observed_model_requests=request_count,
                                  remote_result_unknown=row["remote_result_unknown"])
                    _safe_write(report_path, report)
                    # A retained answer can coexist with an unfinished revision
                    # request. Archive it, then stop the whole paid suite before
                    # using its conversation for any subsequent question.
                    require(row["remote_result_unknown"] is False, "LIVE_REMOTE_REQUEST_OUTCOME_UNKNOWN")
                    conversation_ids[merged["conversation"]] = observation["state"]["conversation"]["id"]
                except Exception as error:
                    _capture_interrupted_requests(report, output / "workspace")
                    report["errors"].append({"case_id": merged["id"], "turn_id": merged["turn_id"],
                                             "error_type": type(error).__name__})
                    report["status"] = "interrupted_attention_required"
                    _safe_write(report_path, report)
                    # An uncertain request may have incurred a charge. Do not
                    # automatically rerun it or continue with contaminated history.
                    raise
    durations = [row["elapsed_seconds"] for row in report["cases"]]
    require(inspect_package(archive, source_commit) == artifact, "LIVE_PACKAGE_CHANGED_DURING_EVALUATION")
    report.update(status="completed_not_reviewed", observed_model_requests=request_count,
                  latency_seconds={"p50": statistics.median(durations), "p95": _percentile(durations, .95)})
    _safe_write(report_path, report)
    template = review_template(items, report)
    for row, review in zip(report["cases"], template["reviews"]):
        if row.get("agent_status") == "PARTIAL":
            review["partial_review"] = {"classification": None, "reason": "",
                                        "affected_findings": [], "resolved": None}
    template["office_acceptance"] = {**{key: artifact[key] for key in
        ("source_commit", "archive_sha256", "binary_sha256")}, "evaluation_run_id": report["evaluation_run_id"],
        "live_report_sha256": digest(report_path), "reviewer": "", "reviewed_at": None,
        "checks": {name: None for name in office_checks}}
    _safe_write(output / "HUMAN-REVIEW-TEMPLATE.json", template)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("archive", "source", "cases", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--profile-config", type=Path)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--request-budget", type=int)
    parser.add_argument("--max-model-requests", type=int, default=5)
    parser.add_argument("--max-question-seconds", type=int, default=120)
    parser.add_argument("--hard-question-seconds", type=int)
    parser.add_argument("--source-format", choices=("auto", "fixed", "free"), default="auto")
    parser.add_argument("--encoding", default="auto")
    parser.add_argument("--framework", default="")
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--compilation-context", type=Path)
    parser.add_argument("--office-check", action="append", dest="office_checks", default=[])
    args = parser.parse_args(argv)
    report = evaluate_native(**vars(args))
    print(json.dumps({"status": report["status"], "cases": len(report["cases"]), "quality_status": "not_reviewed"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
