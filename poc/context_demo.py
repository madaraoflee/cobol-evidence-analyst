#!/usr/bin/env python3
"""Offline feasibility acceptance for static callsite context separation."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Sequence

from call_contexts import audit_call_contexts
from context_paths import contextualize_errors
from error_paths import ErrorContract, audit_error_paths
from structural_index import build_structural_index


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "call-context-v3"


def check_expectations(context_audit: dict, error_view: dict, profile: dict) -> dict:
    """Compare source identities against an explicitly selected local oracle.

    This checker never derives expected behavior from the analyzer result or
    upgrades a source-context PASS to business or execution acceptance.
    """
    contexts = context_audit["contexts"]
    checks = []

    def check(name, passed, **details):
        checks.append({"name": name, "status": "PASS" if passed else "FAIL", **details})

    expected = profile["expected_context_counts"]
    if not expected or len(expected) > 128 or any(type(n) is not int or not 1 <= n <= 1024 for n in expected.values()):
        raise ValueError("The source-context oracle requires bounded positive context counts.")
    actual = dict(Counter(c["program_name"] for c in contexts))
    check("exact_context_counts", actual == expected, expected=expected, actual=actual)
    roots = [c for c in contexts if c["parent_context_id"] is None]
    check("unique_expected_root", len(roots) == 1 and roots[0]["program_name"] == profile["root_program"])
    root_id = roots[0]["context_id"] if len(roots) == 1 else None
    check("all_context_parameter_mappings_complete", all(c["parameter_mapping_complete"] for c in contexts))
    check("context_audit_not_truncated", not context_audit["summary"]["truncated"])
    totals = profile["expected_totals"]
    check("source_callsite_count", len({c["via_callsite_id"] for c in contexts if c["parent_context_id"] is not None}) == totals["source_callsites"])
    check("expanded_static_calls", len(contexts) - len(roots) == totals["expanded_call_occurrences"])
    static_bindings = {m["binding_id"]: m for c in contexts for m in c["parameter_mappings"]}
    check("static_binding_count", len(static_bindings) == totals["static_confirmed_bindings"])
    check("static_writeback_candidate_count", sum(m["possible_writeback"] for m in static_bindings.values()) == totals["static_writeback_candidates"])
    entry_mappings = profile["expected_entry_mappings"]
    if not entry_mappings or len(entry_mappings) > 512:
        raise ValueError("The source-context oracle requires bounded entry mappings.")
    for index, target in enumerate(entry_mappings):
        matches = [(c, m) for c in contexts if c["parent_context_id"] == root_id
                   and c["program_name"] == target["callee_program"]
                   for m in c["parameter_mappings"]
                   if m["caller_field"]["name"] == target["caller_field"]
                   and m["callee_field"]["name"] == target["callee_field"]
                   and m["passing_mode"] == target["passing_mode"]]
        check(f"entry_mapping_{index + 1}", len(matches) == 1,
              expected=target, matches=len(matches))
    branches = profile["expected_nested_branches"]
    if not branches or len(branches) > 128:
        raise ValueError("The source-context oracle requires bounded nested branches.")
    selected_contexts = []
    for index, branch in enumerate(branches):
        candidates = []
        for context in contexts:
            if context["program_chain"] != branch["program_chain"]:
                continue
            for route in error_view["parameter_routes"]:
                if (route["context_id"] == context["context_id"]
                        and route["field_name"] == branch["leaf_status_field"]
                        and route["terminal_reason"] == "root_context_field"
                        and route["reference_return_candidate"]
                        and route["fields_inner_to_outer"][-1]["context_id"] == root_id
                        and route["fields_inner_to_outer"][-1]["name"] == branch["root_status_field"]):
                    candidates.append(context)
        check(f"nested_branch_{index + 1}", len(candidates) == 1,
              expected=branch["root_status_field"], matches=len(candidates))
        selected_contexts.extend(candidates)
        for parameter_index, target in enumerate(branch["shared_worker_parameters"]):
            matches = [m for c in candidates for m in c["parameter_mappings"]
                       if m["caller_field"]["name"] == target["caller_field"]
                       and m["callee_field"]["name"] == target["callee_field"]
                       and m["passing_mode"] == target["passing_mode"]
                       and m["parameter_position"] == target["position"]
                       and m["group_member_index"] == target["member_index"]]
            check(f"shared_mapping_{index + 1}_{parameter_index + 1}", len(matches) == 1)
    check("nested_branches_have_distinct_contexts", len(selected_contexts) == len(branches)
          and len({c["context_id"] for c in selected_contexts}) == len(branches))
    check("shared_nested_callsite_is_not_merged", len(selected_contexts) > 1
          and len({c["via_callsite_id"] for c in selected_contexts}) == 1
          and len({tuple(c["callsite_chain"]) for c in selected_contexts}) == len(selected_contexts))
    by_id = {c["context_id"]: c for c in contexts}
    for index, target in enumerate(profile["expected_leaf_mappings"]):
        matched_contexts = {c["context_id"] for c in contexts if c["program_name"] == target["callee_program"]
                            and c["parent_context_id"] in by_id
                            and by_id[c["parent_context_id"]]["program_name"] == target["caller_program"]
                            for m in c["parameter_mappings"]
                            if m["caller_field"]["name"] == target["caller_field"]
                            and m["callee_field"]["name"] == target["callee_field"]
                            and m["passing_mode"] == target["passing_mode"]}
        check(f"leaf_mapping_{index + 1}", len(matched_contexts) == expected[target["callee_program"]])
    check("projection_not_truncated", not error_view["summary"]["truncated"])
    failures = sum(c["status"] == "FAIL" for c in checks)
    return {"status": "PASS" if not failures else "FAIL", "checks": checks,
            "passed": len(checks) - failures, "failed": failures,
            "evaluation_scope": "explicit_fixture_static_context_identity_expectations",
            "business_acceptance_passed": False, "runtime_execution_tested": False}


def build_context_demo(source_root: Path, database_path: Path, profile: dict) -> dict:
    build = build_structural_index(source_root, database_path, quiet=True)
    contexts = audit_call_contexts(database_path, profile["root_program"])
    contracts = [ErrorContract(c["program_name"], tuple(c["status_fields"]),
                               tuple(c.get("output_fields", ()))) for c in profile["contracts"]]
    errors = audit_error_paths(database_path, profile["root_program"], contracts)
    if build["snapshot_id"] != contexts["snapshot_id"]:
        raise ValueError("The source snapshot changed between indexing and context analysis.")
    view = contextualize_errors(contexts, errors)
    acceptance = check_expectations(contexts, view, profile)
    return {
        "demo_id": "CALL-CONTEXT-V3", "mode": "offline_source_context_acceptance",
        "snapshot_id": contexts["snapshot_id"], "build_report": build,
        "context_audit": contexts, "error_audit": errors, "context_error_view": view,
        "source_context_acceptance": acceptance,
        "full_business_analysis_verified": False, "runtime_execution_tested": False,
        "model_called": False, "network_calls": False,
    }


def render_markdown(bundle: dict) -> str:
    audit = bundle["context_audit"]
    acceptance = bundle["source_context_acceptance"]
    view = bundle["context_error_view"]
    context_by_id = {c["context_id"]: c for c in audit["contexts"]}
    counts = Counter(c["program_name"] for c in audit["contexts"])
    lines = ["# 同一子程序重复调用：可行性验证", "",
             f"源码上下文验收：{acceptance['status']}；{acceptance['passed']} 项通过，{acceptance['failed']} 项失败。",
             "这只验证当前样例的调用点身份与参数对应；没有执行 COBOL，也不证明完整业务分析已经通过。", "",
             f"快照：`{bundle['snapshot_id']}`", "",
             f"{len(counts)} 个程序展开为 {len(audit['contexts'])} 个静态调用上下文。同一子程序的复用不会仅按程序名合并。", "",
             "## 共享子程序的两条状态参数链", "",
             "下列是参数位置/布局连通的候选回写链，不是已执行的错误传播。", "",
             "| 共享程序字段 | 外层调用分支 | 对应入口字段 | 判断 |",
             "| --- | --- | --- | --- |"]
    for route in view["parameter_routes"]:
        context = context_by_id[route["context_id"]]
        if (counts[context["program_name"]] < 2 or context["depth"] < 2
                or "status" not in route["roles"]):
            continue
        outer = route["fields_inner_to_outer"][-1]
        branch = context["callsite_chain"][0]
        root_call = next(c for c in audit["contexts"] if c["callsite_chain"] == [branch])
        caller_refs = root_call["evidence_refs"]
        anchor = next((r for r in caller_refs if r["evidence_id"] in {
            ref["evidence_id"] for m in root_call["parameter_mappings"] for ref in m["evidence_refs"]}), None)
        # Context identity, not display order, is the branch key.
        label = f"{anchor['relative_path']}:{anchor['start_line']}" if anchor else branch
        decision = "可能回写入口" if route["reference_return_candidate"] else "未确认入口回写"
        lines.append(f"| `{context['program_name']}::{route['field_name']}` | `{label}` | `{outer['name']}` | {decision} |")
    lines.extend(["", "## 复制边界与错误观察", "",
                  "BY CONTENT/BY VALUE 复制参数，即使内层再次 BY REFERENCE 传递，也不会恢复对最外层原字段的引用回写。",
                  f"报告保留 {len(view['parameter_routes'])} 条状态/输出参数链和 {view['summary']['observation_references']} 个带上下文的本地错误观察引用。相同源码观察可以被两条静态链引用，不代表实际发生了两次错误。", "",
                  "## 明确未通过的范围", "",
                  "- 完整异常/溢出控制流、条件可行性、实际数值和错误路径执行。",
                  "- 循环迭代或重复 PERFORM 的运行次数、共享工作区状态和存储别名。",
                  "- 配置型调用的实际目标、真实模型完整回答和界面集成。", "",
                  "递归、动态调用、参数不完整和预算截断的具体边界见配套 JSON。源码验收 PASS 不能替代业务验收。", ""])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=FIXTURE_ROOT / "main")
    parser.add_argument("--profile", type=Path, default=FIXTURE_ROOT / "profile.json")
    parser.add_argument("--database", type=Path, default=Path(".poc-data/context-v3/structural-index.sqlite"))
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args(argv)
    if args.profile.stat().st_size > 256000:
        raise ValueError("The local acceptance profile exceeds its size budget.")
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    bundle = build_context_demo(args.source, args.database, profile)
    for output, content in ((args.json_output, json.dumps(bundle, ensure_ascii=False, indent=2)),
                            (args.markdown_output, render_markdown(bundle))):
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(content, encoding="utf-8")
    result = bundle["source_context_acceptance"]
    print(json.dumps({"demo_id": bundle["demo_id"], "source_context_acceptance": result["status"],
                      "passed": result["passed"], "failed": result["failed"],
                      "contexts": len(bundle["context_audit"]["contexts"]),
                      "full_business_analysis_verified": False, "network_calls": False},
                     ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
