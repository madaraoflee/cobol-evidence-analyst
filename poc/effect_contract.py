"""Versioned external-effect contracts over existing framework bindings.

Manual operation meanings, explicit runtime configuration and source callsites
remain distinct evidence. This module does not infer cursor, locking, database
contents or successful persistence from operation names.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re


EFFECT_CONTRACT_VERSION = "framework-effect-contract-v1"
MAX_CONTRACT_BYTES = 256_000
_RESOURCE_SUPPORT = {
    "cobol-file": ("bounded", "FD/SELECT/ASSIGN and explicit file requests; runtime contents unknown"),
    "sql": ("bounded", "SQL operation and host variables; database contents and execution result unknown"),
    "dds": ("candidate", "DDS artifacts are indexed; access-path, join and lock semantics require target validation"),
    "cl": ("candidate", "Indexed CALL/SBMJOB, library-list and file-override source commands; effective job environment and execution are unverified"),
    "jcl": ("candidate", "JCL artifacts are indexed; job allocation and runtime procedures are unresolved"),
    "cics": ("unsupported", "No target-validated CICS effect adapter is configured"),
}


def resource_profile(context=None):
    """Declare actual adapter limits for the selected target, never sample proof."""
    from compilation_context import resolve_compilation_context
    selected = resolve_compilation_context(context)
    kinds = list(dict.fromkeys(selected.resource_kinds))
    return {"target_profile": selected.target_profile, "dialect": selected.dialect,
            "target_validated": False,
            "resources": [{"kind": kind, "support_status": _RESOURCE_SUPPORT.get(kind, ("unsupported", "No effect adapter is configured"))[0],
                           "scope": _RESOURCE_SUPPORT.get(kind, ("unsupported", "No effect adapter is configured"))[1]}
                          for kind in kinds],
            "acceptance_basis": "configured_target_and_adapter_capability_not_public_sample_success"}


def load_runtime_contract(reference_path):
    """Load an optional local companion through the existing bounded validator.

    A folder may contain runtime-contract.json; a manual file may have a sibling
    <stem>.runtime-contract.json. The companion is data and provides additional
    effect evidence, not instructions or production runtime verification.
    """
    from framework_io import validate_runtime_contract
    if not reference_path:
        return {"contracts": [], "references": [], "boundaries": []}
    path = Path(reference_path).expanduser()
    candidate = path / "runtime-contract.json" if path.is_dir() else path.with_suffix(".runtime-contract.json")
    if not candidate.exists():
        return {"contracts": [], "references": [], "boundaries": []}
    try:
        if candidate.is_symlink() or candidate.stat().st_size > MAX_CONTRACT_BYTES:
            raise ValueError("runtime_contract_file_not_supported")
        raw = candidate.read_bytes()
        text = raw.decode("utf-8")
        contract = validate_runtime_contract(json.loads(text))
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        return {"contracts": [], "references": [], "boundaries": [{"reason": "framework_runtime_contract_unavailable"}]}
    digest = hashlib.sha256(raw).hexdigest()
    # Keep physical configuration lines, rather than inventing an evidence
    # excerpt from a projection of the loaded object.
    references, lines, start, chunk = [], text.splitlines(), 1, []
    def flush():
        nonlocal start, chunk
        if chunk:
            end = start + len(chunk) - 1
            references.append({"reference_id": f"fw:contract:{digest[:16]}:{start}-{end}",
                "document_name": candidate.name, "document_sha256": digest,
                "heading": "Explicit effect configuration", "page": None,
                "start_line": start, "end_line": end, "text": "\n".join(chunk),
                "text_truncated": False, "selection_reason": "effect_contract_configuration",
                "provenance_kind": "configuration"})
            start, chunk = end + 1, []
    for line in lines:
        if len(line) > 2200:
            return {"contracts": [], "references": [], "boundaries": [{"reason": "framework_runtime_contract_line_too_large"}]}
        if chunk and sum(len(row) + 1 for row in chunk) + len(line) > 2200:
            flush()
        chunk.append(line)
    flush()
    return {"contracts": [{"value": contract, "source_sha256": digest,
                           "reference_ids": [row["reference_id"] for row in references]}],
            "references": references, "boundaries": []}


def _interface_candidates(fact, knowledge):
    from framework_binding import _stem
    return [interface for interface in knowledge.get("interfaces", [])
            if _stem(interface.get("target_template", ""), str(fact.get("target_name", ""))) is not None]


def _runtime_effects(fact, knowledge):
    result = []
    value = (fact.get("operation") or fact.get("documented_operation_candidate") or {}).get("value")
    for configured in knowledge.get("runtime_effect_contracts", []):
        contract = configured["value"]
        for service in contract["services"]:
            if service["program"] != fact.get("target_name") or service["argument"] != fact.get("argument"):
                continue
            operation = next((row for row in service["operations"] if row["value"] == value), None)
            if operation is None:
                continue
            action = operation["action"]
            reads = action == "get" or action == "next" or action == "first" and operation.get("reads_record")
            writes = action == "save"
            result.append({"contract_id": contract["contract_id"], "contract_version": contract["contract_version"],
                "framework_version": contract["provenance"]["target_version"],
                "provenance": {"kind": "human_confirmation" if contract["provenance"]["kind"] == "company_validated" else "configuration",
                               "declaration_kind": contract["provenance"]["kind"],
                               "reference": contract["provenance"]["reference"], "source_sha256": configured["source_sha256"]},
                "reference_ids": configured["reference_ids"], "input_fields": [service["function_field"], *service["key_fields"].values()],
                "output_fields": [service["status_field"], *(service["record_fields"].values() if reads else [])],
                "record_layout": service["record_fields"], "record_layout_source_verified": False,
                "status_field": service["status_field"],
                "status_codes": contract["statuses"], "resources": [{"kind": "declared_dataset", "name": contract["dataset"]["id"],
                    "read_request": bool(reads), "write_request": writes, "access_path": service["access_path"]}],
                "cursor": {"action": action, "session": service["session"]},
                "locking": contract["locking"], "transaction": contract["transaction"],
                "restart": contract["restart"], "runtime_verified": False,
                "target_validation": "human_confirmed" if contract["provenance"]["kind"] == "company_validated" else "unvalidated_candidate",
                "support_scope": "explicit_configuration_bound_to_source_call_not_runtime_result"})
    return result


def attach_effect_contracts(facts, knowledge, *, compilation_context=None):
    """Enrich existing source bindings; source implementations retain precedence."""
    from compilation_context import resolve_compilation_context
    selected = resolve_compilation_context(compilation_context)
    rules = {row["rule_id"]: row for row in knowledge.get("rules", [])}
    enriched = []
    for original in facts:
        fact = deepcopy(original)
        enriched.append(fact)
        if fact.get("kind") != "framework_operation":
            continue
        candidates = _interface_candidates(fact, knowledge)
        versions = sorted({row["target_version"] for row in candidates if row.get("target_version")})
        conflict = len(versions) > 1 or bool(selected.framework_version and any(
            version.upper() != selected.framework_version.upper() for version in versions))
        manual_candidates = [{"interface_id": row["interface_id"], "framework_version": row.get("target_version"),
            "document_name": row.get("document_name"), "document_sha256": row.get("document_sha256"),
            "reference_ids": row.get("reference_ids", [])} for row in candidates]
        statuses = []
        for interface in candidates:
            for identifier in interface.get("status_rule_ids", []):
                if identifier in rules:
                    rule = rules[identifier]
                    statuses.append({"value": rule["symbol"], "meaning": rule["meaning"],
                                     "reference_ids": rule["reference_ids"], "document_sha256": rule["document_sha256"]})
        runtime = _runtime_effects(fact, knowledge)
        runtime_versions = {row["framework_version"] for row in runtime}
        if runtime_versions and versions and any(version not in versions for version in runtime_versions):
            conflict = True
        if selected.framework_version and any(version.upper() != selected.framework_version.upper() for version in runtime_versions):
            conflict = True
        contract = {"schema_version": EFFECT_CONTRACT_VERSION,
            "contract_id": "effect:" + hashlib.sha256((str(fact.get("interface_id")) + "\0" + str(fact.get("rule_id"))).encode()).hexdigest()[:24],
            "framework_versions": versions, "selected_framework_version": selected.framework_version,
            "applicable_call": {"target": fact.get("target_name"), "relation_type": fact.get("relation_type"),
                "arguments": [{"position": 1, "name": fact.get("argument"), "passing_mode": "REFERENCE"}],
                "function_field": fact.get("function_field"), "operation": fact.get("operation")},
            "record_layout": fact.get("layout_validation", {"status": "unknown", "callee_abi_verified": False}),
            "input_fields": [fact["function_field"]] if fact.get("function_field") else [],
            "output_scope": "argument_storage_including_subordinate_fields_unknown_effects",
            "status_codes": statuses, "manual_candidates": manual_candidates,
            "explicit_configuration_candidates": runtime,
            "provenance": {"kind": "manual", "reference_ids": fact.get("reference_ids", [])},
            "resource_profile": resource_profile(selected), "runtime_verified": False,
            "unknown_effects": ["argument_modifications", "file_table_contents", "cursor", "locks", "transaction_result"],
            "support_scope": "documented_operation_request_and_source_binding",
            "status": "version_conflict" if conflict else "source_implementation_required" if fact.get("target_source_available") else
                      "bound" if fact.get("dependency_covered") else "unresolved"}
        if (runtime and not conflict and fact.get("dependency_covered") and selected.framework_version
                and all(row["target_validation"] == "human_confirmed" and row["record_layout_source_verified"] for row in runtime)):
            contract["unknown_effects"] = ["production_data", "runtime_status", "successful_persistence"]
        if conflict:
            fact["dependency_covered"] = False
            fact["effect_contract_conflicts"] = [{"reason": "framework_version_conflict", "manual_versions": versions,
                "configuration_versions": sorted(runtime_versions), "selected_version": selected.framework_version,
                "reference_ids": sorted({identifier for row in candidates for identifier in row.get("reference_ids", [])})}]
        fact["effect_contract"] = contract
    return enriched


def visible_effect_contract(contract, reference_ids):
    """Withdraw effect details whose own evidence was not transmitted."""
    result = deepcopy(contract)
    result["status_codes"] = [row for row in result.get("status_codes", [])
                              if set(row.get("reference_ids", [])) <= set(reference_ids)]
    configured = result.get("explicit_configuration_candidates", [])
    result["explicit_configuration_candidates"] = [row for row in configured
        if set(row.get("reference_ids", [])) <= set(reference_ids)]
    if len(result["explicit_configuration_candidates"]) < len(configured):
        result["unknown_effects"] = sorted(set(result.get("unknown_effects", [])) |
            {"argument_modifications", "cursor", "locks", "transaction_result", "configuration_evidence_not_supplied"})
    result["evidence_visibility_checked"] = True
    return result
