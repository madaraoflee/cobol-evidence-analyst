"""Bounded, evidence-backed CALL argument and LINKAGE parameter bindings.

Position and compatible declaration layouts establish syntactic parameter
correspondence. They do not establish execution, a computed value, or successful
return. Reference writeback remains a candidate; content/value never gets one.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable


MAX_PARAMETERS = 64
MAX_GROUP_MEMBERS = 256
MAX_SUPPORTING_REFS = 8
MAX_CALL_TEXT = 16_384
IDENTIFIER = r"[A-Z][A-Z0-9-]{0,63}"
NAME_RE = re.compile(IDENTIFIER)
DECLARATION_RE = re.compile(rf"^(\d{{1,2}})\s+({IDENTIFIER})\b(.*)$", re.S)
REJECTED_NAMES = {
    "ADDRESS", "BY", "CONTENT", "END-CALL", "EXCEPTION", "FILLER", "FUNCTION",
    "GIVING", "LENGTH", "OF", "OMITTED", "ON", "OPTIONAL", "REFERENCE",
    "RETURNING", "VALUE", "USING",
    "CALL", "CANCEL", "COMPUTE", "CONTINUE", "DISPLAY", "ELSE", "END-IF",
    "END-PERFORM", "EVALUATE", "EXIT", "GOBACK", "GO", "IF", "MOVE",
    "PERFORM", "STOP", "THEN", "TO", "WHEN",
}
BOUNDARY_REASONS = frozenset({
    "call_form_not_supported", "call_parameter_limit", "dynamic_target_not_resolved",
    "call_target_not_found", "call_target_ambiguous", "caller_scope_ambiguous",
    "procedure_signature_not_found", "procedure_signature_ambiguous",
    "procedure_signature_not_supported", "parameter_arity_mismatch",
    "parameter_mode_mismatch", "caller_field_not_found", "caller_field_ambiguous",
    "callee_field_not_found", "callee_field_ambiguous", "callee_not_linkage_parameter",
    "caller_storage_not_supported", "copy_scope_incomplete", "parameter_layout_not_supported",
    "parameter_layout_mismatch", "parameter_evidence_limit", "by_value_layout_not_supported",
    "program_scope_not_supported",
})


@dataclass(frozen=True)
class Parameter:
    name: str
    mode: str


@dataclass(frozen=True)
class CallForm:
    target: str
    dynamic: bool
    parameters: tuple[Parameter, ...]


def _id(prefix: str, *parts: object) -> str:
    return prefix + "_" + hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()[:24]


def _parameters(text: str, *, signature: bool) -> tuple[Parameter, ...]:
    tokens = text.replace(",", " ").split()
    parameters: list[Parameter] = []
    mode = "REFERENCE"
    cursor = 0
    while cursor < len(tokens):
        if tokens[cursor] == "BY":
            if cursor + 2 >= len(tokens) or tokens[cursor + 1] not in (
                {"REFERENCE", "VALUE"} if signature else {"REFERENCE", "CONTENT", "VALUE"}
            ):
                raise ValueError("procedure_signature_not_supported" if signature else "call_form_not_supported")
            mode = tokens[cursor + 1]
            cursor += 2
        name = tokens[cursor]
        if not NAME_RE.fullmatch(name) or name in REJECTED_NAMES:
            raise ValueError("procedure_signature_not_supported" if signature else "call_form_not_supported")
        parameters.append(Parameter(name, mode))
        if len(parameters) > MAX_PARAMETERS:
            raise ValueError("call_parameter_limit")
        cursor += 1
    if signature and len({item.name for item in parameters}) != len(parameters):
        raise ValueError("procedure_signature_not_supported")
    return tuple(parameters)


def parse_call(text: str) -> CallForm:
    """Parse identifiers only; literals, expressions, RETURNING and ABI options stop."""
    if len(text) > MAX_CALL_TEXT:
        raise ValueError("call_parameter_limit")
    match = re.fullmatch(rf"CALL\s+(?:'({IDENTIFIER})'|\"({IDENTIFIER})\"|({IDENTIFIER}))(.*)", text.strip().upper(), re.S)
    if not match:
        raise ValueError("call_form_not_supported")
    tail = match.group(4).strip()
    if tail.endswith("."):
        tail = tail[:-1].rstrip()
    tail = re.sub(r"\s*\bEND-CALL$", "", tail).rstrip()
    # A handler's clause header is part of the CALL span. Its body must be
    # separate indexed statements; inline imperative text is not consumed here.
    tail = re.sub(r"\s+\b(?:NOT\s+)?ON\s+EXCEPTION$", "", tail).rstrip()
    if tail and not tail.startswith("USING "):
        raise ValueError("call_form_not_supported")
    parameters = _parameters(tail[6:].strip(), signature=False) if tail else ()
    if tail and not parameters:
        raise ValueError("call_form_not_supported")
    return CallForm(match.group(1) or match.group(2) or match.group(3), bool(match.group(3)), parameters)


def parse_signature(text: str) -> tuple[Parameter, ...]:
    if len(text) > MAX_CALL_TEXT:
        raise ValueError("procedure_signature_not_supported")
    match = re.fullmatch(r"PROCEDURE\s+DIVISION(?:\s+USING\s+(.+?))?\s*\.", text.strip().upper(), re.S)
    if not match:
        raise ValueError("procedure_signature_not_supported")
    return _parameters(match.group(1), signature=True) if match.group(1) else ()


def ensure_call_binding_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS call_bindings (
            binding_id TEXT PRIMARY KEY,
            callsite_id TEXT NOT NULL,
            caller_program TEXT NOT NULL,
            callee_program TEXT,
            parameter_position INTEGER,
            group_member_index INTEGER NOT NULL DEFAULT 0,
            passing_mode TEXT,
            caller_symbol_id TEXT,
            callee_symbol_id TEXT,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            supporting_evidence_ids_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_call_bindings_callsite ON call_bindings(callsite_id);
        CREATE INDEX IF NOT EXISTS idx_call_bindings_caller ON call_bindings(caller_symbol_id);
        CREATE INDEX IF NOT EXISTS idx_call_bindings_callee ON call_bindings(callee_symbol_id);
    """)


def clear_call_bindings(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM relations WHERE relation_type IN ('PASSES_AS', 'MAY_WRITE_BACK')")
    connection.execute("DELETE FROM call_bindings")
    for row in connection.execute("SELECT relation_id, metadata_json FROM relations WHERE relation_type = 'CALLS'").fetchall():
        metadata = json.loads(row["metadata_json"])
        if metadata.get("boundary") == "call_parameter_binding_incomplete":
            metadata.pop("boundary")
            connection.execute("UPDATE relations SET metadata_json = ? WHERE relation_id = ?",
                               (json.dumps(metadata, sort_keys=True), row["relation_id"]))


class BindingContext:
    def __init__(self, connection: sqlite3.Connection, normalize_source: Callable | None):
        self.connection = connection
        self.normalize_source = normalize_source
        self.units = {row["unit_id"]: row for row in connection.execute("SELECT * FROM code_units")}
        self.symbols = {row["symbol_id"]: row for row in connection.execute("SELECT * FROM symbols")}
        self.evidence = {row["evidence_id"]: row for row in connection.execute("SELECT * FROM evidence_spans")}
        self.spans = {
            (row["relative_path"], row["start_line"], row["end_line"]): row
            for row in self.evidence.values()
        }
        self.expansions = {row["symbol_id"]: row for row in connection.execute("SELECT * FROM copy_expansions")}
        self.relations = {row["relation_id"]: row for row in connection.execute("SELECT * FROM relations")}
        self.call_relations: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for relation in self.relations.values():
            if relation["relation_type"] == "CALLS":
                self.call_relations[relation["from_entity_id"]].append(relation)
        self.expansion_sources: dict[tuple[str, str, str], list[sqlite3.Row]] = defaultdict(list)
        for expansion in self.expansions.values():
            self.expansion_sources[(expansion["source_symbol_id"], expansion["program_symbol_id"], expansion["inclusion_relation_ids_json"])].append(self.symbols[expansion["symbol_id"]])
        self.layouts: dict[str, tuple[list[sqlite3.Row], list[tuple], str]] = {}
        self.fields: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        self.programs: dict[str, list[sqlite3.Row]] = defaultdict(list)
        self.source_fields: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        self.signatures: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        for row in self.symbols.values():
            if row["symbol_type"] == "Program":
                self.programs[row["name"]].append(row)
            if row["symbol_type"] in {"Field", "ConditionName"}:
                self.fields[(row["program_name"], row["name"])].append(row)
                if row["symbol_id"] not in self.expansions:
                    self.source_fields[(row["relative_path"], row["program_name"])].append(row)
        for rows in self.source_fields.values():
            rows.sort(key=lambda row: self.units[row["definition_unit_id"]]["start_line"])
        for unit in self.units.values():
            if unit["unit_type"] == "ProcedureSignature":
                self.signatures[(unit["relative_path"], unit["program_name"])].append(unit)
        self.incomplete = {row["program_name"] for row in connection.execute("SELECT program_name FROM copy_scope_boundaries")}
        program_counts: dict[str, int] = defaultdict(int)
        for programs in self.programs.values():
            for program in programs:
                program_counts[program["relative_path"]] += 1
        self.compound_program_files = {path for path, count in program_counts.items() if count > 1}

    def origin(self, symbol: sqlite3.Row) -> sqlite3.Row:
        expansion = self.expansions.get(symbol["symbol_id"])
        return self.symbols[expansion["source_symbol_id"]] if expansion else symbol

    def inclusions(self, symbol: sqlite3.Row) -> list[sqlite3.Row]:
        expansion = self.expansions.get(symbol["symbol_id"])
        return [self.relations[value] for value in json.loads(expansion["inclusion_relation_ids_json"])] if expansion else []

    def storage(self, symbol: sqlite3.Row) -> str | None:
        inclusions = self.inclusions(symbol)
        unit = self.units[inclusions[0]["from_entity_id"] if inclusions else symbol["definition_unit_id"]]
        visited: set[str] = set()
        while unit and unit["unit_id"] not in visited:
            visited.add(unit["unit_id"])
            if unit["unit_type"] == "Section":
                return unit["name"]
            unit = self.units.get(unit["parent_unit_id"])
        return None

    def counterpart(self, original: sqlite3.Row, root: sqlite3.Row) -> sqlite3.Row | None:
        expansion = self.expansions.get(root["symbol_id"])
        if not expansion:
            return original
        matches = self.expansion_sources.get((original["symbol_id"], expansion["program_symbol_id"], expansion["inclusion_relation_ids_json"]), [])
        return matches[0] if len(matches) == 1 else None

    def layout(self, symbol: sqlite3.Row) -> tuple[list[sqlite3.Row], list[tuple], str]:
        if symbol["symbol_id"] in self.layouts:
            return self.layouts[symbol["symbol_id"]]
        original = self.origin(symbol)
        unit = self.units[original["definition_unit_id"]]
        root_decl = _declaration(unit["normalized_text"])
        if root_decl is None:
            raise ValueError("parameter_layout_not_supported")
        ordered = self.source_fields[(original["relative_path"], original["program_name"])]
        if self.inclusions(symbol):
            first_declaration = DECLARATION_RE.match(self.units[ordered[0]["definition_unit_id"]]["normalized_text"].upper()) if ordered else None
            if first_declaration is None or int(first_declaration.group(1)) not in {1, 77}:
                # A subordinate COPY fragment inherits its storage ancestry at
                # the inclusion site. Scope expansion does not recover that
                # hierarchy, so an OCCURS/REDEFINES parent cannot be ruled out.
                raise ValueError("parameter_layout_not_supported")
        ancestors: list[tuple[int, sqlite3.Row]] = []
        for candidate in ordered:
            candidate_unit = self.units[candidate["definition_unit_id"]]
            if candidate_unit["parent_unit_id"] != unit["parent_unit_id"]:
                ancestors.clear()
                continue
            level_match = DECLARATION_RE.match(candidate_unit["normalized_text"].upper())
            if level_match is None:
                raise ValueError("parameter_layout_not_supported")
            level = int(level_match.group(1))
            while ancestors and ancestors[-1][0] >= level:
                ancestors.pop()
            if candidate["symbol_id"] == original["symbol_id"]:
                break
            ancestors.append((level, candidate_unit))
        if any(_declaration(ancestor["normalized_text"]) != (level, "GROUP") for level, ancestor in ancestors):
            # A simple-looking leaf under OCCURS or REDEFINES is not a simple
            # unqualified argument; its enclosing layout is significant too.
            raise ValueError("parameter_layout_not_supported")
        rows = [original]
        if root_decl[1] == "GROUP":
            offset = next(i for i, row in enumerate(ordered) if row["symbol_id"] == original["symbol_id"])
            for member in ordered[offset + 1:]:
                member_unit = self.units[member["definition_unit_id"]]
                match = DECLARATION_RE.match(member_unit["normalized_text"].upper())
                if member_unit["parent_unit_id"] != unit["parent_unit_id"] or match is None or int(match.group(1)) <= root_decl[0]:
                    break
                rows.append(member)
                if len(rows) > MAX_GROUP_MEMBERS + 1:
                    raise ValueError("parameter_layout_not_supported")
            if len(rows) == 1:
                raise ValueError("parameter_layout_not_supported")
        declarations: list[tuple] = []
        rebound: list[sqlite3.Row] = []
        for offset, row in enumerate(rows):
            item = self.units[row["definition_unit_id"]]
            declaration = _declaration(item["normalized_text"])
            if declaration is None:
                raise ValueError("parameter_layout_not_supported")
            # A group declaration must actually contain children; an elementary
            # item must not have them. Unsupported level-88/66 shapes stop here.
            following = DECLARATION_RE.match(self.units[rows[offset + 1]["definition_unit_id"]]["normalized_text"].upper()) if offset + 1 < len(rows) else None
            has_children = following is not None and int(following.group(1)) > declaration[0]
            if (declaration[1] == "GROUP") != has_children:
                raise ValueError("parameter_layout_not_supported")
            declarations.append((declaration[0] - root_decl[0], *declaration[1:]))
            bound = self.counterpart(row, symbol)
            if bound is None:
                raise ValueError("parameter_layout_not_supported")
            rebound.append(bound)
        last = self.units[rows[-1]["definition_unit_id"]]
        evidence = self.spans.get((unit["relative_path"], unit["start_line"], last["end_line"]))
        if evidence is None or self.normalize_source is None:
            raise ValueError("parameter_layout_not_supported")
        normalized, _ = self.normalize_source(evidence["text"])
        actual = re.sub(r"\s+", " ", " ".join(item.text for item in normalized)).strip().upper()
        expected = re.sub(r"\s+", " ", " ".join(self.units[row["definition_unit_id"]]["normalized_text"] for row in rows)).strip().upper()
        if actual != expected:
            # A hidden COPY, REPLACE, compiler directive or unparsed declaration
            # between known fields must not disappear from a layout proof.
            raise ValueError("parameter_layout_not_supported")
        result = rebound, declarations, evidence["evidence_id"]
        self.layouts[symbol["symbol_id"]] = result
        return result

    def refs(self, ids: list[str]) -> list[dict[str, object]]:
        return [{"evidence_id": evidence_id, "relative_path": self.evidence[evidence_id]["relative_path"],
                 "start_line": self.evidence[evidence_id]["start_line"], "end_line": self.evidence[evidence_id]["end_line"]}
                for evidence_id in dict.fromkeys(ids)]


def _declaration(text: str) -> tuple | None:
    match = DECLARATION_RE.fullmatch(text.strip().upper())
    if not match:
        return None
    if match.group(2) == "FILLER":
        return None
    level = int(match.group(1))
    if level not in {*range(1, 50), 77}:
        return None
    tail = match.group(3).strip()
    if tail.endswith("."):
        tail = tail[:-1].rstrip()
    if not tail:
        return (level, "GROUP")
    # Values affect initialization, not the layout; only simple literal VALUE
    # clauses are ignored. Any additional layout-affecting clause is rejected.
    tail = re.sub(r"\s+VALUE(?:\s+IS)?\s+(?:[-+]?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\"|ZERO(?:ES|S)?|SPACES?)$", "", tail)
    scalar = re.fullmatch(r"(?:PIC|PICTURE)(?:\s+IS)?\s+([SX9V0-9()]+)(?:\s+(?:USAGE(?:\s+IS)?\s+)?(DISPLAY|COMP|COMP-3|COMP-4|COMP-5|BINARY|PACKED-DECIMAL))?", tail)
    if not scalar:
        return None
    picture = scalar.group(1)
    if not re.fullmatch(r"X(?:\([1-9][0-9]{0,3}\))?|S?(?:9(?:\([1-9][0-9]{0,3}\))?)+(?:V(?:9(?:\([1-9][0-9]{0,3}\))?)+)?", picture):
        return None
    usage = scalar.group(2) or "DISPLAY"
    usage = {"COMP": "BINARY", "COMP-4": "BINARY", "COMP-3": "PACKED-DECIMAL"}.get(usage, usage)
    if picture.startswith("X") and usage != "DISPLAY":
        return None
    return (level, "SCALAR", picture, usage)


def _by_value_supported(layout: list[tuple]) -> bool:
    if len(layout) != 1 or layout[0][1] != "SCALAR":
        return False
    picture, usage = layout[0][2:]
    if "V" in picture or usage not in {"BINARY", "COMP-5"}:
        return False
    digits = sum(int(match.group(1) or 1) for match in re.finditer(r"9(?:\(([0-9]+)\))?", picture))
    return 1 <= digits <= 18


def rebuild_call_bindings(connection: sqlite3.Connection, normalize_source: Callable | None = None) -> dict[str, object]:
    """Rebuild against all files after COPY expansion and static call resolution."""
    context = BindingContext(connection, normalize_source)
    for call in sorted((unit for unit in context.units.values() if unit["unit_type"] == "Statement" and unit["name"] == "CALL"), key=lambda row: (row["relative_path"], row["start_line"])):
        callee_name: str | None = None

        def record(reason: str, *, position: int | None = None, mode: str | None = None,
                   caller_field: sqlite3.Row | None = None, callee_field: sqlite3.Row | None = None,
                   refs: list[str] | None = None, member_index: int = 0) -> None:
            status = "confirmed" if reason in {"parameter_position_layout_match", "call_without_parameters"} else "unresolved"
            ids = list(dict.fromkeys(refs or [call["evidence_id"]]))
            binding_id = _id("binding", call["unit_id"], position, member_index)
            connection.execute("INSERT INTO call_bindings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (binding_id, call["unit_id"], call["program_name"] or "", callee_name,
                                position, member_index, mode, caller_field["symbol_id"] if caller_field else None,
                                callee_field["symbol_id"] if callee_field else None, status, reason,
                                call["evidence_id"], json.dumps(ids)))
            if status == "unresolved":
                for edge in context.call_relations.get(call["unit_id"], []):
                    metadata = json.loads(edge["metadata_json"])
                    metadata["boundary"] = "call_parameter_binding_incomplete"
                    connection.execute("UPDATE relations SET metadata_json = ? WHERE relation_id = ?",
                                       (json.dumps(metadata, sort_keys=True), edge["relation_id"]))
            if status != "confirmed" or caller_field is None or callee_field is None:
                return
            metadata: dict[str, object] = {"parameter_position": position, "passing_mode": mode,
                                          "callsite_id": call["unit_id"], "supporting_evidence_refs": context.refs(ids)}
            if member_index:
                metadata["group_member_index"] = member_index
            for relation_type, source, target, relation_status in [
                ("PASSES_AS", caller_field, callee_field, "confirmed"),
                *(([("MAY_WRITE_BACK", callee_field, caller_field, "candidate")]) if mode == "REFERENCE" else []),
            ]:
                relation_metadata = dict(metadata)
                if relation_type == "MAY_WRITE_BACK":
                    relation_metadata["boundary"] = "runtime_writeback_not_proven"
                connection.execute("INSERT INTO relations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                   (_id("param_rel", binding_id, relation_type), call["relative_path"],
                                    source["definition_unit_id"], relation_type, target["name"], target["program_name"],
                                    target["symbol_id"], relation_status, call["evidence_id"], json.dumps(relation_metadata, sort_keys=True)))

        try:
            form = parse_call(call["normalized_text"])
            callee_name = form.target if not form.dynamic else None
            if form.dynamic:
                raise ValueError("dynamic_target_not_resolved")
            caller_programs = context.programs.get(call["program_name"], [])
            if len(caller_programs) != 1:
                raise ValueError("caller_scope_ambiguous")
            targets = context.programs.get(form.target, [])
            if len(targets) != 1:
                raise ValueError("call_target_ambiguous" if targets else "call_target_not_found")
            target = targets[0]
            if call["relative_path"] in context.compound_program_files or target["relative_path"] in context.compound_program_files:
                # The parser does not recover nested-program lexical scope or
                # END PROGRAM restoration; do not flatten it into a binding.
                raise ValueError("program_scope_not_supported")
            signatures = context.signatures.get((target["relative_path"], target["program_name"]), [])
            if len(signatures) != 1:
                raise ValueError("procedure_signature_ambiguous" if signatures else "procedure_signature_not_found")
            signature = signatures[0]
            if signature["parse_status"] != "complete":
                raise ValueError("procedure_signature_not_supported")
            formal_parameters = parse_signature(signature["normalized_text"])
            if len(form.parameters) != len(formal_parameters):
                raise ValueError("parameter_arity_mismatch")
            if call["program_name"] in context.incomplete or form.target in context.incomplete:
                raise ValueError("copy_scope_incomplete")
        except ValueError as error:
            record(str(error))
            continue
        if not form.parameters:
            record("call_without_parameters", refs=[call["evidence_id"], signature["evidence_id"]])
        for position, (actual, formal) in enumerate(zip(form.parameters, formal_parameters), 1):
            caller_field = callee_field = None
            refs = [call["evidence_id"], signature["evidence_id"]]
            try:
                if (actual.mode == "VALUE") != (formal.mode == "VALUE"):
                    raise ValueError("parameter_mode_mismatch")
                for role, name, program in (("caller", actual.name, call["program_name"]), ("callee", formal.name, form.target)):
                    candidates = context.fields.get((program, name), [])
                    if len(candidates) != 1:
                        raise ValueError(f"{role}_field_ambiguous" if candidates else f"{role}_field_not_found")
                    if role == "caller":
                        caller_field = candidates[0]
                    else:
                        callee_field = candidates[0]
                assert caller_field is not None and callee_field is not None
                callee_decl = DECLARATION_RE.match(context.units[callee_field["definition_unit_id"]]["normalized_text"].upper())
                if context.storage(callee_field) != "LINKAGE" or callee_decl is None or int(callee_decl.group(1)) not in {1, 77}:
                    raise ValueError("callee_not_linkage_parameter")
                if context.storage(caller_field) not in {"WORKING-STORAGE", "LOCAL-STORAGE", "LINKAGE"}:
                    raise ValueError("caller_storage_not_supported")
                caller_members, caller_layout, caller_evidence = context.layout(caller_field)
                callee_members, callee_layout, callee_evidence = context.layout(callee_field)
                refs += [caller_evidence, callee_evidence]
                refs += [edge["evidence_id"] for field in (caller_field, callee_field) for edge in context.inclusions(field)]
                if len(set(refs)) > MAX_SUPPORTING_REFS:
                    raise ValueError("parameter_evidence_limit")
                if caller_layout != callee_layout:
                    raise ValueError("parameter_layout_mismatch")
                if actual.mode == "VALUE" and not _by_value_supported(caller_layout):
                    raise ValueError("by_value_layout_not_supported")
                for member_index, (caller_member, callee_member) in enumerate(zip(caller_members, callee_members)):
                    record("parameter_position_layout_match", position=position, mode=actual.mode,
                           caller_field=caller_member, callee_field=callee_member, refs=refs, member_index=member_index)
            except ValueError as error:
                record(str(error), position=position, mode=actual.mode, caller_field=caller_field,
                       callee_field=callee_field, refs=refs[:MAX_SUPPORTING_REFS])
    return {
        "callsites": connection.execute("SELECT COUNT(DISTINCT callsite_id) FROM call_bindings").fetchone()[0],
        "confirmed_bindings": connection.execute("SELECT COUNT(*) FROM call_bindings WHERE status = 'confirmed' AND parameter_position IS NOT NULL").fetchone()[0],
        "writeback_candidates": connection.execute("SELECT COUNT(*) FROM relations WHERE relation_type = 'MAY_WRITE_BACK'").fetchone()[0],
        "boundary_counts": {row["reason"]: row["count"] for row in connection.execute("SELECT reason, COUNT(*) count FROM call_bindings WHERE status = 'unresolved' GROUP BY reason ORDER BY reason")},
    }
