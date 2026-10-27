"""Share repeated navigation facts on the wire without changing source evidence.

The canonical investigation, statement occurrences and evidence ledger retain
their original shapes. Only the provider projection uses inherited list fields;
an occurrence still has its own record, position and non-shared attributes.
"""

from __future__ import annotations

from copy import deepcopy
from collections import Counter
import json


_VERSION = "shared-navigation/v1"
_MARKER = "prompt_projection"
_SUFFIX = "_shared_fields"
_FACTS = "shared_navigation_facts"
_REFERENCE = "navigation_fact_ref"
_NAVIGATION = {"business_map", "evidence_groups", "business_analysis_brief", "question_investigation"}
_CONTEXT_AUTHORITY = {"pages", "working_set"}
_AUTHORITY_FIELDS = _CONTEXT_AUTHORITY | {"source_text", "framework_references", "framework_facts", "history"}
_IDENTIFIERS = {
    "evidence_id", "reference_id", "occurrence_id", "group_id", "claim_id",
    "required_evidence_ids", "core_evidence_ids", "visible_evidence_ids",
    "supplied_reference_ids", "source_evidence_ids", "framework_reference_ids",
    "evidence_ids", "reference_ids",
}
_INSTRUCTION = (
    "For navigation list X, merge sibling X_shared_fields into EACH row of X; "
    "explicit row fields take precedence. Shared values are exactly equal, "
    "including unknowns and guards. Rows remain distinct. "
    "Only navigation facts are shared; source pages, whole-program metadata, "
    "framework text, evidence IDs and the question are unchanged. "
    "A navigation_fact_ref names fields in shared_navigation_facts; "
    "merge them into that row before reading it. "
    "Neither inheritance nor references establish execution or new bindings."
)


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _size(value):
    return len(_encoded(value).encode("utf-8"))


def _contains_authority(value):
    if isinstance(value, list):
        return any(_contains_authority(item) for item in value)
    return isinstance(value, dict) and any(key in _AUTHORITY_FIELDS or _contains_authority(item)
                                         for key, item in value.items())


def _project_navigation(value):
    """Factor equal VALUES as well as keys; never merge different occurrences."""
    if isinstance(value, list):
        return [_project_navigation(item) for item in value]
    if not isinstance(value, dict):
        return value
    projected = {key: deepcopy(item) if key in _AUTHORITY_FIELDS else _project_navigation(item)
                 for key, item in value.items()}
    for key, rows in list(projected.items()):
        shared_key = key + _SUFFIX
        # Existing names, positional encodings and non-record lists are opaque.
        # A missing field is never equated with explicit null, false or zero.
        if (key in _AUTHORITY_FIELDS or not isinstance(rows, list) or len(rows) < 2 or shared_key in projected
                or any(not isinstance(row, dict) or "_values" in row for row in rows)):
            continue
        shared = {}
        for field, item in rows[0].items():
            if (field in _IDENTIFIERS or field.endswith(_SUFFIX)
                    or field in _AUTHORITY_FIELDS or _contains_authority(item)):
                continue
            encoded = _encoded(item)
            if all(field in row and _encoded(row[field]) == encoded for row in rows[1:]):
                shared[field] = item
        if not shared:
            continue
        inherited = [{field: item for field, item in row.items() if field not in shared} for row in rows]
        before = _size({key: rows})
        after = _size({shared_key: shared, key: inherited})
        if before - after < 96:
            continue
        projected[key] = inherited
        projected[shared_key] = shared
    return projected


def _navigation_sections(payload):
    for key in sorted(_NAVIGATION):
        if key in payload:
            yield payload, key
        shared = key + _SUFFIX
        if shared in payload:
            yield payload, shared
    for context in payload.get("source_context", []):
        if isinstance(context, dict):
            for key in list(context):
                if key not in _CONTEXT_AUTHORITY:
                    yield context, key


def _has_reserved_fields(value):
    if isinstance(value, list):
        return any(_has_reserved_fields(item) for item in value)
    if not isinstance(value, dict):
        return False
    return any(key in {_REFERENCE, _FACTS, _MARKER} or key.endswith(_SUFFIX)
               or key not in _AUTHORITY_FIELDS and _has_reserved_fields(item)
               for key, item in value.items())


def _share_navigation_records(payload):
    """Share exact records and equal row field sets, including provenance."""
    counts = Counter()
    facts, references = {}, {}

    def reference_for(value):
        encoded = _encoded(value)
        reference = references.get(encoded)
        if reference is None:
            reference = "nav-" + str(len(facts) + 1)
            references[encoded] = reference
            facts[reference] = value
        return reference

    def share_fields(value):
        """Share equal field sets within a list without grouping its rows.

        Calls to the same target often repeat parameter layouts while retaining
        distinct positions and guards. File observations likewise repeat a
        program scope among otherwise different statements. A subset is shared
        only where each field is present and byte-for-byte equal; no default,
        target-name inference or scope widening is involved.
        """
        if isinstance(value, dict):
            return {key: item if key in _AUTHORITY_FIELDS else share_fields(item)
                    for key, item in value.items()}
        if not isinstance(value, list):
            return value
        rows = [share_fields(item) for item in value]
        if len(rows) < 2 or any(not isinstance(row, dict) or "_values" in row for row in rows):
            return rows
        equal_fields = {}
        for index, row in enumerate(rows):
            for field, item in row.items():
                if (field in _IDENTIFIERS or field in _AUTHORITY_FIELDS or field == _REFERENCE
                        or field.endswith(_SUFFIX) or _contains_authority(item)):
                    continue
                equal_fields.setdefault((field, _encoded(item)), []).append(index)
        groups = {}
        for (field, _), indices in equal_fields.items():
            if len(indices) > 1:
                groups.setdefault(tuple(indices), {})[field] = rows[indices[0]][field]
        # One existing reference per row is sufficient. Prefer the largest
        # saving, then recompute it against rows not used by another subset.
        candidates = sorted(groups.items(), key=lambda item: (
            -(len(item[0]) - 1) * _size(item[1]), item[0], _encoded(item[1])))
        for indices, fields in candidates:
            eligible = [index for index in indices if _REFERENCE not in rows[index]]
            if len(eligible) < 2:
                continue
            encoded_fields = _encoded(fields)
            reference = references.get(encoded_fields, "nav-" + str(len(facts) + 1))
            replacements = [{_REFERENCE: reference, **{key: item for key, item in rows[index].items()
                             if key not in fields}} for index in eligible]
            overhead = 0 if encoded_fields in references else _size({reference: fields})
            saving = sum(_size(rows[index]) for index in eligible) - sum(map(_size, replacements)) - overhead
            if saving < 96:
                continue
            reference_for(fields)
            for index, replacement in zip(eligible, replacements):
                rows[index] = replacement
        return rows

    def collect(value):
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            encoded = _encoded(value)
            if len(encoded) >= 256 and not _contains_authority(value):
                counts[encoded] += 1
            for key, item in value.items():
                if key not in _AUTHORITY_FIELDS:
                    collect(item)

    sections = list(_navigation_sections(payload))
    for owner, key in sections:
        owner[key] = share_fields(owner[key])
    for owner, key in sections:
        collect(owner[key])

    def replace(value):
        if isinstance(value, list):
            return [replace(item) for item in value]
        if not isinstance(value, dict):
            return value
        encoded = _encoded(value)
        if counts[encoded] > 1 and len(encoded) >= 256:
            reference = reference_for(value)
            return {_REFERENCE: reference, **{key: item for key, item in value.items()
                    if key in _IDENTIFIERS or key in {"argument", "kind", "relative_path", "start_line", "end_line"}}}
        return {key: item if key in _AUTHORITY_FIELDS else replace(item) for key, item in value.items()}

    for owner, key in sections:
        owner[key] = replace(owner[key])
    if facts:
        payload[_FACTS] = facts


def project_prompt_payload(payload):
    """Return a smaller provider-only view, or the original when it cannot help.

    No truncation, relevance selection, fact inference, ID rebinding or changes
    to local completeness occur here. Rebuild this projection after every trim.
    Protected source/framework/history fields are copied verbatim, including
    unknown extension fields; compilation/COPY provenance is not reinterpreted.
    """
    if not isinstance(payload, dict) or _MARKER in payload or _FACTS in payload:
        return payload
    if any(_has_reserved_fields(owner[key]) for owner, key in _navigation_sections(payload)):
        return payload
    projected = deepcopy(payload)
    if "business_analysis_brief" in projected:
        from business_synthesis import project_analysis_brief
        projected["business_analysis_brief"] = project_analysis_brief(projected["business_analysis_brief"])
    for key in _NAVIGATION:
        if key in projected:
            projected.update(_project_navigation({key: projected[key]}))
    for context in projected.get("source_context", []):
        if not isinstance(context, dict):
            continue
        for key in list(context):
            if key not in _CONTEXT_AUTHORITY:
                context[key] = _project_navigation(context[key])
    _share_navigation_records(projected)
    marker = {"format": _VERSION, "instruction": _INSTRUCTION}
    if _size(payload) - _size(projected) <= _size({_MARKER: marker}) + 96:
        return payload
    projected[_MARKER] = marker
    return projected
