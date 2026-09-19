#!/usr/bin/env python3
"""Run one bounded COBOL investigation through an approved company API."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Callable, Mapping, Sequence

from agent_loop import BoundedAgentLoop
from api_diagnostics import APIResponseDiagnostics
from company_api import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_TIMEOUT_SECONDS,
    APIConfigurationError,
    CompanyAPIConfig,
    OpenAICompatibleChatClient,
    Transport,
    probe_capabilities,
)
from investigation_tools import InvestigationTools
from framework_knowledge import build_framework_context


RUNNER_SCHEMA_VERSION = "bounded-cobol-agent-run/v1"


def _not_ready(
    reason_code: str,
    *,
    capability_report: Mapping[str, object] | None = None,
    diagnostics: APIResponseDiagnostics | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "runner_status": "NOT_READY",
        "reason_code": reason_code,
        "agent_result": None,
        "privacy": {
            "api_key_recorded": False,
            "base_url_recorded": False,
            "model_identifiers_recorded": False,
        },
    }
    if capability_report is not None:
        result["capability_report"] = dict(capability_report)
    if diagnostics is not None:
        result["api_diagnostics"] = diagnostics.to_dict()
        result["privacy"]["scope"] = "CONFIGURATION_AND_CAPABILITY_REPORT"
    return result


def run_investigation(
    question: str,
    database_path: Path | str,
    config: CompanyAPIConfig,
    *,
    transport: Transport | None = None,
    allow_network: bool = False,
    entry_program: str | None = None,
    analysis_scope: Mapping[str, object] | None = None,
    framework_context: Mapping[str, object] | None = None,
    capture_api_responses: bool = False,
    analysis_mode: str = "strict",
    source_root: Path | str | None = None,
    max_source_pages: int = 12,
    reading_strategy: str = "focused",
    progress: Callable | None = None,
    check_cancel: Callable | None = None,
) -> dict[str, object]:
    """Validate the local entry, probe the endpoint, then investigate its snapshot."""

    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    if analysis_mode not in {"business", "strict"}:
        raise ValueError("analysis_mode must be business or strict")
    if reading_strategy not in {"focused", "full_chain"}:
        raise ValueError("reading_strategy must be focused or full_chain")
    if analysis_mode == "business":
        if source_root is None:
            return _not_ready("SOURCE_ROOT_REQUIRED")
        from business_analysis import run_business_analysis
        return run_business_analysis(
            question, database_path, source_root, config,
            entry_program=entry_program, analysis_scope=analysis_scope,
            framework_context=framework_context, transport=transport,
            allow_network=allow_network, capture_api_responses=capture_api_responses,
            max_pages=max_source_pages, reading_strategy=reading_strategy,
            progress=progress, check_cancel=check_cancel,
        )

    diagnostics = APIResponseDiagnostics(protected_values=(
        config.resolve_api_key(), config.base_url, config.chat_model, config.embedding_model,
    )) if capture_api_responses else None

    try:
        tools = InvestigationTools(database_path)
        source_preflight = tools.resolve_entry(entry_program)
    except (OSError, ValueError, sqlite3.Error):
        return _not_ready("STRUCTURAL_INDEX_INVALID", diagnostics=diagnostics)
    if not source_preflight["ready"]:
        output = _not_ready(str(source_preflight["reason_code"]), diagnostics=diagnostics)
        output["source_preflight"] = source_preflight
        return output

    selected_framework = dict(framework_context) if framework_context is not None else build_framework_context(
        database_path, entry_program=entry_program, question=question.strip(),
    )
    framework_scope = selected_framework.get("coverage")
    framework_scope = framework_scope if isinstance(framework_scope, Mapping) else {}
    selected_entries = source_preflight.get("entries", [])
    wrong_entry = bool(selected_entries and (
        framework_scope.get("entry_program") != selected_entries[0]["name"]
        or framework_scope.get("entry_relative_path") != selected_entries[0]["relative_path"]
    ))
    if selected_framework.get("status") == "MATCHED" and (
        framework_scope.get("snapshot_id") != source_preflight["snapshot_id"] or wrong_entry
    ):
        selected_framework.update(
            status="LOADED" if selected_framework.get("document") else "UNAVAILABLE",
            reason_code="FRAMEWORK_SOURCE_CONTEXT_STALE", source_matches=[], references=[],
            boundaries=["The framework source matches belong to an unavailable source snapshot; discover current source evidence again."],
        )

    capability_report = probe_capabilities(
        config,
        transport=transport,
        allow_network=allow_network,
        probe_embeddings=False,
        diagnostics=diagnostics,
    )
    readiness = capability_report.get("agent_readiness", {})
    if not isinstance(readiness, Mapping) or not readiness.get("ready"):
        output = _not_ready(
            "COMPANY_API_NOT_READY",
            capability_report=capability_report,
            diagnostics=diagnostics,
        )
        output["source_preflight"] = source_preflight
        output["framework_context"] = selected_framework
        return output

    mode = readiness.get("mode")
    native_tool_calling = mode == "NATIVE_TOOL_CALLING"
    provider_strict_json = bool(readiness.get("provider_strict_json"))
    client = OpenAICompatibleChatClient(
        config,
        transport=transport,
        allow_network=allow_network,
        diagnostics=diagnostics,
    )
    result = BoundedAgentLoop(
        client,
        tools,
        native_tool_calling=native_tool_calling,
        strict_json=(not native_tool_calling and provider_strict_json),
    ).run(question.strip(), entry_program=entry_program, analysis_scope=analysis_scope,
          framework_context=selected_framework)
    stop_reason = result.get("stop_reason")
    normally_completed = stop_reason in {"completed", "model_abstained"}
    output: dict[str, object] = {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "runner_status": "COMPLETED" if normally_completed else "SAFE_STOP",
        "reason_code": (
            "AGENT_RUN_COMPLETED" if normally_completed else "AGENT_SAFETY_STOPPED"
        ),
        "selected_mode": mode,
        "source_preflight": source_preflight,
        "capability_report": capability_report,
        "agent_result": result,
        "stop_detail": {
            "reason": stop_reason,
            "code": str(stop_reason).upper(),
        },
        "framework_context": selected_framework,
        "privacy": {
            "api_key_recorded": False,
            "base_url_recorded": False,
            "model_identifiers_recorded": False,
        },
    }
    if diagnostics is not None:
        output["api_diagnostics"] = diagnostics.to_dict()
        output["privacy"]["scope"] = "CONFIGURATION_AND_CAPABILITY_REPORT"
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one bounded investigation against a local structural index. "
            "No network request is made unless --allow-network is present."
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--analysis-mode", choices=("business", "strict"), default="strict")
    parser.add_argument("--source", type=Path, help="Source directory required by business analysis.")
    parser.add_argument("--max-source-pages", type=int, default=12)
    parser.add_argument("--reading-strategy", choices=("focused", "full_chain"), default="focused",
                        help="Read selected pages or every page in the reachable source scope in batches.")
    parser.add_argument(
        "--entry", "--entry-program", dest="entry_program",
        help="PROGRAM-ID, source relative path, or unique source filename to investigate.",
    )
    parser.add_argument("--base-url")
    parser.add_argument("--chat-model")
    parser.add_argument("--api-style")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS
    )
    parser.add_argument("--allow-insecure-localhost", action="store_true")
    parser.add_argument("--allow-network", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # The Windows wrapper preserves its existing database/question positions and
    # forwards all remaining options unchanged; Python owns argument parsing.
    if len(arguments) >= 2 and not arguments[0].startswith("-"):
        arguments = ["--database", arguments[0], "--question", arguments[1], *arguments[2:]]
    args = build_parser().parse_args(arguments)
    try:
        config = CompanyAPIConfig.from_env(
            base_url=args.base_url,
            chat_model=args.chat_model,
            api_style=args.api_style,
            timeout_seconds=args.timeout_seconds,
            max_output_tokens=args.max_output_tokens,
            allow_insecure_localhost=args.allow_insecure_localhost,
        )
    except APIConfigurationError as exc:
        output = _not_ready(exc.code)
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 2

    output = run_investigation(
        args.question,
        args.database,
        config,
        allow_network=args.allow_network,
        entry_program=args.entry_program,
        analysis_mode=args.analysis_mode, source_root=args.source,
        max_source_pages=args.max_source_pages, reading_strategy=args.reading_strategy,
    )
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    if output["runner_status"] == "COMPLETED":
        return 0
    return 3 if output["runner_status"] == "SAFE_STOP" else 2


__all__ = [
    "RUNNER_SCHEMA_VERSION",
    "build_parser",
    "main",
    "run_investigation",
]


if __name__ == "__main__":
    sys.exit(main())
