#!/usr/bin/env python3
"""Bounded static callsite contexts over a read-only source index.

A context separates source-level parameter correspondences at different CALL
sites. It is not a runtime activation, storage allocation or execution trace.
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
from urllib.parse import quote

from call_bindings import BindingContext, parse_call, parse_signature
from structural_index import normalize_cobol_lines


AUDITOR_VERSION = "bounded-call-contexts-v0.1"
MAX_FACT_ROWS = 100_000
MAX_SOURCE_FILES = 10_000
MAX_TEXT_BYTES = 64_000_000
MAX_EVIDENCE_LINES = 500_000
MAX_CALL_EXPANSIONS = 10_000
MAX_BOUNDARIES = 1_000
MAX_CONTROLS = 64
_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_$#@-]{0,63}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256("\x1f".join(map(str, parts)).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().upper()


def _path_valid(value: object) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 4096 and "\\" not in value
            and not PurePosixPath(value).is_absolute()
            and all(part not in {"", ".", ".."} for part in value.split("/")))


class _Facts:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        tables = ("source_files", "code_units", "symbols", "evidence_spans", "call_bindings", "copy_expansions",
                  "relations", "copy_scope_boundaries")
        for table in tables:
            limit = MAX_SOURCE_FILES if table == "source_files" else MAX_FACT_ROWS
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if count > limit:
                raise ValueError("Index exceeds the call-context fact budget.")
        text_columns = {
            "metadata": "key value",
            "source_files": "relative_path sha256",
            "evidence_spans": "evidence_id relative_path source_sha256 text",
            "code_units": "unit_id relative_path unit_type name program_name parent_unit_id normalized_text content_hash evidence_id parse_status",
            "symbols": "symbol_id relative_path symbol_type name program_name qualified_name definition_unit_id evidence_id",
            "call_bindings": "binding_id callsite_id caller_program callee_program passing_mode caller_symbol_id callee_symbol_id status reason evidence_id supporting_evidence_ids_json",
            "copy_expansions": "symbol_id unit_id source_symbol_id program_symbol_id inclusion_relation_ids_json",
            "relations": "relation_id relative_path from_entity_id relation_type target_name target_scope target_entity_id status evidence_id metadata_json",
            "copy_scope_boundaries": "program_name relative_path reason evidence_id",
        }
        text_bytes = 0
        for table, columns in text_columns.items():
            expression = " + ".join(f"LENGTH(CAST(COALESCE({column}, '') AS BLOB))" for column in columns.split())
            text_bytes += connection.execute(f"SELECT COALESCE(SUM({expression}), 0) FROM {table}").fetchone()[0]
        if text_bytes > MAX_TEXT_BYTES:
            raise ValueError("Index exceeds the call-context text budget.")
        metadata_rows = connection.execute("SELECT key, value FROM metadata LIMIT 33").fetchall()
        if len(metadata_rows) > 32:
            raise ValueError("Index metadata exceeds the call-context budget.")
        metadata = dict(metadata_rows)
        self.snapshot_id = metadata.get("snapshot_id", "")
        self.files = {row["relative_path"]: dict(row) for row in connection.execute(
            "SELECT relative_path, sha256, line_count FROM source_files ORDER BY relative_path")}
        seen_paths = set()
        manifest = hashlib.sha256()
        for name in sorted(self.files, key=str.casefold):
            row = self.files[name]
            if (not _path_valid(name) or name.casefold() in seen_paths
                    or not isinstance(row["sha256"], str) or not _HASH.fullmatch(row["sha256"])
                    or type(row["line_count"]) is not int or row["line_count"] < 0):
                raise ValueError("Invalid or ambiguous stored source manifest.")
            seen_paths.add(name.casefold())
            manifest.update(name.encode("utf-8") + b"\0" + row["sha256"].encode("ascii") + b"\n")
        if self.snapshot_id != "sha256:" + manifest.hexdigest():
            raise ValueError("Snapshot identifier does not match the stored source manifest.")
        self.units = {row["unit_id"]: dict(row) for row in connection.execute("SELECT * FROM code_units")}
        self.symbols = {row["symbol_id"]: dict(row) for row in connection.execute("SELECT * FROM symbols")}
        self.evidence = {row["evidence_id"]: dict(row) for row in connection.execute("SELECT * FROM evidence_spans")}
        if sum(max(0, row["end_line"] - row["start_line"] + 1) for row in self.evidence.values()) > MAX_EVIDENCE_LINES:
            raise ValueError("Stored evidence exceeds the call-context line budget.")
        self.programs: dict[str, list[dict]] = defaultdict(list)
        programs_per_file: Counter = Counter()
        for symbol in self.symbols.values():
            if symbol["symbol_type"] == "Program":
                self.programs[symbol["name"]].append(symbol)
                programs_per_file[symbol["relative_path"]] += 1
        self.compound_files = {name for name, count in programs_per_file.items() if count > 1}
        self.calls: dict[str, list[dict]] = defaultdict(list)
        self.signatures: dict[str, list[dict]] = defaultdict(list)
        for unit in self.units.values():
            if unit["unit_type"] == "Statement" and unit["name"] == "CALL":
                self.calls[unit["program_name"]].append(unit)
            if unit["unit_type"] == "ProcedureSignature":
                self.signatures[unit["program_name"]].append(unit)
        for rows in self.calls.values():
            rows.sort(key=lambda row: (row["relative_path"], row["start_line"], row["unit_id"]))
        relevant = connection.execute(
            "SELECT * FROM relations WHERE relation_type IN "
            "('CALLS', 'CALL_TARGET_FROM', 'CONTROL_DEPENDS_ON', 'INCLUDES_COPY') ORDER BY relation_id LIMIT ?",
            (MAX_FACT_ROWS + 1,),
        ).fetchall()
        if len(relevant) > MAX_FACT_ROWS or sum(len(row["metadata_json"]) for row in relevant) > MAX_TEXT_BYTES:
            raise ValueError("Call-context relations exceed the fact budget.")
        self.relations: dict[str, list[dict]] = defaultdict(list)
        self.relations_by_id: dict[str, dict] = {}
        for row in relevant:
            relation = dict(row)
            if len(relation["metadata_json"]) > 16384:
                raise ValueError("Call-context relation metadata exceeds its budget.")
            relation["metadata"] = json.loads(relation["metadata_json"])
            if not isinstance(relation["metadata"], dict):
                raise ValueError("Invalid relation metadata.")
            self.relations[row["from_entity_id"]].append(relation)
            self.relations_by_id[row["relation_id"]] = relation
        self.expansions = {row["symbol_id"]: dict(row) for row in connection.execute("SELECT * FROM copy_expansions")}
        for expansion in self.expansions.values():
            if len(expansion["inclusion_relation_ids_json"]) > 16384:
                raise ValueError("COPY inclusion metadata exceeds its budget.")
            inclusion_ids = json.loads(expansion["inclusion_relation_ids_json"])
            if (not isinstance(inclusion_ids, list) or not 1 <= len(inclusion_ids) <= 8
                    or any(not isinstance(value, str) or value not in self.relations_by_id for value in inclusion_ids)):
                raise ValueError("Invalid parameter COPY inclusion chain.")
        self.bindings: dict[str, list[dict]] = defaultdict(list)
        for row in connection.execute(
                "SELECT * FROM call_bindings ORDER BY callsite_id, parameter_position, group_member_index, binding_id"):
            self.bindings[row["callsite_id"]].append(dict(row))
        self._verified_units: set[str] = set()
        self._verified_refs: dict[str, dict] = {}
        self._binding_context: BindingContext | None = None
        self._member_cache: dict[tuple[str, int], tuple[list[tuple[str, str]], set[str]]] = {}
        # Overlapping raw spans must describe the same stored source lines.
        # This detects inconsistent index edits, not a coordinated rewrite of
        # all stored facts; there is no external source trust anchor here.
        source_lines: dict[tuple[str, int], str] = {}
        for evidence_id in self.evidence:
            self.ref(evidence_id)
            row = self.evidence[evidence_id]
            raw_lines = row["text"].split("\n")
            if len(raw_lines) != row["end_line"] - row["start_line"] + 1:
                raise ValueError("Stored evidence text disagrees with its line range.")
            for offset, line in enumerate(raw_lines):
                key = (row["relative_path"], row["start_line"] + offset)
                if key in source_lines and source_lines[key] != line:
                    raise ValueError("Overlapping evidence spans disagree.")
                source_lines[key] = line

    def ref(self, evidence_id: str) -> dict:
        if evidence_id in self._verified_refs:
            return dict(self._verified_refs[evidence_id])
        row = self.evidence.get(evidence_id)
        if row is None:
            raise ValueError("Referenced evidence is missing from the stored snapshot.")
        source = self.files.get(row["relative_path"])
        if (source is None or row["source_sha256"] != source["sha256"]
                or type(row["start_line"]) is not int or type(row["end_line"]) is not int
                or not 0 < row["start_line"] <= row["end_line"] <= max(1, source["line_count"])
                or not isinstance(row["text"], str)
                or evidence_id != _id("ev", row["relative_path"], row["source_sha256"],
                                      row["start_line"], row["end_line"])):
            raise ValueError("Evidence is inconsistent with the stored source snapshot.")
        result = {key: row[key] for key in (
            "evidence_id", "relative_path", "start_line", "end_line", "source_sha256")}
        result["span_sha256"] = hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
        self._verified_refs[evidence_id] = result
        return dict(result)

    def refs(self, *evidence_ids: str) -> list[dict]:
        return [self.ref(value) for value in sorted(set(evidence_ids))]

    def verify_unit(self, unit: dict) -> None:
        if unit["unit_id"] in self._verified_units:
            return
        self.ref(unit["evidence_id"])
        if hashlib.sha256(unit["normalized_text"].encode("utf-8")).hexdigest() != unit["content_hash"]:
            raise ValueError("Indexed unit text does not match its content hash.")
        evidence = self.evidence[unit["evidence_id"]]
        lines, _ = normalize_cobol_lines(evidence["text"])
        source_text = _compact(" ".join(line.text for line in lines))
        normalized = _compact(unit["normalized_text"])
        # Conditions can occupy only a portion of their physical source span.
        if not normalized or normalized not in source_text:
            raise ValueError("Indexed unit text disagrees with stored evidence.")
        self._verified_units.add(unit["unit_id"])

    def scope_reason(self, name: str) -> str | None:
        programs = self.programs.get(name, [])
        if len(programs) != 1:
            return "program_scope_ambiguous" if programs else "program_not_found"
        symbol = programs[0]
        unit = self.units.get(symbol["definition_unit_id"])
        if (unit is None or unit["unit_type"] != "Program" or unit["program_name"] != name
                or symbol["program_name"] != name or unit["relative_path"] != symbol["relative_path"]):
            raise ValueError("Program definition is inconsistent with its symbol.")
        self.verify_unit(unit)
        if symbol["relative_path"] in self.compound_files:
            return "compound_program_scope_not_supported"
        return None

    def expected_members(self, call: dict, position: int, actual_name: str,
                         callee_program: str, formal_name: str, signature: dict) -> tuple[list[tuple[str, str]], set[str]]:
        """Recheck the indexed layout so mutated/missing member rows cannot pass."""
        key = (call["unit_id"], position)
        if key in self._member_cache:
            return self._member_cache[key]
        if self._binding_context is None:
            # The full tables this read-only helper consumes were bounded above.
            self._binding_context = BindingContext(self.connection, normalize_cobol_lines)
        verifier = self._binding_context
        if call["program_name"] in verifier.incomplete or callee_program in verifier.incomplete:
            raise ValueError("Confirmed parameter binding crosses an incomplete COPY scope.")
        actual = verifier.fields.get((call["program_name"], actual_name), [])
        formal = verifier.fields.get((callee_program, formal_name), [])
        if len(actual) != 1 or len(formal) != 1:
            raise ValueError("Confirmed binding does not identify unique parameter roots.")
        formal_unit = verifier.units[formal[0]["definition_unit_id"]]
        if (verifier.storage(actual[0]) not in {"WORKING-STORAGE", "LOCAL-STORAGE", "LINKAGE"}
                or verifier.storage(formal[0]) != "LINKAGE"
                or not re.match(r"(?:01|1|77)\s", formal_unit["normalized_text"].upper())):
            raise ValueError("Confirmed parameter roots use unsupported storage or declaration levels.")
        caller_members, caller_layout, caller_evidence = verifier.layout(actual[0])
        callee_members, callee_layout, callee_evidence = verifier.layout(formal[0])
        if caller_layout != callee_layout:
            raise ValueError("Confirmed parameter layouts are inconsistent.")
        required_refs = {call["evidence_id"], signature["evidence_id"], caller_evidence, callee_evidence}
        required_refs.update(edge["evidence_id"] for symbol in (actual[0], formal[0])
                             for edge in verifier.inclusions(symbol))
        result = ([(left["symbol_id"], right["symbol_id"]) for left, right in zip(caller_members, callee_members)],
                  required_refs)
        self._member_cache[key] = result
        return result

    def field(self, symbol_id: str, context: dict) -> dict:
        symbol = self.symbols.get(symbol_id)
        if (symbol is None or symbol["symbol_type"] != "Field"
                or symbol["program_name"] != context["program_name"]):
            raise ValueError("Confirmed parameter symbol is outside its program scope.")
        unit = self.units.get(symbol["definition_unit_id"])
        if unit is None or unit["unit_type"] != "DataItem" or unit["program_name"] != context["program_name"]:
            raise ValueError("Parameter field definition is inconsistent with its scope.")
        self.verify_unit(unit)
        self.ref(symbol["evidence_id"])
        storage_unit = unit
        expansion = self.expansions.get(symbol_id)
        if expansion:
            if len(expansion["inclusion_relation_ids_json"]) > 16384:
                raise ValueError("COPY inclusion metadata exceeds its budget.")
            inclusion_ids = json.loads(expansion["inclusion_relation_ids_json"])
            if (not isinstance(inclusion_ids, list) or not 1 <= len(inclusion_ids) <= 8
                    or any(not isinstance(value, str) for value in inclusion_ids)):
                raise ValueError("Invalid parameter COPY inclusion chain.")
            edge = self.relations_by_id.get(inclusion_ids[0])
            if edge is None or edge["relation_type"] != "INCLUDES_COPY":
                raise ValueError("Parameter COPY inclusion site is missing.")
            storage_unit = self.units.get(edge["from_entity_id"])
            if storage_unit is None or storage_unit["program_name"] != context["program_name"]:
                raise ValueError("Parameter COPY inclusion has an inconsistent scope.")
            self.verify_unit(storage_unit)
        storage_section = None
        visited = set()
        while storage_unit and len(visited) < 64 and storage_unit["unit_id"] not in visited:
            visited.add(storage_unit["unit_id"])
            if storage_unit["unit_type"] == "Section":
                storage_section = storage_unit["name"]
                break
            storage_unit = self.units.get(storage_unit["parent_unit_id"])
        return {"field_instance_id": _id("ctxfield", context["context_id"], symbol_id),
                "context_id": context["context_id"], "symbol_id": symbol_id, "name": symbol["name"],
                "storage_section": storage_section}


def audit_call_contexts(
    database_path: Path, root_program: str, *, max_contexts: int = 128,
    max_depth: int = 12, max_bindings: int = 5000,
) -> dict[str, object]:
    """Instantiate bounded source correspondences for complete static CALL chains.

    Unreachable/conditional CALLs may be included. Repeated execution of one
    static CALL, PERFORM invocation stacks and runtime storage are not modeled.
    """
    if not isinstance(root_program, str) or not _IDENTIFIER.fullmatch(root_program):
        raise ValueError("Root program must be an uppercase, unqualified identifier.")
    if any(type(value) is not int or not minimum <= value <= maximum for value, minimum, maximum in (
            (max_contexts, 1, 1024), (max_depth, 0, 64), (max_bindings, 0, 50_000))):
        raise ValueError("Call-context budgets are outside supported bounds.")
    path = Path(database_path).expanduser().resolve()
    connection = sqlite3.connect(f"file:{quote(path.as_posix(), safe='/')}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        facts = _Facts(connection)
        if root_program not in facts.programs:
            raise ValueError("Root program is not present in the stored snapshot.")
        contexts: list[dict] = []
        boundaries: list[dict] = []
        boundary_counts: Counter = Counter()
        bindings_scanned = 0
        expansions = 0
        budget_reasons: set[str] = set()

        def boundary(reason: str, context: dict | None = None, call: dict | None = None, **details: object) -> None:
            boundary_counts[reason] += 1
            if len(boundaries) >= MAX_BOUNDARIES:
                return
            boundaries.append({"reason": reason,
                "context_id": context["context_id"] if context else None,
                "program_name": context["program_name"] if context else root_program,
                "callsite_id": call["unit_id"] if call else None,
                "evidence_refs": facts.refs(call["evidence_id"]) if call else [], **details})

        def budget(reason: str, context: dict, call: dict | None = None) -> None:
            if reason not in budget_reasons:
                budget_reasons.add(reason)
                boundary(reason, context, call)

        def make_context(program: str, parent: dict | None, call: dict | None) -> dict:
            chain = [*parent["callsite_chain"], call["unit_id"]] if parent and call else []
            symbol = facts.programs[program][0]
            context = {"context_id": _id("callctx", facts.snapshot_id, root_program, *chain),
                "parent_context_id": parent["context_id"] if parent else None,
                "program_name": program, "program_symbol_id": symbol["symbol_id"],
                "depth": len(chain), "callsite_chain": chain,
                "program_chain": [*parent["program_chain"], program] if parent else [program],
                "via_callsite_id": call["unit_id"] if call else None,
                "evidence_refs": facts.refs(symbol["evidence_id"], *([call["evidence_id"]] if call else [])),
                "parameter_mapping_complete": True, "parameter_mappings": [], "callsite_controls": []}
            contexts.append(context)
            return context

        root_reason = facts.scope_reason(root_program)
        pending: deque[dict] = deque()
        if root_reason:
            boundary(root_reason)
        else:
            pending.append(make_context(root_program, None, None))
        while pending and expansions < MAX_CALL_EXPANSIONS:
            parent = pending.popleft()
            for call in facts.calls[parent["program_name"]]:
                if expansions >= MAX_CALL_EXPANSIONS:
                    budget("call_expansion_budget_exhausted", parent, call)
                    break
                expansions += 1
                facts.verify_unit(call)
                if call["relative_path"] != facts.programs[parent["program_name"]][0]["relative_path"]:
                    boundary("callsite_program_scope_ambiguous", parent, call)
                    continue
                try:
                    form = parse_call(call["normalized_text"])
                except ValueError as error:
                    boundary("call_form_not_supported", parent, call, binding_reason=str(error))
                    continue
                if call["parse_status"] != "complete":
                    boundary("callsite_parse_incomplete", parent, call)
                    continue
                if form.dynamic:
                    boundary("dynamic_target_not_resolved", parent, call, target_field=form.target)
                    continue
                scope_reason = facts.scope_reason(form.target)
                if scope_reason:
                    boundary(scope_reason, parent, call, target_program=form.target)
                    continue
                target = facts.programs[form.target][0]
                calls = [row for row in facts.relations[call["unit_id"]] if row["relation_type"] == "CALLS"]
                if len(calls) != 1 or calls[0]["status"] != "confirmed":
                    boundary("literal_target_not_uniquely_confirmed", parent, call, target_program=form.target)
                    continue
                edge = calls[0]
                facts.ref(edge["evidence_id"])
                if (edge["target_entity_id"] != target["symbol_id"] or edge["target_name"] != form.target
                        or edge["evidence_id"] != call["evidence_id"]):
                    raise ValueError("Confirmed call relation is inconsistent with its source callsite.")
                if form.target in parent["program_chain"]:
                    boundary("recursive_call_chain_not_expanded", parent, call, target_program=form.target)
                    continue
                if parent["depth"] >= max_depth:
                    budget("call_depth_budget_exhausted", parent, call)
                    continue
                if len(contexts) >= max_contexts:
                    budget("context_budget_exhausted", parent, call)
                    continue
                child = make_context(form.target, parent, call)
                pending.append(child)
                controls = [row for row in facts.relations[call["unit_id"]]
                            if row["relation_type"] == "CONTROL_DEPENDS_ON"]
                if len(controls) > MAX_CONTROLS:
                    boundary("callsite_control_budget_exhausted", child, call)
                for control in controls[:MAX_CONTROLS]:
                    condition = facts.units.get(control["target_entity_id"])
                    outcome = control["metadata"].get("outcome")
                    if (condition is None or condition["unit_type"] != "Condition"
                            or condition["program_name"] != parent["program_name"]
                            or not isinstance(outcome, str)):
                        raise ValueError("Callsite control is outside its source program.")
                    facts.verify_unit(condition)
                    if not ((condition["name"] == "IF" and outcome in {"true", "false"})
                            or (condition["name"] == "WHEN" and _compact(outcome) == _compact(condition["normalized_text"]).rstrip("."))
                            or (condition["name"] == "EVALUATE" and outcome == "selector")):
                        raise ValueError("Callsite control outcome disagrees with its condition.")
                    child["callsite_controls"].append({"condition_id": condition["unit_id"],
                        "outcome": outcome, "status": control["status"],
                        "evidence_refs": facts.refs(control["evidence_id"], condition["evidence_id"])})
                rows = facts.bindings[call["unit_id"]]
                if not rows:
                    boundary("parameter_bindings_missing", child, call)
                    child["parameter_mapping_complete"] = False
                remaining = max_bindings - bindings_scanned
                if len(rows) > remaining:
                    budget("binding_budget_exhausted", child, call)
                    child["parameter_mapping_complete"] = False
                duplicate_reference_actuals: dict[str, set[int]] = defaultdict(set)
                positions: set[int] = set()
                members_seen: dict[int, set[int]] = defaultdict(set)
                expected_counts: dict[int, int] = {}
                formal_parameters = None
                for row in rows[:remaining]:
                    bindings_scanned += 1
                    if row["caller_program"] != parent["program_name"] or row["callee_program"] != form.target:
                        raise ValueError("Parameter binding disagrees with its source callsite scope.")
                    if row["evidence_id"] != call["evidence_id"]:
                        raise ValueError("Parameter binding does not cite its source callsite.")
                    if (len(row["supporting_evidence_ids_json"]) > 16384
                            or not isinstance(row["reason"], str) or len(row["reason"]) > 128
                            or row["status"] not in {"confirmed", "unresolved"}):
                        raise ValueError("Parameter binding metadata is invalid or exceeds its budget.")
                    refs = json.loads(row["supporting_evidence_ids_json"])
                    if (not isinstance(refs, list) or not 1 <= len(refs) <= 8
                            or any(not isinstance(value, str) for value in refs)
                            or len(set(refs)) != len(refs) or call["evidence_id"] not in refs):
                        raise ValueError("Parameter binding has invalid supporting evidence.")
                    evidence_refs = facts.refs(*refs)
                    if row["status"] != "confirmed":
                        child["parameter_mapping_complete"] = False
                        boundary("parameter_binding_unresolved", child, call, binding_id=row["binding_id"],
                                 binding_reason=row["reason"], parameter_position=row["parameter_position"])
                        continue
                    if formal_parameters is None:
                        signatures = facts.signatures[form.target]
                        if len(signatures) != 1 or signatures[0]["parse_status"] != "complete":
                            raise ValueError("Confirmed binding has no unique complete procedure signature.")
                        facts.verify_unit(signatures[0])
                        formal_parameters = parse_signature(signatures[0]["normalized_text"])
                        if len(formal_parameters) != len(form.parameters):
                            raise ValueError("Confirmed binding disagrees with procedure parameter count.")
                        if signatures[0]["evidence_id"] not in refs:
                            raise ValueError("Confirmed binding omits its procedure signature evidence.")
                    if row["parameter_position"] is None:
                        if form.parameters or row["reason"] != "call_without_parameters":
                            raise ValueError("Confirmed empty parameter binding disagrees with its call.")
                        continue
                    position, member = row["parameter_position"], row["group_member_index"]
                    if (type(position) is not int or not 1 <= position <= len(form.parameters)
                            or type(member) is not int or not 0 <= member <= 256
                            or row["passing_mode"] != form.parameters[position - 1].mode
                            or (row["passing_mode"] == "VALUE") != (formal_parameters[position - 1].mode == "VALUE")
                            or row["reason"] != "parameter_position_layout_match"):
                        raise ValueError("Confirmed parameter binding has invalid position or mode.")
                    caller_field = facts.field(row["caller_symbol_id"], parent)
                    callee_field = facts.field(row["callee_symbol_id"], child)
                    expected, required_refs = facts.expected_members(call, position,
                        form.parameters[position - 1].name, form.target, formal_parameters[position - 1].name, signatures[0])
                    if (member >= len(expected) or expected[member] != (row["caller_symbol_id"], row["callee_symbol_id"])
                            or set(refs) != required_refs or member in members_seen[position]):
                        raise ValueError("Confirmed parameter member or its evidence disagrees with the declaration layout.")
                    members_seen[position].add(member)
                    expected_counts[position] = len(expected)
                    if member == 0:
                        if (caller_field["name"] != form.parameters[position - 1].name
                                or callee_field["name"] != formal_parameters[position - 1].name):
                            raise ValueError("Confirmed root parameter does not match its actual argument.")
                        positions.add(position)
                    if row["passing_mode"] == "REFERENCE":
                        duplicate_reference_actuals[caller_field["symbol_id"]].add(position)
                    child["parameter_mappings"].append({"binding_id": row["binding_id"],
                        "parameter_position": position, "group_member_index": member,
                        "passing_mode": row["passing_mode"], "caller_field": caller_field,
                        "callee_field": callee_field,
                        "transfer": "reference_correspondence" if row["passing_mode"] == "REFERENCE" else "copy_in",
                        "possible_writeback": row["passing_mode"] == "REFERENCE",
                        "runtime_writeback_proven": False, "evidence_refs": evidence_refs})
                if positions != set(range(1, len(form.parameters) + 1)):
                    child["parameter_mapping_complete"] = False
                    if rows and len(rows) <= remaining and all(row["status"] == "confirmed" for row in rows):
                        boundary("parameter_position_coverage_incomplete", child, call)
                if any(members_seen[position] != set(range(count)) for position, count in expected_counts.items()):
                    child["parameter_mapping_complete"] = False
                    if len(rows) <= remaining:
                        boundary("parameter_member_coverage_incomplete", child, call)
                for symbol_id, alias_positions in sorted(duplicate_reference_actuals.items()):
                    if len(alias_positions) > 1:
                        boundary("duplicate_reference_actual_alias_possible", child, call,
                                 caller_symbol_id=symbol_id, parameter_positions=sorted(alias_positions))
        if pending:
            budget("call_expansion_budget_exhausted", pending[0])
        mapping_count = sum(len(context["parameter_mappings"]) for context in contexts)
        counts = Counter(context["program_name"] for context in contexts)
        return {
            "auditor_version": AUDITOR_VERSION, "snapshot_id": facts.snapshot_id,
            "root_program": root_program, "status": "PARTIAL_SOURCE_AUDIT",
            "evaluation_scope": "bounded_static_callsite_contexts", "complete": False,
            "runtime_execution_tested": False, "runtime_writeback_proven": False,
            "evidence_integrity_scope": "stored_snapshot_consistency_not_authenticated_source_bytes",
            "reachability_scope": "structural_calls_including_conditional_or_unreachable_calls",
            "field_identity_scope": "context_scoped_source_identity_not_runtime_storage_allocation",
            "contexts": contexts, "boundaries": boundaries,
            "summary": {"contexts": len(contexts), "distinct_programs": len(counts),
                "programs_with_multiple_contexts": {name: count for name, count in sorted(counts.items()) if count > 1},
                "parameter_mappings": mapping_count,
                "possible_writebacks": sum(mapping["possible_writeback"] for context in contexts
                                           for mapping in context["parameter_mappings"]),
                "binding_rows_considered": bindings_scanned, "callsites_considered": expansions,
                "boundaries": sum(boundary_counts.values()), "boundary_counts": dict(sorted(boundary_counts.items())),
                "boundaries_truncated": sum(boundary_counts.values()) > len(boundaries),
                "truncated": bool(budget_reasons) or sum(boundary_counts.values()) > len(boundaries)},
            "budgets": {"max_contexts": max_contexts, "max_depth": max_depth, "max_bindings": max_bindings,
                        "max_call_expansions": MAX_CALL_EXPANSIONS, "max_boundaries": MAX_BOUNDARIES},
            "limitations": [
                "Contexts distinguish full static CALL-site chains; they are not runtime activations.",
                "Conditional, dormant-paragraph and post-exit CALLs can appear; reachability and execution order are not proven.",
                "Repeated PERFORM or loop executions of one static CALL are not unfolded into separate occurrences.",
                "Context-scoped field identities do not imply disjoint storage; WORKING-STORAGE may persist or be shared.",
                "Reference aliasing and group overlap are not fully modeled; repeated actual arguments may alias.",
                "REFERENCE permits possible writeback only; CONTENT and VALUE do not return writes through that parameter.",
                "Parameter mappings establish source correspondence, not reaching values, successful return or complete error propagation.",
                "Stored manifest, spans and indexed facts are checked for consistency, not authenticated against original source bytes.",
            ],
        }
    finally:
        connection.close()
