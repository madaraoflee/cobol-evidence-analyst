"""Bounded source-preserving COBOL statement AST, without external runtimes.

The input is a complete procedure/paragraph *body*, not a whole compilation
unit. The supported language is deliberately small: IF, single-selector
EVALUATE, MOVE, single-receiver COMPUTE and simple exits. Unknown syntax stops
the affected compound and the remaining input; it never becomes a guessed
statement. Ordering and guards describe syntax, not feasibility or final values.
No cache is global: callers can cache this immutable result by content/version.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import re

from statement_facts import (
    COBOL_RESERVED, COBOL_VERBS, SCOPE_TERMINATORS, CobolLexError, CobolToken,
    assignment_facts, lex_cobol, parse_data_ref, references_from_tokens,
)


PARSER_VERSION = "bounded-statement-ast-v3"
_WORD = re.compile(r"[A-Z_$#@][A-Z0-9_$#@-]*", re.I)
_NUMBER = re.compile(r"\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?")
_NAME = re.compile(r"(?=[A-Z0-9_$#@-]*[A-Z_$#@])[A-Z0-9_$#@][A-Z0-9_$#@-]*\Z", re.I)
_FIGURATIVE = frozenset({"ZERO", "ZEROS", "ZEROES", "SPACE", "SPACES", "HIGH-VALUES",
                        "LOW-VALUES", "QUOTE", "QUOTES", "NULL", "NULLS"})
_VERBS = COBOL_VERBS
_TERMINATORS = frozenset({"ELSE", "WHEN", "END-IF", "END-EVALUATE", "END-COMPUTE", "."})
_CLAUSES = frozenset({"ON", "NOT", "END-CALL", "END-PERFORM", "END-READ", "END-WRITE",
                      "END-ADD", "END-SUBTRACT", "END-MULTIPLY", "END-DIVIDE"})
_RESERVED = COBOL_RESERVED | _VERBS | _TERMINATORS | _CLAUSES | _FIGURATIVE | frozenset(
    "THEN TO FROM BY INTO GIVING ROUNDED ALSO OTHER ANY THRU THROUGH IS AND OR OF IN "
    "EQUAL GREATER LESS THAN TRUE FALSE NUMERIC ALPHABETIC POSITIVE NEGATIVE "
    "CORRESPONDING CORR SIZE ERROR EXCEPTION RUN PROGRAM PARAGRAPH SECTION".split())


@dataclass(frozen=True)
class SourceSpan:
    """Offsets and end column are exclusive; line/column numbers are one-based."""

    start_line: int
    end_line: int
    start_column: int
    end_column: int
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class Guard:
    kind: str
    condition: str
    outcome: bool
    span: SourceSpan
    selector: str | None = None
    alternatives: tuple[str, ...] = ()
    prior_alternatives: tuple[tuple[str, ...], ...] = ()
    branch_index: int | None = None
    is_other: bool = False
    selector_span: SourceSpan | None = None


@dataclass(frozen=True)
class Statement:
    kind: str
    text: str
    span: SourceSpan
    guards: tuple[Guard, ...] = ()
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    expression: str | None = None
    rounded: bool = False
    children: tuple[Statement, ...] = ()
    else_children: tuple[Statement, ...] = ()
    effects_unknown: bool = False
    expressions: tuple[tuple[str, str], ...] = ()
    loop: dict | None = None
    exception_children: tuple[Statement, ...] = ()
    normal_children: tuple[Statement, ...] = ()
    exit_kind: str = ""
    size_error_handler_present: bool = False


@dataclass(frozen=True)
class WriteOccurrence:
    order: int
    statement_kind: str
    target: str
    expression: str
    rounded: bool
    guards: tuple[Guard, ...]
    span: SourceSpan


@dataclass(frozen=True)
class Boundary:
    reason: str
    span: SourceSpan
    raw_text: str


@dataclass(frozen=True)
class ParseResult:
    nodes: tuple[Statement, ...]
    statements: tuple[Statement, ...]
    writes: tuple[WriteOccurrence, ...]
    boundaries: tuple[Boundary, ...]
    complete: bool
    source: str
    parser_version: str = PARSER_VERSION


_Token = CobolToken

class _BoundaryError(ValueError):
    def __init__(self, reason: str, offset: int):
        self.reason, self.offset = reason, offset


class _Source:
    def __init__(self, text: str, start_line: int):
        self.text, self.start_line = text, start_line
        # CRLF is one physical newline. A lone CR is also a physical newline.
        self.starts = [0] + [match.end() for match in re.finditer(r"\r\n|\r|\n", text)]

    def span(self, start: int, end: int) -> SourceSpan:
        first = bisect_right(self.starts, start) - 1
        last = bisect_right(self.starts, max(start, end - 1)) - 1
        return SourceSpan(self.start_line + first, self.start_line + last,
            start - self.starts[first] + 1, end - self.starts[last] + 1, start, end)

    def boundary(self, reason: str, start: int, end: int | None = None) -> Boundary:
        end = len(self.text) if end is None else end
        return Boundary(reason, self.span(start, end), self.text[start:end])


def _lex(source: _Source, source_format: str, max_tokens: int, check_cancel=None) -> tuple[_Token, ...]:
    try:
        return lex_cobol(source.text, source_format=source_format, max_tokens=max_tokens, check_cancel=check_cancel)
    except CobolLexError as error:
        raise _BoundaryError(error.reason, error.offset) from error


def _identifier(token: _Token) -> bool:
    return token.kind == "word" and token.value not in _RESERVED and bool(_NAME.fullmatch(token.value))


class _Expression:
    """Consume a bounded arithmetic or explicit boolean expression grammar."""

    def __init__(self, tokens: tuple[_Token, ...]):
        self.tokens, self.position, self.depth = tokens, 0, 0

    def take(self, *values: str) -> bool:
        if self.position < len(self.tokens) and self.tokens[self.position].value in values:
            self.position += 1
            return True
        return False

    def arithmetic(self, *, text_operand: bool = False) -> bool:
        start = self.position
        if not self.product(text_operand=text_operand):
            return False
        while self.take("+", "-"):
            if not self.product():
                return False
        # A text operand can participate in a comparison, not in arithmetic.
        # Field types are unknown here, so only explicit text forms are rejected.
        nonnumeric = any(token.kind == "literal" or token.value in _FIGURATIVE - {"ZERO", "ZEROS", "ZEROES"}
                         for token in self.tokens[start:self.position])
        return not nonnumeric or self.position == start + 1

    def product(self, *, text_operand: bool = False) -> bool:
        if not self.atom(text_operand=text_operand):
            return False
        while self.take("*", "/", "**"):
            if not self.atom():
                return False
        return True

    def atom(self, *, text_operand: bool = False) -> bool:
        self.depth += 1
        if self.depth > 64:
            return False
        try:
            if self.take("+", "-"):
                return self.atom()
            if self.take("("):
                return self.arithmetic() and self.take(")")
            if self.position >= len(self.tokens):
                return False
            token = self.tokens[self.position]
            reference, following = parse_data_ref(self.tokens, self.position)
            if reference:
                self.position = following
                return True
            if (token.kind == "number" or token.value in {"ZERO", "ZEROS", "ZEROES"}
                    or text_operand and (token.kind == "literal" or token.value in _FIGURATIVE)):
                self.position += 1
                return True
            return False
        finally:
            self.depth -= 1

    def boolean(self) -> bool:
        if not self.conjunction():
            return False
        while self.take("OR"):
            if not self.conjunction():
                return False
        return True

    def conjunction(self) -> bool:
        if not self.comparison():
            return False
        while self.take("AND"):
            if not self.comparison():
                return False
        return True

    def comparison(self) -> bool:
        self.depth += 1
        if self.depth > 64:
            return False
        try:
            if self.take("NOT"):
                return self.comparison()
            first = self.position
            if self.take("("):
                if self.boolean() and self.take(")") and (self.position == len(self.tokens)
                        or self.tokens[self.position].value in {"AND", "OR", ")"}):
                    return True
                self.position = first
            if not self.arithmetic(text_operand=True):
                return False
            operand_end = self.position
            has_is = self.take("IS")
            has_not = self.take("NOT")
            if self.take("NUMERIC", "ALPHABETIC", "POSITIVE", "NEGATIVE", "ZERO"):
                return operand_end == first + 1 and _identifier(self.tokens[first])
            if self.take("=", "<>", "<", ">", "<=", ">="):
                return self.arithmetic(text_operand=True)
            if self.take("EQUAL"):
                self.take("TO")
                return self.arithmetic(text_operand=True)
            if self.take("GREATER", "LESS"):
                self.take("THAN")
                if self.take("OR"):
                    if not self.take("EQUAL"):
                        return False
                    self.take("TO")
                return self.arithmetic(text_operand=True)
            return not has_is and not has_not and operand_end == first + 1 and _identifier(self.tokens[first])
        finally:
            self.depth -= 1


def _valid_expression(tokens: tuple[_Token, ...], *, condition: bool = False) -> bool:
    parser = _Expression(tokens)
    return bool(tokens) and (parser.boolean() if condition else parser.arithmetic()) and parser.position == len(tokens)


class _Parser:
    def __init__(self, source: _Source, tokens: tuple[_Token, ...], max_depth: int, check_cancel=None):
        self.check_cancel = check_cancel
        self.source, self.tokens, self.max_depth = source, tokens, max_depth
        self.position = 0

    def value(self) -> str:
        return self.tokens[self.position].value if self.position < len(self.tokens) else ""

    def take(self, value: str) -> bool:
        if self.value() == value:
            self.position += 1
            return True
        return False

    def fail(self, reason: str):
        offset = self.tokens[self.position].start if self.position < len(self.tokens) else len(self.source.text)
        raise _BoundaryError(reason, offset)

    def raw(self, tokens: tuple[_Token, ...]) -> str:
        return self.source.text[tokens[0].start:tokens[-1].end] if tokens else ""

    def node(self, kind: str, first: int, guards: tuple[Guard, ...], **fields) -> Statement:
        start, end = self.tokens[first].start, self.tokens[self.position - 1].end
        return Statement(kind, self.source.text[start:end], self.source.span(start, end), guards, **fields)

    def collect(self, stops: frozenset[str]) -> tuple[_Token, ...]:
        start = self.position
        while self.position < len(self.tokens) and self.value() not in stops:
            self.position += 1
        return self.tokens[start:self.position]

    def sequence(self, guards: tuple[Guard, ...], depth: int, stops: frozenset[str]) -> tuple[Statement, ...]:
        result = []
        while self.position < len(self.tokens) and self.value() not in stops:
            result.append(self.statement(guards, depth))
        return tuple(result)

    def statement(self, guards: tuple[Guard, ...], depth: int) -> Statement:
        if self.check_cancel:
            self.check_cancel()
        if depth >= self.max_depth:
            self.fail("syntax_depth_budget_exhausted")
        first, kind = self.position, self.value()
        self.position += 1
        if kind == "IF":
            condition = self.collect(_VERBS | _TERMINATORS | {"THEN"})
            if not _valid_expression(condition, condition=True):
                self.fail("if_condition_not_supported")
            self.take("THEN")
            guard = Guard("IF", self.raw(condition), True,
                self.source.span(self.tokens[first].start, self.tokens[self.position - 1].end))
            body = self.sequence(guards + (guard,), depth + 1, frozenset({"ELSE", "END-IF", "."}))
            if not body:
                self.fail("empty_if_branch")
            other = ()
            if self.take("ELSE"):
                false_guard = Guard("IF", guard.condition, False, guard.span)
                other = self.sequence(guards + (false_guard,), depth + 1, frozenset({"END-IF", "."}))
                if not other:
                    self.fail("empty_else_branch")
            if not self.take("END-IF") and self.value() != ".":
                self.fail("if_scope_incomplete")
            return self.node(kind, first, guards, children=body, else_children=other)
        if kind == "EVALUATE":
            return self.evaluate(first, guards, depth)
        if kind in {"MOVE", "COMPUTE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE"}:
            return self.assignment(kind, first, guards, depth)
        if kind == "DISPLAY":
            operands = self.collect(_VERBS | _TERMINATORS | SCOPE_TERMINATORS)
            if not operands:
                self.fail("display_operands_missing")
            end = next((index for index, token in enumerate(operands) if token.value in {"UPON", "WITH"}), len(operands))
            return self.node(kind, first, guards,
                reads=tuple(ref.identity for ref in references_from_tokens(operands[:end])))
        if kind in {"CALL", "PERFORM", "READ", "WRITE", "REWRITE", "DELETE", "START", "OPEN", "CLOSE"}:
            return self.opaque(kind, first, guards, depth)
        if kind in {"CONTINUE", "GOBACK"}:
            return self.node(kind, first, guards)
        if kind == "STOP" and self.take("RUN"):
            return self.node("STOP_RUN", first, guards)
        if kind == "EXIT":
            exit_kind = "NOOP"
            if self.value() in {"PROGRAM", "PARAGRAPH", "SECTION", "PERFORM"}:
                exit_kind = self.value()
                self.position += 1
                if exit_kind == "PERFORM" and self.take("CYCLE"):
                    exit_kind = "PERFORM_CYCLE"
            return self.node("EXIT", first, guards, exit_kind=exit_kind)
        self.fail("statement_not_supported:" + kind)

    def opaque(self, kind: str, first: int, guards: tuple[Guard, ...], depth=0) -> Statement:
        """Retain known call/I/O syntax without inventing storage effects.

        Explicit inline loops and I/O handlers remain single opaque nodes.
        Their nested writes are intentionally not added to ordered writes.
        """
        end_word = "END-" + kind
        header = self.collect(_VERBS | _TERMINATORS | (frozenset() if kind == "PERFORM" else _CLAUSES) | {end_word, "AT", "INVALID"})
        if not header:
            self.fail("opaque_statement_header_missing")
        if kind == "CALL":
            if not (_identifier(header[0]) or header[0].kind == "literal"):
                self.fail("call_target_form_not_supported")
            if any(token.value in {"ALSO", "THEN"} for token in header):
                self.fail("call_header_not_supported")
        elif kind == "PERFORM":
            inline = header[0].value in {"UNTIL", "WITH", "TEST", "VARYING"} or any(token.value == "TIMES" for token in header)
            if inline:
                return self.perform_loop(first, guards, header, depth)
            if not _identifier(header[0]):
                self.fail("perform_target_not_supported")
            if len(header) not in {1, 3} or len(header) == 3 and (
                    header[1].value not in {"THRU", "THROUGH"} or not _identifier(header[2])):
                self.fail("perform_header_not_supported")
        elif kind not in {"OPEN", "CLOSE"} and not _identifier(header[0]):
            self.fail("file_operand_not_supported")
        if self.value() in {"ON", "NOT", "AT", "INVALID"}:
            return self.opaque_scope(kind, first, guards, end_word)
        self.take(end_word)
        return self.node(kind, first, guards, effects_unknown=True)

    def perform_loop(self, first, guards, header, depth):
        values = [token.value for token in header]
        test_before = True
        position = 0
        if values[:1] == ["WITH"]:
            position += 1
        if values[position:position + 1] == ["TEST"]:
            position += 1
            if values[position:position + 1] not in (["BEFORE"], ["AFTER"]):
                self.fail("perform_test_position_not_supported")
            test_before = values[position] == "BEFORE"
            position += 1
        loop = {"test_before": test_before}
        if values[-1:] == ["TIMES"] and position == 0 and _valid_expression(header[:-1]):
            loop.update({"kind": "times", "count": self.raw(header[:-1])})
        elif values[position:position + 1] == ["UNTIL"] and _valid_expression(header[position + 1:], condition=True):
            loop.update({"kind": "until", "condition": self.raw(header[position + 1:])})
        elif values[position:position + 1] == ["VARYING"]:
            remaining = header[position + 1:]
            variable, after_variable = parse_data_ref(remaining)
            marks = {word: [index for index, token in enumerate(remaining) if token.value == word]
                     for word in ("FROM", "BY", "UNTIL", "AFTER")}
            if (not variable or any(len(marks[word]) != 1 for word in ("FROM", "BY", "UNTIL"))
                    or marks["AFTER"] or marks["FROM"][0] != after_variable
                    or not marks["FROM"][0] < marks["BY"][0] < marks["UNTIL"][0]):
                self.fail("perform_varying_header_not_supported")
            origin = remaining[marks["FROM"][0] + 1:marks["BY"][0]]
            step = remaining[marks["BY"][0] + 1:marks["UNTIL"][0]]
            condition = remaining[marks["UNTIL"][0] + 1:]
            if not _valid_expression(origin) or not _valid_expression(step) or not _valid_expression(condition, condition=True):
                self.fail("perform_varying_expression_not_supported")
            loop.update({"kind": "varying", "variable": variable.identity, "from": self.raw(origin),
                         "by": self.raw(step), "condition": self.raw(condition)})
        else:
            self.fail("perform_loop_header_not_supported")
        body = self.sequence(guards, depth + 1, frozenset({"END-PERFORM", "."}))
        if not body or not self.take("END-PERFORM"):
            self.fail("opaque_scope_incomplete")
        # General AST write consumers still abstain from treating loop bodies as
        # unconditional. Flow services consume the explicit loop structure.
        return self.node("PERFORM", first, guards, children=body, loop=loop, effects_unknown=True)

    def opaque_scope(self, kind: str, first: int, guards: tuple[Guard, ...], terminal: str) -> Statement:
        stack = [terminal]
        closers = {"END-IF", "END-EVALUATE", "END-PERFORM", "END-CALL", "END-COMPUTE",
                   "END-READ", "END-WRITE", "END-REWRITE", "END-DELETE", "END-START",
                   "END-ADD", "END-SUBTRACT", "END-MULTIPLY", "END-DIVIDE"}
        allowed = {"MOVE", "COMPUTE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE", "DISPLAY", "CALL", "PERFORM", "READ", "WRITE", "REWRITE", "DELETE",
                   "START", "OPEN", "CLOSE", "CONTINUE", "GOBACK", "STOP", "EXIT", "IF", "EVALUATE"}
        while self.position < len(self.tokens):
            value = self.value()
            if value == ".":
                self.fail("opaque_scope_requires_explicit_terminator")
            if value in _VERBS - allowed:
                self.fail("opaque_nested_statement_not_supported:" + value)
            if value in {"IF", "EVALUATE"}:
                stack.append("END-" + value)
            elif value in {"COMPUTE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE", "CALL", "READ", "WRITE", "REWRITE", "DELETE", "START", "PERFORM"}:
                # A nested call/assignment need not have an explicit terminator.
                # Claim one only if it occurs before the next statement/scope
                # boundary, or its header introduces an explicit handler/loop.
                following = self.position + 1
                header = []
                while following < len(self.tokens) and self.tokens[following].value not in _VERBS | _TERMINATORS:
                    candidate = self.tokens[following].value
                    if candidate in closers:
                        break
                    header.append(candidate)
                    following += 1
                closer = "END-" + value
                explicit = following < len(self.tokens) and self.tokens[following].value == closer
                handler = any(word in {"ON", "AT", "INVALID"} for word in header)
                inline_loop = value == "PERFORM" and (header[:1] in (["UNTIL"], ["WITH"], ["TEST"], ["VARYING"])
                                                     or "TIMES" in header)
                if explicit or handler or inline_loop:
                    stack.append(closer)
            elif value in closers:
                if not stack or value != stack[-1]:
                    self.fail("opaque_scope_mismatched_terminator")
                stack.pop()
            elif value == "ELSE" and stack[-1] != "END-IF":
                self.fail("opaque_scope_unmatched_else")
            elif value == "WHEN" and stack[-1] != "END-EVALUATE":
                self.fail("opaque_scope_unmatched_when")
            if len(stack) > self.max_depth:
                self.fail("syntax_depth_budget_exhausted")
            self.position += 1
            if not stack:
                return self.node(kind, first, guards, effects_unknown=True)
        self.fail("opaque_scope_incomplete")

    def assignment(self, kind: str, first: int, guards: tuple[Guard, ...], depth=0) -> Statement:
        header = self.collect(_VERBS | SCOPE_TERMINATORS | _TERMINATORS | _CLAUSES)
        body_tokens = self.tokens[first:self.position]
        facts = assignment_facts(self.raw(body_tokens))
        if facts is None:
            # A fixed-format token can cross sequence columns or a continued
            # literal. Its token value is the preprocessed value; its span
            # still identifies the original physical source.
            facts = assignment_facts(" ".join(token.value for token in body_tokens))
        if facts is None:
            self.fail(kind.lower() + "_operands_not_supported")
        if kind == "COMPUTE":
            equal = next((index for index, token in enumerate(header) if token.value == "="), -1)
            if equal < 0 or not _valid_expression(header[equal + 1:]):
                self.fail("compute_expression_not_supported")
        if kind != "MOVE" and self.value() in {"ON", "NOT", "AT", "INVALID"}:
            return self.arithmetic_scope(kind, first, guards, facts, depth)
        self.take("END-" + kind)
        expressions = tuple(expression for _, expression in facts.expressions)
        return self.node(kind, first, guards, reads=facts.reads, writes=facts.writes,
            expression=expressions[0] if len(set(expressions)) == 1 else None,
            rounded=facts.rounded, expressions=facts.expressions)

    def arithmetic_scope(self, kind, first, guards, facts, depth):
        """Retain exact error/normal branches without unconditional write promotion."""
        terminal = "END-" + kind
        error_body, normal_body = (), ()
        if self.take("ON"):
            if not self.take("SIZE") or not self.take("ERROR"):
                self.fail("arithmetic_exception_clause_not_supported")
            error_body = self.sequence(guards, depth + 1, frozenset({"NOT", terminal, "."}))
            if not error_body:
                self.fail("empty_size_error_branch")
        if self.take("NOT"):
            if not self.take("ON") or not self.take("SIZE") or not self.take("ERROR"):
                self.fail("arithmetic_normal_clause_not_supported")
            normal_body = self.sequence(guards, depth + 1, frozenset({terminal, "."}))
            if not normal_body:
                self.fail("empty_normal_arithmetic_branch")
        if not error_body and not normal_body or not self.take(terminal):
            self.fail("opaque_scope_incomplete")
        return self.node(kind, first, guards, reads=facts.reads, expressions=facts.expressions,
            rounded=facts.rounded, effects_unknown=True,
            exception_children=error_body, normal_children=normal_body,
            size_error_handler_present=bool(error_body))

    def evaluate(self, first: int, guards: tuple[Guard, ...], depth: int) -> Statement:
        selector = self.collect(_VERBS | _TERMINATORS)
        truth = len(selector) == 1 and selector[0].value in {"TRUE", "FALSE"}
        if not truth and not _valid_expression(selector):
            self.fail("evaluate_selector_not_supported")
        branches, previous, seen_other = [], [], False
        while self.take("WHEN"):
            if seen_other:
                self.fail("when_after_other")
            when_start = self.position - 1
            alternatives, other = [], False
            while True:
                selected = self.collect(_VERBS | _TERMINATORS)
                other = len(selected) == 1 and selected[0].value == "OTHER"
                if not other and not self.valid_when(selected, truth):
                    self.fail("evaluate_when_not_supported")
                if other and alternatives:
                    self.fail("other_combined_with_when")
                alternatives.append(self.raw(selected))
                if self.value() != "WHEN":
                    break
                if other:
                    self.fail("when_after_other")
                self.position += 1
            guard = Guard("EVALUATE", " | ".join(alternatives), True,
                self.source.span(self.tokens[when_start].start, self.tokens[self.position - 1].end),
                selector=self.raw(selector), alternatives=tuple(alternatives),
                prior_alternatives=tuple(previous), branch_index=len(branches), is_other=other,
                selector_span=self.source.span(self.tokens[first].start, selector[-1].end))
            body = self.sequence(guards + (guard,), depth + 1, frozenset({"WHEN", "END-EVALUATE", "."}))
            if not body:
                self.fail("empty_when_branch")
            branches.append(self.node("WHEN", when_start, guards + (guard,), children=body))
            previous.append(tuple(alternatives))
            seen_other = other
        if not branches:
            self.fail("evaluate_without_when")
        if not self.take("END-EVALUATE") and self.value() != ".":
            self.fail("evaluate_scope_incomplete")
        return self.node("EVALUATE", first, guards, children=tuple(branches))

    @staticmethod
    def valid_when(tokens: tuple[_Token, ...], truth: bool) -> bool:
        if truth:
            return _valid_expression(tokens, condition=True)
        if len(tokens) == 1 and (tokens[0].kind in {"literal", "number"} or _identifier(tokens[0]) or tokens[0].value in _FIGURATIVE | {"ANY"}):
            return True
        if len(tokens) == 3 and tokens[1].value in {"THRU", "THROUGH"}:
            return all(token.kind in {"literal", "number"} or _identifier(token) or token.value in _FIGURATIVE
                       for token in (tokens[0], tokens[2]))
        return len(tokens) == 2 and tokens[0].value in {"+", "-"} and tokens[1].kind == "number"


def parse_statements(source: str, *, start_line: int = 1, source_format: str = "free",
                     complete: bool = True, max_tokens: int = 50000, max_depth: int = 64, check_cancel=None) -> ParseResult:
    """Parse a complete body; incomplete input yields no promoted statements.

    ``statements`` are leaf AST nodes in textual order. ``writes`` includes
    mutually exclusive alternatives and unreachable textual assignments: guards
    and ordering alone are not a reaching-definition or execution analysis.
    A lexical failure invalidates the whole input. A syntax failure preserves
    only previously completed top-level nodes, plus the untouched remainder.
    """
    if not isinstance(source, str) or not isinstance(start_line, int) or start_line < 1:
        raise ValueError("source must be text and start_line must be positive")
    if source_format not in {"free", "fixed"} or not 1 <= max_depth <= 64 or max_tokens < 1:
        raise ValueError("invalid source format or parser budget")
    if check_cancel:
        check_cancel()
    original = _Source(source, start_line)
    if not complete:
        return ParseResult((), (), (), (original.boundary("input_fragment_incomplete", 0),), False, source)
    try:
        tokens = _lex(original, source_format, max_tokens, check_cancel)
    except _BoundaryError as error:
        return ParseResult((), (), (), (original.boundary(error.reason, 0),), False, source)
    parser = _Parser(original, tokens, max_depth, check_cancel)
    nodes, boundaries = [], []
    while parser.position < len(tokens):
        if parser.take("."):
            continue
        start = tokens[parser.position].start
        try:
            nodes.append(parser.statement((), 0))
        except _BoundaryError as error:
            boundaries.append(original.boundary(error.reason, start))
            break
    leaves = []

    def visit(node: Statement):
        if node.kind in {"IF", "EVALUATE", "WHEN"}:
            for child in node.children + node.else_children:
                visit(child)
        else:
            leaves.append(node)

    for node in nodes:
        visit(node)
    writes = []
    for statement in leaves:
        for target in statement.writes:
            writes.append(WriteOccurrence(len(writes), statement.kind, target,
                dict(statement.expressions).get(target, statement.expression or ""),
                statement.rounded, statement.guards, statement.span))
    return ParseResult(tuple(nodes), tuple(leaves), tuple(writes), tuple(boundaries), not boundaries, source)
