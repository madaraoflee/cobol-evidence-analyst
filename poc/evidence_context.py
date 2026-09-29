"""Question-local evidence pool and final request visibility accounting."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json


@dataclass(frozen=True)
class SourceRef:
    origin_kind: str
    original_relative_path: str
    source_sha256: str
    start_line: int
    end_line: int
    excerpt_sha256: str
    evidence_id: str
    include_chain: tuple = ()


@dataclass
class Observation:
    id: str
    semantic_role: str
    source_refs: list[SourceRef]
    field_ids: list[str] = field(default_factory=list)
    callsite_id: str | None = None
    context_id: str | None = None
    related_observation_ids: list[str] = field(default_factory=list)
    interpretation_basis: str = "source_observation"


@dataclass
class EvidenceGroup:
    group_id: str
    anchor: dict
    purpose: str
    observations: list[Observation] = field(default_factory=list)
    required_locations: list[dict] = field(default_factory=list)
    supplied_locations: list[dict] = field(default_factory=list)
    open_frontier: list[dict] = field(default_factory=list)


@dataclass
class ReadTask:
    task_id: str
    relative_path: str
    requested_range: dict
    supplied_ranges: list[tuple[int, int]] = field(default_factory=list)
    next_start_line: int | None = None
    reason: str = "requested_read"
    group_id: str | None = None
    state: str = "open"
    material_to_question: bool = True

    def advance(self, context):
        previous = self.next_start_line or self.requested_range["start_line"]
        for page in context.get("pages", []):
            if page.get("relative_path") == self.relative_path and not page.get("span_truncated"):
                self.supplied_ranges.append((page["start_line"], page["end_line"]))
        cursor = self.requested_range["start_line"]
        for first, last in sorted(set(self.supplied_ranges)):
            if first > cursor:
                break
            cursor = max(cursor, last + 1)
        if cursor > self.requested_range["end_line"]:
            self.state, self.next_start_line = "complete", None
        elif cursor > previous:
            self.next_start_line = cursor
        else:
            self.state = "stalled"


@dataclass
class InvestigationState:
    question: str
    focus_candidates: list[dict] = field(default_factory=list)
    active_groups: list[EvidenceGroup] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    open_tasks: list[ReadTask] = field(default_factory=list)
    completed_action_fingerprints: set[str] = field(default_factory=set)
    draft_answer: str | None = None
    draft_round_id: str | None = None
    revision_attempted: bool = False
    source_manifest: list[dict] = field(default_factory=list)


def page_priority(page):
    roles = set(page.get("semantic_roles", ()))
    reasons = set(page.get("selection_reasons", ()))
    if roles & {"result", "condition", "input", "callsite", "return_processing", "declaration"}:
        return 0
    if reasons & {"agent_requested_read", "business_rule", "conversation_context"}:
        return 1
    if "question_match" in reasons:
        return 2
    return 3


class EvidenceContext:
    def __init__(self):
        self.pages = {}
        self.contexts = []
        self.retrieved_ids = set()
        self.sent_any_round_ids = set()
        self.tasks = {}

    def accept(self, context, operation):
        fresh = []
        for page in context.get("pages", []):
            identifier = page.get("evidence_id")
            if identifier:
                self.retrieved_ids.add(identifier)
                if identifier not in self.pages:
                    self.pages[identifier] = page
                    fresh.append(page)
        self.contexts.append({"call_chain": context.get("call_chain", {}),
                              "outline": context.get("outline", []),
                              "notice": context.get("notice"),
                              "read_unavailable": context.get("read_unavailable")})
        requested = context.get("requested_range")
        if requested:
            key = json.dumps(requested, sort_keys=True)
            task = next((item for item in self.tasks.values()
                if item.state == "open" and item.relative_path == requested["relative_path"]
                and item.requested_range["start_line"] <= requested["start_line"]
                and item.requested_range["end_line"] >= requested["end_line"]), None)
            if task is None:
                task = self.tasks.setdefault(key, ReadTask(
                    "read_" + hashlib.sha256(key.encode()).hexdigest()[:16],
                    requested["relative_path"], requested,
                    next_start_line=requested["start_line"]))
            task.advance(context)
        return fresh

    def selected_pages(self, maximum):
        pages = sorted(self.pages.values(), key=lambda page: (page_priority(page), page.get("relative_path", ""), page.get("start_line", 0)))
        selected, used = [], 0
        for page in pages:
            size = len(page.get("source_text", ""))
            if size <= maximum - used:
                selected.append(page)
                used += size
        identifiers = {page["evidence_id"] for page in selected}
        return [page for identifier, page in self.pages.items() if identifier in identifiers]

    def bundle(self, selected):
        visible = list(selected)
        links, outlines, notices = {}, {}, []
        for context in self.contexts:
            for link in context.get("call_chain", {}).get("links", []):
                item = {key: value for key, value in link.items() if key != "evidence_id"}
                caller = [p["evidence_id"] for p in visible if p.get("relative_path") == item.get("caller_path")
                          and p["start_line"] <= item.get("caller_start_line", -1)
                          and p["end_line"] >= item.get("caller_end_line", 10**12)]
                target = [p["evidence_id"] for p in visible if p.get("relative_path") == item.get("target_path")
                          and p["start_line"] <= item.get("target_start_line", -1) <= p["end_line"]]
                item.update(caller_evidence_ids=caller, target_evidence_ids=target,
                            requires_source_read=not bool(caller))
                links[item.get("relation_id") or json.dumps(item, sort_keys=True)] = item
            for outline in context.get("outline", []):
                key = (outline.get("relative_path"), json.dumps(outline.get("items", outline), sort_keys=True))
                outlines[key] = outline
            if context.get("notice") or context.get("read_unavailable"):
                notices.append(context)
        reads = [{"relative_path": task.relative_path, "requested_range": task.requested_range,
                  "supplied_ranges": task.supplied_ranges, "next_start_line": task.next_start_line,
                  "range_complete": task.state == "complete", "state": task.state}
                 for task in self.tasks.values()]
        return [{"pages": visible, "call_chain": {"links": list(links.values())[:96],
                "omitted_links": max(0, len(links) - 96)}, "outline": list(outlines.values())[:24],
                "notices": notices[-8:], "open_reads": reads}]

    @staticmethod
    def reconcile_payload(payload):
        """Recalculate navigation claims from the excerpts still in this request."""
        bundle = payload["source_context"][0]
        pages = bundle["pages"]
        visible = {page["evidence_id"] for page in pages if page.get("evidence_id")}

        def covers(path, first, last):
            if not path or type(first) is not int or type(last) is not int:
                return []
            ranges = sorted((page["start_line"], page["end_line"], page.get("evidence_id"))
                            for page in pages if page.get("relative_path") == path
                            and not page.get("span_truncated")
                            and type(page.get("start_line")) is int and type(page.get("end_line")) is int)
            chosen, cursor = [], first
            for start, end, identifier in ranges:
                if start > cursor:
                    break
                if end >= cursor:
                    if identifier:
                        chosen.append(identifier)
                    cursor = max(cursor, end + 1)
                if cursor > last:
                    return chosen
            return []

        for link in bundle.get("call_chain", {}).get("links", []):
            caller = covers(link.get("caller_path"), link.get("caller_start_line"),
                            link.get("caller_end_line"))
            target = covers(link.get("target_path"), link.get("target_start_line"),
                            link.get("target_start_line"))
            link.update(caller_evidence_ids=caller, target_evidence_ids=target,
                        requires_source_read=not bool(caller))
        for outline in bundle.get("outline", []):
            path = outline.get("relative_path")
            outline["retrieved_ranges"] = [{"start_line": p["start_line"], "end_line": p["end_line"]}
                for p in pages if p.get("relative_path") == path]
            for unit in outline.get("units", []):
                unit["complete_text_supplied"] = bool(covers(path, unit.get("start_line"),
                                                               unit.get("end_line")))
        for group in payload.get("evidence_groups", []):
            required = set(group.get("required_evidence_ids", []))
            supplied = sorted(required & visible)
            group["visible_evidence_ids"] = supplied
            group["complete_text_supplied"] = bool(required) and required <= visible and not group.get("open_frontier")

    @staticmethod
    def manifest(payload):
        source = payload["source_context"][0]["pages"]
        framework = payload.get("framework_references", [])
        return {"source_ids": [p["evidence_id"] for p in source],
                "framework_ids": [r["reference_id"] for r in framework],
                "source_characters": sum(len(p.get("source_text", "")) for p in source),
                "framework_characters": sum(len(r.get("text", "")) for r in framework)}
