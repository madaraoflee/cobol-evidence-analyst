#!/usr/bin/env python3
"""Deterministic, offline structural index for COBOL POC repositories.

This module is intentionally parser-light. It extracts a conservative subset of
COBOL structure and relationships while preserving physical source line numbers.
It never calls a model or a network service. Unknown and ambiguous relationships
remain explicit instead of being guessed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from repo_inventory import (
    DEFAULT_EXTENSIONS,
    classify_artifact,
    decode_source,
    iter_source_files,
    parse_extensions,
    sha256_bytes,
)


SCHEMA_VERSION = "0.1"

PROGRAM_ID_RE = re.compile(
    r"\bPROGRAM-ID\s*\.\s*([A-Z0-9_$#@-]+)", re.IGNORECASE
)
DIVISION_RE = re.compile(
    r"^\s*(IDENTIFICATION|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION\b",
    re.IGNORECASE,
)
SECTION_RE = re.compile(
    r"^\s*([A-Z0-9_$#@-]+)\s+SECTION\s*\.\s*$", re.IGNORECASE
)
PARAGRAPH_RE = re.compile(
    r"^\s*([A-Z0-9_$#@-]+)\s*\.\s*$", re.IGNORECASE
)
DATA_ITEM_RE = re.compile(
    r"^\s*(0?[1-9]|[1-4][0-9]|66|77|88)\s+([A-Z0-9_$#@-]+)\b(.*)$",
    re.IGNORECASE,
)
COPY_RE = re.compile(
    r"\bCOPY\s+([A-Z0-9_$#@.-]+)", re.IGNORECASE
)
LITERAL_CALL_RE = re.compile(
    r"\bCALL\s+(['\"])([^'\"]+)\1", re.IGNORECASE
)
DYNAMIC_CALL_RE = re.compile(
    r"\bCALL\s+(?!['\"])([A-Z0-9_$#@-]+)", re.IGNORECASE
)
PERFORM_TARGET_RE = re.compile(
    r"\bPERFORM\s+([A-Z0-9_$#@-]+)"
    r"(?:\s+(?:THRU|THROUGH)\s+([A-Z0-9_$#@-]+))?",
    re.IGNORECASE,
)
IDENTIFIER_RE = re.compile(r"\b[A-Z][A-Z0-9_$#@-]*\b", re.IGNORECASE)
EXEC_SQL_START_RE = re.compile(r"\bEXEC\s+SQL\b", re.IGNORECASE)
EXEC_SQL_END_RE = re.compile(r"\bEND-EXEC\b", re.IGNORECASE)

PARAGRAPH_EXCLUSIONS = {
    "ACCEPT",
    "ADD",
    "ALTER",
    "CALL",
    "CANCEL",
    "CLOSE",
    "COMPUTE",
    "CONTINUE",
    "COPY",
    "DELETE",
    "DISPLAY",
    "DIVIDE",
    "ELSE",
    "END-CALL",
    "END-COMPUTE",
    "END-DELETE",
    "END-EVALUATE",
    "END-IF",
    "END-PERFORM",
    "END-READ",
    "END-REWRITE",
    "END-SEARCH",
    "END-START",
    "END-STRING",
    "END-UNSTRING",
    "END-WRITE",
    "ENTRY",
    "EVALUATE",
    "EXIT",
    "GOBACK",
    "GO",
    "IF",
    "INITIALIZE",
    "INSPECT",
    "MERGE",
    "MOVE",
    "MULTIPLY",
    "NEXT",
    "OPEN",
    "PERFORM",
    "READ",
    "RELEASE",
    "RETURN",
    "REWRITE",
    "SEARCH",
    "SET",
    "SORT",
    "START",
    "STOP",
    "STRING",
    "SUBTRACT",
    "UNSTRING",
    "WHEN",
    "WRITE",
}

PERFORM_NON_TARGETS = {
    "UNTIL",
    "VARYING",
    "WITH",
    "TIMES",
    "FOREVER",
    "TEST",
}

COBOL_NON_FIELD_WORDS = {
    "ACCEPT",
    "ADD",
    "ADDRESS",
    "AFTER",
    "ALL",
    "ALPHABETIC",
    "ALPHABETIC-LOWER",
    "ALPHABETIC-UPPER",
    "ALPHANUMERIC",
    "ALSO",
    "AND",
    "ARE",
    "ASCENDING",
    "AT",
    "BEFORE",
    "BY",
    "CALL",
    "CHARACTERS",
    "COMPUTE",
    "CONTINUE",
    "CORRESPONDING",
    "DEPENDING",
    "DESCENDING",
    "DISPLAY",
    "DIVIDE",
    "ELSE",
    "END",
    "EQUAL",
    "EVALUATE",
    "FALSE",
    "FROM",
    "FUNCTION",
    "GIVING",
    "GREATER",
    "HIGH-VALUES",
    "IF",
    "IN",
    "INITIALIZE",
    "INPUT",
    "INTO",
    "IS",
    "LESS",
    "LOW-VALUES",
    "MODE",
    "MOVE",
    "MULTIPLY",
    "NEGATIVE",
    "NOT",
    "NUMERIC",
    "OF",
    "OMITTED",
    "ON",
    "OR",
    "OUTPUT",
    "PERFORM",
    "POSITIVE",
    "READ",
    "RECORD",
    "REDEFINES",
    "REMAINDER",
    "REPLACING",
    "ROUNDED",
    "RUN",
    "SIZE",
    "SPACE",
    "SPACES",
    "START",
    "SUBTRACT",
    "THAN",
    "THEN",
    "THROUGH",
    "THRU",
    "TIMES",
    "TO",
    "TRUE",
    "UNTIL",
    "VARYING",
    "WHEN",
    "WITH",
    "WRITE",
    "ZERO",
    "ZEROES",
    "ZEROS",
}

RESOLVABLE_RELATION_TYPES = {
    "CALLS",
    "CALL_TARGET_FROM",
    "PERFORMS",
    "PERFORMS_THRU",
    "INCLUDES_COPY",
    "READS",
    "WRITES",
}


@dataclass(frozen=True)
class NormalizedLine:
    start_line: int
    end_line: int
    text: str


@dataclass(frozen=True)
class SourceDocument:
    relative_path: str
    source_sha256: str
    encoding: str
    used_fallback_encoding: bool
    format_hint: str
    artifact_kind: str
    raw_lines: tuple[str, ...]
    lines: tuple[NormalizedLine, ...]


@dataclass(frozen=True)
class EvidenceSpan:
    evidence_id: str
    relative_path: str
    start_line: int
    end_line: int
    source_sha256: str
    text: str


@dataclass(frozen=True)
class CodeUnit:
    unit_id: str
    relative_path: str
    unit_type: str
    name: str
    program_name: str | None
    parent_unit_id: str | None
    start_line: int
    end_line: int
    normalized_text: str
    content_hash: str
    evidence_id: str
    parse_status: str = "complete"


@dataclass(frozen=True)
class Symbol:
    symbol_id: str
    relative_path: str
    symbol_type: str
    name: str
    program_name: str | None
    qualified_name: str
    definition_unit_id: str
    evidence_id: str


@dataclass(frozen=True)
class Relation:
    relation_id: str
    relative_path: str
    from_entity_id: str
    relation_type: str
    target_name: str | None
    target_scope: str | None
    target_entity_id: str | None
    status: str
    evidence_id: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedFile:
    document: SourceDocument
    evidence_spans: tuple[EvidenceSpan, ...]
    code_units: tuple[CodeUnit, ...]
    symbols: tuple[Symbol, ...]
    relations: tuple[Relation, ...]


@dataclass
class ControlFrame:
    kind: str
    condition_id: str
    outcome: str


def _stable_id(prefix: str, *parts: object) -> str:
    joined = "\x1f".join("" if part is None else str(part) for part in parts)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_cobol_lines(text: str) -> tuple[tuple[NormalizedLine, ...], str]:
    """Normalize fixed/free COBOL while retaining physical source line ranges."""

    normalized: list[NormalizedLine] = []
    fixed_votes = 0
    free_votes = 0

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        expanded = raw_line.expandtabs(8)
        code = expanded
        indicator = " "
        first_six = expanded[:6] if len(expanded) >= 6 else ""
        looks_fixed = (
            len(expanded) >= 7
            and (
                first_six.strip().isdigit()
                or (not first_six.strip() and expanded[6] in " */-Dd")
            )
        )

        if looks_fixed:
            fixed_votes += 1
            indicator = expanded[6]
            if indicator in "*/":
                continue
            code = expanded[7:72]
        else:
            free_votes += 1
            stripped = expanded.lstrip()
            if stripped.startswith("*>") or stripped.startswith("*"):
                continue

        if "*>" in code:
            code = code.split("*>", 1)[0]
        code = code.rstrip()
        if not code.strip():
            continue

        if looks_fixed and indicator == "-" and normalized:
            previous = normalized[-1]
            normalized[-1] = NormalizedLine(
                start_line=previous.start_line,
                end_line=line_number,
                text=f"{previous.text.rstrip()} {code.lstrip()}",
            )
            continue

        normalized.append(
            NormalizedLine(
                start_line=line_number,
                end_line=line_number,
                text=code,
            )
        )

    if fixed_votes > max(5, free_votes // 3):
        format_hint = "fixed"
    elif fixed_votes and free_votes:
        format_hint = "mixed"
    else:
        format_hint = "free_or_unknown"
    return tuple(normalized), format_hint


def read_source_document(path: Path, root: Path) -> SourceDocument | None:
    data = path.read_bytes()
    decoded = decode_source(data)
    if decoded is None:
        return None

    lines, format_hint = normalize_cobol_lines(decoded.text)
    normalized_text = "\n".join(line.text for line in lines)
    suffix = path.suffix.lower()
    program_ids = tuple(
        match.upper() for match in PROGRAM_ID_RE.findall(normalized_text)
    )
    return SourceDocument(
        relative_path=path.relative_to(root).as_posix(),
        source_sha256=sha256_bytes(data),
        encoding=decoded.encoding,
        used_fallback_encoding=decoded.used_fallback,
        format_hint=format_hint,
        artifact_kind=classify_artifact(suffix, program_ids, normalized_text),
        raw_lines=tuple(decoded.text.splitlines()),
        lines=lines,
    )


def _span_text(document: SourceDocument, start_line: int, end_line: int) -> str:
    return "\n".join(document.raw_lines[start_line - 1 : end_line])


def _unique_identifiers(fragment: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    without_literals = re.sub(r"(['\"]).*?\1", " ", fragment)
    for match in IDENTIFIER_RE.findall(without_literals.upper()):
        if match in COBOL_NON_FIELD_WORDS or match.isdigit():
            continue
        if match not in seen:
            result.append(match)
            seen.add(match)
    return result


def _statement_kind(text: str) -> str:
    upper = text.strip().upper()
    if upper.startswith("EXEC SQL"):
        return "EXEC_SQL"
    first = upper.split(None, 1)[0].rstrip(".") if upper else "OTHER"
    normalized = first.replace("-", "_")
    return normalized if normalized in {
        "ACCEPT",
        "ADD",
        "CALL",
        "CLOSE",
        "COMPUTE",
        "CONTINUE",
        "COPY",
        "DELETE",
        "DISPLAY",
        "DIVIDE",
        "ELSE",
        "END_EVALUATE",
        "END_IF",
        "END_PERFORM",
        "EVALUATE",
        "GOBACK",
        "GO",
        "IF",
        "INITIALIZE",
        "MOVE",
        "MULTIPLY",
        "OPEN",
        "PERFORM",
        "READ",
        "REWRITE",
        "SET",
        "START",
        "STOP",
        "STRING",
        "SUBTRACT",
        "UNSTRING",
        "WHEN",
        "WRITE",
    } else "OTHER"


def _extract_data_access(text: str) -> tuple[list[str], list[str], dict[str, object]]:
    upper = text.upper().strip()
    reads: list[str] = []
    writes: list[str] = []
    metadata: dict[str, object] = {}

    compute = re.search(
        r"\bCOMPUTE\s+([A-Z0-9_$#@-]+)(?:\s+ROUNDED)?\s*=\s*(.+)",
        upper,
    )
    if compute:
        writes = [compute.group(1)]
        reads = _unique_identifiers(compute.group(2))
        metadata = {
            "expression": compute.group(2).rstrip("."),
            "rounded": "ROUNDED" in upper,
        }
        return reads, writes, metadata

    move = re.search(r"\bMOVE\s+(.+?)\s+TO\s+(.+?)(?:\.|$)", upper)
    if move:
        reads = _unique_identifiers(move.group(1))
        writes = _unique_identifiers(move.group(2))
        metadata = {"expression": move.group(1).strip(), "operation": "MOVE"}
        return reads, writes, metadata

    add = re.search(r"\bADD\s+(.+?)\s+TO\s+(.+?)(?:\.|$)", upper)
    if add:
        source_ids = _unique_identifiers(add.group(1))
        target_ids = _unique_identifiers(add.group(2))
        reads = source_ids + [item for item in target_ids if item not in source_ids]
        writes = target_ids
        metadata = {"expression": add.group(1).strip(), "operation": "ADD"}
        return reads, writes, metadata

    subtract = re.search(
        r"\bSUBTRACT\s+(.+?)\s+FROM\s+(.+?)(?:\.|$)", upper
    )
    if subtract:
        source_ids = _unique_identifiers(subtract.group(1))
        target_ids = _unique_identifiers(subtract.group(2))
        reads = source_ids + [item for item in target_ids if item not in source_ids]
        writes = target_ids
        metadata = {
            "expression": subtract.group(1).strip(),
            "operation": "SUBTRACT",
        }
        return reads, writes, metadata

    multiply = re.search(
        r"\bMULTIPLY\s+(.+?)\s+BY\s+(.+?)(?:\.|$)", upper
    )
    if multiply:
        source_ids = _unique_identifiers(multiply.group(1))
        target_ids = _unique_identifiers(multiply.group(2))
        reads = source_ids + [item for item in target_ids if item not in source_ids]
        writes = target_ids
        metadata = {
            "expression": multiply.group(1).strip(),
            "operation": "MULTIPLY",
        }
        return reads, writes, metadata

    divide = re.search(
        r"\bDIVIDE\s+(.+?)\s+(?:INTO|BY)\s+(.+?)(?:\.|$)", upper
    )
    if divide:
        source_ids = _unique_identifiers(divide.group(1))
        target_ids = _unique_identifiers(divide.group(2))
        reads = source_ids + [item for item in target_ids if item not in source_ids]
        writes = target_ids
        metadata = {"expression": divide.group(1).strip(), "operation": "DIVIDE"}
        return reads, writes, metadata

    return reads, writes, metadata


def parse_document(document: SourceDocument) -> ParsedFile:
    evidence: dict[str, EvidenceSpan] = {}
    units: list[CodeUnit] = []
    symbols: list[Symbol] = []
    relations: list[Relation] = []

    current_division: str | None = None
    current_program_name: str | None = None
    current_program_id: str | None = None
    current_section_id: str | None = None
    current_paragraph_id: str | None = None
    control_stack: list[ControlFrame] = []

    root_unit_id: str | None = None
    if document.artifact_kind in {"copybook", "cobol_fragment_or_copybook"}:
        copy_name = Path(document.relative_path).stem.upper()
        root_start = 1
        root_end = max(1, len(document.raw_lines))
        evidence_id = _stable_id(
            "ev",
            document.relative_path,
            document.source_sha256,
            root_start,
            root_end,
        )
        evidence[evidence_id] = EvidenceSpan(
            evidence_id=evidence_id,
            relative_path=document.relative_path,
            start_line=root_start,
            end_line=root_end,
            source_sha256=document.source_sha256,
            text=_span_text(document, root_start, root_end),
        )
        root_unit_id = _stable_id(
            "unit", document.relative_path, "Copybook", copy_name, root_start
        )
        root_text = "\n".join(line.text for line in document.lines)
        units.append(
            CodeUnit(
                unit_id=root_unit_id,
                relative_path=document.relative_path,
                unit_type="Copybook",
                name=copy_name,
                program_name=copy_name,
                parent_unit_id=None,
                start_line=root_start,
                end_line=root_end,
                normalized_text=root_text,
                content_hash=_content_hash(root_text),
                evidence_id=evidence_id,
            )
        )
        symbols.append(
            Symbol(
                symbol_id=_stable_id(
                    "sym", document.relative_path, "Copybook", copy_name
                ),
                relative_path=document.relative_path,
                symbol_type="Copybook",
                name=copy_name,
                program_name=None,
                qualified_name=copy_name,
                definition_unit_id=root_unit_id,
                evidence_id=evidence_id,
            )
        )
        current_program_name = copy_name

    def ensure_evidence(line: NormalizedLine) -> str:
        evidence_id = _stable_id(
            "ev",
            document.relative_path,
            document.source_sha256,
            line.start_line,
            line.end_line,
        )
        evidence.setdefault(
            evidence_id,
            EvidenceSpan(
                evidence_id=evidence_id,
                relative_path=document.relative_path,
                start_line=line.start_line,
                end_line=line.end_line,
                source_sha256=document.source_sha256,
                text=_span_text(document, line.start_line, line.end_line),
            ),
        )
        return evidence_id

    def add_unit(
        unit_type: str,
        name: str,
        line: NormalizedLine,
        *,
        parent_unit_id: str | None,
        program_name: str | None,
        parse_status: str = "complete",
    ) -> CodeUnit:
        evidence_id = ensure_evidence(line)
        unit_id = _stable_id(
            "unit",
            document.relative_path,
            unit_type,
            program_name,
            name,
            line.start_line,
            line.text.upper(),
        )
        unit = CodeUnit(
            unit_id=unit_id,
            relative_path=document.relative_path,
            unit_type=unit_type,
            name=name,
            program_name=program_name,
            parent_unit_id=parent_unit_id,
            start_line=line.start_line,
            end_line=line.end_line,
            normalized_text=line.text.strip(),
            content_hash=_content_hash(line.text.strip()),
            evidence_id=evidence_id,
            parse_status=parse_status,
        )
        units.append(unit)
        if parent_unit_id is not None:
            add_relation(
                parent_unit_id,
                "CONTAINS",
                target_entity_id=unit_id,
                target_name=name,
                target_scope=program_name,
                evidence_id=evidence_id,
                status="confirmed",
            )
        return unit

    def add_symbol(
        symbol_type: str,
        name: str,
        unit: CodeUnit,
        *,
        program_name: str | None,
        qualified_name: str,
    ) -> Symbol:
        symbol = Symbol(
            symbol_id=_stable_id(
                "sym",
                document.relative_path,
                symbol_type,
                qualified_name,
                unit.start_line,
            ),
            relative_path=document.relative_path,
            symbol_type=symbol_type,
            name=name.upper(),
            program_name=program_name,
            qualified_name=qualified_name,
            definition_unit_id=unit.unit_id,
            evidence_id=unit.evidence_id,
        )
        symbols.append(symbol)
        return symbol

    def add_relation(
        from_entity_id: str,
        relation_type: str,
        *,
        target_name: str | None,
        target_scope: str | None,
        evidence_id: str,
        status: str = "unresolved",
        target_entity_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> Relation:
        relation_id = _stable_id(
            "rel",
            document.relative_path,
            from_entity_id,
            relation_type,
            target_name,
            target_scope,
            evidence_id,
            len(relations),
        )
        relation = Relation(
            relation_id=relation_id,
            relative_path=document.relative_path,
            from_entity_id=from_entity_id,
            relation_type=relation_type,
            target_name=target_name.upper() if target_name else None,
            target_scope=target_scope,
            target_entity_id=target_entity_id,
            status=status,
            evidence_id=evidence_id,
            metadata=metadata or {},
        )
        relations.append(relation)
        return relation

    def add_statement(
        kind: str,
        line: NormalizedLine,
        *,
        parse_status: str = "complete",
    ) -> CodeUnit:
        parent = (
            current_paragraph_id
            or current_section_id
            or current_program_id
            or root_unit_id
        )
        statement = add_unit(
            "Statement",
            kind,
            line,
            parent_unit_id=parent,
            program_name=current_program_name,
            parse_status=parse_status,
        )
        for frame in control_stack:
            add_relation(
                statement.unit_id,
                "CONTROL_DEPENDS_ON",
                target_name=None,
                target_scope=current_program_name,
                target_entity_id=frame.condition_id,
                evidence_id=statement.evidence_id,
                status="confirmed",
                metadata={"outcome": frame.outcome, "control_kind": frame.kind},
            )
        return statement

    def add_field_relations(
        statement: CodeUnit,
        reads: Iterable[str],
        writes: Iterable[str],
        metadata: dict[str, object],
    ) -> None:
        for field_name in reads:
            add_relation(
                statement.unit_id,
                "READS",
                target_name=field_name,
                target_scope=current_program_name,
                evidence_id=statement.evidence_id,
                metadata=metadata,
            )
        for field_name in writes:
            add_relation(
                statement.unit_id,
                "WRITES",
                target_name=field_name,
                target_scope=current_program_name,
                evidence_id=statement.evidence_id,
                metadata=metadata,
            )

    index = 0
    while index < len(document.lines):
        line = document.lines[index]
        stripped = line.text.strip()
        upper = stripped.upper()
        if not stripped:
            index += 1
            continue

        division = DIVISION_RE.match(upper)
        if division:
            current_division = division.group(1).upper()
            current_section_id = None
            current_paragraph_id = None
            control_stack.clear()
            index += 1
            continue

        program_match = PROGRAM_ID_RE.search(upper)
        if program_match:
            current_program_name = program_match.group(1).upper()
            program_unit = add_unit(
                "Program",
                current_program_name,
                line,
                parent_unit_id=None,
                program_name=current_program_name,
            )
            add_symbol(
                "Program",
                current_program_name,
                program_unit,
                program_name=current_program_name,
                qualified_name=current_program_name,
            )
            current_program_id = program_unit.unit_id
            current_section_id = None
            current_paragraph_id = None
            index += 1
            continue

        section_match = SECTION_RE.match(upper)
        if section_match:
            section_name = section_match.group(1).upper()
            section_unit = add_unit(
                "Section",
                section_name,
                line,
                parent_unit_id=current_program_id or root_unit_id,
                program_name=current_program_name,
            )
            add_symbol(
                "Section",
                section_name,
                section_unit,
                program_name=current_program_name,
                qualified_name=f"{current_program_name or '<NONE>'}::{section_name}",
            )
            current_section_id = section_unit.unit_id
            current_paragraph_id = None
            index += 1
            continue

        if current_division == "PROCEDURE":
            paragraph_match = PARAGRAPH_RE.match(upper)
            if paragraph_match:
                paragraph_name = paragraph_match.group(1).upper()
                if paragraph_name not in PARAGRAPH_EXCLUSIONS:
                    paragraph_unit = add_unit(
                        "Paragraph",
                        paragraph_name,
                        line,
                        parent_unit_id=current_section_id or current_program_id,
                        program_name=current_program_name,
                    )
                    add_symbol(
                        "Paragraph",
                        paragraph_name,
                        paragraph_unit,
                        program_name=current_program_name,
                        qualified_name=(
                            f"{current_program_name or '<NONE>'}::{paragraph_name}"
                        ),
                    )
                    current_paragraph_id = paragraph_unit.unit_id
                    control_stack.clear()
                    index += 1
                    continue

        if current_division == "DATA" or (
            current_program_id is None and root_unit_id is not None
        ):
            data_match = DATA_ITEM_RE.match(upper)
            if data_match:
                level = data_match.group(1).zfill(2)
                field_name = data_match.group(2).upper()
                field_type = "ConditionName" if level == "88" else "Field"
                field_unit = add_unit(
                    "DataItem",
                    field_name,
                    line,
                    parent_unit_id=current_section_id
                    or current_program_id
                    or root_unit_id,
                    program_name=current_program_name,
                )
                add_symbol(
                    field_type,
                    field_name,
                    field_unit,
                    program_name=current_program_name,
                    qualified_name=(
                        f"{current_program_name or '<NONE>'}::{field_name}"
                    ),
                )
                index += 1
                continue

        if EXEC_SQL_START_RE.search(upper):
            sql_lines = [line]
            cursor = index + 1
            while cursor < len(document.lines):
                sql_lines.append(document.lines[cursor])
                if EXEC_SQL_END_RE.search(document.lines[cursor].text):
                    cursor += 1
                    break
                cursor += 1
            sql_line = NormalizedLine(
                start_line=sql_lines[0].start_line,
                end_line=sql_lines[-1].end_line,
                text=" ".join(item.text.strip() for item in sql_lines),
            )
            statement = add_statement("EXEC_SQL", sql_line)
            sql_upper = sql_line.text.upper()
            for table_name in re.findall(
                r"\b(?:FROM|JOIN)\s+([A-Z0-9_$#@.-]+)", sql_upper
            ):
                add_relation(
                    statement.unit_id,
                    "SELECTS_FROM",
                    target_name=table_name,
                    target_scope=None,
                    evidence_id=statement.evidence_id,
                    metadata={"boundary": "database_definition_not_indexed"},
                )
            for table_name in re.findall(
                r"\b(?:UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+"
                r"([A-Z0-9_$#@.-]+)",
                sql_upper,
            ):
                add_relation(
                    statement.unit_id,
                    "UPDATES",
                    target_name=table_name,
                    target_scope=None,
                    evidence_id=statement.evidence_id,
                    metadata={"boundary": "database_definition_not_indexed"},
                )
            index = cursor
            continue

        if re.match(r"^END-IF\b", upper):
            if control_stack:
                for reverse_index in range(len(control_stack) - 1, -1, -1):
                    if control_stack[reverse_index].kind == "IF":
                        del control_stack[reverse_index]
                        break
            add_statement("END_IF", line)
            index += 1
            continue

        if re.match(r"^ELSE\b", upper):
            for frame in reversed(control_stack):
                if frame.kind == "IF":
                    frame.outcome = "false"
                    break
            add_statement("ELSE", line)
            index += 1
            continue

        if re.match(r"^END-EVALUATE\b", upper):
            if control_stack:
                for reverse_index in range(len(control_stack) - 1, -1, -1):
                    if control_stack[reverse_index].kind.startswith("EVALUATE"):
                        del control_stack[reverse_index]
                        break
            add_statement("END_EVALUATE", line)
            index += 1
            continue

        if re.match(r"^WHEN\b", upper):
            when_unit = add_unit(
                "Condition",
                "WHEN",
                line,
                parent_unit_id=current_paragraph_id
                or current_section_id
                or current_program_id,
                program_name=current_program_name,
            )
            for reverse_index in range(len(control_stack) - 1, -1, -1):
                if control_stack[reverse_index].kind.startswith("EVALUATE"):
                    control_stack[reverse_index] = ControlFrame(
                        kind="EVALUATE_WHEN",
                        condition_id=when_unit.unit_id,
                        outcome=upper.rstrip("."),
                    )
                    break
            for field_name in _unique_identifiers(
                re.sub(r"^WHEN\s+", "", upper)
            ):
                add_relation(
                    when_unit.unit_id,
                    "READS",
                    target_name=field_name,
                    target_scope=current_program_name,
                    evidence_id=when_unit.evidence_id,
                )
            index += 1
            continue

        if re.match(r"^IF\b", upper):
            statement = add_statement("IF", line)
            condition_text = re.sub(r"^IF\s+", "", upper).rstrip(".")
            condition_unit = add_unit(
                "Condition",
                "IF",
                line,
                parent_unit_id=statement.unit_id,
                program_name=current_program_name,
            )
            for field_name in _unique_identifiers(condition_text):
                add_relation(
                    condition_unit.unit_id,
                    "READS",
                    target_name=field_name,
                    target_scope=current_program_name,
                    evidence_id=condition_unit.evidence_id,
                    metadata={"condition": condition_text},
                )
            control_stack.append(
                ControlFrame("IF", condition_unit.unit_id, "true")
            )
            if "END-IF" in upper:
                control_stack.pop()
            index += 1
            continue

        if re.match(r"^EVALUATE\b", upper):
            statement = add_statement("EVALUATE", line)
            expression = re.sub(r"^EVALUATE\s+", "", upper).rstrip(".")
            condition_unit = add_unit(
                "Condition",
                "EVALUATE",
                line,
                parent_unit_id=statement.unit_id,
                program_name=current_program_name,
            )
            for field_name in _unique_identifiers(expression):
                add_relation(
                    condition_unit.unit_id,
                    "READS",
                    target_name=field_name,
                    target_scope=current_program_name,
                    evidence_id=condition_unit.evidence_id,
                    metadata={"condition": expression},
                )
            control_stack.append(
                ControlFrame("EVALUATE", condition_unit.unit_id, "selector")
            )
            index += 1
            continue

        statement_kind = _statement_kind(upper)
        next_index = index + 1
        continuable_kinds = {
            "ADD",
            "CALL",
            "COMPUTE",
            "COPY",
            "DIVIDE",
            "MOVE",
            "MULTIPLY",
            "PERFORM",
            "READ",
            "REWRITE",
            "START",
            "STRING",
            "SUBTRACT",
            "UNSTRING",
            "WRITE",
        }
        if (
            current_division == "PROCEDURE"
            and statement_kind in continuable_kinds
            and not upper.endswith(".")
        ):
            collected = [line]
            cursor = index + 1
            while cursor < len(document.lines):
                candidate = document.lines[cursor]
                candidate_upper = candidate.text.strip().upper()
                candidate_kind = _statement_kind(candidate_upper)
                candidate_paragraph = PARAGRAPH_RE.match(candidate_upper)
                starts_new_structure = bool(
                    DIVISION_RE.match(candidate_upper)
                    or SECTION_RE.match(candidate_upper)
                    or (
                        candidate_paragraph
                        and candidate_paragraph.group(1).upper()
                        not in PARAGRAPH_EXCLUSIONS
                    )
                    or candidate_upper.startswith(
                        ("ELSE", "END-IF", "WHEN", "END-EVALUATE")
                    )
                    or candidate_kind != "OTHER"
                )
                if starts_new_structure:
                    break
                collected.append(candidate)
                cursor += 1
                if candidate_upper.endswith("."):
                    break

            if len(collected) > 1:
                line = NormalizedLine(
                    start_line=collected[0].start_line,
                    end_line=collected[-1].end_line,
                    text=" ".join(
                        item.text.strip() for item in collected
                    ),
                )
                upper = line.text.strip().upper()
                next_index = cursor

        parse_status = "complete" if statement_kind != "OTHER" else "partial"
        statement = add_statement(statement_kind, line, parse_status=parse_status)

        copy_match = COPY_RE.search(upper)
        if copy_match:
            copy_target = copy_match.group(1).rstrip(".").upper()
            add_relation(
                statement.unit_id,
                "INCLUDES_COPY",
                target_name=copy_target,
                target_scope=None,
                evidence_id=statement.evidence_id,
            )

        literal_calls = [
            value.strip().upper()
            for _, value in LITERAL_CALL_RE.findall(upper)
        ]
        for target in literal_calls:
            add_relation(
                statement.unit_id,
                "CALLS",
                target_name=target,
                target_scope=None,
                evidence_id=statement.evidence_id,
                metadata={"call_form": "literal"},
            )

        dynamic_match = DYNAMIC_CALL_RE.search(upper)
        if dynamic_match and not literal_calls:
            target_field = dynamic_match.group(1).upper()
            add_relation(
                statement.unit_id,
                "CALL_TARGET_FROM",
                target_name=target_field,
                target_scope=current_program_name,
                evidence_id=statement.evidence_id,
                metadata={
                    "call_form": "identifier",
                    "boundary": "runtime_target_requires_value_flow",
                },
            )

        perform_match = PERFORM_TARGET_RE.search(upper)
        if perform_match:
            start_target = perform_match.group(1).upper()
            end_target = (
                perform_match.group(2).upper()
                if perform_match.group(2)
                else None
            )
            if start_target not in PERFORM_NON_TARGETS:
                relation_type = "PERFORMS_THRU" if end_target else "PERFORMS"
                add_relation(
                    statement.unit_id,
                    relation_type,
                    target_name=start_target,
                    target_scope=current_program_name,
                    evidence_id=statement.evidence_id,
                    metadata={"range_end": end_target} if end_target else {},
                )

        for operation, relation_type in (
            ("READ", "READS_FILE"),
            ("START", "READS_FILE"),
            ("WRITE", "WRITES_FILE"),
            ("REWRITE", "WRITES_FILE"),
        ):
            operation_match = re.search(
                rf"\b{operation}\s+([A-Z0-9_$#@-]+)", upper
            )
            if operation_match:
                add_relation(
                    statement.unit_id,
                    relation_type,
                    target_name=operation_match.group(1),
                    target_scope=current_program_name,
                    evidence_id=statement.evidence_id,
                    metadata={"boundary": "file_definition_not_indexed"},
                )

        reads, writes, expression_metadata = _extract_data_access(upper)
        add_field_relations(statement, reads, writes, expression_metadata)
        index = next_index

    return ParsedFile(
        document=document,
        evidence_spans=tuple(evidence.values()),
        code_units=tuple(units),
        symbols=tuple(symbols),
        relations=tuple(relations),
    )


def _connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS source_files (
            relative_path TEXT PRIMARY KEY,
            sha256 TEXT NOT NULL,
            encoding TEXT NOT NULL,
            used_fallback_encoding INTEGER NOT NULL,
            format_hint TEXT NOT NULL,
            artifact_kind TEXT NOT NULL,
            line_count INTEGER NOT NULL,
            indexed_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS evidence_spans (
            evidence_id TEXT PRIMARY KEY,
            relative_path TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            source_sha256 TEXT NOT NULL,
            text TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS code_units (
            unit_id TEXT PRIMARY KEY,
            relative_path TEXT NOT NULL,
            unit_type TEXT NOT NULL,
            name TEXT NOT NULL,
            program_name TEXT,
            parent_unit_id TEXT,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            normalized_text TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            parse_status TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS symbols (
            symbol_id TEXT PRIMARY KEY,
            relative_path TEXT NOT NULL,
            symbol_type TEXT NOT NULL,
            name TEXT NOT NULL,
            program_name TEXT,
            qualified_name TEXT NOT NULL,
            definition_unit_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS relations (
            relation_id TEXT PRIMARY KEY,
            relative_path TEXT NOT NULL,
            from_entity_id TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            target_name TEXT,
            target_scope TEXT,
            target_entity_id TEXT,
            status TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_units_path
            ON code_units(relative_path);
        CREATE INDEX IF NOT EXISTS idx_units_program
            ON code_units(program_name, unit_type);
        CREATE INDEX IF NOT EXISTS idx_symbols_lookup
            ON symbols(symbol_type, name, program_name);
        CREATE INDEX IF NOT EXISTS idx_relations_from
            ON relations(from_entity_id, relation_type);
        CREATE INDEX IF NOT EXISTS idx_relations_target
            ON relations(target_entity_id, relation_type);
        """
    )
    try:
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS code_units_fts USING fts5(
                unit_id UNINDEXED,
                name,
                program_name,
                normalized_text
            )
            """
        )
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            "This Python SQLite build does not include FTS5 support."
        ) from exc


def _delete_file_facts(connection: sqlite3.Connection, relative_path: str) -> None:
    unit_ids = [
        row["unit_id"]
        for row in connection.execute(
            "SELECT unit_id FROM code_units WHERE relative_path = ?",
            (relative_path,),
        )
    ]
    for unit_id in unit_ids:
        connection.execute(
            "DELETE FROM code_units_fts WHERE unit_id = ?", (unit_id,)
        )
    connection.execute(
        "DELETE FROM relations WHERE relative_path = ?", (relative_path,)
    )
    connection.execute(
        "DELETE FROM symbols WHERE relative_path = ?", (relative_path,)
    )
    connection.execute(
        "DELETE FROM code_units WHERE relative_path = ?", (relative_path,)
    )
    connection.execute(
        "DELETE FROM evidence_spans WHERE relative_path = ?", (relative_path,)
    )
    connection.execute(
        "DELETE FROM source_files WHERE relative_path = ?", (relative_path,)
    )


def _insert_parsed_file(
    connection: sqlite3.Connection, parsed: ParsedFile
) -> None:
    document = parsed.document
    indexed_at = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO source_files (
            relative_path, sha256, encoding, used_fallback_encoding,
            format_hint, artifact_kind, line_count, indexed_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document.relative_path,
            document.source_sha256,
            document.encoding,
            int(document.used_fallback_encoding),
            document.format_hint,
            document.artifact_kind,
            len(document.raw_lines),
            indexed_at,
        ),
    )

    connection.executemany(
        """
        INSERT INTO evidence_spans (
            evidence_id, relative_path, start_line, end_line,
            source_sha256, text
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                item.evidence_id,
                item.relative_path,
                item.start_line,
                item.end_line,
                item.source_sha256,
                item.text,
            )
            for item in parsed.evidence_spans
        ],
    )

    connection.executemany(
        """
        INSERT INTO code_units (
            unit_id, relative_path, unit_type, name, program_name,
            parent_unit_id, start_line, end_line, normalized_text,
            content_hash, evidence_id, parse_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                item.unit_id,
                item.relative_path,
                item.unit_type,
                item.name,
                item.program_name,
                item.parent_unit_id,
                item.start_line,
                item.end_line,
                item.normalized_text,
                item.content_hash,
                item.evidence_id,
                item.parse_status,
            )
            for item in parsed.code_units
        ],
    )

    connection.executemany(
        """
        INSERT INTO code_units_fts (
            unit_id, name, program_name, normalized_text
        ) VALUES (?, ?, ?, ?)
        """,
        [
            (
                item.unit_id,
                item.name,
                item.program_name or "",
                item.normalized_text,
            )
            for item in parsed.code_units
        ],
    )

    connection.executemany(
        """
        INSERT INTO symbols (
            symbol_id, relative_path, symbol_type, name, program_name,
            qualified_name, definition_unit_id, evidence_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                item.symbol_id,
                item.relative_path,
                item.symbol_type,
                item.name,
                item.program_name,
                item.qualified_name,
                item.definition_unit_id,
                item.evidence_id,
            )
            for item in parsed.symbols
        ],
    )

    connection.executemany(
        """
        INSERT INTO relations (
            relation_id, relative_path, from_entity_id, relation_type,
            target_name, target_scope, target_entity_id, status,
            evidence_id, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                item.relation_id,
                item.relative_path,
                item.from_entity_id,
                item.relation_type,
                item.target_name,
                item.target_scope,
                item.target_entity_id,
                item.status,
                item.evidence_id,
                json.dumps(item.metadata, ensure_ascii=False, sort_keys=True),
            )
            for item in parsed.relations
        ],
    )


def _symbol_maps(
    connection: sqlite3.Connection,
) -> tuple[
    dict[str, list[str]],
    dict[str, list[str]],
    dict[tuple[str, str], list[str]],
    dict[tuple[str, str], list[str]],
]:
    programs: dict[str, list[str]] = defaultdict(list)
    copybooks: dict[str, list[str]] = defaultdict(list)
    paragraphs: dict[tuple[str, str], list[str]] = defaultdict(list)
    fields: dict[tuple[str, str], list[str]] = defaultdict(list)

    for row in connection.execute(
        "SELECT symbol_id, symbol_type, name, program_name FROM symbols"
    ):
        symbol_type = row["symbol_type"]
        name = row["name"]
        program_name = row["program_name"]
        if symbol_type == "Program":
            programs[name].append(row["symbol_id"])
        elif symbol_type == "Copybook":
            copybooks[name].append(row["symbol_id"])
        elif symbol_type == "Paragraph" and program_name:
            paragraphs[(program_name, name)].append(row["symbol_id"])
        elif symbol_type in {"Field", "ConditionName"} and program_name:
            fields[(program_name, name)].append(row["symbol_id"])
    return programs, copybooks, paragraphs, fields


def _resolve_relations(connection: sqlite3.Connection) -> None:
    programs, copybooks, paragraphs, fields = _symbol_maps(connection)

    placeholders = ",".join("?" for _ in RESOLVABLE_RELATION_TYPES)
    rows = list(
        connection.execute(
            f"""
            SELECT relation_id, relation_type, target_name, target_scope,
                   metadata_json
              FROM relations
             WHERE relation_type IN ({placeholders})
            """,
            tuple(sorted(RESOLVABLE_RELATION_TYPES)),
        )
    )

    for row in rows:
        relation_type = row["relation_type"]
        target_name = row["target_name"]
        target_scope = row["target_scope"]
        metadata = json.loads(row["metadata_json"])
        candidates: list[str] = []

        if target_name is None:
            continue
        if relation_type == "CALLS":
            candidates = programs.get(target_name, [])
        elif relation_type == "INCLUDES_COPY":
            candidates = copybooks.get(target_name, [])
        elif relation_type in {"PERFORMS", "PERFORMS_THRU"} and target_scope:
            candidates = paragraphs.get((target_scope, target_name), [])
        elif relation_type in {"READS", "WRITES", "CALL_TARGET_FROM"} and target_scope:
            candidates = fields.get((target_scope, target_name), [])

        if len(candidates) == 1:
            status = "confirmed"
            target_entity_id = candidates[0]
            metadata.pop("resolution_reason", None)
        elif len(candidates) > 1:
            status = "candidate"
            target_entity_id = None
            metadata["resolution_reason"] = "ambiguous_symbol"
            metadata["candidate_count"] = len(candidates)
        else:
            status = "unresolved"
            target_entity_id = None
            metadata["resolution_reason"] = "target_not_found"

        connection.execute(
            """
            UPDATE relations
               SET target_entity_id = ?, status = ?, metadata_json = ?
             WHERE relation_id = ?
            """,
            (
                target_entity_id,
                status,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                row["relation_id"],
            ),
        )


def _snapshot_id(documents: Sequence[SourceDocument]) -> str:
    hasher = hashlib.sha256()
    for document in sorted(
        documents, key=lambda item: item.relative_path.casefold()
    ):
        hasher.update(document.relative_path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(document.source_sha256.encode("ascii"))
        hasher.update(b"\n")
    return f"sha256:{hasher.hexdigest()}"


def _database_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
        for table in (
            "source_files",
            "code_units",
            "symbols",
            "relations",
            "evidence_spans",
        )
    }


def build_structural_index(
    source_root: Path,
    database_path: Path,
    *,
    extensions: frozenset[str] = DEFAULT_EXTENSIONS,
    include_extensionless: bool = False,
    quiet: bool = False,
) -> dict[str, object]:
    """Incrementally build the local structural index."""

    root = source_root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Source root is not a directory: {source_root}")

    database = database_path.expanduser().resolve()
    connection = _connect(database)
    try:
        _ensure_schema(connection)
        existing_hashes = {
            row["relative_path"]: row["sha256"]
            for row in connection.execute(
                "SELECT relative_path, sha256 FROM source_files"
            )
        }

        documents: list[SourceDocument] = []
        unreadable_count = 0
        source_files = list(
            iter_source_files(root, extensions, include_extensionless)
        )
        for file_index, path in enumerate(source_files, start=1):
            try:
                document = read_source_document(path, root)
            except OSError:
                document = None
            if document is None:
                unreadable_count += 1
            else:
                documents.append(document)
            if not quiet and file_index % 500 == 0:
                print(
                    f"Prepared {file_index}/{len(source_files)} files...",
                    file=sys.stderr,
                )

        current_paths = {document.relative_path for document in documents}
        removed_paths = sorted(set(existing_hashes) - current_paths)
        changed_documents = [
            document
            for document in documents
            if existing_hashes.get(document.relative_path)
            != document.source_sha256
        ]
        skipped_count = len(documents) - len(changed_documents)

        with connection:
            for relative_path in removed_paths:
                _delete_file_facts(connection, relative_path)

            for document in changed_documents:
                if document.relative_path in existing_hashes:
                    _delete_file_facts(connection, document.relative_path)
                parsed = parse_document(document)
                _insert_parsed_file(connection, parsed)

            _resolve_relations(connection)
            snapshot_id = _snapshot_id(documents)
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (SCHEMA_VERSION,),
            )
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES('snapshot_id', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (snapshot_id,),
            )
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES('source_root_hash', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (_content_hash(str(root)),),
            )
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES('indexed_at_utc', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (datetime.now(timezone.utc).isoformat(),),
            )

        relation_statuses = {
            row["status"]: row["count"]
            for row in connection.execute(
                """
                SELECT status, COUNT(*) AS count
                  FROM relations
                 GROUP BY status
                 ORDER BY status
                """
            )
        }
        report: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": _snapshot_id(documents),
            "privacy": {
                "network_calls": False,
                "source_stored_locally": True,
                "absolute_source_paths_stored": False,
            },
            "files": {
                "candidate": len(source_files),
                "decoded": len(documents),
                "unreadable_or_binary": unreadable_count,
                "indexed_or_updated": len(changed_documents),
                "skipped_unchanged": skipped_count,
                "removed": len(removed_paths),
            },
            "database_counts": _database_counts(connection),
            "relation_statuses": relation_statuses,
            "database_path": str(database),
        }
        return report
    finally:
        connection.close()


def search_code(
    database_path: Path, query: str, *, limit: int = 10
) -> list[dict[str, object]]:
    tokens = _unique_identifiers(query)
    if not tokens:
        return []
    match_expression = " OR ".join(f'"{token}"' for token in tokens)
    connection = _connect(database_path.expanduser().resolve())
    try:
        rows = connection.execute(
            """
            SELECT c.unit_id, c.unit_type, c.name, c.program_name,
                   c.relative_path, c.start_line, c.end_line,
                   c.normalized_text, c.evidence_id,
                   bm25(code_units_fts) AS score
              FROM code_units_fts
              JOIN code_units AS c ON c.unit_id = code_units_fts.unit_id
             WHERE code_units_fts MATCH ?
             ORDER BY score
             LIMIT ?
            """,
            (match_expression, max(1, min(limit, 100))),
        )
        return [dict(row) for row in rows]
    finally:
        connection.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build an offline SQLite/FTS5 structural index for COBOL and "
            "COPYBOOK source files."
        )
    )
    parser.add_argument("source", type=Path, help="Windows or local source folder")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(".poc-data/structural-index.sqlite"),
        help="SQLite database path",
    )
    parser.add_argument(
        "--report-output",
        type=Path,
        help="Optional JSON build report path",
    )
    parser.add_argument(
        "--extensions",
        help="Comma-separated source extensions",
    )
    parser.add_argument(
        "--include-extensionless",
        action="store_true",
        help="Also inspect source members without an extension",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        extensions = parse_extensions(args.extensions)
        report = build_structural_index(
            args.source,
            args.database,
            extensions=extensions,
            include_extensionless=args.include_extensionless,
            quiet=args.quiet,
        )
        rendered = json.dumps(
            report, ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
        if args.report_output:
            args.report_output.parent.mkdir(parents=True, exist_ok=True)
            args.report_output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
