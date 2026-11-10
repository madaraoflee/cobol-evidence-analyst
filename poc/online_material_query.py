"""Queries over immutable online material, without a source checkout or database.

``MaterialQuery(reader)`` pins the reader's manifest. ``search(text, limit=20, offset=0)``
returns lexical matches, total count, truncation and coverage. ``relations(path,
relation_type=None)`` returns exported facts unchanged. ``incoming(target,
relation_type='CALLS', limit=100, offset=0)`` verifies reverse-index candidates against
facts; matching a target name never resolves it to a unique program.

``evidence(path, start_line, end_line, source_reader, max_characters=12000)``
accepts a callable ``source_reader(path, *, max_bytes) -> bytes`` bound to the
manifest repository and commit. It verifies the entire fetched file, then
returns a bounded excerpt and citation. It retains no source bytes between
calls; callers may supply their own bounded, version-aware source cache.
"""

from __future__ import annotations

import codecs
from copy import deepcopy
import re

from online_material_format import (
    MAX_SOURCE_BYTES, MaterialError, bucket, canonical_json, digest, normalized_term, reference_key,
    relative_path, tokens, valid_hash,
)


_RELATION_TYPE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_ERROR_CODE = re.compile(r"(?:MATERIAL|SOURCE)_[A-Z0-9_]{1,64}\Z")
_MAX_QUERY_CHARACTERS = 16384
_MAX_QUERY_TERMS = 64
_MAX_RESULT_LIMIT = 1000
_MAX_EXCERPT_CHARACTERS = 1024 * 1024
_MAX_CANDIDATE_PATHS = 100000
_MAX_MATCHED_TERM_PAIRS = 500000
_MAX_QUERY_PATH_BYTES = 32 * 1024 * 1024
_MAX_QUERY_TRANSFER_BYTES = 128 * 1024 * 1024
_MAX_SCANNED_FACTS = 500000
_DECODE_CHUNK_BYTES = 64 * 1024
_LINE_ENDINGS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")


def _positive_integer(value, maximum, code):
    if type(value) is not int or not 1 <= value <= maximum:
        raise MaterialError(code)
    return value


def _relation_type(value):
    if not isinstance(value, str) or not _RELATION_TYPE.fullmatch(value.upper()):
        raise MaterialError("MATERIAL_INVALID_RELATION_TYPE")
    return value.upper()


def _excerpt(raw, encoding, start, end, maximum):
    """Count all decoded physical lines while retaining only the bounded page."""
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    line_count, line_length, remaining = 0, 0, maximum
    prefix, fragments = "", []
    actual_end, truncated_at, partial_line = start, None, False
    beginning, pending_cr = True, False

    def accept_line():
        nonlocal remaining, actual_end, truncated_at, partial_line
        number = line_count + 1
        if not start <= number <= end or truncated_at is not None:
            return
        separator = 1 if fragments else 0
        if remaining < separator + min(1, line_length):
            truncated_at = {"line": number, "character": 0}
            return
        remaining -= separator
        value = prefix[:remaining]
        fragments.append(value)
        remaining -= len(value)
        actual_end = number
        if len(value) < line_length:
            truncated_at = {"line": number, "character": len(value)}
            partial_line = True

    for offset in range(0, len(raw) + _DECODE_CHUNK_BYTES, _DECODE_CHUNK_BYTES):
        chunk = raw[offset:offset + _DECODE_CHUNK_BYTES]
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
        # Once the page is filled, only the physical count remains necessary.
        # This also prevents a file of millions of short lines from retaining
        # a whole-file list or spending one Python iteration per skipped line.
        if pieces and (line_count >= end or truncated_at is not None
                       or line_count + len(pieces) < start):
            line_length = int(pieces[-1][-1] not in _LINE_ENDINGS)
            line_count += len(pieces) - line_length
            prefix = ""
        else:
            for piece in pieces:
                complete = piece[-1] in _LINE_ENDINGS
                if complete:
                    piece = piece[:-2] if piece.endswith("\r\n") else piece[:-1]
                line_length += len(piece)
                if start <= line_count + 1 <= end and truncated_at is None:
                    prefix += piece[:max(0, remaining - len(prefix))]
                if complete:
                    accept_line()
                    line_count += 1
                    line_length, prefix = 0, ""
        if not chunk:
            break
    if line_length:
        accept_line()
        line_count += 1
    return {"text": "\n".join(fragments), "line_count": line_count,
            "end": actual_end, "truncated": truncated_at is not None,
            "truncated_at": truncated_at, "last_line_truncated": partial_line,
            "complete_line_count": len(fragments) - int(partial_line)}


class MaterialQuery:
    def __init__(self, reader):
        self.reader = reader
        self._manifest = deepcopy(reader.manifest)

    def _context(self):
        return {
            "revision": self._manifest["revision"],
            "repository": deepcopy(self._manifest["repository"]),
            "coverage": deepcopy(self._manifest["coverage"]),
        }

    def _check_transfer(self, initial_bytes):
        # The portable reader exposes cumulative network/directory reads. A
        # query stops after a bounded reader operation crosses this total; its
        # per-operation budget bounds that final operation as well.
        if self._transfer_bytes() - initial_bytes > _MAX_QUERY_TRANSFER_BYTES:
            raise MaterialError("MATERIAL_QUERY_BUDGET_EXCEEDED")

    def _transfer_bytes(self):
        return getattr(self.reader, "network_fetched_bytes", getattr(self.reader, "fetched_bytes", 0))

    def _file(self, path):
        relative_path(path)
        item = self.reader.file(path)
        if item is None:
            raise MaterialError("MATERIAL_FILE_NOT_INDEXED")
        return item

    def _facts(self, path):
        item = self.reader.facts(path)
        if item is None:
            raise MaterialError("MATERIAL_FILE_NOT_INDEXED")
        for relation in item["relations"]:
            kind = relation.get("relation_type")
            target = relation.get("target_name")
            status = relation.get("status")
            if (not isinstance(kind, str) or not _RELATION_TYPE.fullmatch(kind)
                    or not isinstance(target, str) or len(target) > 4096
                    or not isinstance(status, str) or not status or len(status) > 128
                    or any(ord(character) < 32 for character in status)):
                raise MaterialError("MATERIAL_INVALID_FACTS")
        return item

    def search(self, text, limit=20, *, offset=0):
        """Rank any-term lexical hits by the number of distinct matched terms.

        The total covers the supplied material only. A zero result is not a
        finding that a business concept or source file is absent.
        """
        _positive_integer(limit, _MAX_RESULT_LIMIT, "MATERIAL_INVALID_LIMIT")
        if type(offset) is not int or not 0 <= offset < 2 ** 63:
            raise MaterialError("MATERIAL_INVALID_OFFSET")
        if not isinstance(text, str) or len(text) > _MAX_QUERY_CHARACTERS:
            raise MaterialError("MATERIAL_INVALID_QUERY")
        query_terms = sorted(tokens(text))
        if len(query_terms) > _MAX_QUERY_TERMS:
            raise MaterialError("MATERIAL_QUERY_TERM_LIMIT")
        matched = {}
        matched_pairs, path_bytes = 0, 0
        initial_bytes = self._transfer_bytes()
        for term in query_terms:
            paths = self.reader.postings(term, route="terms")
            self._check_transfer(initial_bytes)
            for path in paths:
                matched_pairs += 1
                if path not in matched:
                    path_bytes += len(path.encode("utf-8"))
                    if len(matched) >= _MAX_CANDIDATE_PATHS or path_bytes > _MAX_QUERY_PATH_BYTES:
                        raise MaterialError("MATERIAL_QUERY_BUDGET_EXCEEDED")
                    matched[path] = []
                if matched_pairs > _MAX_MATCHED_TERM_PAIRS:
                    raise MaterialError("MATERIAL_QUERY_BUDGET_EXCEEDED")
                matched[path].append(term)
        ranked = sorted(matched, key=lambda path: (-len(matched[path]), path))
        matches = []
        for path in ranked[offset:offset + limit]:
            item = self._file(path)
            self._check_transfer(initial_bytes)
            matches.append({
                "relative_path": path,
                "sha256": item["sha256"],
                "line_count": item["line_count"],
                "matched_terms": matched[path],
                "score": len(matched[path]),
            })
        return {
            **self._context(), "query_kind": "lexical", "match_mode": "any_term",
            "terms": query_terms,
            "status": "matches" if ranked else (
                "no_lexical_matches" if query_terms else "no_query_terms"),
            "absence_established": False,
            "matches": matches, "total_matches": len(ranked), "offset": offset,
            "truncated": len(ranked) > offset + len(matches),
        }

    def inventory(self, limit=20, *, cursor=None):
        """Enumerate selected-snapshot files independently of business wording."""
        page = self.reader.files(limit, cursor=cursor)
        rows = [{key: item[key] for key in ("relative_path", "sha256", "line_count")}
                for item in page["files"]]
        while rows and len(canonical_json(rows)) > 20000:
            rows.pop()
        if page["files"] and not rows:
            raise MaterialError("MATERIAL_QUERY_BUDGET_EXCEEDED")
        clipped = len(rows) != len(page["files"])
        next_cursor = ({"bucket": bucket(rows[-1]["relative_path"]), "after": rows[-1]["relative_path"]}
                       if clipped else page["next_cursor"])
        return {"files": rows, "cursor": page["cursor"], "next_cursor": next_cursor,
                "complete": not clipped and page["complete"], "revision": self._manifest["revision"],
                "scope": "indexed_snapshot", "expected_files": self._manifest["coverage"].get("indexed_files")}

    def arithmetic_candidates(self, path, limit=20, *, offset=0):
        """Expose exported arithmetic/assignment anchors, never inferred formulas."""
        _positive_integer(limit, _MAX_RESULT_LIMIT, "MATERIAL_INVALID_LIMIT")
        if type(offset) is not int or not 0 <= offset < 2 ** 63:
            raise MaterialError("MATERIAL_INVALID_OFFSET")
        metadata = self._file(path)
        facts = self._facts(path)
        rows = []
        # Assignments may carry intrinsic calculations or a renamed amount;
        # their business relevance is decided only after reading original text.
        kinds = {"ADD", "COMPUTE", "DIVIDE", "MULTIPLY", "SUBTRACT", "MOVE", "SET"}
        for rule in facts["rules"]:
            if rule.get("rule_kind") not in kinds:
                continue
            identifier = rule.get("rule_id")
            occurrences = rule.get("occurrence_count", 1)
            if (not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_:-]{1,200}", identifier)
                    or type(rule.get("first_line")) is not int or type(rule.get("last_line")) is not int
                    or not 1 <= rule["first_line"] <= rule["last_line"] <= metadata["line_count"]
                    or type(occurrences) is not int or not 1 <= occurrences <= max(1, metadata["size"])):
                raise MaterialError("MATERIAL_INVALID_FACTS")
            candidate = {"candidate_id": "C" + digest(canonical_json([self._manifest["revision"], path, identifier]))[:24],
                         "path": path, "start_line": rule["first_line"], "end_line": rule["last_line"],
                         "rule_id": identifier, "rule_kind": rule["rule_kind"],
                         "occurrence_count": occurrences,
                         "reads": deepcopy(rule.get("reads", [])), "writes": deepcopy(rule.get("writes", [])),
                         "condition": deepcopy(rule.get("condition", []))}
            rows.append(candidate)
        rows.sort(key=lambda row: (row["start_line"], row["end_line"], row["candidate_id"]))
        selected = rows[offset:offset + limit]
        return {"candidates": selected, "total_matches": len(rows), "offset": offset,
                "truncated": offset + len(selected) < len(rows), "source_sha256": metadata["sha256"],
                "scope": "exported_arithmetic_and_assignment_candidates", "absence_established": False}

    def relations(self, path, relation_type=None):
        """Return exported relations and preserve their evidence and status."""
        relative_path(path)
        selected_type = None if relation_type is None else _relation_type(relation_type)
        facts = self._facts(path)
        relations = [deepcopy(item) for item in facts["relations"]
                     if selected_type is None or item["relation_type"] == selected_type]
        return {**self._context(), "relative_path": path,
                "source_sha256": facts["source_sha256"], "relations": relations}

    def incoming(self, target, relation_type="CALLS", limit=100, *, offset=0):
        """Verify reverse postings, preserving confirmed and candidate facts.

        Only CALLS and INCLUDES_COPY have reverse routes in this format. The
        result reports lexical target-name matches, not a unique destination.
        """
        _positive_integer(limit, _MAX_RESULT_LIMIT, "MATERIAL_INVALID_LIMIT")
        if type(offset) is not int or not 0 <= offset < 2 ** 63:
            raise MaterialError("MATERIAL_INVALID_OFFSET")
        selected_type = _relation_type(relation_type)
        if selected_type not in {"CALLS", "INCLUDES_COPY"}:
            raise MaterialError("MATERIAL_UNSUPPORTED_REVERSE_RELATION")
        if (not isinstance(target, str) or not target or len(target) > 4096
                or any(ord(character) < 32 for character in target)):
            raise MaterialError("MATERIAL_INVALID_TARGET")
        normalized_target = normalized_term(target)
        initial_bytes = self._transfer_bytes()
        paths = self.reader.postings(reference_key(selected_type, target), route="references")
        self._check_transfer(initial_bytes)
        if (len(paths) > _MAX_CANDIDATE_PATHS
                or sum(len(path.encode("utf-8")) for path in paths) > _MAX_QUERY_PATH_BYTES):
            raise MaterialError("MATERIAL_QUERY_BUDGET_EXCEEDED")
        matches = []
        total_matches = 0
        status_counts = {}
        scanned_facts = 0
        for path in paths:
            facts = self._facts(path)
            self._check_transfer(initial_bytes)
            scanned_facts += sum(len(facts[field]) for field in ("symbols", "relations", "rules"))
            if scanned_facts > _MAX_SCANNED_FACTS:
                raise MaterialError("MATERIAL_QUERY_BUDGET_EXCEEDED")
            for relation in facts["relations"]:
                if (relation["relation_type"] != selected_type
                        or normalized_term(relation["target_name"]) != normalized_target):
                    continue
                total_matches += 1
                status = relation.get("status", "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1
                if total_matches > offset and len(matches) < limit:
                    matches.append({"relative_path": path, "relation": deepcopy(relation)})
        return {
            **self._context(), "target": target, "relation_type": selected_type,
            "target_resolution": "name_match_only", "absence_established": False,
            "candidate_path_count": len(paths), "status_counts": status_counts,
            "matches": matches, "total_matches": total_matches, "offset": offset,
            "truncated": total_matches > offset + len(matches),
        }

    def evidence(self, path, start_line, end_line, source_reader, max_characters=12000):
        """Verify bytes before exposing an excerpt with a pinned citation.

        Lines are one-based and inclusive. ``truncated_at`` is the first omitted
        line and zero-based character offset. A partial last line is identified
        by ``last_line_truncated``; the citation end is the last line actually
        represented, not the end of an omitted requested range.
        """
        _positive_integer(start_line, 2**63 - 1, "MATERIAL_INVALID_RANGE")
        _positive_integer(end_line, 2**63 - 1, "MATERIAL_INVALID_RANGE")
        _positive_integer(max_characters, _MAX_EXCERPT_CHARACTERS,
                          "MATERIAL_INVALID_CHARACTER_LIMIT")
        if end_line < start_line:
            raise MaterialError("MATERIAL_INVALID_RANGE")
        item = self._file(path)
        source_hash = valid_hash(item.get("sha256"))
        size = item.get("size")
        if type(size) is not int or size < 0:
            raise MaterialError("MATERIAL_SOURCE_METADATA_INVALID")
        if size > MAX_SOURCE_BYTES:
            raise MaterialError("MATERIAL_SOURCE_SIZE_LIMIT")
        line_count = item.get("line_count")
        if type(line_count) is not int or line_count < 0:
            raise MaterialError("MATERIAL_SOURCE_METADATA_INVALID")
        if end_line > line_count:
            raise MaterialError("MATERIAL_RANGE_OUT_OF_BOUNDS")
        encoding = item.get("encoding")
        if not isinstance(encoding, str) or not encoding or len(encoding) > 64:
            raise MaterialError("MATERIAL_SOURCE_ENCODING_INVALID")
        if encoding == "latin-1-fallback":
            encoding = "latin-1"
        try:
            raw = source_reader(path, max_bytes=size)
        except MaterialError as error:
            code = error.code
            if not isinstance(code, str) or not _ERROR_CODE.fullmatch(code):
                code = "MATERIAL_SOURCE_UNAVAILABLE"
            raise MaterialError(code) from None
        except Exception:
            raise MaterialError("MATERIAL_SOURCE_UNAVAILABLE") from None
        if not isinstance(raw, bytes):
            raise MaterialError("MATERIAL_SOURCE_RESPONSE_INVALID")
        if len(raw) != size:
            raise MaterialError("MATERIAL_SOURCE_SIZE_MISMATCH")
        if digest(raw) != source_hash:
            raise MaterialError("MATERIAL_SOURCE_HASH_MISMATCH")
        try:
            excerpt = _excerpt(raw, encoding, start_line, end_line, max_characters)
        except (UnicodeError, LookupError, TypeError):
            raise MaterialError("MATERIAL_SOURCE_ENCODING_INVALID") from None
        if excerpt.pop("line_count") != line_count:
            raise MaterialError("MATERIAL_SOURCE_LINE_COUNT_MISMATCH")
        actual_end = excerpt.pop("end")
        return {
            **self._context(), **excerpt,
            "citation": {"revision": self._manifest["revision"],
                         "commit": self._manifest["repository"]["commit"],
                         "path": path, "hash": source_hash,
                         "start": start_line, "end": actual_end},
            "requested_range": {"start": start_line, "end": end_line},
        }
