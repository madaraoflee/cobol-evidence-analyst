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

from business_analysis import _TextResponseError, _extract_text
from agent_policy import resolve_agent_policy
from business_chat import _history_messages, _usage_report, run_business_chat
from business_index import build_business_index
from company_api import (APIClientError, APIConfigurationError, CompanyAPIConfig,
                         OpenAICompatibleChatClient, PROJECT_ENV_FILE, _read_local_env)
from repository_discovery import ensure_repository_search
from runtime_settings import load_agent_policy
from model_profiles import model_config
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
            and isinstance(turn.get("question"), str) and turn["question"].strip() for turn in turns)
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
        counts[case["quality_status"]] += 1
    status = "complete" if counts["complete"] == len(cases) and cases else (
        "partial" if counts["partial"] or counts["complete"] else "not_reviewed")
    return status, counts


def review_template(items, report):
    """Export an unscored human review form; empty fields never imply a pass."""
    runs = {(row["id"], row["turn_id"]): row.get("run_id") for row in report.get("cases", [])}
    return {"schema_version": "business-human-review/v1", "reviews": [{
        "case_id": item["id"], "turn_id": turn["id"],
        "run_id": runs.get((item["id"], turn["id"])), "reviewer": "", "reviewed_at": None,
        "required_findings": [{"id": finding["id"], "result": None, "note": ""}
            for finding in (turn.get("gold", item.get("gold", {})) or {}).get("required_findings", [])],
        "forbidden_claims": [{"id": claim.get("id", str(index)) if isinstance(claim, dict) else str(index),
                              "present": None, "note": ""}
            for index, claim in enumerate((turn.get("gold", item.get("gold", {})) or {}).get("forbidden_claims", []), 1)],
        "usefulness": None, "followup_continuity": None, "comments": ""
    } for item in items for turn in _turns(item)]}


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
    reviews = {}
    if reviews_file is not None and Path(reviews_file).is_file():
        review_data = json.loads(Path(reviews_file).read_text(encoding="utf-8"))
        reviews = {(row.get("case_id"), row.get("turn_id")): row for row in review_data.get("reviews", [])}
    missing_captures = [f"{item['id']}/{turn['id']}" for item in items for turn in _turns(item)
        if mode == "paired-synthesis" and not (evaluation_dir / "captures" / f"{item['id']}-{turn['id']}.json").is_file()]
    report = {"schema_version": "live-business-eval/v2", "created_at_utc": datetime.now(timezone.utc).isoformat(),
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
                       "workbench_defaults": {"timeout_seconds": 60.0, "max_output_tokens": 2048},
                       "matches_workbench_defaults": config.timeout_seconds == 60.0 and config.max_output_tokens == 2048,
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
                    "mode": mode, "quality_status": _review_status(turn, reviews.get((item["id"], turn["id"]))),
                    "human_review": reviews.get((item["id"], turn["id"])), "branches": []}
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
                        messages = synthesis_messages(turn["question"], history, context)
                        request = json.dumps({"model": config.chat_model, "messages": messages,
                            "max_tokens": config.max_output_tokens}, ensure_ascii=False,
                            separators=(",", ":")).encode()
                        if len(request) > policy.max_request_bytes:
                            raise ValueError("SYNTHESIS_CONTEXT_TOO_LARGE")
                        response = client.complete(messages=messages)
                        answer = _extract_text(response)
                        case_result["branches"].append({"name": branch_name, "answer": answer.text,
                            "request_bytes": len(request), "request_sha256": hashlib.sha256(request).hexdigest(),
                            "usage": response.get("usage"), "truncated": answer.truncated})
                except (ValueError, OSError, APIClientError, _TextResponseError) as exc:
                    case_result["error"] = getattr(exc, "code", str(exc))
                case_result["elapsed_seconds"] = round(time.monotonic() - started, 3)
                durations.append(case_result["elapsed_seconds"])
                report["cases"].append(case_result)
        report["latency_seconds"] = {"p50": round(statistics.median(durations), 3) if durations else None,
                                     "p95": _percentile(durations, .95)}
        report["quality_status"], report["review_counts"] = _review_summary(report["cases"])
        return report

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
                  "turn_id": item["turn_id"], "quality_status": _review_status(item, reviews.get((item["id"], item["turn_id"]))),
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
                  "review_checks": item.get("review_checks", []),
                  "human_review": reviews.get((item["id"], item["turn_id"]))}
        report["cases"].append(result)
        if capture_context and metrics.get("quality_trace_path"):
            trace = json.loads(Path(metrics["quality_trace_path"]).read_text(encoding="utf-8"))
            result["run_id"] = trace.get("run_id")
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
    report["quality_status"], report["review_counts"] = _review_summary(report["cases"])
    return report


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
