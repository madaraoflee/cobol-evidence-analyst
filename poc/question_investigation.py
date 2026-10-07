"""Bounded calculation evidence coverage over already located source candidates.

Coverage describes source supplied to a request, never execution or complete
value flow. Indexed rules nominate locations; original source satisfies items.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections import deque
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

from file_impact_evidence import is_file_impact_question, nominate_file_impact_evidence
from business_behavior import wants_behavior_explanation
from question_intent import calculation_intent


_BUSINESS_DETAIL = re.compile(r"流程|(?:逻辑|邏輯)(?!\s*(?:标志|標誌|字段|欄位|栏位|变量|變量|类型|類型))|"
    r"处理目的|處理目的|业务目的|業務目的|业务功能|業務功能|程序(?:功能|作用)|"
    r"业务规则|業務規則|处理规则|處理規則|返回(?:标志|標誌|状态|狀態|结果|結果|码|碼|值)|"
    r"如何处理|如何處理|怎么处理|怎麼處理|用途|用于什么|用於什麼|业务含义|業務含義|"
    r"(?<![\w-])logic(?![\w-])|"
    r"\b(?:workflow|flow|purpose|business rules?|processing steps?|return (?:flag|status|code|result))\b", re.I)
_ARITHMETIC = ("COMPUTE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE")
_MAX_PATHS = 8
_MAX_FORMULAS = 32
_MAX_LINKED_RULES = 32
_MAX_FIELDS = 32
_MAX_INPUT_DEPTH = 4


def _get(value, name, default=None):
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _rule(row, kind):
    value = dict(row)
    result = {"kind": kind, "relative_path": value["relative_path"],
            "start_line": value.get("first_line", value.get("start_line")),
            "end_line": value.get("last_line", value.get("end_line")),
            "statement": value.get("normalized_text", value.get("statement", "")),
            "reads": json.loads(value["reads_json"]) if "reads_json" in value else value.get("reads", []),
            "writes": json.loads(value["writes_json"]) if "writes_json" in value else value.get("writes", []),
            "paragraph_name": value.get("paragraph_name"),
            "input_fields": value.get("input_fields", []),
            "input_owner_paths": value.get("input_owner_paths", []),
            "occurrence_count": value.get("occurrence_count", 1),
            "rule_kind": value.get("rule_kind"), "source_sha256": value.get("source_sha256")}
    if result["rule_kind"] in _ARITHMETIC and " GIVING " in result["statement"].upper():
        from structural_index import _unique_identifiers
        giving = result["statement"].upper().split(" GIVING ", 1)[1]
        giving = re.split(r"\b(?:ON|NOT|END-ADD|END-SUBTRACT|END-MULTIPLY|END-DIVIDE)\b", giving)[0]
        # Sparse arithmetic access can list an explicit GIVING receiver as a
        # read. It is an output, not a recursive input origin or a cycle.
        receivers = set(_unique_identifiers(giving)).intersection(result["writes"])
        result["reads"] = [field for field in result["reads"] if field not in receivers]
    return result


def _input_candidates(db, path, fields):
    """Follow bounded same-file write candidates, never reaching definitions."""
    queue = deque((field, 0, ()) for field in fields[:_MAX_FIELDS])
    visited, rules, collected, frontier = set(), set(), [], []
    while queue:
        field, depth, ancestors = queue.popleft()
        if field in ancestors:
            frontier.append({"kind": "inputs", "relative_path": path,
                             "reason": "input_dependency_cycle", "field": field})
            continue
        if field in visited:
            continue
        if depth >= _MAX_INPUT_DEPTH:
            frontier.append({"kind": "inputs", "relative_path": path,
                             "reason": "input_depth_budget", "field": field})
            continue
        if len(visited) >= _MAX_FIELDS:
            frontier.append({"kind": "inputs", "relative_path": path,
                             "reason": "field_name_budget", "field": field})
            break
        visited.add(field)
        rows = db.execute("SELECT b.* FROM business_rules b WHERE b.relative_path=? AND EXISTS "
            "(SELECT 1 FROM business_rule_fields f WHERE f.rule_id=b.rule_id AND f.field_role='write' "
            "AND f.field_name=?) ORDER BY b.first_line,b.rule_id LIMIT ?",
            (path, field, _MAX_LINKED_RULES + 1)).fetchall()
        for row in rows:
            if row["rule_id"] in rules:
                continue
            if len(rules) >= _MAX_LINKED_RULES:
                frontier.append({"kind": "inputs", "relative_path": path,
                                 "reason": "field_candidate_budget"})
                return collected, sorted(visited), frontier
            rules.add(row["rule_id"])
            candidate = _rule(row, "inputs")
            collected.append(candidate)
            queue.extend((read, depth + 1, (*ancestors, field)) for read in candidate["reads"] if read != field)
    # Multiple seed fields can meet before ancestry traversal detects a cycle.
    # Inspect the bounded candidate graph as well; self-updates require an input
    # origin rather than being treated as a multi-field dependency cycle.
    graph = {field: {read for row in collected if field in row["writes"]
                     for read in row["reads"] if read != field} for field in sorted(visited)}
    active, done = set(), set()
    def visit(field):
        if field in active:
            frontier.append({"kind": "inputs", "relative_path": path,
                             "reason": "input_dependency_cycle", "field": field})
            return
        if field in done or field not in graph:
            return
        active.add(field)
        for read in sorted(graph[field]):
            visit(read)
        active.remove(field)
        done.add(field)
    for field in graph:
        visit(field)
    frontier = list({json.dumps(gap, sort_keys=True): gap for gap in frontier}.values())
    required = list(dict.fromkeys([*fields, *(field for row in collected for field in row["reads"])]))
    return collected, required[:_MAX_FIELDS], frontier


def _indexed_candidates(database_path, paths, question, *, business_steps=False, root_paths=None,
                        file_impact=False):
    """Select exact field obligations before applying bounded source budgets."""
    candidates, frontier, hashes = [], [], {}
    primary_kind = "business_steps" if business_steps else "formula"
    tokens = set(re.findall(r"[A-Z][A-Z0-9_$#@-]*", question.upper()))
    encoded = quote(Path(database_path).resolve().as_posix(), safe="/:")
    with closing(sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        scoped_paths = paths[:_MAX_PATHS]
        slots = ",".join("?" for _ in scoped_paths)
        source_hashes = dict(db.execute("SELECT relative_path,sha256 FROM source_files "
                                       f"WHERE relative_path IN ({slots})", scoped_paths))
        hashes = {path: source_hashes[path] for path in scoped_paths if path in source_hashes}
        if len(paths) > _MAX_PATHS:
            frontier.append({"kind": primary_kind, "reason": "located_path_budget",
                             "omitted_count": len(paths) - _MAX_PATHS})
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='business_rules'").fetchone():
            return [], [{"kind": primary_kind, "reason": "indexed_rules_unavailable"}], hashes
        targeted = []
        field_matched = False
        kinds = "'COMPUTE','ADD','SUBTRACT','MULTIPLY','DIVIDE'"
        if business_steps:
            kinds += ",'MOVE','IF','EVALUATE','WHEN','EXEC_SQL'"
        if file_impact:
            kinds += ",'READ','START','WRITE','REWRITE','DELETE'"
        if tokens and scoped_paths:
            field_slots = ",".join("?" for _ in tokens)
            path_order = "CASE b.relative_path " + " ".join(
                f"WHEN ? THEN {index}" for index, _ in enumerate(scoped_paths)) + " END"
            # Match fields inside located paths before LIMIT, including inputs.
            # CROSS JOIN preserves the path-first lookup so common field names
            # elsewhere cannot cause a repository-wide field scan. Distinct
            # expressions for one result remain separate obligations.
            targeted = db.execute(
                "SELECT b.*,SUM(f.field_role='write') AS matched_writes,"
                "SUM(f.field_role='read') AS matched_reads FROM business_rules b "
                "CROSS JOIN business_rule_fields f ON f.rule_id=b.rule_id "
                f"WHERE f.field_name IN ({field_slots}) AND b.relative_path IN ({slots}) "
                "AND f.field_role IN ('read','write') "
                f"AND b.rule_kind IN ({kinds}) "
                "GROUP BY b.rule_id ORDER BY matched_writes DESC,matched_reads DESC,"
                + path_order + ",b.first_line,b.rule_id LIMIT ?",
                (*sorted(tokens), *scoped_paths, *scoped_paths, _MAX_FORMULAS + 1)).fetchall()
            # A known field assigned without arithmetic still defines the
            # question's target. Program names and unknown ASCII tokens do not.
            field_matched = bool(targeted) or db.execute(
                "SELECT 1 FROM business_rules b CROSS JOIN business_rule_fields f ON f.rule_id=b.rule_id "
                f"WHERE b.relative_path IN ({slots}) AND f.field_name IN ({field_slots}) "
                "AND f.field_role IN ('read','write') LIMIT 1",
                (*scoped_paths, *sorted(tokens))).fetchone() is not None
        if targeted:
            if len(targeted) > _MAX_FORMULAS:
                frontier.append({"kind": primary_kind, "reason": "formula_candidate_budget"})
            candidates.extend(_rule(row, primary_kind) for row in targeted[:_MAX_FORMULAS])
        # Without a field match, retain program-level discovery and its explicit
        # enumeration limits. Unrelated formulas are not missing evidence for
        # a question whose exact input or result field is already located.
        # Outgoing dependencies remain obligations: a callee can rename a
        # caller's field in its parameter list without changing that value.
        roots = set(paths if root_paths is None else root_paths)
        for path in (scoped_paths if not field_matched else [path for path in scoped_paths if path not in roots]):
            grouping = "paragraph_name,writes_json,rule_kind" if business_steps else "paragraph_name,writes_json"
            rows = db.execute(
                f"WITH grouped AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY {grouping} "
                "ORDER BY first_line,rule_id) AS group_rank FROM business_rules WHERE relative_path=? "
                f"AND rule_kind IN ({kinds})) "
                "SELECT * FROM grouped WHERE group_rank=1 ORDER BY first_line,rule_id LIMIT ?",
                (path, _MAX_FORMULAS + 1)).fetchall()
            if len(rows) > _MAX_FORMULAS:
                frontier.append({"kind": primary_kind, "relative_path": path, "reason": "formula_group_budget"})
            candidates.extend(_rule(row, primary_kind) for row in rows[:_MAX_FORMULAS])
        # Exact identifiers are a ranking signal, never an abbreviation expansion.
        candidates = list({(row["kind"], row["relative_path"], row["start_line"], row["statement"]): row
                           for row in candidates}.values())
        candidates.sort(key=lambda row: (
            -len(tokens.intersection(row["writes"])), -len(tokens.intersection(row["reads"])),
            paths.index(row["relative_path"]), row["start_line"]))
        if len(candidates) > _MAX_FORMULAS:
            frontier.append({"kind": primary_kind, "reason": "formula_candidate_budget"})
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
            for fields, kind in ((outputs, "result_adjustments"),):
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
            input_candidates, inputs, input_frontier = _input_candidates(db, path, inputs)
            candidates.extend(input_candidates)
            frontier.extend(input_frontier)
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
                # A confirmed COPY edge nominates physical declaration source;
                # the host path remains explicit instead of merging field names
                # from unrelated programs or asserting executed value flow.
                for copy in db.execute("SELECT s.relative_path FROM relations r JOIN symbols s "
                        "ON s.symbol_id=r.target_entity_id WHERE r.relative_path=? "
                        "AND r.relation_type='INCLUDES_COPY' AND r.status='confirmed'", (path,)):
                    copy_path = copy["relative_path"]
                    if copy_path not in scoped_paths:
                        continue
                    for declaration in db.execute("SELECT u.*,s.name AS field_name FROM symbols s "
                            "JOIN code_units u ON u.unit_id=s.definition_unit_id WHERE s.relative_path=? "
                            f"AND s.name IN ({slots}) AND u.normalized_text LIKE '% VALUE %' "
                            "ORDER BY u.start_line LIMIT ?", (copy_path, *inputs, _MAX_LINKED_RULES)):
                        candidates.append(_rule({**dict(declaration),
                            "input_fields": [declaration["field_name"]], "input_owner_paths": [path]}, "inputs"))
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
    requested = {(row["relative_path"], field) for row in candidates
                 if row["kind"] in {"formula", "business_steps", "inputs"}
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


def _visible_ids(candidate, pages, *, page_cache=None):
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
    active_format = candidate.get("source_format")
    if not isinstance(active_format, str) or active_format not in {"fixed", "free"}:
        active_format = "auto"
    for page in relevant:
        start, end = page["start_line"], page["end_line"]
        if start > cursor:
            break
        text = str(page.get("source_text", ""))
        key = (page["evidence_id"], start, end, text, active_format)
        cached = page_cache.get(key) if isinstance(page_cache, dict) else None
        if cached is None:
            cleaned_lines = []
            for raw in text.splitlines():
                cleaned, active_format, _ = _clean(raw, active_format)
                cleaned_lines.append(cleaned)
            cached = (cleaned_lines, active_format)
            if isinstance(page_cache, dict):
                page_cache[key] = cached
        cleaned_lines, active_format = cached
        for index, cleaned in enumerate(cleaned_lines, start):
            if first <= index <= (last if candidate.get("occurrence_count", 1) <= 1 else end):
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


def _dependencies(database_path, paths, candidates, hashes, *, business_steps=False):
    formulas = [row for row in candidates if row["kind"] == "formula"]
    fields_by_path = {path: {field for row in candidates if row["relative_path"] == path
                            for field in [*row["reads"], *row["writes"]]} for path in paths}
    dependency_paths = {row["relative_path"] for row in candidates if row["kind"] in
                        {"formula", "business_steps", "inputs"}}
    paragraphs = {(row["relative_path"], row["paragraph_name"]) for row in candidates
                  if row["kind"] in {"formula", "business_steps", "inputs"}}
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
                related = (business_steps or (edge["target_path"] in dependency_paths and not is_perform) or
                    (is_perform and (path, edge["target_name"]) in paragraphs) or
                    bool(fields_by_path[path].intersection(tokens)) or
                    (not formulas and edge["relation_type"] in {"CALLS", "CALL_TARGET_FROM"}
                     and "USING" in tokens))
                if not related:
                    continue
                unresolved = (edge["relation_type"] == "CALL_TARGET_FROM"
                              or edge["status"] != "confirmed" or not edge["target_path"])
                result.append({"kind": "dependencies", "relative_path": path,
                    "start_line": edge["start_line"], "end_line": edge["end_line"],
                    "statement": edge["text"], "reads": [], "writes": [],
                    "source_sha256": hashes.get(path), "unresolved": unresolved,
                    "source_available": bool(edge["target_path"]) and edge["relation_type"] != "CALL_TARGET_FROM",
                    "target_name": edge["target_name"], "relation_type": edge["relation_type"],
                    "unresolved_reason": "runtime_target_unresolved" if edge["relation_type"] == "CALL_TARGET_FROM"
                        else "external_implementation_unavailable"})
    return result, frontier


def _framework_dependencies(candidates, pages, framework_facts):
    """Resolve only the documented call whose complete source is supplied."""
    supplied_ids = {page.get("evidence_id") for page in pages}
    result = []
    for original in candidates:
        row = dict(original)
        if (row.get("kind") == "dependencies" and row.get("unresolved")
                and not row.get("source_available") and row.get("relation_type") == "CALLS"
                and _visible_ids(row, pages)):
            facts = [fact for fact in framework_facts
                if fact.get("kind") == "framework_operation"
                and fact.get("dependency_covered") is True
                and fact.get("interpretation_basis") == "documented_framework_rule"
                and fact.get("relation_type") == "CALLS"
                and fact.get("runtime_verified") is False
                and all(fact.get(key) == row.get(key) for key in
                    ("relative_path", "source_sha256", "start_line", "end_line", "target_name"))
                and fact.get("source_evidence_ids") and fact.get("reference_ids")
                and set(fact["source_evidence_ids"]) <= supplied_ids]
            if facts:
                row.update(unresolved=False, framework_fact_ids=[fact["fact_id"] for fact in facts],
                    framework_reference_ids=list(dict.fromkeys(identifier for fact in facts
                        for identifier in fact["reference_ids"])))
        result.append(row)
    return result


def _calculation_roots(business_map, source_pages):
    selected = set(business_map.get("selected_paths", []))
    identity = business_map.get("source_identity", {})
    roots = list(dict.fromkeys(business_map.get("direct_paths", [])))
    if not roots and identity.get("status") == "resolved":
        roots = list(dict.fromkeys(identity.get("direct_paths", [])))
    if not roots:
        roots = list(dict.fromkeys(page.get("relative_path") for page in source_pages
            if "conversation_context" in page.get("selection_reasons", [])
            and page.get("relative_path") in selected))
    return [path for path in roots if path in selected]


def _calculation_paths(business_map, source_pages):
    """Root obligations in matches; incoming callers remain navigation context."""
    selected = set(business_map.get("selected_paths", []))
    paths = _calculation_roots(business_map, source_pages)
    # Only a resolved outgoing dependency may nominate additional arithmetic.
    # A caller's unrelated formulas do not become obligations for its callee.
    # Build adjacency once: scanning every relation for each reached source,
    # followed by list membership checks, becomes superlinear on large chains.
    outgoing = {}
    for edge in business_map.get("relations", []):
        target = edge.get("target_path")
        if (target in selected and edge.get("relation_type") in {"CALLS", "INCLUDES_COPY"}
                and edge.get("resolution") == "confirmed"):
            outgoing.setdefault(edge.get("caller_path"), []).append(target)
    visited = set(paths)
    position = 0
    while position < len(paths):
        caller = paths[position]
        position += 1
        for target in outgoing.get(caller, ()):
            if target not in visited:
                visited.add(target)
                paths.append(target)
    return paths


def build_question_investigation(question, business_map, *, database_path=None,
                                source_pages=(), evidence_groups=(), completed_actions=(), max_actions=4,
                                candidate_cache=None, framework_facts=()):
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
    calculation_kind = calculation_intent(question)
    calculation = calculation_kind == "rules"
    file_impact = is_file_impact_question(question)
    business_steps = not calculation and (calculation_kind == "execution" or bool(_BUSINESS_DETAIL.search(question))
                                         or wants_behavior_explanation(question) or file_impact)
    if not calculation and not business_steps:
        return base
    if not located:
        kind = "business_steps" if business_steps else "formula"
        item = {"id": "calculation_" + kind, "kind": kind, "status": "OPEN",
                "evidence_ids": [], "candidate_count": 0, "missing_count": 0,
                "reason": kind + "_not_located"}
        return {**base, "required_items": [item], "open_gaps": [
            {"kind": kind, "status": "OPEN", "reason": kind + "_not_located"}]}
    # Request fitting repeatedly changes the visible pages, while the map and
    # its dependency closure remain fixed. Reuse only navigation here; evidence
    # visibility is still checked against the actual pages on every call.
    history_roots = tuple(dict.fromkeys(page.get("relative_path") for page in source_pages
        if "conversation_context" in page.get("selection_reasons", [])))
    scope_hit = (isinstance(candidate_cache, dict)
        and candidate_cache.get("scope_map") is business_map
        and candidate_cache.get("scope_snapshot") == business_map.get("snapshot_id")
        and candidate_cache.get("scope_history_roots") == history_roots)
    if scope_hit:
        paths, root_paths = candidate_cache["scope_paths"], candidate_cache["scope_roots"]
    else:
        paths = _calculation_paths(business_map, source_pages)
        root_paths = _calculation_roots(business_map, source_pages)
    scope_cache = {"scope_map": business_map, "scope_history_roots": history_roots,
                   "scope_snapshot": business_map.get("snapshot_id"),
                   "scope_paths": paths, "scope_roots": root_paths,
                   "visible_pages": candidate_cache.get("visible_pages", {})
                       if scope_hit else {}}
    candidates, frontier, hashes = [], [], {}
    impact_frontier = [{"kind": "dependencies", **gap}
                       for gap in business_map.get("outgoing_dependency_frontier", [])] if file_impact else []
    cache_hit = False
    if database_path is not None:
        key = (str(Path(database_path).resolve()), business_map.get("snapshot_id"), tuple(paths),
               tuple(root_paths), question)
        cache_hit = isinstance(candidate_cache, dict) and candidate_cache.get("key") == key
        if cache_hit:
            candidates, frontier, hashes = candidate_cache["value"]
            business_steps = candidate_cache.get("business_steps", business_steps)
        else:
            candidates, frontier, hashes = _indexed_candidates(database_path, paths, question,
                business_steps=business_steps, root_paths=root_paths, file_impact=file_impact)
            if calculation and not any(row["kind"] == "formula" for row in candidates):
                # Some calculations select or transfer values through branches.
                # Reuse the existing bounded step selection without asserting a
                # formula, complete value flow, or any actual runtime result.
                steps, step_frontier, step_hashes = _indexed_candidates(database_path, paths, question,
                    business_steps=True, root_paths=root_paths, file_impact=file_impact)
                if any(row["kind"] == "business_steps" for row in steps):
                    business_steps = True
                    candidates, frontier, hashes = steps, step_frontier, step_hashes
            if file_impact:
                file_candidates, file_frontier, file_hashes = nominate_file_impact_evidence(database_path, paths)
                candidates.extend(file_candidates)
                frontier.extend(file_frontier)
                hashes.update(file_hashes)
            dependencies, dependency_frontier = _dependencies(database_path, list(hashes), candidates, hashes,
                                                              business_steps=business_steps)
            candidates.extend(dependencies)
            frontier.extend(dependency_frontier)
            if isinstance(candidate_cache, dict):
                candidate_cache.clear()
                candidate_cache.update(**scope_cache, key=key, value=(candidates, frontier, hashes),
                                       business_steps=business_steps)
        paths = list(hashes)
    else:
        if isinstance(candidate_cache, dict):
            candidate_cache.update(scope_cache)
        candidates = [_rule(row, "business_steps" if business_steps else "formula")
                      for row in business_map.get("rule_leads", []) if row.get("relative_path") in paths
                      and (business_steps or row.get("rule_kind") in _ARITHMETIC)]
        if calculation and not candidates:
            steps = [row for row in business_map.get("rule_leads", [])
                if row.get("relative_path") in paths and row.get("rule_kind") in
                {"MOVE", "IF", "EVALUATE", "WHEN", "EXEC_SQL"}]
            business_steps = bool(steps)
            if len(steps) > _MAX_FORMULAS:
                frontier.append({"kind": "business_steps", "reason": "formula_candidate_budget"})
            candidates = [_rule(row, "business_steps") for row in steps[:_MAX_FORMULAS]]
    pages = _visible_pages(source_pages, paths, hashes)
    candidates = [*candidates, *_visible_input_declarations(pages, candidates)]
    candidates = _condition_spans(candidates, evidence_groups)
    unique = {}
    for row in candidates:
        if row.get("start_line"):
            unique[(row["kind"], row["relative_path"], row["start_line"], row["statement"])] = row
    candidates = list(unique.values())
    candidates = _framework_dependencies(candidates, pages, framework_facts)
    missing = []
    items = []
    kinds = ["formula", "inputs", "conditions", "result_adjustments", "dependencies"]
    if business_steps:
        kinds.insert(0, "business_steps")
    if file_impact:
        kinds.extend(("file_io", "file_definitions", "dds_definitions"))
    for kind in kinds:
        related = [row for row in candidates if row["kind"] == kind]
        supplied, absent, unresolved = [], [], []
        for row in related:
            identifiers = _visible_ids(row, pages, page_cache=scope_cache["visible_pages"])
            if identifiers:
                supplied.extend(identifiers)
            else:
                absent.append(row)
            if row.get("unresolved"):
                unresolved.append(row)
        missing.extend(absent)
        required = kind == ("business_steps" if business_steps else "formula")
        status = ("UNRESOLVED" if unresolved and not absent else "PARTIAL" if absent and supplied
                  else "OPEN" if absent or required and not related else
                  "SATISFIED" if related else "NOT_APPLICABLE")
        covered = [row for row in related if row.get("framework_fact_ids")]
        items.append({"id": "calculation_" + kind, "kind": kind, "status": status,
            "evidence_ids": list(dict.fromkeys(supplied)), "candidate_count": len(related),
            "missing_count": len(absent), "reason": ("runtime_target_unresolved" if
                any(row.get("unresolved_reason") == "runtime_target_unresolved" for row in unresolved)
                else "external_implementation_unavailable") if unresolved
                else "source_not_supplied" if absent else kind + "_not_located" if required and not related
                else "documented_framework_rule" if covered else "source_candidates_supplied" if related else "no_indexed_candidate",
            **({"framework_fact_ids": list(dict.fromkeys(identifier for row in covered
                    for identifier in row["framework_fact_ids"])),
                "framework_reference_ids": list(dict.fromkeys(identifier for row in covered
                    for identifier in row["framework_reference_ids"]))} if covered else {}),
            **({"targets": list(dict.fromkeys(row["target_name"] for row in unresolved))[:8]} if unresolved else {})})
    input_fields = {(row["relative_path"], field) for row in candidates
                    if row["kind"] in {"formula", "business_steps", "inputs"}
                    for field in row["reads"]}
    input_origins = {(path, field) for row in candidates if row["kind"] == "inputs"
                     for path in [row["relative_path"], *row.get("input_owner_paths", [])]
                     for field in [*(field for field in row["writes"] if field not in row["reads"]),
                                   *row.get("input_fields", [])]}
    unknown_inputs = sorted(input_fields - input_origins)
    if unknown_inputs:
        item = next(item for item in items if item["kind"] == "inputs")
        item.update(status="PARTIAL" if item["evidence_ids"] else "UNRESOLVED",
                    reason="input_source_not_located", fields=[field for _, field in unknown_inputs][:_MAX_FIELDS])
    input_frontier = [gap for gap in frontier if gap["kind"] == "inputs"]
    if input_frontier:
        item = next(item for item in items if item["kind"] == "inputs")
        if item["status"] in {"SATISFIED", "NOT_APPLICABLE"}:
            item.update(status="PARTIAL", reason="input_candidate_frontier")
    seen = set()
    for action in completed_actions:
        for tool in ("inspect_business_context", "read"):
            args = action.get(tool)
            if isinstance(args, Mapping):
                seen.add((tool, args.get("relative_path"), args.get("line", args.get("start_line"))))
    plans, planned = [], set()
    # File operands and layouts need physical source; a field slice can omit
    # SELECT/FD entirely. Keep these reads ahead of supplementary input origins.
    if file_impact:
        missing.sort(key=lambda row: row["kind"] not in {"file_io", "file_definitions", "dds_definitions"})
    for row in missing:
        key = (row["relative_path"], row["start_line"])
        if key in planned:
            continue
        planned.add(key)
        inspect = {"relative_path": key[0], "line": key[1],
                   "fields": list(dict.fromkeys([*row["writes"], *row["reads"], *row.get("input_fields", [])]))[:_MAX_FIELDS]}
        last = row["end_line"] if row.get("occurrence_count", 1) == 1 else key[1] + 8
        read = {"relative_path": key[0], "start_line": max(1, key[1] - 4),
                "end_line": min(max(key[1] + 8, last), key[1] + 96)}
        file_source = row["kind"] in {"file_io", "file_definitions", "dds_definitions"}
        if file_source:
            read = {"relative_path": key[0], "start_line": key[1], "end_line": last}
        tool = "read" if file_source or ("inspect_business_context", *key) in seen else "inspect_business_context"
        arguments = read if tool == "read" else inspect
        if (tool, key[0], arguments.get("line", arguments.get("start_line"))) in seen:
            continue
        plans.append({"tool": tool, "arguments": arguments, "read_fallback": read,
                      "reason": "input_origin_not_supplied" if row["kind"] == "inputs" and row.get("input_fields")
                                else row["kind"] + "_source_not_supplied"})
        if len(plans) >= max(0, min(int(max_actions), 4)):
            break
    if unknown_inputs and database_path is not None and len(plans) < min(max_actions, 4):
        from business_index import _clean
        from structural_index import DATA_ITEM_RE
        declared = set()
        for page in pages:
            for raw in str(page.get("source_text", "")).splitlines():
                match = DATA_ITEM_RE.match(_clean(raw, "auto")[0])
                if match:
                    declared.add((page["relative_path"], match.group(2).upper()))
        encoded = quote(Path(database_path).resolve().as_posix(), safe="/:")
        with closing(sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)) as db:
            counts = dict(db.execute("SELECT relative_path,line_count FROM source_files WHERE relative_path IN ("
                + ",".join("?" for _ in paths[:_MAX_PATHS]) + ")", paths[:_MAX_PATHS]))
        for path, field in unknown_inputs:
            cursor = 1
            for page in sorted((page for page in pages if page["relative_path"] == path),
                               key=lambda page: page["start_line"]):
                if page["start_line"] > cursor:
                    break
                cursor = max(cursor, page["end_line"] + 1)
            # A supplied declaration without VALUE, or a fully supplied file
            # without an origin, is a real unknown. A hidden declaration can
            # still be obtained through the existing semantic inspection tool.
            if (path, field) in declared or cursor > counts.get(path, 0):
                continue
            row = next((row for row in candidates if row["relative_path"] == path and field in row["reads"]
                        and ("inspect_business_context", path, row["start_line"]) not in seen), None)
            if row is None or (path, row["start_line"]) in planned:
                continue
            planned.add((path, row["start_line"]))
            plans.append({"tool": "inspect_business_context",
                "arguments": {"relative_path": path, "line": row["start_line"], "fields": [field]},
                "read_fallback": {"relative_path": path, "start_line": 1,
                                  "end_line": min(96, counts[path])},
                "read_fallback_basis": "bounded_declaration_candidate",
                "reason": "input_origin_not_supplied"})
            if len(plans) >= min(max_actions, 4):
                break
    if max_actions <= 0:
        plans = []
    external = next((item for item in items if item["kind"] == "dependencies"
                     and item["reason"] in {"external_implementation_unavailable", "runtime_target_unresolved"}), None)
    if external and calculation and not any(row["kind"] == "formula" for row in candidates):
        formula = next(item for item in items if item["kind"] == "formula")
        formula.update(status="UNRESOLVED", reason=external["reason"],
                       targets=external.get("targets", []))
    gaps = [{"kind": item["kind"], "status": item["status"], "reason": item["reason"]}
            for item in items if item["status"] in {"OPEN", "PARTIAL", "UNRESOLVED"}]
    gaps.extend(impact_frontier[:8])
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
    formula_supplied = any(item["kind"] in {"formula", "business_steps"} and item["evidence_ids"] for item in items)
    boundary_supplied = bool(external and external["evidence_ids"])
    state = "needs_evidence" if plans else "bounded_partial" if gaps else "located"
    file_observations = {}
    if file_impact:
        from file_impact import collect_file_impact
        file_observations = {"file_impact": collect_file_impact(pages)}
    known_file_supplied = any(row["kind"] in {"io_operation", "field_assignment"}
        for row in file_observations.get("file_impact", {}).get("observations", []))
    return {**base, **file_observations, "required_items": items, "planned_actions": plans, "open_gaps": gaps[:16],
            "state": state, "can_answer": (formula_supplied or boundary_supplied or known_file_supplied) and not plans,
            "candidate_paths": paths[:_MAX_PATHS],
            "candidate_cache": {"hit": cache_hit, "candidate_count": len(candidates)}}
