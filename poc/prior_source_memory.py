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
        self.retracted_blocks = {}
        self.pending_reread_ids = set()

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
                "declared_blocks": deepcopy(list(self.declared_blocks.values())),
                "retracted_blocks": deepcopy(self.retracted_blocks),
                "pending_reread_ids": sorted(self.pending_reread_ids)}

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
                        or body["kind"] not in {"finding", "draft", "source"}
                        or block.get("content_kind", "source_reference" if body["kind"] == "source" else "model_candidate")
                            != ("source_reference" if body["kind"] == "source" else "model_candidate")
                        or not valid_findings([{"text": body["text"], "evidence_ids": body["evidence_ids"]}])
                        or set(_REFERENCE.findall(body["text"])) - set(body["evidence_ids"])
                        or body["origins"] != [origins[identifier] for identifier in body["evidence_ids"]]):
                    raise ValueError("PRIOR_SOURCE_MEMORY_INVALID")
                self.declared_blocks[block["memory_id"]] = deepcopy(block)
            for block in saved["blocks"]:
                if block != self.declared_blocks.get(block["memory_id"]):
                    raise ValueError("PRIOR_SOURCE_MEMORY_INVALID")
                self.blocks[block["memory_id"]] = deepcopy(block)
            retracted = saved.get("retracted_blocks", {})
            pending = saved.get("pending_reread_ids", [])
            if (not isinstance(retracted, dict) or any(key not in self.declared_blocks
                    or self.declared_blocks[key]["kind"] == "source"
                    or not isinstance(reason, str) or not reason for key, reason in retracted.items())
                    or set(retracted) & self.blocks.keys()
                    or not isinstance(pending, list) or any(key not in pages for key in pending)):
                raise ValueError("PRIOR_SOURCE_MEMORY_INVALID")
            self.retracted_blocks = deepcopy(retracted)
            self.pending_reread_ids = set(pending)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            self.pages, self.origins, self.blocks, self.declared_blocks = {}, {}, {}, {}
            self.retracted_blocks, self.pending_reread_ids = {}, set()
            raise ValueError("PRIOR_SOURCE_MEMORY_INVALID") from exc

    def prompt(self):
        return {"schema": SCHEMA, "draft_is_evidence": False,
                "confirmed_facts": [],
                "instruction": "source_reference只记录曾供证原文的位置；model_candidate是模型候选解释，不是确认事实。"
                    "真实引用仅证明来源，不能证明候选的公式、条件或结论正确。决定性或有冲突的结论应按引用回读原文。"
                    "补查动作可附 findings:[{text:候选解释,evidence_ids:[当前原文或本清单的引用ID]}]，"
                    "以保存已读内容；findings不能代替补查动作或最终答案。",
                "requires_source_reread": sorted(self.pending_reread_ids),
                "blocks": deepcopy(list(self.blocks.values()))}

    def archived_pages(self, identifiers):
        return [self._archived_page(identifier) for identifier in identifiers]

    def visible_ids(self, payload):
        memory = payload.get("prior_source_memory") or {}
        if memory.get("schema") != SCHEMA or memory.get("draft_is_evidence") is not False:
            return set()
        identifiers = set()
        for block in memory.get("blocks", []):
            # Retraction changes future candidate text, not which original
            # sources an already-sent request was authorized to cite.
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
        if any(old["text"] == text and set(old["evidence_ids"]) == set(identifiers)
               for key, old in self.declared_blocks.items() if key in self.retracted_blocks):
            return False
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
        for identifier, block in self.blocks.items():
            if (block["kind"] == kind and block["text"] == text
                    and set(block["evidence_ids"]) == set(identifiers)):
                # Repeating the same interpretation in another round adds no
                # knowledge. Keep its first supplying origin and wire identity.
                return identifier
        archive_evidence(self.database_path, accepted_pages.values())
        self.pages.update(accepted_pages)
        self.origins.update(accepted_origins)
        # Use the first supplying round consistently when the same source was
        # previously remembered. Blocks remain exact, independently testable units.
        body = {"text": text, "evidence_ids": identifiers,
                "origins": [deepcopy(self.origins[key]) for key in identifiers],
                "kind": kind, "round_id": round_record["round_id"]}
        block = {"memory_id": "memory_" + _digest(body), **body, "verification": "unverified",
                 "content_kind": "model_candidate"}
        if block["memory_id"] in self.retracted_blocks:
            return False
        self.blocks[block["memory_id"]] = block
        self.declared_blocks[block["memory_id"]] = deepcopy(block)
        return block["memory_id"]

    def _source_reference(self, evidence_id, *, include_source=False):
        page, origin = self.pages[evidence_id], self.origins[evidence_id]
        text = f"原文位置：{page['relative_path']}:{page['start_line']}-{page['end_line']}。"
        original = text + "\n已供给的完整引用原文：\n" + page["source_text"]
        if (include_source and len(original) <= 4000
                and not (set(_REFERENCE.findall(original)) - {evidence_id})):
            text = original
        body = {"text": text,
                "evidence_ids": [evidence_id], "origins": [deepcopy(origin)],
                "kind": "source", "round_id": origin["round_id"]}
        block = {"memory_id": "memory_" + _digest(body), **body,
                 "verification": "unverified", "content_kind": "source_reference"}
        return block

    def _remember_source_reference(self, evidence_id):
        block = self._source_reference(evidence_id)
        self.blocks[block["memory_id"]] = block
        self.declared_blocks[block["memory_id"]] = deepcopy(block)

    def compact_for_request(self, payload):
        """Shorten candidate prose before byte fitting discards actual source.

        Compact only this request. The full candidates remain in the local
        checkpoint. Prefer complete original excerpts over verbose candidates;
        large excerpts retain all distinct candidate sentences and exact origins.
        Each successful call strictly reduces bytes and cannot queue rereads.
        """
        memory = payload.get("prior_source_memory") or {}
        blocks = memory.get("blocks", [])

        def wire_bytes(value):
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            return len(json.dumps(encoded, ensure_ascii=False).encode()) - 2

        best = None
        for index, block in enumerate(blocks):
            if (block.get("kind") == "source"
                    or block != self.declared_blocks.get(block.get("memory_id"))):
                continue
            elsewhere = {other["memory_id"] for other in blocks if other is not block
                         and other == self.declared_blocks.get(other.get("memory_id"))}
            references, needs_candidate = [], False
            for identifier in block["evidence_ids"]:
                reference = self._source_reference(identifier, include_source=True)
                needs_candidate = needs_candidate or reference["text"] == self._source_reference(identifier)["text"]
                if reference["memory_id"] not in elsewhere:
                    references.append(reference)
            if needs_candidate:
                # Keep complete original sentences, not a new inferred summary
                # or a cut formula/condition. Repeated sentences add no meaning.
                sentences = list(dict.fromkeys(part for part in re.findall(
                    r".*?(?:[。！？!?]|\n+|$)", block["text"], re.DOTALL) if part.strip()))
                if not sentences:
                    continue
                body = {key: deepcopy(block[key]) for key in ("evidence_ids", "origins", "kind", "round_id")}
                body["text"] = "".join(sentences)
                candidate = {"memory_id": "memory_" + _digest(body), **body,
                    "verification": "unverified", "content_kind": "model_candidate"}
                if candidate["memory_id"] in self.retracted_blocks:
                    continue
                if candidate["memory_id"] not in elsewhere:
                    references.append(candidate)
            saved = wire_bytes(block) - sum(wire_bytes(item) for item in references) - max(0, len(references) - 1)
            if saved > 0 and (best is None or saved > best[0]):
                best = (saved, index, references)
        if best is None:
            return None
        _, index, references = best
        removed = blocks[index]
        for block in references:
            self.declared_blocks[block["memory_id"]] = deepcopy(block)
        blocks[index:index + 1] = references
        return removed

    def revoke_candidates(self, *, evidence_ids=(), memory_ids=(), reason="contradicted_claim"):
        """Retract interpretations, retaining the independent source provenance.

        A semantic reviewer supplies the affected citations or block IDs. This
        operation does not infer that the underlying source is wrong, and does
        not promote a corrected model sentence into a verified fact.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("PRIOR_SOURCE_MEMORY_RETRACTION_INVALID")
        affected, requested = set(evidence_ids), set(memory_ids)
        revoked = []
        for identifier, block in list(self.blocks.items()):
            if block["kind"] == "source" or not (
                    identifier in requested or affected.intersection(block["evidence_ids"])):
                continue
            revoked.append(identifier)
            self.retracted_blocks[identifier] = reason
            self.blocks.pop(identifier)
            for evidence_id in block["evidence_ids"]:
                self.pending_reread_ids.add(evidence_id)
                self._remember_source_reference(evidence_id)
        return revoked

    def reread_pages(self, evidence_ids=None):
        """Recover exact archived source for a decisive or contradicted claim."""
        identifiers = self.pending_reread_ids if evidence_ids is None else set(evidence_ids)
        return self.archived_pages(sorted(identifiers & self.pages.keys()))

    def mark_reread(self, evidence_ids):
        """A caller invokes this only after actual request visibility is known."""
        self.pending_reread_ids.difference_update(evidence_ids)

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
        discarded_sources = set()
        for key in previous - accepted:
            discarded_sources.update(self.blocks.pop(key)["evidence_ids"])
        retained_sources = {identifier for block in self.blocks.values() for identifier in block["evidence_ids"]}
        for identifier in sorted(discarded_sources - retained_sources):
            # Replacing candidate prose must not erase the original source.
            # Re-supply a lost excerpt once on the next existing turn, without
            # keeping a superseded interpretation or opening another request.
            self._remember_source_reference(identifier)
            self.pending_reread_ids.add(identifier)
        return len(accepted - {False, None})
