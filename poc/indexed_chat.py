"""Reuse the local index for questions; refresh is an explicit intake operation."""

from __future__ import annotations

import copy
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from company_api import APIConfigurationError, CompanyAPIConfig
from report_view import write_report_view


def try_indexed_question(source, output, *, question, entry, extensions, include_extensionless,
                         encoding, source_format, config, api_options, transport,
                         framework_reference_path, capture_api_responses, history,
                         progress, check_cancel, policy=None):
    from repository_discovery import _connect, repository_search_overview
    from business_chat import run_business_chat
    from business_index import PARSER_VERSION
    from analyze_source import _write
    try:
        path = output / "diagnosis.json"
        if path.is_symlink():
            return None
        previous = json.loads(path.read_text(encoding="utf-8"))
        if not previous.get("source_manifest_verified"):
            return None
        if not entry and (previous.get("scope") or {}).get("mode") not in {"repository_index", "repository_question"}:
            return None
        overview = repository_search_overview(output / "structural-index.sqlite", source)
        with closing(_connect(output / "structural-index.sqlite")) as connection:
            metadata = dict(connection.execute("SELECT key,value FROM metadata WHERE key IN "
                "('parser_version','source_options','source_root_hash','index_kind')"))
        if (metadata.get("parser_version") != PARSER_VERSION
                or metadata.get("index_kind") != "business_sparse"
                or metadata.get("source_root_hash") != hashlib.sha256(str(source).encode()).hexdigest()):
            return None
        database_options = json.loads(metadata["source_options"])
        requested_options = {"extensions": sorted(extensions),
            "include_extensionless": include_extensionless,
            "encoding": encoding, "source_format": source_format}
        if any(database_options.get(key) != value for key, value in requested_options.items()):
            return None
        previous["source_root"] = str(source)
        previous.setdefault("source_options", {}).update(requested_options, analysis_mode="business")
        if (previous.get("build_report") or {}).get("snapshot_id") != overview["snapshot_id"]:
            # A selected-file refresh commits the database before the display
            # report is rewritten. The database is the authoritative snapshot.
            previous.setdefault("build_report", {})["snapshot_id"] = overview["snapshot_id"]
            previous["snapshot_id"] = overview["snapshot_id"]
            previous["report_repaired_from_index"] = True
    except (OSError, ValueError, KeyError, sqlite3.Error):
        return None
    if check_cancel:
        check_cancel()
    if progress:
        progress({"phase": "using_index", "completed": 0, "total": None, "unit": "steps"})
    report = copy.deepcopy(previous)
    report.update(generated_at_utc=datetime.now(timezone.utc).isoformat(), question=question,
                  question_status="RUNNING", entry_requested=entry, messages=[],
                  runner_status="INDEX_READY", reason_code="SOURCE_INDEX_REUSED",
                  source_verification_scope="retrieved_sources", index_reused=True)
    report["source_options"].update(reading_strategy="retrieval")
    report["repository_search"] = overview
    try:
        selected_config = config or CompanyAPIConfig.from_env(**(api_options or {}))
        agent = run_business_chat(question, output / "structural-index.sqlite", source, selected_config,
            history=history, entry_program=entry, framework_reference_path=framework_reference_path,
            capture_api_responses=capture_api_responses, allow_network=True, transport=transport,
            progress=progress, check_cancel=check_cancel, policy=policy)
    except APIConfigurationError as exc:
        agent = {"runner_status": "NOT_READY", "reason_code": exc.code, "agent_result": None}
    result = agent.get("agent_result") or {}
    if result.get("snapshot_id") and result["snapshot_id"] != overview["snapshot_id"]:
        from analyze_source import _catalog
        programs, _, _ = _catalog(output / "structural-index.sqlite")
        report.setdefault("build_report", {})["snapshot_id"] = result["snapshot_id"]
        report["snapshot_id"] = result["snapshot_id"]
        overview = repository_search_overview(output / "structural-index.sqlite", source)
        report["repository_search"] = overview
        program_path = output / "programs.json"
        previous_programs = json.loads(program_path.read_text(encoding="utf-8"))
        _write(program_path, {**previous_programs, "snapshot_id": result["snapshot_id"],
                              "programs": programs})
    report["question_status"] = result.get("status") if agent["runner_status"] == "COMPLETED" else agent["runner_status"]
    if result.get("framework_context"):
        report["framework_context"] = result["framework_context"]
    report["investigation"] = result.get("investigation", {})
    report["metrics"] = result.get("metrics", {})
    _write(output / "diagnosis.json", report)
    _write(output / "diagnosis.md", "# 本次业务对话\n\n使用已有源码索引，按问题检索。\n", markdown=True)
    _write(output / "agent-result.json", agent)
    write_report_view(output / "agent-result.json", agent)
    _write(output / "agent-result.md", result.get("answer", "本次没有取得模型回答。"), markdown=True)
    _write(output / "framework-context.json", report.get("framework_context", {}))
    return report
