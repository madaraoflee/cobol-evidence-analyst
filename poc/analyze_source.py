#!/usr/bin/env python3
"""Refresh a user-selected source index, diagnose it, and optionally investigate."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from company_api import APIConfigurationError, CompanyAPIConfig, Transport
from api_error_details import build_diagnostic, build_local_diagnostic, format_diagnostic, sanitize_diagnostic
from repo_inventory import DEFAULT_EXTENSIONS, parse_extensions
from run_agent import run_investigation
from structural_index import build_structural_index
from business_index import build_business_index
from source_catalog import refresh_source_catalog, select_related_sources
from framework_knowledge import build_framework_context
from repository_discovery import ensure_repository_search
from report_view import write_report_view


DEFAULT_SCOPE_SOURCE_BYTES = 16 * 1024 * 1024

ARTIFACT_NAMES = (
    "structural-index.sqlite", "diagnosis.json", "diagnosis.md",
    "programs.json", "agent-result.json", "agent-result.md", "source-catalog.sqlite",
    "framework-context.json",
    "agent-result-view.json", "programs-view.json", "diagnosis-view.json",
)


class AnalysisCancelled(RuntimeError):
    """Cooperative cancellation; incomplete evidence never becomes current."""


def _verify_scope(source: Path, indexed: dict[str, str], progress: Callable | None = None,
                  check_cancel: Callable | None = None) -> None:
    for offset, (relative, expected) in enumerate(indexed.items()):
        if check_cancel:
            check_cancel()
        if progress:
            progress({"phase": "verifying", "completed": offset, "total": len(indexed),
                      "unit": "files", "current_file": relative})
        path = source / relative
        if path.is_symlink() or source not in path.resolve().parents:
            raise ValueError("An indexed source path changed or escaped the selected directory.")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                if check_cancel:
                    check_cancel()
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("Source files changed during analysis; rerun against a stable exported directory.")


def _paths(source_root: Path, output_root: Path) -> tuple[Path, Path]:
    source = source_root.expanduser().resolve()
    output_input = output_root.expanduser()
    if output_input.is_symlink():
        raise ValueError("Output must not be a symbolic link.")
    output = output_input.resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("Source and output must be separate, non-nested directories.")
    if output.exists() and not output.is_dir():
        raise ValueError("Output must be a directory.")
    for name in (*ARTIFACT_NAMES, "structural-index.sqlite-wal", "structural-index.sqlite-shm", "source-catalog.sqlite-wal", "source-catalog.sqlite-shm"):
        path = output / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("A reserved output path is not a regular file.")
    for name in ("quality-traces", "semantic-scopes", "impact-results"):
        path = output / name
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError("A reserved output directory is invalid.")
    for name in ("versioned-evidence.sqlite", "versioned-evidence.sqlite-wal", "versioned-evidence.sqlite-shm",
                 "source-versions.sqlite", "source-versions.sqlite-wal", "source-versions.sqlite-shm", "source-versions.sqlite-journal"):
        path = output / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("A reserved output path is not a regular file.")
    return source, output


def _write(path: Path, value: object, *, markdown: bool = False) -> None:
    content = str(value) if markdown else json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _catalog(database: Path) -> tuple[list[dict], list[dict], dict[str, str]]:
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        programs = [dict(row) for row in connection.execute(
            """SELECT s.name AS program_name, s.relative_path, u.start_line,
                      s.evidence_id
                 FROM symbols s JOIN code_units u ON u.unit_id = s.definition_unit_id
                WHERE s.symbol_type = 'Program'
                ORDER BY s.name, s.relative_path, u.start_line"""
        )]
        dependencies = [dict(row) for row in connection.execute(
            """SELECT relative_path, relation_type, target_name, status
                 FROM relations WHERE relation_type IN ('CALLS', 'CALL_TARGET_FROM', 'INCLUDES_COPY')
                  AND status != 'confirmed'
                ORDER BY relative_path, relation_type, target_name"""
        )]
        hashes = {row["relative_path"]: row["sha256"] for row in connection.execute(
            "SELECT relative_path, sha256 FROM source_files"
        )}
    for item in programs:
        item["entry_key"] = f"{item['relative_path']}::{item['program_name']}::{item['start_line']}"
        item["name_origin"] = "program_id"
    return programs, dependencies, hashes


def _select_entry(programs: list[dict], entry: str | None) -> tuple[dict | None, str | None]:
    if not entry:
        return None, None
    key = entry.strip().replace("\\", "/").casefold()
    if key.startswith("./"):
        key = key[2:]
    # PROGRAM-ID takes precedence over a coincidentally identical filename.
    matches = [item for item in programs if (item.get("entry_key") or "").casefold() == key]
    if not matches:
        matches = [item for item in programs if item["program_name"].casefold() == key]
    if not matches:
        matches = [item for item in programs if item["relative_path"].casefold() == key]
    if not matches:
        matches = [item for item in programs if Path(item["relative_path"]).name.casefold() == key]
    if not matches:
        return None, "ENTRY_NOT_FOUND"
    if len(matches) != 1:
        return None, "ENTRY_AMBIGUOUS"
    selected = matches[0]
    if sum(item["program_name"].casefold() == selected["program_name"].casefold() for item in programs) != 1:
        return None, "ENTRY_AMBIGUOUS"
    return selected, None


def _inline(value: object) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\\", "\\\\").replace("|", "\\|").replace("`", "\\`")
            .replace("[", "\\[").replace("]", "\\]").replace("\n", " ").replace("\r", " "))


def _render(report: dict, programs: list[dict]) -> str:
    build = report.get("build_report") or {}
    files = build.get("files", {})
    directory_files = (report.get("catalog_report") or {}).get("files", files)
    lines = ["# 源码接入诊断", "", f"本次状态：{report['runner_status']}；原因：{report['reason_code']}。", "",
             f"源码目录：{_inline(report['source_root'])}", "",
             f"索引快照：{_inline(build.get('snapshot_id', '尚未生成'))}", "",
             f"目录候选文件 {directory_files.get('candidate', 0)}，可选入口 {len(programs)}；"
             f"本次详细解析文件 {files.get('decoded', 0) if report.get('source_manifest_verified') else 0}，"
             f"详细索引更新 {files.get('indexed_or_updated', 0) if report.get('source_manifest_verified') else 0}。", "",
             "入口与路径来自本次指定目录。轻量目录可能使用待确认文件名；详细解析后才确认 PROGRAM-ID 和代码证据。完整清单见 programs.json。", ""]
    if report.get("catalog_snapshot_id"):
        lines += [f"目录版本：{_inline(report['catalog_snapshot_id'])}。", ""]
    if report.get("scope"):
        lines += ["分析范围：" + _inline(json.dumps(report["scope"], ensure_ascii=False)), ""]
    if report.get("selected_entry"):
        entry = report["selected_entry"]
        lines += [f"已选入口：{_inline(entry['program_name'])}，{_inline(entry['relative_path'])}:{entry['start_line']}。", ""]
    framework = report.get("framework_context") or {}
    document = framework.get("document") or {}
    lines += ["## 框架资料与当前源码", "", f"状态：{_inline(framework.get('status', 'NOT_CONFIGURED'))}。", ""]
    if document:
        lines += [f"资料：{_inline(document.get('title', ''))}；SHA256：{_inline(document.get('sha256', ''))}。", ""]
    for match in framework.get("source_matches", []):
        lines += [f"- {_inline(match.get('relative_path'))}:{match.get('start_line')}："
                  f"{_inline(', '.join(match.get('matched_terms', [])))} → "
                  f"{_inline(', '.join(match.get('reference_ids', [])))}。"]
    if framework.get("references"):
        lines += ["", "本次检索的资料章节（完整节选见 framework-context.json）：", ""]
        for reference in framework["references"]:
            page = f"第 {reference['page']} 页，" if reference.get("page") else ""
            lines += [f"- {_inline(reference['reference_id'])}：{_inline(reference['heading'])}；"
                      f"{page}文字稿 L{reference['start_line']}–L{reference['end_line']}。"]
    lines += ["", "资料命中是词项匹配线索；框架解释必须另引源码证据。文档约定不证明现场表值、闭源实现或实际运行结果。", ""]
    lines += ["## 需要处理的事项", ""]
    lines += [f"- {_inline(item)}" for item in report["messages"]] or ["当前没有接入阻断项。"]
    if programs:
        lines += ["", "## 实际程序（最多显示前 50 个）", "", "| PROGRAM-ID / 待确认入口 | 源文件 | 定义行 |", "| --- | --- | --- |"]
        lines += [f"| {_inline(item['program_name'])} | {_inline(item['relative_path'])} | {item['start_line']} |" for item in programs[:50]]
    lines += ["", "## 本次问答", "", f"状态：{report['question_status']}。详情见 agent-result.md。", "",
              "索引就绪只表示已接入当前源码，不表示完整业务语义、运行结果或所有方言已验证。", ""]
    return "\n".join(lines)


def analyze_source(
    source_root: Path,
    output_root: Path,
    *,
    entry: str | None = None,
    question: str | None = None,
    extensions: frozenset[str] = DEFAULT_EXTENSIONS,
    include_extensionless: bool = True,
    encoding: str = "auto",
    source_format: str = "auto",
    allow_network: bool = False,
    config: CompanyAPIConfig | None = None,
    transport: Transport | None = None,
    api_options: dict | None = None,
    quiet: bool = True,
    index_mode: str = "full",
    verify_content: bool = False,
    progress: Callable[[dict], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
    framework_reference_path: Path | str | None = None,
    capture_api_responses: bool = False,
    analysis_mode: str = "strict",
    max_source_pages: int = 12,
    reading_strategy: str = "focused",
    answer_detail: str = "detailed",
    conversation_history: list[dict] | None = None,
    agent_policy=None,
) -> dict:
    source, output = _paths(source_root, output_root)
    external_progress = progress
    def forward_progress(event: dict) -> None:
        if check_cancel:
            check_cancel()
        if external_progress:
            external_progress(event)
    progress = forward_progress
    if index_mode not in {"full", "catalog"}:
        raise ValueError("index_mode must be full or catalog.")
    if analysis_mode not in {"business", "strict"}:
        raise ValueError("analysis_mode must be business or strict.")
    if reading_strategy not in {"retrieval", "focused", "full_chain"}:
        raise ValueError("reading_strategy must be retrieval, focused or full_chain.")
    if answer_detail not in {"brief", "detailed"}:
        raise ValueError("answer_detail must be brief or detailed.")
    if type(max_source_pages) is not int or not 1 <= max_source_pages <= 128:
        raise ValueError("max_source_pages must be an integer from 1 to 128.")
    if entry is not None and not entry.strip():
        raise ValueError("--entry must not be empty.")
    if question is not None and not question.strip():
        raise ValueError("--question must not be empty.")
    if analysis_mode == "business" and reading_strategy == "retrieval" and question and allow_network and not verify_content:
        from indexed_chat import try_indexed_question
        cached = try_indexed_question(source, output, question=question, entry=entry,
            extensions=extensions, include_extensionless=include_extensionless, encoding=encoding,
            source_format=source_format, config=config, api_options=api_options, transport=transport,
            framework_reference_path=framework_reference_path, capture_api_responses=capture_api_responses,
            history=conversation_history, progress=progress, check_cancel=check_cancel,
            policy=agent_policy, answer_detail=answer_detail)
        if cached is not None:
            return cached
    repository_mode = analysis_mode == "business" and entry is None
    output.mkdir(parents=True, exist_ok=True)
    progress({"phase": "framework_reference", "completed": 0, "total": None, "unit": "references"})
    report = {
        "schema_version": "source-analysis/v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "runner_status": "PREPARING", "reason_code": "INDEX_REFRESH_IN_PROGRESS",
        "source_root": str(source), "entry_requested": entry, "selected_entry": None,
        "question": question, "question_status": "NOT_REQUESTED" if not question else "PENDING",
        "messages": [], "build_report": None,
        "source_options": {"extensions": sorted(extensions), "include_extensionless": include_extensionless,
                           "encoding": encoding, "source_format": source_format, "verify_content": verify_content,
                           "analysis_mode": analysis_mode, "max_source_pages": max_source_pages,
                           "reading_strategy": reading_strategy, "answer_detail": answer_detail},
        "artifacts": {name: str(output / name) for name in ARTIFACT_NAMES},
        "full_business_analysis_verified": False, "index_mode": index_mode,
        "catalog_ready": False, "source_manifest_verified": False,
        "framework_context": build_framework_context(reference_path=framework_reference_path),
    }
    programs: list[dict] = []
    agent: dict = {"runner_status": "NOT_REQUESTED", "agent_result": None}

    def save() -> None:
        _write(output / "diagnosis.json", report)
        write_report_view(output / "diagnosis.json", report)
        _write(output / "diagnosis.md", _render(report, programs), markdown=True)
        program_report = {"snapshot_id": (report.get("build_report") or {}).get("snapshot_id"),
            "catalog_snapshot_id": report.get("catalog_snapshot_id"), "scope": report.get("scope"), "programs": programs}
        _write(output / "programs.json", program_report)
        write_report_view(output / "programs.json", program_report)
        _write(output / "agent-result.json", agent)
        write_report_view(output / "agent-result.json", agent)
        _write(output / "framework-context.json", report["framework_context"])
        answer = (agent.get("agent_result") or {}).get("answer")
        _write(output / "agent-result.md", "\n".join([
            "# 本次源码问答", "", f"状态：{report['question_status']}；运行状态：{agent['runner_status']}。", "",
            f"快照：{_inline((report.get('build_report') or {}).get('snapshot_id', '未生成'))}。", "",
            answer or format_diagnostic(agent.get("diagnostic")) or "本次没有生成业务答案。请查看 diagnosis.md 中的原因与下一步。", "",
        ]), markdown=True)

    # Replace prior reports before indexing so a failed refresh cannot display an old answer as current.
    save()
    try:
        if not source.is_dir():
            raise ValueError("Source must be an existing directory; check --source.")
        if not quiet:
            print("正在读取指定源码并更新索引…", file=sys.stderr)
        catalog = None
        scope = None
        business_build = None
        indexed: dict[str, str] = {}
        if index_mode == "catalog" and not repository_mode:
            catalog = refresh_source_catalog(source, output / "source-catalog.sqlite", extensions=extensions,
                include_extensionless=include_extensionless, encoding=encoding, source_format=source_format,
                progress=progress, verify_content=verify_content)
            programs = catalog["programs"]
            report["catalog_ready"] = bool(programs)
            report["catalog_snapshot_id"] = catalog["snapshot_id"]
            report["catalog_report"] = catalog
            report["messages"].extend(item.get("message", str(item)) if isinstance(item, dict) else item for item in catalog.get("warnings", []))
            report["program_count"] = len(programs)
            report["scope"] = catalog.get("scope", {"kind": "catalogue"})
            report["build_report"] = {"snapshot_id": None, "files": catalog["files"],
                "diagnostics": {"copybook_count": catalog.get("copybook_count", 0), "warnings": catalog.get("warnings", [])}}
            if not programs:
                report.update(runner_status="BLOCKED", reason_code="NO_PROGRAMS_RECOGNIZED")
                report["messages"].append("没有找到可接入的源码。检查源目录、扩展名、编码和导出格式。")
            elif not question and not entry:
                report.update(runner_status="INDEX_READY", reason_code="SOURCE_CATALOG_READY")
                report["messages"].append("源码目录已接入；详细解析将在选择入口后按需执行。目录候选不代表完整程序或依赖已核验。")
            else:
                chosen_entry = entry or (programs[0].get("entry_key") or programs[0]["program_name"] if len(programs) == 1 else None)
                if not chosen_entry:
                    report.update(runner_status="BLOCKED", reason_code="ENTRY_REQUIRED")
                    report["messages"].append("请先从源码目录选择一个入口，再分析该入口与可用依赖。")
                else:
                    if analysis_mode == "business":
                        business_build = build_business_index(source, output / "structural-index.sqlite",
                            extensions=extensions, include_extensionless=include_extensionless,
                            encoding=encoding, source_format=source_format, quiet=quiet,
                            catalog=catalog, entry_program=chosen_entry, progress=progress,
                            check_cancel=check_cancel, verify_content=verify_content,
                            framework_reference_path=framework_reference_path)
                        scope = business_build
                    else:
                        scope = select_related_sources(source, catalog, chosen_entry, encoding=encoding,
                            source_format=source_format, max_files=24, max_depth=2,
                            max_scan_bytes=2097152, max_total_source_bytes=DEFAULT_SCOPE_SOURCE_BYTES, progress=progress)
                    report["scope"] = scope["scope"]
                    report["scope"]["boundaries"] = scope.get("missing_dependencies", [])
                    if analysis_mode == "strict":
                        report["scope"].update(budget_kind="source_input_bytes", memory_usage_bounded=False)
                    if any(item.get("status") == "ENTRY_EXCEEDS_BYTE_BUDGET" for item in scope.get("missing_dependencies", [])):
                        report.update(runner_status="INDEX_READY", reason_code="ENTRY_SCOPE_LIMIT")
                        report["question_status"] = "SCOPE_LIMIT"
                        report["messages"].append("所选单文件超过 16 MiB 源文件输入预算；此预算不是内存上限。目录仍可使用，请选择较小入口或按业务导出该程序的相关部分。")
                        scope = None
                        question = None
                        save()
                        return report
                    report["selected_entry"] = scope.get("selected_entry")
        if index_mode == "full" or scope is not None or repository_mode:
            builder = build_business_index if analysis_mode == "business" else build_structural_index
            build = business_build or builder(source, output / "structural-index.sqlite", extensions=extensions,
                include_extensionless=include_extensionless, encoding=encoding, source_format=source_format,
                quiet=quiet, include_paths=scope["relative_paths"] if scope else None,
                progress=progress, check_cancel=check_cancel, verify_content=verify_content,
                max_source_bytes=DEFAULT_SCOPE_SOURCE_BYTES if scope else None,
                **({"framework_reference_path": framework_reference_path} if analysis_mode == "business" else {}))
            report["build_report"] = build
            if analysis_mode == "business":
                report["framework_semantics"] = build.get("framework_semantics", {})
            detailed_programs, dependencies, indexed = _catalog(output / "structural-index.sqlite")
            if analysis_mode != "business":
                _verify_scope(source, indexed, progress, check_cancel)
                report["source_manifest_verified"] = True
            report["source_verification_scope"] = "selected_sources" if scope else "full_index"
            report["atomic_source_snapshot"] = False
            report["unresolved_dependencies"] = dependencies
            report["messages"].extend(build.get("diagnostics", {}).get("warnings", []))
            if catalog:
                detailed_paths = {item["relative_path"] for item in detailed_programs}
                programs = [item for item in programs if item["relative_path"] not in detailed_paths] + detailed_programs
                programs.sort(key=lambda item: (item["program_name"].casefold(), item["relative_path"].casefold()))
                report["catalog_ready"] = bool(programs)
                selected_path = (scope.get("selected_entry") or {}).get("relative_path")
                selected_options = [item for item in detailed_programs if item["relative_path"] == selected_path]
                selected = next((item for item in selected_options if item["program_name"] == (scope.get("selected_entry") or {}).get("program_name")), None)
                selected = selected or (selected_options[0] if len(selected_options) == 1 else None)
                entry_error = None if selected else "ENTRY_NOT_RECOGNIZED_IN_SCOPE"
                report["scope"].update(detail_file_count=build["files"]["decoded"], catalogue_program_count=len(programs),
                    source_verification=("incremental_metadata_and_content_hash"
                        if analysis_mode == "business" and not verify_content else "selected_sources_content_hash"),
                    full_repository_verified=False)
            else:
                programs = detailed_programs
                selected, entry_error = (None, None) if repository_mode else _select_entry(programs, entry)
                report["scope"] = build["scope"]
            if repository_mode:
                report["scope"].update(mode="repository_question" if question else "repository_index",
                                       repository_file_count=len(indexed))
                report["catalog_ready"] = bool(indexed)
            if analysis_mode == "business":
                report["repository_search"] = ensure_repository_search(
                    output / "structural-index.sqlite", source,
                    check_cancel=check_cancel, progress=progress, verify_content=verify_content)
                # Search ingestion verifies every new/changed source against
                # the structural hash. A separate whole-repository hash pass
                # here repeated the same disk reads on every refresh.
                report["source_manifest_verified"] = True
                report["source_verification_method"] = (
                    "full_content_hash" if verify_content else "incremental_metadata_and_content_hash")
            report["program_count"] = len(programs)
            report["selected_entry"] = selected
            if dependencies:
                report["messages"].append(f"有 {len(dependencies)} 项调用或 COPY 依赖未确认；将继续解释现有源码，并把闭源对象、缺失源码和动态目标明确列为边界。")
            if not detailed_programs and not repository_mode:
                report.update(runner_status="BLOCKED", reason_code="NO_PROGRAMS_RECOGNIZED")
                report["messages"].append("当前入口未识别到 PROGRAM-ID。检查编码及 fixed/free 格式；可以选择其他源码继续。")
            elif entry_error:
                report.update(runner_status="BLOCKED", reason_code=entry_error)
                report["messages"].append("入口不存在或无法唯一定位。请从目录选择准确的文件和程序定义位置，并核对是否存在重复入口。")
            else:
                report.update(runner_status="NEEDS_ATTENTION" if report["messages"] else "INDEX_READY", reason_code="SOURCE_INDEX_READY")
            if report["runner_status"] != "BLOCKED" and not (analysis_mode == "business" and reading_strategy == "retrieval"):
                progress({"phase": "framework", "completed": 0, "total": None, "unit": "steps"})
                report["framework_context"] = build_framework_context(
                    output / "structural-index.sqlite",
                    entry_program=(report["selected_entry"] or {}).get("program_name"),
                    question=question or "", reference_path=framework_reference_path,
                    source_root=source, check_cancel=check_cancel,
                )
        if report["runner_status"] == "BLOCKED":
            report["question_status"] = "BLOCKED" if question else "NOT_REQUESTED"
            agent = {"runner_status": "NOT_READY", "reason_code": report["reason_code"], "agent_result": None}
        elif question and not allow_network:
            report["question_status"] = "NETWORK_DISABLED"
            agent = {"runner_status": "NETWORK_DISABLED", "agent_result": None}
            report["messages"].append("源码已接入；本次未调用模型。需要问答时，在相同命令增加 --allow-network。")
        elif question:
            report["question_status"] = "RUNNING"
            save()
            if not quiet:
                print("正在生成业务分析…" if analysis_mode == "business" else "正在检查接口能力并调查当前源码…", file=sys.stderr)
            if progress:
                progress({"phase": "investigating", "completed": 0, "total": None, "unit": "steps"})
            if check_cancel:
                check_cancel()
            try:
                selected_config = config or CompanyAPIConfig.from_env(**(api_options or {}))
                selected_entry = report["selected_entry"] or {}
                investigation_entry = (selected_entry.get("entry_key") or selected_entry.get("relative_path")) if analysis_mode == "business" else selected_entry.get("program_name")
                agent = run_investigation(question.strip(), output / "structural-index.sqlite", selected_config,
                                          entry_program=investigation_entry,
                                          allow_network=True, transport=transport, analysis_scope=report.get("scope"),
                                          framework_context=report["framework_context"],
                                          framework_reference_path=framework_reference_path,
                                          capture_api_responses=capture_api_responses,
                                          analysis_mode=analysis_mode, source_root=source,
                                          max_source_pages=max_source_pages, reading_strategy=reading_strategy,
                                          answer_detail=answer_detail,
                                          progress=progress, check_cancel=check_cancel,
                                          conversation_history=conversation_history, agent_policy=agent_policy)
            except APIConfigurationError as exc:
                agent = {"runner_status": "NOT_READY", "reason_code": exc.code, "agent_result": None}
                diagnostic = sanitize_diagnostic(getattr(exc, "diagnostic", None))
                if diagnostic is not None:
                    agent["diagnostic"] = diagnostic
            diagnostic = sanitize_diagnostic(agent.get("diagnostic"))
            if diagnostic is not None:
                report["diagnostic"] = diagnostic
            result = agent.get("agent_result") or {}
            if (analysis_mode == "business" and reading_strategy == "retrieval"
                    and result.get("snapshot_id")
                    and result["snapshot_id"] != (report.get("build_report") or {}).get("snapshot_id")):
                programs, _, _ = _catalog(output / "structural-index.sqlite")
                report["build_report"]["snapshot_id"] = result["snapshot_id"]
                if report.get("repository_search"):
                    from repository_discovery import repository_search_overview
                    report["repository_search"] = repository_search_overview(output / "structural-index.sqlite", source)
            if analysis_mode == "business" and isinstance(result.get("framework_context"), dict):
                report["framework_context"] = result["framework_context"]
            investigation = result.get("investigation") or agent.get("investigation")
            if analysis_mode == "business" and isinstance(investigation, dict):
                report["investigation"] = {key: investigation[key] for key in (
                    "mode", "repository_file_count", "selected_file_count", "matched_file_count",
                    "search_rounds", "fallback_all", "search_stop_reason", "dependency_expansion_complete",
                ) if key in investigation}
            if agent["runner_status"] == "COMPLETED":
                report["question_status"] = result.get("status", "COMPLETED")
            else:
                report["question_status"] = agent["runner_status"]
                report["messages"].append(format_diagnostic(diagnostic) or "源码索引已建立，问答未完成。请查看页面的 API 返回，或 agent-result.json 的 reason_code、capability_report、agent_result.stop_reason，区分接口、协议和调查限制。")
            # Catch edits made while the model was investigating the stored snapshot.
            if not (analysis_mode == "business" and reading_strategy == "retrieval"):
                _verify_scope(source, indexed, progress, check_cancel)
    except AnalysisCancelled:
        report.update(runner_status="CANCELLED", reason_code="USER_CANCELLED", source_manifest_verified=False, catalog_ready=False)
        report["framework_context"] = build_framework_context(reference_path=framework_reference_path)
        report["question_status"] = "CANCELLED" if question else "NOT_REQUESTED"
        programs = []
        agent = {"runner_status": "CANCELLED", "agent_result": None}
        save()
        raise
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        report.update(runner_status="BLOCKED", reason_code="SOURCE_ANALYSIS_FAILED")
        report["source_manifest_verified"] = False
        report["framework_context"] = build_framework_context(reference_path=framework_reference_path)
        report["question_status"] = "BLOCKED" if question else "NOT_REQUESTED"
        diagnostic = build_local_diagnostic(exc)
        report["diagnostic"] = diagnostic
        if type(exc) is ValueError and exc.args == ("SOURCE_INDEX_EMPTY",) and getattr(exc, "input_skips", None):
            skips = exc.input_skips
            report["build_report"] = {"snapshot_id": None,
                "files": {"candidate": exc.source_input_count, "decoded": 0, "unreadable_or_binary": len(skips)},
                "input_skips": skips, "diagnostics": {"warnings": ["所选候选文件均未能读取，没有发布新的源码索引。"]}}
            report["scope"] = {"kind": "selected_sources" if entry else "full_directory",
                "file_count": 0, "input_coverage_complete": False,
                "boundaries": [{"relative_path": item["relative_path"], "relation_type": "SOURCE_INPUT",
                                "target_name": "", "status": item["reason_code"]} for item in skips]}
        if type(exc) is RuntimeError and exc.args in (
            ("This SQLite build does not include FTS5 support.",),
            ("This Python SQLite build does not include FTS5 support.",),
        ):
            report["messages"].append("当前 Python SQLite 不支持 FTS5，请使用包含 FTS5 的 Python 环境。")
        report["messages"].append(format_diagnostic(diagnostic))
        response_diagnostics = agent.get("api_diagnostics")
        prior_diagnostic = sanitize_diagnostic(agent.get("diagnostic"))
        unaccepted_response = agent.get("unaccepted_response")
        prior_result = agent.get("agent_result") or {}
        narrative = prior_result.get("narrative") or {}
        previous_text = narrative.get("text") if isinstance(narrative, dict) else None
        previous_text = previous_text or prior_result.get("answer")
        if isinstance(previous_text, str) and previous_text.strip():
            # Keep the received explanation inspectable after its source
            # authority expires, without granting it active source references.
            unaccepted_response = {"reason_code": "SOURCE_ANALYSIS_FAILED", "text": previous_text[:60_000]}
        agent = {"runner_status": "NOT_READY", "reason_code": "SOURCE_ANALYSIS_FAILED",
                 "agent_result": None, "diagnostic": diagnostic}
        if prior_diagnostic is not None:
            agent["prior_diagnostic"] = prior_diagnostic
        if response_diagnostics is not None:
            # Response receipt remains observable after source authority expires.
            agent["api_diagnostics"] = response_diagnostics
        if unaccepted_response:
            agent["unaccepted_response"] = unaccepted_response
    save()
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Index the specified source directory, show real program names, and optionally ask the approved API.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--entry", help="Actual PROGRAM-ID, relative source path, or unique filename.")
    parser.add_argument("--question")
    parser.add_argument("--analysis-mode", choices=("business", "strict"), default="business",
                        help="business: source reading and explanation; strict: legacy action-contract investigation.")
    parser.add_argument("--max-source-pages", type=int, default=12, help="Focused page budget or full-chain batch size, from 1 to 128.")
    parser.add_argument("--reading-strategy", choices=("retrieval", "focused", "full_chain"), default="retrieval",
                        help="full_chain: read all available selected source pages in batches; focused: bounded page selection.")
    parser.add_argument("--answer-detail", choices=("brief", "detailed"), default="detailed",
                        help="Business retrieval answers are detailed by default; brief keeps the conclusion and key conditions.")
    parser.add_argument("--framework-reference", type=Path, help="Private UTF-8 Markdown reference; defaults to FRAMEWORK_REFERENCE_PATH from the project .env.")
    parser.add_argument("--index-mode", choices=("catalog", "full"), default="full", help="catalog: quick inventory, then bounded entry analysis; full: detailed whole-directory index.")
    parser.add_argument("--verify-content", action="store_true", help="Reread content instead of trusting unchanged file metadata.")
    parser.add_argument("--extensions")
    members = parser.add_mutually_exclusive_group()
    members.add_argument("--exclude-extensionless", dest="include_extensionless", action="store_false")
    members.add_argument("--include-extensionless", dest="include_extensionless", action="store_true", help="Include members without suffixes (the default).")
    parser.set_defaults(include_extensionless=True)
    parser.add_argument("--encoding", default="auto")
    parser.add_argument("--source-format", choices=("auto", "fixed", "free"), default="auto")
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--base-url")
    parser.add_argument("--chat-model")
    parser.add_argument("--api-style")
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--max-output-tokens", type=int,
                        help="Output limit override; otherwise use environment, local file, then the 4096-token analysis default.")
    parser.add_argument("--allow-insecure-localhost", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        from runtime_settings import load_agent_policy
        policy = load_agent_policy() if args.question and args.analysis_mode == "business" and args.reading_strategy == "retrieval" else None
        report = analyze_source(args.source, args.output, entry=args.entry, question=args.question,
                                extensions=parse_extensions(args.extensions), include_extensionless=args.include_extensionless,
                                encoding=args.encoding, source_format=args.source_format, allow_network=args.allow_network,
                                index_mode=args.index_mode, verify_content=args.verify_content,
                                framework_reference_path=args.framework_reference,
                                analysis_mode=args.analysis_mode, max_source_pages=args.max_source_pages,
                                reading_strategy=args.reading_strategy, answer_detail=args.answer_detail, agent_policy=policy,
                                api_options={"base_url": args.base_url, "chat_model": args.chat_model, "api_style": args.api_style,
                                             "timeout_seconds": args.timeout_seconds, "max_output_tokens": args.max_output_tokens,
                                             "default_max_output_tokens": 4096, "profile_name": "analysis",
                                             "allow_insecure_localhost": args.allow_insecure_localhost}, quiet=False)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({"runner_status": "BLOCKED", "reason_code": "INVALID_PATH_OR_OPTIONS", "message": str(exc),
                          "report_files_updated": False,
                          "previous_reports_are_current": False}, ensure_ascii=False))
        return 2
    print(json.dumps({key: report.get(key) for key in (
        "runner_status", "reason_code", "source_root", "program_count", "selected_entry", "question_status", "messages", "artifacts"
    )}, ensure_ascii=False, indent=2))
    if report["runner_status"] == "BLOCKED" or report["question_status"] == "NOT_READY":
        return 2
    if report["question_status"] in {"SAFE_STOP", "ABSTAINED"}:
        return 3
    if report["question_status"] == "NETWORK_DISABLED":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
