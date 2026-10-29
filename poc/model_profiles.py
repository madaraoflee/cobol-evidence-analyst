"""Shared workbench and evaluation adapter settings."""

from __future__ import annotations

from company_api import CompanyAPIConfig, DEFAULT_MAX_OUTPUT_TOKENS, DEFAULT_TIMEOUT_SECONDS

WORKBENCH_MAX_OUTPUT_TOKENS = 8192
WORKBENCH_TIMEOUT_SECONDS = 60.0


def model_config(profile="adapter", *, timeout_seconds=None, max_output_tokens=None, reasoning_effort=None):
    if profile not in {"adapter", "workbench"}:
        raise ValueError("MODEL_PROFILE_INVALID")
    return CompanyAPIConfig.from_env(
        timeout_seconds=timeout_seconds if timeout_seconds is not None else (
            WORKBENCH_TIMEOUT_SECONDS if profile == "workbench" else DEFAULT_TIMEOUT_SECONDS),
        max_output_tokens=max_output_tokens,
        reasoning_effort=reasoning_effort,
        default_max_output_tokens=WORKBENCH_MAX_OUTPUT_TOKENS if profile == "workbench" else DEFAULT_MAX_OUTPUT_TOKENS,
        profile_name=profile,
    )
