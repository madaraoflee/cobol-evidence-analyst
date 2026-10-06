"""Prepare traceable source pages from a verified structural snapshot."""

from __future__ import annotations

from collections import defaultdict, deque
from bisect import bisect_left, bisect_right
import hashlib
import json
import codecs
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from typing import Callable


MAX_OUTLINE_ITEMS = 40
MAX_OUTLINE_FILES = 128
MAX_CHAIN_LINKS = 80
_LINE_ENDINGS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")


def _cancel(check: Callable[[], None] | None) -> None:
    if check:
        check()


def _relative_path(relative: str) -> PurePosixPath:
    path = PurePosixPath(relative)
    if (not relative or path.is_absolute() or "\\" in relative
            or any(part in {"", ".", ".."} or ":" in part for part in relative.split("/"))):
        raise ValueError("SOURCE_PATH_INVALID")
    return path


def _safe_file(root: Path, relative: str) -> Path:
    path = _relative_path(relative)
    candidate = root
    try:
        if root.is_symlink() or root.resolve(strict=True) != root:
            raise ValueError("SOURCE_PATH_INVALID")
        for part in path.parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise ValueError("SOURCE_PATH_INVALID")
        if not candidate.resolve(strict=True).is_relative_to(root):
            raise ValueError("SOURCE_PATH_INVALID")
        if not stat.S_ISREG(candidate.stat().st_mode):
            raise ValueError("SOURCE_PATH_INVALID")
    except OSError as exc:
        raise ValueError("SOURCE_PATH_INVALID") from exc
    return candidate


def _verified_lines(root: Path, item: dict, check: Callable | None, limit: int, *, verify_only=False):
    """Yield bounded physical lines; exhaust the iterator before trusting them.

    Neither an enormous source file nor a malformed enormous line is held in
    memory. Callers stage pages inside a transaction until final hash validation.
    """
    candidate = _safe_file(root, item["relative_path"])
    encoding = item["encoding"]
    if encoding == "latin-1-fallback":
        encoding = "latin-1"
    try:
        decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    except (LookupError, TypeError) as exc:
        raise ValueError("SOURCE_ENCODING_INVALID") from exc
    prefix, length, line_count = "", 0, 0
    pending_cr, beginning = False, True
    try:
        before = candidate.stat()
        with candidate.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("SOURCE_PATH_INVALID")
            digest = hashlib.sha256()
            while True:
                _cancel(check)
                chunk = handle.read(64 * 1024)
                digest.update(chunk)
                decoded = decoder.decode(chunk, final=not chunk)
                if beginning and decoded:
                    decoded = decoded.lstrip("\ufeff")
                    beginning = False
                if pending_cr and (decoded or not chunk):
                    decoded = decoded.removeprefix("\n")
                    pending_cr = False
                if decoded.endswith("\r"):
                    pending_cr = True
                pieces = decoded.splitlines(keepends=True)
                if verify_only:
                    # The whole byte stream still passes the same decoder,
                    # hash, identity and line-count checks. Cached search
                    # pages do not need one Python yield per physical line.
                    if pieces:
                        length = int(pieces[-1][-1] not in _LINE_ENDINGS)
                        line_count += len(pieces) - length
                    if not chunk:
                        break
                    continue
                for piece in pieces:
                    complete = piece[-1] in _LINE_ENDINGS
                    if complete:
                        piece = piece[:-2] if piece.endswith("\r\n") else piece[:-1]
                    if not length and complete:
                        line_count += 1
                        yield piece[:limit], len(piece) > limit
                    else:
                        prefix += piece[:max(0, limit - len(prefix))]
                        length += len(piece)
                        if complete:
                            line_count += 1
                            yield prefix, length > limit
                            prefix, length = "", 0
                if not chunk:
                    break
            if length:
                line_count += 1
                if not verify_only:
                    yield prefix, length > limit
            final = os.fstat(handle.fileno())
        after = _safe_file(root, item["relative_path"]).stat()
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        if identity(before) != identity(final) or identity(final) != identity(after):
            raise ValueError("SOURCE_HASH_MISMATCH")
    except OSError as exc:
        raise ValueError("SOURCE_PATH_INVALID") from exc
    except UnicodeError as exc:
        raise ValueError("SOURCE_ENCODING_INVALID") from exc
    if digest.hexdigest() != item["sha256"]:
        raise ValueError("SOURCE_HASH_MISMATCH")
    if line_count != item["line_count"]:
        raise ValueError("SOURCE_LINE_COUNT_MISMATCH")


def _verified_pages(root, item, check, paragraph_starts, budget):
    buffered, size = [], 0
    def page(rows):
        return {"relative_path": item["relative_path"], "start_line": rows[0][0], "end_line": rows[-1][0],
                "source_sha256": item["sha256"], "span_truncated": any(row[2] for row in rows),
                "source_text": "\n".join(row[1] for row in rows)}
    for number, (text, truncated) in enumerate(_verified_lines(root, item, check, budget), 1):
        if buffered and (truncated or size + len(text) + 1 > budget):
            lower = max(1, len(buffered) * 3 // 4)
            splits = [index for index in range(lower, len(buffered)) if buffered[index][0] in paragraph_starts]
            split = splits[-1] if splits and not truncated else len(buffered)
            yield page(buffered[:split])
            buffered = buffered[split:]
            size = sum(len(row[1]) for row in buffered) + max(0, len(buffered) - 1)
            if buffered and (truncated or size + len(text) + 1 > budget):
                yield page(buffered)
                buffered, size = [], 0
        if truncated:
            yield page([(number, text, True)])
        else:
            size += len(text) + bool(buffered)
            buffered.append((number, text, False))
    if buffered:
        yield page(buffered)


def _identify_page(page):
    identity = "\x1f".join(str(value) for value in (
        page["relative_path"], page["source_sha256"], page["start_line"], page["end_line"],
        hashlib.sha256(page["source_text"].encode()).hexdigest()))
    if page.get("include_chain"):
        identity += "\x1f" + json.dumps(page["include_chain"], sort_keys=True, ensure_ascii=False)
    return "ev_page_" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def _persist_page(connection, page):
    if page["span_truncated"]:
        return
    values = tuple(page[key] for key in ("evidence_id", "relative_path", "start_line", "end_line", "source_sha256", "source_text"))
    connection.execute("INSERT OR IGNORE INTO evidence_spans (evidence_id,relative_path,start_line,end_line,source_sha256,text) VALUES (?,?,?,?,?,?)", values)
    persisted = connection.execute("SELECT evidence_id,relative_path,start_line,end_line,source_sha256,text FROM evidence_spans WHERE evidence_id=?", (page["evidence_id"],)).fetchone()
    if tuple(persisted) != values:
        raise ValueError("EVIDENCE_ID_CONFLICT")


def read_source_page_batch(database_path, pages, *, check_cancel=None):
    """Load already verified, persisted pages without rereading whole files."""
    connection = sqlite3.connect(Path(database_path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        result = []
        for page in pages:
            _cancel(check_cancel)
            if "source_text" in page:
                result.append(page)
                continue
            row = connection.execute("SELECT relative_path,start_line,end_line,source_sha256,text FROM evidence_spans WHERE evidence_id=?", (page["evidence_id"],)).fetchone()
            if row is None or any(row[key] != page[key] for key in ("relative_path", "start_line", "end_line", "source_sha256")):
                raise ValueError("EVIDENCE_ID_CONFLICT")
            loaded = dict(page, source_text=row["text"])
            if _identify_page(loaded) != page["evidence_id"]:
                raise ValueError("EVIDENCE_ID_CONFLICT")
            result.append(loaded)
        return result
    finally:
        connection.close()


def _terms(question: str) -> list[str]:
    terms = {word.casefold() for word in re.findall(r"[A-Za-z][A-Za-z0-9_$#@.-]*", question)
             if len(word) > 1 and word.casefold() not in {"the", "and", "this", "what", "does", "how", "explain", "program"}}
    for phrase in re.findall(r"[\u3400-\u9fff]+", question):
        terms.add(phrase)
        for width in (4, 3, 2):
            terms.update(phrase[index:index + width] for index in range(len(phrase) - width + 1))
    terms -= {"程序", "分析", "什么", "如何", "这个", "為什", "为什么", "為什麼"}
    return sorted(terms, key=lambda term: (-len(term), term))[:128]


def _entry(programs: list[dict], value: str | None) -> dict | None:
    if value is None:
        if len(programs) == 1:
            return programs[0]
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("ENTRY_NOT_FOUND")
    value = value.strip().removeprefix("./").casefold()
    for field in ("entry_key", "relative_path", "program_name"):
        matches = [item for item in programs if item[field].casefold() == value]
        if matches:
            if len(matches) != 1:
                raise ValueError("ENTRY_AMBIGUOUS")
            return matches[0]
    raise ValueError("ENTRY_NOT_FOUND")


def _outline(connection: sqlite3.Connection, files: list[dict], check: Callable | None):
    outlines, programs, paragraphs, relations, interfaces = [], [], [], [], []
    for item in files:
        _cancel(check)
        relative = item["relative_path"]
        local_programs = [dict(row) for row in connection.execute(
            "SELECT name AS program_name, relative_path, start_line, end_line FROM code_units "
            "WHERE relative_path=? AND unit_type='Program' ORDER BY start_line", (relative,))]
        for program in local_programs:
            program["entry_key"] = f"{relative}::{program['program_name']}::{program['start_line']}"
        programs.extend(local_programs)
        local_paragraphs = [dict(row) for row in connection.execute(
            "SELECT name, program_name, relative_path, start_line, end_line FROM code_units "
            "WHERE relative_path=? AND unit_type IN ('Paragraph','Section') ORDER BY start_line", (relative,))]
        paragraphs.extend(local_paragraphs)
        local_interfaces = [dict(row) for row in connection.execute(
            "SELECT name,program_name,relative_path,start_line,end_line,unit_type FROM code_units "
            "WHERE relative_path=? AND (unit_type='ProcedureSignature' OR "
            "(unit_type='Section' AND name IN ('LINKAGE','WORKING-STORAGE'))) ORDER BY start_line", (relative,))]
        interfaces.extend(local_interfaces)
        local_relations = [dict(row) for row in connection.execute(
            "SELECT r.relation_type,r.target_name,r.status,r.relative_path,u.program_name,e.start_line,e.end_line, "
            "s.relative_path AS target_path,t.start_line AS target_line,t.program_name AS target_program "
            "FROM relations r LEFT JOIN code_units u ON u.unit_id=r.from_entity_id "
            "LEFT JOIN symbols s ON s.symbol_id=r.target_entity_id "
            "LEFT JOIN code_units t ON t.unit_id=s.definition_unit_id "
            "JOIN evidence_spans e ON e.evidence_id=r.evidence_id "
            "WHERE r.relative_path=? AND r.relation_type IN ('CALLS','CALL_TARGET_FROM','PERFORMS','PERFORMS_THRU','INCLUDES_COPY') "
            "ORDER BY e.start_line,r.relation_type", (relative,))]
        relations.extend(local_relations)
        counts = {name: connection.execute(f"SELECT COUNT(*) FROM {table} WHERE relative_path=?", (relative,)).fetchone()[0]
                  for name, table in (("code_unit_count", "code_units"), ("symbol_count", "symbols"), ("relation_count", "relations"))}
        names = [program["program_name"] for program in local_programs]
        if len(outlines) < MAX_OUTLINE_FILES:
            outlines.append({"relative_path": relative, "program_name": names[0] if len(names) == 1 else None,
                             "program_names": names[:MAX_OUTLINE_ITEMS], "line_count": item["line_count"], **counts,
                             "paragraph_count": len(local_paragraphs), "call_count": len(local_relations),
                             "interfaces": local_interfaces[:MAX_OUTLINE_ITEMS],
                             "paragraphs": local_paragraphs[:MAX_OUTLINE_ITEMS], "calls": local_relations[:MAX_OUTLINE_ITEMS],
                             "outline_truncated": max(len(local_paragraphs), len(local_relations), len(local_interfaces), len(names)) > MAX_OUTLINE_ITEMS})
    return outlines, programs, paragraphs, relations, interfaces


def _priorities(entry, paragraphs, relations, files, verified, programs=()):
    file_priority, line_priority = {}, defaultdict(set)
    by_paragraph = defaultdict(list)
    for paragraph in paragraphs:
        by_paragraph[(paragraph["relative_path"], paragraph["program_name"], paragraph["name"].upper())].append(paragraph)
    by_file = defaultdict(list)
    for relation in relations:
        by_file[relation["relative_path"]].append(relation)
    copy_paths = defaultdict(list)
    for item in files:
        relative = item["relative_path"]
        if Path(relative).suffix.casefold() in {".cpy", ".copy", ".copybook"}:
            for alias in {Path(relative).name.upper(), relative.upper()}:
                copy_paths[alias].append(relative)
    links, visited = [], set()
    roots = [entry] if entry else list(programs)
    if entry is None:
        known_roots = {(item["relative_path"], item["program_name"]) for item in roots}
        for relation in relations:
            key = (relation["relative_path"], relation["program_name"])
            if key not in known_roots:
                roots.append({"relative_path": key[0], "program_name": key[1]})
                known_roots.add(key)
    queue = deque((item["relative_path"], item["program_name"], 0) for item in roots)
    allowed = {item["relative_path"] for item in files}
    while queue:
        relative, program_name, depth = queue.popleft()
        if (relative, program_name) in visited or relative not in verified:
            continue
        visited.add((relative, program_name))
        file_priority[relative] = max(file_priority.get(relative, 0), max(1, 5 - depth))
        for relation in by_file[relative]:
            if relation["program_name"] not in {None, program_name}:
                continue
            if relation["relation_type"] in {"CALLS", "CALL_TARGET_FROM", "INCLUDES_COPY"}:
                dynamic = relation["relation_type"] == "CALL_TARGET_FROM"
                target = relation["target_path"] if relation["status"] == "confirmed" and not dynamic else None
                resolution = "unresolved" if dynamic else relation["status"]
                # A literal filename is a reading lead, not proof of COPY expansion.
                if not target and relation["relation_type"] == "INCLUDES_COPY":
                    matches = copy_paths.get((relation["target_name"] or "").upper(), [])
                    if len(matches) == 1:
                        target, resolution = matches[0], "literal_copy_candidate"
                target_status = ("verified" if target in verified else
                                 "outside_investigation" if target and target not in allowed else
                                 "excluded" if target else "missing")
                links.append({"relation_type": relation["relation_type"], "caller_path": relative,
                              "caller_program": program_name, "start_line": relation["start_line"],
                              "end_line": relation["end_line"], "target_name": relation["target_name"],
                              "target_path": target, "resolution": resolution, "target_source_status": target_status,
                              "depth": depth, "_target_program": relation["target_program"],
                              "_target_line": relation["target_line"] or 1})
                line_priority[relative].update((relation["start_line"], relation["end_line"]))
                if target_status == "verified":
                    line_priority[target].add(relation["target_line"] or 1)
                    queue.append((target, relation["target_program"] or Path(target).stem.upper(),
                                  depth + int(relation["relation_type"] == "CALLS")))
            elif relation["status"] == "confirmed" and relation["relation_type"] in {"PERFORMS", "PERFORMS_THRU"}:
                line_priority[relative].add(relation["start_line"])
                key = (relative, relation["program_name"], relation["target_name"].upper())
                candidates = by_paragraph[key]
                if len(candidates) == 1:
                    line_priority[relative].add(candidates[0]["start_line"])
    return file_priority, line_priority, links


def _span_pages(page_lookup, relative, start, end):
    indices, starts, ends = page_lookup.get(relative, ([], [], []))
    return indices[bisect_left(ends, start):bisect_right(starts, end)]


def _link_pages(link, page_lookup, interfaces):
    caller = _span_pages(page_lookup, link["caller_path"], link["start_line"], link["end_line"])
    target, context = [], []
    if link["target_source_status"] == "verified":
        local = interfaces.get((link["target_path"], link["_target_program"]), [])
        signature = [item for item in local if item["unit_type"] == "ProcedureSignature"]
        for item in signature:
            target.extend(_span_pages(page_lookup, link["target_path"], item["start_line"], item["end_line"]))
        if not target:
            target = _span_pages(page_lookup, link["target_path"], link["_target_line"], link["_target_line"])
        for item in local:
            if item["name"] == "LINKAGE":
                end = signature[0]["end_line"] if signature else item["end_line"]
                context.extend(_span_pages(page_lookup, link["target_path"], item["start_line"], end))
    return list(dict.fromkeys(caller)), list(dict.fromkeys(target)), list(dict.fromkeys(context))


def prepare_source_reading(database_path, source_root, *, entry_program, question,
                           max_pages=12, page_chars=12000, check_cancel=None,
                           reading_strategy="focused", progress=None, include_paths=None) -> dict:
    """Verify indexed files, select distributed pages, and persist exact citations.

    Coverage describes the indexed source selection, never the whole repository.
    Full-chain pages are persisted and contain metadata only; load their text
    with read_source_page_batch. max_pages bounds a batch in that mode, while
    focused mode uses it as the total selected-page limit. Neither is delivery.
    """
    if type(max_pages) is not int or not 1 <= max_pages <= 128:
        raise ValueError("max_pages must be between 1 and 128")
    if reading_strategy not in {"focused", "full_chain"}:
        raise ValueError("reading_strategy must be focused or full_chain")
    full_chain = reading_strategy == "full_chain"
    if type(page_chars) is not int or not 32 <= page_chars <= 50000:
        raise ValueError("page_chars must be between 32 and 50000")
    if not isinstance(question, str) or len(question) > 16000:
        raise ValueError("question must be a string of at most 16000 characters")
    database = Path(database_path).expanduser()
    if any(Path(str(database) + suffix).is_symlink() for suffix in ("", "-wal", "-shm")):
        raise ValueError("SNAPSHOT_PATH_INVALID")
    root_input = Path(source_root).expanduser()
    if root_input.is_symlink():
        raise ValueError("SOURCE_PATH_INVALID")
    root = root_input.resolve()
    if not root.is_dir():
        raise ValueError("SOURCE_PATH_INVALID")
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        snapshot = metadata.get("snapshot_id")
        if not snapshot or snapshot == "unknown":
            raise ValueError("SNAPSHOT_INVALID")
        expected_root = metadata.get("source_root_hash")
        if expected_root and expected_root != hashlib.sha256(str(root).encode()).hexdigest():
            raise ValueError("SOURCE_ROOT_MISMATCH")
        repository_files = [dict(row) for row in connection.execute("SELECT relative_path,sha256,encoding,line_count FROM source_files ORDER BY relative_path")]
        if not repository_files:
            raise ValueError("SOURCE_INDEX_EMPTY")
        files = repository_files
        if include_paths is not None:
            if isinstance(include_paths, (str, bytes)):
                raise ValueError("SOURCE_READING_SCOPE_INVALID")
            requested = set(include_paths)
            for relative in requested:
                if not isinstance(relative, str):
                    raise ValueError("SOURCE_READING_SCOPE_INVALID")
                _relative_path(relative)
            if requested - {item["relative_path"] for item in repository_files}:
                raise ValueError("SOURCE_READING_SCOPE_NOT_INDEXED")
            files = [item for item in repository_files if item["relative_path"] in requested]
            if not files:
                raise ValueError("SOURCE_READING_SCOPE_EMPTY")
        for item in files:
            _relative_path(item["relative_path"])
        outline, programs, paragraphs, relations, interfaces = _outline(connection, files, check_cancel)
        entry = _entry(programs, entry_program)
        paragraph_lines = defaultdict(set)
        for paragraph in paragraphs:
            paragraph_lines[paragraph["relative_path"]].add(paragraph["start_line"])
        terms, candidates, truncated_lines = _terms(question), [], 0
        verified, excluded, boundaries = set(), {}, []
        for item in files:
            _cancel(check_cancel)
            relative = item["relative_path"]
            offset = len(candidates)
            local_truncated = 0
            connection.execute("SAVEPOINT source_file_pages")
            try:
                for page in _verified_pages(root, item, check_cancel, paragraph_lines[relative], page_chars):
                    _cancel(check_cancel)
                    text = page["source_text"]
                    folded = text.casefold()
                    hits = [term for term in terms if term in folded]
                    page["evidence_id"] = _identify_page(page)
                    if full_chain:
                        _persist_page(connection, page)
                    candidate = {key: value for key, value in page.items() if key != "source_text"}
                    candidate.update(_score=sum(min(len(term), 32) for term in hits) * 1000,
                                     _hits=hits[:8], _relevant=False, source_characters=len(text))
                    if full_chain and page["span_truncated"]:
                        candidate["source_text"] = text
                    candidates.append(candidate)
                    local_truncated += int(page["span_truncated"])
                    if progress and len(candidates) % 128 == 0:
                        progress({"phase": "reading_sources", "completed": len(candidates), "total": None,
                                  "unit": "pages", "stage": "verifying_source_pages"})
            except ValueError as exc:
                connection.execute("ROLLBACK TO source_file_pages")
                connection.execute("RELEASE source_file_pages")
                del candidates[offset:]
                if (entry and relative == entry["relative_path"]) or str(exc) not in {
                    "SOURCE_PATH_INVALID", "SOURCE_HASH_MISMATCH", "SOURCE_ENCODING_INVALID", "SOURCE_LINE_COUNT_MISMATCH",
                }:
                    raise
                excluded[relative] = str(exc)
                boundaries.append({"type": "source_reading", "reason": "source_file_excluded",
                                   "relative_path": relative, "reason_code": str(exc),
                                   "message": "This file could not be verified against the snapshot. Its source, structure, and citations are excluded; conclusions depending on it remain incomplete."})
                continue
            connection.execute("RELEASE source_file_pages")
            verified.add(relative)
            truncated_lines += local_truncated
        file_priority, line_priority, links = _priorities(entry, paragraphs, relations, files, verified, programs)
        line_priority = {relative: sorted(values) for relative, values in line_priority.items()}
        page_lookup, interface_lookup = {}, defaultdict(list)
        for item in interfaces:
            interface_lookup[(item["relative_path"], item["program_name"])].append(item)
        for index, page in enumerate(candidates):
            relative = page["relative_path"]
            indices, starts, ends = page_lookup.setdefault(relative, ([], [], []))
            indices.append(index)
            starts.append(page["start_line"])
            ends.append(page["end_line"])
            priority_lines = line_priority.get(relative, [])
            page["_relevant"] = bisect_left(priority_lines, page["start_line"]) < bisect_right(priority_lines, page["end_line"])
            page["_score"] += page["_relevant"] * 100 + file_priority.get(relative, 0)
        link_indices = [_link_pages(link, page_lookup, interface_lookup) for link in links]
        chosen, reasons = set(), defaultdict(list)
        def choose(index, reason):
            if full_chain or len(chosen) < max_pages or index in chosen:
                chosen.add(index)
                if reason not in reasons[index]:
                    reasons[index].append(reason)
        if full_chain or len(candidates) <= max_pages:
            for index in range(len(candidates)):
                choose(index, "complete_indexed_scope")
        else:
            entry_indices = [i for i, page in enumerate(candidates) if entry and page["relative_path"] == entry["relative_path"]]
            if entry_indices:
                choose(entry_indices[0], "entry_start")
            # Reserve pages for both sides of a dependency before repeated entry
            # terms can consume the budget. Distinct targets precede extra sites.
            dependency_limit = min(max_pages, len(chosen) + max(1, max_pages // 2))
            linked_targets = set()
            ordered_links = sorted(range(len(links)), key=lambda i: (
                links[i]["target_source_status"] != "verified",
                links[i]["depth"], links[i]["relation_type"] != "CALLS",
                -max((candidates[j]["_score"] for group in link_indices[i][:2] for j in group), default=0), i))
            for link_index in ordered_links:
                link = links[link_index]
                key = (link["relation_type"], link["target_path"], link["target_name"])
                if key in linked_targets or link["target_source_status"] != "verified":
                    continue
                linked_targets.add(key)
                caller, target, context = link_indices[link_index]
                for index in target + caller:
                    if len(chosen) < dependency_limit or index in chosen:
                        choose(index, "call_or_perform_relation")
                        choose(index, "dependency_target" if index in target else "dependency_callsite")
            # The linkage declaration and full USING signature may span pages.
            for link_index in ordered_links:
                for index in link_indices[link_index][2]:
                    if len(chosen) < dependency_limit or index in chosen:
                        choose(index, "parameter_interface")
            if entry_indices:
                choose(entry_indices[-1], "entry_end")
            rank_budget = max(len(chosen), max_pages - max(1, max_pages // 4))
            ranked = sorted(range(len(candidates)), key=lambda i: (-candidates[i]["_score"], i))
            for index in ranked:
                if len(chosen) >= rank_budget:
                    break
                if candidates[index]["_hits"] or candidates[index]["_relevant"]:
                    choose(index, "question_match" if candidates[index]["_hits"] else "call_or_perform_relation")
            while len(chosen) < max_pages:
                unselected = set(range(len(candidates))) - chosen
                chosen_files = {candidates[i]["relative_path"] for i in chosen}
                index = max(unselected, key=lambda i: (candidates[i]["relative_path"] not in chosen_files,
                                                       min((abs(i - selected) for selected in chosen), default=len(candidates)),
                                                       candidates[i]["_score"], -i))
                choose(index, "distributed_coverage")
        pages, refs, pages_by_index = [], [], {}
        by_path = defaultdict(list)
        for index in sorted(chosen):
            by_path[candidates[index]["relative_path"]].append(index)
        files_by_path = {item["relative_path"]: item for item in files}
        for relative, indices in by_path.items():
            loaded = {}
            if not full_chain:
                selected_spans = {(candidates[i]["start_line"], candidates[i]["end_line"]) for i in indices}
                for page in _verified_pages(root, files_by_path[relative], check_cancel, paragraph_lines[relative], page_chars):
                    if (page["start_line"], page["end_line"]) in selected_spans:
                        loaded[(page["start_line"], page["end_line"])] = page["source_text"]
            for index in indices:
                candidate = candidates[index]
                page = {key: value for key, value in candidate.items() if not key.startswith("_")}
                if not full_chain:
                    page["source_text"] = loaded[(page["start_line"], page["end_line"])]
                page["selection_reasons"] = reasons[index]
                pages.append(page)
                pages_by_index[index] = page
                if page["span_truncated"]:
                    boundaries.append({"type": "source_reading", "reason": "line_exceeds_page_budget",
                                       "relative_path": relative, "start_line": page["start_line"],
                                       "message": "A source line exceeded the page character budget; its excerpt is not a complete citation."})
                    continue
                if not full_chain:
                    _persist_page(connection, page)
                refs.append({key: value for key, value in page.items() if key != "source_text"})
        if full_chain:
            # Rotate across files so later chain nodes receive analysis before a
            # single enormous entry consumes the whole run or user wait time.
            ordered_paths = sorted(by_path, key=lambda path: (
                path != (entry or {}).get("relative_path"), -file_priority.get(path, 0), path))
            queues = deque(deque(pages_by_index[index] for index in by_path[path]) for path in ordered_paths)
            pages = []
            while queues:
                queue = queues.popleft()
                pages.append(queue.popleft())
                if queue:
                    queues.append(queue)
        complete = len(chosen) == len(candidates) and not truncated_lines and not excluded
        if len(chosen) != len(candidates) or truncated_lines:
            boundaries.append({"type": "source_reading", "reason": "reading_budget_limited",
                               "message": "The reading plan covers selected pages only; omitted pages and incomplete lines remain unreviewed."})
        chain_rows = []
        for link, (caller, target, context) in zip(links, link_indices):
            groups = {}
            for name, indices in (("caller", caller), ("target", target), ("interface", context)):
                valid = [pages_by_index[i]["evidence_id"] for i in indices if i in pages_by_index
                         and not pages_by_index[i]["span_truncated"]]
                groups[name + "_evidence_ids"] = valid
                groups[name + "_selected"] = bool(indices) and len(valid) == len(indices)
            groups["interface_selected"] = not context or groups["interface_selected"]
            groups["selection_complete"] = groups["caller_selected"] and groups["target_selected"] and groups["interface_selected"]
            chain_rows.append({**{key: value for key, value in link.items() if not key.startswith("_")}, **groups})
        resolved_calls = [row for row in chain_rows if row["relation_type"] == "CALLS" and row["resolution"] == "confirmed"]
        covered_calls = sum(row["selection_complete"] for row in resolved_calls)
        unresolved_calls = sum(row["relation_type"] in {"CALLS", "CALL_TARGET_FROM"} and row["resolution"] != "confirmed" for row in chain_rows)
        if any(not row["selection_complete"] or row["resolution"] != "confirmed" for row in chain_rows):
            boundaries.append({"type": "source_reading", "reason": "dependency_context_incomplete",
                               "message": "Some call sites, called interfaces, or shared definitions are unselected, unavailable, or unresolved. Selected dependency pages are reading leads, not proof of complete business-flow coverage."})
        outline = [dict(item, source_status="verified") if item["relative_path"] in verified else
                   {"relative_path": item["relative_path"], "source_status": "excluded", "reason_code": excluded[item["relative_path"]]}
                   for item in outline]
        for item in outline:
            for relation in item.get("calls", []):
                if relation["relation_type"] == "CALL_TARGET_FROM":
                    relation.update(status="unresolved", target_path=None, target_line=None,
                                    target_program=None, target_source_status="missing")
                elif relation["target_path"] in excluded:
                    relation.update(status="target_unavailable", target_source_status="excluded",
                                    target_line=None, target_program=None)
        if len(files) > MAX_OUTLINE_FILES or any(item.get("outline_truncated") for item in outline):
            boundaries.append({"type": "source_reading", "reason": "outline_truncated",
                               "message": "Structural counts cover the indexed files; the displayed outline lists are bounded."})
        if len(chain_rows) > MAX_CHAIN_LINKS:
            boundaries.append({"type": "source_reading", "reason": "call_chain_truncated",
                               "message": "Dependency counts include all discovered links; the displayed dependency list is bounded."})
        _cancel(check_cancel)
        connection.commit()
        return {"snapshot_id": snapshot, "pages": pages, "evidence_refs": refs, "outline": outline,
                "programs": [program for program in programs if program["relative_path"] in verified],
                "all_call_chain_links": chain_rows,
                "call_chain": {"scope": "reading_plan", "links": chain_rows[:MAX_CHAIN_LINKS],
                               "total_links": len(chain_rows), "truncated": len(chain_rows) > MAX_CHAIN_LINKS},
                "boundaries": boundaries, "entry": entry,
                "coverage": {"scope": "investigation_sources" if include_paths is not None else "indexed_sources", "snapshot_kind": "indexed_sources", "total_files": len(files),
                             "repository_total_files": len(repository_files),
                             "repository_total_lines": sum(item["line_count"] for item in repository_files),
                             "investigation_files": len(files), "investigation_lines": sum(item["line_count"] for item in files),
                             "omitted_repository_files": len(repository_files) - len(files),
                             "repository_complete": complete and len(files) == len(repository_files),
                             "total_lines": sum(item["line_count"] for item in files), "total_pages": len(candidates),
                             "verified_files": len(verified), "excluded_files": len(excluded),
                             "excluded_lines": sum(item["line_count"] for item in files if item["relative_path"] in excluded),
                             "page_count_complete": not excluded,
                             "total_pages_scope": "verified_sources" if excluded else "indexed_sources",
                             "selected_files": len(by_path), "selected_pages": len(pages),
                             "selected_lines": sum(page["end_line"] - page["start_line"] + 1 for page in pages),
                             "selected_characters": sum(page["source_characters"] for page in pages),
                             "complete": complete, "omitted_pages": len(candidates) - len(pages),
                             "truncated_line_count": truncated_lines, "max_pages": max_pages, "page_chars": page_chars,
                             "selection_strategy": "all_verified_pages_file_rotation" if full_chain else "entry_dependency_interfaces_question_terms_distributed",
                             "reading_strategy": reading_strategy, "batch_pages": max_pages,
                             "total_batches": (len(pages) + max_pages - 1) // max_pages,
                             "call_chain": {"resolved_calls": len(resolved_calls), "covered_calls": covered_calls,
                                            "uncovered_calls": len(resolved_calls) - covered_calls,
                                            "unresolved_calls": unresolved_calls},
                             "source_hashes_verified": not excluded, "selected_source_hashes_verified": True,
                             "model_reading_completed": False}}
    finally:
        connection.close()
