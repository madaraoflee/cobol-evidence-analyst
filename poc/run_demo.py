#!/usr/bin/env python3
"""Build and run the six-step CALC-01 offline evidence demonstration."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from business_acceptance import evaluate_source_coverage
from investigation_tools import InvestigationTools
from structural_index import build_structural_index


DEFAULT_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "synthetic-insurance-v1"
)

SEARCH_QUERY = " ".join(
    (
        "OUT-INSTALMENT-PREMIUM",
        "WS-ANNUAL-PREMIUM",
        "WS-BASE-PREMIUM",
        "WS-OCCUPATION-LOADING",
        "WS-RIDER-PREMIUM-TOTAL",
        "WS-HIGH-SUM-DISCOUNT",
        "WS-MODE-FACTOR",
        "SYN_CALC_ROUTING",
        "IN-POLICY-STATUS",
    )
)

EVIDENCE_PRIORITIES = (
    ("programs/SYNP040.cbl", 66),
    ("programs/SYNP040.cbl", 59),
    ("programs/SYNP100.cbl", 20),
    ("programs/SYNP200.cbl", 18),
    ("programs/SYNP030.cbl", 20),
    ("programs/SYNP040.cbl", 48),
    ("programs/SYNP040.cbl", 54),
    ("programs/SYNP020.cbl", 9),
    ("programs/SYNP040.cbl", 34),
    ("programs/SYNP000.cbl", 22),
    ("programs/SYNP000.cbl", 31),
    ("copybooks/SYNPRM.cpy", 32),
)


def _all_evidence_refs(outputs: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    refs: list[dict[str, object]] = []
    seen: set[str] = set()

    def add(ref: dict[str, object] | None) -> None:
        if not ref:
            return
        evidence_id = str(ref.get("evidence_id", ""))
        if evidence_id and evidence_id not in seen:
            seen.add(evidence_id)
            refs.append(ref)

    for output in outputs:
        for ref in output.get("evidence_refs", []):
            add(ref)
        for hit in output.get("hits", []):
            add(hit.get("evidence_ref"))
        for edge in output.get("edges", []):
            add(edge.get("evidence_ref"))
        for match in output.get("matches", []):
            add(match.get("definition", {}).get("evidence_ref"))
            for relation in match.get("incoming_relations", []):
                add(relation.get("evidence_ref"))
            for relation in match.get("outgoing_relations", []):
                add(relation.get("evidence_ref"))
    return refs


def _choose_evidence_ids(outputs: Iterable[dict[str, object]]) -> list[str]:
    refs = _all_evidence_refs(outputs)
    chosen: list[str] = []
    seen: set[str] = set()

    for expected_path, expected_start in EVIDENCE_PRIORITIES:
        for ref in refs:
            if (
                ref.get("relative_path") == expected_path
                and int(ref.get("start_line", -1)) == expected_start
            ):
                evidence_id = str(ref["evidence_id"])
                if evidence_id not in seen:
                    chosen.append(evidence_id)
                    seen.add(evidence_id)
                break

    for ref in refs:
        if len(chosen) >= 12:
            break
        evidence_id = str(ref["evidence_id"])
        if evidence_id not in seen:
            chosen.append(evidence_id)
            seen.add(evidence_id)
    return chosen[:12]


def _writer_expression(
    inspection: dict[str, object], field_name: str
) -> tuple[str | None, bool]:
    for match in inspection.get("matches", []):
        for relation in match.get("incoming_relations", []):
            if relation.get("relation_type") != "WRITES":
                continue
            if relation.get("target", {}).get("name") != field_name:
                continue
            expression = relation.get("metadata", {}).get("expression")
            if expression and expression != "ZERO":
                return str(expression), bool(
                    relation.get("metadata", {}).get("rounded")
                )
    return None, False


def build_demo_bundle(
    source_root: Path, database_path: Path
) -> dict[str, object]:
    build_report = build_structural_index(
        source_root, database_path, quiet=True
    )
    tools = InvestigationTools(database_path)

    search = tools.search_code(SEARCH_QUERY, limit=25)
    inspect_result = tools.inspect_symbol(
        "OUT-INSTALMENT-PREMIUM", program_name="SYNP040"
    )
    inspect_annual = tools.inspect_symbol(
        "WS-ANNUAL-PREMIUM", program_name="SYNP040"
    )
    trace_calls = tools.trace_relations(
        "SYNP000",
        symbol_type="Program",
        relation_types=["CALLS", "CALL_TARGET_FROM", "SELECTS_FROM"],
        max_depth=2,
        max_edges=30,
    )
    trace_adjustments = tools.trace_relations(
        "SYNP040",
        symbol_type="Program",
        relation_types=["PERFORMS", "SELECTS_FROM"],
        max_depth=2,
        max_edges=30,
    )

    first_five = [
        search,
        inspect_result,
        inspect_annual,
        trace_calls,
        trace_adjustments,
    ]
    evidence_ids = _choose_evidence_ids(first_five)
    evidence = tools.read_evidence(evidence_ids, max_chars=24_000)
    tool_trace = [*first_five, evidence]

    instalment_expression, instalment_rounded = _writer_expression(
        inspect_result, "OUT-INSTALMENT-PREMIUM"
    )
    annual_expression, annual_rounded = _writer_expression(
        inspect_annual, "WS-ANNUAL-PREMIUM"
    )
    literal_calls = sorted(
        {
            edge["target"]["name"]
            for edge in trace_calls.get("edges", [])
            if edge["relation_type"] == "CALLS"
        }
    )
    external_tables = sorted(
        {
            edge["target"]["name"]
            for trace_result in (trace_calls, trace_adjustments)
            for edge in trace_result.get("edges", [])
            if edge["relation_type"] == "SELECTS_FROM"
        }
    )
    dynamic_boundaries = [
        boundary
        for boundary in trace_calls.get("boundaries", [])
        if boundary["relation_type"] == "CALL_TARGET_FROM"
    ]
    # This offline gold check scans the snapshot independently. It is neither
    # a model tool call nor evidence that the six-step trace retrieved it all.
    source_fact_coverage = evaluate_source_coverage(database_path)

    return {
        "demo_id": "CALC-01-P1B",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "offline_deterministic_evidence_demo",
        "privacy": {
            "network_calls": False,
            "company_api_called": False,
            "source": "original_synthetic_fixture",
        },
        "build_report": build_report,
        "question": "分期保费最终是怎样计算出来的？",
        "tool_budget": {
            "maximum_calls": 6,
            "calls_used": len(tool_trace),
        },
        "tool_trace": tool_trace,
        "source_fact_coverage": source_fact_coverage,
        "source_fact_coverage_execution": {
            "mode": "local_offline_snapshot_evaluation",
            "included_in_agent_tool_trace": False,
            "model_calls": 0,
        },
        "answer_preview": {
            "instalment_expression": instalment_expression,
            "instalment_rounded": instalment_rounded,
            "annual_expression": annual_expression,
            "annual_rounded": annual_rounded,
            "literal_call_targets": literal_calls,
            "external_configuration_tables": external_tables,
            "dynamic_call_boundaries": dynamic_boundaries,
            "evidence_span_count": evidence.get("span_count", 0),
            "support_status": (
                "PARTIAL"
                if instalment_expression and annual_expression
                else "INCOMPLETE"
            ),
            "source_fact_coverage_summary": source_fact_coverage["summary"],
            "full_business_analysis_verified": False,
        },
    }


def render_markdown(bundle: dict[str, object]) -> str:
    report = bundle["build_report"]
    answer = bundle["answer_preview"]
    trace = bundle["tool_trace"]
    spans = trace[-1]["spans"]
    coverage = bundle["source_fact_coverage"]
    counts = coverage["summary"]
    lines = [
        "# CALC-01 P1-B 可执行演示结果",
        "",
        "> 核心判断：六步离线轨迹可以展示公式和调用证据，但还没有完成业务分析。下方完整性检查来自独立的本地金标准核查，不属于六步工具检索，也不证明模型已经读全证据或答完整。",
        "",
        f"- Snapshot：`{report['snapshot_id']}`",
        f"- 源码：{report['files']['decoded']} 个原创 COBOL/COPYBOOK 文件",
        f"- 事实：{report['database_counts']['symbols']} 个符号、{report['database_counts']['relations']} 条关系、{report['database_counts']['evidence_spans']} 个 EvidenceSpan",
        f"- 工具预算：{bundle['tool_budget']['calls_used']} / {bundle['tool_budget']['maximum_calls']}",
        f"- 预览状态：`{answer['support_status']}`；完整业务分析验证：未完成",
        "",
        "## 从代码取得的结论",
        "",
        f"- 最终公式：`{answer['instalment_expression']}`；含 ROUNDED 标记：`{'是' if answer['instalment_rounded'] else '否'}`。",
        f"- 年化公式：`{answer['annual_expression']}`；含 ROUNDED 标记：`{'是' if answer['annual_rounded'] else '否'}`。",
        "- 确认的字面量调用：`" + "`, `".join(answer["literal_call_targets"]) + "`。",
        "- 外部配置表：`" + "`, `".join(answer["external_configuration_tables"]) + "`。",
        "- 动态产品计算器仍由 `LK-CALCULATOR-PROGRAM` 在运行时决定；源码快照没有控制表数据，不能把某个候选写成实际目标。",
        "",
        "## 独立来源事实覆盖检查",
        "",
        f"- 范围：`{coverage['evaluation_scope']}`；状态：`{coverage['status']}`。",
        f"- 已覆盖 {counts['covered']} 项，缺失 {counts['missing']} 项，保留边界 {counts['boundary']} 项。",
        "- 此检查直接读取同一索引快照，不计入六次 Agent 工具调用；未运行自然语言理解、模型回答或 COBOL 计算。",
        "- 来源事实覆盖不等于完整回答验收；以下人工审查缺口不会因为找到两条公式而自动消失。",
        "",
    ]
    for obligation in coverage["obligations"]:
        if obligation["status"] != "missing":
            continue
        description = obligation.get("description", obligation["obligation_id"])
        reason = obligation.get("reason", "必要的同语句读写或控制关系未被当前快照证明。")
        lines.append(f"- **{description}**：{reason}")
    lines.extend((
        "",
        "## 六步工具轨迹",
        "",
    ))
    for index, step in enumerate(trace, start=1):
        item_count = (
            step.get("result_count")
            or step.get("match_count")
            or step.get("edge_count")
            or step.get("span_count")
            or 0
        )
        lines.append(
            f"{index}. `{step['tool']}` — `{step['status']}`，返回 {item_count} 项，边界 {len(step.get('boundaries', []))} 项。"
        )

    lines.extend(("", "## 六步工具实际读取的源码证据", ""))
    for span in spans:
        lines.append(
            f"- `{span['relative_path']}:L{span['start_line']}-L{span['end_line']}` — `{span['integrity']}` / `{span['content_type']}`"
        )
    lines.extend(
        (
            "",
            "## 当前边界",
            "",
            "- 这是 P1-B 离线证据演示，不是公司 API 生成的最终中文回答。",
            "- 已建立程序范围内的 COPY 字段定义绑定，但这不等于 CALL USING 到 LINKAGE 的参数位置映射或跨程序值流证明。",
            "- DDL/DDS、控制表记录、Job Schedule、生产输入与运行日志没有提供，实际费率值和实际动态调用目标不可确认。",
            "- ROUNDED 标记不等于所有数值精度、溢出和运行路径已经验证；没有执行 COBOL。",
            "- 项目已有受限 Agent 执行器；本次离线演示未调用模型，真实问题的检索和回答完整度仍需单独验收。",
            "",
        )
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the CALC-01 six-step offline evidence demonstration."
    )
    parser.add_argument(
        "--source", type=Path, default=DEFAULT_FIXTURE
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(".poc-data/demo/structural-index.sqlite"),
    )
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bundle = build_demo_bundle(args.source, args.database)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(
            render_markdown(bundle), encoding="utf-8"
        )
    summary = {
        "demo_id": bundle["demo_id"],
        "snapshot_id": bundle["build_report"]["snapshot_id"],
        "tool_calls": bundle["tool_budget"]["calls_used"],
        "support_status": bundle["answer_preview"]["support_status"],
        "evidence_spans": bundle["answer_preview"]["evidence_span_count"],
        "source_fact_coverage": bundle["source_fact_coverage"]["summary"],
        "full_business_analysis_verified": False,
        "network_calls": bundle["privacy"]["network_calls"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
