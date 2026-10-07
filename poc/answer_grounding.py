"""Source-bound call observations for answering business questions.

This module performs no model or filesystem calls. Observations identify
reading boundaries, not execution, final values, or an incorrect answer.
"""

from __future__ import annotations

from copy import deepcopy
import json
import re

from framework_semantics import visible_framework_facts


_MAX_SCAN_CHARACTERS = 80000
_MAX_RISKS = 32
_PROGRAM = re.compile(r"^\s*PROGRAM-ID\s*\.\s*['\"]?([A-Z0-9_$#@-]+)", re.I)
def _eligible_pages(source_pages):
    return [page for page in source_pages if page.get("evidence_id") and page.get("source_sha256")
            and isinstance(page.get("source_text"), str) and page["source_text"]
            and isinstance(page.get("start_line"), int) and page["start_line"] > 0
            and isinstance(page.get("end_line"), int)
            and page["end_line"] - page["start_line"] + 1 == len(page["source_text"].splitlines())]


def _call_tokens(body, first_line, source_format):
    from statement_parser import _BoundaryError, _Source, _lex

    location = _Source(body, first_line)
    try:
        yield location, _lex(location, source_format, 16000), None
        return
    except _BoundaryError:
        pass
    # The bounded AST lexer rejects numeric paragraph names such as
    # 1000-CHECK. The existing structural parser still isolates CALL headers
    # after those PERFORMs, without interpreting their execution or effects.
    from structural_index import SourceDocument, normalize_cobol_lines, parse_document
    lines, hint = normalize_cobol_lines(body, source_format)
    document = SourceDocument("supplied-fragment.cbl", "supplied", "utf-8", False,
                              hint, "COBOL", tuple(body.splitlines()), lines)
    for unit in parse_document(document).code_units:
        if unit.unit_type != "Statement" or unit.name != "CALL":
            continue
        location = _Source(unit.normalized_text, first_line + unit.start_line - 1)
        try:
            yield location, _lex(location, "free", 16000), first_line + unit.end_line - 1
        except _BoundaryError:
            continue


def _call_observations(pages):
    from business_synthesis import _supplied_source_spans
    from statement_parser import _CLAUSES, _TERMINATORS, _VERBS
    from syntax_evidence import _bodies

    calls, scanned, truncated = [], 0, False
    for span in _supplied_source_spans(pages, {page["evidence_id"] for page in pages}):
        if not span["lines"]:
            continue
        text = span["source_text"]
        if scanned + len(text) > _MAX_SCAN_CHARACTERS:
            truncated = True
            break
        scanned += len(text)
        units, _ = _bodies(text, min(span["lines"]))
        streams = (stream for _, body, first_line, source_format in units
                   for stream in _call_tokens(body, first_line, source_format))
        for location, tokens, header_end_line in streams:
            # Call-risk nomination needs lexical call headers, not a completed
            # value-flow AST. An unsupported earlier statement must not hide a
            # later call. The existing lexer masks comments and fixed columns,
            # and keeps quoted values as literal tokens.
            embedded = False
            for index, token in enumerate(tokens):
                if token.kind == "word" and token.value in {"EXEC", "END-EXEC"}:
                    embedded = token.value == "EXEC"
                    continue
                if embedded or token.kind != "word" or token.value != "CALL" or index + 1 >= len(tokens):
                    continue
                target = tokens[index + 1]
                if target.kind not in {"word", "literal"}:
                    continue
                header = []
                cursor = index + 2
                for candidate in tokens[cursor:]:
                    if candidate.value in _VERBS | _CLAUSES | _TERMINATORS:
                        break
                    header.append(candidate.value)
                    cursor += 1
                modes = [header[index + 1] for index, value in enumerate(header[:-1])
                         if value == "BY" and header[index + 1] in {"VALUE", "CONTENT"}]
                call_span = location.span(token.start, tokens[max(index + 1, cursor - 1)].end)
                end_line = max(call_span.end_line, header_end_line or 0)
                suffix = [item.value for item in tokens[cursor:cursor + 3]]
                references = [page["evidence_id"] for page in pages
                    if page.get("relative_path") == span["key"][0]
                    and page.get("source_sha256") == span["key"][1]
                    and repr(page.get("include_chain") or []) == span["key"][2]
                    and page["start_line"] <= end_line and page["end_line"] >= call_span.start_line]
                calls.append({"relative_path": span["key"][0], "source_sha256": span["key"][1],
                    "start_line": call_span.start_line, "end_line": end_line,
                    "include_chain": deepcopy(next((page.get("include_chain", []) for page in pages
                        if page["evidence_id"] in references), [])),
                    "target": target.value[1:-1].upper() if target.kind == "literal" else target.value,
                    "dynamic": target.kind != "literal", "copy_modes": sorted(set(modes)),
                    "has_exception_handler": suffix[:2] == ["ON", "EXCEPTION"] or suffix == ["NOT", "ON", "EXCEPTION"],
                    "supplied_reference_ids": sorted(set(references))})
    return calls, truncated


def answer_grounding_risks(source_pages, *, framework_references=(), framework_facts=(), business_map=None):
    """Nominate high-risk call boundaries using supplied syntax and navigation."""
    from structural_index import normalize_cobol_lines

    pages = _eligible_pages(source_pages)
    facts = visible_framework_facts(framework_facts, pages, framework_references)
    programs = {}
    for page in pages:
        for line in normalize_cobol_lines(page["source_text"])[0]:
            if match := _PROGRAM.match(line.text):
                programs.setdefault(match[1].upper(), set()).add(
                    (page.get("relative_path"), page["source_sha256"]))
    calls, truncated = _call_observations(pages)
    supplied_paths = {page["relative_path"] for page in pages if page.get("relative_path")}
    # The bounded AST deliberately stops at unsupported syntax. Navigation can
    # still nominate a call whose physical site is present in supplied source.
    for edge in (business_map or {}).get("relations", []):
        if edge.get("relation_type") not in {"CALLS", "CALL_TARGET_FROM"}:
            continue
        line = edge.get("caller_line")
        matches = [page for page in pages if isinstance(line, int)
            and page.get("relative_path") == edge.get("caller_path")
            and page["start_line"] <= line <= page["end_line"]]
        same_calls = [call for call in calls if call["relative_path"] == edge.get("caller_path")
                      and call["start_line"] == line and call["target"] == edge.get("target_name")]
        if not matches:
            continue
        if same_calls:
            for call in same_calls:
                call["target_path"] = edge.get("target_path")
                call["target_resolution"] = edge.get("resolution")
            continue
        page = matches[0]
        calls.append({"relative_path": page["relative_path"], "source_sha256": page["source_sha256"],
            "start_line": line, "end_line": line, "include_chain": deepcopy(page.get("include_chain", [])),
            "target": edge.get("target_name"), "dynamic": edge["relation_type"] == "CALL_TARGET_FROM",
            "target_path": edge.get("target_path"),
            "target_resolution": edge.get("resolution"),
            "copy_modes": [], "has_exception_handler": False,
            "supplied_reference_ids": sorted({item["evidence_id"] for item in matches})})

    reasons, seen = [], set()
    def add(kind, call):
        row = {"kind": kind, **call}
        key = json.dumps(row, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            reasons.append(row)

    for call in calls:
        # A resolved path identifies the selected implementation. A namesake
        # elsewhere cannot stand in for it; names are only a fallback when the
        # navigation has not selected a path and the supplied identity is unique.
        target_path = call.get("target_path")
        candidates = programs.get(call["target"], set())
        ambiguous = not target_path and (call.get("target_resolution") == "ambiguous" or len(candidates) > 1)
        source_supplied = (target_path in supplied_paths if target_path else
                           len(candidates) == 1 and not ambiguous)
        call["source_status"] = "supplied_excerpt" if source_supplied else "ambiguous" if ambiguous else "not_supplied"
        if call["copy_modes"]:
            add("parameter_copy_boundary", call)
        if call["dynamic"]:
            add("runtime_call_target", call)
        covered = any(fact.get("dependency_covered") and fact.get("target_name") == call["target"]
            and fact.get("relative_path") == call["relative_path"]
            and fact.get("source_sha256") == call["source_sha256"]
            and fact.get("start_line", 0) <= call["start_line"] <= fact.get("end_line", 0) for fact in facts)
        if not call["dynamic"] and not source_supplied and not covered:
            add("call_implementation_not_supplied", call)
    handlers = [call for call in calls if call["has_exception_handler"]]
    if len(handlers) > 1:
        for call in handlers:
            add("multiple_call_exception_sites", call)
    return {"required": bool(reasons), "reasons": reasons[:_MAX_RISKS],
            "omitted_risks": max(0, len(reasons) - _MAX_RISKS), "source_scan_truncated": truncated,
            "semantic_execution_verified": False}
