"""Small, deliberately bounded syntax recognizers for structural facts.

These helpers classify source syntax only. They do not prove runtime values,
execution paths, database column lineage, or complete language semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


FRONTEND_VERSION = "cobol-facts-v6"


def compile_condition_value(expression, context):
    """Evaluate only explicit, configured preprocessing conditions."""
    match = re.fullmatch(r"\s*(NOT\s+)?([A-Z0-9_$#@-]+)(?:\s+(?:IS\s+)?(DEFINED)|\s*=\s*(.+?))?\s*", expression, re.I)
    if not match:
        return None
    name = match[2].upper()
    if match[3]:
        settings = dict(context.compile_conditions)
        value = True if name in settings else False if context.conditions_complete else None
        return not value if match[1] and value is not None else value
    value = {"TRUE": True, "FALSE": False}.get(name) if name in {"TRUE", "FALSE"} else context.condition(name)
    if value is None:
        return None
    if match[4]:
        literal = match[4].strip("'\"").upper()
        result = str(int(value)) == literal if isinstance(value, bool) and literal in {"0", "1"} else str(value).upper() == literal
    elif isinstance(value, (bool, int)):
        result = bool(value)
    elif str(value).upper() in {"TRUE", "FALSE"}:
        result = str(value).upper() == "TRUE"
    else:
        return None
    return not result if match[1] else result


class CompilationGuardState:
    """Shared tri-state conditional preprocessing for both index paths."""
    def __init__(self, context=None):
        from compilation_context import resolve_compilation_context
        self.context = resolve_compilation_context(context)
        self.frames, self.diagnostics, self.invalid = [], [], False

    @property
    def status(self):
        values = [frame["value"] for frame in self.frames]
        return False if False in values else None if self.invalid or None in values else True

    @property
    def guards(self):
        return [{key: frame[key] for key in ("expression", "outcome", "value", "source_line")} for frame in self.frames]

    def feed(self, code, line):
        directive = re.fullmatch(r"\s*>>\s*(IF|ELSE|END-IF)\b(.*)", code, re.I)
        if not directive:
            if code.lstrip().startswith(">>") and not re.fullmatch(r"\s*>>\s*SOURCE\s+FORMAT\s+(?:IS\s+)?(?:FREE|FIXED)\s*", code, re.I):
                self.invalid = True
                self.diagnostics.append({"reason": "compile_directive_unsupported", "line": line})
                return True
            return False
        kind, body = directive[1].upper(), directive[2].strip()
        if kind == "IF":
            setting = compile_condition_value(body, self.context)
            self.frames.append({"expression": body, "outcome": True, "value": setting,
                                "source_line": line, "else_seen": False})
            if setting is None:
                self.diagnostics.append({"reason": "compile_condition_unknown", "line": line, "expression": body})
        elif not self.frames or body:
            self.invalid = True
            self.diagnostics.append({"reason": "compile_condition_scope_invalid", "line": line})
        elif kind == "ELSE":
            if self.frames[-1]["else_seen"]:
                self.invalid = True
                self.diagnostics.append({"reason": "compile_condition_duplicate_else", "line": line})
            self.frames[-1]["else_seen"] = True
            self.frames[-1]["outcome"] = False
            setting = self.frames[-1]["value"]
            self.frames[-1]["value"] = not setting if setting is not None else None
        else:
            self.frames.pop()
        return True

    def finish(self):
        """Return unclosed guards; callers demote their earlier source facts."""
        if not self.frames:
            return []
        self.invalid = True
        pending = self.guards
        self.diagnostics.extend({"reason": "compile_condition_scope_unclosed", "line": guard["source_line"],
                                 "expression": guard["expression"]} for guard in pending)
        return pending

COBOL_VERBS = frozenset("ACCEPT ADD ALTER CALL CANCEL CLOSE COMPUTE CONTINUE DELETE DISPLAY DIVIDE "
    "ENTRY EVALUATE EXEC EXIT GO GOBACK IF INITIALIZE INSPECT MERGE MOVE MULTIPLY OPEN PERFORM "
    "READ RELEASE RETURN REWRITE SEARCH SET SORT START STOP STRING SUBTRACT UNSTRING WRITE "
    "NEXT COPY REPLACE".split())
SCOPE_TERMINATORS = frozenset("END-ADD END-CALL END-COMPUTE END-DELETE END-DIVIDE END-EVALUATE "
    "END-IF END-MULTIPLY END-PERFORM END-READ END-RETURN END-REWRITE END-SEARCH END-START "
    "END-STRING END-SUBTRACT END-UNSTRING END-WRITE END-EXEC".split())
FIGURATIVE_CONSTANTS = frozenset("ZERO ZEROS ZEROES SPACE SPACES HIGH-VALUES LOW-VALUES "
    "QUOTE QUOTES NULL NULLS".split())
COBOL_RESERVED = COBOL_VERBS | SCOPE_TERMINATORS | FIGURATIVE_CONSTANTS | frozenset(
    "ELSE WHEN THEN TO FROM BY INTO GIVING ROUNDED ALSO OTHER ANY THRU THROUGH IS AND OR OF IN "
    "EQUAL GREATER LESS THAN TRUE FALSE NUMERIC ALPHABETIC ALPHABETIC-LOWER ALPHABETIC-UPPER "
    "POSITIVE NEGATIVE CORRESPONDING CORR SIZE ERROR EXCEPTION RUN PROGRAM PARAGRAPH SECTION "
    "ON NOT AT INVALID END OVERFLOW UNTIL WITH TEST VARYING TIMES AFTER BEFORE ADDRESS "
    "USING RETURNING REFERENCE CONTENT VALUE OMITTED REMAINDER INPUT OUTPUT I-O EXTEND "
    "REPLACING LEADING TRAILING ALL FUNCTION LENGTH DELIMITED POINTER TALLYING".split())
_LEX_WORD = re.compile(r"[A-Z0-9_$#@][A-Z0-9_$#@-]*", re.I)
_LEX_NUMBER = re.compile(r"\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?")
_FORMAT_DIRECTIVE = re.compile(r">>\s*SOURCE\s+FORMAT\s+(?:IS\s+)?(FREE|FIXED)", re.I)


@dataclass(frozen=True)
class CobolToken:
    value: str
    kind: str
    start: int
    end: int


class CobolLexError(ValueError):
    def __init__(self, reason: str, offset: int):
        super().__init__(reason)
        self.reason, self.offset = reason, offset


def strip_inline_comment(text: str) -> str:
    """Strip only an unquoted comment marker, respecting doubled quotes."""
    quote, index = None, 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in "'\"":
            quote = char
        elif text.startswith("*>", index):
            return text[:index]
        index += 1
    return text


def _open_literal_quote(text: str) -> str | None:
    quote, index = None, 0
    while index < len(text):
        if quote and text[index] == quote:
            if index + 1 < len(text) and text[index + 1] == quote:
                index += 2
                continue
            quote = None
        elif not quote and text[index] in "'\"":
            quote = text[index]
        index += 1
    return quote


def rstrip_cobol_code(text: str) -> str:
    """Trailing blanks in an open literal are data, not source indentation."""
    return text if _open_literal_quote(text) else text.rstrip()


def clean_cobol_line(raw: str, active_format: str = "auto") -> tuple[str, str, bool]:
    """Shared physical-line preprocessing for sparse and detailed indexes."""
    expanded = raw.expandtabs(8)
    directive = _FORMAT_DIRECTIVE.search(expanded)
    if directive:
        return "", directive.group(1).lower(), False
    fixed = active_format == "fixed" or (active_format == "auto" and len(expanded) >= 7 and (
        expanded[:6].isdigit() or (not expanded[:6].strip() and expanded[6] in " */-Dd")))
    if fixed:
        if expanded[6:7] in {"*", "/"}:
            return "", active_format, False
        continuation, code = expanded[6:7] == "-", expanded[7:72]
    else:
        continuation, code = False, expanded
    return rstrip_cobol_code(strip_inline_comment(code)).lstrip(), active_format, continuation


def join_continuation(previous: str, following: str) -> str:
    """Join a fixed continuation, including a continued nonnumeric literal."""
    quote = _open_literal_quote(previous)
    tail = following.lstrip()
    if quote and tail.startswith(quote):
        return previous + tail[1:]
    return previous.rstrip() + " " + tail


def _masked_fixed(text: str, check_cancel=None) -> tuple[str, frozenset[int]]:
    masked, continuations, offset = [], set(), 0
    for number, physical in enumerate(text.splitlines(keepends=True)):
        if check_cancel and number % 128 == 0:
            check_cancel()
        line = physical.rstrip("\r\n")
        ending = physical[len(line):]
        if line.strip() and len(line) < 7:
            raise CobolLexError("fixed_line_missing_indicator", offset)
        indicator = line[6:7]
        if indicator in {"D", "d"}:
            raise CobolLexError("debug_line_requires_compilation_context", offset + 6)
        if indicator not in {"", " ", "*", "/", "-"}:
            raise CobolLexError("fixed_indicator_not_supported", offset + 6)
        if indicator == "-":
            continuations.add(offset)
        if indicator in {"*", "/"}:
            masked.append(" " * len(line) + ending)
        else:
            masked.append(" " * min(7, len(line)) + line[7:72] + " " * max(0, len(line) - 72) + ending)
        offset += len(physical)
    return "".join(masked), frozenset(continuations)


def lex_cobol(source: str, *, source_format: str = "free", max_tokens: int = 50000, check_cancel=None) -> tuple[CobolToken, ...]:
    """Tokenize with original offsets; quoted words never become executable syntax."""
    if source_format not in {"free", "fixed"}:
        raise ValueError("source format must be free or fixed")
    if check_cancel:
        check_cancel()
    text, continuations = _masked_fixed(source, check_cancel) if source_format == "fixed" else (source, frozenset())
    tokens, position, next_check = [], 0, 4096
    while position < len(text):
        if check_cancel and position >= next_check:
            check_cancel()
            next_check = position + 4096
        char = text[position]
        if char.isspace():
            position += 1
            continue
        if text.startswith("*>", position):
            while position < len(text) and text[position] not in "\r\n":
                if check_cancel and position >= next_check:
                    check_cancel()
                    next_check = position + 4096
                position += 1
            continue
        start = position
        user_word = _LEX_WORD.match(text, position)
        numeric_word = bool(user_word and re.search(r"[A-Z_$#@]", user_word[0], re.I)
                            and not (char.isdigit() and (candidate_number := _LEX_NUMBER.match(text, position))
                                     and candidate_number.end() >= user_word.end()))
        if char in "'\"":
            quote, value = char, char
            position += 1
            while position < len(text):
                if check_cancel and position >= next_check:
                    check_cancel()
                    next_check = position + 4096
                if text[position] in "\r\n":
                    newline = re.match(r"\r\n|\r|\n", text[position:])[0]
                    following = position + len(newline)
                    if following not in continuations:
                        raise CobolLexError("continued_literal_not_supported", start)
                    position = following + min(7, len(text) - following)
                    while position < len(text) and text[position] in " \t":
                        position += 1
                    if position >= len(text) or text[position] != quote:
                        raise CobolLexError("continued_literal_requires_quote", start)
                    position += 1
                    continue
                value += text[position]
                if text[position] == quote:
                    position += 1
                    if position < len(text) and text[position] == quote:
                        value += quote
                        position += 1
                        continue
                    break
                position += 1
            else:
                raise CobolLexError("unterminated_literal", start)
            tokens.append(CobolToken(value, "literal", start, position))
        elif numeric_word:
            position = user_word.end()
            tokens.append(CobolToken(user_word[0].upper(), "word", start, position))
        elif char.isdigit() and (number := _LEX_NUMBER.match(text, position)):
            position = number.end()
            if position < len(text) and (text[position].isalnum() or text[position] in "_$#@-"):
                raise CobolLexError("numeric_or_identifier_form_not_supported", start)
            tokens.append(CobolToken(number[0], "number", start, position))
        elif word := _LEX_WORD.match(text, position):
            position = word.end()
            tokens.append(CobolToken(word[0].upper(), "word", start, position))
        elif text[position:position + 2] in {"**", "<=", ">=", "<>"}:
            position += 2
            tokens.append(CobolToken(text[start:position], "symbol", start, position))
        elif char in ".=<>+-*/():,":
            position += 1
            tokens.append(CobolToken(char, "symbol", start, position))
        else:
            raise CobolLexError("unrecognized_source_character", start)
        if len(tokens) > max_tokens:
            raise CobolLexError("token_budget_exhausted", start)
    return tuple(tokens)


def cobol_code_only(text: str) -> str:
    """Mask comments and literals while retaining physical offsets."""
    result = list(text)
    quote, index = None, 0
    while index < len(text):
        start = index
        if text.startswith("*>", index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
        elif text[index] in "'\"":
            quote = text[index]
            index += 1
            while index < len(text):
                if text[index] == quote:
                    index += 1
                    if index < len(text) and text[index] == quote:
                        index += 1
                        continue
                    break
                index += 1
        else:
            index += 1
            continue
        for offset in range(start, index):
            if result[offset] not in "\r\n":
                result[offset] = " "
    return "".join(result).upper()


def program_identifiers(text: str, *, source_format: str = "free") -> tuple[tuple[str, int, int], ...]:
    """Find executable PROGRAM-ID headers without scanning literal contents.

    Only a header clause is tokenized, so later unsupported dialect characters
    or a truncated header prefix cannot erase an earlier complete declaration.
    Returned offsets refer to the supplied physical text, including fixed columns.
    """
    if source_format not in {"free", "fixed", "auto"}:
        raise ValueError("source format must be free, fixed or auto")
    normalized, active = [], source_format
    for raw in text.splitlines(keepends=True):
        line, ending = raw.rstrip("\r\n"), raw[len(raw.rstrip("\r\n")):]
        code, active, _ = clean_cobol_line(line, active)
        offset = line.find(code) if code else 0
        normalized.append(" " * max(0, offset) + code +
                          " " * max(0, len(line) - max(0, offset) - len(code)) + ending)
    source = "".join(normalized)
    hidden = cobol_code_only(source)
    result = []
    header = re.compile(r"(?<![A-Z0-9_$#@-])PROGRAM-ID\s*\.\s*", re.I)
    name_pattern = re.compile(r"'(?:''|[^'\r\n])*'|\"(?:\"\"|[^\"\r\n])*\"|[A-Z0-9_$#@-]+", re.I)
    for match in header.finditer(hidden):
        # Quoted names are masked in hidden. Locate the operand from the
        # actual header period rather than consuming masked literal whitespace.
        period = source.find(".", match.start(), match.end())
        before = hidden[:match.start()].rstrip()
        line_start = hidden.rfind("\n", 0, match.start()) + 1
        if hidden[line_start:match.start()].strip() and before and before[-1] != ".":
            continue
        operand_start = period + 1
        while operand_start < len(source) and source[operand_start].isspace():
            operand_start += 1
        name = name_pattern.match(source, operand_start)
        if name is None:
            continue
        # Avoid accepting a truncated bare name at the end of a header buffer.
        if name.end() == len(source) and name[0][:1] not in {"'", '"'}:
            continue
        try:
            tokens = lex_cobol(source[match.start():name.end()])
        except CobolLexError:
            continue
        if len(tokens) != 3 or tokens[0].value != "PROGRAM-ID" or tokens[1].value != ".":
            continue
        token = tokens[2]
        if token.kind not in {"word", "literal", "number"}:
            continue
        # The bare-name pattern stops before '+'. Do not accept the word
        # prefix of a scientific literal such as 71E+3 as a program name.
        if (token.kind == "word" and re.fullmatch(r"[0-9]+E", token.value, re.I)
                and re.match(r"\+[0-9]+", source[name.end():])):
            continue
        if token.kind == "number":
            # Exported program identifiers can consist entirely of digits.
            # Do not reinterpret a decimal prefix or scientific literal as one.
            if re.fullmatch(r"[0-9]+", token.value) is None:
                continue
            if (source[name.end():name.end() + 1] == "."
                    and source[name.end() + 1:name.end() + 2].isdigit()):
                continue
        value = token.value[1:-1].replace(token.value[0] * 2, token.value[0]) if token.kind == "literal" else token.value
        if value:
            result.append((value.upper(), match.start(), name.end()))
    return tuple(result)


def program_identifier(text: str, *, source_format: str = "free") -> str | None:
    identifiers = program_identifiers(text, source_format=source_format)
    return identifiers[0][0] if identifiers else None


@dataclass(frozen=True)
class DataRef:
    name: str
    qualifiers: tuple[str, ...] = ()
    subscripts: tuple[str, ...] = ()
    reference_modification: tuple[str, str] | None = None
    start: int = 0
    end: int = 0

    @property
    def identity(self) -> str:
        result = self.name + "".join(" OF " + value for value in self.qualifiers)
        if self.subscripts:
            result += "(" + ", ".join(self.subscripts) + ")"
        if self.reference_modification:
            result += "(" + ":".join(self.reference_modification) + ")"
        return result


def identifier_token(token: CobolToken) -> bool:
    return token.kind == "word" and token.value not in COBOL_RESERVED


def _token_text(tokens: tuple[CobolToken, ...]) -> str:
    result = " ".join(token.value for token in tokens)
    return re.sub(r"\s*([(),:])\s*", r"\1", result)


def parse_data_ref(tokens: tuple[CobolToken, ...], position: int = 0) -> tuple[DataRef | None, int]:
    start = position
    if position >= len(tokens) or not identifier_token(tokens[position]):
        return None, position
    name, qualifiers, subscripts, modification = tokens[position].value, [], [], None
    position += 1
    # Qualification identifies storage. Qualifiers are not independent reads.
    while position + 1 < len(tokens) and tokens[position].value in {"OF", "IN"} and identifier_token(tokens[position + 1]):
        qualifiers.append(tokens[position + 1].value)
        position += 2
    if position < len(tokens) and tokens[position].value == "(":
        depth, opening = 1, position
        position += 1
        while position < len(tokens) and depth:
            depth += (tokens[position].value == "(") - (tokens[position].value == ")")
            position += 1
        if depth:
            return None, start
        inside = tokens[opening + 1:position - 1]
        parts = _token_text(inside)
        if ":" in parts:
            first, second = parts.split(":", 1)
            modification = (first, second)
        else:
            subscripts = [part.strip() for part in parts.split(",") if part.strip()]
        while position + 1 < len(tokens) and tokens[position].value in {"OF", "IN"} and identifier_token(tokens[position + 1]):
            qualifiers.append(tokens[position + 1].value)
            position += 2
    return DataRef(name, tuple(qualifiers), tuple(subscripts), modification,
                   tokens[start].start, tokens[position - 1].end), position


def references_from_tokens(tokens: tuple[CobolToken, ...]) -> tuple[DataRef, ...]:
    result, seen, position = [], set(), 0
    while position < len(tokens):
        reference, following = parse_data_ref(tokens, position)
        if reference is None:
            position += 1
            continue
        if reference.identity not in seen:
            result.append(reference)
            seen.add(reference.identity)
        position = following
    return tuple(result)


def data_references(text: str) -> tuple[DataRef, ...]:
    try:
        return references_from_tokens(lex_cobol(text))
    except CobolLexError:
        return ()


@dataclass(frozen=True)
class AssignmentFacts:
    kind: str
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    expressions: tuple[tuple[str, str], ...]
    rounded: bool = False
    diagnostics: tuple[str, ...] = ()
    effects_unknown: bool = False


def assignment_facts(text: str) -> AssignmentFacts | None:
    """Parse a bounded assignment header, shared by both indexes and the AST.

    Arithmetic operands and receivers have different roles. Exception clauses
    retain a diagnostic and never publish an unconditional write fact.
    """
    try:
        tokens = lex_cobol(text)
    except CobolLexError:
        return None
    if not tokens or tokens[0].value not in {"MOVE", "COMPUTE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE"}:
        return None
    kind, body = tokens[0].value, tokens[1:]
    boundary = next((i for i, token in enumerate(body) if token.kind == "word" and (
        token.value in COBOL_VERBS | SCOPE_TERMINATORS | {"ELSE", "WHEN", "ON", "NOT", "AT", "INVALID"})
        or token.value == "."), len(body))
    tail, body = body[boundary:], body[:boundary]
    exceptional = bool(tail and tail[0].value in {"ON", "NOT", "AT", "INVALID"})
    tail_values = tuple(token.value for token in tail[:4])
    size_error = kind != "MOVE" and (tail_values[:3] == ("ON", "SIZE", "ERROR")
                                     or tail_values[:4] == ("NOT", "ON", "SIZE", "ERROR"))

    def operands(values, *, receivers=False):
        values, result, index, rounded = tuple(values), [], 0, False
        while index < len(values):
            token = values[index]
            if token.value in {",", "ROUNDED"}:
                rounded |= token.value == "ROUNDED"
                index += 1
                continue
            reference, following = parse_data_ref(values, index)
            if reference:
                result.append((reference.identity, reference.identity))
                index = following
            elif not receivers and (token.kind in {"number", "literal"} or token.value in FIGURATIVE_CONSTANTS):
                result.append((None, token.value))
                index += 1
            elif not receivers and token.value in {"+", "-"} and index + 1 < len(values) and values[index + 1].kind == "number":
                result.append((None, token.value + values[index + 1].value))
                index += 2
            else:
                return None, rounded
        return result, rounded

    def split(values, word):
        positions = [i for i, token in enumerate(values) if token.kind == "word" and token.value == word]
        if len(positions) != 1:
            return None
        index = positions[0]
        return values[:index], values[index + 1:]

    inputs, outputs, expressions, rounded = [], [], [], False
    if kind in {"MOVE", "COMPUTE"}:
        halves = split(body, "TO" if kind == "MOVE" else "=") if kind == "MOVE" else None
        if kind == "COMPUTE":
            equal = [i for i, token in enumerate(body) if token.value == "="]
            halves = (body[equal[0] + 1:], body[:equal[0]]) if len(equal) == 1 else None
        if not halves:
            return None
        source, targets = halves
        outputs, rounded = operands(targets, receivers=True)
        if not outputs or not source:
            return None
        if kind == "MOVE":
            inputs, _ = operands(source)
            if inputs is None or len(inputs) != 1:
                return None
        else:
            inputs = [(reference.identity, reference.identity) for reference in references_from_tokens(source)]
        # Preserve source spacing where it is meaningful, but remove physical
        # comments from the executable expression supplied to consumers.
        expression = text[source[0].start:source[-1].end]
        expression = " ".join(strip_inline_comment(line).strip() for line in expression.splitlines())
        expressions = [(target, expression) for target, _ in outputs]
    else:
        divider = {"ADD": "TO", "SUBTRACT": "FROM", "MULTIPLY": "BY", "DIVIDE": "INTO"}[kind]
        if kind == "DIVIDE" and any(token.value == "BY" for token in body):
            divider = "BY"
        giving = split(body, "GIVING")
        header, receivers = giving if giving else (body, None)
        halves = split(header, divider)
        if not halves:
            if kind != "ADD" or receivers is None:
                return None
            halves = (header, ())
        left, right = operands(halves[0]), operands(halves[1], receivers=receivers is None)
        sources, _ = left
        bases, base_rounded = right
        if not sources or bases is None or (not bases and kind != "ADD"):
            return None
        if receivers is not None and bases and len(bases) != 1:
            return None
        if kind in {"MULTIPLY", "DIVIDE"} and len(sources) != 1:
            return None
        outputs, rounded = operands(receivers, receivers=True) if receivers is not None else (bases, base_rounded)
        if not outputs:
            return None
        inputs = sources + bases
        summed = " + ".join(value for _, value in sources)
        if len(sources) > 1:
            summed = "(" + summed + ")"
        for target, _ in outputs:
            base = " + ".join(value for _, value in bases) if giving else target
            if kind == "ADD":
                expression = summed + (" + " + base if base else "")
            elif kind == "SUBTRACT":
                expression = base + " - " + summed
            elif kind == "MULTIPLY":
                expression = summed + " * " + base
            else:
                expression = (base + " / " + summed) if divider == "INTO" else (summed + " / " + base)
            expressions.append((target, expression))
    reads = tuple(dict.fromkeys(name for name, _ in inputs if name))
    writes = tuple(dict.fromkeys(name for name, _ in outputs if name))
    index_reads = []
    for name in (*reads, *writes):
        for reference in data_references(name):
            for expression in (*reference.subscripts, *(reference.reference_modification or ())):
                index_reads.extend(item.identity for item in data_references(expression))
    reads = tuple(dict.fromkeys((*reads, *index_reads)))
    if exceptional:
        return AssignmentFacts(kind, reads, (), tuple(expressions), rounded,
                               ("exception_scope_requires_control_flow",
                                "arithmetic_size_error_clause" if size_error else "assignment_tail_not_supported"), True)
    return AssignmentFacts(kind, reads, writes, tuple(expressions), rounded)


def dependency_facts(text: str) -> tuple[tuple[str, str, str | None], ...]:
    """Static relation syntax derived from executable tokens, never literals."""
    try:
        tokens = lex_cobol(text)
    except CobolLexError:
        return ()
    result = []
    for index, token in enumerate(tokens[:-1]):
        if token.kind != "word":
            continue
        target = tokens[index + 1]
        if token.value in {"CALL", "COPY"} and (identifier_token(target) or target.kind == "literal"):
            relation = "INCLUDES_COPY" if token.value == "COPY" else "CALLS" if target.kind == "literal" else "CALL_TARGET_FROM"
            result.append((relation, target.value.strip("'\"").upper(), None))
        elif token.value == "PERFORM" and identifier_token(target):
            endpoint = (tokens[index + 3].value if index + 3 < len(tokens)
                        and tokens[index + 2].value in {"THRU", "THROUGH"}
                        and identifier_token(tokens[index + 3]) else None)
            result.append(("PERFORMS_THRU" if endpoint else "PERFORMS", target.value, endpoint))
    return tuple(dict.fromkeys(result))


_NAME = r"[A-Z][A-Z0-9_$#@-]*"
_LITERAL = r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\""
_CONDITION_TOKEN = re.compile(
    rf"{_LITERAL}|{_NAME}|[+-]?\d+(?:\.\d+)?|<=|>=|<>|[=<>()]",
    re.IGNORECASE,
)
_RELATION_WORDS = {
    "IS", "NOT", "EQUAL", "TO", "GREATER", "THAN", "LESS", "AND",
    "OR", "THEN", "NUMERIC", "ALPHABETIC", "ALPHABETIC-LOWER",
    "ALPHABETIC-UPPER", "POSITIVE", "NEGATIVE",
}
_CLASS_TESTS = {
    "NUMERIC", "ALPHABETIC", "ALPHABETIC-LOWER", "ALPHABETIC-UPPER",
    "POSITIVE", "NEGATIVE", "ZERO",
}


def sentence_terminated(text: str) -> bool:
    """Whether the final character is a separator period outside a literal."""

    text = text.rstrip()
    if not text.endswith("."):
        return False
    quote: str | None = None
    index = 0
    while index < len(text) - 1:
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in {"'", '"'}:
            quote = char
        index += 1
    return quote is None


def condition_syntax_supported(text: str, statement_words: set[str]) -> bool:
    """Recognize explicit simple conditions, not abbreviated or inline forms."""

    text = text.strip().upper()
    if text.endswith(" THEN"):
        text = text[:-5].rstrip()
    tokens: list[str] = []
    position = 0
    for match in _CONDITION_TOKEN.finditer(text):
        if text[position:match.start()].strip():
            return False
        tokens.append(match.group())
        position = match.end()
    if text[position:].strip() or not tokens or len(tokens) > 256:
        return False
    index = 0

    def operand() -> bool:
        nonlocal index
        if index >= len(tokens):
            return False
        token = tokens[index]
        if token in _RELATION_WORDS or token in statement_words:
            return False
        if token in {"=", "<", ">", "<=", ">=", "<>", "(", ")"}:
            return False
        index += 1
        return True

    def comparison() -> bool:
        nonlocal index
        if not operand():
            return False
        if index == len(tokens) or tokens[index] in {"AND", "OR", ")"}:
            # A single name can denote a level-88 condition. A lone literal or
            # number cannot, even though it is a legal comparison operand.
            return bool(re.fullmatch(_NAME, tokens[index - 1]))
        if tokens[index] == "IS":
            index += 1
        if index < len(tokens) and tokens[index] == "NOT":
            index += 1
        if index >= len(tokens):
            return False
        operator = tokens[index]
        index += 1
        if operator in _CLASS_TESTS:
            return True
        if operator in {"=", "<", ">", "<=", ">=", "<>"}:
            return operand()
        if operator == "EQUAL":
            if index < len(tokens) and tokens[index] == "TO":
                index += 1
            return operand()
        if operator in {"GREATER", "LESS"}:
            if index < len(tokens) and tokens[index] == "THAN":
                index += 1
            if tokens[index:index + 2] == ["OR", "EQUAL"]:
                index += 2
                if index < len(tokens) and tokens[index] == "TO":
                    index += 1
            return operand()
        return False

    def term(depth: int) -> bool:
        nonlocal index
        if depth > 32:
            return False
        if index < len(tokens) and tokens[index] == "NOT":
            index += 1
        if index < len(tokens) and tokens[index] == "(":
            index += 1
            if not expression(depth + 1):
                return False
            if index >= len(tokens) or tokens[index] != ")":
                return False
            index += 1
            return True
        return comparison()

    def expression(depth: int) -> bool:
        nonlocal index
        if not term(depth):
            return False
        while index < len(tokens) and tokens[index] in {"AND", "OR"}:
            index += 1
            if not term(depth):
                return False
        return True

    return expression(0) and index == len(tokens)


def sql_code_only(text: str) -> tuple[str, bool]:
    """Mask SQL literals/comments, preserving offsets and physical newlines."""

    result = list(text)
    index = 0
    while index < len(text):
        start = index
        if text.startswith("--", index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                return "", False
            index = end + 2
        elif text[index] in {"'", '"'}:
            quote = text[index]
            index += 1
            while index < len(text):
                if text[index] == quote:
                    if index + 1 < len(text) and text[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                return "", False
        else:
            index += 1
            continue
        for offset in range(start, index):
            if result[offset] != "\n":
                result[offset] = " "
    return "".join(result).upper(), True


def sql_host_access(code: str) -> tuple[list[str], list[str], bool]:
    """Return host reads/writes for a bounded set of static SQL statements.

    SELECT/FETCH INTO outputs include explicitly written indicator variables.
    Other host references in supported statements are inputs. SQL output
    expressions, dynamic SQL, host structures, and unrecognized forms abstain.
    """

    envelope = re.fullmatch(
        r"\s*EXEC\s+SQL\s+([\s\S]*?)\bEND-EXEC\s*\.?\s*", code,
    )
    if not envelope or re.search(r"\b(?:EXEC\s+SQL|END-EXEC)\b", envelope.group(1)):
        return [], [], False
    body = envelope.group(1)
    if ";" in body:
        return [], [], False
    verb_match = re.match(r"\s*([A-Z-]+)\b", body)
    if not verb_match:
        return [], [], False
    verb = verb_match.group(1)
    if verb not in {"SELECT", "FETCH", "INSERT", "UPDATE", "DELETE"}:
        return [], [], False

    host_pattern = re.compile(rf"(?<![:\w]):\s*({_NAME})\b")
    hosts = list(host_pattern.finditer(body))
    # A colon that is not a supported host reference must not silently become
    # an input. Qualified structures and array references require more parsing.
    masked_hosts = host_pattern.sub(lambda match: " " * len(match.group()), body)
    if ":" in masked_hosts or any(
        re.match(r"\s*[.(]", body[match.end():]) for match in hosts
    ):
        return [], [], False

    top_words: list[tuple[str, int, int]] = []
    depth = 0
    for token in re.finditer(r"[A-Z][A-Z0-9_$#@-]*|[()]", masked_hosts):
        value = token.group()
        if value == "(":
            depth += 1
        elif value == ")":
            depth -= 1
            if depth < 0:
                return [], [], False
        elif depth == 0:
            top_words.append((value, token.start(), token.end()))
    if depth:
        return [], [], False

    keywords = [word[0] for word in top_words]
    all_words = set(re.findall(r"[A-Z][A-Z0-9_$#@-]*", masked_hosts))
    if all_words & {"RETURNING", "OUTPUT", "RETURN", "DESCRIPTOR"}:
        return [], [], False
    # Only the initial statement verb is allowed at the outer level. INSERT
    # SELECT and dialect-specific DML output clauses deliberately abstain.
    if set(keywords[1:]) & {
        "INSERT", "UPDATE", "DELETE", "FETCH", "SELECT", "CALL", "EXECUTE",
        "MERGE", "BEGIN", "END", "UNION", "EXCEPT", "INTERSECT",
    }:
        return [], [], False
    table = rf"{_NAME}(?:\.{_NAME})?"
    if verb == "INSERT":
        shape = re.match(
            rf"\s*INSERT\s+INTO\s+{table}(?:\s*\([^()]+\))?\s+VALUES\s*\(", body,
        )
        if not shape or not body.rstrip().endswith(")"):
            return [], [], False
        if any(word[0] not in {"INSERT", "INTO", "VALUES"}
               for word in top_words if word[1] >= shape.end()):
            return [], [], False
    elif verb == "UPDATE":
        shape = re.match(rf"\s*UPDATE\s+{table}\s+SET\s+", body)
        if not shape or "=" not in body[shape.end():].split("WHERE", 1)[0]:
            return [], [], False
        if "INTO" in keywords or "VALUES" in keywords:
            return [], [], False
    elif verb == "DELETE":
        shape = re.match(rf"\s*DELETE\s+FROM\s+{table}(?=\s|$)", body)
        if not shape:
            return [], [], False
        remainder = body[shape.end():].strip()
        if remainder and not re.match(r"WHERE\s+\S", remainder):
            return [], [], False
        if "INTO" in keywords or "VALUES" in keywords:
            return [], [], False
    elif verb == "SELECT":
        from_words = [word for word in top_words if word[0] == "FROM"]
        if len(from_words) != 1 or not re.match(
            rf"\s*(?:{table}|\()", body[from_words[0][2]:],
        ):
            return [], [], False
        projection_end = min(
            [word[1] for word in top_words if word[0] in {"INTO", "FROM"}],
        )
        if not body[verb_match.end():projection_end].strip():
            return [], [], False
    elif verb == "FETCH":
        if not re.match(rf"\s*FETCH\s+{_NAME}\s+INTO\b", body):
            return [], [], False

    output_start = output_end = -1
    if verb in {"SELECT", "FETCH"}:
        into_words = [word for word in top_words if word[0] == "INTO"]
        if len(into_words) > 1:
            return [], [], False
        if into_words:
            output_start = into_words[0][2]
            if verb == "SELECT":
                from_words = [
                    word for word in top_words
                    if word[0] == "FROM" and word[1] > output_start
                ]
                if not from_words:
                    return [], [], False
                output_end = from_words[0][1]
            else:
                output_end = len(body)
            outputs = body[output_start:output_end]
            residue = host_pattern.sub(" ", outputs)
            residue = re.sub(r"\bINDICATOR\b", " ", residue)
            if not host_pattern.search(outputs) or residue.replace(",", " ").strip():
                return [], [], False
        elif verb == "FETCH":
            return [], [], False

    reads: list[str] = []
    writes: list[str] = []
    for match in hosts:
        destination = (
            writes if output_start <= match.start() < output_end else reads
        )
        name = match.group(1)
        if name not in destination:
            destination.append(name)
    return reads, writes, True


def io_clause_prefix(text):
    """Return a complete I/O clause introducer from lexical tokens only.

    NOT ON SIZE ERROR belongs to its arithmetic operation. An incomplete or
    unknown prefix is deliberately not classified as a complete I/O clause.
    """
    try:
        tokens = lex_cobol(text)
    except CobolLexError:
        return None
    values = tuple(token.value if token.kind == "word" else None for token in tokens[:3])
    for prefix in (("NOT", "INVALID", "KEY"), ("NOT", "AT", "END"),
                   ("INVALID", "KEY"), ("AT", "END")):
        if values[:len(prefix)] == prefix:
            return " ".join(prefix)
    return None


def io_clause_guards(lines, *, include_operation_scopes=False):
    """Retain explicit I/O clause scopes using the shared lexical tokens.

    Only an explicitly closed READ/WRITE/REWRITE/DELETE/START is used. Literal
    keywords, comments, an excerpt's open tail and nested operations cannot
    create an unqualified error branch.
    """
    lines = tuple(lines)
    if any(line.text.lstrip().startswith(">>") for line in lines):
        return [{"kind": "io_scope_unknown", "start_line": min(line.start_line for line in lines),
                 "end_line": max(line.end_line for line in lines)}] if include_operation_scopes else []
    text = "\n".join(line.text for line in lines)
    offsets, offset = [], 0
    for line in lines:
        offsets.append((offset, line.start_line))
        offset += len(line.text) + 1
    from bisect import bisect_right
    starts = [start for start, _ in offsets]
    def line_at(position):
        return offsets[max(0, bisect_right(starts, position) - 1)][1]
    try:
        tokens = lex_cobol(text)
    except CobolLexError:
        return ([{"kind": "io_scope_unknown", "start_line": min(line.start_line for line in lines),
                  "end_line": max(line.end_line for line in lines), "reason": "source_lexical_boundary"}]
                if include_operation_scopes else [])
    stack, result = [], []
    verbs = {"READ", "WRITE", "REWRITE", "DELETE", "START"}
    for index, token in enumerate(tokens):
        word = token.value if token.kind != "literal" else ""
        if word in verbs:
            stack.append({"verb": word, "start": line_at(token.start), "token_index": index,
                          "header": None, "clause": None, "segments": [], "ambiguous_lines": set()})
        elif stack and (word == "END-" + stack[-1]["verb"] or word == "."):
            current = stack.pop()
            if current["clause"]:
                current["segments"].append((current["clause"], line_at(token.start) - 1))
            for (clause, first), last in current["segments"]:
                if first <= last:
                    result.append({"kind": "io_clause", "condition": clause, "start_line": first,
                                   "end_line": last, "operation": current["verb"],
                                   "operation_text": current["header"], "operation_line": current["start"]})
            if include_operation_scopes:
                result.append({"kind": "io_operation_scope", "operation_line": current["start"],
                               "start_line": current["start"], "end_line": line_at(token.start), "closed": True,
                               "ambiguous_lines": sorted(current["ambiguous_lines"])})
            if word == ".":
                if include_operation_scopes:
                    result.extend({"kind": "io_scope_unknown", "start_line": outer["start"],
                                   "end_line": line_at(token.start), "reason": "implicit_nested_scope_not_mapped"}
                                  for outer in stack)
                stack.clear()
        elif stack:
            values = [item.value if item.kind != "literal" else "" for item in tokens[index:index + 4]]
            clause = None
            if values[:3] in (["NOT", "INVALID", "KEY"], ["NOT", "AT", "END"]):
                clause = " ".join(values[:3])
            elif values[:2] in (["INVALID", "KEY"], ["AT", "END"]) and not (
                    index and tokens[index - 1].value == "NOT"):
                clause = " ".join(values[:2])
            if clause:
                current = stack[-1]
                if current["header"] is None:
                    current["header"] = " ".join(item.value for item in tokens[current["token_index"]:index])
                if current["clause"]:
                    if index and line_at(tokens[index - 1].end - 1) == line_at(token.start):
                        current["ambiguous_lines"].add(line_at(token.start))
                    current["segments"].append((current["clause"], line_at(token.start) - 1))
                current["clause"] = (clause, line_at(token.start))
    if include_operation_scopes:
        result.extend({"kind": "io_operation_scope", "operation_line": current["start"],
                       "start_line": current["start"], "end_line": max(line.end_line for line in lines),
                       "closed": False} for current in stack)
    return result
