"""Supply a bounded, physical source set before selecting smaller excerpts."""

from collections import deque
from contextlib import closing
import sqlite3

from agent_policy import resolve_agent_policy
from repository_discovery import _connect, _fast_snapshot
from source_reading import _identify_page, _persist_page, _safe_file, _verified_lines


_DEPTH_LIMIT = 4
_FRONTIER_LIMIT = 32
_ROOT_KINDS = {"program", "relative_path", "basename", "stem"}


def build_complete_working_set(database_path, source_session, business_map, policy,
                              *, check_cancel=None):
    """Prefer the complete root, then dependencies under the ordinary allowance.

    Physical completeness describes only candidate_paths. Static dependencies
    and original text do not prove execution, parameter binding or value flow.
    """
    policy = resolve_agent_policy(policy)
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
        "depth_limit": _DEPTH_LIMIT, "reason": "source_identity_not_eligible"}
    result = {"pages": [], "metadata": metadata}
    if (source_session is None
            or identity.get("status") != "resolved" or identity.get("kind") not in _ROOT_KINDS
            or len(roots) != 1):
        return result
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
    try:
        with closing(_connect(database_path)) as db:
            db.execute("BEGIN")
            _, overview = _fast_snapshot(db, source_session.source_root)
            snapshot = overview["snapshot_id"]
            if business_map.get("snapshot_id") != snapshot:
                frontier("snapshot_changed", blocking=True, reason_code="SNAPSHOT_CHANGED")
                return result
            pending, queued = deque([(roots[0], 0)]), {roots[0]}
            while pending:
                cancel()
                relative, depth = pending.popleft()
                row = db.execute("SELECT relative_path,sha256,encoding,line_count FROM source_files "
                                 "WHERE relative_path=?", (relative,)).fetchone()
                if row is None:
                    frontier("source_not_indexed", relative, blocking=True)
                    continue
                records.append(dict(row))
                metadata["candidate_paths"].append(relative)
                targets = db.execute("SELECT DISTINCT s.relative_path FROM relations r "
                    "JOIN symbols s ON s.symbol_id=r.target_entity_id WHERE r.relative_path=? "
                    "AND r.relation_type IN ('CALLS','INCLUDES_COPY') AND r.status='confirmed' "
                    "ORDER BY s.relative_path LIMIT ?", (relative, policy.max_semantic_files + 1)).fetchall()
                for target_row in targets:
                    target = target_row[0]
                    if target in queued:
                        continue
                    if depth >= _DEPTH_LIMIT:
                        budget_frontier("dependency_depth_budget", target)
                    elif len(queued) >= policy.max_semantic_files:
                        budget_frontier("dependency_file_budget", target)
                    else:
                        queued.add(target)
                        pending.append((target, depth + 1))
                unresolved = db.execute("SELECT DISTINCT r.relation_type,r.target_name,r.status "
                    "FROM relations r LEFT JOIN symbols s ON s.symbol_id=r.target_entity_id "
                    "WHERE r.relative_path=? AND (r.relation_type='CALL_TARGET_FROM' OR "
                    "(r.relation_type IN ('CALLS','INCLUDES_COPY') AND "
                    "(r.status IS NULL OR r.status!='confirmed' OR s.relative_path IS NULL))) "
                    "ORDER BY r.relation_type,r.target_name LIMIT ?", (relative, _FRONTIER_LIMIT + 1)).fetchall()
                for edge in unresolved[:_FRONTIER_LIMIT]:
                    frontier("runtime_target_unresolved" if edge["relation_type"] == "CALL_TARGET_FROM"
                             else "external_implementation_unavailable", relative,
                             target_name=edge["target_name"], relation_type=edge["relation_type"])
                if len(unresolved) > _FRONTIER_LIMIT:
                    frontier("unresolved_targets_limited", relative, minimum_omitted_count=1)
            if fatal:
                return result

        staged, total_bytes, total_chars, ordinary_chars = [], 0, 0, 0
        captured_paths = {item["relative_path"] for item in source_session.source_manifest()}
        # The root stays first. Previously inspected dependencies receive the
        # next slots, while an oversized file cannot displace smaller sources.
        records = records[:1] + sorted(records[1:],
            key=lambda item: item["relative_path"] not in captured_paths)
        for item in records:
            cancel()
            relative = item["relative_path"]
            is_root = relative == roots[0]
            character_limit = root_limit if is_root else policy.max_source_characters
            character_reason = "complete_source_characters" if is_root else "source_characters"
            physical_root = (source_session.mirror_root if relative in captured_paths
                             else source_session.source_root)
            size = _safe_file(physical_root, relative).stat().st_size
            if total_bytes + size > byte_limit:
                budget_frontier("source_byte_budget", relative, byte_limit=byte_limit)
                if relative == roots[0]:
                    return result
                continue
            captured = source_session.capture(relative)
            total_bytes += captured.size
            if total_bytes > byte_limit:
                budget_frontier("source_byte_budget", relative, byte_limit=byte_limit)
                if relative == roots[0]:
                    return result
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
                if relative == roots[0]:
                    return result
                continue
            lines = [text for text, _ in verified]
            text = "\n".join(lines)
            if len(text) > character_limit or (not is_root and
                    ordinary_chars + len(text) > policy.max_source_characters):
                budget_frontier(character_reason, relative)
                if relative == roots[0]:
                    return result
                continue
            if is_root:
                metadata["root_source_characters"] = len(text)
                metadata["complete_root_budget_applied"] = len(text) > policy.max_source_characters
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
        metadata.update(status="partial" if budget_limited else "supplied",
                        physical_complete=not budget_limited,
                        reason=metadata["reason"] if budget_limited else "candidate_source_set_supplied",
                        snapshot_id=snapshot)
        result["pages"] = staged
    except (OSError, UnicodeError, ValueError, sqlite3.Error) as exc:
        frontier("source_read_unavailable", blocking=True, error_type=type(exc).__name__)
    return result
