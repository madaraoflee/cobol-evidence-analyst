#!/usr/bin/env python3
"""Bounded local CFG for an explicit, structured COBOL statement subset.

Edges describe possible source control alternatives, never executed paths.
Unsupported syntax terminates at a boundary instead of disappearing from flow.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

from call_bindings import parse_call
from call_contexts import _Facts, _id


CFG_VERSION = "bounded-exception-cfg-v0.1"
MAX_TOKENS = 100_000
MAX_SYNTAX_DEPTH = 64
MAX_STATEMENTS = 20_000
MAX_BUILD_DEPTH = 128
_NAME = re.compile(r"[A-Z][A-Z0-9_$#@-]{0,63}\Z")
_NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?\Z")
_LEXER = re.compile(r"\s+|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\d+(?:\.\d+)?|[A-Z][A-Z0-9_$#@-]*|\*\*|<>|>=|<=|[=<>+*/().,-]", re.I)
_VERBS = frozenset({"MOVE", "IF", "ELSE", "END-IF", "CALL", "END-CALL", "COMPUTE", "END-COMPUTE",
    "PERFORM", "END-PERFORM", "GOBACK", "STOP", "EXIT", "CONTINUE", "EVALUATE", "END-EVALUATE",
    "WHEN", "EXEC", "END-EXEC", "GO", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE", "INITIALIZE", "SET",
    "ACCEPT", "DISPLAY", "OPEN", "CLOSE", "READ", "WRITE", "REWRITE", "DELETE", "START", "STRING",
    "UNSTRING", "INSPECT", "SEARCH", "NEXT", "ALTER", "CANCEL", "ENTRY", "RETURN", "RELEASE",
    "SORT", "MERGE", "COPY", "REPLACE", "ON", "NOT", "END", "END-PROGRAM"})


@dataclass(frozen=True)
class _Token:
    text: str
    unit: dict
    offset: int


class _SyntaxBoundary(ValueError):
    def __init__(self, reason: str, token: _Token):
        self.reason = reason
        self.token = token


def _tokenize(units: list[dict]) -> list[_Token]:
    tokens: list[_Token] = []
    for unit in units:
        source = unit["normalized_text"]
        offset = 0
        while offset < len(source):
            match = _LEXER.match(source, offset)
            if match is None:
                tokens.append(_Token("@UNSUPPORTED", unit, offset))
                # Preserve the first unknown position and stop interpreting the
                # remainder of this statement as valid COBOL syntax.
                break
            if not match.group().isspace():
                value = match.group()
                tokens.append(_Token(value if value[:1] in {"'", '"'} else value.upper(), unit, offset))
                if len(tokens) > MAX_TOKENS:
                    raise ValueError("Procedure exceeds the bounded CFG token budget.")
            offset = match.end()
    return tokens


class _Parser:
    def __init__(self, tokens: list[_Token], fields: set[str]):
        self.tokens = tokens
        self.fields = fields
        self.position = 0

    def at(self, *words: str) -> bool:
        return [token.text for token in self.tokens[self.position:self.position + len(words)]] == list(words)

    def take(self, *words: str) -> bool:
        if self.at(*words):
            self.position += len(words)
            return True
        return False

    def error(self, reason: str, token: _Token | None = None) -> None:
        if token is None:
            token = self.tokens[min(self.position, len(self.tokens) - 1)]
        raise _SyntaxBoundary(reason, token)

    def scalar(self, *, field_only: bool = False) -> str:
        if self.position >= len(self.tokens):
            self.error("incomplete_scalar_operand")
        token = self.tokens[self.position]
        sign = ""
        if token.text in {"+", "-"} and not field_only:
            sign = token.text
            self.position += 1
            if self.position >= len(self.tokens):
                self.error("incomplete_numeric_operand", token)
            token = self.tokens[self.position]
        value = sign + token.text
        if (field_only and value not in self.fields) or (not field_only and not (
                value in self.fields or _NUMBER.fullmatch(value) or value in {"ZERO", "ZEROS", "ZEROES"})):
            self.error("operand_form_or_field_not_supported", token)
        self.position += 1
        return value

    def sequence(self, stops: tuple[tuple[str, ...], ...] = (), depth: int = 0) -> list[dict]:
        result: list[dict] = []
        while self.position < len(self.tokens):
            if any(self.at(*stop) for stop in stops):
                break
            token = self.tokens[self.position]
            if token.text == ".":
                if stops:
                    self.error("implicit_scope_termination_not_supported", token)
                self.position += 1
                continue
            if depth >= MAX_SYNTAX_DEPTH:
                self.error("syntax_depth_budget_exhausted", token)
            result.append(self.statement(depth))
        return result

    def branch_body(self, stops: tuple[tuple[str, ...], ...], depth: int) -> list[dict]:
        start = self.position
        result = self.sequence(stops, depth + 1)
        if self.position == start:
            self.error("empty_imperative_branch_not_supported")
        return result

    def statement(self, depth: int) -> dict:
        token = self.tokens[self.position]
        self.position += 1
        kind = token.text
        node = {"kind": kind, "token": token}
        if kind == "MOVE":
            source = self.scalar()
            if not self.take("TO"):
                self.error("move_form_not_supported", token)
            targets: list[str] = []
            while self.position < len(self.tokens) and self.tokens[self.position].text not in _VERBS | {"."}:
                targets.append(self.scalar(field_only=True))
                if len(targets) > 64:
                    self.error("move_target_budget_exhausted", token)
            if not targets:
                self.error("move_form_not_supported", token)
            return node | {"source": source, "targets": targets}
        if kind == "IF":
            field = self.scalar(field_only=True)
            negate = self.take("NOT")
            if self.take("EQUAL"):
                self.take("TO")
                operator = "="
            elif self.position < len(self.tokens) and self.tokens[self.position].text in {"=", "<>", ">", ">=", "<", "<="}:
                operator = self.tokens[self.position].text
                self.position += 1
            else:
                self.error("condition_form_not_supported", token)
            if negate:
                operator = {"=": "<>", "<>": "=", ">": "<=", ">=": "<", "<": ">=", "<=": ">"}[operator]
            value = self.scalar()
            self.take("THEN")
            true_body = self.branch_body((("ELSE",), ("END-IF",)), depth)
            false_body = self.branch_body((("END-IF",),), depth) if self.take("ELSE") else []
            if not self.take("END-IF"):
                self.error("if_explicit_terminator_missing", token)
            return node | {"field": field, "operator": operator, "value": value,
                           "true_body": true_body, "false_body": false_body}
        if kind in {"CALL", "COMPUTE"}:
            return self.exception_statement(node, depth)
        if kind == "PERFORM":
            if token.unit["name"] != "PERFORM" or token.offset != 0 or self.position >= len(self.tokens):
                self.error("perform_form_not_supported", token)
            target = self.tokens[self.position].text
            if not _NAME.fullmatch(target) or target in _VERBS:
                self.error("perform_form_not_supported", token)
            self.position += 1
            end = target
            if self.take("THRU") or self.take("THROUGH"):
                if self.position >= len(self.tokens) or not _NAME.fullmatch(self.tokens[self.position].text):
                    self.error("perform_range_not_supported", token)
                end = self.tokens[self.position].text
                self.position += 1
            if self.position < len(self.tokens) and self.tokens[self.position].text not in _VERBS | {"."}:
                self.error("perform_form_not_supported", token)
            return node | {"target_paragraph": target, "end_paragraph": end}
        if kind == "GOBACK":
            if self.position < len(self.tokens) and self.tokens[self.position].text not in _VERBS | {"."}:
                self.error("goback_form_not_supported", token)
            return node
        if kind == "STOP":
            if not self.take("RUN"):
                self.error("stop_form_not_supported", token)
            if self.position < len(self.tokens) and self.tokens[self.position].text not in _VERBS | {"."}:
                self.error("stop_form_not_supported", token)
            return node | {"kind": "GOBACK", "exit_kind": "stop_run"}
        if kind in {"EXIT", "CONTINUE"}:
            if self.position < len(self.tokens) and self.tokens[self.position].text not in _VERBS | {"."}:
                self.error("exit_variant_not_supported", token)
            return node | {"kind": "EXIT", "exit_kind": "paragraph_noop"}
        self.error("statement_form_not_supported", token)

    def exception_statement(self, node: dict, depth: int) -> dict:
        token = node["token"]
        kind = node["kind"]
        terminal = "END-CALL" if kind == "CALL" else "END-COMPUTE"
        clause = ("EXCEPTION",) if kind == "CALL" else ("SIZE", "ERROR")
        header: list[str] = []
        while self.position < len(self.tokens):
            text = self.tokens[self.position].text
            if text in _VERBS | {"."}:
                break
            header.append(text)
            self.position += 1
        if kind == "CALL":
            if token.unit["name"] != "CALL" or token.offset != 0:
                self.error("callsite_not_independently_indexed", token)
            try:
                form = parse_call("CALL " + " ".join(header))
            except ValueError:
                self.error("call_form_not_supported", token)
            if any(parameter.name not in self.fields for parameter in form.parameters):
                self.error("call_argument_scope_not_supported", token)
            if form.dynamic and form.target not in self.fields:
                self.error("call_target_scope_not_supported", token)
            node |= {"callsite_id": token.unit["unit_id"], "target": form.target,
                     "dynamic_target": form.dynamic,
                     "passing_parameters": [{"name": item.name, "mode": item.mode} for item in form.parameters]}
        else:
            if not header or header[0] not in self.fields:
                self.error("compute_receiver_not_supported", token)
            target = header.pop(0)
            rounded = bool(header and header[0] == "ROUNDED")
            if rounded:
                header.pop(0)
            if not header or header.pop(0) != "=":
                self.error("compute_form_not_supported", token)
            if not self.arithmetic(header):
                self.error("compute_expression_not_supported", token)
            node |= {"target": target, "rounded": rounded, "expression": " ".join(header)}
        error_body: list[dict] | None = None
        normal_body: list[dict] | None = None
        stops = (("ON", *clause), ("NOT", "ON", *clause), (terminal,))
        if self.take("ON", *clause):
            error_body = self.branch_body(stops, depth)
        if self.take("NOT", "ON", *clause):
            normal_body = self.branch_body(((terminal,),), depth)
        explicit_end = self.take(terminal)
        if (error_body is not None or normal_body is not None) and not explicit_end:
            self.error("exception_explicit_terminator_missing", token)
        if not explicit_end and self.position < len(self.tokens) and not self.at(".") and self.tokens[self.position].text not in _VERBS:
            self.error("exception_statement_scope_not_supported", token)
        if kind == "COMPUTE":
            node["size_error_receiver_preserved"] = error_body is not None
        return node | {"error_body": error_body, "normal_body": normal_body,
                       "has_error_handler": error_body is not None, "has_normal_handler": normal_body is not None}

    def arithmetic(self, tokens: list[str]) -> bool:
        if not tokens or len(tokens) > 1024:
            return False
        cursor = 0

        def atom(depth: int) -> bool:
            nonlocal cursor
            if depth > 64 or cursor >= len(tokens):
                return False
            if tokens[cursor] in {"+", "-"}:
                cursor += 1
                return atom(depth + 1)
            if tokens[cursor] == "(":
                cursor += 1
                if not expression(depth + 1) or cursor >= len(tokens) or tokens[cursor] != ")":
                    return False
                cursor += 1
                return True
            if tokens[cursor] in self.fields or _NUMBER.fullmatch(tokens[cursor]):
                cursor += 1
                return True
            return False

        def expression(depth: int) -> bool:
            nonlocal cursor
            if not atom(depth):
                return False
            while cursor < len(tokens) and tokens[cursor] in {"+", "-", "*", "/", "**"}:
                cursor += 1
                if not atom(depth):
                    return False
            return True

        return expression(0) and cursor == len(tokens)

    def parse(self) -> list[dict]:
        result = []
        while self.position < len(self.tokens):
            if self.take("."):
                continue
            token = self.tokens[self.position]
            try:
                result.append(self.statement(0))
            except _SyntaxBoundary as error:
                # Do not execute a partially parsed IF or handler. The entire
                # unsupported structured statement terminates this paragraph.
                result.append({"kind": "BOUNDARY", "token": token, "reason": error.reason,
                               "problem_token": error.token})
                break
        return result


class _BudgetBoundary(ValueError):
    pass


class _Builder:
    def __init__(self, facts: _Facts, program: str, blocks: list[dict], *, max_nodes: int,
                 max_edges: int, max_perform_depth: int):
        self.facts, self.program, self.blocks = facts, program, blocks
        self.max_nodes, self.max_edges, self.max_perform_depth = max_nodes, max_edges, max_perform_depth
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        self.boundaries: list[dict] = []
        self.paragraphs: dict[str, list[int]] = {}
        for index, block in enumerate(blocks):
            if block["name"]:
                self.paragraphs.setdefault(block["name"], []).append(index)

    def node(self, kind: str, token: _Token | None, chain: tuple[str, ...], **details: object) -> str:
        if len(self.nodes) >= self.max_nodes:
            raise _BudgetBoundary("node_budget_exhausted")
        statement_id = token.unit["unit_id"] if token else None
        evidence_refs = self.facts.refs(token.unit["evidence_id"]) if token else []
        node_id = _id("cfg", self.facts.snapshot_id, self.program, len(self.nodes), *chain)
        self.nodes.append({"node_id": node_id, "kind": kind, "statement_id": statement_id,
                           "instance_chain": list(chain), "evidence_refs": evidence_refs, **details})
        return node_id

    def connect(self, incoming: list[tuple[str, str]], target: str) -> None:
        for source, outcome in incoming:
            if len(self.edges) >= self.max_edges:
                raise _BudgetBoundary("edge_budget_exhausted")
            self.edges.append({"source": source, "target": target, "outcome": outcome})

    def boundary(self, reason: str, incoming: list[tuple[str, str]], token: _Token | None,
                 chain: tuple[str, ...], **details: object) -> list[tuple[str, str]]:
        node_id = self.node("BOUNDARY", token, chain, reason=reason, **details)
        self.connect(incoming, node_id)
        self.boundaries.append({"reason": reason, "node_id": node_id,
                                "evidence_refs": self.nodes[-1]["evidence_refs"]})
        return []

    def sequence(self, statements: list[dict], incoming: list[tuple[str, str]], chain: tuple[str, ...],
                 active: tuple[tuple[int, int], ...], build_depth: int = 0) -> list[tuple[str, str]]:
        if build_depth >= MAX_BUILD_DEPTH:
            raise _BudgetBoundary("combined_control_depth_budget_exhausted")
        tails = incoming
        for statement in statements:
            if not tails:
                break
            kind, token = statement["kind"], statement["token"]
            if kind == "BOUNDARY":
                tails = self.boundary(statement["reason"], tails, token, chain)
                continue
            if kind == "PERFORM":
                starts = self.paragraphs.get(statement["target_paragraph"], [])
                ends = self.paragraphs.get(statement["end_paragraph"], [])
                if len(starts) != 1 or len(ends) != 1 or starts[0] > ends[0]:
                    tails = self.boundary("perform_range_not_uniquely_resolved", tails, token, chain)
                    continue
                start, end = starts[0], ends[0]
                if len(chain) >= self.max_perform_depth:
                    tails = self.boundary("perform_depth_budget_exhausted", tails, token, chain)
                    continue
                if any(not (end < left or start > right) for left, right in active):
                    tails = self.boundary("recursive_or_overlapping_perform_not_expanded", tails, token, chain)
                    continue
                entry = self.node("JOIN", token, chain, role="perform_entry",
                                  target_paragraph=statement["target_paragraph"], end_paragraph=statement["end_paragraph"])
                self.connect(tails, entry)
                child_chain = (*chain, token.unit["unit_id"])
                tails = [(entry, "next")]
                for block in self.blocks[start:end + 1]:
                    tails = self.sequence(block["statements"], tails, child_chain, (*active, (start, end)), build_depth + 1)
                    if not tails:
                        break
                if tails:
                    returned = self.node("JOIN", token, chain, role="perform_return")
                    self.connect(tails, returned)
                    tails = [(returned, "next")]
                continue
            details = {key: value for key, value in statement.items()
                       if key not in {"kind", "token", "true_body", "false_body", "error_body", "normal_body"}}
            node_id = self.node(kind, token, chain, **details)
            self.connect(tails, node_id)
            if kind == "GOBACK":
                tails = []
            elif kind == "IF":
                first = self.sequence(statement["true_body"], [(node_id, "true")], chain, active, build_depth + 1)
                second = self.sequence(statement["false_body"], [(node_id, "false")], chain, active, build_depth + 1)
                tails = self.join(first + second, token, chain, "if_join")
            elif kind in {"CALL", "COMPUTE"}:
                outcome = "exception" if kind == "CALL" else "size_error"
                normal = self.sequence(statement["normal_body"] or [], [(node_id, "normal")], chain, active, build_depth + 1)
                if statement["error_body"] is None:
                    abnormal = self.boundary("unhandled_call_exception" if kind == "CALL" else "unhandled_size_error",
                        [(node_id, outcome)], token, chain)
                else:
                    abnormal = self.sequence(statement["error_body"], [(node_id, outcome)], chain, active, build_depth + 1)
                tails = self.join(normal + abnormal, token, chain, "exception_join")
            else:
                tails = [(node_id, "next")]
        return tails

    def join(self, tails: list[tuple[str, str]], token: _Token, chain: tuple[str, ...], role: str) -> list[tuple[str, str]]:
        if not tails:
            return []
        joined = self.node("JOIN", token, chain, role=role)
        self.connect(tails, joined)
        return [(joined, "next")]

    def build(self) -> str:
        entry = self.node("ENTRY", None, ())
        tails = [(entry, "next")]
        for block in self.blocks:
            tails = self.sequence(block["statements"], tails, (), ())
            if not tails:
                break
        if tails:
            terminal = self.node("EXIT", None, (), exit_kind="program_fallthrough")
            self.connect(tails, terminal)
        return entry


def build_exception_cfg(database_path: Path, program_name: str, *, max_nodes: int = 1000,
                        max_edges: int = 3000, max_perform_depth: int = 8) -> dict[str, object]:
    """Build source alternatives for supported explicit local control structures."""
    if not isinstance(program_name, str) or not _NAME.fullmatch(program_name):
        raise ValueError("Program name must be an uppercase, unqualified identifier.")
    if any(type(value) is not int or not minimum <= value <= maximum for value, minimum, maximum in (
            (max_nodes, 1, 20_000), (max_edges, 0, 60_000), (max_perform_depth, 0, 32))):
        raise ValueError("CFG budgets are outside supported bounds.")
    path = Path(database_path).expanduser().resolve()
    connection = sqlite3.connect(f"file:{quote(path.as_posix(), safe='/')}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        facts = _Facts(connection)
        if program_name not in facts.programs:
            raise ValueError("Program is not present in the stored source snapshot.")
        scope_reason = facts.scope_reason(program_name)
        blocks: list[dict] = [{"name": None, "statements": []}]
        if scope_reason:
            builder = _Builder(facts, program_name, [], max_nodes=max_nodes, max_edges=max_edges,
                               max_perform_depth=max_perform_depth)
            builder.boundary(scope_reason, [], None, ())
            entry = builder.nodes[0]["node_id"]
        else:
            signatures = facts.signatures[program_name]
            if len(signatures) != 1:
                raise ValueError("CFG requires a unique indexed procedure signature.")
            facts.verify_unit(signatures[0])
            procedure_start = signatures[0]["end_line"]
            program_path = facts.programs[program_name][0]["relative_path"]
            units = sorted((unit for unit in facts.units.values()
                            if unit["program_name"] == program_name and unit["relative_path"] == program_path
                            and unit["start_line"] > procedure_start
                            and unit["unit_type"] in {"Statement", "Paragraph", "Section"}),
                           key=lambda unit: (unit["start_line"], unit["end_line"], unit["unit_id"]))
            if len(units) > MAX_STATEMENTS:
                raise ValueError("Procedure exceeds the bounded CFG statement budget.")
            field_counts = Counter(symbol["name"] for symbol in facts.symbols.values()
                                   if symbol["program_name"] == program_name and symbol["symbol_type"] == "Field")
            fields = {name for name, count in field_counts.items() if count == 1}
            raw_blocks: list[dict] = [{"name": None, "units": []}]
            for unit in units:
                facts.verify_unit(unit)
                if unit["unit_type"] == "Paragraph":
                    raw_blocks.append({"name": unit["name"], "units": []})
                elif unit["unit_type"] == "Section":
                    raw_blocks[-1]["units"].append(unit | {"normalized_text": "@UNSUPPORTED"})
                else:
                    raw_blocks[-1]["units"].append(unit)
            total_tokens = 0
            blocks = []
            for block in raw_blocks:
                tokens = _tokenize(block["units"])
                total_tokens += len(tokens)
                if total_tokens > MAX_TOKENS:
                    raise ValueError("Procedure exceeds the bounded CFG token budget.")
                blocks.append({"name": block["name"], "statements": _Parser(tokens, fields).parse()})
            builder = _Builder(facts, program_name, blocks, max_nodes=max_nodes, max_edges=max_edges,
                               max_perform_depth=max_perform_depth)
            try:
                entry = builder.build()
            except _BudgetBoundary as error:
                # A partial branch graph would silently lose alternatives.
                # Replace it with one terminal boundary rather than returning
                # a seemingly usable but truncated normal path.
                builder.nodes.clear()
                builder.edges.clear()
                builder.boundaries.clear()
                builder.boundary(str(error), [], None, ())
                entry = builder.nodes[0]["node_id"]
        counts = Counter(node["kind"] for node in builder.nodes)
        reasons = Counter(boundary["reason"] for boundary in builder.boundaries)
        return {"cfg_version": CFG_VERSION, "snapshot_id": facts.snapshot_id, "program_name": program_name,
            "entry_node_id": entry, "nodes": builder.nodes, "edges": builder.edges, "boundaries": builder.boundaries,
            "status": "PARTIAL_SOURCE_CFG", "complete": False, "runtime_execution_tested": False,
            "evaluation_scope": "bounded_structured_intraprogram_source_alternatives",
            "evidence_integrity_scope": "stored_snapshot_consistency_not_authenticated_source_bytes",
            "summary": {"nodes": len(builder.nodes), "edges": len(builder.edges), "boundaries": len(builder.boundaries),
                "nodes_by_kind": dict(sorted(counts.items())), "boundary_counts": dict(sorted(reasons.items())),
                "supported_graph_closed": not builder.boundaries,
                "truncated": any("budget" in reason for reason in reasons)},
            "budgets": {"max_nodes": max_nodes, "max_edges": max_edges, "max_perform_depth": max_perform_depth},
            "limitations": [
                "Branches are possible source alternatives; predicate feasibility and execution have not been proven.",
                "CALL exception describes invocation failure, not a nonzero business status returned by a callee.",
                "Normal CALL does not model callee effects; reference arguments require conservative external effect handling.",
                "Handled single-target COMPUTE size error preserves the receiver; normal numeric precision is not proven.",
                "Explicit IF/CALL/COMPUTE scopes and bounded paragraph PERFORM ranges are modeled; unsupported forms terminate at boundaries.",
                "Plain EXIT is a no-op; only reaching the end of a performed range returns to its caller.",
                "Loop, SQL, EVALUATE, GO, external ABI, alias, data-layout and runtime storage semantics are outside this CFG subset.",
            ]}
    finally:
        connection.close()
