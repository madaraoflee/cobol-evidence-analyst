"""Bounded file-impact reading leads, never execution or LF/PF lineage proof."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import re
import sqlite3

from business_index import _clean
from statement_facts import sentence_terminated


_FILE_TERMS = re.compile(r"文件|字段|欄位|栏位|記錄|记录|(?<![A-Z0-9_$#@-])"
                         r"(?:LF|PF|files?|fields?|records?)(?![A-Z0-9_$#@-])", re.I)
_IMPACT_TERMS = re.compile(r"影响|影響|修改|哪些|哪個|哪个|列出|\b(?:affect\w*|impact\w*|chang\w*|which|list)\b", re.I)
_NAME = r"[A-Z][A-Z0-9_$#@-]*"
_SELECT = re.compile(rf"^SELECT\s+(?:OPTIONAL\s+)?({_NAME})\b", re.I)
_FD = re.compile(rf"^FD\s+({_NAME})\b", re.I)
_SECTION = re.compile(r"^(?:[A-Z-]+\s+SECTION|PROCEDURE\s+DIVISION)\b", re.I)
_DDS_SUFFIXES = (".dds", ".lf", ".pf")
_MAX_PATHS, _MAX_PAGES, _MAX_LINES = 8, 32, 96


def is_file_impact_question(question):
    return bool(_FILE_TERMS.search(question) and _IMPACT_TERMS.search(question))


def _candidate(kind, path, first, last, text, sha):
    return {"kind": kind, "relative_path": path, "start_line": first,
            "end_line": last, "statement": text, "reads": [], "writes": [],
            "source_sha256": sha, "occurrence_count": 1}


def _lines(db, path, sha, tables, frontier, *, first=1, last=None):
    if "repo_pages" not in tables:
        return {}
    join = ("LEFT JOIN evidence_spans e ON e.evidence_id=p.evidence_id "
            "AND e.relative_path=p.relative_path AND e.source_sha256=p.source_sha256 "
            "AND e.start_line=p.start_line AND e.end_line=p.end_line") if "evidence_spans" in tables else ""
    text = "COALESCE(e.text,NULLIF(p.fallback_text,''),'')" if join else "p.fallback_text"
    rows = db.execute(f"SELECT p.*, {text} AS source_text FROM repo_pages p {join} "
        "WHERE p.relative_path=? AND p.end_line>=? AND (? IS NULL OR p.start_line<=?) "
        "ORDER BY p.start_line LIMIT ?", (path, first, last, last, _MAX_PAGES + 1)).fetchall()
    if len(rows) > _MAX_PAGES:
        frontier.append({"kind": "file_definitions", "relative_path": path, "reason": "indexed_page_budget"})
    result = {}
    for row in rows[:_MAX_PAGES]:
        if row["source_sha256"] != sha or row["span_truncated"] or not row["source_text"]:
            frontier.append({"kind": "file_definitions", "relative_path": path,
                             "reason": "indexed_source_unavailable"})
            continue
        for line, raw in enumerate(row["source_text"].splitlines(), row["start_line"]):
            if first <= line and (last is None or line <= last):
                result[line] = _clean(raw, "auto")[0]
    return result


def _span(kind, path, lines, first, last, sha, frontier):
    if last - first + 1 > _MAX_LINES:
        frontier.append({"kind": kind, "relative_path": path, "start_line": first,
                         "reason": "definition_span_budget"})
        last = first + _MAX_LINES - 1
    if any(line not in lines for line in range(first, last + 1)):
        frontier.append({"kind": kind, "relative_path": path, "start_line": first,
                         "reason": "indexed_source_gap"})
        return None
    return _candidate(kind, path, first, last,
                      " ".join(lines[line] for line in range(first, last + 1)).strip(), sha)


def _definitions(path, lines, sha, copybook, frontier):
    candidates, objects, assigned_aliases = [], [], set()
    numbers = sorted(lines)
    for first in numbers:
        code = lines[first]
        select, fd = _SELECT.match(code), _FD.match(code)
        if select:
            last = first
            while last in lines and last - first < _MAX_LINES - 1 and not sentence_terminated(lines[last]):
                last += 1
            row = _span("file_definitions", path, lines, first, last, sha, frontier)
            if row:
                candidates.append(row)
                # Only explicit object spellings are leads. Dynamic assignment,
                # runtime overrides, and filename abbreviations are not resolved.
                assignment = re.search(rf"\bASSIGN\s+(?:TO\s+)?(?:(?:DATABASE|DISK)\s+)?"
                    rf"(?:'([^']*)'|\"([^\"]*)\"|({_NAME}))", row["statement"], re.I)
                if assignment:
                    name = next((value for value in assignment.groups() if value is not None), "").upper()
                    if re.fullmatch(_NAME, name) and name not in {"DYNAMIC", "EXTERNAL", "DISK"}:
                        objects.append(name)
                        assigned_aliases.add(select.group(1).upper())
                if select.group(1).upper() not in assigned_aliases:
                    objects.append(select.group(1).upper())
        elif fd or (copybook and re.match(rf"^(?:01|77)\s+{_NAME}\b", code, re.I)):
            stop = next((line for line in numbers if line > first and (
                _FD.match(lines[line]) or _SECTION.match(lines[line]) or
                (copybook and re.match(r"^(?:01|77)\s+", lines[line])))), numbers[-1] + 1)
            row = _span("file_definitions", path, lines, first, stop - 1, sha, frontier)
            if row:
                candidates.append(row)
            if fd and fd.group(1).upper() not in assigned_aliases:
                objects.append(fd.group(1).upper())
        elif re.match(r"^FILE\s+SECTION\b", code, re.I):
            candidates.append(_candidate("file_definitions", path, first, first, code, sha))
    return candidates, list(dict.fromkeys(objects))


def nominate_file_impact_evidence(database_path, paths, *, max_candidates=48):
    """Nominate I/O, declarations, and exact-object DDS pages for source reads.

    Missing dependencies remain frontier entries. DDS names and text references
    only select reading candidates; they do not establish an LF/PF relation.
    """
    if type(max_candidates) is not int or not 1 <= max_candidates <= 128:
        raise ValueError("FILE_IMPACT_CANDIDATE_BUDGET_INVALID")
    candidates, frontier, hashes = [], [], {}
    scoped = list(dict.fromkeys(paths))[:_MAX_PATHS]
    if not scoped:
        return candidates, frontier, hashes
    if len(set(paths)) > _MAX_PATHS:
        frontier.append({"kind": "file_io", "reason": "located_path_budget",
                         "omitted_count": len(set(paths)) - _MAX_PATHS})
    with closing(sqlite3.connect(Path(database_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        slots = ",".join("?" for _ in scoped)
        files = {row["relative_path"]: dict(row) for row in db.execute(
            f"SELECT relative_path,sha256,artifact_kind FROM source_files WHERE relative_path IN ({slots})", scoped)}
        hashes.update((path, row["sha256"]) for path, row in files.items())
        if "repo_pages" not in tables:
            frontier.append({"kind": "file_definitions", "reason": "indexed_source_pages_unavailable"})
        objects = []
        for path in scoped:
            sha = hashes.get(path)
            if not sha:
                frontier.append({"kind": "file_definitions", "relative_path": path, "reason": "source_not_indexed"})
                continue
            lines = _lines(db, path, sha, tables, frontier)
            rules = []
            if "business_rules" in tables:
                rules = db.execute("SELECT * FROM business_rules WHERE relative_path=? "
                    "AND rule_kind IN ('READ','START','WRITE','REWRITE','DELETE') "
                    "ORDER BY first_line,rule_id LIMIT ?", (path, max_candidates + 1)).fetchall()
            elif "code_units" in tables:
                rules = db.execute("SELECT start_line AS first_line,end_line AS last_line,normalized_text "
                    "FROM code_units WHERE relative_path=? AND unit_type='Statement' "
                    "AND name IN ('READ','START','WRITE','REWRITE','DELETE') "
                    "ORDER BY start_line,unit_id LIMIT ?", (path, max_candidates + 1)).fetchall()
            else:
                frontier.append({"kind": "file_io", "relative_path": path, "reason": "indexed_rules_unavailable"})
            if len(rules) > max_candidates:
                frontier.append({"kind": "file_io", "relative_path": path, "reason": "file_io_candidate_budget"})
            for rule in rules[:max_candidates]:
                first = rule["first_line"]
                last = min(rule["last_line"], first + _MAX_LINES - 1)
                actual = _lines(db, path, sha, tables, frontier, first=first, last=last)
                # Sparse repeated rules span multiple occurrences. Locate the
                # first physical statement so distant repeats cannot hide it.
                for end in sorted(actual):
                    text = " ".join(actual.get(line, "") for line in range(first, end + 1)).strip()
                    if text.upper() == rule["normalized_text"].upper():
                        last = end
                        break
                else:
                    text = rule["normalized_text"]
                candidates.append(_candidate("file_io", path, first, last, text, sha))
            copybook = files[path]["artifact_kind"] in {"copybook", "cobol_fragment_or_copybook"}
            definitions, names = _definitions(path, lines, sha, copybook, frontier)
            candidates.extend(definitions)
            objects.extend(names)
            if not definitions:
                frontier.append({"kind": "file_definitions", "relative_path": path, "reason": "file_definitions_not_located"})
        dds_paths = []
        for name in list(dict.fromkeys(objects))[:16]:
            # Filename metadata is bounded separately from source-page reads.
            exact = []
            for suffix in _DDS_SUFFIXES:
                filename = name + suffix.upper()
                rows = db.execute("SELECT relative_path FROM source_files "
                    "WHERE UPPER(relative_path)=? OR UPPER(relative_path) LIKE ? ESCAPE '!' "
                    "ORDER BY relative_path LIMIT 9", (filename, "%/" + filename.replace("_", "!_") )).fetchall()
                exact.extend(row[0] for row in rows[:8])
                if len(rows) > 8:
                    frontier.append({"kind": "dds_definitions", "reason": "dds_lookup_budget", "object": name})
            referenced = []
            if "repo_fts" in tables:
                referenced = [row[0] for row in db.execute("SELECT DISTINCT p.relative_path FROM repo_fts "
                    "JOIN repo_pages p ON p.page_id=repo_fts.rowid WHERE repo_fts MATCH ? "
                    "AND (LOWER(p.relative_path) LIKE '%.dds' OR LOWER(p.relative_path) LIKE '%.lf' "
                    "OR LOWER(p.relative_path) LIKE '%.pf') ORDER BY p.relative_path LIMIT 9", ('"' + name + '"',))]
                if len(referenced) > 8:
                    frontier.append({"kind": "dds_definitions", "reason": "dds_lookup_budget", "object": name})
                    referenced = referenced[:8]
            matches = list(dict.fromkeys([*exact, *referenced]))
            if not matches:
                frontier.append({"kind": "dds_definitions", "reason": "dds_object_not_located", "object": name})
            dds_paths.extend(matches)
        dds_paths = list(dict.fromkeys(dds_paths))
        if len(dds_paths) > 16 or len(set(objects)) > 16:
            frontier.append({"kind": "dds_definitions", "reason": "dds_candidate_budget"})
        for path in dds_paths[:16]:
            sha = db.execute("SELECT sha256 FROM source_files WHERE relative_path=?", (path,)).fetchone()
            if not sha:
                continue
            hashes[path] = sha[0]
            lines = _lines(db, path, sha[0], tables, frontier)
            if not lines:
                frontier.append({"kind": "dds_definitions", "relative_path": path, "reason": "dds_source_not_located"})
            numbers = sorted(lines)
            for offset in range(0, len(numbers), _MAX_LINES):
                chunk = numbers[offset:offset + _MAX_LINES]
                row = _span("dds_definitions", path, lines, chunk[0], chunk[-1], sha[0], frontier)
                if row:
                    candidates.append(row)
    unique = {(row["kind"], row["relative_path"], row["start_line"], row["end_line"]): row for row in candidates}
    ordered = sorted(unique.values(), key=lambda row: (
        {"file_io": 0, "file_definitions": 1, "dds_definitions": 2}[row["kind"]],
        scoped.index(row["relative_path"]) if row["relative_path"] in scoped else len(scoped),
        row["relative_path"], row["start_line"]))
    if len(ordered) > max_candidates:
        frontier.append({"kind": "file_io", "reason": "file_impact_candidate_budget",
                         "omitted_count": len(ordered) - max_candidates})
    frontier = list({tuple(sorted(gap.items())): gap for gap in frontier}.values())
    return ordered[:max_candidates], frontier, hashes
