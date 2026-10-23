"""Small directed relationships between facts already present in a request.

This is a source-syntax projection, not a call graph resolver, execution trace,
or field-lineage engine. In particular, a matching name alone cannot establish
an object binding or turn a program's input/output inventory into a transform.
"""

from collections import defaultdict
from copy import deepcopy
import hashlib
import json


FACT_GROUPS = ("purpose_candidates", "entries", "programs", "relationships", "data_roles", "visible_rules")


def orientation_fact_id(row):
    payload = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "bo_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def retained_handoffs(orientation):
    """A removed origin removes its entire edge, including copied metadata."""
    retained = {row["orientation_id"] for group in FACT_GROUPS for row in orientation[group]}
    return [row for row in orientation["handoffs"] if set(row["source_fact_ids"]) <= retained]


def build_business_handoffs(orientation, *, max_items=32):
    """Use only retained orientation rows, each with complete provenance."""
    result = []
    definitions, local_definitions, declarations = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in orientation["programs"]:
        definitions[row.get("program_name")].append(row)
        local_definitions[(row.get("relative_path"), row.get("program_name"))].append(row)
    for row in orientation["data_roles"]:
        if row.get("kind") == "file_declaration":
            declarations[(row.get("relative_path"), row.get("program_name"), row.get("file_name"))].append(row)

    def local_program(row):
        values = local_definitions.get((row.get("relative_path"), row.get("program_name")), [])
        return values[0] if row.get("program_name") and len(values) == 1 else None

    def endpoint(kind, name=None, **fields):
        return {"kind": kind, **({"name": name} if name else {}),
                **{key: value for key, value in fields.items() if value is not None}}

    def program_endpoint(row):
        return endpoint("program", row["program_name"], source_path=row["relative_path"])

    def emit(relation, origin, target, facts, *, limits=(), **details):
        if len(result) >= max_items:
            return
        # An edge never invents a new source span or discards a required guard.
        unique = {fact["orientation_id"]: fact for fact in facts}
        refs = sorted({ref for fact in unique.values() for ref in fact["supplied_reference_ids"]})
        row = {"relation": relation, "from": origin, "to": target,
               "source_fact_ids": sorted(unique), "supplied_reference_ids": refs,
               "provenance": "complete_source_locations_and_conditions_in_source_fact_ids",
               "conditions_from_fact_ids": [key for key, fact in unique.items()
                                            if fact.get("guards") or fact.get("compilation_guards")],
               "basis": "retained_source_syntax", "runtime_verified": False,
               "limits": list(limits), **details}
        if any(fact.get("compilation_status") == "candidate" for fact in unique.values()):
            row["limits"].append("compilation_branch_not_resolved")
        if any(fact.get("control_context") == "statement_control_context_not_bound" for fact in unique.values()):
            row["limits"].append("statement_control_scope_not_bound")
        row["handoff_id"] = "bh_" + orientation_fact_id(row)[3:]
        result.append(row)

    calls = [row for row in orientation["relationships"]
             if row.get("kind") == "program_call" or row.get("command") == "CALL"]
    for call in calls:
        is_cl = call.get("command") == "CALL"
        target = call.get("target") or {}
        if (not call.get("target_name") or (is_cl and target.get("binding_status") == "dynamic_or_unsupported")
                or (not is_cl and call.get("target_binding") != "literal_request")):
            continue
        caller = local_program(call)
        if not is_cl and caller is None:
            continue
        facts = [call] + ([caller] if caller else [])
        candidates = definitions.get(call["target_name"], [])
        resolution = "implementation_not_in_supplied_facts"
        details = {}
        if len(candidates) == 1:
            facts.append(candidates[0])
            resolution = "unique_supplied_program_name_candidate"
            # This is explicitly a candidate. A literal CALL still resolves a
            # compiled runtime object, not a repository path.
            details["implementation_candidate"] = program_endpoint(candidates[0])
        elif candidates:
            facts.extend(candidates)
            resolution = "ambiguous_supplied_program_definitions"
        origin = (endpoint("cl_source", source_path=call["relative_path"],
                           context=call.get("command_context")) if is_cl else program_endpoint(caller))
        emit("requests_program_call", origin, endpoint("requested_program", call["target_name"],
             spelling=target.get("spelling")), facts,
             limits=("runtime_program_binding_and_call_result_not_established",),
             target_resolution=resolution, **details)
        if is_cl and call.get("command_context") == "submitted_job_candidate":
            for submission in orientation["entries"]:
                # The nested CALL and SBMJOB must be the very same source
                # command, not neighbouring lines or matching JOB names.
                if (submission.get("command") == "SBMJOB"
                        and submission["source_locations"][0] == call["source_locations"][0]
                        and submission.get("statement") == call.get("statement")
                        and call.get("parent_command") == "SBMJOB"):
                    emit("submits_call", endpoint("submitted_job_request", submission.get("job_name"),
                         source_path=submission["relative_path"]), endpoint("requested_program", call["target_name"]),
                         [submission, call], limits=("submission_schedule_and_execution_not_established",
                                                   "submitter_overrides_not_inherited_by_this_projection"))

    for role in orientation["data_roles"]:
        kind = role.get("kind")
        program = local_program(role)
        if kind == "io_operation" and program and role.get("file_binding_status") == "confirmed_source_syntax":
            selected = declarations.get((role.get("relative_path"), role.get("program_name"), role.get("file_name")), [])
            if len(selected) != 1:
                continue
            declaration = selected[0]
            relation = {"READ": "reads_record_from", "START": "positions_file_cursor", "WRITE": "writes_record_to",
                        "REWRITE": "rewrites_record_in", "DELETE": "deletes_record_from"}.get(role.get("operation"))
            if not relation:
                continue
            target = endpoint("program_file", role["file_name"], assigned_name=declaration.get("assigned_name"),
                              dynamic_assignment_symbol=declaration.get("dynamic_assignment_symbol"))
            emit(relation, program_endpoint(program), target, [program, declaration, role],
                 limits=("execution_path_io_success_and_runtime_file_binding_not_established",),
                 record_names=role.get("record_names", []), operation=role["operation"])
        elif kind == "sql_table_access" and program:
            emit("reads_sql_table" if role["access_role"] == "read" else "requests_sql_table_change",
                 program_endpoint(program), endpoint("sql_table_operand", role["table_name"]), [program, role],
                 limits=("deployment_mode_database_binding_and_sql_result_not_established",),
                 statement_fact_id=role["orientation_id"])
        elif kind == "dds_pfile" and role.get("record_name"):
            for name in role.get("physical_file_names", []):
                emit("dds_record_declares_pfile", endpoint("dds_record", role["record_name"],
                     source_path=role["relative_path"]), endpoint("physical_file_operand", name), [role],
                     limits=("dds_source_path_does_not_establish_runtime_logical_file_identity",))
        elif role.get("command") == "OVRDBF":
            original, replacement = role.get("file") or {}, role.get("tofile") or {}
            if (not original.get("name") or not replacement.get("name")
                    or any(item.get("binding_status") == "dynamic_or_unsupported" for item in (original, replacement))):
                continue
            emit("requests_file_override", endpoint("file_operand", original["spelling"]),
                 endpoint("replacement_file_operand", replacement["spelling"]), [role],
                 limits=("effective_scope_member_and_runtime_binding_not_established",
                         "not_propagated_to_programs_or_submitted_jobs_by_name"),
                 declared_scope=role.get("declared_override_scope"), member_operand=role.get("member"))

    for convention in orientation["visible_rules"]:
        if (convention.get("interpretation_level") != "framework_convention"
                or not convention.get("dependency_covered") or convention.get("target_source_available")
                or not convention.get("operation")):
            continue
        sites = convention.get("callsite_locations", [])
        matches = [call for call in calls if call.get("kind") == "program_call"
                   and call.get("target_binding") == "literal_request"
                   and call.get("target_name") == convention.get("target_name")
                   and call["source_locations"][0] in sites]
        if len(matches) != 1:
            continue
        call = matches[0]
        emit("requests_documented_framework_operation", endpoint("callsite", call["target_name"],
             source_path=call["relative_path"]), endpoint("documented_operation", convention["operation"]["value"]),
             [call, convention], limits=("closed_implementation_internal_effects_and_call_result_not_established",),
             basis="source_bound_supplied_framework_convention", operation=deepcopy(convention["operation"]),
             argument=convention.get("argument"), function_field=convention.get("function_field"))
    return result
