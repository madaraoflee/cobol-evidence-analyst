#!/usr/bin/env python3
"""Create a local, source-configurable framework evidence bundle."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from framework_audit import audit_framework
from repo_inventory import DEFAULT_EXTENSIONS, build_inventory, parse_extensions
from structural_index import build_structural_index


RUNNER_SCHEMA_VERSION = "1.0"


def _validated_paths(source_root: Path, output_root: Path) -> tuple[Path, Path]:
    source = Path(source_root).expanduser().resolve()
    output_input = Path(output_root).expanduser()
    if not source.is_dir():
        raise ValueError("Source must be an existing directory.")
    if output_input.is_symlink():
        raise ValueError("Output must not be a symbolic link.")
    output = output_input.resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("Source and output must be separate, non-nested directories.")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Output must be a new directory or an existing empty directory.")
    return source, output


def _verify_source_manifest(source: Path, database: Path, audit: dict) -> None:
    """Bind observed references to the raw index and check for intervening edits."""
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        indexed = dict(connection.execute("SELECT relative_path, sha256 FROM source_files"))
    audited = {item["relative_path"]: item["source_hash"] for item in audit.get("source_files", [])}
    if indexed != audited:
        raise ValueError("Source changed or audit/index file scopes disagree; use a fresh output directory.")
    for relative_path, expected_hash in indexed.items():
        path = source / relative_path
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(source):
            raise ValueError("An indexed source was removed or redirected during analysis.")
        if any(parent.is_symlink() for parent in path.parents if parent != source and source in parent.parents):
            raise ValueError("An indexed source directory was redirected during analysis.")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise ValueError("Source changed during analysis; use a fresh output directory.")


def _md(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return (text.replace("\\", "\\\\").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;")
            .replace("|", "\\|").replace("`", "\\`")
            .replace("[", "\\[").replace("]", "\\]")
            .replace("\n", " ").replace("\r", " "))


def _location(reference: dict) -> str:
    origin = reference.get("origin", reference)
    return f"{_md(origin.get('relative_path', '?'))}:{_md(origin.get('line', '?'))}"


def _references(references: list) -> str:
    rendered = []
    for reference in references:
        text = _location(reference)
        chain = reference.get("include_chain", [])
        if chain:
            text += "（包含位置：" + " → ".join(_location(item) for item in chain) + "）"
        rendered.append(text)
    return "; ".join(rendered) or "无可用位置"


def _declared_contract_lines(contracts: dict) -> list[str]:
    lines = []
    entry = contracts.get("entry")
    if entry:
        lines += [f"入口类型声明：{_md(entry.get('mode'))}；控制 COPY：{_md(entry.get('control_copy'))}。", "",
                  "| 过程 | 配置声明的职责 | 发现定义 | 发现 PERFORM |", "| --- | --- | --- | --- |"]
        for section in entry.get("sections", []):
            lines.append(f"| {_md(section.get('name'))} | {_md(section.get('role'))} | "
                         f"{_md(section.get('definition_observed', False))} | {_md(section.get('perform_observed', False))} |")
        lines.append("")
    else:
        lines += ["当前入口没有精确匹配的配置声明。", ""]
    for contract in contracts.get("io_contracts", []):
        lines += [f"I/O 程序 {_md(contract.get('program'))}：功能字段 {_md(contract.get('function_field'))}；"
                  f"I/O 状态字段 {_md(contract.get('status_field'))}。以下功能意义仍是配置声明。", "",
                  "| 功能值 | 声明的意义 | 声明会读取记录 |", "| --- | --- | --- |"]
        for operation in contract.get("operations", []):
            lines.append(f"| {_md(operation.get('value'))} | {_md(operation.get('meaning'))} | {_md(operation.get('reads_record'))} |")
        lines.append("")
    for decision in contracts.get("record_decisions", []):
        lines += [f"记录处理决定字段 {_md(decision.get('field'))}（{_md(decision.get('program'))}），与 I/O 状态分开：", "",
                  "| 控制值 | 声明的意义 |", "| --- | --- |"]
        for value in decision.get("values", []):
            lines.append(f"| {_md(value.get('value'))} | {_md(value.get('meaning'))} |")
        lines.append("")
    requirements = contracts.get("artifact_requirements", [])
    if requirements:
        lines += ["| 依赖资料类型 | 源码相对位置 | 可用状态 |", "| --- | --- | --- |"]
        for requirement in requirements:
            lines.append(f"| {_md(requirement.get('kind'))} | {_md(requirement.get('relative_path'))} | {_md(requirement.get('status', 'unknown'))} |")
        lines.append("")
    return lines


def _boundary_text(boundary: dict) -> str:
    details = [_md(boundary["detail"])] if boundary.get("detail") else []
    for key, label in (("target", "目标"), ("copy_name", "COPY"), ("relative_path", "文件"), ("status", "状态")):
        if boundary.get(key):
            details.append(f"{label} {_md(boundary[key])}")
    references = boundary.get("references", [])
    if boundary.get("origin"):
        references = [*references, boundary]
    if references:
        details.append(_references(references))
    return "；".join(details) or "此项不在当前已证明范围内。"


def render_framework_report(audit: dict, question: str | None = None) -> str:
    """Render source observations separately from declared or unknown semantics."""
    scope = audit.get("scope", {})
    observations = audit.get("observations", {})
    profile = audit.get("profile")
    lines = [
        "# 框架源码审查",
        "",
        "已生成源码观察结果；这不是完整业务分析验收，也没有运行目标程序。",
        "",
        f"入口：{_md(audit.get('entry_program', ''))}。",
        "",
        f"记录的业务问题：{_md(question) if question else '未提供'}",
        "",
        "问题仅被记录，本次离线审查未回答该问题。",
        "",
        "## 范围与证据",
        "",
        "原始结构索引与展开后的框架观察是两种独立视图。现有查询工具和控制流引擎仍读取原始索引，未自动接入过程 COPY 展开结果。",
        "",
        f"有限语法范围内的源码展开完成：{_md(scope.get('source_expansion_complete', False))}；"
        "这不等于编译器等价、控制流完整或运行时验证。",
        "",
        "报告中的源文件位置及包含链指向实际读取的源码；源文件哈希保存在 JSON 中，可与本地原始索引交叉核对。",
        "",
        "## 观察到的过程调用",
        "",
        "以下仅表示源码中存在调用表达式，不表示该分支一定执行或其运行顺序已经证明。",
        "",
    ]
    performs = observations.get("performs", [])
    if performs:
        lines += ["| 目标 | 定义匹配 | 调用源码与包含链 | 目标定义 |", "| --- | --- | --- | --- |"]
        for item in performs:
            lines.append(f"| {_md(item.get('target', ''))} | {_md(item.get('target_status', 'unknown'))} | "
                         f"{_references(item.get('references', []))} | {_references(item.get('target_definitions', []))} |")
    else:
        lines.append("没有提取到可报告的 PERFORM 观察；不能据此断言不存在框架控制。")
    lines += ["", "## 观察到的跨程序调用", ""]
    calls = observations.get("calls", [])
    if calls:
        lines += ["| 目标 | 目标形式 | 调用时功能值 | 源码与包含链 |", "| --- | --- | --- | --- |"]
        for item in calls:
            lines.append(f"| {_md(item.get('target', ''))} | {_md(item.get('target_kind', 'unknown'))} | "
                         f"{_md(item.get('function_value_at_call', 'not_resolved'))} | {_references(item.get('references', []))} |")
        lines += ["", "调用匹配某个 I/O 配置，不等于已证明到达该调用的功能值、游标共享、锁或事务行为。"]
    else:
        lines.append("没有提取到可报告的跨程序调用。")
    lines += ["", "## 配置声明（未验证）", ""]
    if profile:
        provenance = profile.get("provenance", {})
        lines += [f"配置：{_md(profile.get('profile_id', ''))}，版本：{_md(profile.get('profile_version', ''))}。", "",
                  f"来源类别声明：{_md(provenance.get('kind', 'unknown'))}；适用版本声明：{_md(provenance.get('target_version', 'unknown'))}。", "",
                  f"来源说明：{_md(provenance.get('reference', ''))}。", ""]
    else:
        lines += ["未提供框架配置；不按入口名、段号或函数名猜测框架语义。", ""]
    lines += _declared_contract_lines(audit.get("declared_contracts", {}))
    lines += ["## 尚未闭合的边界", ""]
    boundaries = audit.get("boundaries", [])
    if boundaries:
        for boundary in boundaries:
            lines.append(f"- {_md(boundary.get('reason', 'unknown'))}：{_boundary_text(boundary)}")
    else:
        lines.append("本次有限观察未报告额外边界；这仍不代表完整业务效果已验证。")
    lines += ["", "本次未调用外部模型或网络，也未编译、运行或修改原始业务源码。本地 SQLite 保存源码，请将整个输出目录按源码同级保管。", ""]
    return "\n".join(lines)


def build_project_poc(
    source_root: Path,
    output_root: Path,
    entry_program: str,
    *,
    question: str | None = None,
    profile: dict | None = None,
    extensions: frozenset[str] = DEFAULT_EXTENSIONS,
) -> dict:
    source, output = _validated_paths(source_root, output_root)
    if not isinstance(entry_program, str) or not entry_program.strip():
        raise ValueError("An entry program is required.")
    if profile is not None and not isinstance(profile, dict):
        raise ValueError("Profile must be a JSON object.")
    # Audit first, so a malformed profile does not create partial output.
    audit = audit_framework(source, entry_program.strip(), profile, extensions=extensions)
    inventory = build_inventory(source, extensions=extensions, include_extensionless=True, quiet=True)
    # Recheck immediately before writing; never intentionally replace prior work.
    _validated_paths(source, output)
    output.mkdir(parents=True, exist_ok=True)
    if output.resolve() != output or output.is_symlink():
        raise ValueError("Output was redirected before reserving the database.")
    output_stat = output.stat()
    output_identity = (output_stat.st_dev, output_stat.st_ino)
    database = output / "structural-index.sqlite"
    with database.open("xb"):
        pass
    current_output_stat = output.stat()
    if (output.resolve() != output
            or (current_output_stat.st_dev, current_output_stat.st_ino) != output_identity
            or database.is_symlink()):
        raise ValueError("Output was redirected while reserving the database.")
    build_report = build_structural_index(source, database, extensions=extensions,
                                          include_extensionless=True, quiet=True)
    if inventory["snapshot"]["snapshot_id"] != build_report["snapshot_id"]:
        raise ValueError("Source changed between inventory and indexing; use a fresh output directory.")
    _verify_source_manifest(source, database, audit)
    final_inventory = build_inventory(source, extensions=extensions, include_extensionless=True, quiet=True)
    if final_inventory["snapshot"]["snapshot_id"] != build_report["snapshot_id"]:
        raise ValueError("Source file set changed after indexing; use a fresh output directory.")
    bundle = {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "mode": "offline_framework_source_audit",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "entry_program": entry_program.strip(),
        "question": question,
        "question_status": "RECORDED_NOT_ANSWERED",
        "privacy": {"network_calls": False, "model_calls": 0,
                    "target_program_executed": False, "source_modified": False,
                    "local_index_contains_source": True},
        "provenance": {"index_scope": "raw_source", "framework_scope": "derived_source_observations",
                       "derived_observations_used_by_existing_query_or_cfg_tools": False,
                       "audit_files_match_raw_index": True, "indexed_files_rechecked": True,
                       "atomic_source_snapshot": False},
        "selection": {"extensions": sorted(extensions), "include_extensionless": True},
        "build_report": build_report,
        "inventory": inventory,
        "audit": audit,
        "artifacts": {"database": str(database), "json_report": str(output / "framework-report.json"),
                      "markdown_report": str(output / "framework-report.md"), "inventory": str(output / "inventory.json")},
    }
    # Exclusive creation also protects report files from late output collisions.
    for filename, data in (("inventory.json", inventory), ("framework-report.json", bundle)):
        with (output / filename).open("x", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    with (output / "framework-report.md").open("x", encoding="utf-8") as handle:
        handle.write(render_framework_report(audit, question))
    return bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build an offline source/configuration audit; no model or target-program execution.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--entry", required=True)
    parser.add_argument("--question", help="Record a business question; this audit does not answer it.")
    parser.add_argument("--profile", type=Path, help="Optional local framework-contract JSON object.")
    parser.add_argument("--extensions", help="Comma-separated suffixes; extensionless sources are always included.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        profile = None
        if args.profile:
            with args.profile.expanduser().open(encoding="utf-8") as handle:
                profile = json.load(handle)
        bundle = build_project_poc(args.source, args.output, args.entry, question=args.question,
                                   profile=profile, extensions=parse_extensions(args.extensions))
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"runner_status": "FAILED", "error": str(exc),
                          "reports_complete": False, "question_answered": False,
                          "note": "Any partial output is retained; use a fresh output directory after correcting the error."},
                         ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    print(json.dumps({"runner_status": "COMPLETED", "question_answered": False,
                      "full_business_analysis_verified": False,
                      "artifacts": bundle["artifacts"], "summary": bundle["audit"].get("summary", {})},
                     ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
