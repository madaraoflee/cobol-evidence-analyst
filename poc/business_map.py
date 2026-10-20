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
from repository_identity import identity_blocks_analysis, resolve_source_identity
from conversation_focus import build_session_subject
from question_planner import program_relation_direction
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


def question_boundaries(boundaries, selected_paths, *, source_scopes=(), source_versions=None):
    """Keep unrelated catalog diagnostics separate from the requested source.

    Only located files and version-matched physical callsites establish a
    narrower scope. Unlocated effects, COPY/layout issues and flow diagnostics
    stay conservative. Excluding a different procedure is not an assertion
    about whether that procedure can execute or affect a later result.
    """
    selected, versions = set(selected_paths), source_versions or {}
    relevant, repository = [], []
    for boundary in boundaries:
        path = boundary.get("relative_path", boundary.get("caller_path"))
        basis = None
        if selected and path and path not in selected:
            basis = "outside_locally_selected_source_paths"
        elif (path and boundary.get("relation_type") in {"CALLS", "CALL_TARGET_FROM", "PERFORMS"}
              and not boundary.get("requested_procedure_dependency")
              and boundary.get("source_sha256") == versions.get(path)
              and boundary.get("position_status") == "source_excerpt_match"):
            scopes = [scope for scope in source_scopes if scope.get("relative_path") == path
                and scope.get("exclusive", True) is True
                and versions.get(path) == scope.get("source_sha256")]
            line = boundary.get("start_line", boundary.get("caller_line"))
            if scopes and type(line) is int and not any(scope["start_line"] <= line <= scope["end_line"] for scope in scopes):
                basis = "outside_version_matched_requested_occurrences"
        if basis:
            repository.append({**boundary, "scope_basis": basis,
                               "scope": "repository_diagnostic_without_question_relevance_proof"})
        else:
            relevant.append(boundary)
    return relevant, repository


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


def _call_graph(connection, check_cancel, *, navigation_rows=None):
    _cancel(check_cancel)
    incoming = defaultdict(list)
    edges = []
    aliases = defaultdict(set)
    source_rows = (connection.execute("SELECT relative_path FROM source_files") if navigation_rows is None
                   else [(row["target_path"],) for row in navigation_rows if row.get("target_path")])
    for row in source_rows:
        relative = row[0]
        for alias in (relative, Path(relative).name, Path(relative).stem):
            aliases[alias.casefold()].add(relative)
    rows = (row for row in (_navigation_graph(connection, check_cancel, details=True)
                           if navigation_rows is None else navigation_rows)
            if row["caller_line"] is not None)
    for row in sorted(rows, key=lambda row: (row["relative_path"], row["caller_line"],
                                            row["relation_type"], row["relation_order"])):
        _cancel(check_cancel)
        edge = {key: row[key] for key in ("relation_id", "relation_type", "target_name", "evidence_id",
                                         "caller_line", "target_path", "caller_program")}
        edge.update(caller_path=row["relative_path"], resolution=row["status"])
        if row.get("requested_procedure_dependency"):
            edge["requested_procedure_dependency"] = True
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


def _rule_leads(connection, paths, previews, question, check_cancel, *, coverage=None, diversify=False,
                source_scopes=()):
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
    rules_table = "business_rules"
    occurrences = "business_rule_occurrences"
    if source_scopes:
        from question_investigation import _scope_tables
        rules_table, occurrences = _scope_tables(connection, sorted(paths), source_scopes)
        if occurrences == "question_scope_occurrences":
            # Rule shapes may repeat elsewhere. Navigation locations and
            # budgets must describe only the retained physical occurrences.
            connection.execute("CREATE TEMP VIEW navigation_scope_rules AS SELECT b.rule_id,b.relative_path,"
                "b.program_name,b.paragraph_name,b.rule_kind,MIN(o.start_line) AS first_line,"
                "MAX(o.end_line) AS last_line,COUNT(*) AS occurrence_count,b.normalized_text,"
                "b.reads_json,b.writes_json,b.condition_json FROM question_scope_occurrences o "
                "CROSS JOIN main.business_rules b ON b.rule_id=o.rule_id GROUP BY b.rule_id")
            rules_table = "navigation_scope_rules"
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
            f"SELECT * FROM {rules_table} WHERE relative_path=? AND first_line<=? "
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
                f"SELECT * FROM {rules_table} WHERE relative_path=? "
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
                f"SELECT * FROM {rules_table} WHERE relative_path=? AND normalized_text LIKE ? "
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
                f"ORDER BY first_line,rule_id) AS group_rank FROM {rules_table} WHERE relative_path=? "
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
            f"SELECT b.* FROM {rules_table} b WHERE b.relative_path=? "
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
                "rule_id": row["rule_id"],
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
    if selected and connection.execute("SELECT 1 FROM sqlite_master WHERE name='business_rule_occurrences'").fetchone():
        by_rule = {item["rule_id"]: item for item in selected}
        slots = ",".join("?" for _ in by_rule)
        for row in connection.execute(f"SELECT rule_id,scope_json FROM {occurrences} "
                f"WHERE rule_id IN ({slots}) AND parse_status='candidate' GROUP BY rule_id", tuple(by_rule)):
            guards = [guard for guard in json.loads(row["scope_json"]) if guard.get("kind") == "COMPILATION"]
            if guards:
                by_rule[row["rule_id"]].update(compilation_status="candidate", compilation_guards=guards,
                                              support_basis="source_syntax_only")
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


def build_business_map(database_path, source_root, question, *, search_terms=None, focus_paths=None, prior_paths=None,
                       check_cancel=None, entry_program=None, history=()):
    """Map direct matches, transitive static callers and nearby rule operations.

    It reads the saved index only. In particular, it does not rescan source files
    or ask the model to summarise individual pages.
    """
    relation_direction = program_relation_direction(question)
    impact_requested = relation_direction == "incoming" or (
        relation_direction != "outgoing" and bool(_IMPACT_LANGUAGE.search(question)))
    def navigation_scopes(identity):
        if impact_requested or identity.get("status") != "resolved":
            return []
        from question_investigation import _question_scopes
        return _question_scopes(database_path, identity.get("direct_paths", []), question, history)
    discovery = discover_repository(database_path, question, search_terms=search_terms, focus_paths=focus_paths,
                                    check_cancel=check_cancel, fallback_to_repository=False,
                                    entry_program=entry_program, history=history,
                                    rooted_navigation_resolver=navigation_scopes)
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN")
        _, overview = _fast_snapshot(connection, source_root)
        identity = discovery["source_identity"]
        previous = identity.get("session_subject")
        if previous is None and identity.get("status") == "resolved":
            # The desktop can keep an explicit entry selected across turns.
            # It constrains the source, but must not erase a same-source field
            # referent. Only the most recent completed answer may nominate it.
            recent = next((message for message in reversed(list(history))
                           if message.get("role") == "assistant"), {})
            candidate = (recent.get("investigation_state") or {}).get("session_subject")
            if (isinstance(candidate, dict) and candidate.get("status") == "resolved"
                    and set(candidate.get("root_paths", ())) == set(identity.get("direct_paths", ()))):
                previous = candidate
        subject = build_session_subject(connection, question, identity,
                                        previous=previous)
        blocked = identity_blocks_analysis(identity)
        direct = {item["relative_path"] for item in discovery["selection_reasons"]
                  if "text_match" in item["reasons"]}
        if blocked:
            direct = set()
        elif identity["direct_paths"]:
            direct = set(identity["direct_paths"])
        else:
            exact_lexical = _lexical_identifier_paths(connection, question + " " + " ".join(search_terms or []))
            if exact_lexical:
                direct = exact_lexical
        query_direct = set(direct)
        historical = _paths_from_history(connection, prior_paths)
        if not direct and not blocked:
            direct.update(historical)
        intent = "impact" if impact_requested else "explain"
        rooted = discovery.get("rooted_navigation")
        incoming, all_edges = (_call_graph(connection, check_cancel,
            **({"navigation_rows": rooted["rows"]} if rooted else {})) if direct else ({}, []))
        outgoing, outgoing_frontier = {}, []
        if rooted:
            distance = rooted["distance"]
            selected = set(rooted["selected_paths"])
        elif identity.get("exclusive_source_scope") and direct:
            distance = {path: 0 for path in direct}
            selected = set(direct)
        elif intent == "impact" and direct:
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
        source_versions = {}
        selected_paths = sorted(selected)
        # Keep definition lookup proportional to the nominated sources. The
        # path index already exists in older snapshots; no catalog rebuild or
        # new repository-sized index is needed for an interactive question.
        for start in range(0, len(selected_paths), 400):
            _cancel(check_cancel)
            batch = selected_paths[start:start + 400]
            slots = ",".join("?" for _ in batch)
            for row in connection.execute("SELECT u.relative_path,u.name,f.sha256 FROM code_units u "
                    "JOIN source_files f ON f.relative_path=u.relative_path "
                    f"WHERE u.relative_path IN ({slots}) AND u.unit_type='Program' "
                    "ORDER BY u.relative_path,u.start_line", batch):
                program_names[row["relative_path"]].append(row["name"])
                source_versions[row["relative_path"]] = row["sha256"]
        ordered = sorted(selected, key=lambda path: (distance.get(path, 0), path))
        programs = [{"relative_path": path, "program_names": program_names[path] or [Path(path).stem.upper()],
                     "distance": distance.get(path, 0), "direct_source_match": path in query_direct}
                    for path in ordered]
        relevant_edges = [edge for edge in all_edges if edge["caller_path"] in selected and
                          (edge["target_path"] in selected or not edge["target_path"])]
        relevant_edges.sort(key=lambda edge: (distance.get(edge["caller_path"], 0), edge["caller_path"], edge["caller_line"]))
        rule_query = " ".join(search_terms or []) + " " + question
        rule_query += " " + " ".join(subject.get("focus_entities", []))
        rule_coverage = {}
        leads = _rule_leads(connection, query_direct, discovery["matched_pages"], rule_query, check_cancel,
                            coverage=rule_coverage, diversify=identity["status"] == "resolved",
                            source_scopes=rooted["source_scopes"] if rooted else ())
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
        if relation_direction:
            relation_candidates = [edge for edge in relevant_edges if edge["relation_type"] == "CALLS" and
                (relation_direction in {"outgoing", "related"} and edge["caller_path"] in direct or
                 relation_direction in {"incoming", "related"} and edge["target_path"] in direct)]
        relation_candidates.sort(key=lambda edge: (edge["caller_path"] not in direct,
                                                     -edge["caller_line"], edge["caller_path"]))
        seen_callers = set()
        for edge in relation_candidates:
            if edge["caller_path"] in seen_callers:
                continue
            if not relation_direction and intent != "impact" and edge["caller_line"] < 2000 and edge["caller_path"] in direct:
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
        boundaries, repository_boundaries = question_boundaries(discovery["boundaries"], ordered,
            source_scopes=rooted["source_scopes"] if rooted else (), source_versions=source_versions)
        return {"snapshot_id": overview["snapshot_id"], "intent": intent, "relation_direction": relation_direction,
                "direct_paths": sorted(query_direct), "selected_paths": ordered,
                "outgoing_dependency_paths": sorted(set(outgoing) - direct,
                    key=lambda path: (outgoing[path], path)),
                "outgoing_dependency_frontier": outgoing_frontier,
                "programs": programs, "relations": relevant_edges,
                "rule_leads": leads, "spotlights": spotlights,
                "source_identity": identity, "boundaries": boundaries,
                "repository_boundaries": repository_boundaries, "source_versions": source_versions,
                **({"navigation_scope": {key: rooted[key] for key in
                    ("status", "source_scopes", "root_paths", "dependency_expansion_complete", "relation_rows_read", "effects_verified")},
                    "outgoing_dependency_frontier": rooted["frontier"]} if rooted else {}),
                "session_subject": subject,
                "rule_lead_coverage": rule_coverage, "semantic_anchors": anchors[:4],
                "matched_files": discovery["matched_file_count"],
                "matched_pages": discovery["matched_page_count"],
                "search_terms": _query_terms(question, search_terms)[0]}
    finally:
        connection.close()
