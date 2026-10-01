"""Bounded calculation evidence coverage over already located source candidates.

Coverage describes source supplied to a request, never execution or complete
value flow. Indexed rules nominate locations; original source satisfies items.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote


_CALCULATION = re.compile(r"计算|計算|公式|怎么算|怎麼算|如何算|算出|\b(?:calculation|calculate[ds]?|calculating|formula|computed?)\b", re.I)
_ARITHMETIC = ("COMPUTE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE")
_MAX_PATHS = 8
_MAX_FORMULAS = 32
_MAX_LINKED_RULES = 32
_MAX_FIELDS = 32


def _get(value, name, default=None):
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _rule(row, kind):
    value = dict(row)
    return {"kind": kind, "relative_path": value["relative_path"],
            "start_line": value.get("first_line", value.get("start_line")),
            "end_line": value.get("last_line", value.get("end_line")),
            "statement": value.get("normalized_text", value.get("statement", "")),
            "reads": json.loads(value["reads_json"]) if "reads_json" in value else value.get("reads", []),
            "writes": json.loads(value["writes_json"]) if "writes_json" in value else value.get("writes", []),
            "paragraph_name": value.get("paragraph_name"),
            "input_fields": value.get("input_fields", []),
            "occurrence_count": value.get("occurrence_count", 1),
            "rule_kind": value.get("rule_kind"), "source_sha256": value.get("source_sha256")}


def _indexed_candidates(database_path, paths, question):
    """Use existing sparse facts, with distinct results ahead of repeat counters."""
    candidates, frontier, hashes = [], [], {}
    encoded = quote(Path(database_path).resolve().as_posix(), safe="/:")
    with closing(sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        scoped_paths = paths[:_MAX_PATHS]
        slots = ",".join("?" for _ in scoped_paths)
        hashes = dict(db.execute("SELECT relative_path,sha256 FROM source_files "
                                f"WHERE relative_path IN ({slots})", scoped_paths))
        if len(paths) > _MAX_PATHS:
            frontier.append({"kind": "formula", "reason": "located_path_budget",
                             "omitted_count": len(paths) - _MAX_PATHS})
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='business_rules'").fetchone():
            return [], [{"kind": "formula", "reason": "indexed_rules_unavailable"}], hashes
        for path in paths[:_MAX_PATHS]:
            rows = db.execute(
                "WITH grouped AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY paragraph_name,writes_json "
                "ORDER BY first_line,rule_id) AS group_rank FROM business_rules WHERE relative_path=? "
                "AND rule_kind IN ('COMPUTE','ADD','SUBTRACT','MULTIPLY','DIVIDE')) "
                "SELECT * FROM grouped WHERE group_rank=1 ORDER BY first_line,rule_id LIMIT ?",
                (path, _MAX_FORMULAS + 1)).fetchall()
            if len(rows) > _MAX_FORMULAS:
                frontier.append({"kind": "formula", "relative_path": path, "reason": "formula_group_budget"})
            candidates.extend(_rule(row, "formula") for row in rows[:_MAX_FORMULAS])
        # Exact identifiers are a ranking signal, never an abbreviation expansion.
        tokens = set(re.findall(r"[A-Z][A-Z0-9_$#@-]*", question.upper()))
        candidates.sort(key=lambda row: (
            not bool(tokens.intersection(row["writes"])), not bool(tokens.intersection(row["reads"])),
            paths.index(row["relative_path"]), row["start_line"]))
        if len(candidates) > _MAX_FORMULAS:
            frontier.append({"kind": "formula", "reason": "formula_candidate_budget"})
            candidates = candidates[:_MAX_FORMULAS]
        formulas = list(candidates)
        # Field equality is scoped to the source path, not a cross-program binding.
        for path in dict.fromkeys(row["relative_path"] for row in formulas):
            local = [row for row in formulas if row["relative_path"] == path]
            outputs = list(dict.fromkeys(field for row in local for field in row["writes"]))
            inputs = list(dict.fromkeys(field for row in local for field in row["reads"]))
            for fields, kind in ((outputs, "result_adjustments"), (inputs, "inputs")):
                if len(fields) > _MAX_FIELDS:
                    frontier.append({"kind": kind, "relative_path": path, "reason": "field_name_budget",
                                     "omitted_count": len(fields) - _MAX_FIELDS})
            outputs, inputs = outputs[:_MAX_FIELDS], inputs[:_MAX_FIELDS]
            for fields, kind in ((outputs, "result_adjustments"), (inputs, "inputs")):
                if not fields:
                    continue
                slots = ",".join("?" for _ in fields)
                rows = db.execute(
                    "SELECT b.* FROM business_rules b WHERE b.relative_path=? AND EXISTS "
                    "(SELECT 1 FROM business_rule_fields f WHERE f.rule_id=b.rule_id "
                    f"AND f.field_role='write' AND f.field_name IN ({slots})) "
                    "ORDER BY b.first_line,b.rule_id LIMIT ?", (path, *fields, _MAX_LINKED_RULES + 1)).fetchall()
                if len(rows) > _MAX_LINKED_RULES:
                    frontier.append({"kind": kind, "relative_path": path, "reason": "field_candidate_budget"})
                candidates.extend(_rule(row, kind) for row in rows[:_MAX_LINKED_RULES])
            if inputs:
                slots = ",".join("?" for _ in inputs)
                declarations = db.execute("SELECT u.*,s.name AS field_name FROM symbols s "
                    "JOIN code_units u ON u.unit_id=s.definition_unit_id WHERE s.relative_path=? "
                    f"AND s.name IN ({slots}) AND u.normalized_text LIKE '% VALUE %' "
                    "ORDER BY u.start_line LIMIT ?", (path, *inputs, _MAX_LINKED_RULES + 1)).fetchall()
                if len(declarations) > _MAX_LINKED_RULES:
                    frontier.append({"kind": "inputs", "relative_path": path, "reason": "input_origin_budget"})
                for declaration in declarations[:_MAX_LINKED_RULES]:
                    candidates.append(_rule({**dict(declaration), "input_fields": [declaration["field_name"]]}, "inputs"))
                for signature in db.execute("SELECT * FROM code_units WHERE relative_path=? "
                        "AND unit_type='ProcedureSignature' ORDER BY start_line LIMIT 8", (path,)):
                    text = signature["normalized_text"].upper()
                    if "USING" in text:
                        tokens = set(re.findall(r"[A-Z][A-Z0-9_$#@-]*", text.split("USING", 1)[1]))
                        supplied_inputs = [field for field in inputs if field in tokens]
                        if supplied_inputs:
                            candidates.append(_rule({**dict(signature), "input_fields": supplied_inputs}, "inputs"))
            paragraphs = list(dict.fromkeys(row["paragraph_name"] for row in local if row["paragraph_name"]))
            fields = list(dict.fromkeys([*inputs, *outputs]))
            if paragraphs or fields:
                parts, args = [], [path]
                if paragraphs:
                    parts.append("b.paragraph_name IN (" + ",".join("?" for _ in paragraphs) + ")")
                    args.extend(paragraphs)
                if fields:
                    parts.append("EXISTS (SELECT 1 FROM business_rule_fields f WHERE f.rule_id=b.rule_id "
                                 "AND f.field_name IN (" + ",".join("?" for _ in fields) + "))")
                    args.extend(fields)
                rows = db.execute("SELECT b.* FROM business_rules b WHERE b.relative_path=? "
                    "AND b.rule_kind IN ('IF','WHEN','EVALUATE') AND (" + " OR ".join(parts) + ") "
                    "ORDER BY b.first_line,b.rule_id LIMIT ?", (*args, _MAX_LINKED_RULES + 1)).fetchall()
                if len(rows) > _MAX_LINKED_RULES:
                    frontier.append({"kind": "conditions", "relative_path": path, "reason": "condition_candidate_budget"})
                candidates.extend(_rule(row, "conditions") for row in rows[:_MAX_LINKED_RULES])
        for row in candidates:
            row["source_sha256"] = hashes.get(row["relative_path"])
    return candidates, frontier, hashes


def _visible_pages(source_pages, paths, hashes):
    pages, conflicts = {}, set()
    for page in source_pages:
        identifier = page.get("evidence_id")
        if not identifier or page.get("relative_path") not in paths or page.get("span_truncated"):
            continue
        if hashes and page.get("source_sha256") != hashes.get(page["relative_path"]):
            continue
        if identifier in pages and any(pages[identifier].get(key) != page.get(key) for key in
                ("relative_path", "start_line", "end_line", "source_sha256", "source_text", "include_chain")):
            conflicts.add(identifier)
        pages[identifier] = page
    lines = {}
    for identifier, page in pages.items():
        if identifier in conflicts:
            continue
        for line, text in enumerate(str(page.get("source_text", "")).splitlines(), page.get("start_line", 1)):
            key = (page["relative_path"], line)
            if key in lines and lines[key][0] != text:
                conflicts.update((identifier, lines[key][1]))
            else:
                lines[key] = (text, identifier)
    return [page for identifier, page in pages.items() if identifier not in conflicts]


def _visible_input_declarations(pages, candidates):
    """Sparse indexing omits working-storage; read supplied declarations only."""
    from business_index import _clean
    from structural_index import DATA_ITEM_RE
    requested = {(row["relative_path"], field) for row in candidates if row["kind"] == "formula"
                 for field in row["reads"]}
    origins = []
    for page in pages:
        for line, raw in enumerate(str(page.get("source_text", "")).splitlines(), page["start_line"]):
            text = _clean(raw, "auto")[0]
            match = DATA_ITEM_RE.match(text)
            if match and re.search(r"(?<![A-Z0-9_$#@-])VALUE(?![A-Z0-9_$#@-])", text, re.I):
                field = match.group(2).upper()
                if (page["relative_path"], field) in requested:
                    origins.append({"kind": "inputs", "relative_path": page["relative_path"],
                        "start_line": line, "end_line": line, "statement": text, "reads": [], "writes": [],
                        "input_fields": [field], "source_sha256": page.get("source_sha256"), "occurrence_count": 1})
    return origins


def _visible_ids(candidate, pages):
    from business_index import _clean
    first, last = candidate["start_line"], candidate["end_line"]
    # Repeated identical sparse rules have a first/last occurrence envelope,
    # rather than one continuous statement. Require the first actual statement.
    if candidate.get("occurrence_count", 1) > 1:
        last = first
    candidate_limit = first + 96 if candidate.get("occurrence_count", 1) > 1 else last
    relevant = [page for page in pages if page["relative_path"] == candidate["relative_path"]
                and page.get("start_line", 0) <= candidate_limit and page.get("end_line", 0) >= first]
    relevant.sort(key=lambda page: (page["start_line"], page["end_line"], page["evidence_id"]))
    cursor, lines, identifiers = first, {}, []
    for page in relevant:
        start, end = page["start_line"], page["end_line"]
        if start > cursor:
            break
        text_lines = str(page.get("source_text", "")).splitlines()
        for index, text in enumerate(text_lines, start):
            if first <= index <= (last if candidate.get("occurrence_count", 1) <= 1 else end):
                cleaned = _clean(text, "auto")[0]
                if index in lines and lines[index] != cleaned:
                    return []
                lines[index] = cleaned
        cursor = max(cursor, end + 1)
        identifiers.append(page["evidence_id"])
    def comparable(text):
        # Sparse facts uppercase code, so a differing literal cannot establish
        # identity. Preserve literals while comparing case-insensitive keywords.
        pieces = re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")", text)
        return re.sub(r"\s+", " ", "".join(piece if index % 2 else piece.upper()
                                             for index, piece in enumerate(pieces))).strip()

    text = comparable(" ".join(lines[index] for index in sorted(lines)))
    statement = comparable(candidate.get("statement", "")).rstrip(".")
    exact_statement = bool(statement and re.search(r"(?<![A-Z0-9_$#@-])" + re.escape(statement) +
                                                  r"(?=\s*(?:\.|$))", text))
    return list(dict.fromkeys(identifiers)) if cursor > last and exact_statement else []


def _condition_spans(candidates, evidence_groups):
    """Use parsed predicate spans when sparse facts include comment padding."""
    ends = {}
    for group in evidence_groups:
        for observation in _get(group, "observations", ()):
            if _get(observation, "semantic_role") != "condition":
                continue
            for ref in _get(observation, "source_refs", ()):
                key = (_get(ref, "original_relative_path"), _get(ref, "source_sha256"),
                       _get(ref, "start_line"))
                ends[key] = max(ends.get(key, 0), _get(ref, "end_line", 0))
    result = []
    for candidate in candidates:
        end = ends.get((candidate["relative_path"], candidate.get("source_sha256"), candidate["start_line"]))
        if candidate["kind"] == "conditions" and end and end < candidate["end_line"]:
            candidate = {**candidate, "end_line": end}
        result.append(candidate)
    return result


def _dependencies(database_path, paths, candidates, hashes):
    formulas = [row for row in candidates if row["kind"] == "formula"]
    formula_paths = {row["relative_path"] for row in formulas}
    fields_by_path = {path: {field for row in candidates if row["relative_path"] == path
                            for field in [*row["reads"], *row["writes"]]} for path in paths}
    paragraphs = {(row["relative_path"], row["paragraph_name"]) for row in formulas}
    result, frontier = [], []
    encoded = quote(Path(database_path).resolve().as_posix(), safe="/:")
    with closing(sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        for path in paths[:_MAX_PATHS]:
            rows = db.execute("SELECT r.target_name,r.relation_type,r.status,e.start_line,e.end_line,e.text,"
                "s.relative_path AS target_path FROM relations r JOIN evidence_spans e USING(evidence_id) "
                "LEFT JOIN symbols s ON s.symbol_id=r.target_entity_id WHERE r.relative_path=? "
                "AND r.relation_type IN ('CALLS','CALL_TARGET_FROM','PERFORMS','PERFORMS_THRU','INCLUDES_COPY') "
                "ORDER BY e.start_line,r.relation_id LIMIT 65", (path,)).fetchall()
            if len(rows) > 64:
                frontier.append({"kind": "dependencies", "relative_path": path,
                                 "reason": "dependency_candidate_budget", "minimum_omitted_count": 1})
            for edge in rows[:64]:
                tokens = set(re.findall(r"[A-Z][A-Z0-9_$#@-]*", edge["text"].upper()))
                is_perform = edge["relation_type"].startswith("PERFORM")
                related = ((edge["target_path"] in formula_paths and not is_perform) or
                    (is_perform and (path, edge["target_name"]) in paragraphs) or
                    bool(fields_by_path[path].intersection(tokens)) or
                    (not formulas and edge["relation_type"] in {"CALLS", "CALL_TARGET_FROM"}
                     and "USING" in tokens))
                if not related:
                    continue
                unresolved = edge["status"] != "confirmed" or not edge["target_path"]
                result.append({"kind": "dependencies", "relative_path": path,
                    "start_line": edge["start_line"], "end_line": edge["end_line"],
                    "statement": edge["text"], "reads": [], "writes": [],
                    "source_sha256": hashes.get(path), "unresolved": unresolved,
                    "target_name": edge["target_name"], "relation_type": edge["relation_type"]})
    return result, frontier


def _calculation_paths(business_map, source_pages):
    """Root obligations in matches; incoming callers remain navigation context."""
    selected = set(business_map.get("selected_paths", []))
    identity = business_map.get("source_identity", {})
    roots = list(dict.fromkeys(business_map.get("direct_paths", [])))
    if not roots and identity.get("status") == "resolved":
        roots = list(dict.fromkeys(identity.get("direct_paths", [])))
    if not roots:
        roots = list(dict.fromkeys(page.get("relative_path") for page in source_pages
            if "conversation_context" in page.get("selection_reasons", [])
            and page.get("relative_path") in selected))
    paths = [path for path in roots if path in selected]
    # Only a resolved outgoing dependency may nominate additional arithmetic.
    # A caller's unrelated formulas do not become obligations for its callee.
    position = 0
    while position < len(paths):
        caller = paths[position]
        position += 1
        for edge in business_map.get("relations", []):
            target = edge.get("target_path")
            if (edge.get("caller_path") == caller and target in selected and target not in paths
                    and edge.get("relation_type") in {"CALLS", "INCLUDES_COPY"}
                    and edge.get("resolution") == "confirmed"):
                paths.append(target)
    return paths


def build_question_investigation(question, business_map, *, database_path=None,
                                source_pages=(), evidence_groups=(), completed_actions=(), max_actions=4,
                                candidate_cache=None):
    """Return material obligations and bounded actions for a located calculation."""
    identity = business_map.get("source_identity", {})
    paths = list(dict.fromkeys(business_map.get("selected_paths", [])))
    located = bool(business_map.get("direct_paths")) or identity.get("status") == "resolved"
    if not located:
        history_paths = {page.get("relative_path") for page in source_pages
                         if "conversation_context" in page.get("selection_reasons", [])}
        located = bool(history_paths.intersection(paths))
    base = {"scope": "source_candidates", "semantic_execution_verified": False,
            "required_items": [], "planned_actions": [], "open_gaps": [],
            "state": "located" if located else "unresolved", "can_answer": located}
    if identity.get("status") in {"ambiguous", "not_found"}:
        return {**base, "state": "unresolved", "can_answer": False}
    calculation = bool(_CALCULATION.search(question))
    if not calculation:
        return base
    if not located:
        item = {"id": "calculation_formula", "kind": "formula", "status": "OPEN",
                "evidence_ids": [], "candidate_count": 0, "missing_count": 0,
                "reason": "formula_not_located"}
        return {**base, "required_items": [item], "open_gaps": [
            {"kind": "formula", "status": "OPEN", "reason": "formula_not_located"}]}
    paths = _calculation_paths(business_map, source_pages)
    candidates, frontier, hashes = [], [], {}
    cache_hit = False
    if database_path is not None:
        key = (str(Path(database_path).resolve()), business_map.get("snapshot_id"), tuple(paths), question)
        cache_hit = isinstance(candidate_cache, dict) and candidate_cache.get("key") == key
        if cache_hit:
            candidates, frontier, hashes = candidate_cache["value"]
        else:
            candidates, frontier, hashes = _indexed_candidates(database_path, paths, question)
            dependencies, dependency_frontier = _dependencies(database_path, paths, candidates, hashes)
            candidates.extend(dependencies)
            frontier.extend(dependency_frontier)
            if isinstance(candidate_cache, dict):
                candidate_cache.clear()
                candidate_cache.update(key=key, value=(candidates, frontier, hashes))
    else:
        candidates = [_rule(row, "formula") for row in business_map.get("rule_leads", [])
                      if row.get("rule_kind") in _ARITHMETIC and row.get("relative_path") in paths]
    pages = _visible_pages(source_pages, paths, hashes)
    candidates = [*candidates, *_visible_input_declarations(pages, candidates)]
    candidates = _condition_spans(candidates, evidence_groups)
    unique = {}
    for row in candidates:
        if row.get("start_line"):
            unique[(row["kind"], row["relative_path"], row["start_line"], row["statement"])] = row
    candidates = list(unique.values())
    missing = []
    items = []
    for kind in ("formula", "inputs", "conditions", "result_adjustments", "dependencies"):
        related = [row for row in candidates if row["kind"] == kind]
        supplied, absent, unresolved = [], [], []
        for row in related:
            identifiers = _visible_ids(row, pages)
            if identifiers:
                supplied.extend(identifiers)
            else:
                absent.append(row)
            if row.get("unresolved"):
                unresolved.append(row)
        missing.extend(absent)
        status = ("UNRESOLVED" if unresolved and not absent else "PARTIAL" if absent and supplied
                  else "OPEN" if absent or kind == "formula" and not related else
                  "SATISFIED" if related else "NOT_APPLICABLE")
        items.append({"id": "calculation_" + kind, "kind": kind, "status": status,
            "evidence_ids": list(dict.fromkeys(supplied)), "candidate_count": len(related),
            "missing_count": len(absent), "reason": "external_implementation_unavailable" if unresolved
                else "source_not_supplied" if absent else "formula_not_located" if kind == "formula" and not related
                else "source_candidates_supplied" if related else "no_indexed_candidate",
            **({"targets": list(dict.fromkeys(row["target_name"] for row in unresolved))[:8]} if unresolved else {})})
    input_fields = {(row["relative_path"], field) for row in candidates if row["kind"] == "formula"
                    for field in row["reads"]}
    input_origins = {(row["relative_path"], field) for row in candidates if row["kind"] == "inputs"
                     for field in [*row["writes"], *row.get("input_fields", [])]}
    unknown_inputs = sorted(input_fields - input_origins)
    if unknown_inputs:
        item = next(item for item in items if item["kind"] == "inputs")
        item.update(status="PARTIAL" if item["evidence_ids"] else "UNRESOLVED",
                    reason="input_source_not_located", fields=[field for _, field in unknown_inputs][:_MAX_FIELDS])
    seen = set()
    for action in completed_actions:
        for tool in ("inspect_business_context", "read"):
            args = action.get(tool)
            if isinstance(args, Mapping):
                seen.add((tool, args.get("relative_path"), args.get("line", args.get("start_line"))))
    plans, planned = [], set()
    for row in missing:
        key = (row["relative_path"], row["start_line"])
        if key in planned:
            continue
        planned.add(key)
        inspect = {"relative_path": key[0], "line": key[1],
                   "fields": list(dict.fromkeys([*row["writes"], *row["reads"]]))[:_MAX_FIELDS]}
        last = row["end_line"] if row.get("occurrence_count", 1) == 1 else key[1] + 8
        read = {"relative_path": key[0], "start_line": max(1, key[1] - 4),
                "end_line": min(max(key[1] + 8, last), key[1] + 96)}
        tool = "read" if ("inspect_business_context", *key) in seen else "inspect_business_context"
        arguments = read if tool == "read" else inspect
        if (tool, key[0], arguments.get("line", arguments.get("start_line"))) in seen:
            continue
        plans.append({"tool": tool, "arguments": arguments, "read_fallback": read,
                      "reason": row["kind"] + "_source_not_supplied"})
        if len(plans) >= max(0, min(int(max_actions), 4)):
            break
    if max_actions <= 0:
        plans = []
    external = next((item for item in items if item["kind"] == "dependencies"
                     and item["reason"] == "external_implementation_unavailable"), None)
    if external and not any(row["kind"] == "formula" for row in candidates):
        formula = items[0]
        formula.update(status="UNRESOLVED", reason="external_implementation_unavailable",
                       targets=external.get("targets", []))
    gaps = [{"kind": item["kind"], "status": item["status"], "reason": item["reason"]}
            for item in items if item["status"] in {"OPEN", "PARTIAL", "UNRESOLVED"}]
    gaps.extend(frontier[:8])
    input_coverage = next(item for item in items if item["kind"] == "inputs")
    for group in evidence_groups:
        for gap in _get(group, "open_frontier", ()):
            if (gap.get("reason") in {"caller_candidates_limited", "caller_expansion_candidates_limited",
                                      "caller_nearby_candidates_limited"}
                    and input_coverage["status"] in {"SATISFIED", "NOT_APPLICABLE"}):
                continue
            if gap.get("relative_path") in paths:
                gaps.append({"kind": "semantic_candidates", "reason": gap.get("reason")})
                if len(gaps) >= 16:
                    break
        if len(gaps) >= 16:
            break
    # A bounded partial answer may describe supplied rules with their gaps. It
    # must not claim complete lineage or a unique final runtime definition.
    formula_supplied = any(item["kind"] == "formula" and item["evidence_ids"] for item in items)
    boundary_supplied = bool(external and external["evidence_ids"])
    state = "needs_evidence" if plans else "bounded_partial" if gaps else "located"
    return {**base, "required_items": items, "planned_actions": plans, "open_gaps": gaps[:16],
            "state": state, "can_answer": (formula_supplied or boundary_supplied) and not plans,
            "candidate_paths": paths[:_MAX_PATHS],
            "candidate_cache": {"hit": cache_hit, "candidate_count": len(candidates)}}
