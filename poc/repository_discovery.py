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

from source_reading import _cancel, _identify_page, _persist_page, _verified_lines, _verified_pages


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
    for word in _WORDS.findall(text):
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
        CREATE VIRTUAL TABLE IF NOT EXISTS repo_fts USING fts5(tokens, tokenize='unicode61 remove_diacritics 2');
    """)


def _remove_file(connection, relative):
    connection.execute("DELETE FROM repo_fts WHERE rowid IN (SELECT page_id FROM repo_pages WHERE relative_path=?)", (relative,))
    connection.execute("DELETE FROM repo_pages WHERE relative_path=?", (relative,))
    connection.execute("DELETE FROM repo_sources WHERE relative_path=?", (relative,))


def ensure_repository_search(database_path, source_root, check_cancel=None, progress=None):
    """Index all snapshot text locally; verify even files whose tokens are cached.

    A failed refresh leaves the search marked unavailable. Its previous pages
    cannot be returned as current evidence, even if the source snapshot id did
    not change yet. Source/page facts are published only after whole-file hashes
    and the enclosing SQLite snapshot have been verified.
    """
    root_input = Path(source_root).expanduser()
    if root_input.is_symlink():
        raise ValueError("SOURCE_PATH_INVALID")
    root = root_input.resolve()
    connection = _connect(database_path, True)
    try:
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
        current_paths = {item["relative_path"] for item in files}
        removed = set(existing) - current_paths
        for relative in removed:
            _remove_file(connection, relative)
        updated, cached = 0, 0
        for offset, item in enumerate(files, 1):
            _cancel(check_cancel)
            relative = item["relative_path"]
            previous = existing.get(relative, {})
            reusable = prior.get("version") == SEARCH_VERSION and all(previous.get(key) == item[key] for key in ("sha256", "encoding", "line_count"))
            # A structural rebuild may remove old evidence even for unchanged
            # text. Recreate that file rather than returning dangling citations.
            if reusable:
                missing = connection.execute("SELECT COUNT(*) FROM repo_pages p LEFT JOIN evidence_spans e ON e.evidence_id=p.evidence_id WHERE p.relative_path=? AND p.span_truncated=0 AND e.evidence_id IS NULL", (relative,)).fetchone()[0]
                reusable = not missing
            if reusable:
                for _ in _verified_lines(root, item, check_cancel, 1):
                    pass
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
