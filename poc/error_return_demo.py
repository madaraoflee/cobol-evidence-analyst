#!/usr/bin/env python3
"""Offline, explicit-fixture acceptance of cross-program business error returns."""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import shutil
import tempfile
from typing import Sequence

from error_paths import ErrorContract
from interprogram_paths import audit_interprogram_paths
from structural_index import build_structural_index


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "error-return-v5"


def _value(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Expected values must be decimal strings, integers or explicit unknowns.")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("Invalid expected numeric value.") from error
    if not number.is_finite() or len(str(value)) > 64:
        raise ValueError("Expected values must be bounded finite decimals.")
    return str(number.normalize())


def _values(exit_record: dict) -> dict:
    return {**exit_record["output_values"], **exit_record["status_values"]}


def _matches(values: dict, expected: dict) -> bool:
    return all(name in values and _value(values[name]) == _value(value)
               for name, value in expected.items())


def _sets(values) -> list:
    return sorted({_value(value) for value in values}, key=lambda value: (value is None, value or ""))


def _check(check_id: str, passed: bool, **details) -> dict:
    return {"check_id": check_id, "status": "PASS" if passed else "FAIL", **details}


def _parameter(step: dict, caller_field: str, callee_field: str, value, mode: str, written_back: bool) -> bool:
    return any(parameter["caller_field"] == caller_field and parameter["callee_field"] == callee_field
               and parameter["passing_mode"] == mode and parameter["written_back"] is written_back
               and _value(parameter["value"]) == _value(value) for parameter in step.get("parameters", []))


def _source_name(field) -> str:
    return field["name"] if isinstance(field, dict) else field


def _return_checks(audit: dict, case: dict, closed: bool) -> list[dict]:
    contexts = {context["context_id"]: context for context in audit["contexts"]}
    checks = []
    for chain in case.get("expected_return_chains", []):
        matched = []
        for event in audit["events"]:
            if (event["event_kind"] != "business_error_return" or event["program_name"] != chain["origin_program"]
                    or event.get("status_field") != chain["origin_status_field"]
                    or _value(event.get("status_value")) != _value(chain["status_value"])):
                continue
            context = contexts[event["context_id"]]
            parent = contexts.get(context["parent_context_id"])
            if parent and context["program_chain"] == chain["program_chain"] and any(
                    _source_name(mapping["caller_field"]) == chain["entry_input_field"]
                    for mapping in parent["parameter_mappings"]):
                matched.append(event)
        event = matched[0] if len(matched) == 1 else None
        selected_witness = None
        selected_steps = []
        if event:
            leaf_context = contexts[event["context_id"]]
            parent_id = leaf_context["parent_context_id"]
            for witness in audit["witnesses"]:
                if event["event_id"] not in witness["event_ids"]:
                    continue
                trace = witness["trace"]
                indices = []
                predicates = [
                    lambda step: step.get("kind") == "call_return" and step.get("callee_context_id") == event["context_id"]
                        and _parameter(step, chain["return_to_status_field"], chain["origin_status_field"], chain["status_value"], "REFERENCE", True),
                    lambda step: step.get("kind") == "call_return" and step.get("callee_context_id") == parent_id
                        and _parameter(step, chain["entry_work_status_field"], chain["wrapper_status_field"], chain["status_value"], "REFERENCE", True),
                    lambda step: step.get("kind") == "call_enter" and step.get("callee_program") == chain["finalizer_program"]
                        and _parameter(step, chain["entry_work_status_field"], chain["finalizer_status_input"], chain["status_value"], "CONTENT", False),
                    lambda step: step.get("kind") == "call_return" and step.get("callee_program") == chain["finalizer_program"]
                        and _parameter(step, chain["expected_root_status_field"], chain["finalizer_status_output"], chain["status_value"], "REFERENCE", True),
                    lambda step: step.get("kind") == "call_enter" and step.get("callee_program") == chain["join_program"]
                        and _parameter(step, chain["expected_root_status_field"], chain["join_input_status_field"], chain["status_value"], "CONTENT", False),
                    lambda step: step.get("kind") == "call_return" and step.get("callee_program") == chain["join_program"]
                        and _parameter(step, chain["expected_root_summary_status_field"], chain["join_status_field"], chain["expected_model_summary_value"], "REFERENCE", True),
                ]
                start = 0
                for predicate in predicates:
                    found = next((index for index in range(start, len(trace)) if predicate(trace[index])), None)
                    if found is None:
                        break
                    indices.append(found)
                    start = found + 1
                expected_root = {chain["expected_root_status_field"]: chain["expected_root_status_value"],
                                 chain["expected_root_summary_status_field"]: chain["expected_model_summary_value"],
                                 **chain["expected_root_outputs"]}
                paired = (len(indices) == 6 and trace[indices[2]]["callee_context_id"] == trace[indices[3]]["callee_context_id"]
                          and trace[indices[4]]["callee_context_id"] == trace[indices[5]]["callee_context_id"])
                if paired and _matches(_values(witness), expected_root):
                    selected_witness = witness["witness_id"]
                    selected_steps = [trace[index] for index in indices]
                    break
        findings_ok = bool(event and all(_value(value) == "0" and event["outputs"].get(name, {}).get("finding")
                                        == "zero_on_all_modeled_exits" for name, value in chain["expected_root_outputs"].items()))
        status_ok = bool(event and _sets(event["status_values"].get(chain["expected_root_status_field"], []))
                         == [_value(chain["expected_root_status_value"])])
        checks.append(_check("RETURN-CHAIN:" + chain["branch"], closed and findings_ok and status_ok
                             and selected_witness is not None, matched_events=len(matched),
                             event_id=event["event_id"] if event else None,
                             context_id=event["context_id"] if event else None,
                             witness_id=selected_witness, verified_steps=selected_steps,
                             expected_status_value=chain["status_value"], expected_root_outputs=chain["expected_root_outputs"]))
    for expected in case.get("expected_local_leaf_returns", []):
        events = [event for event in audit["events"] if event["event_kind"] == "business_error_return"
                  and event["program_name"] == expected["program_name"]
                  and event.get("status_field") == expected["status_field"]
                  and _value(event.get("status_value")) == _value(expected["value"])]
        witness_ids = []
        for event in events:
            leaf_context = contexts[event["context_id"]]
            middle_context = contexts.get(leaf_context["parent_context_id"])
            root_context = contexts.get(middle_context["parent_context_id"]) if middle_context else None
            local_mappings = [mapping for mapping in leaf_context["parameter_mappings"]
                              if _source_name(mapping["callee_field"]) == expected["status_field"]
                              and mapping["passing_mode"] == expected["passing_mode"]]
            if len(local_mappings) != 1 or not root_context or root_context["parent_context_id"] is not None:
                continue
            middle_field = _source_name(local_mappings[0]["caller_field"])
            outer_mappings = [mapping for mapping in middle_context["parameter_mappings"]
                              if _source_name(mapping["callee_field"]) == middle_field
                              and _source_name(mapping["caller_field"]) == expected["root_field"]
                              and mapping["passing_mode"] == "REFERENCE"]
            if len(outer_mappings) != 1:
                continue
            for witness in audit["witnesses"]:
                if event["event_id"] not in witness["event_ids"]:
                    continue
                trace = witness["trace"]
                copy_return = next((index for index, step in enumerate(trace)
                                    if step["kind"] == "call_return" and step.get("callee_context_id") == event["context_id"]
                                    and _parameter(step, middle_field, expected["status_field"], expected["value"],
                                                   expected["passing_mode"], False)), None)
                outer_return = copy_return is not None and any(
                    step["kind"] == "call_return" and step.get("callee_context_id") == middle_context["context_id"]
                    and _parameter(step, expected["root_field"], middle_field, expected["root_value"], "REFERENCE", True)
                    for step in trace[copy_return + 1:])
                if outer_return and _matches(_values(witness), {expected["root_field"]: expected["root_value"]}):
                    witness_ids.append(witness["witness_id"])
        checks.append(_check("COPY-BARRIER:" + expected["passing_mode"], closed and len(events) == 1 and bool(witness_ids),
                             matched_events=len(events), witness_ids=sorted(set(witness_ids)),
                             expected_local_value=expected["value"], expected_root_value=expected["root_value"]))
    return checks


def check_case(audit: dict, case: dict) -> list[dict]:
    """Compare every recorded root exit with a separate, non-vacuous oracle."""
    exits = audit["root_exits"]
    summary = audit["summary"]
    closed = bool(exits and not audit["boundaries"] and not summary["truncated"]
                  and not summary["root_exits_truncated"]
                  and summary["modeled_root_exits"] == len(exits))
    checks = [_check("BOUNDED-EXPLORATION", closed, modeled_root_exits=summary["modeled_root_exits"],
                     recorded_exits=len(exits), boundary_count=len(audit["boundaries"]),
                     truncated=summary["truncated"], root_exits_truncated=summary["root_exits_truncated"])]
    expected = case.get("expected_root_values", {})
    value_sets = case.get("expected_root_value_sets", {})
    if not isinstance(expected, dict) or not isinstance(value_sets, dict) or not (expected or value_sets):
        raise ValueError("Each case needs an explicit nonempty root-value oracle.")
    for name, value in expected.items():
        actual = _sets(_values(record).get(name) for record in exits)
        passed = closed and all(_matches(_values(record), {name: value}) for record in exits)
        checks.append(_check("ROOT:" + name, passed, field=name, expected=[_value(value)], actual=actual))
    for name, values in value_sets.items():
        if not isinstance(values, list) or not values or len(values) > 32:
            raise ValueError("Value-set expectations must be explicit bounded alternatives.")
        actual = _sets(_values(record).get(name) for record in exits)
        desired = _sets(values)
        passed = closed and actual == desired and all(name in _values(record) for record in exits)
        checks.append(_check("ALTERNATIVES:" + name, passed, field=name, expected=desired, actual=actual))
    for index, conditional in enumerate(case.get("expected_conditional_exits", [])):
        if set(conditional) != {"when", "then"} or not conditional["when"] or not conditional["then"]:
            raise ValueError("Conditional expectations require nonempty when and then mappings.")
        selected = [record for record in exits if _matches(_values(record), conditional["when"])]
        passed = closed and bool(selected) and all(_matches(_values(record), conditional["then"]) for record in selected)
        checks.append(_check("CONDITIONAL:" + str(index + 1), passed, condition=conditional["when"],
                             expected=conditional["then"], matched_exits=len(selected)))
    if case.get("no_call_exception_events") is True:
        unexpected = [event["event_id"] for event in audit["events"] if event["event_kind"] == "call_exception"]
        checks.append(_check("BUSINESS-RETURN-NOT-CALL-EXCEPTION", closed and not unexpected,
                             unexpected_event_ids=unexpected))
    checks.extend(_return_checks(audit, case, closed))
    return checks


def build_error_return_demo(source_root: Path, database_path: Path, profile: dict) -> dict:
    cases = profile["cases"]
    contracts = profile["contracts"]
    if not 1 <= len(cases) <= 16 or not 1 <= len(contracts) <= 32:
        raise ValueError("The return acceptance profile exceeds its bounded scope.")
    if len({case["case_id"] for case in cases}) != len(cases):
        raise ValueError("Return acceptance case identities must be unique.")
    build = build_structural_index(source_root, database_path, quiet=True)
    typed_contracts = [ErrorContract(item["program_name"], tuple(item["status_fields"]),
                                    tuple(item["output_fields"])) for item in contracts]
    results = []
    for case in cases:
        audit = audit_interprogram_paths(database_path, profile["root_program"], typed_contracts,
                                        initial_values=case["initial_values"], call_policy=case["call_policy"])
        if audit["snapshot_id"] != build["snapshot_id"]:
            raise ValueError("Source snapshot changed during return-path acceptance.")
        checks = check_case(audit, case)
        results.append({"case_id": case["case_id"], "initial_values": case["initial_values"],
                        "call_policy": case["call_policy"], "checks": checks,
                        "status": "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL",
                        "audit": audit})
    checks = [check for result in results for check in result["checks"]]
    failed = sum(check["status"] == "FAIL" for check in checks)
    return {"demo_id": "ERROR-RETURN-V5", "task_id": "T01", "snapshot_id": build["snapshot_id"],
            "cases": results,
            "return_acceptance": {"status": "FAIL" if failed else "PASS", "passed": len(checks) - failed,
                                  "failed": failed, "evaluation_scope": "explicit_static_interprogram_fixture_oracles",
                                  "business_acceptance_passed": False},
            "full_business_analysis_verified": False, "runtime_execution_tested": False,
            "model_called": False, "network_calls": False}


def _display(values: list) -> str:
    return "、".join("未知" if value is None else format(Decimal(value), "f") for value in values) or "无退出"


def render_markdown(bundle: dict) -> str:
    acceptance = bundle["return_acceptance"]
    lines = ["# T01：跨程序错误返回验证", "",
             f"所选源码模型预期：**{acceptance['status']}**，{acceptance['passed']} 项通过，{acceptance['failed']} 项失败。",
             "PASS 表示符合单独编写的样例预期，不代表整个业务系统正确，也不是 COBOL 运行结果。", "",
             f"源码快照：`{bundle['snapshot_id']}`", "",
             "## 入口最终结果", "",
             "| 案例 | 输入 | 入口字段 | 模型退出值 | 预期 | 检查 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for case in bundle["cases"]:
        inputs = ", ".join(f"{name}={value}" for name, value in case["initial_values"].items())
        for check in case["checks"]:
            if "field" in check:
                lines.append(f"| {case['case_id']} | {inputs} | {check['field']} | {_display(check['actual'])} | {_display(check['expected'])} | {check['status']} |")
    lines.extend(["", "## 如何理解这些路径", "",
        "当前案例明确假设 CALL 可以正常调用和返回；这不意味着真实环境不会加载失败。子程序返回非零业务状态后，仍恢复调用方的正常返回路径，不会误走 ON EXCEPTION。默认分析接口另可探索调用失败分支。",
        "COMPUTE 的正常结果仍为未知，溢出作为独立源码备选分支保留。例如另一路状态出现 0/24，不代表输入 1 实际会溢出，也不能用它证明具体正常金额。", ""])
    if "source_mutation" in bundle:
        mutation = bundle["source_mutation"]
        lines.extend(["## 故意接错的源码副本", "",
                      f"仅在临时副本 `{mutation['relative_path']}` 插入 `{mutation['inserted_statement']}`；原样例不改动。",
                      "这里仍使用原安全预期，因此 FAIL 才是应有的报警结果。JSON 保存变更说明、新快照、非零终态和完整模型见证。", ""])
    lines.extend(["## 逐步复核", "",
                  "配套 JSON 的每个案例保留 root_exits、events 和 witnesses；trace 连续记录程序进入、数值赋值、参数传入、正常返回及入口退出。输出是事件发生后的观察，不自动声称因果关系或所有字段都有清零义务。以下返回表只画实际 REFERENCE 回写，CONTENT/VALUE 返回不画成反向赋值。",
                  "返回链表选取汇总状态为 21 的一个实际模型见证；这不替代上面的全量退出检查。B 单侧错误案例还保留 A 溢出优先、汇总为 24 的备选路径。", ""])
    for case in bundle["cases"]:
        audit = case["audit"]
        lines.append(f"### {case['case_id']}")
        lines.append("")
        lines.append(f"已建模退出 {audit['summary']['modeled_root_exits']} 条；边界 {len(audit['boundaries'])} 项；探索截断 {audit['summary']['truncated']}。")
        lines.append("")
        for check in case["checks"]:
            if check.get("verified_steps"):
                lines.extend([f"参数返回链 `{check['check_id']}`：{check['status']}；见证 `{check['witness_id']}`。", "",
                              "| 步骤 | 调用双方 | 参数实际值 | 调用或返回位置 |", "| --- | --- | --- | --- |"])
                for step in check["verified_steps"]:
                    action = "返回" if step["kind"] == "call_return" else "传入"
                    params = [item for item in step["parameters"] if _value(item["value"]) == _value(check["expected_status_value"])
                              and (action != "返回" or item["written_back"])]
                    descriptions = [f"{item['callee_field']} → {item['caller_field']} = {_display([item['value']])}（{item['passing_mode']}）"
                                    if action == "返回" else f"{item['caller_field']} → {item['callee_field']} = {_display([item['value']])}（{item['passing_mode']}）"
                                    for item in params]
                    refs = step.get("evidence_refs", [])
                    locations = ", ".join(f"{ref['relative_path']}:{ref.get('start_line', ref.get('line_start', '?'))}" for ref in refs[:2])
                    lines.append(f"| {action} | {step['caller_program']} / {step['callee_program']} | {'；'.join(descriptions)} | {locations} |")
                lines.append("")
        if "source_mutation" in bundle:
            for witness in audit["witnesses"][:1]:
                for step in witness["trace"]:
                    if step["kind"] == "MOVE" and step.get("writes", {}).get("OUTPUT-A") == "7":
                        locations = ", ".join(f"{ref['relative_path']}:{ref.get('start_line', '?')}" for ref in step["evidence_refs"])
                        lines.extend([f"实际检出的覆盖：`{step['program_name']}` 在 `{locations}` 写入 `OUTPUT-A=7`；该见证的入口退出仍为 7。完整调用/返回与赋值顺序见 `{witness['witness_id']}`。", ""])
        failed = [check["check_id"] for check in case["checks"] if check["status"] == "FAIL"]
        if failed:
            lines.append("未通过检查：" + "、".join(failed) + "。")
            lines.append("")
        for boundary in audit["boundaries"][:8]:
            lines.append("- 明确边界：" + boundary["reason"])
        if audit["boundaries"]:
            lines.append("")
    lines.extend(["## 本项边界", "",
        "这是有预算、受支持语法内的跨程序抽象分析。未知条件保留备选路径，模型见证可能无法在真实运行中到达；遇到递归、动态目标、别名、共享状态或缺失证据等不能略过后宣布成功。",
        "尚未执行 COBOL，未调用模型或公司 API，未连接界面；正常金额精度、复杂循环和完整业务回答继续留在后续任务。T01 不等于 P2 完成。", ""])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--profile", type=Path, default=FIXTURE_ROOT / "profile.json")
    parser.add_argument("--database", type=Path, default=Path(".poc-data/error-return-v5/structural-index.sqlite"))
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--caller-overwrite", action="store_true",
                        help="Analyze an explicit temporary source mutation; the original safe oracle must fail.")
    args = parser.parse_args(argv)
    if args.profile.stat().st_size > 256000:
        raise ValueError("Return acceptance profile exceeds its size budget.")
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    source = (args.source or args.profile.parent / profile["source_root"]).resolve()
    outputs = [path.resolve() for path in (args.database, args.json_output, args.markdown_output) if path]
    if len(outputs) != len(set(outputs)) or any(path == args.profile.resolve() or path.is_relative_to(source) for path in outputs):
        raise ValueError("Generated output paths must be distinct and outside source/profile inputs.")
    if args.caller_overwrite:
        with tempfile.TemporaryDirectory(prefix="error-return-variant-") as directory:
            copied = Path(directory) / "source"
            shutil.copytree(source, copied)
            target = copied / "programs" / "EXENTRY.cbl"
            original = target.read_text(encoding="utf-8")
            anchor = "    CALL 'EXFINAL' USING BY CONTENT WORK-B STATUS-B"
            if original.count(anchor) != 1:
                raise ValueError("The caller-overwrite mutation needs its unique unchanged source anchor.")
            target.write_text(original.replace(anchor, "    MOVE 7 TO OUTPUT-A.\n" + anchor, 1), encoding="utf-8")
            bundle = build_error_return_demo(copied, args.database, profile)
            bundle["source_mutation"] = {"relative_path": "programs/EXENTRY.cbl", "inserted_statement": "MOVE 7 TO OUTPUT-A.",
                "before_unique_anchor": anchor.strip(), "original_source_unchanged": True,
                "scope": "temporary_source_variant_not_original_program"}
    else:
        bundle = build_error_return_demo(source, args.database, profile)
    for output, content in ((args.json_output, json.dumps(bundle, ensure_ascii=False, indent=2)),
                            (args.markdown_output, render_markdown(bundle))):
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(content, encoding="utf-8")
    acceptance = bundle["return_acceptance"]
    print(json.dumps({"task_id": "T01", **acceptance, "runtime_execution_tested": False,
                      "network_calls": False}, ensure_ascii=False, indent=2))
    return 0 if acceptance["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
