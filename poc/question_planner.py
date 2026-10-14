"""Local, source-aware question plans; model suggestions remain candidates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re


PLAN_VERSION = "business-analysis-plan/v2"
_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_$#@-])[A-Z][A-Z0-9_$#@-]*(?:\s+(?:OF|IN)\s+[A-Z][A-Z0-9_$#@-]*)*", re.I)
_HINTS = {
    "origin": r"从哪里|從哪裡|来源|來源|哪里来的|哪裡來的|\b(?:origin|source\s+of|where.+from)\b",
    "overwrite": r"最后|最後|变成|變成|没有生效|沒有生效|覆盖|覆蓋|\b(?:overwrit|final|not.+effect)",
    "impact": r"影响|影響|修改|变更|變更|\b(?:impact|affect|change)\b",
    "condition": r"为什么|為什麼|条件|條件|情况|情況|\b(?:why|condition|case|branch)\b",
    "exception": r"异常|異常|错误|錯誤|失败|失敗|\b(?:exception|error|fail)\b",
    "calculation": r"计算|計算|公式|算式|怎么算|怎麼算|\b(?:calculate|calculates|calculated|calculating|calculation|calculations|compute|computes|computed|computing|formula|formulas)\b",
    "flow": r"流程|逻辑|邏輯|处理|處理|\b(?:flow|logic|process)\b",
}
_TOOLS = {
    "origin": ("backward_slice", "explain_assignment", "trace_parameter"),
    "overwrite": ("explain_assignment", "backward_slice", "forward_slice"),
    "impact": ("forward_slice", "explain_external_effect"),
    "condition": ("backward_slice", "explain_assignment"),
    "exception": ("explain_external_effect", "trace_parameter"),
    "calculation": ("explain_assignment", "backward_slice"),
    "flow": ("backward_slice", "trace_parameter", "explain_external_effect"),
    "explanation": ("explain_assignment",),
    "declaration": ("inspect_business_context",),
}


@dataclass(frozen=True)
class AnalysisPlan:
    intents: tuple[str, ...]
    entities: tuple[str, ...]
    paths: tuple[str, ...]
    tools: tuple[str, ...]
    required_obligations: tuple[str, ...]
    focus_basis: str
    unresolved: tuple[str, ...] = ()
    version: str = PLAN_VERSION

    def to_dict(self):
        return asdict(self)


class QuestionPlanner:
    def plan(self, question, business_map, *, source_pages=(), history=(), candidate=None, entity_validator=None):
        """Facts and conversation focus determine depth even without a trigger word."""
        facts = business_map.get("rule_leads", ())
        known = {str(field).upper() for fact in facts for role in ("reads", "writes")
                 for field in fact.get(role, ())}
        entities = [token.upper() for token in _IDENTIFIER.findall(str(question)) if token.upper() in known or
                    (entity_validator and "-" in token and entity_validator(token.upper()))]
        paths = list(dict.fromkeys(business_map.get("direct_paths", ()) or
                                  business_map.get("source_identity", {}).get("direct_paths", ())))
        basis = "question_and_source"
        referring = bool(re.search(r"这个|這個|这里|這裡|刚才|剛才|前面|最后|最後|第二种|第二種|\b(?:this|that|previous|second)\b", str(question), re.I))
        if referring and not entities:
            for message in reversed(list(history)):
                state = message.get("investigation_state") or {}
                prior = state.get("analysis_plan") or (state.get("question_investigation") or {}).get("analysis_plan") or {}
                entities = [item for item in prior.get("entities", ()) if item in known or
                    (entity_validator(item) if entity_validator else not known)]
                if not entities:
                    entities = [token.upper() for token in _IDENTIFIER.findall(str(message.get("content", ""))) if token.upper() in known]
                if not paths:
                    paths = [item.get("relative_path") for item in state.get("focus_candidates", ()) if item.get("relative_path")]
                if entities or paths:
                    basis = "conversation_focus_and_source"
                    break
        if not paths:
            paths = list(dict.fromkeys(page.get("relative_path") for page in source_pages
                         if page.get("relative_path") in business_map.get("selected_paths", ())))
        intents = [name for name, pattern in _HINTS.items() if re.search(pattern, str(question), re.I)]
        declaration = bool(re.search(r"类型|類型|长度|長度|初值|初始值|\b(?:data\s+type|type\s+of|length|initial\s+value|default\s+value)\b",
                                     str(question), re.I))
        if declaration and not set(intents) & {"origin", "overwrite", "impact", "condition", "exception", "calculation"}:
            intents = ["declaration"]
        # An identified field with real assignments warrants lineage inspection
        # even if the question is only "this field?" in ordinary chat.
        if entities and not intents:
            intents = ["origin"]
        if not intents:
            intents = ["explanation"]
        tools = list(dict.fromkeys(tool for intent in intents for tool in _TOOLS[intent]))
        unresolved = []
        if referring and not entities:
            unresolved.append("conversation_entity_not_resolved")
        if not paths:
            unresolved.append("source_scope_not_resolved")
        # A proposed entity or path is accepted only after local membership checks.
        if isinstance(candidate, dict):
            entities.extend(item for item in candidate.get("entities", ()) if item in known)
            paths.extend(item for item in candidate.get("paths", ()) if item in business_map.get("selected_paths", ()))
        obligations = ["source_identity", "supplied_source"]
        for intent in intents:
            obligations.extend({"origin": ("input_definitions", "parameter_mapping"),
                "overwrite": ("reaching_definitions", "later_writes", "control_conditions"),
                "impact": ("def_use", "external_effects"),
                "condition": ("control_conditions",), "exception": ("error_return",),
                "calculation": ("assignment_expression", "input_definitions", "numeric_layout"),
                "flow": ("control_conditions", "calls", "external_effects"),
                "declaration": ("declaration_layout",)}.get(intent, ()))
        return AnalysisPlan(tuple(intents), tuple(dict.fromkeys(entities)), tuple(dict.fromkeys(paths)),
            tuple(tools), tuple(dict.fromkeys(obligations)), basis, tuple(unresolved))


class EvidencePlanner:
    def organize(self, plan, candidates):
        order = {kind: index for index, kind in enumerate(("formula", "business_steps", "result_adjustments",
                    "conditions", "inputs", "dependencies", "file_io", "file_definitions", "dds_definitions"))}
        return sorted(candidates, key=lambda item: (order.get(item.get("kind"), len(order)),
                      item.get("relative_path", ""), item.get("start_line", 0), item.get("statement", "")))
