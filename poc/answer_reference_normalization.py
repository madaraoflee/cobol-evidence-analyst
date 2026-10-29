"""Repair citation spelling only against the exact supplied identifier set.

No source is inferred from neighboring prose. A repaired link is not evidence
that the associated claim is supported.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re


_IDENTIFIER = re.compile(r"(?:ev[_:-]|fw:)[A-Za-z0-9_:./-]{1,160}\Z")
_CANDIDATE = re.compile(r"(?P<open>[\[(（])[ \t]*(?P<id>(?:ev[_:-]|fw:)"
                        r"[A-Za-z0-9_:./-]{1,160})[ \t]*(?P<close>[\])）])")
_ALIAS = re.compile(r"ev_([0-9a-f]{24})\Z")
_PAIR = {"(": ")", "（": "）", "[": "]"}
_IDENTITY_FIELDS = ("relative_path", "source_sha256", "document_name", "document_sha256",
                    "start_line", "end_line", "include_chain", "replacement_trace", "copy_instance")
_MAX_AUDIT_ROWS = 16
_MAX_AUDIT_LIST_BYTES = 3500
_MAX_MARKER_CHARS = 256


def _record(rows, row, byte_count):
    """Bound diagnostic copies independently of the normalized answer."""
    size = len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + (2 if rows else 0)
    if len(rows) >= _MAX_AUDIT_ROWS or byte_count + size > _MAX_AUDIT_LIST_BYTES:
        return byte_count
    rows.append(row)
    return byte_count + size


def _inventory(allowed):
    """Retain ambiguity when callers supply duplicate IDs with different origins."""
    entries = allowed.items() if isinstance(allowed, Mapping) else (
        ((row.get("evidence_id") or row.get("reference_id")), row)
        if isinstance(row, Mapping) else (row, None) for row in allowed)
    identities, invalid = {}, set()
    for identifier, row in entries:
        if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier):
            continue
        if isinstance(row, Mapping):
            declared = {row[key] for key in ("evidence_id", "reference_id")
                        if isinstance(row.get(key), str)}
            if declared - {identifier}:
                invalid.add(identifier)
                continue
            identity = {key: row[key] for key in _IDENTITY_FIELDS if key in row}
            signature = json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)
        else:
            signature = None
        identities.setdefault(identifier, set()).add(signature)
    ambiguous = invalid | {identifier for identifier, origins in identities.items() if len(origins) > 1}
    return set(identities) - ambiguous, ambiguous


def _escaped(text, index):
    slash = index - 1
    while slash >= 0 and text[slash] == "\\":
        slash -= 1
    return (index - slash - 1) % 2 == 1


def _protected_ranges(text):
    """Protect Markdown code and links while preserving original offsets."""
    ranges, fence, opening, offset = [], None, None, 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^(?: {0,3}>[ \t]?)*[ \t]{0,3}"
                          r"(?:(?:[-+*]|\d{1,9}[.)])[ \t]+)?(`{3,}|~{3,})(.*)", line)
        if fence:
            if (marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence)
                    and not marker[2].strip()):
                ranges.append((opening, offset + len(line)))
                fence, opening = None, None
        elif marker:
            fence, opening = marker[1], offset
        elif line.startswith(("    ", "\t")):
            ranges.append((offset, offset + len(line)))
        offset += len(line)
    if fence:
        ranges.append((opening, len(text)))

    def covered(start, end):
        return any(a < end and start < b for a, b in ranges)

    runs = list(re.finditer(r"`+", text))
    cursor = 0
    while cursor < len(runs):
        first = runs[cursor]
        if covered(first.start(), first.end()) or _escaped(text, first.start()):
            cursor += 1
            continue
        closing = next((index for index in range(cursor + 1, len(runs))
                        if len(runs[index][0]) == len(first[0])
                        and not covered(runs[index].start(), runs[index].end())), None)
        if closing is None:
            cursor += 1
            continue
        ranges.append((first.start(), runs[closing].end()))
        cursor = closing + 1
    # Rewriting a link destination or an autolink would change a different
    # Markdown construct. Citation normalization is confined to prose.
    for match in re.finditer(r"!?\[[^\]\n]*\]\([^\n)]*(?:\([^\n)]*\)[^\n)]*)*\)|<[^>\n]+>", text):
        ranges.append(match.span())
    return ranges


def normalize_answer_references(answer, allowed):
    """Return normalized prose and a non-semantic audit of exact-ID repairs.

    ``allowed`` is an ID-to-source mapping, an iterable of IDs, or supplied
    source/reference rows. Duplicate conflicting identities are unresolved.
    Only complete single-ID parentheses/brackets are considered. Audit lists
    have fixed row/byte limits; omitted counts do not limit prose repairs.
    """
    text = str(answer)
    identifiers, ambiguous = _inventory(allowed)
    protected = _protected_ranges(text)
    replacements, repairs, unresolved = [], [], []
    repair_count = unresolved_count = 0
    repair_bytes = unresolved_bytes = 2
    for match in _CANDIDATE.finditer(text):
        if (_PAIR[match["open"]] != match["close"] or _escaped(text, match.start())
                or any(start < match.end() and match.start() < end for start, end in protected)):
            continue
        identifier = match["id"]
        canonical, reason = identifier, None
        if identifier in ambiguous:
            reason = "AMBIGUOUS_SUPPLIED_ID"
        elif identifier not in identifiers:
            alias = _ALIAS.fullmatch(identifier)
            target = "ev_page_" + alias[1] if alias else None
            if target in ambiguous:
                reason = "AMBIGUOUS_SUPPLIED_ID"
            elif target is not None and target in identifiers:
                canonical = target
            else:
                reason = "REFERENCE_NOT_SUPPLIED"
        if reason:
            unresolved_count += 1
            unresolved_bytes = _record(unresolved, {
                "start": match.start(), "end": match.end(),
                "marker": match[0][:_MAX_MARKER_CHARS],
                "marker_truncated": len(match[0]) > _MAX_MARKER_CHARS,
                "marker_sha256": hashlib.sha256(match[0].encode("utf-8")).hexdigest(),
                "id": identifier, "identifier": identifier, "reason": reason,
            }, unresolved_bytes)
            continue
        replacement = f"[{canonical}]"
        if replacement == match[0]:
            continue
        replacements.append((match.start(), match.end(), replacement))
        repair_count += 1
        repair_bytes = _record(repairs, {
            "start": match.start(), "end": match.end(),
            "original": match[0][:_MAX_MARKER_CHARS],
            "original_truncated": len(match[0]) > _MAX_MARKER_CHARS,
            "original_sha256": hashlib.sha256(match[0].encode("utf-8")).hexdigest(),
            "original_id": identifier, "canonical_id": canonical,
            "replacement": replacement, "before": match[0][:_MAX_MARKER_CHARS], "after": replacement,
            "match_basis": ("unique_supplied_page_id_with_identical_complete_identifier_digest"
                            if canonical != identifier else "exact_supplied_id"),
            "reason": ("EXACT_PAGE_IDENTIFIER_DIGEST_ALIAS" if canonical != identifier
                       else "EXACT_SUPPLIED_REFERENCE_FORMAT"),
            "semantic_verification": False,
        }, repair_bytes)
    fragments, cursor = [], 0
    for start, end, replacement in replacements:
        fragments.extend((text[cursor:start], replacement))
        cursor = end
    fragments.append(text[cursor:])
    text = "".join(fragments)
    return {"answer": text, "repairs": repairs, "unresolved": unresolved,
            "repairs_total_count": repair_count,
            "repairs_omitted_count": repair_count - len(repairs),
            "unresolved_total_count": unresolved_count,
            "unresolved_omitted_count": unresolved_count - len(unresolved),
            "method": "exact_supplied_reference_normalization", "semantic_verification": False}
