"""Selected-file structural sidecar and source-mapped business evidence."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import shutil
import os
import sqlite3
import tempfile

from evidence_context import EvidenceGroup, Observation, SourceRef
from source_reading import _identify_page
from source_session import archive_evidence
from structural_index import PARSER_VERSION, build_structural_index
from procedure_expansion import expand_program


SEMANTIC_VERSION = "selected-structural-v1"
_ACTIVE = set()


def _prune_cache(directory, limit):
    files = sorted((path for path in directory.glob("*.sqlite")
                    if path not in _ACTIVE and not path.name.endswith(".building.sqlite")),
                   key=lambda path: path.stat().st_mtime_ns)
    size = sum(path.stat().st_size for path in directory.glob("*.sqlite"))
    for path in files:
        if size <= limit:
            break
        amount = path.stat().st_size
        path.unlink(missing_ok=True)
        path.with_suffix(".map.json").unlink(missing_ok=True)
        size -= amount


@dataclass
class SemanticScope:
    database_path: Path
    scope_key: str
    input_manifest: list[dict]
    derived_manifest: list[dict]
    source_map: dict
    frontier: list[dict]
    mirror_root: Path
    cache_hit: bool
    source_session: object
    _temporary: object = field(repr=False)
    _line_cache: dict = field(default_factory=dict, repr=False)
    cache_limit: int = 536870912

    def close(self):
        _ACTIVE.discard(self.database_path)
        self._temporary.cleanup()
        _prune_cache(self.database_path.parent, self.cache_limit)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _select_paths(repository_database, anchors, requested_calls, policy):
    paths, frontier, bytes_estimate = [], [], 0
    with closing(sqlite3.connect(repository_database)) as db:
        db.row_factory = sqlite3.Row
        pending = [a["relative_path"] for a in anchors if a.get("relative_path")]
        pending += list(requested_calls)
        visited = set()
        while pending:
            relative = pending.pop(0)
            if relative in visited:
                continue
            visited.add(relative)
            row = db.execute("SELECT relative_path FROM source_files WHERE relative_path=?", (relative,)).fetchone()
            if row is None:
                frontier.append({"relative_path": relative, "reason": "source_not_indexed"})
                continue
            if len(paths) >= policy.max_semantic_files:
                frontier.append({"relative_path": relative, "reason": "file_budget"})
                continue
            # SQL metadata is allowed here; no unrelated file is opened.
            info = db.execute("SELECT size FROM repo_source_state WHERE relative_path=?", (relative,)).fetchone()
            size = info[0] if info else 0
            if bytes_estimate + size > policy.max_semantic_source_bytes:
                frontier.append({"relative_path": relative, "reason": "byte_budget"})
                continue
            paths.append(relative)
            bytes_estimate += size
            for target, status, kind in db.execute(
                "SELECT s.relative_path,r.status,r.relation_type FROM relations r "
                "LEFT JOIN symbols s ON s.symbol_id=r.target_entity_id "
                "WHERE r.relative_path=? AND r.relation_type IN ('CALLS','INCLUDES_COPY') "
                "ORDER BY r.relation_type,r.target_name", (relative,)):
                if status == "confirmed" and target and target != relative and target not in visited:
                    pending.append(target)
                elif status != "confirmed":
                    frontier.append({"relative_path": relative, "relation_type": kind,
                                     "reason": status or "unresolved"})
    return paths, frontier


def _prepare_semantic_scope(repository_database, source_session, *, anchors,
                            requested_calls=(), policy, check_cancel=None, temporary):
    paths, frontier = _select_paths(repository_database, anchors, requested_calls, policy)
    if not paths:
        raise ValueError("SEMANTIC_SCOPE_EMPTY")
    root = Path(temporary.name)
    input_manifest = []
    actual_bytes = 0
    for relative in list(paths):
        if check_cancel:
            check_cancel()
        captured = source_session.capture(relative)
        if actual_bytes + captured.size > policy.max_semantic_source_bytes:
            paths.remove(relative)
            frontier.append({"relative_path": relative, "reason": "byte_budget"})
            continue
        actual_bytes += captured.size
        input_manifest.append({"relative_path": relative, "sha256": captured.sha256,
                               "encoding": captured.encoding, "source_format": captured.source_format})
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(captured.path, destination)
    if not paths:
        raise ValueError("SEMANTIC_SCOPE_BYTE_BUDGET")
    source_map = {}
    derived_manifest = list(input_manifest)
    # The complete SQL catalog decides ambiguity; only an inclusion target is
    # captured by the provider. No source-directory enumeration is involved.
    with closing(sqlite3.connect(repository_database)) as db:
        catalog = [row[0] for row in db.execute("SELECT relative_path FROM source_files")]
        programs = {row[0]: row[1] for row in db.execute(
            "SELECT relative_path,name FROM code_units WHERE unit_type='Program'")}
    expansions = 0
    for relative in list(paths):
        if expansions >= policy.max_semantic_expansions or relative not in programs:
            continue
        with closing(sqlite3.connect(repository_database)) as db:
            has_copy = db.execute("SELECT 1 FROM relations WHERE relative_path=? AND relation_type='INCLUDES_COPY' LIMIT 1", (relative,)).fetchone()
        if not has_copy:
            continue
        expansion = expand_program(source_session.mirror_root, programs[relative],
            source_provider=source_session, source_catalog=catalog,
            entry_relative_path=relative, max_depth=policy.max_semantic_expansions,
            max_lines=max(20000, policy.max_semantic_source_bytes // 32))
        frontier.extend(expansion["boundaries"])
        if not expansion["source_expansion_complete"] or not expansion["includes"]:
            continue
        included = [entry["relative_path"] for entry in expansion["source_files"]]
        extra = [child for child in included if child not in paths]
        extra_captures = [source_session.capture(child) for child in extra]
        if (len(paths) + len(extra) > policy.max_semantic_files or
                actual_bytes + sum(item.size for item in extra_captures) > policy.max_semantic_source_bytes):
            frontier.append({"relative_path": relative, "reason": "expansion_scope_budget"})
            continue
        for child in included:
            if child not in paths:
                captured = source_session.capture(child)
                actual_bytes += captured.size
                paths.append(child)
                input_manifest.append({"relative_path": child, "sha256": captured.sha256,
                                       "encoding": captured.encoding, "source_format": captured.source_format})
                derived_manifest.append({"relative_path": child, "sha256": captured.sha256,
                                         "encoding": captured.encoding, "source_format": captured.source_format})
                target = root / child
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(captured.path, target)
        # Normalized free-form text has one explicit physical provenance record
        # per derived line. The derived hash is never reported as a host hash.
        content = "\n".join(line["code"] for line in expansion["lines"]) + "\n"
        (root / relative).write_text(content, encoding="utf-8")
        source_map[relative] = [{"origin": item["origin"], "include_chain": item["include_chain"]}
                                for item in expansion["lines"]]
        derived_manifest = [item for item in derived_manifest if item["relative_path"] != relative]
        derived_manifest.append({"relative_path": relative,
                                 "sha256": hashlib.sha256(content.encode()).hexdigest(),
                                 "encoding": "utf-8", "source_format": "free"})
        expansions += 1
    # All derived inputs use a fixed parser mode. The source map disambiguates
    # original and derived positions and is included in the cache identity.
    digest = hashlib.sha256(json.dumps({"root": str(source_session.source_root),
        "input": sorted(input_manifest, key=lambda x: x["relative_path"]),
        "derived": sorted(derived_manifest, key=lambda x: x["relative_path"]),
        "map": source_map, "parser": PARSER_VERSION, "semantic": SEMANTIC_VERSION},
        sort_keys=True).encode()).hexdigest()
    cache = Path(repository_database).resolve().parent / "semantic-scopes"
    cache.mkdir(parents=True, exist_ok=True)
    database = cache / f"{digest}.sqlite"
    warm = database.is_file()
    if not warm:
        temporary_database = cache / f"{digest}.building.sqlite"
        try:
            build_structural_index(root, temporary_database, include_paths=paths,
                encoding="auto", source_format="auto",
                source_options_by_path={item["relative_path"]: {
                    "encoding": item["encoding"], "source_format": item["source_format"]}
                    for item in derived_manifest},
                verify_content=True, check_cancel=check_cancel)
            temporary_database.replace(database)
            (cache / f"{digest}.map.json").write_text(json.dumps(source_map), encoding="utf-8")
        finally:
            temporary_database.unlink(missing_ok=True)
    else:
        map_path = cache / f"{digest}.map.json"
        if not map_path.is_file() or json.loads(map_path.read_text(encoding="utf-8")) != source_map:
            raise ValueError("SEMANTIC_SOURCE_MAP_MISMATCH")
    os.utime(database, None)
    _ACTIVE.add(database)
    _prune_cache(cache, policy.semantic_cache_bytes)
    return SemanticScope(database, digest, input_manifest, derived_manifest,
                         source_map, frontier, root, warm, source_session, temporary,
                         cache_limit=policy.semantic_cache_bytes)


def prepare_semantic_scope(repository_database, source_session, *, anchors,
                           requested_calls=(), policy, check_cancel=None):
    temporary = tempfile.TemporaryDirectory(prefix="semantic-scope-")
    try:
        return _prepare_semantic_scope(repository_database, source_session,
            anchors=anchors, requested_calls=requested_calls, policy=policy,
            check_cancel=check_cancel, temporary=temporary)
    except BaseException:
        temporary.cleanup()
        raise


def _source_pages(scope, unit):
    relative = unit["relative_path"]
    mapped = scope.source_map.get(relative)
    if mapped:
        origins = [mapped[index - 1] for index in range(unit["start_line"], unit["end_line"] + 1)
                   if 1 <= index <= len(mapped)]
    else:
        origins = [{"origin": {"relative_path": relative, "line": index,
                                "source_hash": scope.source_session.capture(relative).sha256},
                    "include_chain": []}
                   for index in range(unit["start_line"], unit["end_line"] + 1)]
    groups = []
    for item in origins:
        origin = item["origin"]
        chain = item["include_chain"]
        if groups and groups[-1][0] == origin["relative_path"] and groups[-1][1] == chain and groups[-1][3] + 1 == origin["line"]:
            groups[-1][3] = origin["line"]
        else:
            groups.append([origin["relative_path"], chain, origin["line"], origin["line"]])
    pages = []
    for path, chain, start, end in groups:
        captured = scope.source_session.capture(path)
        lines = scope._line_cache.setdefault(path, captured.path.read_text(encoding=captured.encoding).splitlines())
        text = "\n".join(lines[start - 1:end])
        page = {"relative_path": path, "start_line": start, "end_line": end,
                "source_sha256": captured.sha256, "source_text": text,
                "include_chain": chain, "selection_reasons": ["business_context"]}
        page["evidence_id"] = _identify_page(page)
        pages.append(page)
    return pages


def build_business_evidence(scope, source_session, *, anchor, focus_fields=(),
                            purpose="explain", policy):
    """Keep exact units and conservative links around one selected anchor."""
    path, line = anchor["relative_path"], int(anchor["line"])
    selected, roles, bases = {}, {}, {}
    frontier = list(scope.frontier)

    def bounded(db, query, arguments, limit, reason, relative):
        candidates = db.execute(query + " LIMIT ?", (*arguments, limit + 1)).fetchall()
        if len(candidates) > limit:
            count = db.execute("SELECT COUNT(*) FROM (" + query + ")", arguments).fetchone()[0]
            frontier.append({"reason": reason, "relative_path": relative,
                "candidate_count": count, "omitted_count": count - limit, "limit": limit,
                "interpretation_basis": "conservative_relation_candidates_not_execution_proof"})
        return candidates[:limit]

    with closing(sqlite3.connect(scope.database_path)) as db:
        db.row_factory = sqlite3.Row
        locations = [(path, line)]
        for host_path, mapped in scope.source_map.items():
            locations.extend((host_path, i) for i, item in enumerate(mapped, 1)
                if item["origin"]["relative_path"] == path and item["origin"]["line"] == line)
        for unit_path, anchor_line in locations:
            row = db.execute("SELECT * FROM code_units WHERE relative_path=? AND unit_type='Statement' "
                "AND start_line<=? AND end_line>=? ORDER BY end_line-start_line LIMIT 1",
                (unit_path, anchor_line, anchor_line)).fetchone()
            if row:
                selected[row["unit_id"]] = row
                roles[row["unit_id"]] = "result" if row["name"] in {"COMPUTE", "MOVE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE"} else "callsite" if row["name"] == "CALL" else "anchor"
        if not selected:
            return EvidenceGroup("group_" + scope.scope_key[:12], anchor, purpose,
                                 open_frontier=[{"reason": "anchor_not_structurally_parsed", **anchor}])
        anchor_units = list(selected)
        for unit_id in anchor_units:
            for relation in db.execute("SELECT * FROM relations WHERE from_entity_id=?", (unit_id,)):
                if relation["relation_type"] == "CONTROL_DEPENDS_ON" and relation["target_entity_id"]:
                    row = db.execute("SELECT * FROM code_units WHERE unit_id=?", (relation["target_entity_id"],)).fetchone()
                    if row:
                        selected[row["unit_id"]], roles[row["unit_id"]] = row, "condition"
                if relation["relation_type"] in {"READS", "WRITES"} and relation["target_entity_id"]:
                    symbol = db.execute("SELECT * FROM symbols WHERE symbol_id=?", (relation["target_entity_id"],)).fetchone()
                    if symbol:
                        declaration = db.execute("SELECT * FROM code_units WHERE unit_id=?", (symbol["definition_unit_id"],)).fetchone()
                        if declaration:
                            selected[declaration["unit_id"]], roles[declaration["unit_id"]] = declaration, "declaration"
                        # Nearby prior writes are candidates, not reaching definitions.
                        for linked in bounded(db,
                                "SELECT u.*,r.relation_type FROM relations r JOIN code_units u "
                                "ON u.unit_id=r.from_entity_id WHERE r.target_entity_id=? "
                                "AND r.relation_type IN ('READS','WRITES') "
                                "ORDER BY CASE WHEN u.unit_id=? THEN 0 "
                                "WHEN r.relation_type='WRITES' AND u.start_line<=? THEN 1 ELSE 2 END,"
                                "ABS(u.start_line-?),u.start_line,u.unit_id",
                                (symbol["symbol_id"], unit_id, selected[unit_id]["start_line"],
                                 selected[unit_id]["start_line"]), 12,
                                "same_symbol_candidates_limited", selected[unit_id]["relative_path"]):
                            selected[linked["unit_id"]] = linked
                            if linked["unit_id"] in anchor_units:
                                continue
                            candidate_role = ("input" if relation["relation_type"] == "READS" and
                                linked["relation_type"] == "WRITES" else "return_processing" if
                                relation["relation_type"] == "WRITES" and linked["relation_type"] == "READS"
                                else "related_statement")
                            roles.setdefault(linked["unit_id"], candidate_role)
                            bases[linked["unit_id"]] = "same_symbol_candidate_not_reaching_definition"
            call = selected[unit_id]
            if call["name"] == "CALL":
                for binding in db.execute("SELECT * FROM call_bindings WHERE callsite_id=?", (unit_id,)):
                    for column, role in (("caller_symbol_id", "input"), ("callee_symbol_id", "parameter")):
                        symbol_id = binding[column]
                        if symbol_id:
                            symbol = db.execute("SELECT definition_unit_id FROM symbols WHERE symbol_id=?", (symbol_id,)).fetchone()
                            if symbol:
                                declaration = db.execute("SELECT * FROM code_units WHERE unit_id=?", (symbol[0],)).fetchone()
                                if declaration:
                                    selected[declaration["unit_id"]], roles[declaration["unit_id"]] = declaration, role
                            for linked in bounded(db, "SELECT u.*,r.relation_type FROM relations r "
                                    "JOIN code_units u ON u.unit_id=r.from_entity_id "
                                    "WHERE r.target_entity_id=? AND r.relation_type IN ('READS','WRITES') "
                                    "ORDER BY ABS(u.start_line-?),u.start_line,u.unit_id",
                                    (symbol_id, call["start_line"]), 16,
                                    "binding_candidates_limited", call["relative_path"]):
                                selected[linked["unit_id"]] = linked
                                if linked["unit_id"] not in anchor_units:
                                    roles.setdefault(linked["unit_id"], "input" if linked["relation_type"] == "WRITES" else "return_processing")
                                    bases[linked["unit_id"]] = "same_symbol_candidate_not_reaching_definition"
            # Neighbouring control and return checks are candidates, never
            # proof that one physical line follows at runtime.
            nearby = bounded(db, "SELECT * FROM code_units WHERE relative_path=? AND program_name=? AND unit_type='Statement' "
                "AND start_line BETWEEN ? AND ? ORDER BY ABS(start_line-?),start_line,unit_id",
                (call["relative_path"], call["program_name"], max(1, call["start_line"] - 12), call["end_line"] + 16,
                 call["start_line"]), 30, "nearby_candidates_limited", call["relative_path"])
            for row in nearby:
                if row["name"] in {"IF", "ELSE", "EVALUATE", "WHEN", "ON SIZE ERROR", "CALL", "MOVE", "COMPUTE", "END-IF"}:
                    selected[row["unit_id"]] = row
                    roles.setdefault(row["unit_id"], "condition" if row["name"] in {"IF", "ELSE", "EVALUATE", "WHEN"} else "return_processing")
        for call in list(selected.values()):
            if call["unit_type"] != "Statement" or call["name"] != "CALL":
                continue
            for relation in db.execute("SELECT r.status,s.relative_path,s.name FROM relations r "
                    "LEFT JOIN symbols s ON s.symbol_id=r.target_entity_id "
                    "WHERE r.from_entity_id=? AND r.relation_type='CALLS'", (call["unit_id"],)):
                if relation["status"] != "confirmed" or not relation["relative_path"]:
                    continue
                for callee in bounded(db, "SELECT * FROM code_units WHERE relative_path=? "
                        "AND unit_type='Statement' AND name IN ('COMPUTE','MOVE','IF','EVALUATE','WHEN') "
                        "ORDER BY start_line", (relation["relative_path"],), 24,
                        "callee_candidates_limited", relation["relative_path"]):
                    selected[callee["unit_id"]] = callee
                    roles.setdefault(callee["unit_id"], "callee_processing")
        # Reach the caller of a computed result and the caller of a paragraph.
        # Static links nominate source to read; they do not assert execution.
        frontier_units = list(selected)
        if len(frontier_units) > 32:
            frontier.append({"reason": "caller_expansion_candidates_limited", "candidate_count": len(frontier_units),
                             "omitted_count": len(frontier_units) - 32, "limit": 32})
        for unit_id in frontier_units[:32]:
            unit = selected[unit_id]
            if unit["unit_type"] not in {"Statement", "Paragraph"}:
                continue
            targets = []
            if unit["program_name"]:
                targets.extend(row[0] for row in db.execute("SELECT symbol_id FROM symbols "
                    "WHERE symbol_type='Program' AND name=? AND relative_path=?",
                    (unit["program_name"], unit["relative_path"])))
            if unit["parent_unit_id"]:
                targets.extend(row[0] for row in db.execute("SELECT symbol_id FROM symbols "
                    "WHERE definition_unit_id=? AND symbol_type='Paragraph'", (unit["parent_unit_id"],)))
            for target in targets:
                for caller in bounded(db, "SELECT u.* FROM relations r JOIN code_units u ON u.unit_id=r.from_entity_id "
                        "WHERE r.target_entity_id=? AND r.relation_type IN ('CALLS','PERFORMS','PERFORMS_THRU') "
                        "ORDER BY u.relative_path,u.start_line", (target,), 8,
                        "caller_candidates_limited", unit["relative_path"]):
                    selected[caller["unit_id"]], roles[caller["unit_id"]] = caller, "callsite"
                    for nearby in bounded(db, "SELECT * FROM code_units WHERE relative_path=? AND program_name=? "
                            "AND unit_type='Statement' AND start_line BETWEEN ? AND ? "
                            "ORDER BY ABS(start_line-?),start_line,unit_id", (caller["relative_path"], caller["program_name"],
                            max(1, caller["start_line"] - 12), caller["end_line"] + 24, caller["start_line"]),
                            24, "caller_nearby_candidates_limited", caller["relative_path"]):
                        if nearby["name"] in {"IF", "ELSE", "EVALUATE", "WHEN", "MOVE", "COMPUTE", "CALL", "PERFORM", "END-IF"}:
                            selected[nearby["unit_id"]] = nearby
                            roles.setdefault(nearby["unit_id"], "condition" if nearby["name"] in {"IF", "EVALUATE", "WHEN", "ELSE"} else "return_processing")
        observations, supplied = [], []
        ordered = sorted(selected.values(), key=lambda row: (
            row["unit_id"] not in anchor_units,
            roles.get(row["unit_id"]) not in {"result", "condition", "input", "parameter"},
            row["relative_path"] != path, abs(row["start_line"] - line), row["unit_id"]))
        for omitted in ordered[80:]:
            frontier.append({"reason": "observation_budget", "relative_path": omitted["relative_path"],
                "start_line": omitted["start_line"], "end_line": omitted["end_line"], "limit": 80})
        for unit in ordered[:80]:
            pages = _source_pages(scope, unit)
            refs = []
            for page in pages:
                page["semantic_roles"] = [roles.get(unit["unit_id"], "related_statement")]
                page["interpretation_basis"] = ("source_observation" if unit["unit_id"] in anchor_units else
                    bases.get(unit["unit_id"], "conservative_relation_candidate"))
                refs.append(SourceRef("source", page["relative_path"], page["source_sha256"],
                    page["start_line"], page["end_line"], hashlib.sha256(page["source_text"].encode()).hexdigest(),
                    page["evidence_id"], tuple(json.dumps(x, sort_keys=True) for x in page.get("include_chain", []))))
                supplied.append(page)
            observations.append(Observation(unit["unit_id"], roles.get(unit["unit_id"], "related_statement"), refs,
                interpretation_basis="source_observation" if unit["unit_id"] in anchor_units
                                     else bases.get(unit["unit_id"], "conservative_relation_candidate")))
    archive_evidence(source_session.database_path, supplied)
    return EvidenceGroup("group_" + hashlib.sha256((scope.scope_key + str(anchor)).encode()).hexdigest()[:16],
        anchor, purpose, observations, supplied_locations=supplied, open_frontier=frontier)
