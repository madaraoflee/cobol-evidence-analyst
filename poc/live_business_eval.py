#!/usr/bin/env python3
"""Measure real business answers against reviewed questions on a local source snapshot.

The report stays local. A source-free model ping runs before indexing or sending
any COBOL excerpt. This tool never treats retrieval alone as answer quality.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time
import hashlib
import secrets
import subprocess

from business_analysis import _TextResponseError, _extract_text
from agent_policy import resolve_agent_policy
from business_chat import _history_messages, _usage_report, run_business_chat
from business_index import build_business_index
from company_api import (APIClientError, APIConfigurationError, CompanyAPIConfig,
                         OpenAICompatibleChatClient, PROJECT_ENV_FILE, _read_local_env)
from repository_discovery import ensure_repository_search
from runtime_settings import load_agent_policy
from model_profiles import WORKBENCH_MAX_OUTPUT_TOKENS, model_config
from business_chat import _SYSTEM
from source_reading import _safe_file


def _cases(path: Path) -> list[dict]:
    document = json.loads(path.read_text(encoding="utf-8"))
    items = document.get("cases") if isinstance(document, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("EVALUATION_CASES_INVALID")
    seen = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            raise ValueError("EVALUATION_CASES_INVALID")
        turns = item.get("turns")
        single = isinstance(item.get("question"), str) and bool(item["question"].strip())
        multiple = isinstance(turns, list) and bool(turns) and all(
            isinstance(turn, dict) and isinstance(turn.get("id"), str)
            and bool(turn["id"].strip())
            and isinstance(turn.get("question"), str) and turn["question"].strip() for turn in turns)
        if multiple and len({turn["id"] for turn in turns}) != len(turns):
            raise ValueError("EVALUATION_CASES_INVALID")
        if item["id"] in seen or single == multiple:
            raise ValueError("EVALUATION_CASES_INVALID")
        if any(not isinstance(item.get(key, []), list) or
               any(not isinstance(value, str) for value in item.get(key, []))
               for key in ("expected_source_paths", "review_checks")):
            raise ValueError("EVALUATION_CASES_INVALID")
        seen.add(item["id"])
    return items


def _framework_path(value: str | None) -> str:
    if value is None:
        local = _read_local_env(PROJECT_ENV_FILE)
        value = os.environ.get("FRAMEWORK_REFERENCE_PATH", local.get("FRAMEWORK_REFERENCE_PATH", ""))
    if not value:
        return ""
    path = Path(value).expanduser()
    return str((PROJECT_ENV_FILE.parent / path).resolve() if not path.is_absolute() else path.resolve())


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 3)


def _safe_write(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


SYNTHESIS_PROMPT_VERSION = "business-final-synthesis-v1"
REVIEW_IDENTITY_VERSION = "business-review-identity/v2"


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _runtime_provenance(transport):
    """Record the implementation actually loaded; this runner is never a native app."""
    root = Path(__file__).resolve().parent
    files = sorted([*root.glob("*.py"), *[path for path in (root / "web").glob("*") if path.is_file()]])
    implementation = hashlib.sha256()
    for path in files:
        name = path.relative_to(root).as_posix().encode()
        data = path.read_bytes()
        implementation.update(len(name).to_bytes(8, "big") + name)
        implementation.update(len(data).to_bytes(8, "big") + data)
    commit, dirty = None, None
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                                capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"],
                     cwd=root, check=True, capture_output=True, text=True, timeout=5).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return {"schema_version": "business-runtime-provenance/v1",
            "execution_surface": "source_python", "runtime_platform": sys.platform,
            "transport_kind": "synthetic" if transport is not None else "remote_http",
            "source_commit": commit, "source_dirty": dirty,
            "implementation_sha256": implementation.hexdigest()}


def _measurement(row, runtime, cases_sha256, *, scope="full_question"):
    """Bind timing to the same answer and run as the human review."""
    return {"scope": scope, "case_id": row["id"], "turn_id": row["turn_id"],
            "branch": row.get("name", "automatic"), "run_id": row["run_id"],
            "answer_sha256": hashlib.sha256(row.get("answer", "").encode()).hexdigest(),
            "elapsed_seconds": row["elapsed_seconds"], "cases_sha256": cases_sha256,
            "runtime_sha256": _fingerprint(runtime)}


def _evidence_metadata(context):
    """Keep actual supplied evidence identity without copying source into the form."""
    sources = []
    for row in context.get("sources", []):
        sources.append({"evidence_id": row.get("evidence_id"),
            "path": row.get("path", row.get("relative_path")),
            "source_sha256": row.get("source_sha256"),
            "start_line": row.get("start_line"), "end_line": row.get("end_line"),
            "excerpt_sha256": row.get("excerpt_sha256") or (
                hashlib.sha256(row["excerpt"].encode()).hexdigest() if "excerpt" in row else None),
            "include_chain": row.get("include_chain", [])})
    framework = []
    for row in context.get("framework", []):
        framework.append({"reference_id": row.get("reference_id"),
            "document": row.get("document_name", row.get("document")),
            "document_sha256": row.get("document_sha256"),
            "range": row.get("range") or {"start_line": row.get("start_line"),
                                           "end_line": row.get("end_line")},
            "excerpt_sha256": row.get("excerpt_sha256") or (
                hashlib.sha256(row["excerpt"].encode()).hexdigest() if "excerpt" in row else None)})
    return {"sources": sorted(sources, key=_fingerprint),
            "framework": sorted(framework, key=_fingerprint)}


def _review_identity(*, run_id, case_id, turn_id, branch, answer, model, prompt,
                     policy, gold, context, source_snapshot_id=None,
                     framework_revision=None, evidence_status="supplied", question="", history=(),
                     runtime_provenance=None, measurement=None):
    evidence = _evidence_metadata(context)
    identity = {"schema_version": REVIEW_IDENTITY_VERSION, "run_id": run_id,
        "case_id": case_id, "turn_id": turn_id, "branch": branch,
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "history_sha256": _fingerprint(history),
        "answer_sha256": hashlib.sha256(answer.encode()).hexdigest(), "model": model,
        "prompt": prompt, "policy_sha256": _fingerprint(policy),
        "gold_sha256": _fingerprint(gold), "source_snapshot_id": source_snapshot_id,
        "source_versions": sorted({row["source_sha256"] for row in evidence["sources"]
                                   if row.get("source_sha256")}),
        "framework_revision": framework_revision,
        "framework_versions": sorted({row["document_sha256"] for row in evidence["framework"]
                                      if row.get("document_sha256")}),
        "evidence_fingerprint_status": evidence_status,
        "evidence_sha256": _fingerprint(evidence)}
    # Optional extensions preserve archived v2 identities. Release acceptance
    # requires these extensions; historical review display does not.
    if runtime_provenance is not None:
        identity["runtime_sha256"] = _fingerprint(runtime_provenance)
    if measurement is not None:
        identity["measurement_sha256"] = _fingerprint(measurement)
    identity["identity_sha256"] = _fingerprint(identity)
    return identity


def _review_correctness(turn, review):
    if not review:
        return "not_reviewed"
    gold = turn.get("gold") or {}
    if not gold.get("required_findings") and not gold.get("forbidden_claims"):
        return "pending"
    if any(row.get("result") == "incorrect" for row in review.get("required_findings", [])) or any(
            row.get("present") is True for row in review.get("forbidden_claims", [])):
        return "incorrect"
    if any(row.get("result") == "omitted" for row in review.get("required_findings", [])):
        return "incomplete"
    return "correct" if _review_status(turn, review) == "complete" else "pending"


def _bind_review(row, turn, reviews):
    """A complete form is useful only for the exact archived answer it reviewed."""
    identity = row.get("review_identity")
    candidates = [review for review in reviews if review.get("case_id") == identity["case_id"]
                  and review.get("turn_id") == identity["turn_id"]
                  and review.get("branch", "automatic") == identity["branch"]]
    matched = next((review for review in candidates
        if review.get("identity") == identity and review.get("run_id") == identity["run_id"]), None)
    row["human_review"] = matched
    row["review_binding_status"] = "matched" if matched else "stale" if candidates else "not_reviewed"
    row["rejected_reviews"] = [{"run_id": review.get("run_id"),
        "reason": "missing_identity" if not review.get("identity") else "identity_mismatch"}
        for review in candidates if review is not matched]
    row["quality_status"] = _review_status(turn, matched)
    row["review_completion"] = row["quality_status"]
    row["correctness_status"] = _review_correctness(turn, matched)


def apply_reviews(report, items, reviews):
    """Attach reviews to saved answers offline; rerunning a model creates new identities."""
    turns = {(item["id"], turn["id"]): {**item, **turn}
             for item in items for turn in _turns(item)}
    for case in report.get("cases", []):
        turn = turns.get((case["id"], case["turn_id"]))
        if turn is None:
            continue
        rows = case.get("branches") or [case]
        for row in rows:
            identity = row.get("review_identity")
            valid_identity = (isinstance(identity, dict)
                and identity.get("schema_version") == REVIEW_IDENTITY_VERSION
                and identity.get("identity_sha256") == _fingerprint({
                    key: value for key, value in identity.items() if key != "identity_sha256"})
                and identity.get("answer_sha256") == hashlib.sha256(row.get("answer", "").encode()).hexdigest()
                and identity.get("case_id") == case["id"]
                and identity.get("turn_id") == case["turn_id"]
                and identity.get("branch") == row.get("name", "automatic")
                and identity.get("question_sha256") == hashlib.sha256(case.get("question", "").encode()).hexdigest()
                and case.get("question", "") == turn.get("question", "")
                and identity.get("gold_sha256") == _fingerprint(turn.get("gold") or {}))
            if valid_identity and "runtime_sha256" in identity:
                runtime = row.get("runtime_provenance", report.get("runtime_provenance"))
                valid_identity = (isinstance(runtime, dict)
                                  and identity["runtime_sha256"] == _fingerprint(runtime))
            if valid_identity and "measurement_sha256" in identity:
                measurement = row.get("measurement")
                valid_identity = (isinstance(measurement, dict)
                    and identity["measurement_sha256"] == _fingerprint(measurement)
                    and measurement.get("elapsed_seconds") == row.get("elapsed_seconds")
                    and measurement.get("answer_sha256") == identity["answer_sha256"]
                    and measurement.get("run_id") == row.get("run_id"))
            if not valid_identity:
                row.update(human_review=None, review_binding_status="stale",
                           quality_status="not_reviewed", review_completion="not_reviewed",
                           correctness_status="not_reviewed")
                continue
            _bind_review(row, turn, reviews)
        if case.get("branches"):
            case["quality_status"], _ = _review_summary(rows)
            case["review_completion"] = case["quality_status"]
    report["quality_status"], report["review_counts"] = _review_summary(report.get("cases", []))
    report["review_completion"] = report["quality_status"]
    rows = [row for case in report.get("cases", []) for row in (case.get("branches") or [case])]
    report["correctness_counts"] = {status: sum(row.get("correctness_status") == status for row in rows)
        for status in ("not_reviewed", "pending", "correct", "incomplete", "incorrect")}
    accepted = [row["human_review"] for row in rows if row.get("human_review")]
    report["reviewed_findings"] = {result: sum(finding.get("result") == result
        for review in accepted for finding in review.get("required_findings", []))
        for result in ("met", "omitted", "incorrect")}
    report["unsupported_claims_present"] = sum(claim.get("present") is True
        for review in accepted for claim in review.get("forbidden_claims", []))
    report["stale_review_count"] = sum(row.get("review_binding_status") == "stale" for row in rows)
    return report


def _turns(item):
    return item["turns"] if item.get("turns") else [{**item, "id": item["id"]}]


def _source_excerpt(source, item):
    relative = item["path"]
    path = _safe_file(source, relative)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != item["sha256"]:
        raise ValueError("REVIEWED_SOURCE_CHANGED")
    lines = raw.decode(item.get("encoding", "utf-8")).splitlines()
    start, end = item["start_line"], item["end_line"]
    if type(start) is not int or type(end) is not int or start < 1 or end < start or end > len(lines):
        raise ValueError("REVIEWED_RANGE_INVALID")
    excerpt = "\n".join(lines[start - 1:end])
    return {"path": relative, "source_sha256": digest, "start_line": start,
            "end_line": end, "excerpt": excerpt,
            "excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
            "role": item.get("role")}


def _reviewed_context(source, turn, framework_path):
    reviewed = turn.get("reviewed_context") or {}
    sources = [_source_excerpt(source, item) for item in reviewed.get("source_ranges", [])]
    framework = []
    for item in reviewed.get("framework_ranges", []):
        path = Path(item["document"]).expanduser().resolve(strict=True)
        if framework_path and path != Path(framework_path).resolve():
            raise ValueError("REVIEWED_FRAMEWORK_PATH_INVALID")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("REVIEWED_FRAMEWORK_CHANGED")
        lines = raw.decode("utf-8").splitlines()
        start, end = item["start_line"], item["end_line"]
        if start < 1 or end < start or end > len(lines):
            raise ValueError("REVIEWED_RANGE_INVALID")
        excerpt = "\n".join(lines[start - 1:end])
        framework.append({"document": item["document"], "document_sha256": item["sha256"],
                          "start_line": start, "end_line": end, "excerpt": excerpt,
                          "excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest()})
    return {"sources": sources, "framework": framework}


def _frozen_context(path):
    artifact = json.loads(path.read_text(encoding="utf-8"))
    if artifact.get("prompt_version") != SYNTHESIS_PROMPT_VERSION:
        raise ValueError("CAPTURE_PROMPT_MISMATCH")
    context = artifact.get("context")
    if not isinstance(context, dict):
        raise ValueError("CAPTURE_MISSING")
    for kind in ("sources", "framework"):
        for item in context.get(kind, []):
            if hashlib.sha256(item["excerpt"].encode()).hexdigest() != item["excerpt_sha256"]:
                raise ValueError("CAPTURE_HASH_MISMATCH")
    return artifact


def _validate_frozen_sources(source, artifact, framework_path):
    for item in artifact["context"].get("sources", []):
        relative = item["path"]
        path = _safe_file(source, relative)
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["source_sha256"]:
            raise ValueError("CAPTURE_SOURCE_CHANGED")
        lines = path.read_text(encoding=item.get("encoding", "utf-8")).splitlines()
        start, end = item["start_line"], item["end_line"]
        if start < 1 or end < start or end > len(lines):
            raise ValueError("CAPTURE_RANGE_INVALID")
        if "\n".join(lines[start - 1:end]) != item["excerpt"]:
            raise ValueError("CAPTURE_RANGE_MISMATCH")
    for item in artifact["context"].get("framework", []):
        if not framework_path:
            raise ValueError("CAPTURE_FRAMEWORK_UNVERIFIABLE")
        root = Path(framework_path).resolve(strict=True)
        path = (root / item.get("document_name", "")).resolve(strict=True) if root.is_dir() else root
        if root.is_dir() and not path.is_relative_to(root):
            raise ValueError("CAPTURE_FRAMEWORK_PATH_INVALID")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != item.get("document_sha256"):
            raise ValueError("CAPTURE_FRAMEWORK_CHANGED")
        lines = raw.decode("utf-8").splitlines()
        location = item.get("range") or {}
        start, end = location.get("start_line"), location.get("end_line")
        if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end > len(lines):
            raise ValueError("CAPTURE_FRAMEWORK_RANGE_INVALID")
        if item["excerpt"] not in "\n".join(lines[start - 1:end]):
            raise ValueError("CAPTURE_FRAMEWORK_RANGE_MISMATCH")


def synthesis_messages(question, history, context):
    """Only evidence differs between paired branches."""
    return [{"role": "system", "content": _SYSTEM + "\n本次只作最终合成，不请求工具。"},
            *[{"role": row["role"], "content": row["content"]} for row in history],
            {"role": "user", "content": json.dumps({"question": question,
                "source_evidence": context["sources"],
                "framework_evidence": context["framework"]}, ensure_ascii=False)}]


def _review_status(turn, review):
    if not review:
        return "not_reviewed"
    gold = turn.get("gold") or {}
    required = {item["id"] for item in gold.get("required_findings", [])}
    graded = {item.get("id") for item in review.get("required_findings", [])
              if item.get("result") in {"met", "omitted", "incorrect"}}
    forbidden = {item.get("id", str(index)) if isinstance(item, dict) else str(index)
                 for index, item in enumerate(gold.get("forbidden_claims", []), 1)}
    checked = {item.get("id") for item in review.get("forbidden_claims", [])
               if type(item.get("present")) is bool}
    return "complete" if (required <= graded and forbidden <= checked
            and review.get("usefulness") is not None
            and review.get("followup_continuity") is not None) else "partial"


def _review_summary(cases):
    counts = {"not_reviewed": 0, "partial": 0, "complete": 0}
    for case in cases:
        for row in (case.get("branches") or [case]):
            counts[row["quality_status"]] += 1
    status = "complete" if counts["complete"] == sum(counts.values()) and cases else (
        "partial" if counts["partial"] or counts["complete"] else "not_reviewed")
    return status, counts


def review_template(items, report):
    """Export an unscored human review form; empty fields never imply a pass."""
    answers = {(row["id"], row["turn_id"]): row for row in report.get("cases", [])}
    return {"schema_version": "business-human-review/v2", "reviews": [{
        "case_id": item["id"], "turn_id": turn["id"], "branch": row.get("name", "automatic"),
        "run_id": row.get("run_id"), "identity": row.get("review_identity"),
        "reviewer": "", "reviewed_at": None,
        "required_findings": [{"id": finding["id"], "result": None, "note": ""}
            for finding in (turn.get("gold", item.get("gold", {})) or {}).get("required_findings", [])],
        "forbidden_claims": [{"id": claim.get("id", str(index)) if isinstance(claim, dict) else str(index),
                              "present": None, "note": ""}
            for index, claim in enumerate((turn.get("gold", item.get("gold", {})) or {}).get("forbidden_claims", []), 1)],
        "usefulness": None, "followup_continuity": None, "comments": ""
    } for item in items for turn in _turns(item)
      for row in (answers.get((item["id"], turn["id"]), {}).get("branches") or
                  [answers.get((item["id"], turn["id"]), {})])]}


def evaluate(source: Path, evaluation_dir: Path, cases_file: Path, *, allow_network: bool = False,
             source_format: str = "auto", encoding: str = "auto", framework: str | None = None,
             policy=None, plan: bool = False, case_id=None, mode="automatic",
             profile="adapter", timeout_seconds=None, max_output_tokens=None,
             capture_context=False, config=None, transport=None, reviews_file=None,
             preflight=True) -> dict:
    source = source.expanduser().resolve(strict=True)
    evaluation_dir = evaluation_dir.expanduser().resolve()
    if not source.is_dir() or source.is_relative_to(evaluation_dir) or evaluation_dir.is_relative_to(source):
        raise ValueError("EVALUATION_PATHS_OVERLAP")
    items = _cases(cases_file.expanduser().resolve(strict=True))
    if case_id is not None:
        items = [item for item in items if item["id"] == case_id]
        if not items:
            raise ValueError("EVALUATION_CASE_NOT_FOUND")
    if mode not in {"automatic", "reviewed-context", "paired-synthesis"}:
        raise ValueError("EVALUATION_MODE_INVALID")
    policy = resolve_agent_policy(policy) if policy is not None else load_agent_policy()
    config = config or model_config(profile, timeout_seconds=timeout_seconds,
                                    max_output_tokens=max_output_tokens)
    turns = sum(len(_turns(item)) for item in items)
    per_turn = policy.max_model_requests if mode == "automatic" else 2 if mode == "paired-synthesis" else 1
    preflight_count = int(bool(preflight))
    reviews = []
    if reviews_file is not None and Path(reviews_file).is_file():
        review_data = json.loads(Path(reviews_file).read_text(encoding="utf-8"))
        reviews = review_data.get("reviews", [])
        if not isinstance(reviews, list) or any(not isinstance(row, dict) for row in reviews):
            raise ValueError("EVALUATION_REVIEWS_INVALID")
    missing_captures = [f"{item['id']}/{turn['id']}" for item in items for turn in _turns(item)
        if mode == "paired-synthesis" and not (evaluation_dir / "captures" / f"{item['id']}-{turn['id']}.json").is_file()]
    report = {"schema_version": "live-business-eval/v3", "evaluation_run_id": secrets.token_hex(12),
              "runtime_provenance": _runtime_provenance(transport),
              "cases_sha256": hashlib.sha256(cases_file.read_bytes()).hexdigest(),
              "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "configuration": config.safe_summary(), "case_count": len(items),
              "plan": {"case_count": len(items), "user_turn_count": turns, "mode": mode,
                       "profile": profile, "preflight_model_requests": preflight_count,
                       "max_model_requests_per_case": policy.max_model_requests,
                       "max_model_requests": preflight_count + turns * per_turn,
                       "requests_per_turn": per_turn,
                       "missing_frozen_captures": missing_captures,
                       "max_output_tokens_per_request": config.max_output_tokens,
                       "timeout_seconds_per_request": config.timeout_seconds,
                       "preflight_max_output_tokens": 32,
                       "max_request_bytes": policy.max_request_bytes,
                       "preflight_request_bytes": len(json.dumps({
                           "model": config.chat_model, "messages": [{"role": "user", "content": "Reply with OK."}],
                           "max_tokens": 32}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
                       "policy": policy.to_dict(),
                       "configuration_scope": "evaluation_runner",
                       "workbench_defaults": {"timeout_seconds": 60.0, "max_output_tokens": WORKBENCH_MAX_OUTPUT_TOKENS},
                       "matches_workbench_defaults": config.timeout_seconds == 60.0 and config.max_output_tokens == WORKBENCH_MAX_OUTPUT_TOKENS,
                       "comparison_note": "Evaluation uses its recorded configuration; results do not represent the web workbench when timeout or output settings differ.",
                       "spend_authorization": "not_granted_by_request_budget",
                       "currency_estimate": None, "retry_policy": "no_automatic_retries",
                       "sampling": "provider_default", "price_information_required": True,
                       "model_fingerprint": hashlib.sha256(config.chat_model.encode()).hexdigest()},
              "model_preflight": {}, "index_seconds": None, "search_index_seconds": None,
              "cases": [], "latency_seconds": {"p50": None, "p95": None},
              "quality_status": "not_reviewed", "review_counts": {"not_reviewed": 0, "partial": 0, "complete": 0}}
    if plan or not allow_network or missing_captures:
        report["model_preflight"] = {"status": "planned" if plan else "network_not_enabled"}
        return report
    if preflight:
        started = time.monotonic()
        try:
            ping = OpenAICompatibleChatClient(replace(config, max_output_tokens=32),
                                              allow_network=True, transport=transport)
            response = ping.complete(messages=[{"role": "user", "content": "Reply with OK."}])
            text = _extract_text(response)
            if not text.text.strip():
                raise _TextResponseError("MODEL_TEXT_EMPTY")
        except (APIClientError, APIConfigurationError, _TextResponseError) as exc:
            report["model_preflight"] = {"status": "failed", "code": exc.code,
                                         "http_status": getattr(exc, "http_status", None),
                                         "elapsed_seconds": round(time.monotonic() - started, 3)}
            return report
        report["model_preflight"] = {"status": "responded", "elapsed_seconds": round(time.monotonic() - started, 3),
                                     "model_requests": 1, "usage": _usage_report([response.get("usage")], 1)}
    else:
        report["model_preflight"] = {"status": "skipped", "model_requests": 0}

    if mode != "automatic":
        durations = []
        client = OpenAICompatibleChatClient(config, allow_network=True, transport=transport)
        for item in items:
            for turn in _turns(item):
                started = time.monotonic()
                case_result = {"id": item["id"], "turn_id": turn["id"], "question": turn["question"],
                    "mode": mode, "quality_status": "not_reviewed", "branches": []}
                try:
                    reviewed = _reviewed_context(source, turn, _framework_path(framework))
                    branches = [("reviewed", reviewed)]
                    history = turn.get("history", item.get("history", []))
                    if mode == "paired-synthesis":
                        frozen = _frozen_context(evaluation_dir / "captures" / f"{item['id']}-{turn['id']}.json")
                        _validate_frozen_sources(source, frozen, _framework_path(framework))
                        if (frozen.get("question") != turn["question"]
                                or ("history" in turn and frozen.get("history", []) != turn["history"])
                                or frozen.get("source_snapshot_id") != item.get("source_snapshot_id", frozen.get("source_snapshot_id"))
                                or frozen.get("framework_revision") != item.get("framework_revision", frozen.get("framework_revision"))):
                            raise ValueError("CAPTURE_CONTEXT_MISMATCH")
                        history = frozen.get("history", [])
                        branches.insert(0, ("automatic_context", frozen["context"]))
                    for branch_name, context in branches:
                        branch_started = time.monotonic()
                        messages = synthesis_messages(turn["question"], history, context)
                        request = json.dumps({"model": config.chat_model, "messages": messages,
                            "max_tokens": config.max_output_tokens}, ensure_ascii=False,
                            separators=(",", ":")).encode()
                        if len(request) > policy.max_request_bytes:
                            raise ValueError("SYNTHESIS_CONTEXT_TOO_LARGE")
                        response = client.complete(messages=messages)
                        answer = _extract_text(response)
                        branch_result = {"name": branch_name, "answer": answer.text,
                            "id": item["id"], "turn_id": turn["id"],
                            "elapsed_seconds": round(time.monotonic() - branch_started, 3),
                            "run_id": report["evaluation_run_id"] + ":" + branch_name,
                            "quality_status": "not_reviewed",
                            "request_bytes": len(request), "request_sha256": hashlib.sha256(request).hexdigest(),
                            "usage": response.get("usage"), "truncated": answer.truncated}
                        branch_result["measurement"] = _measurement(branch_result,
                            report["runtime_provenance"], report["cases_sha256"], scope="synthesis_only")
                        branch_result["review_identity"] = _review_identity(
                            run_id=branch_result["run_id"], case_id=item["id"], turn_id=turn["id"],
                            branch=branch_name, answer=answer.text, model=config.chat_model,
                            prompt={"version": SYNTHESIS_PROMPT_VERSION,
                                    "system_sha256": hashlib.sha256(messages[0]["content"].encode()).hexdigest(),
                                    "request_sha256": hashlib.sha256(request).hexdigest()},
                            policy=policy.to_dict(), gold=turn.get("gold", item.get("gold", {})) or {},
                            context=context, source_snapshot_id=item.get("source_snapshot_id"),
                            framework_revision=item.get("framework_revision"), question=turn["question"], history=history,
                            runtime_provenance=report["runtime_provenance"], measurement=branch_result["measurement"])
                        case_result["branches"].append(branch_result)
                except (ValueError, OSError, APIClientError, _TextResponseError) as exc:
                    case_result["error"] = getattr(exc, "code", str(exc))
                case_result["elapsed_seconds"] = round(time.monotonic() - started, 3)
                durations.append(case_result["elapsed_seconds"])
                report["cases"].append(case_result)
        report["latency_seconds"] = {"p50": round(statistics.median(durations), 3) if durations else None,
                                     "p95": _percentile(durations, .95)}
        return apply_reviews(report, items, reviews)

    evaluation_dir.mkdir(parents=True, exist_ok=True)
    database = evaluation_dir / "structural-index.sqlite"
    started = time.monotonic()
    build_business_index(source, database, source_format=source_format, encoding=encoding,
                         include_extensionless=True, verify_content=True)
    report["index_seconds"] = round(time.monotonic() - started, 3)
    started = time.monotonic()
    overview = ensure_repository_search(database, source)
    report["search_index_seconds"] = round(time.monotonic() - started, 3)
    report["snapshot_id"] = overview["snapshot_id"]
    report["indexed_files"] = overview["indexed_files"]

    histories: dict[str, list[dict]] = {}
    durations = []
    cases_to_run = [{**item, **turn, "id": item["id"], "turn_id": turn["id"],
                     "conversation": item.get("conversation", item["id"])}
                    for item in items for turn in _turns(item)]
    for item in cases_to_run:
        conversation = item.get("conversation") if isinstance(item.get("conversation"), str) else item["id"]
        history = histories.setdefault(conversation, list(item.get("history", [])))
        frozen_history = _history_messages(history, policy)
        started = time.monotonic()
        output = run_business_chat(item["question"], database, source, config, history=history,
                                   framework_reference_path=_framework_path(framework), allow_network=True,
                                   policy=policy, transport=transport, capture_context=capture_context)
        elapsed = round(time.monotonic() - started, 3)
        durations.append(elapsed)
        agent = output["agent_result"]
        citations = agent.get("narrative", {}).get("citations", [])
        cited_paths = sorted({ref["relative_path"] for ref in citations if ref.get("relative_path")})
        investigation = agent.get("investigation", {})
        navigation_paths = sorted(set(investigation.get("business_map", {}).get("selected_paths", [])))
        retrieved_paths = sorted(set(investigation.get("selected_paths", [])))
        metrics = agent.get("metrics", {})
        expected = list(dict.fromkeys(item.get("expected_source_paths", [])))
        result = {"id": item["id"], "question": item["question"], "conversation": conversation,
                  "history": frozen_history,
                  "turn_id": item["turn_id"], "quality_status": "not_reviewed",
                  "elapsed_seconds": elapsed, "model_requests": metrics.get("model_requests"),
                  "metrics": metrics, "policy": metrics.get("policy", policy.to_dict()),
                  "usage": metrics.get("usage"), "tool_calls": metrics.get("tool_calls", {}),
                  "request_bytes": metrics.get("request_bytes", []),
                  "runner_status": output.get("runner_status"), "answer": agent.get("answer", ""),
                  "run_id": None,
                  "cited_source_paths": cited_paths, "retrieved_source_paths": retrieved_paths,
                  "navigation_source_paths": navigation_paths,
                  "expected_source_paths": expected,
                  "expected_paths_navigated": sorted(set(expected) & set(navigation_paths)),
                  "expected_paths_retrieved": sorted(set(expected) & set(retrieved_paths)),
                  "expected_paths_cited": sorted(set(expected) & set(cited_paths)),
                  "review_checks": item.get("review_checks", [])}
        report["cases"].append(result)
        trace = {}
        if metrics.get("quality_trace_path"):
            trace = json.loads(Path(metrics["quality_trace_path"]).read_text(encoding="utf-8"))
            result["run_id"] = trace.get("run_id")
        result["run_id"] = result["run_id"] or report["evaluation_run_id"] + ":" + item["id"] + ":" + item["turn_id"]
        final = trace.get("final", {})
        final_rounds = set(final.get("final_answer_round_ids", []))
        if final.get("final_answer_round_id"):
            final_rounds.add(final["final_answer_round_id"])
        actual_rounds = [row for row in trace.get("rounds", []) if row.get("round_id") in final_rounds]
        actual_context = {kind: [entry for row in actual_rounds for entry in row.get(kind, [])]
                          for kind in ("sources", "framework")}
        result["supplied_evidence"] = _evidence_metadata(actual_context)
        result["measurement"] = _measurement(result, report["runtime_provenance"], report["cases_sha256"])
        result["review_identity"] = _review_identity(
            run_id=result["run_id"], case_id=item["id"], turn_id=item["turn_id"], branch="automatic",
            answer=result["answer"], model=config.chat_model,
            prompt={"version": trace.get("prompt_version", "unknown"),
                    "system_sha256": hashlib.sha256(_SYSTEM.encode()).hexdigest(),
                    "request_sha256": [row.get("request_body_sha256") for row in actual_rounds]},
            policy=result["policy"], gold=item.get("gold") or {}, context=actual_context,
            source_snapshot_id=trace.get("analysis_revision", agent.get("snapshot_id", overview["snapshot_id"])),
            framework_revision=item.get("framework_revision"),
            evidence_status="supplied" if actual_rounds else "unavailable", question=item["question"], history=frozen_history,
            runtime_provenance=report["runtime_provenance"], measurement=result["measurement"])
        result["supplied_source_paths"] = sorted({row.get("path", row.get("relative_path"))
            for row in actual_context["sources"] if row.get("path", row.get("relative_path"))})
        result["expected_paths_supplied"] = sorted(set(expected) & set(result["supplied_source_paths"]))
        if capture_context and trace:
            round_id = trace.get("final", {}).get("final_answer_round_id")
            answer_round = next((row for row in trace.get("rounds", []) if row["round_id"] == round_id), None)
            if answer_round and answer_round.get("context"):
                context = answer_round["context"]
                artifact = {"schema_version": "frozen-synthesis-context/v1",
                    "prompt_version": SYNTHESIS_PROMPT_VERSION, "question": item["question"],
                    "history": frozen_history,
                    "source_snapshot_id": item.get("source_snapshot_id", overview["snapshot_id"]),
                    "framework_revision": item.get("framework_revision"),
                    "context": {"sources": [{"path": row["path"],
                        "source_sha256": row["source_sha256"],
                        "start_line": row["start_line"], "end_line": row["end_line"],
                        "excerpt": row["excerpt"], "excerpt_sha256": row["excerpt_sha256"]}
                        for row in context.get("sources", [])],
                        "framework": [{"reference_id": row["reference_id"],
                            "document_sha256": row.get("document_sha256"),
                            "document_name": row.get("document_name"),
                            "range": row.get("range"), "excerpt": row["excerpt"],
                            "excerpt_sha256": row["excerpt_sha256"]}
                            for row in context.get("framework", [])]}}
                _safe_write(evaluation_dir / "captures" / f"{item['id']}-{item['turn_id']}.json", artifact)
        history.extend(({"role": "user", "content": item["question"]},
                        {"role": "assistant", "content": agent.get("answer", ""),
                        "evidence_refs": agent.get("evidence_refs", []),
                         "cited_evidence_ids": [ref.get("evidence_id") for ref in citations if ref.get("evidence_id")],
                         "investigation_state": agent.get("investigation_state")}))
    report["latency_seconds"] = {"p50": round(statistics.median(durations), 3),
                                 "p95": _percentile(durations, .95)}
    return apply_reviews(report, items, reviews)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure real model answers and full question latency locally.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--evaluation-dir", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--source-format", choices=("auto", "fixed", "free"), default="auto")
    parser.add_argument("--encoding", default="auto")
    parser.add_argument("--framework")
    parser.add_argument("--case-id")
    parser.add_argument("--mode", choices=("automatic", "reviewed-context", "paired-synthesis"), default="automatic")
    parser.add_argument("--profile", choices=("adapter", "workbench"), default="adapter")
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--capture-context", action="store_true")
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--review-report", type=Path,
                        help="Apply reviews to an archived report locally without generating new answers.")
    parser.add_argument("--review-template", type=Path,
                        help="Write an unscored JSON form for independent human review.")
    parser.add_argument("--no-preflight", action="store_true")
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--plan", "--dry-run", action="store_true",
                        help="Show request bounds without indexing or calling a model; this does not authorize spending.")
    parser.add_argument("--agent-settings", type=Path,
                        help="Optional deployment policy file used by both planning and evaluation.")
    args = parser.parse_args(argv)
    report_path = args.evaluation_dir.expanduser().resolve() / "live-business-eval.json"
    try:
        if args.review_report:
            if not args.reviews:
                raise ValueError("EVALUATION_REVIEWS_REQUIRED")
            report = json.loads(args.review_report.expanduser().resolve(strict=True).read_text(encoding="utf-8"))
            review_data = json.loads(args.reviews.expanduser().resolve(strict=True).read_text(encoding="utf-8"))
            review_items = _cases(args.cases.expanduser().resolve(strict=True))
            report = apply_reviews(report, review_items, review_data.get("reviews", []))
        else:
            report = evaluate(args.source, args.evaluation_dir, args.cases,
                          allow_network=args.allow_network, source_format=args.source_format,
                          encoding=args.encoding, framework=args.framework, plan=args.plan,
                          policy=load_agent_policy(args.agent_settings), case_id=args.case_id,
                          mode=args.mode, profile=args.profile,
                          timeout_seconds=args.timeout_seconds,
                          max_output_tokens=args.max_output_tokens,
                          capture_context=args.capture_context,
                          reviews_file=args.reviews, preflight=not args.no_preflight)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Evaluation setup failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    _safe_write(report_path, report)
    if args.review_template:
        template_items = _cases(args.cases.expanduser().resolve())
        if args.case_id:
            template_items = [item for item in template_items if item["id"] == args.case_id]
        _safe_write(args.review_template.expanduser().resolve(),
                    review_template(template_items, report))
    print(json.dumps({"report": str(report_path), "model_preflight": report["model_preflight"],
                      "index_seconds": report["index_seconds"], "latency_seconds": report["latency_seconds"],
                      "case_count": report["case_count"], "completed_case_count": len(report["cases"]),
                      "plan": report["plan"]}, ensure_ascii=False))
    return 0 if report["model_preflight"]["status"] in {"responded", "planned"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
