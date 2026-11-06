"""Local, snapshot-bound navigation state; never source or execution proof."""

import hashlib
import json


def frontier_scope(business_map, roots, business_overview):
    identity = business_map.get("source_identity", {})
    value = [business_map.get("snapshot_id"), roots, business_overview,
             bool(identity.get("exclusive_source_scope")),
             business_map.get("navigation_scope", {}).get("source_scopes", [])]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def source_frontier_state(business_map, roots, business_overview, saved=None):
    scope = frontier_scope(business_map, roots, business_overview)
    if (isinstance(saved, dict) and saved.get("schema_version") == 1
            and saved.get("scope_key") == scope
            and saved.get("snapshot_id") == business_map.get("snapshot_id")
            and all(isinstance(saved.get(key), list) for key in
                    ("pending_paths", "pending_expansions", "read_candidate_paths", "unresolved_boundaries"))):
        return json.loads(json.dumps(saved)), False
    return {"schema_version": 1, "snapshot_id": business_map.get("snapshot_id"),
            "scope_key": scope, "root_paths": list(roots), "pending_paths": [],
            "pending_expansions": [], "read_candidate_paths": [], "unresolved_boundaries": [],
            **({"reset_reason": "source_frontier_scope_changed"} if saved else {})}, True


def prepare_investigation_frontier(business_map, question, roots, paths, source_scopes,
                                   saved=None, *, advance=False, batch_size=8):
    value = [business_map.get("snapshot_id"), str(question), roots, source_scopes,
             bool(business_map.get("source_identity", {}).get("exclusive_source_scope"))]
    scope = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    valid = (isinstance(saved, dict) and saved.get("schema_version") == 1 and saved.get("scope_key") == scope
             and all(isinstance(saved.get(key), list) for key in ("known_paths", "active_paths", "completed_paths")))
    state = json.loads(json.dumps(saved)) if valid else {
        "schema_version": 1, "scope_key": scope, "snapshot_id": business_map.get("snapshot_id"),
        "known_paths": [], "active_paths": [], "completed_paths": [], "batch_complete": False,
        "processed_rule_ids": [], "active_rule_ids": [], "primary_rules_remaining": False,
        **({"reset_reason": "investigation_frontier_scope_changed"} if saved else {})}
    state["known_paths"] = list(dict.fromkeys([*state["known_paths"], *paths]))
    previous_active = list(state["active_paths"])
    if advance and state["batch_complete"]:
        if state.get("primary_rules_remaining") and state.get("active_rule_ids"):
            state["processed_rule_ids"] = list(dict.fromkeys([
                *state.get("processed_rule_ids", []), *state["active_rule_ids"]]))
        else:
            state["completed_paths"] = list(dict.fromkeys([*state["completed_paths"], *state["active_paths"]]))
            state["active_paths"] = []
            state["processed_rule_ids"] = []
        state["batch_complete"] = False
    if not state["active_paths"]:
        completed = set(state["completed_paths"])
        state["active_paths"] = [path for path in state["known_paths"] if path not in completed][:batch_size]
        if not state["active_paths"] and previous_active:
            # Keep the final batch reviewable while the caller combines its
            # durable per-batch ledger; an empty batch invents absent formulas.
            state["active_paths"] = previous_active
    done_or_active = set(state["completed_paths"]) | set(state["active_paths"])
    state["remaining_paths"] = [path for path in state["known_paths"] if path not in done_or_active]
    state["batch_key"] = hashlib.sha256(json.dumps([
        scope, state["active_paths"], state.get("processed_rule_ids", [])]).encode()).hexdigest()
    return state
