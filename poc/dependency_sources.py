"""Describe indexed source availability without asserting a runtime binding."""

from copy import deepcopy


def dependency_source_availability(connection, relation_type, target_name, *,
                                   target_path=None, metadata=None, candidate_cache=None, relation_id=None):
    metadata = metadata or {}
    result = {"target_source_status_scope": "indexed_sources"}
    if relation_type == "CALL_TARGET_FROM":
        return {**result, "target_source_status": "dynamic_target_unresolved",
                **(dynamic_target_source_candidates(connection, relation_id, candidate_cache=candidate_cache)
                   if relation_id else {})}
    if target_path:
        return {**result, "target_source_status": "indexed"}
    symbol_type = {"CALLS": "Program", "INCLUDES_COPY": "Copybook"}.get(relation_type)
    candidates = []
    if symbol_type:
        key = (symbol_type, target_name)
        cache = candidate_cache if candidate_cache is not None else {}
        if key not in cache:
            cache[key] = [dict(row) for row in connection.execute(
                "SELECT s.relative_path,u.start_line,u.end_line,u.program_name "
                "FROM symbols s JOIN code_units u ON u.unit_id=s.definition_unit_id "
                "JOIN source_files f ON f.relative_path=s.relative_path "
                "WHERE s.symbol_type=? AND s.name=? ORDER BY s.relative_path,u.start_line LIMIT 9", key)]
        candidates = cache[key]
    if candidates:
        return {**result, "target_source_status": "candidate_sources_indexed",
                "target_source_candidates": deepcopy(candidates[:8]),
                "target_source_candidates_truncated": len(candidates) > 8}
    return {**result, "target_source_status":
            "unavailable" if metadata.get("resolution_reason") == "target_not_found" else "unresolved"}


_DYNAMIC_ASSIGNMENT_LIMIT = 32
_DYNAMIC_LITERAL_LIMIT = 8
_DYNAMIC_CANDIDATE_LIMIT = 8


def dynamic_target_source_candidates(connection, relation_id, *, candidate_cache=None):
    """Nominate indexed programs named by local literals, never bind the CALL.

    Initial values can persist across invocations, and an ACCEPT, branch or
    callee may overwrite the field. Every literal is only a source-reading
    lead. Indirect assignments, COPY field expansion and value-flow proofs are
    deliberately outside this bounded nomination. Assignments must end before
    the CALL's physical line; same-line statement ordering is not inferred.
    The returned names are not an exhaustive set of possible runtime values.
    """
    import re
    from statement_facts import CobolLexError, assignment_facts, lex_cobol

    cache_key = ("dynamic_source_candidates", relation_id)
    if candidate_cache is not None and cache_key in candidate_cache:
        return deepcopy(candidate_cache[cache_key])
    result = {"dynamic_target_candidates": [], "dynamic_target_candidates_truncated": False,
              "runtime_target_resolved": False, "dynamic_value_set_complete": False}
    call = connection.execute(
        "SELECT r.target_name,r.relative_path,u.program_name,e.start_line,e.end_line,f.sha256 "
        "FROM relations r JOIN code_units u ON u.unit_id=r.from_entity_id "
        "JOIN evidence_spans e ON e.evidence_id=r.evidence_id "
        "JOIN source_files f ON f.relative_path=r.relative_path "
        "WHERE r.relation_id=? AND r.relation_type='CALL_TARGET_FROM'", (relation_id,)).fetchone()
    if call is not None:
        result["caller_start_line"] = call["start_line"]
    # Qualified/subscripted fields require a separate layout-binding proof.
    if call is None or not re.fullmatch(r"[A-Z0-9_$#@-]+", call["target_name"] or ""):
        return result
    fields = connection.execute(
        "SELECT u.normalized_text,u.start_line,u.end_line,u.evidence_id "
        "FROM symbols s JOIN code_units u ON u.unit_id=s.definition_unit_id "
        "WHERE s.symbol_type='Field' AND s.name=? AND s.program_name IS ? "
        "AND s.relative_path=? ORDER BY u.start_line LIMIT 2",
        (call["target_name"], call["program_name"], call["relative_path"])).fetchall()
    if len(fields) != 1:
        return result
    origins = {}

    def nominate(expression, row, kind):
        try:
            tokens = lex_cobol(expression, max_tokens=16)
        except CobolLexError:
            return
        if len(tokens) != 1 or tokens[0].kind != "literal":
            return
        literal = tokens[0].value
        name = literal[1:-1].replace(literal[0] * 2, literal[0]).strip().upper()
        if not re.fullmatch(r"[A-Z0-9_$#@-]{1,128}", name):
            return
        if name not in origins and len(origins) >= _DYNAMIC_LITERAL_LIMIT:
            result["dynamic_target_candidates_truncated"] = True
            return
        provenance = {"relative_path": call["relative_path"], "source_sha256": call["sha256"],
            "start_line": row["start_line"], "end_line": row["end_line"], "kind": kind}
        if "evidence_id" in row.keys():
            provenance["evidence_id"] = row["evidence_id"]
        origins.setdefault(name, []).append(provenance)

    declaration = fields[0]
    try:
        tokens = lex_cobol(declaration["normalized_text"], max_tokens=256)
    except CobolLexError:
        tokens = ()
    for index, token in enumerate(tokens):
        if token.kind == "word" and token.value == "VALUE":
            tail = tokens[index + 1:]
            if tail and tail[0].value == "IS":
                tail = tail[1:]
            if tail and tail[0].kind == "literal":
                nominate(tail[0].value, declaration, "initial_value")
            break
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {"business_rules", "business_rule_fields", "business_rule_occurrences"} <= tables:
        assignment_key = ("dynamic_assignment_window", call["relative_path"],
                          call["program_name"], call["target_name"])
        cached_window = (candidate_cache or {}).get(assignment_key)
        assignments = None
        if cached_window is not None:
            first, last = sorted((cached_window["cutoff"], call["start_line"]))
            # The path/program/line index visits only the interval between
            # callsites. Repeated calls without an intervening target write
            # reuse the same bounded field-specific assignment window.
            changed = connection.execute(
                "SELECT 1 FROM business_rule_occurrences o CROSS JOIN business_rules b ON b.rule_id=o.rule_id "
                "WHERE o.relative_path=? AND o.program_name IS ? AND o.start_line>=? AND o.start_line<? "
                "AND b.rule_kind='MOVE' AND EXISTS (SELECT 1 FROM business_rule_fields f "
                "WHERE f.rule_id=b.rule_id AND f.field_role='write' AND f.field_name=?) LIMIT 1",
                (call["relative_path"], call["program_name"], first, last, call["target_name"])).fetchone()
            if changed is None:
                assignments = cached_window["rows"]
        if assignments is None:
            assignments = [dict(row) for row in connection.execute(
                "SELECT b.normalized_text,o.start_line,o.end_line,o.source_sha256 FROM business_rules b "
                "JOIN business_rule_occurrences o ON o.rule_id=b.rule_id "
                "WHERE b.relative_path=? AND b.program_name IS ? AND b.rule_kind='MOVE' "
                "AND o.start_line < ? AND o.source_sha256=? AND EXISTS (SELECT 1 FROM business_rule_fields f "
                "WHERE f.rule_id=b.rule_id AND f.field_role='write' AND f.field_name=?) "
                "ORDER BY o.start_line DESC,o.start_column DESC LIMIT ?",
                (call["relative_path"], call["program_name"], call["start_line"], call["sha256"],
                 call["target_name"], _DYNAMIC_ASSIGNMENT_LIMIT + 1))]
            if candidate_cache is not None:
                candidate_cache[assignment_key] = {"cutoff": call["start_line"], "rows": assignments}
        if len(assignments) > _DYNAMIC_ASSIGNMENT_LIMIT:
            result["dynamic_target_candidates_truncated"] = True
        for row in assignments[:_DYNAMIC_ASSIGNMENT_LIMIT]:
            if row["end_line"] >= call["start_line"] or row["source_sha256"] != call["sha256"]:
                continue
            facts = assignment_facts(row["normalized_text"])
            if facts is None or facts.effects_unknown:
                continue
            for target, expression in facts.expressions:
                if target == call["target_name"]:
                    nominate(expression, row, "literal_assignment")
    else:
        # Structural snapshots retain original MOVE statement units instead.
        assignments = connection.execute(
            "SELECT normalized_text,start_line,end_line,evidence_id FROM code_units "
            "WHERE relative_path=? AND program_name IS ? AND unit_type='Statement' "
            "LIMIT ?", (call["relative_path"], call["program_name"],
            _DYNAMIC_ASSIGNMENT_LIMIT + 1)).fetchall()
        if len(assignments) > _DYNAMIC_ASSIGNMENT_LIMIT:
            result["dynamic_target_candidates_truncated"] = True
        for row in assignments[:_DYNAMIC_ASSIGNMENT_LIMIT]:
            if row["end_line"] >= call["start_line"]:
                continue
            facts = assignment_facts(row["normalized_text"])
            if facts and not facts.effects_unknown:
                for target, expression in facts.expressions:
                    if target == call["target_name"]:
                        nominate(expression, row, "literal_assignment")
    for name, provenance in origins.items():
        definitions = connection.execute(
            "SELECT s.relative_path,u.start_line,u.end_line,u.program_name,f.sha256 AS source_sha256 "
            "FROM symbols s JOIN code_units u ON u.unit_id=s.definition_unit_id "
            "JOIN source_files f ON f.relative_path=s.relative_path "
            "WHERE s.symbol_type='Program' AND s.name=? ORDER BY s.relative_path,u.start_line LIMIT ?",
            (name, _DYNAMIC_CANDIDATE_LIMIT + 1)).fetchall()
        for definition in definitions:
            if len(result["dynamic_target_candidates"]) >= _DYNAMIC_CANDIDATE_LIMIT:
                result["dynamic_target_candidates_truncated"] = True
                break
            result["dynamic_target_candidates"].append({**dict(definition),
                "nomination_basis": "local_literal_reading_candidate", "runtime_binding_verified": False,
                "target_field": call["target_name"], "source_provenance": provenance[:4],
                "source_provenance_truncated": len(provenance) > 4,
                "source_provenance_omitted_count": max(0, len(provenance) - 4)})
    if candidate_cache is not None:
        candidate_cache[cache_key] = deepcopy(result)
    return result
