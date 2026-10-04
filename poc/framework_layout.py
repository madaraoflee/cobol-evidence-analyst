"""Check the local storage evidence behind a documented interface binding.

This is a narrow caller-layout check, not verification of a generated callee's
ABI. Unknown storage never upgrades a candidate. Direct data declarations and
one plain, resolved COPY level are supported; other layouts remain explicit.
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
    return {"relative_path": unit["relative_path"], "source_sha256": unit.get("source_sha256"),
            "start_line": unit["start_line"], "end_line": unit["end_line"],
            "evidence_id": unit["evidence_id"], "role": role}


def _preprocessing(db, path):
    # Directives can precede PROGRAM-ID or be inherited through either data or
    # procedure COPY. Their lifetime is not constrained to a COBOL program.
    return _rows(db, "SELECT u.*,e.source_sha256 FROM code_units u LEFT JOIN evidence_spans e "
                 "ON e.evidence_id=u.evidence_id WHERE u.relative_path=? "
                 "AND u.unit_type='PreprocessorDirective' ORDER BY u.start_line", (path,))


def _context(db, path, program):
    rows = _units(db, path, program)
    units = {item["unit_id"]: item for item in rows}
    events = [(item["start_line"], 0, item, _storage(item, units), []) for item in rows if item["unit_type"] == "DataItem"]
    issues = []
    directives = _preprocessing(db, path)
    if directives:
        issues.append(("framework_preprocessing_not_resolved", directives))
    for relation in _rows(db, "SELECT * FROM relations WHERE relative_path=? AND relation_type='INCLUDES_COPY'", (path,)):
        candidates = _rows(db, "SELECT u.*,e.source_sha256 FROM code_units u LEFT JOIN evidence_spans e "
                           "ON e.evidence_id=u.evidence_id WHERE u.unit_id=?", (relation["from_entity_id"],))
        if len(candidates) != 1:
            continue
        inclusion = candidates[0]
        storage = _storage(inclusion, units) if inclusion.get("program_name") == program else None
        try:
            metadata = json.loads(relation["metadata_json"])
        except (TypeError, ValueError):
            metadata = {"boundary": "invalid_metadata"}
        if metadata.get("boundary") or inclusion.get("parse_status") != "complete" or not _COPY.fullmatch(inclusion["normalized_text"].strip()):
            issues.append(("framework_layout_copy_form_not_supported", [inclusion]))
            continue
        if relation["status"] != "confirmed" or not relation["target_entity_id"]:
            issues.append(("framework_layout_copy_unresolved", [inclusion]))
            continue
        targets = _rows(db, "SELECT relative_path FROM symbols WHERE symbol_id=? AND symbol_type='Copybook'", (relation["target_entity_id"],))
        if len(targets) != 1:
            issues.append(("framework_layout_copy_unresolved", [inclusion]))
            continue
        copy_path = targets[0]["relative_path"]
        directives = _preprocessing(db, copy_path)
        if directives:
            issues.append(("framework_copy_preprocessing_not_resolved", [inclusion] + directives))
            continue
        if db.execute("SELECT 1 FROM relations WHERE relative_path=? AND relation_type='INCLUDES_COPY' LIMIT 1", (copy_path,)).fetchone():
            issues.append(("framework_layout_nested_copy_not_supported", [inclusion]))
            continue
        # Procedure COPY can change subsequent token interpretation, so it is
        # checked above even though it supplies no parameter declarations.
        if storage is None:
            continue
        copied = [item for item in _units(db, copy_path) if item["unit_type"] == "DataItem"]
        if not copied:
            issues.append(("framework_layout_copy_unresolved", [inclusion]))
            continue
        events.extend((inclusion["start_line"], index + 1, item, storage, [inclusion]) for index, item in enumerate(copied))
    if len(events) > _MAX_LAYOUT_ITEMS:
        return [], [("framework_layout_item_limit", [])]
    nodes, stack, current_storage = [], [], None
    for _, _, unit, storage, inclusions in sorted(events, key=lambda item: (item[0], item[1])):
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


def validate_framework_layouts(db, facts: list[dict]) -> list[dict]:
    """Return copies of facts; withdraw coverage when caller storage is unknown."""
    contexts = {}
    result = []
    for original in facts:
        fact = deepcopy(original)
        result.append(fact)
        if fact.get("kind") != "framework_operation" or fact.get("target_source_available") or fact.get("reason") == "source_implementation_takes_precedence":
            continue
        key = fact.get("relative_path"), fact.get("program_name")
        if key not in contexts:
            contexts[key] = _context(db, *key)
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
