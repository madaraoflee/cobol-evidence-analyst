"""Resolve source identities from definitions, without treating mentions as owners."""

from __future__ import annotations

from pathlib import PurePosixPath
import re

from repo_inventory import DEFAULT_EXTENSIONS


_PATH = re.compile(r"[^\s`\"<>,，;；?？:：。！!“”‘’]+(?:[/\\][^\s`\"<>,，;；?？:：。！!“”‘’]+)+")
_FILENAME = re.compile(r"[^\s`\"<>,，;；?？:：。！!“”‘’/\\]+")
_ASCII_PATH = re.compile(r"[A-Za-z0-9_$#@.%+'-]+(?:[/\\][A-Za-z0-9_$#@.%+'-]+)+")
_SOURCE_REFERENCE = re.compile(
    r"(?:[A-Za-z]:[/\\])?[A-Za-z0-9_$#@.%+'-]+"
    r"(?:[/\\][^\s`\"<>,，;；?？:：。！!“”‘’/\\]+)*\.(?:" +
    "|".join(re.escape(extension[1:]) for extension in sorted(DEFAULT_EXTENSIONS)) +
    r")(?![A-Za-z0-9_$#@.-])", re.I)
_PATH_REQUEST = re.compile(r"(?:请|請)?(?:说明|說明|解释|解釋|分析|查看|检查|檢查|阅读|閱讀|读|讀|显示|顯示)")
_QUOTED = re.compile(r"[`\"'“”‘’]([^`\"'“”‘’]+)[`\"'“”‘’]")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_$#@-]*(?:\.[A-Za-z0-9]+)?")
_PROGRAM = re.compile(r"(?:\bprogram(?:-id)?\b|程序)\s*[:：]?\s*[`\"']?([A-Za-z0-9][A-Za-z0-9_$#@-]*)", re.I)
_FIELD = re.compile(r"(?:\bfield\b|字段|欄位|变量|變量)\s*[:：]?\s*[`\"']?([A-Za-z0-9][A-Za-z0-9_$#@-]*)", re.I)
_PROGRAM_LABEL = re.compile(r"(?:\bprogram-id\b|程序名)\s*[:：]?\s*[`\"']?([A-Za-z0-9][A-Za-z0-9_$#@-]*)", re.I)
_PROGRAM_SUBJECT = re.compile(
    r"(?<![A-Za-z0-9_$#@.-])[`\"']?([A-Za-z0-9][A-Za-z0-9_$#@-]*)[`\"']?"
    r"\s*(?:这个|這個|这支|這支|该|該)?(?:程序|程式)(?!名)")
_CALL_MENTION = re.compile(r"调用|調用|呼叫|\bcall(?:s|ed|ing)?\b", re.I)
_CALL_QUESTION = re.compile(r"什么|甚麼|何时|何時|为何|為何|为什么|為什麼|是否|如何|"
                            r"\b(?:when|why|how|what)\b", re.I)
_MULTIPLE_OR_NEGATED_SUBJECT = re.compile(
    r"比较|比較|对比|對比|分别|分別|各自|不是|并非|並非|"
    r"\b(?:compare|comparison|both|respectively|instead\s+of)\b|\bnot\s+(?:the\s+)?program\b", re.I)
_SUBJECT_COORDINATION = re.compile(r"和|与|與|及|或者|\b(?:and|or)\b", re.I)
_LATER_ANALYSIS_REQUEST = re.compile(
    r"(?:请|請)?(?:分析|解释|解釋|说明|說明|查看|检查|檢查)|"
    r"\b(?:analy[sz]e|explain|describe|inspect)\b", re.I)
_EXCLUSIVE_SOURCE_REQUEST = re.compile(
    r"(?:只|仅|僅)(?:分析|查看|阅读|閱讀|检查|檢查|解释|解釋|说明|說明|看|读|讀)|"
    r"(?:限于|限於|仅限|僅限)\s*(?:此|本|这个|這個|该|該)?\s*(?:文件|程序|程式|源码|原文)|"
    r"\bonly\s+(?:(?:this|that|the|selected)\s+)?(?:file|source|program)\b|"
    r"\b(?:analy[sz]e|read|inspect|explain)\s+only\b|"
    r"\bonly\s+(?:analy[sz]e|read|inspect|explain)\b|"
    r"\b(?:do\s+not|don't)\s+(?:read|search|inspect)\s+(?:any\s+)?other\s+(?:files|sources|programs)\b",
    re.I)
_WRAPPERS = {"(": ")", "（": "）", "[": "]", "【": "】", "{": "}"}
_EXCLUSIVE_ACTION = re.compile(
    r"(?:只|仅|僅)(?:分析|查看|阅读|閱讀|检查|檢查|解释|解釋|说明|說明|看|读|讀)|"
    r"(?:analy[sz]e|read|inspect|explain)\s+only|only\s+(?:analy[sz]e|read|inspect|explain)", re.I)


def _unquoted_identifier(value):
    """Remove sentence wrappers without discarding punctuation inside a path."""
    value = value.rstrip(".")
    request = _PATH_REQUEST.match(value)
    if request and value[request.end():request.end() + 1] in _WRAPPERS:
        value = value[request.end():]
    while value and value[0] in _WRAPPERS and value.endswith(_WRAPPERS[value[0]]):
        value = value[1:-1].rstrip(".")
    return value


def mask_source_references(question):
    """Exclude source spellings from data-reference parsing, preserving offsets.

    A hyphenated basename or directory is not a COBOL field. Mask complete
    references before parsing so ``FINAL-AMOUNT in rule-file.cbl`` cannot
    acquire a bogus field qualifier. Source identity resolution still uses
    the original question and independently reports missing/ambiguous files.
    """
    text = str(question)
    for match in reversed(list(_QUOTED.finditer(text))):
        if PurePosixPath(match[1].replace("\\", "/")).suffix.casefold() in DEFAULT_EXTENSIONS:
            text = text[:match.start()] + " " * len(match[0]) + text[match.end():]
    return _SOURCE_REFERENCE.sub(lambda match: " " * len(match[0]), text)


def _exclusive_source_scope(question, identity):
    names = list(dict.fromkeys([*identity.get("requested", []), *identity.get("direct_paths", []),
                               *(PurePosixPath(path).name for path in identity.get("direct_paths", []))]))
    for match in _EXCLUSIVE_SOURCE_REQUEST.finditer(question):
        if not _EXCLUSIVE_ACTION.fullmatch(match[0]):
            return True
        following = question[match.end():].lstrip()
        if re.match(r"(?:(?:this|that|the|selected)\s+(?:file|source|program)\b|"
                    r"(?:此|本|这个|這個|该|該)(?:文件|程序|程式|源码|原文))", following, re.I):
            return True
        for name in names:
            if name and re.match(r"[`\"'“‘（(\[【]?" + re.escape(name) + r"(?![A-Za-z0-9_$#@.-])",
                                 following, re.I):
                return True
    return False


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


def _call_analysis_subject(connection, text):
    """Recognize an explicit program subject before a call relationship.

    A later callee filename describes the relationship, not the source owner.
    Do not rank a list of subjects or infer an owner from ordinary prose.
    """
    call = _CALL_MENTION.search(text)
    if call is None or _MULTIPLE_OR_NEGATED_SUBJECT.search(text):
        return None
    prefix = text[:call.start()]
    suffix = text[call.end():]
    # Relative clauses can make the callee the object of the question, and
    # a later analysis request can explicitly change the topic again.
    if re.match(r"\s*的(?!\s*是)", suffix):
        return None
    subjects = [(match[1], False, match.end())
                for match in _PROGRAM_SUBJECT.finditer(prefix)]
    # A Chinese topic declaration ("X 这个程序") or a question preceding
    # the relationship fixes the focus. "program X calls Y, how does Y ..."
    # does not: keep the normal resolver for that later change of subject.
    if not subjects and not _CALL_QUESTION.search(prefix):
        return None
    for pattern in (_PROGRAM_LABEL, _PROGRAM):
        for match in pattern.finditer(prefix):
            name = match[1]
            preceding = prefix[match.start(1) - 1:match.start(1)]
            if (pattern is _PROGRAM_LABEL or _program_paths(connection, name)
                    or preceding in {"`", '"', "'"}):
                subjects.append((name, True, match.end()))
    names = list(dict.fromkeys(name.upper() for name, _, _ in subjects))
    if len(names) != 1:
        return None
    if _SUBJECT_COORDINATION.search(prefix[:max(end for _, _, end in subjects)]):
        return None
    # An independently supplied file/path before the relationship still has
    # its normal identity semantics; do not silently overrule that selection.
    if any(PurePosixPath(match[0].rstrip(".")).suffix.casefold() in DEFAULT_EXTENSIONS
           for match in _FILENAME.finditer(prefix)) or _PATH.search(prefix):
        return None
    name = subjects[0][0]
    explicit_program = any(explicit for _, explicit, _ in subjects)
    subject_paths = (_program_paths(connection, name) if explicit_program
                     else _resolve_identifiers(connection, name)["direct_paths"])
    for request in _LATER_ANALYSIS_REQUEST.finditer(suffix):
        # Parenthetical call-target clarification is separate from the new
        # request's object. "Explain the conditions (calls X.cbl)" keeps the
        # subject; "Explain X.cbl's output" explicitly changes it.
        clause = re.split(r"[。；;！？!?\n（]|(?:^|\s)\(", suffix[request.end():], maxsplit=1)[0]
        later = _resolve_identifiers(connection, clause)
        if later["status"] == "none" or {item.casefold() for item in later["requested"]} == {name.casefold()}:
            continue
        if subject_paths and set(later["direct_paths"]) == set(subject_paths):
            continue
        return None
    return name, explicit_program


def _with_constraint(result, *, hard, source="question"):
    result = {**result, "hard_constraint": hard, "selection_source": source}
    if not hard and result["status"] in {"ambiguous", "not_found"}:
        # These paths are investigation candidates, never a chosen runtime
        # implementation. A missing mention must not hide known candidates.
        result["direct_paths"] = sorted({path for item in result["candidates"]
                                         for path in item["relative_paths"]})
    return result


def _resolve_input(connection, text):
    named = _resolve_identifiers(connection, text)
    # An explicit qualified path is an identity constraint, even when the
    # surrounding sentence also mentions a caller or another program.
    if any(item["kind"] == "relative_path" for item in named["candidates"]):
        return _with_constraint(named, hard=True)
    subject = _call_analysis_subject(connection, text)
    if subject is not None:
        name, explicit_program = subject
        programs = _program_paths(connection, name) if explicit_program else []
        result = (_result([{"identifier": name, "kind": "program", "relative_paths": programs}])
                  if programs else _resolve_identifiers(connection, name))
        if result["status"] == "none":
            result = _result([{"identifier": name, "kind": "program", "relative_paths": []}])
        for candidate in result["candidates"]:
            candidate["role"] = "analysis_subject"
        result["selection_basis"] = "explicit_analysis_subject_before_call"
        return _with_constraint(result, hard=False)
    if named["kind"] == "basename":
        # A filename alongside a separately named program can describe a
        # relationship. Keep both as soft candidates rather than treating the
        # filename mention as an exclusive user selection.
        programs = [{"identifier": name, "kind": "program", "relative_paths": paths}
                    for name in dict.fromkeys(_NAME.findall(text))
                    if (paths := _program_paths(connection, name))]
        if programs:
            return _with_constraint(_result([*programs, *named["candidates"]]), hard=False)
        return _with_constraint(named, hard=True)
    return _with_constraint(named, hard=False)


def _resolve_identifiers(connection, text):
    # A framework document path is reference material, not a missing source
    # constraint. Preserve Markdown deliberately included in the source index.
    # Mask before choosing quoted paths so an accompanying unquoted source
    # path keeps its normal identity semantics.
    for pattern in (_QUOTED, _PATH):
        for match in reversed(list(pattern.finditer(text))):
            value = match[1] if pattern is _QUOTED else _unquoted_identifier(match[0])
            canonical = value.replace("\\", "/").removeprefix("./").rstrip(".")
            if ("/" in canonical and PurePosixPath(canonical).suffix.casefold() in {".md", ".markdown"}
                    and not connection.execute(
                        "SELECT 1 FROM source_files WHERE relative_path=? COLLATE NOCASE LIMIT 1",
                        (canonical,)).fetchone()):
                text = text[:match.start()] + " " * len(match[0]) + text[match.end():]
    # Paths with directories take precedence over words derived from filenames.
    paths = []
    quoted_paths = [match[1] for match in _QUOTED.finditer(text)
                    if "/" in match[1] or "\\" in match[1] or PurePosixPath(match[1]).suffix.casefold() in DEFAULT_EXTENSIONS]
    path_tokens = [(value, True) for value in quoted_paths]
    if not quoted_paths:
        path_tokens = [(match[0], False) for match in _PATH.finditer(text)]
    for value, quoted in path_tokens:
        identifier = value.rstrip(".") if quoted else _unquoted_identifier(value)
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
    filenames = list(dict.fromkeys(value for match in _FILENAME.finditer(text)
                     if PurePosixPath(value := _unquoted_identifier(match[0])).suffix.casefold() in DEFAULT_EXTENSIONS))
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
    # An explicit program noun remains an object request when its definition
    # is absent; it must not silently reuse the previous conversation subject.
    for name in dict.fromkeys(_PROGRAM_SUBJECT.findall(text)):
        if not any(item["identifier"].casefold() == name.casefold() for item in candidates):
            candidates.append({"identifier": name, "kind": "program",
                               "relative_paths": _program_paths(connection, name)})
    return _result(candidates)


def resolve_entry_identity(connection, entry_program):
    """Validate a selected entry against indexed definitions, including entry keys."""
    value = entry_program.strip().replace("\\", "/").removeprefix("./") \
        if isinstance(entry_program, str) else ""
    paths, kind, ambiguous = [], "program", False
    if "::" in value:
        kind = "relative_path"
        parts = value.split("::")
        if len(parts) == 3 and parts[0] and parts[1] and parts[2].isdigit() and int(parts[2]) > 0:
            paths = [row[0] for row in connection.execute(
                "SELECT relative_path FROM code_units WHERE unit_type='Program' "
                "AND relative_path=? COLLATE NOCASE AND name=? AND start_line=?",
                (parts[0], parts[1].upper(), int(parts[2])))]
    else:
        definitions = [row[0] for row in connection.execute(
            "SELECT relative_path FROM code_units WHERE unit_type='Program' AND name=?",
            (value.upper(),))]
        if definitions:
            paths, ambiguous = sorted(set(definitions)), len(definitions) > 1
        else:
            kind = "relative_path" if "/" in value else "basename"
            scope = "relative_path=? COLLATE NOCASE" if "/" in value else \
                "(relative_path=? COLLATE NOCASE OR relative_path LIKE ? ESCAPE '\\')"
            arguments = (value,) if "/" in value else (value, "%/" + _like_literal(value))
            definitions = [row[0] for row in connection.execute(
                "SELECT relative_path FROM code_units WHERE unit_type='Program' AND " + scope +
                " ORDER BY relative_path", arguments)] if value else []
            paths, ambiguous = sorted(set(definitions)), len(definitions) > 1
    candidate = {"identifier": value, "kind": kind, "relative_paths": sorted(set(paths)),
                 "role": "analysis_subject"}
    if ambiguous:
        candidate["ambiguous"] = True
    return _with_constraint(_result([candidate]), hard=True, source="entry_program")


def resolve_source_identity(connection, question, search_terms=None, *, focus_paths=None,
                            entry_program=None, history=()):
    """Separate user file constraints from revisable source-navigation hints.

    Search terms may improve a soft candidate set. An established caller hint
    survives supplementary callee searches; an explicit focus action can revise
    it after every requested path has been checked against indexed files.
    """
    result = _resolve_input(connection, question)
    question_has_identity = result["status"] != "none"
    if result["status"] == "none" and entry_program is not None:
        result = resolve_entry_identity(connection, entry_program)
    if not question_has_identity and history:
        from conversation_focus import resolve_conversation_subject
        inherited = resolve_conversation_subject(connection, question, history,
            current_identity={"status": "none", "hard_constraint": False})
        if inherited is not None:
            if result["status"] == "none":
                result = inherited
            elif (result["status"] == "resolved"
                    and set(result["direct_paths"]) == set(inherited["direct_paths"])):
                # A persistent desktop entry constrains identity. Preserve the
                # user's same-root referent and explicit source-only boundary.
                result = {**result, **{key: inherited[key] for key in
                    ("session_subject", "focused_entities", "exclusive_source_scope") if key in inherited}}
    if result["status"] == "none":
        from conversation_focus import resolve_question_field_subject
        field_subject = resolve_question_field_subject(connection, question)
        if field_subject is not None:
            result = field_subject
    result["exclusive_source_scope"] = bool(result.get("exclusive_source_scope") or
                                              _exclusive_source_scope(question, result))
    if focus_paths is not None:
        if result["hard_constraint"] or result["exclusive_source_scope"]:
            return {**result, "focus_rejected": "explicit_source_constraint"}
        valid = (isinstance(focus_paths, (list, tuple)) and 0 < len(focus_paths) <= 8
                 and all(isinstance(path, str) and path for path in focus_paths))
        candidates = []
        if valid:
            for path in dict.fromkeys(focus_paths):
                matches = [row[0] for row in connection.execute(
                    "SELECT relative_path FROM source_files WHERE relative_path=?", (path,))]
                if len(matches) != 1:
                    valid = False
                    break
                candidates.append({"identifier": path, "kind": "relative_path", "relative_paths": matches,
                                   "role": "analysis_subject"})
        if not valid:
            return {**result, "focus_rejected": "indexed_source_paths_required"}
        return {**_with_constraint(_result(candidates), hard=False, source="focus"),
                "selection_basis": "model_selected_source_paths", "exclusive_source_scope": False}
    if result["hard_constraint"]:
        return result
    if result["status"] == "resolved" and len(result["direct_paths"]) == 1:
        # Searching for supporting evidence must not replace the analysis root.
        # A validated focus action is the explicit operation for changing it.
        return result
    # Supplementary business words without definitions cannot create a new
    # hard missing-file constraint or discard the existing candidate set.
    for value in search_terms or ():
        candidate = _resolve_input(connection, value)
        if candidate["status"] != "none" and any(item["relative_paths"] for item in candidate["candidates"]):
            return {**_with_constraint(candidate, hard=False, source="search_terms"),
                    "exclusive_source_scope": result["exclusive_source_scope"]}
    return result


def identity_blocks_analysis(identity):
    """Only unresolved user file constraints block evidence investigation."""
    return bool(identity.get("hard_constraint", True) and
                identity.get("status") in {"ambiguous", "not_found"})


def identity_boundaries(identity):
    if not identity_blocks_analysis(identity):
        return []
    return [{"reason": "source_identity_" + identity["status"],
             "requested": identity["requested"], "candidates": identity["candidates"],
             "message": "Source identity needs clarification." if identity["status"] == "ambiguous"
                        else "The explicitly requested source is not indexed."}]


def identity_sql_scope(identity, alias="p", *, supplemental=False):
    """Keep root selection separate from supplementary evidence discovery."""
    if identity_blocks_analysis(identity):
        return " AND 0", ()
    if supplemental and not identity.get("exclusive_source_scope"):
        return "", ()
    if identity["status"] == "none" or (not identity.get("hard_constraint", True)
                                        and not identity.get("exclusive_source_scope")):
        return "", ()
    paths = identity["direct_paths"]
    if not paths:
        return " AND 0", ()
    return f" AND {alias}.relative_path IN ({','.join('?' for _ in paths)})", tuple(paths)
