"""Question-bound progress and convergence for an interactive investigation.

Only new supplied evidence, resolved obligations, or a changed coverage gap
justify further work. A differently worded answer is not proof of progress.
The retained draft is explicitly a candidate, never a new source of facts.
"""

from __future__ import annotations

import hashlib
import json
from response_language import localized_text


_LABELS = {
    "inputs": "输入来源", "conditions": "处理条件", "formula": "计算规则",
    "formulas": "计算规则", "calculation": "计算规则", "dependencies": "关联实现",
    "accumulation": "累加过程", "accumulation_step": "累加过程",
    "rounding": "舍入规则", "result_adjustments": "后续调整",
    "result_overrides": "后续覆盖", "outputs": "输出结果",
    "exceptions": "异常分支", "file_io": "文件读写", "call": "调用关系",
    "declarations": "字段定义", "file_definitions": "文件定义",
    "scheduler_binding": "调度条件", "source_availability": "源码读取状态",
}
_ENGLISH_LABELS = {
    "inputs": "Input sources", "conditions": "Processing conditions", "formula": "Calculation rules",
    "formulas": "Calculation rules", "calculation": "Calculation rules", "dependencies": "Related implementations",
    "accumulation": "Accumulation", "accumulation_step": "Accumulation", "rounding": "Rounding rules",
    "result_adjustments": "Later adjustments", "result_overrides": "Later overwrites", "outputs": "Outputs",
    "exceptions": "Exception branches", "file_io": "File access", "call": "Calls",
    "declarations": "Field definitions", "file_definitions": "File definitions",
    "scheduler_binding": "Scheduling conditions", "source_availability": "Source availability",
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def _material_entries(pages, references):
    """Pair legacy excerpt keys with source-coordinate keys for migration."""
    entries = []
    for page in pages:
        legacy = _digest(("source", page.get("relative_path"), page.get("source_sha256"),
                          page.get("start_line"), page.get("end_line"), page.get("source_text"),
                          page.get("include_chain", [])))
        binding = {key: page.get(key) for key in
                   ("relative_path", "source_sha256", "include_chain", "replacement_trace", "copy_instance")}
        # Missing and empty expansion metadata describe the same root source.
        for key in ("include_chain", "replacement_trace"):
            binding[key] = binding[key] or []
        start, end, text = page.get("start_line"), page.get("end_line"), page.get("source_text")
        lines = text.split("\n") if isinstance(text, str) else []
        if (type(start) is int and type(end) is int and start > 0
                and end - start + 1 == len(lines)):
            keys = {"source-line:" + _digest((binding, start + offset, line))
                    for offset, line in enumerate(lines)}
        else:
            # Invalid or partial coordinates must not claim coverage of unseen
            # lines. Retain the exact material identity until a valid read.
            keys = {"source-excerpt:" + _digest((binding, start, end, text))}
        entries.append((legacy, keys))
    for item in references:
        legacy = _digest(("framework", item.get("document_sha256"), item.get("reference_id"),
                          item.get("text_offset_chars"), item.get("text")))
        # Reference IDs are presentation handles; document location and text
        # establish supplied information even when those handles are rebuilt.
        value = (item.get("document_sha256"), item.get("document_name"), item.get("page"),
                 item.get("start_line"), item.get("end_line"),
                 item.get("text_offset_chars") or 0, item.get("text"))
        entries.append((legacy, {"framework:" + _digest(value)}))
    return entries


def material_keys(pages, references=()):
    """Count supplied source lines, independent of excerpt boundaries or IDs."""
    return {key for _, keys in _material_entries(pages, references) for key in keys}


def semantic_material_keys(pages, groups):
    """Stable source-bound nodes and connections, excluding tool/temporary IDs.

    Call after request fitting: omitted local analysis cannot count as supplied
    progress. Narrative findings, diagnostics and candidate prose are ignored.
    """
    def source_binding(ref):
        if (not ref.get("source_sha256") or type(ref.get("start_line")) is not int
                or type(ref.get("end_line")) is not int):
            return None
        if not any(page.get("source_text") and not page.get("span_truncated")
                and all(page.get(key) == ref.get(key) for key in ("relative_path", "source_sha256"))
                and tuple(page.get("include_chain") or ()) == tuple(ref.get("include_chain") or ())
                and tuple(page.get("replacement_trace") or ()) == tuple(ref.get("replacement_trace") or ())
                and page.get("copy_instance") == ref.get("copy_instance")
                and page["start_line"] <= ref["start_line"] <= ref["end_line"] <= page["end_line"]
                for page in pages):
            return None
        binding = {key: ref.get(key) for key in ("relative_path", "source_sha256", "start_line", "end_line",
            "start_column", "end_column", "include_chain", "replacement_trace", "copy_instance")}
        for key in ("include_chain", "replacement_trace"):
            binding[key] = list(binding[key] or ())
        return binding

    keys = set()
    for group in groups:
        analysis = group.get("semantic_analysis") or {}
        nodes = {}
        for node in analysis.get("slice_statements", []):
            refs = [source_binding(ref) for ref in node.get("source_refs", [])]
            if not refs or any(ref is None for ref in refs):
                continue
            key = _digest({"source": refs, **{name: node.get(name) for name in
                ("kind", "statement", "reads", "writes", "expressions")}})
            nodes[node.get("occurrence_id")] = key
            keys.add("semantic-node:" + key)
        for result in analysis.get("tool_results", []):
            anchor = nodes.get(result.get("occurrence_id"))
            if not anchor:
                continue
            for field in ("occurrences", "later_reads", "later_writes"):
                for identifier in result.get(field, []):
                    target = nodes.get(identifier) if isinstance(identifier, str) else None
                    if target and target != anchor:
                        # Connectivity is independent of whether the same
                        # relation was returned by a forward/backward tool.
                        keys.add("semantic-link:" + _digest(sorted((anchor, target))))
        for assignment in analysis.get("assignments", []):
            reader = nodes.get(assignment.get("statement", {}).get("occurrence_id"))
            if not reader:
                continue
            for field, definitions in assignment.get("reaching_definitions", {}).items():
                for identifier in definitions:
                    writer = nodes.get(identifier)
                    if writer:
                        keys.add("semantic-definition:" + _digest((writer, reader, field)))
    return keys


def progress_snapshot(round_number, investigation, evidence_count, *, summary=None, assessment=None,
                      answer_language="zh-CN"):
    items = [item for item in investigation.get("required_items", [])
             if item.get("status") != "NOT_APPLICABLE"]
    completed = sum(item.get("status") == "SATISFIED" for item in items)
    gaps = [item.get("kind", item.get("id", "")) for item in items
            if item.get("status") != "SATISFIED"]
    gaps.extend((assessment or {}).get("missing_aspects", []))
    labels = list(dict.fromkeys(localized_text(answer_language, _LABELS.get(item, "相关业务细节"),
        _ENGLISH_LABELS.get(item, "Relevant business details")) for item in gaps if item))
    return {"round": max(1, round_number), "evidence_count": evidence_count,
            "completed_items": completed, "total_items": len(items), "open_items": labels[:32],
            "summary": summary or (localized_text(answer_language,
                "已补充相关源码，正在核对处理条件与结果。",
                "Relevant source has been added. Checking processing conditions and outcomes.") if evidence_count
                else localized_text(answer_language, "正在根据原问题定位相关程序和业务规则。",
                    "Locating relevant programs and business rules for the original question."))}


class AnalysisContinuation:
    """A total-round-independent stopping rule, with persisted progress."""

    def __init__(self, question, saved=None):
        saved = saved or {}
        self.question = question
        self.known_material = set(saved.get("known_material", []))
        self.known_semantic = set(saved.get("known_semantic", []))
        self.known_obligations = set(saved.get("known_obligations", []))
        self.reviewed_gaps = set(saved.get("reviewed_gaps", []))
        self.idle_tool_rounds = int(saved.get("idle_tool_rounds", 0))
        self.awaiting_tools = bool(saved.get("awaiting_tools", False))
        self.stop_reason = saved.get("stop_reason")
        self.review = saved.get("review", {})
        self.draft = saved.get("draft", "")
        self.revision_requests = int(saved.get("revision_requests", 0))
        self.total_revision_requests = int(saved.get("total_revision_requests", self.revision_requests))

    def observe(self, pages, references, investigation, *, semantic_groups=()):
        entries = _material_entries(pages, references)
        material = {key for _, keys in entries for key in keys}
        # Older checkpoints stored excerpt hashes, which cannot reconstruct
        # unseen historical text. A matching supplied excerpt can safely seed
        # its line coverage without inventing a new productive tool round.
        for legacy, keys in entries:
            if legacy in self.known_material:
                self.known_material.update(keys)
        semantic = semantic_material_keys(pages, semantic_groups)
        # Evidence identities and their order may change while the underlying
        # supplied source remains identical. Material changes are tracked above;
        # only newly satisfied obligation identities count separately here.
        obligations = {_digest({key: item.get(key) for key in ("id", "kind")})
                       for item in investigation.get("required_items", [])
                       if item.get("status") == "SATISFIED"}
        progressed = bool(material - self.known_material or obligations - self.known_obligations
                          or semantic - self.known_semantic)
        if progressed:
            self.revision_requests = 0
        if self.awaiting_tools:
            self.idle_tool_rounds = 0 if progressed else self.idle_tool_rounds + 1
        self.awaiting_tools = False
        self.known_material.update(material)
        self.known_semantic.update(semantic)
        self.known_obligations.update(obligations)
        # A second different lookup may recover a naming mismatch. Repeated
        # empty searches must not produce an unbounded loop of paid requests.
        if self.idle_tool_rounds >= 2:
            self.stop_reason = "no_evidence_progress"
        return progressed

    def needs_followup(self, answer, assessment, pages, references=(), *, max_revisions=2,
                       max_total_revisions=None):
        gaps = sorted(set(assessment.get("missing_aspects", [])))
        # Reordering or repeating the same denial does not create a new gap.
        contradictions = sorted({(str(item.get("relative_path") or ""),
                                  str(item.get("assertion") or ""))
                                 for item in assessment.get("source_availability_contradictions", [])})
        signature = _digest({"material": sorted(material_keys(pages, references)),
                             "semantic": sorted(self.known_semantic),
                             "obligations": sorted(self.known_obligations),
                             "gaps": gaps, "contradictions": contradictions,
                             "status": assessment.get("status")})
        self.draft = answer
        self.review = assessment
        if signature in self.reviewed_gaps:
            self.stop_reason = "answer_review_no_progress"
            return False
        if (self.revision_requests >= max_revisions
                or (max_total_revisions is not None and self.total_revision_requests >= max_total_revisions)):
            self.stop_reason = "answer_revision_budget"
            return False
        self.reviewed_gaps.add(signature)
        self.revision_requests += 1
        self.total_revision_requests += 1
        return True

    def prompt_memory(self):
        return {"original_question_field": "question",
                "draft_is_evidence": False,
                "instruction": "保留上一轮有证据支持的结论，按原问题补全尚未解释的事项。"
                    "草稿不是事实依据；引用必须对应实际供应原文。需要资料时继续请求具体工具。",
                "unresolved_aspects": self.review.get("missing_aspects", []),
                "source_availability_contradiction": bool(self.review.get("source_availability_contradiction"))}

    def checkpoint(self):
        return {"known_material": sorted(self.known_material),
                "known_semantic": sorted(self.known_semantic),
                "known_obligations": sorted(self.known_obligations),
                "reviewed_gaps": sorted(self.reviewed_gaps), "idle_tool_rounds": self.idle_tool_rounds,
                "awaiting_tools": self.awaiting_tools, "stop_reason": self.stop_reason,
                "draft": self.draft, "review": self.review,
                "revision_requests": self.revision_requests,
                "total_revision_requests": self.total_revision_requests}
