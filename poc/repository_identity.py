"""Resolve source identities from definitions, without treating mentions as owners."""

from __future__ import annotations

from pathlib import PurePosixPath
import re

from repo_inventory import DEFAULT_EXTENSIONS


_PATH = re.compile(r"[^\s`\"<>,，;；?？:：]+(?:[/\\][^\s`\"<>,，;；?？:：]+)+")
_FILENAME = re.compile(r"[^\s`\"<>,，;；?？:：/\\]+")
_ASCII_PATH = re.compile(r"[A-Za-z0-9_$#@.%+'-]+(?:[/\\][A-Za-z0-9_$#@.%+'-]+)+")
_PATH_REQUEST = re.compile(r"(?:请|請)?(?:说明|說明|解释|解釋|分析|查看|检查|檢查|阅读|閱讀|读|讀|显示|顯示)")
_QUOTED = re.compile(r"[`\"']([^`\"']+)[`\"']")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_$#@-]*(?:\.[A-Za-z0-9]+)?")
_PROGRAM = re.compile(r"(?:\bprogram(?:-id)?\b|程序)\s*[:：]?\s*[`\"']?([A-Za-z0-9][A-Za-z0-9_$#@-]*)", re.I)
_FIELD = re.compile(r"(?:\bfield\b|字段|欄位|变量|變量)\s*[:：]?\s*[`\"']?([A-Za-z0-9][A-Za-z0-9_$#@-]*)", re.I)
_PROGRAM_LABEL = re.compile(r"(?:\bprogram-id\b|程序名)\s*[:：]?\s*[`\"']?([A-Za-z0-9][A-Za-z0-9_$#@-]*)", re.I)


def _result(candidates):
    missing = any(not item["relative_paths"] for item in candidates)
    ambiguous = any(len(item["relative_paths"]) > 1 or item.get("ambiguous") for item in candidates)
    status = "ambiguous" if ambiguous else "not_found" if missing else "resolved" if candidates else "none"
    return {"status": status, "requested": [item["identifier"] for item in candidates],
            "kind": candidates[0]["kind"] if candidates else None,
            "direct_paths": sorted({path for item in candidates for path in item["relative_paths"]})
                            if status == "resolved" else [],
            "candidates": candidates}


def _program_paths(connection, name):
    # Both index writers normalize definition symbols to uppercase. Preserve
    # that key so the existing (symbol_type,name,program_name) index is usable;
    # NOCASE on a binary index would scan every source unit for each word.
    return sorted({row[0] for row in connection.execute(
        "SELECT relative_path FROM symbols WHERE symbol_type='Program' AND name=?", (name.upper(),))})


def _like_literal(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _resolve_input(connection, text):
    # Paths with directories take precedence over words derived from filenames.
    paths = []
    quoted_paths = [match[1] for match in _QUOTED.finditer(text)
                    if "/" in match[1] or "\\" in match[1] or PurePosixPath(match[1]).suffix.casefold() in DEFAULT_EXTENSIONS]
    path_tokens = [(value, True) for value in quoted_paths]
    if not quoted_paths:
        path_tokens = [(match[0], False) for match in _PATH.finditer(text)]
    for value, quoted in path_tokens:
        identifier = value.rstrip(".")
        normalized = identifier.replace("\\", "/")
        canonical = normalized.removeprefix("./")
        kind = "relative_path" if "/" in normalized else "basename"
        exact = [row[0] for row in connection.execute(
            "SELECT relative_path FROM source_files WHERE relative_path=?", (canonical,))]
        if not exact and not quoted:
            # Chinese prose can touch an ASCII path without whitespace. Strip
            # only recognized request syntax, never an arbitrary path prefix.
            ascii_path = _ASCII_PATH.search(canonical)
            if ascii_path:
                before, after = canonical[:ascii_path.start()], canonical[ascii_path.end():]
                if (not before or _PATH_REQUEST.fullmatch(before)) and (not after or after.startswith("的")):
                    canonical = ascii_path[0]
                    identifier = canonical
                    exact = [row[0] for row in connection.execute(
                        "SELECT relative_path FROM source_files WHERE relative_path=?", (canonical,))]
        if not exact and not PurePosixPath(canonical).suffix and not quoted:
            continue
        if kind == "basename":
            candidates = [row[0] for row in connection.execute(
                "SELECT relative_path FROM source_files WHERE relative_path=? COLLATE NOCASE "
                "OR relative_path LIKE ? ESCAPE '\\' ORDER BY relative_path", (canonical, "%/" + _like_literal(canonical)))]
        else:
            candidates = exact or [row[0] for row in connection.execute(
                "SELECT relative_path FROM source_files WHERE relative_path=? COLLATE NOCASE ORDER BY relative_path", (canonical,))]
        paths.append({"identifier": identifier, "kind": kind, "relative_paths": candidates})
    if paths:
        return _result(list({item["identifier"]: item for item in paths}.values()))

    # Keep the whole filename token. Splitting punctuation could reinterpret
    # an unknown requested file as the suffix of another indexed basename.
    filenames = list(dict.fromkeys(match[0].rstrip(".") for match in _FILENAME.finditer(text)
                     if PurePosixPath(match[0].rstrip(".")).suffix.casefold() in DEFAULT_EXTENSIONS))
    if filenames:
        candidates = []
        for name in filenames:
            matches = [row[0] for row in connection.execute(
                "SELECT relative_path FROM source_files WHERE relative_path=? COLLATE NOCASE "
                "OR relative_path LIKE ? ESCAPE '\\' ORDER BY relative_path", (name, "%/" + _like_literal(name)))]
            candidates.append({"identifier": name, "kind": "basename", "relative_paths": matches})
        return _result(candidates)

    names = list(dict.fromkeys(match[0] for match in _NAME.finditer(text)))
    explicit_programs = list(dict.fromkeys(_PROGRAM_LABEL.findall(text)))
    for match in _PROGRAM.finditer(text):
        name = match[1]
        preceding = text[match.start(1) - 1:match.start(1)]
        if _program_paths(connection, name) or preceding in {"`", "\"", "'"}:
            explicit_programs.append(name)
    explicit_programs = list(dict.fromkeys(explicit_programs))
    if explicit_programs:
        return _result([{"identifier": name, "kind": "program", "relative_paths": _program_paths(connection, name)}
                        for name in explicit_programs])

    explicit_fields = {name.casefold() for name in _FIELD.findall(text)}
    candidates = []
    stems_by_name = None
    has_rule_fields = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='business_rule_fields'").fetchone()
    for name in names:
        # A field/paragraph name remains a lexical query even if it resembles a
        # filename stem. Explicit program or path syntax can resolve collisions.
        if name.casefold() in explicit_fields:
            continue
        programs = _program_paths(connection, name)
        if not programs:
            # A field is a lexical search, not a source identity. Its presence
            # is enough here; enumerating every rule using a common field would
            # join millions of rules just to discard their paths below.
            symbol = connection.execute(
                "SELECT 1 FROM symbols WHERE symbol_type IN ('Field','ConditionName','Paragraph','Section') "
                "AND name=? LIMIT 1", (name.upper(),)).fetchone()
            field = (connection.execute(
                "SELECT 1 FROM business_rule_fields WHERE field_name=? LIMIT 1",
                (name.upper(),)).fetchone() if has_rule_fields and not symbol else None)
            if symbol or field:
                continue
            other_symbols = []
        else:
            other_symbols = [row[0] for row in connection.execute(
                "SELECT DISTINCT relative_path FROM symbols WHERE symbol_type IN ('Field','ConditionName','Paragraph','Section') "
                "AND name=? ORDER BY relative_path", (name.upper(),))]
            if has_rule_fields:
                other_symbols = sorted(set(other_symbols) | {row[0] for row in connection.execute(
                    "SELECT DISTINCT b.relative_path FROM business_rule_fields f JOIN business_rules b USING(rule_id) "
                    "WHERE f.field_name=?", (name.upper(),))})
        if other_symbols:
            if programs:
                candidates.append({"identifier": name, "kind": "program_or_symbol", "ambiguous": True,
                                   "relative_paths": sorted(set(programs + other_symbols))})
            continue
        if programs:
            candidates.append({"identifier": name, "kind": "program", "relative_paths": programs})
            continue
        if stems_by_name is None:
            stems_by_name = {}
            for row in connection.execute("SELECT relative_path FROM source_files ORDER BY relative_path"):
                stems_by_name.setdefault(PurePosixPath(row[0]).stem.casefold(), []).append(row[0])
        stems = stems_by_name.get(name.casefold(), [])
        if stems:
            candidates.append({"identifier": name, "kind": "stem", "relative_paths": stems})
    return _result(candidates)


def resolve_source_identity(connection, question, search_terms=None):
    """Keep an explicit question identity while refining its lexical search.

    Definitions are authoritative; CALL and comment references never establish
    identity. Unknown ordinary words and fields preserve lexical behavior.
    """
    result = _resolve_input(connection, question)
    if result["status"] != "none":
        return result
    for value in search_terms or ():
        candidate = _resolve_input(connection, value)
        if candidate["status"] != "none":
            return candidate
    return result


def identity_boundaries(identity):
    if identity["status"] not in {"ambiguous", "not_found"}:
        return []
    return [{"reason": "source_identity_" + identity["status"],
             "requested": identity["requested"], "candidates": identity["candidates"],
             "message": "Source identity needs clarification." if identity["status"] == "ambiguous"
                        else "The explicitly requested source is not indexed."}]


def identity_sql_scope(identity, alias="p"):
    """Return a parameterized path filter shared by discovery and retrieval."""
    if identity["status"] == "none":
        return "", ()
    paths = identity["direct_paths"]
    if not paths:
        return " AND 0", ()
    return f" AND {alias}.relative_path IN ({','.join('?' for _ in paths)})", tuple(paths)
