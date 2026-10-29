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
_MAX_DATA_DOCUMENTS = 128
_MAX_GROUP_FIELDS = 256
_MAX_MUTABLE_FIELD_OBSERVATIONS = 128
_MAX_MUTABLE_GROUP_BYTES = 16000
_PROGRAM = re.compile(r"^\s*PROGRAM-ID\s*\.\s*['\"]?([A-Z0-9_$#@-]+)", re.I)


def _eligible_pages(source_pages):
    return [page for page in source_pages if page.get("evidence_id") and page.get("source_sha256")
            and isinstance(page.get("source_text"), str) and page["source_text"]
            and isinstance(page.get("start_line"), int) and page["start_line"] > 0
            and isinstance(page.get("end_line"), int)
            and page["end_line"] - page["start_line"] + 1 == len(page["source_text"].splitlines())]


def _fragment_units(body, source_format):
    from structural_index import SourceDocument, normalize_cobol_lines, parse_document

    lines, hint = normalize_cobol_lines(body, source_format)
    document = SourceDocument("supplied-fragment.cbl", "supplied", "utf-8", False,
                              hint, "COBOL", tuple(body.splitlines()), lines)
    return parse_document(document).code_units


def _call_tokens(body, first_line, source_format):
    from statement_parser import _BoundaryError, _CLAUSES, _Source, _TERMINATORS, _Token, _lex

    location = _Source(body, first_line)
    # Numeric paragraph names are outside the AST lexer's identifier subset.
    # Mask just their first character for tokenization, then restore the exact
    # token value before parse_call. This cannot make such a parameter valid.
    lexical_text, numeric_names = body, set()
    for _ in range(64):
        try:
            tokens = _lex(_Source(lexical_text, first_line), source_format, 16000)
            tokens = tuple(_Token(body[token.start:token.end].upper(), "word", token.start, token.end)
                           if token.start in numeric_names else token for token in tokens)
            if any(token.value == "EXEC" and index + 1 < len(tokens) and tokens[index + 1].value == "SQL"
                   for index, token in enumerate(tokens)):
                # Embedded syntax uses its own masking and statement envelope.
                # A successful generic token scan is not a proof that a later
                # collected CALL header is a complete parameter list.
                break
            yield location, tokens, None, True
            return
        except _BoundaryError as exc:
            if exc.reason != "numeric_or_identifier_form_not_supported":
                break
            numeric_names.add(exc.offset)
            lexical_text = lexical_text[:exc.offset] + "N" + lexical_text[exc.offset + 1:]
    # The bounded AST lexer rejects numeric paragraph names such as
    # 1000-CHECK. The existing structural parser still isolates CALL headers
    # after those PERFORMs, without interpreting their execution or effects.
    for unit in _fragment_units(body, source_format):
        if unit.unit_type != "Statement" or unit.name != "CALL":
            continue
        location = _Source(unit.normalized_text, first_line + unit.start_line - 1)
        try:
            tokens = _lex(location, "free", 16000)
            # The legacy collector can isolate only the first physical line
            # of a multiline CALL. A visible clause/terminator is required
            # before its parameter list can be called complete.
            complete = any(token.value in _CLAUSES | _TERMINATORS for token in tokens[2:])
            yield location, tokens, first_line + unit.end_line - 1, complete
        except _BoundaryError as exc:
            # A recognizable CALL prefix still identifies an unknown boundary.
            # Never present its partial parameter list as a complete signature.
            prefix = _Source(unit.normalized_text[:exc.offset], location.start_line)
            try:
                yield location, _lex(prefix, "free", 16000), first_line + unit.end_line - 1, False
            except _BoundaryError:
                continue


def _actual_parameters(tokens, lexical_complete):
    from call_bindings import parse_call

    try:
        if not lexical_complete:
            raise ValueError("call_header_lexically_incomplete")
        if any(token.value == "," for token in tokens):
            raise ValueError("call_parameter_separator_not_supported")
        form = parse_call(" ".join(token.value for token in tokens))
    except ValueError as exc:
        return {"parameter_parse_status": "unknown", "parameter_parse_reason": str(exc),
                "actual_parameters": None}
    return {"parameter_parse_status": "parsed", "actual_parameters": [
        {"position": position, "name": parameter.name, "mode": parameter.mode}
        for position, parameter in enumerate(form.parameters, 1)]}


def _source_instance_fields(page):
    return {"include_chain": deepcopy(page.get("include_chain") or []),
            **({"replacement_trace": deepcopy(page["replacement_trace"])} if page.get("replacement_trace") else {}),
            **({"copy_instance": deepcopy(page["copy_instance"])} if page.get("copy_instance") is not None else {})}


def _source_location(pages, span, start_line, end_line):
    from business_synthesis import _source_instance_key

    references = [page for page in pages
        if _source_instance_key(page) == span["key"]
        and page["start_line"] <= end_line and page["end_line"] >= start_line]
    return {"relative_path": span["key"][0], "source_sha256": span["key"][1],
            "start_line": start_line, "end_line": end_line,
            **_source_instance_fields(references[0] if references else {}),
            "supplied_reference_ids": sorted({page["evidence_id"] for page in references})}


def _call_observations(pages):
    from business_synthesis import _supplied_source_spans
    from statement_parser import _CLAUSES, _TERMINATORS, _VERBS
    from structural_index import _SQLCodeScanner
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
        for location, tokens, header_end_line, lexical_complete in streams:
            # Call-risk nomination needs lexical call headers, not a completed
            # value-flow AST. An unsupported earlier statement must not hide a
            # later call. The existing lexer masks comments and fixed columns,
            # and keeps quoted values as literal tokens.
            embedded, sql_end = False, -1
            for index, token in enumerate(tokens):
                if token.start < sql_end:
                    continue
                if (token.kind == "word" and token.value == "EXEC"
                        and index + 1 < len(tokens) and tokens[index + 1].value == "SQL"):
                    # COBOL tokens alone cannot distinguish END-EXEC inside
                    # SQL comments. Match a real token against SQL-masked code.
                    sql_code = _SQLCodeScanner().feed(location.text[token.start:])
                    end = next((candidate.end for candidate in tokens[index + 2:]
                        if candidate.kind == "word" and candidate.value == "END-EXEC"
                        and sql_code[candidate.start - token.start:candidate.end - token.start] == "END-EXEC"),
                        len(location.text))
                    sql_end = end
                    continue
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
                calls.append({**_source_location(pages, span, call_span.start_line, end_line),
                    "target": target.value[1:-1].upper() if target.kind == "literal" else target.value,
                    "dynamic": target.kind != "literal", "copy_modes": sorted(set(modes)),
                    "has_exception_handler": suffix[:2] == ["ON", "EXCEPTION"] or suffix == ["NOT", "ON", "EXCEPTION"],
                    **_actual_parameters(tokens[index:cursor], lexical_complete)})
    return calls, truncated


def _sql_output_observations(pages):
    from business_synthesis import _supplied_source_spans
    from statement_facts import sql_code_only, sql_host_access

    observations, scanned, truncated = [], 0, False
    for span in _supplied_source_spans(pages, {page["evidence_id"] for page in pages}):
        if not span["lines"]:
            continue
        text = span["source_text"]
        if scanned + len(text) > _MAX_SCAN_CHARACTERS:
            truncated = True
            break
        scanned += len(text)
        if "EXEC" not in text.upper():
            continue
        # SQL owns an explicit EXEC/END-EXEC envelope. It does not need a
        # complete COBOL paragraph or the AST adapter's stricter format vote.
        for unit in _fragment_units(text, "auto"):
            if unit.name != "EXEC_SQL" or unit.parse_status != "complete":
                continue
            code, complete = sql_code_only(unit.normalized_text)
            if not complete or code.split()[:3] != ["EXEC", "SQL", "SELECT"]:
                continue
            _, outputs, supported = sql_host_access(code)
            if not supported or not outputs:
                continue
            first_line = min(span["lines"])
            observations.append({**_source_location(pages, span,
                first_line + unit.start_line - 1, first_line + unit.end_line - 1),
                "host_outputs": outputs, "operation": "SELECT_INTO",
                "post_sql_value_status": "depends_on_sql_execution_semantics_and_subsequent_assignments",
                "unchanged_value_guaranteed": False,
                "absence_of_local_assignment_proves_unchanged": False,
                "sqlcode_semantics_interpreted": False})
    return observations, truncated


def _supplied_data_documents(pages):
    """Parse contiguous physical excerpts without inventing missing ancestry."""
    from business_synthesis import _supplied_source_spans
    from structural_index import SourceDocument, normalize_cobol_lines, parse_document

    documents, scanned, truncated = [], 0, False
    spans = _supplied_source_spans(pages, {page["evidence_id"] for page in pages})
    ends, conflicting = {}, set()
    # Compatible overlaps have already merged. Remaining overlaps of one
    # physical version contradict each other and cannot support a binding.
    for span in spans:
        if span["lines"]:
            key = span["key"]
            if min(span["lines"]) <= ends.get(key, 0):
                conflicting.add(key)
            ends[key] = max(ends.get(key, 0), max(span["lines"]))
    for span in spans:
        text = span["source_text"]
        if not span["lines"] or span["key"] in conflicting:
            continue
        if scanned + len(text) > _MAX_SCAN_CHARACTERS or len(documents) >= _MAX_DATA_DOCUMENTS:
            truncated = True
            break
        scanned += len(text)
        lines, hint = normalize_cobol_lines(text)
        # A fragment may expose definitions, but is never a caller namespace
        # unless a real Program unit and the call site are both present.
        document = SourceDocument(span["key"][0], span["key"][1], "utf-8", False,
            hint, "cobol_fragment_or_copybook", tuple(text.splitlines()), lines)
        parsed = parse_document(document)
        documents.append({"span": span, "parsed": parsed,
            "units": {unit.unit_id: unit for unit in parsed.code_units},
            "programs": [unit for unit in parsed.code_units if unit.unit_type == "Program"],
            "first_line": min(span["lines"])})
    return documents, truncated


def _mutable_output_groups(call, pages, documents, business_map):
    """Bind observed children to a supplied caller or one explicit data COPY.

    This lists possible storage writes, not a complete layout or return values.
    Nested, replaced, disconnected, or ambiguous definitions remain unbound.
    """
    from procedure_expansion import _COPY, _REPLACE_WORD
    from structural_index import DATA_ITEM_RE
    from business_synthesis import _source_instance_key

    key = _source_instance_key(call)
    callers = [doc for doc in documents if doc["span"]["key"] == key
        and min(doc["span"]["lines"]) <= call["start_line"] <= max(doc["span"]["lines"])
        and len(doc["programs"]) == 1
        and doc["first_line"] + doc["programs"][0].start_line - 1 <= call["start_line"]]
    if len(callers) != 1:
        return []
    caller = callers[0]
    program = caller["programs"][0].name
    # A visible REPLACE can change declaration names/levels across an include.
    if any(unit.name == "REPLACE" or (unit.unit_type == "Statement"
           and _REPLACE_WORD.search(unit.normalized_text)) for unit in caller["units"].values()):
        return []
    scopes = [(caller, None)]
    for unit in caller["units"].values():
        if unit.unit_type != "Statement" or unit.name != "COPY" or unit.program_name != program:
            continue
        parent = caller["units"].get(unit.parent_unit_id)
        # Procedure COPYs do not provide a data declaration namespace.
        if parent is None or parent.unit_type != "Section" or parent.name not in {
                "WORKING-STORAGE", "LOCAL-STORAGE", "LINKAGE"}:
            continue
        match = _COPY.fullmatch(unit.normalized_text)
        if match is None:
            return []
        name = next(value for value in match.groups() if value is not None).upper()
        line = caller["first_line"] + unit.start_line - 1
        edges = [edge for edge in (business_map or {}).get("relations", [])
            if edge.get("relation_type") == "INCLUDES_COPY"
            and edge.get("caller_path") == call["relative_path"] and edge.get("caller_line") == line
            and str(edge.get("target_name", "")).upper() == name
            and edge.get("resolution") in {"confirmed", "unbound_copy_candidate"}
            and edge.get("target_path")]
        paths = {edge["target_path"] for edge in edges}
        candidates = []
        for doc in documents:
            if doc["programs"] or doc["first_line"] != 1:
                continue
            doc_key = doc["span"]["key"]
            chain = list(next((page.get("include_chain") or [] for page in pages
                if _source_instance_key(page) == doc_key), []))
            versions = {page["source_sha256"] for page in pages
                if page.get("relative_path") == doc_key[0]
                and _source_instance_key(page)[2:] == doc_key[2:]}
            if len(versions) != 1:
                continue
            direct_chain = (len(chain) == len(call.get("include_chain") or []) + 1
                and chain[:-1] == list(call.get("include_chain") or []) and isinstance(chain[-1], dict)
                and chain[-1].get("relative_path") == call["relative_path"]
                and chain[-1].get("source_hash") == call["source_sha256"]
                and chain[-1].get("line") == line and chain[-1].get("copy_name", "").upper() == name)
            mapped = not chain and len(paths) == 1 and doc_key[0] in paths and not call.get("include_chain")
            if direct_chain or mapped:
                candidates.append(doc)
        if len(candidates) == 1:
            # Unexpanded nested COPYs or REPLACE can introduce competing names
            # or alter a declaration's ancestry. Do not guess a partial scope.
            if any(member.unit_type == "Statement" and (member.name == "COPY"
                   or _REPLACE_WORD.search(member.normalized_text))
                   for member in candidates[0]["units"].values()):
                return []
            site = _source_location(pages, caller["span"], line, caller["first_line"] + unit.end_line - 1)
            scopes.append((candidates[0], site))
        else:
            return []

    groups = []
    for argument in call.get("mutable_outputs") or []:
        matches = [(doc, site, unit) for doc, site in scopes for unit in doc["units"].values()
            if unit.unit_type == "DataItem" and unit.name == argument
            and (site is not None or unit.program_name == program)]
        if len(matches) != 1:
            continue
        doc, site, root = matches[0]
        declaration = DATA_ITEM_RE.match(root.normalized_text)
        if declaration is None or declaration.group(3).strip().rstrip("."):
            continue
        level = int(declaration.group(1))
        if not 1 <= level <= 49:
            continue
        children = []
        for unit in doc["units"].values():
            if unit.unit_type != "DataItem" or unit.start_line <= root.start_line:
                continue
            child = DATA_ITEM_RE.match(unit.normalized_text)
            if (unit.parent_unit_id != root.parent_unit_id or unit.program_name != root.program_name
                    or child is None or int(child.group(1)) <= level):
                break
            if int(child.group(1)) <= 49 and unit.name != "FILLER":
                children.append(unit)
            if len(children) > _MAX_GROUP_FIELDS:
                children = []
                break
        if not children:
            continue
        # An unexpanded COPY/REPLACE between declarations could reset levels.
        if any(unit.name in {"COPY", "REPLACE"} and root.start_line < unit.start_line <= children[-1].end_line
               for unit in doc["units"].values()):
            continue
        start = doc["first_line"] + root.start_line - 1
        end = doc["first_line"] + children[-1].end_line - 1
        groups.append({"argument": argument, **_source_location(pages, doc["span"], start, end),
            "binding": "direct_copy" if site else "caller_declaration",
            "include_site": site, "observed_fields": [
                {"name": unit.name, "start_line": doc["first_line"] + unit.start_line - 1,
                 "end_line": doc["first_line"] + unit.end_line - 1} for unit in children],
            "complete_layout_verified": False, "return_values_verified": False})
    return groups


def answer_grounding_risks(source_pages, *, framework_references=(), framework_facts=(), business_map=None):
    """Nominate high-risk call boundaries using supplied syntax and navigation."""
    from structural_index import normalize_cobol_lines
    from business_synthesis import _source_instance_key

    pages = _eligible_pages(source_pages)
    facts = visible_framework_facts(framework_facts, pages, framework_references)
    programs = {}
    for page in pages:
        for line in normalize_cobol_lines(page["source_text"])[0]:
            if match := _PROGRAM.match(line.text):
                programs.setdefault(match[1].upper(), set()).add(
                    (page.get("relative_path"), page["source_sha256"]))
    calls, truncated = _call_observations(pages)
    supplied_versions = {}
    for page in pages:
        if page.get("relative_path"):
            supplied_versions.setdefault(page["relative_path"], set()).add(page["source_sha256"])
    navigation_versions = (business_map or {}).get("source_versions", {})
    unverified_navigation_calls = 0
    navigation_sites, navigation_scanned = {}, 0
    # Navigation may refine an observed call's target identity. A matching path
    # and line alone cannot establish that a stale relation is still a CALL in
    # the supplied version, or turn a comment/string into executable syntax.
    for edge in (business_map or {}).get("relations", []):
        if edge.get("relation_type") not in {"CALLS", "CALL_TARGET_FROM"}:
            continue
        line = edge.get("caller_line")
        matches = [page for page in pages if isinstance(line, int)
            and page.get("relative_path") == edge.get("caller_path")
            and page["start_line"] <= line <= page["end_line"]]
        same_calls = [call for call in calls if call["relative_path"] == edge.get("caller_path")
                      and call["start_line"] == line and call["target"] == edge.get("target_name")
                      and call["dynamic"] == (edge["relation_type"] == "CALL_TARGET_FROM")]
        if not matches:
            continue
        expected_version = navigation_versions.get(edge.get("caller_path"))
        if expected_version:
            same_calls = [call for call in same_calls if call["source_sha256"] == expected_version]
        if any(key in edge for key in ("include_chain", "replacement_trace", "copy_instance")):
            identity = _source_instance_key(edge)[2:]
            same_calls = [call for call in same_calls if _source_instance_key(call)[2:] == identity]
            matches = [page for page in matches if _source_instance_key(page)[2:] == identity]
        if not same_calls:
            # Short retrieved excerpts need not include a complete paragraph.
            # Retain their navigation fallback only after the structural parser
            # recognizes an actual matching CALL in the supplied text.
            from call_bindings import parse_call

            verified = []
            for page in matches:
                if expected_version and page["source_sha256"] != expected_version:
                    continue
                key = (page.get("relative_path"), page["source_sha256"], page["start_line"],
                       page["end_line"], page["source_text"], page.get("format_hint", "auto"))
                if key not in navigation_sites:
                    sites = set()
                    if navigation_scanned + len(page["source_text"]) <= _MAX_SCAN_CHARACTERS:
                        navigation_scanned += len(page["source_text"])
                        for unit in _fragment_units(page["source_text"], page.get("format_hint", "auto")):
                            if unit.unit_type != "Statement" or unit.name != "CALL":
                                continue
                            try:
                                form = parse_call(unit.normalized_text)
                            except ValueError:
                                continue
                            sites.add((page["start_line"] + unit.start_line - 1, form.target, form.dynamic))
                    else:
                        truncated = True
                    navigation_sites[key] = sites
                if (line, edge.get("target_name"), edge["relation_type"] == "CALL_TARGET_FROM") in navigation_sites[key]:
                    verified.append(page)
            identities = {_source_instance_key(page) for page in verified}
            if len(identities) == 1:
                page = verified[0]
                call = {"relative_path": page["relative_path"], "source_sha256": page["source_sha256"],
                    "start_line": line, "end_line": line, **_source_instance_fields(page),
                    "target": edge.get("target_name"), "dynamic": edge["relation_type"] == "CALL_TARGET_FROM",
                    "copy_modes": [], "has_exception_handler": False,
                    "parameter_parse_status": "unknown", "parameter_parse_reason": "call_header_not_parsed",
                    "actual_parameters": None,
                    "supplied_reference_ids": sorted({item["evidence_id"] for item in verified})}
                calls.append(call)
                same_calls = [call]
        identities = {_source_instance_key(call) for call in same_calls}
        if not same_calls or len(identities) != 1:
            unverified_navigation_calls += 1
            continue
        for call in same_calls:
            call["target_path"] = edge.get("target_path")
            call["target_resolution"] = edge.get("resolution")
            if navigation_versions.get(edge.get("target_path")):
                call["target_source_sha256"] = navigation_versions[edge["target_path"]]

    reasons, seen = [], set()
    remaining_fields = _MAX_MUTABLE_FIELD_OBSERVATIONS
    # Reserve each retained reason's JSON list delimiters; every group below
    # is charged its full UTF-8 representation plus a possible comma.
    remaining_group_bytes = max(0, _MAX_MUTABLE_GROUP_BYTES - 2 * _MAX_RISKS)
    def add(kind, call):
        nonlocal remaining_fields, remaining_group_bytes
        row = {"kind": kind, **call}
        key = json.dumps(row, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            if "mutable_output_groups" in row:
                groups, omitted = [], 0
                for group in row["mutable_output_groups"]:
                    fields = group["observed_fields"]
                    selected = fields[:remaining_fields]
                    bounded = {**group, "observed_fields": selected}
                    size = len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
                    while selected and size > remaining_group_bytes:
                        selected = selected[:-1]
                        bounded["observed_fields"] = selected
                        size = len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
                    omitted += len(fields) - len(selected)
                    if selected:
                        groups.append(bounded)
                        remaining_fields -= len(selected)
                        remaining_group_bytes -= size
                row["mutable_output_groups"] = groups
                row["mutable_output_fields_omitted"] = omitted
                row["mutable_output_observation_truncated"] = omitted > 0
            reasons.append(row)

    data_documents, data_truncated = _supplied_data_documents(pages) if any(
        call.get("actual_parameters") for call in calls) else ([], False)
    for call in calls:
        # A resolved path identifies the selected implementation. A namesake
        # elsewhere cannot stand in for it; names are only a fallback when the
        # navigation has not selected a path and the supplied identity is unique.
        target_path = call.get("target_path")
        target_version = call.get("target_source_sha256")
        path_versions = supplied_versions.get(target_path, set())
        candidates = programs.get(call["target"], set())
        ambiguous = (not target_version and len(path_versions) > 1 if target_path else
                     call.get("target_resolution") == "ambiguous" or len(candidates) > 1)
        source_supplied = ((target_version in path_versions if target_version else len(path_versions) == 1)
                          if target_path else
                           len(candidates) == 1 and not ambiguous)
        call["source_status"] = "supplied_excerpt" if source_supplied else "ambiguous" if ambiguous else "not_supplied"
        call["callee_source_status"] = call["source_status"]
        if not source_supplied:
            call["mutable_outputs"] = ([parameter["name"] for parameter in call["actual_parameters"]
                if parameter["mode"] == "REFERENCE"] if call["actual_parameters"] is not None else None)
            call["mutable_output_scope"] = "argument_storage_including_subordinate_fields"
            call["post_call_value_status"] = "requires_return_effects_or_subsequent_local_assignment"
            call["unchanged_value_guaranteed"] = False
            call["mutable_output_groups"] = _mutable_output_groups(call, pages, data_documents, business_map)
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
        elif not source_supplied and covered and call.get("mutable_outputs") != []:
            # A visible operation contract does not by itself establish that
            # every referenced argument retains its pre-call value.
            add("call_reference_effect_boundary", call)
    handlers = [call for call in calls if call["has_exception_handler"]]
    if len(handlers) > 1:
        for call in handlers:
            add("multiple_call_exception_sites", call)
    sql_outputs, sql_truncated = _sql_output_observations(pages)
    for observation in sql_outputs:
        add("sql_select_into_effect_boundary", observation)
    return {"required": bool(reasons) or bool(unverified_navigation_calls), "reasons": reasons[:_MAX_RISKS],
            "omitted_risks": max(0, len(reasons) - _MAX_RISKS),
            "unverified_navigation_calls": unverified_navigation_calls,
            "source_scan_truncated": truncated or sql_truncated or data_truncated,
            "semantic_execution_verified": False}
