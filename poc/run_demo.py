#!/usr/bin/env python3
"""Build and run the six-step CALC-01 offline evidence demonstration."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

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
    inspect_result = tools.inspect_symbol("OUT-INSTALMENT-PREMIUM")
    inspect_annual = tools.inspect_symbol("WS-ANNUAL-PREMIUM")
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
                "SUPPORTED_WITH_BOUNDARIES"
                if instalment_expression and annual_expression
                else "INCOMPLETE"
            ),
        },
    }


def render_markdown(bundle: dict[str, object]) -> str:
    report = bundle["build_report"]
    answer = bundle["answer_preview"]
    trace = bundle["tool_trace"]
    spans = trace[-1]["spans"]
    lines = [
        "# CALC-01 P1-B 可执行演示结果",
        "",
        "> 核心判断：确定性事实层已能在 6 次受限工具调用内定位最终分期公式、年化组成、调用路径、外部控制表和动态调用边界；本报告没有调用公司模型，因此不把它包装成自然语言 Agent 已完成。",
        "",
        f"- Snapshot：`{report['snapshot_id']}`",
        f"- 源码：{report['files']['decoded']} 个原创 COBOL/COPYBOOK 文件",
        f"- 事实：{report['database_counts']['symbols']} 个符号、{report['database_counts']['relations']} 条关系、{report['database_counts']['evidence_spans']} 个 EvidenceSpan",
        f"- 工具预算：{bundle['tool_budget']['calls_used']} / {bundle['tool_budget']['maximum_calls']}",
        f"- 支持状态：`{answer['support_status']}`",
        "",
        "## 从代码取得的结论",
        "",
        f"- 最终公式：`{answer['instalment_expression']}`；明确舍入：`{'是' if answer['instalment_rounded'] else '否'}`。",
        f"- 年化公式：`{answer['annual_expression']}`；明确舍入：`{'是' if answer['annual_rounded'] else '否'}`。",
        "- 确认的字面量调用：`" + "`, `".join(answer["literal_call_targets"]) + "`。",
        "- 外部配置表：`" + "`, `".join(answer["external_configuration_tables"]) + "`。",
        "- 动态产品计算器仍由 `LK-CALCULATOR-PROGRAM` 在运行时决定；源码快照没有控制表数据，不能把某个候选写成实际目标。",
        "",
        "## 六步工具轨迹",
        "",
    ]
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

    lines.extend(("", "## 已读取源码证据", ""))
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
            "- 当前轻量解析器尚未展开 COPYBOOK 参数映射，因此跨程序共享字段关系仍可能显示为 unresolved。",
            "- DDL/DDS、控制表记录、Job Schedule、生产输入与运行日志没有提供，实际费率值和实际动态调用目标不可确认。",
            "- 下一阶段接入公司 OpenAI-compatible API，让模型只在这四个工具和 6 步预算内完成问题规划与回答组织。",
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
        "network_calls": bundle["privacy"]["network_calls"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
