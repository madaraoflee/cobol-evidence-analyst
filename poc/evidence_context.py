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
        requested_end = self.requested_range["end_line"]
        total = context.get("file_total_lines")
        if isinstance(total, int) and total >= 0:
            requested_end = min(requested_end, total)
        for page in context.get("pages", []):
            if page.get("relative_path") == self.relative_path and not page.get("span_truncated"):
                self.supplied_ranges.append((page["start_line"], page["end_line"]))
        cursor = self.requested_range["start_line"]
        for first, last in sorted(set(self.supplied_ranges)):
            if first > cursor:
                break
            cursor = max(cursor, last + 1)
        if cursor > requested_end:
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
    if roles & {"anchor", "result", "condition", "input", "parameter"}:
        return 0
    if roles & {"callsite", "return_processing", "declaration"}:
        return 1
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
        self.selection_trim_events = []
        self.selection_frontier = []
        self._accept_sequence = 0
        self._page_order = {}
        self._group_sequence = {}
        self.working_set = None

    def covering_id(self, identifier, selected):
        """Rebind a physical excerpt only to identical text in a complete file."""
        original = self.pages.get(identifier)
        if original is None or any(page.get("evidence_id") == identifier for page in selected):
            return identifier
        for page in selected:
            if ("complete_working_set" not in page.get("selection_reasons", ())
                    or page.get("span_truncated")
                    or any(page.get(key) != original.get(key) for key in
                           ("relative_path", "source_sha256"))
                    or tuple(page.get("include_chain", ())) != tuple(original.get("include_chain", ()))
                    or page["start_line"] > original["start_line"]
                    or page["end_line"] < original["end_line"]):
                continue
            first = original["start_line"] - page["start_line"]
            last = original["end_line"] - page["start_line"] + 1
            if "\n".join(page["source_text"].split("\n")[first:last]) == original["source_text"]:
                return page["evidence_id"]
        return identifier

    def accept(self, context, operation):
        self._accept_sequence += 1
        if operation == "complete_working_set":
            self.working_set = context.get("metadata")
            for task in self.tasks.values():
                total = next((row.get("line_count") for row in (self.working_set or {}).get("source_manifest", [])
                              if row.get("relative_path") == task.relative_path), None)
                cursor = task.next_start_line or task.requested_range["start_line"]
                matching = [page for page in context.get("pages", [])
                            if page.get("relative_path") == task.relative_path
                            and not page.get("span_truncated")
                            and (page.get("start_line", 0) <= cursor <= page.get("end_line", 0)
                                 or (total is not None and page.get("start_line") == 1
                                     and page.get("end_line") == total < cursor))]
                if matching and (self.working_set or {}).get("status") == "supplied":
                    task.advance({**context, "pages": matching, "file_total_lines": total})
        fresh = []
        for position, page in enumerate(context.get("pages", [])):
            identifier = page.get("evidence_id")
            if identifier:
                self.retrieved_ids.add(identifier)
                if identifier not in self.pages:
                    self.pages[identifier] = dict(page)
                    fresh.append(page)
                else:
                    saved = self.pages[identifier]
                    for key in ("relative_path", "start_line", "end_line", "source_sha256", "source_text", "include_chain"):
                        if key in saved and key in page and saved[key] != page[key]:
                            raise ValueError("EVIDENCE_ID_CONTENT_MISMATCH")
                    for key in ("semantic_roles", "selection_reasons", "group_ids"):
                        saved[key] = list(dict.fromkeys([*saved.get(key, []), *page.get(key, [])]))
                    if page.get("group_id"):
                        saved["group_id"] = page["group_id"]
                saved = self.pages[identifier]
                saved["group_ids"] = list(dict.fromkeys([*saved.get("group_ids", []),
                    *([saved["group_id"]] if saved.get("group_id") else []),
                    *([page["group_id"]] if page.get("group_id") else [])]))
                self._page_order[identifier] = (self._accept_sequence, position, operation)
                for group in [*page.get("group_ids", []), *([page["group_id"]] if page.get("group_id") else [])]:
                    self._group_sequence[group] = self._accept_sequence
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

    def selected_pages(self, maximum, *, evidence_groups=(), priority_targets=()):
        """Keep recent anchors and their core evidence together within whole-page budgets."""
        self.selection_trim_events, self.selection_frontier = [], []
        core_roles = {"anchor", "result", "condition", "input", "parameter"}
        core_by_group = {}
        for page in self.pages.values():
            if set(page.get("semantic_roles", ())) & core_roles:
                for group in page.get("group_ids", ()):
                    core_by_group.setdefault(group, set()).add(page["evidence_id"])
        for group in evidence_groups:
            for observation in group.observations:
                if observation.semantic_role in core_roles:
                    core_by_group.setdefault(group.group_id, set()).update(
                        ref.evidence_id for ref in observation.source_refs if ref.evidence_id in self.pages)
        core_identifiers = {identifier for members in core_by_group.values() for identifier in members}

        complete = [page for page in self.pages.values()
                    if "complete_working_set" in page.get("selection_reasons", ())]
        if sum(len(page.get("source_text", "")) for page in complete) > maximum:
            complete = []
        replacements = {identifier: self.covering_id(identifier, complete) for identifier in self.pages}
        candidates = {identifier: page for identifier, page in self.pages.items()
                      if replacements[identifier] == identifier}
        core_by_group = {group: {replacements[identifier] for identifier in identifiers}
                         for group, identifiers in core_by_group.items()}
        core_identifiers = {identifier for members in core_by_group.values() for identifier in members}

        def target_rank(page):
            for index, target in enumerate(priority_targets):
                if isinstance(target, str):
                    matched = target in {page.get("evidence_id"), page.get("relative_path")}
                else:
                    first = target.get("line", target.get("start_line", 1))
                    last = target.get("end_line", first)
                    matched = (target.get("relative_path") == page.get("relative_path") and
                               page.get("start_line", 0) <= last and page.get("end_line", 0) >= first)
                if matched:
                    return index
            return len(priority_targets)

        def rank(page):
            sequence, position, operation = self._page_order.get(page["evidence_id"], (0, 0, ""))
            recent_request = operation in {"read", "read_continuation"}
            return (target_rank(page), 0 if recent_request or page["evidence_id"] in core_identifiers else page_priority(page),
                    -sequence, position)

        pages = sorted(candidates.values(), key=rank)
        units, grouped = [], {page["evidence_id"] for page in complete}
        for page in pages:
            identifier = page["evidence_id"]
            if identifier in grouped:
                continue
            eligible = [(group, ids) for group, ids in core_by_group.items() if identifier in ids]
            members = min(eligible, key=lambda item: (min(rank(self.pages[value]) for value in item[1]),
                -self._group_sequence.get(item[0], 0)))[1] if eligible else {identifier}
            group = sorted((self.pages[item] for item in members if item not in grouped), key=rank)
            units.append(group)
            grouped.update(item["evidence_id"] for item in group)
        units.sort(key=lambda unit: min(rank(page) for page in unit))
        if complete:
            units.insert(0, complete)
        selected, used = [], 0
        for unit in units:
            if sum(len(page.get("source_text", "")) for page in unit) <= maximum - used:
                selected.extend(unit)
                used += sum(len(page.get("source_text", "")) for page in unit)
            else:
                for page in unit:
                    item = {"evidence_id": page["evidence_id"], "relative_path": page.get("relative_path"),
                            "start_line": page.get("start_line"), "end_line": page.get("end_line"),
                            "reason": "source_characters", "dropped_characters": len(page.get("source_text", "")),
                            "group_ids": sorted({*page.get("group_ids", []),
                                *(group for group, members in core_by_group.items() if page["evidence_id"] in members)})}
                    self.selection_trim_events.append(item)
                    self.selection_frontier.append({**item, "reason": "source_budget_evidence_omitted"})
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
        result = {"pages": visible, "call_chain": {"links": list(links.values())[:96],
                "omitted_links": max(0, len(links) - 96)}, "outline": list(outlines.values())[:24],
                "notices": notices[-8:], "open_reads": reads,
                "source_selection_trim_events": self.selection_trim_events[:24],
                "source_selection_trim_event_count": len(self.selection_trim_events),
                "source_selection_trim_events_omitted": max(0, len(self.selection_trim_events) - 24),
                "open_frontier": self.selection_frontier[:24],
                "open_frontier_count": len(self.selection_frontier),
                "open_frontier_omitted": max(0, len(self.selection_frontier) - 24)}
        if self.working_set is not None:
            result["working_set"] = {**self.working_set}
        return [result]

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

        working_set = bundle.get("working_set")
        if working_set is not None:
            manifest = working_set.get("source_manifest", [])
            supplied = [row["relative_path"] for row in manifest
                if any(page.get("relative_path") == row["relative_path"]
                    and page.get("source_sha256") == row["sha256"]
                    and page.get("evidence_id") == row.get("evidence_id")
                    and page.get("start_line") == 1 and page.get("end_line") == row["line_count"]
                    and len(page.get("source_text", "")) == row["source_characters"]
                    and not page.get("span_truncated") for page in pages)]
            working_set["supplied_complete_paths"] = supplied
            working_set["omitted_complete_paths"] = [row["relative_path"] for row in manifest
                                                     if row["relative_path"] not in supplied]
            working_set["physical_complete"] = (working_set.get("status") == "supplied"
                and bool(manifest) and len(supplied) == len(manifest))
            if working_set.get("status") == "supplied" and not working_set["physical_complete"]:
                working_set["transmission_status"] = "partial"
            else:
                working_set["transmission_status"] = working_set.get("status")

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
            missing = required - visible
            group["open_frontier"] = [item for item in group.get("open_frontier", [])
                                      if item.get("reason") != "required_evidence_not_visible"]
            existing = {item.get("evidence_id") for item in group.get("open_frontier", [])}
            derived = sorted(missing - existing)
            for identifier in derived[:24]:
                group.setdefault("open_frontier", []).append({"evidence_id": identifier,
                    "reason": "required_evidence_not_visible", "group_id": group.get("group_id")})
            group["missing_evidence_count"] = len(missing)
            group["omitted_visibility_frontier_count"] = max(0, len(derived) - 24)
            group["complete_text_supplied"] = bool(required) and required <= visible and not group.get("open_frontier")

    @staticmethod
    def manifest(payload):
        source = payload["source_context"][0]["pages"]
        framework = payload.get("framework_references", [])
        result = {"source_ids": [p["evidence_id"] for p in source],
                "framework_ids": [r["reference_id"] for r in framework],
                "source_characters": sum(len(p.get("source_text", "")) for p in source),
                "framework_characters": sum(len(r.get("text", "")) for r in framework)}
        if payload["source_context"][0].get("working_set") is not None:
            result["working_set"] = json.loads(json.dumps(payload["source_context"][0]["working_set"]))
        return result
