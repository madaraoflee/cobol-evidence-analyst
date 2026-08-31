#!/usr/bin/env python3
"""Bounded, read-only investigation tools for the demonstrable COBOL POC.

The tools deliberately expose domain operations instead of arbitrary SQL or
filesystem access.  All source text is returned only by ``read_evidence`` and
is explicitly labelled as untrusted evidence data.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Sequence
from urllib.parse import quote


TOOL_CONTRACT_VERSION = "0.1"

ALLOWED_RELATION_TYPES = frozenset(
    {
        "CALLS",
        "CALL_TARGET_FROM",
        "PERFORMS",
        "PERFORMS_THRU",
        "INCLUDES_COPY",
        "READS",
        "WRITES",
        "CONTROL_DEPENDS_ON",
        "READS_FILE",
        "WRITES_FILE",
        "SELECTS_FROM",
        "UPDATES",
    }
)

IDENTIFIER_RE = re.compile(r"[A-Z][A-Z0-9_$#@-]*", re.IGNORECASE)
SYMBOL_NAME_RE = re.compile(r"^[A-Z0-9_$#@.-]{1,128}$", re.IGNORECASE)

MAX_SEARCH_RESULTS = 25
MAX_SYMBOL_MATCHES = 10
MAX_SCOPE_UNITS = 500
MAX_RELATIONS_PER_INSPECTION = 100
MAX_TRACE_DEPTH = 3
MAX_TRACE_EDGES = 200
MAX_EVIDENCE_SPANS = 12
MAX_EVIDENCE_CHARS = 50_000


class InvestigationTools:
    """A snapshot-bound facade over the local structural index."""

    def __init__(self, database_path: Path | str):
        self.database_path = Path(database_path).expanduser().resolve()
        if not self.database_path.is_file():
            raise ValueError(f"Structural index does not exist: {database_path}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        encoded_path = quote(self.database_path.as_posix(), safe="/:")
        connection = sqlite3.connect(
            f"file:{encoded_path}?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _snapshot_id(connection: sqlite3.Connection) -> str:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'snapshot_id'"
        ).fetchone()
        return row["value"] if row else "unknown"

    def _base_result(
        self,
        connection: sqlite3.Connection,
        tool_name: str,
        status: str,
    ) -> dict[str, object]:
        return {
            "tool": tool_name,
            "contract_version": TOOL_CONTRACT_VERSION,
            "snapshot_id": self._snapshot_id(connection),
            "status": status,
            "truncated": False,
            "boundaries": [],
            "diagnostics": [],
        }

    def snapshot_coverage(self) -> dict[str, object]:
        """Describe what the bound structural snapshot can and cannot prove.

        This is local orchestrator metadata, intentionally not a fifth model
        tool.  Runtime records and logs are never implied by source indexing.
        """

        with self._connect() as connection:
            kinds = [
                str(row["artifact_kind"])
                for row in connection.execute(
                    """
                    SELECT DISTINCT artifact_kind
                      FROM source_files
                     ORDER BY artifact_kind
                    """
                )
            ]
            kind_set = set(kinds)
            missing = [
                "database records",
                "control-table values",
                "runtime parameters",
                "runtime logs",
            ]
            if not kind_set.intersection(
                {"ddl_or_db_file_definition", "sql_or_ddl"}
            ):
                missing.insert(0, "DDL/DDS/database definitions")
            if "job_or_command" not in kind_set:
                missing.insert(1, "job/JCL/command definitions")
            return {
                "type": "snapshot_coverage",
                "snapshot_id": self._snapshot_id(connection),
                "indexed_artifact_kinds": kinds,
                "missing_artifacts": missing,
                "runtime_state_indexed": False,
            }

    @staticmethod
    def _search_tokens(query: str) -> list[str]:
        tokens: list[str] = []
        seen: set[str] = set()
        without_literals = re.sub(r"(['\"]).*?\1", " ", query)
        for match in IDENTIFIER_RE.findall(without_literals.upper()):
            if match not in seen:
                tokens.append(match)
                seen.add(match)
        return tokens[:32]

    @staticmethod
    def _validate_symbol_name(name: str) -> str:
        normalized = name.strip().upper()
        if not SYMBOL_NAME_RE.fullmatch(normalized):
            raise ValueError(
                "Symbol name must contain only COBOL identifier characters."
            )
        return normalized

    @staticmethod
    def _evidence_ref(row: sqlite3.Row) -> dict[str, object]:
        return {
            "evidence_id": row["evidence_id"],
            "relative_path": row["relative_path"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
        }

    @staticmethod
    def _relation_record(row: sqlite3.Row, *, depth: int | None = None) -> dict[str, object]:
        metadata = json.loads(row["metadata_json"] or "{}")
        record: dict[str, object] = {
            "relation_id": row["relation_id"],
            "relation_type": row["relation_type"],
            "status": row["status"],
            "source": {
                "entity_id": row["from_entity_id"],
                "unit_type": row["source_unit_type"],
                "name": row["source_name"],
                "program_name": row["source_program_name"],
            },
            "target": {
                "entity_id": row["target_entity_id"],
                "name": row["target_name"],
                "scope": row["target_scope"],
            },
            "metadata": metadata,
            "evidence_ref": {
                "evidence_id": row["evidence_id"],
                "relative_path": row["evidence_path"],
                "start_line": row["evidence_start_line"],
                "end_line": row["evidence_end_line"],
            },
        }
        if depth is not None:
            record["depth"] = depth
        return record

    @staticmethod
    def _relation_select() -> str:
        return """
            SELECT r.relation_id, r.from_entity_id, r.relation_type,
                   r.target_name, r.target_scope, r.target_entity_id,
                   r.status, r.evidence_id, r.metadata_json,
                   u.unit_type AS source_unit_type,
                   u.name AS source_name,
                   u.program_name AS source_program_name,
                   e.relative_path AS evidence_path,
                   e.start_line AS evidence_start_line,
                   e.end_line AS evidence_end_line
              FROM relations AS r
              LEFT JOIN code_units AS u ON u.unit_id = r.from_entity_id
              JOIN evidence_spans AS e ON e.evidence_id = r.evidence_id
        """

    @staticmethod
    def _scope_unit_ids(
        connection: sqlite3.Connection, definition_unit_id: str
    ) -> tuple[list[str], bool]:
        rows = list(
            connection.execute(
                """
                WITH RECURSIVE scope(entity_id, depth) AS (
                    SELECT ?, 0
                    UNION
                    SELECT r.target_entity_id, scope.depth + 1
                      FROM relations AS r
                      JOIN scope ON r.from_entity_id = scope.entity_id
                     WHERE r.relation_type = 'CONTAINS'
                       AND r.target_entity_id IS NOT NULL
                       AND scope.depth < 8
                )
                SELECT entity_id
                  FROM scope
                 LIMIT ?
                """,
                (definition_unit_id, MAX_SCOPE_UNITS + 1),
            )
        )
        truncated = len(rows) > MAX_SCOPE_UNITS
        return [row["entity_id"] for row in rows[:MAX_SCOPE_UNITS]], truncated

    @staticmethod
    def _resolve_symbols(
        connection: sqlite3.Connection,
        name: str,
        *,
        program_name: str | None = None,
        symbol_type: str | None = None,
        limit: int = MAX_SYMBOL_MATCHES,
    ) -> tuple[list[sqlite3.Row], bool]:
        clauses = ["s.name = ?"]
        parameters: list[object] = [name]
        if program_name:
            clauses.append("s.program_name = ?")
            parameters.append(program_name.upper())
        if symbol_type:
            clauses.append("UPPER(s.symbol_type) = ?")
            parameters.append(symbol_type.upper())
        parameters.append(limit + 1)
        rows = list(
            connection.execute(
                f"""
                SELECT s.symbol_id, s.symbol_type, s.name, s.program_name,
                       s.qualified_name, s.definition_unit_id,
                       s.evidence_id, s.relative_path,
                       u.unit_type, u.start_line, u.end_line
                  FROM symbols AS s
                  JOIN code_units AS u
                    ON u.unit_id = s.definition_unit_id
                 WHERE {' AND '.join(clauses)}
                 ORDER BY s.symbol_type, s.program_name, s.relative_path,
                          u.start_line
                 LIMIT ?
                """,
                tuple(parameters),
            )
        )
        return rows[:limit], len(rows) > limit

    def search_code(
        self, query: str, *, limit: int = 10
    ) -> dict[str, object]:
        """Find exact symbols first, then bounded FTS candidates."""

        safe_limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
        tokens = self._search_tokens(query)
        with self._connect() as connection:
            result = self._base_result(connection, "search_code", "OK")
            result["query"] = query
            result["tokens"] = tokens
            if not tokens:
                result["status"] = "NOT_FOUND"
                result["hits"] = []
                result["evidence_refs"] = []
                result["diagnostics"].append(
                    {
                        "code": "NO_CODE_ANCHOR",
                        "message": (
                            "No COBOL-style identifier was found. The Agent "
                            "should translate the business question into one or "
                            "more code anchors before retrying."
                        ),
                    }
                )
                return result

            placeholders = ",".join("?" for _ in tokens)
            exact_rows = list(
                connection.execute(
                    f"""
                    SELECT c.unit_id, c.unit_type, c.name, c.program_name,
                           c.relative_path, c.start_line, c.end_line,
                           c.evidence_id, s.symbol_id, s.symbol_type,
                           s.qualified_name
                      FROM symbols AS s
                      JOIN code_units AS c
                        ON c.unit_id = s.definition_unit_id
                     WHERE s.name IN ({placeholders})
                     ORDER BY CASE s.symbol_type
                                  WHEN 'Program' THEN 0
                                  WHEN 'Paragraph' THEN 1
                                  WHEN 'Field' THEN 2
                                  ELSE 3
                              END,
                              s.name, c.relative_path, c.start_line
                     LIMIT ?
                    """,
                    (*tokens, safe_limit + 1),
                )
            )

            match_expression = " OR ".join(f'"{token}"' for token in tokens)
            fts_rows = list(
                connection.execute(
                    """
                    SELECT c.unit_id, c.unit_type, c.name, c.program_name,
                           c.relative_path, c.start_line, c.end_line,
                           c.evidence_id,
                           bm25(code_units_fts) AS score
                      FROM code_units_fts
                      JOIN code_units AS c
                        ON c.unit_id = code_units_fts.unit_id
                     WHERE code_units_fts MATCH ?
                     ORDER BY score, c.relative_path, c.start_line
                     LIMIT ?
                    """,
                    (match_expression, safe_limit * 3 + 1),
                )
            )

            hits: list[dict[str, object]] = []
            seen_units: set[str] = set()
            for row in exact_rows:
                if row["unit_id"] in seen_units:
                    continue
                seen_units.add(row["unit_id"])
                hits.append(
                    {
                        "match_type": "exact_symbol",
                        "unit_id": row["unit_id"],
                        "unit_type": row["unit_type"],
                        "name": row["name"],
                        "program_name": row["program_name"],
                        "symbol_id": row["symbol_id"],
                        "symbol_type": row["symbol_type"],
                        "qualified_name": row["qualified_name"],
                        "evidence_ref": self._evidence_ref(row),
                    }
                )

            for row in fts_rows:
                if row["unit_id"] in seen_units:
                    continue
                seen_units.add(row["unit_id"])
                hits.append(
                    {
                        "match_type": "full_text",
                        "unit_id": row["unit_id"],
                        "unit_type": row["unit_type"],
                        "name": row["name"],
                        "program_name": row["program_name"],
                        "fts_score": row["score"],
                        "evidence_ref": self._evidence_ref(row),
                    }
                )

            truncated = len(hits) > safe_limit or len(exact_rows) > safe_limit
            hits = hits[:safe_limit]
            result["hits"] = hits
            result["evidence_refs"] = [
                hit["evidence_ref"] for hit in hits
            ]
            result["result_count"] = len(hits)
            result["truncated"] = truncated
            if not hits:
                result["status"] = "NOT_FOUND"
            elif truncated:
                result["status"] = "PARTIAL"
                result["diagnostics"].append(
                    {
                        "code": "RESULT_LIMIT_REACHED",
                        "message": f"Search stopped at {safe_limit} results.",
                    }
                )
            return result

    def inspect_symbol(
        self,
        name: str,
        *,
        program_name: str | None = None,
        symbol_type: str | None = None,
        max_relations: int = 40,
    ) -> dict[str, object]:
        """Inspect definitions and bounded direct structural relationships."""

        normalized_name = self._validate_symbol_name(name)
        safe_relation_limit = max(
            1, min(int(max_relations), MAX_RELATIONS_PER_INSPECTION)
        )
        with self._connect() as connection:
            rows, matches_truncated = self._resolve_symbols(
                connection,
                normalized_name,
                program_name=program_name,
                symbol_type=symbol_type,
            )
            status = "OK" if rows else "NOT_FOUND"
            if len(rows) > 1:
                status = "AMBIGUOUS"
            result = self._base_result(connection, "inspect_symbol", status)
            result["query"] = {
                "name": normalized_name,
                "program_name": program_name.upper() if program_name else None,
                "symbol_type": symbol_type,
            }
            matches: list[dict[str, object]] = []
            any_truncated = matches_truncated

            for symbol in rows:
                scope_ids, scope_truncated = self._scope_unit_ids(
                    connection, symbol["definition_unit_id"]
                )
                any_truncated = any_truncated or scope_truncated
                placeholders = ",".join("?" for _ in scope_ids)
                outgoing_rows = list(
                    connection.execute(
                        self._relation_select()
                        + f"""
                         WHERE r.from_entity_id IN ({placeholders})
                           AND r.relation_type != 'CONTAINS'
                         ORDER BY e.relative_path, e.start_line,
                                  r.relation_type
                         LIMIT ?
                        """,
                        (*scope_ids, safe_relation_limit + 1),
                    )
                )

                incoming_rows = list(
                    connection.execute(
                        self._relation_select()
                        + """
                         WHERE r.relation_type != 'CONTAINS'
                           AND (
                               r.target_entity_id = ?
                               OR (
                                   r.target_entity_id IS NULL
                                   AND r.target_name = ?
                               )
                           )
                         ORDER BY e.relative_path, e.start_line,
                                  r.relation_type
                         LIMIT ?
                        """,
                        (
                            symbol["symbol_id"],
                            symbol["name"],
                            safe_relation_limit + 1,
                        ),
                    )
                )

                relation_truncated = (
                    len(outgoing_rows) > safe_relation_limit
                    or len(incoming_rows) > safe_relation_limit
                )
                any_truncated = any_truncated or relation_truncated
                outgoing = [
                    self._relation_record(row)
                    for row in outgoing_rows[:safe_relation_limit]
                ]
                incoming = [
                    self._relation_record(row)
                    for row in incoming_rows[:safe_relation_limit]
                ]
                matches.append(
                    {
                        "symbol": {
                            "symbol_id": symbol["symbol_id"],
                            "symbol_type": symbol["symbol_type"],
                            "name": symbol["name"],
                            "program_name": symbol["program_name"],
                            "qualified_name": symbol["qualified_name"],
                        },
                        "definition": {
                            "unit_id": symbol["definition_unit_id"],
                            "unit_type": symbol["unit_type"],
                            "evidence_ref": self._evidence_ref(symbol),
                        },
                        "scope_unit_count": len(scope_ids),
                        "outgoing_relations": outgoing,
                        "incoming_relations": incoming,
                    }
                )

            result["matches"] = matches
            result["match_count"] = len(matches)
            result["truncated"] = any_truncated
            if any_truncated and result["status"] == "OK":
                result["status"] = "PARTIAL"
            if len(rows) > 1:
                result["diagnostics"].append(
                    {
                        "code": "AMBIGUOUS_SYMBOL",
                        "message": (
                            "More than one exact symbol exists. Add program_name "
                            "or symbol_type before treating a definition as fact."
                        ),
                    }
                )
            if any_truncated:
                result["diagnostics"].append(
                    {
                        "code": "INSPECTION_LIMIT_REACHED",
                        "message": "The bounded structural context was truncated.",
                    }
                )
            return result

    @staticmethod
    def _entity_for_id(
        connection: sqlite3.Connection, entity_id: str
    ) -> dict[str, object] | None:
        symbol = connection.execute(
            """
            SELECT symbol_id AS entity_id, symbol_type AS entity_type,
                   name, program_name, definition_unit_id
              FROM symbols
             WHERE symbol_id = ?
            """,
            (entity_id,),
        ).fetchone()
        if symbol:
            return dict(symbol)
        unit = connection.execute(
            """
            SELECT unit_id AS entity_id, unit_type AS entity_type,
                   name, program_name, unit_id AS definition_unit_id
              FROM code_units
             WHERE unit_id = ?
            """,
            (entity_id,),
        ).fetchone()
        return dict(unit) if unit else None

    @staticmethod
    def _owner_program_entity(
        connection: sqlite3.Connection, unit_id: str
    ) -> dict[str, object] | None:
        row = connection.execute(
            "SELECT program_name FROM code_units WHERE unit_id = ?",
            (unit_id,),
        ).fetchone()
        if not row or not row["program_name"]:
            return None
        program = connection.execute(
            """
            SELECT symbol_id AS entity_id, symbol_type AS entity_type,
                   name, program_name, definition_unit_id
              FROM symbols
             WHERE symbol_type = 'Program' AND name = ?
             ORDER BY relative_path
             LIMIT 1
            """,
            (row["program_name"],),
        ).fetchone()
        return dict(program) if program else None

    def trace_relations(
        self,
        start_name: str,
        *,
        program_name: str | None = None,
        symbol_type: str | None = None,
        relation_types: Iterable[str] | None = None,
        direction: str = "outgoing",
        max_depth: int = 3,
        max_edges: int = 80,
    ) -> dict[str, object]:
        """Trace approved relationships without exposing a raw graph query."""

        normalized_name = self._validate_symbol_name(start_name)
        normalized_direction = direction.lower()
        if normalized_direction not in {"outgoing", "incoming"}:
            raise ValueError("direction must be 'outgoing' or 'incoming'.")

        requested_types = (
            {item.upper() for item in relation_types}
            if relation_types
            else set(ALLOWED_RELATION_TYPES)
        )
        invalid_types = requested_types - ALLOWED_RELATION_TYPES
        if invalid_types:
            raise ValueError(
                "Unsupported relation types: " + ", ".join(sorted(invalid_types))
            )
        safe_depth = max(1, min(int(max_depth), MAX_TRACE_DEPTH))
        safe_edges = max(1, min(int(max_edges), MAX_TRACE_EDGES))

        with self._connect() as connection:
            symbols, symbols_truncated = self._resolve_symbols(
                connection,
                normalized_name,
                program_name=program_name,
                symbol_type=symbol_type,
            )
            status = "OK" if symbols else "NOT_FOUND"
            if len(symbols) > 1:
                status = "AMBIGUOUS"
            result = self._base_result(connection, "trace_relations", status)
            result["query"] = {
                "start_name": normalized_name,
                "program_name": program_name.upper() if program_name else None,
                "symbol_type": symbol_type,
                "relation_types": sorted(requested_types),
                "direction": normalized_direction,
                "max_depth": safe_depth,
                "max_edges": safe_edges,
            }
            result["edges"] = []
            result["visited_entities"] = []
            if not symbols:
                return result
            if len(symbols) > 1 or symbols_truncated:
                result["start_candidates"] = [
                    {
                        "symbol_id": row["symbol_id"],
                        "symbol_type": row["symbol_type"],
                        "name": row["name"],
                        "program_name": row["program_name"],
                        "qualified_name": row["qualified_name"],
                    }
                    for row in symbols
                ]
                result["diagnostics"].append(
                    {
                        "code": "AMBIGUOUS_START_SYMBOL",
                        "message": "Refine the start symbol before graph traversal.",
                    }
                )
                return result

            start = symbols[0]
            start_entity = {
                "entity_id": start["symbol_id"],
                "entity_type": start["symbol_type"],
                "name": start["name"],
                "program_name": start["program_name"],
                "definition_unit_id": start["definition_unit_id"],
            }
            frontier: deque[dict[str, object]] = deque([start_entity])
            visited: set[str] = {str(start_entity["entity_id"])}
            visited_output: list[dict[str, object]] = [start_entity]
            seen_relations: set[str] = set()
            edges: list[dict[str, object]] = []
            boundaries: list[dict[str, object]] = []
            truncated = False

            placeholders_types = ",".join("?" for _ in requested_types)
            sorted_types = tuple(sorted(requested_types))

            for depth in range(1, safe_depth + 1):
                level_count = len(frontier)
                if level_count == 0:
                    break
                next_frontier: deque[dict[str, object]] = deque()

                for _ in range(level_count):
                    entity = frontier.popleft()
                    if normalized_direction == "outgoing":
                        definition_id = str(entity["definition_unit_id"])
                        source_ids, scope_truncated = self._scope_unit_ids(
                            connection, definition_id
                        )
                        truncated = truncated or scope_truncated
                        source_placeholders = ",".join("?" for _ in source_ids)
                        relation_rows = list(
                            connection.execute(
                                self._relation_select()
                                + f"""
                                 WHERE r.from_entity_id IN ({source_placeholders})
                                   AND r.relation_type IN ({placeholders_types})
                                 ORDER BY e.relative_path, e.start_line,
                                          r.relation_type
                                 LIMIT ?
                                """,
                                (*source_ids, *sorted_types, safe_edges + 1),
                            )
                        )
                    else:
                        relation_rows = list(
                            connection.execute(
                                self._relation_select()
                                + f"""
                                 WHERE r.relation_type IN ({placeholders_types})
                                   AND (
                                       r.target_entity_id = ?
                                       OR (
                                           r.target_entity_id IS NULL
                                           AND r.target_name = ?
                                       )
                                   )
                                 ORDER BY e.relative_path, e.start_line,
                                          r.relation_type
                                 LIMIT ?
                                """,
                                (
                                    *sorted_types,
                                    entity["entity_id"],
                                    entity["name"],
                                    safe_edges + 1,
                                ),
                            )
                        )

                    for row in relation_rows:
                        if row["relation_id"] in seen_relations:
                            continue
                        if len(edges) >= safe_edges:
                            truncated = True
                            break
                        seen_relations.add(row["relation_id"])
                        edge = self._relation_record(row, depth=depth)
                        edges.append(edge)

                        metadata = edge["metadata"]
                        if row["status"] != "confirmed" or metadata.get("boundary"):
                            boundaries.append(
                                {
                                    "relation_id": row["relation_id"],
                                    "relation_type": row["relation_type"],
                                    "target_name": row["target_name"],
                                    "status": row["status"],
                                    "reason": metadata.get(
                                        "resolution_reason",
                                        metadata.get("boundary", "unresolved"),
                                    ),
                                }
                            )

                        if normalized_direction == "outgoing":
                            next_entity = (
                                self._entity_for_id(
                                    connection, row["target_entity_id"]
                                )
                                if row["target_entity_id"]
                                else None
                            )
                        else:
                            next_entity = self._owner_program_entity(
                                connection, row["from_entity_id"]
                            )

                        if next_entity:
                            next_id = str(next_entity["entity_id"])
                            if next_id not in visited:
                                visited.add(next_id)
                                visited_output.append(next_entity)
                                next_frontier.append(next_entity)
                    if truncated and len(edges) >= safe_edges:
                        break

                frontier = next_frontier
                if truncated and len(edges) >= safe_edges:
                    break

            result["start_entity"] = start_entity
            result["edges"] = edges
            result["edge_count"] = len(edges)
            result["visited_entities"] = visited_output
            result["visited_entity_count"] = len(visited_output)
            result["boundaries"] = boundaries
            result["truncated"] = truncated
            if truncated:
                result["status"] = "PARTIAL"
                result["diagnostics"].append(
                    {
                        "code": "TRACE_BUDGET_EXHAUSTED",
                        "message": (
                            "The trace stopped at its depth, scope, or edge budget."
                        ),
                    }
                )
            return result

    def read_evidence(
        self,
        evidence_ids: Iterable[str],
        *,
        max_chars: int = 16_000,
    ) -> dict[str, object]:
        """Read previously discovered evidence IDs; arbitrary paths are invalid."""

        requested: list[str] = []
        seen: set[str] = set()
        for evidence_id in evidence_ids:
            normalized = str(evidence_id).strip()
            if normalized and normalized not in seen:
                requested.append(normalized)
                seen.add(normalized)
        if not requested:
            raise ValueError("At least one evidence_id is required.")
        if len(requested) > MAX_EVIDENCE_SPANS:
            requested = requested[:MAX_EVIDENCE_SPANS]
            request_truncated = True
        else:
            request_truncated = False
        safe_chars = max(200, min(int(max_chars), MAX_EVIDENCE_CHARS))

        with self._connect() as connection:
            result = self._base_result(connection, "read_evidence", "OK")
            spans: list[dict[str, object]] = []
            missing: list[str] = []
            remaining = safe_chars
            content_truncated = False
            integrity_invalid = False

            for evidence_id in requested:
                row = connection.execute(
                    """
                    SELECT e.evidence_id, e.relative_path, e.start_line,
                           e.end_line, e.source_sha256, e.text,
                           sf.sha256 AS current_source_sha256
                      FROM evidence_spans AS e
                      LEFT JOIN source_files AS sf
                        ON sf.relative_path = e.relative_path
                     WHERE e.evidence_id = ?
                    """,
                    (evidence_id,),
                ).fetchone()
                if not row:
                    missing.append(evidence_id)
                    continue

                integrity = (
                    "VALID"
                    if row["source_sha256"] == row["current_source_sha256"]
                    else "INVALID"
                )
                integrity_invalid = integrity_invalid or integrity == "INVALID"
                text = row["text"]
                span_truncated = len(text) > remaining
                if remaining <= 0:
                    content_truncated = True
                    break
                if span_truncated:
                    text = text[:remaining]
                    content_truncated = True
                remaining -= len(text)
                spans.append(
                    {
                        "evidence_id": row["evidence_id"],
                        "relative_path": row["relative_path"],
                        "start_line": row["start_line"],
                        "end_line": row["end_line"],
                        "source_sha256": row["source_sha256"],
                        "integrity": integrity,
                        "content_type": "UNTRUSTED_SOURCE_TEXT",
                        "source_text": text,
                        "span_truncated": span_truncated,
                    }
                )
                if span_truncated:
                    break

            result["spans"] = spans
            result["span_count"] = len(spans)
            result["missing_evidence_ids"] = missing
            result["returned_characters"] = safe_chars - remaining
            result["truncated"] = request_truncated or content_truncated
            if integrity_invalid:
                result["status"] = "INTEGRITY_ERROR"
                result["diagnostics"].append(
                    {
                        "code": "SOURCE_HASH_MISMATCH",
                        "message": "At least one evidence span no longer matches its source snapshot.",
                    }
                )
            elif missing or request_truncated or content_truncated:
                result["status"] = "PARTIAL"
            if missing:
                result["diagnostics"].append(
                    {
                        "code": "EVIDENCE_NOT_FOUND",
                        "message": "Some requested evidence IDs were not present in this snapshot.",
                    }
                )
            if result["truncated"]:
                result["diagnostics"].append(
                    {
                        "code": "EVIDENCE_BUDGET_REACHED",
                        "message": "Evidence output stopped at the configured span or character budget.",
                    }
                )
            return result


def tool_definitions() -> list[dict[str, object]]:
    """Return OpenAI-compatible function schemas for the later Agent loop."""

    return [
        {
            "type": "function",
            "function": {
                "name": "search_code",
                "description": "Locate bounded COBOL code candidates by exact symbol and full text.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 1024,
                        },
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_RESULTS},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "inspect_symbol",
                "description": "Inspect exact definitions and direct bounded relationships for a COBOL symbol.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "pattern": "^[A-Za-z0-9_$#@.-]+$",
                        },
                        "program_name": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "pattern": "^[A-Za-z0-9_$#@.-]+$",
                        },
                        "symbol_type": {
                            "type": "string",
                            "enum": ["Program", "Paragraph", "Field", "Copybook"],
                        },
                        "max_relations": {"type": "integer", "minimum": 1, "maximum": MAX_RELATIONS_PER_INSPECTION},
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "trace_relations",
                "description": "Trace approved code relationships for no more than three hops.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "start_name": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "pattern": "^[A-Za-z0-9_$#@.-]+$",
                        },
                        "program_name": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "pattern": "^[A-Za-z0-9_$#@.-]+$",
                        },
                        "symbol_type": {
                            "type": "string",
                            "enum": ["Program", "Paragraph", "Field", "Copybook"],
                        },
                        "relation_types": {
                            "type": "array",
                            "items": {"type": "string", "enum": sorted(ALLOWED_RELATION_TYPES)},
                            "minItems": 1,
                            "maxItems": len(ALLOWED_RELATION_TYPES),
                            "uniqueItems": True,
                        },
                        "direction": {"type": "string", "enum": ["outgoing", "incoming"]},
                        "max_depth": {"type": "integer", "minimum": 1, "maximum": MAX_TRACE_DEPTH},
                        "max_edges": {"type": "integer", "minimum": 1, "maximum": MAX_TRACE_EDGES},
                    },
                    "required": ["start_name"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_evidence",
                "description": "Read exact source spans by previously discovered evidence IDs only.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "evidence_ids": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 128,
                            },
                            "minItems": 1,
                            "maxItems": MAX_EVIDENCE_SPANS,
                            "uniqueItems": True,
                        },
                        "max_chars": {"type": "integer", "minimum": 200, "maximum": MAX_EVIDENCE_CHARS},
                    },
                    "required": ["evidence_ids"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def _json_print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run bounded read-only tools against a structural COBOL index."
    )
    parser.add_argument("--database", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search-code")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)

    inspect = subparsers.add_parser("inspect-symbol")
    inspect.add_argument("name")
    inspect.add_argument("--program-name")
    inspect.add_argument("--symbol-type")
    inspect.add_argument("--max-relations", type=int, default=40)

    trace = subparsers.add_parser("trace-relations")
    trace.add_argument("start_name")
    trace.add_argument("--program-name")
    trace.add_argument("--symbol-type")
    trace.add_argument("--relation-type", action="append", dest="relation_types")
    trace.add_argument("--direction", choices=("outgoing", "incoming"), default="outgoing")
    trace.add_argument("--max-depth", type=int, default=3)
    trace.add_argument("--max-edges", type=int, default=80)

    evidence = subparsers.add_parser("read-evidence")
    evidence.add_argument("evidence_ids", nargs="+")
    evidence.add_argument("--max-chars", type=int, default=16_000)

    subparsers.add_parser("tool-definitions")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tools = InvestigationTools(args.database)
    if args.command == "search-code":
        output = tools.search_code(args.query, limit=args.limit)
    elif args.command == "inspect-symbol":
        output = tools.inspect_symbol(
            args.name,
            program_name=args.program_name,
            symbol_type=args.symbol_type,
            max_relations=args.max_relations,
        )
    elif args.command == "trace-relations":
        output = tools.trace_relations(
            args.start_name,
            program_name=args.program_name,
            symbol_type=args.symbol_type,
            relation_types=args.relation_types,
            direction=args.direction,
            max_depth=args.max_depth,
            max_edges=args.max_edges,
        )
    elif args.command == "read-evidence":
        output = tools.read_evidence(
            args.evidence_ids, max_chars=args.max_chars
        )
    else:
        output = tool_definitions()
    _json_print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
