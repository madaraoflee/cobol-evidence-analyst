"""Reuse the local index for questions; refresh is an explicit intake operation."""

from __future__ import annotations

import copy
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from company_api import APIConfigurationError, CompanyAPIConfig
from api_error_details import build_diagnostic, format_diagnostic, sanitize_diagnostic
from report_view import write_report_view


def _index_update_requirements(database, source, requested_options):
    """Check compatibility before a question can fall back to full intake.

    No index is a first import. A different source root is a new selection.
    An existing index for this root must be explicitly updated when its
    semantics change; it cannot be safely reused or silently rebuilt here.
    """
    from business_index import PARSER_VERSION
    from repository_discovery import _connect, _fast_snapshot

    if not database.is_file():
        return []
    try:
        with closing(_connect(database)) as connection:
            metadata = dict(connection.execute("SELECT key,value FROM metadata WHERE key IN "
                "('parser_version','source_options','source_root_hash','index_kind')"))
        root_hash = hashlib.sha256(str(source).encode()).hexdigest()
        if metadata.get("source_root_hash") and metadata["source_root_hash"] != root_hash:
            return []
        reasons = []
        if metadata.get("parser_version") != PARSER_VERSION:
            reasons.append("parser_version_changed")
        if metadata.get("index_kind") != "business_sparse":
            reasons.append("index_kind_changed")
        database_options = json.loads(metadata.get("source_options", "{}"))
        if not isinstance(database_options, dict):
            return [*reasons, "index_metadata_invalid"]
        if any(database_options.get(key) != value for key, value in requested_options.items()
               if key != "compilation_context"):
            reasons.append("source_options_changed")
        if database_options.get("compilation_context") != requested_options["compilation_context"]:
            reasons.append("compilation_context_changed")
        if not reasons:
            with closing(_connect(database)) as connection:
                try:
                    _fast_snapshot(connection, source)
                except (ValueError, KeyError, sqlite3.Error):
                    reasons.append("search_index_version_changed")
        return reasons
    except (OSError, ValueError, sqlite3.Error):
        return ["index_metadata_invalid"]


def _write_index_update_required(source, output, *, question, entry, reasons):
    from analyze_source import _write
    from business_index import PARSER_VERSION

    try:
        previous = json.loads((output / "diagnosis.json").read_text(encoding="utf-8"))
        report = copy.deepcopy(previous) if isinstance(previous, dict) else {}
    except (OSError, ValueError):
        report = {}
    explanations = {
        "parser_version_changed": "已有索引的解析器版本与当前版本不同。",
        "index_kind_changed": "已有索引类型与当前业务分析方式不同。",
        "source_options_changed": "源码编码、格式或文件选择设置已改变。",
        "compilation_context_changed": "编译条件、COPY 配置或框架版本等编译上下文已改变。",
        "index_metadata_invalid": "已有索引的版本或配置记录无法读取。",
        "search_index_version_changed": "已有检索索引的版本或快照需要更新。",
    }
    diagnostic = build_diagnostic("SOURCE_INDEX_UPDATE_REQUIRED")
    message = "".join(explanations[reason] for reason in reasons) + "请先使用“更新源码”完成索引更新，再继续提问。"
    report.update(generated_at_utc=datetime.now(timezone.utc).isoformat(), source_root=str(source),
        question=question, entry_requested=entry, runner_status="BLOCKED", question_status="NOT_READY",
        reason_code="SOURCE_INDEX_UPDATE_REQUIRED", index_reused=False, source_manifest_verified=False,
        diagnostic=diagnostic, messages=[message], metrics={"model_requests": 0}, investigation={},
        index_update={"required": True, "reasons": reasons, "expected_parser_version": PARSER_VERSION,
                      "source_directory_scanned": False, "model_requests": 0})
    agent = {"runner_status": "NOT_READY", "reason_code": "SOURCE_INDEX_UPDATE_REQUIRED",
             "agent_result": None, "diagnostic": diagnostic}
    _write(output / "diagnosis.json", report)
    write_report_view(output / "diagnosis.json", report)
    _write(output / "diagnosis.md", "# 索引需要更新\n\n" + message + "\n", markdown=True)
    _write(output / "agent-result.json", agent)
    write_report_view(output / "agent-result.json", agent)
    _write(output / "agent-result.md", message + "\n", markdown=True)
    return report


def try_indexed_question(source, output, *, question, entry, extensions, include_extensionless,
                         encoding, source_format, config, api_options, transport,
                         framework_reference_path, capture_api_responses, history,
                         progress, check_cancel, policy=None, answer_detail="detailed", compilation_context=None,
                         conversation_id=None, locale=None, start_new_investigation=False):
    from repository_discovery import _connect, repository_search_overview
    from business_chat import run_business_chat
    from business_index import PARSER_VERSION
    from analyze_source import _write
    from compilation_context import resolve_compilation_context
    from repository_identity import resolve_entry_identity
    requested_options = {"extensions": sorted(extensions),
        "include_extensionless": include_extensionless, "encoding": encoding, "source_format": source_format}
    requested_options["compilation_context"] = resolve_compilation_context(
        compilation_context, source_options=requested_options).to_dict()
    if check_cancel:
        check_cancel()
    requirements = _index_update_requirements(output / "structural-index.sqlite", source, requested_options)
    if requirements:
        return _write_index_update_required(source, output, question=question, entry=entry, reasons=requirements)
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
            if entry:
                selected_identity = resolve_entry_identity(connection, entry)
                if selected_identity["status"] != "resolved":
                    # An earlier selected-entry index may cover a different
                    # member while the catalog lists the requested member.
                    # Return to normal intake instead of answering from it.
                    return None
                if (previous.get("scope") or {}).get("mode") not in {"repository_index", "repository_question"}:
                    from source_catalog import _entry_matches
                    programs_path = output / "programs.json"
                    if programs_path.is_symlink():
                        return None
                    catalog_programs = json.loads(programs_path.read_text(encoding="utf-8")).get("programs", [])
                    matches = _entry_matches(catalog_programs, entry)
                    if len(matches) != 1 or matches[0]["relative_path"] not in selected_identity["direct_paths"]:
                        return None
        if (metadata.get("parser_version") != PARSER_VERSION
                or metadata.get("index_kind") != "business_sparse"
                or metadata.get("source_root_hash") != hashlib.sha256(str(source).encode()).hexdigest()):
            return None
        database_options = json.loads(metadata["source_options"])
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
    report.pop("diagnostic", None)
    report.update(generated_at_utc=datetime.now(timezone.utc).isoformat(), question=question,
                  question_status="RUNNING", entry_requested=entry, messages=[],
                  runner_status="INDEX_READY", reason_code="SOURCE_INDEX_REUSED",
                  source_verification_scope="retrieved_sources", index_reused=True)
    report["source_options"].update(reading_strategy="retrieval", answer_detail=answer_detail)
    report["repository_search"] = overview
    try:
        selected_config = config or CompanyAPIConfig.from_env(**(api_options or {}))
        agent = run_business_chat(question, output / "structural-index.sqlite", source, selected_config,
            history=history, conversation_id=conversation_id,
            entry_program=entry, framework_reference_path=framework_reference_path,
            capture_api_responses=capture_api_responses, allow_network=True, transport=transport,
            progress=progress, check_cancel=check_cancel, policy=policy, answer_detail=answer_detail, locale=locale,
            start_new_investigation=start_new_investigation)
    except APIConfigurationError as exc:
        agent = {"runner_status": "NOT_READY", "reason_code": exc.code, "agent_result": None}
        diagnostic = sanitize_diagnostic(getattr(exc, "diagnostic", None))
        if diagnostic is not None:
            agent["diagnostic"] = diagnostic
    diagnostic = sanitize_diagnostic(agent.get("diagnostic"))
    if diagnostic is not None:
        report["diagnostic"] = diagnostic
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
    write_report_view(output / "diagnosis.json", report)
    _write(output / "diagnosis.md", "# 本次业务对话\n\n使用已有源码索引，按问题检索。\n", markdown=True)
    _write(output / "agent-result.json", agent)
    write_report_view(output / "agent-result.json", agent)
    _write(output / "agent-result.md", result.get("answer") or format_diagnostic(diagnostic) or "本次没有取得模型回答。", markdown=True)
    _write(output / "framework-context.json", report.get("framework_context", {}))
    return report
