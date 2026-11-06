"""Describe indexed source availability without asserting a runtime binding."""

from copy import deepcopy


def dependency_source_availability(connection, relation_type, target_name, *,
                                   target_path=None, metadata=None, candidate_cache=None):
    metadata = metadata or {}
    result = {"target_source_status_scope": "indexed_sources"}
    if relation_type == "CALL_TARGET_FROM":
        return {**result, "target_source_status": "dynamic_target_unresolved"}
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
