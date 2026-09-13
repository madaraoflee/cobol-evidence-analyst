"""A bounded source interpreter conditional on explicit record-access scenarios.

Control comes from expanded source, not section numbers or configured roles.
Only one deterministically selected path is followed. Unknown predicates,
unsupported storage and uncontracted external effects stop at evidence boundaries.
This is neither a COBOL runtime nor a proof that all production paths are covered.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

from call_bindings import BindingContext, _by_value_supported, parse_signature
from call_contexts import _Facts
from exception_cfg import build_exception_cfg
from exception_paths import _fits, _layout, _number
from framework_io import apply_io_call, initial_io_state, validate_runtime_contract
from framework_projection import digest, projected_index
from repo_inventory import DEFAULT_EXTENSIONS


_NAME = re.compile(r"[A-Z][A-Z0-9_$#@-]{0,63}\Z")
_UNSAFE = re.compile(r"(?<![A-Z0-9_$#@-])(?:GLOBAL|EXTERNAL|REDEFINES|RENAMES|OCCURS|BASED|POINTER|RECURSIVE|REENTRANT)(?![A-Z0-9_$#@-])")


class _Boundary(ValueError):
    def __init__(self, reason: str, **details):
        self.reason, self.details = reason, details


def _scalar_layout(declaration):
    if declaration[1] != "SCALAR":
        return None
    picture, usage = declaration[2:]
    text = re.fullmatch(r"X(?:\(([1-9][0-9]*)\))?", picture)
    if text and usage == "DISPLAY":
        length = int(text.group(1) or 1)
        return {"kind": "text", "length": length} if length <= 2048 else None
    numeric = _layout(picture)
    return {"kind": "numeric", **numeric} if numeric else None


def _logical(value, layout):
    if value is None:
        return None
    if layout["kind"] == "text":
        return value.rstrip(" ")
    number = Decimal(value)
    return int(number) if number == number.to_integral_value() else format(number, "f")


def _receive(value, kind, layout):
    if kind != layout["kind"]:
        raise _Boundary("scalar_category_conversion_not_supported")
    if value is None:
        return None
    if kind == "text":
        if not value.isascii():
            raise _Boundary("text_encoding_storage_not_modeled")
        if len(value) > layout["length"]:
            raise _Boundary("text_move_truncation_not_modeled")
        return value.ljust(layout["length"])
    if _fits(value, layout) is None:
        raise _Boundary("numeric_move_not_exactly_representable")
    return value


class _Model:
    def __init__(self, facts, binder, database, name):
        self.name, self.binder, self.facts = name, binder, facts
        if facts.scope_reason(name):
            raise _Boundary(facts.scope_reason(name))
        if name in binder.incomplete:
            raise _Boundary("incomplete_copy_scope_not_supported")
        for unit in facts.units.values():
            if unit["program_name"] == name and unit["unit_type"] in {"DataItem", "Program"}:
                facts.verify_unit(unit)
                masked = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", "", unit["normalized_text"].upper())
                if _UNSAFE.search(masked):
                    raise _Boundary("shared_aliased_or_reentrant_storage_not_supported")
        signatures = facts.signatures[name]
        if len(signatures) != 1:
            raise _Boundary("procedure_signature_not_unique")
        self.signature = signatures[0]
        facts.verify_unit(self.signature)
        try:
            self.parameters = parse_signature(self.signature["normalized_text"])
        except ValueError as error:
            raise _Boundary(str(error)) from error
        self.layouts, self.storage, self.symbols = {}, {}, {}
        self.groups = {}
        self.unsupported = set()
        for (program, field), symbols in binder.fields.items():
            if program != name:
                continue
            if len(symbols) != 1:
                self.unsupported.add(field)
                continue
            symbol = symbols[0]
            self.symbols[field] = symbol
            self.storage[field] = binder.storage(symbol)
            try:
                members, declarations, evidence = binder.layout(symbol)
            except ValueError:
                self.unsupported.add(field)
                continue
            for member in members:
                facts.verify_unit(facts.units[member["definition_unit_id"]])
            facts.ref(evidence)
            if declarations[0][1] == "GROUP":
                self.groups[field] = (members, declarations, evidence)
            else:
                layout = _scalar_layout(declarations[0])
                if layout:
                    self.layouts[field] = layout
                else:
                    self.unsupported.add(field)
        if len(self.layouts) > 2000:
            raise _Boundary("framework_field_budget_exhausted")
        self.bound_linkage = set()
        for parameter in self.parameters:
            members, _, _ = self.parameter_layout(parameter.name, formal=True)
            self.bound_linkage.update(member["name"] for member in members)
        self.cfg = build_exception_cfg(database, name, framework_mode=True)
        if self.cfg["snapshot_id"] != facts.snapshot_id:
            raise ValueError("Derived snapshot changed during framework control analysis.")
        self.nodes = {node["node_id"]: node for node in self.cfg["nodes"]}
        self.outgoing = defaultdict(list)
        for edge in self.cfg["edges"]:
            if edge["source"] not in self.nodes or edge["target"] not in self.nodes:
                raise ValueError("Framework CFG has an invalid edge endpoint.")
            self.outgoing[edge["source"]].append(edge)

    def parameter_layout(self, field, *, formal=False):
        if field not in self.symbols or field in self.unsupported:
            raise _Boundary("parameter_layout_not_supported", field=field)
        symbol = self.symbols[field]
        if self.storage[field] not in ({"LINKAGE"} if formal else {"WORKING-STORAGE", "LOCAL-STORAGE", "LINKAGE"}):
            raise _Boundary("parameter_storage_not_supported", field=field)
        if formal:
            declaration = self.facts.units[symbol["definition_unit_id"]]["normalized_text"]
            if not re.match(r"(?:01|1|77)\s", declaration):
                raise _Boundary("formal_parameter_must_be_linkage_root", field=field)
        try:
            members, declarations, evidence = self.binder.layout(symbol)
        except ValueError as error:
            raise _Boundary(str(error), field=field) from error
        for member, declaration in zip(members, declarations):
            if declaration[1] == "SCALAR" and member["name"] not in self.layouts:
                raise _Boundary("parameter_scalar_layout_not_supported", field=member["name"])
        return members, declarations, evidence

    def field(self, name):
        if name not in self.layouts or name in self.unsupported:
            raise _Boundary("scalar_field_layout_not_supported", field=name)
        if self.storage[name] not in {"WORKING-STORAGE", "LOCAL-STORAGE", "LINKAGE"}:
            raise _Boundary("scalar_storage_not_supported", field=name)
        if self.storage[name] == "LINKAGE" and name not in self.bound_linkage:
            raise _Boundary("unbound_linkage_storage_not_supported", field=name)
        return self.layouts[name]

    def operand(self, expression, values):
        if expression in self.symbols:
            layout = self.field(expression)
            return values[expression], layout["kind"]
        if expression in {"SPACE", "SPACES"}:
            return " ", "text"
        if expression[:1] in {"'", '"'} and expression[-1:] == expression[:1]:
            marker = expression[0]
            return expression[1:-1].replace(marker + marker, marker), "text"
        number = _number(expression)
        if number is None:
            raise _Boundary("scalar_operand_not_resolved", operand=expression)
        return number, "numeric"


@dataclass
class _Frame:
    model: _Model
    values: dict
    node_id: str
    bindings: list | None = None
    call_node: dict | None = None


def audit_framework_paths(source_root, entry_program, runtime_contract=None, scenario=None, *,
                          initial_values=None, output_fields=None, extensions=DEFAULT_EXTENSIONS,
                          scratch_root=None, max_steps=2000, max_call_depth=12, max_paths=64):
    """Follow one scenario-selected path; unknown alternatives are boundaries.

    ``max_paths`` is an accepted safety ceiling, not an exploration count. This
    version follows at most one path, assumes ordinary returns for source calls,
    and uses explicit scenario faults only for declared external services.
    """
    if not isinstance(entry_program, str) or not _NAME.fullmatch(entry_program):
        raise ValueError("Entry program must be an uppercase source identifier.")
    for value, maximum in ((max_steps, 20000), (max_call_depth, 32), (max_paths, 256)):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError("Framework path budget is outside supported bounds.")
    initial_values = {} if initial_values is None else initial_values
    if not isinstance(initial_values, dict) or len(initial_values) > 256 or any(
            not isinstance(name, str) or not _NAME.fullmatch(name) or type(value) not in {int, str}
            or type(value) is str and len(value) > 2048
            or type(value) is int and abs(value) > 10**18 - 1
            for name, value in initial_values.items()):
        raise ValueError("Initial values require a bounded mapping of scalar source fields.")
    if output_fields is not None and (not isinstance(output_fields, list) or len(output_fields) > 256
            or any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in output_fields)
            or len(set(output_fields)) != len(output_fields)):
        raise ValueError("Output fields require unique source identifiers.")
    if (runtime_contract is None) != (scenario is None):
        raise ValueError("A runtime contract and scenario must be supplied together.")
    scenario = deepcopy(scenario)
    initial_values = dict(initial_values)
    contract = validate_runtime_contract(runtime_contract) if runtime_contract is not None else None
    io_state = initial_io_state(contract, scenario) if contract is not None else None
    services = {service["program"]: service for service in contract["services"]} if contract else {}
    result = {"schema_version": "1.0", "entry_program": entry_program,
              "scope": {"runtime_verified": False, "full_business_analysis_verified": False,
                        "conditional_on_contract": contract is not None, "model_path_complete": False,
                        "all_paths_explored": False, "source_call_outcome_policy": "ordinary_return_only",
                        "unknown_predicate_policy": "stop_at_boundary"},
              "source_snapshot_id": None, "derived_snapshot_id": None,
              "contract_hash": digest(contract) if contract is not None else None,
              "scenario_hash": digest(scenario) if scenario is not None else None,
              "initial_values": initial_values,
              "trace": [], "root_exits": [], "boundaries": [],
              "final_io_state": io_state,
              "budgets": {"max_steps": max_steps, "max_call_depth": max_call_depth, "max_paths": max_paths},
              "limitations": ["One deterministic source path conditional on supplied inputs and declared scenario; not all possible paths.",
                              "Source callees follow actual bodies with ordinary return; runtime invocation failures are not enumerated.",
                              "Literal program targets use the index's case-insensitive identifier resolution; runtime linking is not verified.",
                              "Unknown conditions, unsupported statements, storage, layout or conversions stop analysis.",
                              "Uninitialized fields are unknown; declaration VALUE initialization is not inferred. Supported working storage persists between ordinary source calls.",
                              "ASCII text equality uses right-space padding; encoding-dependent storage, collating order and text truncation are not modeled.",
                              "Declared access paths, files, locks, transactions and restart events are simulated, never runtime verified."]}
    counts = Counter()

    def boundary(reason, frame=None, node=None, **details):
        result["boundaries"].append({"reason": reason, "program_name": frame.model.name if frame else entry_program,
                                     "node_id": node["node_id"] if node else None,
                                     "evidence_refs": node.get("evidence_refs", []) if node else [], **details})

    def record(frame, node, event=None, **details):
        result["trace"].append({"step": counts["steps"], "program_name": frame.model.name,
                                "kind": node["kind"], "event": event or node["kind"].lower(),
                                "node_id": node["node_id"], "instance_chain": node["instance_chain"],
                                "evidence_refs": node["evidence_refs"], **details})

    with projected_index(Path(source_root), entry_program, extensions=extensions, scratch_root=scratch_root) as projection:
        result["source_snapshot_id"], result["derived_snapshot_id"] = projection.source_snapshot_id, projection.derived_snapshot_id
        root_expansion = projection.expansions[entry_program]
        if not root_expansion["source_expansion_complete"]:
            boundary("root_source_expansion_incomplete", source_boundaries=root_expansion["boundaries"])
        else:
            database = projection.database
            with closing(sqlite3.connect(f"file:{quote(database.as_posix(), safe='/')}?mode=ro", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                facts = _Facts(connection)
                binder = BindingContext(connection, facts.normalize_source)
                models, working_values = {}, {}

                def model(name):
                    if name not in models:
                        # Identification clauses may continue on physical lines
                        # that the structural Program fact does not contain.
                        # Validate the complete expanded header before assigning
                        # ordinary persistent working-storage semantics.
                        expanded = "\n".join(line["code"] for line in projection.expansions[name]["lines"])
                        header = re.search(r"\bPROGRAM-ID\s*\.\s*([A-Z][A-Z0-9_$#@-]*)(?=\s|\.)(.*?)\.",
                                           expanded, re.I | re.S)
                        if header is None or header.group(1).upper() != name or header.group(2).strip():
                            raise _Boundary("program_identification_or_lifetime_clause_not_modeled", callee_program=name)
                        models[name] = _Model(facts, binder, database, name)
                    return models[name]

                def environment(current):
                    previous = working_values.get(current.name, {})
                    return {name: previous.get(name) if current.storage[name] == "WORKING-STORAGE" else None
                            for name in current.layouts}

                def next_node(frame, node, outcome=None):
                    edges = frame.model.outgoing[node["node_id"]]
                    selected = edges if outcome is None else [edge for edge in edges if edge["outcome"] == outcome]
                    if len(selected) != 1:
                        raise _Boundary("framework_control_edge_not_unique", outcome=outcome)
                    frame.node_id = selected[0]["target"]

                def writes(frame, updates):
                    return {name: _logical(value, frame.model.field(name)) for name, value in updates.items()}

                def source_bindings(caller, callee, node):
                    actuals = node["passing_parameters"]
                    if len(actuals) != len(callee.parameters):
                        raise _Boundary("parameter_arity_mismatch")
                    bindings, seen_actual, seen_formal = [], set(), set()
                    for actual, formal in zip(actuals, callee.parameters):
                        if (actual["mode"] == "VALUE") != (formal.mode == "VALUE"):
                            raise _Boundary("parameter_mode_mismatch")
                        left, left_layout, left_evidence = caller.model.parameter_layout(actual["name"])
                        right, right_layout, right_evidence = callee.parameter_layout(formal.name, formal=True)
                        if left_layout != right_layout:
                            raise _Boundary("parameter_layout_mismatch")
                        if actual["mode"] == "VALUE" and not _by_value_supported(left_layout):
                            raise _Boundary("by_value_layout_not_supported")
                        for old, new, declaration in zip(left, right, left_layout):
                            if declaration[1] != "SCALAR":
                                continue
                            old_name, new_name = old["name"], new["name"]
                            caller.model.field(old_name)
                            callee.field(new_name)
                            if old_name in seen_actual or new_name in seen_formal:
                                raise _Boundary("overlapping_argument_storage_not_supported")
                            seen_actual.add(old_name)
                            seen_formal.add(new_name)
                            bindings.append({"caller_field": old_name, "callee_field": new_name, "mode": actual["mode"],
                                             "evidence_refs": facts.refs(left_evidence, right_evidence)})
                    return bindings

                def external_service(frame, node, service):
                    arguments = node["passing_parameters"]
                    if len(arguments) != 1 or arguments[0] != {"name": service["argument"], "mode": "REFERENCE"}:
                        raise _Boundary("io_call_argument_contract_mismatch")
                    members, declarations, _ = frame.model.parameter_layout(service["argument"])
                    if declarations[0][1] != "GROUP":
                        raise _Boundary("io_contract_argument_requires_group")
                    leaves = {member["name"] for member, declaration in zip(members, declarations) if declaration[1] == "SCALAR"}
                    mapped = {service["function_field"], service["status_field"], *service["key_fields"].values(), *service["record_fields"].values()}
                    if not mapped <= leaves:
                        raise _Boundary("io_contract_fields_outside_argument_group")
                    for field in mapped:
                        frame.model.field(field)
                    function_layout = frame.model.field(service["function_field"])
                    if function_layout["kind"] != "text" or any(len(operation["value"]) > function_layout["length"] for operation in service["operations"]):
                        raise _Boundary("io_function_layout_contract_mismatch")
                    if any(operation["value"] != operation["value"].rstrip(" ") or not operation["value"].isascii()
                           for operation in service["operations"]):
                        raise _Boundary("io_function_padding_or_encoding_not_modeled")
                    status_layout = frame.model.field(service["status_field"])
                    status_values = []
                    for value in contract["statuses"].values():
                        status_values.append(_receive(str(value) if type(value) is int else value,
                                             "numeric" if type(value) is int else "text", status_layout))
                    if len(set(status_values)) != len(status_values):
                        raise _Boundary("io_status_values_collide_after_storage_padding")
                    # The pure record model uses logical strings. A noncanonical
                    # trailing-space record would change merely by passing through
                    # fixed-width source storage and cannot be silently rewritten.
                    string_values = [value for row in scenario["records"] for value in row.values() if type(value) is str]
                    string_values += [value for path in contract["access_paths"] for value in path["select"].values() if type(value) is str]
                    if any(value != value.rstrip(" ") or not value.isascii() for value in string_values):
                        raise _Boundary("io_record_padding_or_encoding_not_modeled")
                    for mapping in (service["key_fields"], service["record_fields"]):
                        for dataset_field, source_field in mapping.items():
                            layout = frame.model.field(source_field)
                            desired = "numeric" if contract["dataset"]["fields"][dataset_field] == "integer" else "text"
                            if layout["kind"] != desired or desired == "numeric" and layout["scale"]:
                                raise _Boundary("io_record_layout_contract_mismatch", field=source_field)
                    return {name: _logical(frame.values[name], frame.model.field(name)) for name in mapped}

                stack, frame, node = [], None, None
                try:
                    current = model(entry_program)
                    root_values = environment(current)
                    for name, value in initial_values.items():
                        layout = current.field(name)
                        if layout["kind"] == "numeric":
                            number = _number(str(value))
                            if number is None:
                                raise _Boundary("initial_numeric_value_not_supported", field=name)
                            root_values[name] = _receive(number, "numeric", layout)
                        else:
                            if type(value) is not str:
                                raise _Boundary("initial_text_value_not_supported", field=name)
                            root_values[name] = _receive(value, "text", layout)
                    outputs = sorted(current.layouts) if output_fields is None else output_fields
                    for name in outputs:
                        current.field(name)
                    stack.append(_Frame(current, root_values, current.cfg["entry_node_id"]))
                    while stack:
                        frame = stack[-1]
                        node = frame.model.nodes[frame.node_id]
                        if counts["steps"] >= max_steps:
                            raise _Boundary("framework_step_budget_exhausted")
                        counts["steps"] += 1
                        kind = node["kind"]
                        if kind == "BOUNDARY":
                            raise _Boundary(node["reason"])
                        if kind == "MOVE":
                            value, category = frame.model.operand(node["source"], frame.values)
                            updates = {target: _receive(value, category, frame.model.field(target)) for target in node["targets"]}
                            frame.values.update(updates)
                            record(frame, node, writes=writes(frame, updates))
                            next_node(frame, node)
                        elif kind in {"IF", "LOOP_TEST"}:
                            left, left_kind = frame.model.operand(node["field"], frame.values)
                            right, right_kind = frame.model.operand(node["value"], frame.values)
                            if left is None or right is None:
                                raise _Boundary("condition_value_not_resolved", field=node["field"])
                            if left_kind != right_kind:
                                raise _Boundary("condition_category_conversion_not_supported")
                            operator = node["operator"]
                            if left_kind == "text":
                                if operator not in {"=", "<>"}:
                                    raise _Boundary("text_collating_order_not_modeled")
                                if not left.isascii() or not right.isascii():
                                    raise _Boundary("text_encoding_storage_not_modeled")
                                width = max(len(left), len(right))
                                a, b = left.ljust(width), right.ljust(width)
                            else:
                                a, b = Decimal(left), Decimal(right)
                            accepted = {"=": lambda: a == b, "<>": lambda: a != b,
                                        "<": lambda: a < b, "<=": lambda: a <= b,
                                        ">": lambda: a > b, ">=": lambda: a >= b}[operator]()
                            counts["loop_tests"] += kind == "LOOP_TEST"
                            record(frame, node, field=node["field"], operator=operator, compared_to=node["value"], outcome=accepted)
                            next_node(frame, node, "true" if accepted else "false")
                        elif kind == "CALL":
                            target = node["target"]
                            if node["dynamic_target"]:
                                raise _Boundary("dynamic_call_target_not_modeled")
                            if target in projection.expansions:
                                expansion = projection.expansions[target]
                                if not expansion["source_expansion_complete"]:
                                    raise _Boundary("callee_source_expansion_incomplete", callee_program=target,
                                                    source_boundaries=expansion["boundaries"])
                                if target in {item.model.name for item in stack}:
                                    raise _Boundary("recursive_source_call_not_modeled", callee_program=target)
                                if len(stack) >= max_call_depth + 1:
                                    raise _Boundary("framework_call_depth_budget_exhausted")
                                child_model = model(target)
                                bindings = source_bindings(frame, child_model, node)
                                child_values = environment(child_model)
                                for binding in bindings:
                                    child_values[binding["callee_field"]] = frame.values[binding["caller_field"]]
                                counts["source_calls"] += 1
                                record(frame, node, "call_enter", callee_program=target, bindings=bindings,
                                       passed_values={binding["caller_field"]: _logical(frame.values[binding["caller_field"]],
                                                       frame.model.field(binding["caller_field"])) for binding in bindings},
                                       callee_signature_refs=facts.refs(child_model.signature["evidence_id"]))
                                next_node(frame, node, "normal")
                                stack.append(_Frame(child_model, child_values, child_model.cfg["entry_node_id"], bindings, node))
                            elif target in services:
                                supplied = external_service(frame, node, services[target])
                                alternatives = apply_io_call(contract, io_state, target, supplied)
                                if len(alternatives) != 1:
                                    raise _Boundary("io_contract_outcome_not_deterministic")
                                effect = alternatives[0]
                                counts["io_calls"] += 1
                                if effect.get("boundary"):
                                    record(frame, node, "io_call", io_event=effect["event"], outcome=effect["outcome"], writes={})
                                    raise _Boundary(effect["boundary"]["reason"])
                                updates = {}
                                for name, value in effect["values"].items():
                                    if name not in supplied:
                                        raise _Boundary("io_update_outside_contract_argument", field=name)
                                    updates[name] = _receive(str(value) if type(value) is int else value,
                                                             "numeric" if type(value) is int else "text", frame.model.field(name))
                                frame.values.update(updates)
                                io_state = effect["state"]
                                record(frame, node, "io_call", io_event=effect["event"], outcome=effect["outcome"], writes=writes(frame, updates))
                                next_node(frame, node, effect["outcome"])
                            else:
                                raise _Boundary("uncontracted_external_call", callee_program=target)
                        elif kind == "GOBACK" or kind == "EXIT" and node.get("exit_kind") == "program_fallthrough":
                            if node.get("exit_kind") == "stop_run":
                                raise _Boundary("stop_run_scope_not_modeled")
                            working_values[frame.model.name] = {name: value for name, value in frame.values.items()
                                                               if frame.model.storage[name] == "WORKING-STORAGE"}
                            if len(stack) == 1:
                                record(frame, node, "root_return")
                                result["root_exits"].append({"values": {name: _logical(frame.values[name], frame.model.field(name)) for name in outputs},
                                                             "io_state": io_state})
                                stack.pop()
                                result["scope"]["model_path_complete"] = True
                            else:
                                parent = stack[-2]
                                updates = {binding["caller_field"]: frame.values[binding["callee_field"]]
                                           for binding in frame.bindings if binding["mode"] == "REFERENCE"}
                                parent.values.update(updates)
                                record(frame, node, "call_return", caller_program=parent.model.name,
                                       callee_program=frame.model.name, writes=writes(parent, updates),
                                       passing_modes=[binding["mode"] for binding in frame.bindings],
                                       callsite_evidence_refs=frame.call_node["evidence_refs"])
                                stack.pop()
                        elif kind in {"ENTRY", "JOIN", "EXIT"}:
                            record(frame, node, role=node.get("role"))
                            next_node(frame, node)
                        else:
                            raise _Boundary("framework_statement_effect_not_modeled", statement_kind=kind)
                except _Boundary as error:
                    boundary(error.reason, frame, node, **error.details)
                result["final_io_state"] = io_state
                result["trace"] = projection.map_result(result["trace"])
                result["boundaries"] = projection.map_result(result["boundaries"])
        projection.verify_originals()
    result["summary"] = {"steps": counts["steps"], "source_calls": counts["source_calls"],
                         "io_calls": counts["io_calls"], "loop_tests": counts["loop_tests"],
                         "selected_paths": int(bool(result["trace"])), "root_exits": len(result["root_exits"]),
                         "boundaries": len(result["boundaries"]),
                         "boundary_counts": dict(Counter(item["reason"] for item in result["boundaries"])),
                         "truncated": any("budget" in item["reason"] or any(
                             nested["reason"] in {"copy_line_limit", "copy_depth_limit", "copy_inclusion_limit"}
                             for nested in item.get("source_boundaries", [])) for item in result["boundaries"]),
                         "unresolved_output_fields": sorted({name for exit in result["root_exits"]
                             for name, value in exit["values"].items() if value is None})}
    return result
