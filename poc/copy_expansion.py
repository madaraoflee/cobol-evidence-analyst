"""Materialize bounded, scope-specific data COPY definitions with provenance.

These are definition bindings, not shared storage or CALL parameter mappings.
Only unmodified data COPY is expanded. Incomplete expansion prevents definitive
field resolution in the affected program; it never imports names globally.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict


MAX_COPY_DEPTH = 8
MAX_COPY_FIELDS_PER_PROGRAM = 10_000
MAX_COPY_INCLUSIONS_PER_PROGRAM = 1_000


def _id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def clear_copy_expansions(connection: sqlite3.Connection) -> None:
    """Remove only generated facts, before changing any source-file facts."""
    connection.execute(
        "DELETE FROM relations WHERE relation_type = 'CONTAINS' "
        "AND target_entity_id IN (SELECT unit_id FROM copy_expansions)"
    )
    connection.execute(
        "DELETE FROM code_units_fts WHERE unit_id IN "
        "(SELECT unit_id FROM copy_expansions)"
    )
    connection.execute(
        "DELETE FROM symbols WHERE symbol_id IN "
        "(SELECT symbol_id FROM copy_expansions)"
    )
    connection.execute(
        "DELETE FROM code_units WHERE unit_id IN "
        "(SELECT unit_id FROM copy_expansions)"
    )
    connection.execute("DELETE FROM copy_expansions")
    connection.execute("DELETE FROM copy_scope_boundaries")


def rebuild_copy_expansions(connection: sqlite3.Connection) -> dict[str, object]:
    """Rebind from the full snapshot, including unchanged consuming programs."""
    copies: dict[str, list[sqlite3.Row]] = defaultdict(list)
    fields: dict[str, list[sqlite3.Row]] = defaultdict(list)
    includes: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    replacement_files: dict[str, str] = {}
    for row in connection.execute(
        "SELECT relative_path, normalized_text, evidence_id, name FROM code_units "
        "WHERE unit_type IN ('Statement', 'DataItem', 'Condition') AND name != 'EXEC_SQL'"
    ):
        code = re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", " ", row["normalized_text"])
        if (
            re.search(r"(?<![A-Z0-9_$#@-])REPLACE(?![A-Z0-9_$#@-])", code, re.IGNORECASE)
            or (row["name"] != "COPY" and re.search(
                r"(?<![A-Z0-9_$#@-])COPY(?![A-Z0-9_$#@-])", code, re.IGNORECASE
            ))
        ):
            replacement_files[row["relative_path"]] = row["evidence_id"]
    for row in connection.execute(
        "SELECT * FROM symbols WHERE symbol_type = 'Copybook' ORDER BY relative_path"
    ):
        copies[row["name"]].append(row)
    for row in connection.execute(
        "SELECT s.*, u.start_line, u.end_line, u.normalized_text, "
        "u.content_hash, u.parse_status FROM symbols s JOIN code_units u "
        "ON u.unit_id = s.definition_unit_id "
        "WHERE s.symbol_type IN ('Field', 'ConditionName') "
        "ORDER BY s.relative_path, u.start_line"
    ):
        fields[row["relative_path"]].append(row)
    for row in connection.execute(
        "SELECT r.*, u.program_name FROM relations r JOIN code_units u "
        "ON u.unit_id = r.from_entity_id WHERE r.relation_type = 'INCLUDES_COPY' "
        "ORDER BY r.relative_path, u.start_line"
    ):
        includes[(row["relative_path"], row["program_name"])].append(row)

    bound_count = 0
    programs = list(connection.execute(
        "SELECT * FROM symbols WHERE symbol_type = 'Program' ORDER BY relative_path"
    ))
    for program in programs:
        pending = [
            (edge, (), ()) for edge in reversed(includes.get(
                (program["relative_path"], program["program_name"]), []
            ))
        ]
        program_count = 0
        inclusion_count = 0

        def boundary(reason: str, evidence_id: str) -> None:
            connection.execute(
                "INSERT INTO copy_scope_boundaries VALUES (?, ?, ?, ?)",
                (program["program_name"], program["relative_path"], reason, evidence_id),
            )

        if program["relative_path"] in replacement_files:
            boundary("copy_form_not_supported", replacement_files[program["relative_path"]])
            continue

        while pending:
            edge, ancestors, path = pending.pop()
            path = (*path, edge["relation_id"])
            inclusion_count += 1
            if inclusion_count > MAX_COPY_INCLUSIONS_PER_PROGRAM:
                boundary("copy_expansion_limit", edge["evidence_id"])
                break
            if len(path) > MAX_COPY_DEPTH:
                boundary("copy_depth_limit", edge["evidence_id"])
                continue
            copy_boundary = json.loads(edge["metadata_json"]).get("boundary")
            if copy_boundary:
                boundary(str(copy_boundary), edge["evidence_id"])
                continue
            targets = copies.get(edge["target_name"], [])
            if len(targets) != 1:
                boundary(
                    "copy_target_ambiguous" if targets else "copy_target_not_found",
                    edge["evidence_id"],
                )
                continue
            copy = targets[0]
            if copy["relative_path"] in replacement_files:
                boundary("copy_form_not_supported", replacement_files[copy["relative_path"]])
                continue
            if copy["symbol_id"] in ancestors:
                boundary("copy_cycle", edge["evidence_id"])
                continue
            source_fields = fields.get(copy["relative_path"], [])
            if program_count + len(source_fields) > MAX_COPY_FIELDS_PER_PROGRAM:
                boundary("copy_expansion_limit", edge["evidence_id"])
                break
            for source in source_fields:
                symbol_id = _id("copy_sym", program["symbol_id"], *path, source["symbol_id"])
                unit_id = _id("copy_unit", program["symbol_id"], *path, source["definition_unit_id"])
                connection.execute(
                    "INSERT INTO code_units VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (unit_id, source["relative_path"], "DataItem", source["name"],
                     program["program_name"], program["definition_unit_id"],
                     source["start_line"], source["end_line"], source["normalized_text"],
                     source["content_hash"], source["evidence_id"], source["parse_status"]),
                )
                connection.execute(
                    "INSERT INTO symbols VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (symbol_id, source["relative_path"], source["symbol_type"],
                     source["name"], program["program_name"],
                     f"{program['program_name']}::{source['name']}", unit_id, source["evidence_id"]),
                )
                connection.execute(
                    "INSERT INTO code_units_fts VALUES (?, ?, ?, ?)",
                    (unit_id, source["name"], program["program_name"], source["normalized_text"]),
                )
                connection.execute(
                    "INSERT INTO relations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (_id("copy_rel", unit_id), program["relative_path"],
                     program["definition_unit_id"], "CONTAINS", source["name"],
                     program["program_name"], unit_id, "confirmed", source["evidence_id"], "{}"),
                )
                connection.execute(
                    "INSERT INTO copy_expansions VALUES (?, ?, ?, ?, ?)",
                    (symbol_id, unit_id, source["symbol_id"], program["symbol_id"], json.dumps(path)),
                )
                program_count += 1
                bound_count += 1
            ancestors = (*ancestors, copy["symbol_id"])
            pending.extend(
                (nested, ancestors, path) for nested in reversed(includes.get(
                    (copy["relative_path"], copy["name"]), []
                ))
            )
    reasons = {
        row["reason"]: row["count"] for row in connection.execute(
            "SELECT reason, COUNT(*) AS count FROM copy_scope_boundaries GROUP BY reason"
        )
    }
    return {"bound_fields": bound_count, "incomplete_scopes": int(connection.execute(
        "SELECT COUNT(DISTINCT program_name) FROM copy_scope_boundaries"
    ).fetchone()[0]), "boundary_counts": reasons}
