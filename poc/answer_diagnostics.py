"""Shareable answer diagnostics: counts and fixed codes, never source or prose."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import subprocess

from api_error_details import sanitize_diagnostic


STOP_REASONS = frozenset({
    "sufficient_material", "MODEL_OUTPUT_TRUNCATED", "answer_incomplete", "request_budget",
    "usable_draft_retained", "source_identity_ambiguous", "source_identity_not_found",
    "unsupported_citations", "evidence_incomplete", "question_evidence_incomplete",
    "evidence_read_incomplete", "working_set_transmission_incomplete", "tool_unavailable",
    "RETRIEVAL_UNRESOLVED", "REQUEST_TIMEOUT", "MODEL_CLIENT_ERROR", "MODEL_RESPONSE_INVALID",
    "MODEL_REFUSED", "MODEL_CONTENT_FILTERED", "MODEL_REQUEST_BUDGET_EXHAUSTED",
    "completed", "model_abstained", "unknown", "other",
})


def _enum(value, allowed):
    return value if isinstance(value, str) and value in allowed else "unknown"


def _number(value):
    return value if type(value) is int and value >= 0 else None


def _finish(value):
    if value is None:
        return "unknown"
    return value if isinstance(value, str) and value in {"stop", "length", "tool_calls", "function_call", "content_filter"} else "other"


def _commit():
    try:
        value = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent,
                               capture_output=True, text=True, timeout=2, check=True).stdout.strip()
        return value if re.fullmatch(r"[a-f0-9]{40,64}", value) else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def response_character_counts(raw, reply=None, *, choice_index=None):
    """Count the selected choice before parsing, without retaining its content."""
    index = _number(getattr(reply, "choice_index", choice_index))
    choices = raw.get("choices", []) if isinstance(raw, dict) else []
    if not isinstance(choices, list):
        choices = []
    content = None
    if index is not None and index < len(choices) and isinstance(choices[index], dict):
        message = choices[index].get("message")
        if isinstance(message, dict):
            content = message.get("content")
    if isinstance(content, list):
        content = "\n".join(part["text"] for part in content if isinstance(part, dict)
                            and part.get("type") in {"text", "output_text"}
                            and isinstance(part.get("text"), str))
    return {"raw_content_characters": len(content) if isinstance(content, str) else None,
            "parsed_answer_characters": len(reply.text) if isinstance(getattr(reply, "text", None), str) else None,
            "choice_index": index}


def build_answer_diagnostics(*, config, quality, result):
    """Project only fixed codes, counts, and a model fingerprint from local data."""
    configuration = quality.get("configuration", {})
    investigation = result.get("investigation", {})
    business_map = investigation.get("business_map", {})
    identity = business_map.get("source_identity", {})
    final = quality.get("final", {})
    question = final.get("question_investigation", {})
    reading = result.get("reading_coverage", {})
    working = investigation.get("working_set", {})
    rounds = []
    for offset, row in enumerate(quality.get("rounds", [])[:64], 1):
        response = row.get("response") or {}
        sources = row.get("sources", [])
        history = row.get("history_summary", {})
        stage = {"continue": "continuation", "revise": "synthesis_review"}.get(row.get("stage"), row.get("stage"))
        rounds.append({"round_id": f"round-{offset}",
            "stage": _enum(stage, {"answer", "discover", "investigate", "synthesis_review", "continuation"}),
            "request_bytes": _number(row.get("request_bytes")),
            "source_file_count": len({p.get("path") for p in sources if isinstance(p.get("path"), str)}),
            "page_count": len(sources),
            "source_characters": sum(_number(p.get("characters")) or 0 for p in sources),
            "source_line_count": sum(max(0, (p.get("end_line") or 0) - (p.get("start_line") or 1) + 1) for p in sources
                                     if type(p.get("end_line")) is int and type(p.get("start_line")) is int),
            "trim_event_count": len(row.get("trim_events", [])),
            "history_message_count": _number(history.get("count")),
            "history_characters": _number(history.get("characters")),
            "finish_reason": _finish(response.get("finish_reason")),
            **{key: _number(response.get(key)) for key in ("raw_content_characters", "parsed_answer_characters", "choice_index")}})
    limitations = set()
    indexed = _number(investigation.get("repository_file_count"))
    if indexed == 0:
        limitations.add("index_not_ready")
    status = identity.get("status")
    if status == "ambiguous":
        limitations.add("source_identity_ambiguous")
    elif status == "not_found":
        limitations.add("explicit_source_not_indexed")
    sent_sources = [page for row in quality.get("rounds", []) for page in row.get("sources", [])]
    provided_paths = {page.get("path") for page in sent_sources if isinstance(page.get("path"), str)}
    provided_pages = {(page.get("path"), page.get("start_line"), page.get("end_line")) for page in sent_sources}
    retrieval = investigation.get("retrieval_status")
    if retrieval == "unresolved" and sent_sources and (
            investigation.get("current_question_match_observed") or status == "resolved"):
        retrieval = "source_candidates"
    if retrieval == "unresolved":
        limitations.add("search_no_match")
        if status in {None, "none"}:
            limitations.add("business_terms_unresolved")
    tasks = final.get("open_tasks", [])
    if tasks or any(item.get("reason") == "source_not_supplied" for item in question.get("required_items", [])):
        limitations.add("located_source_not_read")
    source_trimmed = any(event.get("role") == "source" or event.get("reason") == "source_characters"
                         for row in quality.get("rounds", []) for event in row.get("trim_events", []))
    if working.get("omitted_complete_paths") or source_trimmed:
        limitations.add("source_budget_omitted")
    reasons = {item.get("reason") for item in [*result.get("boundaries", []), *question.get("open_gaps", [])]
               if isinstance(item, dict)}
    if reasons.intersection({"external_implementation_unavailable", "external_implementation_missing"}):
        limitations.add("external_implementation_missing")
    if "runtime_target_unresolved" in reasons:
        limitations.add("runtime_target_unresolved")
    if reasons.intersection({"input_source_not_located", "input_candidate_frontier"}):
        limitations.add("unresolved_inputs")
    failed_read = any(item.get("outcome") == "unavailable" and "read" in item
                      for item in result.get("investigation_state", {}).get("completed_actions", []))
    if failed_read or any(item.get("code") in {"SOURCE_READ_FAILED", "SOURCE_UNAVAILABLE", "SOURCE_CHANGED"}
           for item in result.get("diagnostics", [])) or any(task.get("state") in {"failed", "stalled"} for task in tasks):
        limitations.add("source_read_failed")
    if result.get("answer_truncated") or result.get("finish_reason") == "length":
        limitations.add("output_limit_reached")
    if result.get("stop_reason") == "request_budget":
        limitations.add("request_budget_exhausted")
    if result.get("stop_reason") == "answer_incomplete":
        limitations.add("answer_incomplete")
    if any(item.get("code") in {"MODEL_RESPONSE_INVALID", "MODEL_RESPONSE_PARSE_FAILED", "MODEL_ACTION_INVALID",
                               "MODEL_TEXT_EMPTY", "MODEL_MESSAGE_MISSING", "MODEL_ERROR_RESPONSE",
                               "MODEL_STATUS_ONLY", "MODEL_ACTION_ONLY", "MODEL_ACTION_RESPONSE"}
           for item in result.get("diagnostics", [])):
        limitations.add("parser_failed")
    policy = configuration.get("policy", {})
    api_failures = []
    for item in result.get("diagnostics", [])[:64]:
        if not isinstance(item, dict):
            continue
        diagnostic = sanitize_diagnostic(item.get("diagnostic"))
        if diagnostic is not None:
            api_failures.append({"stage": _enum(item.get("stage"), {
                "configuration", "provider_request", "context_assembly", "response_parse",
                "response_validation", "continuation", "synthesis_review", "direct",
                "map", "reduce", "model_request", "repository_search", "page",
                "program", "synthesis"}), **diagnostic})
    return {"schema_version": "business-answer-diagnostics/v1",
        "api_failures": api_failures,
        "runtime": {"commit": _commit(),
            "profile": _enum(getattr(config, "profile_name", None), {"adapter", "workbench", "analysis", "custom"}),
            "model_fingerprint": hashlib.sha256(config.chat_model.encode()).hexdigest(),
            "output_limit_source": _enum(getattr(config, "output_limit_source", None), {"profile", "environment", "dotenv", "explicit"})},
        "configured": {"max_output_tokens": _number(config.max_output_tokens),
            "max_model_requests": _number(policy.get("max_model_requests")),
            "requested_detail": _enum(result.get("answer_detail"), {"brief", "detailed"})},
        "requests": rounds,
        "source_coverage": {"indexed_files": indexed,
            "selected_files": max(_number(investigation.get("selected_file_count")) or 0,
                                  len(business_map.get("selected_paths", []))),
            "provided_files": len(provided_paths) if sent_sources else _number(reading.get("sent_files")),
            "pages": len(provided_pages) if sent_sources else _number(reading.get("sent_pages")),
            "unread_tasks": len(tasks), "omitted_complete_files": len(working.get("omitted_complete_paths", [])),
            "identity_status": "not_requested" if status == "none" else _enum(status, {"resolved", "ambiguous", "not_found"}),
            "retrieval_status": _enum(retrieval, {"source_candidates", "framework_candidates", "unresolved", "not_attempted"}),
            "limitation_codes": sorted(limitations)},
        "output": {"finish_reason": _finish(result.get("finish_reason")),
            "answer_truncated": result.get("answer_truncated") is True,
            "continuation_attempted": result.get("continuation_attempted") is True,
            "final_answer_characters": len(result.get("answer", ""))},
        "stop_reason": result.get("stop_reason") if result.get("stop_reason") in STOP_REASONS else "other"}
