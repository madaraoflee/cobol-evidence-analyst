"""Question-specific structure and rule leads from an existing local snapshot.

The map is a navigation aid. Source excerpts remain the authority for business
answers; a shared field name or static caller does not prove runtime influence.
"""

from __future__ import annotations

from collections import defaultdict, deque
import json
from pathlib import Path
import re

from repository_discovery import _connect, _fast_snapshot, _navigation_graph, _query_terms, discover_repository
from repository_identity import resolve_source_identity
from source_reading import _cancel
from file_impact_evidence import is_file_impact_question


_IMPACT_LANGUAGE = re.compile(
    r"影响|影響|改动|修改|變更|变更|用到.{0,24}程序|哪些程序.{0,24}用到|"
    r"\b(?:impact|affected|affects?|change|where[ -]?used|used[ -]?by)\b",
    re.IGNORECASE,
)
_CODE_TERM = re.compile(r"[A-Za-z][A-Za-z0-9_$#@-]{2,}")
_OUTGOING_PATH_LIMIT = 32
_OUTGOING_DEPTH_LIMIT = 4
_OUTGOING_FRONTIER_LIMIT = 32


def _exact_identifier_paths(connection, question):
    """Compatibility wrapper: only definitions establish source identity."""
    return set(resolve_source_identity(connection, question)["direct_paths"])


def _lexical_identifier_paths(connection, question):
    """Prefer complete field phrases over common word pieces, without identity."""
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
            "UNION SELECT relative_path FROM symbols WHERE symbol_type='Program' "
            "AND name=? LIMIT 2", (candidate, candidate.upper())
        ):
            result.append(row[0])
    return list(dict.fromkeys(result))


def _call_graph(connection, check_cancel):
    _cancel(check_cancel)
    incoming = defaultdict(list)
    edges = []
    aliases = defaultdict(set)
    for row in connection.execute("SELECT relative_path FROM source_files"):
        relative = row[0]
        for alias in (relative, Path(relative).name, Path(relative).stem):
            aliases[alias.casefold()].add(relative)
    rows = (row for row in _navigation_graph(connection, check_cancel, details=True)
            if row["caller_line"] is not None)
    for row in sorted(rows, key=lambda row: (row["relative_path"], row["caller_line"],
                                            row["relation_type"], row["relation_order"])):
        _cancel(check_cancel)
        edge = {key: row[key] for key in ("relation_id", "relation_type", "target_name", "evidence_id",
                                         "caller_line", "target_path", "caller_program")}
        edge.update(caller_path=row["relative_path"], resolution=row["status"])
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


def _outgoing_paths(edges, seeds, check_cancel):
    """Nominate bounded dependencies of the requested roots for source reading.

    Incoming callers remain a separate impact navigation graph. Only confirmed
    CALL/COPY links from these roots expand here; these are static candidates,
    not proof that a callee executes or writes a particular business value.
    """
    forward = defaultdict(set)
    for edge in edges:
        if (edge["relation_type"] in {"CALLS", "INCLUDES_COPY"}
                and edge["resolution"] == "confirmed" and edge.get("target_path")):
            forward[edge["caller_path"]].add(edge["target_path"])
    roots = sorted(seeds)
    distance = {path: 0 for path in roots[:_OUTGOING_PATH_LIMIT]}
    queue = deque(distance)
    frontier = []
    omitted_frontier_count = 0
    if len(roots) > _OUTGOING_PATH_LIMIT:
        frontier.append({"reason": "outgoing_dependency_root_budget",
                         "omitted_count": len(roots) - _OUTGOING_PATH_LIMIT})
    omitted = set()
    while queue:
        _cancel(check_cancel)
        caller = queue.popleft()
        for target in sorted(forward[caller]):
            if target in distance or target in omitted:
                continue
            reason = ("outgoing_dependency_depth_budget" if distance[caller] >= _OUTGOING_DEPTH_LIMIT
                      else "outgoing_dependency_path_budget" if len(distance) >= _OUTGOING_PATH_LIMIT
                      else None)
            if reason:
                omitted.add(target)
                if len(frontier) < _OUTGOING_FRONTIER_LIMIT - 1:
                    frontier.append({"reason": reason, "relative_path": target,
                                     "caller_path": caller})
                else:
                    omitted_frontier_count += 1
                continue
            distance[target] = distance[caller] + 1
            queue.append(target)
    if omitted_frontier_count:
        frontier.append({"reason": "outgoing_dependency_frontier_budget",
                         "omitted_count": omitted_frontier_count})
    return distance, frontier


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


def _rule_leads(connection, paths, previews, question, check_cancel, *, coverage=None, diversify=False):
    coverage = coverage if coverage is not None else {}
    ordered_paths = sorted(paths)
    coverage.update({"scope": "navigation_candidates", "preview_budget": 32,
        "preview_candidates": len(previews), "omitted_previews": max(0, len(previews) - 32),
        "path_budget": 80, "path_candidates": len(paths), "omitted_paths": max(0, len(paths) - 80),
        "per_path_rule_budget": 4, "global_rule_budget": 48, "candidate_rules": 0,
        "selected_rules": 0, "omitted_rules_per_path": {}, "omitted_rules_global": 0,
        "query_frontier": [], "candidate_selection_complete": False})
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='business_rules'").fetchone():
        return []
    terms = [term.upper() for term in _CODE_TERM.findall(question)
             if term.casefold() not in {"business", "program", "programs", "source", "analysis", "impact", "change"}]
    unique_terms = list(dict.fromkeys(terms))
    terms = unique_terms[:8]
    coverage["omitted_rule_query_terms"] = max(0, len(unique_terms) - 8)
    by_path = defaultdict(dict)
    for preview in previews[:32]:
        relative = preview["relative_path"]
        if relative not in paths:
            continue
        rows = connection.execute(
            "SELECT * FROM business_rules WHERE relative_path=? AND first_line<=? "
            "AND last_line>=? ORDER BY first_line,rule_id LIMIT 41",
            (relative, preview["end_line"] + 12, max(1, preview["start_line"] - 12)),
        ).fetchall()
        if len(rows) > 40:
            coverage["query_frontier"].append({"relative_path": relative, "reason": "preview_rule_budget", "minimum_omitted_rules": len(rows) - 40})
        for row in rows[:40]:
            by_path[relative][row["rule_id"]] = (dict(row), "source_match")
    for relative in ordered_paths[:80]:
        _cancel(check_cancel)
        if not diversify:
            # Preserve generic business-description navigation when there is
            # no defined program identity; defined identities use result groups.
            rows = connection.execute(
                "SELECT * FROM business_rules WHERE relative_path=? "
                "AND rule_kind IN ('COMPUTE','ADD','SUBTRACT','MULTIPLY','DIVIDE') "
                "ORDER BY CASE WHEN rule_kind='COMPUTE' THEN 0 ELSE 1 END,first_line,rule_id LIMIT 4",
                (relative,),
            ).fetchall()
            if len(rows) > 3:
                coverage["query_frontier"].append({"relative_path": relative, "reason": "program_calculation_budget", "minimum_omitted_rules": len(rows) - 3})
            for row in rows[:3]:
                by_path[relative].setdefault(row["rule_id"], (dict(row), "program_calculation"))
        for term in terms:
            rows = connection.execute(
                "SELECT * FROM business_rules WHERE relative_path=? AND normalized_text LIKE ? "
                "ORDER BY first_line,rule_id LIMIT 61", (relative, "%" + term + "%"),
            ).fetchall()
            if len(rows) > 60:
                coverage["query_frontier"].append({"relative_path": relative, "reason": "term_rule_budget", "term": term, "minimum_omitted_rules": len(rows) - 60})
            for row in rows[:60]:
                by_path[relative].setdefault(row["rule_id"], (dict(row), "term_match"))
        if diversify:
            # A program identity does not need to occur in the calculation's
            # text. Represent distinct paragraphs and result fields within the
            # existing candidate and output budgets, rather than every repeat.
            rows = connection.execute(
                "WITH candidates AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY paragraph_name,writes_json "
                "ORDER BY first_line,rule_id) AS group_rank FROM business_rules WHERE relative_path=? "
                "AND rule_kind IN ('COMPUTE','ADD','SUBTRACT','MULTIPLY','DIVIDE','MOVE')) "
                "SELECT * FROM candidates WHERE group_rank=1 ORDER BY first_line,rule_id LIMIT 61", (relative,)
            ).fetchall()
            if len(rows) > 60:
                coverage["query_frontier"].append({"relative_path": relative, "reason": "identity_rule_group_budget", "minimum_omitted_groups": len(rows) - 60})
            for row in rows[:60]:
                by_path[relative].setdefault(row["rule_id"], (dict(row), "source_identity"))
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
        rows = connection.execute(query.replace("LIMIT 100", "LIMIT 101"), (relative, *names)).fetchall()
        if len(rows) > 100:
            coverage["query_frontier"].append({"relative_path": relative, "reason": "shared_field_rule_budget", "minimum_omitted_rules": len(rows) - 100})
        for row in rows[:100]:
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
    coverage["candidate_rules"] = len(ranked)
    per_path = defaultdict(list)
    for item in ranked:
        per_path[item[2]["relative_path"]].append(item)
    eligible = []
    for relative, candidates in per_path.items():
        chosen, groups = [], set()
        if diversify:
            for item in candidates:
                rule = item[2]
                group = (rule["paragraph_name"], tuple(rule["writes"] or rule["conditions"] or rule["reads"]), rule["rule_kind"] if not rule["writes"] else "write")
                if group not in groups:
                    chosen.append(item)
                    groups.add(group)
                if len(chosen) >= 4:
                    break
        for item in candidates:
            if len(chosen) >= 4:
                break
            if item not in chosen:
                chosen.append(item)
        eligible.extend(chosen)
        if len(candidates) > len(chosen):
            coverage["omitted_rules_per_path"][relative] = len(candidates) - len(chosen)
    eligible.sort(key=lambda item: (-item[0], item[2]["relative_path"], item[1]))
    selected = [item[2] for item in eligible[:48]]
    coverage["selected_rules"] = len(selected)
    coverage["omitted_rules_global"] = max(0, len(eligible) - 48)
    coverage["candidate_selection_complete"] = not (coverage["omitted_previews"] or coverage["omitted_paths"] or
        coverage["omitted_rules_per_path"] or coverage["omitted_rules_global"] or coverage["query_frontier"] or
        coverage["omitted_rule_query_terms"])
    return selected


def _related_program_rules(connection, selected, direct, distance, existing, check_cancel, *, file_impact=False):
    """Surface distant calculations and decisions when entry text is generic."""
    if not selected or not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='business_rules'"
    ).fetchone():
        return existing
    seen = {(item["relative_path"], item["start_line"]) for item in existing}
    candidates = sorted(selected - direct, key=lambda path: (-distance.get(path, 0), path))
    kinds = "'COMPUTE','ADD','SUBTRACT','MULTIPLY','DIVIDE','IF','WHEN','EVALUATE','EXEC_SQL'"
    if file_impact:
        kinds += ",'MOVE','READ','START','WRITE','REWRITE','DELETE'"
    output_rank = "WHEN rule_kind IN ('WRITE','REWRITE','DELETE') THEN -1 " if file_impact else ""
    for relative in candidates[:24]:
        _cancel(check_cancel)
        for row in connection.execute(
            "SELECT * FROM business_rules WHERE relative_path=? "
            f"AND rule_kind IN ({kinds}) "
            "ORDER BY CASE " + output_rank + "WHEN rule_kind IN ('COMPUTE','ADD','SUBTRACT','MULTIPLY','DIVIDE') "
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
                                    check_cancel=check_cancel, fallback_to_repository=False)
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN")
        _, overview = _fast_snapshot(connection, source_root)
        identity = discovery["source_identity"]
        direct = {item["relative_path"] for item in discovery["selection_reasons"]
                  if "text_match" in item["reasons"]}
        if identity["status"] != "none":
            direct = set(identity["direct_paths"])
        else:
            exact_lexical = _lexical_identifier_paths(connection, question + " " + " ".join(search_terms or []))
            if exact_lexical:
                direct = exact_lexical
        query_direct = set(direct)
        historical = _paths_from_history(connection, prior_paths)
        if not direct and identity["status"] == "none":
            direct.update(historical)
        intent = "impact" if _IMPACT_LANGUAGE.search(question) else "explain"
        incoming, all_edges = _call_graph(connection, check_cancel) if direct else ({}, [])
        outgoing, outgoing_frontier = {}, []
        if intent == "impact" and direct:
            distance = _impact_paths(incoming, direct, check_cancel)
            if is_file_impact_question(question):
                outgoing, outgoing_frontier = _outgoing_paths(all_edges, direct, check_cancel)
            for path, depth in outgoing.items():
                distance.setdefault(path, depth)
            selected = set(distance)
        elif direct:
            distance = _explain_paths(incoming, all_edges, direct, overview["indexed_files"], check_cancel)
            selected = set(distance)
        else:
            selected, distance = set(), {}
        program_names = defaultdict(list)
        selected_paths = sorted(selected)
        # Keep definition lookup proportional to the nominated sources. The
        # path index already exists in older snapshots; no catalog rebuild or
        # new repository-sized index is needed for an interactive question.
        for start in range(0, len(selected_paths), 400):
            _cancel(check_cancel)
            batch = selected_paths[start:start + 400]
            slots = ",".join("?" for _ in batch)
            for row in connection.execute("SELECT relative_path,name FROM code_units "
                    f"WHERE relative_path IN ({slots}) AND unit_type='Program' "
                    "ORDER BY relative_path,start_line", batch):
                program_names[row["relative_path"]].append(row["name"])
        ordered = sorted(selected, key=lambda path: (distance.get(path, 0), path))
        programs = [{"relative_path": path, "program_names": program_names[path] or [Path(path).stem.upper()],
                     "distance": distance.get(path, 0), "direct_source_match": path in query_direct}
                    for path in ordered]
        relevant_edges = [edge for edge in all_edges if edge["caller_path"] in selected and
                          (edge["target_path"] in selected or not edge["target_path"])]
        relevant_edges.sort(key=lambda edge: (distance.get(edge["caller_path"], 0), edge["caller_path"], edge["caller_line"]))
        rule_query = " ".join(search_terms or []) + " " + question
        rule_coverage = {}
        leads = _rule_leads(connection, query_direct, discovery["matched_pages"], rule_query, check_cancel,
                            coverage=rule_coverage, diversify=identity["status"] == "resolved")
        if intent == "explain":
            before_related = len(leads)
            leads = _related_program_rules(connection, selected, direct, distance, leads, check_cancel)
            rule_coverage["related_rules_added"] = len(leads) - before_related
        elif outgoing:
            before_related = len(leads)
            leads = _related_program_rules(connection, set(outgoing), direct, outgoing, leads,
                                           check_cancel, file_impact=True)
            rule_coverage["related_rules_added"] = len(leads) - before_related
        anchors, anchor_groups = [], set()
        for rule in leads:
            if rule["rule_kind"] not in {"COMPUTE", "MOVE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE"}:
                continue
            group = (rule["relative_path"], rule["paragraph_name"], tuple(rule["writes"]))
            if group in anchor_groups:
                continue
            anchor_groups.add(group)
            anchors.append({**rule, "line": rule["start_line"],
                            "fields": list(dict.fromkeys([*rule["writes"], *rule["reads"]]))})
        rule_coverage["semantic_anchor_candidates"] = len(anchors)
        rule_coverage["semantic_anchor_budget"] = 4
        rule_coverage["omitted_semantic_anchors"] = max(0, len(anchors) - 4)
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
                "outgoing_dependency_paths": sorted(set(outgoing) - direct,
                    key=lambda path: (outgoing[path], path)),
                "outgoing_dependency_frontier": outgoing_frontier,
                "programs": programs, "relations": relevant_edges,
                "rule_leads": leads, "spotlights": spotlights,
                "source_identity": identity, "boundaries": discovery["boundaries"],
                "rule_lead_coverage": rule_coverage, "semantic_anchors": anchors[:4],
                "matched_files": discovery["matched_file_count"],
                "matched_pages": discovery["matched_page_count"],
                "search_terms": _query_terms(question, search_terms)[0]}
    finally:
        connection.close()
