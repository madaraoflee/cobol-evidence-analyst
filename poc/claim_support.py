"""Independently check a small, explicit source-statement claim language.

The caller supplies evidence whose snapshot integrity was already checked.  This
module rechecks that contract, parses the complete span, and renders its own
claim.  It does not evaluate arithmetic, infer execution paths, or assess prose.
"""

from __future__ import annotations

import re
import hashlib
from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from typing import Any


CHECKER_VERSION = "compute-statement-v0.2"
MAX_SOURCE_CHARS = 16_000
MAX_EXPRESSION_TOKENS = 40
MAX_EXPRESSION_CHARS = 240
MAX_CLAIM_CHARS = 320

_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*", re.ASCII)
_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?", re.ASCII)
_TOKEN = re.compile(
    r"[0-9]+\.[0-9]+|[A-Z0-9][A-Z0-9-]*|\*\*|[+*/()=-]",
    re.ASCII,
)
_RESERVED = frozenset(
    "ADD BY COMPUTE CONTINUE CORRESPONDING DISPLAY DIVIDE ELSE END-COMPUTE "
    "END-EVALUATE END-EXEC END-IF ERROR EVALUATE EXEC FALSE FROM FUNCTION "
    "GIVING HIGH-VALUES IF IN LOW-VALUES MOVE MULTIPLY NOT OF ON ROUNDED "
    "RUN SIZE SPACE SPACES SQL STOP SUBTRACT THEN TO TRUE WHEN ZERO ZEROES "
    "ZEROS".split()
)
_ASSERTION_KEYS = frozenset({"predicate", "target", "expression", "rounded"})


def _is_identifier(value: str) -> bool:
    return (
        len(value) <= 64
        and _IDENTIFIER.fullmatch(value) is not None
        and value not in _RESERVED
    )


def _valid_expression(tokens: list[str]) -> bool:
    """Recognize bounded arithmetic without evaluating or simplifying it."""
    if not 1 <= len(tokens) <= MAX_EXPRESSION_TOKENS:
        return False
    if any(not isinstance(token, str) or not token for token in tokens):
        return False
    if len(" ".join(tokens)) > MAX_EXPRESSION_CHARS:
        return False

    position = 0

    def primary() -> bool:
        nonlocal position
        if position >= len(tokens):
            return False
        token = tokens[position]
        if token in {"+", "-"}:
            position += 1
            # One unary sign is enough for this deliberately small grammar.
            if position >= len(tokens) or tokens[position] in {"+", "-"}:
                return False
            return primary()
        if token == "(":
            position += 1
            if not expression():
                return False
            if position >= len(tokens) or tokens[position] != ")":
                return False
            position += 1
            return True
        if _is_identifier(token) or _NUMBER.fullmatch(token):
            position += 1
            return True
        return False

    def product() -> bool:
        nonlocal position
        if not primary():
            return False
        while position < len(tokens) and tokens[position] in {"*", "/", "**"}:
            position += 1
            if not primary():
                return False
        return True

    def expression() -> bool:
        nonlocal position
        if not product():
            return False
        while position < len(tokens) and tokens[position] in {"+", "-"}:
            position += 1
            if not product():
                return False
        return True

    return expression() and position == len(tokens)


def _render_claim(assertion: Mapping[str, Any]) -> str:
    rounded = "含 ROUNDED 标记" if assertion["rounded"] else "不含 ROUNDED 标记"
    return (
        f"所引源码中的单条语句为 COMPUTE {assertion['target']} = "
        f"{' '.join(assertion['expression'])}；{rounded}。"
        "此结论仅核对语句中的公式和 ROUNDED 标记。"
    )


def normalize_assertion(value: object) -> dict[str, Any]:
    """Validate the exact model-facing schema; do not repair its meaning."""
    if not isinstance(value, Mapping) or set(value) != _ASSERTION_KEYS:
        raise ValueError("Invalid compute assertion schema.")
    if value["predicate"] != "compute_statement":
        raise ValueError("Unsupported assertion predicate.")
    target = value["target"]
    expression = value["expression"]
    if not isinstance(target, str) or not _is_identifier(target):
        raise ValueError("Invalid compute target.")
    if not isinstance(expression, list) or not _valid_expression(expression):
        raise ValueError("Invalid compute expression.")
    if not isinstance(value["rounded"], bool):
        raise ValueError("Invalid rounded flag.")
    normalized = {
        "predicate": "compute_statement",
        "target": target,
        "expression": list(expression),
        "rounded": value["rounded"],
    }
    if len(_render_claim(normalized)) > MAX_CLAIM_CHARS:
        raise ValueError("Compute claim exceeds the rendering limit.")
    return normalized


def _verified_source(
    evidence_ids: object, verified_evidence: object
) -> str | None:
    if (
        not isinstance(evidence_ids, list)
        or len(evidence_ids) != 1
        or not isinstance(evidence_ids[0], str)
        or not evidence_ids[0]
        or not isinstance(verified_evidence, Mapping)
    ):
        return None
    evidence_id = evidence_ids[0]
    span = verified_evidence.get(evidence_id)
    if not isinstance(span, Mapping):
        return None
    source_hash = span.get("source_sha256")
    start, end = span.get("start_line"), span.get("end_line")
    source = span.get("source_text")
    if (
        span.get("evidence_id") != evidence_id
        or span.get("integrity") != "VALID"
        or span.get("span_truncated") is not False
        or span.get("content_type") != "UNTRUSTED_SOURCE_TEXT"
        or not isinstance(source_hash, str)
        or re.fullmatch(r"[0-9a-fA-F]{64}", source_hash, re.ASCII) is None
        or type(start) is not int
        or type(end) is not int
        or start <= 0
        or end < start
        or not isinstance(source, str)
        or not source
        or len(source) > MAX_SOURCE_CHARS
        or len(source.splitlines()) != end - start + 1
    ):
        return None
    return source


def _parse_source(source: str) -> dict[str, Any] | None:
    # Only ordinary ASCII source and ordinary line endings enter the parser.
    # In particular, unicode spaces/control characters cannot alter tokenization.
    if not source.isascii() or any(
        ord(character) < 32 and character not in "\n\r" for character in source
    ):
        return None
    if "\r" in source.replace("\r\n", ""):
        return None
    lines = source.splitlines()
    # Without explicit format metadata, six leading blanks could also be a
    # fixed sequence area.  Treat that ambiguity conservatively as fixed form.
    fixed = [
        re.match(r"(?:[0-9]{6}| {6})", line, re.ASCII) is not None
        for line in lines
    ]
    if any(fixed):
        if not all(fixed):
            return None
        if any(len(line) < 7 or line[6] != " " for line in lines):
            return None
        # Ignore no meaningful trailing columns: source past column 72 needs a
        # separate dialect-aware parser, including sequence-area conventions.
        if any(line[72:].strip() for line in lines):
            return None
        lines = [line[7:72] for line in lines]
    if any(not line.strip() for line in lines):
        return None
    statement = " ".join(line.strip() for line in lines).upper()
    match = re.fullmatch(
        r"COMPUTE +([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*)"
        r"( +ROUNDED)? *= *(.*?) *(?: +END-COMPUTE *\.?|\.)",
        statement,
        re.ASCII,
    )
    if not match:
        return None
    expression_text = match.group(3)
    tokens: list[str] = []
    position = 0
    while position < len(expression_text):
        if expression_text[position] == " ":
            position += 1
            continue
        # Consume whole COBOL-like words before validating the narrow grammar;
        # otherwise IN--AMOUNT or 1-RATE could become different arithmetic.
        token = _TOKEN.match(expression_text, position)
        if not token:
            return None
        if token.group() in {"+", "-", "*", "/"}:
            # Check the original separators before rendering spaced tokens;
            # decimal prefixes must not turn glued words into arithmetic.
            start = match.start(3) + token.start()
            end = match.start(3) + token.end()
            if statement[start - 1] != " " or statement[end] != " ":
                return None
        tokens.append(token.group())
        if len(tokens) > MAX_EXPRESSION_TOKENS:
            return None
        position = token.end()
    try:
        return normalize_assertion(
            {
                "predicate": "compute_statement",
                "target": match.group(1),
                "expression": tokens,
                "rounded": match.group(2) is not None,
            }
        )
    except ValueError:
        return None


def check_claim_support(
    assertion: object,
    evidence_ids: object,
    verified_evidence: object,
) -> dict[str, Any]:
    """Return a bounded verdict containing no model prose or failed source."""
    result: dict[str, Any] = {
        "support_status": "unsupported",
        "reason_code": "INVALID_ASSERTION",
        "checker_version": CHECKER_VERSION,
        "claim_text": None,
    }
    try:
        normalized = normalize_assertion(assertion)
    except ValueError:
        return result
    source = _verified_source(evidence_ids, verified_evidence)
    if source is None:
        result["reason_code"] = "EVIDENCE_NOT_VERIFIED"
        return result
    parsed = _parse_source(source)
    if parsed is None:
        result["reason_code"] = "SOURCE_FORM_NOT_SUPPORTED"
        return result
    if parsed != normalized:
        result["reason_code"] = "COMPUTE_ASSERTION_MISMATCH"
        return result
    result.update(
        support_status="supported",
        reason_code="COMPUTE_STATEMENT_MATCH",
        claim_text=_render_claim(normalized),
    )
    return result


STATEMENT_CHECKER_VERSION = "source-statement-support-v5"


def statement_tokens(text: str) -> tuple[str, ...] | None:
    """Normalize tokens with the statement front end, never evaluate them."""
    from statement_parser import _BoundaryError, _Source, _lex

    try:
        return tuple(token.value for token in _lex(_Source(text, 1), "free", 512))
    except (_BoundaryError, ValueError):
        return None


def supplied_statement_facts(page: Mapping[str, Any], *, source_format: str = "auto",
                             compilation_context=None, parsed_document=None,
                             include_declarations=False) -> list[dict[str, Any]]:
    """Project supported statement occurrences from an already captured excerpt.

    This uses the same structural/statement front ends as indexing. Evidence
    remains bound to the supplied excerpt; this function never reads live files.
    A supported occurrence is a syntax fact, not an execution or final-value
    proof. Unsupported units are deliberately absent from the fact list.
    """
    from structural_index import SourceDocument, normalize_cobol_lines, parse_document
    from statement_parser import parse_statements
    from statement_facts import assignment_facts, condition_syntax_supported

    source = page.get("source_text")
    first, last = page.get("start_line"), page.get("end_line")
    if (not isinstance(source, str) or not source or page.get("span_truncated")
            or type(first) is not int or type(last) is not int or first <= 0 or last < first
            or len(source.splitlines()) != last - first + 1
            or re.fullmatch(r"[a-fA-F0-9]{64}", str(page.get("source_sha256", ""))) is None):
        return []
    if parsed_document is not None:
        document = parsed_document.document
        if (document.relative_path != str(page.get("relative_path", "supplied.cbl"))
                or document.source_sha256 != str(page["source_sha256"])
                or document.raw_lines != tuple(source.splitlines())):
            return []
        parsed = parsed_document
    else:
        lines, hint = normalize_cobol_lines(source, source_format)
        document = SourceDocument(str(page.get("relative_path", "supplied.cbl")),
            str(page["source_sha256"]), "utf-8", False, hint, "COBOL", tuple(source.splitlines()), lines)
        parsed = parse_document(document, compilation_context=compilation_context)
    by_id = {unit.unit_id: unit for unit in parsed.code_units}
    controls, compilation = {}, {}
    for relation in parsed.relations:
        if relation.metadata.get("compilation_status") == "candidate":
            compilation[relation.from_entity_id] = {
                "compilation_status": "candidate",
                "compilation_guards": relation.metadata.get("compilation_guards", []),
                "compilation_diagnostics": relation.metadata.get("diagnostics", [])}
        if relation.relation_type != "CONTROL_DEPENDS_ON" or relation.status != "confirmed":
            continue
        condition_unit = by_id.get(relation.target_entity_id)
        if condition_unit is None:
            continue
        controls.setdefault(relation.from_entity_id, []).append({
            "kind": condition_unit.name, "condition": condition_unit.normalized_text,
            "outcome": relation.metadata.get("outcome"),
            **{key: relation.metadata[key] for key in ("selector", "alternatives", "prior_alternatives",
                "branch_index", "is_other", "evaluate_source", "prior_when_sources") if key in relation.metadata},
            "start_line": first + condition_unit.start_line - 1,
            "end_line": first + condition_unit.end_line - 1})
    facts = []
    for unit in parsed.code_units:
        if unit.parse_status not in {"complete", "candidate"}:
            continue
        if unit.unit_type != "Statement" and not (include_declarations and unit.unit_type == "DataItem"):
            continue
        body = unit.normalized_text
        location = {"start_line": first + unit.start_line - 1,
                    "end_line": first + unit.end_line - 1,
                    "program_name": unit.program_name,
                    "occurrence_id": unit.unit_id,
                    "statement_kind": unit.name,
                    "guards": controls.get(unit.unit_id, []),
                    "source_text": "\n".join(source.splitlines()[unit.start_line - 1:unit.end_line]),
                    **compilation.get(unit.unit_id, {})}
        if unit.unit_type == "DataItem":
            # An elementary VALUE clause proves only the declared initial
            # literal, never a runtime or final value after later writes.
            declaration = re.fullmatch(r"(?:01|77)\s+([A-Z][A-Z0-9-]*)\s+PIC(?:TURE)?\s+"
                r"[A-Z0-9()]+\s+VALUE(?:\s+IS)?\s+([+-]?\d+(?:\.\d+)?|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")\s*\.", body, re.I)
            if declaration:
                tokens = statement_tokens(declaration[2])
                if tokens:
                    facts.append({**location, "predicate": "declaration_value", "target": declaration[1].upper(),
                                  "expression": list(tokens)})
            continue
        if unit.name in {"COMPUTE", "MOVE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE"}:
            assignment = assignment_facts(body)
            exceptional_header = (assignment is not None and assignment.effects_unknown
                                  and "arithmetic_size_error_clause" in assignment.diagnostics)
            statement = parse_statements(body, start_line=location["start_line"], source_format="free")
            if not exceptional_header and (not statement.complete or len(statement.statements) != 1):
                continue
            node = statement.statements[0] if statement.statements else None
            if node is not None and node.effects_unknown and not exceptional_header:
                continue
            # Arithmetic verbs may have receiver-specific expressions.
            expressions = assignment.expressions if assignment else ()
            for target, expression in expressions:
                if assignment.kind != "MOVE":
                    from statement_parser import _BoundaryError, _Source, _lex, _valid_expression
                    try:
                        if not _valid_expression(_lex(_Source(expression, 1), "free", 512)):
                            continue
                    except _BoundaryError:
                        continue
                tokens = statement_tokens(expression)
                if tokens:
                    facts.append({**location, "predicate": "assignment_statement", "target": target,
                        "expression": list(tokens), "rounded": assignment.rounded,
                        "normal_result_condition": "no_size_error" if exceptional_header else None,
                        "exception_effects_unverified": exceptional_header})
        elif unit.name == "IF":
            condition = re.sub(r"^IF\s+", "", body, flags=re.I).removesuffix(" THEN").rstrip(".")
            from statement_parser import _VERBS
            if condition_syntax_supported(condition, set(_VERBS)):
                tokens = statement_tokens(condition)
                if tokens:
                    facts.append({**location, "predicate": "condition_statement", "target": None,
                                  "condition": list(tokens), "outcome": True})
        elif unit.name in {"WRITE", "REWRITE", "DELETE"}:
            statement = parse_statements(body, start_line=location["start_line"], source_format="free")
            tokens = statement_tokens(body)
            if statement.complete and tokens and len(tokens) >= 2 and _is_identifier(tokens[1]):
                facts.append({**location, "predicate": "write_statement", "target": tokens[1],
                              "effect": unit.name})
    # A procedural COPY or captured body can have no division header. The
    # structural index deliberately does not join its physical lines as a
    # program body. Recover only complete, bounded COMPUTE nodes from the
    # actual supplied text; this proves syntax, never COPY binding or execution.
    if (len(source) <= MAX_SOURCE_CHARS and document.format_hint in {"free", "fixed"}
            and any(unit.name == "COMPUTE" for unit in parsed.code_units)
            and not any(unit.unit_type in {"Program", "ProcedureSignature", "DataItem"}
                        for unit in parsed.code_units)):
        standalone = parse_statements(source, start_line=first, source_format=document.format_hint,
                                      max_tokens=4096)
        if standalone.complete and not standalone.boundaries:
            raw_lines = source.splitlines()
            existing = {(fact["statement_kind"], fact["start_line"], fact.get("target")) for fact in facts}
            for node in standalone.statements:
                if (node.kind != "COMPUTE" or node.effects_unknown
                        or node.span.end_line <= node.span.start_line):
                    continue
                if not (re.search(r"\bEND-COMPUTE\s*$", node.text, re.I)
                        or re.match(r"\s*\.", source[node.span.end_offset:])):
                    # A physical page ending after a valid expression may
                    # still omit a following arithmetic continuation line.
                    continue
                for target, expression in node.expressions:
                    if (node.kind, node.span.start_line, target) in existing:
                        continue
                    tokens = statement_tokens(expression)
                    if not tokens:
                        continue
                    identity = "\x1f".join(str(value) for value in (document.relative_path,
                        document.source_sha256, node.span.start_line, node.span.end_line,
                        node.span.start_column, node.span.end_column, node.text, target))
                    facts.append({"start_line": node.span.start_line, "end_line": node.span.end_line,
                        "program_name": None,
                        "occurrence_id": "supplied_" + hashlib.sha256(identity.encode()).hexdigest()[:24],
                        "statement_kind": node.kind,
                        "guards": [{"kind": guard.kind,
                            "condition": source[guard.span.start_offset:guard.span.end_offset],
                            "outcome": guard.outcome, "start_line": guard.span.start_line,
                            "end_line": guard.span.end_line} for guard in node.guards],
                        "source_text": "\n".join(raw_lines[node.span.start_line - first:node.span.end_line - first + 1]),
                        "predicate": "assignment_statement", "target": target,
                        "expression": list(tokens), "rounded": node.rounded,
                        "normal_result_condition": None, "exception_effects_unverified": False})
    boundaries = sorted((unit for unit in parsed.code_units if unit.unit_type in
                  {"Paragraph", "Section", "Program", "ProcedureSignature"}), key=lambda unit: unit.start_line)
    boundary_starts = [unit.start_line for unit in boundaries]
    line_starts = [line.start_line for line in document.lines]
    for unit in boundaries:
        if unit.unit_type != "Paragraph" or unit.parse_status != "complete":
            continue
        next_index = bisect_right(boundary_starts, unit.end_line)
        following = boundaries[next_index] if next_index < len(boundaries) else None
        if following is None:
            continue  # An excerpt ending at EXIT cannot prove the paragraph ends there.
        body = "\n".join(line.text for line in document.lines[
            bisect_right(line_starts, unit.end_line):bisect_left(line_starts, following.start_line)])
        statements = parse_statements(body)
        if statements.complete and statements.nodes and all(
                statement.kind in {"EXIT", "CONTINUE"} for statement in statements.nodes):
            facts.append({"predicate": "noop_procedure", "target": unit.name,
                "statement_kind": "PARAGRAPH", "occurrence_id": unit.unit_id,
                "start_line": first + unit.start_line - 1,
                "end_line": first + following.start_line - 2,
                "source_text": "\n".join(source.splitlines()[unit.start_line - 1:following.start_line - 1]),
                "body_kinds": [statement.kind for statement in statements.statements],
                "support_scope": "complete_local_procedure_body_no_data_or_io_statements"})
    return facts


def check_statement_support(assertion: Mapping[str, Any], fact: Mapping[str, Any],
                            page: Mapping[str, Any]) -> dict[str, Any]:
    """Check one explicit source assertion against one occurrence.

    The narrow COMPUTE checker is retained for the syntax it supports. The
    shared front-end projection extends the same exact-token comparison to
    MOVE, arithmetic, IF headers and explicit write requests. No result here
    proves that the statement executes, a write succeeds, or a value is final.
    """
    predicate = assertion.get("predicate")
    fields = {"assignment_statement": ("target", "expression", "rounded"),
              "declaration_value": ("target", "expression"),
              "condition_statement": ("condition", "outcome"),
              "write_statement": ("target", "effect")}.get(predicate)
    result = {"support_status": "unsupported", "reason_code": "SEMANTICS_NOT_SUPPORTED",
              "checker_version": STATEMENT_CHECKER_VERSION,
              "support_scope": "source_statement_syntax"}
    if predicate == "declaration_value":
        result["support_scope"] = "source_declared_initial_value"
    if fields is None or predicate != fact.get("predicate"):
        return result
    if assertion.get("statement_kind") and assertion["statement_kind"] != fact.get("statement_kind"):
        result["reason_code"] = "SOURCE_OPERATION_NOT_COMPARABLE"
        return result
    if predicate == "assignment_statement" and fact.get("statement_kind") == "COMPUTE":
        narrow = {"predicate": "compute_statement", "target": assertion.get("target"),
                  "expression": assertion.get("expression"), "rounded": assertion.get("rounded")}
        identifier = str(page.get("evidence_id", ""))
        span = {**page, "source_text": fact.get("source_text"),
                "start_line": fact["start_line"], "end_line": fact["end_line"],
                "integrity": "VALID", "span_truncated": False, "content_type": "UNTRUSTED_SOURCE_TEXT"}
        checked = check_claim_support(narrow, [identifier], {identifier: span})
        if checked["reason_code"] not in {"SOURCE_FORM_NOT_SUPPORTED", "INVALID_ASSERTION"}:
            return {**checked, "support_scope": "source_statement_syntax"}
    def same_field(field):
        claimed, supplied = assertion.get(field), fact.get(field)
        if predicate == "declaration_value" and field == "expression" and claimed != supplied:
            # Numeric VALUE literals have exact decimal meaning; formatting
            # such as 500 versus 500.00 does not change that declared value.
            # This does not normalize formulas or simulate storage conversion.
            from decimal import Decimal
            if (isinstance(claimed, list) and isinstance(supplied, list)
                    and len(claimed) <= 2 and len(supplied) <= 2):
                left, right = "".join(claimed), "".join(supplied)
                if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", left) and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", right):
                    return Decimal(left) == Decimal(right)
        return claimed == supplied
    matched = all(same_field(field) for field in fields)
    result.update(support_status="supported" if matched else "contradicted",
                  reason_code="SOURCE_STATEMENT_MATCH" if matched else "SOURCE_STATEMENT_MISMATCH")
    return result
