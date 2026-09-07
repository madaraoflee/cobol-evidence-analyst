"""Independently check a small, explicit source-statement claim language.

The caller supplies evidence whose snapshot integrity was already checked.  This
module rechecks that contract, parses the complete span, and renders its own
claim.  It does not evaluate arithmetic, infer execution paths, or assess prose.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


CHECKER_VERSION = "compute-statement-v0.1"
MAX_SOURCE_CHARS = 16_000
MAX_EXPRESSION_TOKENS = 40
MAX_EXPRESSION_CHARS = 240
MAX_CLAIM_CHARS = 320

_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*", re.ASCII)
_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?", re.ASCII)
_TOKEN = re.compile(
    r"[0-9]+\.[0-9]+|[A-Z0-9][A-Z0-9-]*|[+*/()=-]",
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
        while position < len(tokens) and tokens[position] in {"*", "/"}:
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
