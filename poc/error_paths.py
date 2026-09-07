#!/usr/bin/env python3
"""Bounded, offline error-flow observations over an indexed COBOL snapshot.

The caller supplies the meaning of status/output fields explicitly. This audit
does not infer business semantics from names and is not a control-flow proof.
It records source shapes, missing checks and possible overwrites with evidence.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Sequence
from urllib.parse import quote


AUDITOR_VERSION = "bounded-error-flow-v0.1"
_IDENTIFIER = r"[A-Z][A-Z0-9_$#@-]*"
_ZERO = r"(?:0+|ZERO|ZEROS|ZEROES)"
_ZERO_RE = re.compile(rf"{_ZERO}\Z")


@dataclass(frozen=True)
class ErrorContract:
    """Reviewed field roles, not a claim that the program always honors them."""

    program_name: str
    status_fields: tuple[str, ...]
    output_fields: tuple[str, ...] = ()


def _text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().upper().rstrip(".")


class _Facts:
    def __init__(self, connection: sqlite3.Connection):
        for table, limit in (("code_units", 100000), ("relations", 500000),
                             ("evidence_spans", 100000)):
            if connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > limit:
                raise ValueError("Index exceeds the bounded error audit fact budget.")
        source_bytes = connection.execute(
            "SELECT COALESCE(SUM(LENGTH(CAST(text AS BLOB))), 0) FROM evidence_spans"
        ).fetchone()[0]
        if source_bytes > 32_000_000:
            raise ValueError("Index exceeds the bounded error audit evidence budget.")
        self.units = {row["unit_id"]: dict(row) for row in connection.execute("SELECT * FROM code_units")}
        self.relations: dict[str, list[dict]] = defaultdict(list)
        for row in connection.execute("SELECT * FROM relations"):
            relation = dict(row)
            relation["metadata"] = json.loads(relation["metadata_json"])
            self.relations[relation["from_entity_id"]].append(relation)
        self.evidence = {row["evidence_id"]: dict(row) for row in connection.execute("SELECT * FROM evidence_spans")}
        self.files = dict(connection.execute("SELECT relative_path, sha256 FROM source_files"))
        self.symbols = {row["symbol_id"]: dict(row) for row in connection.execute("SELECT * FROM symbols")}
        self.programs: dict[str, list[dict]] = defaultdict(list)
        self.statements: dict[str, list[dict]] = defaultdict(list)
        for unit in self.units.values():
            if unit["unit_type"] == "Program":
                self.programs[unit["program_name"]].append(unit)
            if unit["unit_type"] == "Statement":
                self.statements[unit["program_name"]].append(unit)
        for statements in self.statements.values():
            statements.sort(key=lambda unit: (unit["relative_path"], unit["start_line"], unit["unit_id"]))
        self.selectors: dict[str, str] = {}
        self.evaluate_owners: dict[str, str] = {}
        for name in self.programs:
            stack: list[tuple[str, str]] = []
            program_units = sorted(
                (u for u in self.units.values() if u["program_name"] == name
                 and u["unit_type"] in {"Statement", "Condition", "Paragraph"}),
                key=lambda u: (u["relative_path"], u["start_line"],
                               0 if u["unit_type"] == "Statement" else 1),
            )
            for unit in program_units:
                if unit["unit_type"] == "Paragraph":
                    stack.clear()
                elif unit["unit_type"] == "Statement" and unit["name"] == "EVALUATE":
                    stack.append((_text(unit["normalized_text"])[len("EVALUATE "):], unit["unit_id"]))
                elif unit["unit_type"] == "Statement" and unit["name"] == "END_EVALUATE":
                    if stack:
                        stack.pop()
                elif unit["unit_type"] == "Condition" and unit["name"] == "WHEN" and stack:
                    self.selectors[unit["unit_id"]] = stack[-1][0]
                    self.evaluate_owners[unit["unit_id"]] = stack[-1][1]

    def refs(self, *ids: str) -> list[dict]:
        refs = []
        for evidence_id in sorted(set(ids)):
            row = self.evidence.get(evidence_id)
            if row is None or row["source_sha256"] != self.files.get(row["relative_path"]):
                raise ValueError("Evidence is not bound to the stored source snapshot.")
            if not 0 < row["start_line"] <= row["end_line"]:
                raise ValueError("Evidence has an invalid source range.")
            refs.append({key: row[key] for key in (
                "evidence_id", "relative_path", "start_line", "end_line", "source_sha256"
            )} | {"span_sha256": hashlib.sha256(row["text"].encode("utf-8")).hexdigest()})
        return refs

    def controls(self, unit: dict) -> list[dict]:
        result = []
        for relation in self.relations[unit["unit_id"]]:
            if relation["relation_type"] != "CONTROL_DEPENDS_ON":
                continue
            condition = self.units.get(relation["target_entity_id"])
            if condition is None:
                continue
            result.append({
                "condition_id": condition["unit_id"],
                "condition": _text(condition["normalized_text"]),
                "outcome": relation["metadata"].get("outcome"),
                "selector": self.selectors.get(condition["unit_id"]),
                "evaluate_id": self.evaluate_owners.get(condition["unit_id"]),
                "confirmed": relation["status"] == "confirmed" and condition["parse_status"] == "complete",
                "evidence_id": condition["evidence_id"],
            })
        return result

    def observation(self, unit: dict, kind: str, *, event_controls: Sequence[dict] | None = None,
                    **details: object) -> dict:
        controls = self.controls(unit) if event_controls is None else event_controls
        return {
            "kind": kind, "program_name": unit["program_name"],
            "entity_id": unit["unit_id"], "statement": _text(unit["normalized_text"]),
            "evidence_refs": self.refs(unit["evidence_id"], *(c["evidence_id"] for c in controls)),
            "controls": [{k: v for k, v in c.items() if k != "evidence_id"} for c in controls],
            **details,
        }


def _guard_role(control: dict, fields: Sequence[str]) -> str | None:
    """Only simple zero comparisons, never guesses about named conditions."""
    if not control["confirmed"]:
        return None
    selector = control.get("selector")
    if selector in fields:
        value = re.sub(r"^WHEN\s+", "", control["condition"])
        if _ZERO_RE.fullmatch(value):
            return "success"
        if re.fullmatch(r"[+-]?\d+|OTHER", value):
            return "error"
        return None
    for field in fields:
        match = re.fullmatch(
            rf"IF\s+{re.escape(field)}\s+(?:(NOT)\s+)?(=|EQUAL(?:\s+TO)?|<>|NOT\s*=)\s+{_ZERO}",
            control["condition"],
        )
        if match:
            success = not match.group(1) and match.group(2) not in {"<>", "NOT ="}
            if control["outcome"] == "false":
                success = not success
            elif control["outcome"] != "true":
                return None
            return "success" if success else "error"
    return None


def _compatible(left: Sequence[dict], right: Sequence[dict]) -> bool:
    """Opposite arms of the same invocation cannot both execute.

    Distinct comparisons are not assumed contradictory: intervening writes may
    have changed a status field. This deliberately over-reports possible writes.
    """
    for first in left:
        for second in right:
            if (first["condition_id"] == second["condition_id"]
                    and first.get("invocation") == second.get("invocation")
                    and first["outcome"] != second["outcome"]
                    and first["outcome"] in {"true", "false"}
                    and second["outcome"] in {"true", "false"}):
                return False
            if (first.get("evaluate_id") is not None
                    and first.get("evaluate_id") == second.get("evaluate_id")
                    and first.get("invocation") == second.get("invocation")
                    and first["condition_id"] != second["condition_id"]):
                return False
    return True


def _walk_local(facts: _Facts, program: str, max_depth: int, max_events: int) -> tuple[list[dict], list[dict]]:
    """Expand simple paragraph PERFORMs, retaining branch conditions as facts."""
    statements = facts.statements[program]
    procedure_lines = [u["start_line"] for u in facts.units.values()
                       if u["program_name"] == program and u["unit_type"] == "Division"
                       and u["name"] == "PROCEDURE"]
    if procedure_lines:
        statements = [u for u in statements if u["start_line"] > min(procedure_lines)]
    paragraphs = sorted((u for u in facts.units.values() if u["program_name"] == program
                         and u["unit_type"] == "Paragraph"), key=lambda u: u["start_line"])
    events: list[dict] = []
    boundaries: list[dict] = []
    invocation_count = 0

    def walk(items: Sequence[dict], stack: tuple[str, ...], inherited: list[dict]) -> bool:
        nonlocal invocation_count
        invocation_count += 1
        invocation = invocation_count
        for unit in items:
            if len(events) >= max_events:
                if not any(b["reason"] == "event_budget_exhausted" for b in boundaries):
                    boundaries.append(facts.observation(unit, "boundary", reason="event_budget_exhausted"))
                return False
            controls = inherited + [dict(c, invocation=invocation) for c in facts.controls(unit)]
            events.append({"unit": unit, "controls": controls, "order": len(events) + 1})
            name = unit["name"]
            if name in {"GOBACK", "STOP"}:
                if controls:
                    boundaries.append(facts.observation(unit, "boundary", reason="conditional_exit_not_expanded"))
                else:
                    return True
            if name == "GO":
                boundaries.append(facts.observation(unit, "boundary", reason="goto_control_flow_not_expanded"))
                return True
            if name != "PERFORM":
                continue
            relation = next((r for r in facts.relations[unit["unit_id"]]
                             if r["relation_type"] in {"PERFORMS", "PERFORMS_THRU"}), None)
            simple = re.fullmatch(rf"PERFORM\s+{_IDENTIFIER}(?:\s+(?:THRU|THROUGH)\s+{_IDENTIFIER})?",
                                  _text(unit["normalized_text"]))
            if not simple or relation is None or relation["status"] != "confirmed":
                boundaries.append(facts.observation(unit, "boundary", reason="perform_form_or_target_not_resolved"))
                continue
            target_symbol = facts.symbols.get(relation["target_entity_id"])
            target = facts.units.get(target_symbol["definition_unit_id"]) if target_symbol else None
            if target is None:
                boundaries.append(facts.observation(unit, "boundary", reason="perform_target_not_resolved"))
                continue
            if target["unit_id"] in stack or len(stack) >= max_depth:
                boundaries.append(facts.observation(unit, "boundary", reason=(
                    "recursive_perform" if target["unit_id"] in stack else "perform_depth_budget_exhausted")))
                continue
            end_name = relation["metadata"].get("range_end", target["name"])
            ends = [p for p in paragraphs if p["name"] == end_name and p["start_line"] >= target["start_line"]]
            if len(ends) != 1:
                boundaries.append(facts.observation(unit, "boundary", reason="perform_range_not_resolved"))
                continue
            after = [p["start_line"] for p in paragraphs if p["start_line"] > ends[0]["start_line"]]
            last_line = min(after) if after else 2**63
            body = [u for u in statements if target["start_line"] < u["start_line"] < last_line]
            if walk(body, (*stack, target["unit_id"]), controls):
                return True
        return False

    walk(statements, (), [])
    return events, boundaries


def _sql_checks(facts: _Facts, program: str, contract: ErrorContract) -> list[dict]:
    observations = []
    statements = facts.statements[program]
    for index, unit in enumerate(statements):
        if unit["name"] != "EXEC_SQL":
            continue
        following = []
        for candidate in statements[index + 1:]:
            if (candidate["parent_unit_id"] != unit["parent_unit_id"]
                    or candidate["name"] in {"EXEC_SQL", "CALL", "PERFORM", "GOBACK", "STOP", "GO"}):
                break
            following.append(candidate)
        guards = [u for u in following if u["name"] in {"IF", "EVALUATE"}
                  and re.search(r"\bSQLCODE\b", _text(u["normalized_text"]))]
        if not guards:
            observations.append(facts.observation(unit, "sql_error_check", status="missing",
                                                 reason="no_local_sqlcode_guard_before_next_effect"))
            continue
        guard = guards[0]
        failure_writes = []
        arms: set[str] = set()
        guard_condition_ids = {u["unit_id"] for u in facts.units.values()
                               if u["unit_type"] == "Condition" and u["parent_unit_id"] == guard["unit_id"]}
        base_condition_ids = {c["condition_id"] for c in facts.controls(unit)}
        direct_failure_write = False
        written_when_ids: set[str] = set()
        for candidate in following:
            if candidate["start_line"] <= guard["start_line"]:
                continue
            controls = facts.controls(candidate)
            error_controls = [c for c in controls if _guard_role(c, ("SQLCODE",)) == "error"
                              and facts.units[c["condition_id"]]["start_line"] >= guard["start_line"]]
            writes = [r for r in facts.relations[candidate["unit_id"]]
                      if r["relation_type"] == "WRITES" and r["target_name"] in contract.status_fields]
            if error_controls and writes:
                for relation in writes:
                    expression = _text(relation["metadata"].get("expression", ""))
                    if relation["metadata"].get("operation") == "MOVE" and re.fullmatch(r"[+-]?\d+", expression) and int(expression) != 0:
                        failure_writes.append(candidate)
                        arms.update(c["condition"] for c in error_controls)
                        written_when_ids.update(c["condition_id"] for c in error_controls if c.get("selector") == "SQLCODE")
                        control_ids = {c["condition_id"] for c in controls}
                        if (any(c["condition_id"] in guard_condition_ids for c in error_controls)
                                and control_ids <= base_condition_ids | guard_condition_ids):
                            direct_failure_write = True
        evaluations = [u for u in following if _text(u["normalized_text"]) == "EVALUATE SQLCODE"
                       and u["start_line"] >= guard["start_line"]]
        exhaustive_evaluation = False
        missing_arms = []
        for evaluation in evaluations[:1]:
            when_units = [facts.units[key] for key, owner in facts.evaluate_owners.items()
                          if owner == evaluation["unit_id"]]
            failure_units = [u for u in when_units if not _ZERO_RE.fullmatch(
                re.sub(r"^WHEN\s+", "", _text(u["normalized_text"])))]
            missing_arms = [_text(u["normalized_text"]) for u in failure_units
                            if u["unit_id"] not in written_when_ids]
            exhaustive_evaluation = (any(_text(u["normalized_text"]) == "WHEN OTHER" for u in failure_units)
                                     and not missing_arms)
        overwritten_sqlcode = any(
            u["start_line"] < guard["start_line"] and any(
                r["relation_type"] == "WRITES" and r["target_name"] == "SQLCODE"
                for r in facts.relations[u["unit_id"]]) for u in following)
        exhaustive_shape = (direct_failure_write or exhaustive_evaluation) and not overwritten_sqlcode
        status = "source_shape_observed" if exhaustive_shape else "missing"
        observation = facts.observation(unit, "sql_error_check", status=status,
            reason=("sqlcode_failure_arm_sets_nonzero_status" if exhaustive_shape
                    else "sqlcode_guard_without_exhaustive_nonzero_status_assignment"),
            failure_arms_with_status_assignment=sorted(arms), missing_failure_arms=sorted(missing_arms))
        observation["evidence_refs"] = facts.refs(unit["evidence_id"], guard["evidence_id"],
            *(u["evidence_id"] for u in failure_writes),
            *(c["evidence_id"] for u in failure_writes for c in facts.controls(u)))
        observations.append(observation)
    return observations


def audit_error_paths(
    database_path: Path | str, root_program: str, contracts: Sequence[ErrorContract], *,
    max_programs: int = 64, max_depth: int = 12, max_events: int = 5000,
) -> dict[str, object]:
    """Inspect bounded source flow, not runtime behavior or whole-answer quality.

    Literal-call reachability is separated from explicitly selected programs.
    Stored evidence is hash-bound; current working files are intentionally not
    read. Missing controls are findings, not evidence that errors occur at run time.
    """
    if not re.fullmatch(_IDENTIFIER, root_program):
        raise ValueError("The root program must be an uppercase program identifier.")
    if not (1 <= max_programs <= 256 and 1 <= max_depth <= 64 and 1 <= max_events <= 50000):
        raise ValueError("Error audit budgets are outside supported bounds.")
    selected = {}
    for contract in contracts:
        if not isinstance(contract, ErrorContract) or not contract.status_fields:
            raise ValueError("Each error contract requires explicit status fields.")
        if any(not re.fullmatch(_IDENTIFIER, name) for name in
               (contract.program_name, *contract.status_fields, *contract.output_fields)):
            raise ValueError("Contract identifiers must be uppercase and unqualified.")
        if contract.program_name in selected:
            raise ValueError("Program error contracts must be unique.")
        selected[contract.program_name] = contract
    if root_program not in selected or len(selected) > max_programs:
        raise ValueError("The bounded contract set must include the root program.")
    path = Path(database_path).expanduser().resolve()
    connection = sqlite3.connect(f"file:{quote(path.as_posix(), safe='/')}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", metadata.get("snapshot_id", "")):
            raise ValueError("The index does not contain a valid snapshot identifier.")
        facts = _Facts(connection)
        if root_program not in facts.programs:
            raise ValueError("Root program is not present in the index snapshot.")
        observations: list[dict] = []
        boundaries: list[dict] = []
        call_paths: list[dict] = []
        reachable: set[str] = set()

        def visit(program: str, chain: tuple[str, ...]) -> None:
            if program in chain:
                boundaries.append({"kind": "boundary", "reason": "recursive_call_chain",
                                   "program_path": [*chain, program], "evidence_refs": []})
                return
            if len(chain) >= max_depth or len(reachable) >= max_programs and program not in reachable:
                boundaries.append({"kind": "boundary", "reason": "call_graph_budget_exhausted",
                                   "program_path": [*chain, program], "evidence_refs": []})
                return
            if program in reachable:
                return
            reachable.add(program)
            for unit in facts.statements[program]:
                if unit["name"] != "CALL":
                    continue
                for relation in facts.relations[unit["unit_id"]]:
                    if relation["relation_type"] == "CALL_TARGET_FROM":
                        boundaries.append(facts.observation(unit, "boundary",
                            reason="dynamic_target_and_exception_outcome_not_proven",
                            target_field=relation["target_name"]))
                    if relation["relation_type"] != "CALLS":
                        continue
                    target = relation["target_name"]
                    call_paths.append(facts.observation(unit, "literal_call_path",
                        program_path=[*chain, program, target], resolution_status=relation["status"]))
                    if relation["status"] == "confirmed":
                        visit(target, (*chain, program))
                    else:
                        boundaries.append(facts.observation(unit, "boundary", reason="callee_not_uniquely_resolved"))

        visit(root_program, ())
        program_reports = []
        program_events: dict[str, list[dict]] = {}
        remaining_events = max_events
        program_scope = sorted(reachable | set(selected), key=lambda p: (p != root_program, p not in reachable, p))
        if len(program_scope) > max_programs:
            boundaries.append({"kind": "boundary", "reason": "program_scope_budget_exhausted",
                               "omitted_programs": program_scope[max_programs:], "evidence_refs": []})
        for program in program_scope[:max_programs]:
            contract = selected.get(program)
            if contract is None:
                boundaries.append({"kind": "boundary", "program_name": program,
                                   "reason": "status_output_contract_missing", "evidence_refs": []})
                continue
            definitions = facts.programs.get(program, [])
            if len(definitions) != 1:
                boundaries.append({"kind": "boundary", "program_name": program,
                                   "reason": "program_not_uniquely_resolved", "evidence_refs": []})
                continue
            known_fields = {s["name"] for s in facts.symbols.values()
                            if s["program_name"] == program and s["symbol_type"] == "Field"}
            for field in (*contract.status_fields, *contract.output_fields):
                if field not in known_fields and field not in {"RETURN-CODE", "SQLCODE"}:
                    boundaries.append({"kind": "boundary", "program_name": program,
                        "reason": "contract_field_not_defined", "field": field, "evidence_refs": []})
            events, local_boundaries = _walk_local(facts, program, max_depth, remaining_events)
            program_events[program] = events
            remaining_events -= len(events)
            boundaries.extend(local_boundaries)
            observations.extend(_sql_checks(facts, program, contract))
            status_origins = []
            clears = []
            exception_call = None
            for event in events:
                unit = event["unit"]
                source_text = _text(unit["normalized_text"])
                if unit["name"] == "CALL":
                    exception_call = unit if re.search(r"(?<!NOT )\bON\s+EXCEPTION\b", source_text) else None
                elif source_text in {"END-CALL", "NOT ON EXCEPTION"}:
                    exception_call = None
                if ((unit["parse_status"] != "complete" and source_text not in {"END-CALL", "EXIT"})
                        or unit["name"] in {"READ", "WRITE", "REWRITE", "START", "DELETE", "INITIALIZE", "SET"}):
                    boundaries.append(facts.observation(unit, "boundary", reason="statement_effects_not_fully_modeled"))
                if unit["name"] in {"CALL", "PERFORM"}:
                    guarded = any(_guard_role(c, contract.status_fields) == "success" for c in event["controls"])
                    observations.append(facts.observation(unit, "step_status_gate", order=event["order"],
                        event_controls=event["controls"],
                        status="source_shape_observed" if guarded else "not_observed",
                        reason="success_status_guard" if guarded else "step_without_simple_success_guard"))
                for relation in facts.relations[unit["unit_id"]]:
                    if relation["relation_type"] != "WRITES":
                        continue
                    target = relation["target_name"]
                    expression = _text(relation["metadata"].get("expression", ""))
                    is_move = relation["metadata"].get("operation") == "MOVE"
                    is_clear = is_move and bool(_ZERO_RE.fullmatch(expression))
                    if target in contract.status_fields:
                        nonzero = is_move and bool(re.fullmatch(r"[+-]?\d+", expression)) and int(expression) != 0
                        if nonzero:
                            status_origins.append((event, target))
                            observations.append(facts.observation(unit, "error_status_origin", field=target,
                                value=expression, order=event["order"], status="source_shape_observed",
                                origin_context="call_exception_clause" if exception_call else "local_statement",
                                callsite_id=exception_call["unit_id"] if exception_call else None))
                            if exception_call is not None:
                                observations.append(facts.observation(unit, "call_exception_status_assignment",
                                    field=target, value=expression, callsite_id=exception_call["unit_id"],
                                    status="source_clause_shape_only",
                                    evidence_refs=facts.refs(unit["evidence_id"], exception_call["evidence_id"]),
                                    reason="exception_clause_position_not_exception_control_flow_proof"))
                        elif is_clear:
                            for prior, prior_target in status_origins:
                                if prior_target == target and _compatible(prior["controls"], event["controls"]):
                                    observations.append(facts.observation(unit, "possible_error_status_reset", field=target,
                                        previous_entity_id=prior["unit"]["unit_id"], status="review_required",
                                        evidence_refs=facts.refs(unit["evidence_id"], prior["unit"]["evidence_id"],
                                            *(c["evidence_id"] for c in (*prior["controls"], *event["controls"])))))
                        elif is_move and re.fullmatch(_IDENTIFIER, expression):
                            reads = [r for r in facts.relations[unit["unit_id"]]
                                     if r["relation_type"] == "READS" and r["target_name"] == expression]
                            if reads:
                                observations.append(facts.observation(unit, "error_status_forwarding",
                                    event_controls=event["controls"],
                                    source_field=expression, target_field=target,
                                    order=event["order"], status="source_shape_observed",
                                    source_binding_status=reads[0]["status"],
                                    error_guard_observed=any(_guard_role(c, (expression,)) == "error" for c in event["controls"]),
                                    runtime_value_propagation_proven=False))
                    if target not in contract.output_fields:
                        continue
                    error_guard = any(_guard_role(c, contract.status_fields) == "error" for c in event["controls"])
                    if is_clear and exception_call is not None:
                        observations.append(facts.observation(unit, "call_exception_output_clear",
                            field=target, callsite_id=exception_call["unit_id"],
                            status="source_clause_shape_only", runtime_path_proven=False,
                            evidence_refs=facts.refs(unit["evidence_id"], exception_call["evidence_id"])))
                    if is_clear and error_guard:
                        clears.append((event, target))
                        observations.append(facts.observation(unit, "error_output_clear", field=target,
                            event_controls=event["controls"],
                            order=event["order"], status="source_shape_observed"))
                    elif not is_clear:
                        for prior, prior_target in clears:
                            if prior_target == target and _compatible(prior["controls"], event["controls"]):
                                observations.append(facts.observation(unit, "possible_output_overwrite_after_error_clear",
                                    field=target, previous_entity_id=prior["unit"]["unit_id"],
                                    evidence_refs=facts.refs(unit["evidence_id"], prior["unit"]["evidence_id"],
                                        *(c["evidence_id"] for c in (*prior["controls"], *event["controls"]))),
                                    status="review_required", reason="compatible_bounded_source_order_not_runtime_proof"))
            cleared_fields = {target for _, target in clears}
            for field in contract.output_fields:
                if field not in cleared_fields:
                    observations.append({"kind": "local_error_output_clear_not_observed", "program_name": program,
                        "field": field, "status": "not_observed", "scope": "local_program_only",
                        "reason": "not_a_claim_that_delegated_finalization_is_missing",
                        "evidence_refs": facts.refs(definitions[0]["evidence_id"])})
            program_reports.append({"program_name": program,
                "scope": "literal_call_reachable" if program in reachable else "explicit_selection_not_proven_reachable",
                "status_fields": list(contract.status_fields), "output_fields": list(contract.output_fields),
                "bounded_event_count": len(events), "error_output_fields_with_clear_shape": sorted(cleared_fields)})

        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        transfers = []
        all_bindings = []
        if "call_bindings" in tables:
            for row_number, row in enumerate(connection.execute("SELECT * FROM call_bindings LIMIT 100001")):
                if row_number >= 100000:
                    boundaries.append({"kind": "boundary", "reason": "parameter_transfer_budget_exhausted", "evidence_refs": []})
                    break
                binding = dict(row)
                if binding.get("caller_program") not in reachable | set(selected):
                    continue
                all_bindings.append(binding)
                if binding.get("status") != "confirmed":
                    call_unit = facts.units.get(binding.get("callsite_id"))
                    if call_unit is not None:
                        boundaries.append(facts.observation(call_unit, "boundary", reason="call_parameter_transfer_not_resolved",
                            binding_reason=binding.get("reason")))
                caller = facts.symbols.get(binding.get("caller_symbol_id"), {})
                callee = facts.symbols.get(binding.get("callee_symbol_id"), {})
                caller_contract = selected.get(binding.get("caller_program"))
                callee_contract = selected.get(binding.get("callee_program"))
                roles = set((caller_contract.status_fields + caller_contract.output_fields) if caller_contract else ())
                callee_roles = set((callee_contract.status_fields + callee_contract.output_fields) if callee_contract else ())
                if caller.get("name") not in roles and callee.get("name") not in callee_roles:
                    continue
                ids = [binding["evidence_id"]]
                ids.extend(json.loads(binding.get("supporting_evidence_ids_json", "[]")))
                transfers.append({"kind": "parameter_error_field_binding",
                    "caller_program": binding.get("caller_program"), "callee_program": binding.get("callee_program"),
                    "caller_field": caller.get("name"), "callee_field": callee.get("name"),
                    "passing_mode": binding.get("passing_mode"), "binding_status": binding.get("status"),
                    "reason": binding.get("reason"), "evidence_refs": facts.refs(*ids),
                    "runtime_writeback_proven": False})
        else:
            boundaries.append({"kind": "boundary", "reason": "parameter_bindings_unavailable", "evidence_refs": []})
        local_clears = {(o["program_name"], o["field"]): o for o in observations if o["kind"] == "error_output_clear"}
        overwrite_fields = {(o["program_name"], o["field"]) for o in observations
                            if o["kind"] == "possible_output_overwrite_after_error_clear"}
        for missing in [o for o in observations if o["kind"] == "local_error_output_clear_not_observed"]:
            caller_program, output_field = missing["program_name"], missing["field"]
            for binding in all_bindings:
                caller_symbol = facts.symbols.get(binding.get("caller_symbol_id"), {})
                callee_symbol = facts.symbols.get(binding.get("callee_symbol_id"), {})
                callee_program = binding.get("callee_program")
                clear = local_clears.get((callee_program, callee_symbol.get("name")))
                if (binding.get("caller_program") != caller_program or caller_symbol.get("name") != output_field
                        or binding.get("status") != "confirmed" or binding.get("passing_mode") != "REFERENCE"
                        or clear is None or (callee_program, callee_symbol.get("name")) in overwrite_fields):
                    continue
                caller_events = program_events.get(caller_program, [])
                calls = [e for e in caller_events if e["unit"]["unit_id"] == binding.get("callsite_id")]
                if len(calls) != 1 or calls[0]["controls"]:
                    continue
                call = calls[0]
                if any(e["order"] > call["order"] and (e["unit"]["name"] in {"CALL", "PERFORM"}
                       or any(r["relation_type"] == "WRITES" and r["target_name"] == output_field
                              and not (r["metadata"].get("operation") == "MOVE" and _ZERO_RE.fullmatch(
                                  _text(r["metadata"].get("expression", ""))))
                              for r in facts.relations[e["unit"]["unit_id"]])) for e in caller_events):
                    continue
                if any(e["order"] > clear["order"] and e["unit"]["name"] == "CALL"
                       for e in program_events.get(callee_program, [])):
                    continue
                caller_contract = selected[caller_program]
                callee_contract = selected.get(callee_program)
                status_bindings = [b for b in all_bindings if b.get("callsite_id") == binding.get("callsite_id")
                    and b.get("status") == "confirmed" and b.get("passing_mode") in {"REFERENCE", "CONTENT"}
                    and facts.symbols.get(b.get("caller_symbol_id"), {}).get("name") in caller_contract.status_fields
                    and callee_contract is not None
                    and facts.symbols.get(b.get("callee_symbol_id"), {}).get("name") in callee_contract.status_fields]
                if not status_bindings:
                    continue
                evidence_ids = [ref["evidence_id"] for ref in clear["evidence_refs"]]
                evidence_ids.append(binding["evidence_id"])
                for item in [binding, *status_bindings]:
                    evidence_ids.extend(json.loads(item.get("supporting_evidence_ids_json", "[]")))
                observations.append(facts.observation(call["unit"], "delegated_error_output_clear",
                    field=output_field, callee_program=callee_program, callee_field=callee_symbol["name"],
                    status="source_binding_candidate", runtime_path_proven=False,
                    evidence_refs=facts.refs(*evidence_ids),
                    reason="unconditional_last_call_maps_status_input_and_reference_output_to_local_clear"))
        counts = Counter(item["kind"] for item in observations)
        return {
            "auditor_version": AUDITOR_VERSION, "snapshot_id": metadata.get("snapshot_id", "unknown"),
            "root_program": root_program, "evaluation_scope": "bounded_source_error_flow_observations",
            "evidence_integrity_scope": "stored_snapshot_not_current_working_files",
            "complete": False, "full_control_flow_proven": False, "runtime_execution_tested": False,
            "status": "PARTIAL_SOURCE_AUDIT",
            "limitations": ["Branch feasibility, aliasing, fall-through and runtime data are not fully modeled.",
                "A call binding proves parameter position and mode, not execution or error propagation.",
                "Output clearing is a local source shape, not proof that every failure reaches that clear."],
            "budgets": {"max_programs": max_programs, "max_depth": max_depth, "max_events": max_events},
            "summary": {"programs_audited": len(program_reports), "literal_call_paths": len(call_paths),
                "parameter_error_bindings": len(transfers), "boundaries": len(boundaries),
                "observations_by_kind": dict(sorted(counts.items()))},
            "programs": program_reports, "call_paths": call_paths,
            "parameter_transfers": transfers, "observations": observations, "boundaries": boundaries,
        }
    finally:
        connection.close()
