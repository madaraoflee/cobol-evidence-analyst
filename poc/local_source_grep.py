"""Page through literal hits in the imported local source snapshot."""

from __future__ import annotations

from bisect import bisect_right
from contextlib import closing
import hashlib
import json
import sqlite3

from repository_discovery import _connect, _fast_snapshot, _graph_cache_key
from statement_facts import CobolLexError, lex_cobol, strip_inline_comment
from statement_parser import parse_statements
from source_reading import _cancel, _identify_page, _relative_path, _safe_file, _verified_lines


MAX_PAGES = 512
MAX_FILES = 512
RAW_FILE_BYTES = 8 * 1024 * 1024
MAX_RAW_FILES = 2
LAST_PAGE = 2**63 - 1
_SOURCE_ERRORS = {"SOURCE_PATH_INVALID", "SOURCE_HASH_MISMATCH", "SOURCE_ENCODING_INVALID",
                  "SOURCE_LINE_COUNT_MISMATCH", "GREP_RAW_FILE_TOO_LARGE"}


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:24]


def _cursor(binding, file_id, page_id, line, gap):
    position = f"{file_id}.{page_id}.{line}.{int(gap)}"
    return f"{position}.{_fingerprint((binding, position))}"


def _position(binding, token):
    if token is None:
        return 0, 0, 1, False
    if not isinstance(token, str) or len(token) > 128:
        raise ValueError("GREP_CURSOR_INVALID")
    parts = token.split(".")
    if len(parts) != 5 or not all(part.isascii() and part.isdecimal() for part in parts[:4]):
        raise ValueError("GREP_CURSOR_INVALID")
    file_id, page_id, line, gap = map(int, parts[:4])
    if (not 0 < file_id <= LAST_PAGE or not 0 <= page_id <= LAST_PAGE
            or not 0 < line <= LAST_PAGE or gap not in (0, 1)
            or token != _cursor(binding, file_id, page_id, line, gap)):
        raise ValueError("GREP_CURSOR_INVALID")
    return file_id, page_id, line, bool(gap)


def _segments(db, source, page_id, first_line):
    """Yield contiguous physical intervals, including explicit holes needing disk verification."""
    pages = db.execute(
        "SELECT p.page_id,p.evidence_id,p.relative_path,p.start_line,p.end_line,"
        "p.source_sha256,p.span_truncated,e.text AS source_text FROM repo_pages p "
        "LEFT JOIN evidence_spans e ON e.evidence_id=p.evidence_id AND e.relative_path=p.relative_path "
        "AND e.source_sha256=p.source_sha256 AND e.start_line=p.start_line AND e.end_line=p.end_line "
        "WHERE p.relative_path=? AND p.page_id>=? ORDER BY p.page_id", (source["relative_path"], page_id))
    total = source["line_count"]
    for row in pages:
        start, end = row["start_line"], row["end_line"]
        if end < first_line or start > total:
            continue
        if start > first_line:
            yield row["page_id"], first_line, start - 1, None
            first_line = start
        text = row["source_text"]
        if (row["span_truncated"] or text is None or row["source_sha256"] != source["sha256"]
                or end > total or text.count("\n") + 1 != end - start + 1
                or _identify_page(dict(row)) != row["evidence_id"]):
            lines = None
        else:
            lines = text.split("\n")[first_line - start:]
        yield row["page_id"], first_line, min(end, total), lines
        first_line = end + 1
    if first_line <= total:
        yield LAST_PAGE, first_line, total, None


def _raw_hits(root, source, needle, check_cancel, session):
    """Retain only a bounded line-hit bitmap, after verifying all bytes of this one file."""
    key = (str(root), source["relative_path"], source["sha256"], source["encoding"], source["line_count"], needle)
    cache = getattr(session, "_grep_raw_hits", {})
    if key in cache:
        return cache[key]
    candidate = _safe_file(root, source["relative_path"])

    def check_raw_read():
        _cancel(check_cancel)
        try:
            size = candidate.stat().st_size
        except OSError as exc:
            raise ValueError("SOURCE_PATH_INVALID") from exc
        if size > RAW_FILE_BYTES:
            raise ValueError("GREP_RAW_FILE_TOO_LARGE")

    check_raw_read()
    if source["line_count"] > RAW_FILE_BYTES:
        raise ValueError("GREP_RAW_FILE_TOO_LARGE")
    hits = bytearray()
    for text, truncated in _verified_lines(root, source, check_raw_read, RAW_FILE_BYTES):
        if truncated or len(hits) >= RAW_FILE_BYTES:
            raise ValueError("GREP_RAW_FILE_TOO_LARGE")
        hits.append(needle in text.casefold())
    if session is not None:
        if len(cache) >= 2:
            cache.pop(next(iter(cache)))
        cache[key] = hits
        session._grep_raw_hits = cache
    return hits


def _raw_numbers(hits, start, end):
    offset = hits.find(b"\x01", start - 1, end)
    while offset >= 0:
        yield offset + 1
        offset = hits.find(b"\x01", offset + 1, end)


def _grep_guard_ranges(lines, first, hit_line, term, fmt, check_cancel):
    """Find an actual controlling header and branch markers in the hit's complete COBOL sentence."""
    if any(">>" in line or "\t" in line for line in lines):
        return []
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)
    hit_offset = hit_line - first
    text = "\n".join(lines)
    try:
        tokens = lex_cobol(text, source_format=fmt, max_tokens=8000, check_cancel=check_cancel)
    except CobolLexError:
        return []
    # Literal periods and comment text never delimit a COBOL sentence.
    stops = [t.start for t in tokens if t.value == "."]
    before = next((p for p in reversed(stops) if p < offsets[hit_offset]), None)
    after = next((p for p in stops if p >= offsets[hit_offset + 1] - 2), None)
    if before is None or after is None:
        return []
    start = bisect_right(offsets, before) - 1
    body = " " * (before + 1 - offsets[start]) + text[before + 1:after + 1]
    parsed = parse_statements(body, start_line=first + start, source_format=fmt,
                              max_tokens=8000, check_cancel=check_cancel)
    if not parsed.complete:
        return []
    ranges = set()
    guarded_leaves = []
    raw = lines[hit_offset]
    code = strip_inline_comment(raw if fmt == "free" else " " * 7 + raw[7:72])
    if fmt == "fixed" and raw[6:7] in {"*", "/"}:
        return []

    for node in parsed.statements:
        if (node.kind not in {"MOVE", "COMPUTE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE", "CALL"}
                or not node.span.start_line <= hit_line <= node.span.end_line):
            continue
        # An identical word in another statement or in a comment on this line
        # must not associate that unrelated operation with our match.
        left = max(offsets[start] + node.span.start_offset, offsets[hit_offset])
        right = min(offsets[start] + node.span.end_offset, offsets[hit_offset] + len(code))
        if term.casefold() not in text[left:right].casefold():
            continue
        if node.guards:
            guarded_leaves.append(node)
        for guard in node.guards:
            ranges.add((guard.span.start_line, guard.span.end_line))
            if guard.selector_span:
                ranges.add((guard.selector_span.start_line, guard.selector_span.end_line))
    # On the false branch an IF header alone cannot identify the taken arm.
    # Locate its own ELSE between the parsed siblings, even with nested IFs.
    if any(not g.outcome for n in guarded_leaves for g in n.guards):
        nodes = list(parsed.nodes)
        while nodes:
            ancestor = nodes.pop()
            nodes.extend(ancestor.children + ancestor.else_children)
            if ancestor.kind != "IF" or not ancestor.else_children:
                continue
            branch = ancestor.else_children
            if any(branch[0].span.start_offset <= n.span.start_offset <= branch[-1].span.end_offset
                   for n in guarded_leaves):
                lower = offsets[start] + ancestor.children[-1].span.end_offset
                upper = offsets[start] + branch[0].span.start_offset
                marker = next((t for t in tokens if t.value == "ELSE" and lower <= t.start < upper), None)
                if marker:
                    number = first + bisect_right(offsets, marker.start) - 1
                    ranges.add((number, number))
    return sorted(ranges)


def grep_read_location(database_path, hit, term, *, max_chars, check_cancel=None):
    """Extend a literal hit's ordinary read to its verified enclosing condition when it fits."""
    path, line = hit["relative_path"], hit["line"]
    ordinary = {"relative_path": path, "start_line": max(1, line - 12), "end_line": line + 18}
    _cancel(check_cancel)
    try:
        with closing(_connect(database_path)) as db:
            db.execute("BEGIN")
            source = db.execute("SELECT relative_path,sha256,format_hint,line_count FROM source_files "
                                "WHERE relative_path=? AND sha256=?", (path, hit["source_sha256"])).fetchone()
            if not source or source["format_hint"] not in {"fixed", "free"}:
                return ordinary, []
            first, last = max(1, line - 384), min(source["line_count"], line + 128)
            page = db.execute("SELECT page_id FROM repo_pages WHERE relative_path=? "
                              "AND end_line>=? ORDER BY page_id LIMIT 1", (path, first)).fetchone()
            if not page or not first <= line <= last:
                return ordinary, []
            lines, scanned = [], 0
            for n, (_, start, end, segment) in enumerate(_segments(db, source, page[0], first)):
                _cancel(check_cancel)
                if n >= 32 or segment is None:
                    return ordinary, []
                chunk = segment[:last - start + 1]
                scanned += sum(len(row) + 1 for row in chunk)
                if scanned > 64_000:
                    return ordinary, []
                lines.extend(chunk)
                if end >= last:
                    break
    except (sqlite3.Error, OSError):
        return ordinary, []
    if len(lines) != last - first + 1 or term.casefold() not in lines[line - first].casefold():
        return ordinary, []
    guards = _grep_guard_ranges(lines, first, line, term, source["format_hint"], check_cancel)
    # The standard source reader consumes its character allowance from the
    # start of the requested span. Keep expensive preamble lines from crowding
    # out the match itself, even for an unguarded statement.
    size = sum(len(s) + 1 for s in lines[ordinary["start_line"] - first:line - first + 1]) - 1
    while ordinary["start_line"] < line and size > max_chars:
        size -= len(lines[ordinary["start_line"] - first]) + 1
        ordinary["start_line"] += 1
    if not guards or guards[0][0] >= ordinary["start_line"]:
        return ordinary, []
    guard = guards[0][0]
    if first == 1 and last == source["line_count"] and last <= 120 and scanned <= max_chars:
        return {"relative_path": path, "start_line": 1, "end_line": last}, []
    end = min(last, ordinary["end_line"])
    if sum(len(row) + 1 for row in lines[guard - first:end - first + 1]) <= max_chars:
        return {**ordinary, "start_line": guard, "end_line": end}, []
    leads = [{"relative_path": path, "start_line": lo, "end_line": hi} for lo, hi in guards
             if lo < ordinary["start_line"]]
    return ordinary, leads


def grep_repository(database_path, source_root, query, *, cursor=None, limit=12,
                    check_cancel=None, source_session=None, paths=None):
    """Return line locators, requiring an ordinary hash-checked source read for citations.

    checked_files counts files visited in this call, checked_pages counts physical page
    intervals visited (including holes). Both may revisit the boundary page on resume.
    A cursor binds the literal, path scope and index generation. Gaps carry forward.
    """
    if (not isinstance(query, str) or not query.strip() or len(query) > 256
            or len(query.splitlines()) != 1 or query != query.splitlines()[0]):
        raise ValueError("GREP_QUERY_INVALID")
    if type(limit) is not int or not 1 <= limit <= 64:
        raise ValueError("GREP_LIMIT_INVALID")
    if paths is not None:
        if isinstance(paths, str):
            raise ValueError("GREP_SCOPE_INVALID")
        paths = sorted({str(_relative_path(path)) for path in paths})
    needle = query.casefold()
    _cancel(check_cancel)
    with closing(_connect(database_path)) as db:
        db.execute("BEGIN")
        root, overview = _fast_snapshot(db, source_root)
        binding = _fingerprint((_graph_cache_key(db), paths, needle))
        initial_file, initial_page, initial_line, gap = _position(binding, cursor)
        statement = "SELECT rowid AS file_id,relative_path,encoding,sha256,line_count FROM source_files WHERE rowid>=?"
        args = [initial_file]
        if paths is not None:
            statement += " AND relative_path IN (SELECT value FROM json_each(?))"
            args.append(json.dumps(paths))
        result = {"snapshot_id": overview["snapshot_id"], "scope": "indexed_snapshot",
                  "checked_files": 0, "checked_pages": 0, "matches": [], "match_file_lines": {}, "boundaries": [],
                  "next_cursor": None, "reached_end": False, "complete": False}
        raw_files = 0
        for source in db.execute(statement + " ORDER BY rowid", args):
            _cancel(check_cancel)
            file_id = source["file_id"]
            page = initial_page if file_id == initial_file else 0
            line = initial_line if file_id == initial_file else 1
            if result["checked_files"] >= MAX_FILES or result["checked_pages"] >= MAX_PAGES:
                result["next_cursor"] = _cursor(binding, file_id, page, line, gap)
                return result
            result["checked_files"] += 1
            hits, raw_error = None, None
            for page, start, end, lines in _segments(db, source, page, line):
                _cancel(check_cancel)
                if result["checked_pages"] >= MAX_PAGES:
                    result["next_cursor"] = _cursor(binding, file_id, page, start, gap)
                    return result
                result["checked_pages"] += 1
                if lines is None:
                    if hits is None and raw_error is None:
                        if raw_files >= MAX_RAW_FILES:
                            result["next_cursor"] = _cursor(binding, file_id, page, start, gap)
                            return result
                        raw_files += 1
                        try:
                            hits = _raw_hits(root, source, needle, check_cancel, source_session)
                        except ValueError as exc:
                            if str(exc) not in _SOURCE_ERRORS:
                                raise
                            raw_error = str(exc)
                            result["boundaries"].append({"reason": "grep_source_unverified",
                                "reason_code": raw_error, "relative_path": source["relative_path"]})
                    if raw_error:
                        gap = True
                        continue
                    numbers = _raw_numbers(hits, start, end)
                else:
                    numbers = (i for i, text in enumerate(lines, start) if needle in text.casefold())
                for number in numbers:
                    if len(result["matches"]) == limit:
                        result["next_cursor"] = _cursor(binding, file_id, page, number, gap)
                        return result
                    result["match_file_lines"][source["relative_path"]] = source["line_count"]
                    result["matches"].append({"relative_path": source["relative_path"],
                                              "line": number, "source_sha256": source["sha256"]})
        result.update(reached_end=True, complete=not gap)
        return result
