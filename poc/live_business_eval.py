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

from business_analysis import _TextResponseError, _extract_text
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import (APIClientError, APIConfigurationError, CompanyAPIConfig,
                         OpenAICompatibleChatClient, PROJECT_ENV_FILE, _read_local_env)
from repository_discovery import ensure_repository_search


def _cases(path: Path) -> list[dict]:
    document = json.loads(path.read_text(encoding="utf-8"))
    items = document.get("cases") if isinstance(document, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("EVALUATION_CASES_INVALID")
    seen = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            raise ValueError("EVALUATION_CASES_INVALID")
        if item["id"] in seen or not isinstance(item.get("question"), str) or not item["question"].strip():
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


def evaluate(source: Path, evaluation_dir: Path, cases_file: Path, *, allow_network: bool,
             source_format: str = "auto", encoding: str = "auto", framework: str | None = None) -> dict:
    source = source.expanduser().resolve(strict=True)
    evaluation_dir = evaluation_dir.expanduser().resolve()
    if not source.is_dir() or source.is_relative_to(evaluation_dir) or evaluation_dir.is_relative_to(source):
        raise ValueError("EVALUATION_PATHS_OVERLAP")
    items = _cases(cases_file.expanduser().resolve(strict=True))
    config = CompanyAPIConfig.from_env()
    report = {"schema_version": "live-business-eval/v1", "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "configuration": config.safe_summary(), "case_count": len(items),
              "model_preflight": {}, "index_seconds": None, "search_index_seconds": None,
              "cases": [], "latency_seconds": {"p50": None, "p95": None},
              "quality_status": "not_reviewed"}
    if not allow_network:
        report["model_preflight"] = {"status": "network_not_enabled"}
        return report
    started = time.monotonic()
    try:
        ping = OpenAICompatibleChatClient(replace(config, max_output_tokens=32), allow_network=True)
        response = ping.complete(messages=[{"role": "user", "content": "Reply with OK."}])
        text = _extract_text(response)
        if not text.text.strip():
            raise _TextResponseError("MODEL_TEXT_EMPTY")
    except (APIClientError, APIConfigurationError, _TextResponseError) as exc:
        report["model_preflight"] = {"status": "failed", "code": exc.code,
                                     "http_status": getattr(exc, "http_status", None),
                                     "elapsed_seconds": round(time.monotonic() - started, 3)}
        return report
    report["model_preflight"] = {"status": "responded", "elapsed_seconds": round(time.monotonic() - started, 3)}

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
    for item in items:
        conversation = item.get("conversation") if isinstance(item.get("conversation"), str) else item["id"]
        history = histories.setdefault(conversation, [])
        started = time.monotonic()
        output = run_business_chat(item["question"], database, source, config, history=history,
                                   framework_reference_path=_framework_path(framework), allow_network=True)
        elapsed = round(time.monotonic() - started, 3)
        durations.append(elapsed)
        agent = output["agent_result"]
        citations = agent.get("narrative", {}).get("citations", [])
        cited_paths = sorted({ref["relative_path"] for ref in citations if ref.get("relative_path")})
        retrieved_paths = sorted(set(agent.get("investigation", {}).get("business_map", {}).get("selected_paths", [])))
        expected = list(dict.fromkeys(item.get("expected_source_paths", [])))
        result = {"id": item["id"], "question": item["question"], "conversation": conversation,
                  "elapsed_seconds": elapsed, "model_requests": agent.get("metrics", {}).get("model_requests"),
                  "runner_status": output.get("runner_status"), "answer": agent.get("answer", ""),
                  "cited_source_paths": cited_paths, "retrieved_source_paths": retrieved_paths,
                  "expected_source_paths": expected,
                  "expected_paths_retrieved": sorted(set(expected) & set(retrieved_paths)),
                  "expected_paths_cited": sorted(set(expected) & set(cited_paths)),
                  "review_checks": item.get("review_checks", []), "human_review": None}
        report["cases"].append(result)
        history.extend(({"role": "user", "content": item["question"]},
                        {"role": "assistant", "content": agent.get("answer", ""),
                         "evidence_refs": agent.get("evidence_refs", []),
                         "cited_evidence_ids": [ref.get("evidence_id") for ref in citations if ref.get("evidence_id")]}))
    report["latency_seconds"] = {"p50": round(statistics.median(durations), 3),
                                 "p95": _percentile(durations, .95)}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure real model answers and full question latency locally.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--evaluation-dir", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--source-format", choices=("auto", "fixed", "free"), default="auto")
    parser.add_argument("--encoding", default="auto")
    parser.add_argument("--framework")
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args(argv)
    report_path = args.evaluation_dir.expanduser().resolve() / "live-business-eval.json"
    try:
        report = evaluate(args.source, args.evaluation_dir, args.cases,
                          allow_network=args.allow_network, source_format=args.source_format,
                          encoding=args.encoding, framework=args.framework)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Evaluation setup failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    _safe_write(report_path, report)
    print(json.dumps({"report": str(report_path), "model_preflight": report["model_preflight"],
                      "index_seconds": report["index_seconds"], "latency_seconds": report["latency_seconds"],
                      "case_count": len(report["cases"])}, ensure_ascii=False))
    return 0 if report["model_preflight"]["status"] == "responded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
