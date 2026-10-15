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
from repository_identity import identity_blocks_analysis
from question_planner import QuestionPlanner, EvidencePlanner, question_references
from statement_facts import COBOL_VERBS, SCOPE_TERMINATORS, CobolLexError, lex_cobol, data_references


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


def _file_mapping_candidates(database_path, paths, question, source_scopes=()):
    """Nominate file identity/layout from versioned local source, not DataRefs."""
    references = set(question_references(question))
    requested_names = {name for name in references if name.endswith("-FILE")}
    explicit_mapping = bool(re.search(r"(?:文件|\bfiles?\b).*(?:映射|布局|对应|對應|路径|路徑|mapping|layout)|"
        r"(?:SELECT|FD|ASSIGN)\b", question, re.I))
    if not requested_names and not explicit_mapping:
        return [], [], {}, []
    from file_impact_evidence import _definitions, _lines

    candidates, frontier, entities, hashes = [], [], [], {}
    encoded = quote(Path(database_path).resolve().as_posix(), safe="/:")
    with closing(sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        if "repo_pages" not in tables:
            return [], [{"kind": "file_definitions", "reason": "indexed_source_pages_unavailable"}], {}, []
        scoped = paths[:_MAX_PATHS]
        slots = ",".join("?" for _ in scoped)
        files = {row["relative_path"]: dict(row) for row in db.execute(
            f"SELECT relative_path,sha256,artifact_kind FROM source_files WHERE relative_path IN ({slots})", scoped)}
        hashes = {path: row["sha256"] for path, row in files.items()}
        definitions, layouts, compilation_lines = [], {}, {}
        options = db.execute("SELECT value FROM metadata WHERE key='source_options'").fetchone()
        context = json.loads(options[0]).get("compilation_context", {}) if options else {}
        def apply_compilation(row):
            statuses = compilation_lines[row["relative_path"]]
            guards = [guard for number in range(row["start_line"], row["end_line"] + 1)
                      for guard in statuses.get(number, {}).get("guards", ())]
            unknown = any(statuses.get(number, {}).get("status") is None
                          for number in range(row["start_line"], row["end_line"] + 1))
            if unknown:
                return {**row, "compilation_status": "candidate", "unresolved": True,
                        "unresolved_reason": "compile_condition_unknown", "target_name": named(row)[1],
                        "compilation_guards": list({json.dumps(guard, sort_keys=True): guard for guard in guards}.values())}
            return row
        def words(text):
            try:
                return [token.value for token in lex_cobol(text)]
            except CobolLexError:
                return []
        def named(row):
            tokens = words(row["statement"])
            offset = 2 if tokens[:2] == ["SELECT", "OPTIONAL"] else 1
            return (tokens[0], tokens[offset]) if len(tokens) > offset else (None, None)
        for path, file in files.items():
            local_frontier = []
            lines = _lines(db, path, file["sha256"], tables, local_frontier)
            from statement_facts import CompilationGuardState
            compilation = CompilationGuardState(context)
            statuses, active_lines = {}, {}
            for number, code in sorted(lines.items()):
                directive = compilation.feed(code, number)
                statuses[number] = {"status": compilation.status, "guards": compilation.guards}
                active_lines[number] = "" if directive or compilation.status is False else code
            for guard in compilation.finish():
                for number in statuses:
                    if number >= guard["source_line"] and statuses[number]["status"] is True:
                        statuses[number]["status"] = None
            compilation_lines[path], lines = statuses, active_lines
            rows, _ = _definitions(path, lines, file["sha256"], False, local_frontier)
            definitions.extend(apply_compilation(row) for row in rows if named(row)[0] in {"SELECT", "FD"})
            layout_rows, _ = _definitions(path, lines, file["sha256"], True, local_frontier)
            layouts[path] = [apply_compilation(row) for row in layout_rows if named(row)[0] in {"01", "77"}]
            frontier.extend(local_frontier)
        available = {named(row)[1] for row in definitions}
        path_index = " INDEXED BY idx_symbols_path" if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_symbols_path'").fetchone() else ""
        requested_names = {name for name in requested_names if name in available or not db.execute(
            "SELECT 1 FROM symbols" + path_index + f" WHERE relative_path IN ({slots}) AND name=? "
            "AND symbol_type IN ('Field','ConditionName') LIMIT 1", (*scoped, name)).fetchone()}
        requested_names.update(references.intersection(available))
        if not requested_names:
            if not explicit_mapping:
                return [], [], {}, []
            return [], [{"kind": "file_definitions", "reason": "file_entity_not_located"}], hashes, []
        for name in sorted(requested_names):
            selected = [row for row in definitions if named(row)[1] == name]
            selects = [row for row in selected if named(row)[0] == "SELECT"]
            descriptions = [row for row in selected if named(row)[0] == "FD"]
            bound = len(selects) == len(descriptions) == 1 and selects[0]["relative_path"] == descriptions[0]["relative_path"]
            reasons = []
            if not selects or not descriptions:
                reasons.append("file_declaration_missing")
            if len(selects) > 1 or len(descriptions) > 1:
                reasons.append("file_reference_ambiguous")
            if not selected:
                reasons = ["file_entity_not_located"]
            if any(row.get("compilation_status") == "candidate" for row in selected):
                bound = False
                reasons.append("compile_condition_unknown")
            for reason in reasons:
                frontier.append({"kind": "file_definitions", "reason": reason, "file_name": name,
                                 "select_count": len(selects), "fd_count": len(descriptions)})
            entities.append({"entity_type": "FileRef", "file_name": name, "binding_status": "bound" if bound else "candidate",
                "source_versions": [{"relative_path": row["relative_path"], "source_sha256": row["source_sha256"],
                    "start_line": row["start_line"], "end_line": row["end_line"]} for row in selected],
                "external_identity_verified": False, "diagnostics": reasons})
            candidates.extend({**row, "file_name": name} for row in selected)
            records = set()
            for row in descriptions:
                record_rows = [layout for layout in layouts[row["relative_path"]]
                    if row["start_line"] <= layout["start_line"] <= row["end_line"]]
                records.update(named(layout)[1] for layout in record_rows)
                candidates.append({**row, "kind": "file_layout", "file_name": name})
                if len(record_rows) != 1:
                    frontier.append({"kind": "file_layout", "reason": "file_record_layout_ambiguous_or_missing", "file_name": name})
            work_records = set()
            host_paths = {row["relative_path"] for row in selected}
            for path in host_paths:
                rows = db.execute("SELECT b.* FROM business_rules b WHERE relative_path=? "
                    "AND rule_kind IN ('READ','START','WRITE','REWRITE','DELETE') ORDER BY first_line,rule_id LIMIT 65", (path,)).fetchall()
                if len(rows) > 64:
                    frontier.append({"kind": "file_io", "reason": "file_io_candidate_budget", "relative_path": path})
                for row in rows[:64]:
                    statuses = compilation_lines[path]
                    if any(statuses.get(number, {}).get("status") is False
                           for number in range(row["first_line"], row["last_line"] + 1)):
                        continue
                    tokens = words(row["normalized_text"])
                    if len(tokens) < 2 or tokens[1] not in {name, *records}:
                        continue
                    limits = [scope for scope in source_scopes if scope["relative_path"] == path and scope.get("exclusive") is not False]
                    if limits and not any(scope["start_line"] <= row["first_line"] <= row["last_line"] <= scope["end_line"] for scope in limits):
                        continue
                    candidates.append(apply_compilation({**_rule(row, "file_io"), "reads": [], "writes": [],
                                                        "source_sha256": hashes[path], "file_name": name}))
                    for marker in ("INTO", "FROM"):
                        if marker in tokens and tokens.index(marker) + 1 < len(tokens):
                            work_records.add(tokens[tokens.index(marker) + 1])
            for record in sorted(work_records):
                local = [row for path in host_paths for row in layouts[path] if named(row)[1] == record]
                copied = []
                for path in host_paths:
                    for edge in db.execute("SELECT r.status,r.target_name,s.relative_path AS target_path,e.* FROM relations r "
                        "JOIN evidence_spans e USING(evidence_id) LEFT JOIN symbols s ON s.symbol_id=r.target_entity_id "
                        "WHERE r.relative_path=? AND r.relation_type='INCLUDES_COPY' ORDER BY e.start_line LIMIT 65", (path,)):
                        member = [row for row in layouts.get(edge["target_path"], ()) if named(row)[1] == record]
                        if not member:
                            if edge["status"] != "confirmed":
                                frontier.append({"kind": "copy_layout", "reason": "copy_layout_member_not_located",
                                                 "target_name": edge["target_name"], "record_name": record})
                            continue
                        call = {"kind": "copy_layout", "relative_path": path, "source_sha256": hashes[path],
                            "start_line": edge["start_line"], "end_line": edge["end_line"], "statement": edge["text"],
                            "reads": [], "writes": [], "occurrence_count": 1, "file_name": name, "record_name": record}
                        candidates.append(call)
                        status = compilation_lines[path].get(edge["start_line"], {}).get("status")
                        if status is not True or any(row.get("compilation_status") == "candidate" for row in member):
                            candidates[-1] = {**call, "unresolved": True, "unresolved_reason": "compile_condition_unknown",
                                              "target_name": edge["target_name"]}
                            frontier.append({"kind": "copy_layout", "reason": "compile_condition_unknown", "record_name": record})
                            continue
                        if edge["status"] != "confirmed" or words(edge["text"]) != ["COPY", edge["target_name"], "."]:
                            frontier.append({"kind": "copy_layout", "reason": "copy_layout_binding_unresolved", "record_name": record})
                        else:
                            copied.extend(member)
                if len(local) + len(copied) != 1:
                    frontier.append({"kind": "file_layout", "reason": "work_record_layout_ambiguous_or_missing", "record_name": record})
                candidates.extend({**row, "kind": "file_layout" if row in local else "copy_layout", "file_name": name,
                                   "record_name": record} for row in [*local, *copied])
            entities[-1]["record_names"] = sorted(records | work_records)
    return candidates, frontier, hashes, entities


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
            "layout_fields": value.get("layout_fields", []),
            "input_owner_paths": value.get("input_owner_paths", []),
            "occurrence_count": value.get("occurrence_count", 1),
            "rule_kind": value.get("rule_kind"), "source_sha256": value.get("source_sha256"),
            "rule_id": value.get("rule_id")}
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


def _question_scopes(database_path, paths, question, history=()):
    """Bind procedure and occurrence limits to current indexed source units."""
    if not paths:
        return []
    encoded = quote(Path(database_path).resolve().as_posix(), safe="/:")
    with closing(sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        identifiers = question_references(question)
        rows = []
        if identifiers:
            symbol_index = " INDEXED BY idx_symbols_path" if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_symbols_path'").fetchone() else ""
            rows = db.execute("SELECT u.*,f.sha256 AS source_sha256,f.format_hint AS source_format FROM symbols s" + symbol_index + " "
                "JOIN code_units u ON u.unit_id=s.definition_unit_id JOIN source_files f USING(relative_path) "
                "WHERE s.relative_path IN (" + ",".join("?" for _ in paths[:_MAX_PATHS]) + ") "
                "AND (s.symbol_type='Paragraph' OR s.symbol_type='Section' AND EXISTS ("
                "SELECT 1 FROM code_units p WHERE p.relative_path=u.relative_path "
                "AND p.unit_type='ProcedureSignature' AND p.start_line<u.start_line)) AND s.name IN (" +
                ",".join("?" for _ in identifiers) + ") ORDER BY u.relative_path,u.start_line",
                (*paths[:_MAX_PATHS], *identifiers)).fetchall()
        inherited = []
        whole_program = bool(re.search(r"整个程序|整個程序|全程序|所有段落|全部段落|\b(?:whole|entire)\s+program\b", question, re.I))
        if not rows and not whole_program and re.search(r"刚才|剛才|前面|这里|這裡|这个|這個|同一个|同一個|仍|\b(?:previous|same|that|this)\b", question, re.I):
            for message in reversed(list(history)):
                state = message.get("investigation_state") or {}
                previous = state.get("question_investigation") or {}
                plan = state.get("analysis_plan") or previous.get("analysis_plan") or {}
                for scope in plan.get("source_scopes", []):
                    if scope.get("relative_path") not in paths[:_MAX_PATHS]:
                        continue
                    current = db.execute("SELECT sha256 FROM source_files WHERE relative_path=?",
                                         (scope["relative_path"],)).fetchone()
                    if current and current[0] == scope.get("source_sha256"):
                        inherited.append(dict(scope))
                if inherited:
                    break
        scopes = [{"relative_path": row["relative_path"], "source_sha256": row["source_sha256"],
            "procedure_name": row["name"], "procedure_kind": row["unit_type"],
            "source_format": row["source_format"],
            "start_line": row["start_line"], "end_line": row["end_line"],
            "range_basis": "indexed_procedure", "complete_value_flow": False} for row in rows] or inherited
        if whole_program:
            for scope in scopes:
                scope["exclusive"] = False
        count = None
        if re.search(r"(?:前|首|最前)[两兩二2]\s*(?:条|條|个|個)?\s*IF\b|\bfirst\s+(?:two|2)\s+IF", question, re.I):
            count = 2
        elif (re.search(r"第一\s*(?:条|條|个|個)?\s*IF\b|\bfirst\s+IF\b", question, re.I)
              and re.search(r"(?:下一|紧接|緊接).*?IF\b|\bnext\s+IF\b", question, re.I)):
            count = 2
        if count and db.execute("SELECT 1 FROM sqlite_master WHERE name='business_rule_occurrences'").fetchone():
            for scope in scopes:
                occurrences = db.execute("SELECT o.start_line,o.end_line FROM business_rule_occurrences o "
                    "JOIN business_rules b USING(rule_id) WHERE o.relative_path=? AND b.rule_kind='IF' "
                    "AND o.start_line>=? AND o.end_line<=? ORDER BY o.start_line,o.start_column LIMIT ?",
                    (scope["relative_path"], scope["start_line"], scope["end_line"], count + 1)).fetchall()
                if len(occurrences) >= count:
                    scope.update(start_line=occurrences[0]["start_line"],
                        end_line=(occurrences[count]["start_line"] - 1 if len(occurrences) > count else scope["end_line"]),
                        range_basis="first_if_occurrences", occurrence_count=count)
        explicit_limit = count or re.search(r"只看|只问|只問|仅看|僅看|仅限|僅限|\b(?:only|within|limited\s+to)\b", question, re.I)
        if scopes and not explicit_limit and not whole_program:
            # Mentioning a procedure as one part of a compound question does
            # not discard independently requested fields elsewhere in its
            # program. Preserve the body as evidence, without making it an
            # exclusive filter when a locally known operand lies outside it.
            for scope in scopes:
                for field in identifiers:
                    outside = db.execute("SELECT 1 FROM business_rules b JOIN business_rule_fields f USING(rule_id) "
                        "WHERE b.relative_path=? AND f.field_name=? AND NOT(b.first_line>=? AND b.last_line<=?) LIMIT 1",
                        (scope["relative_path"], field, scope["start_line"], scope["end_line"])).fetchone()
                    inside = db.execute("SELECT 1 FROM business_rules b JOIN business_rule_fields f USING(rule_id) "
                        "WHERE b.relative_path=? AND f.field_name=? AND b.first_line>=? AND b.last_line<=? LIMIT 1",
                        (scope["relative_path"], field, scope["start_line"], scope["end_line"])).fetchone()
                    if outside and not inside:
                        scope["exclusive"] = False
                        break
        return scopes


def _scope_tables(db, paths, scopes):
    """Limit rule nomination before SQL budgets; preserve occurrence identity."""
    scopes = [scope for scope in scopes if scope.get("exclusive") is not False]
    if not scopes:
        return "business_rules", "business_rule_occurrences"
    constrained = {scope["relative_path"] for scope in scopes}
    unscoped = [path for path in paths if path not in constrained]
    clauses, arguments = [], []
    for scope in scopes:
        clauses.append("(relative_path=? AND start_line>=? AND end_line<=?)")
        arguments.extend((scope["relative_path"], scope["start_line"], scope["end_line"]))
    if unscoped:
        clauses.append("relative_path IN (" + ",".join("?" for _ in unscoped) + ")")
        arguments.extend(unscoped)
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='business_rule_occurrences'").fetchone():
        db.execute("CREATE TEMP TABLE question_scope_occurrences AS SELECT * FROM business_rule_occurrences WHERE " +
                   " OR ".join(clauses), arguments)
        db.execute("CREATE INDEX question_scope_occurrence_rule ON question_scope_occurrences(rule_id)")
        db.execute("CREATE TEMP VIEW question_scope_rules AS SELECT b.* FROM main.business_rules b "
                   "WHERE EXISTS (SELECT 1 FROM question_scope_occurrences o WHERE o.rule_id=b.rule_id)")
        return "question_scope_rules", "question_scope_occurrences"
    # Older indexes can supply a body, but cannot prove occurrence completeness.
    clauses = [clause.replace("start_line", "first_line").replace("end_line", "last_line") for clause in clauses]
    db.execute("CREATE TEMP TABLE question_scope_rules AS SELECT * FROM business_rules WHERE " +
               " OR ".join(clauses), arguments)
    return "question_scope_rules", "business_rule_occurrences"


def _field_declarations(db, path, fields):
    if not fields:
        return []
    slots = ",".join("?" for _ in fields)
    return db.execute("SELECT u.*,s.name AS field_name,"
        "substr(s.qualified_name,instr(s.qualified_name,'::')+2) AS field_identity "
        "FROM symbols s JOIN code_units u ON u.unit_id=s.definition_unit_id WHERE s.relative_path=? "
        f"AND (s.name IN ({slots}) OR substr(s.qualified_name,instr(s.qualified_name,'::')+2) IN ({slots})) "
        "AND s.symbol_type IN ('Field','ConditionName') ORDER BY u.start_line LIMIT ?",
        (path, *fields, *fields, _MAX_LINKED_RULES + 1)).fetchall()


def _parse_physical_body(text, first, source_format):
    """Apply the source frontend's format/continuation rules, retaining lines."""
    from statement_parser import parse_statements
    from structural_index import normalize_cobol_lines

    normalized, _ = normalize_cobol_lines(text, source_format)
    physical = [""] * len(text.splitlines())
    for line in normalized:
        physical[line.start_line - 1] = line.text
    return parse_statements("\n".join(physical), start_line=first, source_format="free")


def _parsed_controlled_rules(db, headers, tokens, rules_table, occurrence_table):
    """Recover guards through the shared parser when sparse occurrences omit them."""
    result, frontier, visited = {}, [], set()
    positioned_headers = []
    for header in headers:
        if header["rule_kind"] not in {"IF", "EVALUATE", "WHEN"}:
            continue
        occurrences = db.execute(f"SELECT start_line FROM {occurrence_table} WHERE rule_id=? "
            "AND relative_path=? ORDER BY start_line,start_column LIMIT 65",
            (header["rule_id"], header["relative_path"])).fetchall()
        if len(occurrences) > 64:
            frontier.append({"kind": "conditions", "relative_path": header["relative_path"],
                             "reason": "control_occurrence_budget"})
        positioned_headers.extend({**dict(header), "first_line": row["start_line"]} for row in occurrences[:64])
    for header in positioned_headers:
        path = header["relative_path"]
        unit = db.execute("SELECT start_line,end_line FROM code_units WHERE relative_path=? "
            "AND unit_type='Paragraph' AND start_line<=? AND end_line>=? ORDER BY start_line DESC LIMIT 1",
            (path, header["first_line"], header["first_line"])).fetchone()
        source = db.execute("SELECT sha256,format_hint,line_count FROM source_files WHERE relative_path=?", (path,)).fetchone()
        if unit:
            first, last = unit["start_line"] + 1, unit["end_line"]
        else:
            signature = db.execute("SELECT end_line FROM code_units WHERE relative_path=? "
                "AND unit_type='ProcedureSignature' AND end_line<? ORDER BY end_line DESC LIMIT 1",
                (path, header["first_line"])).fetchone()
            if not signature:
                continue
            first, last = signature[0] + 1, source["line_count"]
        key = (path, first, last)
        if key in visited:
            continue
        if len(visited) >= _MAX_PATHS:
            frontier.append({"kind": "conditions", "relative_path": path, "reason": "control_procedure_budget"})
            break
        visited.add(key)
        if last - first >= 8192 or not db.execute("SELECT 1 FROM sqlite_master WHERE name='repo_pages'").fetchone():
            frontier.append({"kind": "conditions", "relative_path": path, "reason": "control_scope_candidate_budget"})
            continue
        evidence = db.execute("SELECT e.start_line,e.end_line,e.text FROM repo_pages p "
            "JOIN evidence_spans e USING(evidence_id) WHERE p.relative_path=? AND p.source_sha256=? "
            "AND e.source_sha256=p.source_sha256 AND p.end_line>=? AND p.start_line<=? "
            "AND p.span_truncated=0 ORDER BY p.start_line LIMIT 65", (path, source["sha256"], first, last)).fetchall()
        lines = {}
        for page in evidence[:64]:
            lines.update({number: line for number, line in enumerate(page["text"].splitlines(), page["start_line"])
                          if first <= number <= last})
        text = "\n".join(lines.get(number, "") for number in range(first, last + 1))
        if len(lines) != last - first + 1 or len(text) > 192000:
            frontier.append({"kind": "conditions", "relative_path": path, "reason": "control_scope_source_not_indexed"})
            continue
        parsed = _parse_physical_body(text, first, source["format_hint"])
        if not parsed.complete:
            frontier.append({"kind": "conditions", "relative_path": path, "reason": "control_scope_incomplete",
                             "diagnostics": list(dict.fromkeys(item.reason for item in parsed.boundaries))})
        for statement in parsed.statements:
            if not any(tokens.intersection(reference.identity for reference in data_references(guard.condition))
                       for guard in statement.guards if guard.kind in {"IF", "EVALUATE", "WHEN"}):
                continue
            rows = db.execute(f"SELECT b.*,o.start_line AS occurrence_start,o.end_line AS occurrence_end,"
                f"o.start_column AS occurrence_column,o.procedure_name AS occurrence_procedure FROM {rules_table} b "
                f"JOIN {occurrence_table} o USING(rule_id) WHERE o.relative_path=? AND b.rule_kind=? "
                "AND o.start_line=? AND o.source_sha256=? ORDER BY o.start_column LIMIT ?",
                (path, statement.kind, statement.span.start_line, source["sha256"], _MAX_FORMULAS + 1)).fetchall()
            for row in rows:
                result[(row["rule_id"], row["occurrence_start"], row["occurrence_column"])] = {
                    **dict(row), "first_line": row["occurrence_start"], "last_line": row["occurrence_end"],
                    "paragraph_name": row["occurrence_procedure"]}
                if len(result) > _MAX_FORMULAS:
                    return list(result.values()), frontier
    return list(result.values()), frontier


def _indexed_candidates(database_path, paths, question, *, business_steps=False, root_paths=None,
                        file_impact=False, source_scopes=(), discovery_terms=(), nondata_entities=()):
    """Select exact field obligations before applying bounded source budgets."""
    candidates, frontier, hashes = [], [], {}
    primary_kind = "business_steps" if business_steps else "formula"
    tokens = set(question_references(question)) - set(nondata_entities)
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
        rules_table, occurrence_table = _scope_tables(db, scoped_paths, source_scopes)
        requested_fields = set()
        if tokens:
            field_slots = ",".join("?" for _ in tokens)
            matched_symbols = db.execute("SELECT relative_path,program_name,symbol_type,name,"
                "substr(qualified_name,instr(qualified_name,'::')+2) AS field_identity FROM symbols "
                f"WHERE relative_path IN ({slots}) AND (name IN ({field_slots}) OR "
                f"substr(qualified_name,instr(qualified_name,'::')+2) IN ({field_slots}))",
                (*scoped_paths, *sorted(tokens), *sorted(tokens))).fetchall()
            bound = {row["name"] for row in matched_symbols} | {row["field_identity"] for row in matched_symbols}
            # A parsed operand can support direct source syntax even when its
            # declaration is missing/unsupported. This does not claim symbol
            # binding or a verified numeric layout.
            source_operands = {row[0] for row in db.execute("SELECT DISTINCT f.field_name FROM business_rules b "
                "CROSS JOIN business_rule_fields f ON f.rule_id=b.rule_id "
                f"WHERE b.relative_path IN ({slots}) AND f.field_name IN ({field_slots})",
                (*scoped_paths, *sorted(tokens)))}
            bound.update(source_operands)
            for token in list(tokens):
                identities = {row["field_identity"] for row in matched_symbols
                    if row["symbol_type"] in {"Field", "ConditionName"}
                    and token in {row["name"], row["field_identity"]}}
                requested_fields.update(identities)
                if len(identities) > 1 and " OF " not in token:
                    frontier.append({"kind": primary_kind, "reason": "field_reference_ambiguous",
                                     "field": token, "candidates": sorted(identities)[:8]})
                if identities:
                    # The declaration supplies canonical identity, while a
                    # statement may legally use its unique bare spelling.
                    # Preserve actual parsed operand syntax as a lookup key;
                    # do not replace it with a name absent from the rule index.
                    if token not in source_operands:
                        tokens.discard(token)
                    tokens.update(identities)
                    if " OF " in token and token not in source_operands:
                        for symbol in matched_symbols:
                            if symbol["field_identity"] != token or symbol["symbol_type"] not in {"Field", "ConditionName"}:
                                continue
                            count = db.execute("SELECT COUNT(DISTINCT qualified_name) FROM symbols "
                                "WHERE relative_path=? AND program_name IS ? AND name=? "
                                "AND symbol_type IN ('Field','ConditionName')",
                                (symbol["relative_path"], symbol["program_name"], symbol["name"])).fetchone()[0]
                            if count == 1:
                                # A unique declaration binds this spelling.
                                # An overloaded bare name remains a candidate,
                                # never an assumed alias for one record.
                                tokens.add(symbol["name"])
            unbound = sorted(token for token in tokens if token not in bound
                             and (" OF " in token or any(character in token for character in "-_$#@")))
            if unbound:
                frontier.append({"kind": primary_kind, "reason": "requested_field_not_bound", "fields": unbound[:8]})
                # A manual's business label can lead to a different, locally
                # found source label. It nominates this program's rules; it
                # never binds the original label as a COBOL field. Preserve
                # the unresolved identity and require physical formula source.
                discovered = False
                for term in discovery_terms:
                    references = question_references(term)
                    if not references or set(references).issubset(tokens):
                        continue
                    text_rows = db.execute("SELECT normalized_text FROM code_units "
                        f"WHERE relative_path IN ({slots}) AND instr(upper(normalized_text),?)>0 LIMIT 9",
                        (*scoped_paths, str(term).upper())).fetchall()
                    if not text_rows and db.execute("SELECT 1 FROM sqlite_master WHERE name='repo_fts'").fetchone():
                        # DISPLAY text and comments are searchable source
                        # labels, although they are not executable rule facts.
                        # Reuse the same frozen FTS pages, with source-version
                        # identity, rather than scanning live source files.
                        text_rows = db.execute("SELECT e.text FROM repo_fts "
                            "JOIN repo_pages p ON p.page_id=repo_fts.rowid "
                            "JOIN evidence_spans e ON e.evidence_id=p.evidence_id "
                            f"WHERE repo_fts MATCH ? AND p.relative_path IN ({slots}) "
                            "AND p.source_sha256=e.source_sha256 AND p.source_sha256=("
                            "SELECT sha256 FROM source_files s WHERE s.relative_path=p.relative_path) LIMIT 9",
                            ('"' + str(term).replace('"', '""') + '"', *scoped_paths)).fetchall()
                    if any(re.search(r"(?<![A-Z0-9_$#@-])" + re.escape(str(term).upper()) +
                                     r"(?![A-Z0-9_$#@-])", row[0].upper()) for row in text_rows):
                        discovered = True
                        break
                if not requested_fields and not source_scopes and not discovered:
                    return [], frontier, hashes
        for scope in source_scopes:
            if scope.get("range_basis") == "indexed_procedure":
                unit = db.execute("SELECT * FROM code_units WHERE relative_path=? AND name=? "
                    "AND unit_type IN ('Paragraph','Section') AND start_line=?",
                    (scope["relative_path"], scope["procedure_name"], scope["start_line"])).fetchone()
                if unit:
                    candidates.append(_rule({**dict(unit), "paragraph_name": unit["name"],
                                             "source_sha256": scope["source_sha256"]}, "procedure_body"))
        targeted = []
        field_matched = bool(requested_fields)
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
                f"SUM(f.field_role='read') AS matched_reads FROM {rules_table} b "
                "CROSS JOIN business_rule_fields f ON f.rule_id=b.rule_id "
                f"WHERE f.field_name IN ({field_slots}) AND b.relative_path IN ({slots}) "
                "AND f.field_role IN ('read','write','condition') "
                f"AND b.rule_kind IN ({kinds}) "
                "GROUP BY b.rule_id ORDER BY matched_writes DESC,matched_reads DESC,"
                + path_order + ",b.first_line,b.rule_id LIMIT ?",
                (*sorted(tokens), *scoped_paths, *scoped_paths, _MAX_FORMULAS + 1)).fetchall()
            # A known field assigned without arithmetic still defines the
            # question's target. Program names and unknown ASCII tokens do not.
            field_matched = field_matched or bool(targeted) or db.execute(
                f"SELECT 1 FROM {rules_table} b CROSS JOIN business_rule_fields f ON f.rule_id=b.rule_id "
                f"WHERE b.relative_path IN ({slots}) AND f.field_name IN ({field_slots}) "
                "AND f.field_role IN ('read','write','condition') LIMIT 1",
                (*scoped_paths, *sorted(tokens))).fetchone() is not None
            # Conditional operands select the controlled effects as well as
            # the header. The parser's occurrence guards supply that relation;
            # neighboring paragraphs or similarly named fields do not.
            if business_steps and any(row["rule_kind"] in {"IF", "EVALUATE", "WHEN"} for row in targeted):
                def matching_guard(encoded_scopes):
                    return any(tokens.intersection(reference.identity for reference in
                        data_references(str(guard.get("condition", ""))))
                        for guard in json.loads(encoded_scopes or "[]")
                        if guard.get("kind") in {"IF", "EVALUATE", "WHEN"})
                db.create_function("question_guard_matches", 1, matching_guard)
                controlled = db.execute(f"SELECT b.* FROM {rules_table} b WHERE b.relative_path IN ({slots}) "
                    f"AND b.rule_kind IN ({kinds}) AND EXISTS (SELECT 1 FROM {occurrence_table} o "
                    "WHERE o.rule_id=b.rule_id AND question_guard_matches(o.scope_json)) "
                    "ORDER BY b.first_line,b.rule_id LIMIT ?", (*scoped_paths, _MAX_FORMULAS + 1)).fetchall()
                if not controlled:
                    controlled, control_frontier = _parsed_controlled_rules(db, targeted, tokens, rules_table, occurrence_table)
                    frontier.extend(control_frontier)
                targeted = list({(row["rule_id"], row["first_line"], row["last_line"]): row
                                 for row in [*targeted, *controlled]}.values())
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
                f"ORDER BY first_line,rule_id) AS group_rank FROM {rules_table} WHERE relative_path=? "
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
        if requested_fields and not any(row["kind"] in {"formula", "business_steps"} for row in candidates):
            for path in scoped_paths:
                for declaration in _field_declarations(db, path, sorted(requested_fields)):
                    candidates.append(_rule({**dict(declaration), "layout_fields": [declaration["field_identity"]]}, "declarations"))
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
            layout_fields = list(dict.fromkeys([*inputs, *outputs]))
            # Show physical hierarchy with the PIC/USAGE declaration. Qualifier
            # nodes are evidence about identity, never extra operand reads.
            for field in list(layout_fields):
                for ref in data_references(field):
                    layout_fields.append(ref.name + "".join(" OF " + name for name in ref.qualifiers))
                    layout_fields.extend(ref.qualifiers[index] + "".join(" OF " + name for name in ref.qualifiers[index + 1:])
                                         for index in range(len(ref.qualifiers)))
            for declaration in _field_declarations(db, path, list(dict.fromkeys(layout_fields))[:_MAX_FIELDS]):
                candidates.append(_rule({**dict(declaration), "layout_fields": [declaration["field_identity"]]}, "declarations"))
            for copy in db.execute("SELECT s.relative_path FROM relations r JOIN symbols s "
                    "ON s.symbol_id=r.target_entity_id WHERE r.relative_path=? "
                    "AND r.relation_type='INCLUDES_COPY' AND r.status='confirmed'", (path,)):
                copy_path = copy["relative_path"]
                if copy_path in scoped_paths:
                    for declaration in _field_declarations(db, copy_path, list(dict.fromkeys(layout_fields))[:_MAX_FIELDS]):
                        candidates.append(_rule({**dict(declaration), "layout_fields": [declaration["field_identity"]],
                                                 "input_owner_paths": [path]}, "declarations"))
            for fields, kind in ((outputs, "result_adjustments"),):
                if not fields:
                    continue
                slots = ",".join("?" for _ in fields)
                rows = db.execute(
                    f"SELECT b.* FROM {rules_table} b WHERE b.relative_path=? AND EXISTS "
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
                declarations = [row for row in _field_declarations(db, path, inputs)
                                if " VALUE " in row["normalized_text"].upper()]
                if len(declarations) > _MAX_LINKED_RULES:
                    frontier.append({"kind": "inputs", "relative_path": path, "reason": "input_origin_budget"})
                for declaration in declarations[:_MAX_LINKED_RULES]:
                    candidates.append(_rule({**dict(declaration), "input_fields": [declaration["field_identity"]]}, "inputs"))
                # A confirmed COPY edge nominates physical declaration source;
                # the host path remains explicit instead of merging field names
                # from unrelated programs or asserting executed value flow.
                for copy in db.execute("SELECT s.relative_path FROM relations r JOIN symbols s "
                        "ON s.symbol_id=r.target_entity_id WHERE r.relative_path=? "
                        "AND r.relation_type='INCLUDES_COPY' AND r.status='confirmed'", (path,)):
                    copy_path = copy["relative_path"]
                    if copy_path not in scoped_paths:
                        continue
                    for declaration in _field_declarations(db, copy_path, inputs):
                        if " VALUE " not in declaration["normalized_text"].upper():
                            continue
                        candidates.append(_rule({**dict(declaration),
                            "input_fields": [declaration["field_identity"]], "input_owner_paths": [path]}, "inputs"))
                for signature in db.execute("SELECT * FROM code_units WHERE relative_path=? "
                        "AND unit_type='ProcedureSignature' ORDER BY start_line LIMIT 8", (path,)):
                    text = signature["normalized_text"].upper()
                    if "USING" in text:
                        parameters = set(question_references(text.split("USING", 1)[1]))
                        supplied_inputs = [field for field in inputs if field in parameters]
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
                rows = db.execute(f"SELECT b.* FROM {rules_table} b WHERE b.relative_path=? "
                    "AND b.rule_kind IN ('IF','WHEN','EVALUATE') AND (" + " OR ".join(parts) + ") "
                    "ORDER BY b.first_line,b.rule_id LIMIT ?", (*args, _MAX_LINKED_RULES + 1)).fetchall()
                if len(rows) > _MAX_LINKED_RULES:
                    frontier.append({"kind": "conditions", "relative_path": path, "reason": "condition_candidate_budget"})
                candidates.extend(_rule(row, "conditions") for row in rows[:_MAX_LINKED_RULES])
        for row in candidates:
            row["source_sha256"] = hashes.get(row["relative_path"])
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='business_rule_occurrences'").fetchone():
            identifiers = list(dict.fromkeys(row["rule_id"] for row in candidates if row.get("rule_id")))
            occurrences = {}
            if identifiers:
                # Occurrences are bounded independently from retrieval shapes.
                # They retain source columns and branch scopes without loading
                # every repeated rule in a large program.
                slots = ",".join("?" for _ in identifiers)
                for occurrence in db.execute("SELECT * FROM (SELECT *,ROW_NUMBER() OVER (PARTITION BY rule_id "
                        f"ORDER BY start_line,start_column) AS occurrence_rank FROM {occurrence_table} "
                        f"WHERE rule_id IN ({slots})) WHERE occurrence_rank<=64", identifiers):
                    occurrences.setdefault(occurrence["rule_id"], []).append(dict(occurrence))
            for row in candidates:
                row["occurrences"] = occurrences.get(row.get("rule_id"), [])
                if source_scopes and row["occurrences"]:
                    row.update(start_line=min(item["start_line"] for item in row["occurrences"]),
                        end_line=max(item["end_line"] for item in row["occurrences"]),
                        occurrence_count=len(row["occurrences"]), require_all_occurrences=True)
                    names = {item.get("procedure_name") for item in row["occurrences"]}
                    if len(names) == 1:
                        row["paragraph_name"] = names.pop()
                conditional = [item for item in row["occurrences"] if item["parse_status"] == "candidate"]
                if conditional and len(conditional) == len(row["occurrences"]):
                    row.update(compilation_status="candidate", support_basis="source_syntax_only",
                               unresolved=True, unresolved_reason="compile_condition_unknown",
                               target_name=(row["writes"] or row["reads"] or ["conditional_source"])[0],
                               compilation_guards=[guard for item in conditional for guard in json.loads(item["scope_json"])
                                                   if guard.get("kind") == "COMPILATION"])
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


def _check_occurrence_scopes(scopes, pages):
    """A positional limit cannot assert a closed IF after cutting its body."""
    checked, gaps = [], []
    for original in scopes:
        scope = dict(original)
        if scope.get("range_basis") == "first_if_occurrences":
            lines = {}
            for page in pages:
                if page["relative_path"] != scope["relative_path"] or page.get("source_sha256") != scope["source_sha256"]:
                    continue
                lines.update({number: text for number, text in enumerate(page["source_text"].splitlines(), page["start_line"])
                              if scope["start_line"] <= number <= scope["end_line"]})
            if len(lines) == scope["end_line"] - scope["start_line"] + 1:
                source = "\n".join(lines[number] for number in range(scope["start_line"], scope["end_line"] + 1))
                parsed = _parse_physical_body(source, scope["start_line"], scope.get("source_format", "auto"))
                scope["control_scope_status"] = "source_scope_closed" if parsed.complete else "incomplete_or_unsupported"
                if not parsed.complete:
                    gaps.append({"kind": "conditions", "reason": "statement_scope_incomplete",
                        "relative_path": scope["relative_path"], "start_line": scope["start_line"],
                        "end_line": scope["end_line"], "diagnostics": [item.reason for item in parsed.boundaries]})
            else:
                scope["control_scope_status"] = "source_not_fully_supplied"
        checked.append(scope)
    return checked, gaps


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


def _flow_input_origins(candidates, pages, evidence_groups):
    """Close only actual reads with complete, versioned source-flow witnesses."""
    import hashlib
    from statement_facts import data_references

    def tokens(text):
        try:
            values = [(token.kind, token.value) for token in lex_cobol(str(text))]
        except CobolLexError:
            return ()
        while values and values[-1][1] == ".":
            values.pop()
        return tuple(values)

    def field_identity(text):
        references = data_references(str(text))
        return references[0].identity if len(references) == 1 else text

    ref_cache = {}
    def supplied(ref):
        key = json.dumps(ref, sort_keys=True)
        if key in ref_cache:
            return ref_cache[key]
        first, last = ref.get("start_line"), ref.get("end_line")
        if (type(first) is not int or type(last) is not int or not 0 < first <= last
                or last - first >= 4096 or not ref.get("source_sha256") or not ref.get("excerpt_sha256")):
            return ()
        lines, identifiers = {}, []
        for page in pages:
            if (page.get("relative_path") != ref.get("relative_path")
                    or page.get("source_sha256") != ref["source_sha256"]
                    or tuple(page.get("include_chain") or ()) != tuple(ref.get("include_chain") or ())
                    or tuple(page.get("replacement_trace") or ()) != tuple(ref.get("replacement_trace") or ())
                    or page.get("span_truncated")):
                continue
            used = False
            for number, text in enumerate(str(page.get("source_text", "")).splitlines(), page["start_line"]):
                if first <= number <= last:
                    if number in lines and lines[number] != text:
                        ref_cache[key] = ()
                        return ()
                    lines[number], used = text, True
            if used:
                identifiers.append(page["evidence_id"])
        complete = all(number in lines for number in range(first, last + 1))
        actual = hashlib.sha256("\n".join(lines[number] for number in range(first, last + 1)).encode()).hexdigest() if complete else None
        ref_cache[key] = tuple(dict.fromkeys(identifiers)) if actual == ref["excerpt_sha256"] else ()
        return ref_cache[key]

    proofs = []
    for group in evidence_groups:
        analysis = _get(group, "semantic_analysis", {}) or {}
        for assignment in analysis.get("assignments", []):
            proofs.extend(assignment.get("read_origins", []))
    reads = {}
    for candidate in candidates:
        if candidate["kind"] not in {"formula", "business_steps", "inputs"}:
            continue
        occurrences = candidate.get("occurrences") or ([candidate] if candidate.get("occurrence_count", 1) == 1 else [])
        for field in candidate["reads"]:
            pair = candidate["relative_path"], field
            reads.setdefault(pair, []).append((candidate, occurrences))
    resolved, invalidated, witnesses = set(), set(), []
    for pair, candidates_and_occurrences in reads.items():
        matched, complete, identifiers, missing_readers = [], True, [], []
        for candidate, occurrences in candidates_and_occurrences:
            if not occurrences:
                complete = False
            for occurrence in occurrences:
                matches = [proof for proof in proofs if field_identity(proof.get("reader_field")) == field_identity(pair[1])
                    and tokens(proof.get("reader_statement")) == tokens(candidate["statement"])
                    and any(ref.get("relative_path") == pair[0] and ref.get("source_sha256") == candidate.get("source_sha256")
                        and ref.get("start_line") == occurrence["start_line"] and ref.get("end_line") == occurrence["end_line"]
                        and (not occurrence.get("start_column") or ref.get("start_column") == occurrence["start_column"])
                        for ref in proof.get("reader_source_refs", []))]
                if not matches:
                    complete = False
                    missing_readers.append({"relative_path": pair[0], "source_sha256": candidate.get("source_sha256"),
                        "field": pair[1], "statement": candidate["statement"], "start_line": occurrence["start_line"],
                        "end_line": occurrence["end_line"], "start_column": occurrence.get("start_column")})
                for proof in matches:
                    # A source-flow proof matched to this read still controls
                    # closure when one of its required excerpts is absent.
                    # A supplied VALUE declaration cannot substitute for the
                    # missing assignment/call/context that this proof needs.
                    matched.append(proof)
                    reference_ids = [supplied(ref) for ref in proof.get("source_refs", [])]
                    if not reference_ids or not all(reference_ids):
                        complete = False
                        continue
                    identifiers.extend(identifier for values in reference_ids for identifier in values)
                    if proof.get("status") != "resolved_source_origin":
                        complete = False
                        if proof.get("status") == "unresolved_source_origin":
                            # A version- and occurrence-matched flow proof
                            # takes precedence over a standalone VALUE clause.
                            # Retain its exact unknown rather than allowing a
                            # declaration to close loop/reentry/read provenance.
                            invalidated.add(pair)
        if matched:
            if not complete:
                invalidated.add(pair)
            initial_definitions = {json.dumps(definition, sort_keys=True): definition for proof in matched
                                   for definition in proof.get("initial_definitions", [])}
            witnesses.append({"relative_path": pair[0], "field": pair[1],
                "status": "source_origin_supplied" if complete else "source_origin_unresolved",
                "basis": "reaching_definitions_and_confirmed_call_returns",
                "evidence_ids": list(dict.fromkeys(identifiers)),
                "reader_occurrences": list(dict.fromkeys(proof["reader_occurrence_id"] for proof in matched)),
                "unknown_reasons": sorted({reason for proof in matched for reason in proof.get("unknown_reasons", [])}),
                "initial_definitions": list(initial_definitions.values()),
                "activation_semantics": sorted({definition["activation_semantics"] for definition in initial_definitions.values()
                                                if definition.get("activation_semantics")}),
                "runtime_value_known": False,
                "missing_reader_occurrences": list({json.dumps(row, sort_keys=True): row for row in missing_readers}.values()),
                "execution_verified": False})
        if complete and matched:
            resolved.add(pair)
    return resolved, invalidated, witnesses


def _visible_ids(candidate, pages, *, page_cache=None):
    from business_index import _clean
    if candidate.get("occurrences"):
        supplied = []
        for occurrence in candidate["occurrences"]:
            if occurrence["source_sha256"] != candidate.get("source_sha256"):
                continue
            local = {**candidate, "occurrences": [], "occurrence_count": 1,
                     "start_line": occurrence["start_line"], "end_line": occurrence["end_line"],
                     "start_column": occurrence["start_column"], "end_column": occurrence["end_column"]}
            identifiers = _visible_ids(local, pages, page_cache=page_cache)
            if candidate.get("require_all_occurrences") and not identifiers:
                return []
            supplied.extend(identifiers)
        return list(dict.fromkeys(supplied))
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
    # Textual evidence verification also covers declarations and target
    # resources whose syntax is outside the statement parser (PIC, DDS, CL).
    # Whole-span equality proves only that the supplied text matches; it does
    # not promote unsupported semantics into a parsed fact.
    if statement and text.rstrip(".") == statement:
        return list(dict.fromkeys(identifiers)) if cursor > last else []
    try:
        actual, expected = lex_cobol(text), lex_cobol(statement)
    except CobolLexError:
        return []
    expected_values = [(token.kind, token.value) for token in expected]
    exact_statement = False
    for index in range(len(actual) - len(expected) + 1):
        if not expected or [(token.kind, token.value) for token in actual[index:index + len(expected)]] != expected_values:
            continue
        following = actual[index + len(expected):index + len(expected) + 1]
        # The next executable verb closes an occurrence on the same physical
        # line. A following receiver/operator does not close the candidate.
        # Declarations and control headers may end before their clause/body.
        next_boundary = (not following or following[0].value in COBOL_VERBS | SCOPE_TERMINATORS
                         | {".", "ELSE", "WHEN", "ON", "NOT", "AT", "INVALID"})
        declaration = bool(expected and expected[0].kind == "number")
        control_header = bool(expected and expected[0].value in {"IF", "WHEN", "EVALUATE"})
        if next_boundary or declaration or control_header:
            exact_statement = True
            break
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
            candidate = {**candidate, "end_line": end,
                         "occurrences": [{**item, "end_line": min(item["end_line"], end)}
                                         for item in candidate.get("occurrences", [])
                                         if item["start_line"] <= end]}
        result.append(candidate)
    return result


def _dependencies(database_path, paths, candidates, hashes, *, business_steps=False, source_scopes=()):
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
                limits = [scope for scope in source_scopes if scope["relative_path"] == path and scope.get("exclusive") is not False]
                if limits and not any(scope["start_line"] <= edge["start_line"] <= edge["end_line"] <= scope["end_line"]
                                      for scope in limits):
                    # An incoming PERFORM is relevant to this procedure, but
                    # unrelated calls elsewhere in its program are not gaps.
                    if not (edge["relation_type"].startswith("PERFORM") and
                            edge["target_name"] in {scope["procedure_name"] for scope in limits
                                                    if scope.get("range_basis") == "indexed_procedure"}):
                        continue
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


def _calculation_paths(business_map, source_pages, *, source_scopes=()):
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
        limits = [scope for scope in source_scopes if scope["relative_path"] == edge.get("caller_path") and scope.get("exclusive") is not False]
        if limits and edge.get("relation_type") == "CALLS":
            line = edge.get("caller_line")
            if type(line) is not int or not any(scope["start_line"] <= line <= scope["end_line"] for scope in limits):
                continue
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
                                candidate_cache=None, framework_facts=(), history=()):
    """Return material obligations and bounded actions for a located calculation."""
    identity = business_map.get("source_identity", {})
    paths = list(dict.fromkeys(business_map.get("selected_paths", [])))
    located = bool(business_map.get("direct_paths")) or identity.get("status") == "resolved"
    if not located:
        history_paths = {page.get("relative_path") for page in source_pages
                         if "conversation_context" in page.get("selection_reasons", [])}
        located = bool(history_paths.intersection(paths))
    planner_entities = candidate_cache.setdefault("planner_entities", {}) if isinstance(candidate_cache, dict) else {}
    def validate_entity(entity):
        if database_path is None or not paths:
            return False
        cache_key = (business_map.get("snapshot_id"), tuple(paths[:_MAX_PATHS]), entity)
        if cache_key not in planner_entities:
            encoded = quote(Path(database_path).resolve().as_posix(), safe="/:")
            with closing(sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)) as db:
                slots = ",".join("?" for _ in paths[:_MAX_PATHS])
                path_index = (" INDEXED BY idx_symbols_path" if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_symbols_path'").fetchone() else "")
                planner_entities[cache_key] = db.execute("SELECT 1 FROM symbols" + path_index + " WHERE relative_path IN (" + slots +
                    ") AND symbol_type IN ('Field','ConditionName') AND "
                    "(name=? OR substr(qualified_name,instr(qualified_name,'::')+2)=?) LIMIT 1",
                    (*paths[:_MAX_PATHS], entity, entity)).fetchone() is not None
        return planner_entities[cache_key]
    scope_key = (str(database_path), business_map.get("snapshot_id"), tuple(paths[:_MAX_PATHS]), str(question))
    if isinstance(candidate_cache, dict) and candidate_cache.get("question_scope_key") == scope_key:
        source_scopes = candidate_cache.get("question_scopes", [])
    else:
        source_scopes = _question_scopes(database_path, _calculation_roots(business_map, source_pages),
                                        str(question), history) if database_path else []
    file_key = (str(database_path), business_map.get("snapshot_id"), tuple(paths[:_MAX_PATHS]), str(question))
    if isinstance(candidate_cache, dict) and candidate_cache.get("file_question_key") == file_key:
        file_candidates, file_frontier, file_hashes, file_entities = candidate_cache["file_question_facts"]
    else:
        file_candidates, file_frontier, file_hashes, file_entities = _file_mapping_candidates(
            database_path, paths, str(question), source_scopes) if database_path and paths else ([], [], {}, [])
    plan = QuestionPlanner().plan(question, business_map, source_pages=source_pages, history=history,
                                  entity_validator=validate_entity if database_path else None,
                                  source_scopes=source_scopes, file_entities=file_entities,
                                  file_impact=is_file_impact_question(question))
    candidate_question = str(question) + (" " + " ".join(plan.entities) if plan.entities else "")
    base = {"scope": "source_candidates", "semantic_execution_verified": False,
            "analysis_plan": plan.to_dict(),
            "source_scope": {"ranges": source_scopes, "interpretation": "source_occurrences_not_runtime_execution"},
            "required_items": [], "planned_actions": [], "open_gaps": [],
            "state": "located" if located else "unresolved", "can_answer": located}
    if identity_blocks_analysis(identity):
        return {**base, "state": "unresolved", "can_answer": False}
    activation = None
    if "activation" in plan.intents:
        from activation_evidence import investigate_activation
        activation = investigate_activation(business_map, source_pages, database_path=database_path,
            max_actions=max_actions, candidate_cache=candidate_cache)
        if not set(plan.intents) & {"calculation", "origin", "overwrite", "impact", "exception"}:
            return {**base, **activation}

    def include_activation(result):
        if activation is None:
            return result
        actions = {json.dumps({"tool": action["tool"], "arguments": action["arguments"]}, sort_keys=True): action
                   for action in [*result.get("planned_actions", ()), *activation["planned_actions"]]}
        all_actions = list(actions.values())
        actions = all_actions[:max(0, max_actions)]
        gaps = [*activation["open_gaps"], *result.get("open_gaps", ())]
        if len(all_actions) > len(actions):
            gaps.append({"kind": "caller_invocation", "reason": "combined_action_frontier",
                "remaining": len(all_actions)-len(actions)})
        return {**result, "required_items": [*result.get("required_items", ()), *activation["required_items"]],
            "planned_actions": actions, "open_gaps": gaps,
            "activation_evidence": activation["activation_evidence"],
            "state": "needs_evidence" if actions else "bounded_partial" if gaps else "located",
            "can_answer": bool(result.get("can_answer") or activation["can_answer"]) and not actions}
    intent_question = str(question)
    for scope in source_scopes:
        intent_question = re.sub(r"(?<![A-Za-z0-9_$#@-])" + re.escape(scope["procedure_name"]) +
                                 r"(?![A-Za-z0-9_$#@-])", " ", intent_question, flags=re.I)
    calculation_kind = calculation_intent(intent_question)
    calculation = calculation_kind == "rules"
    file_impact = is_file_impact_question(question)
    file_mapping = bool(file_entities or file_frontier)
    file_only = bool(file_entities) and not file_impact and set(plan.intents) == {"file_mapping"}
    business_steps = not calculation and (calculation_kind == "execution" or bool(_BUSINESS_DETAIL.search(question))
        or wants_behavior_explanation(question) or file_impact or file_mapping or
        bool(set(plan.intents) & {"origin", "overwrite", "impact", "condition", "exception"}) or bool(source_scopes))
    if not calculation and not business_steps:
        supplied = [page.get("evidence_id") for page in source_pages if page.get("evidence_id")
                    and page.get("relative_path") in paths and page.get("source_text")]
        return include_activation({**base, "required_items": [{"id": "question_supplied_source", "kind": "source",
            "status": "SATISFIED" if supplied else "OPEN", "evidence_ids": supplied,
            "reason": "source_supplied" if supplied else "source_not_supplied"}],
            "can_answer": located and bool(supplied)})
    if not located:
        kind = "business_steps" if business_steps else "formula"
        item = {"id": "calculation_" + kind, "kind": kind, "status": "OPEN",
                "evidence_ids": [], "candidate_count": 0, "missing_count": 0,
                "reason": kind + "_not_located"}
        return include_activation({**base, "required_items": [item], "open_gaps": [
            {"kind": kind, "status": "OPEN", "reason": kind + "_not_located"}]})
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
        paths = _calculation_paths(business_map, source_pages, source_scopes=source_scopes)
        root_paths = _calculation_roots(business_map, source_pages)
    scope_cache = {"scope_map": business_map, "scope_history_roots": history_roots,
                   "file_question_key": file_key, "file_question_facts": (file_candidates, file_frontier, file_hashes, file_entities),
                   "question_scope_key": scope_key, "question_scopes": source_scopes,
                   "planner_entities": planner_entities,
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
               tuple(root_paths), candidate_question, tuple(business_map.get("search_terms", ())))
        cache_hit = isinstance(candidate_cache, dict) and candidate_cache.get("key") == key
        if cache_hit:
            candidates, frontier, hashes = candidate_cache["value"]
            business_steps = candidate_cache.get("business_steps", business_steps)
        else:
            if file_only:
                candidates, frontier, hashes = list(file_candidates), list(file_frontier), dict(file_hashes)
            else:
                candidates, frontier, hashes = _indexed_candidates(database_path, paths, candidate_question,
                business_steps=business_steps, root_paths=root_paths, file_impact=file_impact, source_scopes=source_scopes,
                discovery_terms=business_map.get("search_terms", ()), nondata_entities=[entity["file_name"] for entity in file_entities])
            if calculation and not file_only and not any(row["kind"] == "formula" for row in candidates):
                # Some calculations select or transfer values through branches.
                # Reuse the existing bounded step selection without asserting a
                # formula, complete value flow, or any actual runtime result.
                steps, step_frontier, step_hashes = _indexed_candidates(database_path, paths, candidate_question,
                    business_steps=True, root_paths=root_paths, file_impact=file_impact, source_scopes=source_scopes,
                    discovery_terms=business_map.get("search_terms", ()), nondata_entities=[entity["file_name"] for entity in file_entities])
                if any(row["kind"] == "business_steps" for row in steps):
                    business_steps = True
                    candidates, frontier, hashes = steps, step_frontier, step_hashes
            if file_impact:
                impact_candidates, impact_frontier, impact_hashes = nominate_file_impact_evidence(database_path, paths)
                candidates.extend(impact_candidates)
                frontier.extend(impact_frontier)
                hashes.update(impact_hashes)
            if file_mapping and not file_only:
                candidates.extend(file_candidates)
                frontier.extend(file_frontier)
                hashes.update(file_hashes)
            dependencies, dependency_frontier = ([], []) if file_only else _dependencies(database_path, list(hashes), candidates, hashes,
                                                              business_steps=business_steps, source_scopes=source_scopes)
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
    checked_scopes, scope_gaps = _check_occurrence_scopes(source_scopes, pages)
    base["source_scope"]["ranges"] = checked_scopes
    base["analysis_plan"]["source_scopes"] = tuple(checked_scopes)
    frontier = [*frontier, *scope_gaps]
    candidates = [*candidates, *_visible_input_declarations(pages, candidates)]
    candidates = _condition_spans(candidates, evidence_groups)
    unique = {}
    for row in candidates:
        if row.get("start_line"):
            unique[(row["kind"], row["relative_path"], row["start_line"], row["statement"])] = row
    candidates = list(unique.values())
    candidates = EvidencePlanner().organize(plan, candidates)
    candidates = _framework_dependencies(candidates, pages, framework_facts)
    missing = []
    items = []
    kinds = ["procedure_body", "formula", "declarations", "inputs", "conditions", "result_adjustments", "dependencies"]
    if business_steps:
        kinds.insert(0, "business_steps")
    if file_impact:
        kinds.extend(("file_io", "file_definitions", "dds_definitions"))
    if file_mapping:
        kinds.extend(kind for kind in ("file_definitions", "file_layout", "copy_layout", "file_io") if kind not in kinds)
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
        primary = ("file_definitions" if file_only else "procedure_body" if any(row["kind"] == "procedure_body" for row in candidates)
                   and not any(row["kind"] in {"business_steps", "formula"} for row in candidates)
                   else "business_steps" if business_steps else "formula")
        required = kind == primary
        status = ("UNRESOLVED" if unresolved and not absent else "PARTIAL" if absent and supplied
                  else "OPEN" if absent or required and not related else
                  "SATISFIED" if related else "NOT_APPLICABLE")
        covered = [row for row in related if row.get("framework_fact_ids")]
        items.append({"id": "calculation_" + kind, "kind": kind, "status": status,
            "evidence_ids": list(dict.fromkeys(supplied)), "candidate_count": len(related),
            "missing_count": len(absent), "reason": ("runtime_target_unresolved" if
                any(row.get("unresolved_reason") == "runtime_target_unresolved" for row in unresolved)
                else "compile_condition_unknown" if any(row.get("unresolved_reason") == "compile_condition_unknown" for row in unresolved)
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
    flow_origins, invalidated_origins, origin_witnesses = _flow_input_origins(candidates, pages, evidence_groups)
    input_origins = (input_origins - invalidated_origins) | flow_origins
    # The field-name graph is an overapproximation without occurrence order.
    # Replace only its cycle warning for reads whose stronger source-flow
    # witnesses are closed for every supplied version/context/occurrence.
    frontier = [gap for gap in frontier if not (gap.get("reason") == "input_dependency_cycle"
        and (gap.get("relative_path"), gap.get("field")) in flow_origins
        and (gap.get("relative_path"), gap.get("field")) not in invalidated_origins)]
    if origin_witnesses:
        item = next(item for item in items if item["kind"] == "inputs")
        item["source_flow_origins"] = origin_witnesses
        if flow_origins:
            item["evidence_ids"] = list(dict.fromkeys([*item["evidence_ids"], *(identifier
                for witness in origin_witnesses if witness["status"] == "source_origin_supplied"
                for identifier in witness["evidence_ids"])]))
            if item["status"] == "NOT_APPLICABLE":
                item.update(status="SATISFIED", reason="source_flow_origins_supplied")
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
    else:
        # Obtain the actual business effect before supplementary origins and
        # declarations consume a small per-turn allowance. PIC remains a
        # required item and is read next, once the statement is supplied.
        missing = EvidencePlanner().organize(plan, missing)
    grouped_missing = []
    for row in missing:
        if (row["kind"] == "declarations" and grouped_missing and grouped_missing[-1]["kind"] == "declarations"
                and row["relative_path"] == grouped_missing[-1]["relative_path"]
                and row["start_line"] <= grouped_missing[-1]["end_line"] + 1):
            grouped_missing[-1] = {**grouped_missing[-1], "end_line": max(row["end_line"], grouped_missing[-1]["end_line"])}
        else:
            grouped_missing.append(row)
    for row in grouped_missing:
        key = (row["relative_path"], row["start_line"])
        scope_read = next((scope for scope in source_scopes
            if scope.get("exclusive") is not False and scope.get("range_basis") == "first_if_occurrences"
            and scope["relative_path"] == row["relative_path"] and row["kind"] in
                {"formula", "business_steps", "conditions", "result_adjustments"}
            and scope["start_line"] <= row["start_line"] <= row["end_line"] <= scope["end_line"]
            and scope["end_line"] - scope["start_line"] <= 96), None)
        if scope_read:
            # The exact occurrence window supplies neighboring formulas and
            # their guards together. Separate semantic inspections of each
            # line can spend all reads on the same window and starve PIC.
            key = (row["relative_path"], scope_read["start_line"])
        if key in planned:
            continue
        planned.add(key)
        inspect = {"relative_path": key[0], "line": key[1],
                   "fields": list(dict.fromkeys([*row["writes"], *row["reads"], *row.get("input_fields", [])]))[:_MAX_FIELDS]}
        last = row["end_line"] if row.get("occurrence_count", 1) == 1 else key[1] + 8
        read = {"relative_path": key[0], "start_line": max(1, key[1] - 4),
                "end_line": min(max(key[1] + 8, last), key[1] + 96)}
        file_source = row["kind"] in {"procedure_body", "declarations", "file_io", "file_definitions", "file_layout", "copy_layout", "dds_definitions"}
        if file_source:
            read = {"relative_path": key[0], "start_line": key[1], "end_line": last}
        if scope_read:
            read = {"relative_path": key[0], "start_line": scope_read["start_line"], "end_line": scope_read["end_line"]}
        tool = "read" if scope_read or file_source or ("inspect_business_context", *key) in seen else "inspect_business_context"
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
        for declaration in candidates:
            if declaration["kind"] == "declarations" and _visible_ids(declaration, pages):
                declared.update((declaration["relative_path"], field) for field in declaration.get("layout_fields", []))
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
            if (file_only and gap.get("relation_type") in {"CALLS", "CALL_TARGET_FROM", "PERFORMS", "PERFORMS_THRU"}
                    and all(item["status"] in {"SATISFIED", "NOT_APPLICABLE"} for item in items
                            if item["kind"] in {"file_definitions", "file_layout", "copy_layout", "file_io"})
                    and all(entity["binding_status"] == "bound" for entity in file_entities)):
                base.setdefault("scope_exclusions", []).append({"frontier": dict(gap),
                    "basis": "supplied_static_file_mapping_does_not_establish_runtime_effects_or_external_identity"})
                continue
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
    formula_supplied = any(item["kind"] in {"formula", "business_steps", "procedure_body"} and item["evidence_ids"] for item in items)
    boundary_supplied = bool(external and external["evidence_ids"])
    state = "needs_evidence" if plans else "bounded_partial" if gaps else "located"
    file_observations = {}
    if file_impact or file_mapping:
        from file_impact import collect_file_impact
        file_observations = {"file_impact": collect_file_impact(pages)}
    known_file_supplied = (any(item["kind"] in {"file_definitions", "file_layout", "copy_layout", "file_io"}
        and item["evidence_ids"] for item in items) if file_only else any(row["kind"] in
        {"io_operation", "field_assignment", "file_declaration", "record_binding"}
        for row in file_observations.get("file_impact", {}).get("observations", [])))
    return include_activation({**base, **file_observations, "required_items": items, "planned_actions": plans, "open_gaps": gaps[:16],
            "state": state, "can_answer": (formula_supplied or boundary_supplied or known_file_supplied) and not plans,
            "candidate_paths": paths[:_MAX_PATHS],
            "candidate_cache": {"hit": cache_hit, "candidate_count": len(candidates)}})
