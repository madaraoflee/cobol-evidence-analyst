#!/usr/bin/env python3
"""Run one bounded COBOL investigation through an approved company API."""

from __future__ import annotations

import argparse
import json
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
) -> dict[str, object]:
    """Probe the endpoint, select the safe mode, then run one investigation."""

    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")

    capability_report = probe_capabilities(
        config,
        transport=transport,
        allow_network=allow_network,
        probe_embeddings=False,
    )
    readiness = capability_report.get("agent_readiness", {})
    if not isinstance(readiness, Mapping) or not readiness.get("ready"):
        return _not_ready(
            "COMPANY_API_NOT_READY",
            capability_report=capability_report,
        )

    try:
        tools = InvestigationTools(database_path)
    except (OSError, ValueError):
        return _not_ready(
            "STRUCTURAL_INDEX_INVALID",
            capability_report=capability_report,
        )

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
    ).run(question.strip())
    return {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "runner_status": "COMPLETED",
        "reason_code": "AGENT_RUN_COMPLETED",
        "selected_mode": mode,
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
    args = build_parser().parse_args(argv)
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
    )
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if output["runner_status"] == "COMPLETED" else 2


__all__ = [
    "RUNNER_SCHEMA_VERSION",
    "build_parser",
    "main",
    "run_investigation",
]


if __name__ == "__main__":
    sys.exit(main())
