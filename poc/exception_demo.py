#!/usr/bin/env python3
"""Offline acceptance of explicitly selected local exceptional-path models."""

from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3
from typing import Sequence
from urllib.parse import quote

from call_contexts import audit_call_contexts
from error_paths import ErrorContract
from exception_paths import audit_exception_paths
from structural_index import build_structural_index


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "exception-flow-v4"


def build_exception_demo(source_root: Path, database_path: Path, profile: dict) -> dict:
    contracts = profile["contracts"]
    expectations = profile["local_exceptional_expectations"]
    if not 1 <= len(contracts) <= 32 or not 1 <= len(expectations) <= 128:
        raise ValueError("Exceptional acceptance profile exceeds its bounded scope.")
    if len({c["program_name"] for c in contracts}) != len(contracts):
        raise ValueError("Exceptional acceptance contracts must have unique programs.")
    build = build_structural_index(source_root, database_path, quiet=True)
    contexts = audit_call_contexts(database_path, profile["root_program"])
    audits = [audit_exception_paths(database_path, c["program_name"], ErrorContract(
        c["program_name"], tuple(c["status_fields"]), tuple(c["output_fields"]))) for c in contracts]
    if any(r["snapshot_id"] != build["snapshot_id"] for r in [contexts, *audits]):
        raise ValueError("Snapshot changed during exceptional-path acceptance.")
    path = Path(database_path).resolve()
    connection = sqlite3.connect(f"file:{quote(path.as_posix(), safe='/')}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        if connection.execute("SELECT value FROM metadata WHERE key='snapshot_id'").fetchone()[0] != build["snapshot_id"]:
            raise ValueError("Snapshot changed before source-anchor verification.")
        by_program = {a["program_name"]: a for a in audits}
        checks = []
        if "expected_context_counts" in profile:
            expected = profile["expected_context_counts"]
            actual = dict(Counter(c["program_name"] for c in contexts["contexts"]))
            checks.append({"expectation_id": "STATIC-CONTEXT-COUNTS",
                           "status": "PASS" if expected == actual and not contexts["summary"]["truncated"] else "FAIL",
                           "expected": expected, "actual": actual})
        for expected in expectations:
            audit = by_program.get(expected["program_name"])
            matches = []
            if audit:
                nodes = {n["node_id"]: n for n in audit["cfg"]["nodes"]}
                for event in audit["events"]:
                    node = nodes[event["node_id"]]
                    if event["event_kind"] != expected["event_kind"] or node["kind"] != expected["anchor"]["statement_name"]:
                        continue
                    row = connection.execute("SELECT normalized_text,relative_path FROM code_units WHERE unit_id=?",
                                             (node["statement_id"],)).fetchone()
                    if row and row[1] == expected["anchor"]["relative_path"] and re.sub(
                            r"\s+", " ", expected["anchor"]["source_text_contains"].strip()).upper() in row[0].upper():
                        matches.append(event)
            desired = {"zero_on_all_modeled_exits": "zero_on_all_modeled_exits",
                       "nonzero_exit_possible": "nonzero_exit_possible_in_model"}.get(expected["expected"])
            if desired is None:
                raise ValueError("Unsupported local exceptional expectation.")
            event = matches[0] if len(matches) == 1 else None
            finding = event["outputs"].get(expected["output_field"], {}).get("finding") if event else None
            statuses = event["status_values"].get(expected["status_field"], []) if event else []
            expected_status = str(Decimal(str(expected["status_value"])).normalize())
            passed = bool(event and finding == desired and statuses == [expected_status]
                          and not audit["summary"]["truncated"])
            checks.append({"expectation_id": expected["expectation_id"],
                           "status": "PASS" if passed else "FAIL", "program_name": expected["program_name"],
                           "event_kind": expected["event_kind"], "matched_events": len(matches),
                           "expected_finding": desired, "actual_finding": finding,
                           "expected_status": expected_status, "actual_status_values": statuses,
                           "event_id": event["event_id"] if event else None,
                           "evidence_refs": event["evidence_refs"] if event else []})
    finally:
        connection.close()
    context_refs = []
    refs_truncated = False
    for context in contexts["contexts"]:
        for index, audit in enumerate(audits):
            if audit["program_name"] != context["program_name"]:
                continue
            for event in audit["events"]:
                if len(context_refs) >= 2000:
                    refs_truncated = True
                    break
                context_refs.append({"context_id": context["context_id"],
                    "program_name": context["program_name"], "event_id": event["event_id"],
                    "program_audit_index": index,
                    "local_model_scope": "independent_local_analysis_not_interprogram_execution"})
    failed = sum(c["status"] == "FAIL" for c in checks)
    return {"demo_id": "EXCEPTION-FLOW-V4", "snapshot_id": build["snapshot_id"],
            "context_audit": contexts, "program_audits": audits,
            "context_event_refs": context_refs, "context_event_refs_truncated": refs_truncated,
            "local_exception_acceptance": {"status": "PASS" if not failed else "FAIL", "checks": checks,
                "passed": len(checks) - failed, "failed": failed,
                "evaluation_scope": "explicit_fixture_local_abstract_path_expectations",
                "business_acceptance_passed": False},
            "full_business_analysis_verified": False, "runtime_execution_tested": False,
            "model_called": False, "network_calls": False}


def render_markdown(bundle: dict) -> str:
    acceptance = bundle["local_exception_acceptance"]
    lines = ["# 调用异常与计算溢出：路径模型验证", "",
             f"本地模型预期验收：{acceptance['status']}，{acceptance['passed']} 项通过、{acceptance['failed']} 项失败。",
             "这里的 PASS 只表示结果符合显式源码预期，包括检出故意出错的反例；不代表业务程序正确或已经执行 COBOL。", "",
             f"快照：`{bundle['snapshot_id']}`", "",
             "## 所选异常分支的实际分析结果", "",
             "| 程序 | 分支 | 退出状态 | 输出判断 | 验收 |", "| --- | --- | --- | --- | --- |"]
    meanings = {"zero_on_all_modeled_exits": "该事件的全部已建模退出均为零",
                "nonzero_exit_possible_in_model": "存在非零退出的模型路径",
                "unproven": "证据或支持范围不足", "not_reached_in_model": "模型未到达该事件"}
    for check in acceptance["checks"]:
        if "program_name" not in check:
            continue
        event = "调用异常" if check["event_kind"] == "exception" else "计算溢出"
        status = ", ".join("未知" if value is None else value for value in check["actual_status_values"])
        lines.append(f"| `{check['program_name']}` | {event} | {status or '未确认'} | {meanings.get(check['actual_finding'], '事件锚点缺失或不唯一')} | {check['status']} |")
    lines.extend(["", "## 可逐步复核的模型路径", "",
                  "每条路径从当前程序入口开始；正常返回不会执行调用失败处理，溢出与非溢出分支也互斥。以下节点均可在配套 JSON 找到来源行号。", ""])
    shown = 0
    for audit in bundle["program_audits"]:
        selected_events = {c.get("event_id") for c in acceptance["checks"] if c.get("program_name") == audit["program_name"]}
        nodes = {n["node_id"]: n for n in audit["cfg"]["nodes"]}
        for witness in audit["witnesses"]:
            if witness["event_id"] not in selected_events or shown >= 8:
                continue
            labels = []
            for edge in witness["edges"]:
                node = nodes[edge["source"]]
                if node["kind"] == "MOVE":
                    labels.append(f"MOVE {node['source']} TO {' '.join(node['targets'])}")
                elif node["kind"] in {"CALL", "COMPUTE", "IF"}:
                    labels.append(f"{node['kind']} [{edge['outcome']}]")
            lines.append(f"- `{audit['program_name']}`：" + " → ".join(labels) + " → 程序退出。")
            shown += 1
    boundary_count = sum(len(a["boundaries"]) for a in bundle["program_audits"])
    if any(a["summary"]["witnesses_truncated"] for a in bundle["program_audits"]):
        lines.extend(["", "部分模型路径示例达到展示预算；输出判断仍基于完整的有界探索，不能只凭已展示的路径判断。"])
    lines.extend(["", "## 上下文与证明范围", "",
        f"{bundle['context_audit']['summary']['contexts']} 个静态调用上下文保留 {len(bundle['context_event_refs'])} 个本地事件引用；这些引用不是跨程序执行记录。同一局部结果会在不同上下文下引用，不表示错误实际发生多次。",
        f"本次局部路径审查保留 {boundary_count} 项边界，逐项原因及截断状态见 JSON。正常 CALL 返回只把引用参数视为未知，不执行被调用程序；算术正常结果也保持未知。", "",
        "CALL 异常不是子程序业务错误的通用捕获。当前只支持显式 END-IF/END-CALL/END-COMPUTE 等受限结构；隐式句点闭合、循环、SQL、EVALUATE、别名与共享状态等不支持情况会停止于边界。",
        "尚未证明完整业务路径、实际异常可达性、金额精度或最终答案质量；全部业务案例仍未执行。", ""])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=FIXTURE_ROOT / "main")
    parser.add_argument("--profile", type=Path, default=FIXTURE_ROOT / "profile.json")
    parser.add_argument("--database", type=Path, default=Path(".poc-data/exception-v4/structural-index.sqlite"))
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args(argv)
    if args.profile.stat().st_size > 256000:
        raise ValueError("Exceptional acceptance profile exceeds its size budget.")
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    bundle = build_exception_demo(args.source, args.database, profile)
    for output, content in ((args.json_output, json.dumps(bundle, ensure_ascii=False, indent=2)),
                            (args.markdown_output, render_markdown(bundle))):
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(content, encoding="utf-8")
    acceptance = bundle["local_exception_acceptance"]
    print(json.dumps({"demo_id": bundle["demo_id"], "local_exception_acceptance": acceptance["status"],
                      "passed": acceptance["passed"], "failed": acceptance["failed"],
                      "contexts": bundle["context_audit"]["summary"]["contexts"],
                      "full_business_analysis_verified": False, "runtime_execution_tested": False,
                      "network_calls": False}, ensure_ascii=False, indent=2))
    return 0 if acceptance["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
