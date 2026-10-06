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


PARSER_VERSION = "bounded-statement-ast-v1"
_WORD = re.compile(r"[A-Z_$#@][A-Z0-9_$#@-]*", re.I)
_NUMBER = re.compile(r"\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?")
_NAME = re.compile(r"[A-Z_$#@][A-Z0-9_$#@-]*\Z", re.I)
_FIGURATIVE = frozenset({"ZERO", "ZEROS", "ZEROES", "SPACE", "SPACES", "HIGH-VALUES",
                        "LOW-VALUES", "QUOTE", "QUOTES", "NULL", "NULLS"})
_VERBS = frozenset("ACCEPT ADD ALTER CALL CANCEL CLOSE COMPUTE CONTINUE DELETE DISPLAY DIVIDE "
    "ENTRY EVALUATE EXEC EXIT GO GOBACK IF INITIALIZE INSPECT MERGE MOVE MULTIPLY OPEN PERFORM "
    "READ RELEASE RETURN REWRITE SEARCH SET SORT START STOP STRING SUBTRACT UNSTRING WRITE "
    "NEXT COPY REPLACE".split())
_TERMINATORS = frozenset({"ELSE", "WHEN", "END-IF", "END-EVALUATE", "END-COMPUTE", "."})
_CLAUSES = frozenset({"ON", "NOT", "END-CALL", "END-PERFORM", "END-READ", "END-WRITE",
                      "END-ADD", "END-SUBTRACT", "END-MULTIPLY", "END-DIVIDE"})
_RESERVED = _VERBS | _TERMINATORS | _CLAUSES | _FIGURATIVE | frozenset(
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


@dataclass(frozen=True)
class _Token:
    value: str
    kind: str
    start: int
    end: int


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


def _fixed_mask(text: str, source_format: str) -> str:
    if source_format == "free":
        return text
    masked, offset = [], 0
    for physical in text.splitlines(keepends=True):
        line = physical.rstrip("\r\n")
        ending = physical[len(line):]
        if line.strip() and len(line) < 7:
            raise _BoundaryError("fixed_line_missing_indicator", offset)
        indicator = line[6:7]
        if indicator in {"-", "D", "d"}:
            raise _BoundaryError("fixed_continuation_or_debug_line_not_supported", offset + 6)
        if indicator not in {"", " ", "*", "/"}:
            raise _BoundaryError("fixed_indicator_not_supported", offset + 6)
        if indicator in {"*", "/"}:
            masked.append(" " * len(line) + ending)
        else:
            masked.append(" " * min(7, len(line)) + line[7:72] + " " * max(0, len(line) - 72) + ending)
        offset += len(physical)
    return "".join(masked)


def _lex(source: _Source, source_format: str, max_tokens: int) -> tuple[_Token, ...]:
    text = _fixed_mask(source.text, source_format)
    result, position = [], 0
    while position < len(text):
        char = text[position]
        if char.isspace():
            position += 1
            continue
        if text.startswith("*>", position):
            while position < len(text) and text[position] not in "\r\n":
                position += 1
            continue
        start = position
        if char in {"'", '"'}:
            quote = char
            position += 1
            while position < len(text):
                if text[position] in "\r\n":
                    raise _BoundaryError("continued_literal_not_supported", start)
                if text[position] == quote:
                    position += 1
                    if position < len(text) and text[position] == quote:
                        position += 1
                        continue
                    break
                position += 1
            else:
                raise _BoundaryError("unterminated_literal", start)
            result.append(_Token(source.text[start:position], "literal", start, position))
        elif char.isdigit() and (number := _NUMBER.match(text, position)):
            position = number.end()
            if position < len(text) and (text[position].isalnum() or text[position] in "_$#@-"):
                raise _BoundaryError("numeric_or_identifier_form_not_supported", start)
            result.append(_Token(number[0], "number", start, position))
        elif word := _WORD.match(text, position):
            position = word.end()
            value = word[0].upper()
            if not _NAME.fullmatch(value) and value != "-":
                raise _BoundaryError("identifier_form_not_supported", start)
            result.append(_Token(value, "symbol" if value == "-" else "word", start, position))
        elif text[position:position + 2] in {"**", "<=", ">=", "<>"}:
            position += 2
            result.append(_Token(text[start:position], "symbol", start, position))
        elif char in ".=<>+-*/()":
            position += 1
            result.append(_Token(char, "symbol", start, position))
        else:
            raise _BoundaryError("unrecognized_source_character", start)
        if len(result) > max_tokens:
            raise _BoundaryError("token_budget_exhausted", start)
    return tuple(result)


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
            if (_identifier(token) or token.kind == "number" or token.value in {"ZERO", "ZEROS", "ZEROES"}
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
    def __init__(self, source: _Source, tokens: tuple[_Token, ...], max_depth: int):
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
        if kind in {"MOVE", "COMPUTE"}:
            return self.assignment(kind, first, guards)
        if kind in {"CALL", "PERFORM", "READ", "WRITE", "REWRITE", "DELETE", "START", "OPEN", "CLOSE"}:
            return self.opaque(kind, first, guards)
        if kind in {"CONTINUE", "GOBACK"}:
            return self.node(kind, first, guards)
        if kind == "STOP" and self.take("RUN"):
            return self.node("STOP_RUN", first, guards)
        if kind == "EXIT":
            if self.value() in {"PROGRAM", "PARAGRAPH", "SECTION"}:
                self.position += 1
            return self.node("EXIT", first, guards)
        self.fail("statement_not_supported:" + kind)

    def opaque(self, kind: str, first: int, guards: tuple[Guard, ...]) -> Statement:
        """Retain known call/I/O syntax without inventing storage effects.

        Explicit inline loops and I/O handlers remain single opaque nodes.
        Their nested writes are intentionally not added to ordered writes.
        """
        end_word = "END-" + kind
        header = self.collect(_VERBS | _TERMINATORS | _CLAUSES | {end_word, "AT", "INVALID"})
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
                return self.opaque_scope(kind, first, guards, end_word)
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

    def opaque_scope(self, kind: str, first: int, guards: tuple[Guard, ...], terminal: str) -> Statement:
        stack = [terminal]
        closers = {"END-IF", "END-EVALUATE", "END-PERFORM", "END-CALL", "END-COMPUTE",
                   "END-READ", "END-WRITE", "END-REWRITE", "END-DELETE", "END-START"}
        allowed = {"MOVE", "COMPUTE", "CALL", "PERFORM", "READ", "WRITE", "REWRITE", "DELETE",
                   "START", "OPEN", "CLOSE", "CONTINUE", "GOBACK", "STOP", "EXIT", "IF", "EVALUATE"}
        while self.position < len(self.tokens):
            value = self.value()
            if value == ".":
                self.fail("opaque_scope_requires_explicit_terminator")
            if value in _VERBS - allowed:
                self.fail("opaque_nested_statement_not_supported:" + value)
            if value in {"IF", "EVALUATE"}:
                stack.append("END-" + value)
            elif value in {"COMPUTE", "CALL", "READ", "WRITE", "REWRITE", "DELETE", "START", "PERFORM"}:
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

    def assignment(self, kind: str, first: int, guards: tuple[Guard, ...]) -> Statement:
        if kind == "MOVE":
            sender = self.collect(_VERBS | _TERMINATORS | {"TO"})
            valid_sender = (len(sender) == 1 and (_identifier(sender[0]) or sender[0].kind in {"number", "literal"}
                            or sender[0].value in _FIGURATIVE))
            valid_sender |= len(sender) == 2 and sender[0].value in {"+", "-"} and sender[1].kind == "number"
            if not valid_sender or not self.take("TO"):
                self.fail("move_sender_not_supported")
            targets = self.collect(_VERBS | _TERMINATORS | _CLAUSES)
            if not targets or any(not _identifier(token) for token in targets):
                self.fail("move_receiver_not_supported")
            return self.node(kind, first, guards,
                reads=tuple(token.value for token in sender if _identifier(token)),
                writes=tuple(token.value for token in targets), expression=self.raw(sender))
        if self.position >= len(self.tokens) or not _identifier(self.tokens[self.position]):
            self.fail("compute_receiver_not_supported")
        target = self.value()
        self.position += 1
        rounded = self.take("ROUNDED")
        if not self.take("="):
            self.fail("compute_single_receiver_required")
        expression = self.collect(_VERBS | _TERMINATORS | _CLAUSES)
        if not _valid_expression(expression):
            self.fail("compute_expression_not_supported")
        if self.value() in _CLAUSES:
            return self.opaque_scope(kind, first, guards, "END-COMPUTE")
        self.take("END-COMPUTE")
        return self.node(kind, first, guards, writes=(target,), expression=self.raw(expression), rounded=rounded,
            reads=tuple(dict.fromkeys(token.value for token in expression if _identifier(token))))

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
                     complete: bool = True, max_tokens: int = 50000, max_depth: int = 64) -> ParseResult:
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
    original = _Source(source, start_line)
    if not complete:
        return ParseResult((), (), (), (original.boundary("input_fragment_incomplete", 0),), False, source)
    try:
        tokens = _lex(original, source_format, max_tokens)
    except _BoundaryError as error:
        return ParseResult((), (), (), (original.boundary(error.reason, 0),), False, source)
    parser = _Parser(original, tokens, max_depth)
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
            writes.append(WriteOccurrence(len(writes), statement.kind, target, statement.expression or "",
                statement.rounded, statement.guards, statement.span))
    return ParseResult(tuple(nodes), tuple(leaves), tuple(writes), tuple(boundaries), not boundaries, source)
