"""Check the local storage evidence behind a documented interface binding.

This is a narrow caller-layout check, not verification of a generated callee's
ABI. Unknown storage never upgrades a candidate. Direct data declarations and
resolved COPY nesting and single-token replacement are supported; other layouts remain explicit.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import json
import re


_NAME = r"[A-Z][A-Z0-9-]*"
_DECL = re.compile(rf"^(\d{{1,2}})\s+({_NAME})\b(.*)$", re.I | re.S)
_COPY = re.compile(r"COPY\s+(?:'[A-Z0-9_.-]+'|\"[A-Z0-9_.-]+\"|[A-Z0-9_-]+)\s*\.", re.I)
_PIC = re.compile(r"(?:PIC|PICTURE)\s+(?:IS\s+)?(X\s*\(\s*(\d{1,6})\s*\)|X+)(?=\s|$)(.*)", re.I | re.S)
_TAIL = re.compile(r"\s*(?:(?:USAGE\s+(?:IS\s+)?)?DISPLAY\s*)?(?:VALUE\s+(?:IS\s+)?(?:'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|SPACES?)\s*)?", re.I | re.S)
_STORAGE = {"WORKING-STORAGE", "LOCAL-STORAGE", "LINKAGE"}
_MAX_LAYOUT_ITEMS = 16_384


def _rows(db, sql, args=()):
    cursor = db.execute(sql, args)
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _units(db, path, program=None):
    sql = ("SELECT u.*,e.source_sha256 FROM code_units u "
           "LEFT JOIN evidence_spans e ON e.evidence_id=u.evidence_id WHERE u.relative_path=? "
           "AND u.unit_type IN ('DataItem','Section','Program','Copybook')")
    args = [path]
    if program is not None:
        sql += " AND u.program_name=?"
        args.append(program)
    return _rows(db, sql + " ORDER BY u.start_line,u.end_line,u.unit_id", args)


def _storage(unit, units):
    current, visited = unit, set()
    while current and current["unit_id"] not in visited:
        visited.add(current["unit_id"])
        if current["unit_type"] == "Section":
            return current["name"].upper() if current["name"].upper() in _STORAGE else None
        current = units.get(current.get("parent_unit_id"))
    return None


def _reference(unit, role):
    result = {"relative_path": unit["relative_path"], "source_sha256": unit.get("source_sha256"),
            "start_line": unit["start_line"], "end_line": unit["end_line"],
            "evidence_id": unit["evidence_id"], "role": role}
    if unit.get("layout_replacement_trace"):
        result["replacement_trace"] = deepcopy(unit["layout_replacement_trace"])
    return result


def _preprocessing(db, path):
    # Directives can precede PROGRAM-ID or be inherited through either data or
    # procedure COPY. Their lifetime is not constrained to a COBOL program.
    return _rows(db, "SELECT u.*,e.source_sha256 FROM code_units u LEFT JOIN evidence_spans e "
                 "ON e.evidence_id=u.evidence_id WHERE u.relative_path=? "
                 "AND u.unit_type='PreprocessorDirective' ORDER BY u.start_line", (path,))


def _copy_spec(text):
    """Use the shared COPY grammar, including indexed explicit member paths."""
    from procedure_expansion import _parse_copy
    quoted = re.match(r"\s*COPY\s+(['\"])([^'\"]+)\1", text, re.I)
    if quoted and "/" in quoted[2]:
        name = quoted[2]
        if name.startswith("/") or ".." in name.split("/") or "\\" in name:
            return None
        parsed = _parse_copy(text[:quoted.start(2)] + "LAYOUT-MEMBER" + text[quoted.end(2):])
        return (name.upper(), parsed[1], parsed[2]) if parsed else None
    return _parse_copy(text)


def _copy_target_paths(db, relation, parsed, catalog):
    if relation["status"] == "confirmed" and relation["target_entity_id"]:
        return [item["relative_path"] for item in _rows(db,
            "SELECT relative_path FROM symbols WHERE symbol_id=? AND symbol_type='Copybook'",
            (relation["target_entity_id"],))]
    if parsed is None or parsed[1] or not parsed[2]:
        return []
    try:
        metadata = json.loads(relation["metadata_json"])
    except (TypeError, ValueError):
        return []
    if (metadata.get("boundary") != "copy_replacing_or_library_not_expanded"
            or metadata.get("compilation_status") == "candidate"):
        return []
    if not catalog:
        from pathlib import PurePosixPath
        for item in _rows(db, "SELECT relative_path FROM symbols WHERE symbol_type='Copybook'"):
            member = PurePosixPath(item["relative_path"])
            for alias in {member.name.upper(), member.stem.upper(), member.as_posix().upper()}:
                catalog.setdefault(alias, set()).add(item["relative_path"])
    return sorted(catalog.get(parsed[0], ()))


def _context(db, path, program, *, replacement_audit=None):
    from procedure_expansion import _replace_tokens
    from statement_facts import CobolLexError, lex_cobol

    rows = _units(db, path, program)
    units = {item["unit_id"]: item for item in rows}
    events, issues, catalog, cached = [], [], {}, {}
    inclusion_count = 0

    def contents(relative):
        if relative not in cached:
            declarations = _units(db, relative)
            edges = _rows(db, "SELECT * FROM relations WHERE relative_path=? AND relation_type='INCLUDES_COPY'", (relative,))
            actions = [(item["start_line"], "declaration", item) for item in declarations if item["unit_type"] == "DataItem"]
            for relation in edges:
                found = _rows(db, "SELECT u.*,e.source_sha256 FROM code_units u LEFT JOIN evidence_spans e "
                    "ON e.evidence_id=u.evidence_id WHERE u.unit_id=?", (relation["from_entity_id"],))
                if len(found) != 1:
                    actions.append((0, "missing", relation))
                else:
                    actions.append((found[0]["start_line"], "include", (relation, found[0])))
            cached[relative] = (sorted(actions, key=lambda action: action[0]), _preprocessing(db, relative), declarations)
        return cached[relative]

    # Stack traversal keeps lexical inclusion order, including subordinate
    # members inserted between a containing group and later siblings.
    root_actions = [(item["start_line"], "declaration", item) for item in rows if item["unit_type"] == "DataItem"]
    root_actions += [item for item in contents(path)[0] if item[1] != "declaration"]
    work = [(action, None, [], (), (path,)) for action in reversed(sorted(root_actions, key=lambda item: item[0]))]
    directives = contents(path)[1]
    if directives:
        issues.append(("framework_preprocessing_not_resolved", directives))
    while work:
        (line, kind, value), inherited_storage, chain, replacements, ancestors = work.pop()
        if kind == "missing":
            issues.append(("framework_layout_copy_unresolved", list(chain)))
            continue
        if kind == "declaration":
            unit = value
            storage = inherited_storage if chain else _storage(unit, units)
            if storage is None:
                continue
            text = unit["normalized_text"]
            traces = []
            for pairs, site in replacements:
                text, changes, issue = _replace_tokens(text, pairs, site)
                traces.extend(changes)
                if issue:
                    issues.append(("framework_layout_copy_form_not_supported", [unit, *chain]))
            if traces:
                unit = {**unit, "normalized_text": text, "layout_replacement_trace": traces}
            events.append((unit, storage, list(chain)))
            if len(events) > _MAX_LAYOUT_ITEMS:
                return [], [("framework_layout_item_limit", list(chain))]
            continue
        relation, inclusion = value
        inclusion_count += 1
        if inclusion_count > _MAX_LAYOUT_ITEMS:
            return [], [("framework_layout_item_limit", [inclusion, *chain])]
        storage = inherited_storage if chain else (_storage(inclusion, units) if inclusion.get("program_name") == program else None)
        try:
            metadata = json.loads(relation["metadata_json"])
        except (TypeError, ValueError):
            metadata = {"boundary": "invalid_metadata"}
        parsed = _copy_spec(inclusion["normalized_text"].strip())
        if (inclusion.get("parse_status") != "complete" or parsed is None or parsed[1]
                or metadata.get("compilation_status") == "candidate"
                or metadata.get("boundary") not in {None, "copy_replacing_or_library_not_expanded"}):
            issues.append(("framework_layout_copy_form_not_supported", [inclusion, *chain]))
            continue
        replacing = parsed[2]
        try:
            simple = all(len(lex_cobol(before)) == 1 and len(lex_cobol(after)) == 1 for before, after in replacing)
        except CobolLexError:
            simple = False
        # Match the shared preprocessor's conservative nested replacement
        # boundary. Plain nesting and a replacement on the leaf are determinate.
        if not simple or replacements:
            issues.append(("framework_layout_copy_form_not_supported", [inclusion, *chain]))
            continue
        targets = _copy_target_paths(db, relation, parsed, catalog)
        if len(targets) != 1:
            issues.append(("framework_layout_copy_unresolved", [inclusion, *chain]))
            continue
        target = targets[0]
        if target in ancestors:
            issues.append(("framework_layout_copy_cycle", [inclusion, *chain]))
            continue
        if len(ancestors) >= 65:
            issues.append(("framework_layout_copy_depth_limit", [inclusion, *chain]))
            continue
        if replacing:
            issue = (replacement_audit(target, replacing) if replacement_audit else
                     "framework_replacement_source_not_fully_checked")
            if issue:
                issues.append((issue, [inclusion, *chain]))
                continue
        actions, directives, declarations = contents(target)
        if directives:
            issues.append(("framework_copy_preprocessing_not_resolved", [*chain, inclusion, *directives]))
            continue
        if any(unit["unit_type"] == "Program" for unit in declarations):
            issues.append(("framework_layout_copy_form_not_supported", [inclusion, *chain]))
            continue
        if storage is not None and not actions:
            issues.append(("framework_layout_copy_unresolved", [inclusion, *chain]))
            continue
        next_chain = [*chain, inclusion]
        site = {"relative_path": inclusion["relative_path"], "source_hash": inclusion.get("source_sha256"),
                "line": inclusion["start_line"], "end_line": inclusion["end_line"], "copy_name": parsed[0]}
        next_replacements = (*replacements, (replacing, site)) if replacing else replacements
        work.extend((action, storage, next_chain, next_replacements, (*ancestors, target)) for action in reversed(actions))
    nodes, stack, current_storage = [], [], None
    for unit, storage, inclusions in events:
        if storage != current_storage:
            stack = []
            current_storage = storage
        match = _DECL.fullmatch(unit["normalized_text"].strip())
        if match is None:
            issues.append(("framework_layout_declaration_not_supported", [unit]))
            stack = []
            continue
        level, name, tail = int(match.group(1)), match.group(2).upper(), match.group(3).strip().rstrip(".").strip()
        if level in {66, 77}:
            stack = []
        while stack and stack[-1]["level"] >= level:
            stack.pop()
        node = {"unit": unit, "name": name, "level": level, "tail": tail,
                "ancestors": list(stack), "storage": storage, "inclusions": inclusions}
        nodes.append(node)
        if level != 88:
            stack.append(node)
    return nodes, issues


def _check(nodes, issues, fact):
    names = defaultdict(list)
    for node in nodes:
        names[node["name"]].append(node)
    function_rows = names[fact.get("function_field")]
    argument_rows = names[fact.get("argument")]
    relevant = function_rows + argument_rows
    evidence = [(node["unit"], "function_declaration" if node in function_rows else "argument_declaration") for node in relevant]
    if len(function_rows) > 1 or len(argument_rows) > 1:
        return "framework_layout_field_ambiguous", None, evidence
    if not function_rows or not argument_rows:
        if issues:
            return issues[0][0], None, evidence + [(unit, "layout_boundary") for unit in issues[0][1]]
        return "framework_layout_declaration_missing", None, evidence
    function, argument = function_rows[0], argument_rows[0]
    ancestors = function["ancestors"]
    relevant += ancestors + argument["ancestors"]
    evidence.extend((node["unit"], "layout_ancestor") for node in relevant if node not in function_rows + argument_rows)
    evidence.extend((unit, "copy_inclusion") for node in relevant for unit in node["inclusions"])
    if any(not node["storage"] or node["unit"].get("parse_status") != "complete" or not node["unit"].get("source_sha256") for node in relevant):
        return "framework_layout_declaration_incomplete", None, evidence
    if argument not in ancestors:
        return "framework_function_outside_argument", None, evidence
    group = [node for node in nodes if node is argument or argument in node["ancestors"]]
    unsafe = [node for node in group + argument["ancestors"] if re.search(r"\b(?:REDEFINES|RENAMES|OCCURS)\b", node["tail"], re.I)]
    relevant_names = {node["name"] for node in relevant}
    unsafe.extend(node for node in nodes if (match := re.search(rf"\bREDEFINES\s+({_NAME})\b", node["tail"], re.I))
                  and match.group(1).upper() in relevant_names and node not in unsafe)
    if unsafe:
        evidence.extend((node["unit"], "layout_boundary") for node in unsafe)
        return "framework_layout_alias_or_occurs_not_supported", None, evidence
    if any(node["level"] not in range(1, 50) or node["tail"] for node in [argument] + ancestors):
        return "framework_argument_group_not_supported", None, evidence
    match = _PIC.fullmatch(function["tail"])
    if not match or function["level"] not in range(2, 50) or not _TAIL.fullmatch(match.group(3)):
        return "framework_function_storage_not_supported", None, evidence
    length = int(match.group(2)) if match.group(2) else len(match.group(1))
    if not 1 <= length <= 65_536:
        return "framework_function_storage_not_supported", None, evidence
    value = fact.get("observed_function_value")
    if value is None and fact.get("operation"):
        value = fact["operation"].get("value")
    if value is not None and (not isinstance(value, str) or not value.isascii()):
        return "framework_function_literal_not_supported", length, evidence
    if value is not None and len(value) > length:
        return "framework_function_literal_truncated", length, evidence
    if issues:
        return issues[0][0], length, evidence + [(unit, "layout_boundary") for unit in issues[0][1]]
    return None, length, evidence


def validate_framework_layouts(db, facts: list[dict], *, replacement_audit=None,
                               contexts=None) -> list[dict]:
    """Check every fact, optionally sharing layouts within this unchanged DB read."""
    contexts = {} if contexts is None else contexts
    result = []
    for original in facts:
        fact = deepcopy(original)
        result.append(fact)
        if fact.get("kind") != "framework_operation" or fact.get("target_source_available") or fact.get("reason") == "source_implementation_takes_precedence":
            continue
        key = fact.get("relative_path"), fact.get("program_name")
        if key not in contexts:
            contexts[key] = _context(db, *key, replacement_audit=replacement_audit)
        reason, length, evidence = _check(*contexts[key], fact)
        refs = []
        for unit, role in evidence:
            ref = _reference(unit, role)
            if ref not in refs:
                refs.append(ref)
        # The binder may retain a function literal across separate elementary
        # assignments. Those declarations must also reach the model before the
        # call can be covered; adding them here preserves the source-read plan.
        for ref in fact.get("binding_evidence_refs", []):
            if ref not in refs:
                refs.append(deepcopy(ref))
        if any(ref["relative_path"] == fact.get("relative_path") and ref["source_sha256"] != fact.get("source_sha256") for ref in refs):
            reason = "framework_layout_source_mismatch"
        fact["layout_validation"] = {"status": "unresolved" if reason else "confirmed",
                                     "reason": reason or "local_function_storage_checked",
                                     "function_length": length, "scope": "local_parameter_storage_only",
                                     "callee_abi_verified": False}
        fact["layout_evidence_refs"] = refs
        spans = fact.setdefault("source_ranges", [])
        for ref in refs:
            if ref not in spans:
                spans.append(ref)
        if reason:
            previously_covered = fact.get("dependency_covered", False)
            fact["dependency_covered"] = False
            if previously_covered or fact.get("reason") == "documented_operation_bound":
                fact["reason"] = reason
                if fact.get("operation"):
                    fact["documented_operation_candidate"] = fact.pop("operation")
                    fact["operation"] = None
    return result
