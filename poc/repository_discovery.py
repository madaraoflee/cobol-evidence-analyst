"""Local, snapshot-bound text search and dependency-led repository discovery.

Search hits are investigation leads, not proofs of business relevance. This
module has no business-topic dictionary and never calls a remote service.
"""

from __future__ import annotations

from collections import defaultdict, deque
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from source_reading import _cancel, _identify_page, _persist_page, _relative_path, _safe_file, _verified_lines, _verified_pages


SEARCH_VERSION = "repository-text-v1"
PAGE_CHARS = 12000
MAX_MATCHED_PAGES = 200
MAX_QUERY_TERMS = 128
_WORDS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_$#@.-]*|[\u3400-\u9fff]+")
_PARTS = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|[^A-Za-z0-9]+")
_STOP = frozenset("a an and are as at be by can could do does explain for from how i in is it me of on or please show that the their these this to what when where which who why will with would you your program programs source code business analysis impact find all related affected".split())
_CJK_STOP = frozenset(("请", "请问", "説明", "说明", "解釋", "解释", "分析", "程序", "业务", "業務", "如何", "什么", "什麼", "哪些", "影响", "影響", "相關", "相关", "所有", "找到"))


def _tokens(text):
    """Split separators, camel case and CJK spans without a topic vocabulary."""
    result = []
    # The output already contains each token once. Deduplicate words before
    # splitting, too, so repeated COBOL verbs and identifiers on a source page
    # do not repeat the same tokenization work thousands of times.
    for word in dict.fromkeys(_WORDS.findall(text)):
        if "\u3400" <= word[0] <= "\u9fff":
            if len(word) <= 64:
                result.append(word)
            for size in (4, 3, 2):
                result.extend(word[start:start + size] for start in range(len(word) - size + 1))
        else:
            result.append(word.casefold())
            result.extend(part.casefold() for part in _PARTS.split(word) if part)
    return list(dict.fromkeys(result))


def _query_terms(question, search_terms):
    if not isinstance(question, str) or len(question) > 16000:
        raise ValueError("REPOSITORY_QUESTION_INVALID")
    if search_terms is None:
        inputs = [question]
    elif isinstance(search_terms, (list, tuple)) and all(isinstance(value, str) and len(value) <= 2000 for value in search_terms):
        inputs = [question, *search_terms]
    else:
        raise ValueError("REPOSITORY_SEARCH_TERMS_INVALID")
    terms = list(dict.fromkeys(term for value in inputs for term in _tokens(value)
                              if term not in _STOP and term not in _CJK_STOP and len(term) > 1))
    return terms[:MAX_QUERY_TERMS], max(0, len(terms) - MAX_QUERY_TERMS)


def _connect(database_path, writable=False):
    database = Path(database_path).expanduser()
    if any(Path(str(database) + suffix).is_symlink() for suffix in ("", "-wal", "-shm")):
        raise ValueError("SNAPSHOT_PATH_INVALID")
    connection = sqlite3.connect(database.resolve().as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _schema(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS repo_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS repo_sources (
            relative_path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, encoding TEXT NOT NULL,
            line_count INTEGER NOT NULL, page_count INTEGER NOT NULL, truncated_pages INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS repo_pages (
            page_id INTEGER PRIMARY KEY, evidence_id TEXT, relative_path TEXT NOT NULL,
            start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, source_sha256 TEXT NOT NULL,
            span_truncated INTEGER NOT NULL, fallback_text TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS repo_pages_path ON repo_pages(relative_path);
        CREATE TABLE IF NOT EXISTS repo_source_state (
            relative_path TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
            size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS repo_relations_path ON relations(relative_path,relation_type);
        CREATE VIRTUAL TABLE IF NOT EXISTS repo_fts USING fts5(tokens, tokenize='unicode61 remove_diacritics 2');
    """)
    _ensure_source_state_schema(connection)


def _ensure_source_state_schema(connection):
    columns = {row[1] for row in connection.execute("PRAGMA table_info(repo_source_state)")}
    expected = {"relative_path", "sha256", "size", "mtime_ns"}
    if columns and columns != expected:
        # This table is only a disposable retrieval cache; rebuild it when its
        # metadata layout changes so existing source snapshots remain usable.
        connection.execute("DROP TABLE repo_source_state")
    connection.execute("CREATE TABLE IF NOT EXISTS repo_source_state (relative_path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL)")


def _remove_file(connection, relative):
    connection.execute("DELETE FROM repo_fts WHERE rowid IN (SELECT page_id FROM repo_pages WHERE relative_path=?)", (relative,))
    connection.execute("DELETE FROM repo_pages WHERE relative_path=?", (relative,))
    connection.execute("DELETE FROM repo_sources WHERE relative_path=?", (relative,))
    connection.execute("DELETE FROM repo_source_state WHERE relative_path=?", (relative,))


def ensure_repository_search(database_path, source_root, check_cancel=None, progress=None,
                             *, verify_content=False):
    """Index all snapshot text locally; reuse unchanged verified source pages.

    A failed refresh leaves the search marked unavailable. Its previous pages
    cannot be returned as current evidence, even if the source snapshot id did
    not change yet. Source/page facts are published only after whole-file hashes
    and the enclosing SQLite snapshot have been verified. An unchanged size
    and modification time reuse the prior verification; ``verify_content``
    explicitly rehashes every source, including otherwise unchanged files.
    """
    root_input = Path(source_root).expanduser()
    if root_input.is_symlink():
        raise ValueError("SOURCE_PATH_INVALID")
    root = root_input.resolve()
    connection = _connect(database_path, True)
    try:
        connection.execute("PRAGMA cache_size = -65536")
        _schema(connection)
        connection.execute("INSERT OR REPLACE INTO repo_metadata VALUES ('ready','0')")
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        snapshot = metadata.get("snapshot_id")
        if not snapshot or snapshot == "unknown":
            raise ValueError("SNAPSHOT_INVALID")
        expected_root = metadata.get("source_root_hash")
        if expected_root and expected_root != hashlib.sha256(str(root).encode()).hexdigest():
            raise ValueError("SOURCE_ROOT_MISMATCH")
        files = [dict(row) for row in connection.execute("SELECT relative_path,sha256,encoding,line_count FROM source_files ORDER BY relative_path")]
        if not files:
            raise ValueError("SOURCE_INDEX_EMPTY")
        prior = dict(connection.execute("SELECT key,value FROM repo_metadata"))
        existing = {row["relative_path"]: dict(row) for row in connection.execute("SELECT * FROM repo_sources")}
        states = {row["relative_path"]: dict(row) for row in connection.execute("SELECT * FROM repo_source_state")}
        current_paths = {item["relative_path"] for item in files}
        removed = set(existing) - current_paths
        for relative in removed:
            _remove_file(connection, relative)
        updated, cached, metadata_cached, verified_files = 0, 0, 0, 0
        for offset, item in enumerate(files, 1):
            _cancel(check_cancel)
            relative = item["relative_path"]
            verified_before = _safe_file(root, relative).stat()
            previous = existing.get(relative, {})
            reusable = prior.get("version") == SEARCH_VERSION and all(previous.get(key) == item[key] for key in ("sha256", "encoding", "line_count"))
            # A structural rebuild may remove old evidence even for unchanged
            # text. Recreate that file rather than returning dangling citations.
            if reusable:
                missing = connection.execute("SELECT COUNT(*) FROM repo_pages p LEFT JOIN evidence_spans e ON e.evidence_id=p.evidence_id WHERE p.relative_path=? AND p.span_truncated=0 AND e.evidence_id IS NULL", (relative,)).fetchone()[0]
                reusable = not missing
            if reusable:
                state = states.get(relative, {})
                if (not verify_content and state.get("sha256") == item["sha256"]
                        and state.get("size") == verified_before.st_size
                        and state.get("mtime_ns") == verified_before.st_mtime_ns):
                    metadata_cached += 1
                else:
                    for _ in _verified_lines(root, item, check_cancel, 1):
                        pass
                    verified_files += 1
                cached += 1
            else:
                _remove_file(connection, relative)
                count, truncated = 0, 0
                for page in _verified_pages(root, item, check_cancel, set(), PAGE_CHARS):
                    _cancel(check_cancel)
                    page["evidence_id"] = _identify_page(page)
                    _persist_page(connection, page)
                    cursor = connection.execute("INSERT INTO repo_pages(evidence_id,relative_path,start_line,end_line,source_sha256,span_truncated,fallback_text) VALUES (?,?,?,?,?,?,?)",
                        (None if page["span_truncated"] else page["evidence_id"], relative, page["start_line"], page["end_line"], item["sha256"], int(page["span_truncated"]), page["source_text"] if page["span_truncated"] else ""))
                    tokens = " ".join(_tokens(relative + "\n" + page["source_text"]))
                    connection.execute("INSERT INTO repo_fts(rowid,tokens) VALUES (?,?)", (cursor.lastrowid, tokens))
                    count += 1
                    truncated += int(page["span_truncated"])
                connection.execute("INSERT INTO repo_sources VALUES (?,?,?,?,?,?)", (relative, item["sha256"], item["encoding"], item["line_count"], count, truncated))
                updated += 1
                verified_files += 1
            info = _safe_file(root, relative).stat()
            if (verified_before.st_size, verified_before.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                raise ValueError("SOURCE_HASH_MISMATCH")
            connection.execute("INSERT OR REPLACE INTO repo_source_state VALUES (?,?,?,?)",
                               (relative, item["sha256"], info.st_size, info.st_mtime_ns))
            if progress:
                progress({"phase": "repository_search", "stage": "indexing", "completed": offset,
                          "total": len(files), "unit": "files", "current_file": relative})
        truncated = [dict(row) for row in connection.execute("SELECT relative_path,truncated_pages FROM repo_sources WHERE truncated_pages>0 ORDER BY relative_path")]
        boundaries = [{"reason": "source_line_exceeds_search_page_budget", **row,
                       "message": "Oversized source lines were indexed only as bounded excerpts; text-search coverage is incomplete."} for row in truncated]
        programs = [dict(row) for row in connection.execute("SELECT name AS program_name,relative_path FROM code_units WHERE unit_type='Program' ORDER BY relative_path,start_line LIMIT 80")]
        program_count = connection.execute("SELECT COUNT(*) FROM code_units WHERE unit_type='Program'").fetchone()[0]
        overview = {"snapshot_id": snapshot, "indexed_files": len(files),
                    "indexed_pages": connection.execute("SELECT COUNT(*) FROM repo_pages").fetchone()[0],
                    "total_lines": sum(item["line_count"] for item in files), "updated_files": updated,
                    "cached_files": cached, "deleted_files": len(removed), "full_text_complete": not truncated,
                    "metadata_cache_reused": metadata_cached, "content_hash_verified": verified_files,
                    "program_samples": programs, "program_count": program_count,
                    "omitted_program_samples": max(0, program_count - len(programs)), "boundaries": boundaries}
        connection.executemany("INSERT OR REPLACE INTO repo_metadata VALUES (?,?)",
                               (("snapshot_id", snapshot), ("version", SEARCH_VERSION), ("overview", json.dumps(overview)), ("ready", "1")))
        connection.commit()
        return overview
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _snippet(text, terms, budget=1000):
    folded = text.casefold()
    positions = [folded.find(term.casefold()) for term in terms]
    positions = [position for position in positions if position >= 0]
    start = max(0, min(positions, default=0) - budget // 4)
    return text[start:start + budget], start


def _dependency_selection(connection, seeds, all_paths, check_cancel):
    forward, incoming = defaultdict(list), defaultdict(list)
    aliases = defaultdict(set)
    for relative in all_paths:
        for alias in (relative, Path(relative).name, Path(relative).stem):
            aliases[alias.casefold()].add(relative)
    unresolved = []
    for row in connection.execute("SELECT r.relative_path,r.relation_type,r.target_name,r.status,r.evidence_id,s.relative_path AS target_path FROM relations r LEFT JOIN symbols s ON s.symbol_id=r.target_entity_id WHERE r.relation_type IN ('CALLS','CALL_TARGET_FROM','INCLUDES_COPY') ORDER BY r.relative_path,r.relation_id"):
        _cancel(check_cancel)
        edge = dict(row)
        target = edge["target_path"] if edge["status"] == "confirmed" and edge["relation_type"] != "CALL_TARGET_FROM" else None
        if not target and edge["relation_type"] == "INCLUDES_COPY":
            candidates = aliases.get((edge["target_name"] or "").casefold(), set())
            if len(candidates) == 1:
                target = next(iter(candidates))
                edge["resolution"] = "unbound_copy_candidate"
        if target in all_paths:
            edge["target_path"] = target
            forward[edge["relative_path"]].append(edge)
            incoming[target].append(edge)
        if edge["status"] != "confirmed" or target not in all_paths:
            unresolved.append(edge)
    reasons = defaultdict(set)
    for seed in seeds:
        reasons[seed].add("text_match")
    # Only seeds and their caller ancestry drive reverse traversal. A shared
    # definition discovered downstream must not pull in sibling businesses.
    queue, visited, deferred = deque(sorted(seeds)), set(), []
    hub_limit = max(8, (len(all_paths) + 3) // 4)
    while queue:
        _cancel(check_cancel)
        relative = queue.popleft()
        if relative in visited:
            continue
        visited.add(relative)
        parents = incoming[relative]
        is_hub = len({edge["relative_path"] for edge in parents}) >= hub_limit
        for edge in parents:
            caller = edge["relative_path"]
            if is_hub and caller not in seeds:
                deferred.append({"relative_path": caller, "via_path": relative,
                                 "reason": "shared_dependency_reverse_expansion_deferred",
                                 "relation_type": edge["relation_type"], "evidence_id": edge["evidence_id"]})
                continue
            reasons[caller].add("caller_of:" + relative)
            if caller not in visited:
                queue.append(caller)
    queue, visited = deque(sorted(reasons)), set()
    while queue:
        _cancel(check_cancel)
        relative = queue.popleft()
        if relative in visited:
            continue
        visited.add(relative)
        for edge in forward[relative]:
            target = edge["target_path"]
            reasons[target].add("dependency_of:" + relative)
            if target not in visited:
                queue.append(target)
    selected = set(reasons)
    unresolved = [dict(edge, reason="dependency_target_unresolved") for edge in unresolved if edge["relative_path"] in selected]
    deferred = [item for item in deferred if item["relative_path"] not in selected]
    return sorted(selected), [{"relative_path": relative, "reasons": sorted(reasons[relative])} for relative in sorted(reasons)], deferred, unresolved


def discover_repository(database_path, question, *, search_terms=None, check_cancel=None):
    """Retrieve all matching files, then expand supported static dependencies.

    The evidence preview is bounded, but matching paths are not top-k clipped.
    Missing and dynamic targets remain visible. With no lexical matches, the
    indexed repository is selected for reading instead of rejecting the query.
    """
    terms, omitted_terms = _query_terms(question, search_terms)
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN")
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        search_metadata = dict(connection.execute("SELECT key,value FROM repo_metadata"))
        if search_metadata.get("ready") != "1" or search_metadata.get("snapshot_id") != metadata.get("snapshot_id") or search_metadata.get("version") != SEARCH_VERSION:
            raise ValueError("REPOSITORY_SEARCH_STALE")
        stale = connection.execute("SELECT COUNT(*) FROM repo_sources r LEFT JOIN source_files s USING(relative_path) WHERE s.sha256 IS NULL OR r.sha256!=s.sha256 OR r.encoding!=s.encoding OR r.line_count!=s.line_count").fetchone()[0]
        missing = connection.execute("SELECT COUNT(*) FROM source_files s LEFT JOIN repo_sources r USING(relative_path) WHERE r.sha256 IS NULL").fetchone()[0]
        if stale or missing:
            raise ValueError("REPOSITORY_SEARCH_STALE")
        overview = json.loads(search_metadata["overview"])
        all_paths = {row[0] for row in connection.execute("SELECT relative_path FROM source_files")}
        query = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        matches, seeds, matched_count = [], set(), 0
        preview_by_path, preview_count = {}, 0
        if query:
            rows = connection.execute("SELECT p.*,bm25(repo_fts) AS score FROM repo_fts JOIN repo_pages p ON p.page_id=repo_fts.rowid WHERE repo_fts MATCH ? ORDER BY score,p.relative_path,p.start_line", (query,))
            for row in rows:
                _cancel(check_cancel)
                relative = row["relative_path"]
                seeds.add(relative)
                matched_count += 1
                if preview_count >= MAX_MATCHED_PAGES and relative not in preview_by_path:
                    crowded = max(preview_by_path, key=lambda path: len(preview_by_path[path]))
                    if len(preview_by_path[crowded]) > 1:
                        preview_by_path[crowded].pop()
                        preview_count -= 1
                if preview_count < MAX_MATCHED_PAGES:
                    preview_by_path.setdefault(relative, deque()).append(dict(row))
                    preview_count += 1
            # Show one page from each matching file before second pages, while
            # retaining every matching path even when the preview is bounded.
            preview_queue = deque(preview_by_path.values())
            while preview_queue:
                pages = preview_queue.popleft()
                row = pages.popleft()
                evidence = connection.execute("SELECT text FROM evidence_spans WHERE evidence_id=?", (row["evidence_id"],)).fetchone()
                if not row["span_truncated"] and evidence is None:
                    raise ValueError("REPOSITORY_SEARCH_STALE")
                snippet, offset = _snippet(evidence[0] if evidence else row["fallback_text"], terms)
                matches.append({key: row[key] for key in ("evidence_id", "relative_path", "start_line", "end_line", "source_sha256", "span_truncated", "score")} | {"snippet": snippet, "snippet_char_offset": offset})
                if pages:
                    preview_queue.append(pages)
        fallback = not seeds
        paths, reasons, deferred, unresolved = _dependency_selection(connection, all_paths if fallback else seeds, all_paths, check_cancel)
        if fallback:
            paths = sorted(all_paths)
            reasons = [{"relative_path": relative, "reasons": ["no_text_match_repository_fallback"]} for relative in paths]
        boundaries = list(overview.get("boundaries", [])) + unresolved
        if deferred:
            boundaries.append({"reason": "shared_dependency_reverse_expansion_deferred", "candidate_count": len(deferred),
                               "message": "Shared dependencies have additional callers. They are listed as candidates; relevance requires further investigation."})
        if omitted_terms:
            boundaries.append({"reason": "query_terms_limited", "omitted_terms": omitted_terms})
        return {"snapshot_id": metadata["snapshot_id"], "selected_paths": paths,
                "matched_pages": matches, "matched_file_count": len(seeds), "matched_page_count": matched_count,
                "omitted_matched_pages": matched_count - len(matches), "selected_file_count": len(paths),
                "omitted_matched_file_previews": len(seeds) - len(preview_by_path),
                "repository_file_count": len(all_paths), "selection_reasons": reasons,
                "search_terms": terms, "fallback_all": fallback, "deferred_candidates": deferred,
                "boundaries": boundaries, "selection_is_relevance_proof": False,
                "all_matching_files_selected": True, "dependency_expansion_complete": not deferred and not unresolved}
    finally:
        connection.close()


# Interactive questions read persisted evidence, not every source file. Index
# refresh is an explicit ingestion operation; the snapshot remains identifiable.
def _fast_snapshot(connection, source_root=None):
    metadata = dict(connection.execute("SELECT key,value FROM metadata WHERE key IN ('snapshot_id','source_root_hash','indexed_at_utc','index_kind')"))
    try:
        search = dict(connection.execute("SELECT key,value FROM repo_metadata"))
    except sqlite3.OperationalError as exc:
        raise ValueError("REPOSITORY_SEARCH_NOT_READY") from exc
    if (search.get("ready") != "1" or search.get("snapshot_id") != metadata.get("snapshot_id")
            or search.get("version") != SEARCH_VERSION):
        raise ValueError("REPOSITORY_SEARCH_STALE")
    if source_root is not None:
        raw = Path(source_root).expanduser()
        if raw.is_symlink():
            raise ValueError("SOURCE_PATH_INVALID")
        root = raw.resolve()
        expected = metadata.get("source_root_hash")
        if expected and expected != hashlib.sha256(str(root).encode()).hexdigest():
            raise ValueError("SOURCE_ROOT_MISMATCH")
    else:
        root = None
    overview = json.loads(search["overview"])
    if overview.get("snapshot_id") != metadata.get("snapshot_id"):
        raise ValueError("REPOSITORY_SEARCH_STALE")
    return root, {**overview, "indexed_at_utc": metadata.get("indexed_at_utc"),
                  "cache_reused": True, "source_refresh_performed": False,
                  "source_state_scope": "indexed_snapshot"}


def repository_search_overview(database_path, source_root=None):
    """Open the existing search snapshot without listing or reading source files."""
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN")
        return _fast_snapshot(connection, source_root)[1]
    finally:
        connection.close()


def _state_schema(connection):
    # Supports a previously built index without asking users to reimport it.
    _ensure_source_state_schema(connection)


def _current_selected_source(connection, root, relative, cache, check_cancel, source_session=None):
    """Check only a retrieved file against one question's captured bytes."""
    if relative in cache["states"]:
        return cache["states"][relative]
    _cancel(check_cancel)
    item = connection.execute("SELECT relative_path,sha256,encoding,line_count FROM source_files WHERE relative_path=?", (relative,)).fetchone()
    state = {"relative_path": relative, "current": False}
    if item is None:
        state["reason_code"] = "SOURCE_NOT_INDEXED"
        cache["states"][relative] = state
        return state
    if source_session is not None:
        try:
            captured = source_session.capture(relative)
            cache["checked_files"] += 1
            cache["content_verified_files"] += 1
            if captured.sha256 != item["sha256"]:
                state.update(reason_code="SOURCE_HASH_MISMATCH", captured_sha256=captured.sha256)
            else:
                state.update(current=True, verification="content_hash", source_sha256=captured.sha256)
        except ValueError as exc:
            state["reason_code"] = str(exc)
        cache["states"][relative] = state
        return state
    try:
        info = _safe_file(root, relative).stat()
        cache["checked_files"] += 1
        observed = [info.st_size, info.st_mtime_ns]
        saved = connection.execute("SELECT sha256,size,mtime_ns FROM repo_source_state WHERE relative_path=?", (relative,)).fetchone()
        expected = list(saved)[1:] if saved and saved["sha256"] == item["sha256"] else None
        if expected is None:
            if "legacy_stats" not in cache:
                row = connection.execute("SELECT value FROM metadata WHERE key='file_stats'").fetchone()
                cache["legacy_stats"] = json.loads(row[0]) if row else {}
            expected = cache["legacy_stats"].get(relative)
        if expected == observed:
            cache["reused_files"] += 1
            state.update(current=True, verification="size_mtime", source_sha256=item["sha256"])
        else:
            cache["content_verified_files"] += 1
            for _ in _verified_lines(root, dict(item), check_cancel, 1):
                pass
            after = _safe_file(root, relative).stat()
            if observed != [after.st_size, after.st_mtime_ns]:
                raise ValueError("SOURCE_HASH_MISMATCH")
            state.update(current=True, verification="content_hash", source_sha256=item["sha256"])
        connection.execute("INSERT OR REPLACE INTO repo_source_state VALUES (?,?,?,?)", (relative, item["sha256"], *observed))
    except ValueError as exc:
        if str(exc) not in {"SOURCE_PATH_INVALID", "SOURCE_HASH_MISMATCH", "SOURCE_LINE_COUNT_MISMATCH", "SOURCE_ENCODING_INVALID"}:
            raise
        state["reason_code"] = str(exc)
    cache["states"][relative] = state
    return state


def _bounded_page(connection, row, terms, budget, *, start_line=None, end_line=None):
    """Crop on physical line boundaries and persist a citation for the crop."""
    if row["span_truncated"] or not row["evidence_id"]:
        return None
    evidence = connection.execute("SELECT relative_path,start_line,end_line,source_sha256,text FROM evidence_spans WHERE evidence_id=?", (row["evidence_id"],)).fetchone()
    if evidence is None or any(evidence[key] != row[key] for key in ("relative_path", "start_line", "end_line", "source_sha256")):
        raise ValueError("REPOSITORY_SEARCH_STALE")
    original = {**dict(evidence), "source_text": evidence["text"], "span_truncated": False}
    if _identify_page(original) != row["evidence_id"]:
        raise ValueError("EVIDENCE_ID_CONFLICT")
    lines = evidence["text"].split("\n")
    lower = max(0, (start_line or row["start_line"]) - row["start_line"])
    upper = min(len(lines), (end_line or row["end_line"]) - row["start_line"] + 1)
    if lower >= upper:
        return None
    if start_line is None:
        folded = [line.casefold() for line in lines]
        hits = [(sum(min(32, len(term)) for term in terms if term in text), index)
                for index, text in enumerate(folded)]
        _, center = max(hits, default=(0, lower), key=lambda pair: (pair[0], -pair[1]))
        if row.get("focus_line") is not None:
            center = row["focus_line"] - row["start_line"]
        center = min(max(center, lower), upper - 1)
        first, last, used = center, center + 1, len(lines[center])
        while used <= budget and (first > lower or last < upper):
            # Keep immediate context on both sides of the matching line.
            changed = False
            for direction in (-1, 1):
                index = first - 1 if direction < 0 else last
                if lower <= index < upper and used + len(lines[index]) + 1 <= budget:
                    used += len(lines[index]) + 1
                    first, last = (index, last) if direction < 0 else (first, index + 1)
                    changed = True
            if not changed:
                break
    else:
        first, last, used = lower, lower, 0
        while last < upper and used + len(lines[last]) + bool(last > first) <= budget:
            used += len(lines[last]) + bool(last > first)
            last += 1
    if first >= last or used > budget:
        return None
    page = {"relative_path": row["relative_path"], "start_line": row["start_line"] + first,
            "end_line": row["start_line"] + last - 1, "source_sha256": row["source_sha256"],
            "source_text": "\n".join(lines[first:last]), "span_truncated": False}
    page["evidence_id"] = _identify_page(page)
    page["source_characters"] = len(page["source_text"])
    _persist_page(connection, page)
    return page


def _context_relations(connection, paths, limit=64):
    if not paths:
        return [], 0
    slots = ",".join("?" for _ in paths)
    query = ("SELECT r.relation_id,r.relative_path AS caller_path,r.relation_type,r.target_name,r.status AS resolution,"
             "r.evidence_id,e.start_line AS caller_start_line,e.end_line AS caller_end_line,"
             "s.relative_path AS target_path,u.start_line AS target_start_line,u.end_line AS target_end_line "
             "FROM relations r JOIN evidence_spans e ON e.evidence_id=r.evidence_id "
             "LEFT JOIN symbols s ON s.symbol_id=r.target_entity_id "
             "LEFT JOIN code_units u ON u.unit_id=s.definition_unit_id "
             f"WHERE r.relation_type IN ('CALLS','CALL_TARGET_FROM','INCLUDES_COPY','PERFORMS') "
             f"AND (r.relative_path IN ({slots}) OR s.relative_path IN ({slots})) "
             "ORDER BY r.relation_type='PERFORMS',r.relative_path,e.start_line LIMIT ?")
    rows = connection.execute(query, (*paths, *paths, limit + 1)).fetchall()
    result = []
    for row in rows[:limit]:
        item = dict(row)
        if item["relation_type"] == "CALL_TARGET_FROM" or item["resolution"] != "confirmed":
            item.update(target_path=None, target_start_line=None, target_end_line=None)
        item["target_source_status"] = "indexed" if item["target_path"] else "unavailable"
        item["runtime_verified"] = False
        item["parameter_binding_verified"] = False
        result.append(item)
    return result, max(0, len(rows) - limit)


def _retrieved_outline(connection, relative, pages, limit=24):
    """Return enclosing structures near the actual excerpts, including deep hits.

    These are complete indexed source ranges for a subsequent read, not a
    claim that the corresponding paragraph text has already been supplied.
    Existing snapshots contain all required fields; no reindex is needed.
    """
    spans = [(page["start_line"], page["end_line"]) for page in pages
             if page["relative_path"] == relative]
    values = ",".join("(?,?)" for _ in spans)
    query = f"""
        WITH spans(first_line,last_line) AS (VALUES {values}),
        units AS (
            SELECT unit_id,unit_type,name,program_name,start_line,end_line
            FROM code_units WHERE relative_path=?
              AND unit_type IN ('Program','Section','Paragraph','ProcedureSignature')
        ), programs AS (
            SELECT start_line,end_line FROM units u WHERE unit_type='Program'
              AND EXISTS (SELECT 1 FROM spans p
                          WHERE u.start_line<=p.last_line AND u.end_line>=p.first_line)
        )
        SELECT u.*,
          CASE
            WHEN unit_type='Program' AND EXISTS (SELECT 1 FROM spans p
                 WHERE u.start_line<=p.last_line AND u.end_line>=p.first_line) THEN 0
            WHEN unit_type='ProcedureSignature' AND EXISTS (SELECT 1 FROM programs p
                 WHERE u.start_line BETWEEN p.start_line AND p.end_line) THEN 1
            WHEN EXISTS (SELECT 1 FROM spans p
                 WHERE u.start_line<=p.first_line AND u.end_line>=p.last_line) THEN 2
            WHEN EXISTS (SELECT 1 FROM spans p
                 WHERE u.start_line<=p.last_line AND u.end_line>=p.first_line) THEN 3
            ELSE 4
          END AS context_rank,
          (SELECT MIN(CASE WHEN u.end_line<p.first_line THEN p.first_line-u.end_line
                          WHEN u.start_line>p.last_line THEN u.start_line-p.last_line
                          ELSE 0 END) FROM spans p) AS distance,
          COUNT(*) OVER () AS total_units
        FROM units u
        ORDER BY context_rank,distance,(end_line-start_line),start_line LIMIT ?
    """
    rows = connection.execute(query, (*[line for span in spans for line in span], relative, limit)).fetchall()
    units = []
    roles = ("program", "procedure_signature", "enclosing_structure", "overlapping_structure", "nearby_structure")
    for row in rows:
        item = {key: row[key] for key in ("unit_type", "name", "program_name", "start_line", "end_line")}
        item["context_role"] = roles[row["context_rank"]]
        item["complete_text_supplied"] = any(first <= row["start_line"] and last >= row["end_line"]
                                              for first, last in spans)
        units.append(item)
    return {"relative_path": relative, "units": units,
            "retrieved_ranges": [{"start_line": first, "end_line": last} for first, last in spans],
            "selection": "retrieved_source_structure",
            "omitted_units": max(0, (rows[0]["total_units"] if rows else 0) - len(units))}


def _assemble_context(connection, root, overview, rows, terms, *, max_pages, max_chars,
                      check_cancel=None, requested_span=None, source_session=None):
    _state_schema(connection)
    cache = {"states": {}, "checked_files": 0, "reused_files": 0, "content_verified_files": 0}
    pages, boundaries, used, seen = [], [], 0, set()
    per_page = max(512, min(6000, max_chars // max(1, min(max_pages, len(rows)))))
    for row in rows:
        if len(pages) >= max_pages or used >= max_chars:
            break
        _cancel(check_cancel)
        state = _current_selected_source(connection, root, row["relative_path"], cache, check_cancel, source_session)
        if not state["current"]:
            continue
        kwargs = requested_span or {}
        budget = max_chars - used if requested_span else min(per_page, max_chars - used)
        page = _bounded_page(connection, row, terms, budget, **kwargs)
        if page and page["evidence_id"] not in seen:
            page["selection_reasons"] = [row.get("selection_reason", "question_match")]
            page["source_verification"] = state["verification"]
            seen.add(page["evidence_id"])
            pages.append(page)
            used += len(page["source_text"])
            if requested_span and page["end_line"] < min(row["end_line"], requested_span["end_line"]):
                break
    if any(row["span_truncated"] for row in rows):
        boundaries.append({"reason": "source_line_exceeds_search_page_budget", "message": "An oversized source line has no complete source citation in this index."})
    selected = list(dict.fromkeys(page["relative_path"] for page in pages))
    links, omitted = _context_relations(connection, selected)
    usable = []
    for link in links:
        state = _current_selected_source(connection, root, link["caller_path"], cache, check_cancel, source_session)
        if state["current"]:
            link["caller_evidence_ids"] = [page["evidence_id"] for page in pages
                if page["relative_path"] == link["caller_path"]
                and page["start_line"] <= link["caller_start_line"] and page["end_line"] >= link["caller_end_line"]]
            link["target_evidence_ids"] = [page["evidence_id"] for page in pages
                if page["relative_path"] == link["target_path"] and page["start_line"] <= link["target_start_line"] <= page["end_line"]]
            usable.append(link)
    for state in cache["states"].values():
        if not state["current"]:
            boundaries.append({"reason": "source_changed_since_index", "relative_path": state["relative_path"],
                               "reason_code": state["reason_code"], "needs_refresh": True,
                               "message": "This retrieved file differs from the indexed snapshot or is unavailable; its old text was not supplied."})
    outline = []
    for relative in selected:
        outline.append(_retrieved_outline(connection, relative, pages))
    connection.commit()
    cache_report = {key: value for key, value in cache.items() if key not in {"states", "legacy_stats"}}
    cache_report.update(index_reused=True, source_directory_scanned=False, source_files_reparsed=0)
    return {"snapshot_id": overview["snapshot_id"], "pages": pages,
            "evidence_refs": [{key: value for key, value in page.items() if key != "source_text"} for page in pages],
            "selected_paths": selected, "outline": outline, "boundaries": boundaries,
            "call_chain": {"scope": "retrieved_sources_and_immediate_neighbours", "links": usable,
                           "truncated": bool(omitted), "minimum_omitted_links": omitted},
            "coverage": {"scope": "retrieved_excerpts", "repository_total_files": overview["indexed_files"],
                         "repository_total_pages": overview["indexed_pages"], "selected_files": len(selected),
                         "selected_pages": len(pages), "selected_characters": used, "max_pages": max_pages,
                         "max_characters": max_chars, "repository_complete": False,
                         "source_state_scope": "selected_files_only", "new_files_checked": False,
                         "selected_source_hashes_verified": bool(cache["states"]) and all(
                             state.get("current") and state.get("verification") == "content_hash"
                             for state in cache["states"].values()),
                         "current_content_hashes_recomputed": cache["content_verified_files"]},
            "cache": cache_report, "needs_refresh": bool(boundaries and any(item.get("needs_refresh") for item in boundaries))}


def retrieve_repository_context(database_path, source_root, question, *, search_terms=None,
                                prior_paths=None, max_pages=8, max_chars=32000, check_cancel=None,
                                source_session=None):
    """Retrieve a small evidence bundle and neighbouring structure from SQLite.

    No-match questions get representative indexed pages, never a full-repository
    source read. The agent can search again or request explicit source ranges.
    """
    if type(max_pages) is not int or not 1 <= max_pages <= 32 or type(max_chars) is not int or not 512 <= max_chars <= 128000:
        raise ValueError("RETRIEVAL_BUDGET_INVALID")
    terms, omitted_terms = _query_terms(question, search_terms)
    connection = _connect(database_path, True)
    try:
        connection.execute("BEGIN")
        root, overview = _fast_snapshot(connection, source_root)
        query = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        ranked, matched_count, matched_files = [], 0, 0
        if query:
            matched_count, matched_files = connection.execute("SELECT COUNT(*),COUNT(DISTINCT p.relative_path) FROM repo_fts JOIN repo_pages p ON p.page_id=repo_fts.rowid WHERE repo_fts MATCH ?", (query,)).fetchone()
            # Explicit follow-up searches take priority over previous-question
            # terms. A new rare identifier must not lose to repeated old hits.
            additions, _ = _query_terms("", search_terms)
            refined_query = " OR ".join('"' + term.replace('"', '""') + '"' for term in additions)
            seen = set()
            for active_query in dict.fromkeys(value for value in (refined_query, query) if value):
                candidates = connection.execute(
                    "WITH matches AS MATERIALIZED (SELECT p.*,bm25(repo_fts) AS score FROM repo_fts "
                    "JOIN repo_pages p ON p.page_id=repo_fts.rowid WHERE repo_fts MATCH ?) "
                    "SELECT *,ROW_NUMBER() OVER (PARTITION BY relative_path ORDER BY score,start_line) AS file_rank "
                    "FROM matches ORDER BY file_rank,score,relative_path,start_line LIMIT ?", (active_query, max_pages * 16))
                for row in candidates:
                    if row["page_id"] not in seen:
                        ranked.append(dict(row))
                        seen.add(row["page_id"])
        # Across-file diversity prevents an enormous matching program from
        # displacing every other implementation. Extra pages remain searchable.
        by_path = {}
        for row in ranked:
            by_path.setdefault(row["relative_path"], deque()).append(row)
        queue, rows = deque(by_path.values()), []
        while queue:
            group = queue.popleft()
            rows.append(group.popleft())
            if group:
                queue.append(group)
        preferred, preferred_ids = [], set()
        for requested in (prior_paths or [])[:max_pages]:
            if not isinstance(requested, str):
                continue
            candidate = requested.split("::", 1)[0]
            _relative_path(candidate)
            paths = [row[0] for row in connection.execute("SELECT relative_path FROM source_files WHERE relative_path=? COLLATE NOCASE UNION SELECT relative_path FROM code_units WHERE unit_type='Program' AND name=? COLLATE NOCASE LIMIT 2", (candidate, requested))]
            for relative in paths:
                row = None
                if query:
                    row = connection.execute("SELECT p.*,bm25(repo_fts) AS score FROM repo_fts JOIN repo_pages p ON p.page_id=repo_fts.rowid WHERE repo_fts MATCH ? AND p.relative_path=? ORDER BY score,p.start_line LIMIT 1", (query, relative)).fetchone()
                if row is None:
                    row = connection.execute("SELECT * FROM repo_pages WHERE relative_path=? ORDER BY start_line LIMIT 1", (relative,)).fetchone()
                if row and row["page_id"] not in preferred_ids:
                    preferred.append({**dict(row), "selection_reason": "conversation_context"})
                    preferred_ids.add(row["page_id"])
        if not rows:
            rows = preferred or [dict(row, selection_reason="repository_orientation") for row in connection.execute("SELECT p.* FROM repo_pages p JOIN (SELECT relative_path,MIN(start_line) AS first_line FROM repo_pages GROUP BY relative_path) f ON p.relative_path=f.relative_path AND p.start_line=f.first_line ORDER BY p.relative_path LIMIT ?", (max_pages,))]
        else:
            reserved = preferred[:min(2, max(1, max_pages // 2))]
            # Keep the previous turn's source context available for pronouns.
            # Explicit new search terms still receive the first retrieval slot.
            initial = rows[:1] if search_terms else []
            combined = initial + reserved + rows + preferred[len(reserved):]
            seen_rows, rows = set(), []
            for row in combined:
                if row["page_id"] not in seen_rows:
                    rows.append(row)
                    seen_rows.add(row["page_id"])
        # Reserve a small part of the bundle for called interfaces and callers.
        seeds = list(dict.fromkeys(row["relative_path"] for row in rows[:max(1, max_pages // 2)]))
        links, _ = _context_relations(connection, seeds)
        neighbours, neighbour_ids = [], set()
        for link in links:
            for relative, line in ((link["caller_path"], link["caller_start_line"]), (link["target_path"], link["target_start_line"])):
                if not relative or line is None:
                    continue
                row = connection.execute("SELECT * FROM repo_pages WHERE relative_path=? AND start_line<=? AND end_line>=? ORDER BY start_line LIMIT 1", (relative, line, line)).fetchone()
                if row and row["page_id"] not in neighbour_ids and row["page_id"] not in {item["page_id"] for item in rows[:max(1, max_pages // 2)]}:
                    neighbours.append({**dict(row), "selection_reason": "dependency_context", "focus_line": line})
                    neighbour_ids.add(row["page_id"])
                if len(neighbours) >= max(1, max_pages // 3):
                    break
            if len(neighbours) >= max(1, max_pages // 3):
                break
        cut = max(1, max_pages - len(neighbours))
        rows = rows[:cut] + neighbours + rows[cut:]
        result = _assemble_context(connection, root, overview, rows, terms, max_pages=max_pages,
                                   max_chars=max_chars, check_cancel=check_cancel, source_session=source_session)
        result.update(search_terms=terms, omitted_search_terms=omitted_terms,
                      matched_page_count=matched_count, matched_file_count=matched_files,
                      fallback_all=False, orientation_only=not matched_count,
                      omitted_matched_pages=max(0, matched_count - sum("question_match" in page["selection_reasons"] for page in result["pages"])))
        return result
    finally:
        connection.close()


def read_repository_context(database_path, source_root, *, relative_path=None, start_line=1,
                            end_line=None, evidence_id=None, max_chars=12000, check_cancel=None,
                            source_session=None):
    """Read a requested indexed range or citation without parsing the program."""
    if type(max_chars) is not int or not 512 <= max_chars <= 128000:
        raise ValueError("RETRIEVAL_BUDGET_INVALID")
    connection = _connect(database_path, True)
    try:
        connection.execute("BEGIN")
        root, overview = _fast_snapshot(connection, source_root)
        if evidence_id is not None:
            if not isinstance(evidence_id, str):
                raise ValueError("EVIDENCE_ID_INVALID")
            evidence = connection.execute("SELECT relative_path,start_line,end_line FROM evidence_spans WHERE evidence_id=?", (evidence_id,)).fetchone()
            if evidence is None:
                raise ValueError("EVIDENCE_NOT_FOUND")
            relative_path, start_line, end_line = tuple(evidence)
        if not isinstance(relative_path, str):
            raise ValueError("SOURCE_PATH_INVALID")
        _relative_path(relative_path)
        if type(start_line) is not int or start_line < 1 or (end_line is not None and (type(end_line) is not int or end_line < start_line)):
            raise ValueError("SOURCE_RANGE_INVALID")
        end_line = end_line if end_line is not None else start_line + 119
        rows = [dict(row, selection_reason="agent_requested_read") for row in connection.execute("SELECT * FROM repo_pages WHERE relative_path=? AND end_line>=? AND start_line<=? ORDER BY start_line LIMIT 33", (relative_path, start_line, end_line))]
        result = _assemble_context(connection, root, overview, rows, [], max_pages=32,
                                   max_chars=max_chars, check_cancel=check_cancel,
                                   requested_span={"start_line": start_line, "end_line": end_line},
                                   source_session=source_session)
        last_line = max((page["end_line"] for page in result["pages"]), default=start_line - 1)
        file_row = connection.execute("SELECT line_count FROM source_files WHERE relative_path=?", (relative_path,)).fetchone()
        total_lines = file_row[0] if file_row else 0
        result.update(requested_range={"relative_path": relative_path, "start_line": start_line, "end_line": end_line},
                      file_total_lines=total_lines, end_of_file=bool(total_lines) and last_line >= total_lines,
                      range_complete=bool(result["pages"]) and last_line >= min(end_line, total_lines),
                      next_start_line=last_line + 1 if last_line < min(end_line, total_lines) else None)
        return result
    finally:
        connection.close()
