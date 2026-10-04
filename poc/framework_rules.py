"""Compile documented framework conventions without executing manual content.

The output describes versioned conventions and explicit interface templates.
It does not infer a runtime contract or establish that a source call occurred.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import re

from framework_knowledge import MAX_SECTION_CHARS, _ReferenceError, _load_document, _summary


SCHEMA_VERSION = "framework-rules/v1"
COMPILER_VERSION = "documented-conventions-v1.1"
_IDENTIFIER = r"[A-Z0-9_$#@-]+"
_CALL = re.compile(rf"\bCALL\s+(?:[\"']({_IDENTIFIER})[\"']|({_IDENTIFIER}))\s+USING\s+({_IDENTIFIER})([^\n]*)", re.I)
_FUNCTION = re.compile(r"\b[A-Z0-9_$#@-]*X{2,8}[A-Z0-9_$#@-]*-FUNCTION\b")
_PLACEHOLDER = re.compile(r"(?<!X)X{2,8}(?!X)")
_STAGE = re.compile(r"[0-9]{2,8}-[A-Z][A-Z0-9_$#@-]*\Z")
_STAGE_TOKEN = re.compile(r"(?<![A-Z0-9_$#@-])[0-9]{2,8}-[A-Z][A-Z0-9_$#@-]*(?![A-Z0-9_$#@-])")
_VERSION = re.compile(r"\b[RV]\d+(?:\.\d+)*(?:[-+][A-Za-z0-9.]+)?\b", re.I)
_HEADERS = {
    "function": "operation", "function code": "operation", "operation": "operation",
    "operation code": "operation", "功能码": "operation", "操作": "operation",
    "status": "status", "status code": "status", "return status": "status",
    "状态": "status", "返回状态": "status",
    "section": "section", "section name": "section", "节": "section",
}


def _id(prefix, *values):
    content = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return prefix + hashlib.sha256(content.encode()).hexdigest()[:24]


def _plain(value):
    value = value.strip()
    value = re.sub(r"\\([\\`*_{}\[\]()#+.!|/-])", r"\1", value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "`\"'":
        value = value[1:-1].strip()
    if value.startswith("**") and value.endswith("**") and len(value) > 4:
        value = value[2:-2].strip()
    return value


def _cells(line):
    return [_plain(cell) for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _reference(section, document):
    digest = section.document_sha256 or document.sha256
    name_key = hashlib.sha256(section.document_name.encode()).hexdigest()[:8] + ":" if section.document_name else ""
    return {"reference_id": f"fw:{digest[:16]}:{name_key}{section.start_line}-{section.end_line}",
            "heading": section.heading, "page": section.page,
            "start_line": section.start_line, "end_line": section.end_line, "text": section.text,
            "document_name": section.document_name or document.documents[0]["name"],
            "document_sha256": digest, "matched_terms": [], "selection_reason": "compiled_rule"}


def _in_scope(heading, scope):
    return heading == scope or heading.startswith(scope + " / ")


def _template(value):
    matches = list(_PLACEHOLDER.finditer(value))
    return matches[0].group() if len(matches) == 1 and len(value) > len(matches[0].group()) else None


def _family_matches(target, section, call_section):
    if _in_scope(section.heading, call_section.heading) or _in_scope(call_section.heading, section.heading):
        return True
    # A documented family label may connect an interface diagram to its later
    # operation chapter. Require an explicit fixed target suffix in that text.
    suffix = _PLACEHOLDER.sub(" ", target).strip(" -_")
    if not re.fullmatch(r"[A-Z]{2,}", suffix):
        return False
    text = re.sub(r"(?<=[A-Za-z])/(?=[A-Za-z])", "", section.heading + "\n" + section.text).upper()
    return bool(re.search(r"(?<![A-Z0-9_-])" + re.escape(suffix) + r"(?![A-Z0-9_-])", text))


def compile_framework_rules(reference_path=None) -> dict:
    """Return source-citable conventions from the current local manual version.

    Only explicit table rows become rules. Interface templates require a CALL
    example, one argument, and one matching function-field declaration scope.
    No model, source repository scan, executable configuration or network runs.
    """
    try:
        document = _load_document(reference_path)
    except _ReferenceError as error:
        summary = _summary(None, code=error.code)
        return {**summary, "schema_version": SCHEMA_VERSION, "compiler_version": COMPILER_VERSION,
                "rules": [], "interfaces": [], "references": [],
                "boundaries": [{"reason": "framework_reference_unavailable", "reason_code": error.code}]}
    summary = _summary(document)
    result = {**summary, "schema_version": SCHEMA_VERSION, "compiler_version": COMPILER_VERSION,
              "rules": [], "interfaces": [], "references": [], "boundaries": []}
    if document is None:
        return result
    versions = {row["name"]: next(iter(_VERSION.findall(row["title"])), None) for row in document.documents}
    result["document"] = {**result["document"], "target_version": next(iter(_VERSION.findall(document.title)), None)}
    references = {}
    sections = []
    rules_by_key = defaultdict(list)
    stage_sources = {}
    for section in document.sections:
        digest = section.document_sha256 or document.sha256
        name = section.document_name or document.documents[0]["name"]
        for symbol in _STAGE_TOKEN.findall(section.text):
            stage_sources.setdefault((name, digest, section.heading, symbol), section)

    def retain(section):
        reference = _reference(section, document)
        references[reference["reference_id"]] = reference
        return reference

    for section in document.sections:
        digest = section.document_sha256 or document.sha256
        name = section.document_name or document.documents[0]["name"]
        sections.append((section, digest, name))
        rows = [line for line in section.text.splitlines() if line.strip().startswith("|")]
        if len(rows) < 2:
            continue
        header = _cells(rows[0])
        kind = _HEADERS.get(header[0].casefold()) if header else None
        if kind is None or len(header) < 2:
            continue
        for line in rows[1:]:
            if len(line) >= MAX_SECTION_CHARS:
                result["boundaries"].append({"reason": "framework_rule_text_truncated",
                    "reference_ids": [retain(section)["reference_id"]]})
                continue
            cells = _cells(line)
            if len(cells) != len(header) or not cells[1] or re.fullmatch(r"[\s:|-]+", line):
                continue
            raw_symbol = cells[0]
            symbols = [_plain(value) for value in re.split(r"\s+/\s+", raw_symbol)]
            if kind == "section":
                if raw_symbol.isdigit():
                    symbols = sorted(term for term in section.terms if _STAGE.fullmatch(term)
                                     and term.split("-", 1)[0] == raw_symbol)
                    if len(symbols) != 1:
                        result["boundaries"].append({"reason": "section_name_not_resolved",
                            "symbol": raw_symbol, "reference_ids": [retain(section)["reference_id"]]})
                        continue
                elif not _STAGE.fullmatch(raw_symbol):
                    # A complete named section is usable without a numeric prefix.
                    if not re.fullmatch(r"[A-Z][A-Z0-9_$#@-]+", raw_symbol):
                        continue
            for symbol in symbols:
                if not re.fullmatch(r"[A-Z0-9_$#@*-]{1,128}", symbol) or not symbol.strip("-"):
                    continue
                reference = retain(section)
                reference_ids = [reference["reference_id"]]
                if kind == "section" and raw_symbol.isdigit():
                    origin = stage_sources.get((name, digest, section.heading, symbol))
                    if origin is None:
                        result["boundaries"].append({"reason": "section_name_origin_unavailable",
                            "symbol": raw_symbol, "reference_ids": reference_ids})
                        continue
                    reference_ids.append(retain(origin)["reference_id"])
                meaning, caveat = cells[1], " | ".join(cells[2:])
                rule = {"rule_id": _id("fwr:", COMPILER_VERSION, digest, name, section.heading, kind, symbol, meaning, caveat),
                        "kind": kind, "symbol": symbol, "meaning": meaning, "caveat": caveat,
                        "scope": section.heading, "document_sha256": digest, "document_name": name,
                        "target_version": versions.get(name), "reference_ids": sorted(set(reference_ids)),
                        "semantic_basis": "documented_convention", "runtime_verified": False}
                rules_by_key[(name, digest, section.heading, kind, symbol)].append(rule)
    for key, candidates in rules_by_key.items():
        variants = {(row["meaning"], row["caveat"]) for row in candidates}
        if len(variants) > 1:
            result["boundaries"].append({"reason": "conflicting_documented_rule", "kind": key[3], "symbol": key[4],
                "reference_ids": sorted({ref for row in candidates for ref in row["reference_ids"]})})
            continue
        chosen = candidates[0]
        chosen["reference_ids"] = sorted({ref for row in candidates for ref in row["reference_ids"]})
        result["rules"].append(chosen)

    functions = []
    calls = []
    for section, digest, name in sections:
        for function in sorted(set(_FUNCTION.findall(section.text))):
            functions.append((function, section, digest, name))
        for match in _CALL.finditer(section.text):
            target, argument = (match[1] or match[2]).upper(), match[3].upper()
            if not _template(target) or _template(target) != _template(argument):
                continue
            # Reject a second argument or unsupported trailing clause. A diagram
            # arrow or sentence punctuation can follow the one explicit argument.
            tail = match[4].strip()
            if tail and not re.fullmatch(r"[.`,; ]*", tail):
                result["boundaries"].append({"reason": "interface_call_form_unsupported",
                    "reference_ids": [retain(section)["reference_id"]]})
                continue
            calls.append((target, argument, section, digest, name))
    for target, argument, call_section, digest, name in calls:
        candidates = [(function, section) for function, section, function_digest, function_name in functions
                      if function_digest == digest and function_name == name
                      and _template(function) == _template(target)
                      and _family_matches(target, section, call_section)]
        grouped = defaultdict(list)
        for function, section in candidates:
            grouped[(function, section.heading)].append(section)
        if len(grouped) != 1:
            result["boundaries"].append({"reason": "interface_function_scope_ambiguous" if grouped else "interface_function_not_resolved",
                "reference_ids": [retain(call_section)["reference_id"]]})
            continue
        (function, scope), function_sections = next(iter(grouped.items()))
        operations = [row for row in result["rules"] if row["document_sha256"] == digest
                      and row["document_name"] == name and row["kind"] == "operation" and _in_scope(row["scope"], scope)]
        statuses = [row for row in result["rules"] if row["document_sha256"] == digest
                    and row["document_name"] == name and row["kind"] == "status" and _in_scope(row["scope"], scope)]
        if not operations:
            result["boundaries"].append({"reason": "interface_operations_not_resolved",
                "reference_ids": [retain(call_section)["reference_id"]]})
            continue
        repeated = defaultdict(set)
        for rule in operations + statuses:
            repeated[(rule["kind"], rule["symbol"])].add((rule["meaning"], rule["caveat"]))
        if any(len(values) > 1 for values in repeated.values()):
            result["boundaries"].append({"reason": "interface_rule_scope_ambiguous",
                "reference_ids": [retain(call_section)["reference_id"]]})
            continue
        reference_ids = {retain(call_section)["reference_id"]}
        reference_ids.update(retain(section)["reference_id"] for section in function_sections)
        interface = {"interface_id": _id("fwi:", COMPILER_VERSION, digest, name, target, argument, function, scope),
            "target_template": target, "argument_template": argument, "function_template": function,
            "scope": scope, "document_sha256": digest, "document_name": name, "target_version": versions.get(name),
            "reference_ids": sorted(reference_ids),
            "operation_rule_ids": [row["rule_id"] for row in operations],
            "status_rule_ids": [row["rule_id"] for row in statuses],
            "semantic_basis": "documented_interface_template", "runtime_verified": False}
        if not any(row["interface_id"] == interface["interface_id"] for row in result["interfaces"]):
            result["interfaces"].append(interface)
    if document.truncated:
        result["boundaries"].append({"reason": "framework_document_truncated"})
        # Retained rows remain citable; an incomplete manual must not license
        # treating an external call as covered by an interface declaration.
        result["interfaces"] = []
    result["references"] = list(references.values())
    result["status"] = "COMPILED" if result["rules"] else "NO_RULES"
    return result
