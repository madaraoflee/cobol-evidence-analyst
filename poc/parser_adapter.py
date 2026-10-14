"""Current front-end adapter and explicit, isolated external parser candidates.

External candidates consume a local JSON bridge contract. They are never
downloaded or enabled automatically, and unconverted parser objects cannot
cross into business analysis. Native candidate availability is not validation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Protocol

from compilation_context import resolve_compilation_context
from semantic_ir import (CompilationIR, DataItem, Expression, ParsedStatement, SourceRef,
                         StorageRegion, bind_reference, compilation_ir_from_dict, digest)
from statement_facts import (COBOL_RESERVED, CobolLexError, CompilationGuardState, assignment_facts, clean_cobol_line,
                             cobol_code_only, lex_cobol, parse_data_ref, program_identifiers)
from statement_parser import parse_statements, PARSER_VERSION


class ParserAdapter(Protocol):
    def parse(self, source: str, *, relative_path: str, context=None,
              source_map=None, source_sha256=None) -> CompilationIR: ...


def expression_tree(text):
    try:
        tokens = lex_cobol(text)
    except CobolLexError:
        return Expression("opaque", text, support_status="unsupported")
    position = 0

    def parse(minimum=0):
        nonlocal position
        if position >= len(tokens):
            raise ValueError("expression_incomplete")
        token = tokens[position]
        if token.value in {"+", "-"}:
            position += 1
            left = Expression("unary", token.value, (parse(4),))
        elif token.value == "(":
            position += 1
            left = parse()
            if position >= len(tokens) or tokens[position].value != ")":
                raise ValueError("expression_parenthesis")
            position += 1
        elif token.kind in {"number", "literal"} or token.value in {"ZERO", "ZEROS", "ZEROES"}:
            position += 1
            left = Expression("decimal" if token.kind == "number" or token.value.startswith("ZERO") else "literal",
                              "0" if token.value.startswith("ZERO") else token.value)
        else:
            ref, following = parse_data_ref(tokens, position)
            if ref is None:
                raise ValueError("expression_operand")
            position = following
            left = Expression("data_ref", ref.identity)
        precedence = {"+": 1, "-": 1, "*": 2, "/": 2, "**": 3}
        while position < len(tokens) and precedence.get(tokens[position].value, -1) >= minimum:
            operator = tokens[position].value
            level = precedence[operator]
            position += 1
            right = parse(level + 1)
            left = Expression("binary", operator, (left, right))
        return left

    try:
        result = parse()
        return result if position == len(tokens) else Expression("opaque", text, support_status="unsupported")
    except ValueError:
        return Expression("opaque", text, support_status="unsupported")


def _normalized_source(source, context):
    result, active, statuses, guards, directives = [], context.source_format, {}, {}, set()
    compilation = CompilationGuardState(context)
    for number, raw in enumerate(source.splitlines(keepends=True), 1):
        line = raw.rstrip("\r\n")
        ending = raw[len(line):]
        code, active, _ = clean_cobol_line(line, active)
        if compilation.feed(code, number):
            directives.add(number)
            code = ""
        statuses[number], guards[number] = compilation.status, compilation.guards
        if compilation.status is False:
            code = ""
        offset = line.find(code) if code else 0
        result.append(" " * max(0, offset) + code + " " * max(0, len(line) - max(0, offset) - len(code)) + ending)
    unclosed = compilation.finish()
    for guard in unclosed:
        for number in range(guard["source_line"], len(statuses)+1):
            if statuses[number] is not False:
                statuses[number] = None
    diagnostics = list(compilation.diagnostics)
    return "".join(result), statuses, guards, directives, tuple(diagnostics)


def _source_refs(relative_path, source_hash, first, last, start_column=1, end_column=None, source_map=None):
    if not source_map:
        return (SourceRef(relative_path, source_hash, first, last, start_column, end_column),)
    references = []
    for line in range(first, last + 1):
        if not 1 <= line <= len(source_map):
            continue
        mapping = source_map[line - 1]
        origin = mapping["origin"]
        chain = tuple(mapping.get("include_chain", []))
        trace = tuple(mapping.get("replacement_trace", []))
        ref = SourceRef(origin["relative_path"], origin["source_hash"], origin["line"], origin.get("end_line", origin["line"]),
            copy_instance=digest(chain)[:24] if chain else None, include_chain=chain,
            replacement_trace=trace, coordinate_status="mapped_line_only")
        if references and references[-1].relative_path == ref.relative_path and references[-1].include_chain == chain and (
                references[-1].replacement_trace == trace and references[-1].end_line + 1 == ref.start_line):
            references[-1] = replace(references[-1], end_line=ref.end_line)
        elif not references or ref != references[-1]:
            references.append(ref)
    return tuple(references)


def _picture_info(picture):
    if not picture:
        return None, None, None, None
    expanded = re.sub(r"([A-Z9])\((\d+)\)", lambda match: match[1] * min(100000, int(match[2])), picture.upper())
    numeric = bool(re.fullmatch(r"S?[9]+(?:V[9]+)?", expanded))
    digits = expanded.count("9") if numeric else None
    scale = len(expanded.split("V", 1)[1]) if numeric and "V" in expanded else 0 if numeric else None
    length = len(expanded.replace("S", "").replace("V", "")) if re.fullmatch(r"[SX9AV]+", expanded) else None
    return digits, scale, expanded.startswith("S") if numeric else None, length


def _data_items(normalized, relative_path, source_hash, source_map, compilation_statuses=None):
    data_end = re.search(r"\bPROCEDURE\s+DIVISION\b", cobol_code_only(normalized), re.I)
    lines = normalized[:data_end.start() if data_end else len(normalized)].splitlines()
    declarations = []
    for number, line in enumerate(lines, 1):
        match = re.match(r"\s*(0[1-9]|[1-4][0-9]|66|77|78|88)\s+([A-Z_$#@][A-Z0-9_$#@-]*)\b", line, re.I)
        if match:
            declarations.append([number, number, int(match[1]), match[2].upper(), line.strip()])
        elif declarations and line.strip() and not re.search(r"\b(?:DIVISION|SECTION)\s*\.", line, re.I):
            declarations[-1][1] = number
            declarations[-1][4] += " " + line.strip()
    result, stack = [], []
    for first, last, level, name, declaration in declarations:
        if level in {1, 77, 78}:
            stack = []
        elif level != 88:
            while stack and stack[-1].level >= level:
                stack.pop()
        parent = stack[-1] if stack else None
        ancestors = (parent.name, *parent.ancestors) if parent else ()
        refs = _source_refs(relative_path, source_hash, first, last, source_map=source_map)
        picture = re.search(r"\bPIC(?:TURE)?\s+(?:IS\s+)?([A-Z0-9,+*/().-]+)", declaration, re.I)
        picture = picture[1].rstrip(".").upper() if picture else None
        usage = re.search(r"\b(?:USAGE\s+(?:IS\s+)?)?(COMP(?:-[1-5])?|COMPUTATIONAL(?:-[1-5])?|BINARY|PACKED-DECIMAL|DISPLAY|INDEX|POINTER)\b", declaration, re.I)
        occurs = re.search(r"\bOCCURS\s+(\d+)(?:\s+TO\s+(\d+))?", declaration, re.I)
        depending = re.search(r"\bDEPENDING\s+(?:ON\s+)?([A-Z0-9_$#@-]+)", declaration, re.I)
        redefines = re.search(r"\bREDEFINES\s+([A-Z0-9_$#@-]+)", declaration, re.I)
        value = re.search(r"\bVALUE\s+(?:IS\s+)?(.+?)\.?$", declaration, re.I)
        digits, scale, signed, _ = _picture_info(picture)
        item = DataItem("item_" + digest([relative_path, [asdict(ref) for ref in refs], first, name])[:24],
            name, level, ancestors, parent.item_id if parent else None, declaration, refs,
            picture, usage[1].upper() if usage else "DISPLAY", scale, digits, signed,
            (int(occurs[1]), int(occurs[2] or occurs[1])) if occurs else None,
            depending[1].upper() if depending else None,
            redefines[1].upper() if redefines else None, value[1] if value else None,
            diagnostics=("condition_name_not_separate_storage",) if level == 88 else
                        ("renames_layout_unsupported",) if level == 66 else ())
        if compilation_statuses is not None and any(compilation_statuses.get(number) is None for number in range(first, last+1)):
            item = replace(item, compilation_status="candidate", diagnostics=(*item.diagnostics, "compile_condition_unknown"))
        result.append(item)
        if level not in {66, 78, 88}:
            stack.append(item)
    return tuple(result)


def _regions(items):
    regions, unknown_overlap = {}, set()
    for item in items:
        parent = regions.get(item.parent_id)
        root = parent.root_id if parent else item.item_id
        aliases = [other for other in items if other.name == item.redefines and other.parent_id == item.parent_id]
        if len(aliases) == 1 and aliases[0].item_id in regions:
            root = regions[aliases[0].item_id].root_id
        elif item.redefines or item.level == 66 or item.compilation_status == "candidate":
            unknown_overlap.add(item.item_id)
        if item.compilation_status == "candidate":
            unknown_overlap.add(item.item_id)
        digits, _, _, length = _picture_info(item.picture)
        if item.usage in {"COMP-3", "PACKED-DECIMAL", "COMPUTATIONAL-3"} and digits is not None:
            length = (digits + 2) // 2
        elif item.usage != "DISPLAY":
            length = None
        elif item.picture:
            # Source encoding does not establish the runtime character layout.
            length = None
        if item.occurs:
            length = length * item.occurs[1] if length is not None and not item.depending_on else None
        regions[item.item_id] = StorageRegion("region_" + item.item_id, item.item_id, root,
            parent.region_id if parent else None, array=bool(item.occurs) or bool(parent and parent.array), length_bytes=length,
            layout_status="unresolved" if item.item_id in unknown_overlap else "conservative",
            diagnostics=("compile_condition_unknown_layout",) if item.compilation_status == "candidate" else
                        ("redefines_target_unresolved_broad_overlap",) if item.item_id in unknown_overlap else
                        ("array_element_requires_index",) if item.occurs or parent and parent.array else
                        ("redefines_overlap_conservative",) if item.redefines else
                        ("usage_layout_requires_dialect",) if length is None and item.picture else ())
    return tuple(replace(region, overlaps=tuple(other.region_id for other in regions.values()
        if other.region_id != region.region_id and (other.root_id == region.root_id or
            other.item_id in unknown_overlap or region.item_id in unknown_overlap))) for region in regions.values())


class BuiltinParserAdapter:
    name = "builtin"
    version = "builtin-ir-adapter-v2"

    def parse(self, source, *, relative_path, context=None, source_map=None, source_sha256=None):
        context = resolve_compilation_context(context)
        source_hash = source_sha256 or hashlib.sha256(source.encode(context.encoding if context.encoding != "auto" else "utf-8")).hexdigest()
        normalized, compilation_statuses, compilation_guards, directives, compilation_diagnostics = _normalized_source(source, context)
        items = _data_items(normalized, relative_path, source_hash, source_map, compilation_statuses)
        identities = [{"name": name, "compilation_status": "active" if compilation_statuses.get(
            normalized.count("\n", 0, start)+1) is True else "candidate"}
            for name, start, _ in program_identifiers(normalized)]
        hidden = cobol_code_only(normalized)
        procedure = re.search(r"\bPROCEDURE\s+DIVISION\b[^.]*\.", hidden, re.I)
        first = procedure.end() if procedure else 0
        fixed = context.source_format == "fixed" or context.source_format == "auto" and any(
            re.match(r"(?:\d{6}| {6})[-*/Dd]", line) or re.match(r"\d{6} ", line)
            for line in source.splitlines())
        base = source if fixed else normalized
        if fixed:
            base = "".join("".join(char if char in "\r\n" else " " for char in line)
                if compilation_statuses.get(number) is False or number in directives else line
                for number, line in enumerate(base.splitlines(keepends=True), 1))
        body = "".join(char if char in "\r\n" else " " for char in base[:first]) + base[first:]
        # Mask headers while retaining every physical line and source column.
        normalized_body = "".join(char if char in "\r\n" else " " for char in normalized[:first]) + normalized[first:]
        chars, block_ends = list(body), []
        for match in re.finditer(r"(?m)^[ \t]*([A-Z0-9_$#@-]+)(?:[ \t]+SECTION)?[ \t]*\.[ \t]*$", normalized_body):
            if match[1].upper() not in COBOL_RESERVED:
                chars[match.start():match.end()] = " " * len(match[0])
                block_ends.append(match.start())
        body = "".join(chars)
        parsed_results, offset = [], 0
        for end in (*block_ends, len(body)):
            if end <= offset:
                continue
            parsed_results.append(parse_statements(body[offset:end],
                start_line=body.count("\n", 0, offset) + 1, source_format="fixed" if fixed else "free"))
            offset = end
        statements = []
        for index, statement in enumerate(statement for parsed in parsed_results for statement in parsed.statements):
            refs = _source_refs(relative_path, source_hash, statement.span.start_line, statement.span.end_line,
                statement.span.start_column, statement.span.end_column, source_map)
            reads = tuple(bind_reference(name, items, refs) for name in statement.reads)
            writes = tuple(bind_reference(name, items, refs) for name in statement.writes)
            assignment = assignment_facts(statement.text)
            expressions = tuple((target, expression_tree(expression)) for target, expression in
                (assignment.expressions if assignment else getattr(statement, "expressions", ()) or
                 tuple((name, statement.expression) for name in statement.writes if statement.expression)))
            diagnostics = assignment.diagnostics if assignment else ()
            if any(ref.subscripts or ref.reference_modification for ref in reads + writes):
                diagnostics += ("array_or_reference_modification_region_not_resolved",)
            candidate = any(compilation_statuses.get(number) is None for number in
                            range(statement.span.start_line, statement.span.end_line+1))
            compile_scope = tuple({"kind": "COMPILATION", "condition": guard["expression"],
                "outcome": guard["outcome"], "value": guard["value"], "source_line": guard["source_line"]}
                for guard in compilation_guards.get(statement.span.start_line, ()))
            if candidate:
                diagnostics += ("compile_condition_unknown",)
            bound = all(ref.binding_status == "bound" for ref in reads + writes)
            shape = digest([statement.kind, [ref.identity for ref in reads],
                            [ref.identity for ref in writes], [asdict(tree) for _, tree in expressions]])
            occurrence = digest([[asdict(ref) for ref in refs], statement.span.start_offset,
                                 statement.span.end_offset, index, shape])
            statements.append(ParsedStatement("stmt_" + occurrence[:24], "shape_" + shape[:24],
                statement.kind, statement.text, refs, reads, writes, expressions,
                (*tuple(asdict(guard) for guard in statement.guards), *compile_scope),
                binding_status="candidate" if candidate else "bound" if bound else "unresolved",
                rounded=statement.rounded, effects_unknown=statement.effects_unknown,
                compilation_status="candidate" if candidate else "active", diagnostics=diagnostics))
        diagnostics = tuple({"reason": boundary.reason, "source_refs": [asdict(ref) for ref in
            _source_refs(relative_path, source_hash, boundary.span.start_line, boundary.span.end_line,
                         source_map=source_map)]} for parsed in parsed_results for boundary in parsed.boundaries) + compilation_diagnostics
        return CompilationIR(context.to_dict(), {"name": self.name, "version": self.version,
                             "statement_parser_version": PARSER_VERSION, "program_identifiers": identities}, items,
                             _regions(items), tuple(statements), diagnostics, all(parsed.complete for parsed in parsed_results)
                             and not any(diagnostic["reason"] != "compile_condition_unknown" for diagnostic in compilation_diagnostics))


@dataclass(frozen=True)
class ExternalParserSpec:
    name: str
    version: str
    upstream: str
    release: str


PROLEAP = ExternalParserSpec("proleap", "2.4.0", "https://github.com/uwol/proleap-cobol-parser",
                            "v2.4.0")
CHE4Z = ExternalParserSpec("che4z", "2.5.1", "https://github.com/eclipse-che4z/che-che4z-lsp-for-cobol",
                          "2.5.1")


class IsolatedParserAdapter:
    """Opt-in local bridge; bounded process execution, no shell or auto-install."""

    def __init__(self, spec, *, command=(), artifact_sha256=None, artifact_hashes=None, timeout_seconds=30,
                 max_result_bytes=8_388_608):
        self.spec, self.command = spec, tuple(command)
        self.artifact_sha256 = artifact_sha256
        self.artifact_hashes = {str(Path(path).resolve()): value for path, value in (artifact_hashes or {}).items()}
        self.timeout_seconds, self.max_result_bytes = timeout_seconds, max_result_bytes

    def parse(self, source, *, relative_path, context=None, source_map=None, source_sha256=None):
        context = resolve_compilation_context(context)
        metadata = asdict(self.spec)
        if not self.command:
            return CompilationIR(context.to_dict(), metadata, diagnostics=({"reason": "parser_unavailable"},))
        try:
            executable = Path(self.command[0]).resolve(strict=True)
            artifacts = {str(Path(arg).resolve()): Path(arg).resolve() for arg in self.command if Path(arg).is_file()}
            artifacts.update({path: Path(path) for path in self.artifact_hashes})
            expected = {str(executable): self.artifact_sha256} | self.artifact_hashes
            if any(not expected.get(path) or hashlib.sha256(artifact.read_bytes()).hexdigest() != expected[path]
                   for path, artifact in artifacts.items()):
                raise ValueError("parser_artifact_hash_mismatch")
        except (OSError, ValueError) as error:
            return CompilationIR(context.to_dict(), metadata, diagnostics=({"reason": str(error)},))
        source_hash = source_sha256 or hashlib.sha256(source.encode(
            context.encoding if context.encoding != "auto" else "utf-8")).hexdigest()
        request = {"schema_version": "parser-bridge/v1", "parser": metadata,
            "source": source, "relative_path": relative_path, "source_sha256": source_hash,
            "source_map": source_map, "compilation_context": context.to_dict()}
        try:
            with tempfile.TemporaryDirectory(prefix="parser-adapter-") as folder:
                output = Path(folder) / "result.json"
                with output.open("wb") as stream:
                    process = subprocess.run(self.command, input=json.dumps(request).encode(), stdout=stream,
                        stderr=subprocess.DEVNULL, cwd=folder, timeout=self.timeout_seconds,
                        env={"PATH": str(executable.parent), "HOME": folder}, check=False)
                if process.returncode or output.stat().st_size > self.max_result_bytes:
                    raise ValueError("parser_process_failed" if process.returncode else "parser_result_budget")
                result = json.loads(output.read_text(encoding="utf-8"))
            if result.get("parser") != metadata or result.get("schema_version") != "parser-bridge/v1":
                raise ValueError("parser_bridge_version_mismatch")
            candidate = compilation_ir_from_dict(result.get("ir", {}))
            if digest(candidate.context) != digest(context.to_dict()) or candidate.adapter.get("name") != self.spec.name or (
                    candidate.adapter.get("version") != self.spec.version):
                raise ValueError("parser_ir_identity_mismatch")
            allowed = {(relative_path, source_hash)} | {(row["origin"]["relative_path"], row["origin"]["source_hash"])
                                                     for row in source_map or ()}
            for row in (*candidate.data_items, *candidate.statements):
                if not row.source_refs or any((ref.relative_path, ref.source_sha256) not in allowed
                        or ref.start_line < 1 or ref.end_line < ref.start_line for ref in row.source_refs):
                    raise ValueError("parser_source_mapping_mismatch")
            return replace(candidate, diagnostics=(*candidate.diagnostics, *result.get("diagnostics", [])))
        except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as error:
            return CompilationIR(context.to_dict(), metadata,
                diagnostics=({"reason": "parser_timeout" if isinstance(error, subprocess.TimeoutExpired) else str(error)},))


def compare_normalized_ir(expected, candidate):
    left, right = expected.to_dict(), candidate.to_dict()
    indexed = {tuple((ref["relative_path"], ref["start_line"], ref["start_column"], ref.get("copy_instance"))
                     for ref in row["source_refs"]): row for row in right["statements"]}
    counts = {"correct": 0, "unsupported": 0, "incorrect": 0}
    for row in left["statements"]:
        key = tuple((ref["relative_path"], ref["start_line"], ref["start_column"], ref.get("copy_instance"))
                    for ref in row["source_refs"])
        other = indexed.get(key)
        if other is None or row["parsed_status"] != "parsed" or other["parsed_status"] != "parsed" or (
                row.get("compilation_status") != "active" or other.get("compilation_status") != "active"):
            counts["unsupported"] += 1
            continue
        def normalized(value):
            result = {key: value[key] for key in ("statement_type", "expressions", "rounded", "effects_unknown")}
            for role in ("reads", "writes"):
                result[role] = [{key: ref[key] for key in ("name", "qualifiers", "subscripts", "reference_modification", "binding_status")}
                                for ref in value[role]]
            result["scope"] = [{key: guard.get(key) for key in ("kind", "condition", "outcome", "selector", "alternatives", "branch_index", "is_other")}
                               for guard in value["scope"]]
            return result
        counts["correct" if normalized(row) == normalized(other) else "incorrect"] += 1
    return {"counts": counts, "expected_diagnostics": left["diagnostics"],
            "candidate_diagnostics": right["diagnostics"], "candidate_extra_occurrences": max(0, len(indexed) - len(left["statements"]))}
