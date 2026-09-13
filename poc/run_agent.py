#!/usr/bin/env python3
"""Run one bounded COBOL investigation through an approved company API."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Mapping, Sequence

from agent_loop import BoundedAgentLoop
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


RUNNER_SCHEMA_VERSION = "bounded-cobol-agent-run/v1"


def _not_ready(
    reason_code: str,
    *,
    capability_report: Mapping[str, object] | None = None,
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
    return result


def run_investigation(
    question: str,
    database_path: Path | str,
    config: CompanyAPIConfig,
    *,
    transport: Transport | None = None,
    allow_network: bool = False,
    entry_program: str | None = None,
) -> dict[str, object]:
    """Validate the local entry, probe the endpoint, then investigate its snapshot."""

    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")

    try:
        tools = InvestigationTools(database_path)
        source_preflight = tools.resolve_entry(entry_program)
    except (OSError, ValueError, sqlite3.Error):
        return _not_ready("STRUCTURAL_INDEX_INVALID")
    if not source_preflight["ready"]:
        output = _not_ready(str(source_preflight["reason_code"]))
        output["source_preflight"] = source_preflight
        return output

    capability_report = probe_capabilities(
        config,
        transport=transport,
        allow_network=allow_network,
        probe_embeddings=False,
    )
    readiness = capability_report.get("agent_readiness", {})
    if not isinstance(readiness, Mapping) or not readiness.get("ready"):
        output = _not_ready(
            "COMPANY_API_NOT_READY",
            capability_report=capability_report,
        )
        output["source_preflight"] = source_preflight
        return output

    mode = readiness.get("mode")
    native_tool_calling = mode == "NATIVE_TOOL_CALLING"
    provider_strict_json = bool(readiness.get("provider_strict_json"))
    client = OpenAICompatibleChatClient(
        config,
        transport=transport,
        allow_network=allow_network,
    )
    result = BoundedAgentLoop(
        client,
        tools,
        native_tool_calling=native_tool_calling,
        strict_json=(not native_tool_calling and provider_strict_json),
    ).run(question.strip(), entry_program=entry_program)
    stop_reason = result.get("stop_reason")
    normally_completed = stop_reason in {"completed", "model_abstained"}
    return {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "runner_status": "COMPLETED" if normally_completed else "SAFE_STOP",
        "reason_code": (
            "AGENT_RUN_COMPLETED" if normally_completed else "AGENT_SAFETY_STOPPED"
        ),
        "selected_mode": mode,
        "source_preflight": source_preflight,
        "capability_report": capability_report,
        "agent_result": result,
        "privacy": {
            "api_key_recorded": False,
            "base_url_recorded": False,
            "model_identifiers_recorded": False,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one bounded investigation against a local structural index. "
            "No network request is made unless --allow-network is present."
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--question", required=True)
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
