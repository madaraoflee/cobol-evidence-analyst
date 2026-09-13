"""Bounded abstract paths for local CALL/COMPUTE exceptional outcomes.

Paths are alternatives in an explicit source model, not executions or a proof
that an exception can occur with a real input. Callees are not executed here.
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

from call_bindings import BindingContext
from call_contexts import _Facts, audit_call_contexts
from error_paths import ErrorContract
from exception_cfg import build_exception_cfg


_NUMBER = re.compile(r"[+-]?\d{1,18}(?:\.\d{1,9})?\Z")
_ZERO = {"ZERO", "ZEROS", "ZEROES"}


def _limit(value: int, maximum: int) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("Exceptional path budget is outside supported bounds.")


def _number(value: str) -> str | None:
    if value in _ZERO:
        return "0"
    if not isinstance(value, str) or not _NUMBER.fullmatch(value):
        return None
    try:
        return str(Decimal(value).normalize())
    except InvalidOperation:
        return None


def _fits(value: str | None, layout: dict) -> str | None:
    """Keep exact constants only if the receiving numeric PIC can represent them."""
    if value is None:
        return None
    number = Decimal(value)
    if number < 0 and not layout["signed"]:
        return None
    if abs(number) >= Decimal(10) ** layout["integer_digits"]:
        return None
    if max(0, -number.as_tuple().exponent) > layout["scale"]:
        return None
    return value


def _layout(picture: str) -> dict | None:
    if not re.fullmatch(r"S?(?:9(?:\([1-9][0-9]*\))?)+(?:V(?:9(?:\([1-9][0-9]*\))?)+)?", picture):
        return None
    integer, _, fraction = picture.lstrip("S").partition("V")
    def digits(part):
        return sum(int(m.group(1) or 1) for m in re.finditer(r"9(?:\(([0-9]+)\))?", part))
    total, scale = digits(integer), digits(fraction)
    if not 1 <= total + scale <= 18 or scale > 9:
        return None
    return {"integer_digits": total, "scale": scale, "signed": picture.startswith("S")}


def analyze_exception_paths(cfg: dict, contract: ErrorContract, field_layouts: dict,
                            call_effects: dict, *, max_states: int = 4000,
                            max_steps: int = 256, max_witnesses: int = 64) -> dict:
    """Explore bounded alternatives, checking output values at modeled exits.

    Unknown inputs remain unknown. Normal CALL returns havoc all confirmed
    reference arguments; COPY/VALUE inputs are not written back. The supported
    model excludes nonargument external/global effects and storage aliasing.
    """
    _limit(max_states, 50000)
    _limit(max_steps, 2048)
    _limit(max_witnesses, 256)
    if contract.program_name != cfg["program_name"] or not contract.output_fields:
        raise ValueError("A matching program contract with explicit outputs is required.")
    if (not contract.status_fields or len(contract.status_fields) > 32 or len(contract.output_fields) > 32
            or len(set(contract.status_fields)) != len(contract.status_fields)
            or len(set(contract.output_fields)) != len(contract.output_fields)):
        raise ValueError("Exceptional path field contracts must be bounded and unique.")
    if len(cfg["nodes"]) > 5000 or len(cfg["edges"]) > 15000 or len(field_layouts) > 2000:
        raise ValueError("Exceptional path input exceeds its fact budget.")
    if not isinstance(call_effects, dict) or len(call_effects) > 5000:
        raise ValueError("Call effects exceed the abstract model budget.")
    for effect in call_effects.values():
        if (not isinstance(effect, dict) or set(effect) != {"complete", "reference_fields"}
                or type(effect["complete"]) is not bool
                or not isinstance(effect["reference_fields"], list) or len(effect["reference_fields"]) > 2000
                or any(not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_$#@-]{0,63}", name)
                       for name in effect["reference_fields"])):
            raise ValueError("Call effects require a strict bounded reference-field declaration.")
    nodes = {n["node_id"]: n for n in cfg["nodes"]}
    if len(nodes) != len(cfg["nodes"]) or cfg["entry_node_id"] not in nodes:
        raise ValueError("CFG node identities or entry are inconsistent.")
    outgoing = defaultdict(list)
    for edge in cfg["edges"]:
        if edge["source"] not in nodes or edge["target"] not in nodes:
            raise ValueError("CFG edge endpoint is missing.")
        outgoing[edge["source"]].append(edge)
    for node in cfg["nodes"]:
        kind = node["kind"]
        terminal = kind in {"GOBACK", "BOUNDARY"} or kind == "EXIT" and node.get("exit_kind") == "program_fallthrough"
        expected = [] if terminal else {
            "IF": ["false", "true"], "CALL": ["exception", "normal"],
            "COMPUTE": ["normal", "size_error"],
        }.get(kind, ["next"])
        if sorted(e["outcome"] for e in outgoing[node["node_id"]]) != expected:
            raise ValueError("CFG alternatives are missing, duplicated or inconsistent with their node.")
    if any(name not in field_layouts for name in (*contract.status_fields, *contract.output_fields)):
        raise ValueError("Status and output fields require supported unambiguous numeric layouts.")
    for layout in field_layouts.values():
        if (set(layout) != {"integer_digits", "scale", "signed"}
                or type(layout["signed"]) is not bool
                or type(layout["integer_digits"]) is not int or type(layout["scale"]) is not int
                or not 1 <= layout["integer_digits"] + layout["scale"] <= 18
                or not 0 <= layout["scale"] <= 9 or layout["integer_digits"] < 0):
            raise ValueError("Invalid numeric layout in the abstract model.")
    events = {}
    for node in cfg["nodes"]:
        for edge in outgoing[node["node_id"]]:
            if edge["outcome"] not in {"exception", "size_error"}:
                continue
            key = node["node_id"] + ":" + edge["outcome"]
            events[key] = {"event_id": key, "node_id": node["node_id"],
                           "event_kind": edge["outcome"], "evidence_refs": node["evidence_refs"],
                           "terminal_paths": 0, "blocked_paths": 0,
                           "outputs": {f: Counter() for f in contract.output_fields},
                           "status_values": {f: set() for f in contract.status_fields},
                           "witness_ids": []}
    if len(events) > 256:
        raise ValueError("Exceptional event count exceeds its budget.")
    # Values are exact representable numeric constants or None (unknown).
    pending = deque([(cfg["entry_node_id"], {}, frozenset(), [])])
    processed = 0
    scheduled = 1
    boundaries = []
    boundary_keys = set()
    witnesses = []
    witnessed = set()
    witnesses_truncated = False
    terminal_count = 0
    truncated = bool(cfg.get("summary", {}).get("truncated"))

    def boundary(reason, node_id, faults):
        key = (reason, node_id)
        if key not in boundary_keys and len(boundaries) < 256:
            boundaries.append({"reason": reason, "node_id": node_id,
                               "evidence_refs": nodes[node_id]["evidence_refs"]})
            boundary_keys.add(key)
        for fault in faults:
            events[fault]["blocked_paths"] += 1

    while pending:
        node_id, state, faults, trace = pending.popleft()
        processed += 1
        node = nodes[node_id]
        kind = node["kind"]
        if len(trace) >= max_steps:
            truncated = True
            boundary("path_step_budget_exhausted", node_id, faults)
            continue
        if kind == "BOUNDARY":
            boundary(node.get("reason", "unsupported_source_boundary"), node_id, faults)
            continue
        terminal = kind == "GOBACK" or kind == "EXIT" and node.get("exit_kind") == "program_fallthrough"
        if terminal:
            terminal_count += 1
            outcome = {name: ("unknown" if state.get(name) is None else
                              "zero" if Decimal(state[name]) == 0 else "nonzero")
                       for name in contract.output_fields}
            for fault in faults:
                event = events[fault]
                event["terminal_paths"] += 1
                for name, value in outcome.items():
                    event["outputs"][name][value] += 1
                for name in contract.status_fields:
                    event["status_values"][name].add(state.get(name))
                category = (fault, tuple(sorted(outcome.items())))
                if category not in witnessed and len(witnesses) >= max_witnesses:
                    witnesses_truncated = True
                if category not in witnessed and len(witnesses) < max_witnesses:
                    witnessed.add(category)
                    witness_id = "path_" + str(len(witnesses) + 1)
                    witnesses.append({"witness_id": witness_id, "event_id": fault,
                                      "edges": trace, "exit_node_id": node_id,
                                      "output_values": {f: state.get(f) for f in contract.output_fields},
                                      "status_values": {f: state.get(f) for f in contract.status_fields},
                                      "scope": "abstract_model_path_not_runtime_trace"})
                    event["witness_ids"].append(witness_id)
            continue
        next_state = dict(state)
        allowed = None
        if kind == "MOVE":
            source = node["source"]
            value = _number(source)
            if value is None:
                if source not in field_layouts:
                    boundary("move_source_not_supported_numeric_field", node_id, faults)
                    continue
                value = state.get(source)
            if any(name not in field_layouts for name in node["targets"]):
                boundary("move_target_layout_not_supported", node_id, faults)
                continue
            for name in node["targets"]:
                next_state[name] = _fits(value, field_layouts[name])
        elif kind == "IF":
            if node["field"] not in field_layouts or _number(node["value"]) is None:
                boundary("condition_operand_not_supported", node_id, faults)
                continue
            if state.get(node["field"]) is not None:
                left, right = Decimal(state[node["field"]]), Decimal(_number(node["value"]))
                operators = {"=": left == right, "<>": left != right, "<": left < right,
                             "<=": left <= right, ">": left > right, ">=": left >= right}
                if node["operator"] not in operators:
                    boundary("condition_operator_not_supported", node_id, faults)
                    continue
                allowed = "true" if operators[node["operator"]] else "false"
        elif kind not in {"ENTRY", "JOIN", "EXIT", "CALL", "COMPUTE", "CONTINUE"}:
            boundary("node_semantics_not_supported", node_id, faults)
            continue
        edges = outgoing[node_id]
        if not edges:
            boundary("missing_modeled_exit_or_successor", node_id, faults)
        for edge in edges:
            outcome = edge["outcome"]
            if allowed and outcome != allowed:
                continue
            child_state = dict(next_state)
            child_faults = faults
            if outcome in {"exception", "size_error"}:
                child_faults = faults | {node_id + ":" + outcome}
            if kind == "CALL" and outcome == "normal":
                effect = call_effects.get(node["callsite_id"])
                if effect is None or not effect["complete"]:
                    boundary("normal_call_effects_not_resolved", node_id, child_faults)
                    continue
                for name in effect["reference_fields"]:
                    child_state.pop(name, None)
            if kind == "COMPUTE":
                if node["target"] not in field_layouts:
                    boundary("compute_target_layout_not_supported", node_id, child_faults)
                    continue
                if outcome == "normal" or not node.get("size_error_receiver_preserved", False):
                    child_state.pop(node["target"], None)
            if scheduled >= max_states:
                truncated = True
                boundary("path_state_budget_exhausted", node_id, child_faults)
                continue
            scheduled += 1
            pending.append((edge["target"], child_state, child_faults, [*trace, edge]))
    reports = []
    for event in events.values():
        findings = {}
        for name, counts in event["outputs"].items():
            if counts["nonzero"]:
                finding = "nonzero_exit_possible_in_model"
            elif (counts["zero"] and not counts["unknown"] and not event["blocked_paths"] and not truncated):
                finding = "zero_on_all_modeled_exits"
            elif not event["terminal_paths"] and not event["blocked_paths"] and not truncated:
                finding = "not_reached_in_model"
            else:
                finding = "unproven"
            findings[name] = {"finding": finding, "exit_values": dict(counts)}
        reports.append({**event, "outputs": findings,
                        "status_values": {name: sorted(values, key=lambda value: (value is None, value or ""))
                                          for name, values in event["status_values"].items()},
                        "runtime_path_verified": False})
    return {
        "snapshot_id": cfg["snapshot_id"], "program_name": contract.program_name,
        "evaluation_scope": "bounded_local_abstract_exception_paths",
        "complete": False, "full_control_flow_proven": False, "runtime_execution_tested": False,
        "events": reports, "witnesses": witnesses, "boundaries": boundaries,
        "summary": {"states_processed": processed, "states_scheduled": scheduled,
                    "modeled_exits": terminal_count, "exceptional_events": len(events),
                    "truncated": truncated, "witnesses": len(witnesses),
                    "witnesses_truncated": witnesses_truncated},
        "assumptions": [
            "CALL normal return havocs confirmed reference arguments; callees are not executed.",
            "External/global side effects, storage aliases, reentrancy and actual compiler behavior are not modeled.",
            "Exceptional outcomes are nondeterministic source alternatives, not proof of feasible failures.",
            "Handled single-target COMPUTE preserves its prior receiver on size error; normal result is unknown.",
            "Unknown values in supported numeric comparisons explore both branches; unsupported conditions stop at boundaries.",
            "Modeled counterexamples may be infeasible at runtime.",
        ],
    }


def audit_exception_paths(database_path: Path, program_name: str, contract: ErrorContract,
                          *, max_states: int = 4000, max_steps: int = 256,
                          max_witnesses: int = 64) -> dict:
    cfg = build_exception_cfg(database_path, program_name)
    contexts = audit_call_contexts(database_path, program_name)
    path = Path(database_path).resolve()
    connection = sqlite3.connect(f"file:{quote(path.as_posix(), safe='/')}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        facts = _Facts(connection)
        if cfg["snapshot_id"] != contexts["snapshot_id"] or cfg["snapshot_id"] != facts.snapshot_id:
            raise ValueError("The source snapshot changed between exceptional-path analyses.")
        binder = BindingContext(connection, facts.normalize_source)
        layouts = {}
        for (scope, name), symbols in binder.fields.items():
            if scope != program_name or len(symbols) != 1:
                continue
            try:
                members, layout, _ = binder.layout(symbols[0])
            except ValueError:
                continue
            if len(members) == 1 and layout[0][1] == "SCALAR":
                numeric = _layout(layout[0][2])
                if numeric:
                    facts.verify_unit(facts.units[symbols[0]["definition_unit_id"]])
                    layouts[name] = numeric
        effects = {}
        for context in contexts["contexts"]:
            if context["depth"] != 1:
                continue
            effects[context["via_callsite_id"]] = {
                "complete": context["parameter_mapping_complete"],
                "reference_fields": sorted({m["caller_field"]["name"] for m in context["parameter_mappings"]
                                             if m["passing_mode"] == "REFERENCE"}),
            }
        result = analyze_exception_paths(cfg, contract, layouts, effects,
            max_states=max_states, max_steps=max_steps, max_witnesses=max_witnesses)
        return {**result, "cfg": cfg, "call_effect_context_summary": contexts["summary"]}
    finally:
        connection.close()
