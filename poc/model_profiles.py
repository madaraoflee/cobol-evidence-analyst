"""Shared workbench and evaluation adapter settings."""

from __future__ import annotations

from dataclasses import replace

from company_api import CompanyAPIConfig


def model_config(profile="adapter", *, timeout_seconds=None, max_output_tokens=None):
    if profile not in {"adapter", "workbench"}:
        raise ValueError("MODEL_PROFILE_INVALID")
    config = (CompanyAPIConfig.from_env(timeout_seconds=60.0, max_output_tokens=2048)
              if profile == "workbench" else CompanyAPIConfig.from_env())
    updates = {}
    if timeout_seconds is not None:
        updates["timeout_seconds"] = timeout_seconds
    if max_output_tokens is not None:
        updates["max_output_tokens"] = max_output_tokens
    return replace(config, **updates) if updates else config
