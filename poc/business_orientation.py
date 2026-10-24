"""Bounded business context projected from the source actually in a request.

No repository lookup, filesystem read or model inference occurs here. These
observations organize source syntax and documented intent for an explanation;
they do not establish execution, successful updates or business correctness.
"""

from collections import defaultdict
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import PurePosixPath
import re

from claim_support import supplied_statement_facts
from business_handoffs import build_business_handoffs, orientation_fact_id
from business_explanation_cache import cache_business_explanation
from cl_context import collect_cl_context, is_cl_path
from file_impact import collect_file_impact
from framework_semantics import visible_framework_facts
from structural_index import SourceDocument, normalize_cobol_lines, parse_document


ORIENTATION_VERSION = "business-orientation/v3"
_GROUPS = ("purpose_candidates", "entries", "programs", "relationships", "data_roles", "visible_rules", "handoffs")
_SHA = re.compile(r"[0-9a-fA-F]{64}\Z")
_PURPOSE = re.compile(r"^(?:purpose|function|business\s+(?:purpose|function)|description)\b"
                      r"|^(?:业务目的|業務目的|业务说明|業務說明|功能说明|功能說明|功能[:：])", re.I)


def project_business_orientation(orientation):
    """Share repeated version bindings in the wire representation only.

    Facts, conditions, references and source ranges remain intact. Every source
    location still binds to its exact version through ``source_versions[path]``;
    expanding that map reconstructs the canonical locations without a lookup.
    Unknown/custom representations are passed through unchanged.
    """
    if (not isinstance(orientation, dict) or orientation.get("version") != ORIENTATION_VERSION
            or "source_versions" in orientation or "source_location_version_binding" in orientation):
        return orientation
    versions, locations = {}, []
    for group in _GROUPS[:-1]:
        rows = orientation.get(group)
        if not isinstance(rows, list):
            return orientation
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("source_locations"), list):
                return orientation
            for location in row["source_locations"]:
                if not isinstance(location, dict):
                    return orientation
                path, sha = location.get("relative_path"), location.get("source_sha256")
                if (not isinstance(path, str) or not path or not isinstance(sha, str)
                        or not _SHA.fullmatch(sha) or versions.get(path, sha) != sha):
                    return orientation
                versions[path] = sha
                locations.append(location)
    if not locations:
        return orientation
    projected = deepcopy(orientation)
    projected["source_versions"] = versions
    projected["source_location_version_binding"] = "Every source_locations item binds to source_versions[relative_path]."
    for group in _GROUPS[:-1]:
        for row in projected[group]:
            for location in row["source_locations"]:
                del location["source_sha256"]
    # Tiny inputs do not benefit from the shared map. Never grow their wire.
    return projected if len(json.dumps(projected, ensure_ascii=False)) < len(json.dumps(orientation, ensure_ascii=False)) else orientation


def _metadata_bytes(orientation, limit):
    """Budget the existing lossless wire form before removing observations.

    Canonical rows keep their complete source bindings for local consumers.
    A provider request already shares repeated versions and navigation fields;
    counting those repetitions first would discard useful handoffs even when
    every original fact fits. Include the projection protocol in this allowance.
    Small orientations avoid the extra projection altogether.
    """
    size = len(json.dumps(orientation, ensure_ascii=False).encode("utf-8"))
    if size <= limit:
        return size
    from prompt_projection import project_prompt_payload
    projected = project_prompt_payload({"business_analysis_brief": {
        "business_orientation": project_business_orientation(orientation)}})
    return min(size, len(json.dumps(projected, ensure_ascii=False).encode("utf-8")))


def _trim_observations(orientation, limit):
    """Keep whole facts while bounding repeated provider projection work."""
    if _metadata_bytes(orientation, limit) <= limit:
        return
    original = {group: orientation[group] for group in _GROUPS}
    counts = {group: len(rows) for group, rows in original.items()}
    prefixes, transitions = [counts.copy()], set()
    while any(counts.values()):
        group = ("handoffs" if counts["handoffs"] else
                 max((key for key in _GROUPS if counts[key]), key=lambda key: counts[key]))
        counts[group] -= 1
        prefixes.append(counts.copy())
        # Wire tables and common-field sharing have small-list thresholds.
        # Inspect both sides rather than assuming byte size is monotone there.
        if counts[group] in {0, 1, 2, 3, 4}:
            transitions.update((len(prefixes) - 2, len(prefixes) - 1))

    measured = {}

    def fits(removed):
        if removed not in measured:
            candidate = {**orientation, "truncated": True,
                         **{group: original[group][:prefixes[removed][group]] for group in _GROUPS}}
            measured[removed] = _metadata_bytes(candidate, limit) <= limit
        return measured[removed]

    def first_fit(low, high):
        while low + 1 < high:
            middle = (low + high) // 2
            if fits(middle):
                high = middle
            else:
                low = middle
        return high

    removed_count = len(prefixes) - 1
    if fits(removed_count):
        removed_count = first_fit(0, removed_count)
        for transition in sorted(index for index in transitions if 0 < index < removed_count):
            if fits(transition):
                removed_count = first_fit(0, transition)
                break
    for group in _GROUPS:
        orientation[group] = original[group][:prefixes[removed_count][group]]
    # The schedule removes derived edges before any source observation, so a
    # retained edge always still has every original fact and condition.
    orientation["truncated"] = True


def _parse_run(path, sha, first, last, source_format, text):
    lines, hint = normalize_cobol_lines(text, source_format)
    document = SourceDocument(path, sha, "utf-8", False, hint, "COBOL", tuple(text.splitlines()), lines)
    parsed = parse_document(document)
    facts = ()
    if any(unit.name in {"MOVE", "COMPUTE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE", "IF"}
           for unit in parsed.code_units):
        facts = supplied_statement_facts({"relative_path": path, "source_sha256": sha,
            "start_line": first, "end_line": last, "source_text": text}, parsed_document=parsed)
    return parsed, facts


# Repeated brief/coverage construction often sees precisely the same request.
# Keep at most eight small captured runs, with both physical scope and content
# in the key; large runs are parsed without retaining another source copy.
_cached_parse_run = lru_cache(maxsize=8)(_parse_run)


@cache_business_explanation
def build_business_orientation(question, investigation, source_pages, *, business_map=None,
                               framework_facts=(), framework_references=(), max_items=32,
                               max_source_chars=262144, max_metadata_bytes=24000):
    """Rebuild after request trimming; every row carries currently visible refs.

    ``investigation`` supplies the existing plan, exclusive physical scopes and
    optionally bound CL facts. The map supplies current file versions, never
    semantic evidence. The metadata limit applies to the lossless provider
    projection; canonical facts retain their full bindings. These allowances
    do not limit source retrieval.
    """
    if (type(max_items) is not int or not 1 <= max_items <= 128
            or type(max_source_chars) is not int or max_source_chars < 1
            or type(max_metadata_bytes) is not int or max_metadata_bytes < 2000):
        raise ValueError("BUSINESS_ORIENTATION_BUDGET_INVALID")
    investigation, business_map = investigation or {}, business_map or {}
    result = {"version": ORIENTATION_VERSION,
              "scope": {"basis": "currently_supplied_source_and_references",
                        "semantic_execution_verified": False, "runtime_mode_verified": False,
                        "business_purpose_verified": False, "repository_reads": False,
                        "unknowns_limit_only_dependent_conclusions": True},
              **{key: [] for key in _GROUPS}, "available_facets": [], "unknowns": [],
              "truncated": False}

    def boundary(reason, **fields):
        row = {"reason": reason, **{key: value[:256] if isinstance(value, str) else value
                                   for key, value in fields.items()}}
        if row not in result["unknowns"] and len(result["unknowns"]) < 24:
            result["unknowns"].append(row)

    versions = business_map.get("source_versions") or {}
    scopes = (investigation.get("source_scope") or {}).get("ranges", [])
    physical, by_id, hashes, rejected = defaultdict(dict), {}, {}, set()
    page_rows, char_count = [], 0
    for offset, page in enumerate(source_pages):
        if offset >= 128:
            result["truncated"] = True
            boundary("orientation_page_budget")
            break
        path, sha, eid = (page.get(key) for key in ("relative_path", "source_sha256", "evidence_id"))
        first, last, text = (page.get(key) for key in ("start_line", "end_line", "source_text"))
        if (not isinstance(path, str) or not path or len(path) > 1024 or PurePosixPath(path).is_absolute()
                or ".." in PurePosixPath(path).parts or not isinstance(eid, str) or not eid
                or len(eid) > 256
                or not isinstance(sha, str) or not _SHA.fullmatch(sha)
                or type(first) is not int or type(last) is not int or first < 1 or last < first
                or not isinstance(text, str) or not text or page.get("span_truncated")
                or len(text.splitlines()) != last - first + 1):
            boundary("incomplete_or_invalid_source_page")
            continue
        if versions.get(path, sha) != sha:
            rejected.add(path)
            boundary("source_version_not_current", relative_path=path)
            continue
        if path in hashes and hashes[path] != sha:
            rejected.add(path)
        hashes[path] = sha
        identity = (path, sha, first, last, text)
        if eid in by_id and by_id[eid] != identity:
            rejected.update((path, by_id[eid][0]))
        by_id[eid] = identity
        path_scopes = [scope for scope in scopes if scope.get("relative_path") == path
                       and scope.get("exclusive", True)]
        if any(scope.get("source_sha256") != sha
               or type(scope.get("start_line")) is not int or type(scope.get("end_line")) is not int
               or scope["start_line"] < 1 or scope["end_line"] < scope["start_line"] for scope in path_scopes):
            rejected.add(path)
            boundary("exclusive_source_scope_not_current", relative_path=path)
            continue
        if char_count + len(text) > max_source_chars:
            result["truncated"] = True
            boundary("orientation_source_character_budget")
            continue
        char_count += len(text)
        page_rows.append(page)
        for number, line in enumerate(text.splitlines(), first):
            # Clip only after checking the original page's physical completeness.
            if path_scopes and not any(scope["start_line"] <= number <= scope["end_line"] for scope in path_scopes):
                continue
            previous = physical[path].get(number)
            if previous and previous[0] != line:
                rejected.add(path)
            physical[path][number] = (line, {*previous[1], eid} if previous else {eid})
    for path in rejected:
        physical.pop(path, None)
        boundary("conflicting_or_unusable_source_path", relative_path=path)

    # Adjacent pages of the same captured version form one physical run. Gaps
    # never acquire invented headers, declarations or continuation lines.
    runs, pages = [], []
    formats = {page["relative_path"]: page.get("format_hint", "auto") for page in page_rows}
    for path, rows in physical.items():
        blocks, block = [], []
        for number in sorted(rows):
            if block and number != block[-1] + 1:
                blocks.append(block)
                block = []
            block.append(number)
        if block:
            blocks.append(block)
        for block in blocks:
            first, last = block[0], block[-1]
            runs.append({"relative_path": path, "source_sha256": hashes[path],
                         "start_line": first, "end_line": last,
                         "source_text": "\n".join(rows[number][0] for number in block),
                         "format_hint": formats[path]})
        # Collectors consume original IDs, not synthetic run identifiers.
        for page in page_rows:
            if page["relative_path"] != path:
                continue
            kept, chunks = [], []
            for number in range(page["start_line"], page["end_line"] + 1):
                if number not in rows:
                    if kept:
                        chunks.append(kept)
                        kept = []
                    continue
                kept.append(number)
            if kept:
                chunks.append(kept)
            for chunk in chunks:
                pages.append({**page, "start_line": chunk[0], "end_line": chunk[-1],
                              "source_text": "\n".join(rows[number][0] for number in chunk)})

    visible_framework = visible_framework_facts(framework_facts, pages, framework_references)
    for fact in visible_framework:
        if fact.get("kind") == "framework_section" and fact.get("reason") != "documented_section_observed":
            boundary("framework_stage_control_scope_not_established", relative_path=fact.get("relative_path"),
                     target_name=fact.get("target_name"), framework_reason=fact.get("reason"))
    included_paths = {page["relative_path"] for page in pages if page.get("include_chain")}
    framework_stage_targets = {(fact.get("relative_path"), fact.get("program_name"), fact.get("target_name"))
        for fact in visible_framework if fact.get("kind") == "framework_section"
        and fact.get("reason") == "documented_section_observed"
        and fact.get("program_name") and fact.get("relative_path") not in included_paths}
    framework_stage_programs = {(path, program) for path, program, _ in framework_stage_targets}
    framework_syntax = defaultdict(list)
    if framework_stage_targets:
        from syntax_evidence import build_syntax_guide
        for fact in build_syntax_guide(pages)["facts"]:
            if fact.get("kind") in {"CALL", "PERFORM"} and not fact.get("include_chain"):
                framework_syntax[(fact["relative_path"], fact["source_sha256"],
                    fact["start_line"], fact["end_line"], fact["kind"])].append(fact)

    def provenance(locations):
        identifiers, supplied = set(), []
        for original in locations:
            path = original.get("relative_path")
            first, last = original.get("start_line"), original.get("end_line")
            rows = physical.get(path, {})
            if (type(first) is not int or type(last) is not int or first < 1 or last < first
                    or original.get("source_sha256", hashes.get(path)) != hashes.get(path)
                    or last - first + 1 > len(rows)
                    or any(number not in rows for number in range(first, last + 1))):
                return None
            if "statement" in original and original["statement"] != "\n".join(
                    rows[number][0] for number in range(first, last + 1)):
                return None
            identifiers.update(eid for number in range(first, last + 1) for eid in rows[number][1])
            location = {"relative_path": path, "source_sha256": hashes[path],
                        "start_line": first, "end_line": last}
            if location not in supplied:
                supplied.append(location)
        if not identifiers:
            return None
        return {"evidence_ids": sorted(identifiers), "supplied_reference_ids": sorted(identifiers),
                "source_locations": supplied}

    def add(group, row, locations, *, level="source_syntax", basis="supplied_source_syntax",
            references=()):
        refs = provenance(locations)
        if not refs:
            return
        item = {**deepcopy(row), "basis": basis, "interpretation_level": level, **refs}
        item["supplied_reference_ids"] = sorted({*refs["evidence_ids"], *references})
        item["orientation_id"] = orientation_fact_id(item)
        if item in result[group]:
            return
        if len(result[group]) >= max_items:
            result["truncated"] = True
            boundary("orientation_observation_budget", group=group)
            return
        result[group].append(item)

    statement_contexts, compilation_contexts = defaultdict(list), {}
    for run in runs:
        path, first = run["relative_path"], run["start_line"]
        if is_cl_path(path) or PurePosixPath(path).suffix.lower() in {".dds", ".lf", ".pf"}:
            continue
        parse = _cached_parse_run if len(run["source_text"].encode("utf-8")) <= 131072 else _parse_run
        parsed, statement_facts = parse(path, run["source_sha256"], first, run["end_line"],
                                       run["format_hint"], run["source_text"])
        document, hint = parsed.document, parsed.document.format_hint
        units = {unit.unit_id: unit for unit in parsed.code_units}
        section_definitions = defaultdict(list)
        procedure_definitions = defaultdict(list)
        program_definitions = defaultdict(list)
        for unit in parsed.code_units:
            if unit.unit_type == "Program":
                program_definitions[unit.program_name].append(unit)
            if unit.unit_type == "Section":
                section_definitions[(unit.program_name, unit.name)].append(unit)
            if unit.unit_type in {"Section", "Paragraph"}:
                procedure_definitions[(unit.program_name, unit.name)].append(unit)

        def unit_location(unit):
            return [{"relative_path": path, "start_line": first + unit.start_line - 1,
                     "end_line": first + unit.end_line - 1}]

        def enclosing_section(unit):
            """Bind containment only through an unbroken supplied source run."""
            if ((path, unit.program_name) not in framework_stage_programs
                    or len(program_definitions[unit.program_name]) != 1):
                return None
            visited, parent = set(), units.get(unit.parent_unit_id)
            while parent is not None and parent.unit_id not in visited:
                visited.add(parent.unit_id)
                if parent.program_name != unit.program_name:
                    return None
                if parent.unit_type == "Section":
                    if (parent.parse_status != "complete"
                            or len(section_definitions[(parent.program_name, parent.name)]) != 1
                            or parent.end_line > unit.start_line):
                        return None
                    header = unit_location(parent)[0]
                    return ({"name": parent.name, "start_line": header["start_line"],
                             "end_line": header["end_line"]},
                            {"relative_path": path, "start_line": header["start_line"],
                             "end_line": first + unit.end_line - 1})
                parent = units.get(parent.parent_unit_id)
            return None

        def framework_statement_context(unit):
            matching = framework_syntax.get((path, run["source_sha256"],
                first + unit.start_line - 1, first + unit.end_line - 1, unit.name), [])
            if len(matching) != 1:
                return {"framework_control_context": "not_bound"}, []
            fact = matching[0]
            identifiers = set(fact["supplied_reference_ids"])
            locations = [{key: page[key] for key in
                ("relative_path", "source_sha256", "start_line", "end_line")}
                for page in pages if page.get("evidence_id") in identifiers
                and page["relative_path"] == path and page["source_sha256"] == run["source_sha256"]
                and not page.get("include_chain")]
            return {"framework_control_context": "complete_supplied_syntax",
                    "guards": deepcopy(fact["guards"]), "unit": fact["unit"],
                    "syntax_order_in_unit": fact["syntax_order_in_unit"]}, locations

        controls, compilation, units_by_evidence = defaultdict(list), defaultdict(list), defaultdict(list)
        for unit in parsed.code_units:
            units_by_evidence[unit.evidence_id].append(unit.unit_id)
        for relation in parsed.relations:
            condition = units.get(relation.target_entity_id)
            if relation.relation_type == "CONTROL_DEPENDS_ON" and condition:
                controls[relation.from_entity_id].append((relation, condition))
            if relation.metadata.get("compilation_status") == "candidate":
                for identifier in (relation.from_entity_id, relation.target_entity_id,
                                   *units_by_evidence[relation.evidence_id]):
                    for guard in relation.metadata.get("compilation_guards", []):
                        if guard not in compilation[identifier]:
                            compilation[identifier].append(guard)

        def statement_context(unit):
            guards, locations = [], unit_location(unit)
            for relation, condition in controls[unit.unit_id]:
                span = unit_location(condition)[0]
                locations.append(span)
                guards.append({"kind": condition.name, "condition": condition.normalized_text,
                               "outcome": relation.metadata.get("outcome"), "status": relation.status,
                               "start_line": span["start_line"], "end_line": span["end_line"]})
            context = {"guards": guards, "control_context": "supplied_syntax_not_execution_path"}
            if compilation[unit.unit_id]:
                context.update(compilation_status="candidate", compilation_guards=[])
                for guard in compilation[unit.unit_id]:
                    number = first + guard["source_line"] - 1
                    context["compilation_guards"].append({**guard, "source_line": number})
                    locations.append({"relative_path": path, "start_line": number, "end_line": number})
            return context, locations

        for unit in parsed.code_units:
            if unit.parse_status == "candidate" and compilation[unit.unit_id]:
                context, locations = statement_context(unit)
                compilation_contexts[(path, first + unit.start_line - 1)] = (context, locations)
            if unit.unit_type == "Statement" and (unit.parse_status == "complete"
                    or (unit.parse_status == "candidate" and compilation[unit.unit_id])):
                context, locations = statement_context(unit)
                statement_contexts[(path, first + unit.start_line - 1, unit.name)].append((context, locations))

        for number, raw in enumerate(document.raw_lines, first):
            comment = (raw[7:].strip() if hint == "fixed" and len(raw) > 6 and raw[6] == "*"
                       else raw.split("*>", 1)[1].strip() if raw.lstrip().startswith("*>") else "")
            if comment and len(comment) <= 600 and _PURPOSE.search(comment):
                add("purpose_candidates", {"kind": "source_comment", "text": comment,
                    "purpose_binding": "author_documentation_candidate_not_verified_behavior"},
                    [{"relative_path": path, "start_line": number, "end_line": number}],
                    level="source_documentation", basis="verbatim_supplied_source_comment")
        for unit in parsed.code_units:
            if unit.parse_status != "complete":
                continue
            common = {"program_name": unit.program_name, "relative_path": path,
                      "statement": unit.normalized_text}
            if unit.unit_type == "Program":
                add("programs", {**common, "kind": "program_definition"}, unit_location(unit))
            elif unit.unit_type == "ProcedureSignature":
                add("entries", {**common, "kind": "program_entry",
                    "execution_mode": "unknown", "online_or_batch_verified": False}, unit_location(unit))
        for relation in parsed.relations:
            unit = units.get(relation.from_entity_id)
            if not unit or unit.parse_status != "complete":
                continue
            common = {"program_name": unit.program_name, "relative_path": path,
                      "statement": unit.normalized_text}
            context, locations = statement_context(unit)
            if relation.relation_type in {"CALLS", "CALL_TARGET_FROM"}:
                enclosing = enclosing_section(unit)
                if enclosing:
                    context["enclosing_section"] = enclosing[0]
                    syntax_context, syntax_locations = framework_statement_context(unit)
                    context.update(syntax_context)
                    locations = [*locations, enclosing[1], *syntax_locations]
                add("relationships", {**common, **context, "kind": "program_call",
                    "target_name": relation.target_name, "relation_type": relation.relation_type,
                    "target_binding": "literal_request" if relation.relation_type == "CALLS" else "dynamic_unresolved",
                    "execution_verified": False}, locations)
            elif (relation.relation_type in {"PERFORMS", "PERFORMS_THRU"}
                  and (path, unit.program_name, relation.target_name) in framework_stage_targets
                  and len(program_definitions[unit.program_name]) == 1):
                definitions = procedure_definitions[(unit.program_name, relation.target_name)]
                definition_locations = [unit_location(target)[0] for target in definitions]
                syntax_context, syntax_locations = framework_statement_context(unit)
                context.update(syntax_context)
                add("relationships", {**common, **context, "kind": "procedure_perform",
                    "target_name": relation.target_name, "relation_type": relation.relation_type,
                    "source_target_definitions": [{**location, "source_sha256": run["source_sha256"],
                        "kind": target.unit_type, "name": target.name, "parse_status": target.parse_status}
                        for target, location in zip(definitions, definition_locations)],
                    "perform_scope": ("range_request" if relation.relation_type == "PERFORMS_THRU"
                                      else "named_target_request"),
                    **({"target_range_end": relation.metadata["range_end"]}
                       if relation.metadata.get("range_end") else {}),
                    "execution_verified": False}, [*locations, *definition_locations, *syntax_locations])
            elif relation.relation_type in {"SELECTS_FROM", "UPDATES"} and unit.name == "EXEC_SQL":
                # DELETE FROM's target is a write, not a separate SELECT input.
                if relation.relation_type == "SELECTS_FROM" and re.match(
                        r"EXEC\s+SQL\s+DELETE\s+FROM\b", unit.normalized_text, re.I):
                    continue
                add("data_roles", {**common, **context, "kind": "sql_table_access", "table_name": relation.target_name,
                    "access_role": "read" if relation.relation_type == "SELECTS_FROM" else "write_request",
                    "database_identity_verified": False, "update_success_verified": False}, locations)
        for fact in statement_facts:
            if fact.get("predicate") not in {"assignment_statement", "condition_statement"}:
                continue
            locations = [{"relative_path": path, "start_line": fact["start_line"], "end_line": fact["end_line"]}]
            locations.extend({"relative_path": path, "start_line": guard["start_line"],
                              "end_line": guard["end_line"]} for guard in fact.get("guards", []))
            add("visible_rules", {key: fact[key] for key in ("predicate", "statement_kind", "program_name",
                "target", "expression", "condition", "guards", "source_text", "normal_result_condition",
                "compilation_status", "compilation_guards", "exception_effects_unverified") if key in fact}, locations)

    cl = collect_cl_context(pages, max_observations=max_items * 2, max_source_chars=max_source_chars)
    cl_rows = list(cl["observations"])
    # Indexed CL candidates may retain a verified lexical prefix not included
    # in this request. Accept only the exact statement and its supplied guards.
    for fact in (investigation.get("cl_context") or {}).get("observations", [])[:max_items * 2]:
        path, start, end = (fact.get(key) for key in ("relative_path", "start_line", "end_line"))
        locations = [{"relative_path": path, "source_sha256": fact.get("source_sha256"),
                      "start_line": start, "end_line": end}]
        if not provenance(locations):
            continue
        actual = "\n".join(physical[path][number][0] for number in range(start, end + 1))
        if fact.get("statement") == actual and fact.get("control_scope_supplied") is True:
            cl_rows.append(fact)
    for fact in cl_rows:
        if fact.get("status") != "confirmed_source_syntax":
            continue
        locations = fact.get("source_locations") or [{key: fact[key] for key in
            ("relative_path", "source_sha256", "start_line", "end_line")}]
        for guard in fact.get("guards", []):
            if guard.get("source_ref"):
                locations = [*locations, guard["source_ref"]]
        command = fact["command"]
        group = "entries" if command == "SBMJOB" else "relationships" if command == "CALL" else "data_roles"
        if command not in {"SBMJOB", "CALL", "OVRDBF", "DLTOVR"}:
            continue
        add(group, {key: fact[key] for key in ("kind", "command", "relative_path", "statement", "target_name",
            "target", "job_name", "parameters", "command_context", "parent_command", "guards", "arguments", "file", "tofile",
            "member", "declared_override_scope", "runtime_execution_verified", "effective_override_verified",
            "initial_library_list_origin", "inherited_overrides_verified") if key in fact}, locations)
    if cl_rows:
        boundary("batch_runtime_schedule_and_effective_overrides_unverified")

    files = collect_file_impact(pages, max_observations=max_items * 3, max_source_chars=max_source_chars)
    for fact in files["observations"]:
        if fact.get("status") != "confirmed_source_syntax" or fact.get("provenance_truncated"):
            continue
        if fact["kind"] not in {"file_declaration", "io_operation", "dds_pfile"}:
            continue
        projected = {key: fact[key] for key in ("kind", "relative_path", "program_name", "file_name",
            "assigned_name", "dynamic_assignment_symbol", "assignment_modifier", "operation", "operand",
            "operand_kind", "access_role", "file_binding_status", "record_name", "record_names", "physical_file_names",
            "identity_basis", "external_identity_verified") if key in fact}
        locations = fact["source_locations"]
        if fact["kind"] == "io_operation":
            contexts = statement_contexts.get((fact["relative_path"], locations[0]["start_line"], fact["operation"]), [])
            if len(contexts) == 1:
                projected.update(contexts[0][0])
                locations = [*locations, *contexts[0][1]]
            else:
                projected["control_context"] = "statement_control_context_not_bound"
        compilation_guards, compilation_locations = [], []
        for location in locations:
            conditional = compilation_contexts.get((location["relative_path"], location["start_line"]))
            if conditional:
                for guard in conditional[0].get("compilation_guards", []):
                    if guard not in compilation_guards:
                        compilation_guards.append(guard)
                compilation_locations.extend(conditional[1])
        if compilation_guards:
            projected.update(compilation_status="candidate", compilation_guards=compilation_guards)
            locations = [*locations, *compilation_locations]
        add("data_roles", projected, locations)
    for fact in visible_framework[:max_items]:
        locations = [{**span, "relative_path": span.get("relative_path", fact.get("relative_path")),
                      "source_sha256": span.get("source_sha256", fact.get("source_sha256"))}
                     for span in fact.get("source_ranges", [])]
        projected = {key: fact[key] for key in ("fact_id", "kind", "relative_path", "program_name", "target_name",
            "operation", "arguments", "argument", "function_field", "branches", "runtime_verified",
            "dependency_covered", "target_source_available", "reason") if key in fact}
        projected["callsite_locations"] = [{key: location[key] for key in
            ("relative_path", "source_sha256", "start_line", "end_line")} for location in locations
            if location.get("role") == "callsite"]
        if fact.get("kind") == "framework_section":
            projected["section_locations"] = [{key: location[key] for key in
                ("relative_path", "source_sha256", "start_line", "end_line")} for location in locations
                if location.get("role") == "section"]
        add("visible_rules", projected,
            locations, level="framework_convention", basis="source_bound_supplied_framework_convention",
            references=fact.get("reference_ids", []))

    definitions = {row["program_name"] for row in result["programs"]}
    for row in result["relationships"]:
        if row.get("kind") == "program_call" and row.get("target_name") not in definitions:
            boundary("called_implementation_not_in_supplied_projection", target_name=row.get("target_name"))
    if not result["purpose_candidates"]:
        boundary("business_purpose_not_established_by_supplied_documentation")
    if result["data_roles"]:
        boundary("source_data_roles_do_not_verify_runtime_object_identity_or_success")
    for collector in (cl, files):
        for item in collector.get("boundaries", [])[:8]:
            boundary(item["reason"], **{key: item[key] for key in ("relative_path",) if key in item})
    handoffs = build_business_handoffs(result, max_items=max_items + 1)
    result["handoffs"] = handoffs[:max_items]
    if len(handoffs) > max_items:
        result["truncated"] = True
        boundary("orientation_observation_budget", group="handoffs")
    # Drop entire observations, never references/conditions from a retained one.
    _trim_observations(result, max_metadata_bytes - 256)
    if result["truncated"]:
        boundary("orientation_is_bounded_not_a_complete_business_inventory")
    result["available_facets"] = [name for name, groups in (
        ("business_purpose", ("purpose_candidates",)), ("program_roles", ("entries", "programs", "relationships")),
        ("data_roles", ("data_roles",)), ("visible_rules", ("visible_rules",)), ("handoffs", ("handoffs",)))
        if any(result[group] for group in groups)]
    while _metadata_bytes(result, max_metadata_bytes) > max_metadata_bytes and result["unknowns"]:
        result["unknowns"].pop(0)
        result["truncated"] = True
    return result
