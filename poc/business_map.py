"""Question-specific structure and rule leads from an existing local snapshot.

The map is a navigation aid. Source excerpts remain the authority for business
answers; a shared field name or static caller does not prove runtime influence.
"""

from __future__ import annotations

from collections import defaultdict, deque
import json
from pathlib import Path
import re

from repository_discovery import _connect, _fast_snapshot, _query_terms, discover_repository
from source_reading import _cancel


_IMPACT_LANGUAGE = re.compile(
    r"影响|影響|改动|修改|變更|变更|用到.{0,24}程序|哪些程序.{0,24}用到|"
    r"\b(?:impact|affected|affect|change|where[ -]?used|used[ -]?by)\b",
    re.IGNORECASE,
)
_CODE_TERM = re.compile(r"[A-Za-z][A-Za-z0-9_$#@-]{2,}")


def _exact_identifier_paths(connection, question):
    """Prefer a user's explicit source identifier over its common word pieces."""
    identifiers = list(dict.fromkeys(token for token in _CODE_TERM.findall(question)
                                     if any(char.isdigit() for char in token) or
                                     any(char in "-$#@" for char in token)))[:8]
    matched = set()
    for identifier in identifiers:
        phrase = '"' + identifier.replace('"', '""') + '"'
        for row in connection.execute(
            "SELECT DISTINCT p.relative_path FROM repo_fts JOIN repo_pages p "
            "ON p.page_id=repo_fts.rowid WHERE repo_fts MATCH ?", (phrase,)
        ):
            matched.add(row[0])
    return matched


def _paths_from_history(connection, prior_paths):
    result = []
    for value in (prior_paths or [])[:12]:
        if not isinstance(value, str):
            continue
        candidate = value.split("::", 1)[0]
        for row in connection.execute(
            "SELECT relative_path FROM source_files WHERE relative_path=? COLLATE NOCASE "
            "UNION SELECT relative_path FROM code_units WHERE unit_type='Program' "
            "AND name=? COLLATE NOCASE LIMIT 2", (candidate, candidate)
        ):
            result.append(row[0])
    return list(dict.fromkeys(result))


def _call_graph(connection, check_cancel):
    incoming = defaultdict(list)
    edges = []
    aliases = defaultdict(set)
    for row in connection.execute("SELECT relative_path FROM source_files"):
        relative = row[0]
        for alias in (relative, Path(relative).name, Path(relative).stem):
            aliases[alias.casefold()].add(relative)
    query = (
        "SELECT r.relation_id,r.relative_path AS caller_path,r.relation_type,r.target_name,"
        "r.status AS resolution,r.evidence_id,e.start_line AS caller_line,"
        "s.relative_path AS target_path,u.program_name AS caller_program "
        "FROM relations r JOIN evidence_spans e ON e.evidence_id=r.evidence_id "
        "LEFT JOIN symbols s ON s.symbol_id=r.target_entity_id "
        "LEFT JOIN code_units u ON u.unit_id=r.from_entity_id "
        "WHERE r.relation_type IN ('CALLS','CALL_TARGET_FROM','INCLUDES_COPY') "
        "ORDER BY r.relative_path,e.start_line"
    )
    for row in connection.execute(query):
        _cancel(check_cancel)
        edge = dict(row)
        if edge["resolution"] != "confirmed" or edge["relation_type"] == "CALL_TARGET_FROM":
            edge["target_path"] = None
        if not edge["target_path"] and edge["relation_type"] == "INCLUDES_COPY":
            candidates = aliases.get((edge["target_name"] or "").casefold(), set())
            if len(candidates) == 1:
                edge["target_path"] = next(iter(candidates))
                edge["resolution"] = "unbound_copy_candidate"
        edges.append(edge)
        if edge["target_path"]:
            incoming[edge["target_path"]].append(edge)
    return incoming, edges


def _impact_paths(incoming, seeds, check_cancel):
    distance = {path: 0 for path in seeds}
    queue = deque(sorted(seeds))
    while queue:
        _cancel(check_cancel)
        target = queue.popleft()
        for edge in incoming[target]:
            caller = edge["caller_path"]
            if caller not in distance:
                distance[caller] = distance[target] + 1
                queue.append(caller)
    return distance


def _explain_paths(incoming, edges, seeds, repository_size, check_cancel):
    """Find callers and their dependencies without expanding a shared hub."""
    distance = {path: 0 for path in seeds}
    queue = deque(sorted(seeds))
    hub_limit = max(8, (repository_size + 3) // 4)
    while queue:
        _cancel(check_cancel)
        target = queue.popleft()
        parents = incoming[target]
        if len({edge["caller_path"] for edge in parents}) >= hub_limit:
            continue
        for edge in parents:
            caller = edge["caller_path"]
            if caller not in distance:
                distance[caller] = distance[target] + 1
                queue.append(caller)
    forward = defaultdict(list)
    for edge in edges:
        if edge["target_path"]:
            forward[edge["caller_path"]].append(edge["target_path"])
    queue = deque(sorted(distance, key=lambda path: (distance[path], path)))
    while queue:
        _cancel(check_cancel)
        caller = queue.popleft()
        for target in forward[caller]:
            if target not in distance:
                distance[target] = distance[caller] + 1
                queue.append(target)
    return distance


def _rule_leads(connection, paths, previews, question, check_cancel):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='business_rules'").fetchone():
        return []
    terms = [term.upper() for term in _CODE_TERM.findall(question)
             if term.casefold() not in {"business", "program", "programs", "source", "analysis", "impact", "change"}]
    terms = list(dict.fromkeys(terms))[:8]
    by_path = defaultdict(dict)
    for preview in previews[:32]:
        relative = preview["relative_path"]
        if relative not in paths:
            continue
        for row in connection.execute(
            "SELECT * FROM business_rules WHERE relative_path=? AND first_line<=? "
            "AND last_line>=? ORDER BY first_line LIMIT 40",
            (relative, preview["end_line"] + 12, max(1, preview["start_line"] - 12)),
        ):
            by_path[relative][row["rule_id"]] = (dict(row), "source_match")
    for relative in sorted(paths)[:80]:
        _cancel(check_cancel)
        # A source name or a header comment can identify the right program
        # without matching any field in its calculation. Keep a few arithmetic
        # leads from that program even when the matching page is far away.
        for row in connection.execute(
            "SELECT * FROM business_rules WHERE relative_path=? "
            "AND rule_kind IN ('COMPUTE','ADD','SUBTRACT','MULTIPLY','DIVIDE') "
            "ORDER BY CASE WHEN rule_kind='COMPUTE' THEN 0 ELSE 1 END,first_line LIMIT 3",
            (relative,),
        ):
            by_path[relative].setdefault(row["rule_id"], (dict(row), "program_calculation"))
        for term in terms:
            for row in connection.execute(
                "SELECT * FROM business_rules WHERE relative_path=? AND normalized_text LIKE ? "
                "ORDER BY first_line LIMIT 60", (relative, "%" + term + "%"),
            ):
                by_path[relative].setdefault(row["rule_id"], (dict(row), "term_match"))
    # Follow nearby field operations within the same source. These are search
    # leads; no cross-program value flow is inferred from identical names.
    for relative, found in list(by_path.items()):
        names = list(dict.fromkeys(name for row, _ in found.values()
                                   for column in ("reads_json", "writes_json", "condition_json")
                                   for name in json.loads(row[column])))[:24]
        if not names:
            continue
        slots = ",".join("?" for _ in names)
        query = (
            "SELECT b.* FROM business_rules b WHERE b.relative_path=? "
            "AND EXISTS (SELECT 1 FROM business_rule_fields f WHERE f.rule_id=b.rule_id "
            f"AND f.field_name IN ({slots})) ORDER BY b.first_line LIMIT 100"
        )
        for row in connection.execute(query, (relative, *names)):
            found.setdefault(row["rule_id"], (dict(row), "shared_field"))
    ranked = []
    for relative, found in by_path.items():
        for row, reason in found.values():
            code = row["normalized_text"]
            hits = sum(1 for term in terms if term in code)
            score = (20 if reason in {"source_match", "program_calculation"}
                     else 10 if reason == "term_match" else 0)
            score += hits * 8
            score += 5 if row["rule_kind"] in {"COMPUTE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE", "EXEC_SQL"} else 0
            score += 3 if row["rule_kind"] in {"IF", "WHEN", "EVALUATE"} else 0
            ranked.append((score, row["first_line"], {
                "relative_path": relative, "program_name": row["program_name"],
                "paragraph_name": row["paragraph_name"], "rule_kind": row["rule_kind"],
                "start_line": row["first_line"], "end_line": row["last_line"],
                "occurrence_count": row["occurrence_count"],
                "statement": code[:1800], "reads": json.loads(row["reads_json"]),
                "writes": json.loads(row["writes_json"]),
                "conditions": json.loads(row["condition_json"]),
                "selection_reason": reason,
            }))
    ranked.sort(key=lambda item: (-item[0], item[2]["relative_path"], item[1]))
    selected, per_path = [], defaultdict(int)
    for _, _, rule in ranked:
        if per_path[rule["relative_path"]] >= 4:
            continue
        selected.append(rule)
        per_path[rule["relative_path"]] += 1
        if len(selected) >= 48:
            break
    return selected


def _related_program_rules(connection, selected, direct, distance, existing, check_cancel):
    """Surface distant calculations and decisions when entry text is generic."""
    if not selected or not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='business_rules'"
    ).fetchone():
        return existing
    seen = {(item["relative_path"], item["start_line"]) for item in existing}
    candidates = sorted(selected - direct, key=lambda path: (-distance.get(path, 0), path))
    for relative in candidates[:24]:
        _cancel(check_cancel)
        for row in connection.execute(
            "SELECT * FROM business_rules WHERE relative_path=? "
            "AND rule_kind IN ('COMPUTE','ADD','SUBTRACT','MULTIPLY','DIVIDE','IF','WHEN','EVALUATE','EXEC_SQL') "
            "ORDER BY CASE WHEN rule_kind IN ('COMPUTE','ADD','SUBTRACT','MULTIPLY','DIVIDE') "
            "THEN 0 WHEN rule_kind='EXEC_SQL' THEN 1 ELSE 2 END,first_line LIMIT 3", (relative,)
        ):
            if (relative, row["first_line"]) in seen:
                continue
            existing.append({"relative_path": relative, "program_name": row["program_name"],
                             "paragraph_name": row["paragraph_name"], "rule_kind": row["rule_kind"],
                             "start_line": row["first_line"], "end_line": row["last_line"],
                             "occurrence_count": row["occurrence_count"],
                             "statement": row["normalized_text"][:1800],
                             "reads": json.loads(row["reads_json"]),
                             "writes": json.loads(row["writes_json"]),
                             "conditions": json.loads(row["condition_json"]),
                             "selection_reason": "related_program"})
            seen.add((relative, row["first_line"]))
            if len(existing) >= 48:
                return existing
    return existing


def build_business_map(database_path, source_root, question, *, search_terms=None, prior_paths=None,
                       check_cancel=None):
    """Map direct matches, transitive static callers and nearby rule operations.

    It reads the saved index only. In particular, it does not rescan source files
    or ask the model to summarise individual pages.
    """
    discovery = discover_repository(database_path, question, search_terms=search_terms,
                                    check_cancel=check_cancel)
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN")
        _, overview = _fast_snapshot(connection, source_root)
        direct = {item["relative_path"] for item in discovery["selection_reasons"]
                  if "text_match" in item["reasons"]}
        exact = _exact_identifier_paths(connection, question + " " + " ".join(search_terms or []))
        if exact:
            direct = exact
        query_direct = set(direct)
        historical = _paths_from_history(connection, prior_paths)
        if not direct:
            direct.update(historical)
        intent = "impact" if _IMPACT_LANGUAGE.search(question) else "explain"
        incoming, all_edges = _call_graph(connection, check_cancel) if direct else ({}, [])
        if intent == "impact" and direct:
            distance = _impact_paths(incoming, direct, check_cancel)
            selected = set(distance)
        elif direct:
            distance = _explain_paths(incoming, all_edges, direct, overview["indexed_files"], check_cancel)
            selected = set(distance)
        else:
            selected, distance = set(), {}
        program_names = defaultdict(list)
        for row in connection.execute("SELECT relative_path,name FROM code_units "
                                      "WHERE unit_type='Program' ORDER BY relative_path,start_line"):
            if row["relative_path"] in selected:
                program_names[row["relative_path"]].append(row["name"])
        ordered = sorted(selected, key=lambda path: (distance.get(path, 0), path))
        programs = [{"relative_path": path, "program_names": program_names[path] or [Path(path).stem.upper()],
                     "distance": distance.get(path, 0), "direct_source_match": path in query_direct}
                    for path in ordered]
        relevant_edges = [edge for edge in all_edges if edge["caller_path"] in selected and
                          (edge["target_path"] in selected or not edge["target_path"])]
        relevant_edges.sort(key=lambda edge: (distance.get(edge["caller_path"], 0), edge["caller_path"], edge["caller_line"]))
        rule_query = question + " " + " ".join(search_terms or [])
        leads = _rule_leads(connection, query_direct, discovery["matched_pages"], rule_query, check_cancel)
        if intent == "explain":
            leads = _related_program_rules(connection, selected, direct, distance, leads, check_cancel)
        spotlights = []
        relation_candidates = [edge for edge in relevant_edges if edge["target_path"] in selected]
        relation_candidates.sort(key=lambda edge: (edge["caller_path"] not in direct,
                                                     -edge["caller_line"], edge["caller_path"]))
        seen_callers = set()
        for edge in relation_candidates:
            if edge["caller_path"] in seen_callers:
                continue
            if intent != "impact" and edge["caller_line"] < 2000 and edge["caller_path"] in direct:
                continue
            spotlights.append({"relative_path": edge["caller_path"],
                               "start_line": max(1, edge["caller_line"] - 8),
                               "end_line": edge["caller_line"] + 12})
            seen_callers.add(edge["caller_path"])
            if len(spotlights) >= 2:
                break
        for unique_paths_only in (True, False):
            for rule in leads:
                if unique_paths_only and any(item["relative_path"] == rule["relative_path"] for item in spotlights):
                    continue
                if any(item["relative_path"] == rule["relative_path"] and
                       abs(item["start_line"] - rule["start_line"]) <= 10 for item in spotlights):
                    continue
                spotlights.append({"relative_path": rule["relative_path"],
                                   "start_line": max(1, rule["start_line"] - 6),
                                   "end_line": rule["end_line"] + 8})
                if len(spotlights) >= 6:
                    break
            if len(spotlights) >= 6:
                break
        return {"snapshot_id": overview["snapshot_id"], "intent": intent,
                "direct_paths": sorted(query_direct), "selected_paths": ordered,
                "programs": programs, "relations": relevant_edges,
                "rule_leads": leads, "spotlights": spotlights,
                "matched_files": discovery["matched_file_count"],
                "matched_pages": discovery["matched_page_count"],
                "search_terms": _query_terms(question, search_terms)[0]}
    finally:
        connection.close()
