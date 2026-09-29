"""Source-linked terminology candidates; co-occurrence never proves equivalence."""

from __future__ import annotations

from contextlib import closing
import re
import sqlite3

from framework_knowledge import search_framework_context
from repository_discovery import _query_terms


_SYMBOL = re.compile(r"\b[A-Z][A-Z0-9_$#@-]{2,}\b")


def search_concepts(database_path, query, *, framework_reference_path=None, limit=12):
    terms, _ = _query_terms(query, None)
    candidates = []
    expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
    if expression:
        with closing(sqlite3.connect(database_path)) as db:
            db.row_factory = sqlite3.Row
            for row in db.execute("SELECT p.relative_path,p.start_line,p.end_line,p.source_sha256,e.text "
                    "FROM repo_fts JOIN repo_pages p ON p.page_id=repo_fts.rowid "
                    "LEFT JOIN evidence_spans e ON e.evidence_id=p.evidence_id "
                    "WHERE repo_fts MATCH ? ORDER BY bm25(repo_fts) LIMIT ?", (expression, limit)):
                text = row["text"] or ""
                symbols = list(dict.fromkeys(_SYMBOL.findall(text.upper())))[:16]
                candidates.append({"source": "source", "path": row["relative_path"],
                    "source_sha256": row["source_sha256"], "start_line": row["start_line"],
                    "end_line": row["end_line"], "symbols": symbols,
                    "excerpt": text[:700], "basis": "co_occurrence_candidate"})
    if framework_reference_path:
        found = search_framework_context(query, reference_path=framework_reference_path,
                                         max_references=limit, max_chars=6000)
        for item in found.get("references", []):
            text = item.get("text", "")
            candidates.append({"source": "framework", "path": item.get("document_path"),
                "document_sha256": (found.get("document") or {}).get("sha256"),
                "start_line": item.get("start_line"), "end_line": item.get("end_line"),
                "reference_id": item.get("reference_id"),
                "symbols": list(dict.fromkeys(_SYMBOL.findall(text.upper())))[:16],
                "excerpt": text[:700], "basis": "co_occurrence_candidate"})
    return candidates[:limit]
