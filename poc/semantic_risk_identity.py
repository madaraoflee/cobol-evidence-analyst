"""Stable identities for already nominated, source-bound review concerns.

These keys track review work, not semantic truth. A changed source or assertion
is different work even when its risk category is unchanged. Presentation order,
answer offsets and generated evidence identifiers do not establish progress.
"""

import hashlib
import json
import re


_REFERENCE = re.compile(r"\[(?:ev[_:-]|fw:)[A-Za-z0-9_:./-]+\]")
_QUOTED = re.compile(r"('(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`+[^`]*`+)")
_SOURCE_FIELDS = ("relative_path", "source_sha256", "document_name", "document_sha256",
                  "start_line", "end_line", "include_chain", "include_chain_sha256", "text_offset_chars",
                  "replacement_trace", "copy_instance", "source_binding_sha256",
                  "kind", "path", "sha256", "quote", "source_context")


def _claim_text(value):
    # Whitespace outside literals is presentation; whitespace inside a COBOL
    # literal can change its value and must remain part of the claim identity.
    parts = _QUOTED.split(str(value or ""))
    return "".join(part if index % 2 else re.sub(r"\s+", " ", _REFERENCE.sub("", part))
                   for index, part in enumerate(parts)).strip()


def _source_keys(references):
    keys = set()
    for reference in references or ():
        if not isinstance(reference, dict):
            continue
        identity = {key: reference[key] for key in _SOURCE_FIELDS if key in reference}
        for key in ("relative_path", "path"):
            if isinstance(identity.get(key), str):
                identity[key] = identity[key].replace("\\", "/").removeprefix("./")
        if identity:
            keys.add(json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
    return sorted(keys)


def _risk_key(domain, row):
    span = row.get("answer_span") or row.get("span") or {}
    span = span if isinstance(span, dict) else {}
    text = row.get("assertion") or row.get("quote") or span.get("text") or ""
    # SQL audit excerpts can be shortened while retaining the full span hash.
    # Do not collapse different long assertions sharing the displayed prefix.
    truncated = (type(span.get("start")) is int and type(span.get("end")) is int
                 and span["end"] - span["start"] > len(text))
    identity = {"domain": domain, "kind": row.get("kind"), "claim": _claim_text(text),
                "source_refs": _source_keys(row.get("source_refs"))}
    if span.get("sha256") and (truncated or not text):
        identity["full_claim_sha256"] = span["sha256"]
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def risk_identities(assessment):
    """Deduplicate bounded upstream reports without changing their verdicts.

    Model findings enter only through the locally validated semantic_review
    completion report. Raw model output and invalid review reports are not
    accepted here; source and quote validation belongs to that report's producer.
    """
    groups = [("sql", (assessment.get("sql_boundary_review") or {}).get("risks", [])),
              ("source_semantics", assessment.get("semantic_risk_candidates", []))]
    review = assessment.get("semantic_review") or {}
    if (review.get("status") == "changes_requested"
            and review.get("validation") == "strict_payload_bound"):
        groups.append(("semantic_review", review.get("findings", [])))
        groups.append(("semantic_review_omission", [
            {"kind": "omission", "assertion": row.get("reason"), "source_refs": row.get("source_refs", [])}
            for row in review.get("omissions", []) if isinstance(row, dict)]))
    return frozenset(_risk_key(domain, row) for domain, rows in groups for row in rows
                     if isinstance(row, dict) and row.get("kind"))
