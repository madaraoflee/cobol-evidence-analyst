"""Bounded, context-sensitive abstract error returns across source programs.

The model executes supported source CFG alternatives, not COBOL binaries.
Constants/unknown values flow through positional parameter bindings. Ordinary
CALL return is distinct from invocation failure; arithmetic results stay unknown.
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
import re
import sqlite3
from typing import Sequence
from urllib.parse import quote

from call_bindings import BindingContext, parse_signature
from call_contexts import _Facts, _id, audit_call_contexts
from error_paths import ErrorContract
from exception_cfg import build_exception_cfg
from exception_paths import _fits, _layout, _number
from structural_index import normalize_cobol_lines


AUDITOR_VERSION = "bounded-interprogram-paths-v0.1"
MAX_EVENTS = 1024
MAX_BOUNDARIES = 512
MAX_ROOT_EXITS = 4096
MAX_FIELDS = 2000
MAX_MODELS = 128
MAX_TRACE_PARAMETERS = 1024
MAX_CONTEXT_BINDINGS = 50_000
_NAME = re.compile(r"[A-Z][A-Z0-9_$#@-]{0,63}\Z")
_UNSAFE_STORAGE = re.compile(r"(?<![A-Z0-9_$#@-])(?:GLOBAL|EXTERNAL|REDEFINES|RENAMES|OCCURS|BASED|POINTER|RECURSIVE|REENTRANT)(?![A-Z0-9_$#@-])")


def _refs(*groups: Sequence[dict]) -> list[dict]:
    unique = {ref["evidence_id"]: ref for group in groups for ref in group}
    if len(unique) > 128:
        raise ValueError("Cross-program evidence chain exceeds its bounded scope.")
    return [unique[key] for key in sorted(unique)]


@dataclass(frozen=True)
class _Frame:
    program_name: str
    context_id: str
    node_id: str
    values: dict[str, str]
    origins: dict[str, list[dict]]
    return_node_id: str | None = None
    call_node: dict | None = None
    bindings: tuple[dict, ...] = ()


class _Model:
    def __init__(self, facts: _Facts, binder: BindingContext, database: Path, program: str,
                 contract: ErrorContract):
        self.program = program
        self.contract = contract
        self.cfg = build_exception_cfg(database, program)
        if self.cfg["snapshot_id"] != facts.snapshot_id:
            raise ValueError("Source snapshot changed between cross-program CFG analyses.")
        self.nodes = {node["node_id"]: node for node in self.cfg["nodes"]}
        self.outgoing: dict[str, list[dict]] = defaultdict(list)
        for edge in self.cfg["edges"]:
            if edge["source"] not in self.nodes or edge["target"] not in self.nodes:
                raise ValueError("Cross-program CFG has a missing edge endpoint.")
            self.outgoing[edge["source"]].append(edge)
        for node in self.nodes.values():
            kind = node["kind"]
            terminal = kind in {"GOBACK", "BOUNDARY"} or kind == "EXIT" and node.get("exit_kind") == "program_fallthrough"
            expected = [] if terminal else {"IF": ["false", "true"], "CALL": ["exception", "normal"],
                "COMPUTE": ["normal", "size_error"]}.get(kind, ["next"])
            if sorted(edge["outcome"] for edge in self.outgoing[node["node_id"]]) != expected:
                raise ValueError("Cross-program CFG alternatives are incomplete or duplicated.")
        self.layouts: dict[str, dict] = {}
        signature_units = facts.signatures[program]
        bound_linkage_symbols: set[str] = set()
        signature_supported = len(signature_units) == 1
        if signature_supported:
            try:
                for parameter in parse_signature(signature_units[0]["normalized_text"]):
                    roots = binder.fields.get((program, parameter.name), [])
                    if len(roots) != 1 or binder.storage(roots[0]) != "LINKAGE":
                        raise ValueError("Procedure parameter has no unique LINKAGE storage.")
                    members, _, _ = binder.layout(roots[0])
                    bound_linkage_symbols.update(member["symbol_id"] for member in members)
            except ValueError:
                signature_supported = False
        self.unbound_linkage: set[str] = set()
        for (scope, name), symbols in binder.fields.items():
            if scope != program or len(symbols) != 1:
                continue
            if binder.storage(symbols[0]) == "LINKAGE" and symbols[0]["symbol_id"] not in bound_linkage_symbols:
                self.unbound_linkage.add(name)
                continue
            try:
                members, layout, _ = binder.layout(symbols[0])
            except ValueError:
                continue
            if len(members) == 1 and layout[0][1] == "SCALAR":
                numeric = _layout(layout[0][2])
                if numeric:
                    facts.verify_unit(facts.units[symbols[0]["definition_unit_id"]])
                    self.layouts[name] = numeric
        if len(self.layouts) > MAX_FIELDS:
            raise ValueError("Cross-program numeric fields exceed their budget.")
        self.boundary_reason = None
        self.boundary_refs: list[dict] = []
        for unit in facts.units.values():
            if unit["program_name"] != program:
                continue
            text = re.sub(r"'[^']*'|\"[^\"]*\"", "", unit["normalized_text"].upper())
            if unit["unit_type"] in {"DataItem", "Program"} and _UNSAFE_STORAGE.search(text):
                self.boundary_reason = "shared_aliased_or_reentrant_storage_not_supported"
                self.boundary_refs = facts.refs(unit["evidence_id"])
                break
        if program in binder.incomplete:
            self.boundary_reason = "incomplete_copy_scope_not_supported"
        if not signature_supported:
            self.boundary_reason = "procedure_linkage_storage_scope_not_supported"
        if any(name not in self.layouts for name in (*contract.status_fields, *contract.output_fields)):
            self.boundary_reason = "contract_field_numeric_layout_not_supported"


def audit_interprogram_paths(
    database_path: Path, root_program: str, contracts: Sequence[ErrorContract], *,
    initial_values: dict[str, str | int] | None = None, call_policy: str = "explore",
    max_states: int = 20_000, max_steps: int = 512, max_contexts: int = 128,
    max_depth: int = 12, max_witnesses: int = 64,
) -> dict[str, object]:
    """Explore bounded cross-program source paths with numeric constants/unknown.

    Event output observations are not inferred causal associations or clearing
    obligations. A consumer selects relevant root outputs with explicit contracts.
    """
    if not isinstance(root_program, str) or not _NAME.fullmatch(root_program):
        raise ValueError("Root program must be an uppercase identifier.")
    if call_policy not in {"explore", "normal_return_only"}:
        raise ValueError("Call policy must be explore or normal_return_only.")
    for value, lower, upper in ((max_states, 1, 50000), (max_steps, 1, 2048),
                               (max_contexts, 1, 512), (max_depth, 0, 32), (max_witnesses, 0, 256)):
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError("Cross-program exploration budgets are outside supported bounds.")
    if not isinstance(contracts, (list, tuple)) or not 1 <= len(contracts) <= MAX_MODELS:
        raise ValueError("Explicit bounded program contracts are required.")
    selected: dict[str, ErrorContract] = {}
    for contract in contracts:
        if (not isinstance(contract, ErrorContract) or not isinstance(contract.program_name, str)
                or not _NAME.fullmatch(contract.program_name)
                or not isinstance(contract.status_fields, (list, tuple))
                or not isinstance(contract.output_fields, (list, tuple))
                or not 1 <= len(contract.status_fields) <= 32 or len(contract.output_fields) > 32
                or len(set(contract.status_fields)) != len(contract.status_fields)
                or len(set(contract.output_fields)) != len(contract.output_fields)
                or any(not isinstance(name, str) or not _NAME.fullmatch(name)
                       for name in (*contract.status_fields, *contract.output_fields))
                or contract.program_name in selected):
            raise ValueError("Program error contracts require unique bounded numeric field names.")
        selected[contract.program_name] = contract
    if root_program not in selected or not selected[root_program].output_fields:
        raise ValueError("Root error contract requires explicit output fields.")
    if initial_values is None:
        initial_values = {}
    if not isinstance(initial_values, dict) or len(initial_values) > 256:
        raise ValueError("Initial source values must be a bounded field mapping.")
    database = Path(database_path).expanduser().resolve()
    context_audit = audit_call_contexts(database, root_program, max_contexts=max_contexts,
                                       max_depth=max_depth, max_bindings=50000)
    connection = sqlite3.connect(f"file:{quote(database.as_posix(), safe='/')}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        facts = _Facts(connection)
        if facts.snapshot_id != context_audit["snapshot_id"]:
            raise ValueError("Source snapshot changed between parameter and path analyses.")
        binder = BindingContext(connection, normalize_cobol_lines)
        models: dict[str, _Model] = {}

        def model(program: str) -> _Model:
            if program not in models:
                if program not in selected:
                    raise ValueError("A called program has no reviewed error contract.")
                if len(models) >= MAX_MODELS:
                    raise ValueError("Cross-program model count exceeds its budget.")
                models[program] = _Model(facts, binder, database, program, selected[program])
            return models[program]

        root_model = model(root_program)
        values: dict[str, str] = {}
        for name, value in initial_values.items():
            if name not in root_model.layouts or type(value) not in {str, int}:
                raise ValueError("Initial values require supported numeric fields and exact constants.")
            numeric = _number(str(value))
            if numeric is None or _fits(numeric, root_model.layouts[name]) is None:
                raise ValueError("Initial value is not exactly representable by its numeric field.")
            values[name] = numeric
        root_id = _id("ipctx", facts.snapshot_id, root_program)
        contexts = [{"context_id": root_id, "parent_context_id": None, "program_name": root_program,
                     "program_chain": [root_program], "callsite_chain": [], "via_callsite_id": None,
                     "cfg_call_node_id": None, "parameter_mappings": []}]
        context_by_id = {root_id: contexts[0]}
        binding_contexts: dict[tuple[str, str], dict] = {}
        for context in context_audit["contexts"]:
            if context["parent_context_id"] is None:
                continue
            key = (context["program_chain"][-2], context["via_callsite_id"])
            binding_contexts.setdefault(key, context)
        root_contract = selected[root_program]
        events: dict[str, dict] = {}
        boundaries: list[dict] = []
        boundary_keys: set[tuple] = set()
        boundary_counts: Counter = Counter()
        witnesses: list[dict] = []
        root_exits: list[dict] = []
        witness_categories: set[tuple] = set()
        trace_records: list[tuple[int, dict]] = []
        root_exits_truncated = False
        witnesses_truncated = False
        truncated = False
        processed = 0
        scheduled = 1
        terminal_count = 0
        context_binding_count = 0
        pending = deque([(( _Frame(root_program, root_id, root_model.cfg["entry_node_id"], values, {}),),
                          frozenset(), -1, 0)])

        def add_trace(previous: int, entry: dict) -> int:
            if len(trace_records) >= max_states * 4 + 16:
                raise ValueError("Cross-program trace storage exceeded its explicit state-derived budget.")
            trace_records.append((previous, entry))
            return len(trace_records) - 1

        def trace_value(trace_id: int) -> list[dict]:
            entries = []
            while trace_id >= 0:
                trace_id, entry = trace_records[trace_id]
                entries.append(entry)
                if len(entries) > max_steps + 2:
                    raise ValueError("Cross-program trace chain exceeds its step bound.")
            return list(reversed(entries))

        def step(frame: _Frame, node: dict, kind: str | None = None, **details: object) -> dict:
            return {"kind": kind or node["kind"], "program_name": frame.program_name,
                    "context_id": frame.context_id, "node_id": node["node_id"],
                    "evidence_refs": node["evidence_refs"], **details}

        def boundary(reason: str, frame: _Frame, node: dict, faults: frozenset,
                     *, blocked: bool = True, **details: object) -> None:
            nonlocal truncated
            key = (reason, frame.context_id, node["node_id"])
            boundary_counts[reason] += 1
            if key not in boundary_keys:
                boundary_keys.add(key)
                if len(boundaries) < MAX_BOUNDARIES:
                    boundaries.append({"reason": reason, "program_name": frame.program_name,
                        "context_id": frame.context_id, "node_id": node["node_id"],
                        "evidence_refs": node["evidence_refs"], **details})
                else:
                    truncated = True
            if blocked:
                for event_id in faults:
                    events[event_id]["blocked_paths"] += 1

        def event(frame: _Frame, node: dict, kind: str, *, status_field: str | None = None,
                  status_value: str | None = None) -> str | None:
            key = _id("ipevent", frame.context_id, node["node_id"], kind, status_field, status_value)
            if key in events:
                return key
            if len(events) >= MAX_EVENTS:
                return None
            refs = _refs(node["evidence_refs"], frame.origins.get(status_field, []) if status_field else [])
            events[key] = {"event_id": key, "event_kind": kind, "program_name": frame.program_name,
                "context_id": frame.context_id, "node_id": node["node_id"],
                "status_field": status_field, "status_value": status_value, "evidence_refs": refs,
                "terminal_paths": 0, "blocked_paths": 0,
                "outputs": {name: Counter() for name in root_contract.output_fields},
                "status_values": {name: set() for name in root_contract.status_fields}, "witness_ids": []}
            return key

        def enqueue(frames: tuple[_Frame, ...], faults: frozenset, trace_id: int, steps: int,
                    origin: _Frame, node: dict) -> None:
            nonlocal scheduled, truncated
            if scheduled >= max_states:
                truncated = True
                boundary("path_state_budget_exhausted", origin, node, faults)
                return
            scheduled += 1
            pending.append((frames, faults, trace_id, steps))

        def parameter_rows(bindings: tuple[dict, ...], state: dict[str, str], *, returning: bool) -> list[dict]:
            result = []
            for mapping in bindings:
                name = mapping["callee_field"]["name"] if returning else mapping["caller_field"]["name"]
                result.append({"caller_field": mapping["caller_field"]["name"],
                    "callee_field": mapping["callee_field"]["name"],
                    "parameter_position": mapping["parameter_position"],
                    "group_member_index": mapping["group_member_index"],
                    "passing_mode": mapping["passing_mode"], "value": state.get(name),
                    "written_back": returning and mapping["passing_mode"] == "REFERENCE"})
            return result

        while pending:
            frames, faults, trace_id, steps = pending.popleft()
            processed += 1
            frame = frames[-1]
            current = model(frame.program_name)
            node = current.nodes[frame.node_id]
            if steps >= max_steps:
                truncated = True
                boundary("path_step_budget_exhausted", frame, node, faults)
                continue
            if current.boundary_reason:
                boundary(current.boundary_reason, frame, node, faults, storage_evidence_refs=current.boundary_refs)
                continue
            kind = node["kind"]
            if kind == "BOUNDARY":
                if "budget" in node.get("reason", ""):
                    truncated = True
                boundary(node.get("reason", "unsupported_cfg_boundary"), frame, node, faults)
                continue
            terminal = kind == "GOBACK" or kind == "EXIT" and node.get("exit_kind") == "program_fallthrough"
            if terminal and len(frames) > 1:
                if node.get("exit_kind") == "stop_run":
                    boundary("callee_stop_run_not_a_normal_return", frame, node, faults)
                    continue
                return_faults = set(faults)
                exhausted = False
                for name in current.contract.status_fields:
                    value = frame.values.get(name)
                    if value is None:
                        boundary("callee_business_status_unknown", frame, node, faults, blocked=False, status_field=name)
                    elif Decimal(value) != 0:
                        event_id = event(frame, node, "business_error_return", status_field=name, status_value=value)
                        if event_id is None:
                            truncated = True
                            boundary("event_budget_exhausted", frame, node, frozenset(return_faults))
                            exhausted = True
                            break
                        return_faults.add(event_id)
                if exhausted:
                    continue
                parent = frames[-2]
                parent_values, parent_origins = dict(parent.values), dict(parent.origins)
                for mapping in frame.bindings:
                    if mapping["passing_mode"] != "REFERENCE":
                        continue
                    source_name, target_name = mapping["callee_field"]["name"], mapping["caller_field"]["name"]
                    if target_name not in model(parent.program_name).layouts:
                        continue
                    value = _fits(frame.values.get(source_name), model(parent.program_name).layouts[target_name])
                    if value is None:
                        parent_values.pop(target_name, None)
                    else:
                        parent_values[target_name] = value
                    parent_origins[target_name] = _refs(frame.origins.get(source_name, []), frame.call_node["evidence_refs"])
                return_trace = add_trace(trace_id, step(frame, node, "call_return",
                    caller_program=parent.program_name, callee_program=frame.program_name,
                    parent_context_id=parent.context_id, callee_context_id=frame.context_id,
                    parameters=parameter_rows(frame.bindings, frame.values, returning=True),
                    callee_status_values={name: frame.values.get(name) for name in current.contract.status_fields},
                    callee_output_values={name: frame.values.get(name) for name in current.contract.output_fields},
                    return_node_id=frame.return_node_id))
                returned = replace(parent, node_id=frame.return_node_id, values=parent_values, origins=parent_origins)
                enqueue((*frames[:-2], returned), frozenset(return_faults), return_trace, steps + 1, frame, node)
                continue
            if terminal:
                terminal_count += 1
                output_values = {name: frame.values.get(name) for name in root_contract.output_fields}
                status_values = {name: frame.values.get(name) for name in root_contract.status_fields}
                output_categories = {name: "unknown" if value is None else "zero" if Decimal(value) == 0 else "nonzero"
                                     for name, value in output_values.items()}
                exit_trace = add_trace(trace_id, step(frame, node, "root_exit", output_values=output_values, status_values=status_values))
                for event_id in faults:
                    report = events[event_id]
                    report["terminal_paths"] += 1
                    for name, value in output_categories.items():
                        report["outputs"][name][value] += 1
                    for name, value in status_values.items():
                        report["status_values"][name].add(value)
                category = (tuple(sorted(faults)), tuple(sorted(output_values.items())), tuple(sorted(status_values.items())))
                witness_id = None
                if category not in witness_categories:
                    witness_categories.add(category)
                    if len(witnesses) < max_witnesses:
                        witness_id = "interprogram_path_" + str(len(witnesses) + 1)
                        witnesses.append({"witness_id": witness_id, "event_ids": sorted(faults),
                            "trace": trace_value(exit_trace), "exit_node_id": node["node_id"],
                            "output_values": output_values, "status_values": status_values,
                            "scope": "abstract_source_model_path_not_runtime_trace"})
                        for event_id in faults:
                            events[event_id]["witness_ids"].append(witness_id)
                    else:
                        witnesses_truncated = True
                if len(root_exits) < MAX_ROOT_EXITS:
                    root_exits.append({"exit_id": "root_exit_" + str(terminal_count),
                        "output_values": output_values, "status_values": status_values,
                        "event_ids": sorted(faults), "witness_id": witness_id})
                else:
                    root_exits_truncated = True
                continue
            if kind == "CALL":
                edges = {edge["outcome"]: edge for edge in current.outgoing[node["node_id"]]}
                if call_policy == "explore":
                    event_id = event(frame, node, "call_exception")
                    if event_id is None:
                        truncated = True
                        boundary("event_budget_exhausted", frame, node, faults)
                    else:
                        exceptional = replace(frame, node_id=edges["exception"]["target"])
                        exceptional_trace = add_trace(trace_id, step(frame, node, "call_exception", outcome="exception", event_id=event_id))
                        enqueue((*frames[:-1], exceptional), faults | {event_id}, exceptional_trace, steps + 1, frame, node)
                if node.get("dynamic_target"):
                    boundary("dynamic_call_target_not_resolved", frame, node, faults)
                    continue
                target = node["target"]
                if target in [ancestor.program_name for ancestor in frames]:
                    boundary("recursive_call_not_expanded", frame, node, faults)
                    continue
                if len(frames) - 1 >= max_depth:
                    truncated = True
                    boundary("call_depth_budget_exhausted", frame, node, faults)
                    continue
                if target not in selected:
                    boundary("callee_error_contract_missing", frame, node, faults, callee_program=target)
                    continue
                binding_context = binding_contexts.get((frame.program_name, node["callsite_id"]))
                if binding_context is None or not binding_context["parameter_mapping_complete"]:
                    boundary("call_parameter_mapping_not_complete", frame, node, faults)
                    continue
                bindings = tuple(binding_context["parameter_mappings"])
                if any(mapping["caller_field"]["name"] in current.unbound_linkage for mapping in bindings):
                    boundary("unbound_linkage_argument_not_supported", frame, node, faults)
                    continue
                if len(bindings) > MAX_TRACE_PARAMETERS:
                    truncated = True
                    boundary("call_parameter_budget_exhausted", frame, node, faults)
                    continue
                references: dict[str, set[int]] = defaultdict(set)
                for mapping in bindings:
                    if mapping["passing_mode"] == "REFERENCE":
                        references[mapping["caller_field"]["symbol_id"]].add(mapping["parameter_position"])
                if any(len(positions) > 1 for positions in references.values()):
                    boundary("reference_argument_alias_not_supported", frame, node, faults)
                    continue
                child_id = _id("ipctx", frame.context_id, node["node_id"])
                if child_id not in context_by_id:
                    if len(contexts) >= max_contexts:
                        truncated = True
                        boundary("context_budget_exhausted", frame, node, faults)
                        continue
                    if context_binding_count + len(bindings) > MAX_CONTEXT_BINDINGS:
                        truncated = True
                        boundary("context_parameter_budget_exhausted", frame, node, faults)
                        continue
                    context_binding_count += len(bindings)
                    parent_context = context_by_id[frame.context_id]
                    contextual_bindings = []
                    for mapping in bindings:
                        rebound = dict(mapping)
                        for role, owner in (("caller_field", frame.context_id), ("callee_field", child_id)):
                            rebound[role] = {**mapping[role], "context_id": owner,
                                "field_instance_id": _id("ipfield", owner, mapping[role]["symbol_id"])}
                        contextual_bindings.append(rebound)
                    child_context = {"context_id": child_id, "parent_context_id": frame.context_id,
                        "program_name": target, "program_chain": [*parent_context["program_chain"], target],
                        "callsite_chain": [*parent_context["callsite_chain"], node["callsite_id"]],
                        "via_callsite_id": node["callsite_id"], "cfg_call_node_id": node["node_id"],
                        "local_instance_chain": node["instance_chain"], "parameter_mappings": contextual_bindings}
                    contexts.append(child_context)
                    context_by_id[child_id] = child_context
                child_model = model(target)
                child_values: dict[str, str] = {}
                child_origins: dict[str, list[dict]] = {}
                for mapping in bindings:
                    source_name, target_name = mapping["caller_field"]["name"], mapping["callee_field"]["name"]
                    if target_name not in child_model.layouts:
                        continue
                    value = _fits(frame.values.get(source_name), child_model.layouts[target_name])
                    if value is not None:
                        child_values[target_name] = value
                    child_origins[target_name] = _refs(frame.origins.get(source_name, []), node["evidence_refs"])
                child = _Frame(target, child_id, child_model.cfg["entry_node_id"], child_values, child_origins,
                               edges["normal"]["target"], node, bindings)
                enter_trace = add_trace(trace_id, step(frame, node, "call_enter",
                    caller_program=frame.program_name, callee_program=target,
                    parent_context_id=frame.context_id, callee_context_id=child_id,
                    parameters=parameter_rows(bindings, frame.values, returning=False), outcome="normal"))
                enqueue((*frames, child), faults, enter_trace, steps + 1, frame, node)
                continue
            next_values, next_origins = dict(frame.values), dict(frame.origins)
            allowed = None
            details: dict = {}
            if kind == "MOVE":
                value = _number(node["source"])
                if value is None:
                    if node["source"] not in current.layouts:
                        boundary("move_source_numeric_layout_not_supported", frame, node, faults)
                        continue
                    value = frame.values.get(node["source"])
                if any(name not in current.layouts for name in node["targets"]):
                    boundary("move_target_numeric_layout_not_supported", frame, node, faults)
                    continue
                writes = {}
                for name in node["targets"]:
                    assigned = _fits(value, current.layouts[name])
                    if assigned is None:
                        next_values.pop(name, None)
                    else:
                        next_values[name] = assigned
                    next_origins[name] = _refs(node["evidence_refs"], frame.origins.get(node["source"], []))
                    writes[name] = assigned
                details = {"source": node["source"], "writes": writes}
            elif kind == "IF":
                literal = _number(node["value"])
                if node["field"] not in current.layouts or literal is None:
                    boundary("condition_numeric_operand_not_supported", frame, node, faults)
                    continue
                if frame.values.get(node["field"]) is not None:
                    left, right = Decimal(frame.values[node["field"]]), Decimal(literal)
                    tests = {"=": left == right, "<>": left != right, "<": left < right,
                             "<=": left <= right, ">": left > right, ">=": left >= right}
                    if node["operator"] not in tests:
                        boundary("condition_operator_not_supported", frame, node, faults)
                        continue
                    allowed = "true" if tests[node["operator"]] else "false"
                details = {"field": node["field"], "field_value": frame.values.get(node["field"]),
                           "operator": node["operator"], "value": literal}
            elif kind == "COMPUTE":
                if node["target"] not in current.layouts:
                    boundary("compute_target_numeric_layout_not_supported", frame, node, faults)
                    continue
            elif kind not in {"ENTRY", "JOIN", "EXIT", "CONTINUE"}:
                boundary("node_semantics_not_supported", frame, node, faults)
                continue
            for edge in current.outgoing[node["node_id"]]:
                outcome = edge["outcome"]
                if allowed is not None and outcome != allowed:
                    continue
                child_values, child_origins = dict(next_values), dict(next_origins)
                child_faults = faults
                extra = dict(details)
                if kind == "COMPUTE":
                    extra = {"target": node["target"], "expression": node["expression"],
                             "receiver_before": frame.values.get(node["target"])}
                    if outcome == "size_error":
                        event_id = event(frame, node, "size_error")
                        if event_id is None:
                            truncated = True
                            boundary("event_budget_exhausted", frame, node, faults)
                            continue
                        child_faults = faults | {event_id}
                        extra["event_id"] = event_id
                    if outcome == "normal" or not node.get("size_error_receiver_preserved"):
                        child_values.pop(node["target"], None)
                        child_origins[node["target"]] = node["evidence_refs"]
                    extra["receiver_after"] = child_values.get(node["target"])
                next_trace = add_trace(trace_id, step(frame, node, outcome=outcome, **extra))
                advanced = replace(frame, node_id=edge["target"], values=child_values, origins=child_origins)
                enqueue((*frames[:-1], advanced), child_faults, next_trace, steps + 1, frame, node)
        incomplete = bool(boundary_keys) or truncated
        reports = []
        for report in events.values():
            output_reports = {}
            for name, counts in report["outputs"].items():
                if counts["nonzero"]:
                    finding = "nonzero_exit_possible_in_model"
                elif counts["zero"] and not counts["unknown"] and not report["blocked_paths"] and not incomplete:
                    finding = "zero_on_all_modeled_exits"
                else:
                    finding = "unproven"
                output_reports[name] = {"finding": finding, "exit_values": dict(counts)}
            reports.append({**report, "outputs": output_reports,
                "status_values": {name: sorted(values, key=lambda value: (value is None, value or ""))
                                  for name, values in report["status_values"].items()},
                "output_association": "root_observations_not_causal_or_clearing_obligation",
                "runtime_path_verified": False})
        return {"auditor_version": AUDITOR_VERSION, "snapshot_id": facts.snapshot_id,
            "root_program": root_program, "call_policy": call_policy,
            "evaluation_scope": "bounded_context_sensitive_interprogram_error_returns",
            "status": "PARTIAL_ABSTRACT_SOURCE_AUDIT", "complete": False,
            "full_control_flow_proven": False, "runtime_execution_tested": False,
            "contexts": contexts, "events": reports, "witnesses": witnesses,
            "root_exits": root_exits, "boundaries": boundaries,
            "summary": {"states_processed": processed, "states_scheduled": scheduled,
                "contexts": len(contexts), "programs_modeled": len(models), "modeled_root_exits": terminal_count,
                "events": len(events), "events_by_kind": dict(sorted(Counter(event["event_kind"] for event in reports).items())),
                "boundaries": len(boundary_keys), "boundary_counts": dict(sorted(boundary_counts.items())),
                "boundaries_truncated": len(boundary_keys) > len(boundaries), "truncated": truncated,
                "root_exits_truncated": root_exits_truncated, "witnesses": len(witnesses),
                "witnesses_truncated": witnesses_truncated, "modeled_exploration_closed": not incomplete},
            "budgets": {"max_states": max_states, "max_steps": max_steps, "max_contexts": max_contexts,
                "max_depth": max_depth, "max_witnesses": max_witnesses, "max_root_exits": MAX_ROOT_EXITS},
            "initial_values": values,
            "assumptions": [
                "Numeric states are representable constants or unknown; COMPUTE normal results deliberately remain unknown.",
                "CALL normal enters the indexed callee and copies reference parameters back only on ordinary return.",
                "CONTENT and VALUE inputs never write back through those parameter positions.",
                "CALL invocation failure is separate from a callee returning a nonzero business status.",
                "Each entry havocs nonparameter WORKING-STORAGE to unknown conservatively; this does not assert fresh runtime storage.",
                "Shared/global/external, alias, recursive, reentrant, and callee STOP RUN effects stop at explicit boundaries.",
                "Root PROCEDURE USING parameters are assumed supplied; other LINKAGE fields have no modeled storage address.",
                "normal_return_only assumes call availability; it does not establish runtime availability or disable arithmetic failure alternatives.",
                "Output observations are per field, not inferred causal associations or an obligation that every other root output must be zero.",
                "Witnesses are abstract source-model alternatives and may not be feasible COBOL runtime executions.",
            ]}
    finally:
        connection.close()
