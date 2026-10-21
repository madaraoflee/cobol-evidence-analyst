"""Versioned static activation evidence, separate from runtime scheduling.

Indexed relations nominate callers. Their original statements and guards must
be supplied before obligations are satisfied. Parsing a truncated answer
excerpt never establishes an empty control or compilation scope.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from compilation_context import resolve_compilation_context
from parser_adapter import BuiltinParserAdapter, _normalized_source
from statement_facts import dependency_facts

_MAX_PATHS, _MAX_CALLS, _MAX_LOOKUP_PATHS = 8, 64, 32
_MAX_SOURCE_CHARACTERS, _MAX_SOURCE_PAGES = 262144, 64


def _indexed_source(db, path, sha, max_characters):
    rows = db.execute("SELECT p.start_line,p.end_line,p.span_truncated,"
        "COALESCE(e.text,NULLIF(p.fallback_text,''),'') AS text FROM repo_pages p "
        "LEFT JOIN evidence_spans e ON e.evidence_id=p.evidence_id AND e.source_sha256=p.source_sha256 "
        "AND e.relative_path=p.relative_path AND e.start_line=p.start_line AND e.end_line=p.end_line "
        "WHERE p.relative_path=? AND p.source_sha256=? ORDER BY p.start_line LIMIT ?",
        (path, sha, _MAX_SOURCE_PAGES + 1)).fetchall()
    lines, characters = {}, 0
    for row in rows[:_MAX_SOURCE_PAGES]:
        text = row["text"]
        if row["span_truncated"] or not text or characters + len(text) > max_characters:
            break
        physical = text.split("\n")
        if len(physical) != row["end_line"] - row["start_line"] + 1:
            break
        lines.update(enumerate(physical, row["start_line"]))
        characters += len(text)
    return lines


def _caller_paths(db, roots, names, mapping, tables):
    """Use existing target or FTS/path indexes, never scan all relations."""
    paths = list(dict.fromkeys(edge.get("caller_path") for edge in mapping.get("relations", ())
        if edge.get("target_path") in roots and edge.get("relation_type") in {"CALLS", "CALL_TARGET_FROM"}
        and edge.get("caller_path")))
    gaps = []
    if not names:
        return paths, gaps
    target_index = next((row["name"] for row in db.execute("PRAGMA index_list('relations')")
        if next(iter(db.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno LIMIT 1",
            (row["name"],))), [None])[0] == "target_name"), None)
    if target_index:
        slots = ",".join("?" for _ in names)
        index = '"' + target_index.replace('"', '""') + '"'
        rows = db.execute(f"SELECT DISTINCT relative_path FROM relations INDEXED BY {index} "
            f"WHERE target_name IN ({slots}) AND relation_type IN ('CALLS','CALL_TARGET_FROM') "
            "ORDER BY relative_path LIMIT ?", (*names, _MAX_LOOKUP_PATHS + 1)).fetchall()
        paths.extend(row[0] for row in rows[:_MAX_LOOKUP_PATHS])
        if len(rows) > _MAX_LOOKUP_PATHS:
            gaps.append({"kind": "caller_invocation", "reason": "caller_lookup_frontier", "remaining_at_least": 1})
    elif {"repo_fts", "repo_pages"} <= tables:
        query = " OR ".join('"' + name.replace('"', '""') + '"' for name in names)
        total = db.execute("SELECT COUNT(DISTINCT p.relative_path) FROM repo_fts "
            "JOIN repo_pages p ON p.page_id=repo_fts.rowid WHERE repo_fts MATCH ?", (query,)).fetchone()[0]
        rows = db.execute("SELECT DISTINCT p.relative_path FROM repo_fts "
            "JOIN repo_pages p ON p.page_id=repo_fts.rowid WHERE repo_fts MATCH ? "
            "ORDER BY p.relative_path LIMIT ?", (query, _MAX_LOOKUP_PATHS)).fetchall()
        if rows:
            path_slots, name_slots = ",".join("?" for _ in rows), ",".join("?" for _ in names)
            paths.extend(row[0] for row in db.execute("SELECT DISTINCT relative_path FROM relations "
                f"WHERE relative_path IN ({path_slots}) AND target_name IN ({name_slots}) "
                "AND relation_type IN ('CALLS','CALL_TARGET_FROM') ORDER BY relative_path",
                (*(row[0] for row in rows), *names)))
        if total > len(rows):
            gaps.append({"kind": "caller_invocation", "reason": "caller_lookup_frontier",
                "candidate_path_count": total, "remaining": total - len(rows)})
    else:
        gaps.append({"kind": "caller_invocation", "reason": "caller_lookup_index_unavailable"})
    return list(dict.fromkeys(paths)), gaps


def _candidates(database_path, business_map):
    all_roots = tuple(dict.fromkeys(business_map.get("direct_paths", ())))
    roots = all_roots[:_MAX_PATHS]
    if not database_path or not roots:
        return [], [], [{"kind": "caller_invocation", "reason": "activation_index_unavailable"}]
    entries, callers, gaps = [], [], []
    if len(all_roots) > len(roots):
        gaps.append({"kind": "entry_point", "reason": "entry_path_budget", "remaining": len(all_roots)-len(roots)})
    with closing(sqlite3.connect(Path(database_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"source_files", "relations", "code_units", "evidence_spans", "symbols"} <= tables:
            return [], [], [{"kind": "caller_invocation", "reason": "activation_index_unavailable"}]
        slots = ",".join("?" for _ in roots)
        names = tuple(dict.fromkeys(row[0] for row in db.execute("SELECT name FROM symbols "
            f"WHERE relative_path IN ({slots}) AND symbol_type='Program' ORDER BY relative_path,name", roots)))
        caller_paths, lookup_gaps = _caller_paths(db, set(roots), names, business_map, tables)
        gaps.extend(lookup_gaps)
        if len(caller_paths) > _MAX_PATHS:
            gaps.append({"kind": "caller_invocation", "reason": "caller_path_budget", "remaining": len(caller_paths)-_MAX_PATHS})
        scoped = list(dict.fromkeys([*roots, *caller_paths[:_MAX_PATHS]]))
        options = db.execute("SELECT value FROM metadata WHERE key='source_options'").fetchone()
        context = resolve_compilation_context(json.loads(options[0]).get("compilation_context", {}) if options else None)
        remaining_characters = _MAX_SOURCE_CHARACTERS
        for path in scoped:
            file = db.execute("SELECT sha256,line_count FROM source_files WHERE relative_path=?", (path,)).fetchone()
            if not file:
                continue
            sha = file["sha256"]
            name_slots = ",".join("?" for _ in names)
            call_rows = db.execute("SELECT r.*,u.parse_status,u.name AS unit_name,e.start_line,e.end_line,e.text,"
                "s.relative_path AS target_path FROM relations r JOIN code_units u ON u.unit_id=r.from_entity_id "
                "JOIN evidence_spans e ON e.evidence_id=r.evidence_id LEFT JOIN symbols s ON s.symbol_id=r.target_entity_id "
                f"WHERE r.relative_path=? AND r.relation_type IN ('CALLS','CALL_TARGET_FROM') AND r.target_name IN ({name_slots}) "
                "AND e.source_sha256=? ORDER BY e.start_line,r.relation_id LIMIT ?", (path, *names, sha, _MAX_CALLS+1)).fetchall() \
                if path in caller_paths and names else []
            lines = _indexed_source(db, path, sha, remaining_characters) if call_rows and "repo_pages" in tables else {}
            remaining_characters -= sum(len(line) + 1 for line in lines.values())
            complete = len(lines) == file["line_count"] and all(number in lines for number in range(1, file["line_count"]+1))
            parsed, statuses = None, {}
            if complete:
                source = "\n".join(lines[number] for number in range(1, file["line_count"]+1))
                parsed = BuiltinParserAdapter().parse(source, relative_path=path, source_sha256=sha, context=context)
                _, statuses, _, _, _ = _normalized_source(source, context)

            def reference(row):
                first, last = row["start_line"], row["end_line"]
                expected = {number: lines[number] for number in range(first, last+1) if number in lines}
                if len(expected) != last-first+1:
                    text = row["text"].split("\n")
                    expected = dict(enumerate(text, first)) if len(text) == last-first+1 else {}
                return {"relative_path": path, "source_sha256": sha, "start_line": first,
                    "end_line": last, "expected_lines": expected}

            if path in roots:
                for row in db.execute("SELECT u.unit_id,u.parse_status,e.start_line,e.end_line,e.text FROM code_units u "
                    "JOIN evidence_spans e USING(evidence_id) WHERE u.relative_path=? AND "
                    "u.unit_type='ProcedureSignature' AND e.source_sha256=? ORDER BY e.start_line LIMIT 9", (path, sha)):
                    entries.append({"relative_path": path, "line": row["start_line"], "source_sha256": sha,
                        "occurrence_id": row["unit_id"], "source_ref": reference(row),
                        "compilation_status": "candidate" if row["parse_status"] == "candidate" or
                            statuses.get(row["start_line"], True) is not True else "active"})
            seen_occurrences = set()
            for row in call_rows:
                if len(callers) >= _MAX_CALLS:
                    gaps.append({"kind": "caller_invocation", "reason": "caller_statement_budget", "remaining_at_least": 1})
                    break
                if not any(kind == row["relation_type"] and target == row["target_name"]
                           for kind, target, _ in dependency_facts(row["text"])):
                    continue
                metadata = json.loads(row["metadata_json"])
                statements = [statement for statement in parsed.statements
                    if statement.statement_type == "CALL" and statement.source_refs[0].start_line == row["start_line"]
                    and any(kind == row["relation_type"] and target == row["target_name"]
                            for kind, target, _ in dependency_facts(statement.text))] if parsed else []
                for statement in statements or (None,):
                    occurrence = statement.occurrence_id if statement else row["from_entity_id"]
                    if occurrence in seen_occurrences:
                        continue
                    seen_occurrences.add(occurrence)
                    if len(callers) >= _MAX_CALLS:
                        gaps.append({"kind": "caller_invocation", "reason": "caller_statement_budget", "remaining_at_least": 1})
                        break
                    conditions, condition_refs = [], []
                    scope_known = bool(statement and parsed.parse_complete)
                    if statement:
                        conditions = [dict(guard) for guard in statement.scope]
                        for guard in conditions:
                            span = guard.get("span") or {"start_line": guard.get("source_line"), "end_line": guard.get("source_line")}
                            if type(span.get("start_line")) is int:
                                selector = guard.get("selector_span")
                                if selector:
                                    span = {"start_line": min(span["start_line"], selector["start_line"]),
                                        "end_line": max(span["end_line"], selector["end_line"])}
                                if guard.get("outcome") is False:
                                    span = {"start_line": span["start_line"], "end_line": row["start_line"]}
                                condition_refs.append(reference({**span, "text": ""}))
                            else:
                                condition_refs.append(None)
                                scope_known = False
                    else:
                        controls = db.execute("SELECT r.metadata_json,r.status,u.name,e.start_line,e.end_line,e.text "
                            "FROM relations r JOIN code_units u ON u.unit_id=r.target_entity_id "
                            "JOIN evidence_spans e ON e.evidence_id=u.evidence_id "
                            "WHERE r.from_entity_id=? AND r.relation_type='CONTROL_DEPENDS_ON' AND e.source_sha256=? "
                            "ORDER BY e.start_line", (row["from_entity_id"], sha)).fetchall()
                        scope_known = metadata.get("source_index_kind") != "business_sparse" and row["parse_status"] == "complete"
                        for control in controls:
                            detail = json.loads(control["metadata_json"])
                            conditions.append({"kind": detail.get("control_kind", control["name"]),
                                "condition": control["text"], "outcome": detail.get("outcome"),
                                "span": {"start_line": control["start_line"], "end_line": control["end_line"]}})
                            condition_refs.append(reference(control))
                            scope_known &= control["status"] == "confirmed"
                    status = "candidate" if metadata.get("compilation_status") == "candidate" or row["parse_status"] == "candidate" \
                        or statement and statement.compilation_status == "candidate" else "active"
                    callers.append({"relative_path": path, "line": row["start_line"], "source_sha256": sha,
                        "target_name": row["target_name"], "relation_type": row["relation_type"],
                        "occurrence_id": statement.occurrence_id if statement else row["from_entity_id"],
                        "source_ref": reference(row), "condition_refs": condition_refs, "conditions": conditions,
                        "scope_known": scope_known, "compilation_status": status,
                        "scope_unknown_reason": None if scope_known else "caller_parser_scope_incomplete" if complete else "caller_indexed_source_budget",
                        "binding_confirmed": row["status"] == "confirmed" and row["target_path"] in roots})
    return entries, callers, gaps


def _bound_cl_callers(database_path, business_map, context):
    """Reuse supplied CL syntax without claiming a resolved runtime object."""
    if (not database_path or not context or context.get("frontier")
            or context.get("unread_candidate_count") != 0):
        return []
    roots = list(dict.fromkeys(business_map.get("direct_paths", ())))
    if len(roots) != 1:
        return []
    observations = [row for row in context.get("observations", ()) if row.get("command") == "CALL"]
    if len(observations) > _MAX_CALLS:
        return []
    with closing(sqlite3.connect(Path(database_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        names = [row[0] for row in db.execute(
            "SELECT name FROM symbols WHERE relative_path=? AND symbol_type='Program' LIMIT 2", roots)]
        if len(names) != 1:
            return []
        definitions = list(db.execute(
            "SELECT relative_path FROM symbols WHERE name=? AND symbol_type='Program' LIMIT 2", names))
        if definitions != [(roots[0],)]:
            return []
        paths = list(dict.fromkeys(row.get("relative_path") for row in observations if row.get("relative_path")))
        hashes = dict(db.execute("SELECT relative_path,sha256 FROM source_files WHERE relative_path IN ("
            + ",".join("?" for _ in paths) + ")", paths)) if paths else {}

    def reference(ref):
        first, last, text = (ref.get(key) for key in ("start_line", "end_line", "statement"))
        if (type(first) is not int or type(last) is not int or first < 1 or last < first
                or not isinstance(text, str) or len(text.splitlines()) != last-first+1
                or hashes.get(ref.get("relative_path")) != ref.get("source_sha256")):
            return None
        return {key: ref[key] for key in ("relative_path", "source_sha256", "start_line", "end_line")} | {
            "expected_lines": dict(enumerate(text.splitlines(), first))}

    callers = []
    for row in observations:
        target = row.get("target") or {}
        if (row.get("command") != "CALL" or row.get("target_name") != names[0]
                or target.get("name") != names[0]
                or target.get("binding_status") not in {"library_list_candidate", "explicit_source_spelling"}
                or row.get("diagnostics")
                or row.get("binding_basis") != "indexed_complete_source_parse_and_supplied_span"):
            continue
        source_ref = reference(row)
        if source_ref is None:
            continue
        conditions, condition_refs = [], []
        for guard in row.get("guards", ()):
            for ref in (guard.get("source_ref"), guard.get("if_source_ref")):
                if ref is not None:
                    conditions.append(dict(guard))
                    condition_refs.append(reference(ref))
        scope_known = row.get("control_scope_supplied") is True and row.get("scope_known") is True
        callers.append({"relative_path": row["relative_path"], "line": row["start_line"],
            "source_sha256": row["source_sha256"], "target_name": names[0],
            "target_path": roots[0], "relation_type": "CL_CALL", "source_language": "CL",
            "occurrence_id": row["occurrence_id"], "source_ref": source_ref,
            "conditions": conditions, "condition_refs": condition_refs,
            "scope_known": scope_known,
            "scope_unknown_reason": None if scope_known else "cl_control_or_environment_source_not_supplied",
            "compilation_status": "active", "binding_confirmed": False,
            "static_target_name_match": True,
            "target_binding_basis": "unique_program_source_name_not_runtime_object",
            "runtime_object_verified": False, "command_context": row.get("command_context")})
    return callers


def investigate_activation(business_map, pages, *, database_path=None, max_actions=4, candidate_cache=None,
                           cl_context=None):
    cache = candidate_cache if isinstance(candidate_cache, dict) else {}
    key = (str(database_path), business_map.get("snapshot_id"), tuple(business_map.get("direct_paths", ())))
    if cache.get("activation_key") != key:
        cache["activation_candidates"] = _candidates(database_path, business_map)
        cache["activation_key"] = key
    entries, callers, frontier = cache["activation_candidates"]
    # The CL context was rebuilt against this request's actual pages. Do not
    # cache its visibility with the stable index candidates above.
    callers = [*callers, *_bound_cl_callers(database_path, business_map, cl_context)]

    def covered(ref):
        expected = ref["expected_lines"]
        if not expected:
            return []
        ids, remaining = [], set(range(ref["start_line"], ref["end_line"]+1))
        for page in pages:
            if (page.get("relative_path") != ref["relative_path"] or page.get("source_sha256") != ref["source_sha256"]
                    or page.get("span_truncated") or not page.get("evidence_id") or not page.get("source_text")):
                continue
            first, last = page.get("start_line"), page.get("end_line")
            if type(first) is not int or type(last) is not int:
                continue
            lines = dict(enumerate(page["source_text"].split("\n"), first))
            matched = {number for number in remaining if first <= number <= last and number in expected
                       and lines.get(number) == expected[number]}
            if matched:
                ids.append(page["evidence_id"])
                remaining -= matched
        return list(dict.fromkeys(ids)) if not remaining else []

    plans, seen = [], set()
    def read(ref, reason):
        identity = (ref["relative_path"], ref["start_line"], ref["end_line"])
        if identity in seen:
            return
        seen.add(identity)
        if len(plans) < max_actions:
            plans.append({"tool": "read", "arguments": {"relative_path": identity[0],
                "start_line": identity[1], "end_line": identity[2]}, "reason": reason})

    def supplied(rows, *, conditions=False):
        observations, satisfied = [], []
        for candidate in rows:
            identifiers = covered(candidate["source_ref"])
            complete = bool(identifiers)
            if not identifiers:
                read(candidate["source_ref"], "caller_invocation_not_supplied" if "target_name" in candidate else "entry_definition_not_supplied")
            condition_ids, visible_conditions = [], []
            if "target_name" in candidate:
                for guard, ref in zip(candidate["conditions"], candidate["condition_refs"]):
                    if ref is None:
                        complete = False
                        continue
                    refs = covered(ref)
                    condition_ids.extend(refs)
                    if refs:
                        visible_conditions.append({**guard, "evidence_ids": refs})
                    if not refs:
                        complete = False
                        read(ref, "caller_conditions_not_supplied")
                complete &= candidate["scope_known"]
                if not candidate["scope_known"]:
                    frontier = {"kind": "activation_conditions", "relative_path": candidate["relative_path"],
                        "reason": candidate["scope_unknown_reason"]}
                    if frontier not in local_frontier:
                        local_frontier.append(frontier)
            active = candidate["compilation_status"] == "active" and (
                candidate.get("binding_confirmed", True) or candidate.get("static_target_name_match") is True)
            satisfied.append(bool(identifiers) and active and (complete if conditions else True))
            if identifiers:
                observations.append({key: value for key, value in candidate.items()
                    if key not in {"source_ref", "condition_refs"}} | {"evidence_id": identifiers[0],
                    "evidence_ids": identifiers, "condition_evidence_ids": list(dict.fromkeys(condition_ids)),
                    "conditions": visible_conditions, "condition_candidate_count": len(candidate.get("conditions", ())),
                    "conditions_supplied": complete, "execution_verified": False})
        return observations, satisfied

    local_frontier = []
    entry_rows, entry_ok = supplied(entries)
    caller_rows, caller_ok = supplied(callers)
    _, condition_ok = supplied(callers, conditions=True)
    def obligation(kind, candidates, rows, fulfilled, missing):
        uncertain = any(row["compilation_status"] != "active" or
                        not (row.get("binding_confirmed", True) or row.get("static_target_name_match") is True)
                        for row in rows)
        status = "SATISFIED" if fulfilled and all(fulfilled) else "UNRESOLVED" if uncertain else "PARTIAL" if rows else "OPEN"
        return {"id": "activation_" + kind, "kind": kind, "status": status,
            "evidence_ids": list(dict.fromkeys(identifier for row in rows for identifier in
                [*row["evidence_ids"], *(row.get("condition_evidence_ids", ()) if kind == "activation_conditions" else ())])),
            "candidate_count": len(candidates), "reason": "source_syntax_supplied" if status == "SATISFIED" else
                "compile_or_target_binding_unresolved" if uncertain else missing}
    items = [obligation("entry_point", entries, entry_rows, entry_ok, "entry_definition_not_supplied"),
        obligation("caller_invocation", callers, caller_rows, caller_ok, "upstream_caller_not_supplied"),
        obligation("activation_conditions", callers, caller_rows, condition_ok, "caller_conditions_not_supplied"),
        {"id": "activation_scheduler_binding", "kind": "scheduler_binding", "status": "OPEN", "evidence_ids": [],
         "reason": "external_schedule_binding_not_verified",
         "scope": "schedule_configuration_and_runtime_launch_are_separate_from_static_CALL"}]
    gaps = [*frontier, *local_frontier, *({"kind": item["kind"], "status": item["status"], "reason": item["reason"]}
        for item in items if item["status"] != "SATISFIED")]
    if len(seen) > len(plans):
        gaps.append({"kind": "caller_invocation", "reason": "caller_read_frontier", "remaining": len(seen)-len(plans)})
    return {"required_items": items, "planned_actions": plans, "open_gaps": gaps,
        "state": "needs_evidence" if plans else "bounded_partial", "can_answer": bool(entry_rows or caller_rows),
        "activation_evidence": {"entries": entry_rows, "callers": caller_rows, "candidate_call_count": len(callers),
            "unread_call_count": len(callers)-len(caller_rows), "scheduler_binding_verified": False,
            "execution_verified": False, "scope": "supplied_static_source_not_runtime_trigger_verification"}}
