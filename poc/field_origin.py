"""Conditional file-input origins from supplied physical source only.

This small bridge closes a source-location gap, not a runtime-value proof.
It deliberately requires a complete declaration prefix and an explicit
NOT AT END reader scope; unsupported aliases and paths remain unresolved.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
import hashlib
import re

from file_impact import collect_file_impact
from statement_facts import COBOL_VERBS, CobolLexError, lex_cobol
from statement_parser import parse_statements
from structural_index import DATA_ITEM_RE, normalize_cobol_lines


_NAME = re.compile(r"[A-Z][A-Z0-9_$#@-]*\Z", re.I)
_SHA = re.compile(r"[0-9a-f]{64}\Z", re.I)
_SAFE_BETWEEN = {"IF", "ELSE", "END-IF", "MOVE", "COMPUTE", "ADD", "SUBTRACT",
                 "MULTIPLY", "DIVIDE", "DISPLAY", "CONTINUE"}


def _tokens(text):
    tokens = list(lex_cobol(text))
    while tokens and tokens[-1].value == ".":
        tokens.pop()
    return tuple((token.kind, token.value) for token in tokens)


def _source(pages, path, sha, limit):
    """Require a single physical instance, contiguous from its first line."""
    rows, ids, characters = {}, defaultdict(set), 0
    for page in pages:
        if page.get("relative_path") != path:
            continue
        if page.get("source_sha256") != sha:
            return None
        first, last, text = (page.get(key) for key in ("start_line", "end_line", "source_text"))
        if (page.get("span_truncated") or page.get("include_chain") or page.get("replacement_trace")
                or not page.get("evidence_id") or not isinstance(text, str) or not text
                or type(first) is not int or type(last) is not int or first < 1 or last < first):
            return None
        physical = text.split("\n")
        # Captured pages use join(lines); permit an ordinary final newline too.
        if len(physical) == last - first + 2 and physical[-1] == "":
            physical.pop()
        if len(physical) != last - first + 1:
            return None
        characters += len(text)
        if characters > limit:
            return None
        for number, line in enumerate(physical, first):
            if number in rows and rows[number] != line:
                return None
            rows[number] = line
            ids[number].add(page["evidence_id"])
    if not rows or min(rows) != 1 or len(rows) != max(rows):
        return None
    return rows, ids


def _read_scopes(tokens, line_at):
    """Only explicit, unnested sequential READ success branches are handled."""
    for index, token in enumerate(tokens):
        if token.kind != "word" or token.value != "READ":
            continue
        end = next((offset for offset in range(index + 1, len(tokens))
                    if tokens[offset].kind != "literal"
                    and tokens[offset].value in {"READ", "END-READ", "."}), None)
        if end is None or tokens[end].value != "END-READ":
            continue
        clauses = []
        for offset in range(index + 1, end):
            values = [item.value if item.kind == "word" else None for item in tokens[offset:offset + 3]]
            if values == ["NOT", "AT", "END"]:
                clauses.append(("NOT AT END", offset, offset + 3))
            elif values[:2] == ["AT", "END"] and tokens[offset - 1].value != "NOT":
                clauses.append(("AT END", offset, offset + 2))
        if len([clause for clause in clauses if clause[0] == "NOT AT END"]) != 1:
            continue
        header = [item.value for item in tokens[index:clauses[0][1]]]
        if (len(header) < 2 or not _NAME.fullmatch(header[1])
                or header[2:] not in ([], ["NEXT"], ["NEXT", "RECORD"])):
            continue
        for position, (clause, offset, body) in enumerate(clauses):
            if clause == "NOT AT END":
                stop = clauses[position + 1][1] if position + 1 < len(clauses) else end
                yield {"file_name": header[1], "read_line": line_at(token.start),
                       "end_line": line_at(tokens[end].start), "body": body, "stop": stop,
                       "body_line": line_at(tokens[body].start) if body < stop else None}


def supplied_file_read_origins(candidates, pages, *, max_source_chars=131072, max_paths=8):
    """Return (resolved path/field pairs, source witnesses), never known values.

    Every nominated occurrence must be an exact supplied assignment within
    the same explicit success branch. The caller must retain any stronger
    unresolved reaching-definition witness rather than override it here.
    """
    if (type(max_source_chars) is not int or max_source_chars < 1
            or type(max_paths) is not int or max_paths < 1):
        raise ValueError("File origin limits must be positive integers")
    requested = defaultdict(list)
    for row in candidates:
        if row.get("kind") not in {"formula", "business_steps", "inputs"}:
            continue
        for field in row.get("reads", ()):
            if (isinstance(row.get("relative_path"), str) and row["relative_path"]
                    and isinstance(field, str) and _NAME.fullmatch(field)):
                requested[(row.get("relative_path"), field)].append(row)
    resolved, witnesses = set(), []
    paths = list(dict.fromkeys(path for path, _ in requested))[:max_paths]
    for path in paths:
        local = [row for (owner, _), values in requested.items() if owner == path for row in values]
        hashes = {row.get("source_sha256") for row in local}
        if len(hashes) != 1:
            continue
        sha = next(iter(hashes))
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            continue
        captured = _source(pages, path, sha, max_source_chars)
        if captured is None:
            continue
        physical, identifiers = captured
        text = "\n".join(physical[number] for number in sorted(physical))
        lines, _ = normalize_cobol_lines(text, "auto")
        normalized = "\n".join(line.text for line in lines)
        if not re.search(r"\bFILE\s+SECTION\s*\.", normalized, re.I):
            continue
        try:
            tokens = list(lex_cobol(normalized))
        except CobolLexError:
            continue
        words = [token.value for token in tokens if token.kind == "word"]
        if (words.count("PROGRAM-ID") != 1 or words.count("PROCEDURE") != 1
                or any(word in words for word in ("COPY", "REDEFINES", "RENAMES", "OCCURS"))
                or any(line.text.lstrip().startswith(">>") for line in lines)):
            continue
        starts, offset = [], 0
        for line in lines:
            starts.append(offset)
            offset += len(line.text) + 1
        def line_at(position):
            return lines[max(0, bisect_right(starts, position) - 1)].start_line
        scopes = list(_read_scopes(tokens, line_at))
        if not scopes:
            continue
        counts = Counter(match.group(2).upper() for line in lines
                         if (match := DATA_ITEM_RE.match(line.text)))
        same_pages = [page for page in pages if page.get("relative_path") == path]
        facts = collect_file_impact(same_pages, max_source_chars=max_source_chars)
        if facts["truncated"] or facts["boundaries"]:
            continue
        observations = facts["observations"]
        declaration_end = next((line.end_line for line in lines
                                if re.match(r"PROCEDURE\s+DIVISION\b", line.text, re.I)), None)
        if declaration_end is None:
            continue
        for (owner, field), readers in requested.items():
            if owner != path or counts[field] != 1:
                continue
            bindings = [item for item in observations if item["kind"] == "record_binding"
                        and field in item["fields"] and not item.get("copy_member")]
            if len(bindings) != 1:
                continue
            binding = bindings[0]
            # Do not collapse several record alternatives or nested group
            # aliases to one field name. Those need the full storage model.
            sibling_records = [item for item in observations if item["kind"] == "record_binding"
                               and item["file_name"] == binding["file_name"]]
            field_lines = [line.text for line in lines if (match := DATA_ITEM_RE.match(line.text))
                           and match.group(2).upper() in binding["fields"]]
            if (len(sibling_records) != 1 or any(not re.search(r"\bPIC(?:TURE)?\b", line, re.I)
                                                for line in field_lines)):
                continue
            declarations = [item for item in observations if item["kind"] == "file_declaration"
                            and item["file_name"] == binding["file_name"]]
            if len(declarations) != 1 or counts[binding["record_name"]] != 1:
                continue
            matched, locations, complete = [], [], True
            for reader in readers:
                occurrences = reader.get("occurrences") or (
                    [reader] if reader.get("occurrence_count", 1) == 1 else [])
                if not occurrences:
                    complete = False
                for occurrence in occurrences:
                    first, last = occurrence.get("start_line"), occurrence.get("end_line")
                    if (type(first) is not int or type(last) is not int or first < 1 or last < first
                            or last > len(physical) or occurrence.get("source_sha256", sha) != sha):
                        complete = False
                        continue
                    actual = "\n".join(line.text for line in lines
                                       if first <= line.start_line and line.end_line <= last)
                    try:
                        exact = _tokens(actual) == _tokens(reader["statement"])
                        parsed = parse_statements(actual)
                        reads = (parsed.nodes[0].reads if parsed.complete and len(parsed.nodes) == 1
                                 and not parsed.nodes[0].effects_unknown else ())
                    except (CobolLexError, KeyError):
                        exact, reads = False, []
                    matches = [scope for scope in scopes if scope["file_name"] == binding["file_name"]
                               and scope["body_line"] is not None and scope["body_line"] <= first
                               and last < line_at(tokens[scope["stop"]].start)]
                    if not exact or field not in reads or len(matches) != 1:
                        complete = False
                        continue
                    scope = matches[0]
                    read_facts = [item for item in observations if item["kind"] == "io_operation"
                                  and item["operation"] == "READ" and item["file_name"] == binding["file_name"]
                                  and item["source_locations"][0]["start_line"] == scope["read_line"]
                                  and item.get("file_binding_status") == "confirmed_source_syntax"
                                  and item.get("record_candidate_count") == 1 and field in item["fields"]]
                    if len(read_facts) != 1:
                        complete = False
                        continue
                    preceding = [token for token in tokens[scope["body"]:scope["stop"]]
                                 if line_at(token.start) < first]
                    if any(token.kind == "word" and token.value in COBOL_VERBS - _SAFE_BETWEEN
                           for token in preceding):
                        complete = False
                        continue
                    clobbers = [item for item in observations if item["kind"] == "field_assignment"
                                and {field, binding["record_name"]}.intersection(item["written_fields"])
                                and any(scope["body_line"] <= ref["start_line"] < first
                                        for ref in item["source_locations"])]
                    if clobbers:
                        complete = False
                        continue
                    matched.append({"start_line": first, "end_line": last,
                                    "read_line": scope["read_line"], "read_end_line": scope["end_line"]})
                    locations.extend([{"relative_path": path, "start_line": 1, "end_line": declaration_end},
                                      *binding["source_locations"], *declarations[0]["source_locations"],
                                      {"relative_path": path, "start_line": scope["read_line"],
                                       "end_line": scope["end_line"]}])
            if not complete or not matched:
                continue
            refs = []
            for first, last in sorted({(ref["start_line"], ref["end_line"]) for ref in locations}):
                excerpt = "\n".join(physical[number] for number in range(first, last + 1))
                refs.append({"relative_path": path, "source_sha256": sha, "start_line": first, "end_line": last,
                             "excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest()})
            evidence_ids = sorted({identifier for ref in refs for number in range(ref["start_line"], ref["end_line"] + 1)
                                   for identifier in identifiers[number]})
            resolved.add((path, field))
            witnesses.append({"relative_path": path, "source_sha256": sha, "field": field,
                "status": "source_origin_supplied", "basis": "supplied_fd_record_and_read_success_branch",
                "file_name": binding["file_name"], "record_name": binding["record_name"],
                "assigned_name": declarations[0].get("assigned_name"), "evidence_ids": evidence_ids,
                "source_refs": refs, "reader_occurrences": matched, "unknown_reasons": [],
                "activation_semantics": ["READ succeeds and control reaches this NOT AT END assignment"],
                "runtime_value_known": False, "execution_verified": False,
                "external_identity_verified": False, "input_validity_verified": False})
    return resolved, witnesses
