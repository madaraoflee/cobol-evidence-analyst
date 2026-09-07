#!/usr/bin/env python3
"""Offline, snapshot-bound source-fact coverage for explicit business cases.

This is an executable gold profile, not a natural-language answer evaluator.
Matching indexed syntax is not proof of runtime behavior or a complete answer.
Profiles are selected explicitly and must be reviewed when business scope changes.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Sequence
from urllib.parse import quote


EVALUATOR_VERSION = "source-fact-coverage-v0.1"


@dataclass(frozen=True)
class RelationRequirement:
    relation_type: str
    target_name: str
    status: str = "confirmed"
    metadata: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True)
class StatementRequirement:
    """All relations and controls must belong to the same indexed statement."""

    program_name: str
    name: str
    relations: tuple[RelationRequirement, ...] = ()
    controls: tuple[tuple[str, str], ...] = ()
    forbidden_relation_types: tuple[str, ...] = ()
    unit_type: str = "Statement"


@dataclass(frozen=True)
class CoverageObligation:
    obligation_id: str
    description: str
    requirements: tuple[StatementRequirement, ...]


@dataclass(frozen=True)
class ReviewedGap:
    """A manually reviewed gap; indexed fact matches cannot discharge it."""

    obligation_id: str
    description: str
    reason: str
    witnesses: tuple[StatementRequirement, ...] = ()


@dataclass(frozen=True)
class CoverageProfile:
    profile_id: str
    source_obligations: tuple[CoverageObligation, ...]
    reviewed_gaps: tuple[ReviewedGap, ...] = ()
    boundaries: tuple[tuple[str, str], ...] = ()


def _text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value).strip()).upper().rstrip(".")


def _metadata_matches(actual: dict[str, object], expected: tuple) -> bool:
    return all(
        _text(actual.get(key)) == _text(value)
        if isinstance(value, str)
        else type(actual.get(key)) is type(value) and actual.get(key) == value
        for key, value in expected
    )


class _SnapshotFacts:
    def __init__(self, connection: sqlite3.Connection):
        self.units = {
            row["unit_id"]: dict(row)
            for row in connection.execute("SELECT * FROM code_units")
        }
        self.relations: dict[str, list[dict[str, object]]] = defaultdict(list)
        for row in connection.execute("SELECT * FROM relations"):
            relation = dict(row)
            relation["metadata"] = json.loads(relation["metadata_json"])
            self.relations[relation["from_entity_id"]].append(relation)
        self.evidence = {
            row["evidence_id"]: dict(row)
            for row in connection.execute("SELECT * FROM evidence_spans")
        }
        self.files = {
            row["relative_path"]: row["sha256"]
            for row in connection.execute("SELECT relative_path, sha256 FROM source_files")
        }

    def evidence_ref(self, evidence_id: str) -> dict[str, object] | None:
        row = self.evidence.get(evidence_id)
        if row is None or row["source_sha256"] != self.files.get(row["relative_path"]):
            return None
        if not 0 < row["start_line"] <= row["end_line"]:
            return None
        return {
            "evidence_id": evidence_id,
            "relative_path": row["relative_path"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "source_sha256": row["source_sha256"],
            "span_sha256": hashlib.sha256(row["text"].encode("utf-8")).hexdigest(),
        }

    def match(self, requirement: StatementRequirement) -> list[dict[str, object]]:
        matches: list[dict[str, object]] = []
        for unit in self.units.values():
            if (
                unit["program_name"] != requirement.program_name
                or unit["name"] != requirement.name
                or unit["unit_type"] != requirement.unit_type
                or unit["parse_status"] != "complete"
            ):
                continue
            relations = self.relations.get(unit["unit_id"], [])
            if any(r["relation_type"] in requirement.forbidden_relation_types for r in relations):
                continue
            evidence_ids = {unit["evidence_id"]}
            satisfied = True
            for expected in requirement.relations:
                found = [
                    r for r in relations
                    if r["relation_type"] == expected.relation_type
                    and r["target_name"] == expected.target_name
                    and r["status"] == expected.status
                    and _metadata_matches(r["metadata"], expected.metadata)
                ]
                if not found:
                    satisfied = False
                    break
                evidence_ids.update(r["evidence_id"] for r in found)
                for relation in found:
                    target = self.units.get(relation["target_entity_id"])
                    if target is not None:
                        evidence_ids.add(target["evidence_id"])
            if not satisfied:
                continue
            for condition_text, outcome in requirement.controls:
                found_controls = []
                for relation in relations:
                    condition = self.units.get(relation["target_entity_id"])
                    if (
                        relation["relation_type"] == "CONTROL_DEPENDS_ON"
                        and relation["status"] == "confirmed"
                        and relation["metadata"].get("outcome") == outcome
                        and condition is not None
                        and condition["unit_type"] == "Condition"
                        and condition["parse_status"] == "complete"
                        and _text(condition["normalized_text"]) == _text(condition_text)
                    ):
                        found_controls.append(condition)
                if not found_controls:
                    satisfied = False
                    break
                evidence_ids.update(c["evidence_id"] for c in found_controls)
            refs = [self.evidence_ref(item) for item in sorted(evidence_ids)]
            if satisfied and all(ref is not None for ref in refs):
                matches.append({"entity_id": unit["unit_id"], "evidence_refs": refs})
        return sorted(matches, key=lambda item: item["entity_id"])


def _evaluate_requirements(
    facts: _SnapshotFacts, requirements: Sequence[StatementRequirement]
) -> tuple[list[int], list[dict[str, object]]]:
    missing: list[int] = []
    refs: dict[str, dict[str, object]] = {}
    for number, requirement in enumerate(requirements, start=1):
        matches = facts.match(requirement)
        if not matches:
            missing.append(number)
        for match in matches:
            for ref in match["evidence_refs"]:
                refs[ref["evidence_id"]] = ref
    return missing, sorted(
        refs.values(),
        key=lambda ref: (ref["relative_path"], ref["start_line"], ref["end_line"]),
    )


def evaluate_source_coverage(
    database_path: Path | str, profile: CoverageProfile | None = None
) -> dict[str, object]:
    """Check an explicitly selected gold case without writes or model calls.

    Evidence is bound to the stored snapshot; current working files are not
    re-read. Reviewed business gaps stay missing until a human revises the
    profile. Even a future fully covered profile cannot verify an Agent answer.
    """

    profile = CALC01_PROFILE if profile is None else profile
    if not profile.source_obligations:
        raise ValueError("A coverage profile must contain source obligations.")
    ids = [item.obligation_id for item in (*profile.source_obligations, *profile.reviewed_gaps)]
    ids.extend(item[0] for item in profile.boundaries)
    if len(set(ids)) != len(ids):
        raise ValueError("Coverage obligation IDs must be unique.")
    if any(not item.requirements for item in profile.source_obligations):
        raise ValueError("Source obligations must have at least one requirement.")
    path = Path(database_path).expanduser().resolve()
    connection = sqlite3.connect(f"file:{quote(path.as_posix(), safe='/')}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        facts = _SnapshotFacts(connection)
        obligations: list[dict[str, object]] = []
        for obligation in profile.source_obligations:
            missing, refs = _evaluate_requirements(facts, obligation.requirements)
            obligations.append({
                "obligation_id": obligation.obligation_id,
                "description": obligation.description,
                "kind": "indexed_source_fact",
                "status": "missing" if missing else "covered",
                "missing_requirement_numbers": missing,
                "evidence_refs": refs,
            })
        for gap in profile.reviewed_gaps:
            missing, refs = _evaluate_requirements(facts, gap.witnesses)
            obligations.append({
                "obligation_id": gap.obligation_id,
                "description": gap.description,
                "kind": "reviewed_business_gap",
                "status": "missing",
                "reason": gap.reason,
                "review_status": "reassessment_required" if missing else "not_automatically_discharged",
                "evidence_refs": refs,
            })
        for boundary_id, reason in profile.boundaries:
            obligations.append({
                "obligation_id": boundary_id,
                "kind": "boundary",
                "status": "boundary",
                "reason": reason,
                "evidence_refs": [],
            })
        counts = Counter(item["status"] for item in obligations)
        return {
            "profile_id": profile.profile_id,
            "evaluator_version": EVALUATOR_VERSION,
            "snapshot_id": metadata.get("snapshot_id", "unknown"),
            "index_schema_version": metadata.get("schema_version", "unknown"),
            "evaluation_scope": "indexed_source_fact_coverage",
            "evidence_integrity_scope": "stored_snapshot_not_current_working_files",
            "status": "PARTIAL" if counts["missing"] else "SOURCE_FACTS_COVERED_WITH_BOUNDARIES",
            "full_business_analysis_verified": False,
            "question_understanding_tested": False,
            "agent_retrieval_tested": False,
            "answer_completeness_tested": False,
            "runtime_execution_tested": False,
            "summary": {key: counts[key] for key in ("covered", "missing", "boundary")},
            "obligations": obligations,
        }
    finally:
        connection.close()


def _formula(
    program: str, target: str, expression: str, reads: tuple[str, ...],
    controls: tuple[tuple[str, str], ...] = (),
) -> StatementRequirement:
    return StatementRequirement(program, "COMPUTE", (
        RelationRequirement("WRITES", target, metadata=(("expression", expression), ("rounded", True))),
        *(RelationRequirement("READS", name) for name in reads),
    ), controls=controls)


def _move(program: str, target: str, expression: str, condition: str = "") -> StatementRequirement:
    return StatementRequirement(program, "MOVE", (
        RelationRequirement("WRITES", target, metadata=(("operation", "MOVE"), ("expression", expression))),
    ), controls=((condition, "true"),) if condition else ())


def _sql(program: str, table: str, writes: tuple[str, ...], reads: tuple[str, ...]) -> StatementRequirement:
    return StatementRequirement(program, "EXEC_SQL", (
        RelationRequirement("SELECTS_FROM", table, "unresolved"),
        *(RelationRequirement("WRITES", name) for name in writes),
        *(RelationRequirement("READS", name) for name in reads),
    ))


_ANNUAL = _formula("SYNP040", "WS-ANNUAL-PREMIUM",
    "WS-BASE-PREMIUM + WS-OCCUPATION-LOADING + WS-RIDER-PREMIUM-TOTAL - WS-HIGH-SUM-DISCOUNT + WS-POLICY-FEE",
    ("WS-BASE-PREMIUM", "WS-OCCUPATION-LOADING", "WS-RIDER-PREMIUM-TOTAL", "WS-HIGH-SUM-DISCOUNT", "WS-POLICY-FEE"))
_INSTALMENT = _formula("SYNP040", "OUT-INSTALMENT-PREMIUM",
    "WS-ANNUAL-PREMIUM * WS-MODE-FACTOR", ("WS-ANNUAL-PREMIUM", "WS-MODE-FACTOR"))
_RIDER = _formula("SYNP030", "WS-RIDER-PREMIUM-TOTAL",
    "IN-RIDER-SUM-ASSURED / 1000 * WS-RIDER-RATE", ("IN-RIDER-SUM-ASSURED", "WS-RIDER-RATE"),
    (("IF IN-RIDER-STATUS = 'A'", "true"), ("IF SQLCODE = ZERO", "true")))
_MODE_SQL = _sql("SYNP040", "SYN_MODE_FACTOR", ("WS-MODE-FACTOR",), ("IN-PAYMENT-MODE",))
_BASE_SQL = _sql("SYNP100", "SYN_BASE_RATE", ("WS-BASE-RATE",),
    ("IN-PRODUCT-CODE", "IN-BASE-COVER-CODE", "IN-ATTAINED-AGE", "IN-SMOKER-FLAG", "LK-RATE-VERSION"))


CALC01_PROFILE = CoverageProfile(
    profile_id="CALC-01-source-facts-v1",
    source_obligations=(
        CoverageObligation("annual_components", "年化金额的五项组成和舍入标记", (_ANNUAL,)),
        CoverageObligation("instalment_formula", "分期金额的乘法和舍入标记", (_INSTALMENT,)),
        CoverageObligation("candidate_base_formulas", "两个候选程序各自的基础计算，不证明实际调用目标", tuple(
            _formula(program, "WS-BASE-PREMIUM", "IN-BASE-SUM-ASSURED / 1000 * WS-BASE-RATE",
                ("IN-BASE-SUM-ASSURED", "WS-BASE-RATE"), (("IF SQLCODE = ZERO", "true"),))
            for program in ("SYNP100", "SYNP200")
        )),
        CoverageObligation("occupation_formula", "职业加费计算", (
            _formula("SYNP040", "WS-OCCUPATION-LOADING", "WS-BASE-PREMIUM * WS-OCCUPATION-PERCENT / 100",
                ("WS-BASE-PREMIUM", "WS-OCCUPATION-PERCENT")),)),
        CoverageObligation("discount_condition", "折扣先清零，并在达到门槛时计算", (
            _move("SYNP040", "WS-HIGH-SUM-DISCOUNT", "ZERO"),
            _formula("SYNP040", "WS-HIGH-SUM-DISCOUNT", "WS-BASE-PREMIUM * WS-DISCOUNT-PERCENT / 100",
                ("WS-BASE-PREMIUM", "WS-DISCOUNT-PERCENT"),
                (("IF IN-BASE-SUM-ASSURED >= WS-DISCOUNT-THRESHOLD", "true"),)),)),
        CoverageObligation("single_active_rider", "单个附加保障的有效状态和费率成功条件", (_RIDER,)),
        CoverageObligation("mode_factor_sql", "同一 SQL 中缴费因子的输入、输出和表引用", (_MODE_SQL,)),
        CoverageObligation("adjustment_sql", "职业、折扣、费用的 SQL 输入与输出", (
            _sql("SYNP040", "SYN_OCC_LOAD", ("WS-OCCUPATION-PERCENT",), ("IN-OCCUPATION-CLASS",)),
            _sql("SYNP040", "SYN_DISCOUNT", ("WS-DISCOUNT-THRESHOLD", "WS-DISCOUNT-PERCENT"), ("IN-PRODUCT-CODE",)),
            _sql("SYNP040", "SYN_POLICY_FEE", ("WS-POLICY-FEE",), ("IN-PRODUCT-CODE",)),)),
        CoverageObligation("base_rate_sql", "候选基础计算器中的五个费率输入", (_BASE_SQL,)),
        CoverageObligation("route_sql", "路由查询的产品和日期输入、程序名和版本输出", (
            _sql("SYNP020", "SYN_CALC_ROUTING", ("LK-CALCULATOR-PROGRAM", "LK-RATE-VERSION"),
                ("IN-PRODUCT-CODE", "IN-CALCULATION-DATE")),)),
        CoverageObligation("policy_status_condition", "完整保单状态条件：既非 P 又非 I 时出错", (
            _move("SYNP000", "RETURN-CODE", "12", "IF IN-POLICY-STATUS NOT = 'P' AND IN-POLICY-STATUS NOT = 'I'"),)),
        CoverageObligation("positive_sum_condition", "非正保额触发错误码", (
            _move("SYNP000", "RETURN-CODE", "16", "IF IN-BASE-SUM-ASSURED NOT > ZERO"),)),
        CoverageObligation("error_output_clearing", "错误返回时年化和分期两个输出都清零", tuple(
            _move("SYNP090", target, "ZERO", "IF RETURN-CODE NOT = ZERO")
            for target in ("OUT-ANNUAL-PREMIUM", "OUT-INSTALMENT-PREMIUM")
        )),
        CoverageObligation("literal_call_error_gates", "有前序错误时四个静态业务步骤不进入", tuple(
            StatementRequirement("SYNP000", "CALL", (RelationRequirement("CALLS", target),),
                controls=(("IF RETURN-CODE = ZERO", "true"),))
            for target in ("SYNP010", "SYNP020", "SYNP030", "SYNP040")
        )),
        CoverageObligation("dynamic_call_boundary", "动态调用仅解析程序名字段，不伪造确定程序调用边", (
            StatementRequirement("SYNP000", "CALL", (
                RelationRequirement("CALL_TARGET_FROM", "LK-CALCULATOR-PROGRAM", metadata=(
                    ("call_form", "identifier"), ("boundary", "runtime_target_requires_value_flow"))),),
                controls=(("IF RETURN-CODE = ZERO", "true"),), forbidden_relation_types=("CALLS",)),)),
        CoverageObligation("scoped_parameter_definition", "参数字段在使用程序范围内有定义证据", (
            StatementRequirement("SYNP040", "WS-MODE-FACTOR", unit_type="DataItem"),)),
    ),
    reviewed_gaps=(
        ReviewedGap("all_active_riders", "全部有效附加保障的遍历和累计",
            "已审查的合成夹具只有单个附加保障计算；尚未证明集合遍历、逐项累计及其错误传播。此缺口须人工复核后更新金标准。", (_RIDER,)),
        ReviewedGap("every_adjustment_failure", "每个必要调整查询的失败处理",
            "已审查的调整程序仅在职业查询后检查 SQLCODE；折扣、缴费因子和费用查询的独立失败路径尚未证明。", (_MODE_SQL,)),
        ReviewedGap("effective_base_rate", "基础费率按计算基准日生效",
            "已审查的基础费率查询没有计算基准日条件；路由的日期筛选不能替代费率自身的时点证明。", (_BASE_SQL,)),
    ),
    boundaries=(
        ("parameter_value_flow", "程序范围内 COPY 字段解析不等于 CALL USING 到 LINKAGE 的位置和值流映射。"),
        ("runtime_configuration", "仅有 SQL 语句，未读取数据库定义、有效控制表记录或运行输入；不能确定动态目标、唯一命中或具体金额。"),
        ("numeric_execution", "已检查公式和 ROUNDED 标记，未执行 COBOL，也未完整核验精度、溢出和所有路径。"),
        ("agent_answer_acceptance", "本报告不运行自然语言规划或真实模型，不验证检索完整度和最终回答完整度。"),
    ),
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="Existing structural-index SQLite snapshot")
    args = parser.parse_args(argv)
    report = evaluate_source_coverage(args.database)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["summary"]["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
