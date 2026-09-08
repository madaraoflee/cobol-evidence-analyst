"""Compose parameter correspondences in one static callsite context at a time.

These are source-identity routes, not reaching definitions, runtime values,
feasible error paths, or a proof of disjoint storage between invocations.
"""

from __future__ import annotations

from collections import defaultdict


def _budget(value: int, upper: int) -> None:
    if type(value) is not int or not 1 <= value <= upper:
        raise ValueError("Context projection budget is outside supported bounds.")


def contextualize_errors(context_audit: dict, error_audit: dict, *,
                         max_routes: int = 1000, max_links: int = 5000,
                         max_observation_refs: int = 10000) -> dict:
    """Attach local observations and compose only exact parameter identities.

    A CONTENT/VALUE edge anywhere in a route blocks a reference-return
    candidate to that route's outer endpoint. A local working field ends a
    route: MOVE/COMPUTE/SQL effects are deliberately not guessed here.
    """
    _budget(max_routes, 10000)
    _budget(max_links, 50000)
    _budget(max_observation_refs, 100000)
    if (not context_audit.get("snapshot_id") or
            context_audit["snapshot_id"] != error_audit.get("snapshot_id")):
        raise ValueError("Context and error observations must share one snapshot.")
    contexts = context_audit["contexts"]
    if len(contexts) > 1024 or len(error_audit["observations"]) > 100000:
        raise ValueError("Context projection input exceeds its fact budget.")
    by_id = {c["context_id"]: c for c in contexts}
    if len(by_id) != len(contexts):
        raise ValueError("Context identities must be unique.")
    if sum(c["parent_context_id"] is None for c in contexts) != 1:
        raise ValueError("Contexts must have exactly one root.")
    for context in contexts:
        visited = set()
        current = context
        while current["parent_context_id"] is not None:
            if current["context_id"] in visited or current["parent_context_id"] not in by_id:
                raise ValueError("Parent contexts must form a connected acyclic tree.")
            visited.add(current["context_id"])
            current = by_id[current["parent_context_id"]]
        if context["parent_context_id"] is None and context["parameter_mappings"]:
            raise ValueError("The root context cannot have incoming parameter mappings.")
    roles = {}
    for program in error_audit["programs"]:
        fields = defaultdict(list)
        for role, key in (("status", "status_fields"), ("output", "output_fields")):
            for name in program[key]:
                fields[name].append(role)
        roles[program["program_name"]] = fields
    indexes = defaultdict(list)
    for index, observation in enumerate(error_audit["observations"]):
        indexes[observation["program_name"]].append(index)
    formals = {}
    binding_count = 0
    for context in contexts:
        incoming = defaultdict(list)
        parent = context["parent_context_id"]
        if parent is not None and parent not in by_id:
            raise ValueError("A parent context is missing.")
        for mapping in context["parameter_mappings"]:
            binding_count += 1
            if binding_count > 50000:
                raise ValueError("Context projection input exceeds its binding budget.")
            if (mapping["callee_field"]["context_id"] != context["context_id"] or
                    mapping["caller_field"]["context_id"] != parent or
                    mapping["passing_mode"] not in {"REFERENCE", "CONTENT", "VALUE"}):
                raise ValueError("A parameter mapping crosses the wrong context.")
            incoming[mapping["callee_field"]["symbol_id"]].append(mapping)
        formals[context["context_id"]] = incoming

    overlays, routes, boundaries = [], [], []
    if context_audit.get("summary", {}).get("truncated"):
        boundaries.append({"reason": "upstream_context_audit_truncated"})
    remaining_refs = max_observation_refs
    links_used = 0
    routes_cut = False
    for context in contexts:
        local_indexes = indexes[context["program_name"]]
        overlays.append({
            "context_id": context["context_id"], "program_name": context["program_name"],
            "local_observation_indexes": local_indexes[:remaining_refs],
            "truncated": len(local_indexes) > remaining_refs,
        })
        if len(local_indexes) > remaining_refs:
            boundaries.append({"reason": "observation_reference_budget_exhausted",
                               "context_id": context["context_id"]})
        remaining_refs = max(0, remaining_refs - len(local_indexes))
        for seed in context["parameter_mappings"]:
            field_roles = roles.get(context["program_name"], {}).get(seed["callee_field"]["name"])
            if not field_roles:
                continue
            if len(formals[context["context_id"]][seed["callee_field"]["symbol_id"]]) != 1:
                boundaries.append({"reason": "ambiguous_incoming_parameter_correspondence",
                                   "context_id": context["context_id"],
                                   "symbol_id": seed["callee_field"]["symbol_id"]})
                continue
            if len(routes) >= max_routes or links_used >= max_links:
                routes_cut = True
                break
            fields = [seed["callee_field"]]
            steps = []
            current = context
            mapping = seed
            visited = set()
            terminal = "no_incoming_correspondence_observed"
            while mapping is not None:
                if links_used >= max_links:
                    terminal = "parameter_route_link_budget_exhausted"
                    routes_cut = True
                    break
                if current["context_id"] in visited:
                    raise ValueError("Parent contexts must form an acyclic tree.")
                visited.add(current["context_id"])
                links_used += 1
                steps.append({
                    "binding_id": mapping["binding_id"],
                    "passing_mode": mapping["passing_mode"],
                    "evidence_refs": mapping["evidence_refs"],
                })
                fields.append(mapping["caller_field"])
                current = by_id[current["parent_context_id"]]
                if current["parent_context_id"] is None:
                    terminal = "root_context_field"
                    break
                candidates = formals[current["context_id"]].get(fields[-1]["symbol_id"], [])
                if len(candidates) > 1:
                    terminal = "ambiguous_incoming_parameter_correspondence"
                    break
                mapping = candidates[0] if candidates else None
                if not candidates and current.get("parameter_mapping_complete") is False:
                    terminal = "incoming_parameter_mapping_incomplete"
            all_reference = bool(steps) and all(s["passing_mode"] == "REFERENCE" for s in steps)
            routes.append({
                "context_id": context["context_id"], "program_name": context["program_name"],
                "field_name": seed["callee_field"]["name"], "roles": field_roles,
                "fields_inner_to_outer": fields, "steps_inner_to_outer": steps,
                "terminal_reason": terminal,
                "reference_return_candidate": all_reference and terminal == "root_context_field",
                "copy_boundary_blocks_outer_reference_return": any(
                    s["passing_mode"] in {"CONTENT", "VALUE"} for s in steps),
                "runtime_error_propagation_proven": False,
            })
        # Keep collecting bounded observation references after route truncation.
    if routes_cut:
        boundaries.append({"reason": "parameter_route_budget_exhausted"})
    return {
        "snapshot_id": context_audit["snapshot_id"],
        "evaluation_scope": "context_bound_parameter_correspondences_and_local_observation_references",
        "complete": False, "runtime_error_propagation_proven": False,
        "observation_reference_scope": "same_local_source_observation_reused_per_static_context",
        "contexts": overlays, "parameter_routes": routes, "boundaries": boundaries,
        "summary": {"contexts": len(overlays), "parameter_routes": len(routes),
                    "parameter_links": links_used,
                    "observation_references": max_observation_refs - remaining_refs,
                    "truncated": bool(boundaries)},
        "limitations": [
            "Context identity does not prove execution, fresh storage or absence of aliasing.",
            "Reference-return candidates require a full observed route to the root context field.",
            "Routes stop at local fields; assignments, branch feasibility and numeric values are not composed.",
            "A reference-return candidate does not prove that an error is produced or reaches the outer field.",
        ],
    }
