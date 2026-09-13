#!/usr/bin/env python3
"""Run local, contract-conditional framework paths and independent case checks."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import sys

from framework_io import validate_runtime_contract
from framework_paths import audit_framework_paths
from framework_projection import digest
from repo_inventory import parse_extensions
from run_project_poc import _md, _validated_paths


MAX_JSON_BYTES = 2_000_000
MAX_CASES = 32


def _load_json(path: Path) -> dict:
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("JSON input exceeds the bounded input size.")
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("JSON input must contain an object.")
    return value


def _case_file(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("Case scenario must be a source-relative JSON path.")
    relative = PurePosixPath(value)
    path = root / value
    if relative.is_absolute() or ".." in relative.parts or path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("Case scenario must remain within its case-set directory.")
    return path


def load_case_set(path: Path) -> list[dict]:
    catalog = _load_json(path)
    if set(catalog) != {"schema_version", "cases"} or catalog["schema_version"] != "1.0":
        raise ValueError("Unsupported case-set schema.")
    cases = catalog["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise ValueError("Case set must contain between 1 and 32 cases.")
    seen = set()
    result = []
    for case in cases:
        if (not isinstance(case, dict) or set(case) - {"id", "entry", "scenario", "initial_values", "output_fields", "expected", "resume_from"}
                or {"id", "entry", "scenario", "expected"} - set(case)):
            raise ValueError("Invalid case fields.")
        if not isinstance(case["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", case["id"]) or case["id"] in seen:
            raise ValueError("Case IDs must be unique simple identifiers.")
        if "resume_from" in case and (not isinstance(case["resume_from"], str) or case["resume_from"] not in seen):
            raise ValueError("Restart cases must name an earlier case.")
        seen.add(case["id"])
        validate_expectation(case["expected"])
        result.append({**case, "scenario": _load_json(_case_file(path.parent.resolve(), case["scenario"]))})
    return result


def validate_expectation(expected: dict) -> None:
    allowed = {"model_path_complete", "values", "committed_records", "pending_writes", "io_actions"}
    if not isinstance(expected, dict) or not expected or set(expected) - allowed:
        raise ValueError("Invalid or empty independent case expectation.")
    if "model_path_complete" not in expected or type(expected["model_path_complete"]) is not bool:
        raise ValueError("Every case must declare whether its model path completes.")
    if "values" in expected and (not isinstance(expected["values"], dict) or not expected["values"]):
        raise ValueError("Expected values must be a non-empty field mapping.")
    for field, value in expected.get("values", {}).items():
        if not isinstance(field, str) or not field or type(value) not in (str, int, type(None)):
            raise ValueError("Expected fields must map non-empty names to text, integers or unknown values.")
    for key in ("committed_records", "pending_writes", "io_actions"):
        if key in expected and not isinstance(expected[key], list):
            raise ValueError(f"Invalid expectation: {key}")
    for key in ("committed_records", "pending_writes"):
        for record in expected.get(key, []):
            if (not isinstance(record, dict) or not record
                    or any(not isinstance(field, str) or not field or type(value) not in (str, int)
                           for field, value in record.items())):
                raise ValueError("Expected records must map non-empty names to text or integers.")
    if any(not isinstance(action, str) or not action for action in expected.get("io_actions", [])):
        raise ValueError("Expected I/O actions must be non-empty strings.")


def _same_value(actual, expected) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(_same_value(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_same_value(left, right) for left, right in zip(actual, expected))
    return actual == expected


def _io_event(step: dict) -> dict | None:
    for key in ("io_event", "io", "contract_event"):
        if isinstance(step.get(key), dict) and "action" in step[key]:
            return step[key]
    if "action" in step and "function_value" in step:
        return step
    return None


def check_case(audit: dict, expected: dict | None) -> list[dict]:
    if expected is None:
        return []
    validate_expectation(expected)
    checks = [{"name": "model_path_complete", "expected": expected["model_path_complete"],
               "actual": audit["scope"]["model_path_complete"],
               "passed": audit["scope"]["model_path_complete"] == expected["model_path_complete"]}]
    exits = audit.get("root_exits", [])
    final = exits[0] if len(exits) == 1 else None
    actual_values = final.get("values", {}) if final else {}
    for field, value in expected.get("values", {}).items():
        actual = actual_values.get(field)
        checks.append({"name": "value:" + field, "expected": value, "actual": actual,
                       "passed": final is not None and field in actual_values and type(actual) is type(value) and actual == value})
    for key in ("committed_records", "pending_writes"):
        if key in expected:
            state = final.get("io_state") if final else audit.get("final_io_state") if not exits else None
            actual = state.get(key) if state else None
            checks.append({"name": key, "expected": expected[key], "actual": actual, "passed": _same_value(actual, expected[key])})
    if "io_actions" in expected:
        actual = [event["action"] for step in audit.get("trace", []) if (event := _io_event(step))]
        checks.append({"name": "io_actions", "expected": expected["io_actions"], "actual": actual,
                       "passed": actual == expected["io_actions"]})
    return checks


def _evidence(references: list[dict]) -> str:
    result = []
    for ref in references:
        for span in ref.get("source_spans", []):
            location = f"{_md(span['relative_path'])}:{span['start_line']}"
            if span["start_line"] != span["end_line"]:
                location += f"–{span['end_line']}"
            chain = span.get("include_chain", [])
            if chain:
                location += "（包含：" + " → ".join(f"{_md(c['relative_path'])}:{c['line']}" for c in chain) + "）"
            if location not in result:
                result.append(location)
    return "; ".join(result) or "无源码位置（模型事件）"


def render_path_report(bundle: dict) -> str:
    lines = ["# 框架控制与数据访问：条件化路径报告", "",
             "本报告沿已读取的控制 COPY、业务程序和子程序推进状态；外部数据行为来自所列版本契约及场景。",
             "", "**这是声明契约下的源码模型结果，不是实际业务程序执行，也不证明所有可能路径。**", "",
             f"契约：{_md(bundle['contract']['contract_id'])} / {_md(bundle['contract']['contract_version'])}。", "",
             f"来源声明：{_md(bundle['contract']['provenance'])}。", "",
             "## 案例结果", "",
             "| 案例 | 入口 | 所选模型路径 | 独立预期检查 | 最终输出 |", "| --- | --- | --- | --- | --- |"]
    for case in bundle["cases"]:
        audit = case["audit"]
        values = audit["root_exits"][0]["values"] if audit.get("root_exits") else "无已证明终态"
        status = "到达终态" if audit["scope"]["model_path_complete"] else "停在边界"
        checks = case["checks"]
        checks_text = "未提供预期" if not checks else f"{sum(c['passed'] for c in checks)}/{len(checks)}"
        lines.append(f"| {_md(case['id'])} | {_md(audit['entry_program'])} | {status} | {checks_text} | {_md(values)} |")
    lines += ["", "## 读取条件与模型政策", "",
              "下列条件由契约声明，不是从真实 DDS、数据库或运行环境自动恢复。", ""]
    for path in bundle["contract"]["access_paths"]:
        lines.append(f"- 访问路径 {_md(path['id'])}：按 {_md(path['key_fields'])} 排序；只返回满足 {_md(path['select'])} 的记录。")
    lines += ["", f"锁政策：{_md(bundle['contract']['locking'])}。", "",
              f"事务政策：{_md(bundle['contract']['transaction'])}。", "",
              f"重启政策：{_md(bundle['contract']['restart'])}。", ""]
    for case in bundle["cases"]:
        audit = case["audit"]
        lines += [f"## {case['id']}：执行步骤与依据", "",
                  f"源码快照：{_md(audit.get('source_snapshot_id'))}；场景哈希：{_md(audit.get('scenario_hash'))}。", "",
                  "调用异常只按明确故障场景选择；普通源程序调用按本次普通进入/返回假设分析。未知条件和不支持的形式不跨越。", "",
                  "| 步骤 | 程序 | 事件或变化 | 原始依据与包含链 |", "| --- | --- | --- | --- |"]
        if case.get("resume_from"):
            lines[-2:-2] = [f"重启输入来自先前模型案例 {_md(case['resume_from'])} 的已提交数据与检查点，不是生产检查点。", ""]
        visible = 0
        for index, step in enumerate(audit.get("trace", []), 1):
            io = _io_event(step)
            event = step.get("event", step.get("kind", "step"))
            if io:
                details = {k: io[k] for k in ("function_value", "action", "record_key", "status", "detail", "committed_write_count") if k in io}
            elif step.get("writes"):
                details = {"event": event, "writes": step["writes"]}
            elif event in {"if", "condition", "loop_test", "call_enter", "call_return", "root_exit", "root_return", "boundary"}:
                details = {k: v for k, v in step.items() if k in {"event", "kind", "field", "field_value", "operator", "value", "compared_to", "outcome", "target", "callee_program", "parameters", "bindings", "passed_values", "reason"}}
            else:
                continue
            visible += 1
            lines.append(f"| {index} | {_md(step.get('program_name', ''))} | {_md(details)} | {_evidence(step.get('evidence_refs', []))} |")
        if not visible:
            lines.append("| — | — | 无可展示的已执行模型步骤 | — |")
        lines.append("")
        exits = audit.get("root_exits", [])
        states = exits or [{"io_state": audit.get("final_io_state")}]
        for final in states:
            state = final.get("io_state")
            if state:
                label = "最终持久记录（模型）：" if exits else "边界处已知持久记录（模型；未证明后续结果）："
                lines += [label, "", "| 记录 | 最终字段 |", "| --- | --- |"]
                for index, record in enumerate(state["committed_records"], 1):
                    lines.append(f"| {index} | {_md(record)} |")
                lines += ["", f"尚未提交的写入：{_md(state['pending_writes'])}。", "",
                          f"检查点：{_md(state.get('restart_checkpoint'))}。", ""]
        for boundary in audit.get("boundaries", []):
            lines += [f"- 边界 {_md(boundary['reason'])}：{_evidence(boundary.get('evidence_refs', []))}"]
        for check in case["checks"]:
            if not check["passed"]:
                lines += [f"- 预期不符 {_md(check['name'])}：预期 {_md(check['expected'])}；实际 {_md(check['actual'])}。"]
        lines.append("")
    lines += ["## 使用边界", "",
              "源文件哈希、派生快照和 COPY 包含链用于本地复核，不等于生产运行认证。缺少控制源码、未解析的功能值、"
              "复杂 JOIN、未知条件、别名或预算耗尽时停止；不模拟公司未提供的框架细节。", "",
              "本次没有网络请求、模型调用、真实文件更新、数据库事务或目标 COBOL 执行。报告可能包含敏感记录，须留在批准环境。", ""]
    return "\n".join(lines)


def build_path_bundle(source: Path, output: Path, contract: dict, cases: list[dict], *, extensions=None, max_steps=2000) -> dict:
    source, output = _validated_paths(source, output)
    contract = validate_runtime_contract(contract)
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise ValueError("Invalid number of cases.")
    seen_ids = set()
    for case in cases:
        if not isinstance(case, dict) or not {"id", "entry", "scenario"} <= set(case):
            raise ValueError("Invalid case.")
        if not isinstance(case["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", case["id"]) or case["id"] in seen_ids:
            raise ValueError("Case IDs must be unique simple identifiers.")
        if "resume_from" in case and (not isinstance(case["resume_from"], str) or case["resume_from"] not in seen_ids):
            raise ValueError("Restart cases must name an earlier case.")
        seen_ids.add(case["id"])
        if case.get("expected") is not None:
            validate_expectation(case["expected"])
    output.mkdir(parents=True, exist_ok=True)
    identity = (output.stat().st_dev, output.stat().st_ino)

    def verify_output():
        current = output.stat()
        if output.resolve() != output or output.is_symlink() or (current.st_dev, current.st_ino) != identity:
            raise ValueError("Output directory changed during analysis.")

    results = []
    for case in cases:
        verify_output()
        scenario = deepcopy(case["scenario"])
        if "resume_from" in case:
            previous = next(item for item in results if item["id"] == case["resume_from"])
            exits = previous["audit"].get("root_exits", [])
            state = exits[0].get("io_state") if len(exits) == 1 else None
            if not state or not state.get("restart_checkpoint"):
                raise ValueError("Restart source case has no completed model exit with a checkpoint.")
            if state.get("pending_writes"):
                raise ValueError("Cannot resume from a prior case with unresolved pending writes.")
            scenario["records"] = deepcopy(state["committed_records"])
            scenario["restart_checkpoint"] = deepcopy(state["restart_checkpoint"])
        kwargs = {"initial_values": case.get("initial_values"), "output_fields": case.get("output_fields"),
                  "scratch_root": output, "max_steps": max_steps}
        if extensions is not None:
            kwargs["extensions"] = extensions
        audit = audit_framework_paths(source, case["entry"], contract, scenario, **kwargs)
        if results and audit["source_snapshot_id"] != results[0]["audit"]["source_snapshot_id"]:
            raise ValueError("Source changed between cases; do not combine results from different snapshots.")
        results.append({"id": case["id"], "resume_from": case.get("resume_from"),
                        "audit": audit, "checks": check_case(audit, case.get("expected"))})
    bundle = {"schema_version": "1.0", "generated_at_utc": datetime.now(timezone.utc).isoformat(),
              "contract": contract, "contract_hash": digest(contract), "cases": results,
              "runtime_verified": False, "full_business_analysis_verified": False,
              "network_calls": False, "question_answered": False,
              "summary": {"cases": len(results), "completed_model_paths": sum(c['audit']['scope']['model_path_complete'] for c in results),
                          "checks": sum(len(c['checks']) for c in results),
                          "failed_checks": sum(not check['passed'] for c in results for check in c['checks'])}}
    verify_output()
    with (output / "framework-paths.json").open("x", encoding="utf-8") as handle:
        json.dump(bundle, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    verify_output()
    with (output / "framework-paths.md").open("x", encoding="utf-8") as handle:
        handle.write(render_path_report(bundle))
    return bundle


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Offline, contract-conditional source paths; does not run business programs.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--case-set", type=Path)
    choice.add_argument("--scenario", type=Path)
    parser.add_argument("--entry")
    parser.add_argument("--initial-values", type=Path)
    parser.add_argument("--output-fields", help="Comma-separated root fields to report.")
    parser.add_argument("--max-steps", type=int, default=2000)
    parser.add_argument("--extensions")
    args = parser.parse_args(argv)
    try:
        if args.case_set:
            if args.entry or args.initial_values or args.output_fields:
                raise ValueError("Case set already declares entry, initial values and output fields.")
            cases = load_case_set(args.case_set)
        else:
            if not args.entry:
                raise ValueError("A single scenario requires --entry.")
            cases = [{"id": "selected-scenario", "entry": args.entry, "scenario": _load_json(args.scenario),
                      "initial_values": _load_json(args.initial_values) if args.initial_values else None,
                      "output_fields": args.output_fields.split(",") if args.output_fields else None}]
        bundle = build_path_bundle(args.source, args.output, _load_json(args.contract), cases,
                                   extensions=parse_extensions(args.extensions), max_steps=args.max_steps)
        success = not bundle["summary"]["failed_checks"]
        print(json.dumps({"runner_status": "COMPLETED" if success else "EXPECTATION_FAILED",
                          "summary": bundle["summary"], "runtime_verified": False,
                          "report": str(args.output.resolve() / "framework-paths.md")}, ensure_ascii=False, indent=2))
        return 0 if success else 1
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"runner_status": "FAILED", "error": str(exc), "runtime_verified": False,
                          "note": "Partial output is retained. Correct the issue and use a new output directory."}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
