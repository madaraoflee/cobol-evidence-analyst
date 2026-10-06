"""Bind bounded statement AST facts to the exact excerpts sent for synthesis.

Facts describe local syntax, never execution, final values, or storage aliases.
Only bodies with a visible unit start and a complete sentence end are parsed.
Unsupported syntax remains a visible boundary, with its original source retained.
"""

from __future__ import annotations

from functools import lru_cache
import json
import re

from statement_parser import PARSER_VERSION, _RESERVED, parse_statements

_HEADER = re.compile(r"^\s*([A-Z0-9_$#@-]+)(?:\s+SECTION)?\s*\.\s*$", re.I)
_DIVISION = re.compile(r"^\s*(IDENTIFICATION|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION\b", re.I)
_DIRECTIVE = re.compile(r"^\s*>>\s*SOURCE\s+FORMAT(?:\s+IS)?\s+(FREE|FIXED)\b", re.I)
_NOT_HEADERS = _RESERVED | {"END", "END-EXEC"}
_EXEC_MARKER = re.compile(r"(?<![A-Z0-9_$#@-])(?:END-EXEC|EXEC)(?![A-Z0-9_$#@-])", re.I)
_MAX_SCAN = 80000
_MAX_FACTS = 24
_MAX_BYTES = 14000


def _navigation_lines(text, source_format):
    """Mask comments and literals for unit discovery, preserving physical lines."""
    quote, masked_lines = None, []
    for physical in text.splitlines():
        if source_format == "fixed":
            if physical[6:7] in {"*", "/"}:
                masked_lines.append("")
                continue
            physical = physical[7:72]
        result, index = [], 0
        while index < len(physical):
            char = physical[index]
            if quote:
                result.append(" ")
                if char == quote:
                    if physical[index + 1:index + 2] == quote:
                        result.append(" ")
                        index += 2
                        continue
                    quote = None
            elif physical.startswith("*>", index):
                break
            elif char in {"'", '"'}:
                quote = char
                result.append(" ")
            else:
                result.append(char)
            index += 1
        masked_lines.append("".join(result))
    # Embedded languages may contain label-shaped lines and COBOL-looking
    # tokens. They cannot create units. If an excerpt first exposes END-EXEC,
    # its prefix is an incomplete embedded block, not a visible paragraph.
    first_marker = next((match for line in masked_lines
                         if (match := _EXEC_MARKER.search(line))), None)
    embedded = bool(first_marker and first_marker[0].upper() == "END-EXEC")
    block_comment = False
    for line in masked_lines:
        result, index = [], 0
        while index < len(line):
            marker = _EXEC_MARKER.match(line, index)
            if embedded:
                if block_comment and line.startswith("*/", index):
                    result.append("  ")
                    index += 2
                    block_comment = False
                    continue
                if not block_comment and line.startswith("--", index):
                    result.append(" " * (len(line) - index))
                    break
                if not block_comment and line.startswith("/*", index):
                    block_comment = True
                if not block_comment and marker and marker[0].upper() == "END-EXEC":
                    embedded = False
                    result.append(" " * len(marker[0]))
                    index = marker.end()
                    continue
                result.append(" ")
                index += 1
            elif marker:
                embedded = marker[0].upper() == "EXEC"
                result.append(" " * len(marker[0]))
                index = marker.end()
            else:
                result.append(line[index])
                index += 1
        yield "".join(result)


def _bodies(text, first_line):
    """Discover paragraph boundaries only; the token parser owns all semantics."""
    from structural_index import normalize_cobol_lines

    # Explicit source directives take precedence over a short excerpt's format
    # vote. Format changes, tabs, and fragments remain outside this adapter.
    if "\t" in text:
        return [], "tabs_require_source_mapping"
    directives = {match[1].lower() for line in _navigation_lines(text, "free")
                  if (match := _DIRECTIVE.match(line))}
    if len(directives) > 1:
        return [], "mixed_source_formats"
    _, hint = normalize_cobol_lines(text)
    source_format = next(iter(directives), "fixed" if hint == "fixed" else "free")
    raw = text.splitlines()
    if not directives:
        # The repository detector uses majority voting and needs six fixed rows.
        # Excerpts can be shorter: never parse an identification-area suffix as
        # another MOVE target, or silently truncate ambiguous free-format code.
        active = [line for line in raw if line.strip() and not line.lstrip().startswith("*>")]
        fixed_rows = [line for line in active if len(line) >= 7 and (
            line[:6].isdigit() or not line[:6].strip() and line[6] in " */-Dd")]
        if fixed_rows and len(fixed_rows) != len(active):
            return [], "mixed_or_ambiguous_source_format"
        if fixed_rows:
            numbered = any(line[:6].isdigit() for line in fixed_rows)
            if not numbered and any(line[6] not in "*/" and line[72:].strip() for line in fixed_rows):
                return [], "source_format_ambiguous_column_73"
            source_format = "fixed"
        elif hint == "mixed":
            return [], "mixed_or_ambiguous_source_format"
    if source_format == "fixed" and any(line[6:7] in {"-", "D", "d"} for line in raw):
        return [], "fixed_continuation_or_debug_line_not_supported"
    masked = list(_navigation_lines(text, source_format))
    bodies, start, name, in_data = [], None, None, False

    def finish(end):
        if start is None or start >= end:
            return
        nonempty = [line.strip() for line in masked[start:end] if line.strip()]
        # A clipped tail must not appear to be a complete unconditional body.
        if nonempty and nonempty[-1].endswith("."):
            bodies.append((name, "\n".join(raw[start:end]), first_line + start, source_format))

    for index, line in enumerate(masked):
        division = _DIVISION.match(line)
        if division:
            finish(index)
            start = None
            in_data = division[1].upper() != "PROCEDURE"
            if not in_data and line.rstrip().endswith("."):
                start, name = index + 1, "PROCEDURE"
            continue
        if re.match(r"^\s*END\s+PROGRAM\b", line, re.I):
            finish(index)
            start, in_data = None, True
            continue
        header = _HEADER.match(line)
        if (not in_data and header and header[1].upper() not in _NOT_HEADERS
                and not header[1].upper().startswith("END-")):
            finish(index)
            start, name = index + 1, header[1].upper()
    finish(len(raw))
    return bodies, None if bodies else "complete_unit_not_supplied"


@lru_cache(maxsize=128)
def _parse_body(text, first_line, source_format):
    return parse_statements(text, start_line=first_line, source_format=source_format,
                            max_tokens=16000)


def _location(span):
    return {"start_line": span.start_line, "end_line": span.end_line,
            "start_column": span.start_column, "end_column": span.end_column}


def build_syntax_guide(source_pages):
    """Rebind every fact and guard after trimming; cache only immutable parsing."""
    from business_synthesis import _supplied_source_spans

    eligible = [p for p in source_pages if p.get("evidence_id") and p.get("source_sha256")
                and isinstance(p.get("source_text"), str) and p["source_text"]
                and isinstance(p.get("start_line"), int) and p["start_line"] > 0
                and isinstance(p.get("end_line"), int)
                and p["end_line"] - p["start_line"] + 1 == len(p["source_text"].splitlines())]

    def source_key(page):
        return (page.get("relative_path"), page.get("source_sha256"), repr(page.get("include_chain") or []))

    # An evidence ID may be reused for the same copybook in distinct includes.
    # Resolve it inside its source instance, never through a global last-wins map.
    page_groups, conflicts = {}, set()
    for page in eligible:
        key, ref = source_key(page), page["evidence_id"]
        group = page_groups.setdefault(key, {})
        previous = group.get(ref)
        if previous and any(previous[field] != page[field] for field in ("start_line", "end_line", "source_text")):
            conflicts.add((key, ref))
        group[ref] = page
    eligible = [p for p in eligible if (source_key(p), p["evidence_id"]) not in conflicts]
    result = {"parser_version": PARSER_VERSION, "facts": [],
              "boundaries": [{"reason": "conflicting_reference_content"}] if conflicts else [],
              "interpretation": "local_syntax_not_execution_or_final_value",
              "semantic_execution_verified": False, "omitted_facts": 0}
    scanned, used_bytes = 0, 0
    for supplied in _supplied_source_spans(eligible, {p["evidence_id"] for p in eligible}):
        pages = page_groups[supplied["key"]]
        if not supplied["lines"]:
            continue
        text = supplied["source_text"]
        if scanned + len(text) > _MAX_SCAN:
            result["source_scan_truncated"] = True
            break
        scanned += len(text)
        units, problem = _bodies(text, min(supplied["lines"]))
        path, digest = supplied["key"][:2]
        if problem:
            if len(result["boundaries"]) < 8:
                result["boundaries"].append({"relative_path": path, "reason": problem})
            continue
        for name, body, first_line, source_format in units:
            parsed = _parse_body(body, first_line, source_format)
            for boundary in parsed.boundaries:
                if len(result["boundaries"]) < 8:
                    result["boundaries"].append({"relative_path": path,
                        "reason": boundary.reason, **_location(boundary.span)})
            for order, statement in enumerate(parsed.statements):
                guards = [{"kind": g.kind, "condition": g.condition, "outcome": g.outcome,
                           **_location(g.span), **({"selector": g.selector,
                           "alternatives": list(g.alternatives),
                           "prior_branches_must_not_match": [list(items) for items in g.prior_alternatives],
                           "is_other": g.is_other,
                           **({"selector_span": _location(g.selector_span)} if getattr(g, "selector_span", None) else {})} if g.kind == "EVALUATE" else {})}
                          for g in statement.guards]
                # Include scope headers and prior WHEN predicates as well as
                # the leaf. Every physical line must survive request trimming.
                first = first_line - 1
                last = first_line + len(body.splitlines()) - 1
                references, cursor = [], first
                while cursor <= last:
                    candidates = [ref for ref in supplied["supplied_reference_ids"]
                                  if pages[ref]["start_line"] <= cursor <= pages[ref]["end_line"]]
                    if not candidates:
                        break
                    ref = max(candidates, key=lambda ref: pages[ref]["end_line"])
                    references.append(ref)
                    cursor = pages[ref]["end_line"] + 1
                if not references or len(references) > 16:
                    result["omitted_facts"] += 1
                    continue
                covered = {n for ref in references for n in range(max(first, pages[ref]["start_line"]),
                           min(last, pages[ref]["end_line"]) + 1)}
                if len(covered) != last - first + 1:
                    continue
                fact = {"relative_path": path, "source_sha256": digest, "unit": name,
                        "syntax_order_in_unit": order, "kind": statement.kind,
                        "statement": statement.text, **_location(statement.span),
                        "guards": guards, "writes": list(statement.writes),
                        "effects_unknown": statement.effects_unknown,
                        "supplied_reference_ids": references}
                if statement.rounded:
                    fact["rounded"] = True
                chain = pages[references[0]].get("include_chain")
                if chain:
                    fact["include_chain"] = chain
                size = len(json.dumps(fact, ensure_ascii=False).encode("utf-8"))
                if len(result["facts"]) >= _MAX_FACTS or size > 2500 or used_bytes + size > _MAX_BYTES:
                    result["omitted_facts"] += 1
                    continue
                used_bytes += size
                result["facts"].append(fact)
    return result
