"""Source-bound framework observations, separated from declared contracts.

This is an entry-scoped structural audit, not a control-flow or I/O interpreter.
It intentionally does not merge derived observations into the raw evidence DB.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from procedure_expansion import expand_program


_NAME = re.compile(r"[A-Za-z0-9_$#@-]+\Z")
_TOKEN = re.compile(r"'(?:(?:'')|[^'])*'|\"(?:(?:\"\")|[^\"])*\"|[A-Za-z0-9_$#@-]+|[^\s]")
_DEFINITION = re.compile(r"^\s*([A-Z0-9_$#@-]+)\s*(SECTION)?\s*\.\s*$", re.I)
_RESERVED = frozenset({
    "GOBACK", "EXIT", "CONTINUE", "STOP", "END-IF", "END-CALL",
    "END-PERFORM", "END-EVALUATE", "END-COMPUTE", "END-READ", "END-WRITE",
    "END-EXEC", "ELSE", "END", "DECLARATIVES", "END-DECLARATIVES",
})


def _object(value: object, required: set[str], optional: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) - required - optional or required - set(value):
        raise ValueError(f"Invalid {label} fields")
    return value


def _text(value: object, label: str, *, name: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError(f"Invalid {label}")
    if name and not _NAME.fullmatch(value):
        raise ValueError(f"Invalid {label} identifier")
    return value


def _rows(value: object, label: str, maximum: int = 256) -> list:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"Invalid {label} list")
    return value


def _unique(rows: list, key: str, label: str) -> None:
    values = [row[key].upper() for row in rows]
    if len(set(values)) != len(values):
        raise ValueError(f"Duplicate {label}")


def validate_profile(profile: dict | None) -> dict | None:
    """Validate data, never execute configuration or accept verification flags."""
    if profile is None:
        return None
    p = deepcopy(_object(profile, {"schema_version", "profile_id", "profile_version", "provenance", "entries"},
                         {"io_contracts", "record_decisions", "artifact_requirements"}, "profile"))
    if p["schema_version"] != "1.0":
        raise ValueError("Unsupported profile schema_version")
    for key in ("profile_id", "profile_version"):
        _text(p[key], key)
    provenance = _object(p["provenance"], {"kind", "reference", "target_version"}, set(), "provenance")
    _text(provenance["kind"], "provenance kind")
    if provenance["kind"] not in {"synthetic", "public_candidate", "company_validated"}:
        raise ValueError("Invalid provenance kind")
    for key in ("reference", "target_version"):
        _text(provenance[key], key)
    entries = _rows(p["entries"], "entries")
    for entry in entries:
        _object(entry, {"program", "mode", "control_copy", "sections"}, set(), "entry")
        _text(entry["program"], "program", name=True)
        _text(entry["control_copy"], "control_copy", name=True)
        _text(entry["mode"], "entry mode")
        if entry["mode"] not in {"batch", "online", "subroutine"}:
            raise ValueError("Invalid entry mode")
        for section in _rows(entry["sections"], "sections"):
            _object(section, {"name", "role"}, set(), "section")
            _text(section["name"], "section name", name=True)
            _text(section["role"], "section role")
        _unique(entry["sections"], "name", "section")
    _unique(entries, "program", "entry program")
    contracts = p.setdefault("io_contracts", [])
    for contract in _rows(contracts, "io_contracts"):
        _object(contract, {"program", "function_field", "status_field", "operations"}, set(), "I/O contract")
        for key in ("program", "function_field", "status_field"):
            _text(contract[key], key, name=True)
        if contract["function_field"].upper() == contract["status_field"].upper():
            raise ValueError("I/O function and status fields must be distinct")
        for operation in _rows(contract["operations"], "operations"):
            _object(operation, {"value", "meaning", "reads_record"}, set(), "operation")
            for key in ("value", "meaning"):
                _text(operation[key], key)
            if type(operation["reads_record"]) is not bool:
                raise ValueError("reads_record must be boolean")
        _unique(contract["operations"], "value", "operation value")
    _unique(contracts, "program", "I/O program")
    decisions = p.setdefault("record_decisions", [])
    decision_keys: set[tuple] = set()
    for decision in _rows(decisions, "record_decisions"):
        _object(decision, {"program", "field", "values"}, set(), "record decision")
        for key in ("program", "field"):
            _text(decision[key], key, name=True)
        identity = (decision["program"].upper(), decision["field"].upper())
        if identity in decision_keys:
            raise ValueError("Duplicate record decision")
        decision_keys.add(identity)
        if any(decision["field"].upper() == c["status_field"].upper() for c in contracts):
            raise ValueError("Record decision and I/O status channels must be distinct")
        for value in _rows(decision["values"], "decision values"):
            _object(value, {"value", "meaning"}, set(), "decision value")
            # A blank value is a legitimate declared record decision.
            if not isinstance(value["value"], str) or len(value["value"]) > 2048:
                raise ValueError("Invalid decision value")
            _text(value["meaning"], "decision meaning")
        _unique(decision["values"], "value", "decision value")
    requirements = p.setdefault("artifact_requirements", [])
    for item in _rows(requirements, "artifact_requirements"):
        _object(item, {"kind", "relative_path"}, set(), "artifact requirement")
        _text(item["kind"], "artifact kind")
        path = _text(item["relative_path"], "artifact path")
        if (PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts
                or "\\" in path or ":" in path or path != PurePosixPath(path).as_posix()):
            raise ValueError("Artifact path must be a normalized source-relative path")
    _unique(requirements, "relative_path", "artifact requirement")
    return p


def _reference(line: dict) -> dict:
    return {"origin": deepcopy(line["origin"]), "include_chain": deepcopy(line.get("include_chain", []))}


def _refs(lines: list[dict]) -> list[dict]:
    result = []
    for line in lines:
        ref = _reference(line)
        if ref not in result:
            result.append(ref)
    return result


def _observations(expansion: dict) -> tuple[dict, list[dict]]:
    observations = {"includes": deepcopy(expansion.get("includes", [])), "procedure_definitions": [],
                    "performs": [], "calls": [], "native_io": []}
    boundaries: list[dict] = []
    if not expansion["source_expansion_complete"]:
        boundaries.append({"reason": "procedure_observations_suppressed",
                           "detail": "Incomplete source expansion: retain inclusion evidence, do not infer host-owned procedure facts."})
        return observations, boundaries
    tokens = []
    for line in expansion["lines"]:
        for match in _TOKEN.finditer(line["code"]):
            tokens.append((match.group(), line))
    start = next((i + 2 for i in range(len(tokens) - 1)
                  if tokens[i][0].upper() == "PROCEDURE" and tokens[i + 1][0].upper() == "DIVISION"), None)
    if start is None:
        boundaries.append({"reason": "procedure_division_not_found", "detail": "No supported procedure entry was identified."})
        return observations, boundaries
    first_line = tokens[start - 1][1]
    embedded_lines: set[int] = set()
    embedded = False
    for token, line in tokens[start:]:
        if token.upper() == "EXEC":
            embedded = True
        if embedded:
            embedded_lines.add(id(line))
        if token.upper() == "END-EXEC":
            embedded = False
    started = False
    for line in expansion["lines"]:
        if line is first_line:
            started = True
            continue
        if not started or id(line) in embedded_lines:
            continue
        match = _DEFINITION.fullmatch(line["code"])
        if (match and match[1].upper() not in _RESERVED and not match[1].upper().startswith("END-")
                and not match[1].isdigit()):
            observations["procedure_definitions"].append({
                "name": match[1].upper(), "kind": "section" if match[2] else "paragraph",
                "references": [_reference(line)],
            })
    definitions = observations["procedure_definitions"]
    i = start
    while i < len(tokens):
        token, line = tokens[i]
        word = token.upper()
        next_token = tokens[i + 1] if i + 1 < len(tokens) else None
        if word == "EXEC":
            observations["native_io"].append({"operation": "EXEC", "references": [_reference(line)]})
            boundaries.append({"reason": "embedded_language_not_analyzed", "references": [_reference(line)]})
            i += 1
            while i < len(tokens) and tokens[i][0].upper() != "END-EXEC":
                i += 1
        elif word == "PERFORM":
            if (next_token and _NAME.fullmatch(next_token[0]) and not next_token[0].isdigit()
                    and next_token[0].upper() not in {"UNTIL", "VARYING", "WITH", "TIMES"}
                    and not (i + 2 < len(tokens) and tokens[i + 2][0].upper() in {"TIMES", "OF", "IN", "("})):
                target = next_token[0].upper()
                candidates = [d for d in definitions if d["name"] == target]
                references = _refs([line, next_token[1]])
                observations["performs"].append({
                    "target": target, "references": references,
                    "target_definitions": [r for d in candidates for r in d["references"]],
                    "target_status": "unique_definition" if len(candidates) == 1 else (
                        "missing_definition" if not candidates else "ambiguous_definition"),
                    "relationship": "syntactic_reference_not_execution_order",
                })
                if len(candidates) != 1:
                    boundaries.append({"reason": "perform_target_not_unique", "target": target, "references": references})
            else:
                boundaries.append({"reason": "inline_perform_not_analyzed", "references": [_reference(line)]})
        elif word == "CALL":
            if next_token:
                value = next_token[0]
                literal = value.startswith(("'", '"'))
                if literal or _NAME.fullmatch(value):
                    target = value[1:-1].replace(value[0] * 2, value[0]) if literal else value.upper()
                    observations["calls"].append({
                        "target": target, "target_kind": "literal" if literal else "dynamic",
                        "references": _refs([line, next_token[1]]), "io_contract": None,
                        "function_value_at_call": "not_resolved", "runtime_verified": False,
                        "callee_body_analyzed": False,
                    })
                    boundaries.append({"reason": "callee_semantics_not_analyzed" if literal else "dynamic_call_target_unresolved",
                                       "target": target, "references": _refs([line, next_token[1]])})
                else:
                    boundaries.append({"reason": "call_form_not_analyzed", "references": [_reference(line)]})
        elif word in {"READ", "WRITE", "REWRITE", "START", "OPEN", "CLOSE", "DELETE"}:
            observations["native_io"].append({"operation": word, "references": [_reference(line)]})
        i += 1
    return observations, boundaries


def audit_framework(source_root: Path, entry_program: str, profile: dict | None = None, *, extensions=None) -> dict:
    """Inspect one entry and its included source without running code or models."""
    p = validate_profile(profile)
    expansion = expand_program(Path(source_root), entry_program, extensions=extensions)
    observations, boundaries = _observations(expansion)
    boundaries = deepcopy(expansion["boundaries"]) + boundaries
    declared = {"entry": None, "io_contracts": [], "record_decisions": [], "artifact_requirements": []}
    profile_metadata = None
    if p:
        profile_metadata = {key: p[key] for key in ("profile_id", "profile_version", "provenance")}
        profile_metadata["semantic_status"] = "declared_not_verified"
        entry = next((e for e in p["entries"] if e["program"].upper() == entry_program.upper()), None)
        declared["entry"] = deepcopy(entry)
        if not entry:
            boundaries.append({"reason": "entry_contract_missing", "detail": "Profile has no exact entry mapping."})
        else:
            includes = [inc for inc in observations["includes"]
                        if inc["copy_name"].upper() == entry["control_copy"].upper() and inc["status"] == "expanded"]
            declared["entry"]["control_copy_observed"] = bool(includes)
            if not includes:
                boundaries.append({"reason": "control_copy_not_expanded", "copy_name": entry["control_copy"]})
            for section in declared["entry"]["sections"]:
                section["definition_observed"] = any(d["name"] == section["name"].upper() and d["kind"] == "section"
                                                     for d in observations["procedure_definitions"])
                section["perform_observed"] = any(d["target"] == section["name"].upper()
                                                  for d in observations["performs"])
                section["role_status"] = "declared_not_verified"
                if not section["definition_observed"]:
                    boundaries.append({"reason": "declared_section_not_found", "target": section["name"]})
        declared["io_contracts"] = deepcopy(p["io_contracts"])
        declared["record_decisions"] = [deepcopy(d) for d in p["record_decisions"]
                                         if d["program"].upper() == entry_program.upper()]
        for call in observations["calls"]:
            if call["target_kind"] == "literal":
                contract = next((c for c in p["io_contracts"] if c["program"] == call["target"]), None)
                if contract:
                    call["io_contract"] = contract["program"]
        manifest = {f["relative_path"]: f for f in expansion["source_files"]}
        for requirement in p["artifact_requirements"]:
            row = deepcopy(requirement)
            row["status"] = "present_not_interpreted" if row["relative_path"] in manifest else "missing_from_snapshot"
            declared["artifact_requirements"].append(row)
            boundaries.append({"reason": "external_artifact_semantics_not_analyzed", **row})
        boundaries.append({"reason": "profile_semantics_not_verified",
                           "detail": "Profile provenance and meanings are declarations, not source or runtime proof."})
    else:
        boundaries.append({"reason": "framework_profile_missing", "detail": "Entry mode and framework meanings are not inferred from names."})
    boundaries.append({"reason": "structural_observations_only",
                       "detail": "No path reachability, loop, state value, file selection, cursor, lock, commit or restart proof."})
    manifest_json = json.dumps(expansion["source_files"], sort_keys=True, ensure_ascii=False)
    profile_json = json.dumps(p, sort_keys=True, ensure_ascii=False)
    return {
        "schema_version": "1.0", "entry_program": entry_program.upper(),
        "source_manifest_hash": hashlib.sha256(manifest_json.encode()).hexdigest(),
        "profile_hash": hashlib.sha256(profile_json.encode()).hexdigest() if p else None,
        "profile": profile_metadata, "source_files": expansion["source_files"],
        "scope": {"source_expansion_complete": expansion["source_expansion_complete"],
                  "compiler_equivalent": False, "control_flow_complete": False,
                  "runtime_verified": False, "question_answered": False,
                  "index_scope": "raw_source", "framework_scope": "derived_source_observations"},
        "observations": observations, "declared_contracts": declared, "boundaries": boundaries,
        "summary": {"expanded_lines": len(expansion["lines"]),
                    **{key: len(observations[key]) for key in ("includes", "performs", "calls")},
                    "boundaries": len(boundaries)},
    }
