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
from typing import Sequence

from company_api import APIConfigurationError, CompanyAPIConfig, Transport
from repo_inventory import DEFAULT_EXTENSIONS, iter_source_files, parse_extensions
from run_agent import run_investigation
from structural_index import build_structural_index


ARTIFACT_NAMES = (
    "structural-index.sqlite", "diagnosis.json", "diagnosis.md",
    "programs.json", "agent-result.json", "agent-result.md",
)


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
    for name in (*ARTIFACT_NAMES, "structural-index.sqlite-wal", "structural-index.sqlite-shm"):
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


def _manifest(source: Path, extensions: frozenset[str], include_extensionless: bool) -> dict[str, str]:
    return {
        path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in iter_source_files(source, extensions, include_extensionless)
    }


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
    return programs, dependencies, hashes


def _select_entry(programs: list[dict], entry: str | None) -> tuple[dict | None, str | None]:
    if not entry:
        return None, None
    key = entry.strip().replace("\\", "/").casefold()
    if key.startswith("./"):
        key = key[2:]
    # PROGRAM-ID takes precedence over a coincidentally identical filename.
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
    lines = ["# 源码接入诊断", "", f"本次状态：{report['runner_status']}；原因：{report['reason_code']}。", "",
             f"源码目录：{_inline(report['source_root'])}", "",
             f"索引快照：{_inline(build.get('snapshot_id', '尚未生成'))}", "",
             f"候选文件 {files.get('candidate', 0)}，实际读取 {files.get('decoded', 0)}，"
             f"程序 {len(programs)}，更新 {files.get('indexed_or_updated', 0)}，"
             f"移除旧文件 {files.get('removed', 0)}。", "",
             "程序名与路径来自本次指定目录。完整清单见 programs.json。", ""]
    if report.get("selected_entry"):
        entry = report["selected_entry"]
        lines += [f"已选入口：{_inline(entry['program_name'])}，{_inline(entry['relative_path'])}:{entry['start_line']}。", ""]
    lines += ["## 需要处理的事项", ""]
    lines += [f"- {_inline(item)}" for item in report["messages"]] or ["当前没有接入阻断项。"]
    if programs:
        lines += ["", "## 实际程序（最多显示前 50 个）", "", "| PROGRAM-ID | 源文件 | 定义行 |", "| --- | --- | --- |"]
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
) -> dict:
    source, output = _paths(source_root, output_root)
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
                           "encoding": encoding, "source_format": source_format},
        "artifacts": {name: str(output / name) for name in ARTIFACT_NAMES},
        "full_business_analysis_verified": False,
    }
    programs: list[dict] = []
    agent: dict = {"runner_status": "NOT_REQUESTED", "agent_result": None}

    def save() -> None:
        _write(output / "diagnosis.json", report)
        _write(output / "diagnosis.md", _render(report, programs), markdown=True)
        _write(output / "programs.json", {"snapshot_id": (report.get("build_report") or {}).get("snapshot_id"), "programs": programs})
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
        before = _manifest(source, extensions, include_extensionless)
        build = build_structural_index(source, output / "structural-index.sqlite", extensions=extensions,
                                       include_extensionless=include_extensionless, encoding=encoding,
                                       source_format=source_format, quiet=quiet)
        report["build_report"] = build
        programs, dependencies, indexed = _catalog(output / "structural-index.sqlite")
        after = _manifest(source, extensions, include_extensionless)
        if before != after or any(after.get(path) != digest for path, digest in indexed.items()):
            raise ValueError("Source files changed during indexing; rerun against a stable exported directory.")
        report["source_manifest_verified"] = True
        report["atomic_source_snapshot"] = False
        report["unresolved_dependencies"] = dependencies
        report["program_count"] = len(programs)
        if not quiet:
            print(f"已读取 {build['files']['decoded']} 个文件，识别 {len(programs)} 个程序。", file=sys.stderr)
        report["messages"].extend(build.get("diagnostics", {}).get("warnings", []))
        if dependencies:
            report["messages"].append(f"有 {len(dependencies)} 项调用或 COPY 依赖未确认；请把相关源文件放入同一源码根目录，再重跑。动态目标仍需配置证据。")
        if not programs:
            report.update(runner_status="BLOCKED", reason_code="NO_PROGRAMS_RECOGNIZED")
            report["messages"].append("没有识别到 PROGRAM-ID。检查源目录、扩展名、编码和 fixed/free 格式；仅 API 连通不能解决此问题。")
        else:
            selected, entry_error = _select_entry(programs, entry)
            report["selected_entry"] = selected
            if entry_error:
                report.update(runner_status="BLOCKED", reason_code=entry_error)
                report["messages"].append("入口不存在或无法唯一定位。请从 programs.json 复制实际 PROGRAM-ID 或相对文件路径；重复程序版本应分开索引。")
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
            try:
                selected_config = config or CompanyAPIConfig.from_env(**(api_options or {}))
                agent = run_investigation(question.strip(), output / "structural-index.sqlite", selected_config,
                                          entry_program=(report["selected_entry"] or {}).get("program_name"),
                                          allow_network=True, transport=transport)
            except APIConfigurationError as exc:
                agent = {"runner_status": "NOT_READY", "reason_code": exc.code, "agent_result": None}
            result = agent.get("agent_result") or {}
            if agent["runner_status"] == "COMPLETED":
                report["question_status"] = result.get("status", "COMPLETED")
            else:
                report["question_status"] = agent["runner_status"]
                report["messages"].append("源码索引已建立，问答未完成。请查看 agent-result.json 的 reason_code、capability_report 和 stop_reason 定位 API 能力或调查限制。")
            # Catch edits made while the model was investigating the stored snapshot.
            if _manifest(source, extensions, include_extensionless) != after:
                raise ValueError("Source files changed during investigation; rerun before using an answer.")
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
