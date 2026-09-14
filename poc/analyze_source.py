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
from repo_inventory import DEFAULT_EXTENSIONS, parse_extensions
from run_agent import run_investigation
from structural_index import build_structural_index
from source_catalog import refresh_source_catalog, select_related_sources


DEFAULT_SCOPE_SOURCE_BYTES = 16 * 1024 * 1024

ARTIFACT_NAMES = (
    "structural-index.sqlite", "diagnosis.json", "diagnosis.md",
    "programs.json", "agent-result.json", "agent-result.md", "source-catalog.sqlite",
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
    if entry is not None and not entry.strip():
        raise ValueError("--entry must not be empty.")
    if question is not None and not question.strip():
        raise ValueError("--question must not be empty.")
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": "source-analysis/v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "runner_status": "PREPARING", "reason_code": "INDEX_REFRESH_IN_PROGRESS",
        "source_root": str(source), "entry_requested": entry, "selected_entry": None,
        "question": question, "question_status": "NOT_REQUESTED" if not question else "PENDING",
        "messages": [], "build_report": None,
        "source_options": {"extensions": sorted(extensions), "include_extensionless": include_extensionless,
                           "encoding": encoding, "source_format": source_format, "verify_content": verify_content},
        "artifacts": {name: str(output / name) for name in ARTIFACT_NAMES},
        "full_business_analysis_verified": False, "index_mode": index_mode,
        "catalog_ready": False, "source_manifest_verified": False,
    }
    programs: list[dict] = []
    agent: dict = {"runner_status": "NOT_REQUESTED", "agent_result": None}

    def save() -> None:
        _write(output / "diagnosis.json", report)
        _write(output / "diagnosis.md", _render(report, programs), markdown=True)
        _write(output / "programs.json", {"snapshot_id": (report.get("build_report") or {}).get("snapshot_id"),
            "catalog_snapshot_id": report.get("catalog_snapshot_id"), "scope": report.get("scope"), "programs": programs})
        _write(output / "agent-result.json", agent)
        answer = (agent.get("agent_result") or {}).get("answer")
        _write(output / "agent-result.md", "\n".join([
            "# 本次源码问答", "", f"状态：{report['question_status']}；运行状态：{agent['runner_status']}。", "",
            f"快照：{_inline((report.get('build_report') or {}).get('snapshot_id', '未生成'))}。", "",
            answer or "本次没有生成业务答案。请查看 diagnosis.md 中的原因与下一步。", "",
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
        indexed: dict[str, str] = {}
        if index_mode == "catalog":
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
                    scope = select_related_sources(source, catalog, chosen_entry, encoding=encoding,
                        source_format=source_format, max_files=24, max_depth=2,
                        max_scan_bytes=2097152, max_total_source_bytes=DEFAULT_SCOPE_SOURCE_BYTES, progress=progress)
                    report["scope"] = scope["scope"]
                    report["scope"]["boundaries"] = scope.get("missing_dependencies", [])
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
        if index_mode == "full" or scope is not None:
            build = build_structural_index(source, output / "structural-index.sqlite", extensions=extensions,
                include_extensionless=include_extensionless, encoding=encoding, source_format=source_format,
                quiet=quiet, include_paths=scope["relative_paths"] if scope else None,
                progress=progress, check_cancel=check_cancel, verify_content=verify_content,
                max_source_bytes=DEFAULT_SCOPE_SOURCE_BYTES if scope else None)
            report["build_report"] = build
            detailed_programs, dependencies, indexed = _catalog(output / "structural-index.sqlite")
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
                    source_verification="selected_sources_content_hash", full_repository_verified=False)
            else:
                programs = detailed_programs
                selected, entry_error = _select_entry(programs, entry)
                report["scope"] = build["scope"]
            report["program_count"] = len(programs)
            report["selected_entry"] = selected
            if dependencies:
                report["messages"].append(f"有 {len(dependencies)} 项调用或 COPY 依赖未确认；将继续解释现有源码，并把闭源对象、缺失源码和动态目标明确列为边界。")
            if not detailed_programs:
                report.update(runner_status="BLOCKED", reason_code="NO_PROGRAMS_RECOGNIZED")
                report["messages"].append("当前入口未识别到 PROGRAM-ID。检查编码及 fixed/free 格式；可以选择其他源码继续。")
            elif entry_error:
                report.update(runner_status="BLOCKED", reason_code=entry_error)
                report["messages"].append("入口不存在或无法唯一定位。请从目录选择实际入口；重复程序版本应分开分析。")
            else:
                report.update(runner_status="NEEDS_ATTENTION" if report["messages"] else "INDEX_READY", reason_code="SOURCE_INDEX_READY")
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
                print("正在检查接口能力并调查当前源码…", file=sys.stderr)
            if progress:
                progress({"phase": "investigating", "completed": 0, "total": None, "unit": "steps"})
            if check_cancel:
                check_cancel()
            try:
                selected_config = config or CompanyAPIConfig.from_env(**(api_options or {}))
                agent = run_investigation(question.strip(), output / "structural-index.sqlite", selected_config,
                                          entry_program=(report["selected_entry"] or {}).get("program_name"),
                                          allow_network=True, transport=transport, analysis_scope=report.get("scope"))
            except APIConfigurationError as exc:
                agent = {"runner_status": "NOT_READY", "reason_code": exc.code, "agent_result": None}
            result = agent.get("agent_result") or {}
            if agent["runner_status"] == "COMPLETED":
                report["question_status"] = result.get("status", "COMPLETED")
            else:
                report["question_status"] = agent["runner_status"]
                report["messages"].append("源码索引已建立，问答未完成。请查看 agent-result.json 的 reason_code、capability_report 和 stop_reason 定位 API 能力或调查限制。")
            # Catch edits made while the model was investigating the stored snapshot.
            _verify_scope(source, indexed, progress, check_cancel)
    except AnalysisCancelled:
        report.update(runner_status="CANCELLED", reason_code="USER_CANCELLED", source_manifest_verified=False, catalog_ready=False)
        report["question_status"] = "CANCELLED" if question else "NOT_REQUESTED"
        programs = []
        agent = {"runner_status": "CANCELLED", "agent_result": None}
        save()
        raise
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        report.update(runner_status="BLOCKED", reason_code="SOURCE_ANALYSIS_FAILED")
        report["source_manifest_verified"] = False
        report["question_status"] = "BLOCKED" if question else "NOT_REQUESTED"
        report["messages"].append(str(exc))
        agent = {"runner_status": "NOT_READY", "reason_code": "SOURCE_ANALYSIS_FAILED", "agent_result": None}
    save()
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Index the specified source directory, show real program names, and optionally ask the approved API.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--entry", help="Actual PROGRAM-ID, relative source path, or unique filename.")
    parser.add_argument("--question")
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
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--allow-insecure-localhost", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = analyze_source(args.source, args.output, entry=args.entry, question=args.question,
                                extensions=parse_extensions(args.extensions), include_extensionless=args.include_extensionless,
                                encoding=args.encoding, source_format=args.source_format, allow_network=args.allow_network,
                                index_mode=args.index_mode, verify_content=args.verify_content,
                                api_options={"base_url": args.base_url, "chat_model": args.chat_model, "api_style": args.api_style,
                                             "timeout_seconds": args.timeout_seconds, "max_output_tokens": args.max_output_tokens,
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
