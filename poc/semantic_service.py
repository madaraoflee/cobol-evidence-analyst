"""Shared bounded control/data-flow services over the local statement frontend.

Results are possible source flows. They do not predict runtime data or prove
path feasibility. Unknown effects explicitly participate in reaching definitions.
"""

from __future__ import annotations

from collections import defaultdict, deque
from contextlib import closing
from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from call_bindings import parse_call, parse_signature
from statement_parser import parse_statements, SourceSpan
from statement_facts import data_references, CompilationGuardState, program_identifier, program_identifiers
from numeric_semantics import receiving_contract, store_decimal
from parser_adapter import BuiltinParserAdapter
from semantic_ir import bind_reference, compilation_ir_from_dict, unbounded_region_ids
from structural_index import read_source_document, DIVISION_RE, SECTION_RE, PARAGRAPH_RE, PARAGRAPH_EXCLUSIONS


SERVICE_VERSION = "bounded-semantic-service/v9"


def _key(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:24]


class FactStore:
    """Read one program and its bindings on demand; never load the whole index."""
    def __init__(self, database_path, source_root, *, source_map=None, source_format="auto", source_manifest=None, check_cancel=None):
        self.check_cancel = check_cancel
        self.database_path, self.source_root = Path(database_path), Path(source_root)
        self.source_map, self.source_format = source_map or {}, source_format
        self.source_manifest = (source_manifest if isinstance(source_manifest, dict) else
                                {item["relative_path"]: item for item in source_manifest or ()})
        self._programs, self._parses = {}, {}
        self._storage_refs = {}
        self.query_count = 0
        with closing(sqlite3.connect(self.database_path)) as db:
            self._has_ir = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='semantic_ir_documents'").fetchone())
            options = db.execute("SELECT value FROM metadata WHERE key='source_options'").fetchone()
            source_options = json.loads(options[0]) if options else {}
        self.compilation_context = source_options.get("compilation_context", {"source_format": source_format})
        self.source_options = source_options

    def rows(self, sql, arguments=()):
        self.query_count += 1
        with closing(sqlite3.connect(self.database_path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, arguments)]

    def resolve(self, program):
        rows = self.rows("SELECT DISTINCT relative_path FROM code_units WHERE unit_type='Program' AND name=?", (program,))
        return rows[0]["relative_path"] if len(rows) == 1 else None

    def program(self, path):
        if self.check_cancel:
            self.check_cancel()
        if path in self._programs:
            return self._programs[path]
        options = self.source_options.get("source_options_by_path", {}).get(path, {}) | self.source_manifest.get(path, {})
        source_format = options.get("source_format", self.source_format)
        source = read_source_document(self.source_root / path, self.source_root,
            encoding=options.get("encoding", self.source_options.get("encoding", "auto")), source_format=source_format)
        if source is None:
            raise ValueError("source_not_decodable")
        indexed = self.rows("SELECT sha256 FROM source_files WHERE relative_path=?", (path,))
        if indexed and indexed[0]["sha256"] != source.source_sha256:
            raise ValueError("source_version_mismatch")
        blocks, order, current, lines, procedure = {}, [], "@ENTRY", [], False
        signature, name, diagnostics = (), None, []
        compilation = CompilationGuardState(self.compilation_context)
        physical_status = {}
        def flush():
            nonlocal lines
            if current not in blocks:
                blocks[current] = []
                order.append(current)
            blocks[current].extend(lines)
            lines = []
        sections, section = {}, None
        for index, line in enumerate(source.lines):
            if self.check_cancel and index % 128 == 0:
                self.check_cancel()
            upper = line.text.upper().strip()
            if compilation.feed(line.text, line.start_line):
                continue
            for number in range(line.start_line, line.end_line + 1):
                physical_status[number] = compilation.status
            if compilation.status is False:
                continue
            if compilation.status is None:
                diagnostics.append("compile_condition_unknown")
                if procedure:
                    lines.append(line)
                continue
            if DIVISION_RE.match(upper):
                if upper.startswith("PROCEDURE"):
                    procedure = True
                    try:
                        signature = parse_signature(upper)
                    except ValueError as exc:
                        diagnostics.append(str(exc))
                elif procedure:
                    break
                continue
            if not procedure:
                continue
            heading = SECTION_RE.match(upper)
            paragraph = PARAGRAPH_RE.match(upper)
            if heading or paragraph and paragraph.group(1) not in PARAGRAPH_EXCLUSIONS:
                flush()
                current = (heading or paragraph).group(1).upper()
                if heading:
                    section = current
                    sections[section] = []
                elif section:
                    sections[section].append(current)
                continue
            lines.append(line)
        flush()
        unclosed = compilation.finish()
        for guard in unclosed:
            for number in range(guard["source_line"], len(source.raw_lines)+1):
                if physical_status.get(number) is True:
                    physical_status[number] = None
        diagnostics.extend(row["reason"] for row in compilation.diagnostics)
        identity_lines = [line for line in source.lines if all(
            physical_status.get(number) is True for number in range(line.start_line, line.end_line+1))]
        identity_source = "\n".join(line.text for line in identity_lines)
        name = program_identifier(identity_source)
        if len(program_identifiers(identity_source)) > 1:
            diagnostics.append("multiple_programs_not_supported")
        parsed = {}
        for block, normalized in blocks.items():
            if not normalized:
                parsed[block] = parse_statements("")
                continue
            first, last = normalized[0].start_line, normalized[-1].end_line
            physical = []
            for number in range(first, last + 1):
                if self.check_cancel and (number - first) % 128 == 0:
                    self.check_cancel()
                original_line = source.raw_lines[number - 1]
                status = physical_status.get(number, False)
                if status is True:
                    physical.append(original_line)
                elif status is False:
                    physical.append(" " * len(original_line))
                else:
                    # Candidate source remains in the index, but cannot enter
                    # an asserted execution flow without compilation inputs.
                    prefix = original_line[:7] if source_format == "fixed" else ""
                    physical.append(prefix + "@COMPILE-CANDIDATE")
            text = "\n".join(physical)
            parse_format = ("fixed" if source.format_hint == "fixed" or source_format == "fixed" else "free")
            cache_key = (source.source_sha256, block, text)
            if cache_key not in self._parses:
                self._parses[cache_key] = parse_statements(text, start_line=first, source_format=parse_format, check_cancel=self.check_cancel)
            parsed[block] = self._parses[cache_key]
        fields = self.rows("SELECT s.symbol_id,s.name,s.qualified_name,u.normalized_text,p.name AS storage_section FROM symbols s "
                           "JOIN code_units u ON u.unit_id=s.definition_unit_id "
                           "LEFT JOIN code_units p ON p.unit_id=u.parent_unit_id AND p.unit_type='Section' "
                           "WHERE s.relative_path=? AND s.symbol_type IN ('Field','ConditionName')", (path,))
        ir_rows = self.rows("SELECT ir_json FROM semantic_ir_documents WHERE relative_path=?", (path,)) if self._has_ir else []
        ir = compilation_ir_from_dict(json.loads(ir_rows[0]["ir_json"])) if ir_rows else BuiltinParserAdapter().parse(
            "\n".join(source.raw_lines), relative_path=path, context=self.compilation_context,
            source_map=self.source_map.get(path), source_sha256=source.source_sha256, check_cancel=self.check_cancel)
        counts = defaultdict(int)
        for item in ir.data_items:
            counts[item.name] += 1
        canonical = {item.item_id: item.name if counts[item.name] == 1 else item.qualified_name for item in ir.data_items}
        initial = {canonical[item.item_id]: {"value": item.value, "source_refs": [asdict(ref) for ref in item.source_refs],
                    "status": "initial_value_not_runtime_proof", "activation_semantics": "first_activation_or_initial_program_context_required"}
                   for item in ir.data_items if item.value is not None and item.level != 88 and item.compilation_status == "active"}
        result = {"path": path, "name": name, "sha256": source.source_sha256,
                  "signature": signature, "blocks": parsed, "order": order, "sections": sections,
                  "fields": fields, "diagnostics": diagnostics, "ir": ir, "canonical_items": canonical,
                  "initial_definitions": initial}
        items_by_name, children, roots, fields_by_name, fields_by_qualified = (defaultdict(list) for _ in range(5))
        for item in ir.data_items:
            items_by_name[item.name].append(item)
            children[item.parent_id].append(item.item_id)
        for region in ir.storage_regions:
            roots[region.root_id].append(region.item_id)
        for row in fields:
            fields_by_name[row["name"]].append(row)
            fields_by_qualified[row["qualified_name"].split("::")[-1]].append(row)
        unbounded = unbounded_region_ids(ir.storage_regions, check_cancel=self.check_cancel)
        result.update(items_by_name=items_by_name, children=children, roots=roots,
                      items={item.item_id: item for item in ir.data_items},
                      item_order={item.item_id: index for index, item in enumerate(ir.data_items)},
                      regions={region.item_id: region for region in ir.storage_regions},
                      alias_roots={region.root_id for region in ir.storage_regions
                                   if region.overlaps or region.overlap_scope == "all_visible_storage"},
                      unbounded_items={region.item_id for region in ir.storage_regions if region.region_id in unbounded},
                      fields_by_name=fields_by_name, fields_by_qualified=fields_by_qualified)
        self._programs[path] = result
        result["identity_source_refs"] = []
        identities = program_identifiers(identity_source)
        if len(identities) == 1:
            _, start, end = identities[0]
            offset, covered = 0, []
            for line in identity_lines:
                if offset < end and offset + len(line.text) > start:
                    covered.append(line)
                offset += len(line.text) + 1
            if covered:
                result["identity_source_refs"] = self.source_ref(path, SourceSpan(
                    covered[0].start_line, covered[-1].end_line, 1, 1, 0, 0))
        return result

    def storage_reference(self, path, name):
        key = (path, name)
        if key not in self._storage_refs:
            self._storage_refs[key] = self._resolve_storage_reference(path, name)
        return self._storage_refs[key]

    def _resolve_storage_reference(self, path, name):
        program = self.program(path)
        reference = next(iter(data_references(name)), None)
        if reference is None:
            return {"canonical": name, "binding_status": "unresolved", "exact": False,
                    "affected_storage": list(program["canonical_items"].values()), "diagnostics": ["data_reference_not_supported"]}
        bound = bind_reference(name, program["items_by_name"].get(reference.name, ()))
        items, regions = program["items"], program["regions"]
        if len(bound.candidates) != 1 or bound.binding_status != "bound":
            affected = [program["canonical_items"][key] for key in bound.candidates] or list(program["canonical_items"].values())
            return {"canonical": reference.identity, "binding_status": bound.binding_status, "exact": False,
                    "affected_storage": affected, "diagnostics": ["storage_binding_" + bound.binding_status]}
        key = bound.candidates[0]
        item, region = items[key], regions[key]
        # A group covers its descendants. Sibling elementary items remain
        # separate unless an explicit alias or unresolved layout overlaps them.
        descendants, pending = [], list(program["children"].get(key, ()))
        while pending:
            other = pending.pop()
            if self.check_cancel and len(descendants) % 128 == 0:
                self.check_cancel()
            if items[other].level != 88:
                descendants.append(other)
            pending.extend(program["children"].get(other, ()))
        alias_root = region.root_id in program["alias_roots"]
        local_uncertain = bool(reference.subscripts or reference.reference_modification or region.array or alias_root or region.layout_status == "unresolved"
                         or item.level in {66, 88})
        affected = {key, *descendants}
        unbounded = program["unbounded_items"]
        uncertain = local_uncertain or bool(unbounded)
        if local_uncertain:
            affected.update(program["roots"].get(region.root_id, ()))
        unknown_layout = not affected.isdisjoint(unbounded)
        if unknown_layout:
            affected.update(items)
        else:
            affected.update(unbounded)
        diagnostics = list(region.diagnostics) if uncertain else []
        if unknown_layout:
            diagnostics.append("unresolved_layout_broad_overlap")
        elif unbounded:
            diagnostics.append("may_overlap_unresolved_storage")
        return {"canonical": program["canonical_items"][key], "binding_status": "bound", "exact": not uncertain,
                "region_id": region.region_id, "affected_storage": [program["canonical_items"][other] for other in sorted(affected, key=program["item_order"].__getitem__)],
                "group": bool(descendants), "diagnostics": diagnostics}

    def source_ref(self, path, span):
        program = self.program(path)
        refs = []
        mapping = self.source_map.get(path)
        if mapping:
            for line in range(span.start_line, span.end_line + 1):
                if 0 <= line - 1 < len(mapping):
                    item = mapping[line - 1]
                    origin = item["origin"]
                    ref = {"relative_path": origin["relative_path"], "source_sha256": origin.get("source_hash", origin.get("sha256")),
                        "start_line": origin["line"], "end_line": origin.get("end_line", origin["line"]),
                        "include_chain": item.get("include_chain", []), "replacement_trace": item.get("replacement_trace", [])}
                    # Adjacent derived lines often describe one physical span.
                    # Keep copy instances, replacements and source versions
                    # separate while avoiding one evidence page per comment.
                    previous = refs[-1] if refs else None
                    if (previous and ref["source_sha256"] and all(previous.get(key) == ref.get(key)
                            for key in ("relative_path", "source_sha256", "include_chain", "replacement_trace"))
                            and ref["start_line"] <= previous["end_line"] + 1
                            and ref["end_line"] >= previous["start_line"] - 1):
                        previous["start_line"] = min(previous["start_line"], ref["start_line"])
                        previous["end_line"] = max(previous["end_line"], ref["end_line"])
                    else:
                        refs.append(ref)
        else:
            refs.append({"relative_path": path, "source_sha256": program["sha256"],
                "start_line": span.start_line, "end_line": span.end_line,
                "start_column": span.start_column, "end_column": span.end_column,
                "include_chain": [], "replacement_trace": []})
        return refs


@dataclass
class FlowNode:
    occurrence_id: str
    kind: str
    statement: str
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    expressions: dict
    source_refs: list[dict]
    context: tuple[str, ...]
    guards: list[dict] = field(default_factory=list)
    unknown_effect: bool = False
    control_occurrences: list[str] = field(default_factory=list)
    numeric_semantics: dict = field(default_factory=dict)
    unknown_storage: tuple[str, ...] | None = None
    control_semantics: dict = field(default_factory=dict)
    storage_effects: dict = field(default_factory=dict)
    definite_writes: tuple[str, ...] = ()
    read_bindings: dict[str, tuple[str, ...]] = field(default_factory=dict)
    call_contract: dict = field(default_factory=dict)


@dataclass
class ProcedureSummary:
    name: str
    inputs: list[str]
    conditional_outputs: list[dict]
    state_changes: list[str]
    external_effects: list[dict]
    dependencies: list[str]
    unknown_effects: list[dict]
    source_evidence: list[dict]


@dataclass
class ProgramSummary(ProcedureSummary):
    source_sha256: str = ""
    version: str = SERVICE_VERSION


class SemanticService:
    def __init__(self, facts, *, max_nodes=2000, max_depth=8, check_cancel=None,
                 max_flow_fields=4096, max_flow_states=250000, max_flow_edges=16000):
        self.facts, self.max_nodes, self.max_depth = facts, max_nodes, max_depth
        self.check_cancel = check_cancel
        self.max_flow_fields, self.max_flow_states, self.max_flow_edges = max_flow_fields, max_flow_states, max_flow_edges
        self._work_checks = 0
        self.nodes, self.edges, self.frontier, self.dependencies = {}, set(), [], set()
        self.entry, self.exits = None, set()
        self.reaching_in, self.reaching_out = {}, {}
        self._return_nodes = defaultdict(set)
        self._guard_nodes = {}
        self._loop_exits = defaultdict(set)
        self._loop_cycles = defaultdict(set)
        self._paragraph_exits = defaultdict(set)
        self._section_exits = defaultdict(set)
        self._built = False
        self._flow_complete = True

    def _node(self, statement, path, context, aliases, *, kind=None, reads=None, writes=None, unknown=None):
        if self.check_cancel:
            self.check_cancel()
        if len(self.nodes) >= self.max_nodes:
            raise ValueError("semantic_node_budget")
        resolved = {}
        def bind(name):
            reference = self.facts.storage_reference(path, name)
            resolved[name] = reference
            return aliases.get(name, aliases.get(reference["canonical"], reference["canonical"]))
        expressions = {}
        expression = statement.expression or ""
        for target in (statement.writes if writes is None else writes):
            value = dict(statement.expressions).get(target, expression)
            for formal, actual in sorted(aliases.items(), key=lambda item: -len(item[0])):
                value = re.sub(r"(?<![\w-])" + re.escape(formal) + r"(?![\w-])", actual, value)
            expressions[bind(target)] = value
        identifier = _key(path, context, statement.span.start_offset, statement.span.start_line, statement.kind, len(self.nodes))
        self.nodes[identifier] = FlowNode(identifier, kind or statement.kind, statement.text,
            tuple(bind(name) for name in (statement.reads if reads is None else reads)),
            tuple(bind(name) for name in (statement.writes if writes is None else writes)), expressions,
            self.facts.source_ref(path, statement.span), context,
            [{"kind": guard.kind, "condition": guard.condition, "outcome": guard.outcome,
              "selector": guard.selector, "alternatives": guard.alternatives,
              "prior_alternatives": guard.prior_alternatives, "is_other": guard.is_other} for guard in statement.guards],
            statement.effects_unknown if unknown is None else unknown)
        self.nodes[identifier].control_occurrences = list(dict.fromkeys(self._guard_nodes[(path, context, guard.span.start_offset)]
            for guard in statement.guards if (path, context, guard.span.start_offset) in self._guard_nodes))
        node = self.nodes[identifier]
        expanded_reads = []
        for name in (statement.reads if reads is None else reads):
            reference = resolved[name]
            node.storage_effects["read:" + name] = reference
            if reference["binding_status"] != "bound" or reference.get("group"):
                bound_reads = tuple(aliases.get(value, value) for value in reference["affected_storage"])
            else:
                bound_reads = (bind(name),)
            expanded_reads.extend(bound_reads)
            node.read_bindings[name] = bound_reads
            if reference["binding_status"] != "bound":
                self.frontier.append({"reason": "read_storage_binding_" + reference["binding_status"],
                    "occurrence_id": identifier, "reference": name,
                    "affected_storage": reference["affected_storage"], "source_refs": node.source_refs})
        node.reads = tuple(dict.fromkeys(expanded_reads))
        definite, uncertain_storage = set(), set()
        for target in (statement.writes if writes is None else writes):
            reference = resolved[target]
            affected = tuple(aliases.get(value, value) for value in reference["affected_storage"])
            node.storage_effects[bind(target)] = reference
            if reference["exact"]:
                definite.update(affected)
            else:
                uncertain_storage.update(affected)
        node.definite_writes = tuple(sorted(definite))
        if uncertain_storage:
            node.unknown_effect = True
            node.unknown_storage = tuple(sorted(uncertain_storage))
            self.frontier.append({"reason": "storage_overlap_requires_index_or_layout", "occurrence_id": identifier,
                                  "affected_storage": list(node.unknown_storage), "source_refs": node.source_refs})
        if expressions:
            declared = self.facts.program(path)
            for target in (statement.writes if writes is None else writes):
                choices = declared["fields_by_qualified"].get(target, ())
                if not choices and " OF " not in target:
                    choices = declared["fields_by_name"].get(target, ())
                if len(choices) == 1:
                    self.nodes[identifier].numeric_semantics[bind(target)] = receiving_contract(
                        choices[0]["normalized_text"], rounded=statement.rounded,
                        on_size_error=statement.size_error_handler_present, operation=statement.kind)
                else:
                    self.nodes[identifier].numeric_semantics[bind(target)] = {
                        "status": "unsupported", "reason": "receiver_declaration_ambiguous_or_missing"}
        return identifier

    def _check_work(self, *, force=False):
        self._work_checks += 1
        if self.check_cancel and (force or self._work_checks % 64 == 0):
            self.check_cancel()

    def _sequence(self, statements, path, context, aliases, stack):
        first, tails = None, set()
        for statement in statements:
            if first is not None and not tails:
                break
            entry, exits = self._statement(statement, path, context, aliases, stack)
            if entry is None:
                continue
            if first is None:
                first = entry
            for previous in tails:
                self.edges.add((previous, entry, "sequence"))
            tails = exits
        return first, tails

    def _statement(self, statement, path, context, aliases, stack):
        if statement.loop:
            return self._loop(statement, path, context, aliases, stack)
        if statement.exception_children or statement.normal_children:
            return self._arithmetic_exception(statement, path, context, aliases, stack)
        if statement.kind in {"IF", "EVALUATE", "WHEN"}:
            condition = (statement.children[0].guards[-1].condition if statement.kind == "IF" and statement.children else "")
            if statement.kind == "EVALUATE" and statement.children:
                condition = statement.children[0].guards[-1].selector or ""
            reads = tuple(ref.identity for ref in data_references(condition))
            header = self._node(statement, path, context, aliases, reads=reads, writes=(), unknown=False)
            for branch in statement.children + statement.else_children:
                for guard in branch.guards[len(statement.guards):]:
                    self._guard_nodes[(path, context, guard.span.start_offset)] = header
            branches = ([statement.children, statement.else_children] if statement.kind == "IF"
                        else [branch.children for branch in statement.children] if statement.kind == "EVALUATE"
                        else [statement.children])
            tails = set()
            for index, branch in enumerate(branches):
                entry, exits = self._sequence(branch, path, context, aliases, stack)
                if entry:
                    self.edges.add((header, entry, "branch:" + str(index)))
                    tails.update(exits)
                else:
                    tails.add(header)
            if statement.kind == "EVALUATE" and not any(branch.guards[-1].is_other for branch in statement.children):
                tails.add(header)
            return header, tails
        if statement.kind == "PERFORM":
            header = self._node(statement, path, context, aliases, unknown=False)
            from statement_facts import dependency_facts
            targets = dependency_facts(statement.text)
            target = next((item for item in targets if item[0].startswith("PERFORMS")), None)
            program = self.facts.program(path)
            if target and target[1] in program["blocks"]:
                names = [target[1]]
                if target[2]:
                    if target[2] in program["order"] and program["order"].index(target[2]) >= program["order"].index(target[1]):
                        names = program["order"][program["order"].index(target[1]):program["order"].index(target[2]) + 1]
                    else:
                        return self._unknown(header, "perform_range_unresolved")
                elif target[1] in program["sections"]:
                    names += program["sections"][target[1]]
                if (path, target[1]) in stack or len(stack) >= self.max_depth:
                    return self._unknown(header, "recursive_or_depth_limited_perform")
                entry, exits = self._blocks(path, names, context + ("perform:" + header,), aliases, stack + ((path, target[1]),))
                if entry:
                    self.edges.add((header, entry, "perform_entry"))
                    return header, exits
                return header, {header}
            return self._unknown(header, "perform_loop_or_target_unresolved")
        if statement.kind == "CALL":
            header = self._node(statement, path, context, aliases, unknown=False)
            try:
                call = parse_call(statement.text)
            except ValueError as exc:
                return self._unknown(header, str(exc))
            target_path = None if call.dynamic else self.facts.resolve(call.target)
            self.dependencies.add(call.target)
            if not target_path:
                return self._unknown(header, "dynamic_or_external_call_effect_unbounded")
            callee = self.facts.program(target_path)
            if (target_path, "@PROGRAM") in stack or len(stack) >= self.max_depth:
                return self._unknown(header, "recursive_or_depth_limited_call")
            if len(call.parameters) != len(callee["signature"]):
                return self._unknown(header, "parameter_arity_mismatch")
            # Use the existing layout/binding proof; names alone do not establish ABI compatibility.
            bindings = self.facts.rows("SELECT b.*,cs.qualified_name AS caller_qualified,ds.qualified_name AS callee_qualified, "
                "(SELECT json_group_array(json_object('evidence_id',e.evidence_id,'relative_path',e.relative_path,'start_line',e.start_line,'end_line',e.end_line)) "
                "FROM evidence_spans e JOIN json_each(b.supporting_evidence_ids_json) i ON i.value=e.evidence_id) AS binding_refs_json "
                "FROM call_bindings b JOIN code_units u ON u.unit_id=b.callsite_id "
                "LEFT JOIN symbols cs ON cs.symbol_id=b.caller_symbol_id LEFT JOIN symbols ds ON ds.symbol_id=b.callee_symbol_id "
                "WHERE u.relative_path=? AND u.start_line=? AND b.callee_program=?", (path, statement.span.start_line, call.target))
            if call.parameters and (not bindings or any(item["status"] != "confirmed" for item in bindings)):
                return self._unknown(header, "parameter_binding_not_confirmed")
            if any(set(json.loads(binding["supporting_evidence_ids_json"])) != {
                    ref["evidence_id"] for ref in json.loads(binding["binding_refs_json"] or "[]")} for binding in bindings):
                return self._unknown(header, "parameter_binding_evidence_incomplete")
            mapped = {name: "@local:" + header + ":" + name for name in callee["canonical_items"].values()}
            for actual, formal in zip(call.parameters, callee["signature"]):
                actual_name = self.facts.storage_reference(path, actual.name)["canonical"]
                formal_name = self.facts.storage_reference(target_path, formal.name)["canonical"]
                mapped[formal_name] = aliases.get(actual_name, actual_name) if actual.mode == "REFERENCE" else "@local:" + header + ":" + formal_name
            for binding in bindings:
                if binding["passing_mode"] != "REFERENCE" or not binding["caller_qualified"] or not binding["callee_qualified"]:
                    continue
                actual_name = self.facts.storage_reference(path, binding["caller_qualified"].split("::")[-1])["canonical"]
                formal_name = self.facts.storage_reference(target_path, binding["callee_qualified"].split("::")[-1])["canonical"]
                mapped[formal_name] = aliases.get(actual_name, actual_name)
            entry, exits = self._blocks(target_path, callee["order"], context + ("call:" + header,), mapped,
                                       stack + ((target_path, "@PROGRAM"),))
            exits.update(self._return_nodes["call:" + header])
            support = [*self.nodes[header].source_refs, *self.facts.program(path)["identity_source_refs"],
                       *callee["identity_source_refs"]]
            for binding in bindings:
                for ref in json.loads(binding["binding_refs_json"] or "[]"):
                    support.extend(self.facts.source_ref(ref["relative_path"], SourceSpan(
                        ref["start_line"], ref["end_line"], 1, 1, 0, 0)))
            self.nodes[header].call_contract = {"status": "confirmed_source_binding", "target_path": target_path,
                "target_program": call.target, "source_refs": list({json.dumps(ref, sort_keys=True): ref for ref in support}.values()),
                "parameters": [{key: binding[key] for key in ("binding_id", "parameter_position", "passing_mode",
                    "caller_qualified", "callee_qualified", "status", "reason")} for binding in bindings],
                "normal_return_occurrences": sorted(self._return_nodes["call:" + header]),
                "execution_verified": False}
            if entry:
                self.edges.add((header, entry, "call_entry"))
                return header, exits
            return header, {header}
        identifier = self._node(statement, path, context, aliases)
        if statement.kind == "EXIT":
            exit_kind = statement.exit_kind
            if exit_kind in {"PERFORM", "PERFORM_CYCLE"}:
                marker = next((item for item in reversed(context) if item.startswith("loop:")), None)
                if not marker:
                    return self._unknown(identifier, "exit_perform_outside_supported_loop")
                (self._loop_cycles if exit_kind == "PERFORM_CYCLE" else self._loop_exits)[marker].add(identifier)
                return identifier, set()
            if exit_kind == "PARAGRAPH":
                self._paragraph_exits[(path, context)].add(identifier)
                return identifier, set()
            if exit_kind == "SECTION":
                program = self.facts.program(path)
                section = next((name for name, paragraphs in program["sections"].items()
                                if context[-1] == name or context[-1] in paragraphs), None)
                if not section:
                    return self._unknown(identifier, "exit_section_owner_unresolved")
                self._section_exits[(path, context[:-1], section)].add(identifier)
                return identifier, set()
        if statement.kind == "GOBACK" or statement.kind == "EXIT" and statement.exit_kind == "PROGRAM":
            marker = next((item for item in reversed(context) if item.startswith("call:")), None)
            if marker:
                self._return_nodes[marker].add(identifier)
        if statement.effects_unknown:
            self._unknown(identifier, "statement_effect_not_supported")
        terminal = statement.kind in {"GOBACK", "STOP_RUN"} or statement.kind == "EXIT" and statement.exit_kind == "PROGRAM"
        return identifier, set() if terminal else {identifier}

    def _loop(self, statement, path, context, aliases, stack):
        loop = statement.loop
        condition = loop.get("condition", loop.get("count", ""))
        reads = tuple(ref.identity for ref in data_references(condition))
        header = self._node(statement, path, context, aliases, kind="LOOP_TEST", reads=reads, writes=(), unknown=False)
        self.nodes[header].control_semantics = {**loop, "continue_when": "iteration_remaining" if loop["kind"] == "times" else "condition_false",
                                               "path_feasibility_verified": False}
        marker = "loop:" + header
        entry, exits = self._sequence(statement.children, path, context + (marker,), aliases, stack)
        for key, node in self.nodes.items():
            if marker in node.context and header not in node.control_occurrences:
                node.control_occurrences.append(header)
        cycles = exits | self._loop_cycles.pop(marker, set())
        tails = {header} | self._loop_exits.pop(marker, set())
        initialization = None
        if loop["kind"] == "varying":
            variable = loop["variable"]
            init_stmt = replace(statement, kind="MOVE", writes=(variable,), reads=tuple(ref.identity for ref in data_references(loop["from"])),
                                expression=loop["from"], expressions=((variable, loop["from"]),), loop=None, effects_unknown=False)
            initialization = self._node(init_stmt, path, context, aliases, kind="LOOP_INITIALIZE", unknown=False)
            expression = variable + " + " + loop["by"]
            update_stmt = replace(statement, kind="ADD", writes=(variable,), reads=tuple(ref.identity for ref in data_references(expression)),
                                  expression=expression, expressions=((variable, expression),), loop=None, effects_unknown=False)
            update = self._node(update_stmt, path, context + (marker,), aliases, kind="LOOP_INCREMENT", unknown=False)
            for tail in cycles:
                self.edges.add((tail, update, "loop_increment"))
            cycles = {update}
        if loop["test_before"]:
            if entry:
                self.edges.add((header, entry, "loop_continue"))
                for tail in cycles:
                    self.edges.add((tail, header, "loop_back"))
            if initialization:
                self.edges.add((initialization, header, "loop_test"))
            return initialization or header, tails
        if entry:
            for tail in cycles:
                self.edges.add((tail, header, "loop_test_after"))
            self.edges.add((header, entry, "loop_continue"))
            if initialization:
                self.edges.add((initialization, entry, "loop_first_iteration"))
            return initialization or entry, tails
        return initialization or header, tails

    def _arithmetic_exception(self, statement, path, context, aliases, stack):
        header = self._node(statement, path, context, aliases, kind="SIZE_ERROR_TEST", writes=(), unknown=False)
        targets = tuple(target for target, _ in statement.expressions)
        normal_stmt = replace(statement, writes=targets, expressions=statement.expressions,
                              expression=statement.expressions[0][1] if statement.expressions else None,
                              effects_unknown=False, exception_children=(), normal_children=())
        normal = self._node(normal_stmt, path, context, aliases, unknown=False)
        self.nodes[normal].guards.append({"kind": "SIZE_ERROR", "outcome": False, "condition": "receiving_capacity_and_arithmetic_domain"})
        self.nodes[normal].control_occurrences.append(header)
        self.edges.add((header, normal, "not_size_error"))
        normal_entry, normal_exits = self._sequence(statement.normal_children, path, context + ("not-size-error:" + header,), aliases, stack)
        if normal_entry:
            self.edges.add((normal, normal_entry, "normal_handler"))
        else:
            normal_exits = {normal}
        error_entry, error_exits = self._sequence(statement.exception_children, path, context + ("size-error:" + header,), aliases, stack)
        if error_entry:
            self.edges.add((header, error_entry, "size_error"))
        else:
            # Without ON SIZE ERROR, affected receivers can be truncated. The
            # uncertainty is scoped to syntactically known result storage.
            error = self._node(statement, path, context, aliases, kind="SIZE_ERROR_EFFECT", writes=(), unknown=True)
            self.nodes[error].unknown_storage = tuple(aliases.get(name, name) for name in targets)
            self.frontier.append({"reason": "unhandled_size_error_conversion_requires_profile", "occurrence_id": error,
                                  "affected_storage": list(self.nodes[error].unknown_storage), "source_refs": self.nodes[error].source_refs})
            self.edges.add((header, error, "size_error"))
            error_exits = {error}
        error_nodes, normal_nodes = self._descendants(error_entry), self._descendants(normal_entry)
        for key, node in self.nodes.items():
            if key not in {header, normal} and key in error_nodes | normal_nodes:
                node.control_occurrences.append(header)
                node.guards.append({"kind": "SIZE_ERROR", "outcome": key in error_nodes, "condition": "receiving_capacity_and_arithmetic_domain"})
        return header, normal_exits | error_exits

    def _descendants(self, entry):
        selected, queue = set(), deque([entry] if entry else [])
        while queue:
            self._check_work()
            key = queue.popleft()
            if key in selected:
                continue
            selected.add(key)
            queue.extend(target for source, target, _ in self.edges if source == key)
        return selected

    def _unknown(self, identifier, reason):
        self.nodes[identifier].unknown_effect = True
        self.frontier.append({"reason": reason, "occurrence_id": identifier,
                              "affected_storage": "all_visible_storage", "source_refs": self.nodes[identifier].source_refs})
        return identifier, {identifier}

    def _blocks(self, path, names, context, aliases, stack):
        program = self.facts.program(path)
        first, tails = None, set()
        section_jumps = defaultdict(set)
        for name in names:
            section = next((section for section, paragraphs in program["sections"].items()
                            if name == section or name in paragraphs), None)
            for exited_section in list(section_jumps):
                if exited_section != section:
                    tails.update(section_jumps.pop(exited_section))
            if first is not None and not tails and section_jumps:
                continue
            parsed = program["blocks"][name]
            entry, exits = self._sequence(parsed.nodes, path, context + (name,), aliases, stack)
            exits.update(self._paragraph_exits.pop((path, context + (name,)), set()))
            if section:
                section_exits = self._section_exits.pop((path, context, section), set())
                if section_exits:
                    section_jumps[section].update(section_exits)
            if entry:
                if first is None:
                    first = entry
                for previous in tails:
                    self.edges.add((previous, entry, "fallthrough"))
                tails = exits
            for boundary in parsed.boundaries:
                self._flow_complete = False
                self.frontier.append({"reason": boundary.reason, "relative_path": path,
                                      "start_line": boundary.span.start_line, "affected_storage": "all_visible_storage"})
                # Unsupported syntax is a barrier; later blocks may be entered by PERFORM,
                # but entry fallthrough is not silently asserted through the barrier.
                tails = set()
            if first and not tails and not section_jumps:
                break
        for jumps in section_jumps.values():
            tails.update(jumps)
        return first, tails

    def build(self, path, *, procedure=None):
        program = self.facts.program(path)
        names = [procedure] if procedure else program["order"]
        try:
            self.entry, self.exits = self._blocks(path, names, (program["name"] or path,), {}, ((path, "@PROGRAM"),))
        except ValueError as exc:
            self._flow_complete = False
            self.frontier.append({"reason": str(exc), "affected_storage": "all_visible_storage"})
        self._built = True
        self._reaching()
        return self

    def _reaching(self):
        self._check_work(force=True)
        fields = {name for node in self.nodes.values() for name in (*node.reads, *node.writes, *node.definite_writes,
                                                                  *(node.unknown_storage or ()))}
        if len(fields) > self.max_flow_fields or len(self.edges) > self.max_flow_edges:
            self.frontier.append({"reason": "flow_field_or_edge_budget", "affected_storage": "all_visible_storage"})
            self._flow_complete = False
            self.reaching_in = {key: {} for key in self.nodes}
            self.reaching_out = {key: {} for key in self.nodes}
            return
        predecessors, successors = defaultdict(set), defaultdict(set)
        for first, last, _ in self.edges:
            predecessors[last].add(first)
            successors[first].add(last)
        self.reaching_in = {key: {} for key in self.nodes}
        self.reaching_out = {key: {} for key in self.nodes}
        queue, queued = deque(self.nodes), set(self.nodes)
        state_sizes, total_states = {}, 0
        while queue:
            self._check_work()
            key = queue.popleft()
            queued.discard(key)
            incoming = defaultdict(set)
            if key == self.entry:
                for name in fields:
                    incoming[name].add("@entry:" + name)
            for previous in predecessors[key]:
                for name, definitions in self.reaching_out[previous].items():
                    self._check_work()
                    incoming[name].update(definitions)
            outgoing = {name: set(value) for name, value in incoming.items()}
            node = self.nodes[key]
            if node.unknown_effect:
                for name in (fields if node.unknown_storage is None else node.unknown_storage):
                    outgoing.setdefault(name, set()).add(key)
            for name in node.definite_writes:
                outgoing[name] = {key}
            size = sum(len(value) for value in incoming.values()) + sum(len(value) for value in outgoing.values())
            total_states += size - state_sizes.get(key, 0)
            state_sizes[key] = size
            if total_states > self.max_flow_states:
                self.frontier.append({"reason": "flow_state_budget", "occurrence_id": key, "affected_storage": "all_visible_storage"})
                self._flow_complete = False
                return
            self.reaching_in[key] = dict(incoming)
            if outgoing != self.reaching_out[key]:
                self.reaching_out[key] = outgoing
                for next_key in successors[key]:
                    if next_key not in queued:
                        queue.append(next_key)
                        queued.add(next_key)

    def explain_assignment(self, occurrence_id):
        self._check_work(force=True)
        node = self.nodes[occurrence_id]
        reachable, queue = set(), deque([occurrence_id])
        successors = defaultdict(set)
        for first, last, _ in self.edges:
            successors[first].add(last)
        while queue:
            self._check_work()
            key = queue.popleft()
            for following in successors[key] - reachable:
                reachable.add(following)
                queue.append(following)
        return {"statement": asdict(node), "reaching_definitions": {name: sorted(self.reaching_in[occurrence_id].get(name, ())) for name in node.reads},
                "read_origins": self.read_origins(occurrence_id),
                "later_writes": [key for key in reachable if key != occurrence_id and
                                 set(node.definite_writes or node.writes).intersection(
                                     (*self.nodes[key].definite_writes, *self.nodes[key].writes, *(self.nodes[key].unknown_storage or ())))],
                "flow_status": "possible_source_flow" if self._flow_complete else "incomplete_source_flow", "frontier": self.frontier}

    def read_origins(self, occurrence_id):
        """Bound each actual read to reaching writes and confirmed normal returns.

        This is a source origin witness, not a runtime-value or path-feasibility
        proof. The question service must separately require every raw reference
        in the witness to be supplied before it closes a material obligation.
        """
        reader = self.nodes[occurrence_id]
        successors = defaultdict(set)
        for source, target, _ in self.edges:
            successors[source].add(target)
        reach_cache = {}
        def reaches(start, target):
            if start not in reach_cache:
                seen, pending = set(), deque([start])
                while pending:
                    self._check_work()
                    node_id = pending.popleft()
                    if node_id in seen:
                        continue
                    seen.add(node_id)
                    pending.extend(successors[node_id] - seen)
                reach_cache[start] = seen
            return target in reach_cache[start]
        results = []
        for original, bound in reader.read_bindings.items():
            reference = reader.storage_effects.get("read:" + original, {})
            refs, writes, calls, reasons = list(reader.source_refs), set(), set(), set()
            memo, active = {}, set()
            def visit(read_id, field):
                self._check_work()
                pair = (read_id, field)
                if pair in memo:
                    return memo[pair]
                if pair in active:
                    reasons.add("loop_carried_origin_not_closed")
                    return False
                if len(active) >= 128 or len(memo) + len(active) >= min(self.max_nodes, 512):
                    reasons.add("input_origin_proof_budget")
                    return False
                active.add(pair)
                definitions = self.reaching_in.get(read_id, {}).get(field, set())
                resolved = bool(definitions)
                if not definitions:
                    reasons.add("reaching_origin_not_available")
                for definition in sorted(definitions):
                    self._check_work()
                    writer = self.nodes.get(definition)
                    if writer is None:
                        reasons.add("entry_state_or_external_input_not_defined")
                        resolved = False
                        continue
                    if writer.unknown_effect or field not in writer.definite_writes:
                        refs.extend(writer.source_refs)
                        reasons.add("reaching_write_effect_not_bounded")
                        resolved = False
                        continue
                    writes.add(definition)
                    refs.extend(writer.source_refs)
                    for marker in dict.fromkeys((*writer.context, *self.nodes[read_id].context)):
                        if not marker.startswith("call:"):
                            continue
                        call_id = marker.split(":", 1)[1]
                        header = self.nodes.get(call_id)
                        contract = header.call_contract if header else {}
                        returns = contract.get("normal_return_occurrences", [])
                        crosses_return = marker in writer.context and marker not in self.nodes[read_id].context
                        crosses_entry = marker not in writer.context and marker in self.nodes[read_id].context
                        if (contract.get("status") != "confirmed_source_binding"
                                or not all(parameter.get("status") == "confirmed" for parameter in contract.get("parameters", []))
                                or crosses_return and (not returns or not any(
                                    reaches(definition, returned) and reaches(returned, read_id) for returned in returns))
                                or crosses_entry and not (reaches(definition, call_id) and reaches(call_id, read_id))):
                            reasons.add("call_binding_or_normal_return_not_demonstrated")
                            resolved = False
                            continue
                        calls.add(call_id)
                        refs.extend(contract.get("source_refs", []))
                        if crosses_return:
                            for returned in returns:
                                refs.extend(self.nodes[returned].source_refs)
                    for dependency in writer.reads:
                        if not visit(definition, dependency):
                            resolved = False
                active.remove(pair)
                memo[pair] = resolved
                return resolved
            if not self._flow_complete:
                reasons.add("source_flow_incomplete")
            exact = (reference.get("binding_status") == "bound" and reference.get("exact") is True
                     and not reference.get("group") and len(bound) == 1)
            resolved = self._flow_complete and exact and visit(occurrence_id, bound[0])
            if not exact:
                reasons.add("read_storage_not_exact")
            results.append({"reader_occurrence_id": occurrence_id, "reader_field": original,
                "reader_statement": reader.statement,
                "reader_source_refs": reader.source_refs,
                "status": "resolved_source_origin" if resolved else "unresolved_source_origin",
                "basis": "reaching_definitions_and_confirmed_call_returns",
                "write_occurrences": sorted(writes), "call_occurrences": sorted(calls),
                "source_refs": list({json.dumps(ref, sort_keys=True): ref for ref in refs}.values()),
                "unknown_reasons": sorted(reasons), "source_supply_status": "not_assessed",
                "scope": "possible_source_origin_not_runtime_value", "execution_verified": False})
        return results

    def convert_receiver(self, occurrence_id, target, value, *, profile=None):
        """Convert explicit scenario data; never infer a live business value."""
        node = self.nodes[occurrence_id]
        contract = node.numeric_semantics.get(target, {})
        refs = node.source_refs
        if not refs:
            return {"status": "unsupported", "reason": "receiver_source_unavailable"}
        path = refs[0]["relative_path"]
        declared = self.facts.program(path)["fields"]
        choices = [item for item in declared if item["qualified_name"].split("::")[-1] == target or item["name"] == target]
        if len(choices) != 1:
            return {"status": "unsupported", "reason": "receiver_declaration_ambiguous_or_missing"}
        return store_decimal(value, choices[0]["normalized_text"], rounded=contract.get("rounded", False),
            on_size_error=contract.get("on_size_error_receiver") == "preserved", operation=contract.get("operation", node.kind), profile=profile)

    def backward_slice(self, occurrence_id, field=None):
        self._check_work(force=True)
        selected, queue = set(), deque([occurrence_id])
        while queue:
            self._check_work()
            key = queue.popleft()
            if key in selected or key not in self.nodes:
                continue
            selected.add(key)
            node = self.nodes[key]
            queue.extend(node.control_occurrences)
            for name in node.reads:
                queue.extend(item for item in self.reaching_in[key].get(name, ()) if item in self.nodes)
            # Guards are dependencies even when they do not contribute arithmetic operands.
            for name in (ref.identity for guard in node.guards for ref in data_references(guard.get("condition", ""))):
                queue.extend(item for item in self.reaching_in[key].get(name, ()) if item in self.nodes)
        return self._slice(selected)

    def forward_slice(self, occurrence_id):
        self._check_work(force=True)
        selected, queue = set(), deque([occurrence_id])
        while queue:
            self._check_work()
            key = queue.popleft()
            if key in selected:
                continue
            selected.add(key)
            for consumer, node in self.nodes.items():
                self._check_work()
                if key in node.control_occurrences or any(key in self.reaching_in[consumer].get(name, ()) for name in node.reads):
                    queue.append(consumer)
        return self._slice(selected)

    def _slice(self, selected):
        return {"occurrences": [key for key in self.nodes if key in selected],
                "statements": [asdict(node) for key, node in self.nodes.items() if key in selected],
                "frontier": self.frontier, "flow_status": "possible_source_flow" if self._flow_complete else "incomplete_source_flow"}

    def trace_parameter(self, occurrence_id):
        node = self.nodes[occurrence_id]
        return {"context": node.context, "statement": node.statement, "source_refs": node.source_refs,
                "bindings": self.facts.rows("SELECT b.* FROM call_bindings b JOIN code_units u ON u.unit_id=b.callsite_id "
                    "WHERE u.relative_path=? AND u.start_line=?", (node.source_refs[0]["relative_path"], node.source_refs[0]["start_line"]))}

    def explain_external_effect(self, occurrence_id):
        node = self.nodes[occurrence_id]
        return {"kind": node.kind, "statement": node.statement, "source_refs": node.source_refs,
                "known_syntax": node.kind in {"CALL", "READ", "WRITE", "REWRITE", "DELETE", "OPEN", "CLOSE"},
                "effects_verified": False, "unknown_effect": node.unknown_effect,
                "frontier": [gap for gap in self.frontier if gap.get("occurrence_id") == occurrence_id]}

    def summary(self, path):
        program = self.facts.program(path)
        outputs = [{"target": name, "expression": node.expressions.get(name), "conditions": node.guards,
                    "occurrence_id": node.occurrence_id, "source_refs": node.source_refs,
                    "numeric_semantics": node.numeric_semantics.get(name)}
                   for node in self.nodes.values() for name in node.writes]
        entry_inputs = sorted({name for key, node in self.nodes.items() for name in node.reads
                              if "@entry:" + name in self.reaching_in[key].get(name, ())})
        linkage = set()
        for item in program["fields"]:
            if item["storage_section"] == "LINKAGE":
                linkage.add(self.facts.storage_reference(path, item["qualified_name"].split("::")[-1])["canonical"])
        inputs = [name for name in entry_inputs if name in linkage]
        external = [self.explain_external_effect(key) for key, node in self.nodes.items()
                    if node.kind in {"CALL", "READ", "WRITE", "REWRITE", "DELETE", "OPEN", "CLOSE"}]
        result = asdict(ProgramSummary(program["name"] or path, inputs, outputs,
            sorted({item["target"] for item in outputs}), external, sorted(self.dependencies), self.frontier,
            [ref for node in self.nodes.values() for ref in node.source_refs], program["sha256"]))
        result.update(initial_definitions=program["initial_definitions"],
                      entry_state_inputs=[name for name in entry_inputs if name not in linkage and name not in program["initial_definitions"]],
                      input_origin_status="declaration_and_possible_entry_state", flow_complete=self._flow_complete)
        return result
