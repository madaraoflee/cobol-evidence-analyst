"""Stable, complete indexed-object results kept outside model context."""

from __future__ import annotations

from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from business_map import _call_graph, _impact_paths


_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_$#@-]{1,127}")


def _result_path(database_path, handle):
    if not re.fullmatch(r"[a-f0-9]{32}", handle):
        raise ValueError("IMPACT_HANDLE_INVALID")
    return Path(database_path).resolve().parent / "impact-results" / (handle + ".json")


def list_impact(database_path, identifier, *, page_size=100):
    if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier):
        raise ValueError("IMPACT_IDENTIFIER_INVALID")
    with closing(sqlite3.connect(database_path)) as db:
        db.row_factory = sqlite3.Row
        snapshot = db.execute("SELECT value FROM metadata WHERE key='snapshot_id'").fetchone()[0]
        phrase = '"' + identifier.replace('"', '""') + '"'
        direct = {row[0] for row in db.execute(
            "SELECT DISTINCT p.relative_path FROM repo_fts JOIN repo_pages p ON p.page_id=repo_fts.rowid "
            "WHERE repo_fts MATCH ?", (phrase,))}
        # A source text hit is a candidate. Only explicit indexed relations or
        # rule field facts raise its match class to statement reference.
        statement = {row[0] for row in db.execute(
            "SELECT DISTINCT relative_path FROM relations WHERE target_name=? COLLATE NOCASE", (identifier,))}
        statement.update(row[0] for row in db.execute(
            "SELECT DISTINCT b.relative_path FROM business_rules b JOIN business_rule_fields f "
            "ON f.rule_id=b.rule_id WHERE f.field_name=? COLLATE NOCASE", (identifier,)))
        seeds = direct | statement
        incoming, edges = _call_graph(db, None) if seeds else ({}, [])
        distance = _impact_paths(incoming, seeds, None) if seeds else {}
        routes = {}
        for edge in sorted(edges, key=lambda row: (row["caller_path"], row.get("target_path") or "", row["relation_id"])):
            caller, target = edge["caller_path"], edge.get("target_path")
            if target in distance and distance.get(caller) == distance[target] + 1:
                routes.setdefault(caller, edge)
        rows = []
        for path in sorted(distance):
            file = db.execute("SELECT artifact_kind,sha256 FROM source_files WHERE relative_path=?", (path,)).fetchone()
            if file is None:
                continue
            programs = [dict(row) for row in db.execute(
                "SELECT unit_id,name,start_line FROM code_units WHERE relative_path=? AND unit_type='Program' ORDER BY start_line", (path,))]
            match = ("statement_reference" if path in statement else "text_match" if path in direct
                     else "static_caller_candidate")
            relation_path = []
            cursor, seen = path, set()
            while cursor in routes and cursor not in seen:
                seen.add(cursor)
                edge = routes[cursor]
                relation_path.append({"relation_id": edge["relation_id"],
                    "caller_path": edge["caller_path"], "target_path": edge["target_path"],
                    "relation_type": edge["relation_type"], "resolution": edge["resolution"]})
                cursor = edge["target_path"]
            if programs:
                for program in programs:
                    rows.append({"type": "program", "id": program["unit_id"], "name": program["name"],
                                 "relative_path": path, "source_sha256": file["sha256"],
                                 "match_type": match, "start_line": program["start_line"],
                                 "static_relation_path": relation_path})
            elif file["artifact_kind"] == "copybook":
                rows.append({"type": "copybook", "id": path, "name": Path(path).name,
                             "relative_path": path, "source_sha256": file["sha256"],
                             "match_type": match, "static_relation_path": relation_path})
            else:
                rows.append({"type": "file", "id": path, "name": Path(path).name,
                             "relative_path": path, "source_sha256": file["sha256"],
                             "match_type": match, "static_relation_path": relation_path})
        for relation in db.execute("SELECT relation_id,relative_path,target_name,relation_type,status "
                "FROM relations WHERE target_name=? COLLATE NOCASE AND status!='confirmed' "
                "AND relation_type IN ('CALLS','CALL_TARGET_FROM')", (identifier,)):
            rows.append({"type": "external_target", "id": relation["relation_id"],
                         "name": relation["target_name"], "relative_path": relation["relative_path"],
                         "match_type": "unresolved_static_relation", "resolution": relation["status"]})
    rows.sort(key=lambda row: (row["type"], row.get("relative_path", ""), row["id"]))
    handle = hashlib.sha256((snapshot + "\0" + identifier.upper()).encode()).hexdigest()[:32]
    path = _result_path(database_path, handle)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"schema_version": "indexed-impact-results/v1", "handle": handle,
                "snapshot_id": snapshot, "identifier": identifier,
                "scope": "indexed_text_and_static_relations", "rows": rows}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return impact_page(database_path, handle, cursor=0, page_size=page_size)


def impact_page(database_path, handle, *, cursor=0, page_size=100):
    if type(cursor) is not int or cursor < 0 or type(page_size) is not int or not 1 <= page_size <= 500:
        raise ValueError("IMPACT_CURSOR_INVALID")
    document = json.loads(_result_path(database_path, handle).read_text(encoding="utf-8"))
    rows = document["rows"]
    if cursor > len(rows):
        raise ValueError("IMPACT_CURSOR_INVALID")
    counts = dict(Counter(row["type"] for row in rows))
    return {key: document[key] for key in ("schema_version", "handle", "snapshot_id", "identifier", "scope")} | {
        "counts": counts, "total": len(rows), "cursor": cursor,
        "next_cursor": cursor + page_size if cursor + page_size < len(rows) else None,
        "rows": rows[cursor:cursor + page_size]}


def impact_jsonl(database_path, handle):
    document = json.loads(_result_path(database_path, handle).read_text(encoding="utf-8"))
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in document["rows"])
