#!/usr/bin/env python3
"""Offline call-chain, parameter-binding and error-audit demonstration.

This report describes indexed source and potential writer relations, not an
executed business transaction or a model-generated complete business answer.
"""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
import json
from pathlib import Path
import sqlite3
from typing import Sequence
from urllib.parse import quote

from error_paths import ErrorContract, audit_error_paths
from structural_index import build_structural_index


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "complex-business-v2"
MAX_REPORTED_BINDINGS = 1000
MAX_CALL_PATHS = 100
MAX_CALL_DEPTH = 12
MAX_WRITER_NODES = 100
MAX_REPORTED_CALLS = 5000
MAX_CALL_EXPANSIONS = 1000
MAX_WRITER_RELATIONS = 1000
MAX_WRITER_RECORDS = 200
MAX_DYNAMIC_CALLS = 50


def _ref(row: sqlite3.Row) -> dict[str, object]:
    return {key: row[key] for key in ("evidence_id", "relative_path", "start_line", "end_line")}


def _writer_candidates(connection: sqlite3.Connection, symbol_id: str | None) -> dict[str, object]:
    """Walk possible parameter inputs/writebacks; never identify the last write."""
    queue = deque([(symbol_id, 0)]) if symbol_id else deque()
    visited = set()
    writers = {}
    depth_cut = False
    relation_count = 0
    budget_cut = False
    while queue and len(visited) < MAX_WRITER_NODES and not budget_cut:
        current, depth = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        remaining = MAX_WRITER_RELATIONS - relation_count
        rows = list(connection.execute(
            "SELECT r.*, u.program_name, u.name AS statement_name, e.relative_path, "
            "e.start_line, e.end_line FROM relations r JOIN code_units u "
            "ON u.unit_id = r.from_entity_id JOIN evidence_spans e ON e.evidence_id = r.evidence_id "
            "WHERE r.target_entity_id = ? AND r.relation_type IN ('WRITES', 'PASSES_AS', 'MAY_WRITE_BACK') "
            "ORDER BY e.relative_path, e.start_line, r.relation_id LIMIT ?", (current, remaining + 1),
        ))
        if len(rows) > remaining:
            budget_cut = True
        for row in rows[:remaining]:
            relation_count += 1
            if row["relation_type"] == "WRITES":
                if len(writers) >= MAX_WRITER_RECORDS:
                    budget_cut = True
                    break
                tables = [item[0] for item in connection.execute(
                    "SELECT target_name FROM relations WHERE from_entity_id = ? "
                    "AND relation_type = 'SELECTS_FROM' ORDER BY target_name LIMIT 65", (row["from_entity_id"],),
                )]
                inputs = [item[0] for item in connection.execute(
                    "SELECT target_name FROM relations WHERE from_entity_id = ? "
                    "AND relation_type = 'READS' ORDER BY target_name LIMIT 65", (row["from_entity_id"],),
                )]
                writers[row["relation_id"]] = {
                    "program_name": row["program_name"], "statement_name": row["statement_name"],
                    "target_name": row["target_name"], "status": row["status"],
                    "table_names": tables[:64], "input_fields": inputs[:64], "evidence_ref": _ref(row),
                    "details_truncated": len(tables) > 64 or len(inputs) > 64,
                    "parameter_hops": depth,
                }
            elif depth < MAX_CALL_DEPTH:
                fields = list(connection.execute(
                    "SELECT symbol_id FROM symbols WHERE definition_unit_id = ? "
                    "AND symbol_type = 'Field' LIMIT 2", (row["from_entity_id"],),
                ))
                if len(fields) == 1:
                    queue.append((fields[0][0], depth + 1))
            else:
                depth_cut = True
    return {
        "scope": "possible_parameter_connected_writers_not_reaching_definitions",
        "writers": sorted(writers.values(), key=lambda item: (item["evidence_ref"]["relative_path"], item["evidence_ref"]["start_line"])),
        "visited_fields": len(visited), "truncated": bool(queue) or depth_cut or budget_cut,
        "actual_runtime_target_verified": False,
    }


def _call_paths(edges: Sequence[dict[str, object]], root_program: str) -> dict[str, object]:
    by_program = defaultdict(list)
    for edge in edges:
        by_program[edge["caller"]].append(edge)
    pending = deque([([root_program], [])])
    paths = []
    cycles = []
    truncated = False
    expanded = 0
    while pending and len(paths) + len(cycles) < MAX_CALL_PATHS and expanded < MAX_CALL_EXPANSIONS:
        expanded += 1
        programs, refs = pending.popleft()
        children = by_program.get(programs[-1], [])
        if not children:
            paths.append({"programs": programs, "evidence_refs": refs})
        elif len(programs) - 1 >= MAX_CALL_DEPTH:
            paths.append({"programs": programs, "evidence_refs": refs})
            truncated = True
        else:
            for edge in children:
                if len(paths) + len(cycles) >= MAX_CALL_PATHS:
                    truncated = True
                    break
                if edge["callee"] in programs:
                    cycles.append({"programs": [*programs, edge["callee"]], "evidence_refs": [*refs, edge["evidence_ref"]]})
                else:
                    if len(pending) >= MAX_CALL_EXPANSIONS:
                        truncated = True
                        break
                    pending.append(([*programs, edge["callee"]], [*refs, edge["evidence_ref"]]))
    return {
        "scope": "static_call_paths_not_execution_order", "paths": paths, "cycles": cycles,
        "truncated": truncated or bool(pending),
    }


def build_complex_demo(
    source_root: Path, database_path: Path, root_program: str,
    contracts: Sequence[ErrorContract],
) -> dict[str, object]:
    build = build_structural_index(source_root, database_path, quiet=True)
    encoded = quote(database_path.resolve().as_posix(), safe="/:")
    connection = sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        snapshot = connection.execute("SELECT value FROM metadata WHERE key = 'snapshot_id'").fetchone()[0]
        if snapshot != build["snapshot_id"]:
            raise ValueError("Snapshot changed while building the demonstration.")
        programs = [row[0] for row in connection.execute(
            "SELECT name FROM symbols WHERE symbol_type = 'Program' ORDER BY name"
        )]
        if programs.count(root_program) != 1:
            raise ValueError("The root program must resolve uniquely in this snapshot.")
        calls = list(connection.execute(
            "SELECT r.*, u.program_name AS caller, e.relative_path, e.start_line, e.end_line "
            "FROM relations r JOIN code_units u ON u.unit_id = r.from_entity_id "
            "JOIN evidence_spans e ON e.evidence_id = r.evidence_id "
            "WHERE r.relation_type IN ('CALLS', 'CALL_TARGET_FROM') "
            "ORDER BY u.program_name, e.start_line, r.relation_id LIMIT ?", (MAX_REPORTED_CALLS + 1,),
        ))
        if len(calls) > MAX_REPORTED_CALLS:
            raise ValueError("Call graph exceeds the bounded demonstration budget.")
        edges = [{"caller": row["caller"], "callee": row["target_name"], "callsite_id": row["from_entity_id"], "evidence_ref": _ref(row)}
                 for row in calls if row["relation_type"] == "CALLS" and row["status"] == "confirmed"]
        dynamic_rows = [row for row in calls if row["relation_type"] == "CALL_TARGET_FROM"]
        dynamic = [{
            "caller": row["caller"], "target_field": row["target_name"], "evidence_ref": _ref(row),
            "boundary": "runtime_target_requires_value_flow",
            "source_candidates": _writer_candidates(connection, row["target_entity_id"]),
        } for row in dynamic_rows[:MAX_DYNAMIC_CALLS]]
        binding_rows = list(connection.execute(
            "SELECT * FROM call_bindings ORDER BY caller_program, callsite_id, parameter_position, group_member_index LIMIT ?",
            (MAX_REPORTED_BINDINGS + 1,),
        ))
        counts = {row["status"]: row["count"] for row in connection.execute(
            "SELECT status, COUNT(*) AS count FROM call_bindings GROUP BY status"
        )}
        reasons = {row["reason"]: row["count"] for row in connection.execute(
            "SELECT reason, COUNT(*) AS count FROM call_bindings WHERE status != 'confirmed' GROUP BY reason"
        )}
        bindings = []
        for row in binding_rows[:MAX_REPORTED_BINDINGS]:
            binding = dict(row)
            for role in ("caller", "callee"):
                symbol = connection.execute("SELECT name FROM symbols WHERE symbol_id = ?", (row[f"{role}_symbol_id"],)).fetchone()
                binding[f"{role}_field"] = symbol[0] if symbol else None
            bindings.append(binding)
        error_audit = audit_error_paths(database_path, root_program, contracts)
        if error_audit["snapshot_id"] != snapshot:
            raise ValueError("Snapshot changed between call and error audits.")
        return {
            "demo_id": "COMPLEX-BUSINESS-V2", "mode": "offline_source_analysis", "root_program": root_program,
            "snapshot_id": snapshot, "build_report": build, "programs": programs,
            "static_calls": edges, "call_paths": _call_paths(edges, root_program),
            "dynamic_calls": dynamic,
            "dynamic_calls_truncated": len(dynamic_rows) > MAX_DYNAMIC_CALLS,
            "parameter_bindings": {"counts": counts, "boundary_counts": reasons, "records": bindings, "truncated": len(binding_rows) > MAX_REPORTED_BINDINGS},
            "error_audit": error_audit,
            "full_business_analysis_verified": False, "runtime_execution_tested": False,
            "model_called": False, "network_calls": False,
        }
    finally:
        connection.close()


def render_markdown(bundle: dict[str, object]) -> str:
    binding = bundle["parameter_bindings"]
    lines = [
        "# 复杂业务链：实际源码分析结果", "",
        "本报告展示调用链、参数位置与错误路径审查，不代表已经运行 COBOL 或完成真实模型业务回答。", "",
        f"快照：`{bundle['snapshot_id']}`", "",
        f"主程序组含 {len(bundle['programs'])} 个程序；静态调用点 {len(bundle['static_calls'])} 个；动态调用点 {len(bundle['dynamic_calls'])} 个。",
        f"参数及组内成员映射：{binding['counts'].get('confirmed', 0)} 条已确认，{binding['counts'].get('unresolved', 0)} 条未解析。映射成功只证明受支持的参数位置/布局，不证明实际执行或最终值。", "",
        "## 调用链实例", "",
    ]
    paths = sorted(bundle["call_paths"]["paths"], key=lambda item: (-len(item["programs"]), item["programs"]))
    for path in paths[:8]:
        lines.append("- " + " → ".join(path["programs"]))
    if bundle["call_paths"]["cycles"]:
        lines.append("- 检测到循环调用；已保留边界，不无限展开。")
    lines.extend(["", "## 参数传递实例", "", "| 调用方字段 | 被调方字段 | 位置 / 方式 |", "| --- | --- | --- |"])
    records = [item for item in binding["records"] if item["status"] == "confirmed" and item["parameter_position"] is not None and item["group_member_index"] == 0]
    records.sort(key=lambda item: (item["caller_field"] == item["callee_field"], item["passing_mode"] == "REFERENCE", item["caller_program"], item["parameter_position"]))
    for record in records[:8]:
        lines.append(f"| `{record['caller_program']}::{record['caller_field']}` | `{record['callee_program']}::{record['callee_field']}` | {record['parameter_position']} / {record['passing_mode']} |")
    lines.extend(["", "## 配置型调用", ""])
    if bundle["dynamic_calls_truncated"]:
        lines.append("动态调用来源分析达到预算，以下仅是部分调用点。")
    for call in bundle["dynamic_calls"]:
        lines.append(f"`{call['caller']}` 通过 `{call['target_field']}` 发起动态调用，实际目标尚未确认。")
        for writer in call["source_candidates"]["writers"]:
            if writer["table_names"]:
                ref = writer["evidence_ref"]
                lines.append(f"- 可能的配置来源：`{writer['program_name']}` 查询 `{', '.join(writer['table_names'])}`；输入字段 `{', '.join(writer['input_fields'])}`；证据 `{ref['relative_path']}:{ref['start_line']}–{ref['end_line']}`。")
        lines.append("这只是经参数关系连通的候选写入点，不是当前调用的唯一到达定义，也不是已解析的运行配置。")
        if call["source_candidates"]["truncated"] or any(w["details_truncated"] for w in call["source_candidates"]["writers"]):
            lines.append("候选写入或字段明细达到预算，结果已经截断。")
    audit = bundle["error_audit"]
    summary = audit["summary"]
    counts = summary["observations_by_kind"]
    lines.extend(["", "## 错误传播审查", "",
        f"已审查 {summary['programs_audited']} 个程序，找到 {counts.get('error_status_origin', 0)} 处非零状态赋值、{summary['parameter_error_bindings']} 条状态/输出参数对应，以及 {counts.get('sql_error_check', 0)} 处查询错误检查。以下是源码形态或候选关系，不是全路径执行证明。", "",
        "| 程序 | 观察到的处理 | 状态 |", "| --- | --- | --- |",
    ])
    for observation in audit["observations"]:
        kind = observation["kind"]
        if kind == "sql_error_check":
            text = "查询失败分支设置非零状态"
            status = "已找到源码形态" if observation["status"] == "source_shape_observed" else "未找到完整局部分支，需复核"
        elif kind == "delegated_error_output_clear":
            text = f"通过 {observation['callee_program']} 清零 {observation['field']}"
            status = "参数与源码连通的候选"
        elif kind == "call_exception_output_clear":
            text = f"调用异常时直接清零 {observation['field']}"
            status = "已找到异常子句形态"
        elif kind in {"possible_output_overwrite_after_error_clear", "possible_error_status_reset"}:
            text = f"清零后可能覆写或错误状态可能复位：{observation['field']}"
            status = "需复核"
        else:
            continue
        lines.append(f"| `{observation['program_name']}` | {text} | {status} |")
    lines.extend(["", f"另保留 {summary['boundaries']} 项分析边界；完整观察、来源引用及边界保存在配套 JSON 的 error_audit 中。", "", "## 保留的边界", "",
                  "- BY CONTENT/BY VALUE 不生成对调用方的引用回写；BY REFERENCE 只生成可能回写关系。",
                  "- 配置记录、实际输入和执行日志未提供，动态目标和业务金额未核实。",
                  "- 调用链是静态连通路径，不等于一次交易的实际执行顺序；完整控制流、全部错误路径与后续覆写仍以审查边界为准。",
                  "- 未调用模型，未接通界面；问题理解和完整回答质量仍须另行验收。", ""])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=FIXTURE_ROOT / "main")
    parser.add_argument("--profile", type=Path, default=FIXTURE_ROOT / "profile.json")
    parser.add_argument("--database", type=Path, default=Path(".poc-data/complex-v2/structural-index.sqlite"))
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args(argv)
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    contracts = [ErrorContract(item["program_name"], tuple(item["status_fields"]), tuple(item.get("output_fields", ()))) for item in profile["contracts"]]
    bundle = build_complex_demo(args.source, args.database, profile["root_program"], contracts)
    for output, content in ((args.json_output, json.dumps(bundle, ensure_ascii=False, indent=2)), (args.markdown_output, render_markdown(bundle))):
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(content, encoding="utf-8")
    print(json.dumps({"demo_id": bundle["demo_id"], "program_count": len(bundle["programs"]), "parameter_bindings": bundle["parameter_bindings"]["counts"], "error_audit": bundle["error_audit"].get("summary", {}), "full_business_analysis_verified": False, "network_calls": False}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
