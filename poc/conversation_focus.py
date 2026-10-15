"""Small, locally rebound conversation subjects, separate from model prose.

A subject identifies the source objects being discussed. It is neither proof
of business behavior nor permission to replace a current user source constraint.
Saved hashes describe the old discussion; rebinding never edits that record.
"""

from __future__ import annotations

from copy import deepcopy
from collections import OrderedDict, defaultdict
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock

from statement_facts import data_references


SUBJECT_VERSION = "conversation-subject/v1"
_MAX_PATHS = 8
_MAX_ENTITIES = 32
_NAME = r"[A-Za-z_$#@][A-Za-z0-9_$#@-]*"
_MENTION = re.compile(rf"{_NAME}(?:\s+(?:OF|IN)\s+{_NAME})*", re.I)
_ORDINAL = re.compile(r"第\s*([一二三四五六七八九十1-9]|10)\s*(?:种|種|个|個|项|項|类|類|情况|情況)|\b(first|second|third|fourth|fifth)\b", re.I)
_ORDINAL_VALUES = {value: index for index, value in enumerate("一二三四五六七八九十", 1)}
_ORDINAL_VALUES.update({"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5})
_FIELD_CUE = re.compile(r"(?:字段|欄位|变量|變量|\bfield\b)\s*[:：`\"']?\s*(" + _NAME + ")", re.I)
_PROGRAM_CUE = re.compile(r"(?:程序(?:名)?|程式|(?i:program-id))\s*[:：`\"']?\s*(" + _NAME + ")")
_RESET = re.compile(r"换个问题|换一个问题|換個問題|換一個問題|不看(?:刚才|剛才|前面)(?:的)?程序|"
    r"(?:全库|全庫|整个仓库|整個倉庫).{0,12}(?:有哪些|所有|全部).{0,8}(?:程序|文件)|"
    r"\b(?:change\s+(?:the\s+)?topic|new\s+topic|all\s+programs\s+in\s+(?:the\s+)?repository)\b", re.I)
_MAX_SUBJECT_BYTES = 8192
_LOOKUP_CACHE = OrderedDict()
_DEFINITION_CACHE = OrderedDict()
_LOOKUP_CACHE_LOCK = RLock()
_MAX_LOOKUP_CACHE_ENTRIES = 128


def _bounded_subject(subject):
    def size():
        return len(json.dumps(subject, ensure_ascii=False).encode("utf-8"))
    omitted = 0
    while subject["entity_bindings"] and size() > _MAX_SUBJECT_BYTES:
        subject["entity_bindings"].pop()
        omitted += 1
        subject["omitted_entity_binding_count"] = omitted
    if size() > _MAX_SUBJECT_BYTES:
        subject.update(status="unresolved", root_paths=[], programs=[], entities=[],
            entity_bindings=[], focus_entities=[], ordinal_subjects=[],
            scope={"exclusive_source_scope": False, "paths": [], "origin": "storage_budget"})
        subject["unresolved"] = ["conversation_subject_storage_budget"]
        subject["diagnostics"] = subject["unresolved"]
    return subject


def _rows(connection, sql, parameters=()):
    cursor = connection.execute(sql, parameters)
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor]


def _metadata(connection):
    values = dict(connection.execute("SELECT key,value FROM metadata WHERE key IN "
        "('source_root_hash','snapshot_id','source_options')"))
    options = json.loads(values.get("source_options", "{}"))
    context = options.get("compilation_context") or {}
    return {"source_root_hash": values.get("source_root_hash"),
        "snapshot_id": values.get("snapshot_id"),
        "compilation_context_fingerprint": hashlib.sha256(json.dumps(context,
            sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()}


def _mentions(question):
    result = []
    for match in _MENTION.finditer(str(question)):
        references = data_references(match[0])
        if len(references) == 1:
            result.append((references[0], match.start(), match.end()))
        if len(result) >= 64:
            break
    return result


def _lookup_generation(connection):
    """Cache only sealed on-disk snapshots, never a writer's pending changes."""
    if connection.total_changes:
        return None
    try:
        filename = next((row[2] for row in connection.execute("PRAGMA database_list")
                         if row[1] == "main"), None)
        if not filename:
            return None
        metadata = tuple(connection.execute("SELECT key,value FROM metadata WHERE key IN "
            "('source_root_hash','snapshot_id','source_options','parser_version','schema_version',"
            "'index_kind','indexed_at_utc') ORDER BY key"))
        if not any(row[0] == "snapshot_id" and row[1] for row in metadata):
            return None
        files = []
        for suffix in ("", "-wal"):
            path = Path(filename + suffix)
            if path.exists():
                stat = path.stat()
                files.append((suffix, stat.st_size, stat.st_mtime_ns))
        return (str(Path(filename).resolve()), tuple((row[0], row[1]) for row in metadata), tuple(files))
    except (OSError, sqlite3.Error):
        return None


def _lookup_cached(cache, key):
    if key is None:
        return None
    with _LOOKUP_CACHE_LOCK:
        if key not in cache:
            return None
        cache.move_to_end(key)
        return deepcopy(cache[key])


def _lookup_remember(cache, key, value):
    if key is None:
        return
    with _LOOKUP_CACHE_LOCK:
        cache[key] = deepcopy(value)
        cache.move_to_end(key)
        while len(cache) > _MAX_LOOKUP_CACHE_ENTRIES:
            cache.popitem(last=False)


def _definition_paths(connection, paths):
    """Only uniquely resolved COPY definitions join a program's local symbols."""
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        return _resolved_definition_paths(connection, paths)
    finally:
        if owns_transaction:
            connection.rollback()


def _resolved_definition_paths(connection, paths):
    paths = tuple(sorted(set(paths)))
    generation = _lookup_generation(connection)
    key = (generation, paths) if generation is not None else None
    cached = _lookup_cached(_DEFINITION_CACHE, key)
    if cached is not None:
        return cached
    selected = set(paths)
    for _ in range(8):
        slots = ",".join("?" for _ in selected)
        children = {row[0] for row in connection.execute("SELECT DISTINCT s.relative_path "
            "FROM relations r JOIN symbols s ON s.symbol_id=r.target_entity_id "
            "WHERE r.relation_type='INCLUDES_COPY' AND r.status='confirmed' "
            "AND r.relative_path IN (" + slots + ")", tuple(selected))}
        if children <= selected or len(selected | children) > 128:
            break
        selected.update(children)
    _lookup_remember(_DEFINITION_CACHE, key, selected)
    return selected


@lru_cache(maxsize=2048)
def _qualified_reference(suffix):
    # This is immutable lexical parsing, not a cached source/symbol binding.
    choices = data_references(suffix)
    return choices[0] if len(choices) == 1 else None


def _matching_reference(reference, qualified):
    choice = _qualified_reference(qualified.split("::", 1)[-1])
    if choice is None or choice.name != reference.name:
        return False
    ancestors = iter(choice.qualifiers)
    return all(any(ancestor == qualifier for ancestor in ancestors)
               for qualifier in reference.qualifiers)


def _path_symbol_index(connection):
    return (" INDEXED BY idx_symbols_path" if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_symbols_path'").fetchone() else "")


def _bindings(connection, mentions, paths=None):
    # Candidate selection and its declaration join must observe one snapshot,
    # including callers that did not open the repository read transaction.
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        return _lookup_bindings(connection, mentions, paths)
    finally:
        if owns_transaction:
            connection.rollback()


def _lookup_bindings(connection, mentions, paths=None):
    names = list(dict.fromkeys(reference.name for reference, _, _ in mentions))
    if not names:
        return {}, set()
    generation = _lookup_generation(connection)
    key = (generation, None if paths is None else tuple(sorted(set(paths))),
           tuple(reference.identity for reference, _, _ in mentions)) if generation is not None else None
    cached = _lookup_cached(_LOOKUP_CACHE, key)
    if cached is not None:
        return cached
    ambiguous_names = set()
    if paths is None:
        qualified_names = {reference.name for reference, _, _ in mentions if reference.qualifiers}
        counted_names = [name for name in names if name not in qualified_names]
        if counted_names:
            # A bare reference cannot distinguish any of its same-name symbols.
            # Count every candidate using the covering name index before fetching
            # declaration pages. Qualified references still inspect all rows.
            counts = dict(connection.execute("SELECT s.name,COUNT(*) FROM symbols s WHERE "
                "s.symbol_type IN ('Field','ConditionName') AND s.name IN (" +
                ",".join("?" for _ in counted_names) + ") GROUP BY s.name", counted_names))
            ambiguous_names = {name for name, count in counts.items() if count > 1}
            names = [name for name in names if name not in counted_names or counts.get(name) == 1]
    parameters = list(names)
    where = "s.name IN (" + ",".join("?" for _ in names) + ")"
    if paths is not None:
        local = _definition_paths(connection, paths)
        where += " AND s.relative_path IN (" + ",".join("?" for _ in local) + ")"
        parameters.extend(sorted(local))
    # Common field names can occur in thousands of programs. With several COPY
    # paths SQLite can prefer the name index and fetch all those unrelated rows
    # before testing the path. Seek the existing local path index explicitly.
    index = _path_symbol_index(connection) if paths is not None else ""
    rows = (_rows(connection, "SELECT s.symbol_id,s.name,s.qualified_name FROM symbols s" + index +
        " WHERE s.symbol_type IN ('Field','ConditionName') AND " + where, parameters) if names else [])
    by_name = defaultdict(list)
    for row in rows:
        by_name[row["name"]].append(row)
    selected = {}
    bound, unresolved = {}, set()
    for reference, _, _ in mentions:
        if reference.name in ambiguous_names:
            unresolved.add(reference.identity)
            continue
        matches = [row for row in by_name[reference.name] if _matching_reference(reference, row["qualified_name"])]
        if len(matches) == 1:
            selected[reference.identity] = matches[0]["symbol_id"]
        elif matches:
            unresolved.add(reference.identity)
    # Materialize declaration/version joins only after all same-name and
    # qualified candidates have been considered. No candidate LIMIT may turn
    # a later qualified match into absence or a common field into confirmation.
    if selected:
        identities = sorted(set(selected.values()))
        records = _rows(connection, "SELECT s.symbol_id,s.relative_path,s.program_name,s.qualified_name,"
            "u.start_line AS definition_line,u.parse_status,f.sha256 AS source_sha256 "
            "FROM symbols s JOIN code_units u ON u.unit_id=s.definition_unit_id "
            "JOIN source_files f ON f.relative_path=s.relative_path "
            "WHERE s.symbol_id IN (" + ",".join("?" for _ in identities) + ")", identities)
        records = {row["symbol_id"]: row for row in records}
        for identity, symbol in selected.items():
            row = records.get(symbol)
            if row is not None and row["parse_status"] != "candidate":
                bound[identity] = {field: value for field, value in row.items() if field != "parse_status"}
            else:
                unresolved.add(identity)
    _lookup_remember(_LOOKUP_CACHE, key, (bound, unresolved))
    return bound, unresolved


def _entity_name(binding):
    return binding["qualified_name"].split("::", 1)[-1]


def _unbound_explicit_fields(connection, mentions, bindings, paths):
    references = [reference for reference, _, _ in mentions if ("-" in reference.name or reference.qualifiers)
                  and reference.identity not in bindings]
    if not references:
        return set()
    names = list(dict.fromkeys(reference.name for reference in references))
    local = _definition_paths(connection, paths)
    known_objects = {row[0] for row in connection.execute("SELECT name FROM symbols" + _path_symbol_index(connection) + " WHERE "
        "symbol_type IN ('Program','Paragraph','Section','Copybook') AND name IN (" +
        ",".join("?" for _ in names) + ") AND relative_path IN (" +
        ",".join("?" for _ in local) + ")", (*names, *sorted(local)))}
    return {reference.identity for reference in references if reference.qualifiers or reference.name not in known_objects}


def _identity(paths, *, subject=None):
    paths = list(dict.fromkeys(paths))
    return {"status": "resolved" if len(paths) == 1 else "ambiguous" if paths else "none",
        "kind": "relative_path", "direct_paths": paths, "requested": paths,
        "candidates": [{"identifier": path, "kind": "relative_path",
            "relative_paths": [path], "role": "analysis_subject"} for path in paths],
        "hard_constraint": False, "exclusive_source_scope": False,
        "selection_source": "conversation_subject",
        "selection_basis": "locally_revalidated_conversation_subject",
        **({"session_subject": subject, "focused_entities": subject.get("focus_entities", [])}
           if subject is not None else {})}


def _question_identity(connection, question):
    # Keep explicit object resolution identical to the default repository path.
    # Import lazily: that path also calls this module for conversation fallback.
    from repository_identity import _resolve_input
    return _resolve_input(connection, str(question))


def resolve_question_field_subject(connection, question):
    """Locate explicitly named fields with unique local program ownership.

    Generic prose words, unknown/ambiguous fields, and standalone COPY layouts
    do not become a confirmed program merely because text retrieval finds them.
    """
    text = str(question)
    if _PROGRAM_CUE.search(text) or _RESET.search(text):
        return None
    cues = {name.upper() for name in _FIELD_CUE.findall(text)}
    mentions = [(reference, start, end) for reference, start, end in _mentions(text)
        if "-" in reference.name or reference.qualifiers or reference.name in cues
        or (text[start:end].isascii() and text[start:end].isupper())
        or (start > 0 and text[start - 1:start] in {"`", '"', "'"})]
    bound, ambiguous = _bindings(connection, mentions)
    if not bound or ambiguous or any(reference.identity not in bound for reference, _, _ in mentions):
        return None
    paths = list(dict.fromkeys(row["relative_path"] for row in bound.values()))
    for path in paths:
        programs = connection.execute("SELECT name,parse_status FROM code_units WHERE unit_type='Program' "
            "AND relative_path=? LIMIT 2", (path,)).fetchall()
        if len(programs) != 1 or programs[0][1] == "candidate":
            return None
    result = _identity(paths)
    # A field-only question retains targeted semantic inspection. Complete
    # program supply is a separate policy for an explicit/conversational root.
    result.update(kind="field", selection_source="question_fields", selection_basis="uniquely_bound_question_fields")
    result["requested"] = [_entity_name(row) for row in bound.values()]
    for candidate in result["candidates"]:
        candidate["kind"] = "field"
    return result


def build_session_subject(connection, question, identity, *, previous=None):
    """Build bounded local state from an identity and user-named field objects."""
    metadata = _metadata(connection)
    identity = identity or {}
    paths = list(dict.fromkeys(identity.get("direct_paths", [])))[:_MAX_PATHS]
    records = []
    if paths:
        slots = ",".join("?" for _ in paths)
        records = _rows(connection, "SELECT u.relative_path,u.name AS program_name,"
            "u.start_line AS definition_line,u.parse_status,f.sha256 AS source_sha256 "
            "FROM code_units u JOIN source_files f ON f.relative_path=u.relative_path "
            "WHERE u.unit_type='Program' AND u.relative_path IN (" + slots + ") "
            "ORDER BY u.relative_path,u.start_line LIMIT 65", paths)
    known_paths = list(dict.fromkeys(row["relative_path"] for row in records))
    same_previous = (isinstance(previous, dict) and previous.get("schema") == SUBJECT_VERSION
        and previous.get("root_paths") == paths and previous.get("source_root_hash") == metadata["source_root_hash"])
    previous = deepcopy(previous) if same_previous else {}
    status = ("resolved" if identity.get("status") == "resolved" and len(paths) == 1 and len(records) == 1
        and known_paths == paths and not any(row["parse_status"] == "candidate" for row in records)
        else "unresolved" if not known_paths else "candidate")
    programs = [{key: value for key, value in row.items() if key != "parse_status"} for row in records]
    old_versions = {(row.get("relative_path"), row.get("program_name")): row.get("source_sha256")
                    for row in previous.get("programs", []) if isinstance(row, dict)}
    version_changed = bool(previous) and (old_versions != {
        (row["relative_path"], row["program_name"]): row["source_sha256"] for row in programs}
        or previous.get("compilation_context_fingerprint") != metadata["compilation_context_fingerprint"])
    subject = {"schema": SUBJECT_VERSION, "status": status,
        **metadata, "root_paths": known_paths, "programs": programs, "entities": [],
        "entity_bindings": [], "focus_entities": [], "ordinal_subjects": [],
        "revalidated": True, "version_changed": version_changed,
        "previous_positions_discarded": version_changed,
        "scope": {"exclusive_source_scope": bool(identity.get("exclusive_source_scope")),
            "paths": known_paths, "origin": identity.get("selection_source", "question")},
        "basis": {"selection_source": identity.get("selection_source", "question"),
            "question_sha256": hashlib.sha256(str(question).encode()).hexdigest()},
        "unresolved": []}
    subject["diagnostics"] = subject["unresolved"]
    if status != "resolved":
        subject["diagnostics"].append("conversation_program_not_uniquely_bound")
        return _bounded_subject(subject)
    current_mentions = _mentions(question)
    current_bindings, current_ambiguous = _bindings(connection, current_mentions, paths)
    preserved_mentions = _mentions(" ".join(previous.get("entities", [])))
    preserved_bindings, _ = _bindings(connection, preserved_mentions, paths)
    field_cues = {name.upper() for name in _FIELD_CUE.findall(str(question))}
    unresolved_cues = field_cues - {ref.name for ref, _, _ in current_mentions if ref.identity in current_bindings}
    unbound_fields = _unbound_explicit_fields(connection, current_mentions, current_bindings, paths)
    current_field_request = bool(current_bindings or current_ambiguous or unresolved_cues or unbound_fields)
    entity_bindings = current_bindings if current_field_request else preserved_bindings
    subject["entity_bindings"] = list({row["symbol_id"]: row for row in entity_bindings.values()}.values())[:_MAX_ENTITIES]
    subject["entities"] = [_entity_name(row) for row in subject["entity_bindings"]]
    subject["focus_entities"] = list(subject["entities"])
    if previous and not current_field_request:
        previous_focus = previous.get("focus_entities", previous.get("entities", []))
        prior_focus, prior_unresolved = _bindings(connection,
            _mentions(" ".join(previous_focus)), paths)
        subject["focus_entities"] = list(dict.fromkeys(_entity_name(row) for row in prior_focus.values()))
        if prior_unresolved:
            subject["diagnostics"].append("conversation_previous_focus_ambiguous")
        if len(subject["focus_entities"]) != len(previous_focus):
            subject["diagnostics"].append("conversation_previous_focus_not_bound")
    if current_ambiguous:
        subject["diagnostics"].append("conversation_field_ambiguous")
    if unresolved_cues or unbound_fields - current_ambiguous:
        subject["diagnostics"].append("conversation_field_not_bound")
    # Preserve ordinal labels only if their exact fields still bind locally.
    old_ordinals = []
    for original in previous.get("ordinal_subjects", []):
        if not isinstance(original, dict) or type(original.get("ordinal")) is not int:
            continue
        bindings, unresolved = _bindings(connection, _mentions(" ".join(original.get("entities", []))), paths)
        names = list(dict.fromkeys(_entity_name(row) for row in bindings.values()))
        if names and not unresolved and len(names) == len(original.get("entities", [])):
            old_ordinals.append({"ordinal": original["ordinal"], "entities": names})
    subject["ordinal_subjects"] = old_ordinals[:10]
    markers = list(_ORDINAL.finditer(str(question)))[:10]
    for index, marker in enumerate(markers):
        value = (marker[1] or marker[2]).lower()
        ordinal = int(value) if value.isdigit() else _ORDINAL_VALUES[value]
        end = markers[index + 1].start() if index + 1 < len(markers) else len(str(question))
        names = list(dict.fromkeys(_entity_name(current_bindings[reference.identity])
            for reference, start, _ in current_mentions
            if marker.end() <= start < end and reference.identity in current_bindings))
        if names:
            subject["ordinal_subjects"] = [row for row in subject["ordinal_subjects"] if row["ordinal"] != ordinal]
            subject["ordinal_subjects"].append({"ordinal": ordinal, "entities": names})
    subject["ordinal_subjects"].sort(key=lambda row: row["ordinal"])
    if len(markers) == 1 and not current_bindings:
        value = (markers[0][1] or markers[0][2]).lower()
        ordinal = int(value) if value.isdigit() else _ORDINAL_VALUES[value]
        selected = [row for row in subject["ordinal_subjects"] if row["ordinal"] == ordinal]
        subject["focus_entities"] = list(selected[0]["entities"]) if len(selected) == 1 else []
        if not selected:
            subject["diagnostics"].append("conversation_ordinal_not_established")
    return _bounded_subject(subject)


def _saved_subject(history):
    for message in reversed(list(history or [])[-64:]):
        if not isinstance(message, dict) or message.get("role") != "assistant" or message.get("status", "completed") != "completed":
            continue
        state = message.get("investigation_state") or {}
        present = "session_subject" in message or isinstance(state, dict) and "session_subject" in state
        subject = message.get("session_subject") if "session_subject" in message else (
            state.get("session_subject") if isinstance(state, dict) else None)
        # The newest completed answer is a barrier. An empty/unresolved subject
        # cannot revive an older successful discussion; missing keys permit the
        # separately checked legacy user-question fallback only.
        return present, subject if isinstance(subject, dict) and subject.get("schema") == SUBJECT_VERSION else None
    return False, None


def resolve_conversation_subject(connection, question, history, *, current_identity=None):
    """Return a source identity only for locally revalidated historical focus.

    Current explicit objects and ambiguity always win. Legacy conversations
    may recover a program from the latest explicit user question, never from
    assistant text, citations, evidence ordering, or rule-lead ordering.
    """
    current = current_identity if current_identity is not None else _question_identity(connection, question)
    if current.get("status", "none") != "none" or current.get("hard_constraint") or _PROGRAM_CUE.search(str(question)) or _RESET.search(str(question)):
        return None
    subject_present, previous = _saved_subject(history)
    if subject_present and previous is None:
        return None
    if not subject_present:
        for message in reversed(list(history or [])[-64:]):
            if not isinstance(message, dict) or message.get("role") != "user" or message.get("status", "completed") != "completed":
                continue
            old_question = str(message.get("content", ""))
            identity = _question_identity(connection, old_question)
            if identity.get("status") != "none":
                previous = build_session_subject(connection, old_question, identity)
                break
            if _PROGRAM_CUE.search(old_question):
                return None
            if _RESET.search(old_question):
                return None
            # A later explicit field question cannot revive an older program.
            bindings, ambiguous = _bindings(connection, _mentions(old_question))
            if bindings or ambiguous or _FIELD_CUE.search(old_question):
                return None
    if not isinstance(previous, dict) or previous.get("status") != "resolved":
        return None
    metadata = _metadata(connection)
    if previous.get("source_root_hash") != metadata["source_root_hash"]:
        return None
    paths = previous.get("root_paths", [])
    if not isinstance(paths, list) or len(paths) != 1 or not all(isinstance(path, str) and path for path in paths):
        return None
    # A user may have switched after the last saved answer. Never let an old
    # subject cross a later unknown/ambiguous program or explicit other field.
    latest_user = next((message for message in reversed(list(history or [])[-64:])
        if isinstance(message, dict) and message.get("role") == "user"), None)
    if latest_user:
        user_question = str(latest_user.get("content", ""))
        if _RESET.search(user_question):
            return None
        user_identity = _question_identity(connection, user_question)
        if user_identity.get("status") in {"ambiguous", "not_found"}:
            return None
        if user_identity.get("status") == "none" and _PROGRAM_CUE.search(user_question):
            return None
        if user_identity.get("status") == "resolved" and user_identity.get("direct_paths") != paths:
            previous = build_session_subject(connection, user_question, user_identity)
            if previous["status"] != "resolved":
                return None
            paths = previous["root_paths"]
        user_mentions = _mentions(user_question)
        user_local, user_local_ambiguous = _bindings(connection, user_mentions, paths)
        user_global, user_global_ambiguous = _bindings(connection, user_mentions)
        if (set(user_global) | user_global_ambiguous) - (set(user_local) | user_local_ambiguous):
            return None
        recent = list(history or [])[-64:]
        latest_user_position = max(index for index, message in enumerate(recent)
            if isinstance(message, dict) and message.get("role") == "user")
        last_answer_position = max((index for index, message in enumerate(recent)
            if isinstance(message, dict) and message.get("role") == "assistant"
            and message.get("status", "completed") == "completed"), default=-1)
        if latest_user_position > last_answer_position and (user_local or user_local_ambiguous
                or _unbound_explicit_fields(connection, user_mentions, user_local, paths)
                or _FIELD_CUE.search(user_question)):
            previous = build_session_subject(connection, user_question, _identity(paths), previous=previous)
    mentions = _mentions(question)
    local, local_ambiguous = _bindings(connection, mentions, paths)
    global_bindings, global_ambiguous = _bindings(connection, mentions)
    external = (set(global_bindings) | global_ambiguous) - (set(local) | local_ambiguous)
    if external:
        return None
    rebound = build_session_subject(connection, question, _identity(paths), previous=previous)
    if rebound["status"] != "resolved":
        return None
    result = _identity(paths, subject=rebound)
    result["exclusive_source_scope"] = bool(previous.get("scope", {}).get("exclusive_source_scope"))
    rebound["scope"] = {**rebound["scope"], "exclusive_source_scope": result["exclusive_source_scope"]}
    return result
