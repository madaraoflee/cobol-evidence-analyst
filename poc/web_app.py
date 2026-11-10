#!/usr/bin/env python3
"""Loopback-only, dependency-free web adapter for the source analysis workflow."""

from __future__ import annotations

import argparse
import codecs
import copy
from contextlib import closing
from dataclasses import replace
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Sequence
from urllib.parse import parse_qs, urlsplit
import webbrowser

from analyze_source import ARTIFACT_NAMES, OUTPUT_DIRECTORY_NAMES, OUTPUT_FILE_NAMES, AnalysisCancelled, _paths, analyze_source
from company_api import APIClientError, APIConfigurationError, CompanyAPIConfig, OpenAICompatibleChatClient, Transport
from investigation_tools import InvestigationTools
from repo_inventory import parse_extensions
from framework_knowledge import framework_status, _resolve_reference
from report_view import DIRECT_REPORT_BYTES, VIEW_REPORT_BYTES, ReportReadSession, project_report, report_sha256
from conversation_store import ConversationStore, projected_conversation_title
from runtime_settings import load_agent_policy
from agent_policy import resolve_agent_policy
from api_error_details import build_diagnostic, build_local_diagnostic, format_diagnostic, sanitize_diagnostic
from source_versions import record_version, versions
from source_update import SourceUpdateBackup
from remote_source import (RemoteSourceError, fetch_remote_source, parse_remote_source,
                           resolve_remote_source, validate_remote_token, validate_remote_username)
from remote_incremental import cached_snapshot_matches
from app_paths import RESOURCE_ROOT, state_root
from response_language import localized_text, resolve_answer_language
from answer_markdown import DEFAULT_ANSWER_INSTRUCTIONS


WEB_ROOT = RESOURCE_ROOT / "web"
FRAMEWORK_DEMO_SOURCE = RESOURCE_ROOT / "fixtures" / "framework-workbench" / "source"
FRAMEWORK_DEMO_OUTPUT = state_root() / "framework-runs"
STATIC_FILES = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/i18n.js": "i18n.js", "/styles.css": "styles.css",
                "/appearance.css": "appearance.css", "/theme.js": "theme.js", "/layout.js": "layout.js",
                "/markdown.js": "markdown.js", "/marked.umd.js": "marked.umd.js"}
ONLINE_STATIC_FILES = {"/online": "online.html", "/online.html": "online.html",
                       "/online.js": "online.js", "/online.css": "online.css"}
MAX_REQUEST_BYTES = 32_768
MAX_PROGRESS_WAIT_MS = 20_000
MAX_PROGRESS_WAITERS = 8
REJECTED_BODY_DRAIN_SECONDS = 0.25
MAX_GRAPH_EDGES = 200
MAX_SOURCE_WORKSPACES = 100
MAX_RECENT_CONVERSATIONS = 200
MAX_QUESTION_SUBMISSIONS = 8192
MAX_JOB_STREAMS = 8
JOB_STREAM_INTERVAL_SECONDS = 0.05
JOB_STREAM_HEARTBEAT_SECONDS = 5.0
JOB_STREAM_WRITE_TIMEOUT_SECONDS = 5.0
SOURCE_UPDATE_PENDING = "source-update-pending.json"
OUTPUT_NAMES = frozenset((*OUTPUT_FILE_NAMES, *OUTPUT_DIRECTORY_NAMES, SOURCE_UPDATE_PENDING))
EVIDENCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}")
LOGGER = logging.getLogger(__name__)


def _analysis_update(value):
    """Accept a small progress summary, never an arbitrary provider envelope."""
    if not isinstance(value, dict) or not isinstance(value.get("summary"), str):
        return None
    fields = ("round", "evidence_count", "completed_items", "total_items")
    if any(type(value.get(key)) is not int or not 0 <= value[key] <= 1_000_000 for key in fields):
        return None
    if value["round"] < 1 or value["completed_items"] > value["total_items"]:
        return None
    open_items = value.get("open_items")
    if not isinstance(open_items, list) or any(not isinstance(item, str) for item in open_items[:32]):
        return None
    return {**{key: value[key] for key in fields}, "summary": value["summary"][:2000],
            "open_items": [item[:300] for item in open_items[:32]]}


def _answer_preview(value):
    """Keep only a bounded display draft; it carries no answer authority."""
    if (not isinstance(value, dict) or not isinstance(value.get("text"), str)
            or type(value.get("round")) is not int or not 1 <= value["round"] <= 1_000_000
            or not isinstance(value.get("stage"), str)
            or value.get("stage") not in {"draft", "revision", "continuation", "answer", "verifying"}):
        return None
    return {"text": value["text"][:24000], "round": value["round"], "stage": value["stage"]}


def _analysis_state(value):
    """Project a self-contained public investigation snapshot, not model reasoning."""
    if not isinstance(value, dict) or value.get("schema_version") != "business-investigation-progress/v1":
        return None

    def number(item):
        return type(item) is int and 0 <= item <= 9_007_199_254_740_991

    def bounded_text(item):
        return item if len(item) <= 2000 else item[:1999] + "…"

    def prose(item):
        return (isinstance(item, str) and not re.search(
            r'<(?:think|analysis)(?:\s|>)|["\'](?:reasoning_content|reasoning|chain_of_thought|tool_calls|'
            r'search|read|focus|findings|framework_search|inspect_business_context|list_impact|search_concepts|'
            r'backward_slice|forward_slice|explain_assignment|trace_parameter|explain_external_effect)["\']\s*:', item, re.I))

    if (not number(value.get("revision")) or not number(value.get("round")) or value["round"] < 1
            or not isinstance(value.get("phase"), str)
            or value["phase"] not in {"investigate", "compacting", "answer", "review"}
            or not prose(value.get("summary")) or not number(value.get("omitted_findings"))):
        return None
    counts = value.get("counts")
    count_keys = ("candidate", "open", "conflicts", "total_sources")
    if not isinstance(counts, dict) or any(not number(counts.get(key)) for key in count_keys):
        return None
    findings = value.get("findings")
    if not isinstance(findings, list):
        return None
    rows = []
    for row in findings[:64]:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or not 1 <= len(row["id"]) <= 180
                or not isinstance(row.get("kind"), str)
                or row["kind"] not in {"rules", "conditions", "handoffs", "conflicts", "open_questions"}
                or not prose(row.get("text")) or not number(row.get("evidence_count"))):
            return None
        rows.append({"id": row["id"], "kind": row["kind"], "text": bounded_text(row["text"]),
                     "evidence_count": row["evidence_count"]})
    projected = {"schema_version": value["schema_version"], "revision": value["revision"],
        "round": value["round"], "phase": value["phase"], "summary": bounded_text(value["summary"]),
        "counts": {key: counts[key] for key in count_keys}, "findings": rows,
        "omitted_findings": min(9_007_199_254_740_991, value["omitted_findings"] + max(0, len(findings) - 64))}
    if "global_pending" in counts or "has_pending_work" in value:
        if (not number(counts.get("global_pending")) or type(value.get("has_pending_work")) is not bool
                or counts["global_pending"] > 0 and not value["has_pending_work"]):
            return None
        projected["counts"]["global_pending"] = counts["global_pending"]
        projected["has_pending_work"] = value["has_pending_work"]
    if "compaction" in value:
        compact = value["compaction"]
        keys = ("cycle", "before_bytes", "after_bytes", "retained_entries", "deferred_sources")
        if (not isinstance(compact, dict) or not isinstance(compact.get("status"), str)
                or compact["status"] not in {"not_needed", "running", "completed", "failed"}
                or any(not number(compact.get(key)) for key in keys)
                or type(compact.get("structural_preservation_verified")) is not bool):
            return None
        projected["compaction"] = {"status": compact["status"], **{key: compact[key] for key in keys},
            "structural_preservation_verified": compact["structural_preservation_verified"]}
    return projected


class RequestError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400, *, diagnostic=None) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.diagnostic = sanitize_diagnostic(diagnostic)


def _failure_diagnostic(exc, fallback_code, *, http_status=None):
    """Serialize classified metadata only; exception text stays private."""
    diagnostic = sanitize_diagnostic(getattr(exc, "diagnostic", None))
    if diagnostic:
        return _log_safe_failure(diagnostic)
    code = exc.code if isinstance(exc, (APIClientError, APIConfigurationError)) else fallback_code
    status = getattr(exc, "http_status", http_status)
    return _log_safe_failure(build_local_diagnostic(exc, code, http_status=status))


def _log_safe_failure(value):
    diagnostic = sanitize_diagnostic(value)
    LOGGER.warning("api_failure %s", json.dumps(diagnostic, ensure_ascii=False))
    return diagnostic


def _workspace_key(source, output):
    return hashlib.sha256(json.dumps([str(Path(source).resolve()), str(Path(output).resolve())],
                                    ensure_ascii=False).encode("utf-8")).hexdigest()


def _project(source: str | None = None, output: str | None = None, run_id: str | None = None) -> dict:
    synthetic = bool(source and Path(source).resolve() == FRAMEWORK_DEMO_SOURCE.resolve())
    return {
        "run_id": run_id, "source": source, "output": output,
        "source_origin": "synthetic_framework" if synthetic else "local_source",
        **({"workspace_id": _workspace_key(source, output)} if source and output and not synthetic else {}),
        "diagnosis": None, "programs": [], "agent": None, "snapshot_id": None,
        "relations": {"edges": [], "truncated": False}, "catalog_snapshot_id": None,
    }


def _source_preferences(value):
    """Recover form inputs only; they never authorize a source snapshot."""
    if not isinstance(value, dict):
        return None
    source, output = value.get("source"), value.get("output")
    if (not all(isinstance(item, str) and item and "\x00" not in item and len(item) <= 4096
                for item in (source, output)) or not Path(source).is_absolute()
            or not Path(output).is_absolute() or Path(source).resolve() == FRAMEWORK_DEMO_SOURCE.resolve()):
        return None
    encoding, source_format = value.get("encoding", "auto"), value.get("source_format", "auto")
    if (not isinstance(encoding, str) or len(encoding) > 64 or not isinstance(source_format, str)
            or source_format not in {"auto", "fixed", "free"}):
        return None
    try:
        if encoding != "auto":
            codecs.lookup(encoding)
    except LookupError:
        return None
    extensions = value.get("extensions")
    if extensions is not None and (not isinstance(extensions, (list, tuple, set, frozenset))
                                  or not all(isinstance(item, str) and len(item) <= 64 for item in extensions)):
        return None
    reference = value.get("framework_reference_path")
    return {"source": source, "output": output, "encoding": encoding, "source_format": source_format,
            "extensions": sorted(extensions) if extensions is not None else None,
            "framework_reference_path": reference if isinstance(reference, str) and len(reference) <= 4096 else None,
            **({"compilation_context_path": value["compilation_context_path"]}
               if isinstance(value.get("compilation_context_path"), str) and len(value["compilation_context_path"]) <= 4096 else {})}


def _remote_metadata(value):
    """Recover only public provenance, never credentials or fetch instructions."""
    if not isinstance(value, dict) or value.get("provider") not in {"bitbucket", "bitbucket_cloud"}:
        return None
    commit = value.get("commit")
    if not isinstance(commit, str) or re.fullmatch(r"[a-fA-F0-9]{40,64}", commit) is None:
        return None
    try:
        spec = parse_remote_source(value.get("url"), ref=value.get("ref"), subdirectory=value.get("subdirectory"))
    except (RemoteSourceError, TypeError, ValueError):
        return None
    result = {**spec.public_metadata(), "commit": commit}
    branch = value.get("default_branch")
    if isinstance(branch, str) and 0 < len(branch) <= 1024 and not any(ord(char) < 32 for char in branch):
        result["default_branch"] = branch
    if value.get("sync_mode") in {"archive", "incremental"}:
        result["sync_mode"] = value["sync_mode"]
        for name in ("downloaded_files", "reused_files", "removed_files", "downloaded_bytes", "file_count"):
            count = value.get(name)
            if type(count) is int and 0 <= count <= 2**53 - 1:
                result[name] = count
    return result


def _remote_identity(spec):
    """One repository selection can have many immutable local revisions."""
    return (spec.base_url, spec.project, spec.repository, spec.ref, spec.subdirectory)


def _text(payload: dict, key: str, *, required: bool = False, maximum: int = 1024) -> str | None:
    value = payload.get(key)
    if not required and (value is None or (isinstance(value, str) and not value.strip())):
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise RequestError("INVALID_OPTIONS", f"{key} 必须是有效的非空文本。")
    return value.strip()


def _existing_source_output(source: Path, output: Path) -> bool:
    """Recognize an existing library without treating its old index as current.

    User exports and notes are left untouched. Source validity is checked by
    the normal import/question path; this check only identifies folder ownership.
    """
    database = output / "structural-index.sqlite"
    source_key = hashlib.sha256(str(source).encode()).hexdigest()
    try:
        if not database.is_file():
            # A missing index can be rebuilt beside retained conversations.
            # A present but contradictory index never falls back to history.
            if database.exists() or database.is_symlink():
                return False
            history = output / "conversations.sqlite"
            if history.is_symlink() or not history.is_file():
                return False
            with closing(sqlite3.connect(f"{history.as_uri()}?mode=ro", uri=True, timeout=0.1)) as connection:
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('conversations','messages')")}
                if tables != {"conversations", "messages"}:
                    return False
                required_columns = {
                    "conversations": {"id", "source_key", "title", "snapshot_id", "created_at", "updated_at"},
                    "messages": {"position", "id", "conversation_id", "role", "content", "status", "created_at", "run_id", "details"},
                }
                for table, required in required_columns.items():
                    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                    if not required.issubset(columns):
                        return False
                return connection.execute(
                    "SELECT 1 FROM conversations WHERE source_key=? AND length(id)>0 LIMIT 1", (source_key,)).fetchone() is not None
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=0.1)) as connection:
            metadata = dict(connection.execute(
                "SELECT key,value FROM metadata WHERE key IN ('snapshot_id','source_root_hash')"))
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('source_files','symbols','code_units')")}
        return (isinstance(metadata.get("snapshot_id"), str)
                and metadata["snapshot_id"] not in {"", "unknown"}
                and tables == {"source_files", "symbols", "code_units"}
                and metadata.get("source_root_hash") == source_key)
    except (OSError, ValueError, sqlite3.Error):
        return False


def _validate_options(payload: object) -> dict:
    fields = {"source", "output", "encoding", "source_format", "entry", "question", "locale", "allow_network", "extensions", "index_mode", "verify_content", "max_source_pages", "reading_strategy", "answer_detail", "conversation_id", "retry_message_id", "continue_message_id", "framework_reference_path", "capture_api_responses", "compilation_context_path"}
    if not isinstance(payload, dict) or set(payload) - fields:
        raise RequestError("INVALID_OPTIONS", "请求包含未知设置。")
    source_text, output_text = _text(payload, "source", required=True), _text(payload, "output", required=True)
    source_input, output_input = Path(source_text).expanduser(), Path(output_text).expanduser()
    if not source_input.is_absolute() or not output_input.is_absolute():
        raise RequestError("INVALID_PATH", "源码和输出位置必须使用本机绝对路径。")
    try:
        source, output = _paths(source_input, output_input)
        if not source.is_dir():
            raise ValueError("missing source")
        # A recognized library may also contain user exports or notes. They
        # are never overwritten or removed by an index refresh.
        if (output.exists() and any(path.name not in OUTPUT_NAMES for path in output.iterdir())
                and not _existing_source_output(source, output)):
            raise ValueError("unrelated output contents")
        pending = output / SOURCE_UPDATE_PENDING
        if pending.is_symlink() or (pending.exists() and not pending.is_file()):
            raise ValueError("invalid source update marker")
    except (OSError, ValueError):
        raise RequestError(
            "INVALID_PATH",
            "请使用存在的源码目录和独立的结果目录；可继续使用该源码原有的分析结果，无需清空。新位置请选新建或空目录，且不要与源码嵌套或使用符号链接产物。",
        ) from None
    encoding = _text(payload, "encoding", maximum=64) or "auto"
    if encoding != "auto":
        try:
            codecs.lookup(encoding)
        except LookupError:
            raise RequestError("INVALID_ENCODING", "无法识别所选源码编码。") from None
    source_format = _text(payload, "source_format", maximum=16) or "auto"
    if source_format not in {"auto", "fixed", "free"}:
        raise RequestError("INVALID_SOURCE_FORMAT", "源码格式须为 auto、fixed 或 free。")
    allow_network = payload.get("allow_network", False)
    if type(allow_network) is not bool:
        raise RequestError("INVALID_OPTIONS", "allow_network 必须为布尔值。")
    capture_api_responses = payload.get("capture_api_responses", False)
    if type(capture_api_responses) is not bool:
        raise RequestError("INVALID_OPTIONS", "capture_api_responses 必须为布尔值。")
    index_mode = _text(payload, "index_mode", maximum=16) or "catalog"
    if index_mode not in {"catalog", "full"}:
        raise RequestError("INVALID_OPTIONS", "index_mode 必须是 catalog 或 full。")
    verify_content = payload.get("verify_content", False)
    if type(verify_content) is not bool:
        raise RequestError("INVALID_OPTIONS", "verify_content 必须为布尔值。")
    max_source_pages = payload.get("max_source_pages", 4)
    if type(max_source_pages) is not int or not 1 <= max_source_pages <= 128:
        raise RequestError("INVALID_OPTIONS", "阅读段数须为 1 到 128 的整数。")
    reading_strategy = _text(payload, "reading_strategy", maximum=32) or "retrieval"
    if reading_strategy not in {"retrieval", "focused", "full_chain"}:
        raise RequestError("INVALID_OPTIONS", "阅读方式须为 retrieval、focused 或 full_chain。")
    answer_detail = _text(payload, "answer_detail", maximum=16) or "detailed"
    if answer_detail not in {"brief", "detailed"}:
        raise RequestError("INVALID_OPTIONS", "回答详略须为 brief 或 detailed。")
    locale = _text(payload, "locale", maximum=16)
    if locale is not None and locale not in {"en", "zh-CN", "zh-HK"}:
        raise RequestError("INVALID_OPTIONS", "locale must be en, zh-CN or zh-HK.")
    extension_text = _text(payload, "extensions", maximum=256)
    try:
        extensions = parse_extensions(extension_text)
    except ValueError:
        raise RequestError("INVALID_EXTENSIONS", "扩展名格式无效。") from None
    retry_message_id = _text(payload, "retry_message_id", maximum=64)
    if retry_message_id and re.fullmatch(r"[a-f0-9]{32}", retry_message_id) is None:
        raise RequestError("INVALID_OPTIONS", "重试消息标识无效。")
    continue_message_id = _text(payload, "continue_message_id", maximum=64)
    if continue_message_id and (re.fullmatch(r"[a-f0-9]{32}", continue_message_id) is None or retry_message_id):
        raise RequestError("INVALID_OPTIONS", "继续消息标识无效，不能同时重试。")
    compilation_context_path = _text(payload, "compilation_context_path", maximum=4096)
    if compilation_context_path:
        context_file = Path(compilation_context_path).expanduser()
        if not context_file.is_absolute() or not context_file.is_file() or context_file.stat().st_size > 128000:
            raise RequestError("INVALID_COMPILATION_CONTEXT", "编译设置须为存在的本机 JSON 文件。")
    return {
        "source": source, "output": output, "encoding": encoding, "source_format": source_format,
        "entry": _text(payload, "entry"), "question": _text(payload, "question", maximum=8000),
        "allow_network": allow_network, "extensions": extensions, "index_mode": index_mode, "verify_content": verify_content,
        "capture_api_responses": capture_api_responses,
        "analysis_mode": "business", "max_source_pages": max_source_pages, "reading_strategy": reading_strategy,
        "answer_detail": answer_detail,
        **({"locale": locale} if locale is not None else {}),
        "conversation_id": _text(payload, "conversation_id", maximum=64),
        "retry_message_id": retry_message_id,
        **({"continue_message_id": continue_message_id} if continue_message_id else {}),
        "framework_reference_path": _text(payload, "framework_reference_path", maximum=4096),
        **({"compilation_context_path": compilation_context_path} if compilation_context_path else {}),
    }


def _read_json(path: Path, *, check_cancel=None, report_session=None) -> object:
    if check_cancel:
        check_cancel()
    if path.is_symlink() or not path.is_file():
        raise ValueError("invalid report")
    if path.stat().st_size <= DIRECT_REPORT_BYTES:
        return json.loads(path.read_text(encoding="utf-8"))
    view_path = path.with_name(f"{path.stem}-view.json")
    if view_path.is_symlink() or not view_path.is_file() or view_path.stat().st_size > VIEW_REPORT_BYTES:
        raise ValueError("invalid report view")
    view = json.loads(view_path.read_text(encoding="utf-8"))
    projection = view.get("display_projection", {}) if isinstance(view, dict) else {}
    digest = report_session.sha256 if report_session is not None else report_sha256
    if projection.get("source_size") != path.stat().st_size or projection.get("source_sha256") != digest(path, check_cancel=check_cancel):
        raise ValueError("report view does not match complete report")
    return view


def _workspace_diagnosis(path: Path, *, check_cancel=None, report_session=None) -> object:
    try:
        return _read_json(path, check_cancel=check_cancel, report_session=report_session)
    except ValueError:
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= DIRECT_REPORT_BYTES:
            raise
    # Older imports did not produce a diagnosis view. Convert the complete,
    # validated JSON once; future restarts read its bounded companion. This
    # touches the report only and preserves the original file byte for byte.
    from report_view import project_report, write_report_view
    if check_cancel:
        check_cancel()
    with path.open(encoding="utf-8") as stream:
        complete = json.load(stream)
    if check_cancel:
        check_cancel()
    if not isinstance(complete, dict):
        raise ValueError("invalid diagnosis")
    view = project_report(complete)
    try:
        write_report_view(path, complete, check_cancel=check_cancel)
    except (OSError, ValueError):
        pass
    return view


def _unaccepted_agent(agent: dict, reason_code: str) -> dict:
    """Retain response text for diagnostics without granting citation authority."""
    rejected = {"runner_status": "NOT_READY", "reason_code": reason_code, "agent_result": None}
    diagnostic = sanitize_diagnostic(agent.get("diagnostic")) or sanitize_diagnostic(
        (agent.get("agent_result") or {}).get("diagnostic"))
    if diagnostic:
        rejected["diagnostic"] = diagnostic
    prior = sanitize_diagnostic(agent.get("prior_diagnostic"))
    if prior:
        rejected["prior_diagnostic"] = prior
    if isinstance(agent.get("api_diagnostics"), dict):
        rejected["api_diagnostics"] = agent["api_diagnostics"]
    if isinstance(agent.get("display_projection"), dict):
        rejected["display_projection"] = agent["display_projection"]
    summary = agent.get("diagnostic_summary") or (agent.get("agent_result") or {}).get("diagnostic_summary")
    if isinstance(summary, dict):
        rejected["diagnostic_summary"] = summary
    response = agent.get("unaccepted_response")
    answer = agent.get("agent_result")
    text = response.get("text") if isinstance(response, dict) else None
    if not isinstance(text, str) or not text.strip():
        if isinstance(answer, dict) and answer.get("model_answer_recorded") is not False:
            narrative = answer.get("narrative")
            text = narrative.get("text") if isinstance(narrative, dict) else None
            if not isinstance(text, str) or not text.strip():
                text = answer.get("answer")
    if isinstance(text, str) and text.strip():
        rejected["unaccepted_response"] = {"reason_code": reason_code, "text": text}
    return rejected


def _has_business_answer(value, *, runner_completed=False):
    text = value.get("answer") or value.get("content")
    if (not isinstance(text, str) or not text.strip() or value.get("answer_available") is False
            or value.get("answer_origin") == "diagnostic"):
        return False
    return (value.get("answer_available") is True or value.get("model_answer_recorded") is True
        or value.get("answer_origin") in {"model", "local_source_facts"}
        or runner_completed and value.get("model_answer_recorded") is not False)


def _relationships(database: Path, snapshot_id: str) -> dict:
    """Return a bounded sample by source member, retaining exact evidence paths."""
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        stored_snapshot = connection.execute("SELECT value FROM metadata WHERE key='snapshot_id'").fetchone()
        if stored_snapshot is None or stored_snapshot[0] != snapshot_id:
            raise ValueError("relationship snapshot mismatch")
        columns = """SELECT r.relation_id, u.program_name AS source_program,
                      r.target_name, r.relation_type, r.status, r.target_entity_id,
                      e.evidence_id, e.relative_path, e.start_line, e.end_line
                 FROM relations r
                 JOIN code_units u ON u.unit_id = r.from_entity_id
                 JOIN evidence_spans e ON e.evidence_id = r.evidence_id
                WHERE r.relation_type IN ('CALLS', 'CALL_TARGET_FROM', 'INCLUDES_COPY')"""
        indexed = connection.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
                                     "AND name='repo_relations_path'").fetchone()
        if indexed:
            # A startup preview is bounded by displayed edges. Visit source
            # members in path order and stop after one extra edge; validating
            # every later relation made opening a large saved library perform
            # an unnecessary full graph scan before the server could start.
            rows = [dict(row) for row in connection.execute("""
                SELECT r.relation_id, u.program_name AS source_program,
                       r.target_name, r.relation_type, r.status, r.target_entity_id,
                       e.evidence_id, e.relative_path, e.start_line, e.end_line,
                       r.relative_path AS preview_source_path
                  FROM source_files f
                  CROSS JOIN relations r INDEXED BY repo_relations_path
                    ON r.relative_path=f.relative_path
                  JOIN code_units u ON u.unit_id=r.from_entity_id
                  JOIN evidence_spans e ON e.evidence_id=r.evidence_id
                 WHERE r.relation_type IN ('CALLS','CALL_TARGET_FROM','INCLUDES_COPY')
                 ORDER BY f.relative_path LIMIT ?""", (MAX_GRAPH_EDGES + 1,))]
            # Preserve readable line ordering among the bounded selection;
            # evidence paths remain the actual provenance, even across files.
            rows.sort(key=lambda row: (row["preview_source_path"], row["start_line"], row["relation_type"]))
            for row in rows:
                row.pop("preview_source_path")
        else:
            rows = [dict(row) for row in connection.execute(columns +
                " ORDER BY e.relative_path,e.start_line,r.relation_type LIMIT ?", (MAX_GRAPH_EDGES + 1,))]
    return {"edges": rows[:MAX_GRAPH_EDGES], "truncated": len(rows) > MAX_GRAPH_EDGES, "snapshot_id": snapshot_id}


def _graph_file_change_time(descriptor):
    """Read metadata change time from the already-open file, never creation time."""
    try:
        if sys.platform != "win32":
            return os.fstat(descriptor).st_ctime_ns
        import ctypes
        import msvcrt

        class FileBasicInfo(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in
                ("CreationTime", "LastAccessTime", "LastWriteTime", "ChangeTime")] + [
                    ("FileAttributes", ctypes.c_uint32)]

        observe = ctypes.windll.kernel32.GetFileInformationByHandleEx
        observe.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
        observe.restype = ctypes.c_int
        information = FileBasicInfo()
        # get_osfhandle borrows the stream's handle; its context manager closes it.
        handle = msvcrt.get_osfhandle(descriptor)
        if handle == -1 or not observe(handle, 0, ctypes.byref(information), ctypes.sizeof(information)):
            return None
        return information.ChangeTime if information.ChangeTime > 0 else None
    except Exception:
        # Unsupported filesystems or unavailable metadata only disable reuse.
        return None


def _graph_database_signature(database: Path):
    """Identify an unchanged, checkpointed file without keeping it open.

    This only controls reuse of the small graph preview. Uncertain files still
    use the original query; source analysis and its evidence are never cached.
    """
    def identity(value):
        # On Windows, stat(path).ctime is creation time while fstat(fd).ctime
        # can be change time. Compare their shared birthtime; the separate
        # handle-based ChangeTime check below still detects metadata changes.
        identity_time = "st_birthtime_ns" if sys.platform == "win32" else "st_ctime_ns"
        fields = tuple(getattr(value, key, None) for key in
            ("st_dev", "st_ino", "st_size", "st_mtime_ns", identity_time))
        if (not stat.S_ISREG(value.st_mode) or any(type(item) is not int for item in fields)
                or fields[0] < 0 or fields[1] <= 0 or fields[2] < 100
                or fields[3] <= 0 or fields[4] <= 0):
            return None
        return fields

    def checkpointed():
        for suffix in ("-wal", "-journal"):
            sidecar = Path(str(database) + suffix)
            if sidecar.is_symlink():
                return False
            try:
                observed = sidecar.stat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(observed.st_mode) or observed.st_size:
                return False
        return True

    try:
        if database.is_symlink() or not checkpointed():
            return None
        before = identity(database.stat())
        if before is None:
            return None
        with database.open("rb") as stream:
            change_before = _graph_file_change_time(stream.fileno())
            header = stream.read(100)
            opened = identity(os.fstat(stream.fileno()))
            change_after = _graph_file_change_time(stream.fileno())
        if type(change_before) is not int or change_before <= 0 or change_before != change_after:
            return None
        if before != opened or before != identity(database.stat()) or not checkpointed():
            return None
        if len(header) != 100 or header[:16] != b"SQLite format 3\x00" or header[18:20] not in (b"\x01\x01", b"\x02\x02"):
            return None
        page_size = int.from_bytes(header[16:18], "big")
        page_size = 65536 if page_size == 1 else page_size
        if (page_size < 512 or page_size > 65536 or page_size & (page_size - 1)
                or before[2] % page_size or header[24:28] != header[92:96]):
            return None
        return (str(database.resolve()), *before, change_after, header)
    except (OSError, ValueError):
        return None


def _environment_config() -> CompanyAPIConfig:
    from model_profiles import model_config
    return model_config("workbench")


_FOLDER_DIALOG_SCRIPT = """
import json
import sys
try:
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    selected = filedialog.askdirectory(parent=root, initialdir=sys.argv[1], mustexist=True)
    root.destroy()
    print(json.dumps({'path': selected or None}))
except Exception:
    print(json.dumps({'error': 'FOLDER_PICKER_UNAVAILABLE'}))
"""

_MAC_FOLDER_DIALOG_SCRIPT = """
function run(argv) {
    var app = Application.currentApplication();
    app.includeStandardAdditions = true;
    try {
        return app.chooseFolder({withPrompt: "Choose a folder", defaultLocation: Path(argv[0])}).toString();
    } catch (error) {
        if (error.errorNumber === -128 || String(error).indexOf("(-128)") !== -1) return "__CANCELLED__";
        throw error;
    }
}
"""


def _pick_macos_folder(initial_path: Path) -> str | None:
    try:
        child = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", _MAC_FOLDER_DIALOG_SCRIPT, str(initial_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=300, check=False,
        )
    except subprocess.TimeoutExpired:
        raise RequestError("FOLDER_PICKER_TIMEOUT", "选择文件夹窗口已超时，请重试。", 504) from None
    except OSError:
        raise RequestError("FOLDER_PICKER_UNAVAILABLE", "无法打开本机文件夹选择窗口，请手动填写路径。", 503) from None
    if child.returncode:
        raise RequestError("FOLDER_PICKER_UNAVAILABLE", "无法打开本机文件夹选择窗口，请手动填写路径。", 503)
    selected = child.stdout.strip()
    if selected == "__CANCELLED__":
        return None
    directory = Path(selected).expanduser()
    if not selected or not directory.is_absolute() or not directory.is_dir():
        raise RequestError("FOLDER_PICKER_UNAVAILABLE", "无法打开本机文件夹选择窗口，请手动填写路径。", 503)
    return str(directory.resolve())


def _pick_local_folder(initial_path: Path) -> str | None:
    """Run the native dialog on the child process's main thread."""
    try:
        child = subprocess.run(
            [sys.executable, "-c", _FOLDER_DIALOG_SCRIPT, str(initial_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=300, check=False,
        )
    except subprocess.TimeoutExpired:
        raise RequestError("FOLDER_PICKER_TIMEOUT", "选择文件夹窗口已超时，请重试。", 504) from None
    except OSError:
        if sys.platform == "darwin":
            return _pick_macos_folder(initial_path)
        raise RequestError("FOLDER_PICKER_UNAVAILABLE", "无法打开本机文件夹选择窗口，请手动填写路径。", 503) from None
    try:
        result = json.loads(child.stdout)
        if child.returncode or not isinstance(result, dict) or result.get("error"):
            raise ValueError("dialog unavailable")
        selected = result["path"]
        if selected is None:
            return None
        directory = Path(selected).expanduser()
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError("invalid selected path")
        return str(directory.resolve())
    except (KeyError, TypeError, ValueError, OSError):
        if sys.platform == "darwin":
            return _pick_macos_folder(initial_path)
        raise RequestError("FOLDER_PICKER_UNAVAILABLE", "无法打开本机文件夹选择窗口，请手动填写路径。", 503) from None


class WorkbenchState:
    """One in-memory browser session, one job, and one current source snapshot."""

    def __init__(self, *, analyzer: Callable = analyze_source, config_provider: Callable = _environment_config,
                 demo_output_root: Path | None = None, state_path: Path | None = None,
                 remote_source_root: Path | None = None,
                 model_check_transport: Transport | None = None,
                 policy_provider: Callable = load_agent_policy,
                 folder_picker: Callable[[Path], str | None] = _pick_local_folder,
                 background_restore: bool = False) -> None:
        self.session_token = secrets.token_urlsafe(32)
        self.analyzer, self.config_provider = analyzer, config_provider
        self.policy_provider = policy_provider
        self.model_check_transport, self.folder_picker = model_check_transport, folder_picker
        self.lock = threading.RLock()
        self._graph_preview_cache = None
        self.progress_changed = threading.Condition(self.lock)
        self.progress_waiters = 0
        self.project = _project()
        self.job: dict | None = None
        self._question_submissions = {}
        self.cancel_event = threading.Event()
        self.started_at = self.updated_at = self.phase_started_at = self.progress_updated_at = 0.0
        self.phase_start_completed = 0.0
        self.demo_output_root = demo_output_root if demo_output_root is not None else FRAMEWORK_DEMO_OUTPUT
        self.state_path = state_path
        self.remote_source_root = (remote_source_root if remote_source_root is not None else
                                   (state_path.parent if state_path else state_root()) / "remote-sources")
        self.framework_reference_path = None
        self.answer_detail = "detailed"
        self.answer_instructions = ""
        self.source_preferences = None
        self.source_workspaces = {}
        self._history_truncated = False
        self._recent_history_truncated = False
        self._local_workspace = None
        self._saved_workspace = None
        self._startup_upgrade_plan = None
        self.conversation = None
        self.conversation_store = None
        self._restore_check_cancel = None
        self._restore_progress = None
        self._restore_report_session = None
        self._workspace_profile_loaded = False
        self._retained_startup_workspace = None
        self._profile_restore_incomplete = False
        self._startup_restore_archive = None
        self._startup_restore_thread = None
        if background_restore and state_path is not None and state_path.is_file() and not state_path.is_symlink():
            self._begin_workspace_restore()
        else:
            self._restore_workspace()
            self._upgrade_restored_index()

    def _begin_workspace_restore(self):
        """Publish progress before reading retained reports or an old index."""
        with self.lock:
            self.started_at = self.updated_at = self.phase_started_at = self.progress_updated_at = time.monotonic()
            identifier = secrets.token_hex(16)
            self.job = {"job_id": identifier, "kind": "index", "origin": "workspace_restore",
                "source": None, "output": None, "status": "RUNNING", "result": None, "error": None,
                "cancel_requested": False, "progress": {"phase": "restoring_workspace", "completed": 0,
                    "total": None, "unit": "files", "current_file": None, "bytes_completed": 0, "bytes_total": None}}
            self._notify_job_changed()
            self._startup_restore_thread = threading.Thread(target=self._run_workspace_restore,
                args=(identifier,), daemon=True, name="workspace-restore")
            self._startup_restore_thread.start()

    def _run_workspace_restore(self, identifier):
        # Build the entire restored view privately. Readers never observe half
        # of one library combined with the conversation or snapshot of another.
        candidate = WorkbenchState(analyzer=self.analyzer, config_provider=self.config_provider,
            demo_output_root=self.demo_output_root, remote_source_root=self.remote_source_root,
            policy_provider=self.policy_provider)
        candidate.state_path = self.state_path
        candidate._restore_check_cancel = self._check_cancel
        candidate._restore_progress = lambda phase: self._progress(identifier,
            {"phase": phase, "completed": 0, "total": None})
        try:
            candidate._restore_workspace()
            self._check_cancel()
            # Recheck upgrade provenance on this worker, outside the public
            # state lock. The update worker still performs its integrity gate.
            from workspace_upgrade import plan_workspace_upgrade
            plan = (plan_workspace_upgrade(candidate._saved_workspace, check_integrity=False,
                       check_cancel=self._check_cancel, report_session=candidate._restore_report_session)
                    if (candidate._startup_upgrade_plan or {}).get("status") == "ready" else {})
            with self.lock:
                self._check_cancel()
                for key in ("project", "conversation", "conversation_store", "framework_reference_path",
                            "_graph_preview_cache", "_local_workspace", "_saved_workspace", "_startup_upgrade_plan",
                            "_restore_report_session",
                            "answer_detail", "answer_instructions", "source_preferences", "source_workspaces",
                            "_history_truncated", "_recent_history_truncated"):
                    setattr(self, key, getattr(candidate, key))
                self.updated_at = time.monotonic()
                self.job.update(status="COMPLETED")
                self._notify_job_changed()
                # Keep this terminal receipt when an automatic version upgrade
                # immediately replaces the current job. Existing pollers can
                # finish restoration and reload the new job without a 404.
                self._startup_restore_archive = self._job_snapshot()
                self._upgrade_restored_index(_verified_plan=plan)
        except AnalysisCancelled:
            with self.lock:
                self._retain_startup_profile(candidate)
                self.updated_at = time.monotonic()
                self.job.update(status="CANCELLED")
                self._notify_job_changed()
        except Exception as exc:
            with self.lock:
                self._retain_startup_profile(candidate)
                self.updated_at = time.monotonic()
                self.job.update(status="FAILED", error={"code": "WORKSPACE_RESTORE_FAILED",
                    "message": "恢复资料库未完成。原资料库和历史记录仍保留，请重新打开工作台。",
                    "diagnostic": _failure_diagnostic(exc, "WORKSPACE_RESTORE_FAILED")})
                self._notify_job_changed()

    def _retain_startup_profile(self, candidate):
        """Keep saved library/history navigation even when report loading stops."""
        if not candidate._workspace_profile_loaded:
            # An unreadable profile must never be replaced by initial defaults.
            self._profile_restore_incomplete = True
            return
        for key in ("framework_reference_path", "answer_detail", "answer_instructions", "source_preferences",
                    "source_workspaces", "_history_truncated", "_local_workspace"):
            setattr(self, key, getattr(candidate, key))
        self._retained_startup_workspace = copy.deepcopy(candidate._local_workspace)

    def _relationship_preview(self, database: Path, snapshot_id: str) -> dict:
        """Reuse one unchanged graph preview, with a fresh snapshot check.

        No connection survives this call, so workspace updates can still move
        or replace the database on Windows. A failed check discards the entry.
        """
        signature = _graph_database_signature(database)
        cached = self._graph_preview_cache
        self._graph_preview_cache = None
        if (signature is not None and cached is not None
                and cached["signature"] == signature
                and cached["snapshot_id"] == snapshot_id and cached["limit"] == MAX_GRAPH_EDGES):
            with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
                connection.execute("BEGIN")
                observed = connection.execute("SELECT value FROM metadata WHERE key='snapshot_id'").fetchone()
                if observed is None or observed[0] != snapshot_id:
                    raise ValueError("relationship snapshot mismatch")
                if _graph_database_signature(database) == signature:
                    result = copy.deepcopy(cached["result"])
                    self._graph_preview_cache = cached
                    return result
                signature = None
        result = _relationships(database, snapshot_id)
        if signature is not None and _graph_database_signature(database) == signature:
            self._graph_preview_cache = {"signature": signature, "snapshot_id": snapshot_id,
                "limit": MAX_GRAPH_EDGES, "result": copy.deepcopy(result)}
        return result

    def _upgrade_restored_index(self, *, _verified_plan=None):
        """Use the ordinary rollback-safe local update for known old versions."""
        if self._local_workspace is None:
            return
        from workspace_upgrade import plan_workspace_upgrade

        original = self._startup_upgrade_plan
        if not original or original["status"] != "ready":
            return
        # Display repair must never promote an originally mismatched report
        # into authority for an automatic source-scope change.
        plan = (_verified_plan if _verified_plan is not None else plan_workspace_upgrade(self._saved_workspace,
                check_integrity=False, report_session=self._restore_report_session))
        if plan["status"] != "ready" or plan["attempt_key"] != original["attempt_key"]:
            return
        try:
            # Record the attempt before starting. A failed or interrupted
            # upgrade must not repeat on every launch against the same snapshot.
            self._local_workspace["index_upgrade_attempt"] = plan["attempt_key"]
            self._save_workspace()
            self.start(plan["options"], _index_upgrade={"saved": copy.deepcopy(self._saved_workspace),
                "attempt_key": plan["attempt_key"], "report_session": self._restore_report_session})
            with self.lock:
                if self.job:
                    self.job["origin"] = "index_version_upgrade"
        except (OSError, ValueError, sqlite3.Error) as exc:
            # A failed upgrade stays recoverable in the original workspace;
            # it is not retried in a loop or converted into a model request.
            _failure_diagnostic(exc, "SOURCE_UPDATE_FAILED")

    @staticmethod
    def _workspace_record(value):
        if not isinstance(value, dict):
            return None
        source, output = value.get("source"), value.get("output")
        if not all(isinstance(item, str) and item and len(item) <= 4096 and "\x00" not in item
                   and Path(item).is_absolute() for item in (source, output)):
            return None
        try:
            source, output = _paths(Path(source), Path(output))
            if source == FRAMEWORK_DEMO_SOURCE.resolve():
                return None
            remote = _remote_metadata(value.get("remote_source") or value.get("_remote_source"))
            record = {"workspace_id": _workspace_key(source, output), "source": str(source), "output": str(output),
                      "restore_blocked": value.get("restore_blocked") is True,
                      "source_origin": "remote_repository" if remote else "local_source"}
            if remote:
                record["remote_source"] = remote
            conversation_id = value.get("conversation_id")
            if isinstance(conversation_id, str) and re.fullmatch(r"[a-f0-9]{32}", conversation_id):
                record["conversation_id"] = conversation_id
            attempt = value.get("index_upgrade_attempt")
            if isinstance(attempt, str) and re.fullmatch(r"[a-f0-9]{64}", attempt):
                record["index_upgrade_attempt"] = attempt
            reference = value.get("framework_reference_path")
            record["framework_reference_path"] = reference if isinstance(reference, str) and len(reference) <= 4096 else None
            return record
        except (OSError, ValueError):
            return None

    def _remember_workspace(self, workspace):
        previous = self.source_workspaces.pop(workspace["workspace_id"], None)
        if previous and "index_upgrade_attempt" in previous and "index_upgrade_attempt" not in workspace:
            workspace = {**workspace, "index_upgrade_attempt": previous["index_upgrade_attempt"]}
        self.source_workspaces[workspace["workspace_id"]] = workspace
        while len(self.source_workspaces) > MAX_SOURCE_WORKSPACES:
            self.source_workspaces.pop(next(iter(self.source_workspaces)))
            self._history_truncated = True

    @staticmethod
    def _history_database(workspace):
        path = Path(workspace["output"]) / "conversations.sqlite"
        if not path.is_file() or any(Path(str(path) + suffix).is_symlink() for suffix in ("", "-wal", "-shm")):
            raise ValueError("CONVERSATION_NOT_FOUND")
        return path

    def _workspace_has_history(self, workspace):
        try:
            path = self._history_database(workspace)
            source_key = hashlib.sha256(workspace["source"].encode()).hexdigest()
            with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.1)) as db:
                return db.execute("SELECT 1 FROM conversations WHERE source_key=? LIMIT 1", (source_key,)).fetchone() is not None
        except (OSError, ValueError, sqlite3.Error):
            return False

    @staticmethod
    def _recorded_continuation_entry(db, run_id, metadata, snapshot):
        """Recover an entry from the old run's own source selection."""
        if not isinstance(snapshot, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", snapshot):
            return None
        row = db.execute(
            """SELECT json_extract(checkpoint_json, '$.result.agent_result.investigation.business_map.source_identity')
                 FROM investigation_runs WHERE run_id=? AND json_valid(checkpoint_json)
                  AND json_extract(checkpoint_json, '$.quality.run_id')=?
                  AND json_extract(checkpoint_json, '$.quality.question_id')=?
                  AND json_extract(checkpoint_json, '$.quality.analysis_identity')=?
                  AND json_extract(checkpoint_json, '$.result.agent_result.run_id')=?
                  AND json_extract(checkpoint_json, '$.result.agent_result.snapshot_id')=?""",
            (run_id, run_id, metadata.get("question_id"), metadata.get("analysis_identity"), run_id, snapshot),
        ).fetchone()
        if row is None or not isinstance(row[0], str):
            return None
        identity = json.loads(row[0])
        if not isinstance(identity, dict) or identity.get("selection_source") != "entry_program":
            return None
        requested = identity.get("requested")
        if (isinstance(requested, list) and len(requested) == 1 and isinstance(requested[0], str)
                and 0 < len(requested[0]) <= 1024 and "\x00" not in requested[0]):
            return requested[0]
        return None

    def _recover_legacy_continuation(self, conversation, assistant):
        if (self.conversation_store is None or assistant.get("role") != "assistant"
                or not assistant.get("run_id")
                or assistant.get("status") not in {"partial", "failed"}
                or assistant.get("retry_blocked_reason") == "REMOTE_REQUEST_OUTCOME_UNKNOWN"):
            return {}
        user = next((item for item in conversation.get("messages", []) if item.get("role") == "user"
            and item.get("run_id") == assistant.get("run_id")
            and item.get("status") in {"partial", "failed", "cancelled", "interrupted"}), None)
        if user is None:
            return {}
        trace_run = None
        candidates = []
        trace = (assistant.get("metrics") or {}).get("quality_trace_path")
        if isinstance(trace, str):
            match = re.search(r"(?:^|/)([a-f0-9]{24})\.json$", trace.replace("\\", "/"))
            if match:
                trace_run = match[1]
                candidates.append(trace_run)
        active_result = ((self.project.get("agent") or {}).get("agent_result") or {})
        active = active_result.get("run_id")
        diagnosis = self.project.get("diagnosis") or {}
        active_match = self.project.get("run_id") == assistant["run_id"] and diagnosis.get("question") == user["content"]
        if (active_match and isinstance(active, str)
                and re.fullmatch(r"[a-f0-9]{24}", active)):
            candidates.append(active)
        if not candidates:
            return {}
        database = self.conversation_store.path.parent / "investigation-runs.sqlite"
        if not database.is_file() or any(Path(str(database) + suffix).is_symlink() for suffix in ("", "-wal", "-shm")):
            return {}
        from run_store import fingerprint
        from response_language import SUPPORTED_LANGUAGES
        try:
            target = self.conversation_store.retry_target(conversation["id"], user["id"])
            with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=0.1)) as db:
                for run_id in dict.fromkeys(candidates):
                    row = db.execute("SELECT metadata_json FROM investigation_runs WHERE run_id=?", (run_id,)).fetchone()
                    if row is None:
                        continue
                    metadata = json.loads(row[0])
                    if (not isinstance(metadata, dict) or metadata.get("question_id") !=
                            "sha256:" + hashlib.sha256(user["content"].encode()).hexdigest()):
                        continue
                    policy = resolve_agent_policy(metadata.get("configuration", {}).get("policy"))
                    history = self.conversation_store.model_history(conversation["id"],
                        before_position=target["position"], max_characters=policy.max_history_characters,
                        question=user["content"], include_legacy=True)
                    options = {"entry": None, "answer_detail": assistant.get("answer_detail", "detailed"),
                        "locale": assistant.get("locale"), "history_max_characters": policy.max_history_characters}
                    entries = [None]
                    if active_match and active == run_id:
                        entries.insert(0, diagnosis.get("entry_requested"))
                    if trace_run == run_id:
                        snapshot = assistant.get("snapshot_id")
                        entry = self._recorded_continuation_entry(db, run_id, metadata, snapshot)
                        if entry is not None:
                            entries.insert(0, entry)
                        # A restored report may still carry the exact spelling
                        # of this run's entry even if its source hint normalized it.
                        if (active == run_id and snapshot and active_result.get("snapshot_id") == snapshot
                                and diagnosis.get("question") == user["content"]
                                and diagnosis.get("source_root") == self.project.get("source")
                                and (diagnosis.get("build_report") or {}).get("snapshot_id") == snapshot):
                            entries.insert(0, diagnosis.get("entry_requested"))
                    histories = [history]
                    if getattr(history, "legacy", None) is not None:
                        histories.append(history.legacy)
                    for entry in dict.fromkeys(entries):
                        identity = {"database_path": str((database.parent / "structural-index.sqlite").resolve()),
                            "source_root": str(Path(self.project["source"]).resolve()), "entry_program": entry,
                            "framework_reference_path": str(Path(self.framework_reference_path).resolve())
                                if self.framework_reference_path else None,
                            "answer_detail": options["answer_detail"], "conversation_id": conversation["id"]}
                        # Recover the old record's presentation identity. The
                        # next job still uses the currently saved requirements.
                        for saved_history in histories:
                            scope = {**identity, "history": saved_history}
                            identities = {fingerprint(scope)} | {
                                fingerprint({**scope, "answer_language": language}) for language in SUPPORTED_LANGUAGES}
                            if metadata.get("analysis_identity") in identities:
                                options["entry"] = entry
                                return {"investigation_run_id": run_id, "analysis_options": options}
        except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
            pass
        return {}

    def _conversation_view(self):
        value = copy.deepcopy(self.conversation)
        if value is not None:
            for message in value.get("messages", []):
                if not message.get("investigation_run_id"):
                    message.update(self._recover_legacy_continuation(value, message))
        if value is not None and self.project.get("workspace_id"):
            value["workspace_id"] = self.project["workspace_id"]
        return value

    def recent_conversations(self):
        """Read only registered transcript metadata; never discover or create stores."""
        rows = []
        truncated = False
        for workspace in self.source_workspaces.values():
            try:
                path = self._history_database(workspace)
                source_key = hashlib.sha256(workspace["source"].encode()).hexdigest()
                remote = workspace.get("remote_source")
                label = Path(workspace["source"]).name or workspace["source"]
                if remote:
                    spec = parse_remote_source(remote["url"], ref=remote.get("ref"),
                                               subdirectory=remote.get("subdirectory"))
                    label = spec.repository + (" / " + spec.subdirectory if spec.subdirectory else "")
                with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.1)) as db:
                    db.row_factory = sqlite3.Row
                    items = db.execute(
                        "SELECT c.id,substr(c.title,1,256) AS title,c.snapshot_id,c.created_at,c.updated_at,"
                        "COALESCE((SELECT m.created_at FROM messages m WHERE m.conversation_id=c.id "
                        "AND m.role='user' ORDER BY m.position LIMIT 1),c.created_at) AS first_question_at,"
                        "(SELECT count(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count "
                        "FROM conversations c WHERE c.source_key=? "
                        "AND EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id=c.id) "
                        "ORDER BY first_question_at DESC,c.created_at DESC,c.id DESC LIMIT 101", (source_key,)).fetchall()
                    truncated = truncated or len(items) > 100
                    for item in items[:100]:
                        row = dict(item)
                        if (not isinstance(row["id"], str) or re.fullmatch(r"[a-f0-9]{32}", row["id"]) is None
                                or not all(isinstance(row[key], str) and len(row[key]) <= 128
                                           for key in ("first_question_at", "created_at", "updated_at"))):
                            continue
                        row["title"] = projected_conversation_title(db, row["id"], row["title"])
                        rows.append({**row, "workspace_id": workspace["workspace_id"],
                                     "source_origin": workspace["source_origin"], "source_label": label})
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                continue
        rows.sort(key=lambda row: (row["first_question_at"], row["created_at"], row["id"], row["workspace_id"]), reverse=True)
        self._recent_history_truncated = truncated or len(rows) > MAX_RECENT_CONVERSATIONS
        return rows[:MAX_RECENT_CONVERSATIONS]

    def saved_sources(self):
        rows = []
        for workspace in reversed(list(self.source_workspaces.values())):
            remote = workspace.get("remote_source")
            label = Path(workspace["source"]).name or workspace["source"]
            if remote:
                spec = parse_remote_source(remote["url"], ref=remote.get("ref"),
                                           subdirectory=remote.get("subdirectory"))
                label = spec.repository + (" / " + spec.subdirectory if spec.subdirectory else "")
            rows.append({"workspace_id": workspace["workspace_id"], "source_label": label,
                         "source_origin": workspace["source_origin"], "remote_source": copy.deepcopy(remote),
                         "available": Path(workspace["source"]).is_dir(),
                         "active": workspace["workspace_id"] == self.project.get("workspace_id")})
        return rows

    def _workspace_response(self):
        return {"project": copy.deepcopy(self.project), "conversation": self._conversation_view(),
                "conversations": self.conversation_store.list() if self.conversation_store else [],
                "recent_conversations": self.recent_conversations(),
                "recent_conversations_truncated": self._history_truncated or self._recent_history_truncated,
                "source_workspaces": self.saved_sources(),
                "source_preferences": copy.deepcopy(self.source_preferences), "framework_knowledge": self.framework()}

    def open_source_workspace(self, payload):
        if (not isinstance(payload, dict) or set(payload) != {"workspace_id"}
                or not isinstance(payload["workspace_id"], str)):
            raise RequestError("INVALID_OPTIONS", "请选择已保存的源码资料库。")
        with self.lock:
            if self.job and self.job["status"] == "RUNNING":
                raise RequestError("JOB_ACTIVE", "当前工作完成后可以切换资料库。", 409)
            workspace = self.source_workspaces.get(payload["workspace_id"])
            if workspace is None:
                raise RequestError("WORKSPACE_NOT_FOUND", "找不到已保存的源码资料库。", 404)
            if workspace["workspace_id"] == self.project.get("workspace_id"):
                return self._workspace_response()
            candidate = WorkbenchState(analyzer=self.analyzer, config_provider=self.config_provider,
                                       demo_output_root=self.demo_output_root, policy_provider=self.policy_provider)
            candidate._restore_workspace({**workspace,
                "source_preferences": copy.deepcopy(self.source_preferences), "answer_detail": self.answer_detail,
                "answer_instructions": self.answer_instructions})
            return self._adopt_workspace(candidate)

    def _adopt_workspace(self, candidate):
        fields = ("project", "conversation", "conversation_store", "framework_reference_path", "_graph_preview_cache",
                  "_local_workspace", "_saved_workspace", "_startup_upgrade_plan")
        original = {key: getattr(self, key) for key in fields}
        registry = copy.deepcopy(self.source_workspaces)
        truncated = self._history_truncated
        try:
            for key in fields:
                setattr(self, key, getattr(candidate, key))
            self._save_workspace()
        except (OSError, ValueError, sqlite3.Error):
            for key, value in original.items():
                setattr(self, key, value)
            self.source_workspaces, self._history_truncated = registry, truncated
            raise RequestError("WORKSPACE_SWITCH_FAILED", "资料库切换未能保存，原资料库仍保留。", 500) from None
        self.job = None
        return self._workspace_response()

    def open_source_conversation(self, payload):
        if (not isinstance(payload, dict) or set(payload) != {"workspace_id", "conversation_id"}
                or not isinstance(payload["workspace_id"], str) or re.fullmatch(r"[a-f0-9]{64}", payload["workspace_id"]) is None
                or not isinstance(payload["conversation_id"], str) or re.fullmatch(r"[a-f0-9]{32}", payload["conversation_id"]) is None):
            raise RequestError("INVALID_OPTIONS", "请指定对话所属的源码资料库。")
        with self.lock:
            if self.job and self.job["status"] == "RUNNING":
                raise RequestError("JOB_ACTIVE", "当前工作完成后可以切换对话。", 409)
            workspace = self.source_workspaces.get(payload["workspace_id"])
            if workspace is None:
                raise RequestError("CONVERSATION_NOT_FOUND", "找不到这段对话所属的资料库。", 404)
            try:
                self._history_database(workspace)
            except (OSError, ValueError):
                raise RequestError("CONVERSATION_NOT_FOUND", "对话文件暂时不可用，请恢复原分析结果目录。", 404) from None
            candidate = WorkbenchState(analyzer=self.analyzer, config_provider=self.config_provider,
                                       demo_output_root=self.demo_output_root, policy_provider=self.policy_provider)
            candidate._restore_workspace({**workspace, "conversation_id": payload["conversation_id"],
                "source_preferences": copy.deepcopy(self.source_preferences), "answer_detail": self.answer_detail,
                "answer_instructions": self.answer_instructions})
            if candidate.conversation is None or candidate.conversation_store is None:
                raise RequestError("CONVERSATION_NOT_FOUND", "对话文件暂时不可用，请恢复原分析结果目录。", 404)
            return self._adopt_workspace(candidate)

    def delete_source_conversation(self, workspace_id, identifier):
        with self.lock:
            if self.job and self.job["status"] == "RUNNING":
                raise RequestError("JOB_ACTIVE", "当前工作完成后可以删除对话。", 409)
            workspace = self.source_workspaces.get(workspace_id)
            if workspace is None:
                raise RequestError("CONVERSATION_NOT_FOUND", "找不到这段对话。", 404)
            if workspace_id == self.project.get("workspace_id"):
                return self.delete_conversation(identifier)
            try:
                self._history_database(workspace)
                store = ConversationStore(Path(workspace["output"]), Path(workspace["source"]))
                store.delete(identifier)
            except (OSError, ValueError, sqlite3.Error):
                raise RequestError("CONVERSATION_NOT_FOUND", "找不到这段对话。", 404) from None
            return {"conversation": self._conversation_view(),
                    "conversations": self.conversation_store.list() if self.conversation_store else [],
                    "recent_conversations": self.recent_conversations(),
                    "recent_conversations_truncated": self._history_truncated or self._recent_history_truncated}

    def _save_workspace(self, options=None):
        if self._profile_restore_incomplete:
            raise ValueError("WORKSPACE_PROFILE_RESTORE_INCOMPLETE")
        options = options or self.project
        data = {"source": str(options.get("source") or ""), "output": str(options.get("output") or ""),
                "restore_blocked": self.project.get("restore_blocked") is True,
                "conversation_id": (self.conversation or {}).get("id")}
        if not data["source"] and self._retained_startup_workspace:
            # Changing a preference after cancelling restoration must not erase
            # the saved active library or its conversation pointer.
            data = copy.deepcopy(self._retained_startup_workspace)
        remote = _remote_metadata(options.get("_remote_source") or options.get("remote_source"))
        if remote:
            data["remote_source"] = remote
        synthetic = bool(data["source"] and Path(data["source"]).resolve() == FRAMEWORK_DEMO_SOURCE.resolve())
        if synthetic:
            # An explicitly opened example stays in this session. It must not
            # replace the user's real workspace on the next service restart.
            data = copy.deepcopy(self._local_workspace) or {"source": "", "output": "",
                    "restore_blocked": False, "conversation_id": None}
        workspace = self._workspace_record({**data, "framework_reference_path": self.framework_reference_path})
        if workspace:
            previous = self.source_workspaces.get(workspace["workspace_id"], {})
            local = self._local_workspace or {}
            attempt = previous.get("index_upgrade_attempt")
            if local.get("source") == workspace["source"] and local.get("output") == workspace["output"]:
                attempt = local.get("index_upgrade_attempt", attempt)
            if attempt:
                data["index_upgrade_attempt"] = workspace["index_upgrade_attempt"] = attempt
        if not synthetic:
            self._local_workspace = copy.deepcopy(data)
        if (not synthetic and workspace and (workspace["workspace_id"] in self.source_workspaces
                           or self.project.get("snapshot_id") and self.project.get("source") == data["source"]
                           and self.project.get("output") == data["output"])):
            self._remember_workspace(workspace)
        data.update(framework_reference_path=self.framework_reference_path, answer_detail=self.answer_detail,
                    answer_instructions=self.answer_instructions,
                    source_preferences=copy.deepcopy(self.source_preferences),
                    source_workspaces=copy.deepcopy(list(self.source_workspaces.values())),
                    recent_conversations_truncated=self._history_truncated)
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if self.state_path.is_symlink():
            raise ValueError("WORKSPACE_PATH_INVALID")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                    prefix=self.state_path.name + ".", suffix=".tmp", dir=self.state_path.parent) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False)
            temporary.replace(self.state_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _restore_workspace(self, saved_data=None):
        self._restore_report_session = ReportReadSession()
        report_session = self._restore_report_session
        check_cancel = self._restore_check_cancel
        def stage(phase):
            if check_cancel:
                check_cancel()
            if self._restore_progress:
                self._restore_progress(phase)

        self._graph_preview_cache = None
        if saved_data is None and (self.state_path is None or not self.state_path.is_file() or self.state_path.is_symlink()):
            return
        try:
            saved = saved_data if saved_data is not None else json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                return
            entries = saved.get("source_workspaces")
            if isinstance(entries, list):
                for entry in entries[-MAX_SOURCE_WORKSPACES:]:
                    workspace = self._workspace_record(entry)
                    if workspace:
                        self._remember_workspace(workspace)
                self._history_truncated = len(entries) > MAX_SOURCE_WORKSPACES or saved.get("recent_conversations_truncated") is True
            for entry in (saved.get("source_preferences"), saved):
                workspace = self._workspace_record(entry)
                if workspace and self._workspace_has_history(workspace):
                    # A retained preference is only a migration hint. A matching
                    # owned transcript must exist before it joins recent history.
                    self._remember_workspace(workspace)
            reference = saved.get("framework_reference_path")
            self.framework_reference_path = reference if isinstance(reference, str) else None
            self.answer_detail = "brief" if saved.get("answer_detail") == "brief" else "detailed"
            instructions = saved.get("answer_instructions")
            self.answer_instructions = instructions if isinstance(instructions, str) else ""
            self.source_preferences = _source_preferences(saved.get("source_preferences"))
            self._workspace_profile_loaded = True
            if not all(isinstance(saved.get(key), str) and saved[key] for key in ("source", "output")):
                return
            source_input, output_input = Path(saved["source"]), Path(saved["output"])
            if not source_input.is_absolute() or not output_input.is_absolute():
                return
            if source_input.resolve() == FRAMEWORK_DEMO_SOURCE.resolve():
                return
            source, output = _paths(source_input, output_input)
            self._local_workspace = {"source": str(source), "output": str(output), **{key: saved.get(key) for key in
                                     ("restore_blocked", "conversation_id", "remote_source")}}
            # Older profiles recorded the attempt only on the active library.
            # Bind either form to this exact source/output pair before planning.
            attempt = (self._workspace_record(saved) or {}).get("index_upgrade_attempt")
            if not attempt:
                attempt = self.source_workspaces.get(_workspace_key(source, output), {}).get("index_upgrade_attempt")
            if attempt:
                self._local_workspace["index_upgrade_attempt"] = attempt
                saved = {**saved, "index_upgrade_attempt": attempt}
            self._saved_workspace = copy.deepcopy(saved)
            remote = _remote_metadata(saved.get("remote_source"))
            if self.source_preferences is None and remote is None:
                self.source_preferences = _source_preferences({"source": str(source), "output": str(output)})
            # Keep the inputs available even when an explicit index repair is
            # needed. A saved path alone never grants source authority.
            self.project = _project(str(source), str(output))
            if remote:
                self.project.update(source_origin="remote_repository", remote_source=remote)
            self.project["restore_notice"] = "INDEX_RECHECK_REQUIRED"
            from workspace_upgrade import plan_workspace_upgrade
            # Startup only establishes a reproducible candidate. The full
            # database integrity scan runs in the cancellable update worker,
            # after the local server can show progress and retained history.
            stage("verifying_saved_reports")
            self._startup_upgrade_plan = plan_workspace_upgrade(saved, check_integrity=False,
                                                               check_cancel=check_cancel, report_session=report_session)
            # Historical answers belong to the selected source/output pair,
            # independently of whether today's index can authorize a question.
            stage("restoring_conversations")
            self._restore_conversations(source, output, saved.get("conversation_id"))
            if saved.get("restore_blocked") is True or (output / SOURCE_UPDATE_PENDING).exists() or (output / SOURCE_UPDATE_PENDING).is_symlink():
                self.project["restore_blocked"] = True
                return
            database = output / "structural-index.sqlite"
            if not source.is_dir():
                self.project["restore_notice"] = "SOURCE_PATH_UNAVAILABLE"
                return
            if not database.is_file():
                return
            stage("verifying_saved_reports")
            report = _workspace_diagnosis(output / "diagnosis.json", check_cancel=check_cancel,
                                         report_session=report_session)
            if (not isinstance(report, dict) or report.get("source_manifest_verified") is not True
                    or report.get("source_root") != str(source) or not isinstance(report.get("scope"), dict)):
                return
            try:
                programs = _read_json(output / "programs.json", check_cancel=check_cancel, report_session=report_session)
            except (OSError, ValueError):
                programs = None
            from repository_discovery import _fast_snapshot
            stage("loading_saved_index")
            with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("BEGIN")
                metadata = dict(connection.execute("SELECT key,value FROM metadata WHERE key IN "
                                                    "('snapshot_id','source_root_hash','source_options')"))
                if (not metadata.get("snapshot_id") or metadata.get("snapshot_id") == "unknown"
                        or metadata.get("source_root_hash") != hashlib.sha256(str(source).encode()).hexdigest()):
                    return
                _, overview = _fast_snapshot(connection, source)
                database_options = json.loads(metadata["source_options"])
                if not isinstance(database_options, dict):
                    return
                snapshot = overview["snapshot_id"]
                programs_repaired = (not isinstance(programs, dict) or programs.get("snapshot_id") != snapshot
                    or not isinstance(programs.get("programs"), list)
                    or not all(isinstance(item, dict)
                        and all(isinstance(item.get(key), str) and item[key]
                                for key in ("program_name", "relative_path"))
                        and type(item.get("start_line")) is int and item["start_line"] > 0
                        and (item.get("evidence_id") is None or isinstance(item["evidence_id"], str) and item["evidence_id"])
                        for item in programs["programs"]))
                if programs_repaired:
                    # Restore the current evidence IDs, rather than relabeling
                    # a stale list with the database's new snapshot.
                    items = [dict(row) for row in connection.execute(
                        """SELECT s.name AS program_name, s.relative_path, u.start_line, s.evidence_id
                             FROM symbols s JOIN code_units u ON u.unit_id = s.definition_unit_id
                            WHERE s.symbol_type = 'Program'
                            ORDER BY s.name, s.relative_path, u.start_line""")]
                    for item in items:
                        item.update(entry_key=f"{item['relative_path']}::{item['program_name']}::{item['start_line']}",
                                    name_origin="program_id")
                    programs = {"snapshot_id": snapshot, "catalog_snapshot_id": report.get("catalog_snapshot_id"),
                                "scope": report["scope"], "programs": items}
            build_report = report.get("build_report")
            build_report = build_report if isinstance(build_report, dict) else {}
            source_options = report.get("source_options")
            source_options = source_options if isinstance(source_options, dict) else {}
            index_options = {key: database_options[key] for key in
                             ("extensions", "include_extensionless", "encoding", "source_format")}
            scope_snapshot_mismatch = (build_report.get("snapshot_id") != snapshot
                                       or report.get("snapshot_id", snapshot) != snapshot)
            if scope_snapshot_mismatch:
                # Repair display metadata without converting mismatched scope
                # provenance into automatic-upgrade permission on a later boot.
                report["index_upgrade_scope_unverified"] = True
            repaired = (scope_snapshot_mismatch or programs_repaired
                        or any(source_options.get(key) != value for key, value in index_options.items()))
            report.update(build_report={**build_report, "snapshot_id": snapshot}, snapshot_id=snapshot,
                          repository_search=overview, source_options={**source_options, **index_options})
            if not saved.get("source_preferences") and remote is None:
                self.source_preferences = _source_preferences({"source": str(source), "output": str(output),
                    **index_options, "framework_reference_path": self.framework_reference_path})
            if repaired:
                report["report_repaired_from_index"] = True
                report["program_count"] = len(programs["programs"])
            project = _project(str(source), str(output))
            if remote:
                project.update(source_origin="remote_repository", remote_source=remote)
            project.update(diagnosis=report, programs=programs.get("programs", []),
                           snapshot_id=snapshot, catalog_snapshot_id=report.get("catalog_snapshot_id"),
                           relations=self._relationship_preview(database, snapshot))
            if programs.get("display_projection"):
                project["display_projection"] = programs["display_projection"]
            self.project = project
        except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error):
            return

        # Display reports, version history and conversations are independent
        # of the published index. Their failures must not clear the library.
        if repaired:
            from analyze_source import _write
            from report_view import write_report_view
            # A display projection must never replace the complete diagnosis.
            # Indexed questions already repair its snapshot from SQLite.
            if not report.get("display_projection"):
                try:
                    _write(output / "diagnosis.json", report)
                    write_report_view(output / "diagnosis.json", report)
                except (OSError, ValueError):
                    pass
            if programs_repaired:
                try:
                    _write(output / "programs.json", programs)
                    write_report_view(output / "programs.json", programs)
                except (OSError, ValueError):
                    pass
        project["source_versions"], project["source_version"] = [], None
        try:
            project["source_versions"] = versions(output, source)
            project["source_version"] = next((version for version in project["source_versions"]
                                                if version.get("snapshot_id") == snapshot), None)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            pass
        if saved.get("conversation_id"):
            try:
                stage("verifying_saved_reports")
                agent = _read_json(output / "agent-result.json", check_cancel=check_cancel, report_session=report_session)
                if isinstance(agent, dict) and (agent.get("agent_result") is None or isinstance(agent.get("agent_result"), dict)):
                    agent_snapshot = (agent.get("agent_result") or {}).get("snapshot_id")
                    if agent_snapshot is not None and agent_snapshot != snapshot:
                        agent = _unaccepted_agent(agent, "ANSWER_SNAPSHOT_MISMATCH")
                    project["agent"] = agent
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        if self.conversation_store is None:
            self._restore_conversations(source, output, saved.get("conversation_id"), create=True)

    def _restore_conversations(self, source, output, identifier=None, *, create=False):
        """Open only a retained transcript; an absent store stays absent."""
        path = output / "conversations.sqlite"
        if path.is_symlink() or (not create and not path.is_file()):
            return
        try:
            store = ConversationStore(output, source)
            store.recover_interrupted()
            store.list()
            self.conversation_store = store
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            self.conversation_store = None
            return
        if identifier:
            try:
                self.conversation = store.get(identifier)
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                self.conversation = None

    def framework(self):
        try:
            resolved = _resolve_reference(self.framework_reference_path)
        except ValueError:
            resolved = self.framework_reference_path
        return {**framework_status(self.framework_reference_path),
                "configured_path": str(resolved) if resolved else ""}

    def configure_framework(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"path"}:
            raise RequestError("INVALID_OPTIONS", "请提供框架资料文件或目录。")
        path = _text(payload, "path", maximum=4096)
        with self.lock:
            if self.job and self.job["status"] == "RUNNING":
                raise RequestError("JOB_ACTIVE", "本次回答完成后可以更新框架资料。", 409)
            fields = ("framework_reference_path", "_local_workspace", "source_workspaces", "_history_truncated")
            previous = {key: copy.deepcopy(getattr(self, key)) for key in fields}
            self.framework_reference_path = path
            try:
                status = self.framework()
                self._save_workspace()
            except (OSError, ValueError):
                for key, value in previous.items():
                    setattr(self, key, value)
                raise RequestError("REQUEST_FAILED", "框架资料设置未能保存，请重试。", 500) from None
            return {"framework_knowledge": status}

    def configure_answer_instructions(self, payload: object) -> dict:
        if (not isinstance(payload, dict) or set(payload) != {"answer_instructions"}
                or not isinstance(payload["answer_instructions"], str)):
            raise RequestError("INVALID_OPTIONS", "请提供文字形式的 AI 回答要求。")
        with self.lock:
            if self._profile_restore_incomplete:
                raise RequestError("WORKSPACE_RESTORE_FAILED", "原工作区设置尚未读取，请重新打开工作台后再修改。", 409)
            if self.job and self.job["status"] == "RUNNING" and self.job.get("origin") == "workspace_restore":
                raise RequestError("JOB_ACTIVE", "资料库恢复完成后可以更新回答要求。", 409)
            previous = self.answer_instructions
            self.answer_instructions = payload["answer_instructions"]
            try:
                self._save_workspace()
            except (OSError, ValueError):
                self.answer_instructions = previous
                raise RequestError("REQUEST_FAILED", "回答要求未能保存，请重试。", 500) from None
            return {"answer_instructions": self.answer_instructions}

    def check_model(self, payload: object) -> dict:
        if payload != {}:
            raise RequestError("INVALID_OPTIONS", "模型连接测试请求须为空对象。")
        try:
            config = self.config_provider()
            config.validate()
            probe_config = replace(config, timeout_seconds=min(float(config.timeout_seconds), 15.0),
                                   max_output_tokens=min(config.max_output_tokens, 32))
            response = OpenAICompatibleChatClient(
                probe_config, transport=self.model_check_transport, allow_network=True,
            ).complete(messages=[{"role": "user", "content": "Reply with the single word OK."}])
        except (APIConfigurationError, APIClientError) as exc:
            result = {"usable": False, "code": exc.code, "http_status": exc.http_status,
                      "model_returned": False, "diagnostic": _failure_diagnostic(exc, "MODEL_CHECK_FAILED")}
            if isinstance(exc, APIClientError) and exc.transport_reason is not None:
                result["transport_reason"] = exc.transport_reason
            return result
        except (TypeError, ValueError):
            return {"usable": False, "code": "CONFIGURATION_INVALID", "http_status": None,
                    "model_returned": False, "diagnostic": _log_safe_failure(build_diagnostic("CONFIGURATION_INVALID"))}
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            for choice in choices[:1]:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    answer = content.strip()
                elif isinstance(content, list):
                    answer = "".join(
                        part.get("text", "") for part in content
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    ).strip()
                else:
                    answer = ""
                if answer and not message.get("refusal") and choice.get("finish_reason") != "content_filter":
                    return {"usable": True, "code": "MODEL_REPLY_RECEIVED", "http_status": 200,
                            "model_returned": True}
        return {"usable": False, "code": "MODEL_TEXT_EMPTY", "http_status": 200,
                "model_returned": False, "diagnostic": _log_safe_failure(build_diagnostic("MODEL_TEXT_EMPTY", http_status=200))}

    def pick_folder(self, payload: object) -> dict:
        if not isinstance(payload, dict) or set(payload) - {"initial_path"}:
            raise RequestError("INVALID_OPTIONS", "文件夹选择请求只接受起始路径。")
        suggested = _text(payload, "initial_path", maximum=4096)
        with self.lock:
            current_source = self.project.get("source")
        candidate = Path(suggested or current_source or Path.home()).expanduser()
        initial_path = candidate if candidate.is_dir() else candidate.parent
        if not initial_path.is_dir():
            initial_path = Path.home()
        try:
            selected = self.folder_picker(initial_path)
        except RequestError:
            raise
        except Exception:
            raise RequestError("FOLDER_PICKER_UNAVAILABLE", "无法打开本机文件夹选择窗口，请手动填写路径。", 503) from None
        if selected is None:
            return {"cancelled": True, "path": None}
        directory = Path(selected).expanduser()
        if not directory.is_absolute() or not directory.is_dir():
            raise RequestError("FOLDER_PICKER_UNAVAILABLE", "所选文件夹不可用，请重新选择或手动填写路径。", 503)
        return {"cancelled": False, "path": str(directory.resolve())}

    def get_conversation(self, identifier):
        with self.lock:
            if self.job and self.job["status"] == "RUNNING":
                raise RequestError("JOB_ACTIVE", "当前回答完成后可以切换对话。", 409)
            try:
                self.conversation = self.conversation_store.get(identifier)
            except (ValueError, AttributeError):
                raise RequestError("CONVERSATION_NOT_FOUND", "找不到这段对话。", 404) from None
            self._save_workspace()
            return {"conversation": self._conversation_view(), "recent_conversations": self.recent_conversations(),
                    "recent_conversations_truncated": self._history_truncated or self._recent_history_truncated}

    def new_conversation(self, payload):
        if payload != {}:
            raise RequestError("INVALID_OPTIONS", "新对话请求须为空对象。")
        with self.lock:
            if self.job and self.job["status"] == "RUNNING":
                raise RequestError("JOB_ACTIVE", "请先完成或停止当前回答。", 409)
            if self.conversation_store is None:
                raise RequestError("SOURCE_REQUIRED", "先接入源码，再开始对话。", 409)
            self.conversation = self.conversation_store.create(self.project.get("snapshot_id"))
            self._save_workspace()
            return {"conversation": self._conversation_view(), "recent_conversations": self.recent_conversations(),
                    "recent_conversations_truncated": self._history_truncated or self._recent_history_truncated}

    def delete_conversation(self, identifier):
        with self.lock:
            if self.job and self.job["status"] == "RUNNING":
                raise RequestError("JOB_ACTIVE", "当前回答完成后可以删除对话。", 409)
            if self.conversation_store is None:
                raise RequestError("CONVERSATION_NOT_FOUND", "找不到这段对话。", 404)
            try:
                self.conversation_store.delete(identifier)
            except ValueError:
                raise RequestError("CONVERSATION_NOT_FOUND", "找不到这段对话。", 404) from None
            if (self.project.get("conversation") or {}).get("id") == identifier:
                self.project.pop("conversation", None)
                self.project["agent"] = None
            job_conversation = (((self.job or {}).get("result") or {}).get("conversation") or {})
            if job_conversation.get("id") == identifier:
                self.job = None
            if self.conversation and self.conversation["id"] == identifier:
                self.conversation = None
                self.project["agent"] = None
                self._save_workspace()
            return {"conversation": self._conversation_view(),
                    "conversations": self.conversation_store.list(), "recent_conversations": self.recent_conversations(),
                    "recent_conversations_truncated": self._history_truncated or self._recent_history_truncated}

    def _record_answer(self, options, job_id, project):
        identifier = options.get("conversation_id")
        if not identifier:
            return
        agent = (project.get("agent") or {}).get("agent_result") or {}
        project_agent = project.get("agent") or {}
        available = _has_business_answer(agent, runner_completed=project_agent.get("runner_status") == "COMPLETED")
        status = ("partial" if available and (agent.get("status") == "PARTIAL"
            or agent.get("answer_origin") == "local_source_facts" or project_agent.get("runner_status") != "COMPLETED")
            else "completed" if available else "failed")
        answer_language = agent.get("answer_language") or resolve_answer_language(
            options.get("question", ""), locale=options.get("locale"), history=options.get("conversation_history"),
            answer_instructions=options.get("answer_instructions", self.answer_instructions))
        content = agent.get("answer") or localized_text(answer_language,
            "本次没有取得模型回答。对话已保留，可以重试。",
            "No model answer was received. The conversation has been retained; you can retry.",
            "本次沒有取得模型回答。對話已保留，可以重試。")
        projection = (project.get("agent") or {}).get("display_projection") or {}
        answer_omissions = [item for item in projection.get("omitted", [])
                            if isinstance(item, dict) and item.get("path") == "agent_result.answer"]
        complete_characters = max((item.get("total_characters", 0) for item in answer_omissions
                                   if type(item.get("total_characters")) is int), default=len(content))
        details = {"evidence_refs": agent.get("evidence_refs", []),
                     "investigation_run_id": agent.get("run_id"),
                     "analysis_options": {"entry": options.get("entry"),
                         "answer_detail": options.get("answer_detail", "detailed"),
                         "locale": options.get("locale"),
                         **({"history_max_characters": options.get("_conversation_history_max_characters",
                             options["agent_policy"].max_history_characters)} if options.get("agent_policy") else {})},
                     "source_version": project.get("source_version"),
                     "investigation_state": agent.get("investigation_state", {}),
                     "cited_evidence_ids": [r.get("evidence_id") for r in (agent.get("narrative") or {}).get("citations", []) if r.get("evidence_id")],
                     "framework_references": (agent.get("framework_context") or {}).get("references", []),
                     "related_sources": [{key: item[key] for key in ("relative_path", "program_names", "direct_source_match") if key in item}
                         for item in ((agent.get("investigation") or {}).get("business_map") or {}).get("programs", [])],
                     "impact_result": agent.get("impact_result"),
                     **({"repository_inventory":{**agent["repository_inventory"],"rows":[],
                         "next_cursor":0 if agent["repository_inventory"].get("total",0) else None}} if isinstance(agent.get("repository_inventory"),dict) and agent["repository_inventory"].get("paged") else {}),
                     "snapshot_id": agent.get("snapshot_id"), "metrics": agent.get("metrics", {}),
                     "diagnostics": agent.get("diagnostics", []),
                     "diagnostic": sanitize_diagnostic(agent.get("diagnostic")) or sanitize_diagnostic(project_agent.get("diagnostic")),
                     "analysis_status": agent.get("status"), "stop_reason": agent.get("stop_reason"),
                     "answer_completion": agent.get("answer_completion") or (agent.get("business_review") or {}).get("answer_completion"),
                     "material_gap": agent.get("material_gap"),
                     "answer_origin": agent.get("answer_origin"),
                     "answer_available": available,
                     "model_answer_recorded": agent.get("model_answer_recorded"),
                     "finish_reason": agent.get("finish_reason"),
                     "answer_truncated": agent.get("answer_truncated", False),
                     "diagnostic_summary": agent.get("diagnostic_summary") or project_agent.get("diagnostic_summary"),
                     "answer_display_truncated": complete_characters > len(content),
                     "answer_complete_characters": complete_characters,
                     "answer_detail": options.get("answer_detail", "detailed"),
                     "locale": options.get("locale"), "answer_language": answer_language}
        prior = sanitize_diagnostic(project_agent.get("prior_diagnostic"))
        if prior:
            details["prior_diagnostic"] = prior
        previous = next((message for message in (self.conversation or {}).get("messages", ())
            if message.get("id") == options.get("retry_assistant_id") and message.get("role") == "assistant"), None)
        if not available and previous is not None and _has_business_answer(previous):
            # An unsuccessful continuation cannot revoke the previous answer's
            # captured source version. Keep it visible while the request remains
            # unresolved, without calling that new remote attempt successful.
            content, status = previous["content"], "partial"
            for key in ("evidence_refs", "source_version", "investigation_state", "cited_evidence_ids",
                        "investigation_run_id", "analysis_options",
                        "framework_references", "related_sources", "impact_result", "snapshot_id", "metrics",
                        "answer_completion", "material_gap",
                        "answer_origin", "model_answer_recorded", "finish_reason", "answer_truncated",
                        "answer_display_truncated", "answer_complete_characters", "answer_detail", "locale", "answer_language"):
                if key in previous:
                    details[key] = copy.deepcopy(previous[key])
            details.update(analysis_status="PARTIAL", answer_available=True)
            if not details.get("investigation_run_id"):
                details["investigation_run_id"] = options.get("resume_run_id")
            if not details.get("diagnostic"):
                details["diagnostic"] = previous.get("diagnostic")
        if project_agent.get("reason_code") == "REMOTE_REQUEST_OUTCOME_UNKNOWN":
            details["retry_blocked_reason"] = "REMOTE_REQUEST_OUTCOME_UNKNOWN"
        self.conversation_store.complete_user(job_id, status)
        if options.get("retry_message_id"):
            self.conversation_store.replace_assistant(
                identifier, options.get("retry_assistant_id"), content,
                status=status, run_id=job_id, details=details,
                preserve_previous=bool(options.get("continue_message_id") and available
                    and previous is not None and _has_business_answer(previous)),
                previous_run_id=options.get("_continuation_previous_run_id"),
                previous_metadata=options.get("_continuation_previous_metadata"))
        else:
            self.conversation_store.append(identifier, "assistant", content,
                                           status=status, run_id=job_id, details=details)
        self.conversation = self.conversation_store.get(identifier)

    def _record_failure(self, options, job_id, status):
        identifier = options.get("conversation_id")
        if identifier and self.conversation_store:
            try:
                self.conversation_store.complete_user(job_id, status)
                self.conversation = self.conversation_store.get(identifier)
            except (OSError, ValueError, sqlite3.Error) as exc:
                _failure_diagnostic(exc, "ANALYSIS_FAILED")
                self.conversation, self.conversation_store = None, None

    def framework_demo(self) -> dict:
        from framework_demo import load_framework_demo
        try:
            return load_framework_demo()
        except (OSError, ValueError):
            raise RequestError("FRAMEWORK_DEMO_UNAVAILABLE", "框架案例资料不完整或已变化，请更新案例文件后重试。", 409) from None

    def prepare_framework_demo(self, payload: object) -> dict:
        from framework_demo import select_framework_demo_case
        if not isinstance(payload, dict) or "case_id" not in payload or set(payload) - {"case_id", "locale"}:
            raise RequestError("INVALID_OPTIONS", "请选择一个框架案例。")
        locale = payload.get("locale", "zh-CN")
        if not isinstance(locale, str) or locale not in {"zh-CN", "zh-HK", "en"}:
            raise RequestError("INVALID_OPTIONS", "界面语言无效。")
        demo = self.framework_demo()
        try:
            case = select_framework_demo_case(demo, payload["case_id"])
        except (ValueError, TypeError):
            raise RequestError("FRAMEWORK_DEMO_CASE_UNKNOWN", "所选框架案例不存在。", 404) from None
        # Cases share one source snapshot; choosing another question does not
        # rebuild the synthetic repository or discard its conversation.
        output = (self.demo_output_root / "business-conversation").resolve()
        question = case["question"]
        if isinstance(question, dict):
            question = question.get(locale) or question.get("zh-CN")
        with self.lock:
            same_project = (self.project.get("source") == demo["source_path"]
                            and self.project.get("output") == str(output))
            diagnosis = self.project.get("diagnosis") or {}
            search = diagnosis.get("repository_search") or {}
            search_ready = (search.get("snapshot_id") == self.project.get("snapshot_id")
                            and search.get("full_text_complete") is True
                            and (diagnosis.get("scope") or {}).get("mode") in {"repository_index", "repository_question"})
            if self.job and self.job["status"] == "RUNNING":
                if same_project and self.project.get("run_id") == self.job["job_id"]:
                    return {"job_id": self.job["job_id"], "status": "RUNNING",
                            "suggested_question": question}
                raise RequestError("JOB_ACTIVE", "当前工作完成后可以切换业务案例。", 409)
            if same_project and self.project.get("snapshot_id") and diagnosis.get("source_manifest_verified") and search_ready:
                if self.conversation_store is None:
                    self.conversation_store = ConversationStore(output, Path(demo["source_path"]))
                if not self.conversation:
                    self.conversation = self.conversation_store.create(self.project["snapshot_id"])
                    self._save_workspace()
                return {"status": "READY", "project": copy.deepcopy(self.project),
                        "conversation": copy.deepcopy(self.conversation),
                        "suggested_question": question}
        return {**self.start({
            "source": demo["source_path"], "output": str(output),
            "encoding": "utf-8", "source_format": "free", "index_mode": "full",
            "allow_network": False, "max_source_pages": 4, "reading_strategy": "retrieval",
            "answer_detail": self.answer_detail,
        }), "suggested_question": question}

    def state(self) -> dict:
        configuration_error = None
        try:
            config = self.config_provider()
            config.validate()
            configured = True
        except APIConfigurationError as exc:
            configured = False
            configuration_error = exc.code
        except (TypeError, ValueError):
            configured = False
            configuration_error = "CONFIGURATION_INVALID"
        with self.lock:
            return {
                "session_token": self.session_token,
                "submission_protocol": "session-receipt/v1",
                "api_configured": configured,
                "api_configuration_error": configuration_error,
                "framework_knowledge": self.framework(),
                "answer_detail": self.answer_detail,
                "answer_instructions": self.answer_instructions,
                "default_answer_instructions": DEFAULT_ANSWER_INSTRUCTIONS,
                "source_preferences": copy.deepcopy(self.source_preferences),
                "conversation": self._conversation_view(),
                "conversations": self.conversation_store.list() if self.conversation_store else [],
                "recent_conversations": self.recent_conversations(),
                "source_workspaces": self.saved_sources(),
                "recent_conversations_truncated": self._history_truncated or self._recent_history_truncated,
                "active_job_id": self.job["job_id"] if self.job and self.job["status"] == "RUNNING" else None,
                "project": copy.deepcopy(self.project),
                "job": self._job_snapshot() if self.job else None,
            }

    def _source_reading_options(self):
        options = (self.project.get("diagnosis") or {}).get("source_options") or {}
        reading = {key: options[key] for key in ("encoding", "source_format", "compilation_context_path",
                    "max_source_pages") if options.get(key) is not None}
        if options.get("extensions") is not None:
            reading["extensions"] = ",".join(options["extensions"])
        reading.update(answer_detail=self.answer_detail, framework_reference_path=self.framework_reference_path)
        return reading

    def _cached_source_ready(self, workspace):
        candidate = WorkbenchState(analyzer=self.analyzer, config_provider=self.config_provider,
                                   demo_output_root=self.demo_output_root, policy_provider=self.policy_provider)
        candidate._restore_workspace({**workspace, "answer_instructions": self.answer_instructions})
        return bool(candidate.project.get("snapshot_id") and not candidate.project.get("restore_blocked")
                    and (candidate._startup_upgrade_plan or {}).get("status") == "current")

    def start_remote(self, payload: object, *, _update_workspace=None) -> dict:
        reading_fields = {"encoding", "source_format", "extensions", "answer_detail", "max_source_pages",
                          "reading_strategy", "compilation_context_path", "framework_reference_path"}
        fields = {"url", "ref", "subdirectory", "token", "username", "follow_default_branch"} | reading_fields
        if not isinstance(payload, dict) or set(payload) - fields:
            raise RequestError("INVALID_OPTIONS", "请求包含未知设置。")
        if "follow_default_branch" in payload and type(payload["follow_default_branch"]) is not bool:
            raise RequestError("INVALID_OPTIONS", "请选择是否跟随默认分支。")
        try:
            spec = parse_remote_source(payload.get("url"), ref=payload.get("ref"),
                                       subdirectory=payload.get("subdirectory"))
            token = validate_remote_token(payload.get("token"))
            username = validate_remote_username(payload.get("username"))
            if payload.get("follow_default_branch"):
                spec = replace(spec, ref=None)
        except RemoteSourceError as exc:
            raise RequestError(exc.code, exc.message) from None
        with self.lock:
            if self.job and self.job["status"] == "RUNNING":
                raise RequestError("JOB_ACTIVE", "当前分析尚未结束，请等待完成。", 409)
            if _update_workspace is None:
                for cached in reversed(list(self.source_workspaces.values())):
                    remote = cached.get("remote_source")
                    if not remote or not Path(cached["source"]).is_dir():
                        continue
                    previous = parse_remote_source(remote["url"], ref=remote.get("ref"),
                                                   subdirectory=remote.get("subdirectory"))
                    if _remote_identity(previous) == _remote_identity(spec):
                        requested = {key: value for key, value in payload.items() if key in reading_fields}
                        _validate_options({**requested, "source": cached["source"], "output": cached["output"]})
                        opened = self.open_source_workspace({"workspace_id": cached["workspace_id"]})
                        reading = self._source_reading_options()
                        changed = any(key in requested and requested[key] != reading.get(key)
                                      for key in ("encoding", "source_format", "compilation_context_path",
                                                  "framework_reference_path", "max_source_pages", "answer_detail"))
                        if "extensions" in requested:
                            changed = changed or parse_extensions(requested["extensions"]) != parse_extensions(reading.get("extensions"))
                        if changed or not self._cached_source_ready(cached):
                            response = self.start({**reading, **requested, "source": cached["source"],
                                                   "output": cached["output"], "allow_network": False})
                            return {**response, "source_reused": True, "source": cached["source"],
                                    "output": cached["output"], "source_origin": "remote_repository"}
                        # Reopening a usable copy supersedes a previous failed
                        # or cancelled import, without dismissing question jobs.
                        if self.job and self.job.get("kind") == "index" and self.job["status"] in {"FAILED", "CANCELLED"}:
                            self.job = None
                        return {**opened, "source_reused": True}
            # Download only a first/new revision, keeping earlier source and
            # transcript snapshots intact until the new revision is accepted.
            root = self.remote_source_root
            if root.is_symlink():
                raise RequestError("INVALID_PATH", "远程源码缓存位置不能使用符号链接。")
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            workspace = root.resolve() / secrets.token_hex(16)
            workspace.mkdir(mode=0o700)
            source, output = workspace / "source", workspace / "analysis"
            try:
                source.mkdir()
                options = {key: value for key, value in payload.items()
                           if key in reading_fields}
                options.update(source=str(source), output=str(output), allow_network=False,
                               index_mode="catalog", verify_content=True)
                response = self.start(options, _remote_import={"spec": spec, "token": token,
                    "username": username, "workspace": workspace, "update_workspace": _update_workspace})
                return {**response, "source": str(source), "output": str(output),
                        "source_origin": "remote_repository"}
            except Exception:
                shutil.rmtree(workspace)
                raise

    def update_remote(self, payload):
        if (not isinstance(payload, dict) or set(payload) - {"workspace_id", "token", "username", "ref", "follow_default_branch"}
                or not isinstance(payload.get("workspace_id"), str)):
            raise RequestError("INVALID_OPTIONS", "请选择要检查更新的源码资料库。")
        with self.lock:
            workspace = self.source_workspaces.get(payload["workspace_id"])
            if workspace is None or not workspace.get("remote_source"):
                raise RequestError("WORKSPACE_NOT_FOUND", "找不到远程源码资料库。", 404)
            if workspace["workspace_id"] != self.project.get("workspace_id"):
                raise RequestError("WORKSPACE_CHANGED", "请先打开要检查更新的源码资料库。", 409)
            remote = workspace["remote_source"]
            reading = self._source_reading_options()
            return self.start_remote({**reading, "url": remote["url"], "subdirectory": remote["subdirectory"],
                "ref": payload.get("ref") or remote.get("ref"), "token": payload.get("token"),
                "username": payload.get("username"), "follow_default_branch": payload.get("follow_default_branch", False)},
                _update_workspace=copy.deepcopy(workspace))

    def start(self, payload: object, *, _remote_import=None, _index_upgrade=None) -> dict:
        if not isinstance(payload, dict) or "submission_id" not in payload:
            return self._start(payload, _remote_import=_remote_import, _index_upgrade=_index_upgrade)
        payload = dict(payload)
        identifier = payload.pop("submission_id")
        if (not isinstance(identifier, str) or re.fullmatch(r"[a-f0-9]{32}", identifier) is None
                or payload.get("allow_network") is not True or not payload.get("question")
                or _remote_import is not None or _index_upgrade is not None):
            raise RequestError("INVALID_OPTIONS", "提交标识仅用于发送业务问题。")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")).digest()
        with self.lock:
            receipt = self._question_submissions.get(identifier)
            if receipt is not None:
                if receipt["digest"] != digest:
                    raise RequestError("SUBMISSION_CHANGED", "本次提交的内容已改变，请重新打开工作台查看对话。", 409)
                if "rejected" in receipt:
                    raise RequestError(*receipt["rejected"])
                if "job_id" not in receipt:
                    raise RequestError("SUBMISSION_UNCONFIRMED", "无法确认本次提交，请重新打开工作台查看对话。", 409)
                return self._resume_question_submission(identifier, receipt)
            # Receipts remain valid for the authenticated local service session.
            # Reject new submissions at capacity instead of making an old ID reusable.
            if len(self._question_submissions) >= MAX_QUESTION_SUBMISSIONS:
                raise RequestError("SUBMISSION_CAPACITY", "本次问题未受理。请重新打开工作台后提交。", 429)
            receipt = self._question_submissions[identifier] = {"digest": digest}
            try:
                result = self._start(payload)
            except RequestError as error:
                if 400 <= error.status < 500:
                    receipt["rejected"] = (error.code, error.message, error.status)
                raise
            receipt.update(job_id=result["job_id"], store=self.conversation_store,
                           conversation_id=(self.conversation or {}).get("id"))
            return {**result, "submission_id": identifier}

    def _resume_question_submission(self, identifier, receipt):
        job_id = receipt["job_id"]
        response = {"job_id": job_id, "submission_id": identifier}
        if self.job and self.job["job_id"] == job_id:
            response["status"] = self.job["status"]
            if (self.conversation or {}).get("id") == receipt["conversation_id"]:
                response["conversation"] = self._conversation_view()
            return response
        # Another tab may already have replaced the job. Recover its transcript
        # without changing the server's current workspace or starting another job.
        response.update(status="ACCEPTED", job_replaced=True)
        store, conversation_id = receipt["store"], receipt["conversation_id"]
        if store and conversation_id and store.path.is_file() and not any(
                Path(str(store.path) + suffix).is_symlink() for suffix in ("", "-wal", "-shm")):
            try:
                response["conversation"] = store.get(conversation_id)
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                pass
        return response

    def _start(self, payload: object, *, _remote_import=None, _index_upgrade=None) -> dict:
        with self.lock:
            if self._profile_restore_incomplete:
                raise RequestError("WORKSPACE_RESTORE_FAILED", "原工作区设置尚未读取，请重新打开工作台后再接入源码。", 409)
            if self.job and self.job["status"] == "RUNNING":
                raise RequestError("JOB_ACTIVE", "当前分析尚未结束，请等待完成。", 409)
        options = _validate_options(payload)
        if _index_upgrade is not None:
            options["_index_upgrade"] = _index_upgrade
        with self.lock:
            if self.job and self.job["status"] == "RUNNING":
                raise RequestError("JOB_ACTIVE", "当前分析尚未结束，请等待完成。", 409)
            job_id = secrets.token_hex(16)
            same_project = self.project.get("source") == str(options["source"]) and self.project.get("output") == str(options["output"])
            if _remote_import is not None:
                options["_remote_import"] = _remote_import
            elif same_project and self.project.get("remote_source"):
                options["_remote_source"] = copy.deepcopy(self.project["remote_source"])
            if (options["question"] or options.get("continue_message_id")) and ((same_project and self.project.get("restore_blocked") is True)
                                       or (options["output"] / SOURCE_UPDATE_PENDING).exists()):
                raise RequestError("SOURCE_UPDATE_ROLLBACK_FAILED", "源码更新与恢复均未完成，请保留原结果目录并先恢复资料库。", 409)
            source_update = not options["question"] and not options["allow_network"] and not options.get("entry")
            if source_update or not same_project:
                self._graph_preview_cache = None
            if source_update:
                # An explicit import checks all content, including equal-size
                # edits with preserved timestamps, and discovers added/deleted files.
                options["verify_content"] = True
                options["reading_strategy"] = "retrieval"
                options["_source_update"] = {"project": self.project, "conversation": self.conversation,
                    "store": self.conversation_store, "framework_reference_path": self.framework_reference_path,
                    "answer_detail": self.answer_detail, "same_project": same_project,
                    "source_workspaces": copy.deepcopy(self.source_workspaces), "history_truncated": self._history_truncated,
                    "pending_before": (options["output"] / SOURCE_UPDATE_PENDING).exists()}
                preferences = _source_preferences({**options, "source": str(options["source"]),
                    "output": str(options["output"]), "framework_reference_path": options.get("framework_reference_path") or self.framework_reference_path})
                if preferences and _remote_import is None and not options.get("_remote_source"):
                    self.source_preferences = preferences
                    # Keep retry inputs even if this import later fails and
                    # restores an older workspace. They remain form hints.
                    try:
                        self._save_workspace()
                    except (OSError, ValueError) as exc:
                        _failure_diagnostic(exc, "REQUEST_FAILED")
            continue_message_id = options.get("continue_message_id")
            retry_message_id = options.get("retry_message_id") or continue_message_id
            if retry_message_id and (not same_project or not options["allow_network"] or not options.get("conversation_id")):
                raise RequestError("INVALID_RETRY", "只能在当前对话和源码范围内重试原问题。", 409)
            if not source_update:
                self.conversation_store = ConversationStore(options["output"], options["source"])
            self.answer_detail = options["answer_detail"]
            # Freeze the saved requirements for this job; edits apply to later questions.
            options["answer_instructions"] = self.answer_instructions
            options["framework_reference_path"] = options.get("framework_reference_path") or self.framework_reference_path
            self.framework_reference_path = options["framework_reference_path"]
            if (options["question"] or retry_message_id) and options["allow_network"]:
                try:
                    options["agent_policy"] = resolve_agent_policy(self.policy_provider())
                except (ValueError, TypeError, OSError):
                    raise RequestError("AGENT_SETTINGS_INVALID", "分析策略设置无法读取。请检查 agent-settings.json；本次尚未调用模型。", 400) from None
                try:
                    conversation = self.conversation_store.get_metadata(options["conversation_id"]) if options.get("conversation_id") else self.conversation_store.create(self.project.get("snapshot_id") if same_project else None)
                except ValueError:
                    raise RequestError("CONVERSATION_NOT_FOUND", "找不到这段对话，请新建对话后重试。", 404) from None
                options["conversation_id"] = conversation["id"]
                if not retry_message_id and same_project and options["question"]:
                    retry_message_id = self.conversation_store.retry_for_question(conversation["id"], options["question"])
                    if retry_message_id:
                        options["retry_message_id"] = retry_message_id
                if retry_message_id:
                    try:
                        target = self.conversation_store.retry_target(conversation["id"], retry_message_id)
                    except ValueError:
                        raise RequestError("RETRY_NOT_AVAILABLE", "这条问题已无法重试，请在对话末尾继续提问。", 409) from None
                    if options["question"] and options["question"] != target["question"]:
                        raise RequestError("INVALID_RETRY", "重试只能使用原来的问题。", 409)
                    options["question"] = target["question"]
                    options["retry_assistant_id"] = target["assistant_message_id"]
                    options["retry_message_id"] = retry_message_id
                    if continue_message_id:
                        saved_conversation = self.conversation_store.get(conversation["id"])
                        previous = next((message for message in saved_conversation["messages"]
                            if message["id"] == target["assistant_message_id"]), None)
                        if previous is not None and not previous.get("investigation_run_id"):
                            previous.update(self._recover_legacy_continuation(saved_conversation, previous))
                        investigation_run_id = (previous or {}).get("investigation_run_id")
                        if (not isinstance(investigation_run_id, str)
                                or re.fullmatch(r"[a-f0-9]{24}", investigation_run_id) is None
                                or previous.get("retry_blocked_reason") == "REMOTE_REQUEST_OUTCOME_UNKNOWN"):
                            raise RequestError("CONTINUATION_NOT_AVAILABLE", "原调查记录无法继续，请使用“重试这个问题”重新分析。", 409)
                        original = previous.get("analysis_options") or {}
                        options["entry"] = original.get("entry")
                        options["answer_detail"] = original.get("answer_detail", previous.get("answer_detail", "detailed"))
                        original_locale = original.get("locale", previous.get("locale"))
                        if original_locale is None:
                            options.pop("locale", None)
                        else:
                            options["locale"] = original_locale
                        self.answer_detail = options["answer_detail"]
                        if type(original.get("history_max_characters")) is int:
                            options["_conversation_history_max_characters"] = original["history_max_characters"]
                        options.update(continue_investigation=True, resume_run_id=investigation_run_id)
                        options["_continuation_previous_run_id"] = previous.get("run_id")
                        options["_continuation_previous_metadata"] = {
                            key: copy.deepcopy(previous[key]) for key in ("investigation_run_id", "analysis_options")
                            if key in previous}
                    else:
                        options["start_new_investigation"] = True
                    options["conversation_history"] = self.conversation_store.model_history(
                        conversation["id"], before_position=target["position"],
                        max_characters=options.get("_conversation_history_max_characters", options["agent_policy"].max_history_characters),
                        question=options["question"], include_legacy=True, include_analysis_handoff=True)
                    try:
                        self.conversation_store.claim_retry(conversation["id"], retry_message_id, job_id)
                    except ValueError:
                        raise RequestError("RETRY_NOT_AVAILABLE", "这条问题已在处理或无法重试。", 409) from None
                else:
                    options["conversation_history"] = self.conversation_store.model_history(
                        conversation["id"], max_characters=options["agent_policy"].max_history_characters,
                        question=options["question"], include_legacy=True, include_analysis_handoff=True)
                    self.conversation_store.append(conversation["id"], "user", options["question"], status="pending", run_id=job_id)
                self.conversation = self.conversation_store.get(conversation["id"])
            elif not same_project and not source_update:
                self.conversation = None
            if not source_update and (not same_project or not options["question"]):
                self.project = _project(str(options["source"]), str(options["output"]), job_id)
            if not source_update:
                self._save_workspace(options)
            self.cancel_event = threading.Event()
            self.started_at = self.updated_at = self.phase_started_at = self.progress_updated_at = time.monotonic()
            self.phase_start_completed = 0.0
            self.job = {"job_id": job_id, "kind": "question" if options["question"] else "index",
                        "source": str(options["source"]), "output": str(options["output"]),
                        "status": "RUNNING", "result": None, "error": None,
                        "cancel_requested": False, "progress": {"phase": "preparing", "completed": 0,
                        "total": None, "unit": "files", "current_file": None,
                        "bytes_completed": 0, "bytes_total": None}}
            if _remote_import is not None or options.get("_remote_source"):
                self.job["source_origin"] = "remote_repository"
            self._notify_job_changed()
            threading.Thread(target=self._run, args=(job_id, options), daemon=True).start()
            return {"job_id": job_id, "status": "RUNNING", "source_preferences": copy.deepcopy(self.source_preferences)}

    def _job_snapshot(self) -> dict:
        result = copy.deepcopy(self.job)
        if result["status"] != "RUNNING":
            result["recent_conversations"] = self.recent_conversations()
            result["recent_conversations_truncated"] = self._history_truncated or self._recent_history_truncated
            result["source_workspaces"] = self.saved_sources()
        result["progress_revision"] = self.job.get("progress_revision", 0)
        now = time.monotonic()
        progress = result["progress"]
        progress["elapsed_seconds"] = round((now if result["status"] == "RUNNING" else self.updated_at) - self.started_at, 2)
        progress["last_update_seconds"] = round(max(0.0, now - self.updated_at), 2)
        measured_at = now if result["status"] == "RUNNING" else self.updated_at
        progress["phase_elapsed_seconds"] = round(max(0.0, measured_at - self.phase_started_at), 2)
        if "question_remaining_seconds" in progress:
            progress["question_remaining_seconds"] = round(max(0.0,
                progress["question_remaining_seconds"] - max(0.0, measured_at - self.progress_updated_at)), 2)
        phase_elapsed = max(0.0, self.updated_at - self.phase_started_at)
        completed, total = progress.get("completed", 0), progress.get("total")
        delta = completed - self.phase_start_completed
        progress["eta_seconds"] = (round(max(0.0, (total - completed) * phase_elapsed / delta), 1)
            if total and delta > 0 and phase_elapsed >= 0.25 and result["status"] == "RUNNING" else None)
        progress["eta_scope"] = "current_phase"
        file_completed, file_total = progress.get("file_completed", 0), progress.get("file_total")
        if file_total and file_completed > 0 and phase_elapsed >= 0.25 and result["status"] == "RUNNING":
            progress["eta_seconds"] = round(max(0.0, (file_total - file_completed) * phase_elapsed / file_completed), 1)
            progress["eta_scope"] = "current_file"
        return result

    def _notify_job_changed(self) -> None:
        """Publish a new snapshot under the same lock used by waiting readers."""
        if self.job is not None:
            self.job["progress_revision"] = self.job.get("progress_revision", 0) + 1
        self.progress_changed.notify_all()

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise AnalysisCancelled("Cancelled by the user.")

    def _progress(self, job_id: str, event: dict) -> None:
        self._check_cancel()
        with self.lock:
            if not self.job or self.job["job_id"] != job_id:
                raise AnalysisCancelled("The current job was replaced.")
            if self.job["status"] != "RUNNING":
                raise AnalysisCancelled("The current job has already finished.")
            previous = self.job["progress"]
            if (event.get("phase") != previous.get("phase") or event.get("total") != previous.get("total")
                    or (event.get("phase") == "waiting_for_model"
                        and event.get("model_requests") != previous.get("model_requests"))):
                self.phase_started_at = time.monotonic()
                self.phase_start_completed = event.get("completed", 0)
            # Stage-specific counters are replaced, never inherited from a prior
            # stage with a different denominator or measurement unit.
            self.job["progress"] = {"phase": event.get("phase", "working"), "completed": event.get("completed", 0),
                "total": event.get("total"), "unit": event.get("unit", "files"), "current_file": event.get("current_file"),
                "bytes_completed": event.get("bytes_completed", 0), "bytes_total": event.get("bytes_total"),
                **{key: value for key, value in event.items() if key in {"file_completed", "file_total", "file_unit"}}}
            # Only numeric, bounded activity metadata reaches the workbench.
            # Source bodies, model parameters and arbitrary event details do not.
            for key in ("question_budget_seconds", "question_remaining_seconds"):
                value = event.get(key)
                if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1800:
                    self.job["progress"][key] = value
            for key in ("model_requests", "retrieved_pages"):
                value = event.get(key)
                if type(value) is int and 0 <= value <= 1_000_000:
                    self.job["progress"][key] = value
            current = self.job["progress"]
            # Summary and preview snapshots belong to the job rather than its
            # current stage. A phase-only event must not erase useful updates.
            for key, validate in (("analysis_update", _analysis_update), ("answer_preview", _answer_preview)):
                snapshot = validate(event.get(key))
                if snapshot is not None:
                    current[key] = snapshot
                elif key in previous:
                    current[key] = previous[key]
            snapshot = _analysis_state(event.get("analysis_state"))
            prior_state = previous.get("analysis_state")
            if snapshot is not None and (prior_state is None or snapshot["revision"] >= prior_state["revision"]):
                current["analysis_state"] = snapshot
            elif prior_state is not None:
                current["analysis_state"] = prior_state
            if "question_budget_seconds" in current and "question_remaining_seconds" in current:
                current["question_remaining_seconds"] = min(current["question_remaining_seconds"],
                    current["question_budget_seconds"])
            self.updated_at = self.progress_updated_at = time.monotonic()
            self._notify_job_changed()

    def cancel(self, job_id: str) -> dict:
        with self.lock:
            if not self.job or self.job["job_id"] != job_id:
                raise RequestError("JOB_NOT_FOUND", "任务不存在或已被替换。", 404)
            if self.job["status"] == "RUNNING":
                self.job["cancel_requested"] = True
                self.cancel_event.set()
                self._notify_job_changed()
            return self._job_snapshot()

    def _run(self, job_id: str, options: dict) -> None:
        backup = None
        remote_import = options.pop("_remote_import", None)
        remote_workspace = remote_import["workspace"] if remote_import else None
        remote_retained = False
        remote_update = None

        def discard_remote_workspace():
            nonlocal remote_workspace
            if remote_workspace is not None and not remote_retained:
                try:
                    shutil.rmtree(remote_workspace)
                    remote_workspace = None
                except OSError as exc:
                    _failure_diagnostic(exc, "ANALYSIS_FAILED")

        try:
            source, output = options["source"], options["output"]
            if remote_import is not None:
                spec, token, username = (remote_import[key] for key in ("spec", "token", "username"))
                previous_workspace = remote_import.get("update_workspace")
                remote_import = None
                self._check_cancel()
                source.rmdir()
                try:
                    resolved = None
                    rebuild_cached = False
                    if previous_workspace:
                        resolved = resolve_remote_source(spec, token=token, username=username,
                            progress=lambda event: self._progress(job_id, event), check_cancel=self._check_cancel)
                        old_remote = previous_workspace["remote_source"]
                        remote_update = {"previous_commit": old_remote["commit"], "commit": resolved["commit"],
                                         "status": "updated"}
                        cached_source_intact = True
                        if (resolved["commit"] == old_remote["commit"]
                                and Path(previous_workspace["source"]).is_dir()):
                            cached_source_intact = cached_snapshot_matches(spec, Path(previous_workspace["source"]),
                                old_remote, output=Path(previous_workspace["output"]), check_cancel=self._check_cancel,
                                progress=lambda event: self._progress(job_id, event))
                            if not cached_source_intact:
                                remote_update.update(status="repaired", source_repaired=True)
                        if (resolved["commit"] == old_remote["commit"]
                                and Path(previous_workspace["source"]).is_dir() and cached_source_intact):
                            metadata = _remote_metadata({**old_remote, **resolved})
                            if metadata is None:
                                raise ValueError("invalid remote source provenance")
                            remote_update["status"] = "up_to_date"
                            if self._cached_source_ready(previous_workspace):
                                with self.lock:
                                    self._check_cancel()
                                    self.project = copy.deepcopy(self.project)
                                    self.project["remote_source"] = metadata
                                    self.project["remote_update"] = remote_update
                                    self._save_workspace()
                                    result = copy.deepcopy(self.project)
                                    result["conversation"] = self._conversation_view()
                                    result["conversations"] = self.conversation_store.list() if self.conversation_store else []
                                    self.updated_at = time.monotonic()
                                    self.job.update(status="COMPLETED", result=result,
                                        source=previous_workspace["source"], output=previous_workspace["output"])
                                return
                            # The revision is unchanged but local index artifacts
                            # need repair. Rebuild from cached files, without ZIP.
                            discard_remote_workspace()
                            source, output = Path(previous_workspace["source"]), Path(previous_workspace["output"])
                            options.update(source=source, output=output)
                            options["_source_update"].update(same_project=True,
                                pending_before=(output / SOURCE_UPDATE_PENDING).exists())
                            remote_update["index_rebuilt"] = True
                            with self.lock:
                                self.job.update(source=str(source), output=str(output))
                            rebuild_cached = True
                    if not rebuild_cached:
                        arguments = {"token": token, "progress": lambda event: self._progress(job_id, event),
                                     "check_cancel": self._check_cancel}
                        if username:
                            arguments["username"] = username
                        if resolved is not None:
                            arguments["resolved"] = resolved
                        if (previous_workspace and spec.provider == "bitbucket_cloud"
                                and Path(previous_workspace["source"]).is_dir()):
                            arguments.update(previous_source=Path(previous_workspace["source"]),
                                previous_metadata=previous_workspace["remote_source"],
                                previous_output=Path(previous_workspace["output"]))
                        metadata = fetch_remote_source(spec, source, **arguments)
                finally:
                    token = username = None
                options["_remote_source"] = _remote_metadata(metadata)
                if options["_remote_source"] is None:
                    raise ValueError("invalid remote source provenance")
                self._check_cancel()
            upgrade = options.pop("_index_upgrade", None)
            if upgrade is not None:
                from workspace_upgrade import plan_workspace_upgrade
                self._progress(job_id, {"phase": "preparing_index", "completed": 0, "total": None})
                checked = plan_workspace_upgrade(upgrade["saved"], check_cancel=self._check_cancel,
                                                 report_session=upgrade.get("report_session"))
                if checked.get("status") != "ready" or checked.get("attempt_key") != upgrade["attempt_key"]:
                    raise ValueError("SOURCE_INDEX_CHANGED_DURING_UPGRADE_CHECK")
                self._check_cancel()
            if options.get("_source_update"):
                output.mkdir(parents=True, exist_ok=True)
                backup_names = (*ARTIFACT_NAMES, "source-versions.sqlite")
                # Existing libraries need rollback even when their index is
                # missing. A first import has no prior artifacts to restore;
                # keep its completed index checkpoints if later work stops.
                if options["_source_update"]["same_project"] or any(
                        (output / name).exists() or (output / name).is_symlink() for name in backup_names):
                    backup = SourceUpdateBackup(output, backup_names,
                        progress=lambda event: self._progress(job_id, event), check_cancel=self._check_cancel,
                        rebuildable_databases=("structural-index.sqlite", "source-catalog.sqlite"))
                    options["_source_update"]["artifacts"] = backup
            if options.get("_source_update"):
                pending = output / SOURCE_UPDATE_PENDING
                if pending.is_symlink() or (pending.exists() and not pending.is_file()):
                    raise ValueError("invalid source update marker")
                if not pending.exists():
                    # The output itself records an unfinished update before
                    # any published artifacts can change. It remains useful
                    # when the separate workspace settings cannot be written.
                    with pending.open("x", encoding="utf-8") as stream:
                        json.dump({"job_id": job_id}, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                if backup:
                    backup.prepare_rebuild()
            arguments = {key: value for key, value in options.items()
                         if key not in {"source", "output", "retry_message_id", "continue_message_id", "retry_assistant_id", "_continuation_previous_run_id", "_continuation_previous_metadata", "_conversation_history_max_characters", "_source_update", "_remote_source"}}
            if arguments.get("framework_reference_path") is None:
                arguments.pop("framework_reference_path", None)
            if options["allow_network"] and options["question"]:
                try:
                    arguments["config"] = self.config_provider()
                except APIConfigurationError:
                    # The core workflow records a safe configuration reason.
                    pass
            arguments["progress"] = lambda event: self._progress(job_id, event)
            arguments["check_cancel"] = self._check_cancel
            report = self.analyzer(source, output, **arguments)
            self._check_cancel()
            project = _project(str(source), str(output), job_id)
            if options.get("_remote_source"):
                project.update(source_origin="remote_repository", remote_source=copy.deepcopy(options["_remote_source"]))
            if remote_update:
                project["remote_update"] = remote_update
            project["diagnosis"] = report
            project["catalog_snapshot_id"] = report.get("catalog_snapshot_id")
            program_report = _read_json(output / "programs.json")
            if program_report.get("display_projection"):
                project["display_projection"] = program_report["display_projection"]
            agent = _read_json(output / "agent-result.json")
            snapshot = (report.get("build_report") or {}).get("snapshot_id")
            # A failed refresh can deliberately preserve an older SQLite file.
            # It must never authorize that old file as this browser's snapshot.
            if report.get("source_manifest_verified") is True and snapshot:
                try:
                    if program_report.get("snapshot_id") != snapshot:
                        raise ValueError("snapshot report mismatch")
                    relations = self._relationship_preview(output / "structural-index.sqlite", snapshot)
                except (OSError, ValueError, sqlite3.Error):
                    project["diagnosis"] = {**report, "runner_status": "BLOCKED", "question_status": "NOT_READY",
                                            "source_manifest_verified": False, "catalog_ready": False,
                                            "reason_code": "SOURCE_SNAPSHOT_MISMATCH",
                                            "diagnostic": build_diagnostic("SOURCE_SNAPSHOT_MISMATCH")}
                    project["agent"] = _unaccepted_agent(agent, "SOURCE_SNAPSHOT_MISMATCH")
                else:
                    agent_snapshot = (agent.get("agent_result") or {}).get("snapshot_id")
                    if agent_snapshot is not None and agent_snapshot != snapshot:
                        agent = _unaccepted_agent(agent, "ANSWER_SNAPSHOT_MISMATCH")
                        project["diagnosis"] = {**report, "question_status": "NOT_READY"}
                    project.update(programs=program_report["programs"], agent=agent,
                                   snapshot_id=snapshot, relations=relations)
                    previous = self.project
                    if (previous.get("source") == str(source) and previous.get("output") == str(output)
                            and previous.get("snapshot_id") == snapshot and previous.get("source_version")
                            and not options.get("_source_update")):
                        project["source_version"] = copy.deepcopy(previous["source_version"])
                        project["source_versions"] = copy.deepcopy(previous.get("source_versions", []))
                    else:
                        self._progress(job_id, {"phase": "source_version", "completed": 0, "total": 1,
                                               "unit": "versions"})
                        project["source_version"] = record_version(output, source, snapshot)
                        project["source_versions"] = versions(output, source)
                        self._progress(job_id, {"phase": "source_version", "completed": 1, "total": 1,
                                               "unit": "versions"})
            elif report.get("catalog_ready") is True:
                # A usable catalog permits another question but does not verify
                # any model answer or citations from an unverified source scope.
                if agent.get("agent_result"):
                    agent = _unaccepted_agent(agent, "SOURCE_SNAPSHOT_UNVERIFIED")
                    project["diagnosis"] = {**report, "question_status": "NOT_READY"}
                project.update(programs=program_report["programs"], agent=agent)
            else:
                project["agent"] = _unaccepted_agent(agent, report.get("reason_code") or "SOURCE_SNAPSHOT_UNVERIFIED")
            diagnosis_path = output / "diagnosis.json"
            browser_diagnosis = (project_report(project["diagnosis"])
                if diagnosis_path.is_file() and diagnosis_path.stat().st_size > DIRECT_REPORT_BYTES else None)
            with self.lock:
                if self.job and self.job["job_id"] == job_id:
                    self._check_cancel()
                    update = options.get("_source_update")
                    if update and not project.get("snapshot_id"):
                        failed_report = project.get("diagnosis") or {}
                        diagnostic = sanitize_diagnostic(failed_report.get("diagnostic")) or build_diagnostic("ANALYSIS_FAILED")
                        rollback_error = self._restore_after_failure(options)
                        remote_retained = bool(rollback_error)
                        discard_remote_workspace()
                        self.updated_at = time.monotonic()
                        self.job.update(status="FAILED", result=None, conversation=copy.deepcopy(self.conversation),
                            error=rollback_error or {"code": "SOURCE_UPDATE_FAILED", "message": format_diagnostic(diagnostic),
                                                     "diagnostic": _log_safe_failure(diagnostic)})
                        return
                    if update:
                        self.conversation_store = ConversationStore(output, source)
                        self.conversation = (self.conversation_store.get(update["conversation"]["id"])
                            if update["same_project"] and update["conversation"] else None)
                        project["conversations"] = self.conversation_store.list()
                    self.project = project
                    self._record_answer(options, job_id, project)
                    # Full reports authorize the result above; browser state
                    # retains only the bounded presentation of large reports.
                    if browser_diagnosis is not None:
                        project["diagnosis"] = browser_diagnosis
                    project["conversation"] = self._conversation_view()
                    self._save_workspace(options)
                    if update:
                        (output / SOURCE_UPDATE_PENDING).unlink()
                    self.updated_at = time.monotonic()
                    project["recent_conversations"] = self.recent_conversations()
                    project["recent_conversations_truncated"] = self._history_truncated or self._recent_history_truncated
                    self.job["progress"].pop("answer_preview", None)
                    self.job.update(status="COMPLETED", result=copy.deepcopy(project))
                    remote_retained = True
        except AnalysisCancelled:
            with self.lock:
                if self.job and self.job["job_id"] == job_id:
                    self.updated_at = time.monotonic()
                    rollback_error = self._restore_after_failure(options)
                    remote_retained = bool(rollback_error)
                    discard_remote_workspace()
                    self._record_failure(options, job_id, "failed" if rollback_error else "cancelled")
                    self.job.update(status="FAILED" if rollback_error else "CANCELLED", result=None,
                                    error=rollback_error, conversation=copy.deepcopy(self.conversation))
        except Exception as exc:
            # Never serialize raw adapter errors, environment values or remote
            # responses. Failed work leaves no current evidence authority.
            with self.lock:
                if self.job and self.job["job_id"] == job_id:
                    self.updated_at = time.monotonic()
                    rollback_error = self._restore_after_failure(options)
                    remote_retained = bool(rollback_error)
                    discard_remote_workspace()
                    self._record_failure(options, job_id, "failed")
                    self.job["conversation"] = copy.deepcopy(self.conversation)
                    if isinstance(exc, RemoteSourceError):
                        error = {"code": exc.code, "message": exc.message}
                    else:
                        diagnostic = _failure_diagnostic(exc, "ANALYSIS_FAILED")
                        error = {"code": "ANALYSIS_FAILED", "message": format_diagnostic(diagnostic), "diagnostic": diagnostic}
                    self.job.update(status="FAILED", result=None, error=rollback_error or error)
        finally:
            # Retention belongs to this run, even if a new job has already
            # switched the active project. Failed rollback retains recovery data.
            discard_remote_workspace()
            with self.lock:
                if self.job and self.job["job_id"] == job_id:
                    self._notify_job_changed()
            if backup:
                try:
                    backup.close()
                except OSError as exc:
                    _failure_diagnostic(exc, "ANALYSIS_FAILED")

    def _restore_after_failure(self, options):
        self._graph_preview_cache = None
        try:
            self._restore_source_update(options)
        except Exception as exc:
            # A rollback failure must end the job, retain its backup and revoke
            # authority for artifacts which may only be partly restored.
            backup = (options.get("_source_update") or {}).get("artifacts")
            recovery_directory = backup.preserve() if backup else None
            self.project = _project(str(options["source"]), str(options["output"]))
            self.project["restore_blocked"] = True
            update = options.get("_source_update") or {}
            if update.get("same_project"):
                # Transcript storage is not an index artifact and is not
                # replaced by the update/rollback. Keep this source's history.
                self.conversation, self.conversation_store = update.get("conversation"), update.get("store")
            else:
                self.conversation, self.conversation_store = None, None
            diagnostic = _failure_diagnostic(exc, "ANALYSIS_FAILED")
            try:
                self._save_workspace()
            except (OSError, ValueError):
                pass
            error = {"code": "SOURCE_UPDATE_ROLLBACK_FAILED", "message": format_diagnostic(diagnostic),
                     "diagnostic": diagnostic}
            if recovery_directory:
                error["recovery_directory"] = recovery_directory
            return error
        return None

    def _restore_source_update(self, options):
        update = options.get("_source_update")
        if update:
            if update.get("artifacts"):
                update["artifacts"].restore()
            self.project, self.conversation = update["project"], update["conversation"]
            self.conversation_store = update["store"]
            self.source_workspaces = copy.deepcopy(update.get("source_workspaces", self.source_workspaces))
            self._history_truncated = update.get("history_truncated", self._history_truncated)
            self.framework_reference_path, self.answer_detail = update["framework_reference_path"], update["answer_detail"]
            self._save_workspace()
            if not update.get("pending_before"):
                (options["output"] / SOURCE_UPDATE_PENDING).unlink(missing_ok=True)

    def get_job(self, job_id: str) -> dict:
        with self.lock:
            if self._startup_restore_archive and self._startup_restore_archive["job_id"] == job_id:
                return copy.deepcopy(self._startup_restore_archive)
            if not self.job or self.job["job_id"] != job_id:
                raise RequestError("JOB_NOT_FOUND", "任务不存在或已由新的源码分析替换。", 404)
            return self._job_snapshot()

    def wait_job(self, job_id: str, *, after: int, wait_ms: int) -> dict:
        """Wait for a changed bounded snapshot, without holding up the worker."""
        if (type(after) is not int or not 0 <= after <= 9_007_199_254_740_991
                or type(wait_ms) is not int or not 0 <= wait_ms <= MAX_PROGRESS_WAIT_MS):
            raise RequestError("INVALID_QUERY", "进度查询参数无效。")
        with self.progress_changed:
            if not self.job or self.job["job_id"] != job_id:
                raise RequestError("JOB_NOT_FOUND", "任务不存在或已由新的源码分析替换。", 404)
            if self.progress_waiters >= MAX_PROGRESS_WAITERS:
                raise RequestError("PROGRESS_BUSY", "进度连接繁忙，请稍后重试。", 429)
            self.progress_waiters += 1
            try:
                self.progress_changed.wait_for(
                    lambda: not self.job or self.job["job_id"] != job_id
                    or self.job["status"] != "RUNNING"
                    or self.job.get("progress_revision", 0) != after,
                    timeout=wait_ms / 1000,
                )
                return self.get_job(job_id)
            finally:
                self.progress_waiters -= 1

    def evidence(self, evidence_id: str, conversation_id=None, message_id=None, revision_id=None) -> dict:
        if EVIDENCE_ID.fullmatch(evidence_id) is None:
            raise RequestError("INVALID_EVIDENCE_ID", "证据标识无效。")
        if revision_id and (not conversation_id or not message_id):
            raise RequestError("EVIDENCE_NOT_FOUND", "引用不属于这条对话消息。", 404)
        with self.lock:
            archived = None
            if conversation_id:
                try:
                    if revision_id:
                        message = self.conversation_store.get_revision(conversation_id, message_id, revision_id)
                    else:
                        conversation = self.conversation_store.get(conversation_id)
                        message = next(m for m in conversation["messages"] if m["id"] == message_id)
                    if evidence_id not in {r.get("evidence_id") for r in message.get("evidence_refs", [])}:
                        raise ValueError("missing reference")
                    from source_session import read_archived_evidence
                    archived = read_archived_evidence(Path(self.project["output"]) / "structural-index.sqlite", evidence_id)
                    if archived:
                        return {"snapshot_id": message.get("snapshot_id"), "status": "OK",
                            "spans": [{**archived, "integrity": "VALID", "content_type": "UNTRUSTED_SOURCE_TEXT",
                                       "span_truncated": bool(archived.get("span_truncated"))}], "span_count": 1,
                            "missing_evidence_ids": [], "returned_characters": len(archived["source_text"]),
                            "truncated": bool(archived.get("span_truncated")), "diagnostics": [], "run_id": message.get("run_id")}
                    if message.get("snapshot_id") != self.project.get("snapshot_id"):
                        raise RequestError("HISTORICAL_SOURCE_VERSION", "这条引用属于较早的源码版本；请查看当时的路径和行号，或在当前版本继续提问。", 409)
                except (ValueError, StopIteration, AttributeError):
                    raise RequestError("EVIDENCE_NOT_FOUND", "引用不属于这条对话消息。", 404) from None
            if not self.project["snapshot_id"] or (self.job and self.job["status"] == "RUNNING" and not self.conversation):
                raise RequestError("NO_CURRENT_INDEX", "当前没有完成并验证的源码索引。", 409)
            # The initial analysis result can be inspected before a caller has
            # selected a conversation message. Semantic excerpts live in the
            # versioned store, so authorize them against that current result.
            if not conversation_id:
                current = (self.project.get("agent") or {}).get("agent_result") or {}
                if evidence_id in {ref.get("evidence_id") for ref in current.get("evidence_refs", [])}:
                    from source_session import read_archived_evidence
                    archived = read_archived_evidence(
                        Path(self.project["output"]) / "structural-index.sqlite", evidence_id)
                    if archived:
                        return {"snapshot_id": current.get("snapshot_id"), "status": "OK",
                            "spans": [{**archived, "integrity": "VALID", "content_type": "UNTRUSTED_SOURCE_TEXT",
                                       "span_truncated": bool(archived.get("span_truncated"))}], "span_count": 1,
                            "missing_evidence_ids": [], "returned_characters": len(archived["source_text"]),
                            "truncated": bool(archived.get("span_truncated")), "diagnostics": [], "run_id": self.project["run_id"]}
            database = Path(self.project["output"]) / "structural-index.sqlite"
            if database.is_symlink():
                raise RequestError("INDEX_CHANGED", "索引位置已发生变化，请重新分析。", 409)
            try:
                result = InvestigationTools(database).read_evidence([evidence_id])
            except (OSError, ValueError, sqlite3.Error):
                raise RequestError("INDEX_UNAVAILABLE", "当前源码索引不可用，请重新分析。", 409) from None
            if result["snapshot_id"] != self.project["snapshot_id"]:
                raise RequestError("INDEX_CHANGED", "源码索引已更新，请重新分析。", 409)
            if not result["spans"]:
                raise RequestError("EVIDENCE_NOT_FOUND", "证据不属于当前源码索引。", 404)
            if result["status"] == "INTEGRITY_ERROR":
                raise RequestError("EVIDENCE_INTEGRITY_ERROR", "证据完整性检查失败，请重新分析。", 409)
            return {**result, "run_id": message.get("run_id") if revision_id else self.project["run_id"]}

    def impact(self, handle, conversation_id, message_id, *, cursor=0, export=False):
        from impact_results import impact_page, impact_jsonl
        with self.lock:
            try:
                conversation = self.conversation_store.get(conversation_id)
                message = next(item for item in conversation["messages"] if item["id"] == message_id)
                if (message.get("impact_result") or {}).get("handle") != handle:
                    raise ValueError("handle")
                database = Path(self.project["output"]) / "structural-index.sqlite"
                if export:
                    return {"handle": handle, "jsonl": impact_jsonl(database, handle)}
                return impact_page(database, handle, cursor=cursor)
            except (ValueError, TypeError, StopIteration, OSError, sqlite3.Error):
                raise RequestError("IMPACT_NOT_FOUND", "相关对象清单不可用。", 404) from None


    def repository_inventory(self, handle, conversation_id, message_id, *, cursor=0, page_size=50,
                             export=False, action=None, candidate_id=None, index=0, revision_id=None):
        from repository_inventory_store import InventoryStore
        with self.lock:
            try:
                if self.conversation_store is None:
                    raise ValueError("workspace")
                conversation=self.conversation_store.get(conversation_id)
                message=next(item for item in conversation["messages"] if item["id"]==message_id)
                if revision_id:
                    message=self.conversation_store.get_revision(conversation_id,message_id,revision_id)
                descriptor=message.get("repository_inventory") or {}
                if not descriptor.get("paged") or descriptor.get("handle")!=handle:
                    raise ValueError("handle")
                store=InventoryStore(Path(self.project["output"])/"structural-index.sqlite")
                owned=store.page(handle,page_size=1)
                if owned["snapshot_id"]!=descriptor.get("snapshot_id") or owned.get("run_id")!=message.get("investigation_run_id"):
                    raise ValueError("owner")
                if action=="evidence":
                    return store.evidence(handle,candidate_id,index)
                if action not in {None,""}:
                    raise ValueError("action")
                return store.export(handle) if export else store.page(handle,cursor=cursor,page_size=page_size)
            except (ValueError,TypeError,StopIteration,OSError,sqlite3.Error,KeyError):
                raise RequestError("INVENTORY_NOT_FOUND","完整分析清单或该版本来源不可用。",404) from None


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, app: WorkbenchState, web_root: Path) -> None:
        self.app, self.web_root = app, web_root.resolve()
        self.online_app = None
        self.online_lock = threading.Lock()
        self.job_stream_slots = threading.BoundedSemaphore(MAX_JOB_STREAMS)
        self.job_stream_stop = threading.Event()
        super().__init__(("127.0.0.1", port), RequestHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.allowed_host = f"127.0.0.1:{self.server_port}"

    def server_close(self) -> None:
        self.job_stream_stop.set()
        if self.online_app is not None:
            self.online_app.close()
        super().server_close()


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "LocalWorkbench"
    sys_version = ""

    def log_message(self, *_: object) -> None:
        pass

    def _guard(self, *, token: bool = False) -> None:
        if self.headers.get_all("Host", []) != [self.server.allowed_host]:
            raise RequestError("INVALID_HOST", "只接受本机页面请求。", 403)
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            raise RequestError("CROSS_ORIGIN_DENIED", "不接受其他网站的请求。", 403)
        if self.headers.get("Sec-Fetch-Site") not in {None, "none", "same-origin"}:
            raise RequestError("CROSS_ORIGIN_DENIED", "不接受其他网站的请求。", 403)
        if token:
            supplied = self.headers.get("X-Session-Token", "")
            if not supplied.isascii() or not hmac.compare_digest(supplied, self.server.app.session_token):
                raise RequestError("INVALID_SESSION", "会话已失效，请刷新本机页面。", 403)

    def _send(self, status: int, data: bytes, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _json(self, status: int, value: object) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def _error(self, exc: RequestError) -> None:
        error = {"code": exc.code, "message": exc.message}
        if exc.diagnostic:
            error["diagnostic"] = exc.diagnostic
        self._json(exc.status, {"error": error})

    def _stream_job(self, job_id: str) -> None:
        """Publish current job snapshots without retaining an event transcript."""
        if not self.server.job_stream_slots.acquire(blocking=False):
            raise RequestError("JOB_STREAM_LIMIT", "即时进度连接已满，请使用进度查询。", 503)
        started = False
        previous_timeout = self.connection.gettimeout()

        def snapshot():
            with self.server.app.lock:
                job = self.server.app.get_job(job_id)
                version = (self.server.app.updated_at, job["status"], job.get("cancel_requested"))
                return job, version

        def send_job(job):
            data = json.dumps(job, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.wfile.write(b"event: job\ndata: " + data + b"\n\n")
            self.wfile.flush()

        try:
            job, version = snapshot()
            self.connection.settimeout(JOB_STREAM_WRITE_TIMEOUT_SECONDS)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Connection", "close")
            started = True
            self.close_connection = True
            self.end_headers()
            send_job(job)
            last_write = time.monotonic()
            while job["status"] == "RUNNING" and not self.server.job_stream_stop.wait(JOB_STREAM_INTERVAL_SECONDS):
                with self.server.app.lock:
                    current = self.server.app.job
                    if not current or current["job_id"] != job_id:
                        return
                    current_version = (self.server.app.updated_at, current["status"], current.get("cancel_requested"))
                    changed = current_version != version
                    if changed:
                        job, version = snapshot()
                if changed:
                    send_job(job)
                    last_write = time.monotonic()
                elif time.monotonic() - last_write >= JOB_STREAM_HEARTBEAT_SECONDS:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_write = time.monotonic()
        except Exception:
            if not started:
                raise
            # A closed or slow reader ends only its subscription. The job and
            # its received answer remain available through ordinary polling.
        finally:
            try:
                self.connection.settimeout(previous_timeout)
            except OSError:
                pass
            self.server.job_stream_slots.release()

    def _discard_rejected_post_body(self) -> int:
        """Avoid resetting a sent error when a short rejected body arrives late.

        Authentication has already failed or request validation has rejected
        this POST. Discard bytes only; never parse them or invoke application
        work. A total short deadline also bounds clients that drip body bytes.
        """
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isdigit():
            return 0
        try:
            remaining = int(lengths[0])
        except ValueError:
            return 0
        if not 0 < remaining <= MAX_REQUEST_BYTES:
            return 0
        consumed = 0
        deadline = time.monotonic() + REJECTED_BODY_DRAIN_SECONDS
        previous_timeout = self.connection.gettimeout()
        try:
            while remaining:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    break
                self.connection.settimeout(wait)
                chunk = self.rfile.read1(min(8192, remaining))
                if not chunk:
                    break
                consumed += len(chunk)
                remaining -= len(chunk)
        except OSError:
            pass
        finally:
            self.connection.settimeout(previous_timeout)
        return consumed

    def do_GET(self) -> None:
        try:
            route = urlsplit(self.path)
            if route.scheme or route.netloc or route.fragment:
                raise RequestError("INVALID_ROUTE", "请求路径无效。")
            path = route.path
            self._guard(token=path.startswith("/api/") and path != "/api/state")
            if path.startswith("/api/online/"):
                from online_web_bridge import dispatch_online
                self._json(*dispatch_online(self.server, "GET", self.path))
            elif path == "/api/state" and not route.query:
                self._json(200, self.server.app.state())
            elif path == "/api/framework-demo" and not route.query:
                self._json(200, self.server.app.framework_demo())
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}/events", path) and not route.query:
                self._stream_job(path.split("/")[3])
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}/progress", path):
                query = parse_qs(route.query, keep_blank_values=True)
                if (set(query) != {"after", "wait_ms"} or any(len(values) != 1 for values in query.values())
                        or any(not re.fullmatch(r"[0-9]{1,16}", values[0]) for values in query.values())):
                    raise RequestError("INVALID_QUERY", "进度查询参数无效。")
                self._json(200, self.server.app.wait_job(path.split("/")[3],
                    after=int(query["after"][0]), wait_ms=int(query["wait_ms"][0])))
            elif path.startswith("/api/jobs/") and not route.query:
                self._json(200, self.server.app.get_job(path.removeprefix("/api/jobs/")))
            elif re.fullmatch(r"/api/conversations/[a-f0-9]{32}", path) and not route.query:
                self._json(200, self.server.app.get_conversation(path.rsplit("/", 1)[1]))
            elif path == "/api/evidence":
                query = parse_qs(route.query, keep_blank_values=True)
                if "id" not in query or set(query) - {"id", "conversation_id", "message_id", "revision_id"} or any(len(v) != 1 for v in query.values()):
                    raise RequestError("INVALID_QUERY", "请提供一个有效证据标识。")
                self._json(200, self.server.app.evidence(query["id"][0], query.get("conversation_id", [None])[0],
                    query.get("message_id", [None])[0], query.get("revision_id", [None])[0]))
            elif path == "/api/repository-inventory":
                query=parse_qs(route.query,keep_blank_values=True)
                allowed={"handle","conversation_id","message_id","revision_id","cursor","page_size","export","action","candidate_id","index"}
                if set(query)-allowed or any(len(value)!=1 for value in query.values()):
                    raise RequestError("INVALID_QUERY","分析清单查询参数无效。")
                numbers={key:query.get(key,[default])[0] for key,default in (("cursor","0"),("page_size","50"),("index","0"))}
                if any(not value.isdecimal() for value in numbers.values()) or query.get("export",[""])[0] not in {"","jsonl"}:
                    raise RequestError("INVALID_QUERY","分析清单查询参数无效。")
                self._json(200,self.server.app.repository_inventory(query.get("handle",[""])[0],query.get("conversation_id",[""])[0],query.get("message_id",[""])[0],
                    cursor=int(numbers["cursor"]),page_size=int(numbers["page_size"]),index=int(numbers["index"]),export=query.get("export",[""])[0]=="jsonl",
                    action=query.get("action",[None])[0],candidate_id=query.get("candidate_id",[None])[0],revision_id=query.get("revision_id",[None])[0]))
            elif path == "/api/impact":
                query = parse_qs(route.query, keep_blank_values=True)
                if set(query) - {"handle", "conversation_id", "message_id", "cursor", "export"} or any(len(v) != 1 for v in query.values()):
                    raise RequestError("INVALID_QUERY", "相关对象查询参数无效。")
                cursor_text = query.get("cursor", ["0"])[0]
                if not cursor_text.isdecimal() or query.get("export", [""])[0] not in {"", "jsonl"}:
                    raise RequestError("INVALID_QUERY", "相关对象查询参数无效。")
                self._json(200, self.server.app.impact(query.get("handle", [""])[0],
                    query.get("conversation_id", [""])[0], query.get("message_id", [""])[0],
                    cursor=int(cursor_text),
                    export=query.get("export", [""])[0] == "jsonl"))
            elif path in STATIC_FILES:
                file = self.server.web_root / STATIC_FILES[path]
                if file.is_symlink() or not file.is_file():
                    raise RequestError("NOT_FOUND", "页面文件尚未就绪。", 404)
                content_type = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}[file.suffix]
                self._send(200, file.read_bytes(), content_type + "; charset=utf-8")
            elif path in ONLINE_STATIC_FILES:
                file = self.server.web_root / ONLINE_STATIC_FILES[path]
                if file.is_symlink() or not file.is_file():
                    raise RequestError("NOT_FOUND", "在线页面文件尚未就绪。", 404)
                content_type = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}[file.suffix]
                self._send(200, file.read_bytes(), content_type + "; charset=utf-8")
            else:
                raise RequestError("NOT_FOUND", "请求的页面不存在。", 404)
        except RequestError as exc:
            self._error(exc)
        except Exception as exc:
            self._error(RequestError("REQUEST_FAILED", "请求未完成。", 500,
                diagnostic=_failure_diagnostic(exc, "REQUEST_FAILED", http_status=500)))

    def do_DELETE(self) -> None:
        try:
            self._guard(token=True)
            route = urlsplit(self.path)
            if route.path.startswith("/api/online/"):
                from online_web_bridge import dispatch_online
                self._json(*dispatch_online(self.server, "DELETE", self.path))
                return
            source_match = re.fullmatch(r"/api/recent-conversations/([a-f0-9]{64})/([a-f0-9]{32})", route.path)
            if route.query or route.fragment or (re.fullmatch(r"/api/conversations/[a-f0-9]{32}", route.path) is None and source_match is None):
                raise RequestError("NOT_FOUND", "请求的操作不存在。", 404)
            self._json(200, self.server.app.delete_source_conversation(*source_match.groups()) if source_match else
                       self.server.app.delete_conversation(route.path.rsplit("/", 1)[1]))
        except RequestError as exc:
            self._error(exc)
        except Exception as exc:
            self._error(RequestError("REQUEST_FAILED", "请求未完成。", 500,
                diagnostic=_failure_diagnostic(exc, "REQUEST_FAILED", http_status=500)))

    def do_POST(self) -> None:
        body_consumed = False
        try:
            self._guard(token=True)
            cancel_match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})/cancel", self.path)
            online_request = self.path.startswith("/api/online/")
            if self.path not in {"/api/analyze", "/api/source/remote", "/api/source/remote/update", "/api/source/open", "/api/framework-demo/prepare", "/api/framework", "/api/answer-instructions", "/api/conversations", "/api/conversations/open", "/api/model-check", "/api/pick-folder"} and cancel_match is None and not online_request:
                raise RequestError("NOT_FOUND", "请求的操作不存在。", 404)
            if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
                raise RequestError("INVALID_CONTENT_TYPE", "只接受 JSON 请求。", 415)
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isdigit():
                raise RequestError("INVALID_LENGTH", "请求长度无效。")
            length = int(lengths[0])
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise RequestError("REQUEST_TOO_LARGE", "请求内容过大。", 413)
            try:
                raw_payload = self.rfile.read(length)
                body_consumed = True
                payload = json.loads(raw_payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise RequestError("INVALID_JSON", "请求内容不是有效 JSON。") from None
            if online_request:
                from online_web_bridge import dispatch_online
                self._json(*dispatch_online(self.server, "POST", self.path, payload))
            elif cancel_match:
                if payload != {}:
                    raise RequestError("INVALID_OPTIONS", "取消任务请求须为空对象。")
                self._json(202, self.server.app.cancel(cancel_match.group(1)))
            elif self.path == "/api/framework-demo/prepare":
                self._json(202, self.server.app.prepare_framework_demo(payload))
            elif self.path == "/api/framework":
                self._json(200, self.server.app.configure_framework(payload))
            elif self.path == "/api/answer-instructions":
                self._json(200, self.server.app.configure_answer_instructions(payload))
            elif self.path == "/api/model-check":
                self._json(200, self.server.app.check_model(payload))
            elif self.path == "/api/pick-folder":
                self._json(200, self.server.app.pick_folder(payload))
            elif self.path == "/api/conversations":
                self._json(201, self.server.app.new_conversation(payload))
            elif self.path == "/api/conversations/open":
                self._json(200, self.server.app.open_source_conversation(payload))
            elif self.path == "/api/source/open":
                self._json(200, self.server.app.open_source_workspace(payload))
            elif self.path == "/api/source/remote/update":
                self._json(202, self.server.app.update_remote(payload))
            elif self.path == "/api/source/remote":
                self._json(202, self.server.app.start_remote(payload))
            else:
                self._json(202, self.server.app.start(payload))
        except RequestError as exc:
            self._error(exc)
            if not body_consumed:
                self.wfile.flush()
                self._discard_rejected_post_body()
        except Exception as exc:
            self._error(RequestError("REQUEST_FAILED", "请求未完成。", 500,
                diagnostic=_failure_diagnostic(exc, "REQUEST_FAILED", http_status=500)))
            if not body_consumed:
                self.wfile.flush()
                self._discard_rejected_post_body()

    def do_OPTIONS(self) -> None:
        self._error(RequestError("CROSS_ORIGIN_DENIED", "不提供跨站访问。", 403))


def create_server(port: int = 8765, *, app: WorkbenchState | None = None, web_root: Path = WEB_ROOT) -> LocalServer:
    return LocalServer(port, app or WorkbenchState(state_path=state_root() / "workbench-state.json",
                                                  background_restore=True), web_root)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open a local COBOL source analysis workbench.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        server = create_server(args.port)
    except OSError:
        print("本机端口不可用，请用 --port 指定其他端口。")
        return 2
    print(f"本机工作台：{server.origin}；按 Ctrl+C 关闭。", flush=True)
    if not args.no_browser:
        timer = threading.Timer(0.2, webbrowser.open, args=(server.origin,))
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
