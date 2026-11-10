"""Supply a bounded, physical source set before selecting smaller excerpts."""

from collections import deque
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from agent_policy import resolve_agent_policy
from dependency_sources import dynamic_target_source_candidates
from repository_discovery import _connect, _fast_snapshot
from source_reading import _identify_page, _persist_page, _safe_file, _verified_lines
from source_frontier import source_frontier_state


_DEPTH_LIMIT = 4
_FRONTIER_LIMIT = 32
_ROOT_KINDS = {"program", "relative_path", "basename", "stem"}


def _file_definition_candidates(db, relative, *, after_name=""):
    """Follow explicit object spellings, keeping filename binding provisional.

    Search text nominates reads only. Full source pages below are independently
    captured and verified; neither this helper nor DDS filenames prove runtime
    library selection or overrides.
    """
    from file_impact import _mask_literals
    from file_impact_evidence import _definitions, _exact_dds_paths, _lines, _NAME

    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master "
                                         "WHERE type IN ('table','view')")}
    item = db.execute("SELECT sha256,artifact_kind FROM source_files WHERE relative_path=?",
                      (relative,)).fetchone()
    if item is None or "repo_pages" not in tables:
        return [], []
    gaps = []
    lines = _lines(db, relative, item["sha256"], tables, gaps)
    names = []
    if Path(relative).suffix.lower() in {".dds", ".lf", ".pf"}:
        for raw in lines.values():
            raw = re.sub(r"^\d{5}A", "A", raw.upper())
            if not re.match(r"^A\s", raw):
                continue
            code, complete = _mask_literals(raw)
            if not complete:
                continue
            for match in re.finditer(r"\bPFILE\s*\(\s*([A-Z0-9_$#@/ -]+)\s*\)", code):
                names.extend(match.group(1).split())
    else:
        _, names = _definitions(relative, lines, item["sha256"],
            item["artifact_kind"] in {"copybook", "cobol_fragment_or_copybook"}, gaps)
    names = sorted(set(name for name in names if name > after_name))
    if len(names) > 16:
        gaps.append({"reason": "file_definition_names_pending", "after_name": names[15]})
    paths = []
    for name in names[:16]:
        if not re.fullmatch(_NAME, name):
            gaps.append({"reason": "file_definition_identity_unresolved", "object_name": name})
            continue
        candidates = _exact_dds_paths(db, name, gaps)
        if candidates:
            paths.extend(candidates)
            gaps.append({"reason": "file_definition_binding_unresolved", "object_name": name,
                         "candidate_paths": candidates, "source_available": True})
    # Reading limits in the nomination helper do not imply absent source.
    normalized = [{key: value for key, value in gap.items() if key != "relative_path"}
                  for gap in gaps]
    return list(dict.fromkeys(paths)), normalized


def build_complete_working_set(database_path, source_session, business_map, policy,
                              *, check_cancel=None, business_overview=False, frontier_state=None):
    """Prefer the complete root, then dependencies under the ordinary allowance.

    Physical completeness describes only candidate_paths. Static dependencies
    and original text do not prove execution, parameter binding or value flow.
    """
    policy = resolve_agent_policy(policy)
    inventory = bool(business_map.get("repository_inventory_requested"))
    business_overview = business_overview or inventory
    depth_limit = None if business_overview else _DEPTH_LIMIT
    identity = business_map.get("source_identity", {})
    roots = list(dict.fromkeys(identity.get("direct_paths", [])
                              or business_map.get("direct_paths", [])))
    metadata = {"status": "not_applicable", "physical_complete": False,
        "physical_completeness_scope": "candidate_paths", "closure_complete": False,
        "semantic_execution_verified": False, "root_paths": roots,
        "candidate_paths": [], "source_manifest": [], "frontier": [],
        "omitted_candidate_paths": [], "omitted_candidate_path_count": 0,
        "omitted_frontier_count": 0, "source_characters": 0,
        "max_source_characters": policy.max_source_characters,
        "max_complete_source_characters": policy.max_complete_source_characters,
        "complete_root_budget_applied": False, "root_source_characters": 0,
        "non_root_source_characters": 0,
        "depth_limit": depth_limit, "reason": "source_identity_not_eligible"}
    result = {"pages": [], "metadata": metadata, "needs_refresh": False, "boundaries": []}
    explicit_root = (identity.get("status") == "resolved"
                     and identity.get("kind") in _ROOT_KINDS and len(roots) == 1)
    concept_roots = bool(business_overview and identity.get("status") == "none" and roots)
    if source_session is None or not (explicit_root or concept_roots):
        return result
    state, fresh_frontier = source_frontier_state(business_map, roots, business_overview, frontier_state)
    result["source_frontier_state"] = state
    durable_boundaries = {json.dumps(row, sort_keys=True) for row in state["unresolved_boundaries"]}
    metadata.update(status="fallback", reason="source_set_unavailable", closure_complete=True)
    fatal = False
    budget_limited = False
    omitted_paths = set()

    def frontier(reason, relative_path=None, *, blocking=False, **details):
        nonlocal fatal
        metadata["closure_complete"] = False
        item = {"reason": reason, **details}
        if relative_path:
            item["relative_path"] = relative_path
        if reason not in {"dependency_file_budget", "concept_root_budget", "source_byte_budget",
                "source_characters", "complete_source_characters", "dependency_navigation_pending",
                "unresolved_targets_limited", "file_definition_names_pending"}:
            key = json.dumps(item, sort_keys=True)
            if key not in durable_boundaries:
                state["unresolved_boundaries"].append(item)
                durable_boundaries.add(key)
        if details.get("needs_refresh"):
            # Use the same recovery contract as indexed search and range reads.
            result["needs_refresh"] = True
            result["boundaries"].append(dict(item))
        if len(metadata["frontier"]) < _FRONTIER_LIMIT:
            metadata["frontier"].append(item)
        else:
            metadata["omitted_frontier_count"] += 1
        if blocking:
            if not fatal:
                metadata["reason"] = reason
            fatal = True

    def budget_frontier(reason, relative_path, **details):
        nonlocal budget_limited
        if not budget_limited:
            metadata["reason"] = reason
        budget_limited = True
        if relative_path not in omitted_paths:
            omitted_paths.add(relative_path)
            metadata["omitted_candidate_path_count"] += 1
            if len(metadata["omitted_candidate_paths"]) < _FRONTIER_LIMIT:
                metadata["omitted_candidate_paths"].append(relative_path)
        frontier(reason, relative_path, **details)

    def cancel():
        if check_cancel:
            check_cancel()

    # A valid text scalar occupies at most four bytes in the supported UTF
    # encodings. This derived bound avoids capturing huge files only to reject
    # their decoded text, while staying inside the existing source byte budget.
    root_limit = policy.max_complete_source_characters
    byte_limit = min(policy.max_semantic_source_bytes,
                     (root_limit + policy.max_source_characters) * 4 + policy.max_semantic_files * 4)
    records = []
    dynamic_catalog, dynamic_catalog_bytes = {}, 0
    metadata["dynamic_target_candidates"] = []

    def dynamic_candidate_references(nominated):
        nonlocal dynamic_catalog_bytes
        references, omitted = [], 0
        for candidate in nominated["dynamic_target_candidates"]:
            encoded = json.dumps(candidate, sort_keys=True, ensure_ascii=False).encode("utf-8")
            identity = "dynamic_" + hashlib.sha256(encoded).hexdigest()[:24]
            if identity not in dynamic_catalog:
                if len(dynamic_catalog) >= 16 or dynamic_catalog_bytes + len(encoded) > 12000:
                    omitted += 1
                    continue
                stored = {"candidate_id": identity, **candidate}
                dynamic_catalog[identity] = stored
                dynamic_catalog_bytes += len(encoded)
                metadata["dynamic_target_candidates"].append(stored)
            references.append(identity)
        return {"dynamic_target_candidate_ids": references,
                "dynamic_target_provenance_omitted_count": omitted}
    located_copies = {edge["relation_id"]: edge["target_path"]
        for edge in business_map.get("relations", ()) if edge.get("relation_type") == "INCLUDES_COPY"
        and edge.get("resolution") == "unbound_copy_candidate" and edge.get("target_path") and edge.get("relation_id")}
    try:
        with closing(_connect(database_path)) as db:
            db.execute("BEGIN")
            _, overview = _fast_snapshot(db, source_session.source_root)
            snapshot = overview["snapshot_id"]
            if business_map.get("snapshot_id") != snapshot:
                frontier("snapshot_changed", blocking=True, reason_code="SNAPSHOT_CHANGED")
                return result
            if concept_roots and fresh_frontier:
                # A concept match nominates a reading root, never an exact
                # program identity. Keep the map's relevance/diversity order;
                # a filename sort here would crowd out later matching callers.
                # Verify each nomination against this index.
                root_slots = min(policy.max_semantic_files, policy.initial_pages)
                metadata.update(root_candidate_count=len(roots), root_candidate_budget=root_slots,
                    root_selection_basis=business_map.get("direct_path_selection", {}).get("basis",
                                                                                       "supplied_candidate_order"))
                accepted = []
                for relative in roots:
                    current = (db.execute("SELECT 1 FROM source_files WHERE relative_path=?",
                        (relative,)).fetchone() if inventory else db.execute("SELECT 1 FROM source_files f JOIN symbols s "
                        "ON s.relative_path=f.relative_path WHERE f.relative_path=? "
                        "AND s.symbol_type='Program' LIMIT 1", (relative,)).fetchone())
                    if not current:
                        frontier("concept_root_not_program", relative)
                    elif len(accepted) < root_slots:
                        accepted.append(relative)
                    else:
                        budget_frontier("concept_root_budget", relative)
                        state["pending_paths"].append({"relative_path": relative, "depth": 0})
                roots = accepted
                metadata.update(root_paths=roots, root_selection="business_concept_candidates",
                                source_identity_resolved=False)
                if not roots:
                    metadata.update(status="not_applicable", reason="concept_program_roots_not_located")
                    return result
            visited = set(state["read_candidate_paths"])
            if fresh_frontier:
                batch = [{"relative_path": path, "depth": 0} for path in roots]
            else:
                batch = state["pending_paths"][:policy.max_semantic_files]
                state["pending_paths"] = state["pending_paths"][policy.max_semantic_files:]
            pending = deque((row["relative_path"], row["depth"], None, "") for row in batch)
            queued = {row["relative_path"] for row in batch}
            deferred_paths = {row["relative_path"] for row in state["pending_paths"]}
            expansions = state["pending_expansions"][:policy.max_semantic_files]
            state["pending_expansions"] = state["pending_expansions"][policy.max_semantic_files:]
            pending.extend((row["caller_path"], row["depth"], row["relation_kind"], row["after_key"])
                           for row in expansions)
            dynamic_candidate_cache = {}
            root_set = set(roots)
            # Multiple concept entries share the ordinary allowance. The
            # extended allowance is meaningful only for one physical root.
            if len(roots) > 1:
                root_limit = min(root_limit, policy.max_source_characters)
            metadata["available_unsupplied_paths"] = []

            def enqueue(target, depth):
                if target in queued or target in visited:
                    return
                if depth_limit is not None and depth >= depth_limit:
                    budget_frontier("dependency_depth_budget", target)
                elif len(queued) >= policy.max_semantic_files:
                    budget_frontier("dependency_file_budget", target)
                    if target not in deferred_paths:
                        state["pending_paths"].append({"relative_path": target, "depth": depth + 1})
                        deferred_paths.add(target)
                else:
                    if target in deferred_paths:
                        state["pending_paths"] = [row for row in state["pending_paths"] if row["relative_path"] != target]
                        deferred_paths.remove(target)
                    queued.add(target)
                    pending.append((target, depth + 1, None, ""))

            def expansion_cursor(relative, depth, kind, after):
                state["pending_expansions"].append({"caller_path": relative, "depth": depth,
                                                   "relation_kind": kind, "after_key": after})
                frontier("dependency_navigation_pending", relative, relation_kind=kind,
                         source_available=True, cursor_available=True)

            while pending:
                cancel()
                relative, depth, expansion_kind, after_key = pending.popleft()
                row = db.execute("SELECT relative_path,sha256,encoding,line_count FROM source_files "
                                 "WHERE relative_path=?", (relative,)).fetchone()
                if row is None:
                    frontier("source_not_indexed", relative, blocking=True)
                    continue
                if expansion_kind is None:
                    records.append(dict(row))
                    metadata["candidate_paths"].append(relative)
                    if relative not in visited:
                        state["read_candidate_paths"].append(relative)
                        visited.add(relative)
                if identity.get("exclusive_source_scope"):
                    continue
                targets = db.execute("SELECT DISTINCT s.relative_path FROM relations r "
                    "JOIN symbols s ON s.symbol_id=r.target_entity_id WHERE r.relative_path=? "
                    "AND r.relation_type IN ('CALLS','INCLUDES_COPY') AND r.status='confirmed' "
                    "AND s.relative_path>? ORDER BY s.relative_path LIMIT ?",
                    (relative, after_key if expansion_kind == "confirmed" else "", policy.max_semantic_files + 1)).fetchall() if expansion_kind in {None, "confirmed"} else []
                for target_row in targets[:policy.max_semantic_files]:
                    enqueue(target_row[0], depth)
                if len(targets) > policy.max_semantic_files:
                    expansion_cursor(relative, depth, "confirmed", targets[policy.max_semantic_files - 1][0])
                unresolved = db.execute("SELECT DISTINCT r.relation_id,r.relation_type,r.target_name,r.status "
                    "FROM relations r LEFT JOIN symbols s ON s.symbol_id=r.target_entity_id "
                    "WHERE r.relative_path=? AND r.relation_id>? AND (r.relation_type='CALL_TARGET_FROM' OR "
                    "(r.relation_type IN ('CALLS','INCLUDES_COPY') AND "
                    "(r.status IS NULL OR r.status!='confirmed' OR s.relative_path IS NULL))) "
                    "ORDER BY r.relation_id LIMIT ?", (relative,
                        after_key if expansion_kind == "unresolved" else "", _FRONTIER_LIMIT + 1)).fetchall() if expansion_kind in {None, "unresolved"} else []
                for edge in unresolved[:_FRONTIER_LIMIT]:
                    if edge["relation_type"] == "CALL_TARGET_FROM":
                        nominated = dynamic_target_source_candidates(db, edge["relation_id"],
                            candidate_cache=dynamic_candidate_cache)
                        candidates = list(dict.fromkeys(item["relative_path"]
                            for item in nominated["dynamic_target_candidates"]))
                        frontier("runtime_target_unresolved", relative, target_name=edge["target_name"],
                            relation_id=edge["relation_id"], relation_type=edge["relation_type"], candidate_paths=candidates,
                            source_available=True if candidates else None, runtime_target_resolved=False,
                            dynamic_value_set_complete=False,
                            caller_start_line=nominated.get("caller_start_line"),
                            dynamic_target_candidates_truncated=nominated["dynamic_target_candidates_truncated"],
                            **dynamic_candidate_references(nominated))
                        if business_overview:
                            for candidate in candidates:
                                enqueue(candidate, depth)
                        continue
                    target = located_copies.get(edge["relation_id"]) if edge["relation_type"] == "INCLUDES_COPY" else None
                    if target and db.execute("SELECT 1 FROM source_files WHERE relative_path=?", (target,)).fetchone():
                        frontier("copy_binding_unresolved", relative, target_name=edge["target_name"],
                                 relation_type=edge["relation_type"], target_path=target, source_available=True)
                        if business_overview:
                            enqueue(target, depth)
                        continue
                    candidates = []
                    if edge["relation_type"] in {"CALLS", "INCLUDES_COPY"}:
                        symbol_type = "Program" if edge["relation_type"] == "CALLS" else "Copybook"
                        candidates = [row[0] for row in db.execute("SELECT DISTINCT s.relative_path "
                            "FROM symbols s JOIN source_files f ON f.relative_path=s.relative_path "
                            "WHERE s.symbol_type=? AND UPPER(s.name)=UPPER(?) "
                            "ORDER BY s.relative_path LIMIT ?",
                            (symbol_type, edge["target_name"], _FRONTIER_LIMIT + 1))]
                    if candidates:
                        frontier("dependency_binding_unresolved", relative,
                            target_name=edge["target_name"], relation_type=edge["relation_type"],
                            candidate_paths=candidates[:_FRONTIER_LIMIT], source_available=True,
                            candidate_paths_limited=len(candidates) > _FRONTIER_LIMIT)
                        # Do not choose an implementation from duplicate or
                        # conditionally active definitions on the model's behalf.
                        continue
                    frontier("runtime_target_unresolved" if edge["relation_type"] == "CALL_TARGET_FROM"
                             else "external_implementation_unavailable", relative,
                             target_name=edge["target_name"], relation_type=edge["relation_type"])
                if len(unresolved) > _FRONTIER_LIMIT:
                    frontier("unresolved_targets_limited", relative, minimum_omitted_count=1)
                    expansion_cursor(relative, depth, "unresolved", unresolved[_FRONTIER_LIMIT - 1]["relation_id"])
                if business_overview and expansion_kind in {None, "file_definitions"}:
                    definitions, definition_gaps = _file_definition_candidates(db, relative,
                        after_name=after_key if expansion_kind == "file_definitions" else "")
                    for gap in definition_gaps:
                        if gap["reason"] == "file_definition_names_pending":
                            expansion_cursor(relative, depth, "file_definitions", gap["after_name"])
                        else:
                            frontier(gap.pop("reason"), relative, **gap)
                    for target in definitions:
                        enqueue(target, depth)
            metadata.update(pending_path_count=len(state["pending_paths"]),
                pending_expansion_count=len(state["pending_expansions"]),
                global_candidate_count=len(state["read_candidate_paths"]),
                global_unresolved_count=len(state["unresolved_boundaries"]),
                traversal_complete=not state["pending_paths"] and not state["pending_expansions"])
            if not metadata["traversal_complete"] or state["unresolved_boundaries"]:
                metadata["closure_complete"] = False
            if records and not identity.get("exclusive_source_scope"):
                # A whole physical file cannot repair missing dependency facts.
                # Read only selected files' boundaries, scanning the stored map
                # once rather than decoding it again for every dependency.
                placeholders = ",".join("?" for _ in records)
                for relative, value in db.execute(
                        "SELECT b.key,b.value FROM metadata m,json_each(m.value) b "
                        "WHERE m.key='business_file_boundaries' AND b.key IN (" + placeholders + ")",
                        [item["relative_path"] for item in records]):
                    boundaries = json.loads(value)
                    omitted = {reason: boundaries[reason] for reason in
                               ("source_line_exceeds_fact_budget", "bounded_fact_fragment")
                               if boundaries.get(reason)}
                    if omitted:
                        frontier("source_dependency_scan_incomplete", relative,
                                 parser_boundaries=omitted)
            if fatal:
                return result

        staged, total_bytes, total_chars, ordinary_chars = [], 0, 0, 0
        captured_paths = {item["relative_path"] for item in source_session.source_manifest()}
        # The root stays first. Previously inspected dependencies receive the
        # next slots, while an oversized file cannot displace smaller sources.
        records = [item for item in records if item["relative_path"] in root_set] + sorted(
            (item for item in records if item["relative_path"] not in root_set),
            key=lambda item: item["relative_path"] not in captured_paths)
        for item in records:
            cancel()
            relative = item["relative_path"]
            is_root = relative in root_set
            character_limit = root_limit if is_root else policy.max_source_characters
            character_reason = "complete_source_characters" if is_root else "source_characters"
            physical_root = (source_session.mirror_root if relative in captured_paths
                             else source_session.source_root)
            size = _safe_file(physical_root, relative).stat().st_size
            if total_bytes + size > byte_limit:
                budget_frontier("source_byte_budget", relative, byte_limit=byte_limit)
                continue
            captured = source_session.capture(relative)
            total_bytes += captured.size
            if total_bytes > byte_limit:
                budget_frontier("source_byte_budget", relative, byte_limit=byte_limit)
                continue
            if captured.sha256 != item["sha256"]:
                frontier("source_hash_mismatch", relative, blocking=True,
                         reason_code="SOURCE_HASH_MISMATCH", needs_refresh=True,
                         captured_sha256=captured.sha256)
                return result
            # Exhaust the existing verified physical reader before trusting any
            # text. The capture can contain lines absent from the search index.
            verified = list(_verified_lines(source_session.mirror_root,
                {**item, "encoding": captured.encoding}, check_cancel, character_limit))
            if any(truncated for _, truncated in verified):
                budget_frontier(character_reason, relative)
                continue
            lines = [text for text, _ in verified]
            text = "\n".join(lines)
            if len(text) > character_limit or ((not is_root or len(roots) > 1) and
                    ordinary_chars + len(text) > policy.max_source_characters):
                budget_frontier(character_reason, relative)
                continue
            if is_root:
                metadata["root_source_characters"] += len(text)
                metadata["complete_root_budget_applied"] = (len(roots) == 1
                    and len(text) > policy.max_source_characters)
            else:
                metadata["non_root_source_characters"] += len(text)
            if not is_root or not metadata["complete_root_budget_applied"]:
                ordinary_chars += len(text)
            total_chars += len(text)
            metadata["source_characters"] = total_chars
            page = {"relative_path": relative, "start_line": 1, "end_line": len(lines),
                    "source_sha256": captured.sha256, "source_text": text,
                    "span_truncated": False, "selection_reasons": ["complete_working_set"],
                    "source_verification": "content_hash"}
            page["evidence_id"] = _identify_page(page)
            metadata["source_manifest"].append({"relative_path": relative,
                "sha256": captured.sha256, "encoding": captured.encoding,
                "line_count": len(lines), "source_characters": len(text),
                "evidence_id": page["evidence_id"] if lines else None})
            if lines:
                staged.append(page)

        # An intervening index refresh cannot mix a new dependency catalog with
        # old physical captures. Commit citations only after this final check.
        with closing(_connect(database_path, True)) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT value FROM metadata WHERE key='snapshot_id'").fetchone()
            if current is None or current[0] != snapshot:
                frontier("snapshot_changed", blocking=True, reason_code="SNAPSHOT_CHANGED")
                return result
            for item in metadata["source_manifest"]:
                row = db.execute("SELECT sha256,line_count FROM source_files WHERE relative_path=?",
                                 (item["relative_path"],)).fetchone()
                if row is None or tuple(row) != (item["sha256"], item["line_count"]):
                    frontier("snapshot_changed", item["relative_path"], blocking=True,
                             reason_code="SNAPSHOT_CHANGED")
                    return result
            for page in staged:
                _persist_page(db, page)
            db.commit()
        supplied_paths = {item["relative_path"] for item in metadata["source_manifest"]}
        metadata["available_unsupplied_paths"] = list(dict.fromkeys([
            *(path for path in metadata["candidate_paths"] if path not in supplied_paths),
            *metadata["omitted_candidate_paths"],
            *(path for gap in metadata["frontier"] if gap.get("source_available")
              for path in [*gap.get("candidate_paths", ()),
                           *([gap["target_path"]] if gap.get("target_path") else [])]
              if path not in supplied_paths)]))[:_FRONTIER_LIMIT]
        metadata.update(status=("partial" if staged else "fallback") if budget_limited else "supplied",
                        physical_complete=not budget_limited,
                        reason=metadata["reason"] if budget_limited else "candidate_source_set_supplied",
                        snapshot_id=snapshot)
        result["pages"] = staged
    except (OSError, UnicodeError, ValueError, sqlite3.Error) as exc:
        frontier("source_read_unavailable", blocking=True, error_type=type(exc).__name__)
    return result
