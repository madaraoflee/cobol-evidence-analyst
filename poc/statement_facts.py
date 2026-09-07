"""Small, deliberately bounded syntax recognizers for structural facts.

These helpers classify source syntax only. They do not prove runtime values,
execution paths, database column lineage, or complete language semantics.
"""

from __future__ import annotations

import re


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
