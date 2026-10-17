"""Question-bound progress and convergence for an interactive investigation.

Only new supplied evidence, resolved obligations, or a changed coverage gap
justify further work. A differently worded answer is not proof of progress.
The retained draft is explicitly a candidate, never a new source of facts.
"""

from __future__ import annotations

import hashlib
import json


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


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def material_keys(pages, references=()):
    """Changing a generated evidence ID alone does not add information."""
    values = [("source", page.get("relative_path"), page.get("source_sha256"),
               page.get("start_line"), page.get("end_line"), page.get("source_text"),
               page.get("include_chain", [])) for page in pages]
    values.extend(("framework", item.get("document_sha256"), item.get("reference_id"),
                   item.get("text_offset_chars"), item.get("text")) for item in references)
    return {_digest(value) for value in values}


def progress_snapshot(round_number, investigation, evidence_count, *, summary=None, assessment=None):
    items = [item for item in investigation.get("required_items", [])
             if item.get("status") != "NOT_APPLICABLE"]
    completed = sum(item.get("status") == "SATISFIED" for item in items)
    gaps = [item.get("kind", item.get("id", "")) for item in items
            if item.get("status") != "SATISFIED"]
    gaps.extend((assessment or {}).get("missing_aspects", []))
    labels = list(dict.fromkeys(_LABELS.get(item, "相关业务细节") for item in gaps if item))
    return {"round": max(1, round_number), "evidence_count": evidence_count,
            "completed_items": completed, "total_items": len(items), "open_items": labels[:32],
            "summary": summary or ("已补充相关源码，正在核对处理条件与结果。" if evidence_count
                                   else "正在根据原问题定位相关程序和业务规则。")}


class AnalysisContinuation:
    """A total-round-independent stopping rule, with persisted progress."""

    def __init__(self, question, saved=None):
        saved = saved or {}
        self.question = question
        self.known_material = set(saved.get("known_material", []))
        self.known_obligations = set(saved.get("known_obligations", []))
        self.reviewed_gaps = set(saved.get("reviewed_gaps", []))
        self.idle_tool_rounds = int(saved.get("idle_tool_rounds", 0))
        self.awaiting_tools = bool(saved.get("awaiting_tools", False))
        self.stop_reason = saved.get("stop_reason")
        self.review = saved.get("review", {})
        self.draft = saved.get("draft", "")

    def observe(self, pages, references, investigation):
        material = material_keys(pages, references)
        # Evidence identities and their order may change while the underlying
        # supplied source remains identical. Material changes are tracked above;
        # only newly satisfied obligation identities count separately here.
        obligations = {_digest({key: item.get(key) for key in ("id", "kind")})
                       for item in investigation.get("required_items", [])
                       if item.get("status") == "SATISFIED"}
        progressed = bool(material - self.known_material or obligations - self.known_obligations)
        if self.awaiting_tools:
            self.idle_tool_rounds = 0 if progressed else self.idle_tool_rounds + 1
        self.awaiting_tools = False
        self.known_material.update(material)
        self.known_obligations.update(obligations)
        # A second different lookup may recover a naming mismatch. Repeated
        # empty searches must not produce an unbounded loop of paid requests.
        if self.idle_tool_rounds >= 2:
            self.stop_reason = "no_evidence_progress"
        return progressed

    def needs_followup(self, answer, assessment, pages, references=()):
        gaps = sorted(set(assessment.get("missing_aspects", [])))
        # Reordering or repeating the same denial does not create a new gap.
        contradictions = sorted({(str(item.get("relative_path") or ""),
                                  str(item.get("assertion") or ""))
                                 for item in assessment.get("source_availability_contradictions", [])})
        signature = _digest({"material": sorted(material_keys(pages, references)),
                             "gaps": gaps, "contradictions": contradictions,
                             "status": assessment.get("status")})
        self.draft = answer
        self.review = assessment
        if signature in self.reviewed_gaps:
            self.stop_reason = "answer_review_no_progress"
            return False
        self.reviewed_gaps.add(signature)
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
                "known_obligations": sorted(self.known_obligations),
                "reviewed_gaps": sorted(self.reviewed_gaps), "idle_tool_rounds": self.idle_tool_rounds,
                "awaiting_tools": self.awaiting_tools, "stop_reason": self.stop_reason,
                "draft": self.draft, "review": self.review}
