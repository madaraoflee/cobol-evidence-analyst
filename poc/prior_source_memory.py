"""Explicit source provenance for candidate interpretations carried across rounds.

Memory never establishes a business fact. Only original, actually transmitted
source can authorize a citation; each request must declare the memory it uses.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from source_reading import _identify_page
from source_session import archive_evidence, read_archived_evidence


SCHEMA = "prior-source-memory/v1"
_REFERENCE = re.compile(r"\[((?:ev[_:-]|fw:)[^\]\r\n]{1,160})\]")
_BINDING_FIELDS = ("relative_path", "source_sha256", "start_line", "end_line",
                   "include_chain", "replacement_trace", "copy_instance")


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _excerpt_hash(page):
    return hashlib.sha256(page["source_text"].encode()).hexdigest()


def _binding(page):
    return {key: deepcopy(page[key]) for key in _BINDING_FIELDS if key in page}


def source_binding_sha256(page):
    return _digest(_binding(page))


def valid_findings(value):
    """Validate the entire optional findings array without repairing bad notes."""
    return (isinstance(value, list) and len(value) <= 16 and all(
        isinstance(row, dict) and set(row) == {"text", "evidence_ids"}
        and isinstance(row["text"], str) and 0 < len(row["text"].strip()) <= 4000
        and isinstance(row["evidence_ids"], list) and 0 < len(row["evidence_ids"]) <= 16
        and all(isinstance(item, str) and 0 < len(item) <= 180 for item in row["evidence_ids"])
        for row in value))


class PriorSourceMemory:
    def __init__(self, database_path, *, question, run_id, context=None):
        self.database_path = database_path
        self.scope = _digest({"question": question, "run_id": run_id, "context": context})
        self.pages = {}
        self.origins = {}
        self.blocks = {}
        self.declared_blocks = {}

    def _archived_page(self, identifier):
        saved = self.pages[identifier]
        archived = read_archived_evidence(self.database_path, identifier)
        if (archived is None or _identify_page(saved) != identifier
                or _excerpt_hash(archived) != _excerpt_hash(saved)
                or any(archived.get(key) != saved.get(key) for key in
                       ("relative_path", "source_sha256", "start_line", "end_line"))
                or (archived.get("include_chain") or []) != (saved.get("include_chain") or [])):
            raise ValueError("PRIOR_SOURCE_MEMORY_INVALID")
        return deepcopy(saved)

    @staticmethod
    def _origin_matches(page, origin, records):
        record = next((row for row in records if row.get("round_id") == origin.get("round_id")), None)
        if (record is None or record.get("request_body_sha256") != origin.get("request_body_sha256")
                or re.fullmatch(r"round-[1-9][0-9]*", str(origin.get("round_id", ""))) is None
                or re.fullmatch(r"[0-9a-f]{64}", str(origin.get("request_body_sha256", ""))) is None
                or origin.get("binding") != _binding(page)
                or origin.get("excerpt_sha256") != _excerpt_hash(page)):
            return False
        return any(row.get("evidence_id") == page["evidence_id"]
                   and row.get("path") == page["relative_path"]
                   and row.get("source_sha256") == page["source_sha256"]
                   and row.get("start_line") == page["start_line"]
                   and row.get("end_line") == page["end_line"]
                   and row.get("excerpt_sha256") == _excerpt_hash(page)
                   and row.get("binding_sha256") == source_binding_sha256(page)
                   and (row.get("include_chain") or []) == (page.get("include_chain") or [])
                   for row in record.get("sources", []))

    def checkpoint(self):
        return {"schema": SCHEMA, "scope": self.scope,
                "pages": deepcopy(list(self.pages.values())),
                "origins": deepcopy(self.origins), "blocks": deepcopy(list(self.blocks.values())),
                "declared_blocks": deepcopy(list(self.declared_blocks.values()))}

    def restore(self, saved, round_records):
        if not saved:
            return
        if not isinstance(saved, dict) or saved.get("schema") != SCHEMA or saved.get("scope") != self.scope:
            raise ValueError("PRIOR_SOURCE_MEMORY_INVALID")
        try:
            pages = {page["evidence_id"]: deepcopy(page) for page in saved["pages"]}
            origins = deepcopy(saved["origins"])
            for identifier, page in pages.items():
                if _identify_page(page) != identifier or not self._origin_matches(page, origins[identifier], round_records):
                    raise ValueError("PRIOR_SOURCE_MEMORY_INVALID")
            self.pages, self.origins = pages, origins
            for identifier in pages:
                self._archived_page(identifier)
            for block in saved.get("declared_blocks", saved["blocks"]):
                body = {key: block[key] for key in ("text", "evidence_ids", "origins", "kind", "round_id")}
                if (block["memory_id"] != "memory_" + _digest(body)
                        or block.get("verification") != "unverified"
                        or not valid_findings([{"text": body["text"], "evidence_ids": body["evidence_ids"]}])
                        or set(_REFERENCE.findall(body["text"])) - set(body["evidence_ids"])
                        or body["origins"] != [origins[identifier] for identifier in body["evidence_ids"]]):
                    raise ValueError("PRIOR_SOURCE_MEMORY_INVALID")
                self.declared_blocks[block["memory_id"]] = deepcopy(block)
            for block in saved["blocks"]:
                if block != self.declared_blocks.get(block["memory_id"]):
                    raise ValueError("PRIOR_SOURCE_MEMORY_INVALID")
                self.blocks[block["memory_id"]] = deepcopy(block)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            self.pages, self.origins, self.blocks, self.declared_blocks = {}, {}, {}, {}
            raise ValueError("PRIOR_SOURCE_MEMORY_INVALID") from exc

    def prompt(self):
        return {"schema": SCHEMA, "draft_is_evidence": False,
                "instruction": "仅继承下列明确绑定的本题早先原文；候选解释仍须核对，新的推断应补读。"
                    "补查动作可附 findings:[{text:候选解释,evidence_ids:[当前原文或本清单的引用ID]}]，"
                    "以保存已读内容；findings不能代替补查动作或最终答案。",
                "blocks": deepcopy(list(self.blocks.values()))}

    def archived_pages(self, identifiers):
        return [self._archived_page(identifier) for identifier in identifiers]

    def visible_ids(self, payload):
        memory = payload.get("prior_source_memory") or {}
        if memory.get("schema") != SCHEMA or memory.get("draft_is_evidence") is not False:
            return set()
        identifiers = set()
        for block in memory.get("blocks", []):
            if isinstance(block, dict) and block == self.declared_blocks.get(block.get("memory_id")):
                identifiers.update(block["evidence_ids"])
        return identifiers

    def effective_pages(self, payload, *, verify_archive=False):
        current = {page["evidence_id"]: page for bundle in payload.get("source_context", [])
                   for page in bundle.get("pages", [])}
        for identifier in sorted(self.visible_ids(payload)):
            if identifier not in current:
                current[identifier] = (self._archived_page(identifier) if verify_archive
                                       else deepcopy(self.pages[identifier]))
        return list(current.values())

    def _accept(self, finding, *, payload, round_record, kind):
        if not valid_findings([finding]) or not round_record:
            return False
        text = finding["text"].strip()
        identifiers = list(dict.fromkeys(finding["evidence_ids"]))
        # A note containing an unknown or framework citation is rejected as a
        # whole. Removing its bad reference must not authorize the remaining prose.
        if set(_REFERENCE.findall(text)) - set(identifiers) or any(not key.startswith("ev_page_") for key in identifiers):
            return False
        current = {page["evidence_id"]: page for bundle in payload.get("source_context", [])
                   for page in bundle.get("pages", [])}
        inherited = self.visible_ids(payload)
        if set(identifiers) - (set(current) | inherited):
            return False
        accepted_pages, accepted_origins = {}, {}
        for identifier in identifiers:
            if identifier in current:
                page = current[identifier]
                origin = {"round_id": round_record.get("round_id"),
                          "request_body_sha256": round_record.get("request_body_sha256"),
                          "binding": _binding(page), "binding_sha256": source_binding_sha256(page),
                          "excerpt_sha256": _excerpt_hash(page)}
                if _identify_page(page) != identifier or not self._origin_matches(page, origin, [round_record]):
                    return False
                if identifier in self.pages and _binding(page) != _binding(self.pages[identifier]):
                    return False
            else:
                page, origin = self._archived_page(identifier), self.origins[identifier]
            accepted_pages[identifier] = deepcopy(page)
            accepted_origins[identifier] = deepcopy(self.origins.get(identifier, origin))
        archive_evidence(self.database_path, accepted_pages.values())
        self.pages.update(accepted_pages)
        self.origins.update(accepted_origins)
        # Use the first supplying round consistently when the same source was
        # previously remembered. Blocks remain exact, independently testable units.
        body = {"text": text, "evidence_ids": identifiers,
                "origins": [deepcopy(self.origins[key]) for key in identifiers],
                "kind": kind, "round_id": round_record["round_id"]}
        block = {"memory_id": "memory_" + _digest(body), **body, "verification": "unverified"}
        self.blocks[block["memory_id"]] = block
        self.declared_blocks[block["memory_id"]] = deepcopy(block)
        return block["memory_id"]

    def accept_findings(self, findings, *, payload, round_record):
        if not valid_findings(findings):
            return 0
        return sum(bool(self._accept(row, payload=payload, round_record=round_record, kind="finding")) for row in findings)

    def accept_draft(self, text, *, payload, round_record):
        # Each paragraph must stand alone with all its citations. Text without a
        # source binding remains a draft and never grants historical authority.
        notes = [{"text": part.strip(), "evidence_ids": list(dict.fromkeys(_REFERENCE.findall(part)))}
                 for part in re.split(r"\n\s*\n", text) if part.strip()]
        previous = {key for key, block in self.blocks.items() if block["kind"] == "draft"}
        accepted = {self._accept(row, payload=payload, round_record=round_record, kind="draft") for row in notes}
        for key in previous - accepted:
            self.blocks.pop(key, None)
        return len(accepted - {False, None})
