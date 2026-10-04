"""Bind explicit local interface rules to a bounded subset of source syntax.

The result describes the documented intent of a source call, never its execution
or return value. A function value is retained only from the immediately preceding
supported MOVE in the same straight-line block. This deliberately avoids alias
and control-flow guesses that would require a complete language implementation.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Iterable

from call_bindings import parse_call


_NAME = r"[A-Z][A-Z0-9-]{0,63}"
_PROCEDURE_NAME = rf"(?:{_NAME}|[0-9]{{1,8}}-[A-Z][A-Z0-9-]{{0,54}})"
_LITERAL = r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\""
_TOKEN = re.compile(rf"{_LITERAL}|[A-Za-z][A-Za-z0-9-]*|[+-]?\d+(?:\.\d+)?|[^\s]", re.S)
_MOVE = re.compile(rf"MOVE\s+({_LITERAL}|[+-]?\d+|{_NAME})\s+TO\s+({_NAME})\s*\.?", re.I | re.S)
_PROGRAM = re.compile(rf"PROGRAM-ID\s*\.\s*(?:'({_NAME})'|\"({_NAME})\"|({_NAME}))\s*\.?", re.I)
_SECTION = re.compile(rf"({_PROCEDURE_NAME})\s+SECTION\s*\.", re.I)
_PARAGRAPH = re.compile(rf"({_PROCEDURE_NAME})\s*\.", re.I)
_DECLARATION = re.compile(rf"(?:0?[1-9]|[1-4][0-9]|66|77|88)\s+({_NAME})\b", re.I)
_STARTS = frozenset("""
ACCEPT ADD ALTER CALL CANCEL CLOSE COMPUTE CONTINUE COPY DELETE DISPLAY DIVIDE
ELSE END-ADD END-CALL END-COMPUTE END-DELETE END-DIVIDE END-EVALUATE END-IF
END-MULTIPLY END-PERFORM END-READ END-RETURN END-REWRITE END-SEARCH END-START
END-STRING END-SUBTRACT END-UNSTRING END-WRITE ENTRY EVALUATE EXEC EXIT GOBACK
GO IF INITIALIZE INSPECT MERGE MOVE MULTIPLY NEXT OPEN PERFORM READ RELEASE
REPLACE RETURN REWRITE SEARCH SET SORT START STOP STRING SUBTRACT UNSTRING WHEN
WRITE
""".split())
_CONTROL = frozenset({"IF", "EVALUATE", "PERFORM", "SEARCH", "READ", "RETURN"})
_MAX_STATEMENT_CHARS = 16_384
_MAX_STATEMENT_LINES = 64
_MAX_FACTS = 2048


def _id(*parts: object) -> str:
    return "framework_fact_" + hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()[:24]


def _stem(template: str, value: str) -> str | None:
    """Match one explicit placeholder, not a general wildcard or regex."""
    placeholders = list(re.finditer(r"X{2,8}", template.upper()))
    if len(placeholders) != 1:
        return None
    match = placeholders[0]
    pattern = re.escape(template[:match.start()]) + rf"([A-Z0-9]{{{len(match.group())}}})" + re.escape(template[match.end():])
    result = re.fullmatch(pattern, value, re.I)
    return result.group(1).upper() if result else None


def _instantiate(template: str, stem: str) -> str | None:
    if len(list(re.finditer(r"X{2,8}", template.upper()))) != 1:
        return None
    match = re.search(r"X{2,8}", template.upper())
    if len(match.group()) != len(stem):
        return None
    return (template[:match.start()] + stem + template[match.end():]).upper()


def _literal(text: str) -> str:
    if text.startswith(("'", '"')):
        return text[1:-1].replace(text[0] * 2, text[0])
    return text


def bind_framework_source(
    lines: Iterable[tuple[int, str | None]], knowledge: dict, *,
    relative_path: str, source_sha256: str, source_format: str = "auto",
    initial_program: str | None = None, focus_ranges: list[tuple[int, int] | dict] | None = None,
) -> list[dict]:
    """Attach documented meanings only where source and rule evidence agree.

    COPY content is never expanded here. A COPY or REPLACE is a barrier, but a
    subsequent explicit MOVE/CALL remains independently inspectable. Numeric and
    string literal values retain their spelling; their representation is not
    coerced to fit a rule. Unsupported syntax can yield an uncovered interface
    fact, but can never remove a dependency boundary.

    ``initial_program`` is reserved for callers that have independently checked
    a partial source range belongs to one program's procedure division. It does
    not seed assignments or infer the contents of preceding source lines.
    """
    # Local import avoids a cycle with the business index's post-build pass and
    # shares its quote-aware source/comment handling.
    from business_index import _clean

    if source_format not in {"auto", "fixed", "free"}:
        raise ValueError("source_format_not_supported")
    rules = {rule["rule_id"]: rule for rule in knowledge.get("rules", [])}
    interfaces = knowledge.get("interfaces", [])
    references = {ref["reference_id"] for ref in knowledge.get("references", [])}
    section_rules: dict[str, list[dict]] = {}
    for rule in rules.values():
        if rule.get("kind") == "section":
            section_rules.setdefault(rule.get("symbol", "").upper(), []).append(rule)
    facts: list[dict] = []
    program: str | None = initial_program
    division: str | None = "PROCEDURE" if initial_program else None
    active_format = source_format
    pending: list[tuple[str, int]] = []
    pending_program = False
    controls: list[str] = []
    embedded = False
    replacement_active = False
    last_move: dict | None = None
    declarations: Counter = Counter()
    program_occurrences: Counter = Counter()
    previous_line: int | None = None
    selected_ranges = None if focus_ranges is None else [
        (span.get("start_line"), span.get("end_line")) if isinstance(span, dict) else tuple(span)
        for span in focus_ranges]

    def focused(start: int, end: int) -> bool:
        return selected_ranges is None or any(isinstance(first, int) and isinstance(last, int)
                                              and start <= last and end >= first for first, last in selected_ranges)

    def base(start: int, end: int, target: str, kind: str) -> dict:
        return {
            "fact_id": _id(relative_path, source_sha256, program, start, end, target, kind),
            "kind": kind, "relative_path": relative_path, "source_sha256": source_sha256,
            "program_name": program, "start_line": start, "end_line": end,
            "target_name": target, "relation_type": "CALLS" if kind == "framework_operation" else "SECTION",
            "operation": None, "function_field": None, "argument": None,
            "source_ranges": [{"start_line": start, "end_line": end, "role": "callsite" if kind == "framework_operation" else "section"}],
            "reference_ids": [], "interpretation_basis": "documented_framework_rule",
            "dependency_covered": False, "runtime_verified": False,
        }

    def ref_ids(*items: dict) -> list[str]:
        return sorted({value for item in items for value in item.get("reference_ids", []) if value in references})

    def emit_call(text: str, start: int, end: int) -> None:
        nonlocal last_move
        try:
            call = parse_call(text)
        except ValueError:
            return
        if call.dynamic or not program:
            return
        matches = [(item, _stem(item.get("target_template", ""), call.target)) for item in interfaces]
        matches = [(item, stem) for item, stem in matches if stem is not None]
        if not matches or len(facts) >= _MAX_FACTS or not focused(start, end):
            return
        fact = base(start, end, call.target, "framework_operation")
        fact["reference_ids"] = ref_ids(*(item for item, _ in matches))
        fact["return_status_binding"] = "not_bound"
        fact["reason"] = "interface_ambiguous"
        if len(matches) != 1:
            facts.append(fact)
            return
        interface, stem = matches[0]
        function = _instantiate(interface.get("function_template", ""), stem)
        argument = _instantiate(interface.get("argument_template", ""), stem)
        fact["interface_id"] = interface.get("interface_id")
        fact["function_field"] = function
        fact["argument"] = argument
        if replacement_active:
            fact["reason"] = "replacement_scope_not_resolved"
        elif not function or not argument:
            fact["reason"] = "interface_template_not_supported"
        elif len(call.parameters) != 1 or call.parameters[0].name != argument:
            fact["reason"] = "interface_argument_mismatch"
        elif call.parameters[0].mode != "REFERENCE":
            fact["reason"] = "interface_passing_mode_not_supported"
        elif declarations[(program, function)] > 1 or declarations[(program, argument)] > 1:
            fact["reason"] = "source_field_ambiguous"
        elif not last_move or last_move["field"] != function:
            fact["reason"] = "function_value_not_bound"
        else:
            operation_rules = [rules[value] for value in interface.get("operation_rule_ids", [])
                               if value in rules and rules[value].get("kind") == "operation"
                               and rules[value].get("symbol") == last_move["value"]]
            fact["source_ranges"].append({"start_line": last_move["start_line"],
                                         "end_line": last_move["end_line"], "role": "function_assignment"})
            fact["observed_function_value"] = last_move["value"]
            fact["reason"] = "function_operation_not_documented" if not operation_rules else "function_operation_ambiguous"
            if len(operation_rules) == 1:
                rule = operation_rules[0]
                fact["operation"] = {"value": last_move["value"], "meaning": rule.get("meaning", ""), "caveat": rule.get("caveat", "")}
                fact["rule_id"] = rule["rule_id"]
                fact["reference_ids"] = sorted(set(fact["reference_ids"]) | set(ref_ids(rule)))
                if ref_ids(interface) and ref_ids(rule) and rule.get("meaning"):
                    fact["dependency_covered"] = True
                    fact["reason"] = "documented_operation_bound"
                else:
                    fact["reason"] = "rule_evidence_incomplete"
            if not last_move["literal"]:
                fact["observed_function_operand"] = fact.pop("observed_function_value")
                fact["dependency_covered"] = False
                fact["reason"] = "function_operand_not_resolved"
                if fact["operation"]:
                    fact["documented_operation_candidate"] = fact.pop("operation")
                    fact["operation"] = None
        facts.append(fact)

    def flush(*, discard: bool = False) -> None:
        nonlocal pending, last_move, replacement_active
        if not pending:
            return
        items, pending = pending, []
        text = " ".join(value for value, _ in items)
        first = items[0][0].upper()
        if discard:
            last_move = None
            return
        if first == "MOVE":
            match = _MOVE.fullmatch(text)
            last_move = {"field": match.group(2).upper(), "value": _literal(match.group(1)),
                         "literal": not bool(re.fullmatch(_NAME, match.group(1), re.I)),
                         "start_line": items[0][1], "end_line": items[-1][1]} if match else None
        elif first == "CALL":
            emit_call(text, items[0][1], items[-1][1])
            last_move = None
        else:
            last_move = None
            if first in _CONTROL:
                controls.append(first)
            elif first.startswith("END-") and first[4:] in controls:
                controls.remove(first[4:])
            elif first == "REPLACE":
                replacement_active = not bool(re.fullmatch(r"REPLACE\s+OFF\s*\.?", text, re.I))
        if items[-1][0] == "." and controls:
            last_move = None
            controls.clear()

    for number, raw in lines:
        if previous_line is not None and number != previous_line + 1:
            flush(discard=True)
            last_move = None
        previous_line = number
        if raw is None:
            flush(discard=True)
            last_move = None
            continue
        expanded = raw.expandtabs(8)
        code, next_format, continuation = _clean(raw, active_format)
        if source_format == "auto":
            active_format = next_format
        if not code:
            continue
        upper = code.upper()
        fixed_debug = (active_format == "fixed" or (active_format == "auto" and len(expanded) >= 7 and
                       (expanded[:6].isdigit() or not expanded[:6].strip()))) and len(expanded) >= 7 and expanded[6] in "Dd"
        if fixed_debug:
            flush(discard=True)
            last_move = None
            continue
        if pending_program:
            upper = "PROGRAM-ID. " + upper
            code = "PROGRAM-ID. " + code
            pending_program = False
        if re.fullmatch(r"PROGRAM-ID\s*\.", upper):
            flush(discard=True)
            last_move = None
            pending_program = True
            continue
        program_match = _PROGRAM.fullmatch(code)
        if program_match:
            flush(discard=True)
            program = next(value for value in program_match.groups() if value).upper()
            program_occurrences[program] += 1
            division = None
            last_move, embedded = None, False
            controls.clear()
            continue
        division_match = re.match(r"^(IDENTIFICATION|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION\b", upper)
        if division_match:
            flush(discard=True)
            division = division_match.group(1)
            if division == "IDENTIFICATION":
                program = None
            last_move, embedded = None, False
            controls.clear()
            continue
        if re.match(r"^END\s+PROGRAM\b", upper):
            flush()
            program, division, last_move = None, None, None
            embedded = False
            controls.clear()
            continue
        if division == "DATA":
            if re.match(r"REPLACE\b", upper):
                replacement_active = not bool(re.fullmatch(r"REPLACE\s+OFF\s*\.?", upper))
            declaration = _DECLARATION.match(upper)
            if declaration and program:
                declarations[(program, declaration.group(1))] += 1
            continue
        if division != "PROCEDURE" or not program:
            continue
        section_match = _SECTION.fullmatch(upper)
        paragraph_match = _PARAGRAPH.fullmatch(upper)
        argument_tail = bool(pending and (
            (pending[0][0].upper() == "CALL" and any(token.upper() == "USING" for token, _ in pending))
            or (pending[0][0].upper() == "MOVE" and pending[-1][0].upper() == "TO")))
        if section_match or (paragraph_match and paragraph_match.group(1) not in _STARTS and not argument_tail):
            flush()
            last_move = None
            controls.clear()
            if section_match and not embedded and len(facts) < _MAX_FACTS and focused(number, number):
                name = section_match.group(1)
                matched = section_rules.get(name, [])
                if len(matched) == 1 and ref_ids(matched[0]):
                    rule = matched[0]
                    fact = base(number, number, name, "framework_section")
                    fact["operation"] = {"value": name, "meaning": rule.get("meaning", ""), "caveat": rule.get("caveat", "")}
                    fact["reference_ids"] = ref_ids(rule)
                    fact["rule_id"] = rule["rule_id"]
                    fact["reason"] = "documented_section_observed"
                    facts.append(fact)
            continue
        tokens = list(_TOKEN.finditer(code))
        # A quote extending across physical lines needs continuation semantics
        # this subset does not establish. Never turn its contents into code.
        if any(token.group() in {"'", '"'} for token in tokens):
            flush(discard=True)
            last_move = None
            embedded = True
            continue
        for token in tokens:
            value = token.group()
            keyword = value.upper()
            if embedded:
                last_move = None
                if keyword == "END-EXEC":
                    embedded = False
                continue
            if keyword == "EXEC":
                flush()
                last_move = None
                embedded = True
                continue
            if keyword in _STARTS:
                if keyword == "END-CALL" and pending and pending[0][0].upper() == "CALL":
                    pending.append((value, number))
                    continue
                flush()
            pending.append((value, number))
            if sum(len(value) for value, _ in pending) > _MAX_STATEMENT_CHARS or number - pending[0][1] >= _MAX_STATEMENT_LINES:
                flush(discard=True)
                continue
            if value == ".":
                flush()
    flush()
    # Repeated names in one physical file do not establish a unique program
    # scope. Keep the observation but withdraw its dependency coverage.
    for fact in facts:
        if program_occurrences[fact["program_name"]] > 1:
            fact["dependency_covered"] = False
            fact["reason"] = "program_scope_ambiguous"
    return facts
